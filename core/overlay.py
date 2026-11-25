"""助手浮层：把控制面板注入到游戏页面里，做到「一个窗口就够」。

## 为什么只能往这个方向合并

游戏必须由一个**可见的 Chrome 窗口**真实渲染 —— 机器人识别画面靠的是
`Page.startScreencast`，窗口一旦被最小化、被完全遮住或挪到后台标签，Unity 就
停止出帧（实测 `visibilityState: hidden` 时点击和渲染全停），机器人立刻变瞎。
所以「游戏窗口」删不掉；反过来把游戏 iframe 塞进助手页面更要命：那是第二次
Unity 渲染，等于又开一个游戏实例，CPU/显存只会更高。

能合并的只有另一半：把控制面板搬进游戏窗口。于是有了这个浮层 ——
它由 `Page.addScriptToEvaluateOnNewDocument` 直接执行（不受游戏站 CSP 的
脚本限制），UI 放在 Shadow DOM 里（不受游戏站 CSS 影响），数据靠 fetch 打
`http://127.0.0.1:8765`（服务端已加 CORS 头；https 页面访问 localhost 不会被
当混合内容拦掉，实测 `no-cors` 能触达）。

## 两条不能破的规矩

1. 机器人按**绝对坐标**点击，浮层只要接收点击就会把那一下吃掉。所以跑任务时
   浮层整个 `pointer-events:none`，并缩成右上角一条小字；要停止用
   **Ctrl+Shift+X**，要看日志用 **Ctrl+Shift+H**。
2. 浮层是「锦上添花」：注入失败、或者页面还没加载出来，就必须回落到独立的
   面板窗口（见 gui_server 的 `_open_panel_if_overlay_failed`），
   绝不能让用户对着一个没有界面的程序发呆。
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_PORT = 8765


def overlay_path() -> Path:
    return Path(__file__).resolve().parent.parent / "web" / "overlay.js"


def overlay_source(api: str) -> str:
    """把 overlay.js 包成一段可直接 evaluate 的源码（带上接口地址）。

    包一层 try：浮层自己出错绝不能连累游戏页面。
    """
    try:
        js = overlay_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    if not js.strip():
        return ""
    # 包成 IIFE：Runtime.evaluate 要的是「表达式」，而且浮层自己出错绝不能连累游戏页面
    return ("(function(){try{\n" + js.replace("__KY_API__", api) +
            "\n}catch(e){console.warn('[kyov] 浮层注入失败', e)}})()")
