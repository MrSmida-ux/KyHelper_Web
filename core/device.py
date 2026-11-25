"""Device：任务看到的「设备」。网页版底下换成了 CDP 输入。

接口跟模拟器版一模一样（wait/tap_img/wait_stable/ensure_home 那一套），
所以 tasks/ 里的任务代码一行都不用改 —— 这是这次移植最省事的一点。

坐标约定（缩放自适应的核心）：
    任务里写的坐标、模板匹配出的坐标，**永远**是模板基准 2560×1440。
    tap() 负责把它换算成浏览器里的 CSS 坐标：
        css = geo.origin + ref × (游戏区实际尺寸 / 2560×1440)
    你把浏览器窗口拉大拉小、换成 1600×900 视口，任务代码都不用动。
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable

import cv2
import numpy as np

from .capture import Capture
from .log import save_shot
from .vision import Match, Matcher

REF_W, REF_H = 2560, 1440


class NotFound(RuntimeError):
    pass


class Cancelled(RuntimeError):
    """用户按了停止。"""


class Skipped(RuntimeError):
    """这次先不做（不是报错）。"""


class Device:
    def __init__(self, session, capture: Capture, matcher: Matcher, cfg: dict, log):
        self.s = session
        self.cap = capture
        self.matcher = matcher
        self.cfg = cfg
        self.log = log

        inp = cfg.get("input", {}) or {}
        bot = cfg.get("bot", {}) or {}
        game = cfg.get("game", {}) or {}
        self.jitter = int(inp.get("tap_jitter", 3))
        self.delay_min = float(inp.get("delay_min", 0.35))
        self.delay_max = float(inp.get("delay_max", 0.85))
        self.poll = float(bot.get("poll_interval", 0.30))
        self.default_timeout = float(bot.get("default_timeout", 15))
        self.save_on_fail = bool(bot.get("save_on_fail", True))
        self.back_point = game.get("back_point") or [70, 70]
        self.key_delay = float(inp.get("key_interval", 0.05))

        self.cancel_event: threading.Event | None = None
        self.notifier: Callable[[str], None] | None = None
        self.tap_count = 0
        # 刚做完动作的帧号：下一轮识别必须拿到比它更新的画面
        self._action_seq = -1
        self._forced = False

    def notify(self, msg: str) -> None:
        self.log.warning(f"  ⚠ {msg}")
        if self.notifier is not None:
            try:
                self.notifier(msg)
            except Exception:
                pass

    # ------------------------------------------------------------ 中断
    def check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled("已停止")

    def invalidate(self) -> None:
        """让下一次识别强制重解一帧（点完/外部改动后调用）。"""
        self._forced = True

    # ------------------------------------------------------------ 画面
    def screen(self, refresh: bool = False) -> np.ndarray:
        """当前画面。

        refresh=True（或刚做完动作）时，会先等「动作之后的新帧」到手，
        避免拿着点击前的旧画面判断，把同一步点两遍。
        """
        if self._action_seq >= 0 and self.cap.seq <= self._action_seq:
            self.cap.wait_new(self._action_seq, timeout=0.35)
            self._action_seq = -1
        img = self.cap.frame(force=refresh or self._forced)
        self._forced = False
        if img is None:
            raise NotFound("还没有画面（游戏没加载出来或流断了）")
        return img

    def dump(self, tag: str) -> str:
        try:
            return save_shot(self.screen(), tag, str(self.s.root / "logs" / "fails"))
        except Exception:
            return ""

    # ------------------------------------------------------------ 识别
    def find(self, name: str, threshold: float | None = None,
             region: tuple[int, int, int, int] | None = None) -> Match | None:
        if not self.matcher.has(name):
            return None
        gray = self.cap.gray()
        if gray is None:
            return None
        return self.matcher.find_gray(gray, name, threshold, region)

    def find_all(self, name: str, threshold: float | None = None) -> list[Match]:
        if not self.matcher.has(name):
            return []
        return self.matcher.find_all(self.screen(), name, threshold)

    def exists(self, name: str, threshold: float | None = None) -> bool:
        return self.find(name, threshold) is not None

    def exists_any(self, names, threshold: float | None = None) -> bool:
        """这几个模板任意一个存在就算 True（同一按钮的简/繁两套）。"""
        for name in names:
            if self.exists(name, threshold):
                return True
        return False

    def best_any(self, names) -> tuple[str, float]:
        """报错时用：这几个模板里得分最高的那个，返回 (名字, 分数)。

        为什么需要：模板是按「录像时那种界面语言」裁的，简体和繁体字形不同
        （「全部领取」vs「全部領取」），换语言后分数会掉到 0.7~0.85，够不到门槛。
        失败时把最高分报出来，一眼就能看出是「换语言了」还是「界面变了」。
        """
        best, best_score = "", -1.0
        for name in names:
            if not self.matcher.has(name):
                continue
            # 门槛传 0：只求「最高分是多少」，不管够不够用
            m = self.find(name, 0.0)
            score = float(m.score) if m is not None else 0.0
            if score > best_score:
                best, best_score = name, score
        return best, best_score

    def red_ratio(self, region) -> float:
        """区域内「鲜红」像素占比，用来判断红点角标（数字会变，图案认不住）。"""
        img = self.screen()
        x1, y1, x2, y2 = (int(v) for v in region)
        h, w = img.shape[:2]
        sub = img[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
        if sub.size == 0:
            return 0.0
        hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0].astype(int)
        sat = hsv[:, :, 1].astype(int)
        val = hsv[:, :, 2].astype(int)
        red = ((hue <= 12) | (hue >= 166)) & (sat > 120) & (val > 110)
        return float(red.mean())

    def wait(self, name: str, timeout: float | None = None,
             threshold: float | None = None, interval: float | None = None) -> Match:
        if not self.matcher.has(name):
            raise NotFound(f"模板 [{name}] 不存在，先用录制功能做一张")
        timeout = self.default_timeout if timeout is None else timeout
        interval = self.poll if interval is None else interval
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_cancel()
            m = self.find(name, threshold)
            if m:
                self.log.info(f"  ✓ 找到 {m}")
                return m
            time.sleep(interval)
        shot = self.dump(f"timeout_{name}") if self.save_on_fail else ""
        raise NotFound(f"等待 [{name}] 超时 {timeout:.0f}s" + (f"（现场：{shot}）" if shot else ""))

    def wait_any(self, names: list[str], timeout: float | None = None,
                 threshold: float | None = None) -> tuple[str, Match]:
        existing = [n for n in names if self.matcher.has(n)]
        if not existing:
            raise NotFound(f"这些模板都不存在：{names}")
        timeout = self.default_timeout if timeout is None else timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_cancel()
            for name in existing:
                m = self.find(name, threshold)
                if m:
                    self.log.info(f"  ✓ 命中 {m}")
                    return name, m
            time.sleep(self.poll)
        shot = self.dump("timeout_any") if self.save_on_fail else ""
        raise NotFound(f"等待 {existing} 任意一个出现超时 {timeout:.0f}s"
                       + (f"（现场：{shot}）" if shot else ""))

    # ------------------------------------------------------------ 动作
    def _pause(self) -> None:
        self.check_cancel()
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _dispatch(self, kind: str, x: float, y: float, **kw) -> None:
        params = {"type": kind, "x": float(x), "y": float(y)}
        params.update(kw)
        self.s.cdp.call("Input.dispatchMouseEvent", params, timeout=6)

    def tap(self, x: int, y: int, jitter: bool = True) -> None:
        """点一下。(x, y) 是模板基准坐标。"""
        if jitter and self.jitter > 0:
            x += random.randint(-self.jitter, self.jitter)
            y += random.randint(-self.jitter, self.jitter)
        cx, cy = self.s.to_css(x, y)
        self.check_cancel()
        try:
            self._dispatch("mouseMoved", cx, cy, buttons=0)
            self._dispatch("mousePressed", cx, cy, button="left", buttons=1, clickCount=1)
            self._dispatch("mouseReleased", cx, cy, button="left", buttons=0, clickCount=1)
            self.tap_count += 1
        except Exception as e:
            raise NotFound(f"点击失败：{e}") from e
        self._action_seq = self.cap.seq
        self._pause()

    def move_to(self, x: int, y: int) -> None:
        """只把指针挪过去，不按键。

        用途：云游戏页面偶尔会把上一次的「按下」状态留住，同一个位置再点就没反应。
        挪一下指针能让它自己复位，比补发按键安全。
        """
        cx, cy = self.s.to_css(x, y)
        try:
            self._dispatch("mouseMoved", cx, cy, buttons=0)
        except Exception:
            pass

    def tap_match(self, m: Match) -> None:
        self.tap(m.cx, m.cy)

    def tap_img(self, name: str, timeout: float | None = None,
                threshold: float | None = None) -> bool:
        try:
            m = self.wait(name, timeout if timeout is not None else 5, threshold)
        except NotFound:
            return False
        self.log.info(f"  → 点击 [{name}] @({m.cx},{m.cy})")
        self.tap_match(m)
        return True

    def tap_any(self, names, timeout: float | None = None,
                threshold: float | None = None) -> bool:
        """这几个模板里点最先命中的那个（同一个按钮的简/繁两套）。

        为什么要有：模板是照「录制那一刻的界面语言」裁的，游戏切语言后同一句
        话的笔画整片不同（「全部领取」/「全部領取」「參加」/「参加」），分数会
        掉到 0.7~0.85、够不到 0.85 的门槛。每个按钮各留两套，靠这里挑当前
        语言认得出的那套点，用户就不必因为我们猜错语言而重录模板。
        """
        if isinstance(names, str):
            names = [names]
        try:
            name, m = self.wait_any(list(names),
                                    timeout if timeout is not None else 5, threshold)
        except NotFound:
            return False
        self.log.info(f"  → 点击 [{name}] @({m.cx},{m.cy})")
        self.tap_match(m)
        return True

    def tap_until(self, click: str, expect: str, tries: int = 5, timeout: float = 10) -> Match:
        """连点 [click] 直到 [expect] 出现（点一次没生效就再来）。"""
        for i in range(tries):
            if self.exists(click):
                self.tap_img(click, timeout=2)
            try:
                return self.wait(expect, timeout=timeout)
            except NotFound:
                self.log.warning(f"  第 {i + 1}/{tries} 次点击未生效，重试")
        raise NotFound(f"连点 {tries} 次 [{click}] 仍未出现 [{expect}]")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> None:
        """拖动。网页版没有「滑动」原语，只能自己补中间的 mouseMoved。"""
        ax, ay = self.s.to_css(x1, y1)
        bx, by = self.s.to_css(x2, y2)
        steps = max(4, int(duration_ms / 16))
        self.check_cancel()
        self._dispatch("mouseMoved", ax, ay, buttons=0)
        self._dispatch("mousePressed", ax, ay, button="left", buttons=1, clickCount=1)
        for i in range(1, steps + 1):
            t = i / steps
            # 先快后慢，模拟真手指
            e = 1 - (1 - t) ** 2
            self._dispatch("mouseMoved", ax + (bx - ax) * e, ay + (by - ay) * e, buttons=1)
            time.sleep(max(0.0, duration_ms / 1000.0 / steps))
        self._dispatch("mouseReleased", bx, by, button="left", buttons=0, clickCount=1)
        self._action_seq = self.cap.seq
        self._pause()

    def scroll(self, direction: str = "down", ratio: float = 0.4,
               x: int | None = None) -> None:
        """在画面中部纵向拖动。down = 内容向上翻。"""
        w, h = REF_W, REF_H
        cx = int(x) if x is not None else w // 2
        dist = int(h * ratio / 2)
        if direction == "down":
            self.swipe(cx, h // 2 + dist, cx, h // 2 - dist)
        else:
            self.swipe(cx, h // 2 - dist, cx, h // 2 + dist)

    def key(self, code: int) -> None:
        """按键。网页版只保留 Escape（27）有用，其它按 Android 键码一并忽略。"""
        if code != 4:
            self.log.info(f"  （网页版不支持键码 {code}，忽略）")
            return
        self._escape()

    def _escape(self) -> None:
        try:
            for kind in ("keyDown", "keyUp"):
                self.s.cdp.call("Input.dispatchKeyEvent", {
                    "type": kind, "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27,
                }, timeout=5)
        except Exception as e:
            self.log.warning(f"  发 Escape 失败：{e}")

    BACK_TEMPLATES = ("btn_back", "btn_back_relic", "btn_story_back")

    def back(self) -> None:
        """返回上一级。

        模拟器上是「安卓返回键」，网页版没有这个东西，只能按顺序试：
          1) 界面上的返回箭头模板（有就点，最可靠）
          2) Escape（不少 WebGL 游戏认）
          3) 左上角固定点（config: game.back_point）
        三个都不行的话，任务里应该改用底部「首页」标签兜底 —— flows.ensure_home
        本来就是优先点首页的。
        """
        for name in self.BACK_TEMPLATES:
            if self.matcher.has(name):
                m = self.find(name, 0.86)
                if m is not None:
                    self.log.info(f"  返回：点 [{name}]")
                    self.tap(m.cx, m.cy)
                    return
        self._escape()
        time.sleep(0.15)
        self.check_cancel()
        x, y = self.back_point[0], self.back_point[1]
        self.log.info(f"  返回：点左上角 ({x},{y})")
        self.tap(int(x), int(y))

    # ------------------------------------------------------------ 状态
    @staticmethod
    def _signature(img: np.ndarray) -> np.ndarray:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return cv2.resize(g, (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32)

    def wait_stable(self, timeout: float = 12, diff_threshold: float = 8.0,
                    changed_ratio: float = 0.08, stable_rounds: int = 2,
                    interval: float = 0.30) -> bool:
        """等画面基本不再变化（切场景/加载结束时用）。

        screencast 只在画面变化时推帧，所以「没有新帧」本身就是一个很强的
        「稳定」信号：等 2 轮没有新帧，直接判稳定，比逐帧比对更省。
        """
        prev_seq = self.cap.seq
        prev = self._signature(self.screen())
        stable = 0
        t_start = time.time()
        deadline = t_start + timeout
        while time.time() < deadline:
            self.check_cancel()
            time.sleep(interval)
            if self.cap.seq == prev_seq:
                # 刚进这个函数时画面可能「还没来得及变」（比如点了按钮、转场还没开始），
                # 先给 0.8s 缓冲，别一进来就判稳定。
                if time.time() - t_start < 0.8:
                    continue
                stable += 1
                if stable >= stable_rounds:
                    return True
                continue
            prev_seq = self.cap.seq
            cur = self._signature(self.screen())
            changed = float((np.abs(cur - prev) > diff_threshold).mean())
            if changed < changed_ratio:
                stable += 1
                if stable >= stable_rounds:
                    return True
            else:
                stable = 0
            prev = cur
        self.log.warning(f"  画面 {timeout:.0f}s 内没稳定下来，先按当前状态继续")
        return False

    def recover_home(self, home_tpl: str = "home", tries: int = 6) -> bool:
        self.log.warning("  执行恢复：尝试回到主页")
        for _ in range(tries):
            if self.exists(home_tpl):
                self.log.info("  ✓ 已回到主页")
                return True
            self.back()
            time.sleep(0.6)
        return self.exists(home_tpl)
