"""本地 Web UI 服务（网页版）。

架构和模拟器版一致：一个本地 HTTP 服务 + 浏览器里的控制面板。
区别在于「被控对象」从 MuMu 换成了 Chrome 里的云游戏窗口。

    python run.py            # 起服务，自动开浏览器、自动进游戏
    python run.py --no-ui    # 只跑命令行

接口（前端在 web/）：
  GET  /api/state            连接状态、任务清单、执行状态
  GET  /api/log?since=N      增量日志
  GET  /api/stream           MJPEG 画面（默认关闭，可在界面上开）
  GET  /api/frame            单帧 JPEG（录制/截图用）
  POST /api/run|stop         执行 / 停止
  POST /api/tap              手动点击（归一化坐标）
  POST /api/preview          开关画面预览
  POST /api/browser/restart  重开浏览器窗口（游戏卡死/掉登录时用）
  POST /api/login            粘贴 erolabsjwt 直接登录
  POST /api/login/import     借用本机 Chrome 的登录态（拷 cookie 后重开浏览器）
  POST /api/rec/*            录制：start / tap / cmd / undo / save / cancel
  POST /api/tasks/*          顺序 / 选项 / 勾选 / 改名 / 删除
  POST /api/update/check     立刻查一次 GitHub 上的新版本
  POST /api/update/apply     下载并换包（下完自动退出、由更新脚本重启）
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

ROOT_FILE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_FILE))

from core import Cancelled, NotFound, bootstrap, chrome_profile, paths, recipe, update  # noqa: E402
from core.log import save_shot, setup  # noqa: E402
from core.recorder import Recording  # noqa: E402
from tasks import load_jobs, run_jobs, save_order  # noqa: E402
from tasks.registry import (  # noqa: E402
    load_options, load_selection, order_path, resolved_options, save_options,
    save_selection,
)

ROOT = paths.app_dir()
RES = paths.res_dir()
WEB_DIR = RES / "web"
REF_W, REF_H = 2560, 1440
# 最近一次收到请求的时间：自动更新重启后靠它判断「面板是不是还开着」，
# 开着就不再弹第二个窗口（旧面板自己会刷新）。
LAST_HIT = [0.0]


# ---------------------------------------------------------------- 日志收集
class MemoryLogHandler(logging.Handler):
    def __init__(self, limit: int = 1500):
        super().__init__()
        self.records: list[dict] = []
        self.seq = 0
        self.limit = limit
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        with self._lock:
            self.seq += 1
            self.records.append({
                "seq": self.seq, "t": time.strftime("%H:%M:%S"),
                "level": record.levelname, "msg": msg,
            })
            if len(self.records) > self.limit:
                del self.records[: len(self.records) - self.limit]

    def since(self, n: int) -> list[dict]:
        with self._lock:
            return [r for r in self.records if r["seq"] > n]


def _set_config_value(path: Path, section: str, key: str, value) -> None:
    """只改 config.yaml 里 [section] 下的一个键，其余原样保留（含注释）。

    面板上改帧率模式要记住，但不能用 yaml.safe_dump 重写整个文件 —— 那会把
    用户写的中文注释和排版全冲掉。这里按行扫描，找到 section 后改它下面第一个
    匹配 `key:` 的顶层键；没有就在 section 末尾补一行。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_sec = False
    done = False
    key_prefix = f"{key}:"
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not line.startswith((" ", "\t")):
            # 顶层键：判断是否进入/离开目标 section
            if stripped == f"{section}:":
                in_sec = True
            else:
                in_sec = False
        if in_sec and not done and line.startswith(key_prefix) and not stripped.startswith("#"):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}{key}: {value}")
            done = True
            continue
        out.append(line)
    if not done:
        # section 里没有这个键：插在 section 标题后第一行。
        sec_line = next((i for i, l in enumerate(out)
                         if l.strip() == f"{section}:"), None)
        if sec_line is None:
            out += ["", f"{section}:", f"  {key}: {value}"]
        else:
            out.insert(sec_line + 1, f"  {key}: {value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 会话
class Session:
    def __init__(self) -> None:
        paths.ensure_layout(ROOT)
        self.root = ROOT
        self.cfg = bootstrap.load_config(ROOT)
        self.log = setup(
            str((self.cfg.get("logging") or {}).get("level", "INFO")),
            str(ROOT / str((self.cfg.get("logging") or {}).get("dir", "logs"))),
        )
        self.mem = MemoryLogHandler()
        self.log.addHandler(self.mem)

        # 自动更新：后台线程查 GitHub Release，面板顶栏显示版本号。
        # 仓库私有/改名时 API 404，这里只记一条 info，不影响任何功能。
        up = self.cfg.get("update") or {}
        self.updater = update.Updater(
            ROOT, self.log,
            repo=str(up.get("repo") or update.DEFAULT_REPO),
            enabled=bool(up.get("check", True)),
            interval_hours=float(up.get("interval_hours") or 6),
        )
        self.updater.start()

        self.game, self.cap, self.matcher, self.dev = bootstrap.build(self.cfg, ROOT, self.log)
        self.dev.cancel_event = threading.Event()
        self.cancel = self.dev.cancel_event

        self.lock = threading.RLock()
        self.running = False
        self.stopping = False
        self.current = ""
        self.status: dict[str, str] = {}
        self.notes: dict[str, str] = {}
        self.last_summary = ""
        self.alert = {"msg": "", "ts": 0.0, "task": ""}
        self.dev.notifier = self._notify

        self.recording: Recording | None = None
        self._ready_at = 0.0             # 画面第一次 ready 的时间（对齐要等一会儿）
        self._settle_seq = -1            # 上一次看到的帧号（判断画面还在不在动）
        self._settle_quiet = 0           # 连续「没有新帧」的次数
        self._paused = False             # 临时让守护线程别碰浏览器（导入登录态时用）
        self.preview_on = bool((self.cfg.get("ui") or {}).get("preview", False))
        # 帧率模式：auto=跟随预览（开→60 / 关→20），high/low=手动锁定。
        # 存 ui.fps_mode，缺省 auto。启动时先把目标帧率应用到画面流。
        self.fps_mode = str((self.cfg.get("ui") or {}).get("fps_mode", "auto") or "auto")
        if self.fps_mode not in ("auto", "high", "low"):
            self.fps_mode = "auto"
        self._apply_fps(restart=False)
        self._frames: OrderedDict[int, np.ndarray] = OrderedDict()
        self._frame_seq = 0

        self.tasks = load_jobs(ROOT)
        self.opts = load_options(ROOT)

        self.connected = False
        self._stop = threading.Event()
        self._last_ready = 0.0
        # 用户手动关掉了游戏浏览器窗口：守护线程别再自动把它拉回来。
        # 只有主动点「连接 / 重开 / 重载 / 登录」才复位（见 connect()）。
        self._user_closed = False
        threading.Thread(target=self._supervise, name="supervise", daemon=True).start()

    # ------------------------------------------------------------ 帧率模式
    def _fps_target(self) -> float:
        """按当前模式算目标帧率。auto 跟随预览开关，high/low 手动锁定。"""
        if self.fps_mode == "high":
            return self.cap.fps_high
        if self.fps_mode == "low":
            return self.cap.fps_low
        # auto：预览开着（含录制）用高帧率，关着用低帧率省 CPU
        return self.cap.fps_high if (self.preview_on or self.recording) else self.cap.fps_low

    def _apply_fps(self, restart: bool = True) -> dict:
        """把当前模式对应的帧率应用到画面流。目标没变就不动，避免无谓重启。"""
        target = self._fps_target()
        changed = abs(target - self.cap._active_fps) > 0.5
        self.cap.fps_mode = self.fps_mode
        self.cap.set_fps(target)
        if changed and restart and self.cap._running:
            ok = self.cap.restart()
            if not ok:
                self.log.warning("切换帧率后画面流重启失败")
        self.log.info(f"帧率模式 {self.fps_mode} → 目标 {target:.0f}fps"
                      + ("（已重启画面流）" if changed and restart and self.cap._running else ""))
        return {"ok": True, "mode": self.fps_mode, "target_fps": round(target, 1),
                "fps_high": self.cap.fps_high, "fps_low": self.cap.fps_low}

    def set_fps_mode(self, mode: str) -> dict:
        """手动切换帧率模式，并记住到 config.yaml。"""
        mode = str(mode or "auto")
        if mode not in ("auto", "high", "low"):
            return {"ok": False, "hint": "模式只能是 auto/high/low"}
        self.fps_mode = mode
        # 记住选择：改写 config.yaml 的 ui.fps_mode（只动这一行，保留其它注释/配置）
        try:
            _set_config_value(ROOT / "config.yaml", "ui", "fps_mode", mode)
        except Exception as e:
            self.log.warning(f"记住帧率模式失败：{e}")
        return self._apply_fps()

    # ------------------------------------------------------------ 连接守护
    def connect(self) -> bool:
        """准备画面：起浏览器 → 连页面 → 设视口 → 开画面流。"""
        # 任何一次显式 connect 都是「用户想要画面」：解除手动关闭的暂停。
        # （守护线程自动重连不经过这里——它在下面会先判断 _user_closed。）
        self._user_closed = False
        try:
            ok = self.game.prepare(force=True)
        except Exception as e:
            self.log.warning(f"连接失败：{type(e).__name__}: {e}")
            ok = False
        self.connected = bool(ok)
        if ok:
            if not self.cap._running:
                self.cap.start()
            self._last_ready = time.time()
            self._ready_at = time.time()   # 计时起点：对齐要等主页出现
        return self.connected

    def _supervise(self) -> None:
        """后台看住连接：掉线自动重连；用户登录完/游戏加载完自动接上。

        登录这一步是人在浏览器里做的，程序得自己发现「好了」：
        状态不是 ready 就隔一会儿重试一次 prepare（顺手会重新注入样式，
        因为登录会整页刷新，样式会丢）。
        """
        self.log.info("正在连接游戏画面…")
        self.connect()
        while not self._stop.is_set():
            time.sleep(1.5)
            try:
                if self._paused:
                    # 正在换 profile（导入登录态）：别在这时候把浏览器又拉起来，
                    # 它会重新独占 cookie 库，拷贝直接失败
                    continue
                if self.running:
                    # 跑任务时别折腾画面，只需要防一手：窗口被最小化（Win+D 之类）
                    # 会让 Chrome 直接停止出帧 —— 发现就原地恢复。
                    self.game.ensure_visible()
                    continue
                # 空闲时只做三件轻量的事：窗口尺寸变了才重新对齐；加载完了对齐一次；
                # 顺手看一眼浮层还在不在（页面换 DOM 会把节点清掉，见 core/overlay.py）
                self.game.refresh_geometry()
                try:
                    self.game.ensure_overlay_alive()
                except Exception:
                    pass
                dead = (self.game.cdp is None) or bool(self.game.cdp.error)
                if dead:
                    # 区分两种情况：
                    #   a) 我们启动的那个浏览器进程已经退出（用户手动关了窗口）
                    #      → 停在这里，别再自动开回来；等用户主动点「连接」。
                    #   b) 进程还在，只是页面掉线 / CDP 断了
                    #      → 照旧自动重连（登录加载完自动接上也走这条）。
                    proc = getattr(self.game, "_proc", None)
                    # 进程退出 = 用户关了窗口；_proc 为空(外部浏览器)时退回看端口。
                    # 只关页面/渲染崩了而窗口还在时，端口仍活、进程也还在 → proc_gone=False。
                    if proc is not None:
                        proc_gone = proc.poll() is not None
                    else:
                        proc_gone = not self.game.br.alive()
                    self.cap.stop()
                    self.game.close()
                    self.connected = False
                    self.cap.error = ""
                    if proc_gone:
                        if not self._user_closed:
                            self.log.info(
                                "检测到游戏浏览器窗口已被关闭，停止自动重连"
                                "（需要时点「连接」重新打开）")
                            self._user_closed = True
                        self.game.status = "closed"
                        self.game.hint = "游戏窗口已关闭 · 点「连接」重新打开"
                        continue
                    self.connect()
                    continue
                if self.game.status != "ready":
                    self.connect()    # 内部会等到画布出现 / 认出登录页
                    continue
                if not self.cap._running:
                    self.cap.start()
                self.connected = True
                self._maybe_settle()
            except Exception as e:
                self.log.warning(f"守护线程异常：{e}")

    def _lobby_ready(self) -> bool:
        """主页锚点有没有出现（一个更强的「加载完了」信号）。

        没配锚点模板（或模板还没录）时返回 False —— 只靠「画面不动」判断。
        """
        tpl = (self.cfg.get("game") or {}).get("home_template") or ""
        if not tpl or not self.matcher.has(tpl):
            return False
        gray = self.cap.gray()
        if gray is None:
            return False
        try:
            return self.matcher.find_gray(gray, tpl, 0.82) is not None
        except Exception:
            return False

    def _title_ready(self) -> bool:
        """标题画面（GAME 字样那块 logo）出现了没有 —— 用户点名要用这个当扳机。

        用户的原话：「加载完成到 GAME 字样出来后」再动画面。所以这里用
        `game.title_template`（默认 title_logo）单独认一次，认到了立刻动手，
        不再等「画面静止」—— 标题画面本身有很轻的呼吸动画，等静止等于永远等。
        """
        tpl = (self.cfg.get("game") or {}).get("title_template") or ""
        if not tpl or not self.matcher.has(tpl):
            return False
        gray = self.cap.gray()
        if gray is None:
            return False
        try:
            return self.matcher.find_gray(gray, tpl, 0.86) is not None
        except Exception:
            return False

    def _auto_zoom(self) -> None:
        """对齐前先把网页缩放拨到 100%（默认执行一次）。

        只做一次（`_zoom_done`，页面重载会复位）。这一步必须发真实的键盘事件，
        会抢一下前台焦点；失败只警告、不重试，绝不会反复抢。配置 `zoom_auto: false`
        可关掉自动缩放，改为点面板「缩放 100%」手动执行。
        """
        if not getattr(self.game, "zoom_auto", False) or self.game._zoom_done:
            return
        self.game._zoom_done = True
        self.log.info("按配置自动缩放：把网页缩放拨到 100%（会抢一下前台）…")
        r = self.game.zoom_to_100()
        if not r.get("ok"):
            self.log.warning(f"自动缩放没成功：{r.get('hint') or '未知原因'}")

    def _maybe_settle(self) -> None:
        """加载完成后「对齐一次」：铺满 → 居中，然后锁死。

        用户的要求都在这里：
          ① 必须等标题画面（GAME 字样）出来再动，别在过场动画里瞎折腾；
          ② 黑边太大 —— 按窗口把游戏铺满。

        只动 DOM/CSS，不碰鼠标键盘、不改浏览器缩放 —— 运行期间你能正常用电脑。

        判定「可以动手了」的信号，满足一个就行：
          · `game.title_template`（GAME 字样）认出来了 —— 首选，用户指定的时机；
          · 或者主页锚点模板认出来了；
          · 或者 screencast 不再出新帧（画面静止 3 秒以上）。
        最后一个都没有就等 browser.settle_timeout 秒兜底。
        """
        if not self.game.needs_settle():
            return
        # 刚连上的一瞬间游戏可能还在黑屏/白屏，画面本来就不动 —— 别拿那个当
        # 「加载完了」。至少等这么久，再考虑用「画面静止」作信号。
        if time.time() - (self._ready_at or time.time()) < 6.0:
            return
        if self._title_ready():
            self.log.info("认到标题画面（GAME 字样），开始对齐…")
            self._auto_zoom()
            self.game.settle_layout()
            return
        seq = self.cap.seq
        moved = seq != self._settle_seq
        self._settle_seq = seq
        if moved or seq <= 0:
            self._settle_quiet = 0
            if self._lobby_ready():
                self._settle_quiet = 99       # 锚点命中：直接放行
            else:
                wait = float((self.cfg.get("browser") or {}).get("settle_timeout", 60) or 60)
                if time.time() - (self._ready_at or time.time()) < wait:
                    return
                self.log.warning("画面一直在变（过场动画/加载条），等超时了，先对齐一次")
        else:
            self._settle_quiet += 1
            if self._settle_quiet < 2:
                return                            # 静了 1 个周期（约 1.5s）再确认一次
        self.log.info("开始对齐画面（把舞台铺满窗口并居中）…")
        self._auto_zoom()
        self.game.settle_layout()

    def restart_browser(self) -> dict:
        """重连：刷新游戏页（画面冻住 / 掉登录 / 加载卡住时用）。

        不关窗口 —— 关掉就得重新登一次，刷新能保住 cookie。
        """
        if self.running:
            return {"ok": False, "hint": "正在执行任务，先停止"}
        self.cap.stop()
        try:
            if self.game.cdp is not None:
                self.game.cdp.call("Page.reload", {"ignoreCache": False}, timeout=8)
                self.game._vp_mode = ""      # 刷新后视口覆盖未必还在，让 prepare 重下
                self.game._settled = False   # 刷新后重新对齐一次
                self._ready_at = 0.0
                time.sleep(2.5)
        except Exception as e:
            self.log.warning(f"刷新页面失败，改为重连：{e}")
            self.game.close()
        self.connected = False
        ok = self.connect()
        return {"ok": ok, "hint": self.game.hint if not ok else ""}

    def reload_game(self, clear_cache: bool = False) -> dict:
        """只重进账号页/游戏框，不碰站点会话（登录卡住时用）。"""
        if self.running:
            return {"ok": False, "hint": "正在执行任务，先停止"}
        self.cap.stop()
        ok = self.game.reload_game(clear_cache=clear_cache)
        self._ready_at = 0.0
        if not ok:
            self.connected = False
            return {"ok": False, "hint": "重载失败，试试「重开」或手动刷新窗口"}
        # 游戏框重载后样式会丢，等它重新加载再按流程准备一遍
        time.sleep(2.5)
        self.connected = False
        ok = self.connect()
        return {"ok": True, "connected": ok, "status": self.game.status,
                "hint": self.game.hint if not ok else ""}

    def realign(self) -> dict:
        """再对齐一次画面（= 重新铺满 + 居中）。

        加载完成时程序会自动做一次；如果那一次没赶上（加载很慢、主页锚点没等到、
        或者你中途进去点了什么），点「重新对齐」再来一次就行。
        """
        if self.running:
            return {"ok": False, "hint": "正在执行任务，先停止"}
        if self.game.cdp is None or self.game.cdp.error:
            return {"ok": False, "hint": "画面还没连上，先点「连接」"}
        self.game._settled = False
        self.game._stage_size = (0, 0)
        self.game._stage_shift = (0, 0)
        self.game._stage_scale = 1.0
        ok = self.game.settle_layout()
        return {
            "ok": bool(ok), "settled": self.game._settled,
            "stage": list(self.game._stage_size), "scale": round(self.game._stage_scale, 3),
            "scale_of_display": round(self.game._stage_scale, 3),
            "hint": "" if ok else "对齐没成功，看日志",
        }

    def reset_view(self) -> dict:
        """把画面还原（黑屏急救）：清掉舞台的位移/缩放，回到 1:1 居中。

        对齐算歪、或者上次运行留下坏位移把游戏顶出窗口（整块黑屏）时点它。
        不刷新页面、不重登，只撤掉我们自己加的样式，画面立刻回来。
        """
        if self.running:
            return {"ok": False, "hint": "正在执行任务，先停止"}
        if self.game.cdp is None or self.game.cdp.error:
            return {"ok": False, "hint": "画面还没连上，先点「连接」"}
        ok = self.game.reset_layout()
        # 还原之后 iframe 里的 #unityPlayer 可能又溢出，把裁剪补丁重新贴一次
        try:
            self.game._ensure_frame_css()
        except Exception:
            pass
        return {"ok": bool(ok), "settled": bool(self.game._settled),
                "stage": list(self.game._stage_size),
                "hint": "" if ok else "还原没成功，看日志"}

    def zoom(self) -> dict:
        """把网页缩放拨到 100%，然后重新对齐一次画面（面板按钮入口）。

        为什么要一个按钮：这游戏/站点对缩放敏感，有人 90% 的浏览器缩放会让画面
        看起来「显示不全」。缩放是**浏览器进程**的快捷键，CDP 碰不到，只能发真的
        键盘事件（Ctrl+= 再 Ctrl+0）—— 所以只能由用户主动点，不能偷偷做：
        执行时会把游戏窗口抢到前台，那一两秒不能操作别的窗口。

        实际缩放+对齐逻辑在 `GameSession.zoom_and_settle()`，和「缩放100%」任务
        共用同一套；这里只保留面板按钮的 `running` 保护。
        """
        if self.running:
            return {"ok": False, "hint": "正在执行任务，先停止"}
        return self.game.zoom_and_settle()

    def login_with_jwt(self, jwt: str) -> dict:
        """粘贴 erolabsjwt 直接登录，绕开账号页与人机验证。

        大屏 + 自动化环境下 Turnstile 经常过不去，这条路最稳：
        在平时用的浏览器里登录一次，把 cookie 值抄过来就行。
        """
        if self.running:
            return {"ok": False, "hint": "正在执行任务，先停止"}
        jwt = (jwt or "").strip().strip('"')
        if len(jwt) < 20:
            return {"ok": False, "hint": "令牌看起来不对（太短了）"}
        self.cap.stop()
        self.connected = False
        if not self.game.inject_jwt(jwt):
            return {"ok": False, "hint": "写入失败，看日志"}
        ok = self.connect()
        return {"ok": True, "connected": ok, "status": self.game.status,
                "hint": self.game.hint if not ok else "登录态已注入"}

    def import_login(self, close: bool = False) -> dict:
        """借用本机 Chrome 的登录态。

        为什么只能「拷」不能「接管」：CDP 只认带调试端口启动的浏览器，
        你日常那个 Chrome 没开端口；Chrome 136 起又禁止在默认 profile 上开端口。
        所以这里换个思路 —— 把那份 profile 的 cookie 拷到工具自己的 profile，
        重开之后工具里的浏览器就是已登录状态（连 cf_clearance 一起带过来）。

        顺序很关键，而且有个硬约束：Chrome 开着时对 cookie 库是独占的，
        连只读都不行。所以先探一次锁 —— 锁着就先问用户（close=True 才动手关），
        别把工具浏览器关了才发现拷不动。
        """
        if self.running:
            return {"ok": False, "hint": "正在执行任务，先停止"}
        got = chrome_profile.pick_source()
        if got is None:
            return {"ok": False, "hint": "没找到本机 Chrome / Edge 的用户目录"}
        name, src = got
        if chrome_profile.is_locked(chrome_profile.cookie_db(src)) and not close:
            return {"ok": False, "need_close": True, "browser": name,
                    "hint": f"{name} 还开着，它独占 cookie 库。"
                            f"要我先关掉它吗（会话会保存，下次打开能恢复标签页）？"}
        if not self.game.quit_browser():
            return {"ok": False, "hint": "工具的那个浏览器窗口没关掉，手动关掉它的窗口再点一次"}
        self.cap.stop()
        self.connected = False
        # 拷贝期间把守护线程按住：否则它 1.5 秒后就会把浏览器又拉起来，
        # 重新独占 cookie 库，拷贝必然失败
        self._paused = True
        try:
            time.sleep(0.8)   # 等 Chrome 把内存里的 cookie 落盘，别拷到旧数据
            r = chrome_profile.import_login(self.game.user_data, source=src, close=close, log=self.log)
            if not r.get("ok"):
                self.connect()
                return {"ok": False, "hint": r.get("hint") or "没导入成功，看日志"}
            ok = self.connect()
            return {
                "ok": True, "connected": ok, "status": self.game.status,
                "source": r.get("source"), "cookies": r.get("cookies"),
                "cookies_game": r.get("cookies_game"), "backup": r.get("backup"),
                "hint": "" if ok else (self.game.hint or "登录态已导入，但画面还没准备好"),
            }
        finally:
            self._paused = False

    # ------------------------------------------------------------ 画面
    def grab(self) -> np.ndarray | None:
        img = self.cap.frame(force=True)
        if img is None:
            return None
        self._frame_seq += 1
        with self.lock:
            self._frames[self._frame_seq] = img
            while len(self._frames) > 4:
                self._frames.popitem(last=False)
        return img

    def frame_by_id(self, fid: int | None) -> tuple[int, np.ndarray | None]:
        with self.lock:
            if fid is not None and fid in self._frames:
                return fid, self._frames[fid]
        return self._frame_seq, self.grab()

    def preview(self) -> tuple[bytes, int, tuple[int, int]]:
        got = self.cap.latest()
        if not got:
            return b"", 0, (0, 0)
        jpg, seq = got
        img = self.cap.frame()
        size = (img.shape[1], img.shape[0]) if img is not None else (REF_W, REF_H)
        return jpg, seq, size

    def preview_wait(self, after: int, timeout: float = 4.0):
        return self.cap.preview_wait(after, timeout)

    # ------------------------------------------------------------ 任务
    def reload(self) -> None:
        self._reload_task_modules()
        self.tasks = load_jobs(ROOT)

    def _reload_task_modules(self) -> None:
        """源码方式运行时把 tasks/*.py 重载一遍：改完任务点「刷新任务」就生效。

        打包版跳过（代码在 exe 里，改了也没用）；重载出错就继续用启动时那份，
        不让面板因为改了半截代码就打不开。
        """
        if getattr(sys, "frozen", False):
            return
        try:
            import importlib

            import tasks as pkg
            import tasks.daily
            import tasks.registry

            impls = [n for n in list(sys.modules)
                     if n.startswith("tasks.")
                     and n not in ("tasks.daily", "tasks.registry")]
            for name in sorted(impls, key=lambda s: (-s.count("."), s)):
                importlib.reload(sys.modules[name])
            importlib.reload(tasks.daily)          # 用新的 job 函数重建 JOBS
            tasks.registry.JOBS = tasks.daily.JOBS  # load_jobs 手里的那份也换掉
            pkg.JOBS = tasks.daily.JOBS
        except Exception as e:                     # 语法错/导入错都别炸面板
            self.log.warning(f"  任务模块重载失败（继续用启动时那份）：{e}")

    def _notify(self, msg: str) -> None:
        now = time.time()
        if self.alert["msg"] == msg and now - self.alert["ts"] < 8:
            return
        self.alert = {"msg": msg, "ts": now, "task": self.current}

    def set_options(self, key: str, values: dict) -> dict:
        if self.running:
            return {"ok": False, "hint": "正在执行，等这轮跑完再改"}
        job = next((j for j in self.tasks if j.key == key), None)
        if job is None:
            return {"ok": False, "hint": "任务不存在"}
        clean: dict[str, list[str]] = {}
        for opt in job.options:
            k = str(opt.get("key", ""))
            picked = values.get(k)
            if not isinstance(picked, list):
                continue
            if opt.get("type") == "number":
                try:
                    n = int(picked[0])
                except (ValueError, TypeError, IndexError):
                    continue
                lo = int(opt.get("min", 1))
                hi = int(opt.get("max", 99))
                clean[k] = [str(max(lo, min(hi, n)))]
                continue
            if opt.get("type") == "text":
                if picked and str(picked[0]).strip():
                    clean[k] = [str(picked[0]).strip()[:40]]
                continue
            allowed = [str(c.get("v")) for c in (opt.get("choices") or [])]
            clean[k] = [str(v) for v in picked if str(v) in allowed]
        self.opts[key] = clean
        try:
            save_options(ROOT, self.opts)
        except Exception as e:
            return {"ok": False, "hint": f"没存下来：{e}"}
        return {"ok": True, "options": resolved_options(job, self.opts)}

    def state(self) -> dict:
        tpl_dir = self.matcher.dir
        tasks = []
        for j in self.tasks:
            missing = [n for n in j.needs if not (tpl_dir / f"{n}.png").exists()]
            tasks.append({
                "key": j.key, "name": j.name, "desc": j.desc, "source": j.source,
                "deletable": j.deletable, "needs": j.needs, "missing": missing,
                "status": self.status.get(j.key, ""),
                "status_note": self.notes.get(j.key, ""),
                "options": j.options,
                "selected": resolved_options(j, self.opts) if j.options else {},
            })
        rec = None
        if self.recording:
            rec = {"key": self.recording.key, "name": self.recording.name,
                   "steps": self.recording.labels(), "count": len(self.recording.steps)}
        g = self.cfg.get("game", {}) or {}
        cap_info = {
            "ok": self.connected,
            "engine": "cdp",
            "fps": self.cap.fps_now(),
            "frames": self.cap.seq,
            "size": [REF_W, REF_H],
            "restarts": 0,
            "error": self.cap.error,
            "ui_fps": self.cap.fps_now(),
            "enc_ms": 0.0,
            "preview": self.preview_on,
            "running": bool(self.cap._running),
            "target_fps": round(self.cap.target_fps(), 1),
            "fps_mode": self.fps_mode,
            "fps_high": self.cap.fps_high,
            "fps_low": self.cap.fps_low,
            "every_nth": int(self.cap.stream_params().get("everyNthFrame") or 1),
            "quality": int(self.cap.stream_params().get("quality") or 62),
        }
        return {
            "connected": self.connected,
            "running": self.running,
            "stopping": bool(self.stopping and self.running),
            "current": self.current,
            "game": g.get("name") or "",
            "screen": [REF_W, REF_H],
            "device": [int(self.game.geo["w"]), int(self.game.geo["h"])],
            "capture": cap_info,
            "tasks": tasks,
            "selection": load_selection(ROOT),
            "order_file": str(order_path(ROOT)),
            "recording": rec,
            "alert": self.alert,
            "last_summary": self.last_summary,
            "log_seq": self.mem.seq,
            # ---- 网页版特有 ----
            "preview": self.preview_on,
            "status": self.game.status,
            "hint": self.game.hint,
            "login_err": self.game.login_err,
            "settled": self.game._settled,
            "zoom_auto": bool(getattr(self.game, "zoom_auto", False)),
            "mode": self.game.mode,
            "viewport": [self.game.vw, self.game.vh],
            "perf": self.matcher.perf(),
            # ---- 版本 / 自动更新 ----
            "version": self.updater.info.get("current") or "",
            "update": self.updater.snapshot(),
        }

    def select(self, keys: list[str]) -> dict:
        cur = {j.key for j in self.tasks}
        want = [k for k in keys if k in cur]
        try:
            save_selection(ROOT, want)
        except Exception as e:
            return {"ok": False, "hint": f"勾选没存下来：{e}"}
        return {"ok": True, "keys": want}

    def reorder(self, keys: list[str]) -> dict:
        if self.running:
            return {"ok": False, "hint": "正在执行，等这轮跑完再调顺序"}
        cur = [j.key for j in self.tasks]
        want = [k for k in keys if k in cur]
        want += [k for k in cur if k not in want]
        by_key = {j.key: j for j in self.tasks}
        self.tasks = [by_key[k] for k in want]
        try:
            save_order(ROOT, want)
        except Exception as e:
            return {"ok": False, "hint": f"顺序没存下来：{e}"}
        self.log.info("执行顺序：" + " → ".join(by_key[k].name for k in want))
        return {"ok": True, "keys": want}

    def reveal_order(self) -> dict:
        p = order_path(ROOT)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                save_order(ROOT, [j.key for j in self.tasks])
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p.parent)])
        except Exception as e:
            return {"ok": False, "hint": f"打不开文件夹：{e}", "path": str(p)}
        return {"ok": True, "path": str(p)}

    def start_run(self, keys: list[str]) -> dict:
        if self.running:
            return {"ok": False, "hint": "正在执行中，先停止"}
        if not self.connected:
            return {"ok": False, "hint": "游戏画面还没连上"}
        jobs = [j for j in self.tasks if j.key in keys]
        if not jobs:
            return {"ok": False, "hint": "没有选中任何任务"}

        # 开工前先保证画面已经对齐过：没对齐的话点击坐标会整体偏，
        # 一上手就连错，任务基本跑不动。
        if self.game.needs_settle():
            self.log.info("开工前先对齐一次画面…")
            if not self.game.settle_layout():
                # 已经有一轮在跑（守护线程抢先了）。这时候不能直接开工 ——
                # 舞台正在被挪动，第一次点击用的还是旧坐标，必然点空。
                # 等它落地再重量一次，最多 15 秒。
                self.log.info("等正在进行的对齐落地（避免第一次点击用旧坐标）…")
                if self.game.wait_settle(15.0):
                    self.game.refresh_geometry(force=True)

        self.cancel.clear()
        self.running = True
        self.stopping = False
        self.last_summary = ""
        self.notes = {}
        self.status = {}
        for j in jobs:
            self.status[j.key] = "pending"
        opts_map = {j.key: resolved_options(j, self.opts) for j in jobs if j.options}
        threading.Thread(target=self._worker, args=(jobs, opts_map), daemon=True).start()
        return {"ok": True, "count": len(jobs)}

    def _worker(self, jobs, opts_map: dict) -> None:
        t0 = time.time()

        def on_job(i, total, job):
            self.current = job.key
            self.status[job.key] = "running"

        def on_result(job, state, msg):
            self.status[job.key] = state
            if msg:
                self.notes[job.key] = msg

        self.log.info("=" * 40)
        try:
            ok, fail = run_jobs(self.dev, jobs, on_job=on_job, on_result=on_result,
                                opts_map=opts_map)
            skip = sum(1 for v in self.status.values() if v == "skip")
            tail = f" / 跳过 {skip}" if skip else ""
            self.last_summary = f"成功 {ok} / 失败 {fail}{tail} · {time.time() - t0:.0f}s"
        except Cancelled:
            self.last_summary = "已手动停止"
        except NotFound as e:
            self.log.error(f"识别失败：{e}")
            self.last_summary = f"识别失败：{e}"
        except Exception as e:
            self.log.error(f"异常：{type(e).__name__}: {e}")
            self.last_summary = f"异常：{e}"
        finally:
            self.running = False
            self.stopping = False
            self.current = ""
            # 清掉取消标志：stop() 设的 cancel 是给任务线程用的，任务结束后
            # 必须复位，否则之后的手动点击(/api/tap)、预览操作都会一直抛
            # Cancelled，表现为「停止后界面点什么都没反应」。下次 start_run
            # 也会自己 clear，但手动操作在两次 run 之间就会中招。
            self.cancel.clear()
            self.log.info(self.last_summary or "结束")
            # 跑完顺手重量一次坐标（只读，不重排画面）：长任务里游戏可能自己
            # 换过画布尺寸，不重量的话下一轮点击会整体偏。
            try:
                self.game.refresh_geometry(force=True)
            except Exception:
                pass

    def stop(self) -> dict:
        """点停止：给任务线程发取消信号，下一个检查点就会抛 Cancelled。

        以前这里在 not running 时直接 return，用户看到的是「点了没反应」；
        现在不管在不在跑都记一条日志，界面也能看出「正在停止…」。
        """
        if not self.running:
            self.stopping = False
            return {"ok": False, "hint": "当前没有在执行"}
        if self.stopping:
            return {"ok": True, "hint": "正在停止…"}
        self.stopping = True
        self.cancel.set()
        self.log.warning("收到停止指令…")
        threading.Thread(target=self._stop_watchdog, daemon=True).start()
        return {"ok": True}

    def _stop_watchdog(self, limit: float = 12.0) -> None:
        """盯一下停止有没有真的生效，没生效就写一条醒目的日志，方便排查。"""
        t0 = time.time()
        while time.time() - t0 < limit:
            if not self.running:
                self.log.warning(f"已停止（{time.time() - t0:.1f}s）")
                return
            time.sleep(0.4)
        if self.running:
            self.log.error(f"{limit:.0f}s 了还没停下来 —— 任务线程可能卡在一次点击/等画面上，"
                           f"再等一会儿；一直不停就重启面板")

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self.cap.stop()
        except Exception:
            pass
        try:
            self.game.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 录制
    def _free_key(self, key: str) -> str:
        """分配一个绝不与现有任务/模板文件冲突的 key。

        以前 key 是前端按 S.tasks.length+1 拼的：删过任务会留编号空洞，连续录制
        时列表又没刷新回来 —— 两次都算到同一个 key，而 save() 是直接覆盖同名
        yaml，结果新录的把刚录的悄悄顶掉（看着就是「加了两个只剩一个」）。
        这里以服务端权威状态分配，`task_N` 形式往后递增，绝不复用已存在的编号。
        """
        import re
        key = str(key or "").strip() or "new_task"
        used = {j.key for j in self.tasks}
        recipes = ROOT / "tasks" / "recipes"

        def taken(k: str) -> bool:
            return k in used or (recipes / f"{k}.yaml").exists()

        if not taken(key):
            return key
        m = re.match(r"^(.*?)(\d+)$", key)
        if m:
            prefix, num = m.group(1), int(m.group(2))
            while taken(f"{prefix}{num}"):
                num += 1
            return f"{prefix}{num}"
        i = 2
        while taken(f"{key}_{i}"):
            i += 1
        return f"{key}_{i}"

    def rec_start(self, key: str, name: str) -> dict:
        if self.running:
            return {"ok": False, "hint": "先停止正在执行的任务"}
        self.preview_on = True          # 录制必须在画面上点，自动开预览
        key = self._free_key(key)       # 服务端分配唯一 key，不信任前端算的编号
        self.recording = Recording(ROOT, self.matcher.dir, key, name)
        self._apply_fps()               # auto 模式：录制时强制高帧率，画面跟手
        self.log.info(f"开始录制：{self.recording.name}（{self.recording.key}）")
        return {"ok": True, "key": self.recording.key}

    def rec_tap(self, nx: float, ny: float, fid: int | None) -> dict:
        if not self.recording:
            return {"ok": False, "hint": "没有在录制"}
        _, frame = self.frame_by_id(fid)
        if frame is None:
            return {"ok": False, "hint": "拿不到画面"}
        h, w = frame.shape[:2]
        x = max(0, min(w - 1, int(nx * w)))
        y = max(0, min(h - 1, int(ny * h)))
        res = self.recording.add_tap(frame, x, y)
        if res.get("ok"):
            self.dev.tap(x, y)
            time.sleep(0.35)
            self.log.info(f"  录制：{res['step']} 相似度 {res['score']} "
                          f"{'⚠ ' + res['warning'] if res.get('warning') else ''}")
            self.dev.invalidate()
        return res

    def rec_save(self, key: str, name: str, desc: str, steps=None) -> dict:
        if not self.recording:
            return {"ok": False, "hint": "没有在录制"}
        # 标识框用户可能手改过；若撞到已存在的任务/文件，换一个空闲 key，
        # 绝不覆盖别的录制（save() 本身是直接写同名 yaml，不做存在性检查）。
        key = self._free_key(key) if key else self.recording.key
        try:
            path = self.recording.save(key=key, name=name, desc=desc, steps=steps)
        except Exception as e:
            return {"ok": False, "hint": str(e)}
        self.log.info(f"已保存任务：{path.name}")
        self.recording = None
        self._apply_fps()               # auto 模式：录制结束，回到预览开关对应档位
        self.reload()
        return {"ok": True, "path": str(path), "key": path.stem}

    def rec_cancel(self, cleanup: bool = True) -> dict:
        if not self.recording:
            return {"ok": False}
        if cleanup:
            for t in self.recording.created:
                for ext in (".png", ".json"):
                    p = self.matcher.dir / f"{t}{ext}"
                    if p.exists():
                        p.unlink()
            self.log.info(f"放弃录制，清掉 {len(self.recording.created)} 个临时模板")
        self.recording = None
        self._apply_fps()               # auto 模式：录制结束，回到预览开关对应档位
        return {"ok": True}


SESSION: Session | None = None


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "KyHelperWeb/2.0"

    def log_message(self, fmt, *args):
        pass

    # 允许游戏页面（https://www.ero-labs.com）直接调本地接口 —— 助手浮层就住在
    # 游戏页里，跨域是必然的。只放行本工具用到的方法和头。
    def end_headers(self) -> None:
        try:
            origin = self.headers.get("Origin") or ""
        except Exception:
            origin = ""
        self.send_header("Access-Control-Allow-Origin", origin or "*")
        if origin:
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        # 公网页面（https://www.ero-labs.com）请求 127.0.0.1 属于「私有网络访问」，
        # 新版 Chrome 会额外要这个头，缺了就是 CORS 通过、PNA 拦下（表现为 Failed to fetch）
        try:
            if self.headers.get("Access-Control-Request-Private-Network"):
                self.send_header("Access-Control-Allow-Private-Network", "true")
        except Exception:
            pass
        super().end_headers()

    def do_OPTIONS(self) -> None:
        """跨域预检。浮层发 JSON POST 时浏览器会先来问一句。"""
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code: int = 200) -> None:
        LAST_HIT[0] = time.time()
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _mjpeg(self, s: Session) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.close_connection = True
        s.cap.viewers += 1
        last = 0
        try:
            while True:
                got = s.preview_wait(last, timeout=4.0)
                if got is None:
                    if not s.connected:
                        break
                    continue
                data, seq = got
                if seq <= last:
                    continue
                last = seq
                head = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(data)).encode() + b"\r\n\r\n")
                self.wfile.write(head)
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            s.cap.viewers = max(0, s.cap.viewers - 1)

    def _file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
        }.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- GET ----------
    def do_GET(self):
        s = SESSION
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            return self._file(WEB_DIR / "index.html")
        if u.path.startswith("/static/"):
            name = u.path[len("/static/"):]
            target = (WEB_DIR / name).resolve()
            if WEB_DIR.resolve() not in target.parents:
                return self.send_error(403)
            return self._file(target)

        if u.path == "/api/state":
            return self._json(s.state())
        if u.path == "/api/log":
            since = int((q.get("since") or ["0"])[0])
            return self._json({"records": s.mem.since(since), "seq": s.mem.seq})
        if u.path == "/api/stream":
            if not s.preview_on:
                return self.send_error(204)
            return self._mjpeg(s)
        if u.path == "/api/frame":
            data, seq, (w, h) = s.preview()
            if not data:
                return self._json({"ok": False, "hint": "还没有画面"}, 503)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Id", str(seq))
            self.send_header("X-Screen", f"{w}x{h}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    # ---------- POST ----------
    def do_POST(self):
        s = SESSION
        u = urlparse(self.path)
        d = self._body()
        try:
            if u.path == "/api/run":
                return self._json(s.start_run(d.get("keys") or []))
            if u.path == "/api/stop":
                return self._json(s.stop())
            if u.path == "/api/connect":
                return self._json({"ok": s.connect(), "connected": s.connected})
            if u.path == "/api/browser/restart":
                return self._json(s.restart_browser())
            if u.path == "/api/game/reload":
                return self._json(s.reload_game(clear_cache=bool(d.get("cache"))))
            if u.path == "/api/game/realign":
                return self._json(s.realign())
            if u.path == "/api/game/reset":
                return self._json(s.reset_view())
            if u.path == "/api/game/zoom":
                return self._json(s.zoom())
            if u.path == "/api/login":
                return self._json(s.login_with_jwt(d.get("jwt") or ""))
            if u.path == "/api/login/import":
                return self._json(s.import_login(bool(d.get("close"))))
            if u.path == "/api/preview":
                s.preview_on = bool(d.get("on"))
                if s.preview_on:
                    s.cap.viewers = 0
                s._apply_fps()          # auto 模式跟随预览切高/低帧率
                return self._json({"ok": True, "preview": s.preview_on})
            if u.path == "/api/capture/apply":
                # 改了 config.yaml 的 capture:（帧率/画质）后，不用重启整个程序，
                # 重开一次画面流就生效 —— 重启画面流会让识别丢掉一两帧，无妨。
                ok = s.cap.restart()
                return self._json({"ok": bool(ok), "params": s.cap.stream_params(),
                                   "target_fps": round(s.cap.target_fps(), 1)})
            if u.path == "/api/fps/mode":
                return self._json(s.set_fps_mode(d.get("mode", "auto")))
            if u.path == "/api/tap":
                img = s.cap.frame()
                if img is None:
                    return self._json({"ok": False, "hint": "还没有画面"}, 503)
                h, w = img.shape[:2]
                x, y = int(d["x"] * w), int(d["y"] * h)
                s.dev.tap(x, y)
                s.log.info(f"手动点击 ({x},{y})")
                s.dev.invalidate()
                return self._json({"ok": True})
            if u.path == "/api/shot":
                img = s.grab()
                if img is None:
                    return self._json({"ok": False}, 503)
                p = save_shot(img, "manual", str(ROOT / "logs" / "shots"))
                s.log.info(f"截图已存：{p}")
                return self._json({"ok": True, "path": p})
            if u.path == "/api/reload":
                s.reload()
                return self._json({"ok": True, "count": len(s.tasks)})

            # ---- 自动更新 ----
            if u.path == "/api/update/check":
                return self._json({"ok": True, "update": s.updater.check_now()})
            if u.path == "/api/update/apply":
                return self._json(s.updater.apply_async())

            # ---- 录制 ----
            if u.path == "/api/rec/start":
                return self._json(s.rec_start(d.get("key", "new_task"), d.get("name", "")))
            if u.path == "/api/rec/tap":
                return self._json(s.rec_tap(float(d["x"]), float(d["y"]), d.get("fid")))
            if u.path == "/api/rec/cmd":
                if not s.recording:
                    return self._json({"ok": False, "hint": "没有在录制"})
                return self._json(s.recording.add_cmd(d.get("op", ""), d.get("value")))
            if u.path == "/api/rec/undo":
                if not s.recording:
                    return self._json({"ok": False, "hint": "没有在录制"})
                s.recording.undo()
                return self._json({"ok": True, "steps": s.recording.labels()})
            if u.path == "/api/rec/save":
                return self._json(s.rec_save(d.get("key", ""), d.get("name", ""),
                                             d.get("desc", ""), d.get("steps")))
            if u.path == "/api/rec/cancel":
                return self._json(s.rec_cancel(bool(d.get("cleanup", True))))

            # ---- 任务管理 ----
            if u.path == "/api/tasks/options":
                return self._json(s.set_options(str(d.get("key", "")), d.get("values") or {}))
            if u.path == "/api/tasks/order":
                keys = d.get("keys") or []
                if not isinstance(keys, list):
                    return self._json({"ok": False, "hint": "keys 要是数组"})
                return self._json(s.reorder([str(k) for k in keys]))
            if u.path == "/api/tasks/select":
                keys = d.get("keys") or []
                if not isinstance(keys, list):
                    return self._json({"ok": False, "hint": "keys 要是数组"})
                return self._json(s.select([str(k) for k in keys]))
            if u.path == "/api/tasks/order/reveal":
                return self._json(s.reveal_order())
            if u.path == "/api/tasks/rename":
                key = str(d.get("key", ""))
                name = str(d.get("name", "")).strip()
                if not name:
                    return self._json({"ok": False, "hint": "名字不能为空"})
                job = next((j for j in s.tasks if j.key == key), None)
                if not job:
                    return self._json({"ok": False, "hint": "任务不存在"})
                if not job.path:
                    return self._json({"ok": False, "hint": "内置任务写死在代码里，改名要改 tasks/daily.py"})
                p = Path(job.path)
                try:
                    data = recipe.load_recipe(p)
                    data["name"] = name
                    if d.get("desc") is not None:
                        data["desc"] = str(d["desc"])
                    recipe.save_recipe(p, data)
                except Exception as e:
                    return self._json({"ok": False, "hint": f"写入失败：{e}"})
                s.reload()
                s.log.info(f"任务改名：{key} → {name}")
                return self._json({"ok": True, "name": name})
            if u.path == "/api/tasks/delete":
                key = d.get("key", "")
                job = next((j for j in s.tasks if j.key == key), None)
                if not job:
                    return self._json({"ok": False, "hint": "任务不存在"})
                if not job.deletable:
                    return self._json({"ok": False, "hint": "内置任务不能删"})
                if job.path:
                    p = Path(job.path)
                    if p.exists():
                        p.unlink()
                s.reload()
                s.log.info(f"已删除任务：{key}")
                return self._json({"ok": True})

        except KeyError as e:
            return self._json({"ok": False, "hint": f"缺少参数 {e}"}, 400)
        except Exception as e:
            s.log.error(f"接口异常 {u.path}：{type(e).__name__}: {e}")
            return self._json({"ok": False, "hint": str(e)}, 500)

        self.send_error(404)


def pick_port(start: int = 8765) -> int:
    import socket
    for p in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sk.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def open_ui(url: str) -> None:
    """用 Edge/Chrome 的 app 模式打开控制面板（无地址栏，像个小应用）。"""
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for exe in cands:
        if os.path.exists(exe):
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", "--window-size=1280,860",
                     "--window-position=980,40"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass
    webbrowser.open(url)


def open_panel(url: str) -> bool:
    """开一个独立面板窗口。

    优先在**游戏自己那个 Chrome 实例**里开（/json/new），不额外起进程、不占内存，
    也不用担心「默认 profile 的浏览器被别的会话接管导致窗口一闪就没」。
    这条路走不通才退回新起一个浏览器进程。
    """
    try:
        if SESSION.game.br.alive():
            r = SESSION.game.br.new_tab(url)
            if r and r.get("id"):
                SESSION.log.info("面板窗口已打开（复用游戏浏览器，不额外占内存）")
                return True
    except Exception as e:
        SESSION.log.warning(f"在游戏浏览器里开面板失败：{type(e).__name__}: {e}")
    open_ui(url)
    return True


def _open_panel_if_overlay_failed(url: str) -> None:
    """浮层模式下的兜底：浮层真装不上，才把独立面板窗口开出来。

    以前是「探活失败 4 次（约 23 秒）就开窗」，两个毛病：
    1. 那时候页面可能还在 loading，浮层只是「还没轮到装上」，白开一个窗口；
    2. 开出来的窗口走的是别人默认 profile 的浏览器，一闪就没、也说不清为什么。

    现在每一轮都**主动补注入**一次（页面换 DOM 会清掉浮层），连着 6 轮
    （约 30 秒）都补不上才认输；真认输了也优先复用游戏那个浏览器实例开窗。
    """
    misses = 0
    deadline = time.time() + 120.0
    time.sleep(4.0)
    while time.time() < deadline:
        try:
            if SESSION.game.ensure_overlay_alive():
                SESSION.log.info("助手浮层就在游戏窗口右上角，不再单独开面板")
                return
            cdp = SESSION.game.cdp
            if cdp is not None and not cdp.error and SESSION.connected:
                misses += 1
        except Exception:
            pass
        if misses >= 6:
            break
        time.sleep(5.0)
    SESSION.log.warning("助手浮层装不上（页面可能不让外部脚本落地），改开独立面板窗口")
    open_panel(url)


def _open_if_panel_missing(url: str) -> None:
    """自动更新重启后的兜底开窗。

    旧面板页面在等处「版本号变了」然后 location.reload()，一般 2~3 秒就回来了，
    所以先等；15 秒都没有任何请求进来（用户把窗口关了）才补一个。
    """
    for _ in range(6):
        time.sleep(2.5)
        if LAST_HIT[0] > 0:
            SESSION.log.info("面板已连上，不再另开窗口")
            return
    SESSION.log.info("更新后没等到面板刷新，重新开一个窗口")
    open_ui(url)


def main(open_browser: bool = True, updated: bool = False) -> None:
    global SESSION
    SESSION = Session()
    ui = SESSION.cfg.get("ui") or {}
    port = pick_port(int(ui.get("port", 8765)))
    # 端口可能因为 8765 被占而变；浮层里写的是这个端口，必须回写进配置，
    # 否则浮层会一直去敲一个没人听的端口（_ensure_overlay 发现端口变了会重注入）。
    ui["port"] = port
    SESSION.cfg["ui"] = ui
    url = f"http://127.0.0.1:{port}/"

    SESSION.log.info(f"控制面板：{url}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True

    def _quit_for_update() -> None:
        """换包前收摊：停画面流、解 CDP、关 HTTP 服务，然后硬退出。

        必须 os._exit：正常退出要等一堆守护线程收尾，而更新脚本正等着本进程
        消失才好替换 exe/_internal，拖久了它按超时也算「已退出」，但端口可能
        还被占着 —— 新版本起来会绑不上端口。
        """
        SESSION.log.info("更新：正在退出，换包完成后会自动重启")
        try:
            SESSION.shutdown()
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass
        time.sleep(0.4)
        os._exit(0)

    SESSION.updater.on_exit = _quit_for_update

    if open_browser and not os.environ.get("KYWEB_NO_BROWSER"):
        overlay = bool(ui.get("overlay", False))
        panel = bool(ui.get("panel", True))
        if updated:
            # 自动更新重启：旧面板窗口多半还开着，它会自己刷新回来。
            # 等十几秒，真没有面板来敲门（用户把窗口关了）才补一个。
            SESSION.log.info("更新重启完成，等原面板刷新…")
            threading.Thread(target=_open_if_panel_missing, args=(url,),
                             name="panel-after-update", daemon=True).start()
        elif panel or not overlay:
            threading.Timer(1.0, open_ui, args=(url,)).start()
        else:
            # 浮层模式：不弹独立窗口，25 秒后确认浮层有没有装上
            SESSION.log.info("面板走浮层：控制按钮在游戏窗口右上角（Ctrl+Shift+X 停止）")
            threading.Timer(2.0, _open_panel_if_overlay_failed, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        SESSION.log.info("退出")
    finally:
        httpd.server_close()
        try:
            SESSION.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
