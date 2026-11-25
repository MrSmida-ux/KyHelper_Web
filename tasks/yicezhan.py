"""异策战：把「期间限定 → 异策战」里今天还有次数的 BOSS 全部打完。

用户描述的流程（也是这里的实现顺序）：

    冒险 → 期间限定 → 异策战 → 列表（带感叹号 = 还有挑战次数）
    → 点进去 → 详情页看剩余挑战次数 → 挑战 → 编队 → 挑战 → 打一场（约 2 分钟）
    → Result（统计）→ 关闭 → Result（获得道具）→ 关闭 → 回到列表
    → 再点进去，这次详情页多了 [SKIP] → 点 SKIP 瞬间结算 → 关闭
    → 次数用完，列表里那条的感叹号消失 → 回首页

三个关键判断：

1. **感叹号 = 还有次数**。列表卡片右上角的粉色圆形感叹号，和「免费召唤」列表
   里那个是同一个图标（实测跨界面得分 0.99），次数归零就消失。所以不用识数：
   循环到列表里找不到感叹号为止。这是唯一可靠的「打完了」信号 —— 详情页的
   [SKIP] 按钮在 0/3 时照样亮着，只认它会死循环。
2. **第一场必须真打，之后才出 [SKIP]**。SKIP 是「当天通关后才出现的快速完成」，
   按当日最高分结算，所以第一场老老实实点 [挑战]（详情页 → 编队页两级）。
3. **战斗最长约 5 分钟**。这一场是限时 2 分钟的计时战（TIME 1:44 → 0:00），
   加上加载和 CONNECTING，给 420 秒。判据是左上角 TIME 计时器消失。

结算用的是全屏「Result」页（统计 → 关闭 → 获得道具 → 关闭），和主线那种
Win 页不是一套模板，所以单独做了 yice_result_title / yice_result_close。
"""
from __future__ import annotations

import time

from core.device import Device, NotFound, Skipped
from core.flows import ensure_home

REF_W = 2560

# 列表右侧卡片区（左边是立绘和标题，只看右边这一条）
BADGE_REGION = (2100, 240, 460, 880)
# 徽章 → 卡片中心（实测 (2339,353) → (1874,450)）
CARD_OFF = (-465, 97)
# 详情页右下角 [挑战]（[关闭]/[SKIP]/[挑战] 三个按钮的位置会随 SKIP 出现而左移，
# 所以只有 [挑战] 固定在最右边，直接按坐标点）
DETAIL_CHALLENGE = (2061, 1312)
# 单个 BOSS 一天几次挑战，上限多给几次，防止界面变化时死循环
MAX_ROUNDS = 12
# 一场战斗最多等多久（限时 2 分钟 + 加载 + CONNECTING）
BATTLE_LIMIT = 420.0

BADGE = "yice_badge"
SKIP = "yice_skip"
DETAIL = "yice_detail"          # 详情页「剩余挑战次数」那一行
LIST_TITLE = "yice_list_title"  # 旧标题模板（「异策战」），活动每期换名，只作兼容
RANK = "yice_rank"              # 右上角「排行榜」按钮：活动页通用锚点，每期都在
BATTLE = "yice_battle"          # 战斗左上角 TIME 计时器
RESULT_TITLE = "yice_result_title"
RESULT_CLOSE = "yice_result_close"
# 只含「关闭」二字的小模板：比整块金色按钮皮肤稳定得多。
# 活动每期换皮（按钮底色/形状会变）时，整块 yice_result_close 可能认不出，
# 但「关闭」这两个字每期都在。全屏找它的位置来点，不受皮肤/位置变化影响。
CLOSE_WORD = "yice_close_word"
# 次数用完还点 [SKIP]/[挑战]，游戏会弹「购买挑战次数」（晶石×50）。**绝不能买**。
BUY_DIALOG = "dlg_buy_attempts"
BUY_CANCEL = "btn_buy_cancel"
# [取消] 的兜底坐标（左半屏那个）。右边的 [确定] 无论如何都不点。
BUY_CANCEL_PT = (1056, 1150)

NEEDS = [
    "tab_home", "tab_adventure",
    "btn_limited_time", "btn_yice",
    RANK, LIST_TITLE, BADGE, DETAIL, SKIP, BATTLE,
    "title_party", "btn_challenge", "btn_close_detail",
    RESULT_TITLE, RESULT_CLOSE, CLOSE_WORD, BUY_DIALOG, BUY_CANCEL,
]


def _xy(dev: Device, x: float, y: float) -> tuple[int, int]:
    """把 2560 宽的基准坐标换算到当前画面。"""
    k = dev.screen().shape[1] / REF_W
    return int(x * k), int(y * k)


def _region(dev: Device, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    k = dev.screen().shape[1] / REF_W
    return (int(box[0] * k), int(box[1] * k), int(box[2] * k), int(box[3] * k))


def _find_in(dev: Device, name: str, box, th: float):
    """在屏幕的一块区域里找模板（region 交给 matcher，别自己先裁）。"""
    return dev.matcher.find(dev.screen(True), name, th, region=_region(dev, box))


# ---------------------------------------------------------------- 页面判断
def on_detail(dev: Device) -> bool:
    """是不是在 BOSS 详情页。

    **不要拿 `yice_detail` 判断**：那张模板录的是旧一期那行「剩余挑战次数」，
    这期改成了「挑战次数 3/3」，实测只有 0.70（阈值 0.93）→ 人明明站在详情页、
    「挑战」按钮就在右下角，却报「进不去详情页」。改用右下角橙色**[关闭]按钮**：
    列表页 0.45 / 详情页 0.95，跨期稳定的通用 UI，干净分开。
    """
    dev.screen(True)
    return dev.exists("btn_close_detail", 0.90)


def on_list(dev: Device) -> bool:
    """是不是在活动列表页。

    **不要拿标题判断**：标题每期换（旧「异策战」→ 这期「竞击战圣」，只有 0.70）。
    右上角「排行榜」蓝色按钮是活动页通用 UI，但**列表页和详情页都有它**
    （实测 1.00 / 0.98），所以还必须排除详情页，否则点进详情后主循环会误以为还在列表。
    标题只作兜底。
    """
    dev.screen(True)
    # SKIP 出的半透明结算浮层叠在详情页上，会把详情页的[关闭]按钮压暗 →
    # on_detail() 认不出 → 只看到排行榜就误判「在列表」。结果 close_results 被
    # 列表短路跳过、浮层没关，下一轮在浮层遮挡下又找不到感叹号 → 误判打完收工
    # （11-20 03:53 那轮：进列表报 2 个感叹号，SKIP 后 1 秒就收工，零关闭日志）。
    # 结算信号在就绝不算列表页。
    if _result_shown(dev):
        return False
    if on_detail(dev):
        return False
    return dev.exists(RANK, 0.90) or dev.exists(LIST_TITLE, 0.93)


def has_skip(dev: Device):
    """详情页右下角有没有真的 [SKIP] 按钮。

    这期的详情页底部有一行「每日通过1回关卡后可进行SKIP」的**提示文字**，
    它的图标让 `yice_skip` 模板匹配到 0.858（差一点点就过 0.92），而真正的
    SKIP 是实心橙色按钮、跨期样式不变、得分 0.99。阈值提到 0.95 把两者分开，
    宁可漏 SKIP 去真打一场，也不能把提示文字当成按钮点下去。
    """
    dev.screen(True)
    return _find_in(dev, SKIP, (1150, 1180, 1380, 260), 0.95)


def _in_battle(dev: Device) -> bool:
    return dev.exists(BATTLE, 0.90)


def _result_shown(dev: Device) -> bool:
    """是不是停在结算页。三种信号任一命中即可 —— 每期换皮/换位置都不能只靠一个：

    · Result 大字（半透明浮层时会暗到 0.59，阈值只能放 0.88）
    · 整块 [关闭] 按钮（本期皮肤 1.00，换皮后可能掉）
    · 只含「关闭」二字的小模板（**每期都在、位置随便变**，最可靠）
    """
    dev.screen(True)
    return (dev.exists(RESULT_TITLE, 0.88)
            or dev.exists(RESULT_CLOSE, 0.86)
            or dev.exists(CLOSE_WORD, 0.82))


def _find_close(dev: Device):
    """在结算页上找 [关闭] 的位置，供点击用。

    优先用整块按钮模板（点最正中）；认不出就用「关闭」文字模板 —— 文字每期不变，
    且 Match 自带位置，页脚换位置也能点准。两个都没有返回 None。
    """
    dev.screen(True)
    m = dev.find(RESULT_CLOSE, 0.86)
    if m is None:
        m = dev.find(CLOSE_WORD, 0.82)
    return m


def _badge(dev: Device):
    """列表里第一条还带感叹号的卡片；没有就返回 None。

    **能不能打只看感叹号**：每条 BOSS 右上角那个粉色圆形感叹号 = 今天还有次数，
    次数用完它就消失。列表标题、BOSS 名字每期都换（这期是「圣章鱼型魔兽」，
    上期叫「异策战」），一律不认。所以循环到找不到感叹号就是真的打完了。
    """
    dev.screen(True)
    return _find_in(dev, BADGE, BADGE_REGION, 0.92)


def _badge_count(dev: Device) -> int:
    """列表里带感叹号的条目数（只统计卡片区，用来在日志里报「几个能打」）。"""
    dev.screen(True)
    hits = dev.find_all(BADGE, 0.92)
    box = _region(dev, BADGE_REGION)
    return sum(1 for m in hits if box[0] <= m.cx <= box[0] + box[2]
               and box[1] <= m.cy <= box[1] + box[3])


# ---------------------------------------------------------------- 防误购
def cancel_buy(dev: Device) -> bool:
    """「购买挑战次数」弹窗（晶石×50）出现就点[取消]。

    这个弹窗在次数用完时点 [SKIP] 或 [挑战] 都会跳出来。**确定键（右边那个红的）
    任何情况都不点** —— 点了就是花 50 晶石。找不到[取消]模板就按左半屏的固定位置
    点一下，宁可多点两次也不能点错方向。
    """
    if not dev.exists(BUY_DIALOG, 0.85):
        return False
    dev.log.warning("  出现「购买挑战次数」弹窗 —— 不买，点[取消]")
    dev.notify("挑战次数已用完，游戏弹出「购买挑战次数」，已取消（没有花晶石）")
    m = dev.find(BUY_CANCEL, 0.88)
    if m is not None:
        x, y = m.cx, m.cy
    else:
        x, y = _xy(dev, *BUY_CANCEL_PT)
    dev.tap(x, y)
    time.sleep(1.0)
    dev.screen(True)
    return True


# ---------------------------------------------------------------- 入口
def enter_list(dev: Device) -> None:
    """主页 → 冒险 → 期间限定 → 异策战。"""
    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在弹窗里")
    if not dev.tap_img("tab_adventure", timeout=10, threshold=0.86):
        raise NotFound("底部导航没找到[冒险]")
    dev.wait_stable(timeout=5)
    if not dev.tap_img("btn_limited_time", timeout=10, threshold=0.88):
        dev.notify("冒险页没找到[期间限定]入口 —— 可能这次活动已经结束了")
        raise Skipped("期间限定里没有入口")
    dev.wait_stable(timeout=3)
    if not dev.tap_img("btn_yice", timeout=10, threshold=0.88):
        dev.notify("「期间限定」弹窗里没找到[异策战]，这次可能只开了竞击战")
        raise Skipped("期间限定里没有异策战")
    # 等页面出现 —— 用排行榜锚点 + 感叹号，不看会换的标题。
    # 感叹号最多等 20s（活动页加载慢）；只认感叹号也行：它出现就一定在列表页。
    t0 = time.time()
    while time.time() - t0 < 20:
        dev.check_cancel()
        dev.screen(True)
        if on_list(dev):
            break
        time.sleep(0.5)
    else:
        dev.dump("yice_no_list")
        dev.notify("进了活动页但没认出「排行榜/感叹号」（现场截图在 logs/shots）")
        raise NotFound("活动页没认出来（标题每期会换，现在改认排行榜和感叹号）")
    dev.wait_stable(timeout=2)


def back_to_list(dev: Device) -> bool:
    """从详情页 / 结算页退回列表。战斗中绝不调用 —— 返回键会弹游戏菜单。"""
    for _ in range(4):
        cancel_buy(dev)          # 有购买弹窗先关掉，不然点什么都点在它上面
        if on_list(dev):
            return True
        if _result_shown(dev):
            close_results(dev)
            continue
        if dev.tap_img("btn_close_detail", timeout=3, threshold=0.92):
            time.sleep(1.0)
            dev.screen(True)
            if on_list(dev):
                return True
        # 结算页可能比战斗结束晚近 60s 才渲染（11-21 那场就是）。这里若连续
        # 按返回都回不去，多半是正卡在还没画出来的结算转场里 —— 别把返回键按到底，
        # 给结算页几次专门的等待机会，一旦出现就关掉。
        if _ >= 1:
            before = time.time()
            while time.time() - before < 20:
                dev.check_cancel()
                dev.screen(True)
                if _result_shown(dev):
                    close_results(dev)
                    break
                if on_list(dev):
                    return True
                time.sleep(0.6)
        dev.back()
        time.sleep(1.0)
    return on_list(dev)


# ---------------------------------------------------------------- 结算
def close_results(dev: Device, settle_wait: float = 45.0) -> int:
    """把 [统计] / [获得道具] 这些 Result 页一路关掉，返回关了几个。

    两个关键修复（之前战斗结束后关不掉、卡死、连锁失败就出在这）：

    1. **会等结算页出现**。战斗结束 → Result 渲染出来之间有一段转场/淡入（几秒），
       旧代码进来只检查一眼、没有就立刻 break，等于永远关不掉。现在给 settle_wait
       秒窗口，期间反复认「Result 大字 / 关闭按钮 / 关闭文字」任一信号。
    2. **按找到的位置点，不用固定坐标、也不只认整块按钮皮肤**。[关闭] 的落点由
       `_find_close()` 现找（整块按钮优先，认不出用「关闭」二字模板），活动每期换皮、
       按钮换位置都能点中。
    """
    n = 0
    # 短路：如果已经站在列表页（战斗直接回列表 / 本来就没结算），不用等满 12 秒。
    # 但结算信号还在时绝不短路 —— 半透明浮层叠在列表/详情上时 on_list 可能为真，
    # 此时若短路退出，浮层就一直关不掉，后面全被它挡住。
    dev.screen(True)
    if on_list(dev) and not _result_shown(dev):
        return 0
    # 第一页：可以等一会儿（转场慢）。之后每页：等得短一点。
    for page in range(6):
        wait = settle_wait if page == 0 else 6.0
        deadline = time.time() + wait
        m = None
        while time.time() < deadline:
            dev.check_cancel()
            dev.screen(True)
            if not _result_shown(dev):
                time.sleep(0.5)
                continue
            m = _find_close(dev)
            if m is not None:
                break
            # 认出结算页但[关闭]还没清晰（正在淡入）——再等
            time.sleep(0.5)
        if m is None:
            # 没等到结算页：第一行是「本来就没有结算」，正常退出，不报错。
            if page == 0:
                dev.log.info("  没有结算页，不用关")
            else:
                dev.log.warning("  下一页结算没等到，先停手（避免乱点）")
            break
        dev.log.info(f"  结算页 → 点[关闭] @({m.cx},{m.cy})")
        dev.tap(m.cx, m.cy)
        n += 1
        time.sleep(1.2)
        dev.screen(True)
    if n:
        dev.log.info(f"  已关掉 {n} 页结算")
    return n


def wait_battle(dev: Device) -> str:
    """等这一场打完。返回 started / no_start / timeout。

    先等 TIME 计时器出现（加载 3~8 秒），再等它消失。打完之后可能先出
    CONNECTING 再出 Result，所以「不在战斗」之后再给结算一点时间。
    """
    t0 = time.time()
    started = False
    while time.time() - t0 < 40:
        dev.check_cancel()
        # 战斗可能几秒就打完（高练度/BOSS 血少），HUD 一闪而过 —— 0.5 秒一轮的
        # 轮询根本抓不到。等抓到 HUD 才认 started 的话，40 秒后会误报「挑战没
        # 开始」，但其实 Result 结算页早就在眼前了（11-24 11:52 那场：点挑战
        # 41 秒后报 no_start，现场截图却是完整的 Result 结算页）。
        # 所以：一看到结算页就直接当战斗已打完返回，别再等 HUD、别误报。
        if _result_shown(dev):
            dev.log.info("  战斗已结束（结算页已经出来了）")
            return "result"
        if _in_battle(dev):
            started = True
            break
        time.sleep(0.5)
    if not started:
        return "no_start"

    dev.log.info("  已进入战斗，等它自己打完（最多 7 分钟）")
    deadline = time.time() + BATTLE_LIMIT
    while time.time() < deadline:
        dev.check_cancel()
        if _result_shown(dev) or on_list(dev):
            return "result"
        if not _in_battle(dev):
            time.sleep(1.2)          # 转场会闪一下，稳住再确认
            dev.screen(True)
            if not _in_battle(dev):
                break
        time.sleep(1.0)
    else:
        return "timeout"

    # 战斗结束到 Result 之间还有一段 CONNECTING/黑屏，别在这时候去点任何东西。
    # 关键：转场那一瞬间排行榜(排行榜在列表和详情页都有)可能闪一下就被 `on_list`
    # 当成「已经回列表」→ 提前返回 → close_results 只等 12s 就放弃 → 结算页其实
    # 还要近 60s 才渲染 → 之后按返回键对结算页无效 → 整场卡死（11-21 03:34 那场）。
    # 所以这里要求「在列表」连续稳定多次才算数，一次闪烁不算。
    t1 = time.time()
    list_streak = 0
    while time.time() - t1 < 120:
        dev.check_cancel()
        if _result_shown(dev):
            return "result"
        if on_list(dev):
            list_streak += 1
            if list_streak >= 4:            # 连续 4 轮(约 2s)都在列表，才算真回了列表
                return "list"
        else:
            list_streak = 0
        time.sleep(0.5)
    return "result"


# ---------------------------------------------------------------- 打一场
def challenge_once(dev: Device) -> None:
    """详情页 → [挑战] → 编队页 → [挑战] → 打完 → 关掉结算。"""
    dev.log.info("  详情页点[挑战]，进编队")
    x, y = _xy(dev, *DETAIL_CHALLENGE)
    dev.tap(x, y)
    # 次数刚好用完时这里也会弹「购买挑战次数」
    t0 = time.time()
    while time.time() - t0 < 25:
        dev.check_cancel()
        if cancel_buy(dev):
            raise Skipped("挑战次数用完了（游戏弹了购买弹窗，已取消）")
        if dev.exists("title_party", 0.85):
            break
        time.sleep(0.5)
    dev.wait("title_party", timeout=10)
    dev.wait_stable(timeout=5)
    if not dev.tap_img("btn_challenge", timeout=8, threshold=0.88):
        raise NotFound("编队页没找到[挑战]")
    why = wait_battle(dev)
    if why == "no_start":
        dev.dump("yice_battle_not_start")
        dev.notify("点了[挑战]但没进战斗（现场截图在 logs/shots），先跳过这个")
        raise Skipped("挑战没开始")
    if why == "timeout":
        dev.dump("yice_battle_timeout")
        dev.notify(f"这场打了 {BATTLE_LIMIT:.0f} 秒还没结束，先跳过，麻烦你看一眼")
        raise Skipped("战斗超时")
    dev.log.info("  战斗结束")
    close_results(dev)


def skip_once(dev: Device) -> bool:
    """点 [SKIP] 用当日最高分瞬间完成一次。返回是否真的出了结算。

    次数已经用完时游戏会弹「购买挑战次数」而不是结算 —— 那种情况按「没打成」处理。
    """
    m = has_skip(dev)
    if m is None:
        return False
    dev.log.info(f"  点[SKIP] @({m.cx},{m.cy})")
    dev.tap(m.cx, m.cy)
    t0 = time.time()
    while time.time() - t0 < 15:
        dev.check_cancel()
        if cancel_buy(dev):
            return False
        # SKIP 出的结算是**半透明浮层叠在详情页上**（底下还能看见 SKIP/挑战按钮），
        # Result 大字被压暗（这期只有 0.59，过不了 0.88），但[关闭]按钮仍是 0.95。
        # 所以两种都要认，不能只靠标题 —— 上次就是只认标题，误判「没出结算、次数用完」，
        # 其实挑战次数还剩 1/3，白白收工。
        dev.screen(True)
        if _result_shown(dev):
            close_results(dev)
            return True
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------- 任务
def job_yice(dev: Device) -> None:
    enter_list(dev)
    n_badge = _badge_count(dev)
    dev.log.info(f"  已进入活动列表，{n_badge} 个条目带感叹号（= 今天还能打）" if n_badge
                 else "  已进入活动列表，没有条目带感叹号")

    done = 0
    for _ in range(MAX_ROUNDS):
        dev.check_cancel()
        if not on_list(dev) and not back_to_list(dev):
            raise NotFound("回不到异策战列表")

        b = _badge(dev)
        # 没感叹号也不能看一眼就信：浮层/转场遮挡时 _badge 会假阴性。
        # 先确认真的站在干净的列表页，再连续回读 2 次都没有，才算真打完。
        if b is None:
            if not back_to_list(dev):
                dev.log.warning("  找不到感叹号，但也回不到列表，先收工")
                break
            gone = 0
            for _ in range(3):
                dev.check_cancel()
                dev.screen(True)
                if on_list(dev) and _badge(dev) is None:
                    gone += 1
                    if gone >= 2:
                        break
                time.sleep(0.8)
            if gone >= 2:
                dev.log.info("  列表里已经没有带感叹号的条目了（回读确认 2 次）")
                break
            dev.log.warning("  刚才感叹号是假阴性（浮层/转场遮挡），继续找")
            b = _badge(dev)
            if b is None:
                dev.log.info("  复查仍没有感叹号，收工")
                break

        dev.log.info(f"  条目 @({b.cx},{b.cy}) 还有次数，进去打")
        x, y = _xy(dev, b.cx + CARD_OFF[0], b.cy + CARD_OFF[1])
        dev.tap(x, y)
        # 等详情页出现 —— 认右下角[关闭]按钮，不用会换名的"剩余挑战次数"那行
        t0 = time.time()
        while time.time() - t0 < 15:
            dev.check_cancel()
            dev.screen(True)
            if on_detail(dev):
                break
            time.sleep(0.5)
        else:
            dev.dump("yice_no_detail")
            dev.notify("点了条目但没进详情页（现场截图在 logs/shots）")
            raise Skipped("进不去异策战详情页")
        dev.wait_stable(timeout=2)

        if has_skip(dev) is not None:
            # 当天已经通关过一次，游戏给了 [SKIP]
            if not skip_once(dev):
                # 没认出结算也不急着收工：先尽力把可能的结算页关掉、退回列表，
                # 再看列表里还有没有感叹号决定继续还是停。上次这里直接 break，
                # 结果还剩 1/3 次没打就收工了。
                dev.dump("yice_skip_no_result")
                close_results(dev)
                if not back_to_list(dev):
                    dev.log.warning("  SKIP 后回不到列表，收工")
                    break
                if _badge(dev) is None:
                    dev.log.info("  SKIP 后列表里已经没有感叹号了，收工")
                    break
                dev.log.info("  SKIP 没认出结算，但列表还有感叹号，继续打")
                continue
        else:
            challenge_once(dev)

        done += 1
        dev.log.info(f"  第 {done} 次完成")
        if not back_to_list(dev):
            dev.log.warning("  没能回到列表，下一轮再试")

    if not done:
        if not ensure_home(dev):
            raise NotFound("回不到主页")
        raise Skipped("今天没有可打的异策战（列表里没有感叹号）")

    dev.log.info(f"  本轮共完成 {done} 次异策战")
    if not ensure_home(dev):
        raise NotFound("打完了但回不到主页")
