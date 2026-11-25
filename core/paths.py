"""路径。网页版没有模拟器，这里只处理「程序在哪、数据写哪」。

打包成 exe 时 `__file__` 在临时解压目录（只读），所以分成两个根：
    app_dir()  可写：配置、日志、模板备份、Chrome 用户目录
    res_dir()  只读：内置的 web/ 与默认模板
源码运行时两者相同。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """可写根目录。"""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def res_dir() -> Path:
    """只读资源根目录（PyInstaller 解压到 _MEIPASS）。"""
    if _frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def config_path(root: Path | None = None) -> Path:
    return (root or app_dir()) / "config.yaml"


def ensure_layout(root: Path | None = None) -> None:
    """首次运行把目录建好，并把默认模板复制到可写目录（用户裁的新模板要能落地）。"""
    r = root or app_dir()
    for d in ("logs", "logs/shots", "logs/fails", "tasks/recipes", "chrome-data"):
        (r / d).mkdir(parents=True, exist_ok=True)

    dst = r / "assets" / "templates"
    dst.mkdir(parents=True, exist_ok=True)
    src = res_dir() / "assets" / "templates"
    if src.exists() and src.resolve() != dst.resolve():
        for p in src.glob("*.png"):
            t = dst / p.name
            if not t.exists():
                t.write_bytes(p.read_bytes())
            j = p.with_suffix(".json")
            tj = dst / j.name
            if j.exists() and not tj.exists():
                tj.write_bytes(j.read_bytes())


def find_chrome() -> str:
    """找一个能带 --remote-debugging-port 起来的 Chrome/Edge。

    注意：Chrome 136 以后禁止在「默认用户目录」上开调试端口，所以本程序
    一律用自己的一份 profile（chrome-data/），跟用户平时的浏览器互不干扰。
    """
    cands = [
        os.environ.get("KYWEB_CHROME", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return ""
