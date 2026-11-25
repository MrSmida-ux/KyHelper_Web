"""主线冒险：主页 → 冒险 → 主线故事 → 选难度/章节 → 关卡 → 扫荡券优先 → 回主页。

实测要点（2026-11-16 真机记录）：
- 冒险页点[主线故事 MAIN STORY]进主线。右上 Normal(蓝)/Hard(红) 切换难度。
- 左上章节标签点开是「选择章节」下拉框，纵向列表 + 滚动，每行是「第N章(Hard)」。
  列表顺序固定、行间距固定 112px(基准)，用每章行模板定位；未来新章没有模板时，
  用 ALL 星标锚点 + 已知章当「里程标」按行距推算，所以列表变长也兼容。
- 章节节点地图：所有 Hard 章都是 3 个节节点(第1/2/3节)，坐标固定；点节点弹关卡列表。
  Normal 只打 1-1-1。
- 关卡列表：每节 3 关(如 16-3-1/2/3)，右侧 GET 图标 find_all 定位第几关，点卡片进详情。
- 详情页和遗迹同构：[使用1个] → 确认框「AP×10 与 扫荡券×1」→ 礼物结算。
  扫荡次数是全局上限，扫荡/挑战都吃 AP×10，体力不足跳过。
"""
from __future__ import annotations

import re
import time

from core.device import Device, NotFound, Skipped
from core.flows import cancel_ap_refill, ensure_home, is_home
from .sweep import set_count, use_point

REF_W = 2560
# Hard 章节节点地图的三个节节点中心（基准 2560×1440）
SECTION_POINTS = {1: (521, 848), 2: (1292, 813), 3: (2169, 936)}
# Normal 节点布局不同（11 个节点：第1节/第2节/第3·4节…），只打第1节
NORMAL_SECTION_1 = (420, 870)
# 章节下拉列表行间距（基准像素）
ROW_STEP = 112
MAX_KNOWN_CHAPTER = 16  # 已采模板的最大章号；更大的章用里程标推算


# ---------------------------------------------------------------- 选项数据
DIFFS = [("normal", "Normal"), ("hard", "Hard")]
ALL_DIFFS = [v for v, _ in DIFFS]
DIFF_LABEL = {v: l for v, l in DIFFS}

DIFF_OPTION = {
    "key": "diffs", "label": "难度",
    "hint": "Normal 只打 1-1-1；Hard 按下面填的章节打",
    "choices": [{"v": v, "label": l} for v, l in DIFFS],
    "default": ["hard"],
}
CHAPTER_OPTION = {
    "key": "chapter", "label": "章节", "type": "text",
    "default": ["1-1-1"],
    "hint": "Hard 用：章-节-关，如 16-3-3（章1~99、节1~3、关1~3）",
}
SWEEP_OPTION = {
    "key": "sweeps", "label": "扫荡次数", "type": "number",
    "min": 1, "max": 99, "default": [5],
    "hint": "本次最多打几次（扫荡和挑战都算）。每次 AP×10，体力有限设小点",
}


# ---------------------------------------------------------------- 工具
def _scale(dev: Device, x: int) -> int:
    return int(x * dev.screen().shape[1] / REF_W)


def _wait_exists(dev: Device, tpl: str, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        dev.check_cancel()
        if dev.exists(tpl, 0.82):
            return True
        time.sleep(0.3)
    return False


def _picked(opts: dict, key: str, allowed: list[str]) -> list[str]:
    vals = opts.get(key)
    if not isinstance(vals, list) or not vals:
        return list(allowed)
    return [str(v) for v in vals if v in allowed]


def _count(opts: dict, key: str, default: int, lo: int = 1, hi: int = 99) -> int:
    vals = opts.get(key)
    try:
        n = int(vals[0]) if isinstance(vals, list) and vals else default
    except (ValueError, TypeError):
        n = default
    return max(lo, min(hi, n))


def parse_chapter(text: str) -> tuple[int, int, int]:
    """解析 16-3-3 → (16,3,3)。容错：全角横杠、空格、多段。"""
    s = str(text).replace("－", "-").replace("—", "-").replace("–", "-")
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*-\s*(\d+)", s)
    if not m:
        return (1, 1, 1)
    ch, sec, lv = (int(x) for x in m.groups())
    return (max(1, min(999, ch)), max(1, min(3, sec)), max(1, min(3, lv)))


# ---------------------------------------------------------------- 导航
def enter_story_page(dev: Device) -> bool:
    """主页 → 冒险 → 主线故事页。"""
    if not ensure_home(dev):
        return False
    if not dev.tap_img("tab_adventure", timeout=10):
        raise NotFound("底部导航没找到[冒险]")
    dev.wait_stable(timeout=6)
    for attempt in range(3):
        if dev.tap_img("btn_mainstory_entry", timeout=4):
            dev.wait_stable(timeout=4)
            if dev.exists("title_mainstory", 0.82):
                return True
        dev.log.info(f"  没进主线页，清弹窗重试（第 {attempt + 1} 次）")
        dev.back()
        dev.wait_stable(timeout=3)
    return dev.exists("title_mainstory", 0.82)


def set_difficulty(dev: Device, diff: str) -> bool:
    """切到 Normal / Hard，返回是否切换成功。"""
    tpl = "btn_diff_normal" if diff == "normal" else "btn_diff_hard"
    if not dev.tap_img(tpl, timeout=5):
        dev.log.warning(f"  没找到难度按钮[{DIFF_LABEL[diff]}]")
        return False
    dev.wait_stable(timeout=2.5)
    return True


# ---------------------------------------------------------------- 章节选择
def _row_hits(dev: Device) -> list:
    """当前下拉视口里所有章节行的命中（按 y 排序）。ALL 星标每行一个，精确可靠。"""
    return sorted(dev.find_all("chapter_all_star", threshold=0.9) or [], key=lambda m: m.cy)


def _scroll_chapter_list(dev: Device, direction: str) -> None:
    """在下拉框里滚动。direction=up 看后面的章，down 回到前面的章。"""
    if direction == "up":
        dev.swipe(_scale(dev, 1280), _scale(dev, 850),
                  _scale(dev, 1280), _scale(dev, 450), 380)
    else:
        dev.swipe(_scale(dev, 1280), _scale(dev, 450),
                  _scale(dev, 1280), _scale(dev, 850), 380)
    time.sleep(0.7)


def already_on_chapter(dev: Device, ch: int, diff: str) -> bool:
    """当前节点地图页顶部标签是否已经是目标章节。

    顶部标签和下拉框行用的是同一套 chapter_NN_hard 模板（都是「第N章(Hard)」样式），
    在节点地图页匹配分 1.000、相邻章 ≤0.94。已在目标章就直接跳过整个下拉流程 ——
    连打同一关时省掉「开下拉→滚屏→选行→确定」十几秒。
    Normal 只打第1章，专用 chapter_01_normal 标签模板（无 Hard 后缀）。
    """
    if diff == "normal":
        if ch != 1:
            return False
        return dev.exists("chapter_01_normal", 0.985)
    tpl = f"chapter_{ch:02d}_hard"
    if not dev.matcher.has(tpl):
        return False
    return dev.exists(tpl, 0.985)


def select_chapter(dev: Device, ch: int, diff: str) -> bool:
    """点开章节下拉框，选中第 ch 章并确定。

    先用 already_on_chapter 判断当前是否已在目标章 —— 是就直接返回，不再开下拉框。
    否则开下拉：已知章 1~16 用专用行模板严格匹配；未来新章用 ALL 星标行 +
    已知章当里程标、按固定行距推算，列表变长也兼容。
    """
    # 已经在目标章节：直接复用当前界面，省掉整个下拉选择流程
    if already_on_chapter(dev, ch, diff):
        dev.log.info(f"  当前已在第{ch}章，跳过选章")
        return True

    # 标签模板按难度选：Hard 标签带「(Hard)」，Normal 标签没有
    label_tpl = "label_chapter" if diff == "hard" else "label_chapter_normal"
    if not dev.tap_img(label_tpl, timeout=5):
        dev.log.warning(f"  没找到章节标签[{label_tpl}]，无法选章")
        return False
    dev.wait_stable(timeout=1.2)

    # Normal 只打 1-1-1：第1章就是列表第一行，滚到顶点 ALL 星标最上面那行即可，
    # 不依赖章号模板（Normal 行文字没有难度字样，模板和 Hard 不通用）。
    if diff == "normal" and ch == 1:
        for _ in range(6):
            _scroll_chapter_list(dev, "down")
        rows = _row_hits(dev)
        if not rows:
            dev.log.warning("  Normal 下拉框里没找到章节行")
            dev.back()
            return False
        row = rows[0]
        dev.log.info(f"  Normal 选第1章（列表首行 y={row.cy}）")
        dev.tap(row.cx, row.cy)
        dev.wait_stable(timeout=1.0)
        if not dev.tap_img("btn_chapter_confirm", timeout=4):
            dev.tap(_scale(dev, 1470), _scale(dev, 1065))
        dev.wait_stable(timeout=2.5)
        return True

    # 章号模板 + 严格阈值：真章 1.000、相邻章 ≤0.98，用 0.985 区分。
    # 已知章(1~16)用专用模板；未来新章(>16)用「最后一行=最大章」+ ALL星标行距推算。
    tpl = f"chapter_row_{ch:02d}_hard"
    known = dev.matcher.has(tpl) and ch <= MAX_KNOWN_CHAPTER
    STRICT = 0.985

    def find_target():
        if known:
            return dev.find(tpl, threshold=STRICT)
        return None

    # 先滚到顶，再逐屏上滚找目标章
    for _ in range(6):
        _scroll_chapter_list(dev, "down")
    dev.wait_stable(timeout=0.4)

    hit = None
    for _ in range(12):
        dev.check_cancel()
        hit = find_target()
        if hit is not None:
            break
        if not known:
            # 未知章：滚到底，用「最后一行 = 当前最大章」+ 行距推算
            break
        _scroll_chapter_list(dev, "up")
        time.sleep(0.3)

    if hit is None and not known:
        # 未来章兜底：滚到底，最后一行是最大已解锁章，按差值和行距往上数
        for _ in range(12):
            _scroll_chapter_list(dev, "up")
        rows = _row_hits(dev)
        if rows:
            # 找到最后一个已知章行当锚，推算目标
            last_known = min(MAX_KNOWN_CHAPTER, ch)
            anchor = dev.find(f"chapter_row_{last_known:02d}_hard", threshold=STRICT)
            if anchor:
                step = _scale(dev, ROW_STEP)
                want_y = anchor.cy + (ch - last_known) * step
                near = min(rows, key=lambda m: abs(m.cy - want_y))
                if abs(near.cy - want_y) <= step * 0.6:
                    hit = near

    if hit is None:
        dev.log.warning(f"  下拉框里没定位到第{ch}章（可能超出已解锁范围）")
        dev.back()
        return False

    dev.log.info(f"  点第{ch}章行 y={hit.cy}")
    dev.tap(hit.cx, hit.cy)
    dev.wait_stable(timeout=1.0)
    if not dev.tap_img("btn_chapter_confirm", timeout=4):
        dev.tap(_scale(dev, 1470), _scale(dev, 1065))
    dev.wait_stable(timeout=2.5)
    return True


def pick_level(dev: Device, sec: int, lv: int, diff: str = "hard", chapter: tuple = (1, 1, 1)) -> str:
    """在章节节点地图点第 sec 节 → 关卡列表点指定编号的关卡。

    返回 detail（进到详情页）/ cutscene（点了剧情关，等 SKIP）/ fail。
    按「编号字样」定位（不靠位置假设）：Normal 的 1-1-1 用 level_1_1_1 编号模板
    精确匹配；Hard 用 GET 图标 find_all，列表顺序即编号顺序（16-3-1/2/3 已实测）。
    列表可滚动，编号不在视口时滚动查找。
    """
    if diff == "normal":
        x, y = NORMAL_SECTION_1
    else:
        x, y = SECTION_POINTS.get(sec, SECTION_POINTS[1])
    dev.tap(_scale(dev, x), _scale(dev, y))
    dev.wait_stable(timeout=2.5)

    # Normal 1-1-1：认编号字样（列表可滚动，要滚到顶才看得到）
    if diff == "normal" and chapter[:2] == (1, 1) and lv == 1:
        tpl = "level_1_1_1"
        if dev.matcher.has(tpl):
            hit = None
            # 滚到列表顶（面板中央 x=1800 才在滑动区内；x=1280 是边界无效）
            for _ in range(3):
                dev.swipe(_scale(dev, 1800), _scale(dev, 300),
                          _scale(dev, 1800), _scale(dev, 700), 500)
                time.sleep(0.6)
                hit = dev.find(tpl, threshold=0.92)
                if hit is not None:
                    break
            if hit is not None:
                dev.log.info(f"  认出编号 1-1-1 y={hit.cy}")
                dev.tap(hit.cx + _scale(dev, 400), hit.cy + _scale(dev, 25))  # 点该行卡片
                dev.wait_stable(timeout=3)
                if dev.exists("btn_story_teams", 0.82):
                    return "detail"
                # 若进了剧情演出（极少），交给 SKIP 处理
                if dev.exists("btn_story_menu", 0.82):
                    return "cutscene"
                return "fail"

    # 通用：GET 图标 find_all，按 y 排序（列表顺序 = 编号顺序）
    gets = dev.find_all("card_story_get", threshold=0.9) or []
    gets = sorted(gets, key=lambda m: m.cy)
    idx = lv - 1
    # 编号不在视口则往上滚一屏再找
    if len(gets) <= idx:
        for _ in range(3):
            dev.swipe(_scale(dev, 1800), _scale(dev, 700),
                      _scale(dev, 1800), _scale(dev, 300), 500)
            time.sleep(0.6)
            gets = sorted(dev.find_all("card_story_get", threshold=0.9) or [], key=lambda m: m.cy)
            if len(gets) > idx:
                break
    if len(gets) <= idx:
        dev.log.warning(f"  关卡列表只找到 {len(gets)} 关，要第 {lv} 关")
        return "fail"
    dev.tap(gets[idx].cx, gets[idx].cy)
    dev.wait_stable(timeout=3)
    if dev.exists("btn_story_teams", 0.82):
        return "detail"
    if dev.exists("btn_story_menu", 0.82):
        return "cutscene"
    return "fail"


def skip_cutscene(dev: Device) -> bool:
    """剧情关：点右上 MENU → SKIP → 确认「是」。剧情跳过直接回主页。"""
    if not dev.tap_img("btn_story_menu", timeout=6):
        time.sleep(1.5)
        if not dev.tap_img("btn_story_menu", timeout=6):
            dev.log.warning("  剧情画面没找到 MENU")
            return False
    dev.wait_stable(timeout=1.0)
    if not dev.tap_img("btn_story_skip", timeout=4):
        dev.log.warning("  MENU 里没找到 SKIP")
        return False
    dev.wait_stable(timeout=1.0)
    if not dev.exists("dlg_story_skip", 0.82) and not _wait_exists(dev, "dlg_story_skip", 3):
        dev.log.warning("  没弹出 SKIP 确认框")
        return False
    if not dev.tap_img("btn_story_skip_yes", timeout=4):
        dev.tap(_scale(dev, 1489), _scale(dev, 1074))
    dev.wait_stable(timeout=3)
    return True


# ---------------------------------------------------------------- 详情页
# 扫荡数量调节条的实现放在 tasks/sweep.py（遗迹和主线共用一份）：
# 热区偏上、连点被吞、数字刷新滞后、**数量会被记住**这四个坑都在那里处理。
# 「+ 和 − 外观相似、模板互匹配 0.87」也在那边靠回读数字解决。
def sweep_batch(dev: Device, count: int):
    """在详情页一次性批量扫荡最多 count 次（扫荡券）。

    返回实际扫荡次数(int，可能因体力不足小于 count) / "fallback"（券不可用，改走挑战）
    / 抛 Skipped（扫荡后没结算页）。
    """
    actual = set_count(dev, count)
    n = 1 if actual <= 0 else actual
    if actual <= 0:
        dev.log.info("  数量调节不可用（体力大概只够 1 次），先按单次扫荡")
    elif n < count:
        dev.log.info(f"  数量只调到 {n} 次（体力不够更多），就按 {n} 次扫")
    else:
        dev.log.info(f"  数量已调到 {n} 次")
    dev.tap(*use_point(dev))
    if not (dev.exists("dlg_sweep_confirm", 0.82) or _wait_exists(dev, "dlg_sweep_confirm", 3)):
        # AP 连一次都不够时会弹「AP回复」购买框 —— 取消，绝不买晶石
        if cancel_ap_refill(dev):
            dev.notify("体力不足，已取消晶石购买（未扣晶石），主线任务跳过")
            raise Skipped("体力不足，已取消购买 AP")
        return "fallback"
    if not dev.tap_img("btn_sweep_ok", timeout=4):
        return "fallback"
    if not _wait_exists(dev, "title_gift_relic", 15):
        shot = dev.dump("mainstory_sweep_no_result") if dev.save_on_fail else ""
        dev.notify("扫荡后没出现结算页，可能体力不足，主线任务先跳过"
                   + (f"（现场：{shot}）" if shot else ""))
        raise Skipped("扫荡后没出现结算页，可能体力不足")
    if not dev.tap_img("btn_gift_ok_relic", timeout=5):
        dev.back()
    dev.wait_stable(timeout=3)
    return n


def challenge_once(dev: Device) -> None:
    """扫荡券用不了时走挑战：详情页[挑战] → 编队页[挑战] → 战斗 → 结算 → 领奖。

    注意是两级挑战按钮：详情页点一次进编队页，编队页还要再点一次才真正开打
    （编队页右下角红色[挑战]，btn_battle_start）。
    AP 不足时游戏会弹「AP回复」购买框 —— 只点取消、绝不买晶石，然后跳过任务。
    """
    if not dev.tap_img("btn_story_challenge", timeout=6):
        raise NotFound("详情页没找到[挑战]")
    # 等「编队页」或「AP 回复购买框」二选一，别白等
    arrived = None
    deadline = time.time() + 10
    while time.time() < deadline:
        dev.check_cancel()
        if dev.exists("btn_battle_start", 0.85):
            arrived = "team"
            break
        if dev.exists("title_ap_refill", 0.88):
            arrived = "ap"
            break
        time.sleep(0.6)
    if arrived == "ap":
        cancel_ap_refill(dev)
        dev.notify("体力不足（AP 不够 10），已取消晶石购买，主线任务跳过")
        raise Skipped("体力不足，已取消购买 AP")
    if arrived == "team":
        # 编队页：再点一次[挑战]才真正开打
        dev.log.info("  编队页，点[挑战]开打")
        time.sleep(0.5)
        dev.tap_img("btn_battle_start", timeout=5)
        dev.wait_stable(timeout=4)
    else:
        dev.log.warning("  没进编队页（可能体力不足或直接开打）")
    # 3 波战斗通常 1~2 分钟才出结算页，给足 180 秒（只等 9 秒会误判成失败）
    # 判据用 btn_next_step（Win 结算页右下「下一步」，1.000）；title_result 是另一种结算页样式
    got = False
    deadline = time.time() + 180
    while time.time() < deadline:
        dev.check_cancel()
        if dev.exists("btn_next_step", 0.90) or dev.exists("title_result", 0.82):
            got = True
            break
        time.sleep(2)
    if not got:
        shot = dev.dump("mainstory_challenge_no_result") if dev.save_on_fail else ""
        dev.notify("挑战后没看到结算页，可能体力不足，主线任务先跳过"
                   + (f"（现场：{shot}）" if shot else ""))
        raise Skipped("挑战后没看到结算页，可能体力不足")
    # Win 结算页 →「下一步」→ 战利品页（获得道具）
    if dev.tap_img("btn_next_step", timeout=8):
        dev.wait_stable(timeout=3)
    # 战利品页底部有 [HOME]，一步回主页，比 SKIP→下一战场 省事
    if dev.tap_img("btn_loot_home", timeout=6):
        dev.log.info("  战利品页点[HOME]回主页")
        dev.wait_stable(timeout=4)
        return
    # 没有 HOME 的结算页样式：退回旧领奖流程
    if dev.tap_img("btn_next_step", timeout=8):
        dev.wait("title_reward", timeout=20)
        if dev.tap_img("btn_release", timeout=8):
            dev.wait_stable(timeout=3)
        if dev.tap_img("btn_close_reward", timeout=10):
            dev.wait_stable(timeout=4)
        else:
            dev.back()
    else:
        dev.back()


def run_detail(dev: Device, times: int, counter: dict) -> None:
    """在详情页扫荡最多 times 次：一次性把数量调到目标值、确认一次，比分多遍快得多。
    扫荡券不可用则退回逐次挑战。数量调不上去（体力只够 1 次）就按单次扫，
    扫完标记体力见底，别再往后面的关卡白跑。
    """
    want = min(times, counter["limit"] - counter["done"])
    if want <= 0:
        return
    dev.check_cancel()
    dev.log.info(f"  批量扫荡 {want} 次（一次确认）")
    r = sweep_batch(dev, want)
    if r == "fallback":
        # 券用不了：退回逐次挑战（详情页[挑战] → 编队页[挑战] → 结算）
        dev.log.info("  扫荡券不可用，改用挑战")
        for _ in range(want):
            if counter["done"] >= counter["limit"]:
                break
            dev.check_cancel()
            challenge_once(dev)
            counter["done"] += 1
            dev.log.info(f"  已完成 {counter['done']}/{counter['limit']} 次")
            if not dev.exists("btn_story_teams", 0.82):
                break
    else:
        counter["done"] += r
        dev.log.info(f"  已完成 {counter['done']}/{counter['limit']} 次")
        if r < want:
            counter["exhausted"] = True
            dev.log.info(f"  体力只够 {r} 次，主线先收工")


# ---------------------------------------------------------------- 主任务
def job_main_story(dev: Device, opts: dict) -> None:
    diffs = _picked(opts, "diffs", ALL_DIFFS)
    chapter_raw = ""
    vals = opts.get("chapter")
    if isinstance(vals, list) and vals:
        chapter_raw = str(vals[0])
    ch, sec, lv = parse_chapter(chapter_raw)
    limit = _count(opts, "sweeps", 5)
    counter = {"done": 0, "limit": limit}

    dev.log.info(f"  主线：难度 {'/'.join(DIFF_LABEL[d] for d in diffs)} · "
                 f"章节 {ch}-{sec}-{lv} · 最多 {limit} 次")

    if not enter_story_page(dev):
        raise NotFound("没能进入主线故事页")

    for d in diffs:
        if counter["done"] >= limit or counter.get("exhausted"):
            break
        dev.check_cancel()
        if not set_difficulty(dev, d):
            continue

        if d == "normal":
            targets = [(1, 1, 1)]  # Normal 只打 1-1-1
        else:
            targets = [(ch, sec, lv)]

        for tch, tsec, tlv in targets:
            if counter["done"] >= limit or counter.get("exhausted"):
                break
            dev.check_cancel()
            dev.log.info(f"    目标：{DIFF_LABEL[d]} {tch}-{tsec}-{tlv}")
            if not select_chapter(dev, tch, d):
                dev.log.warning(f"    选章失败，跳过 {tch}-{tsec}-{tlv}")
                continue
            result = pick_level(dev, tsec, tlv, d, (tch, tsec, tlv))
            if result == "cutscene":
                # Normal 1-1-1 实际是剧情关：SKIP 掉即可（跳过直接回主页）
                dev.log.info("    剧情关，执行 SKIP")
                if skip_cutscene(dev):
                    counter["done"] += 1
                    dev.log.info(f"    剧情已跳过 {counter['done']}/{counter['limit']}")
                else:
                    dev.log.warning("    剧情 SKIP 失败")
                ensure_home(dev)
                continue
            if result != "detail":
                dev.log.warning(f"    关卡没进去，跳过 {tch}-{tsec}-{tlv}")
                dev.back()
                dev.wait_stable(timeout=2)
                continue
            run_detail(dev, limit - counter["done"], counter)
            # 没后续难度了就直接回主页 —— 底部导航亮着时点「首页」一步到位，
            # 比一层层按返回快（ensure_home 自己会先试点首页、点不到才 back）
            if counter["done"] >= limit or counter.get("exhausted"):
                break
            # 挑战路径的[HOME]已经把界面带回主页了，主页再按返回会弹「确认退出游戏」
            if is_home(dev):
                break
            # 还有别的难度要跑 → 退回节点地图接着切难度
            dev.back()
            dev.wait_stable(timeout=2)
            dev.back()
            dev.wait_stable(timeout=2)

    ensure_home(dev)
    dev.log.info(f"  主线任务结束，共执行 {counter['done']} 次")


# ---------------------------------------------------------------- 模板清单
MAIN_STORY_NEEDS = [
    "tab_adventure", "btn_mainstory_entry", "title_mainstory", "label_chapter", "label_chapter_normal",
    "chapter_01_normal",
    "btn_diff_normal", "btn_diff_hard", "chapter_all_star",
    "card_story_get", "level_1_1_1", "card_story_cutscene",
    "btn_story_menu", "btn_story_skip", "dlg_story_skip", "btn_story_skip_yes",
    "btn_story_teams", "btn_story_sweep_once", "btn_sweep_reset", "btn_sweep_plus",
    "btn_story_back", "btn_story_challenge", "btn_battle_start",
    "dlg_sweep_confirm", "btn_sweep_ok", "title_gift_relic", "btn_gift_ok_relic",
    "tab_home",
    "btn_ad_close", "ad_buy", "ad_ap_low",
    "title_ap_refill", "btn_ap_refill_cancel", "btn_loot_home", "battle_hud",
    "title_result", "btn_next_step", "title_reward", "btn_release", "btn_close_reward",
] + [f"chapter_row_{c:02d}_hard" for c in range(1, MAX_KNOWN_CHAPTER + 1)]
