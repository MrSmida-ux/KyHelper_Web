"""装配：配置 → Matcher / GameSession / Capture / Device。

跟模拟器版一样是一层薄胶水，区别只是把 ADB 换成了浏览器会话。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import paths
from .browser import GameSession
from .capture import Capture
from .device import Device
from .vision import Matcher

DEFAULT = {
    "browser": {
        "port": 9222, "chrome": "", "user_data": "chrome-data",
        "window": "1440x860", "viewport": "2560x1440", "auto_launch": True,
        "maximize": True,
        "settle_timeout": 60, "fill": True, "zoom_auto": True,
        "jwt": "",
        "url": "https://www.ero-labs.com/cn/cloud_game.html?id=55&connect_type=1&connection_id=38",
    },
    "template": {"dir": "assets/templates", "ref_width": 2560, "ref_height": 1440,
                 "threshold": 0.85, "roi": True, "roi_margin": 180},
    "input": {"tap_jitter": 3, "delay_min": 0.55, "delay_max": 1.10},
    "game": {"name": "双穹幻想 Burst", "home_template": "btn_tokuten",
             "title_template": "title_logo",
             "home_tab": "tab_home", "back_point": [70, 70]},
    "bot": {"poll_interval": 0.30, "default_timeout": 15, "save_on_fail": True},
    "ui": {"port": 8765, "preview_width": 1024, "fps_mode": "auto"},
    "logging": {"level": "INFO", "dir": "logs"},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(root: Path | None = None) -> dict:
    r = Path(root or paths.app_dir())
    p = paths.config_path(r)
    data: dict = {}
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"配置文件读不了，用默认值：{e}")
            data = {}
    cfg = _merge(DEFAULT, data)
    # 相对路径都按程序目录解析，避免「从别处双击」时找不到模板
    tpl = Path(str(cfg["template"]["dir"]))
    if not tpl.is_absolute():
        cfg["template"]["dir"] = str(r / tpl)
    # 调试用：临时换地址/端口，不改配置文件
    import os
    if os.environ.get("KYWEB_PORT"):
        try:
            cfg["browser"]["port"] = int(os.environ["KYWEB_PORT"])
        except ValueError:
            pass
    if os.environ.get("KYWEB_URL"):
        cfg["browser"]["url"] = os.environ["KYWEB_URL"]
    if os.environ.get("KYWEB_VIEWPORT"):
        cfg["browser"]["viewport"] = os.environ["KYWEB_VIEWPORT"]
    return cfg


def build(cfg: dict, root: Path | None = None, log=None):
    r = Path(root or paths.app_dir())
    tpl_cfg = cfg.get("template", {}) or {}
    ref = None
    try:
        ref = (int(tpl_cfg.get("ref_width", 2560)), int(tpl_cfg.get("ref_height", 1440)))
    except Exception:
        pass

    matcher = Matcher(
        str(tpl_cfg.get("dir", r / "assets/templates")),
        ref_size=ref,
        threshold=float(tpl_cfg.get("threshold", 0.85)),
        roi=bool(tpl_cfg.get("roi", True)),
        roi_margin=int(tpl_cfg.get("roi_margin", 180)),
    )

    session = GameSession(cfg, log, r)
    cap = Capture(session, log,
                  preview_width=int((cfg.get("ui") or {}).get("preview_width", 1024)))
    dev = Device(session, cap, matcher, cfg, log)
    return session, cap, matcher, dev
