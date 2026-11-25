"""日志 + 失败现场存档。"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import cv2
import numpy as np


def setup(level: str = "INFO", log_dir: str = "logs") -> logging.Logger:
    # Windows 控制台默认 GBK，中文日志会炸，强制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and (stream.encoding or "").lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("gamebot")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    fh = logging.FileHandler(
        Path(log_dir) / f"run_{time.strftime('%Y%m%d_%H%M%S')}.log", encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


def save_shot(img: np.ndarray, tag: str, out_dir: str = "logs/fails") -> str:
    """把当前画面存盘，用于排查"为什么没认出来"。"""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:40]
    path = Path(out_dir) / f"{time.strftime('%H%M%S')}_{safe}.png"
    ok, buf = cv2.imencode(".png", img)
    if ok:
        buf.tofile(str(path))  # 支持中文路径
    return str(path)
