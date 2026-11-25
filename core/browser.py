"""Chrome 生命周期 + 游戏画面定位。

网页版跟模拟器最大的区别：画面不是「截一张设备图」，而是浏览器里的
一个 iframe（云游戏 WebGL 画布）。所以这里干三件事：

1. 用带 `--remote-debugging-port` 的独立 profile 起 Chrome（Chrome 136+
   不许在默认 profile 上开调试端口，必须给一份自己的用户目录）。
2. 找到承载游戏的 target：跨域的 iframe 是独立 target（OOPIF），
   同域的 iframe 跟父页面共用一个 target。两种都支持。
3. 把页面收拾成「干净的 16:9 游戏屏」：隐藏站点导航/页脚、iframe 铺满，
   舞台按「量到的画布尺寸」框住并居中（视口本身始终等于窗口，不覆盖）。
   之后画面就等价于模拟器截图。

坐标基准（REF）永远是模板的 2560×1440，实际视口多大都能换算过去，
这就是「缩放自适应」的全部秘密。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import paths
from .cdp import Browser, CDP, CDPError

try:                       # 系统级输入只有 Windows 有；别的平台当成没有
    from . import winput
except Exception:          # pragma: no cover
    winput = None

# 说明：除了 `zoom_to_100()` 这一个**用户主动触发**的动作，本模块只在 DOM/CSS
# 层面干活。浏览器缩放是浏览器进程的快捷键，CDP 碰不到；要它生效只能发真的
# 键盘事件（会抢一下前台焦点），所以默认关闭、只在点「缩放 100%」时执行。


# 站点外壳的清理样式：只留游戏本身。
#
# 关键的一条是「居中的 16:9 舞台」。为什么不铺满整个窗口：
#   · 窗口比例一般不是 16:9（最大化后还要扣掉任务栏），铺满会让游戏在窗口里
#     被拉伸/偏移，人看到的位置和识别拿到的位置就对不上了；
#   · 以前的做法是把虚拟视口强行撑到 2560×1440 —— 窗口比视口小，人只能看到
#     左上角一坨（约 1/1.8），用浏览器缩放看全之后内容又变成左上角对齐（不居中）。
# 现在改成：视口老老实实等于窗口，游戏放一块「窗口内能放下的最大 16:9」里居中，
# 四周黑底。人看到的、截图拿到的、模板匹配的都是同一块画面。
#
# 注意：**只对最外层 #cloudGameContainer 施加尺寸和位移**。
# #gamePlayContainer 是它的子节点，如果两个都写 transform，位移会被乘两遍，
# 画布会在窗口里来回跳（实测偏移量正负反复，永远收敛不了）。
PAGE_CSS = """
#nav, #footer, .footer_mobileNav, nav.header, .header { display: none !important; }
html, body { margin: 0 !important; padding: 0 !important; overflow: hidden !important;
             background: #000 !important; height: 100% !important; }
#cloudGameContainer {
    position: fixed !important; top: 50% !important; left: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(100vw, 177.7778vh) !important;
    height: min(100vh, 56.25vw) !important;
    margin: 0 !important; padding: 0 !important; background: #000 !important; }
#gamePlayContainer {
    position: absolute !important; inset: 0 !important;
    width: 100% !important; height: 100% !important;
    margin: 0 !important; padding: 0 !important; background: #000 !important;
    transform: none !important; }
#gamePlayContainer iframe, #game_frame, iframe#game_frame {
    position: absolute !important; inset: 0 !important;
    width: 100% !important; height: 100% !important; border: 0 !important; }
#pageLoading, #fail__message, .loading-mask { display: none !important; }
"""

# iframe 内部（同源时可注入）：去掉边距，画布铺满。
INNER_CSS = """
html, body { margin: 0 !important; padding: 0 !important; overflow: hidden !important;
             width: 100% !important; height: 100% !important; background: #000 !important; }
canvas { display: block !important; }
"""


def _stage_css(w: int, h: int, dx: int = 0, dy: int = 0, k: float = 1.0,
               ox: int = 0, oy: int = 0) -> str:
    """把舞台固定成 w×h，整体平移 dx/dy，再按 k 倍放大。

    为什么还要 dx/dy：游戏内部把画布摆得不一定居中（这套 Unity WebGL 就把画布
    贴在舞台上方，实测偏上 100 多像素）。舞台跟着挪同样的距离，画布就落回正中。

    为什么还要 k：游戏把画布封顶在 ~1280×720（它自己的分辨率上限），窗口更大时
    四周就全是黑边。用 CSS 把舞台放大，黑边就吃掉了 —— 画面会略微变软，但
    轮廓、字距、点击位置都不变（getBoundingClientRect 会把 scale 算进去）。

    为什么用**显式像素**而不是 min(100vw, 177vh)：对齐时要按「量到的画布尺寸」
    算像素、再叠加位移和 k，CSS 表达式做不到「用运行时才知道的数」。
    """
    if w <= 0 or h <= 0:
        return ""
    kk = max(0.2, min(3.0, float(k or 1.0)))
    css = ("#cloudGameContainer {"
           f" width: {int(w)}px !important; height: {int(h)}px !important;"
           " top: 50% !important; left: 50% !important;"
           f" transform: translate(calc(-50% + {int(dx)}px),"
           f" calc(-50% + {int(dy)}px)) scale({kk:.4f}) !important;"
           " transform-origin: 50% 50% !important; }")
    if ox or oy:
        # 游戏自己的容器给画布留了边距（实测 1280×720 的画布被排在 (20,40)），
        # 舞台收成画布尺寸之后这圈边距会把画布顶出去，右下角被裁掉一条。
        # 把 iframe 反向挪回 (0,0)，画布就正好贴满舞台。
        css += ("\n#gamePlayContainer iframe, #game_frame {"
                f" transform: translate({int(ox)}px, {int(oy)}px) !important; }}")
    return css


def _css_apply_src(css: str, sid: str = "__kyweb_css", fn: str = "apply") -> str:
    """把「注入样式」写成一个函数体，写进 id 为 sid 的 <style>。

    为什么要分成两个 <style>：基础样式（清外壳、铺满）和舞台样式（尺寸/位移/缩放）
    的写入者不是同一方 —— 基础样式由「每个新文档自动执行」的脚本写，
    舞台样式由程序按实时测量的值写。以前两者共用一个 <style>，导航后自愈脚本
    会把整个内容换成它注册时那份（里面带着过期的 2560×1440），舞台就跑到屏幕外，
    人看到的就是「画面只显示一部分」。分开就永远不会互相覆盖。
    """
    src = json.dumps(css)
    return ("const " + fn + " = () => {\n"
            "  const d = document.documentElement;\n"
            "  if (!d) return 0;\n"
            "  let s = document.getElementById(" + json.dumps(sid) + ");\n"
            "  if (!s) { s = document.createElement('style'); s.id = " + json.dumps(sid)
            + "; d.appendChild(s); }\n"
            "  if (s.textContent !== " + src + ") s.textContent = " + src + ";\n"
            "  return 1;\n"
            "};\n")


def _css_now_js(base: str, stage: str = "") -> str:
    """立刻在当前文档上执行：基础样式 + 舞台样式（各自写自己的 <style>）。"""
    body = _css_apply_src(base, "__kyweb_css", "applyBase")
    if stage:
        body += _css_apply_src(stage, "__kyweb_stage", "applyStage")
    else:
        body += "const applyStage = () => 0;\n"
    return "(() => {\n" + body + "  return applyBase() + applyStage();\n})()"


def _css_on_new_doc_js(css: str) -> str:
    """每个新文档都执行：页面导航/刷新之后**基础样式**自己回来。

    补了几次 setTimeout：SPA 有时会在 DOMContentLoaded 之后重组 head，
    一次性挂上容易被打掉，多挂几次成本可以忽略。

    **只写基础样式（PAGE_CSS），不要带舞台尺寸**：游戏页启动后会整页导航一次，
    导航时脚本会用「注册那一刻」的尺寸重新贴样式，而注册发生在还没量过窗口的
    时候 —— 贴回去就是配置里的 2560×1440，舞台比窗口大一整圈、跑到屏幕外。
    舞台样式统一由 ensure_css() 按实时值写进另一个 <style>。
    """
    return ("(() => {\n" + _css_apply_src(css, "__kyweb_css", "applyBase")
            + "  applyBase();\n"
            "  document.addEventListener('DOMContentLoaded', applyBase, { once: true });\n"
            "  window.addEventListener('load', applyBase, { once: true });\n"
            "  for (const t of [300, 1200, 3000, 6000]) setTimeout(applyBase, t);\n"
            "})()")


# 在游戏帧（OOPIF）里量「真正的画布」。
# 为什么要跑进帧里：这游戏是 Unity WebGL，DOM 结构是
#   父页面 → iframe#game_frame → iframe#unityPlayer → canvas
# 画布还被封顶在 ~1281×720（比舞台小），所以父页面看到的 iframe 盒子是偏大的，
# 拿它当识别区域就会：① 画面里带黑边 ② 内容贴左上角（人看着「不居中」）
# ③ 点击坐标整体偏。这里把画布在帧内的位置量出来，父页面加上 iframe 的偏移就准了。
#
# **还有一个更底层的坑**（2026-11-17 实测出来的，就是「首页底部按钮显示不全」）：
# 站点的 Unity 模板把 #unityPlayer 这个 iframe 设成 `inner + 40 × inner + 80`，
# 比它所在的游戏帧还大；画布带着 `margin: 20px 0 0 20px` 摆在里面。于是
#   帧视口 1280×720、#unityPlayer 1320×800、画布 (20,40,1280,720)
# 画布右下角被推出帧视口 → 直接被 iframe 裁掉 **右 20px、下 40px**（底部一排
# 按钮就是这 40px 里的）。注意这跟父页面怎么摆无关：iframe 的内容是先按自己的
# 盒子裁好的，在父页面挪 iframe 一点用都没有。
# 解法：只把 #unityPlayer 改成「帧视口大小」（100vw × 100vh）。Unity 的容器本来
# 就是「画布 + 40」再居中（所以它自己的 `-20,-20` 刚好被画布那圈 20 的 margin
# 抵消），#unityPlayer 一旦等于视口，画布就正好落在 (0,0,1280,720) —— 一个像素
# 都不裁。**千万别同时把画布 margin 清掉**，清了画布会跑到 (-20,-20)，反而裁右下。
FRAME_CSS = (
    "#unityPlayer{width:100vw !important;height:100vh !important;"
    "left:0 !important;top:0 !important;right:auto !important;"
    "bottom:auto !important}"
    "html,body{overflow:hidden !important}"
)

CANVAS_JS = r"""(() => {
  const rc = (e) => { const r = e.getBoundingClientRect();
                      return [r.x, r.y, r.width, r.height]; };
  const area = (e) => { const r = e.getBoundingClientRect(); return r.width * r.height; };
  const cvs = [...document.querySelectorAll('canvas')].sort((a, b) => area(b) - area(a));
  if (cvs.length) return JSON.stringify({how: 'canvas', inner: [innerWidth, innerHeight],
                                        rect: rc(cvs[0])});
  const u = document.getElementById('unityPlayer') || document.querySelector('iframe');
  if (!u) return JSON.stringify({how: 'none', inner: [innerWidth, innerHeight]});
  const out = {how: 'iframe', inner: [innerWidth, innerHeight], rect: rc(u),
               inner2: null, rect2: null};
  try {                             // Unity 那层通常同源，能再往里量一步
    const d = u.contentDocument;
    if (d) {
      const c = d.querySelector('canvas');
      if (c) { out.inner2 = [u.contentWindow.innerWidth, u.contentWindow.innerHeight];
               out.rect2 = rc(c); }
    }
  } catch (e) { /* 跨域就算了，用 unityPlayer 的盒子 */ }
  return JSON.stringify(out);
})()"""


class GameSession:
    """一条「浏览器里的游戏」会话。"""

    def __init__(self, cfg: dict, log, root: Path | None = None):
        self.log = log
        self.cfg = cfg                       # 原始配置：浮层/端口等后面还要用
        self.root = Path(root or paths.app_dir())
        b = cfg.get("browser", {}) or {}
        self.port = int(b.get("port", 9222) or 9222)
        self.url = str(b.get("url", "") or "")
        self.window = str(b.get("window", "1440x860") or "1440x860")
        self.auto_launch = bool(b.get("auto_launch", True))
        self.ready_timeout = float(b.get("ready_timeout", 15) or 15)
        self.user_data = Path(b.get("user_data") or (self.root / "chrome-data"))
        if not self.user_data.is_absolute():
            self.user_data = self.root / self.user_data
        self.chrome = str(b.get("chrome") or "") or paths.find_chrome()
        # 可选的登录态注入（见 prepare 里的说明）
        self.jwt = str(b.get("jwt") or "").strip()
        self._jwt_done = False

        vp = str(b.get("viewport", "2560x1440") or "2560x1440").lower()
        try:
            self.vw, self.vh = (int(v) for v in vp.split("x"))
        except Exception:
            self.vw, self.vh = 2560, 1440

        self.br = Browser(self.port)
        self.cdp: CDP | None = None
        self.target_id = ""
        self.mode = ""            # 实际驱动方式（都是 page；这里只记是不是套了 iframe）
        # 游戏画面在视口里的位置和尺寸（CSS px）。默认铺满。
        self.geo = {"x": 0, "y": 0, "w": float(self.vw), "h": float(self.vh)}
        self.status = "init"      # init / need_login / loading / ready / error
        self.hint = ""
        self.login_err = ""       # 游戏内登录错误码（如 1002），空表示没报错
        self._proc: subprocess.Popen | None = None
        self._last_prepare = 0.0
        self._vp_mode = ""        # "" / "window"（视口 = 窗口，已经是唯一模式）
        self._css_script_id = ""  # 「新文档自动注入样式」的脚本 id
        self._overlay_sid = ""    # 「新文档自动注入助手浮层」的脚本 id
        self._overlay_cdp = None  # 上面那个 id 是挂在哪个 CDP 连接上的
        self._overlay_port = 0    # 浮层里写死的面板端口（端口变了要重注入）
        self._overlay_ok = False  # 最近一次探活：浮层节点还在文档里吗
        self._overlay_misses = 0  # 连续没装上的次数（只为压日志，不影响逻辑）
        self._overlay_logged = False   # 「浮层已并入游戏窗口」只报一次
        self._overlay_check_at = 0.0   # 上次巡检时间（节流用）
        self._overlay_lock = threading.RLock()   # 补注浮层要串行（见 _ensure_overlay）
        self._stage_size = (0, 0) # 舞台尺寸（0,0 = 用「窗口内最大 16:9」）
        self._stage_shift = (0, 0)  # 舞台的平移量（把画布挪到窗口正中）
        self._stage_scale = 1.0   # 舞台放大倍数（吃掉画布封顶带来的黑边）
        self._last_geo = 0.0      # 上次检查窗口尺寸的时间
        self._last_measure = 0.0  # 上次「顺手重量画布」的时间
        self._settled = False     # 已经做过「加载后一次性对齐」：之后不再动
        # 对齐互斥锁：对齐要跑 3~6 秒，期间「守护线程」和「任务线程」很容易撞一起
        # （跑完任务会重量一次窗口、守护线程同时也在看）。两边交替改 CSS 会让
        # 彼此的测量互相污染，位移一路累加到 2052px，游戏被推出窗口整块黑屏。
        self._settle_lock = threading.RLock()
        self._frame_sid = ""      # 游戏帧（OOPIF）的调试会话，用来进去量画布
        self._cv_local = None     # 画布在游戏帧里的原始矩形 [x,y,w,h]（含游戏自己的边距）
        self._stage_off = (0, 0)  # iframe 的反向偏移（把画布顶回舞台左上角）
        self._frame_tid = ""
        self._maxed = False
        self.maximize = bool(b.get("maximize", True))
        # 要不要把画布用 CSS 放大到铺满窗口。
        # true  = 黑边基本没有，但画面是被放大插值的（会略糊）；
        # false = 保持 1280×720 原生大小居中，黑边大但一个像素都不插值。
        self.fill = bool(b.get("fill", True))
        # 加载完要不要**自动**把网页缩放拨到 100%（系统级：发真的 Ctrl+= / Ctrl+0）。
        # 默认开：连接后自动执行一次（抢前台焦点 1~2 秒），_zoom_done 保证只做一次。
        # 不想自动做就在配置里关掉，随时点面板上的「缩放 100%」手动执行。
        self.zoom_auto = bool(b.get("zoom_auto", True))
        self._zoom_done = False    # 每次加载只自动做一次
        self._bpid = 0             # 浏览器主进程 pid（缓存）
        self._hwnd = 0             # 游戏窗口句柄（缓存，过期会自动重找）
        self._title_hint = ""      # 网页标题（认窗口用）
        self.info: dict = {}

    # ------------------------------------------------------------ Chrome
    def _dedupe_windows(self) -> None:
        """同一份 profile 里堆了多个游戏窗口就只留一个。

        为什么会堆：Chrome 用 `--app=URL` 启动时，如果这份 profile 已经有实例在跑，
        它不会复用那个窗口，而是**再开一个新窗口**。调试期间反复启动就会看到一排
        「日常助手」。这里主动收拾掉多余的，省得窗口越堆越多。
        """
        try:
            pages = [t for t in self.br.targets()
                     if t.get("type") == "page"
                     and "cloud_game" in str(t.get("url") or "")]
        except Exception:
            return
        if len(pages) <= 1:
            return
        keep = self.target_id or str(pages[0].get("id") or "")
        extra = [t for t in pages if str(t.get("id") or "") != keep]
        for t in extra:
            try:
                self.br.close_tab(str(t.get("id")))
            except Exception:
                pass
        if extra:
            self.log.info(f"关掉了 {len(extra)} 个多余的游戏窗口（只留一个）")

    def ensure_browser(self) -> bool:
        """连上或拉起带调试端口的 Chrome。"""
        if self.br.alive():
            self._dedupe_windows()
            return True
        if not self.auto_launch:
            self.status, self.hint = "error", f"没检测到调试端口 {self.port}，且关闭了自动启动"
            return False
        if not self.chrome:
            self.status, self.hint = "error", "找不到 Chrome/Edge，请在 config.yaml 里写 browser.chrome"
            return False

        self.user_data.mkdir(parents=True, exist_ok=True)
        args = [
            self.chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data}",
            f"--window-size={self.window.replace('x', ',')}",
            "--window-position=40,20",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-crash-restore-bubble",
            # 注意：这里**不要**加 --force-device-scale-factor=1。
            # 那会把整个窗口压成 1x —— 4K 屏上字小到看不清（登录时人得看得见）。
            # 画布 2x 的浪费已经由识别阶段的 Emulation.setDeviceMetricsOverride
            # （deviceScaleFactor:1）解决了，那个只管游戏页，不影响窗口观感。
            # 窗口被遮挡/最小化时浏览器会停止渲染，画面就冻住了 —— 全部关掉
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion",
            "--autoplay-policy=no-user-gesture-required",
            "--app=" + (self.url or "about:blank"),
        ]
        try:
            self.log.info(f"启动浏览器：{Path(self.chrome).name}（调试端口 {self.port}）")
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except Exception as e:
            self.status, self.hint = "error", f"启动浏览器失败：{e}"
            return False

        for _ in range(60):
            if self.br.alive():
                return True
            time.sleep(0.25)
        self.status, self.hint = "error", "浏览器起来了但调试端口没响应"
        return False

    # ------------------------------------------------------------ target
    def _candidates(self) -> list[dict]:
        out = []
        for t in self.br.targets():
            if t.get("type") not in ("page", "iframe"):
                continue
            u = str(t.get("url") or "")
            if u.startswith(("chrome-extension://", "devtools://", "chrome://")):
                continue
            out.append(t)
        return out

    def _pick(self, cands: list[dict]) -> dict | None:
        """挑承载游戏的 target —— **永远是顶层页面**。

        为什么不用 iframe 那个 target：实测 Chrome 的 `Page.startScreencast`
        和 `Emulation.setDeviceMetricsOverride` 都只允许在 top-level target 上执行
        （报 "Command can only be executed on top-level targets"）。
        而父页面上的 screencast 会**把跨域 iframe 的内容一起合成进去**，
        `Input.dispatchMouseEvent` 也能穿过 iframe 命中里面的画布 ——
        所以驱动父页面就够了，跨域不跨域都一样。
        """
        pages = [t for t in cands if t.get("type") == "page"]
        for t in pages:
            if "cloud_game" in str(t.get("url") or ""):
                return t
        for t in pages:
            if str(t.get("url") or "").startswith(("http://", "https://")):
                return t
        return None

    def attach(self) -> bool:
        """找到并连上游戏页面。已连且没断就直接复用。"""
        if self.cdp is not None and not self.cdp.error:
            if any(t.get("id") == self.target_id for t in self.br.targets()):
                return True
            self.close()

        cands = self._candidates()
        t = self._pick(cands)
        if t is None:
            self.status, self.hint = "loading", "还没打开游戏页面"
            return False

        try:
            cdp = CDP(t["webSocketDebuggerUrl"])
            cdp.call("Page.enable", timeout=10)
            cdp.call("Runtime.enable", timeout=10)
        except Exception as e:
            self.status, self.hint = "error", f"连不上页面：{e}"
            return False

        self.cdp = cdp
        self.target_id = str(t.get("id") or "")
        self.mode = "page"
        # 新会话不保证继承旧会话的视口覆盖，标记成「未知」，让 prepare 重新清一次
        self._vp_mode = ""
        self._settled = False        # 新页面：重新对齐一次
        self._zoom_done = False      # 新页面：自动缩放也重来一次
        self._stage_size = (0, 0)
        self._stage_shift = (0, 0)
        self._stage_scale = 1.0
        self._stage_off = (0, 0)
        # 订一份「每个新文档都注入样式」的脚本：游戏页连上服务器会整页导航，
        # 只在 prepare 里注一次会被冲掉（这是不居中问题的根因）
        self._register_css_script()
        self.log.info(f"已连接游戏画面（{str(t.get('url'))[:80]}）")
        return True

    # ------------------------------------------------------------ 画布定位
    def _frame_session(self) -> str:
        """拿游戏帧（跨域 iframe，OOPIF）的调试会话，用来钻进去量画布。

        为什么不直接用 iframe 的盒子：游戏把画布封顶在 ~1281×720，比 iframe 小，
        还贴在左上角。盒子当识别区域 → 画面带黑边、点击整体偏移。
        这里是整条链路里唯一能拿到「画布真实位置」的路子。
        """
        cdp = self.cdp
        if cdp is None:
            return ""
        if self._frame_sid:
            return self._frame_sid
        try:
            tg = cdp.call("Target.getTargets", {}, timeout=6) or {}
        except CDPError as e:
            self.log.warning(f"列 target 失败（先按 iframe 盒子算）：{e}")
            return ""
        cands = []
        for t in tg.get("targetInfos") or []:
            if t.get("type") != "iframe":
                continue
            u = str(t.get("url") or "")
            if not u.startswith("http") or "signup" in u or "/login" in u:
                continue
            cands.append((0 if "game" in u.lower() else 1, u, str(t.get("targetId") or "")))
        for _, u, tid in sorted(cands):
            try:
                r = cdp.call("Target.attachToTarget",
                             {"targetId": tid, "flatten": True}, timeout=6) or {}
                sid = str(r.get("sessionId") or "")
                if sid:
                    self._frame_sid, self._frame_tid = sid, tid
                    self.log.info(f"已挂上游戏帧（{u[:70]}），可以精确量画布")
                    return sid
            except CDPError as e:
                self.log.warning(f"挂游戏帧失败：{e}")
        return ""

    def _canvas_box(self) -> dict:
        """在游戏帧里量画布：返回 {how, inner, rect, [inner2, rect2]}。"""
        sid = self._frame_session()
        if not sid or self.cdp is None:
            return {}
        try:
            raw = self.cdp.evaluate(CANVAS_JS, timeout=4, session_id=sid)
            return json.loads(raw) if raw else {}
        except Exception:
            self._frame_sid = self._frame_tid = ""    # 目标换了/断了，下次重新挂
            return {}

    def _ensure_frame_css(self) -> bool:
        """在**游戏帧自己的文档**里塞一条样式：把 #unityPlayer 撑成帧视口大小。

        为什么必须进帧里改：`#unityPlayer` 是游戏帧里的 iframe，父页面的 CSS
        选不到它；而且 iframe 的内容是先按自己的盒子裁好再交给父页面摆放的，
        在父页面挪位置救不回被裁掉的那一块（见 FRAME_CSS 上面的长注释）。

        只写样式、不碰尺寸计算 —— 100vw/100vh 会跟着帧视口自动变，所以窗口
        大小变了不用重算；同一份 CSS 重复写不会触发重排（比较过 textContent）。
        """
        sid = self._frame_session()
        if not sid or self.cdp is None:
            return False
        css = json.dumps(FRAME_CSS)
        js = ("(() => {\n"
              "  let s = document.getElementById('__kyweb_frame');\n"
              "  if (!s) { s = document.createElement('style'); s.id = '__kyweb_frame';\n"
              "            (document.head || document.documentElement).appendChild(s); }\n"
              f"  if (s.textContent !== {css}) s.textContent = {css};\n"
              "  return s.textContent.length;\n"
              "})()")
        try:
            self.cdp.evaluate(js, timeout=4, session_id=sid)
            return True
        except Exception:
            self._frame_sid = self._frame_tid = ""    # 帧换了/断了，下次重新挂
            return False

    @staticmethod
    def _geo_from_frame(ifr: list, box: dict) -> dict | None:
        """iframe 盒子 + 帧内画布盒子 → 画布在父页面 CSS 坐标里的位置。

        缩放比 = iframe 实际宽 / 帧的 innerWidth（边框、滚动条都不为 0 时也能对上），
        画布若还套了一层（Unity 的 unityPlayer），就再乘一层它的缩放比。

        小于 320×180 的一律当「还没初始化」丢掉：Unity 在真正起来之前，
        iframe 就是 300×150 的默认尺寸，拿它当画布会裁出一个角落的小图。
        """
        MIN_W, MIN_H = 320.0, 180.0
        try:
            ix, iy, iw, ih = (float(ifr[0]), float(ifr[1]), float(ifr[2]), float(ifr[3]))
            inner = box.get("inner") or [iw, ih]
            sx = iw / float(inner[0]) if float(inner[0] or 0) else 1.0
            sy = ih / float(inner[1]) if float(inner[1] or 0) else 1.0
            how = box.get("how")
            cand = None
            if how == "canvas":
                r = box["rect"]
                cand = {"x": ix + float(r[0]) * sx, "y": iy + float(r[1]) * sy,
                        "w": float(r[2]) * sx, "h": float(r[3]) * sy}
            elif how == "iframe":
                r = box["rect"]
                bx, by = ix + float(r[0]) * sx, iy + float(r[1]) * sy
                bw, bh = float(r[2]) * sx, float(r[3]) * sy
                r2, inner2 = box.get("rect2"), box.get("inner2")
                if r2 and inner2 and float(inner2[0] or 0):
                    kx = bw / float(inner2[0])
                    ky = bh / float(inner2[1]) if float(inner2[1] or 0) else kx
                    cand = {"x": bx + float(r2[0]) * kx, "y": by + float(r2[1]) * ky,
                            "w": float(r2[2]) * kx, "h": float(r2[3]) * ky}
                else:
                    cand = {"x": bx, "y": by, "w": bw, "h": bh}
            if cand and cand["w"] >= MIN_W and cand["h"] >= MIN_H:
                return cand
        except Exception:
            return None
        return None

    def _sync_viewport(self) -> bool:
        """把窗口的真实 CSS 尺寸读进 self.vw/vh。

        为什么必须先读：self.vw/vh 的初值是 config 里的模板基准（2560×1440），
        而第一个 measure() 才会更新它。如果在这之前就 ensure_css()，
        舞台会被设成 2560×1440 —— 比窗口大一圈，整块跑到屏幕外（实测
        stage rect = (-320,-215,2560,1440)），游戏量出来的画布位置也全错。
        """
        cdp = self.cdp
        if cdp is None:
            return False
        try:
            w, h = json.loads(cdp.evaluate("JSON.stringify([innerWidth, innerHeight])"))
            w, h = float(w), float(h)
        except Exception:
            return False
        if w <= 0 or h <= 0:
            return False
        self.vw, self.vh = int(round(w)), int(round(h))
        return True

    def _fit_size(self) -> tuple[int, int]:
        """窗口内能放下的最大 16:9 尺寸（整数像素）。"""
        w = min(float(self.vw), float(self.vh) * 16 / 9)
        h = w * 9 / 16
        return max(16, int(round(w))), max(9, int(round(h)))

    def _guard_shift(self, shift: tuple[int, int]) -> tuple[int, int]:
        """位移闸门：超过窗口 1/3 的位移一律削平。

        正常对齐要用的位移只有几十像素（画布在舞台里偏了一点点）。出现几百、
        上千像素只可能是**测量坏了**，照着做就是把整个游戏推出窗口 —— 实测
        日志里 `偏移 (2052, 1155)`，用户看到的就是游戏窗口一片全黑。
        宁可这次不挪，也不能把人正在看的画面弄没。
        """
        mx = max(120, int(self.vw / 3))
        my = max(120, int(self.vh / 3))
        return (max(-mx, min(mx, int(shift[0]))), max(-my, min(my, int(shift[1]))))

    def _apply_stage(self, size: tuple[int, int],
                     shift: tuple[int, int] | None = None,
                     k: float | None = None,
                     off: tuple[int, int] | None = None) -> bool:
        """把舞台设成指定尺寸/位移/缩放（+ iframe 反向偏移），立刻作用到当前文档。"""
        cdp = self.cdp
        if cdp is None:
            return False
        self._stage_size = (int(size[0]), int(size[1]))
        if shift is not None:
            self._stage_shift = self._guard_shift(shift)
        if k is not None:
            self._stage_scale = float(k)
        if off is not None:             # 反向偏移只是抹掉游戏那圈 20~40px 边距
            self._stage_off = (max(-400, min(400, int(off[0]))),
                               max(-400, min(400, int(off[1]))))
        try:
            return bool(cdp.evaluate(_css_now_js(PAGE_CSS, self.stage_css())))
        except CDPError:
            return False

    def _canvas_visible(self, need: float = 0.5, probe: bool = True) -> bool:
        """量到的画布还有没有「大半在窗口里」—— 黑屏就是这条不成立。

        `probe=False` 表示调用方刚刚自己量过（省一次 JS 往返）。
        """
        if probe and self.measure() is None:
            return False
        g = self.geo
        if g["w"] < 320 or g["h"] < 180:
            return False
        vw, vh = float(self.vw), float(self.vh)
        ix = min(vw, g["x"] + g["w"]) - max(0.0, g["x"])
        iy = min(vh, g["y"] + g["h"]) - max(0.0, g["y"])
        if ix <= 0 or iy <= 0:
            return False
        return (ix * iy) >= need * g["w"] * g["h"]

    def needs_settle(self) -> bool:
        """画面还没做过「加载后的一次性对齐」。"""
        return (not self._settled) and self.status == "ready"

    def _wait_canvas(self, timeout: float = 6.0) -> bool:
        """等游戏把画布重新建出来。

        改 iframe 尺寸之后，Unity 会先把 canvas 拆掉再重建，
        这中间的一两秒里「帧里没有画布」—— 这时候 measure() 会退回 iframe 盒子，
        拿它当画布去居中就会把舞台推歪（实测偏移被推到 304px）。
        """
        deadline = time.time() + timeout
        while True:
            box = self._canvas_box() or {}
            # 画布可能直接在游戏帧里，也可能在游戏帧里的 #unityPlayer 里（同源，
            # 量得到 rect2）。两种都算「画布已经在」。
            r = box.get("rect2") if box.get("how") == "iframe" else box.get("rect")
            if r and r[2] >= 320 and r[3] >= 180:
                self._cv_local = [float(v) for v in r]
                return True
            if time.time() >= deadline:
                self.log.warning("等画布重建超时（先按 iframe 盒子继续）")
                return False
            time.sleep(0.35)

    # ------------------------------------------- 把网页缩放拨到 100%（系统级）
    def _vp_probe(self) -> tuple[float, float, float] | None:
        """读一下 innerWidth / innerHeight / devicePixelRatio。

        这三个数就是「页面缩放到多少」的指纹：90% 时 innerWidth 会变大
        1/0.9 倍、devicePixelRatio 变成 0.9 倍（实测 1920→2133、2.0→1.8）。
        """
        if self.cdp is None:
            return None
        try:
            iw, ih, dpr = json.loads(self.cdp.evaluate(
                "JSON.stringify([innerWidth, innerHeight, devicePixelRatio])"))
            return float(iw), float(ih), float(dpr)
        except (CDPError, ValueError, TypeError):
            return None

    def _browser_pid(self) -> int:
        """浏览器主进程的 pid（用来找它的窗口）。

        三条路，按可靠性排：

        1. **自己拉起来的进程**：`ensure_browser()` 里 `Popen` 的那个就是浏览器
           主进程（Chrome 用独立 profile 启动时不会转交给已有实例）。这条最准，
           也最快，基本一次就成。
        2. **浏览器级 CDP**：`SystemInfo.getProcessInfo` 只挂在 browser target 上，
           在**页面会话**里调会报 "wasn't found" —— 之前就是踩了这个坑，pid 永远
           是 0，于是「找不到游戏窗口」。这里改成新开一条 browser 级 WebSocket
           专门问一次。
        3. 都没有就返回 0，交给 `_browser_hwnd()` 用「进程名 + 窗口标题」兜底。
        """
        p = self._proc
        if p is not None and p.poll() is None:
            self._bpid = int(p.pid)
            return self._bpid
        if self._bpid:
            return self._bpid

        pid = 0
        try:
            ws = str((self.br.version() or {}).get("webSocketDebuggerUrl") or "")
            if ws:
                bcdp = CDP(ws, timeout=6)
                try:
                    r = bcdp.call("SystemInfo.getProcessInfo", {}, timeout=6) or {}
                finally:
                    bcdp.close()
                for it in r.get("processInfo") or []:
                    if it.get("type") == "browser":
                        pid = int(it.get("id") or 0)
                        break
        except Exception:            # 老版本 Chrome / 域被关掉：不是致命问题
            pid = 0
        self._bpid = pid
        return pid

    def _page_title(self) -> str:
        """网页标题（`--app` 模式下窗口标题就是它），用来认窗口。"""
        if self._title_hint:
            return self._title_hint
        if self.cdp is None:
            return ""
        try:
            t = self.cdp.evaluate("document.title") or ""
            if isinstance(t, str):
                self._title_hint = t.strip()
        except (CDPError, TypeError):
            pass
        return self._title_hint

    def _browser_hwnd(self) -> int:
        """游戏那个 Chrome 窗口的句柄。

        先按 pid 找，找不到再按「浏览器进程名 + 网页标题」找 —— 后者不依赖
        CDP 的 pid 探测，只要窗口在就能认出来。
        """
        if winput is None or not winput.available():
            return 0
        if self._hwnd and winput.is_window(self._hwnd):
            return self._hwnd
        pid = self._browser_pid()
        hwnd = winput.biggest_window(pid) if pid else 0
        if not hwnd:
            hwnd = winput.browser_window(self._page_title(), pid)
        if not hwnd:
            hwnd = winput.browser_window(self._page_title())
        if hwnd:
            self._hwnd = hwnd
        return hwnd

    def zoom_to_100(self) -> dict:
        """把网页缩放拨到 100% —— **系统级**发真的键盘事件。

        为什么必须这样：Ctrl+滚轮 / Ctrl+= / Ctrl+0 都是**浏览器进程**的快捷键，
        CDP 的合成事件只到渲染进程（实测发完 wheel 后 innerWidth / dpr 一点没变）。
        唯一生效的办法是往系统输入队列塞键盘事件，也就是抢焦点假装有人在敲。

        做法是「先放大一档，再复位到 100%」：
          · Ctrl+=  → 当前 90% 变 100%（若本来 100% 就变 110%）
          · Ctrl+0  → 无条件回到 100%
        这样不管点之前是多少，点完一定停在 100%。中途那个「放大一档」也正好
        重现了用户手动干预的效果（这游戏吃到一次真缩放才会重排画布）。

        返回值给接口用：{ok, before, after, moved, hint}。
        """
        if winput is None or not winput.available():   # pragma: no cover
            return {"ok": False, "hint": "只有 Windows 能发系统级按键"}
        if self.cdp is None or self.cdp.error:
            return {"ok": False, "hint": "画面还没连上"}
        before = self._vp_probe()
        if before is None:
            return {"ok": False, "hint": "读不到页面尺寸"}
        hwnd = self._browser_hwnd()
        if not hwnd:
            # 把当时能看到的浏览器窗口写进日志，出问题能一眼看出是哪一步空的
            try:
                detail = winput.describe()
            except Exception:
                detail = "（枚举窗口失败）"
            self.log.warning(f"找不到游戏窗口：pid={self._bpid or '未知'}；"
                             f"当前浏览器窗口：{detail}")
            return {"ok": False,
                    "hint": "找不到游戏窗口：浏览器可能被关了/最小化，或它在别的桌面上（日志里列了当时的窗口）"}
        if not winput.set_foreground(hwnd):
            return {"ok": False, "hint": "游戏窗口没能切到前台，按键会打到别处，已放弃"}

        self.log.info("手动缩放：窗口已切到前台，发送 Ctrl+= 与 Ctrl+0…")
        n1 = winput.press(winput.VK_OEM_PLUS, ctrl=True)
        time.sleep(0.7)
        mid = self._vp_probe()
        moved = bool(mid and abs(mid[0] - before[0]) > 4)

        n2 = winput.press(winput.VK_0, ctrl=True)
        time.sleep(0.7)
        after = self._vp_probe()
        if not n1 and not n2:
            return {"ok": False, "hint": "系统拒绝接收模拟按键（可能被安全软件或 UAC 拦住）"}

        # Ctrl+0 没生效的特征：读数还停在「放大一档」那个值上（跟 mid 一样、
        # 和 before 不同）。这时候再补一次 —— 通常是按键被抢走焦点吃掉了。
        if mid and after and abs(after[0] - mid[0]) <= 2 and abs(after[0] - before[0]) > 4:
            winput.set_foreground(hwnd)
            winput.press(winput.VK_0, ctrl=True)
            time.sleep(0.6)
            after = self._vp_probe()

        self._sync_viewport()
        self._zoom_done = True
        self._log_zoom(before, mid, after, moved)
        if not moved:
            return {"ok": False, "before": self._fmt_vp(before), "after": self._fmt_vp(after),
                    "moved": False,
                    "hint": "按键没生效（可能本来就是 100%，或窗口没拿到焦点）"}
        return {"ok": True, "before": self._fmt_vp(before), "after": self._fmt_vp(after),
                "moved": True, "hint": ""}

    def zoom_and_settle(self) -> dict:
        """缩放拨到 100% + 重新对齐 —— 面板「缩放100%」按钮和任务的同一套逻辑。

        缩放改了视口，必须重新对齐一次，否则截图坐标全错。缩放是系统级按键，
        `zoom_to_100` 在「本来就是 100%」时返回 ok=False/moved=False，照实返回。
        """
        if self.cdp is None or self.cdp.error:
            return {"ok": False, "hint": "画面还没连上"}
        r = self.zoom_to_100()
        if not r.get("ok"):
            return r
        # 视口变了 → 之前算好的舞台尺寸/偏移作废，重新铺满居中
        self._settled = False
        self._stage_size = (0, 0)
        self._stage_shift = (0, 0)
        ok = self.settle_layout()
        r["settled"] = bool(self._settled)
        r["stage"] = list(self._stage_size)
        r["scale"] = round(self._stage_scale, 3)
        if not ok:
            r["hint"] = "缩放调好了，但画面没对齐成功，看日志"
        return r

    @staticmethod
    def _fmt_vp(p) -> list:
        return [] if not p else [round(p[0], 1), round(p[1], 1), round(p[2], 2)]

    def _log_zoom(self, before, mid, after, moved: bool) -> None:
        """把三次读数写进日志（排查「点完没变化」时就看这一行）。"""
        def fmt(p):
            return "无" if not p else f"{p[0]:.0f}×{p[1]:.0f} dpr{p[2]:.2f}"
        tail = "（按键生效，已复位到 100%）" if moved else "（按键没生效）"
        self.log.info(f"缩放读数：{fmt(before)} → {fmt(mid)} → {fmt(after)}{tail}")

    def settle_layout(self) -> bool:
        """对齐入口：加锁，保证同一时间只有一轮在对齐（见 `_settle_lock`）。"""
        if not self._settle_lock.acquire(blocking=False):
            self.log.info("已经有一轮对齐在进行中，这次跳过（避免两边互相冲突）")
            return False
        try:
            return self._settle_layout()
        finally:
            self._settle_lock.release()

    def wait_settle(self, timeout: float = 15.0) -> bool:
        """等正在跑的那一轮对齐结束（最多 timeout 秒）。

        「开工前」专用：这时候跳过对齐会让第一次点击用旧坐标（画布被挪了 514px
        就必然点空），而等它落地通常只要两三秒 —— 等比跳过安全得多。
        """
        got = self._settle_lock.acquire(timeout=max(0.0, timeout))
        if got:
            self._settle_lock.release()
        if not got:
            self.log.warning(f"对齐一直没结束（等了 {timeout:.0f}s），先继续")
        return got

    def _settle_layout(self) -> bool:
        """加载完成后**只做一次**的对齐：舞台设成画布尺寸 → 放大铺满 → 居中。

        三条硬约束（都是实测踩出来的）：

        1. **舞台尺寸必须等于画布原生尺寸**（这游戏是 1280×720）。
           以前把舞台设成「窗口内最大 16:9」（1920×1009 窗口下是 1794×1009），
           游戏就按 1794×1009 排版、却被裁在 1280×720 的画布里 —— 表现就是
           「画面显示不全」。两边一致之后画布自己就完整了。
        1.5 **先把帧里的 #unityPlayer 撑到帧视口大小**（`_ensure_frame_css`）。
           站点模板把它设成 inner+40 × inner+80，画布右下角被 iframe 裁掉
           右 20px、下 40px —— 首页底部那排按钮就是被这 40px 吃掉的。
        2. **放大倍数不能让舞台超出窗口**。以前舞台 1794×1009 再乘 1.4 倍，
           变成 2511×1413，超出窗口的部分直接被裁掉 —— 那就是用户截图里
           「只看到画面中间一块」的样子。现在 k = min(窗口宽/画布宽, 窗口高/画布高)。
        3. **只做一次**（`_settled`）。之后只有窗口尺寸变化或重新加载游戏才重做，
           过场动画期间绝不碰画面。

        这里**只动 DOM/CSS**，不碰鼠标键盘、不抢前台、不改浏览器缩放 ——
        运行期间你可以正常用电脑。触发时机由 gui_server 决定（认到标题画面的
        GAME 字样、或画面静止之后）。
        """
        if self.cdp is None:
            return False
        if self._settled:
            return False
        self._sync_viewport()
        # 进来先记一份「现在长什么样」：万一对齐算歪了，退回这里至少还看得见。
        snap = (self._stage_size, self._stage_shift, self._stage_scale, self._stage_off)
        # 上一次留下的反向偏移必须清掉：它是按上一次的边距算的，窗口/画布一变
        # 就是错的；而 `_apply_stage(off=None)` 会保留旧值，于是错值一路累积。
        self._stage_off = (0, 0)

        # ① 先把舞台设成「窗口内最大 16:9」，让游戏按一个规整尺寸启动
        #    （量到画布之后再换成画布原生尺寸，见第 ② 步）
        w0, h0 = self._fit_size()
        self._apply_stage((w0, h0), (0, 0), 1.0, (0, 0))
        # ①.5 帧里那一层撑到视口（不然画布右下角被裁，底部按钮显示不全）
        self._ensure_frame_css()
        time.sleep(0.35)

        self._wait_canvas()
        if self.measure() is None:
            return False

        # ② 舞台 = 画布原生尺寸；量不到画布就退回窗口内最大 16:9
        cw, ch = int(round(self.geo["w"])), int(round(self.geo["h"]))
        off = (0, 0)
        if cw >= 320 and ch >= 180:
            w, h = cw, ch
            lc = self._cv_local
            if lc and (lc[0] > 1 or lc[1] > 1):
                # 游戏自己的容器给画布留了边距（实测画布在帧内 (20,40)）。
                # 舞台收成画布尺寸后，这圈边距会把画布顶出右下角 —— 反向挪回去。
                off = (-int(round(lc[0])), -int(round(lc[1])))
        else:
            w, h = w0, h0

        # ③ 要不要把画面放到铺满窗口。
        #    fill=false 就保持原生尺寸（1:1、不插值），黑边留着；
        #    放大时**不许超出**窗口，否则超出部分会被窗口直接裁掉
        #    （以前就是「舞台已经是窗口大小了又乘 1.4」，画面只露出中间一块）。
        k = 1.0
        if self.fill:
            k = min(float(self.vw) / w, float(self.vh) / h)
            k = max(0.3, min(2.0, k))
        self._apply_stage((w, h), (0, 0), k, off)
        self._wait_canvas()
        time.sleep(0.4)
        self.measure()

        # ④ 把画布顶到舞台左上角。
        #      游戏自己会给画布留边距（实测帧内 (20,40)、还可能因为它自己居中而变），
        #      预测不如实测：量一次「画布现在在哪、舞台在哪」，差多少就挪多少。
        #      注意 iframe 的平移在舞台的 scale 之内，屏幕上的位移要除以 k。
        #      **位移超过半个舞台就当测量坏了直接放弃** —— 这时候继续挪只会把
        #      画面越推越远（实测连挪三次累积到 2052px，游戏整块黑屏）。
        for _ in range(3):
            info = self.measure()
            if info is None or info.get("geo_src") != "frame":
                break
            st = info.get("stage") or [0, 0, 0, 0]
            ddx = float(st[0]) - self.geo["x"]
            ddy = float(st[1]) - self.geo["y"]
            if abs(ddx) <= 2 and abs(ddy) <= 2:
                break
            if abs(ddx) > w * 0.5 or abs(ddy) > h * 0.5:
                self.log.warning(f"画布位置量得不对劲（差 {ddx:.0f},{ddy:.0f}px），跳过纠偏")
                break
            off = (off[0] + int(round(ddx / k)), off[1] + int(round(ddy / k)))
            self._apply_stage((w, h), (0, 0), k, off)
            self._wait_canvas()
            time.sleep(0.5)

        # ⑤ 居中：绝对位移（transform 只平移不改尺寸），两三次就收敛。
        #    只在「量到的确实是画布」时才调 —— 退回 iframe 盒子时居中会把舞台推歪。
        for _ in range(3):
            if self.measure() is None or (self.info or {}).get("geo_src") != "frame":
                break
            dx = int(round(self.vw / 2 - (self.geo["x"] + self.geo["w"] / 2)))
            dy = int(round(self.vh / 2 - (self.geo["y"] + self.geo["h"] / 2)))
            if abs(dx) <= 2 and abs(dy) <= 2:
                break
            if abs(dx) > self.vw * 0.5 or abs(dy) > self.vh * 0.5:
                self.log.warning(f"居中要挪 {dx},{dy}px —— 太离谱了，放弃居中")
                break
            cx, cy = self._stage_shift
            self._apply_stage((w, h), (cx + dx, cy + dy), k, off)
            time.sleep(0.45)

        # ⑥ 验收：画布必须「看得见」。看不见就说明这一轮算歪了，退回能看见的样子
        #    （上一次的排布还在窗口里就用它，否则退回 1:1 铺满）。宁可画面小一点、
        #    也不许出现黑屏 —— 这是用户直接反馈过的问题。
        time.sleep(0.2)                 # 让最后一次样式落地，再量
        if not self._canvas_visible(0.8):
            self.log.warning("对齐结果把画面推出了窗口，正在回退（避免黑屏）")
            sz, sh, kk, off2 = snap
            self._apply_stage(sz if sz[0] > 0 else (w0, h0), sh, kk, off2)
            self.measure()
            if not self._canvas_visible(0.8):
                self._apply_stage((w0, h0), (0, 0), 1.0, (0, 0))
                self._wait_canvas(3.0)
                self.measure()
            self._settled = True
            self.log.info("已回退：舞台 "
                          f"{self._stage_size[0]}×{self._stage_size[1]} "
                          f"×{self._stage_scale:.2f}  偏移 {self._stage_shift}")
            return False

        self._settled = True
        self._register_css_script()     # 新文档（刷新/导航）也带上样式
        self.ensure_css()
        self.log.info(
            f"画面已对齐：舞台 {w}×{h} ×{k:.2f}  偏移 {self._stage_shift}"
            f"  画布 {int(self.geo['w'])}×{int(self.geo['h'])}（之后不再自动调整）")
        return True

    def reset_layout(self) -> bool:
        """把画面还原成「窗口内最大 16:9、1:1、不偏移」—— 一定看得见。

        什么时候用：对齐算歪了、或者上一次运行留下的坏位移让游戏窗口整块黑掉。
        只清我们自己加的舞台样式，不动游戏本身、不刷新页面、不重登 —— 点一下
        画面就回来，然后可以再点「重新对齐」按正常流程重排。
        """
        if self.cdp is None or self.cdp.error:
            return False
        with self._settle_lock:          # 等正在跑的那一轮对齐做完，再动手
            self._sync_viewport()
            self._stage_off = (0, 0)
            self._stage_scale = 1.0
            w, h = self._fit_size()
            self._apply_stage((w, h), (0, 0), 1.0, (0, 0))
            self._settled = True      # 还原本身就是「已对齐」，别再自动排一遍
            self._wait_canvas(3.0)
            self.measure()
        self.log.info(f"画面已还原：舞台 {w}×{h} ×1.00、不偏移（黑屏救回来了）")
        return True

    def close(self) -> None:
        if self.cdp is not None and self._frame_sid:
            try:
                self.cdp.call("Target.detachFromTarget",
                              {"sessionId": self._frame_sid}, timeout=3)
            except Exception:
                pass
        self._frame_sid = self._frame_tid = ""
        if self.cdp is not None:
            try:
                self.cdp.close()
            except Exception:
                pass
        self.cdp = None
        self.target_id = ""
        self.mode = ""

    def quit_browser(self, wait: float = 8.0) -> bool:
        """把自己起的那个 Chrome 整只关掉（不碰用户平时那个）。

        为什么需要：导入登录态时要往这份 profile 里写 cookie 库，而 Chrome
        正握着它不放，边写边拷只会拷出半截文件。先走 CDP 的 `Browser.close`
        （干净退出，顺手把内存里的 cookie 落盘），不通再杀我们自己的进程树。
        注意只认 `self._proc` 这个 pid —— 用户手动起的 Chrome 不该被我们杀。
        """
        cdp = self.cdp
        if cdp is not None:
            try:
                cdp.call("Browser.close", timeout=3)
            except Exception:
                pass
        self.close()

        deadline = time.time() + wait
        while time.time() < deadline:
            if not self.br.alive():
                self._reset_after_quit()
                return True
            time.sleep(0.25)

        p = self._proc
        if p is not None and p.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
            except Exception:
                pass
        for _ in range(24):
            if not self.br.alive():
                self._reset_after_quit()
                return True
            time.sleep(0.25)
        self.log.warning("浏览器没能关干净（可能是手动起的那个窗口），请手动关掉再试")
        return False

    def _reset_after_quit(self) -> None:
        """关掉浏览器后清掉「已经做过的事」的标记，下次启动重新来一遍。"""
        self._proc = None
        self._maxed = False
        self._vp_mode = ""
        self._settled = False
        self._stage_size = (0, 0)
        self._stage_shift = (0, 0)
        self._stage_scale = 1.0
        self._stage_off = (0, 0)
        self.status = "init"

    # ------------------------------------------------------------ 窗口 / 视口
    def _maximize(self) -> None:
        """最大化窗口（只在第一次做）。

        为什么不用固定窗口尺寸：`window: 1440x860` 在 1080p 的 125% 缩放下
        几乎顶满屏幕，再大就跑到屏幕外了；最大化能自动吃到这台机器的可用面积。
        """
        cdp = self.cdp
        if cdp is None:
            return
        self._maxed = True
        try:
            r = cdp.call("Browser.getWindowForTarget", {}, timeout=6)
            wid = int((r or {}).get("windowId") or 0)
            if wid:
                try:
                    cdp.call("Browser.setWindowBounds", {
                        "windowId": wid, "bounds": {"windowState": "maximized"},
                    }, timeout=6)
                    self.log.info("窗口已最大化")
                except CDPError:
                    # Chrome 不允许直接最大化「最小化 / 全屏」状态的窗口
                    # （报 "restore it to normal state first"）。先回普通窗口再来一次。
                    cdp.call("Browser.setWindowBounds", {
                        "windowId": wid, "bounds": {"windowState": "normal"},
                    }, timeout=6)
                    cdp.call("Browser.setWindowBounds", {
                        "windowId": wid, "bounds": {"windowState": "maximized"},
                    }, timeout=6)
                    self.log.info("窗口从最小化/全屏恢复并最大化")
        except CDPError as e:
            self.log.warning(f"最大化窗口失败（不影响使用）：{e}")

    def _clear_viewport(self) -> None:
        """把视口还原成浏览器真实窗口大小（每个会话做一次就够）。

        以前这里会把视口覆盖成 2560×1440，为了「画面和模板 1:1，不用插值」。
        代价是虚拟视口比窗口大 —— 人只能看到左上角，缩放到看全又不居中。
        现在舞台由 CSS 居中框住，视口就该等于窗口：
        2x 屏按 dpr 原生出图（2854×1650），裁到舞台后**降**采样到 2560×1440，
        比 1427 宽的画面往 2560 上采样清晰得多 —— 既好看又更好认。
        """
        if self._vp_mode == "window":
            return
        cdp = self.cdp
        if cdp is None:
            return
        try:
            cdp.call("Emulation.clearDeviceMetricsOverride", timeout=10)
            self._vp_mode = "window"
            self.log.info("视口 = 窗口实际大小（游戏在居中的 16:9 舞台里）")
        except CDPError as e:
            self.log.warning(f"还原视口失败：{e}")

    # ------------------------------------------------------------ 窗口状态
    def window_state(self) -> str:
        """当前窗口状态：normal / minimized / maximized / fullscreen / 空串。"""
        cdp = self.cdp
        if cdp is None:
            return ""
        try:
            r = cdp.call("Browser.getWindowForTarget", {}, timeout=5) or {}
            return str((r.get("bounds") or {}).get("windowState") or "normal")
        except CDPError:
            return ""

    def ensure_visible(self) -> None:
        """跑任务时窗口被最小化了就原地恢复（不挪位置）。

        最小化会让 Chrome 停止合成，画面流直接归零（实测 0.6fps），任务就卡死；
        典型触发是 Win+D（显示桌面）或手动点最小化。
        以前的做法是「把窗口挪到屏幕外」，人看着窗口凭空消失，体验很差 ——
        现在只把 windowState 设回 normal，窗口回到原来的位置和大小。
        """
        if self.cdp is None or self.cdp.error:
            return
        if self.window_state() != "minimized":
            return
        try:
            r = self.cdp.call("Browser.getWindowForTarget", {}, timeout=5) or {}
            wid = int(r.get("windowId") or 0)
            if not wid:
                return
            self.cdp.call("Browser.setWindowBounds",
                          {"windowId": wid, "bounds": {"windowState": "normal"}}, timeout=5)
            self.log.warning("窗口被最小化了（画面会停），已恢复显示")
        except CDPError as e:
            self.log.warning(f"恢复窗口失败：{e}")

    # ------------------------------------------------------------ 准备画面
    def prepare(self, force: bool = False) -> bool:
        """注入样式 + 设虚拟视口 + 量游戏区域。可反复调用，幂等。

        返回 True 表示画面可用（游戏画布已经在渲染）。
        """
        if not force and time.time() - self._last_prepare < 1.0:
            return self.status == "ready"
        self._last_prepare = time.time()

        if not self.ensure_browser():
            return False
        if not self.attach():
            return False

        cdp = self.cdp
        assert cdp is not None

        # 1) 视口：等于窗口本身。游戏位置交给 PAGE_CSS 的居中 16:9 舞台，
        #    这样「人看到的」和「识别拿到的」是同一块画面（以前覆盖成 2560×1440
        #    会把窗口撑爆，人只能看到左上角）。
        #    最小化时 Chrome 会停掉合成，画面流归零 —— 顺手先恢复显示。
        self.ensure_visible()
        if not self._maxed and self.maximize:
            self._maximize()
        self._clear_viewport()     # 视口 = 窗口，不下任何覆盖
        self._sync_viewport()      # 舞台尺寸按真实窗口算，别用配置里的基准

        # 1.5) 登录态：可选。本程序用独立 Chrome profile，第一次要么在窗口里登一次
        #      （之后记住），要么把浏览器里的 erolabsjwt 填进 config，这里直接种 cookie。
        #      必须放在注入样式之前 —— 它要 reload 一次页面。
        if self.jwt and not self._jwt_done:
            self._jwt_done = True
            self.inject_jwt(self.jwt)

        # 2) 清掉站点外壳 + 摆好居中的 16:9 舞台。
        #    注意这一步会**反复执行**（下面还有自愈脚本兜底）：页面导航后
        #    当前文档里的 <style> 会消失，所以每次 prepare 都补一次。
        self.ensure_css()

        # 3) iframe 内部也去边距（同域才做得到；跨域时靠父页面把 iframe 铺满）
        try:
            cdp.evaluate(
                "(() => { try {"
                " const d = document.querySelector('iframe').contentDocument;"
                " if (!d) return 0;"
                " let s = d.getElementById('__kyweb_css');"
                " if (!s) { s = d.createElement('style'); s.id = '__kyweb_css'; d.head.appendChild(s); }"
                f" s.textContent = {json.dumps(INNER_CSS)}; return 1;"
                " } catch (e) { return 0; } })()"
            )
        except CDPError:
            pass

        # 3.5) 帧里那一层撑到视口：站点模板把它设成 inner+40 × inner+80，
        #      画布右下角会被裁（底部按钮显示不全）。这里先写一遍，页面刷新后
        #      也能立刻生效；对齐时还会再补一次（幂等）。
        self._ensure_frame_css()

        # 3.6) 助手浮层：把控制面板注入游戏页面，这样只有一个窗口。
        #      position:fixed，跟舞台无关；排在 iframe 处理之后，免得被站点脚本清掉。
        #      见 core/overlay.py（为什么只能往这个方向合并，那里写清楚了）。
        try:
            self._ensure_overlay()
        except Exception as e:      # 浮层是锦上添花，绝不能因此挡住主流程
            self.log.warning(f"注入助手浮层失败：{type(e).__name__}: {e}")

        # 4) 量游戏画布的位置和大小。
        #    云游戏从「页面打开」到「画布出现」要好几秒（要拉 connect 信息、
        #    再嵌 iframe、再加载 WebGL），这里给一个等待窗口，别一秒就判死。
        info = None
        deadline = time.time() + float(self.ready_timeout)
        while time.time() < deadline:
            info = self.measure()
            if info is None:
                time.sleep(0.5)
                continue
            if info.get("need_login"):
                break
            # 跨域 iframe：等「帧里真正的画布」量出来再判 ready。
            # Unity 起来之前画布只有 300×150，这时只能拿 iframe 盒子凑数
            # （画面会带黑边、还会贴角），多等一会儿值得。
            if info.get("geo_src") == "frame" or not info.get("iframe"):
                break
            time.sleep(0.5)
        if info is None:
            self.status = "loading"
            self.hint = "游戏画面还没出来（多等一会儿，或者点「重连」）"
            return False
        if info.get("need_login"):
            self.status = "need_login"
            code = self.login_err
            if code and code != "unknown":
                self.hint = (f"游戏内登录失败（错误代码 {code}）· 在浏览器窗口里点「确认」后重新登录，"
                             "或点「重载游戏」清缓存重进")
            elif code:
                self.hint = "游戏内登录失败 · 点「重载游戏」清缓存后重新登录"
            else:
                self.hint = "请在弹出的浏览器窗口里登录一次（之后会记住）"
            return False

        # 4.6) 这里**不动舞台**。以前在这里反复「收舞台 + 居中」，结果过场动画
        #      期间画面被推来推去（用户原话：加载时一直在调整位置）。现在只把
        #      状态标成 ready，等主页出现之后由 settle_layout() 一次性做完
        #      「铺满 → 居中」，做完就锁定。

        # 4.8) 画面出来了，再补一次浮层。
        #      3.6 那一次注入往往太早（页面还在 bootstrap，UID 还没建好），
        #      站点随后换 DOM 会把节点清掉 —— 日志里「浮层闪一下就没了」就是它。
        #      这时页面已经加载完，补上去的才站得住；还活着就什么都不做。
        try:
            self._ensure_overlay()
        except Exception as e:
            self.log.warning(f"补注助手浮层失败：{type(e).__name__}: {e}")

        self.status = "ready"
        self.hint = ""
        return True

    def stage_css(self) -> str:
        """舞台样式：尺寸 / 位移 / 缩放（写进独立的 <style id="__kyweb_stage">）。"""
        w, h = self._stage_size
        if w <= 0 or h <= 0:
            w, h = self._fit_size()
        # 位移**每次写出前都过一遍闸门**：不管它当初是怎么存进来的（旧版本遗留、
        # 测量抖动算出来的），都不能让画面跑到窗口外面去。
        sx, sy = self._guard_shift(self._stage_shift)
        return _stage_css(w, h, sx, sy, self._stage_scale, *self._stage_off)

    def _register_css_script(self) -> None:
        """注册/更新「每个新文档都注入」的脚本。舞台尺寸变了要重注册。"""
        cdp = self.cdp
        if cdp is None:
            return
        if self._css_script_id:
            try:
                cdp.call("Page.removeScriptToEvaluateOnNewDocument",
                         {"identifier": self._css_script_id}, timeout=5)
            except CDPError:
                pass
            self._css_script_id = ""
        try:
            r = cdp.call("Page.addScriptToEvaluateOnNewDocument",
                         {"source": _css_on_new_doc_js(PAGE_CSS)}, timeout=8)
            self._css_script_id = str((r or {}).get("identifier") or "")
        except CDPError as e:
            self.log.warning(f"注册样式自愈脚本失败（导航后样式可能丢）：{e}")

    def ensure_css(self) -> bool:
        """确保「舞台样式」在**当前文档**上生效（幂等，可随便调）。"""
        cdp = self.cdp
        if cdp is None:
            return False
        try:
            return bool(cdp.evaluate(_css_now_js(PAGE_CSS, self.stage_css())))
        except CDPError:
            return False

    # 浮层探活：节点还在不在文档里，顺便把现场信息带出来（没装上时靠它定位）
    _OVERLAY_PROBE = (
        "(() => { try {"
        " const h = document.getElementById('kyov');"
        " return JSON.stringify({"
        "  ok: !!(h && h.shadowRoot && h.isConnected),"
        "  href: String(location.href).slice(0, 80),"
        "  ready: document.readyState,"
        "  root: !!(document.documentElement || document.body),"
        "  flag: !!window.__kyov_ok,"
        "  apiBase: window.__kyov_api || '',"
        "  api: window.__kyov_api_ok === undefined ? null : window.__kyov_api_ok"
        " });"
        " } catch (e) { return JSON.stringify({ ok: false, err: String(e).slice(0, 100) }); } })()"
    )

    def _overlay_probe(self) -> dict:
        cdp = self.cdp
        if cdp is None or cdp.error:
            return {}
        try:
            raw = cdp.evaluate(self._OVERLAY_PROBE, timeout=4)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    @staticmethod
    def _overlay_fresh(probe: dict, api: str) -> bool:
        """页面上那一份浮层，是不是「这一轮进程、这个端口」注入的。

        为什么要认端口：8765 被占时程序会换一个端口，页面里那份旧浮层却还一直去
        敲老端口 —— 界面上永远「连不上助手」，看起来像浮层坏了。这种情况必须重注。
        老版本注入的浮层没有 apiBase，只要它还在正常通信就认它，别白重建一次。
        """
        if not probe.get("ok"):
            return False
        base = probe.get("apiBase") or ""
        if base == api:
            return True
        return base == "" and probe.get("api") is not False

    def _ensure_overlay(self, force: bool = False) -> bool:
        """注入浮层的入口：加锁，防止 prepare / 守护线程 / 兜底线程同时补注。

        不加锁的话两边会同时看到「还没注册脚本」，各自往 CDP 上挂一份
        「新文档自动注入」—— 页面下次导航就会冒出两个浮层。
        """
        with self._overlay_lock:
            return self._ensure_overlay_locked(force)

    def _ensure_overlay_locked(self, force: bool = False) -> bool:
        """把助手浮层注入游戏页面（幂等，可随便调）。见 core/overlay.py。

        这里是「注入 + 验尸」两步，不是注入完就完事：
        云游戏页在启动过程中会换一次 DOM，我们挂上去的节点会被一起清掉
        （用户看到的就是「浮层闪一下没了」）。所以每次先探活，不在了才重建；
        重建之前还会顺手把残骸删掉，避免同一个页面堆出两个浮层。
        """
        from .overlay import overlay_source

        cfg = self.cfg.get("ui") or {}
        if not cfg.get("overlay", False):
            return False
        cdp = self.cdp
        if cdp is None or cdp.error:
            return False
        try:
            port = int(cfg.get("port") or 8765)
        except Exception:
            port = 8765
        src = overlay_source(f"http://127.0.0.1:{port}")
        if not src:
            return False

        # 换了 CDP 连接（重连/换页面）、或者面板端口变了（8765 被占会换一个），
        # 之前注册的「新文档注入」就失效了，要重来
        if self._overlay_cdp is not cdp or self._overlay_port != port:
            self._overlay_cdp = cdp
            self._overlay_port = port
            self._overlay_sid = ""
            self._overlay_logged = False
        if not self._overlay_sid:
            try:
                r = cdp.call("Page.addScriptToEvaluateOnNewDocument",
                             {"source": src}, timeout=8)
                self._overlay_sid = str((r or {}).get("identifier") or "")
            except CDPError as e:
                self.log.warning(f"注册浮层脚本失败（页面刷新后浮层会丢，但不致命）：{e}")

        # 还活着就别重复执行：重建会把用户勾的任务和已经滚出来的日志清掉
        if not force:
            probe = self._overlay_probe()
            api = f"http://127.0.0.1:{port}"
            if probe.get("ok") and not self._overlay_fresh(probe, api):
                # 页面上那一份是**别的进程/旧端口**注入的：它会一直去敲一个没人听的
                # 端口，界面上永远是「连不上助手」。这种不能迁就，清掉牌子重建一份。
                self.log.info(f"浮层指向的不是这个端口（{probe.get('apiBase') or '未知'}），重注一份")
                try:
                    cdp.evaluate("window.__kyov = null;", timeout=4)
                except CDPError:
                    pass
                probe = {}
            if probe.get("ok"):
                self._overlay_ok = True
                self._overlay_misses = 0
                if not self._overlay_logged:
                    self._overlay_logged = True
                    self.log.info("助手浮层已并入游戏窗口：右上角那块就是控制面板")
                return True

        try:
            cdp.evaluate(src, timeout=8)
        except CDPError as e:
            self._overlay_misses += 1
            if self._overlay_misses <= 2:
                self.log.warning(f"注入助手浮层失败：{e}")
            return False

        probe = self._overlay_probe()
        if probe.get("ok"):
            self._overlay_ok = True
            self._overlay_misses = 0
            if not self._overlay_logged:
                self._overlay_logged = True
                self.log.info("助手浮层已并入游戏窗口：右上角那块就是控制面板")
            return True

        self._overlay_ok = False
        self._overlay_misses += 1
        # 只报前两次，别把日志刷爆（守护线程每轮都会来补一次）
        if self._overlay_misses <= 2:
            detail = "，".join(f"{k}={v}" for k, v in probe.items() if k != "ok") or "拿不到页面信息"
            self.log.warning(f"助手浮层没装上（{detail}），稍后自动重试")
        return False

    def ensure_overlay_alive(self) -> bool:
        """空闲时的轻量巡检：浮层还在不在，不在了立刻补一张。

        守护线程每 1.5 秒转一圈，这里自己节流到 3 秒一次 —— 一次 JS 往返虽然
        便宜，但也没必要每秒都问。返回 True 表示「现在浮层是好的」。
        """
        cfg = self.cfg.get("ui") or {}
        if not cfg.get("overlay", False):
            return False
        cdp = self.cdp
        if cdp is None or cdp.error:
            return False
        now = time.time()
        if now - self._overlay_check_at < 3.0:
            return bool(self._overlay_ok)
        self._overlay_check_at = now
        probe = self._overlay_probe()
        api = f"http://127.0.0.1:{int(cfg.get('port') or 8765)}"
        if self._overlay_fresh(probe, api):
            self._overlay_ok = True
            return True
        self._overlay_ok = False
        return self._ensure_overlay()

    def refresh_geometry(self, force: bool = False) -> bool:
        """只做一件事：窗口尺寸**真的**变了才重新对齐一次；没变只重量坐标。

        用户的原话是「等到加载完居中一次就行，不要老是动」。所以这里
        **不做**任何周期性重排 —— 每 2 秒只有一次 JS 往返读窗口尺寸，
        尺寸没变就直接返回。窗口被拉大拉小（或最大化/还原）时才解锁，
        由 settle_layout() 重新做一遍「铺满 → 居中」。

        为什么要重做：改窗口大小会让画布元素变了尺寸但游戏不重绘，
        底部会留一条黑带（实测截图里就出现过）。

        `force=True` 只是「别等节流、马上量」，**不是**「马上重排」：
        以前它绕过尺寸判断又 settle 一遍，跑完任务时正好和守护线程撞车，
        把游戏推到了窗口外（黑屏）。
        """
        cdp = self.cdp
        if cdp is None or cdp.error:
            return False
        now = time.time()
        if not force and now - self._last_geo < 2.0:
            return False
        self._last_geo = now
        prev = (self.vw, self.vh)
        if not self._sync_viewport():
            return False
        if abs(self.vw - prev[0]) < 2 and abs(self.vh - prev[1]) < 2:
            # 窗口没变：**绝不重排**，只重量一次坐标（纯只读）。
            # 以前 force=True 会绕过这个判断、再 settle 一遍 —— 而 settle 中途
            # 的测量只要抖一下，舞台就被推到屏幕外，用户看到整块黑屏
            # （日志：跑完任务后「窗口尺寸变了（1920×1009 → 1920×1009）」）。
            if now - self._last_measure < (0.6 if force else 5.0):
                return False
            self._last_measure = now
            old = (self.geo["x"], self.geo["y"], self.geo["w"], self.geo["h"])
            if self.measure() is None:
                return False
            # 先确认画面还在窗口里：上次留下的坏位移会让人看到黑屏，发现就自愈。
            if not self._canvas_visible(0.35, probe=False):
                self.log.warning("画面跑到窗口外了（黑屏），正在还原…")
                self.reset_layout()
                return True
            drift = max(abs(self.geo["x"] - old[0]), abs(self.geo["y"] - old[1]),
                        abs(self.geo["w"] - old[2]), abs(self.geo["h"] - old[3]))
            if drift > 24:
                self.log.info(f"画布位置变了（{drift:.0f}px），识别/点击坐标已跟着更新")
                return True
            return False
        self.log.info(f"窗口尺寸变了（{prev[0]}×{prev[1]} → {self.vw}×{self.vh}），重新对齐一次")
        self._settled = False
        self.ensure_css()
        if self.measure() is None:
            return False
        self.settle_layout()
        return True

    def measure(self) -> dict | None:
        """量游戏画面在视口里的位置：返回 {canvas:[x,y,w,h], vw, vh, cross, need_login}。

        坐标是**父页面坐标系**（screencast 就是按这个坐标系出帧的）。
        同域 iframe 能读到里面 canvas 的盒子，跨域读不到，就用 iframe 自己的盒子
        —— 我们的样式已经把 iframe 铺满视口，两者通常是一回事。
        """
        cdp = self.cdp
        if cdp is None:
            return None
        js = """(() => {
          const out = {vw: innerWidth, vh: innerHeight, url: location.href};
          // 登录失败的弹窗有时挂在父页面上、有时在 iframe 里，两处都扫。
          // 游戏前端普遍用 shadow DOM 装弹窗，innerText 看不见，所以递归走 shadowRoot。
          const grab = (root) => {
            let s = '';
            const walk = (n) => {
              if (s.length > 300 || !n) return;
              if (n.nodeType === 3) {
                const t = (n.nodeValue || '').trim();
                if (t) s += t + ' ';
                return;
              }
              if (n.nodeType !== 1 && n.nodeType !== 9 && n.nodeType !== 11) return;
              const tag = (n.tagName || '').toUpperCase();
              if (tag === 'SCRIPT' || tag === 'STYLE') return;
              if (n.shadowRoot) walk(n.shadowRoot);
              const kids = n.childNodes || [];
              for (let i = 0; i < kids.length && s.length <= 300; i++) walk(kids[i]);
            };
            walk(root);
            return s;
          };
          if (document.body) out.ptxt = grab(document.body).slice(0, 300);
          // 舞台（外层容器）自己的盒子：用来算「画布在舞台里偏了多少」
          const st = document.querySelector('#cloudGameContainer');
          if (st) { const b = st.getBoundingClientRect();
                    out.stage = [b.x, b.y, b.width, b.height]; }
          const f = document.querySelector('iframe');
          if (f) {
            const b = f.getBoundingClientRect();
            out.iframe = [b.x, b.y, b.width, b.height, f.src || ''];
            try {
              const d = f.contentDocument;
              if (d) {
                const c = d.querySelector('canvas');
                if (c) {
                  const r = c.getBoundingClientRect();
                  out.canvas = [b.x + r.x, b.y + r.y, r.width, r.height];
                  out.cross = false;
                }
                // 没登录时 iframe 里是账号页，登录失败的提示也在这里
                if (d.body) out.txt = grab(d.body).slice(0, 400);
              }
            } catch (e) { /* 跨域，读不到，用 iframe 盒子 */ }
          } else {
            const c = document.querySelector('canvas');
            if (c) {
              const r = c.getBoundingClientRect();
              out.canvas = [r.x, r.y, r.width, r.height];
              out.cross = false;
            }
          }
          return JSON.stringify(out);
        })()"""
        try:
            raw = cdp.evaluate(js)
            info = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            return None

        url = str(info.get("url") or "").lower()
        # 视口现在等于窗口本身：把真实 CSS 尺寸记下来。
        # Capture._fit 靠它把「帧像素 → CSS 像素」换算出来（2x 屏 kx≈2），
        # 算错就会裁错位置 —— 这是坐标能对准的关键一环。
        try:
            _vw, _vh = float(info.get("vw") or 0), float(info.get("vh") or 0)
            if _vw > 0 and _vh > 0:
                self.vw, self.vh = int(round(_vw)), int(round(_vh))
        except Exception:
            pass
        ifr = info.get("iframe")
        ifr_src = str(ifr[4] if ifr and len(ifr) > 4 else "").lower()
        self.login_err = self._parse_login_err(
            str(info.get("txt") or "") + " " + str(info.get("ptxt") or ""))
        # 没登录时站点会把 iframe 换成 signup.html —— 这时候提示用户去登录一次
        if "signup" in url or "signup" in ifr_src or "/login" in ifr_src:
            return {"need_login": True, "vw": info.get("vw"), "vh": info.get("vh"),
                    "login_err": self.login_err}

        cv = info.get("canvas")
        if (not cv) and ifr:
            info["cross"] = True
            # 跨域：父页面只能看到 iframe 的盒子，但盒子比真实画布大（游戏把画布
            # 封顶在 ~1281×720 且贴左上角）。钻到游戏帧里量画布，坐标才对得准。
            box = self._canvas_box()
            cv_local = (box or {}).get("rect")
            if cv_local:
                self._cv_local = [float(v) for v in cv_local]
            geo = self._geo_from_frame(ifr, box)
            if geo is not None and geo["w"] >= 64 and geo["h"] >= 64:
                info["geo_src"] = "frame"
                self.geo = geo
                self.info = info
                out = dict(info)
                out["geo"] = self.geo
                return out
            cv = [ifr[0], ifr[1], ifr[2], ifr[3]]
        if not cv:
            return None

        w, h = float(cv[2]), float(cv[3])
        if w < 64 or h < 64:
            return None

        self.geo = {"x": float(cv[0]), "y": float(cv[1]), "w": w, "h": h}
        self.info = info
        out = dict(info)
        out["geo"] = self.geo
        return out

    @staticmethod
    def _parse_login_err(txt: str) -> str:
        """从登录框文字里抠出错误码。

        游戏内登录失败会弹「登入錯誤 / 登入失敗，詳細錯誤代碼: 1002」。
        这种码是游戏服务器给的，跟站点登录墙不是一回事 —— 分开报，用户才知道
        该去点「重载游戏」还是去填账号密码。
        """
        if not txt:
            return ""
        m = re.search(r"(?:錯誤|错误|error)\s*(?:代\s*[碼码]|code)\s*[:：]?\s*(\d{2,5})", txt, re.I)
        if m:
            return m.group(1)
        if "登入失敗" in txt or "登录失败" in txt or "登入錯誤" in txt or "登录错误" in txt:
            return "unknown"
        return ""

    def reload_game(self, clear_cache: bool = False) -> bool:
        """只重载游戏 iframe，不动站点会话（不用重新登录站点）。

        官方对「游戏内登录异常」的建议是清缓存后重进；`clear_cache=True` 会顺手
        清掉这份 profile 的 HTTP 缓存。iframe 内重载失败（跨域）就退回整页刷新。
        """
        cdp = self.cdp
        if cdp is None:
            return False
        if clear_cache:
            try:
                cdp.call("Network.enable", timeout=5)
                cdp.call("Network.clearBrowserCache", timeout=8)
            except CDPError as e:
                self.log.warning(f"清缓存失败（继续重载）：{e}")
        js = ("(() => { const f = document.querySelector('iframe');"
              " if (!f) { location.reload(); return 'page'; }"
              " try { f.contentWindow.location.reload(); return 'frame'; }"
              " catch (e) { location.reload(); return 'page'; } })()")
        try:
            where = cdp.evaluate(js)
        except CDPError as e:
            self.log.warning(f"重载游戏失败：{e}")
            return False
        self.log.info(f"重载游戏画面（{'整页' if where == 'page' else '游戏框'}"
                      f"{'，已清缓存' if clear_cache else ''}）")
        self.login_err = ""
        # 重载后不知道 Chrome 还保不保留覆盖，让 prepare 重新下一遍
        self._vp_mode = ""
        self._settled = False        # 画布会重建：重新对齐一次
        self._zoom_done = False
        self._stage_size = (0, 0)
        self._stage_shift = (0, 0)
        self._stage_scale = 1.0
        self._stage_off = (0, 0)
        return True

    def inject_jwt(self, value: str, reload: bool = True) -> bool:
        """把 erolabsjwt 种进这份 profile，直接跳过账号登录页（也就跳过了人机验证）。

        登录页在自动化环境下经常过不了 Cloudflare Turnstile，这一步是稳妥的绕行：
        在平时用的浏览器里登录一次，把 cookie 值抄过来即可。
        """
        cdp = self.cdp
        value = (value or "").strip()
        if cdp is None or not value:
            return False
        from urllib.parse import urlparse
        try:
            host = urlparse(self.url).hostname or "www.ero-labs.com"
        except Exception:
            host = "www.ero-labs.com"
        try:
            cdp.call("Network.enable", timeout=5)
            for dom in (host, "." + host.split(".", 1)[-1]):
                cdp.call("Network.setCookie", {
                    "name": "erolabsjwt", "value": value, "domain": dom,
                    "path": "/", "secure": True, "httpOnly": False,
                    "sameSite": "Lax",
                }, timeout=5)
        except CDPError as e:
            self.log.warning(f"写入登录令牌失败：{e}")
            return False
        self.log.info("已写入登录令牌（erolabsjwt）")
        if reload:
            try:
                cdp.call("Page.reload", {"ignoreCache": False}, timeout=10)
            except CDPError:
                pass
            time.sleep(3.0)
            self._vp_mode = ""
            self._settled = False    # 刷新后画布会重建
            self._zoom_done = False
            self._stage_size = (0, 0)
            self._stage_shift = (0, 0)
            self._stage_scale = 1.0
        self.login_err = ""
        return True

    # ------------------------------------------------------------ 坐标
    def to_css(self, x_ref: float, y_ref: float) -> tuple[float, float]:
        """REF（模板基准 2560×1440）→ 浏览器 CSS 坐标。"""
        g = self.geo
        sx = g["w"] / float(2560)
        sy = g["h"] / float(1440)
        return g["x"] + x_ref * sx, g["y"] + y_ref * sy

    def to_ref_scale(self) -> tuple[float, float]:
        """画面像素 → REF 的缩放比（裁剪后画面一般正好等于 REF，即 1.0）。"""
        g = self.geo
        return 2560.0 / g["w"], 1440.0 / g["h"]
