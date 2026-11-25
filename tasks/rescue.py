"""救援战：参加别人的（援助）+ 自己开一场（发起）。

两个任务共用「编队 → 挑战 → 挑战结果 → 报酬一览」这段，区别只在入口和选目标。

筛选弹窗那套坐标是写死的（游戏 UI 固定 2560x1440）。能这么干的原因是：
弹窗里每一项的位置都不随内容变化，只有「内容区滚不滚动」这一个变量，
所以流程固定成：重置 → 选难度 → 滚到 BOSS → 点掉不要的属性 → 确定。
"""
from __future__ import annotations

import time

import cv2

from core.device import Device, NotFound, Skipped
from core.flows import cancel_ap_refill, dismiss_ads, ensure_home, is_home

# ---------------------------------------------------------------- 选项
# 属性顺序跟游戏里一致：左列 火水风，右列 圣魔想
ELEMENTS: list[tuple[str, str, str]] = [
    ("fire", "火", "own_fire"),
    ("water", "水", "own_water"),
    ("wind", "风", "own_wind"),
    ("holy", "圣", "own_holy"),
    ("dark", "魔", "own_dark"),
    ("mind", "想", "own_mind"),
]
ELEMENT_TPL = {k: t for k, _, t in ELEMENTS}
ELEMENT_LABEL = {k: l for k, l, _ in ELEMENTS}
ALL_ELEMENTS = [k for k, _, _ in ELEMENTS]

# 难度从高到低。顺序同时用于「优先打哪个」和界面上选项的排列。
DIFFS = ["lunatic", "extra", "hard", "normal", "easy"]
DIFF_LABEL = {"lunatic": "Lunatic", "extra": "Extra", "hard": "Hard",
              "normal": "Normal", "easy": "Easy"}
ALL_DIFFS = list(DIFFS)

ELEMENT_OPTION = {
    "key": "elements",
    "label": "属性",
    "hint": "全选 = 不筛选，随便哪个属性都打",
    "choices": [{"v": k, "label": l} for k, l, _ in ELEMENTS],
    "default": list(ALL_ELEMENTS),
}
DIFF_OPTION = {
    "key": "difficulties",
    "label": "难度",
    "hint": "全选 = 不限难度。发起时游戏只给 Hard / Extra / Lunatic",
    "choices": [{"v": k, "label": DIFF_LABEL[k]} for k in DIFFS],
    "default": list(ALL_DIFFS),
}
# 援助：连打几场。每场都吃 BP，清单没活了就提前收工，不会白等到超时。
TIMES_OPTION = {
    "key": "times", "label": "次数", "type": "number",
    "min": 1, "max": 20, "default": [1],
    "hint": "连打几场（每场消耗 BP）。中途清单没活了会提前收工",
}
# 发起：游戏自带的「自动周回」。1 = 只打一次（不开周回），2 以上按这个次数跑。
ROUND_OPTION = {
    "key": "rounds", "label": "周回次数", "type": "number",
    "min": 1, "max": 50, "default": [1],
    "hint": "1 = 只打一次；2 以上会打开游戏里的「自动周回」并设成这个次数。受门票数量限制",
}

# ---------------------------------------------------------------- 筛选弹窗
# 坐标按 2560x1440 记，运行时按实际分辨率等比缩放
REF_W = 2560
FLT_RESET = (502, 1230)
FLT_OK = (2065, 1230)
FLT_DIFF = {
    "easy": (900, 740),
    "normal": (1433, 740),
    "hard": (1887, 740),
    "extra": (449, 865),
    "lunatic": (954, 865),
}
FLT_ATTR_X = [493, 813, 1133, 1453, 1773, 2093]
FLT_ATTR_Y = 700

# ---------------------------------------------------------------- 自动周回（发起专用）
# 详情页右下方那一行「自动周回」：
#   关着 = 灰底开关（滑块在左）→ 点一下弹「设定自动周回次数」
#   开着 = 绿色对勾        → 点一下直接关掉，不弹窗
# 所以要改次数必须「先关、再点开」，直接点开着的那个只会把它关掉。
AUTO_ROW = (2110, 977)
AUTO_CHECK = (2040, 935, 2200, 1020)   # 绿勾那一小块，实测 开 0.096 / 关 0.000
AUTO_MIN = (845, 580)                  # 弹窗调节条：MIN − [周回次数:N] + MAX
AUTO_PLUS = (1548, 580)
AUTO_OK = (1493, 1095)
AUTO_CANCEL = (1031, 1095)

# 底部导航「首页」的中心。位置固定，但模板在非主页页面上是暗的（0.76~0.79），
# 默认阈值认不出来 —— 所以回主页时先按坐标点一下，稳。
HOME_TAB = (310, 1345)

# 单场战斗最坏耗时。自动周回 N 次就把等待上限乘 N 倍。
ONE_ROUND_SECONDS = 170


def _xy(dev: Device, pt: tuple[int, int]) -> tuple[int, int]:
    """把 2560 宽的坐标换算到当前画面。换分辨率时不用重测。"""
    w = dev.screen().shape[1]
    k = w / REF_W
    return int(pt[0] * k), int(pt[1] * k)


def _box(dev: Device, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """把 2560 基准的矩形换算到当前画面。"""
    k = dev.screen().shape[1] / REF_W
    return tuple(int(v * k) for v in box)  # type: ignore[return-value]


def _picked(opts: dict, key: str, default: list[str]) -> list[str]:
    vals = opts.get(key)
    if not isinstance(vals, list) or not vals:
        return list(default)
    return [str(v) for v in vals if v in default]


def _is_all(values: list[str], every: list[str]) -> bool:
    return set(values) >= set(every)


def _green_ratio(dev: Device, box) -> float:
    """区域内「鲜绿」像素占比。游戏里的勾就是这种绿（实测勾上 21%，没勾 0%）。"""
    img = dev.screen()
    x1, y1, x2, y2 = (int(v) for v in box)
    h, w = img.shape[:2]
    sub = img[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
    if sub.size == 0:
        return 0.0
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0].astype(int)
    sat = hsv[:, :, 1].astype(int)
    val = hsv[:, :, 2].astype(int)
    green = (hue >= 40) & (hue <= 85) & (sat > 90) & (val > 90)
    return float(green.mean())


def _attr_on(dev: Device, i: int) -> bool:
    """第 i 个属性图标当前勾没勾。看图标右上角那个绿勾，不看像素模板。"""
    x, y = _xy(dev, (FLT_ATTR_X[i], FLT_ATTR_Y))
    k = dev.screen().shape[1] / REF_W
    return _green_ratio(dev, (x + 5 * k, y - 80 * k, x + 95 * k, y - 15 * k)) > 0.05


# ---------------------------------------------------------------- 自动周回
def auto_round_on(dev: Device) -> bool:
    """详情页的「自动周回」现在开没开。看那个绿勾的像素占比，不靠模板。"""
    return _green_ratio(dev, _box(dev, AUTO_CHECK)) > 0.04


def set_auto_round(dev: Device, rounds: int) -> int:
    """把「自动周回」设成 rounds 次，返回真正设上的次数（1 = 没开）。

    rounds <= 1 时反过来要把开关关掉：上次跑剩下的设置会让这一下挑战变成
    连打 N 场，用户点「打一次」结果打了一串，那是最难受的意外。
    """
    if rounds <= 1:
        if auto_round_on(dev):
            dev.tap(*_xy(dev, AUTO_ROW))
            time.sleep(0.8)
            dev.log.info("  自动周回原来是开着的，已关掉（这次只打一场）")
        else:
            dev.log.info("  只打一场，不开自动周回")
        return 1

    if not dev.exists("rescue_auto_row", 0.90):
        dev.notify("详情页没找到「自动周回」，这次只打一场")
        return 1

    if auto_round_on(dev):
        # 开着的时候点一下只会关掉，不弹窗；关掉再点才能进设置
        dev.tap(*_xy(dev, AUTO_ROW))
        time.sleep(0.8)

    if not dev.tap_img("rescue_auto_row", timeout=4, threshold=0.90):
        dev.notify("点不开「自动周回」设置，这次只打一场")
        return 1
    if not _wait_or_none(dev, "title_auto_round", 6):
        dev.notify("「设定自动周回次数」没弹出来，这次只打一场")
        return 1
    dev.wait_stable(timeout=2)

    dev.tap(*_xy(dev, AUTO_MIN))          # 先归到最小，避免游戏记住上次的次数
    time.sleep(0.4)
    for _ in range(rounds - 1):
        dev.tap(*_xy(dev, AUTO_PLUS))
        time.sleep(0.28)
    dev.tap(*_xy(dev, AUTO_OK))
    time.sleep(1.2)

    if dev.exists("title_auto_round", 0.85):
        dev.tap(*_xy(dev, AUTO_CANCEL))   # 卡在弹窗里最糟，先取消出来
        time.sleep(0.6)
        dev.notify("周回次数没设上（多半是门票不够），这次只打一场")
        return 1
    if not auto_round_on(dev):
        dev.notify("自动周回没打开，这次只打一场")
        return 1
    dev.log.info(f"  自动周回已设 {rounds} 次（门票够不够由游戏判定）")
    return rounds


def _wait_or_none(dev: Device, name: str, timeout: float):
    """等元素出现，超时返回 None（不像 dev.wait 那样抛异常）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dev.check_cancel()
        m = dev.find(name)
        if m is not None:
            return m
        time.sleep(0.3)
    return None


def wait_auto_round(dev: Device, limit: float) -> str:
    """等自动周回自己跑完。

    实测（水/Lunatic/周回 2）：战斗里右上角显示「周回中 1/2 → 2/2」，两场之间会闪
    一下黑屏/结算样的转场，**打完直接回到「救援战」六宫格页，不出挑战结果页**，
    奖励自动进「报酬」。

    踩过的坑：以前只要看到一帧 title_result 就当成打完了，结果正好抓在转场上，
    去点[下一步]时人已经在第二场战斗里了 —— 白报一次「结算页没找到」。所以现在
    要求它连续出现约 9 秒才算真停在结算页，中途闪一下不算。
    """
    deadline = time.time() + limit
    result_hits = 0
    while time.time() < deadline:
        dev.check_cancel()
        if dev.exists("own_water", 0.92):
            time.sleep(1.0)          # 转场那一瞬间也可能闪一下，稳住再确认
            if dev.exists("own_water", 0.92) and not dev.exists("title_party", 0.85):
                dev.log.info("  自动周回跑完，已回到救援战页")
                return "page"
            continue
        if dev.matcher.has("title_result") and dev.exists("title_result", 0.85):
            result_hits += 1
            if result_hits >= 6:
                dev.log.info("  停在挑战结果页，按常规领奖")
                return "result"
        else:
            result_hits = 0
        time.sleep(1.5)
    raise NotFound(f"自动周回等待超时（{limit:.0f} 秒）")


# ---------------------------------------------------------------- 筛选
def apply_filter(dev: Device, elements: list[str], diffs: list[str],
                 commit: bool = True) -> bool:
    """按属性/难度筛选清单。返回 False 表示这次不需要筛（全选了）。

    commit=False 只改勾选、不点确定 —— 自测的时候用它把中间状态拍下来看。
    """
    need_diff = not _is_all(diffs, ALL_DIFFS)
    need_elem = not _is_all(elements, ALL_ELEMENTS)
    if not (need_diff or need_elem):
        dev.log.info("  属性/难度都是全选，不筛选，直接取列表第一个")
        return False

    if not dev.tap_img("btn_rescue_filter", timeout=8):
        raise NotFound("救援清单右上角没找到[筛选]")
    dev.wait_stable(timeout=4)

    # 重置 = 全恢复成「全选」。不清这一下的话，上一次跑完留下的筛选会叠加进来。
    dev.tap(*_xy(dev, FLT_RESET))
    time.sleep(1.0)                  # 0.6 秒偏快，重置弹一下才生效
    # 回到顶部，保证难度按钮的坐标是「没滚动」那一套
    dev.swipe(*_xy(dev, (1200, 760)), *_xy(dev, (1200, 1080)), 350)
    time.sleep(0.8)

    if need_diff:
        # 第一下会把「全选」打散成只勾这一项，之后就变成逐项叠加
        for d in diffs:
            dev.tap(*_xy(dev, FLT_DIFF[d]))
            time.sleep(0.6)          # 原来是 0.35，连点容易被吞
        # 回读勾没勾上：被吞的那一下马上补一次，不靠运气
        for d in ALL_DIFFS:
            want = d in diffs
            x, y = _xy(dev, FLT_DIFF[d])
            k = dev.screen().shape[1] / REF_W
            box = (int(x - 140 * k), int(y - 45 * k),
                   int(x + 140 * k), int(y + 45 * k))
            for _ in range(2):
                dev.screen(True)     # 必须取新帧：勾选动画那零点几秒的状态不算数
                if (_green_ratio(dev, box) > 0.15) == want:
                    break
                dev.tap(x, y)
                time.sleep(0.7)
        dev.log.info("  难度勾选：" + "、".join(DIFF_LABEL[d] for d in diffs))

    if need_elem:
        # 属性没有全选开关，只能把不要的逐个点掉。图标在下面，得先滚出来。
        # 拖得比需要的多 —— 滚到底会被内容挡住（clamp），位置才稳定；
        # 拖少一点就可能停在半路，图标位置就对不上了。
        dev.swipe(*_xy(dev, (1200, 1050)), *_xy(dev, (1200, 450)), 600)
        dev.wait_stable(timeout=4)
        # 先看一眼每个图标现在的状态，只点需要变的那些。
        # 不盲点：万一「重置」没生效（游戏版本变了、点了没吃到），
        # 盲目点 5 下会把该留的也点掉，筛出个空列表还查不出原因。
        for i, e in enumerate(ALL_ELEMENTS):
            want = e in elements
            for _ in range(2):
                dev.screen(True)     # 同上：睡完再取新帧，别拿点之前那一帧判
                if _attr_on(dev, i) == want:
                    break
                dev.tap(*_xy(dev, (FLT_ATTR_X[i], FLT_ATTR_Y)))
                time.sleep(0.55)     # 原来是 0.3，连点容易被吞
            else:
                dev.log.warning(f"  属性 [{ELEMENT_LABEL[e]}] 点了两次还是对不上勾选状态")
        dev.log.info("  属性勾选：" + "、".join(ELEMENT_LABEL[e] for e in elements))

    if commit:
        dev.tap(*_xy(dev, FLT_OK))
        dev.wait_stable(timeout=6)
    return True


# ---------------------------------------------------------------- 清单
# 「参加」按钮按优先级试这几张。**必须同时留着简体和繁体两套** ——
# 这游戏的语言（简/繁）是站点设置项，一换字体就整片变：
#   btn_join_bp2/bp1 —— 简体（老素材，实况简体下 0.99 / 0.95）
#   btn_join_bp     —— 繁体（2026-11-17 用繁体实况帧录的，繁体下 1.00）
#
# 踩过的坑：素材是简体录的，页面被切到繁体，同一句「消耗BP2參加」的笔画整片错位，
# 相似度只剩 **0.57**（连 0.88 的门槛都够不着）—— 日志里就表现成「清单里明明
# 摆着可参加的战，却一直说没有可参加的战」。所以加了诊断（见 wait_join），
# 下次再撞上会直接把「最高只认到 0.xx」打出来，不会再让人瞎猜。
JOIN_TPLS = ("btn_join_bp2", "btn_join_bp1", "btn_join_bp")


def first_join(dev: Device):
    """清单里最靠上的可参加按钮。返回 Match 或 None。"""
    screen = dev.screen(True)
    hits = []
    for tpl in JOIN_TPLS:
        if dev.matcher.has(tpl):
            hits += dev.matcher.find_all(screen, tpl, threshold=0.88, max_results=8)
    if not hits:
        return None
    hits.sort(key=lambda m: m.cy)
    return hits[0]


def _join_best_score(dev: Device) -> float:
    """清单上「参加」按钮的最高相似度 —— 只用来诊断，不点。

    为什么要它：清单里明明摆着可参加的战，却一个模板都没命中时，日志只会说
    「没有可参加的战」，人根本分不清是「真没有」还是「按钮样式变了、模板过期」。
    把最高相似度打出来，一眼就知道该不该重录模板。
    """
    screen = dev.screen(True)
    best = 0.0
    for tpl in JOIN_TPLS:
        if not dev.matcher.has(tpl):
            continue
        try:
            for m in dev.matcher.find_all(screen, tpl, threshold=0.4, max_results=4):
                best = max(best, float(m.score or 0.0))
        except Exception:
            pass
    return best


def wait_join(dev: Device, seconds: float = 30) -> object:
    """等清单里出现可参加的战。30 秒还没有就提示用户并跳过。

    玩家自己发的救援战是随缘的，空列表和「刷了但都被打完了」都正常，
    所以这里不能算失败，只能提示 + 跳过。
    """
    t0 = time.time()
    n = 0
    while time.time() - t0 < seconds:
        dev.check_cancel()
        m = first_join(dev)
        if m is not None:
            dev.log.info(f"  找到可参加的战 @({m.cx},{m.cy})")
            return m
        n += 1
        dev.log.info(f"  清单里暂时没有可参加的战，刷新（第 {n} 次）")
        if not dev.tap_img("btn_rescue_refresh", timeout=5):
            raise NotFound("清单页没找到[刷新]按钮，可能没进到救援清单")
        time.sleep(3.5)
    best = _join_best_score(dev)
    if best >= 0.6:
        # 有影子、只是够不到门槛 —— 不是「样式变了」就是「语言被切了」，别让人瞎猜
        try:
            dev.dump("join_button_low")
        except Exception:
            pass
        dev.log.warning(f"  [参加]按钮最高只认到 {best:.2f}（门槛 0.88）"
                        "—— 多半是游戏语言被切了（模板按简体录的，切繁体就整片错位）")
        dev.notify(f"清单里有战，但[参加]按钮只认到 {best:.2f}。"
                   "先看一眼游戏是不是被切成繁体了（改回简体就好），"
                   "如果不是语言问题，那就是 UI 换样式了，得重录模板")
        raise Skipped(f"[参加]按钮相似度只有 {best:.2f}（语言/UI 变了？）")
    dev.notify(
        f"救援清单 {seconds:.0f} 秒没刷出可参加的战，这个任务先跳过。"
        "可以过一会儿再跑，或者把属性/难度放宽到全选。"
    )
    raise Skipped("清单一直没有可参加的战")


# ---------------------------------------------------------------- 战斗
def _boss_dead(dev: Device) -> bool:
    """「BOSS已讨伐」提示条。

    这条提示是半透明黑底，底下是什么画面就映什么底色，同一个模板在不同页面上
    得分从 0.62 到 1.00 都出现过（以前写死 0.88，结果 0.617 那次直接没认出来，
    白等 8 秒还误报成「BP 不足」）。同页没有提示时只有 0.47，所以放到 0.55。
    """
    return dev.matcher.has("toast_boss_dead") and dev.exists("toast_boss_dead", 0.55)


def fight(dev: Device, rounds: int = 1) -> str:
    """编队页点挑战，等这一轮跑完。返回结局：

    - "result"：出了「挑战结果」页，还得自己走 下一步 → 报酬一览 → 解放 → 关闭
    - "page"  ：自动周回跑完，游戏直接回救援战六宫格页，奖励自动入「报酬」

    点完挑战有三种走向：进战斗、BOSS 已经被别人打完、没反应（多半是 BP 不够）。
    这三种必须分清楚 —— 前两种不该报错，第三种要给用户一句人话。

    速度很重要：救援战是抢的，编队页多磨蹭两秒，BOSS 就可能被别人打死了。
    所以这里不再等「画面稳定」，找到[挑战]就立刻点。
    """
    if not dev.tap_img("btn_challenge", timeout=10, threshold=0.88):
        raise NotFound("编队页没找到[挑战]")

    t0 = time.time()
    started = False
    while time.time() - t0 < 6:
        dev.check_cancel()
        if _boss_dead(dev):
            dev.notify("这场救援战的 BOSS 已经被讨伐了，参加不了，换一场")
            raise Skipped("BOSS 已被讨伐")
        if not dev.exists("title_party", 0.85):
            started = True
            break
        time.sleep(0.3)

    if not started:
        # 到这儿要么是 BP/门票不够弹了购买框，要么就是 BOSS 刚被别人打死、
        # 提示条一闪而过没抓到。先把弹窗清掉再报错，别把界面留在弹窗里。
        cancel_ap_refill(dev)
        dismiss_ads(dev)
        if _boss_dead(dev):
            dev.notify("这场救援战的 BOSS 已经被讨伐了，参加不了，先跳过")
            raise Skipped("BOSS 已被讨伐")
        dev.dump("challenge_not_start")
        dev.notify("点了[挑战]但没进战斗，BP/门票可能不够（现场截图在 logs/shots）")
        raise Skipped("挑战没开始，可能是 BP 不足")

    if rounds > 1:
        # 自动周回：游戏自己连着打 N 场，中间不用我们插手，等它跑完就行。
        # 最大等待时间按场次乘倍数 —— 单场最坏 170 秒。
        limit = ONE_ROUND_SECONDS * rounds + 90
        dev.log.info(f"  已进入战斗，自动周回 {rounds} 次，等游戏自己打完（最多 {limit:.0f} 秒）")
        return wait_auto_round(dev, limit)

    dev.log.info("  已进入战斗，等结算（最多 170 秒）")
    why = wait_result(dev, 170)
    if why == "dead":
        dev.notify("BOSS 已经被别人打完了，这场没打成，先跳过")
        raise Skipped("BOSS 已被讨伐")
    if why == "home":
        dev.notify("这场战斗没弹出结算页就直接回主页了 —— 多半是 BOSS 先被别人打完了，"
                   "奖励没拿到。下次跑早点参加。")
        raise Skipped("没看到结算页，战斗可能已经结束")
    dev.log.info("  战斗结束")
    return "result"


def wait_result(dev: Device, limit: float = 170) -> str:
    """等「挑战结果」，返回 ok / dead / home 三种结局。

    援助别人的战有特殊情况：刚点完挑战，BOSS 正好被别的玩家打死，游戏会直接
    把你送回主页或者飘一行「BOSS已讨伐」，压根不出结算页。这种情况不是出错，
    是白跑一趟。所以既要认结算页，也要认这两个出口，不能傻等到超时。
    """
    deadline = time.time() + limit
    while time.time() < deadline:
        dev.check_cancel()
        if dev.matcher.has("title_result") and dev.exists("title_result", 0.85):
            return "ok"
        if _boss_dead(dev):
            return "dead"
        if is_home(dev):
            return "home"
        time.sleep(0.5)
    shot = dev.dump("timeout_result") if dev.save_on_fail else ""
    raise NotFound("等待 [title_result] 超时"
                   f"{limit:.0f}s" + (f"（现场：{shot}）" if shot else ""))


def collect_reward(dev: Device) -> None:
    """挑战结果 → 下一步 → 报酬一览 → 解放 → 关闭。

    踩过的坑（11-21）：关[关闭]那一下相似度只有 0.878（其它场都是 0.998），
    多半是点在了动画中间/按钮边缘，游戏没吃掉。旧代码 `tap_img` 返回 True 就
    当关掉了，结果停在「报酬一览」模态面板上，返回键关不掉、ensure_home 也
    不认，卡死后一路连锁失败。现在点完必须**回读 title_reward 确认面板真消失**，
    没消失就按最新找到的位置再点，最多 3 次。
    """
    if not dev.tap_img("btn_next_step", timeout=10):
        raise NotFound("结算页没找到[下一步]")
    dev.wait("title_reward", timeout=20)
    if dev.tap_img("btn_release", timeout=10):
        dev.wait_stable(timeout=4)

    for attempt in range(3):
        dev.screen(True)
        if not dev.exists("title_reward", 0.88):
            return                      # 报酬页已消失，真关掉了
        m = dev.find("btn_close_reward", 0.90)   # 0.878 那次够不到门槛，重新找最新位置
        if m is None:
            m = dev.find("btn_close_reward", 0.80)
        if m is not None:
            dev.tap(m.cx, m.cy)
        else:
            dev.back()                  # 实在找不到按钮，退回退路
        dev.wait_stable(timeout=2.5)

    # 3 次还没关掉，留现场 + 提示，别把界面留在模态面板里
    dev.dump("reward_close_fail")
    dev.log.warning("  报酬一览关不掉，可能点偏了（现场在 logs/fails）")


def back_home(dev: Device) -> bool:
    if ensure_home(dev):
        return True
    for _ in range(3):
        dev.back()
        dev.wait_stable(timeout=4)
        if ensure_home(dev):
            return True
    return False


def go_home(dev: Device) -> bool:
    """回主页：先直接点底部左下那个「首页」，不行再走通用兜底。

    救援战页 / 救援清单页上，「首页」标签不是当前页，图标是暗的 —— 模板得分只有
    0.76~0.79，默认阈值 0.85 根本认不出来，ensure_home 会退化成连按返回键。
    位置是固定的，按坐标点一下最快，一步到位。
    """
    if is_home(dev):
        return True
    dev.tap(*_xy(dev, HOME_TAB))
    dev.wait_stable(timeout=6)
    if is_home(dev):
        return True
    return back_home(dev)


def on_rescue_list(dev: Device) -> bool:
    """现在是不是在「救援清单」页（右上角有[筛选]）。

    阈值千万别写 0.95：实况里这一项长期只有 **0.91**（图标半透明、旁边还有光效），
    写 0.95 就等于「明明站在清单页却永远判成不在」—— 日志里表现为：点完确定立刻
    连点三次返回、退出清单、重新进一次，白花 7 秒。两个角上的图标任意一个在就算。
    """
    return dev.exists("btn_rescue_filter", 0.88) or dev.exists("btn_rescue_refresh", 0.88)


def back_to_list(dev: Device, tries: int = 3) -> bool:
    """从编队页 / 详情页退回救援清单。抢救援战的时候随时可能被打死，
    退回清单才能换下一场。"""
    for _ in range(tries):
        if on_rescue_list(dev):
            return True
        dev.back()
        dev.wait_stable(timeout=4)
    return on_rescue_list(dev)


def refresh_list(dev: Device, wait: float = 2.5) -> bool:
    """点清单右上角的刷新。

    BOSS 被别人打死之后，那条战还会带着[参加]留在列表里，不刷新就会一直点同一场
    死战 —— 所以每次「没打成」都要主动刷一下再挑下一场。
    """
    if not dev.tap_img("btn_rescue_refresh", timeout=6):
        return False
    time.sleep(wait)
    return True


def join_fast(dev: Device, m) -> bool:
    """点[参加] → 编队页 → 立刻返回 True。

    中间不做 wait_stable：救援战是抢的，多等两秒 BOSS 就没了。
    """
    dev.tap(m.cx, m.cy)
    deadline = time.time() + 8
    while time.time() < deadline:
        dev.check_cancel()
        if dev.exists("btn_challenge", 0.88):
            return True
        if _boss_dead(dev):
            return False
        time.sleep(0.3)
    return False


def enter_rescue_list(dev: Device) -> None:
    """主页 → 救援战 → 救援清单。"""
    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在某个弹窗里")
    if not dev.tap_img("icon_rescue", timeout=10):
        raise NotFound("主页没找到[救援战]")
    dev.wait_stable(timeout=8)


# ---------------------------------------------------------------- 任务
def _count(opts: dict, key: str, default: int = 1) -> int:
    """读数值选项（次数字段），越界/脏数据都夹到合法范围。"""
    vals = opts.get(key)
    try:
        n = int(vals[0]) if isinstance(vals, list) and vals else default
    except (ValueError, TypeError):
        n = default
    return max(1, n)


def job_rescue_aid(dev: Device, opts: dict) -> None:
    """参加别人的救援战：主页 → 救援战 → 清单 → 筛选/刷新 → 参加 → 挑战 → 领奖，
    打完一场会回到清单，按「次数」接着打下一场。

    后半段（编队 → 战斗 → 结算 → 报酬一览）和「发起」走的是同一对函数 fight() /
    collect_reward()，模板也是同一批。两个任务的结算界面没有区别。
    唯一的差异在战斗触发前后：参加别人的战，BOSS 可能在这期间被别人打死。
    """
    elements = _picked(opts, "elements", ALL_ELEMENTS)
    diffs = _picked(opts, "difficulties", ALL_DIFFS)
    times = _count(opts, "times", 1)
    dev.log.info(f"  目标：{'/'.join(ELEMENT_LABEL[e] for e in elements)} · "
                 f"{'/'.join(DIFF_LABEL[d] for d in diffs)} · {times} 场")

    enter_rescue_list(dev)
    apply_filter(dev, elements, diffs)

    done = 0
    tries = 0
    max_tries = times + 4          # 留几次「刚参加就被别人打死」的换场机会
    while done < times and tries < max_tries:
        tries += 1
        if not on_rescue_list(dev) and not back_to_list(dev):
            dev.log.info("  不在救援清单了，重新进一次")
            go_home(dev)
            enter_rescue_list(dev)
            apply_filter(dev, elements, diffs)

        try:
            m = wait_join(dev, 30)
        except Skipped:
            if done:
                dev.notify(f"援助完成 {done}/{times} 场：清单里暂时没有可参加的战了")
                break
            raise

        # 参加 → 编队页 → 马上挑战。抢救援战不能磨蹭，BOSS 随时会被别人打死。
        if not join_fast(dev, m):
            dev.log.info(f"  第 {tries} 次：还没进编队就打完了，刷新清单换一场")
            back_to_list(dev)
            refresh_list(dev)
            continue

        try:
            fight(dev)
            collect_reward(dev)
        except Skipped as e:
            # BOSS 被别人打死：退回清单 → 刷新 → 重新挑一场
            dev.log.info(f"  第 {tries} 次没打成（{e}），退回清单刷新重来")
            back_to_list(dev)
            refresh_list(dev)
            continue
        done += 1
        dev.log.info(f"  第 {done}/{times} 场打完")

    if done < times:
        dev.log.info(f"  本轮到 {done}/{times} 场收工")
    go_home(dev)


def job_rescue_start(dev: Device, opts: dict) -> None:
    """自己发起救援战：主页 → 冒险 → 特殊关卡 → 救援战 → 属性 → 挑战 → 领奖。

    「周回次数」>1 时打开详情页的「自动周回」，让游戏自己连打 N 场：
    实测打完直接回救援战六宫格页（不出结算页、奖励自动进报酬），
    然后点左下「首页」回主页。周回 1 就是老流程，打一场领一次奖。
    """
    elements = _picked(opts, "elements", ALL_ELEMENTS)
    diffs = _picked(opts, "difficulties", ALL_DIFFS)
    rounds = _count(opts, "rounds", 1)
    dev.log.info(f"  目标：{'/'.join(ELEMENT_LABEL[e] for e in elements)} · "
                 f"{'/'.join(DIFF_LABEL[d] for d in diffs)} · 周回 {rounds} 次")

    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在某个弹窗里")
    if not dev.tap_img("tab_adventure", timeout=10):
        raise NotFound("底部导航没找到[冒险]")
    dev.wait_stable(timeout=6)
    if not dev.tap_img("btn_special_stage", timeout=10):
        raise NotFound("冒险页没找到[特殊关卡]")
    dev.wait("btn_rescue_entry", timeout=12)
    dev.tap_img("btn_rescue_entry", timeout=8)
    dev.wait_stable(timeout=6)

    # 选属性：按游戏里的顺序，第一个点得开的就是它
    picked = ""
    for e in ALL_ELEMENTS:
        if e not in elements:
            continue
        if dev.tap_img(ELEMENT_TPL[e], timeout=4):
            picked = e
            break
    if not picked:
        raise NotFound("六个属性入口一个都没点开")
    dev.log.info(f"  发起 {ELEMENT_LABEL[picked]} 救援战")
    dev.wait("btn_challenge_detail", timeout=15)
    dev.wait_stable(timeout=3)

    # 选难度。详情页只会列出 Hard / Extra / Lunatic，勾了 Easy/Normal 也点不到，
    # 这种情况宁可跳过也不能擅自替用户打一场他不想打的难度。
    want = [d for d in DIFFS if d in diffs]
    if not _is_all(diffs, ALL_DIFFS):
        hit = ""
        for d in want:
            tpl = f"diff_{d}"
            if not dev.matcher.has(tpl):
                continue
            if dev.tap_img(tpl, timeout=3, threshold=0.88):
                hit = d
                break
        if hit:
            dev.log.info(f"  难度选 {DIFF_LABEL[hit]}")
            time.sleep(0.5)
        else:
            dev.notify(
                "这个属性下面没有你勾的难度（发起时游戏通常只给 "
                "Hard / Extra / Lunatic），先跳过。"
            )
            raise Skipped("所选难度在这一页不存在")

    got = set_auto_round(dev, rounds)

    if not dev.tap_img("btn_challenge_detail", timeout=8):
        raise NotFound("详情页没找到[挑战]")
    dev.wait_stable(timeout=5)

    ending = fight(dev, got)
    if ending == "result":
        collect_reward(dev)
    else:
        # 自动周回：奖励自动入「报酬」，这里什么都不用点，直接回主页
        dev.log.info("  奖励已自动结算，回主页")
    go_home(dev)


# ---------------------------------------------------------------- 模板清单
BATTLE_NEEDS = [
    "title_party", "btn_challenge", "toast_boss_dead", "title_result",
    "btn_next_step", "title_reward", "btn_release", "btn_close_reward",
]

AID_NEEDS = [
    "icon_rescue", "btn_rescue_filter", "btn_rescue_refresh",
    "btn_join_bp2", "btn_join_bp1", "tab_home",
] + BATTLE_NEEDS

START_NEEDS = [
    "tab_adventure", "btn_special_stage", "btn_rescue_entry",
    "btn_challenge_detail", "btn_close_detail", "tab_home",
    "rescue_auto_row", "title_auto_round",
] + BATTLE_NEEDS + [t for _, _, t in ELEMENTS] + ["diff_lunatic", "diff_extra", "diff_hard"]
