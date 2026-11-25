"""Windows 系统级输入：只干一件事 —— 给游戏窗口发真实的浏览器快捷键。

为什么非要走到这一层
--------------------
Ctrl + 滚轮 / Ctrl + = / Ctrl + 0 都是**浏览器进程**的快捷键。CDP 的
`Input.dispatchMouseEvent` / `dispatchKeyEvent` 只到渲染进程，实测发完之后
`innerWidth` / `devicePixelRatio` 一点没变。要让浏览器真的动缩放，只能往
系统输入队列塞键盘事件（SendInput），也就是「假装有人在敲键盘」。

代价（所以默认关闭、只在用户点了按钮或明确配置时才做）
------------------------------------------------------
- 必须把游戏窗口抢到前台（`SetForegroundWindow`），那一两秒你不能操作别的窗口；
- 会占用真实键盘焦点，所以调用方要先确认用户没有在输入框里打字。

本模块**不碰鼠标**：模拟标题栏右键 → 「缩放 → 放大」那条路实测不可靠
（Chrome 是自绘边框，ClientToScreen 算不出非客户区高度，可点区域只有顶上
几个像素；桌面上有别的窗口盖上来时就会点空）。
"""

import ctypes
import sys
import time

IS_WIN = sys.platform == "win32"

# 虚拟键码
VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_0 = 0x30
VK_OEM_PLUS = 0xBB        # 主键盘的 "=" / "+"

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_SW_RESTORE = 9

if IS_WIN:                                            # pragma: no cover
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD),
                    ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT),
                                  ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.IsIconic.argtypes = (wintypes.HWND,)
    _user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    _user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    _user32.EnumWindows.argtypes = (_WNDENUMPROC, wintypes.LPARAM)
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    _user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD,
                                          wintypes.BOOL)
    _user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    _user32.SetActiveWindow.argtypes = (wintypes.HWND,)
    _user32.IsWindow.argtypes = (wintypes.HWND,)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD

# 可能出现「游戏窗口」的浏览器进程名（Chrome 系 + 常见国产壳）
BROWSER_EXES = ("chrome.exe", "msedge.exe", "chromium.exe", "brave.exe",
                "vivaldi.exe", "opera.exe", "360se.exe", "360chrome.exe",
                "qqbrowser.exe", "sogouexplorer.exe")


def available() -> bool:
    """当前平台能不能做系统级输入。"""
    return bool(IS_WIN)


def _key(vk: int, up: bool = False) -> int:           # pragma: no cover
    """塞一个键盘事件进系统队列；返回被接受的事件数（0 = 被拒，通常是权限）。"""
    ev = _INPUT(type=_INPUT_KEYBOARD)
    ev.ki = _KEYBDINPUT(wVk=vk, wScan=0,
                        dwFlags=_KEYEVENTF_KEYUP if up else 0,
                        time=0, dwExtraInfo=0)
    return int(_user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(_INPUT)) or 0)


def press(vk: int, ctrl: bool = False) -> int:        # pragma: no cover
    """按一下一个键（可选按住 Ctrl）。返回被系统接受的事件数。"""
    if not IS_WIN:
        return 0
    n = 0
    if ctrl:
        n += _key(VK_CONTROL)
        time.sleep(0.05)
    n += _key(vk)
    time.sleep(0.05)
    n += _key(vk, up=True)
    if ctrl:
        time.sleep(0.03)
        n += _key(VK_CONTROL, up=True)
    return n


def set_foreground(hwnd: int) -> bool:                # pragma: no cover
    """把窗口摆到前台，成功返回 True。

    直接 `SetForegroundWindow` 经常被系统拒（防抢焦点），所以要先把本线程
    和「当前前台窗口的线程」绑一下（`AttachThreadInput`），再设就成功了；
    还不行就再补 `BringWindowToTop` + `SetActiveWindow`，最多试三次。
    """
    if not IS_WIN or not hwnd:
        return False
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.25)
    for _ in range(3):
        if _user32.GetForegroundWindow() == hwnd:
            return True
        fg = _user32.GetForegroundWindow()
        tid_fg = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_me = _kernel32.GetCurrentThreadId()
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(_user32.AttachThreadInput(tid_me, tid_fg, True))
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(tid_me, tid_fg, False)
        time.sleep(0.2)
        if _user32.GetForegroundWindow() == hwnd:
            return True
    return _user32.GetForegroundWindow() == hwnd


def exe_of(pid: int) -> str:                        # pragma: no cover
    """进程的可执行文件名（小写）；拿不到就空串。

    用 PROCESS_QUERY_LIMITED_INFORMATION：同用户权限就能查，不用提权。
    """
    if not IS_WIN or not pid:
        return ""
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        n = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return ""
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        _kernel32.CloseHandle(h)


def windows(only_browsers: bool = False) -> list[dict]:   # pragma: no cover
    """列出现在**可见、有标题**的顶层窗口。返回 [{hwnd,pid,title,rect,area,exe}]。

    只读，不发任何输入。
    """
    if not IS_WIN:
        return []
    out: list[dict] = []

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = int(_user32.GetWindowTextLengthW(hwnd))
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        r = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w = max(0, r.right - r.left)
        h = max(0, r.bottom - r.top)
        wpid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        out.append({"hwnd": int(hwnd), "pid": int(wpid.value), "title": buf.value,
                    "rect": (r.left, r.top, w, h), "area": w * h,
                    "exe": exe_of(int(wpid.value))})
        return True

    proc = _WNDENUMPROC(cb)
    _user32.EnumWindows(proc, 0)
    if only_browsers:
        out = [x for x in out if x["exe"] in BROWSER_EXES]
    return out


def is_window(hwnd: int) -> bool:                   # pragma: no cover
    """这个句柄还是不是一个活着的窗口（缓存句柄过期时用）。"""
    return bool(IS_WIN and hwnd and _user32.IsWindow(hwnd))


def biggest_window(pid: int) -> int:                 # pragma: no cover
    """在这个进程的所有顶层窗口里挑面积最大、有标题的那个（就是游戏窗口）。"""
    if not IS_WIN or not pid:
        return 0
    best = [0, 0]
    for x in windows():
        if x["pid"] != int(pid):
            continue
        if x["area"] > best[1]:
            best[0], best[1] = x["hwnd"], x["area"]
    return best[0]


def browser_window(title_hint: str = "", pid: int = 0) -> int:   # pragma: no cover
    """在所有浏览器窗口里挑「游戏那个」。

    为什么需要这条兜底：浏览器主进程的 pid 拿不到（比如 CDP 的 SystemInfo 域
    在页面会话上调不通、或者这台机器上浏览器是用户自己开的）时，按进程名
    + 窗口标题也能认出来 —— `--app` 模式的窗口标题**就是网页标题**。

    打分：标题完全等于 hint 的最优先，其次标题包含 hint；同档取面积大的。
    """
    if not IS_WIN:
        return 0
    hint = (title_hint or "").strip().lower()
    best, best_score = 0, -1
    for x in windows(only_browsers=True):
        if pid and x["pid"] != int(pid):
            continue
        t = (x["title"] or "").lower()
        if hint and t == hint:
            score = 2 * 10 ** 9 + x["area"]
        elif hint and hint[:24] and hint[:24] in t:
            score = 10 ** 9 + x["area"]
        elif not hint:
            score = x["area"]
        else:
            score = x["area"]          # 标题对不上也留着，面积够大就当它（只有一个浏览器窗口时）
        if score > best_score:
            best, best_score = x["hwnd"], score
    return best


def describe(limit: int = 6) -> str:                # pragma: no cover
    """把当前浏览器窗口列成一行，出错时写进日志用。"""
    xs = windows(only_browsers=True)
    xs.sort(key=lambda x: -x["area"])
    if not xs:
        return "（没有可见的浏览器窗口）"
    parts = [f"{x['exe']}#{x['pid']} {(x['title'] or '')[:28]!r} {x['rect'][2]}×{x['rect'][3]}"
             for x in xs[:limit]]
    return "；".join(parts)
