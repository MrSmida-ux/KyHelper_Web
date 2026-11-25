"""派遣领取：主页 → 特典 → 派遣 → 拉列表找「已归来」→ 全部领取 → 再次派遣 → 主页

实测到的派遣规则（2560x1440）：
- 目的地列表最多 6 项，一屏看 3 项，要往下翻才看得全
- 一次最多派 3 支队伍，冷却 3~12 小时
- 归来后列表项状态字从「可派遣」变成「已归来」，右上角多一个红色「!」
- 点「全部领取」先弹 LvUP（**不一定有**，角色等级越高越少），再弹礼物一览，确认后
  回到派遣页，游戏主动问「是否使用同队伍再度派遣？」—— 点「是」就复用上一轮队伍
- 一天只有一轮能领：跑第二遍列表里全是「可派遣」或倒计时，按「跳过」处理

代码里没做「自动选择 + 派遣」当主路径：实测「自动选择」在未编队时并不会真的填满
队伍，点「派遣」只会提示「请编队1人以上」。所以再派遣只认游戏自己给的询问框，
拿不到询问框就如实记一条警告，奖励已经领到了，不因此判失败。
"""
from __future__ import annotations

import time

from core.device import Device, NotFound, Skipped
from core.flows import ensure_home, enter_tokuten

REF_W = 2560

# 左侧任务栏的可滚动区域（x1,y1,x2,y2）
LIST_BOX = (150, 310, 890, 1120)
_SCROLL_NEXT = ((520, 1010), (520, 430))     # 手指上滑 → 看下一屏
_SCROLL_PREV = ((520, 430), (520, 1010))     # 手指下滑 → 看上一屏


def _xy(dev: Device, x: int, y: int) -> tuple[int, int]:
    """按当前屏宽缩放坐标（模拟器换分辨率也能用）。"""
    w = int(dev.screen().shape[1])
    k = w / REF_W
    return int(x * k), int(y * k)


def _swipe(dev: Device, pts, ms: int = 450) -> None:
    (x1, y1), (x2, y2) = pts
    a, b = _xy(dev, x1, y1), _xy(dev, x2, y2)
    dev.swipe(a[0], a[1], b[0], b[1], ms)


# 模板按「界面语言」各备一套：简体（默认）和繁体（_trad）。
# 游戏一切语言，同一句话的笔画整片不同（派遣/派遣、全部领取/全部領取），
# 相似度从 1.000 掉到 0.73~0.84，刚好卡在 0.85 门槛下面 —— 表现就是
# 「明明在特典页，却说找不到派遣卡片」。所以每个按钮都给一组候选，
# 由 tap_any / exists_any 挑当前语言认得出的那套。
CARD_TPLS = ("card_dispatch", "card_dispatch_trad")
TITLE_TPLS = ("title_dispatch", "title_dispatch_trad")
CLAIM_TPLS = ("btn_dispatch_claim_all", "btn_dispatch_claim_all_trad")


# ---------------------------------------------------------------- 进派遣页
def open_dispatch(dev: Device) -> None:
    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在某个弹窗里")

    dev.log.info("  进入特典")
    if not enter_tokuten(dev):
        raise NotFound("主页没找到 [特典] 按钮")

    dev.log.info("  打开派遣")
    if not dev.tap_any(CARD_TPLS, timeout=8):
        name, score = dev.best_any(CARD_TPLS)
        tip = "（有点像但够不到门槛，多半是界面语言变了，重录这张即可）" if score >= 0.5 else ""
        raise NotFound(f"特典页没找到 [派遣] 卡片：最高 {name} {score:.2f}{tip}")
    dev.wait_stable(timeout=8)

    if not dev.exists_any(TITLE_TPLS, 0.85):
        name, score = dev.best_any(TITLE_TPLS)
        raise NotFound(f"点了派遣卡片但没进到派遣页（标题最高 {name} {score:.2f}）")


def find_returned(dev: Device) -> bool:
    """把任务栏从顶翻到底，看有没有「已归来」。

    列表最多 6 项、一屏 3 项。先看眼前这一屏（多数时候列表就停在有货的位置），
    再滚回顶部逐屏往下翻，回顶路上经过的屏也顺带看一眼。
    """
    if dev.exists("text_returned", 0.88):
        return True

    for _ in range(3):                     # 回到最上面
        _swipe(dev, _SCROLL_PREV, 320)
        time.sleep(0.2)
        if dev.exists("text_returned", 0.88):
            return True

    for _ in range(4):
        dev.check_cancel()
        if dev.exists("text_returned", 0.88):
            return True
        _swipe(dev, _SCROLL_NEXT, 420)
        time.sleep(0.45)
    return dev.exists("text_returned", 0.88)


# ---------------------------------------------------------------- 领奖
def claim_all(dev: Device) -> None:
    """点「全部领取」，把随后的 LvUP / 礼物一览弹窗一路点掉，直到回派遣页。

    阈值都按 0.90 判：这几个按钮长得像（橙红外框 + 白字），互相之间会误匹配到
    0.87 左右，真命中都是 1.000，所以卡在 0.90 才不会点错按钮。
    """
    if not dev.tap_any(CLAIM_TPLS, timeout=6, threshold=0.85):
        name, score = dev.best_any(CLAIM_TPLS)
        raise NotFound(f"派遣页没找到 [全部领取] 按钮：最高 {name} {score:.2f}")
    time.sleep(1.6)

    for _ in range(12):
        dev.check_cancel()
        if dev.exists("btn_lvup_ok", 0.90):
            dev.log.info("  LvUP 弹窗 → 确定")
            dev.tap_img("btn_lvup_ok", timeout=3, threshold=0.90)
            time.sleep(1.3)
            continue
        if dev.exists("btn_gift_confirm", 0.90):
            dev.log.info("  礼物一览 → 确认")
            dev.tap_img("btn_gift_confirm", timeout=3, threshold=0.90)
            time.sleep(1.5)
            continue
        # 回到派遣页，或者「同队伍再度派遣」的询问框已经盖上来，都算弹窗走完
        if dev.exists_any(TITLE_TPLS, 0.85) or dev.exists("btn_dialog_yes", 0.90):
            return
        time.sleep(0.8)

    dev.log.warning("  领奖弹窗没有全部关掉，继续往下走")


def redispatch(dev: Device) -> bool:
    """领完奖后游戏会问「是否使用同队伍再度派遣？」，点「是」即可。

    询问框比派遣页晚一拍出现，所以看到派遣页之后再等一会儿，别急着判「没弹」。
    """
    deadline = time.time() + 12
    while time.time() < deadline:
        dev.check_cancel()
        if dev.exists("btn_dialog_yes", 0.90):
            dev.log.info("  弹出「是否使用同队伍再度派遣」→ 是")
            if dev.tap_img("btn_dialog_yes", timeout=4, threshold=0.90):
                dev.wait_stable(timeout=12)
                return True
        if dev.exists_any(TITLE_TPLS, 0.85):
            time.sleep(1.0)                # 页面刚回来，询问框还在路上
            continue
        time.sleep(0.5)
    return False


def verify_dispatched(dev: Device) -> bool:
    """派遣成功的判据：右下按钮由「派遣」变成「加速」。

    两个按钮长得像，但实测互斥得很干净：可派遣时「派遣」1.000 /「加速」0.847，
    派出后正好反过来 0.842 / 1.000 —— 所以阈值定 0.90，不会看错。
    """
    for _ in range(4):
        dev.check_cancel()
        if dev.exists("btn_accel", 0.90):
            return True
        time.sleep(1.0)
    return False


# ---------------------------------------------------------------- 任务
def job_dispatch(dev: Device) -> None:
    """领取派遣奖励，然后顺手把队伍再派出去一轮。

    幂等：已经派出去的（在冷却中）列表里没有「已归来」，直接跳过。
    """
    open_dispatch(dev)

    if not find_returned(dev):
        dev.log.info("  列表里没有「已归来」的队伍，今天这轮没什么可领的")
        dev.notify("派遣：没有已归来的队伍（冷却中，3~12 小时后再来）")
        raise Skipped("派遣列表里没有「已归来」")

    dev.log.info("  有队伍已归来，领取奖励")
    claim_all(dev)

    if redispatch(dev):
        if verify_dispatched(dev):
            dev.log.info("  ✓ 已用同队伍再派遣一轮")
        else:
            dev.log.warning("  点了「是」但没确认到派遣成功，请留意")
    else:
        # 奖励已经领到了，再派遣失败不该让整个任务算失败
        dev.log.warning("  没等到「同队伍再度派遣」的询问框，本轮只领奖、未再派遣")
        dev.notify("派遣奖励已领取，但没能自动再派遣，请手动点一下派遣")

    if ensure_home(dev):
        dev.log.info("  已返回主页")
    else:
        dev.log.warning("  没能确认回到主页")


DISPATCH_NEEDS = [
    "btn_tokuten", "card_dispatch", "card_dispatch_trad",
    "title_dispatch", "title_dispatch_trad", "text_returned",
    "btn_dispatch_claim_all", "btn_dispatch_claim_all_trad",
    "btn_lvup_ok", "btn_gift_confirm",
    "btn_dialog_yes", "btn_accel", "btn_auto_select", "btn_dispatch_go", "tab_home",
]
