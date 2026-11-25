"""录制：把「点一下」变成可重放的步骤。

核心难点是自动裁模板。用户只给了一个坐标，程序得自己决定框多大、
这块区域稳不稳、会不会跟屏幕别处撞脸。

策略：从点击前的画面裁出若干候选尺寸，逐个评分——
  1. 唯一性优先（全屏只能命中 1 处，否则重放可能点错）
  2. 其次是纹理量（纯色块没法定位）
  3. 再次是尺寸（大一点能多带上下文）
得分低或者撞脸的，会把原因回传给界面提示用户重录。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import recipe

# 候选裁剪尺寸（宽, 高）。游戏按钮大多在这个区间。
CANDIDATES = [(320, 200), (280, 170), (240, 150), (200, 120), (160, 100)]
MIN_STD = 4.0          # 纹理量下限，低于这个基本是纯色背景
DUP_SCORE = 0.90       # 判定"撞脸"的相似度
GOOD_SCORE = 0.95      # 自匹配达标的线


@dataclass
class CropResult:
    box: tuple[int, int, int, int]   # x1, y1, w, h
    score: float
    dup: int                          # 全屏相似区域个数
    std: float
    ok: bool
    hint: str = ""


def _count_hits(gray_screen: np.ndarray, gray_tpl: np.ndarray,
                th: float = DUP_SCORE, max_hits: int = 8) -> int:
    """数全屏有多少处像这块模板（做非极大值抑制，避免同一处算多次）。"""
    if (gray_screen.shape[0] < gray_tpl.shape[0]
            or gray_screen.shape[1] < gray_tpl.shape[1]):
        return 0
    if float(gray_tpl.std()) < 1e-3:
        return 0
    res = cv2.matchTemplate(gray_screen, gray_tpl, cv2.TM_CCOEFF_NORMED)
    if np.isnan(res).any():
        res = np.nan_to_num(res, nan=0.0)
    ys, xs = np.where(res >= th)
    if len(xs) == 0:
        return 0

    cands = sorted(((float(res[y, x]), int(x), int(y))
                    for y, x in zip(ys, xs)), reverse=True)
    h, w = gray_tpl.shape[:2]
    picked: list[tuple[int, int]] = []
    for _, x, y in cands:
        if all(abs(x - px) > w * 0.6 or abs(y - py) > h * 0.6
               for px, py in picked):
            picked.append((x, y))
            if len(picked) >= max_hits:
                break
    return len(picked)


def auto_crop(frame: np.ndarray, cx: int, cy: int) -> CropResult:
    """以 (cx,cy) 为中心挑一块最适合当模板的区域。"""
    H, W = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    best: CropResult | None = None
    for w, h in CANDIDATES:
        x1 = max(0, min(W - w, cx - w // 2))
        y1 = max(0, min(H - h, cy - h // 2))
        if x1 + w > W or y1 + h > H:
            continue
        tpl = frame[y1:y1 + h, x1:x1 + w]
        gray_tpl = gray[y1:y1 + h, x1:x1 + w]
        std = float(gray_tpl.std())
        if std < MIN_STD:
            continue

        # 自匹配得分
        res = cv2.matchTemplate(gray, gray_tpl, cv2.TM_CCOEFF_NORMED)
        if np.isnan(res).any():
            res = np.nan_to_num(res, nan=0.0)
        score = float(res.max())
        dup = _count_hits(gray, gray_tpl)

        cand = CropResult((x1, y1, w, h), score, dup, std,
                          ok=(dup == 1 and score >= GOOD_SCORE))

        # 排序规则：唯一命中的优先 → 得分高 → 纹理多 → 尺寸大
        def rank(c: CropResult):
            return (c.dup == 1, round(c.score, 3), round(c.std, 1), c.box[2] * c.box[3])

        if best is None or rank(cand) > rank(best):
            best = cand

    if best is None:
        return CropResult((0, 0, 0, 0), 0.0, 0, 0.0, False,
                          "这块区域太素了（纯色背景），做模板认不出来")

    if best.dup > 1:
        best.hint = f"屏幕上有 {best.dup} 处长得一样，重放可能点错；建议改点带文字的位置"
    elif best.score < GOOD_SCORE:
        best.hint = f"相似度只有 {best.score:.2f}，这块可能在动；建议换一个静态位置点"
    return best


def _safe_key(s: str) -> str:
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in s.strip())
    out = out.strip("_").lower()
    return out or "task"


class Recording:
    """一次录制会话。"""

    def __init__(self, root: str | Path, tpl_dir: str | Path, key: str, name: str):
        self.root = Path(root)
        self.tpl_dir = Path(tpl_dir)
        self.tpl_dir.mkdir(parents=True, exist_ok=True)
        self.key = _safe_key(key)
        self.name = name or self.key
        self.steps: list[dict | str] = []
        self.created: list[str] = []
        self._seq = 0

    # ---------- 记录 ----------
    def add_tap(self, frame: np.ndarray, x: int, y: int) -> dict:
        """frame 必须是「点击前」的画面 —— 重放时是先看到它再点。"""
        crop = auto_crop(frame, x, y)
        if crop.box[2] == 0:
            return {"ok": False, "hint": crop.hint}

        self._seq += 1
        tname = f"rec_{self.key}_{self._seq:02d}"
        x1, y1, w, h = crop.box
        tpl = frame[y1:y1 + h, x1:x1 + w]

        cv2.imencode(".png", tpl)[1].tofile(str(self.tpl_dir / f"{tname}.png"))
        import json
        (self.tpl_dir / f"{tname}.json").write_text(
            json.dumps({"ref_w": int(frame.shape[1]), "ref_h": int(frame.shape[0]),
                        "x": int(x1), "y": int(y1)}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.created.append(tname)

        step = {"tap_img": {"target": tname, "timeout": 6,
                            "at": [int(x), int(y)]}}
        self.steps.append(step)
        return {
            "ok": True,
            "template": tname,
            "score": round(crop.score, 3),
            "dup": crop.dup,
            "size": [w, h],
            "warning": crop.hint,
            "step": f"点击 {tname}",
            "index": len(self.steps),
        }

    def add_cmd(self, op: str, value=None) -> dict:
        if value is None:
            self.steps.append(op)
            label = op
        else:
            self.steps.append({op: value})
            label = f"{op} {value}"
        return {"ok": True, "step": label, "index": len(self.steps)}

    def undo(self) -> dict:
        """撤销上一步。如果那步带了模板，模板也一起删掉。"""
        if not self.steps:
            return {"ok": False, "hint": "没有可撤销的步骤"}
        last = self.steps.pop()
        removed = ""
        if isinstance(last, dict) and "tap_img" in last:
            a = last["tap_img"]
            tname = a.get("target") if isinstance(a, dict) else a
            if tname in self.created:
                for ext in (".png", ".json"):
                    p = self.tpl_dir / f"{tname}{ext}"
                    if p.exists():
                        p.unlink()
                self.created.remove(tname)
                removed = tname
                # 文件名序号不回退，避免和已删模板重名
        return {"ok": True, "removed": removed, "left": len(self.steps)}

    def move(self, index: int, delta: int) -> dict:
        i = index - 1
        j = i + delta
        if not (0 <= i < len(self.steps) and 0 <= j < len(self.steps)):
            return {"ok": False, "hint": "位置越界"}
        self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
        return {"ok": True}

    # ---------- 保存 ----------
    def save(self, key: str | None = None, name: str | None = None,
             desc: str = "", steps: list | None = None) -> Path:
        if key:
            new_key = _safe_key(key)
            if new_key != self.key:
                self._rename_templates(new_key)
                self.key = new_key
        if name:
            self.name = name
        if steps is not None:
            self.steps = list(steps)
        if not self.steps:
            raise ValueError("一个步骤都没有，先点几下画面")

        data = {
            "key": self.key,
            "name": self.name,
            "desc": desc or f"GUI 录制 · {len(self.steps)} 步",
            "steps": self.steps,
        }
        path = self.root / "tasks" / "recipes" / f"{self.key}.yaml"
        recipe.save_recipe(path, data)
        return path

    def _rename_templates(self, new_key: str) -> None:
        """改了任务 key，模板文件也要跟着改名，否则步骤里的引用会断。"""
        mapping = {}
        for old in list(self.created):
            suffix = old.split("_")[-1]          # 01 / 02 ...
            new = f"rec_{new_key}_{suffix}"
            for ext in (".png", ".json"):
                src = self.tpl_dir / f"{old}{ext}"
                if src.exists():
                    src.rename(self.tpl_dir / f"{new}{ext}")
            mapping[old] = new
        self.created = [mapping[c] for c in self.created]
        for st in self.steps:
            if isinstance(st, dict) and "tap_img" in st:
                a = st["tap_img"]
                if isinstance(a, dict) and a.get("target") in mapping:
                    a["target"] = mapping[a["target"]]

    def labels(self) -> list[str]:
        out = []
        for st in self.steps:
            if isinstance(st, str):
                out.append(st)
            elif isinstance(st, dict):
                op, val = next(iter(st.items()))
                if isinstance(val, dict):
                    t = val.get("target", "")
                    out.append(f"{op} {t}" if t else op)
                else:
                    out.append(f"{op} {val}")
        return out
