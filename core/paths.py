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


def chdir_app() -> None:
    """把工作目录切到 app_dir。

    有些地方（如 tasks/labyrinth.py）用相对路径读 assets/templates，源码运行时
    当前目录就是仓库根，没问题；双击 exe 时当前目录可能是桌面或 C:\\Windows，
    不切就会找不到模板。统一在启动时切一次。
    """
    try:
        os.chdir(app_dir())
    except OSError:
        pass


def _copy_missing(src: Path, dst: Path, pattern: str = "*") -> None:
    """把 src 下匹配 pattern 的文件按原相对路径补到 dst（已存在的不动，不覆盖用户改动）。"""
    if not src.exists():
        return
    for p in src.rglob(pattern):
        if not p.is_file():
            continue
        t = dst / p.relative_to(src)
        if not t.exists():
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(p.read_bytes())


def ensure_layout(root: Path | None = None) -> None:
    """首次运行把目录建好，并把内置资源复制到可写目录（用户裁的新模板要能落地）。"""
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

    # 内置配置：第一次运行放出来一份，用户改坏了删掉还会再生成
    cfg = r / "config.yaml"
    src_cfg = res_dir() / "config.yaml"
    if not cfg.exists() and src_cfg.exists() and src_cfg.resolve() != cfg.resolve():
        cfg.write_bytes(src_cfg.read_bytes())

    # 录制的任务 yaml（含 _embedded）：exe 版没有仓库目录，得放一份到可写目录
    src_rec = res_dir() / "tasks" / "recipes"
    if src_rec.exists() and src_rec.resolve() != (r / "tasks" / "recipes").resolve():
        _copy_missing(src_rec, r / "tasks" / "recipes", "*.yaml")

    # tasks/ 下的 order/selection/options.json 是用户配置，首次给个默认的
    for name in ("order.json", "selection.json", "options.json"):
        s = res_dir() / "tasks" / name
        t = r / "tasks" / name
        if s.exists() and not t.exists():
            t.write_bytes(s.read_bytes())


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
