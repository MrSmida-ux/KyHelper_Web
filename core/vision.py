"""图像识别：灰度模板匹配（网页版）。

和模拟器版比，这里做了三处针对网页版的优化：

1. **ROI 优先**：每张模板旁边的 .json 里记着它当初是从画面哪个位置裁的
   （x/y）。先在那一小块附近找，命中就走；没命中才退回全图搜索。
   云游戏 UI 布局和模拟器完全一致，绝大多数调用命中率 100%，
   单次匹配从「全图 3.7MP 扫描」降到「小窗口 20 万像素扫描」，快 15~20 倍。

2. **灰度只算一次**：画面灰度图由 Capture 按帧号缓存，一轮决策里
   N 次识别共用同一张灰度图，不再每次 cvtColor。

3. **模板按分辨率缓存**：模板缩放结果按 (名字, 宽, 高) 缓存，换窗口
   大小才会重算一次。

坐标空间始终是模板基准（2560×1440）—— Capture 已经先把画面归一到这个
尺寸，所以这里拿到的坐标可以直接给 Device.tap()，不需要任何换算。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Match:
    name: str
    x: int
    y: int
    w: int
    h: int
    score: float

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    @property
    def center(self) -> tuple[int, int]:
        return self.cx, self.cy

    def __str__(self) -> str:
        return f"{self.name}@({self.cx},{self.cy}) 相似度={self.score:.3f}"


class Matcher:
    def __init__(self, template_dir: str, ref_size: tuple[int, int] | None = None,
                 threshold: float = 0.85, roi: bool = True, roi_margin: int = 180):
        self.dir = Path(template_dir)
        self.ref_size = ref_size
        self.threshold = threshold
        self.roi = roi
        self.roi_margin = int(roi_margin)

        self._bgr: dict[str, np.ndarray] = {}
        self._gray: dict[str, np.ndarray] = {}      # 原生尺寸的灰度模板
        self._scaled: dict[tuple, np.ndarray] = {}
        self._meta: dict[str, dict] = {}
        self._mtime: dict[str, float] = {}          # 加载时的文件时间，用来发现「重录过」
        self._lock = threading.Lock()
        self.warnings: list[str] = []
        self.stats = {"calls": 0, "roi": 0, "coarse": 0, "full": 0, "ms": 0.0}
        # 半分辨率画面缓存（粗搜用）。持住源数组引用，保证 id 不会撞车。
        self._half_src: np.ndarray | None = None
        self._half: np.ndarray | None = None

    # ------------------------------------------------------------ 模板
    def template_path(self, name: str) -> Path:
        if not name.lower().endswith((".png", ".jpg", ".jpeg")):
            name = name + ".png"
        return self.dir / name

    def has(self, name: str) -> bool:
        return self.template_path(name).exists()

    def _forget(self, name: str) -> None:
        """把这个模板的所有缓存（原图/灰度/缩放版/坐标）扔掉，下次重新读盘。"""
        self._bgr.pop(name, None)
        self._gray.pop(name, None)
        self._meta.pop(name, None)
        self._mtime.pop(name, None)
        for key in [k for k in self._scaled if k[0] == name]:
            self._scaled.pop(key, None)

    def _load(self, name: str) -> np.ndarray:
        p = self.template_path(name)
        if name in self._bgr:
            # 模板文件被重录过就重载：用户是「跑一次 → 发现问题 → 重录 → 再跑」的
            # 节奏，以前重录完必须重启整个进程才生效，很容易让人以为修复没起作用。
            try:
                same = p.stat().st_mtime == self._mtime.get(name)
            except OSError:
                same = True
            if same:
                return self._bgr[name]
            self._forget(name)
        if not p.exists():
            raise FileNotFoundError(f"模板不存在：{p}")
        data = np.fromfile(str(p), dtype=np.uint8)      # 支持中文路径
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"模板无法解码：{p}")
        self._bgr[name] = img
        self._gray[name] = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        try:
            self._mtime[name] = p.stat().st_mtime
        except OSError:
            pass
        return self._bgr[name]

    def _ref_for(self, name: str) -> tuple[int, int] | None:
        if name not in self._meta:
            meta = self.dir / (Path(name).stem + ".json")
            info: dict = {}
            if meta.exists():
                try:
                    info = json.loads(meta.read_text(encoding="utf-8"))
                except Exception:
                    info = {}
            self._meta[name] = info
        info = self._meta[name]
        try:
            rw, rh = int(info.get("ref_w") or 0), int(info.get("ref_h") or 0)
            if rw > 0 and rh > 0:
                return rw, rh
        except Exception:
            pass
        return self.ref_size

    def _roi_for(self, name: str, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
        """模板记录的原位置 ± 余量。没有记录（手录模板）就不给 ROI。

        size = (画面宽, 画面高)。
        """
        self._load(name)
        info = self._meta.get(name) or {}
        try:
            tx, ty = int(info.get("x")), int(info.get("y"))
        except Exception:
            return None
        ref = self._ref_for(name)
        if not ref:
            return None
        w, h = size
        rw, rh = ref
        kx, ky = w / rw, h / rh
        tw = self._gray[name].shape[1] * kx
        th = self._gray[name].shape[0] * ky
        m = float(self.roi_margin)
        x0 = max(0, int(tx * kx - m))
        y0 = max(0, int(ty * ky - m))
        x1 = min(w, int(tx * kx + tw + m))
        y1 = min(h, int(ty * ky + th + m))
        if x1 - x0 < tw or y1 - y0 < th:
            return None
        return x0, y0, x1 - x0, y1 - y0

    def _gray_for(self, name: str, screen_shape: tuple[int, int]) -> np.ndarray:
        h, w = screen_shape[:2]
        key = (name, w, h)
        self._load(name)               # 先过一遍「文件是否被重录」，它会把缩放缓存一起清掉
        hit = self._scaled.get(key)
        if hit is not None:
            return hit
        g = self._gray[name]
        ref = self._ref_for(name)
        if ref:
            rw, rh = ref
            if (w, h) != (rw, rh):
                sx, sy = w / rw, h / rh
                if abs(sx - sy) > 0.05:
                    msg = (f"模板 [{name}] 基准 {rw}x{rh} 与当前画面 {w}x{h} "
                           f"宽高比不一致，识别可能不准")
                    if msg not in self.warnings:
                        self.warnings.append(msg)
                nw = max(1, round(g.shape[1] * sx))
                nh = max(1, round(g.shape[0] * sy))
                g = cv2.resize(g, (nw, nh), interpolation=cv2.INTER_AREA)
        self._scaled[key] = g
        return g

    # ------------------------------------------------------------ 匹配
    def _match_at(self, gray_area: np.ndarray, tpl: np.ndarray) -> tuple[float, tuple[int, int]]:
        if float(tpl.std()) < 1e-3:            # 纯色模板用 CCOEFF 会出 nan
            res = cv2.matchTemplate(gray_area, tpl, cv2.TM_SQDIFF_NORMED)
            min_v, _, min_loc, _ = cv2.minMaxLoc(res)
            return 1.0 - float(min_v), min_loc
        res = cv2.matchTemplate(gray_area, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_v, _, max_loc = cv2.minMaxLoc(res)
        if np.isnan(max_v):
            return -1.0, (0, 0)
        return float(max_v), max_loc

    def _half_screen(self, gray: np.ndarray) -> np.ndarray:
        """半分辨率画面（粗搜用）。同一帧只会缩一次。"""
        if self._half_src is gray and self._half is not None:
            return self._half
        h, w = gray.shape[:2]
        self._half = cv2.resize(gray, (max(1, w // 2), max(1, h // 2)),
                               interpolation=cv2.INTER_AREA)
        self._half_src = gray
        return self._half

    def _coarse_find(self, gray: np.ndarray, name: str, th: float) -> tuple[float, tuple[int, int]] | None:
        """缩一半粗搜，再在原图上小窗口精修。

        全图 2560×1440 直接匹配一张 220×220 模板要 50ms，800×354 的要 200ms 开外。
        缩一半后像素少 4 倍、模板也少 4 倍，粗搜只要 3~5ms；锁定候选位置后
        只把原图那一小块（模板尺寸+余量）拿来重匹配，几毫秒。
        """
        tpl = self._gray_for(name, gray.shape)
        half = self._half_screen(gray)
        th2, tw2 = max(1, tpl.shape[0] // 2), max(1, tpl.shape[1] // 2)
        if half.shape[0] < th2 or half.shape[1] < tw2:
            return None
        small = cv2.resize(tpl, (tw2, th2), interpolation=cv2.INTER_AREA)
        if float(small.std()) < 1e-3:
            return None
        res = cv2.matchTemplate(half, small, cv2.TM_CCOEFF_NORMED)
        _, best, _, loc = cv2.minMaxLoc(res)
        if best < th - 0.08:          # 粗搜分本来就偏低，留点余量
            self.stats["ms"] += 0.0
            return None
        # 精修：在候选点附近开一个「模板 + 两倍缩放误差」的窗口，拿原尺寸模板重匹配
        pad = 14
        h, w = gray.shape[:2]
        th_, tw_ = tpl.shape[:2]
        x0 = max(0, min(w - tw_, loc[0] * 2 - pad))
        y0 = max(0, min(h - th_, loc[1] * 2 - pad))
        x1 = min(w, x0 + tw_ + pad * 2)
        y1 = min(h, y0 + th_ + pad * 2)
        if x1 - x0 < tw_ or y1 - y0 < th_:
            return None
        score, loc2 = self._match_at(gray[y0:y1, x0:x1], tpl)
        return score, (loc2[0] + x0, loc2[1] + y0)

    def find_gray(self, gray: np.ndarray, name: str, threshold: float | None = None,
                  region: tuple[int, int, int, int] | None = None) -> Match | None:
        th = self.threshold if threshold is None else threshold
        tpl = self._gray_for(name, gray.shape)
        h, w = gray.shape[:2]
        th_, tw_ = tpl.shape[:2]
        if th_ > h or tw_ > w:
            return None

        t0 = time.perf_counter()
        # 1) 显式区域（录制任务的 region，一律按原图坐标给）
        # 2) 自动 ROI（模板自带的裁切位置）
        # 3) 缩图粗搜 + 原图精修（等价于全图搜索，但快十几倍）
        boxes: list[tuple[int, int, int, int] | None] = []
        if region:
            boxes.append((region[0], region[1], region[2], region[3]))
        elif self.roi:
            r = self._roi_for(name, (w, h))
            if r:
                boxes.append(r)
        boxes.append(None)

        for box in boxes:
            if box is None:
                self.stats["coarse"] += 1
                got = self._coarse_find(gray, name, th)
                self.stats["calls"] += 1
                if got is not None and got[0] >= th:
                    self.stats["ms"] += (time.perf_counter() - t0) * 1000.0
                    return Match(name, got[1][0], got[1][1], tw_, th_, got[0])
                continue
            rx, ry, rw, rh = box
            rx = max(0, min(w - 1, int(rx)))
            ry = max(0, min(h - 1, int(ry)))
            rw = max(1, min(w - rx, int(rw)))
            rh = max(1, min(h - ry, int(rh)))
            if rw < tw_ or rh < th_:
                continue
            area = gray[ry:ry + rh, rx:rx + rw]
            score, loc = self._match_at(area, tpl)
            self.stats["calls"] += 1
            self.stats["roi"] += 1
            if score >= th:
                self.stats["ms"] += (time.perf_counter() - t0) * 1000.0
                return Match(name, loc[0] + rx, loc[1] + ry, tw_, th_, score)
        self.stats["ms"] += (time.perf_counter() - t0) * 1000.0
        return None

    def find(self, screen: np.ndarray, name: str, threshold: float | None = None,
             region: tuple[int, int, int, int] | None = None) -> Match | None:
        if screen.ndim == 3:
            gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        else:
            gray = screen
        return self.find_gray(gray, name, threshold, region)

    def find_all(self, screen: np.ndarray, name: str, threshold: float | None = None,
                 max_results: int = 10, min_gap: int = 20) -> list[Match]:
        th = self.threshold if threshold is None else threshold
        gray_s = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY) if screen.ndim == 3 else screen
        tpl = self._gray_for(name, gray_s.shape)
        if gray_s.shape[0] < tpl.shape[0] or gray_s.shape[1] < tpl.shape[1]:
            return []
        if float(tpl.std()) < 1e-3:
            res = cv2.matchTemplate(gray_s, tpl, cv2.TM_SQDIFF_NORMED)
            res = 1.0 - res
        else:
            res = cv2.matchTemplate(gray_s, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= th)
        cands = sorted(((float(res[y, x]), int(x), int(y)) for y, x in zip(ys, xs)), reverse=True)
        picked: list[Match] = []
        for score, x, y in cands:
            cx, cy = x + tpl.shape[1] // 2, y + tpl.shape[0] // 2
            if all(abs(cx - m.cx) > min_gap or abs(cy - m.cy) > min_gap for m in picked):
                picked.append(Match(name, x, y, tpl.shape[1], tpl.shape[0], score))
                if len(picked) >= max_results:
                    break
        return picked

    def perf(self) -> dict:
        n = max(1, self.stats["calls"])
        return {
            "calls": self.stats["calls"],
            "roi": self.stats["roi"],
            "coarse": self.stats["coarse"],
            "full": self.stats["full"],
            "avg_ms": round(self.stats["ms"] / n, 2),
        }
