"""遗迹关卡：冒险 → 遗迹，五种遗迹 × 子项 × 难度多选，扫荡券优先。

实测要点（2026-11-16 用真机跑通后记录）：
- 冒险页点[遗迹 REMAINS]进遗迹选择页，上面五种遗迹卡片。
- 点遗迹卡片弹「任务一览」：结晶列六属性、碎片列六能力，其余只一项。
  弹窗里点条目中心就能进下一页。
- 进到难度页后，五行（初级/中级/上级/特级/超级）**必须点行右端的箭头**
  （基准 x≈2130），点行中间没反应 —— 这是踩过坑的地方。
- 详情页点[使用1个]弹确认框「是否使用 AP×10 与 扫荡券×1 进行扫荡？」，
  确定后进「礼物」结算页；扫荡券用不了（或没弹确认框）就退而点[挑战]。
- 扫荡和挑战都要 AP×10，所以「扫荡次数」当成全局上限，体力有限设小点。
"""
from __future__ import annotations

import time

from core.device import Device, NotFound, Skipped
from core.flows import cancel_ap_refill, ensure_home
from .sweep import set_count, use_point

REF_W = 2560
# 难度页每行右端的箭头 x（基准 2560 宽）。点这里才会进详情页。
DIFF_ARROW_X = 2130

# ---------------------------------------------------------------- 选项数据
# 每项: (值, 界面文字, 模板名)
RUINS = [
    ("crystal", "结晶遗迹", "card_relic_crystal"),
    ("shard", "碎片遗迹", "card_relic_shard"),
    ("potion", "秘药遗迹", "card_relic_potion"),
    ("gold", "雷格币遗迹", "card_relic_gold"),
    ("powder", "锤粉遗迹", "card_relic_powder"),
]
ATTRS = [
    ("dark", "魔", "attr_dark"),
    ("fire", "火", "attr_fire"),
    ("water", "水", "attr_water"),
    ("wind", "风", "attr_wind"),
    ("holy", "圣", "attr_holy"),
    ("mind", "想", "attr_mind"),
]
ABILS = [
    ("hp", "HP被动", "abil_hp"),
    ("patk", "物攻被动", "abil_patk"),
    ("pdef", "物防被动", "abil_pdef"),
    ("matk", "魔攻被动", "abil_matk"),
    ("mdef", "魔防被动", "abil_mdef"),
    ("sp", "SP被动", "abil_sp"),
]
DIFFS = [
    ("chu", "初级", "diff_chu"),
    ("zhong", "中级", "diff_zhong"),
    ("shang", "上级", "diff_shang"),
    ("te", "特级", "diff_te"),
    ("chao", "超级", "diff_chao"),
]

ALL_RUINS = [v for v, _, _ in RUINS]
ALL_ATTRS = [v for v, _, _ in ATTRS]
ALL_ABILS = [v for v, _, _ in ABILS]
ALL_DIFFS = [v for v, _, _ in DIFFS]

RUIN_TPL = {v: t for v, _, t in RUINS}
RUIN_LABEL = {v: l for v, l, _ in RUINS}
ATTR_TPL = {v: t for v, _, t in ATTRS}
ATTR_LABEL = {v: l for v, l, _ in ATTRS}
ABIL_TPL = {v: t for v, _, t in ABILS}
ABIL_LABEL = {v: l for v, l, _ in ABILS}
DIFF_TPL = {v: t for v, _, t in DIFFS}
DIFF_LABEL = {v: l for v, l, _ in DIFFS}


def _choice_option(key, label, hint, items):
    return {
        "key": key, "label": label, "hint": hint,
        "choices": [{"v": v, "label": l} for v, l, _ in items],
        "default": [v for v, _, _ in items],
    }


RUINS_OPTION = _choice_option("ruins", "遗迹", "全选 = 五种遗迹都打", RUINS)
ATTR_OPTION = _choice_option("attrs", "结晶属性", "只对结晶遗迹生效；全选 = 六种属性都打", ATTRS)
ABIL_OPTION = _choice_option("abils", "碎片能力", "只对碎片遗迹生效；全选 = 六种能力都打", ABILS)
DIFF_OPTION = _choice_option("diffs", "难度", "全选 = 初中上特超都打", DIFFS)
# 数值输入型选项（前端渲染成输入框，见 web/app.js 的 type==='number' 分支）
SWEEP_OPTION = {
    "key": "sweeps", "label": "扫荡次数", "type": "number",
    "min": 1, "max": 99, "default": [5],
    "hint": "本次任务最多打几次（扫荡和挑战都算）。每次消耗 AP×10，体力有限就设小一点",
}


# ---------------------------------------------------------------- 工具
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


def _scale(dev: Device, x: int) -> int:
    """把基准 2560 宽的 x 换算到当前屏幕。"""
    return int(x * dev.screen().shape[1] / REF_W)


def _wait_exists(dev: Device, tpl: str, seconds: float) -> bool:
    """等某元素出现，超时返回 False（不抛异常）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        dev.check_cancel()
        if dev.exists(tpl, 0.82):
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------- 导航
def enter_relic_page(dev: Device) -> bool:
    """主页 → 冒险 → 遗迹选择页。冒险页偶尔弹活动公告，清掉再点。"""
    if not ensure_home(dev):
        return False
    if not dev.tap_img("tab_adventure", timeout=10):
        raise NotFound("底部导航没找到[冒险]")
    dev.wait_stable(timeout=6)

    # 点遗迹入口。被活动弹窗挡住时按返回清掉再试（最多 3 轮）
    for attempt in range(3):
        if dev.tap_img("btn_relic_entry", timeout=4):
            dev.wait_stable(timeout=4)
            if dev.exists("title_relic", 0.82) or dev.exists("card_relic_crystal", 0.82):
                return True
        dev.log.info(f"  没进遗迹页，清弹窗重试（第 {attempt + 1} 次）")
        dev.back()
        dev.wait_stable(timeout=3)
    return dev.exists("title_relic", 0.82) or dev.exists("card_relic_crystal", 0.82)


def pick_subitems(dev: Device, tpls: dict[str, str], wanted: list[str], label: str):
    """在「任务一览」弹窗里，把勾选的子项（属性/能力）逐个点进去。

    生成器：每次 yield (值, 标签)，调用方在处理完一个子项回到弹窗后继续 next。
    """
    for v in wanted:
        tpl = tpls.get(v)
        if not tpl or not dev.matcher.has(tpl):
            continue
        # 弹窗列表可能要滚动：先找，找不到就上下滑再找
        m = dev.find(tpl)
        if m is None:
            for _ in range(3):
                dev.swipe(_scale(dev, 1280), _scale(dev, 850),
                          _scale(dev, 1280), _scale(dev, 400), 500)
                dev.wait_stable(timeout=1.5)
                m = dev.find(tpl)
                if m is not None:
                    break
            # 滑到底还找不到，滚回顶部再找一次
            if m is None:
                for _ in range(3):
                    dev.swipe(_scale(dev, 1280), _scale(dev, 400),
                              _scale(dev, 1280), _scale(dev, 900), 500)
                    dev.wait_stable(timeout=1.5)
                m = dev.find(tpl)
        if m is None:
            dev.log.warning(f"  弹窗里没找到 {label}[{tpls[v]}]，跳过这个子项")
            continue
        dev.tap(m.cx, m.cy)
        dev.wait_stable(timeout=4)
        yield v


def back_to_tasklist(dev: Device) -> bool:
    """从难度页/详情页退回「任务一览」弹窗。"""
    for _ in range(3):
        dev.back()
        dev.wait_stable(timeout=2.5)
        if dev.exists("title_tasklist", 0.82):
            return True
    return False


# ---------------------------------------------------------------- 难度页
def tap_difficulty(dev: Device, diff: str) -> bool:
    """在难度页点指定难度行的右端箭头。返回是否点中。"""
    tpl = DIFF_TPL[diff]
    m = dev.find(tpl)
    if m is None:
        dev.log.warning(f"  难度页没找到[{DIFF_LABEL[diff]}]")
        return False
    # 关键：必须点行右端箭头，点行中间没反应
    dev.tap(_scale(dev, DIFF_ARROW_X), m.cy)
    dev.wait_stable(timeout=4)
    return dev.exists("btn_teams_relic", 0.82)


# ---------------------------------------------------------------- 详情页
# 扫荡数量调节条的实现放在 tasks/sweep.py（遗迹和主线共用一份）：
# 热区偏上、连点被吞、数字刷新滞后、**数量会被记住**这四个坑都在那里处理。
# 这里最关键的一条：不管这次扫几次，都要先把数量归 1 —— 否则上一次设的 2
# 会一直留着，每次点「使用2个」就是扫 2 次。
def sweep_batch(dev: Device, count: int):
    """详情页一次性批量扫荡最多 count 次。
    返回实际次数(int，体力不足时小于 count) / "fallback"（券不可用）/ 抛 Skipped。
    """
    n = count
    actual = set_count(dev, count)
    if actual <= 0:
        dev.log.info("  数量调节不可用（体力大概只够 1 次），先按单次扫荡")
        n = 1
    else:
        n = actual
        if n < count:
            dev.log.info(f"  数量只调到 {n} 次（体力不够更多），就按 {n} 次扫")
        else:
            dev.log.info(f"  数量已调到 {n} 次")
    dev.tap(*use_point(dev))
    if not (dev.exists("dlg_sweep_confirm", 0.82) or _wait_exists(dev, "dlg_sweep_confirm", 3)):
        # AP 连一次都不够时会弹「AP回复」购买框 —— 取消，绝不买晶石
        if cancel_ap_refill(dev):
            dev.notify("体力不足，已取消晶石购买（未扣晶石），遗迹任务跳过")
            raise Skipped("体力不足，已取消购买 AP")
        return "fallback"
    if not dev.tap_img("btn_sweep_ok", timeout=4):
        return "fallback"
    if not _wait_exists(dev, "title_gift_relic", 15):
        shot = dev.dump("ruins_sweep_no_result") if dev.save_on_fail else ""
        dev.notify("扫荡后没出现结算页，可能体力不足，遗迹任务先跳过"
                   + (f"（现场：{shot}）" if shot else ""))
        raise Skipped("扫荡后没出现结算页，可能体力不足")
    if not dev.tap_img("btn_gift_ok_relic", timeout=5):
        dev.back()
    dev.wait_stable(timeout=3)
    return n


def challenge_once(dev: Device) -> None:
    """扫荡券用不了时走挑战：详情页[挑战] → 编队页[挑战] → 战斗 → 结算 → 领奖。

    两级挑战按钮：详情页点一次进编队页，编队页右下角红色[挑战]再点一次才开打。
    AP 不足时游戏会弹「AP回复」购买框 —— 只点取消、绝不买晶石，然后跳过任务。
    """
    if not dev.tap_img("btn_challenge_relic", timeout=6):
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
        dev.notify("体力不足（AP 不够 10），已取消晶石购买，遗迹任务跳过")
        raise Skipped("体力不足，已取消购买 AP")
    if arrived == "team":
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
        shot = dev.dump("ruins_challenge_no_result") if dev.save_on_fail else ""
        dev.notify("挑战后没看到结算页，可能体力不足，遗迹任务先跳过"
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
    """在详情页扫荡最多 times 次：一次把数量调到目标值、确认一次，比分多遍快得多。
    券用不了退回逐次挑战；数量调不上去（体力只够 1 次）按单次扫，扫完收工。

    少扫了会再补一次（最多两轮）：这个网页版的「+」偶尔会连续吞点击，
    停一会儿重来往往就好了。RESET 会把旧数量清掉，所以重来不会叠加。
    """
    want = min(times, counter["limit"] - counter["done"])
    if want <= 0:
        return
    dev.check_cancel()
    dev.log.info(f"  批量扫荡 {want} 次（一次确认）")
    for attempt in range(2):
        r = sweep_batch(dev, want)
        if r == "fallback":
            dev.log.info("  扫荡券不可用，改用挑战")
            for _ in range(want):
                if counter["done"] >= counter["limit"]:
                    break
                dev.check_cancel()
                challenge_once(dev)
                counter["done"] += 1
                dev.log.info(f"  已完成 {counter['done']}/{counter['limit']} 次")
                if not dev.exists("btn_teams_relic", 0.82):
                    break
            return
        counter["done"] += r
        dev.log.info(f"  已完成 {counter['done']}/{counter['limit']} 次")
        if r >= want:
            return
        want = min(want - r, counter["limit"] - counter["done"])
        if want <= 0:
            return
        dev.log.info(f"  比要求少扫了，再补 {want} 次")
    counter["exhausted"] = True
    dev.log.info("  数量调不上去了（多半体力只够这么多），遗迹先收工")


# ---------------------------------------------------------------- 主任务
def job_ruins(dev: Device, opts: dict) -> None:
    ruins = _picked(opts, "ruins", ALL_RUINS)
    attrs = _picked(opts, "attrs", ALL_ATTRS)
    abils = _picked(opts, "abils", ALL_ABILS)
    diffs = _picked(opts, "diffs", ALL_DIFFS)
    limit = _count(opts, "sweeps", 5)
    counter = {"done": 0, "limit": limit}

    dev.log.info(f"  遗迹：{'/'.join(RUIN_LABEL[r] for r in ruins)} · "
                 f"难度：{'/'.join(DIFF_LABEL[d] for d in diffs)} · 最多 {limit} 次")

    if not enter_relic_page(dev):
        raise NotFound("没能进入遗迹选择页")

    for r in ruins:
        if counter["done"] >= limit or counter.get("exhausted"):
            break
        dev.check_cancel()
        tpl = RUIN_TPL[r]
        if not dev.matcher.has(tpl):
            dev.log.warning(f"  缺模板 {tpl}，跳过 {RUIN_LABEL[r]}")
            continue
        if not dev.tap_img(tpl, timeout=6):
            dev.log.warning(f"  没找到遗迹[{RUIN_LABEL[r]}]，跳过")
            continue
        dev.wait_stable(timeout=3)
        if not dev.exists("title_tasklist", 0.82):
            dev.log.warning(f"  [{RUIN_LABEL[r]}] 没弹出任务一览，跳过")
            continue

        # 结晶/碎片有多个子项，逐个进；其余遗迹弹窗里就一个条目
        if r == "crystal":
            subs = list(pick_subitems(dev, ATTR_TPL, attrs, "属性"))
            sub_label = ATTR_LABEL
        elif r == "shard":
            subs = list(pick_subitems(dev, ABIL_TPL, abils, "能力"))
            sub_label = ABIL_LABEL
        else:
            # 单项：点弹窗里唯一的条目（用遗迹卡片模板在弹窗内找，或点固定位置）
            m = dev.find("title_tasklist")
            if m:
                dev.tap(m.cx + _scale(dev, 40), m.cy + _scale(dev, 130))
            else:
                dev.tap(_scale(dev, 1280), _scale(dev, 290))
            dev.wait_stable(timeout=3)
            subs = [r]
            sub_label = RUIN_LABEL

        for sub in subs:
            if counter["done"] >= limit or counter.get("exhausted"):
                break
            dev.check_cancel()
            dev.log.info(f"  目标：{RUIN_LABEL[r]}·{sub_label[sub]}")
            # 现在应在难度页
            for d in diffs:
                if counter["done"] >= limit or counter.get("exhausted"):
                    break
                dev.check_cancel()
                if not dev.exists("btn_teams_relic", 0.82) and not dev.exists("diff_chu", 0.82):
                    dev.log.warning("  不在难度页了，跳过剩余难度")
                    break
                dev.log.info(f"    难度：{DIFF_LABEL[d]}")
                if tap_difficulty(dev, d):
                    run_detail(dev, limit - counter["done"], counter)
                    # 从详情页退回难度页
                    dev.back()
                    dev.wait_stable(timeout=2.5)
            # 回到任务一览，处理下一个子项
            if not back_to_tasklist(dev):
                break
        # 回到遗迹选择页，处理下一个遗迹
        dev.back()
        dev.wait_stable(timeout=2.5)

    ensure_home(dev)
    dev.log.info(f"  遗迹任务结束，共执行 {counter['done']} 次")


# ---------------------------------------------------------------- 模板清单
RUINS_NEEDS = [
    "tab_adventure", "btn_relic_entry", "title_relic", "title_tasklist",
    "btn_tasklist_close", "btn_teams_relic", "btn_sweep_once",
    "btn_sweep_reset", "btn_sweep_plus",
    "dlg_sweep_confirm", "btn_sweep_ok", "title_gift_relic", "btn_gift_ok_relic",
    "btn_challenge_relic", "btn_battle_start", "tab_home",
    # 全局广告弹窗（AP/BP不足促销）——core.flows.dismiss_ads 用
    "btn_ad_close", "ad_buy", "ad_ap_low",
    "title_ap_refill", "btn_ap_refill_cancel", "btn_loot_home", "battle_hud",
    # 战斗兜底（挑战路径）
    "title_result", "btn_next_step", "title_reward", "btn_release", "btn_close_reward",
] + [t for _, _, t in RUINS] + [t for _, _, t in ATTRS] \
  + [t for _, _, t in ABILS] + [t for _, _, t in DIFFS]
