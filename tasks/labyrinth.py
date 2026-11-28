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
3. 进层后是「路线页」：房间**数量和位置每层都不一样**（3~6 间、卡片大小也跟着变），
   所以房间靠每张卡顶部的宝箱现扫（多尺度），不写死坐标。
   - 房间槽位固定 6 个，但**小人房的数量和位置每层随机**，必须实时扫描
   - CLEAR 标记 = 已打完，跳过
   - 扫到的每间房都有「骷髅」标记 = 这间还没打；小人房打完会弹伙伴选择；
     顺序按 普通房 → 小人房 → 骷髅房（BOSS 通常带骷髅，自然排在后面）
4. 房间从画面上扫（宝箱定位，数量/位置/大小都不固定），点房→挑战准备页（扫荡亮就扫荡，灰就挑战）→编队页挑战→战斗（时长
   不固定，轮询等待）→结算链。**结算页（队伍站在场景里、右下角[关闭]）一定会
   出现，但礼物/Win/加护/伙伴是随机叠加的**，顺序也不固定，所以要一轮轮扫到
   页面干净为止，不能只按固定顺序 if 一遍。
5. 加护页：选指定类型（魔法/物理）的第一个，识别卡片中心图标
6. 伙伴選択（小人房打完）：看卡片外框颜色，金>紫>蓝，认不准默认第一个
7. 本层全 CLEAR→点「前往下层」→回三扇门页→重刷目标王→下一层
8. 最后一层打完 → 没有[前往下层]了 → 回冒险页 → 点首页回主页

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
KING_LABEL["any"] = "任意王"
KING_TPL = {k: f"maze_kind_{k}" for k, _ in KINGS}

TARGET_OPTION = {
    "key": "kings", "label": "目标王", "single": True,
    "show_when": {"key": "modes", "values": ["manual"]},
    "hint": "今天想打哪个王？每次重进迷宫门重新随机(6选3)，没刷到可返回重刷，超过上限跳过。"
            "「任意」= 第一扇门直接进，不重刷，最快通关",
    "choices": [{"v": k, "label": l} for k, l in KINGS] + [{"v": "any", "label": "任意"}],
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
    "maze_clear", "maze_person_icon", "maze_chest", "maze_skull", "maze_gate_arch",
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

# 旧的「固定 6 槽位」坐标，已废弃（房间数量/位置/大小都会变，现在靠 maze_chest 扫）。
# 留着只为老代码/日志对照，别再拿来点。
ROOM_SLOTS = [
    (460, 445), (1280, 445), (1939, 445),   # 上排：左 / 中(BOSS) / 右
    (460, 965), (1280, 965), (1939, 965),   # 下排：左 / 中 / 右
]
BOSS_SLOT = 1  # 上中（已废弃）

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


def _tap_abs(dev: Device, x: float, y: float, settle: float = 1.4) -> None:
    """点「绝对像素」坐标（扫出来的房间位置），不再按 2560 基准换算。"""
    dev.tap(int(x), int(y))
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


def _tap_ok_until_gone(dev: Device, name: str, tries: int = 5,
                       fallback: tuple[int, int] | None = None) -> bool:
    """点弹窗底部的橙色[确定]，直到该弹窗消失。

    弹窗高度是可变的：加护 4 行时确定在 y≈1212，只有 3 行时整体上移约 180，
    伙伴选择又只有 y≈1082。所以位置一律用 maze_btn_ok 模板现找（实测三种弹窗
    都是同一张图，0.999），不写死 y —— 写死就会像 08:11 那次一样，一直点不中
    确定，加护页反复出现。
    """
    for _ in range(tries):
        dev.check_cancel()
        dev.screen(True)
        if not _has(dev, name):
            return True
        m = _find(dev, "maze_btn_ok", 0.9)
        if m is None:
            if fallback is None:
                return False
            _tap(dev, fallback[0], fallback[1], settle=1.5)
            continue
        dev.tap(m.cx, m.cy)
        dev.wait_stable(timeout=2.0)
    return not _has(dev, name)


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
    _wait_page(dev, 20)          # 选完难度有加载过场，等门页真的出来


def on_gate_page(dev: Device) -> bool:
    """是不是「选门」这一页：刚进迷宫的难度按钮页，或三扇门页。

    三扇门页上**没有**难度按钮，原来只用 maze_btn_hard/normal 判断，等于永远
    认不出门页 —— 点完「前往下层」后就被当成「不在门页」，于是退迷宫重进、
    把刚清好的一层白扔。现在补一张门框拱顶模板（门页 3 处命中、路线页/主页 0）。
    """
    if _has(dev, "maze_btn_hard", 0.9) or _has(dev, "maze_btn_normal", 0.9):
        return True
    return bool(_match_all(dev, "maze_gate_arch", 0.80, per=3))


def go_back_up(dev: Device) -> bool:
    """三扇门页左上箭头 → 返回上一层。返回是否成功离开门页。"""
    px, py = _xy(dev, 64, 96)
    dev.tap(px, py)
    dev.wait_stable(timeout=2)
    return not on_gate_page(dev)


def _on_adventure(dev: Device) -> bool:
    """真的在冒险页：[特殊关卡] 按钮在，且没有被「特殊关卡」弹窗盖着。

    这是「迷宫结束了」的唯一判据：打完最后一层的 BOSS，游戏不给「前往下层」、
    也不一定有结算奖励，直接把人丢回冒险页（11-27 09:06、11-28 10:30 两次都是
    这样，都被误判成「打房失败」）。

    别用底部 tab 认（`tab_adventure` 在这个页面只有 0.73，怎么都过不了阈值）；
    `maze_btn_special` 实测 0.999，是目前最稳的锚点。
    """
    return _has(dev, "maze_btn_special", 0.85) and not _has(dev, "maze_btn_entry", 0.85)


def _maze_over(dev: Device) -> bool:
    """迷宫是不是已经结束（人已经在冒险页上）。

    比 `_on_adventure` 宽一点：只要看见[特殊关卡]就算，不管上面有没有压着
    「特殊关卡」弹窗 —— 打房结算时只关心「还在不在迷宫里」。
    """
    return _has(dev, "maze_btn_special", 0.85)


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
    # （保留这句只为对照历史实现；现在直接看有没有宝箱）
    # 路线页的特征就是「一排房间卡」：每张卡顶部都有一个宝箱
    return bool(_match_all(dev, "maze_chest", 0.85, per=3))


def _wait_page(dev: Device, timeout: float = 15.0) -> str:
    """等页面稳定，返回 'gate' / 'route' / 'other'。

    换层、选难度、结算之后都有一段加载过场（黑屏/白屏/动画），这时候两个判断
    都不成立。以前就是在这段过场里被判定成「不在三扇门页」，于是退迷宫重进，
    刚打完的一层白扔 —— 所以要先等，最多等 timeout 秒。
    """
    end = time.time() + timeout
    while time.time() < end:
        dev.check_cancel()
        dev.screen(True)
        if on_gate_page(dev):
            return "gate"
        if is_route_page(dev):
            return "route"
        time.sleep(0.6)
    return "other"


def in_maze(dev: Device) -> bool:
    """是不是已经在迷宫里面（路线页 / 加护 / 结算 / 礼物 / Win / 伙伴）。

    给「续打」用：跑到一半断了、或手动打了一半停在加护页，再点任务就从当前层
    接着打，不回主页重走入口（重进等于从第 1 层重来，白扔一次）。
    """
    dev.screen(True)
    if _on_adventure(dev) or on_gate_page(dev):
        return False
    if is_route_page(dev):
        return True
    for name in ("maze_jiahu_title", "maze_win", "maze_partner_title", "maze_gift_title"):
        if _has(dev, name):
            return True
    return _room_close(dev) is not None


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


def _enter_first_door(dev: Device, rerolls: int, difficulty: str, floor: int) -> bool:
    """目标=任意：挨个门点，弹出确认框就确定，不认王也不重刷。"""
    for attempt in range(1, max(2, rerolls) + 1):
        dev.check_cancel()
        page = _wait_page(dev, 20 if attempt == 1 else 8)
        if page == "route":
            dev.log.info("  已在路线页，视为已进入本层")
            return True
        if page != "gate":
            dev.log.info("  不在三扇门页，重走迷宫入口")
            if not reroll_gate(dev, difficulty, floor):
                dev.log.warning("  回不到三扇门页，先不打了")
                return False
            continue
        for i, gx in enumerate(GATE_X):
            dev.check_cancel()
            _tap(dev, gx, GATE_Y, settle=1.6)
            ok = _find(dev, "maze_btn_ok", 0.9)
            if ok is None:
                dev.log.info(f"  第{i+1}扇门没弹确认框，换下一扇")
                continue
            king = _door_king(dev)
            dev.log.info(f"  第{i+1}扇门=[{_label(king)}]（任意）→ 确定")
            dev.tap(ok.cx, ok.cy)
            dev.wait_stable(timeout=2.5)
            return True
        dev.log.warning("  三扇门都点不开，重刷")
        if not reroll_gate(dev, difficulty, floor):
            return False
    return False


def find_target_gate(dev: Device, target: str, rerolls: int,
                     difficulty: str = "hard", floor: int = 1) -> bool:
    """在当前三扇门页找目标王并点确定进入本层。找不到重刷，超上限返回 False。"""
    if target == "any":
        return _enter_first_door(dev, rerolls, difficulty, floor)
    tpl = KING_TPL[target]
    if not dev.matcher.has(tpl):
        raise NotFound(f"目标[{KING_LABEL[target]}]的模板 {tpl} 不存在")

    prev_sig = None
    same_rounds = 0
    for attempt in range(1, rerolls + 1):
        dev.check_cancel()
        page = _wait_page(dev, 20 if attempt == 1 else 8)
        if page == "route":
            dev.log.info("  已在路线页，视为已进入本层")
            return True
        if page != "gate":
            page = _wait_page(dev, 15)      # 加载慢时再来一次，别一次就退迷宫
            if page == "route":
                dev.log.info("  已在路线页，视为已进入本层")
                return True
        if page != "gate":
            # 等过了还既不在门页也不在路线页（点歪/弹窗没关掉）→ 重走入口
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


ROOM_SCALES = tuple(round(0.85 + 0.05 * i, 2) for i in range(7))   # 0.85 ~ 1.15
# BOSS 房卡片比普通房高一截，且「5 间那层」比「6 间那层」整卡再大 ~20%，
# 所以单独给一套更宽的尺度（实测 6 间那层 1.00 命中 1.000，5 间那层约 1.2）
BOSS_SCALES = tuple(round(0.75 + 0.05 * i, 2) for i in range(11))  # 0.75 ~ 1.25


def _match_all(dev: Device, name: str, th: float, per: int = 6,
               scales: tuple[float, ...] | None = None,
               quiet: bool = True) -> list[tuple[float, float, int, int]]:
    """多尺度找模板的所有命中：[(分数, 缩放, cx, cy)]，按位置去重。

    房间卡片的大小会随本层房间数变（5 间那层比 6 间画得大约 25%），单尺度匹配
    经常整层认不出来（实测 maze_boss_room 在 5 间那层全图最高只有 0.6，
    缩到 0.8 才有 0.78），所以必须逐尺度扫。
    """
    g = cv2.cvtColor(dev.screen(), cv2.COLOR_BGR2GRAY)
    t0 = _tpl_gray(name)
    if t0 is None:
        return []
    hits: list[tuple[float, float, int, int]] = []
    for s in (scales or ROOM_SCALES):
        t = t0 if abs(s - 1.0) < 1e-6 else cv2.resize(
            t0, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        if t.shape[0] > g.shape[0] or t.shape[1] > g.shape[1]:
            continue
        r = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
        rr = r.copy()
        for _ in range(per):
            _, mx, _, ml = cv2.minMaxLoc(rr)
            if mx < th:
                break
            hits.append((float(mx), float(s), ml[0] + t.shape[1] // 2, ml[1] + t.shape[0] // 2))
            cv2.circle(rr, ml, int(max(t.shape) * 0.55), 0, -1)
    hits.sort(reverse=True)
    out: list[tuple[float, float, int, int]] = []
    for h in hits:
        if all(abs(h[2] - o[2]) > 70 or abs(h[3] - o[3]) > 70 for o in out):
            out.append(h)
    return out


def scan_rooms(dev: Device, quiet: bool = False) -> list[dict]:
    """扫出本层所有房间（位置、大小、数量都不固定，靠每张卡顶部的宝箱定位）。

    返回 [{x, y, scale, clear, boss, person}]，x/y 是可以直接点的卡片中段坐标。

    **BOSS 房没有宝箱**：最后一层的「王之间」整张卡就没有宝箱，卡面是红色大 X +
    骷髅 + BOSS 字样。只按宝箱找的话这间永远扫不出来，于是前面 5 间一 CLEAR 就
    当成「本层打完」，BOSS 一次都没打（11-27 08:45 的实战就是这么漏的）。
    所以再按 maze_boss_card 单独补一遍「没宝箱的卡」。
    """
    dev.screen(True)
    chests = _match_all(dev, "maze_chest", 0.85)
    clears = _match_all(dev, "maze_clear", 0.90)
    skulls = _match_all(dev, "maze_skull", 0.80)
    persons = _match_all(dev, "maze_person_icon", 0.90)
    boss_cards = _match_all(dev, "maze_boss_card", 0.75, scales=BOSS_SCALES)
    rooms = []
    for _sc, s, cx, cy in chests:
        px, py = int(cx), int(cy + 150 * s)      # 宝箱往下 150*s 就是卡片中段
        reach = 200 * s

        def near(hits, _px=px, _py=py, _r=reach):
            return any(abs(h[2] - _px) < _r and abs(h[3] - _py) < _r + 90 for h in hits)

        rooms.append({"x": px, "y": py, "scale": s,
                      "clear": near(clears), "boss": near(skulls), "person": near(persons)})
    for _sc, s, cx, cy in boss_cards:
        # 已经按宝箱识别到的卡（同一个位置）不重复加
        if any(abs(r["x"] - cx) < 300 * s and abs(r["y"] - cy) < 300 * s for r in rooms):
            continue
        near_clear = any(abs(h[2] - cx) < 260 * s and abs(h[3] - cy) < 260 * s for h in clears)
        rooms.append({"x": int(cx), "y": int(cy), "scale": s,
                      "clear": near_clear, "boss": True, "person": False})
    if not quiet:
        dev.log.info("  房间扫描(%d 间): " % len(rooms) + " ".join(
            f"({r['x']},{r['y']}){'B' if r['boss'] else 'P' if r['person'] else '.'}"
            f"{'✓' if r['clear'] else ''}" for r in rooms))
    return rooms




# ---------------------------------------------------------------- 单房战斗
# 房间结算页右下角[关闭]（基准 2560×1440）。这个按钮就是召唤结果页那个
# btn_summon_close —— 07:56:38 的日志里 ensure_home 正好在这个页面命中它 @(2210,1271)。
ROOM_CLOSE_ZONE = (1960, 1150, 580, 280)
ROOM_TITLES = ("maze_gift_title", "maze_win", "maze_jiahu_title", "maze_partner_title")


def _sweep_available(dev: Device) -> bool:
    """扫荡按钮是否可点（亮橙）。

    旧判据只看饱和度均值 —— 深红/火光的背景全都过 80（实测加护页 S=108、
    高饱和占比 0.69），于是把结算页/加护页当成准备页，连点 20 次扫荡坐标
    （07:55 那次就是这么卡住的）。现在：
      1. 结算页/加护页/礼物/Win 直接排除（那些页面上的按钮不是扫荡）
      2. 优先看「又亮又饱和」的橙色像素占比（同页实测 0.007 → 0）
      3. 老判据留作兜底（扫荡券偏暗时），但已排除上面那些页面
    """
    if _room_close(dev) is not None:
        return False
    for name in ROOM_TITLES:
        if _has(dev, name):
            return False
    x1, y1 = _xy(dev, 1850, 965)
    x2, y2 = _xy(dev, 2280, 1050)
    hsv = cv2.cvtColor(dev.screen()[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    if float(((s > 90) & (v > 150)).mean()) > 0.15:
        return True
    return float(s.mean()) > 80 or float(np.mean(s > 60)) > 0.4


def _room_close(dev: Device):
    """在右下角找房间结算页的[关闭]。返回 (分数, x, y) 或 None。

    只在这一小块里找，避免跟别的橙色按钮（扫荡/挑战/确定）撞。用 cv2 直接匹配，
    不走 dev.find —— 它带全图回退，会跑到别的角落乱点。
    """
    x, y, w, h = ROOM_CLOSE_ZONE
    x1, y1 = _xy(dev, x, y)
    x2, y2 = _xy(dev, x + w, y + h)
    gray = cv2.cvtColor(dev.screen(), cv2.COLOR_BGR2GRAY)
    roi = gray[max(0, y1):y2, max(0, x1):x2]
    for name, th in (("btn_summon_close", 0.90), ("yice_close_word", 0.82)):
        if not dev.matcher.has(name):
            continue
        tpl = _tpl_gray(name)
        if tpl is None:
            continue
        if roi.shape[0] < tpl.shape[0] or roi.shape[1] < tpl.shape[1]:
            continue
        res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score >= th:
            th_, tw_ = tpl.shape[:2]
            return score, x1 + loc[0] + tw_ // 2, y1 + loc[1] + th_ // 2
    return None


def close_room_result(dev: Device, rounds: int = 2) -> bool:
    """点掉房间结算页的[关闭]。

    **不是每个房间都有礼物/Win 结算奖励**，但打完一定停在这个页面（队伍站场景里、
    右下角一个[关闭]）。不点掉它，下一轮扫描就全是「未 CLEAR」，会对着同一间房
    反复点（07:55 那次连点 20 次就是没关这个页）。
    """
    closed = False
    for _ in range(max(1, rounds)):
        dev.check_cancel()
        dev.screen(True)
        hit = _room_close(dev)
        if hit is None:
            break
        score, cx, cy = hit
        dev.log.info(f"  关掉房间结算 @({cx},{cy}) {score:.2f}")
        dev.tap(cx, cy)
        dev.wait_stable(timeout=2.0)
        closed = True
    return closed


def _wait_room_done(dev: Device, timeout: float, ignore_close_for: float = 0.0) -> str | None:
    """等房间打完：礼物 / Win / 加护 / 伙伴 / 只有一个[关闭]结算页，谁先来算谁。

    ignore_close_for：刚进战斗的前几秒不认「只有[关闭]」这一条 —— 战斗界面右下角
    万一也有个橙色按钮，别被它当成打完了（礼物/Win/加护这些信号照常认）。
    """
    t0 = time.time()
    end = t0 + timeout
    last_route_check = 0.0
    while time.time() < end:
        dev.check_cancel()
        dev.screen(True)
        for name in ROOM_TITLES:
            if _has(dev, name):
                return name
        if time.time() - t0 >= ignore_close_for and _room_close(dev) is not None:
            return "maze_result"
        # 已经回到路线页（有房间卡）＝这间打完了，只是结算页被跳过/没认出来。
        # 没有这条时，窗口最小化/画面卡一下就会白等满 10 分钟（08:33 那次就是这样）。
        # 前 3 秒不认：刚点完房间/挑战，路线页可能还没被准备页盖住。
        if time.time() - t0 >= max(ignore_close_for, 3.0) and time.time() - last_route_check >= 2.0:
            last_route_check = time.time()
            if _maze_over(dev):
                # 最终 BOSS 打完：没有结算奖励、没有 CLEAR，直接回冒险页
                return "adventure"
            if _match_all(dev, "maze_chest", 0.85, per=2):
                return "route"
        time.sleep(POLL)
    return None


BUFF_ICON_X = (805, 845)        # 加护弹窗左侧图标列的 x 范围（2560×1440 基准）
BUFF_ROW_Y = (346, 528, 710, 892)   # 4 行加护的图标中心（实测行距 182）


def _buff_rows(dev: Device) -> list[tuple[str, int, float]]:
    """认出加护弹窗里有哪些行、每行是哪个类型：[(kind, y, P盾分数)]。

    行位置用实测的 4 行格，每格再校验「这里真有图标」（图标列上高饱和像素够多），
    所以只有 3 行时也不会把空白当选项。

    类型怎么分：原来那张 maze_buff_physical 模板是坏的（全图最高只有 0.56，纯噪声），
    真正能认的是 P 盾：命中 = magic(免疫/抗性类)，不命中 = physical(爆击/攻击类)。
    攻击类图标每期都不一样（白剑、火剑…），单张模板认不了，只能这样反着判。
    """
    img = dev.screen()
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    x1, _ = _xy(dev, BUFF_ICON_X[0], 0)
    x2, _ = _xy(dev, BUFF_ICON_X[1], 0)
    tpl = _tpl_gray("maze_buff_magic")
    out = []
    for cy_ref in BUFF_ROW_Y:
        _, cy = _xy(dev, 0, cy_ref)
        roi = g[max(0, cy - 20):cy + 20, x1:x2]
        sat = hsv[max(0, cy - 20):cy + 20, x1:x2, 1]
        if roi.shape[0] < 20 or roi.shape[1] < 20:
            continue
        if float((sat > 90).mean()) < 0.15:        # 这一行没有图标（不足 3 行时）
            continue
        p = 0.0
        if tpl is not None and roi.shape[0] >= tpl.shape[0] and roi.shape[1] >= tpl.shape[1]:
            p = float(cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED).max())
        out.append(("magic" if p >= 0.90 else "physical", cy, round(p, 2)))
    return out


def _pick_buff(dev: Device, buff_type: str) -> None:
    """加护页：按行认类型，选指定类型的第一个。"""
    rows = _buff_rows(dev)
    if rows:
        dev.log.info("  加护行: " + " / ".join(f"y{cy}:{k}" for k, cy, _ in rows))
    pick = next((r for r in rows if r[0] == buff_type), None)
    if pick is None:
        if rows:
            pick = rows[0]
            dev.log.info(f"  这一轮没有[{buff_type}]类，选第一个 y={pick[1]}")
        else:
            pick = (buff_type, 346, 0.0)
            dev.log.warning("  加护页没认出选项，按第一行的位置点")
    _tap(dev, 825, pick[1], settle=1.2)
    dev.log.info(f"  加护选[{pick[0]}] y={pick[1]}")
    _tap_ok_until_gone(dev, "maze_jiahu_title")


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
    _tap_ok_until_gone(dev, "maze_partner_title", fallback=(1279, 1085))


def settle_after_battle(dev: Device, buff_type: str, do_partner: bool = False) -> bool:
    """打完一间房的结算链，顺序不固定、也不是每个都有：

        结算页[关闭] → 礼物 → Win → 加护 → 伙伴 → 再回到结算页…

    所以不写成固定的 if 顺序，而是一轮轮扫，扫到页面上什么都不剩为止。
    """
    for _ in range(8):
        dev.check_cancel()
        dev.screen(True)
        # 先认**能区分**的页：maze_jiahu_title 原来是「通用绿标题栏」的左半截，
        # 伙伴页也能 0.998 命中，于是伙伴页被当成加护页，一直点空。
        # 现在 jiahu 模板换成了标题文字区（加护页 1.000 / 伙伴页 0.589），伙伴先判更稳
        if _has(dev, "maze_win"):
            _tap_until_gone(dev, "maze_win", 2205, 1275, tries=3)
            continue
        if _has(dev, "maze_partner_title"):
            # 伙伴只会出现在小人房，但认到就选，不然页面关不掉
            _pick_partner(dev)
            continue
        if _has(dev, "maze_jiahu_title"):
            _pick_buff(dev, buff_type)
            continue
        if _has(dev, "maze_gift_title"):
            _tap_ok_until_gone(dev, "maze_gift_title", tries=3, fallback=(1280, 1253))
            continue
        if close_room_result(dev, rounds=1):
            continue
        return True
    dev.log.warning("  结算链转了几轮还没干净，先继续下一步")
    return False


def fight_room(dev: Device, cx: int, cy: int, buff_type: str,
               do_partner: bool = False) -> bool:
    """打一个房间（cx,cy 是扫出来的绝对像素）。返回是否成功打完。"""
    dev.check_cancel()
    _tap_abs(dev, cx, cy, settle=2.2)
    # 准备页：扫荡可点就扫荡（扫荡也会出加护/结算页）
    if _sweep_available(dev):
        dev.log.info("  扫荡可点 → 使用扫荡券")
        _tap(dev, 2065, 1007, settle=1.8)
        if _wait_room_done(dev, timeout=8.0) is not None:
            settle_after_battle(dev, buff_type, do_partner=do_partner)
            return True
        dev.log.warning("  点了扫荡没反应，改走挑战")
    # 灰色 → 挑战 → 编队挑战
    _tap(dev, 2167, 1321, settle=2.0)
    time.sleep(0.8)
    _tap(dev, 2205, 1067, settle=2.5)
    # 轮询战斗结束（时长不固定，靠结算页判断）
    st = _wait_room_done(dev, BATTLE_TIMEOUT, ignore_close_for=5.0)
    if st is None:
        dev.dump("maze_room_battle_timeout")
        dev.log.warning("  房间战斗等了 10 分钟没结束")
        return False
    dev.log.info(f"  战斗结算: {st}")
    settle_after_battle(dev, buff_type, do_partner=do_partner)
    return True


def _confirm_clear(dev: Device, x: int, y: int, timeout: float = 12.0) -> bool:
    """等这间房变成 CLEAR（关掉结算页后回路线页有转场，给几秒）。

    房间位置是扫出来的，所以按「离点击点最近的那间」判断，不再按固定槽位。
    """
    end = time.time() + timeout
    while time.time() < end:
        dev.check_cancel()
        dev.screen(True)
        close_room_result(dev, rounds=1)      # 结算页还没关就顺手关掉
        if _maze_over(dev):                   # 最后一层 BOSS 打完 → 直接被丢回冒险页
            dev.log.info("  已经回到冒险页（迷宫结束），不用等 CLEAR 了")
            return True
        rooms = scan_rooms(dev, quiet=True)
        near = [r for r in rooms if abs(r["x"] - x) < 130 and abs(r["y"] - y) < 130]
        if near and near[0]["clear"]:
            return True
        time.sleep(0.6)
    return False


# ---------------------------------------------------------------- 打一层
def play_floor(dev: Device, buff_type: str) -> bool:
    """打完当前层的所有房间。返回是否成功。

    房间是从画面上扫出来的（数量、位置、大小都不固定），所以不用槽位下标，
    直接拿扫到的坐标点卡片中间。
    """
    tried: set[tuple[int, int]] = set()      # 打完也没 CLEAR 的房间，不再反复点
    for _ in range(24):                      # 每层最多 6 房，留余量防死循环
        dev.check_cancel()
        settle_after_battle(dev, buff_type)   # 上一步的结算页/加护没关干净先关掉
        dev.wait_stable(timeout=1.5)
        rooms = scan_rooms(dev)
        if not rooms:
            # 关掉结算页之后有转场，偶尔会有一两秒什么都认不出来 → 先重试再判失败。
            for _w in range(3):
                time.sleep(1.8)
                dev.screen(True)
                if _maze_over(dev):
                    break
                rooms = scan_rooms(dev, quiet=True)
                if rooms:
                    break
        if not rooms:
            # 打完最后一层的 BOSS，游戏会直接结束迷宫把人丢回冒险页（没有「前往下层」，
            # 也不一定有结算奖励，更没有 CLEAR 可以等）。以前这里当成「认不出房间 → 失败」，
            # 于是明明已经通关却报一次失败（11-27 09:06、11-28 10:30 两次都是）。
            if _maze_over(dev):
                dev.log.info("  已经回到冒险页 —— 最后一层 BOSS 打完，迷宫结束")
                return True
            dev.dump("maze_no_room")
            dev.log.warning("  路线页上没认出任何房间（模板/缩放对不上？）")
            return False
        pending = [r for r in rooms if not r["clear"]]
        if not pending:
            dev.log.info("  本层全部房间 CLEAR")
            return True
        # 分类：普通房 / 小人房 / 带骷髅的（＝还没打的，BOSS 一般也在里面）
        person = [r for r in pending if r["person"]]
        boss = [r for r in pending if r["boss"]]
        normal = [r for r in pending
                  if (r["x"], r["y"]) not in {(p["x"], p["y"]) for p in person}
                  and (r["x"], r["y"]) not in {(b["x"], b["y"]) for b in boss}]
        # 顺序：普通房 → 小人房 → 骷髅房（BOSS 最后）
        order = [r for r in normal + person + boss if (r["x"], r["y"]) not in tried]
        if not order:
            dev.dump("maze_floor_stuck")
            dev.log.warning(f"  {len(pending)} 间打了也不 CLEAR，先停手（现场在 logs/fails）")
            return False
        r = order[0]
        dev.log.info(f"  打房间 @({r['x']},{r['y']})"
                     f"{' [小人房]' if r['person'] else ''}"
                     f"{' [骷髅]' if r['boss'] else ''}")
        if not fight_room(dev, r["x"], r["y"], buff_type, do_partner=bool(r["person"])):
            return False
        if not _confirm_clear(dev, r["x"], r["y"]):
            dev.log.warning("  这间打完还是没 CLEAR，先换下一间")
            tried.add((r["x"], r["y"]))
    dev.log.warning("  本层打了超过 24 房，可能卡住")
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
    if target != "any" and target not in KING_TPL:
        target = "qishi"
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

    # 续打：上次跑到一半断了、或手动打了一半停在加护/结算/门页，再点任务不该回主页
    # 重走入口 —— 重进虽然进度还在，但每次都要重走四步选难度，纯浪费时间。
    page0 = _wait_page(dev, 2.5)
    if page0 == "gate":
        start = "gate"
        dev.log.info("  已经站在三扇门页，直接开始选门")
    elif in_maze(dev):
        start = "floor"
        dev.log.info("  已经在大迷宫里面，从当前层接着打（不回主页重进）")
    else:
        start = "home"
        enter_labyrinth(dev, difficulty)

    # 从门页续打时不知道现在第几层，多留一轮（没「前往下层」时循环自己会 break）
    for floor in range(1, max_floors + (1 if start == "gate" else 0) + 1):
        dev.check_cancel()
        run_floor = floor
        dev.log.info(f"  ── 第 {floor}/{max_floors} 层"
                     f"{'（续打）' if start == 'floor' and floor == 1 else ''} ──")
        same_floor = start == "floor" and floor == 1
        if floor > 1:
            # 上一层全清 → **手动**点「前往下层」回三扇门页（游戏不会自动进层），
            # 点完有加载过场，等门页真的出来再继续
            if _tap_until_gone(dev, "maze_clear", 1274, 1070, tries=3):
                _wait_page(dev, 25)
            elif _wait_page(dev, 20) == "route":
                # 「没有前往下层」≠ 迷宫通关：BOSS 房没打同样不会出现这个按钮。
                # 11-27 08:45 就是这么在 5F 把王之间整间漏掉、直接回主页的 ——
                # 先确认本层真的没有未 CLEAR 的房间，再放行。
                left = [r for r in scan_rooms(dev, quiet=True) if not r["clear"]]
                if left:
                    dev.log.warning(f"  没有[前往下层]，但本层还剩 {len(left)} 间没 CLEAR，"
                                    f"先补打（不退出迷宫）")
                    same_floor = True
                else:
                    dev.log.info("  没有[前往下层]了，迷宫已经到头")
                    break
            else:
                dev.log.warning("  没找到[前往下层]，继续往下走")
        if same_floor:
            # 已经在本层里面了，别去找三扇门（找不到会把进度退掉）
            dev.log.info("  续打：跳过选门，直接打当前层")
        else:
            entered = find_target_gate(dev, target, rerolls, difficulty, run_floor)
            if not entered:
                dev.notify(f"迷宫：第{floor}层重刷{rerolls}次没刷到"
                           f"[{KING_LABEL.get(target, target)}]，跳过（可从当前继续）")
                raise Skipped(f"第{floor}层没刷到[{KING_LABEL.get(target, target)}]")
        # 打本层所有房间
        if not play_floor(dev, buff_type):
            dev.notify(f"迷宫：第{floor}层打房失败，请看一眼现场")
            raise NotFound(f"第{floor}层打房卡住")
        dev.log.info(f"  ✓ 第 {floor} 层完成")
        # 迷宫结束（最后一层 BOSS 打完 / 已经把我们丢回冒险页）：
        # 千万别再去找「前往下层」—— 那会对着冒险页上 (1274,1070) 的关卡图标乱点。
        if _on_adventure(dev):
            dev.log.info("  迷宫已结束（回到冒险页），不再找下一层")
            break

    # 最后一层打完（没有[前往下层]）→ 回冒险页 → 点首页
    dev.wait_stable(timeout=3)
    if _maze_over(dev):
        dev.log.info("  通关，回到冒险页")
        _tap(dev, 373, 1345, settle=2.5)
    if ensure_home(dev):
        dev.log.info("  ✓ 迷宫挑战全部通关，已回主页")
    else:
        dev.log.warning("  没能确认回到主页")
