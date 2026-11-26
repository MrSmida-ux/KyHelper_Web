"""迷宫挑战：一个任务、两种方式（游戏每天只给一次，所以合并成一个任务）。

  方式=跳过   点 SKIP 白嫖：跑 tasks/recipes/_embedded/ 里录制的 7 步，十几秒完事
  方式=手动   按指定王进迷宫，逐层逐房真打，打到通关回主页（本文件的主体逻辑）

手动：6 个王随机刷 3 个，用户指定目标王；难度 Hard 5 层 / Normal 3 层。

== 完整机制（2026-11-26 实打 1F~5F 验证）==
1. 入口：主页→冒险→特殊关卡→迷宫→难度
2. 每层开始是「三扇门」页：点门看弹窗标题「是否选择XX的迷宫 NF？」，
   用王名首字模板识别。匹配目标王→确定；不匹配→取消看下一扇；
   **三扇门的排列在关掉弹窗后不会变**，必须退出迷宫重进才重新随机
   （第 1 层退到冒险页再进最快：门页左上箭头点两下；第 2 层起只退上一层
   再点「前往下层」）。N 次没刷到就跳过提示。
3. 进层后是「路线页」：每层 5 房（最后一层的最后一层是 6 房含 BOSS）。
   - 房间槽位固定 6 个，但**小人房的数量和位置每层随机**，必须实时扫描
   - CLEAR 标记 = 已打完，跳过
   - 先打普通骷髅房，小人房最后打（打完弹伙伴選択），BOSS 最后打
4. 单房：点房→挑战准备页（扫荡可点就扫荡，灰色就挑战）→编队页挑战→
   战斗（时长不固定，轮询等待）→礼物确认→Win关闭→可能加护（概率出现）
5. 加护页：选指定类型（魔法/物理）的第一个，识别卡片中心图标
6. 伙伴選択（小人房打完）：看卡片外框颜色，金>紫>蓝，认不准默认第一个
7. 本层全 CLEAR→点「前往下层」→回三扇门页→重刷目标王→下一层
8. 最后一层 BOSS 打完→直接回冒险页→点首页回主页

模板基准全部是 2560×1440（dev.screen() 的识别帧），不是 /api/frame 的 1345。
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from core import recipe
from core.device import Device, NotFound, Skipped
from core.flows import ensure_home

# ---------------------------------------------------------------- 选项
MODE_OPTION = {
    "key": "modes", "label": "方式", "single": True,
    "hint": "跳过=点 SKIP 白嫖(录制的 7 步流程，十几秒完成)；"
            "手动=指定王真打，逐层逐房打完(耗时几分钟到二十分钟)。"
            "游戏每天只允许跑一次，所以只有这一个任务、在下面二选一",
    "choices": [{"v": "skip", "label": "跳过"},
                {"v": "manual", "label": "手动打"}],
    "default": ["manual"],
}
KINGS = [
    ("qishi", "骑士王"), ("quan", "拳王"), ("sheng", "圣王"),
    ("modao", "魔导王"), ("juji", "狙击王"), ("bawang", "霸王"),
]
KING_LABEL = {k: v for k, v in KINGS}
KING_TPL = {k: f"maze_kind_{k}" for k, _ in KINGS}

TARGET_OPTION = {
    "key": "kings", "label": "目标王", "single": True,
    "show_when": {"key": "modes", "values": ["manual"]},
    "hint": "今天想打哪个王？每次重进迷宫门重新随机(6选3)，没刷到可返回重刷，超过上限跳过",
    "choices": [{"v": k, "label": l} for k, l in KINGS],
    "default": ["qishi"],
}
DIFF_OPTION = {
    "key": "diffs", "label": "难度", "single": True,
    "show_when": {"key": "modes", "values": ["manual"]},
    "hint": "Hard=5层、Normal=3层，默认 Hard",
    "choices": [{"v": "hard", "label": "Hard(5层)"},
                {"v": "normal", "label": "Normal(3层)"}],
    "default": ["hard"],
}
BUFF_OPTION = {
    "key": "buffs", "label": "加护类型", "single": True,
    "show_when": {"key": "modes", "values": ["manual"]},
    "hint": "打完房间可能弹出加护选择，优先选这个类型的第一个。认不出则默认第一个",
    "choices": [{"v": "physical", "label": "物理"},
                {"v": "magic", "label": "魔法"}],
    "default": ["physical"],
}
RETRY_OPTION = {
    "key": "rerolls", "label": "每层重刷上限", "type": "number",
    "show_when": {"key": "modes", "values": ["manual"]},
    "min": 1, "max": 30, "default": [10],
    "hint": "每层找不到目标王时最多返回重刷几次，超过就跳过并提示（可从当前继续）",
}

NEEDS = [
    "tab_adventure", "tab_home", "maze_btn_special", "maze_btn_entry",
    "maze_btn_hard", "maze_btn_normal", "maze_btn_ok", "maze_btn_cancel",
    "maze_clear", "maze_person_icon", "maze_boss_room",
    "maze_gift_title", "maze_win", "maze_jiahu_title", "maze_partner_title",
    "maze_buff_physical", "maze_buff_magic",
]

# ---------------------------------------------------------------- 跳过分支
# 原来「迷宫挑战-跳过」是一个独立的录制任务；游戏每天只能跑一次迷宫，
# 两个任务并列会让人不知道该勾哪个，所以合并成同一个任务的「方式」选项。
# 录制步骤原样保留在 _embedded 子目录（registry 只扫 recipes/*.yaml，看不到它）。
def _skip_recipe_path() -> Path:
    """优先用可写目录里的副本（exe 首次运行会复制过去），退回打包内的。"""
    from core import paths
    cands = [paths.app_dir() / "tasks" / "recipes" / "_embedded" / "迷宫挑战_跳过.yaml",
             Path(__file__).resolve().parent / "recipes" / "_embedded" / "迷宫挑战_跳过.yaml"]
    for c in cands:
        if c.exists():
            return c
    return cands[-1]


SKIP_RECIPE = _skip_recipe_path()
# 文件被人删了也别让整块导入失败：退回按名字检查模板，运行时再报找不到步骤
SKIP_TPL_FALLBACK = [f"rec_迷宫挑战_{i:02d}" for i in range(1, 8)]
_SKIP_STEPS_CACHE: list | None = None


def skip_steps() -> list:
    """读录制下来的跳关步骤（只读一次，缓存住）。"""
    global _SKIP_STEPS_CACHE
    if _SKIP_STEPS_CACHE is None:
        _SKIP_STEPS_CACHE = recipe.parse_steps(
            recipe.load_recipe(SKIP_RECIPE).get("steps", []))
    return _SKIP_STEPS_CACHE


def _skip_needs() -> list[str]:
    try:
        return recipe.collect_needs(skip_steps()) or list(SKIP_TPL_FALLBACK)
    except Exception:
        return list(SKIP_TPL_FALLBACK)


for _n in _skip_needs():          # 跳过分支的模板也一起做启动前检查
    if _n not in NEEDS:
        NEEDS.append(_n)

# ---------------------------------------------------------------- 常量
REF_W = 2560
REF_H = 1440
GATE_X = [535, 1280, 2025]
GATE_Y = 560

# 6 个房间槽位中心（2560×1440），实测
ROOM_SLOTS = [
    (460, 445), (1280, 445), (1939, 445),   # 上排：左 / 中(BOSS) / 右
    (460, 965), (1280, 965), (1939, 965),   # 下排：左 / 中 / 右
]
BOSS_SLOT = 1  # 上中

TH = 0.85
POLL = 0.6
BATTLE_TIMEOUT = 600.0


def _xy(dev: Device, x: int, y: int) -> tuple[int, int]:
    k = dev.screen().shape[1] / REF_W
    return int(x * k), int(y * k)


def _tap(dev: Device, x: int, y: int, settle: float = 1.4) -> None:
    px, py = _xy(dev, x, y)
    dev.tap(px, py)
    if settle:
        dev.wait_stable(timeout=max(2.0, settle + 1.0))


def _has(dev: Device, name: str, th: float = TH) -> bool:
    return dev.matcher.has(name) and dev.exists(name, th)


def _find(dev: Device, name: str, th: float = TH):
    return dev.find(name, th) if dev.matcher.has(name) else None


def _find_any(dev: Device, names: list[str], th: float = TH):
    """返回 (模板名, match) 或 (None, None)。"""
    for n in names:
        r = _find(dev, n, th)
        if r is not None:
            return n, r
    return None, None


def _wait_states(dev: Device, names: list[str], timeout: float = 200.0,
                 th: float = TH):
    """轮询等待任一状态模板出现，返回 (模板名|None, 是否超时)。可中断。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        dev.check_cancel()
        dev.screen(True)
        for n in names:
            if _has(dev, n, th):
                return n, False
        time.sleep(POLL)
    return None, True


def _tap_until_gone(dev: Device, name: str, x: int, y: int,
                    tries: int = 5, th: float = TH) -> bool:
    """重复点击直到某模板消失（应对点击被吞/动画延迟）。"""
    for _ in range(tries):
        dev.check_cancel()
        dev.screen(True)
        if not _has(dev, name, th):
            return True
        _tap(dev, x, y, settle=1.5)
    return not _has(dev, name, th)


# ---------------------------------------------------------------- 导航
def enter_labyrinth(dev: Device, difficulty: str = "hard",
                    from_adventure: bool = False) -> None:
    """主页→冒险→特殊关卡→迷宫(→难度)。from_adventure=True 表示已经在冒险页，
    省掉回主页那一段（重刷三扇门时用，能少一半时间）。"""
    if not from_adventure:
        if not ensure_home(dev):
            raise NotFound("回不到主页")
        if not dev.tap_img("tab_adventure", timeout=8, threshold=0.82):
            raise NotFound("主页没找到[冒险]")
        dev.wait_stable(timeout=4)
    if not dev.tap_img("maze_btn_special", timeout=8, threshold=0.85):
        raise NotFound("冒险页没找到[特殊关卡]")
    dev.wait_stable(timeout=3)
    if not dev.tap_img("maze_btn_entry", timeout=8, threshold=0.85):
        raise NotFound("特殊关卡弹窗里没找到[迷宫]入口")
    dev.wait_stable(timeout=3)
    if difficulty == "normal":
        if dev.tap_img("maze_btn_normal", timeout=5, threshold=0.9):
            dev.wait_stable(timeout=2)
            dev.log.info("  难度切到 Normal（3 层）")
        else:
            dev.log.warning("  没找到[Normal]，沿用当前难度")
    else:
        dev.log.info("  使用 Hard 难度（5 层）")


def on_gate_page(dev: Device) -> bool:
    return _has(dev, "maze_btn_hard", 0.9) or _has(dev, "maze_btn_normal", 0.9)


def go_back_up(dev: Device) -> bool:
    """三扇门页左上箭头 → 返回上一层。返回是否成功离开门页。"""
    px, py = _xy(dev, 64, 96)
    dev.tap(px, py)
    dev.wait_stable(timeout=2)
    return not on_gate_page(dev)


def _on_adventure(dev: Device) -> bool:
    """真的在冒险页：[特殊关卡] 按钮在，且没有被「特殊关卡」弹窗盖着。"""
    return _has(dev, "maze_btn_special", 0.85) and not _has(dev, "maze_btn_entry", 0.85)


def _back_to_adventure(dev: Device, tries: int = 4) -> bool:
    """连点左上返回箭头退出迷宫，直到看见冒险页的[特殊关卡]。

    三扇门的排列是**整局迷宫**决定的，关掉门的确认框再点还是同一批门（实测
    连开 5 轮全是同一组），必须退出迷宫重进才会重新随机。第一层退出没有任何
    损失，所以直接退到冒险页最快：门页左上箭头点两下就到。
    """
    for _ in range(tries):
        dev.check_cancel()
        dev.screen(True)
        if _on_adventure(dev):
            return True
        dev.log.info("  点左上返回，退出迷宫")
        _tap(dev, 64, 96, settle=2.0)
    return _on_adventure(dev)


def is_route_page(dev: Device) -> bool:
    """当前是不是迷宫路线页（有房间槽位特征）。"""
    # 房间方块顶部有统一的红色装饰，用 CLEAR / 小人 / BOSS 任一特征或房间底纹判断
    if _in_slot(dev, "maze_boss_room", *ROOM_SLOTS[BOSS_SLOT], 0.6):
        return True
    if any(_in_slot(dev, "maze_clear", cx, cy, 0.6) for cx, cy in ROOM_SLOTS):
        return True
    return False


def reroll_gate(dev: Device, difficulty: str, floor: int = 1) -> bool:
    """重刷三扇门：退出迷宫再从冒险页/主页进来，门才会重新随机。

    第 1 层随便退（没进度可丢）：门页左上箭头点两下 → 冒险页 → 重新进迷宫。
    第 2 层起退出迷宫会从第 1 层重来，所以只能退回上一层、再点「前往下层」；
    这条路不行就不硬来，交给调用方跳过（宁可少打一层，也别把进度清掉）。
    """
    if on_gate_page(dev) and floor > 1:
        # 第 2 层起退出迷宫会从第 1 层重来，只能退回上一层再点「前往下层」
        go_back_up(dev)
        if is_route_page(dev):
            dev.log.info("  退回上一层，点[前往下层]重刷")
            _tap(dev, 1274, 1070, settle=2.5)
        if on_gate_page(dev):
            return True
        dev.log.warning("  第 %d 层退回上一层的路走不通，放弃重刷", floor)
        return False

    # 第 1 层（或已经不在门页）：门页左上箭头点两下 → 冒险页 → 重进迷宫
    at_adventure = _back_to_adventure(dev)
    if not at_adventure:
        dev.log.info("  退不到冒险页，改从主页重进")
        if not ensure_home(dev):
            return False
    enter_labyrinth(dev, difficulty, from_adventure=at_adventure)
    return on_gate_page(dev)


# ---------------------------------------------------------------- 三扇门找目标王
def _door_king(dev: Device, th: float = 0.80) -> str | None:
    """认出弹出框里是哪个王（只用来打日志、以及判断门有没有真的刷新）。"""
    best, key = th, None
    for k, tpl in KING_TPL.items():
        if not dev.matcher.has(tpl):
            continue
        m = dev.find(tpl, th)
        if m is not None and m.score >= best:
            best, key = m.score, k
    return key


def _label(key: str | None) -> str:
    return KING_LABEL.get(key, key) if key else "未知"


def find_target_gate(dev: Device, target: str, rerolls: int,
                     difficulty: str = "hard", floor: int = 1) -> bool:
    """在当前三扇门页找目标王并点确定进入本层。找不到重刷，超上限返回 False。"""
    tpl = KING_TPL[target]
    if not dev.matcher.has(tpl):
        raise NotFound(f"目标[{KING_LABEL[target]}]的模板 {tpl} 不存在")

    prev_sig = None
    same_rounds = 0
    for attempt in range(1, rerolls + 1):
        dev.check_cancel()
        if is_route_page(dev):
            dev.log.info("  已在路线页，视为已进入本层")
            return True
        if not on_gate_page(dev):
            # 既不在门页也不在路线页（点歪/弹窗没关掉）→ 重走入口，别当成已进层
            dev.log.info("  不在三扇门页，重走迷宫入口")
            if not reroll_gate(dev, difficulty, floor):
                # 回不去门页就不硬来，交给调用方按「没刷到」跳过，别把进度搞乱
                dev.log.warning("  回不到三扇门页，先不打了")
                return False

        doors: list[str | None] = []
        found_idx = None
        for i, gx in enumerate(GATE_X):
            dev.check_cancel()
            ok = None
            for _ in range(2):               # 弹窗偶尔被动画吃掉，同一扇门重试一次
                _tap(dev, gx, GATE_Y, settle=1.6)
                ok = _find(dev, "maze_btn_ok", 0.9)
                if ok is not None:
                    break
                dev.log.info(f"  第{i+1}扇门没弹出确认框，重试")
            if ok is None:
                dev.log.warning(f"  第{i+1}扇门打不开，跳过这扇")
                # 万一是弹窗开了但按钮没认出来，先关掉，别挡着后面的门
                cancel = _find(dev, "maze_btn_cancel", 0.85)
                if cancel:
                    dev.tap(cancel.cx, cancel.cy)
                    dev.wait_stable(timeout=1.2)
                doors.append(None)
                continue
            m = _find(dev, tpl, TH)
            if m is not None:
                dev.log.info(f"  第{i+1}扇门=[{KING_LABEL[target]}] ({m.score:.3f})")
                # 点确定进入
                dev.tap(ok.cx, ok.cy)
                dev.wait_stable(timeout=2.5)
                found_idx = i
                break
            king = _door_king(dev)            # 认一下是哪个王，只为了日志和比对
            doors.append(king)
            dev.log.info(f"  第{i+1}扇门=[{_label(king)}]，不是目标，取消")
            cancel = _find(dev, "maze_btn_cancel", 0.9)
            if cancel:
                dev.tap(cancel.cx, cancel.cy)
                dev.wait_stable(timeout=1.5)
            else:
                # 兜底：只按返回，别多按，按多了会退出迷宫
                dev.back()
                dev.wait_stable(timeout=1.5)
                if not on_gate_page(dev):
                    dev.log.warning("  取消按钮没认出来，用返回键关弹窗后已离开门页")
                    break
        if found_idx is not None:
            return True

        sig = tuple(doors)
        known = sum(1 for k in doors if k)
        dev.log.info("  本轮三扇门：" + " / ".join(_label(k) for k in doors))
        # 至少认出两扇才算数：全认不出来时 sig 全是 None，不能当成「门没变」
        if known >= 2 and prev_sig is not None and sig == prev_sig:
            same_rounds += 1
        else:
            same_rounds = 0
        prev_sig = sig if known >= 2 else None
        if same_rounds >= 2:
            dev.log.warning("  三扇门连续两轮没变，重刷对这个账号没生效，不再空转")
            return False

        where = ("退出迷宫 → 冒险页重进" if floor <= 1
                 else "退回上一层 → 前往下层")
        dev.log.info(f"  第 {attempt}/{rerolls} 次没刷出[{KING_LABEL[target]}]，{where}")
        if not reroll_gate(dev, difficulty, floor):
            dev.log.warning("  没能回到三扇门页，不再重刷")
            return False
        dev.wait_stable(timeout=1.5)
    return False


# ---------------------------------------------------------------- 房间扫描
_TPL_CACHE: dict[str, object] = {}


def _tpl_path(name: str) -> Path:
    """模板文件路径：程序目录优先（用户可能改过），再退回打包内资源。"""
    from core import paths
    for base in (paths.app_dir(), paths.res_dir()):
        p = base / "assets" / "templates" / f"{name}.png"
        if p.exists():
            return p
    return paths.app_dir() / "assets" / "templates" / f"{name}.png"


def _tpl_gray(name: str):
    """读模板灰度图（带缓存）。"""
    if name not in _TPL_CACHE:
        _TPL_CACHE[name] = cv2.imread(str(_tpl_path(name)), 0)
    return _TPL_CACHE[name]


def _in_roi(dev: Device, name: str, roi_box: tuple[int, int, int, int],
            th: float) -> bool:
    """在指定矩形内直接匹配模板（不走 dev.find 的全图回退）。"""
    tpl = _tpl_gray(name)
    if tpl is None:
        return False
    x1, y1, x2, y2 = roi_box
    gray = cv2.cvtColor(dev.screen(), cv2.COLOR_BGR2GRAY) if dev.screen().ndim == 3 else dev.screen()
    roi = gray[max(0, y1):y2, max(0, x1):x2]
    if roi.shape[0] < tpl.shape[0] or roi.shape[1] < tpl.shape[1]:
        return False
    return float(cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED).max()) >= th


def _in_slot(dev: Device, name: str, cx: int, cy: int, th: float) -> bool:
    x1, y1 = _xy(dev, cx - 210, cy - 160)
    x2, y2 = _xy(dev, cx + 210, cy + 160)
    return _in_roi(dev, name, (x1, y1, x2, y2), th)


def scan_rooms(dev: Device) -> dict:
    """实时扫描路线页每个槽位状态。返回 {idx: {'clear':bool,'person':bool,'boss':bool}}。"""
    dev.screen(True)
    result = {}
    for i, (cx, cy) in enumerate(ROOM_SLOTS):
        clear = _in_slot(dev, "maze_clear", cx, cy, 0.8)
        boss = (i == BOSS_SLOT) and _in_slot(dev, "maze_boss_room", cx, cy, 0.75)
        person = False
        if not boss and not clear and dev.matcher.has("maze_person_icon"):
            # 小人图标相对房间中心偏移 (+120,-25)
            ix1, iy1 = _xy(dev, cx + 120 - 45, cy - 25 - 50)
            ix2, iy2 = _xy(dev, cx + 120 + 45, cy - 25 + 50)
            person = _in_roi(dev, "maze_person_icon", (ix1, iy1, ix2, iy2), 0.8)
        result[i] = {"clear": clear, "person": person, "boss": boss}
    dev.log.info("  房间扫描: " + " ".join(
        f"{'B' if result[i]['boss'] else 'P' if result[i]['person'] else '.'}"
        f"{'✓' if result[i]['clear'] else ''}" for i in range(len(ROOM_SLOTS))))
    return result


# ---------------------------------------------------------------- 单房战斗
def _sweep_available(dev: Device) -> bool:
    """扫荡按钮是否可点（亮色）。区域 S均值>80 或高饱和占比>0.4。"""
    x1, y1 = _xy(dev, 1850, 965)
    x2, y2 = _xy(dev, 2280, 1050)
    hsv = cv2.cvtColor(dev.screen()[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    return float(hsv[:, :, 1].mean()) > 80 or float(np.mean(hsv[:, :, 1] > 60)) > 0.4


def _pick_buff(dev: Device, buff_type: str) -> None:
    """加护页：选指定类型的第一个。"""
    g = cv2.cvtColor(dev.screen(), cv2.COLOR_BGR2GRAY)
    tpls = {}
    for k in ("physical", "magic"):
        tpls[k] = cv2.imread(str(_tpl_path(f"maze_buff_{k}")), 0)
    chosen = None
    for cy in (350, 532, 714, 896):
        x1, y1 = _xy(dev, 805, cy - 24)
        x2, y2 = _xy(dev, 845, cy + 16)
        roi = g[y1:y2, x1:x2]
        if roi.shape[0] < 20:
            continue
        scores = {}
        for k, t in tpls.items():
            scores[k] = float(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED).max())
        detected = max(scores, key=scores.get)
        if detected == buff_type and chosen is None:
            chosen = cy
    if chosen is not None:
        _tap(dev, 825, chosen, settle=1.2)
        dev.log.info(f"  加护选[{buff_type}]第一个 y={chosen}")
    else:
        dev.log.warning("  加护没识别出目标类型，点第一个")
        _tap(dev, 825, 350, settle=1.2)
    _tap_until_gone(dev, "maze_jiahu_title", 1280, 1227)


def _pick_partner(dev: Device) -> None:
    """伙伴選択：外框颜色 金>紫>蓝，认不准默认第一个。"""
    hsv = cv2.cvtColor(dev.screen(), cv2.COLOR_BGR2HSV)
    centers = [815, 1167, 1519, 1871]
    cards = []
    for cx in centers:
        x1, y1 = _xy(dev, cx - 40, 444)
        x2, y2 = _xy(dev, cx + 40, 460)
        reg = hsv[y1:y2, x1:x2]
        mk = reg[:, :, 1] > 60
        hs = reg[:, :, 0][mk]
        if len(hs) < 20:
            cards.append("none")
            continue
        hm = float(np.median(hs))
        if 15 <= hm <= 50:
            cards.append("gold")
        elif 120 <= hm <= 170:
            cards.append("purple")
        elif 95 <= hm < 120:
            cards.append("blue")
        elif 40 < hm < 95:
            cards.append("green")
        else:
            cards.append("other")
    pick = 0
    for want in ("gold", "purple", "blue"):
        if want in cards:
            pick = cards.index(want)
            break
    dev.log.info(f"  伙伴外框={cards} → 选 card{pick}")
    _tap(dev, centers[pick], 580, settle=1.3)
    _tap_until_gone(dev, "maze_partner_title", 1279, 1085)


def settle_after_battle(dev: Device, buff_type: str, do_partner: bool = False) -> None:
    """战斗结束后的结算链：礼物→Win→加护→伙伴。全程可中断。"""
    # 礼物
    if _has(dev, "maze_gift_title"):
        _tap_until_gone(dev, "maze_gift_title", 1280, 1253)
    # Win
    if _has(dev, "maze_win"):
        _tap_until_gone(dev, "maze_win", 2205, 1275)
    # 加护（概率出现）
    if _has(dev, "maze_jiahu_title"):
        _pick_buff(dev, buff_type)
    # 伙伴（小人房）
    if do_partner and _has(dev, "maze_partner_title"):
        _pick_partner(dev)


def fight_room(dev: Device, cx: int, cy: int, buff_type: str,
               do_partner: bool = False) -> bool:
    """打一个房间。返回是否成功打完（结算出现）。"""
    dev.check_cancel()
    _tap(dev, cx, cy, settle=2.2)
    # 准备页：扫荡可点就扫荡
    if _sweep_available(dev):
        dev.log.info("  扫荡可点 → 使用扫荡券")
        _tap(dev, 2065, 1007, settle=2.0)
        # 扫荡是瞬间的，可能直接回路线页；等画面稳定
        dev.wait_stable(timeout=3)
        return True
    # 灰色 → 挑战 → 编队挑战
    _tap(dev, 2167, 1321, settle=2.0)
    time.sleep(0.8)
    _tap(dev, 2205, 1067, settle=2.5)
    # 轮询战斗结束（时长不固定，靠结算页判断）
    st, timeout = _wait_states(
        dev, ["maze_gift_title", "maze_win", "maze_jiahu_title", "maze_partner_title"],
        timeout=BATTLE_TIMEOUT)
    if timeout:
        dev.dump("maze_room_battle_timeout")
        dev.log.warning("  房间战斗等了 10 分钟没结束")
        return False
    dev.log.info(f"  战斗结算: {st}")
    settle_after_battle(dev, buff_type, do_partner=do_partner)
    return True


# ---------------------------------------------------------------- 打一层
def play_floor(dev: Device, buff_type: str) -> bool:
    """打完当前层的所有房间。返回是否成功。"""
    for _ in range(20):  # 每层最多 6 房，留余量防死循环
        dev.check_cancel()
        rooms = scan_rooms(dev)
        # 未打完的房
        pending = [i for i, s in rooms.items() if not s["clear"]]
        if not pending:
            dev.log.info("  本层全部房间 CLEAR")
            return True
        # 分类：普通房 / 小人房 / BOSS
        person_rooms = [i for i in pending if rooms[i]["person"]]
        boss_rooms = [i for i in pending if rooms[i]["boss"]]
        normal_rooms = [i for i in pending
                        if i not in person_rooms and i not in boss_rooms]
        # 顺序：普通房 → 小人房 → BOSS
        order = normal_rooms + person_rooms + boss_rooms
        if not order:
            dev.log.warning("  有待打房间但分类为空，重新扫描")
            time.sleep(1)
            continue
        idx = order[0]
        is_person = idx in person_rooms
        is_boss = idx in boss_rooms
        cx, cy = ROOM_SLOTS[idx]
        dev.log.info(f"  打房间 slot{idx} @({cx},{cy})"
                     f"{' [小人房]' if is_person else ''}{' [BOSS]' if is_boss else ''}")
        ok = fight_room(dev, cx, cy, buff_type, do_partner=is_person)
        if not ok:
            return False
    dev.log.warning("  本层打了超过 20 房，可能卡住")
    return False


# ---------------------------------------------------------------- 任务
def _run_skip(dev: Device) -> None:
    """方式=跳过：按录制好的 7 步流程点 SKIP，十几秒结束。"""
    try:
        steps = skip_steps()
    except Exception as e:                       # 文件被删/改坏，给个能看懂的话
        raise NotFound(f"跳过流程读不到（{SKIP_RECIPE}）：{e}")
    if not steps:
        raise NotFound("跳过流程里没有任何步骤")
    dev.log.info(f"  迷宫挑战·跳过：走录制流程 {len(steps)} 步")
    recipe.exec_steps(dev, steps, dev.log)
    dev.log.info("  ✓ 迷宫挑战·跳过 完成")


def job_labyrinth(dev: Device, opts: dict | None = None) -> None:
    opts = opts or {}

    # 方式二选一。老存档里可能两个都勾着（以前是"全部"），不能两个都跑 ——
    # 游戏每天只给一次迷宫，跑错就等于白扔一次，所以勾多了按手动处理并警告。
    modes = [m for m in (opts.get("modes") or ["manual"]) if m in ("skip", "manual")]
    if len(modes) > 1:
        dev.log.warning("  方式同时勾了[跳过]和[手动]，每天只能跑一次 → 按手动处理")
        modes = ["manual"]
    if modes == ["skip"]:
        _run_skip(dev)
        return

    target = (opts.get("kings") or ["qishi"])[0]
    diffs = opts.get("diffs") or ["hard"]
    difficulty = diffs[0] if diffs[0] in ("hard", "normal") else "hard"
    buff_type = (opts.get("buffs") or ["physical"])[0]
    if buff_type not in ("physical", "magic"):
        buff_type = "physical"
    try:
        rerolls = max(1, min(30, int((opts.get("rerolls") or [10])[0])))
    except (ValueError, TypeError, IndexError):
        rerolls = 10
    max_floors = 5 if difficulty == "hard" else 3

    dev.log.info(f"  迷宫挑战·手动：目标[{KING_LABEL.get(target, target)}]，"
                 f"{'Hard(5层)' if difficulty == 'hard' else 'Normal(3层)'}，"
                 f"加护[{buff_type}]，每层重刷上限{rerolls}")

    enter_labyrinth(dev, difficulty)

    for floor in range(1, max_floors + 1):
        dev.check_cancel()
        dev.log.info(f"  ── 第 {floor}/{max_floors} 层 ──")
        if floor > 1:
            # 上一层全清 → 点「前往下层」回三扇门页
            if not _tap_until_gone(dev, "maze_clear", 1274, 1070, tries=3):
                dev.log.warning("  没找到[前往下层]，尝试继续")
            dev.wait_stable(timeout=2)
        # 三扇门页找目标王并进入
        entered = find_target_gate(dev, target, rerolls, difficulty, floor)
        if not entered:
            dev.notify(f"迷宫：第{floor}层重刷{rerolls}次没刷到"
                       f"[{KING_LABEL.get(target, target)}]，跳过（可从当前继续）")
            raise Skipped(f"第{floor}层没刷到[{KING_LABEL.get(target, target)}]")
        # 打本层所有房间
        if not play_floor(dev, buff_type):
            dev.notify(f"迷宫：第{floor}层打房失败，请看一眼现场")
            raise NotFound(f"第{floor}层打房卡住")
        dev.log.info(f"  ✓ 第 {floor} 层完成")

    # 最后一层 BOSS 打完 → 回冒险页 → 点首页
    dev.wait_stable(timeout=3)
    if _has(dev, "tab_adventure", 0.85):
        dev.log.info("  通关，回到冒险页")
        _tap(dev, 373, 1345, settle=2.5)
    if ensure_home(dev):
        dev.log.info("  ✓ 迷宫挑战全部通关，已回主页")
    else:
        dev.log.warning("  没能确认回到主页")
