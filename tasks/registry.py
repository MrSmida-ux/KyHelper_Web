"""任务总表：内置 Python 任务 + 录制的 YAML 任务。

GUI 和命令行都从这里取任务，保证两边看到的是同一份清单。
"""
from __future__ import annotations

import json
from pathlib import Path

from core import recipe
from core.device import Device

from .daily import JOBS, Job

RECIPE_DIR = "tasks/recipes"
# 界面上拖动排好的顺序存这里。放在 tasks/ 下（可写目录），
# 打包时 build.py 会把它一起备份，重新打包不会丢。
ORDER_FILE = "tasks/order.json"
# 每个任务勾选的属性/难度之类。同样要跟着打包备份，不然换台机器就得重选。
OPTIONS_FILE = "tasks/options.json"
# 「这一轮要跑哪几个任务」的勾选。以前只存在浏览器 localStorage 里，
# 换端口（127.0.0.1:8765 → 8766）就等于换了域，勾选全丢；重新打包也带不走。
# 现在跟顺序/选项一样落到磁盘，界面启动时以这份为准。
SELECTION_FILE = "tasks/selection.json"


def recipe_dir(root: str | Path) -> Path:
    return Path(root) / RECIPE_DIR


def order_path(root: str | Path) -> Path:
    return Path(root) / ORDER_FILE


ORDER_HINT = ("改这个文件就能排序：keys 里从上到下就是执行顺序，"
              "可以写任务 key，也可以直接写界面上的任务名；"
              "没写的任务自动排在最后。改完在界面上点「刷新任务」生效。")


def load_order(root: str | Path) -> list[str]:
    """读用户排好的任务顺序。没有或读坏了就返回空表（等于用默认顺序）。

    手改这个文件的人不一定知道 key，所以允许用任务名；也允许直接写一个
    纯数组（["a","b"]）或者 {"order": [...]}，两种写法都认。
    """
    p = order_path(root)
    try:
        # utf-8-sig：记事本另存出来的文件带 BOM，直接 json.loads 会炸
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    if isinstance(data, dict):
        keys = data.get("keys")
        if keys is None:
            keys = data.get("order")
    else:
        keys = data
    if not isinstance(keys, list):
        return []
    out: list[str] = []
    for k in keys:
        s = str(k).strip()
        if s and s not in out:      # 手写时重复一行不算错，去重就完了
            out.append(s)
    return out


def save_order(root: str | Path, keys: list[str]) -> None:
    p = order_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 保留文件里别的字段（比如用户自己写的注释字段），只覆盖 keys
    data: dict = {}
    try:
        old = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(old, dict):
            data = old
    except Exception:
        data = {}
    data["keys"] = list(keys)
    data.setdefault("_hint", ORDER_HINT)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm(s: str) -> str:
    """比对任务名时忽略空格、全半角和一些装饰符号，手写容错高一点。"""
    t = str(s or "").strip().lower()
    for ch in " \t\u3000·・-—_()（）[]【】":
        t = t.replace(ch, "")
    return t


def apply_order(jobs: list[Job], keys: list[str]) -> list[Job]:
    """按 keys 的顺序排；keys 里没有的（新加的任务）排在最后，保持原相对顺序。

    keys 里的每一项先当 key 找，再当任务名找 —— 手改 order.json 时写名字更省事。
    """
    if not keys:
        return jobs
    idx: dict[str, int] = {}
    for i, k in enumerate(keys):
        idx.setdefault(k, i)
        n = _norm(k)
        if n:
            idx.setdefault(n, i)
    tail = len(keys)

    def rank(j: Job) -> int:
        if j.key in idx:
            return idx[j.key]
        return idx.get(_norm(j.name), tail)

    return sorted(jobs, key=rank)


# ---------------------------------------------------------------- 勾选的任务
def selection_path(root: str | Path) -> Path:
    return Path(root) / SELECTION_FILE


def load_selection(root: str | Path) -> list[str] | None:
    """读上次勾选的任务。文件不存在返回 None（界面会退回「默认全选」）。"""
    p = selection_path(root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    keys = data.get("keys") if isinstance(data, dict) else data
    if not isinstance(keys, list):
        return None
    return [str(k) for k in keys]


def save_selection(root: str | Path, keys: list[str]) -> None:
    p = selection_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"keys": [str(k) for k in keys]}, ensure_ascii=False, indent=2),
                 encoding="utf-8")


# ---------------------------------------------------------------- 任务选项
def options_path(root: str | Path) -> Path:
    return Path(root) / OPTIONS_FILE


def load_options(root: str | Path) -> dict[str, dict[str, list[str]]]:
    """读用户给任务勾的选项。读坏了就当没勾过，用任务自带的默认值。"""
    p = options_path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for key, val in data.items():
        if isinstance(val, dict):
            out[str(key)] = {str(k): [str(v) for v in vs]
                             for k, vs in val.items() if isinstance(vs, list)}
    return out


def save_options(root: str | Path, data: dict[str, dict[str, list[str]]]) -> None:
    p = options_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def default_options(job: Job) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for opt in job.options:
        vals = opt.get("default")
        out[str(opt.get("key", ""))] = [str(v) for v in vals] if isinstance(vals, list) else []
    return out


def resolved_options(job: Job, stored: dict[str, dict[str, list[str]]]) -> dict[str, list[str]]:
    """默认值 + 用户勾的，合并成这次执行真正用的那份。

    勾选里出现了选项定义之外的值（改过任务、换过版本）就丢掉，避免脏数据
    传进流程里点出一堆看不懂的东西。
    """
    out = default_options(job)
    saved = stored.get(job.key) or {}
    for opt in job.options:
        key = str(opt.get("key", ""))
        if key not in saved:
            continue
        # 数值型选项：只留一个落在 [min,max] 的整数
        if opt.get("type") == "number":
            try:
                n = int(saved[key][0])
            except (ValueError, TypeError, IndexError):
                continue
            lo = int(opt.get("min", 1))
            hi = int(opt.get("max", 99))
            if lo <= n <= hi:
                out[key] = [str(n)]
            continue
        # 文本型选项：保留原字符串（如章节 "16-3-3"），只清掉首尾空白
        if opt.get("type") == "text":
            if saved[key] and str(saved[key][0]).strip():
                out[key] = [str(saved[key][0]).strip()]
            continue
        allowed = [str(c.get("v")) for c in (opt.get("choices") or [])]
        picked = [v for v in saved[key] if v in allowed]
        if picked:
            out[key] = picked
    return out


def job_from_recipe(path: str | Path) -> Job:
    p = Path(path)
    data = recipe.load_recipe(p)
    steps = recipe.parse_steps(data.get("steps", []))
    if not steps:
        raise ValueError(f"{p.name} 没有任何步骤")

    key = str(data.get("key") or p.stem)
    name = str(data.get("name") or key)

    def _run(dev: Device) -> None:
        recipe.exec_steps(dev, steps, dev.log)

    return Job(
        key=key,
        name=name,
        fn=_run,
        needs=recipe.collect_needs(steps),
        desc=str(data.get("desc", "") or f"录制任务 · {len(steps)} 步"),
        source="recipe",
        path=str(p),
    )


def load_jobs(root: str | Path) -> list[Job]:
    jobs = list(JOBS)
    used = {j.key for j in jobs}

    d = recipe_dir(root)
    if not d.exists():
        return apply_order(jobs, load_order(root))

    for p in sorted(d.glob("*.yaml")):
        try:
            job = job_from_recipe(p)
        except Exception as e:
            import logging
            logging.getLogger("gamebot").warning(f"跳过无效任务 {p.name}：{e}")
            continue
        if job.key in used:
            import logging
            logging.getLogger("gamebot").warning(
                f"任务 key 重复，跳过 {p.name}（{job.key} 已被占用）"
            )
            continue
        used.add(job.key)
        jobs.append(job)
    return apply_order(jobs, load_order(root))


def find_job(root: str | Path, key: str) -> Job | None:
    for j in load_jobs(root):
        if j.key == key:
            return j
    return None
