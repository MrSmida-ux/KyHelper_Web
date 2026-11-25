"""画面来源：Chrome 的 Page.startScreencast。

为什么不用 Page.captureScreenshot：实测 2560×1440 下单张要 0.4~1.5 秒
（强制重排+编码），一个决策周期就卡死了。screencast 走的是合成器，
GPU 出帧后直接推 JPEG，实测 48fps、单帧 20~40ms 就到手，而且**没有请求往返**，
识别永远是「最新一帧」。

代价是它只在画面变化时推帧（静态界面不推）—— 这对我们反而是好事：
静态界面没有新帧，但上一帧依然有效，识别结果不变。

线程模型：
    pump 线程       收帧 → 存 JPEG（不解码！）→ 通知
    识别线程        需要时才解码（按帧号缓存），所以预览不需要解码成本
    预览线程        把最新帧降采样成小图给界面，只影响显示
"""
from __future__ import annotations

import threading
import time

import cv2
import numpy as np

from .browser import GameSession

# 帧的尺寸上限 = 模板基准。screencast 按这个封顶：
# 2x 屏出的是 2854×1650，封到 2560 宽正好一次降采样到识别要的尺寸（省解码成本、
# 也更清楚）；比封到窗口的 CSS 尺寸（1427）再往上采样强得多。
REF_W, REF_H = 2560, 1440

# Chrome 的 screencast **没有帧率参数**，只能 everyNthFrame 隔帧丢。
# 合成器实测推到 ~94fps（2560×1440 超过屏幕分辨率，可能是合成器的刷新节奏），
# 所以「想要 N fps」≈ 每 round(94/N) 帧取 1 帧。
_SRC_FPS = 94.0


def _cfg_fps(cfg: dict, key: str, default: float) -> float:
    try:
        v = float(cfg.get(key) or 0)
        return v if v > 0 else default
    except Exception:
        return default


class Capture:
    def __init__(self, session: GameSession, log, preview_width: int = 1024):
        self.s = session
        self.log = log
        self.preview_width = max(320, int(preview_width))

        self.seq = 0                      # 收到过多少帧
        self.frames = 0                   # 累计帧数（诊断）
        self.fps = 0.0
        self._stamps: list[float] = []

        self._jpeg: bytes = b""
        self._jpeg_seq = 0
        self._lock = threading.Condition()
        self._running = False
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self.error = ""

        # 解码缓存：同一帧号只解一次（一轮决策里多次识别共用）
        self._gray: np.ndarray | None = None
        self._gray_seq = -1
        self._img: np.ndarray | None = None
        self._img_seq = -1
        self._dec_at = 0.0
        self.ttl = 0.12                   # 解出来的画面在这个时间内复用
        self._dlock = threading.Lock()

        # 预览小图
        self._pjpeg = b""
        self._pseq = -1
        self._pstop = threading.Event()
        self._pthread: threading.Thread | None = None
        self.viewers = 0                  # 有界面在看才做预览图，省 CPU

        # ---- 帧率模式（可在运行时切换） ----
        # high = 预览开/录制时：全速推帧 + 接收端限帧到 60，画面流畅
        # low  = 预览关时：源端隔帧到 ~20，省 CPU（识别够用）
        cfg = (self.s.cfg.get("capture") or {}) if getattr(self.s, "cfg", None) else {}
        self.fps_high = _cfg_fps(cfg, "fps_high", 60.0)
        self.fps_low = _cfg_fps(cfg, "fps_low", 20.0)
        # 兼容旧配置：只填了 fps 时用它当 low 档，high 仍给 60
        legacy = _cfg_fps(cfg, "fps", 0.0)
        if legacy > 0 and "fps_low" not in cfg and "fps_high" not in cfg:
            self.fps_low = legacy
        self.fps_mode = "auto"            # auto / high / low，由 Session 同步
        self._active_fps = self.fps_low   # 当前档生效的目标帧率
        self._recv_interval = 0.0         # 接收端限帧间隔（秒），0=不限
        self._last_pass = 0.0             # 上一次放行给识别/预览的时间

    # ------------------------------------------------------------ 参数
    def _quality(self) -> int:
        cfg = (self.s.cfg.get("capture") or {}) if getattr(self.s, "cfg", None) else {}
        try:
            return max(20, min(100, int(cfg.get("quality") or 62)))
        except Exception:
            return 62

    def set_fps(self, target: float) -> None:
        """切换当前目标帧率。

        Chrome 的 screencast 只有 everyNthFrame 整数隔帧（94/1=94、/2=47、/3=31），
        凑不出 60。所以 high 档让源端全速推（nth=1），在**接收端按时间戳限帧**到
        精确 60；low 档直接用源端隔帧（nth≈94/20=5）把编码/传输的 CPU 也省掉。

        只更新内部参数并重启画面流；调用方决定何时切换。
        """
        target = max(1.0, min(120.0, float(target or self.fps_low)))
        self._active_fps = target
        if target >= 45:
            nth = 1                       # 全速推，接收端限帧
            self._recv_interval = 1.0 / target
        else:
            nth = max(1, min(12, int(round(_SRC_FPS / target))))
            self._recv_interval = 0.0     # 源端已隔帧，无需接收端再限
        self._nth = nth
        self._last_pass = 0.0

    def stream_params(self) -> dict:
        """画面流参数。nth 由 set_fps() 按当前档位算好。"""
        return {
            "format": "jpeg", "quality": self._quality(),
            "maxWidth": REF_W, "maxHeight": REF_H,
            "everyNthFrame": getattr(self, "_nth", 1),
        }

    def target_fps(self) -> float:
        """当前档位的目标帧率（给界面显示）。"""
        return self._active_fps

    # ------------------------------------------------------------ 启停
    def start(self) -> bool:
        if self._running:
            return True
        if self.s.cdp is None:
            return False
        try:
            self.s.cdp.call("Page.startScreencast", self.stream_params(), timeout=10)
        except Exception as e:
            self.error = str(e)
            self.log.warning(f"开画面流失败：{e}")
            return False
        self._running = True
        self._started_at = time.time()
        self._pstop.clear()          # 重启时必须清掉，否则预览线程一起来就退出
        self._thread = threading.Thread(target=self._pump, name="screencast", daemon=True)
        self._thread.start()
        if self._pthread is None or not self._pthread.is_alive():
            self._pthread = threading.Thread(target=self._preview_loop, name="preview", daemon=True)
            self._pthread.start()
        return True

    def stop(self) -> None:
        self._running = False
        self._pstop.set()
        if self.s.cdp is not None:
            try:
                self.s.cdp.call("Page.stopScreencast", timeout=3)
            except Exception:
                pass

    def restart(self) -> bool:
        self.stop()
        time.sleep(0.2)
        return self.start()

    # ------------------------------------------------------------ 帧泵
    def _pump(self) -> None:
        cdp = self.s.cdp
        if cdp is None:
            return
        while self._running:
            try:
                ev = cdp.wait_event("Page.screencastFrame", timeout=1.0)
            except Exception as e:
                self.error = str(e)
                break
            if not ev:
                if cdp.error:
                    self.error = cdp.error
                    break
                continue
            d = ev.get("params", {}) or {}
            sid = d.get("sessionId")
            try:
                cdp.call("Page.screencastFrameAck", {"sessionId": sid}, timeout=5)
            except Exception:
                pass
            data = d.get("data") or ""
            if not data:
                continue
            import base64
            try:
                jpg = base64.b64decode(data)
            except Exception:
                continue
            now = time.time()
            # high 档：源端全速推（nth=1），这里按时间戳限到精确 60fps。
            # 必须放在 ACK 之后（上面已 ack），否则丢帧会让 screencast 卡住。
            # 只丢「太密」的帧，识别/预览始终拿最新放行的那帧。
            if self._recv_interval > 0:
                if now - self._last_pass < self._recv_interval * 0.92:
                    continue
                self._last_pass = now
            with self._lock:
                self._jpeg = jpg
                self.seq += 1
                self._jpeg_seq = self.seq
                self._stamps.append(now)
                if len(self._stamps) > 60:
                    del self._stamps[: len(self._stamps) - 60]
                self._lock.notify_all()
        self._running = False

    # ------------------------------------------------------------ 取帧
    def latest(self) -> tuple[bytes, int] | None:
        with self._lock:
            if not self._jpeg:
                return None
            return self._jpeg, self._jpeg_seq

    def wait_new(self, after: int, timeout: float = 3.0) -> tuple[bytes, int] | None:
        deadline = time.time() + timeout
        with self._lock:
            while self._jpeg_seq <= after or not self._jpeg:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._lock.wait(left)
            return self._jpeg, self._jpeg_seq

    def _decode(self, jpg: bytes, gray: bool) -> np.ndarray | None:
        buf = np.frombuffer(jpg, dtype=np.uint8)
        flag = cv2.IMREAD_GRAYSCALE if gray else cv2.IMREAD_COLOR
        try:
            return cv2.imdecode(buf, flag)
        except Exception:
            return None

    def _fit(self, img: np.ndarray) -> np.ndarray:
        """把画面裁到游戏区（居中的 16:9 舞台）并归一到模板基准 2560×1440。

        视口现在等于窗口，所以帧是「整个窗口」的 —— 上下黑边、两侧黑边都在里面。
        这里按 geo（游戏区在 CSS 坐标里的落点）裁掉黑边，再缩放到模板尺寸。
        kx/ky = 帧像素 / CSS 像素：2x 屏是 2，screencast 封顶后可能略小，
        一律按实际帧宽算（这就是切窗口大小后坐标还能对上的原因）。
        """
        g = self.s.geo
        h, w = img.shape[:2]
        vw, vh = float(self.s.vw), float(self.s.vh)
        if vw <= 0 or vh <= 0:
            return img
        kx, ky = w / vw, h / vh                 # 帧像素 / CSS 像素

        x0 = max(0, int(round(g["x"] * kx)))
        y0 = max(0, int(round(g["y"] * ky)))
        x1 = min(w, int(round((g["x"] + g["w"]) * kx)))
        y1 = min(h, int(round((g["y"] + g["h"]) * ky)))
        if x1 - x0 < 32 or y1 - y0 < 32:
            return img
        if (x0, y0, x1, y1) != (0, 0, w, h):
            img = img[y0:y1, x0:x1]
        if img.shape[1] != 2560 or img.shape[0] != 1440:
            interp = cv2.INTER_AREA if img.shape[1] > 2560 else cv2.INTER_LINEAR
            img = cv2.resize(img, (2560, 1440), interpolation=interp)
        return img

    def frame(self, force: bool = False) -> np.ndarray | None:
        """最新一帧（BGR，2560×1440，等价于模拟器截图）。

        screencast 一秒推 40~50 帧，每个决策周期都重新解一遍 3.7MP JPEG
        纯属浪费（一次 20~30ms）。这里做两层复用：
          · 同一帧号只解一次（一轮决策里 N 次识别共用一张图）
          · 解出来的图在 `ttl` 秒内一直可用（决策循环 3~4Hz，没必要跟着 50fps 解）
        动作类操作会让 Device 传 force=True，保证看到的是点完之后的新画面。
        """
        with self._lock:
            jpg, seq = self._jpeg, self._jpeg_seq
        if not jpg:
            return None
        if not force:
            with self._dlock:
                if self._img is not None and (time.time() - self._dec_at) < self.ttl:
                    return self._img
        img = self._decode(jpg, gray=False)
        if img is None:
            return None
        img = self._fit(img)
        with self._dlock:
            self._img, self._img_seq = img, seq
            self._dec_at = time.time()
            self._gray = None
            self._gray_seq = -1
        return img

    def gray(self, force: bool = False) -> np.ndarray | None:
        """最新一帧的灰度图。匹配只用灰度，缓存住一轮决策里反复用。"""
        img = self.frame(force=force)
        if img is None:
            return None
        with self._dlock:
            seq = self._img_seq
            if self._gray is not None and self._gray_seq == seq:
                return self._gray
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        with self._dlock:
            self._gray, self._gray_seq = g, seq
        return g

    def full_jpeg(self) -> bytes:
        with self._lock:
            return self._jpeg

    def age(self) -> float:
        with self._lock:
            t = self._stamps[-1] if self._stamps else self._started_at
        return time.time() - t if t else 999.0

    # ------------------------------------------------------------ 预览
    def _preview_loop(self) -> None:
        while not self._pstop.is_set():
            try:
                if self.viewers <= 0:
                    time.sleep(0.3)
                    continue
                with self._lock:
                    jpg, seq = self._jpeg, self._jpeg_seq
                if not jpg or seq == self._pseq:
                    self._pstop.wait(0.05)
                    continue
                # 只要 1/2 分辨率的解码：预览用不着全分辨率，快 4 倍
                buf = np.frombuffer(jpg, dtype=np.uint8)
                img = cv2.imdecode(buf, cv2.IMREAD_REDUCED_COLOR_2)
                if img is None:
                    self._pstop.wait(0.05)
                    continue
                h, w = img.shape[:2]
                if w > self.preview_width:
                    nh = max(1, round(h * self.preview_width / w))
                    img = cv2.resize(img, (self.preview_width, nh), interpolation=cv2.INTER_AREA)
                ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 58])
                if ok:
                    with self._lock:
                        self._pjpeg = enc.tobytes()
                        self._pseq = seq
                        self._lock.notify_all()
                self._pstop.wait(0.02)
            except Exception as e:      # 预览出问题不能把线程带走
                self.error = f"预览：{e}"
                self._pstop.wait(0.5)

    def preview_wait(self, after: int, timeout: float = 3.0) -> tuple[bytes, int] | None:
        deadline = time.time() + timeout
        with self._lock:
            while self._pseq <= after or not self._pjpeg:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._lock.wait(left)
            return self._pjpeg, self._pseq

    def fps_now(self) -> float:
        with self._lock:
            s = list(self._stamps)
        if len(s) < 3:
            return 0.0
        gaps = [b - a for a, b in zip(s, s[1:]) if b - a > 0.001]
        if not gaps:
            return 0.0
        return round(len(gaps) / sum(gaps), 1)
