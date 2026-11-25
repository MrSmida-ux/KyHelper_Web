"""通用流程工具：回主页、关弹窗。

这些动作内置任务和录制任务都要用，单独放一层，避免 tasks/daily.py 和
core/recipe.py 互相 import。
"""
from __future__ import annotations

import time

from .device import Device


def home_template(dev: Device) -> str:
    return str(dev.cfg.get("game", {}).get("home_template", "") or "")


# 主页备胎锚点。主锚点「特典」按钮是会被挂红点角标的 —— 一旦有未读公告/
# 活动，按钮右上角多个「!」，相似度就从 1.0 掉到 0.82，按 0.85 判就认不出来了。
# 这两个是主页右侧功能区的图标和右上角的「1/1」横幅角标，平时不会被角标盖住。
HOME_FALLBACKS = ("home_flag", "icon_rescue")


def is_home(dev: Device) -> bool:
    tpl = home_template(dev)
    if tpl and dev.matcher.has(tpl) and dev.exists(tpl, 0.80):
        return True
    for name in HOME_FALLBACKS:
        if dev.matcher.has(name) and dev.exists(name, 0.85):
            return True
    return False


def cancel_ap_refill(dev: Device) -> bool:
    """体力不足时游戏会弹「AP回复」框：是否花 晶石×100 换 AP×100。

    **绝不购买**——只点[取消]，任何情况下都不碰右边红色的[确定]。
    双重保险：先认「AP回复」标题确认是这个弹窗，再要求取消按钮落在左半屏
    （即使模板误匹配到右侧[确定]，cx 也会 >= 0.55 屏宽而被挡下）；找不到取消就按返回键。
    返回 True 表示确实处理了这个购买框。
    """
    title = "title_ap_refill"
    if not dev.matcher.has(title) or not dev.exists(title, 0.88):
        return False
    dev.log.warning("  体力不足，弹出[AP回复]购买框 → 点[取消]，不花晶石")
    screen = dev.screen(True)
    w = screen.shape[1]
    hit = None
    if dev.matcher.has("btn_ap_refill_cancel"):
        hit = dev.matcher.find(screen, "btn_ap_refill_cancel", threshold=0.85)
    if hit is not None and hit.cx < w * 0.55:
        dev.tap(hit.cx, hit.cy)
    else:
        dev.log.warning("  没定位到[取消]，只按返回键（不点确定）")
        dev.back()
    dev.wait_stable(timeout=2)
    return True


# 召唤结果页右下角[关闭]的位置（基准 2560×1440）。抽卡流程万一没走完，这个弹窗
# 会盖住整个画面，之后点哪都没反应、每个任务都在同一处撞死 —— 用户遇到的
# 「免费召唤完不点关闭，接着一个接一个报错」就是这么来的。所以放进公共流程里兜底。
SUMMON_CLOSE_ZONE = (1880, 1160, 660, 280)


def close_summon_result(dev: Device, rounds: int = 2) -> bool:
    """屏幕上如果有「召唤结果」弹窗，点右下角[关闭]把它关掉。

    返回 True 表示确实关掉了一个。没有这个模板的模板包会自动跳过。
    只认 [关闭] 这个按钮本身（不认「再召唤」），绝不重复抽卡。
    """
    if not dev.matcher.has("btn_summon_close"):
        return False
    closed = False
    for _ in range(max(1, rounds)):
        dev.screen(True)
        m = dev.matcher.find(dev.screen(), "btn_summon_close",
                             threshold=0.92, region=SUMMON_CLOSE_ZONE)
        if m is None:
            m = dev.find("btn_summon_close", 0.92)
        if m is None:
            break
        dev.log.info(f"  关掉召唤结果页 @({m.cx},{m.cy})")
        dev.tap(m.cx, m.cy)
        dev.wait_stable(timeout=2.5)
        closed = True
    return closed


def dismiss_announcements(dev: Device, rounds: int = 4) -> int:
    """关掉「游戏公告 / 活动一览」这类维护弹窗（底部橙色圆「关闭」）。

    公告只在有更新时弹，而且常常**比主页晚几秒才渲染**。如果回主页时不先关它，
    它会盖住主页锚点 → 判「不在主页」→ 一直按返回键也关不掉 → 第一个任务就失败，
    等失败截图拍下来时公告反而已经出来了（11-21 登录奖励领取就是这么挂的）。

    只认 btn_announce_close：实测公告页 0.996、纯主页测不到（<0.5），不会误点主页。
    """
    if not dev.matcher.has("btn_announce_close"):
        return 0
    n = 0
    for _ in range(max(1, rounds)):
        dev.screen(True)
        m = dev.find("btn_announce_close", 0.90)
        if m is None:
            break
        dev.log.info(f"  关掉游戏公告弹窗 @({m.cx},{m.cy})")
        dev.tap(m.cx, m.cy)
        dev.wait_stable(timeout=2)
        n += 1
    return n


# 战斗画面左上角的指示器模板。不同活动长得不一样：
#   - battle_hud       = 橙色「WAVE」菱形（主线/疾走讨伐战这类）
#   - battle_time_flag = 橙色「TIME」菱形（异策战SR 这类限时战斗）
# 只认一个会漏：11-21 异界战SR 就是 TIME 样式，旧代码只认 WAVE，
# 战斗没等到就去点下一步的「关闭」，6 秒超时后连按返回也回不了主页。
# 两个模板在结算页/主页都 ≤0.42，不会误认，任一命中即算在战斗中。
BATTLE_HUDS = ("battle_hud", "battle_time_flag")
BATTLE_HUD_THRESHOLD = 0.88


# 战斗结算「Result」页的识别信号 + 关闭按钮。这三类活动（异策战、异界战SR、
# 疾走讨伐战等）打完都停在同一个 Result 页上，[关闭] 按钮位置/皮肤每期可能变，
# 但「Result」大字和「关闭」二字稳定 —— 和异策战任务里用的是同一套模板。
# 结算页会把返回键吃掉：不点[关闭]，连按返回也回不到主页 → 后续任务连锁失败。
RESULT_SIGNALS = ("yice_result_title", "yice_result_close", "yice_close_word")
RESULT_CLOSE_THRESHOLD = 0.86


def _result_shown(dev: Device) -> bool:
    thresholds = {"yice_result_title": 0.88, "yice_result_close": RESULT_CLOSE_THRESHOLD,
                  "yice_close_word": 0.82}
    for tpl in RESULT_SIGNALS:
        if dev.matcher.has(tpl) and dev.exists(tpl, thresholds.get(tpl, 0.86)):
            return True
    return False


def close_result_page(dev: Device, rounds: int = 4, settle_wait: float = 6.0) -> bool:
    """通用兜底：若停在战斗结算 Result 页，点[关闭]关掉它。

    录制任务打完一场战斗后如果停在结算页（下一步的「关闭」没点/没找到），
    ensure_home 按返回键无效，就会卡死。这里按「Result 标题 / 关闭按钮 /
    关闭二字」三信号判定，按找到的按钮位置点，不认就按对的模板位置点，
    绝不硬编码坐标（位置每期变）。返回是否确实关掉过。

    结算页可能在点击后晚几秒才渲染，首屏给 settle_wait 秒等待窗口。
    """
    present = [t for t in RESULT_SIGNALS if dev.matcher.has(t)]
    if not present:
        return False

    closed = False
    for page in range(max(1, rounds)):
        # 首屏等结算渲染（战斗→结算转场慢），后续页等得短一点
        deadline = time.time() + (settle_wait if page == 0 else 4.0)
        shown = False
        while time.time() < deadline:
            dev.check_cancel()
            dev.screen(True)
            if _result_shown(dev):
                shown = True
                break
            # 已经回到干净页面（主页/列表），不用再等
            if is_home(dev):
                return closed
            time.sleep(0.5)
        if not shown:
            break

        # 找到关闭按钮就按它的位置点；三个信号里哪个命中用哪个的位置
        m = None
        for tpl, th in (("yice_result_close", RESULT_CLOSE_THRESHOLD),
                        ("yice_close_word", 0.82)):
            if dev.matcher.has(tpl):
                m = dev.find(tpl, th)
                if m is not None:
                    break
        if m is None:
            # 认出结算但没定位到关闭按钮，留现场别乱点
            dev.dump("result_no_close")
            dev.log.warning("  结算页没找到[关闭]按钮（现场在 logs/fails）")
            break
        dev.log.info(f"  关掉战斗结算 Result 页 @({m.cx},{m.cy})")
        dev.tap(m.cx, m.cy)
        dev.wait_stable(timeout=2.5)
        closed = True
    return closed


def close_reward_page(dev: Device, rounds: int = 3) -> bool:
    """通用兜底：若停在「报酬一览」领奖面板，点底部[关闭]关掉它。

    救援战打完走「挑战结果 → 下一步 → 报酬一览 → 解放 → 关闭」这套流程。
    如果最后那下[关闭]点偏（动画中/按钮边缘），面板会留在屏幕上 —— 它是模态
    面板，返回键关不掉、还盖住主页锚点 → ensure_home 一直按返回也回不去，
    一个任务卡死后全连锁。这里认 title_reward（实测只在报酬页 0.997，
    其它页 ≤0.43），按找到的 btn_close_reward 位置点，并回读确认真消失。
    """
    if not (dev.matcher.has("title_reward") and dev.matcher.has("btn_close_reward")):
        return False
    closed = False
    for _ in range(max(1, rounds)):
        dev.screen(True)
        if not dev.exists("title_reward", 0.88):
            break
        m = dev.find("btn_close_reward", 0.90) or dev.find("btn_close_reward", 0.80)
        if m is None:
            break
        dev.log.info(f"  关掉报酬一览面板 @({m.cx},{m.cy})")
        dev.tap(m.cx, m.cy)
        dev.wait_stable(timeout=2.5)
        closed = True
    return closed


def in_battle(dev: Device) -> bool:
    """是否还在战斗里（左上角 WAVE 或 TIME 指示器，两种活动样式都认）。

    实测：各自的战斗画面 1.000，结算页/详情页/主页 ≤0.42。
    """
    for tpl in BATTLE_HUDS:
        if dev.matcher.has(tpl) and dev.exists(tpl, BATTLE_HUD_THRESHOLD):
            return True
    return False


def wait_battle_end(dev: Device, timeout: float = 600.0) -> bool:
    """等战斗结束再干别的。

    战斗中按安卓返回键（key 4）会弹出游戏菜单 —— 连着按就会把菜单弹两次，
    既打断战斗也容易误触退出。所以战斗没打完就先别导航。

    战斗时长从几秒到三五分钟不等（限时战斗、高难度关更久），超时给 10 分钟，
    全程以 HUD 是否消失为准，绝不中途放弃去按返回键（那会卡在战斗里）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        dev.check_cancel()
        if not in_battle(dev):
            return True
        time.sleep(1.5)
    return not in_battle(dev)


def _settle(dev: Device, seconds: float, rounds: int = 2) -> None:
    """等画面稳定；如果一直在动（转场/加载条），再多给一轮时间。

    云游戏从战斗结算回主页、或者从竞技场退回列表，中间经常有十几秒的加载动画。
    以前 wait_stable 一返回 False 就当「这一步失败了」，结果一个任务失败后，
    后面每个任务都在同一条加载动画上撞死 —— 看着就是「接连报错」。
    """
    for _ in range(max(1, rounds)):
        dev.check_cancel()
        if dev.wait_stable(timeout=seconds):
            return


def ensure_home(dev: Device, tries: int = 5, grace: float = 10.0) -> bool:
    """确保回到主页。

    优先点底部「首页」—— 比按返回键安全，返回键在主页会弹「确认退出游戏」。
    主页锚点必须是「只在主页出现」的元素，底部导航栏不行（它在别的页面也画着）。

    grace：前面几轮都没成功时，最后再干等这么多秒轮询主页锚点。
    切场景/结算动画期间页面在动，点哪里都没用，只能等它自己出来。
    """
    tpl = home_template(dev)
    if not tpl:
        return True
    if not dev.matcher.has(tpl):
        dev.log.warning(f"  主页锚点模板 [{tpl}] 不存在，跳过主页检查")
        return True

    # 战斗中按返回会弹出游戏菜单（连按会弹两次），先等它打完
    if in_battle(dev):
        dev.log.info("  还在战斗中，等打完再回主页")
        wait_battle_end(dev)

    # AP 回复购买框 / 广告弹窗 / 召唤结果页会盖住主页锚点和底部导航，先关掉再判断
    cancel_ap_refill(dev)
    dismiss_ads(dev)
    close_summon_result(dev)
    dismiss_announcements(dev)
    # 战斗结算页会吃掉返回键：不点[关闭]，后面点首页/按返回都没用。
    # 先在这里兜底关掉（录制任务打完战斗停在结算页时就靠它）。
    close_result_page(dev)
    # 救援战「报酬一览」是模态面板，点偏[关闭]后会留着盖住主页 → 同样兜底关掉
    close_reward_page(dev)

    tab = str(dev.cfg.get("game", {}).get("home_tab", "tab_home"))
    for i in range(max(1, tries)):
        dev.check_cancel()
        if is_home(dev):
            return True
        # 半路又冒出召唤结果页（抽卡流程没走完）→ 先关它，比按返回键可靠
        if close_summon_result(dev):
            _settle(dev, 3)
            continue
        # 公告弹窗常常晚几秒才渲染出来，盖住主页锚点 → 关掉它，别白按返回键
        if dismiss_announcements(dev):
            _settle(dev, 3)
            continue
        # 结算页可能在返回过程中才渲染出来 → 关掉它再继续
        if close_result_page(dev, settle_wait=3):
            _settle(dev, 3)
            continue
        # 报酬一览面板可能被动画留着 → 关掉再继续
        if close_reward_page(dev):
            _settle(dev, 3)
            continue
        # 底部导航亮着（节点地图、关卡列表、遗迹页等大多数页面都有）→ 直接点「首页」
        # 一步到位，比一下一下按返回快得多。详情页这类没有导航的才退回按返回键。
        if dev.matcher.has(tab):
            hit = dev.find(tab, 0.82)
            if hit is not None:
                dev.log.info(f"  点底部[首页]（第 {i + 1} 次）")
                dev.tap(hit.cx, hit.cy)
                _settle(dev, 6)
                if is_home(dev):
                    return True
        dev.back()
        _settle(dev, 5)

    # 最后一轮：页面可能还在加载（结算/转场），干等一会儿再判一次
    if grace > 0:
        dev.log.info(f"  还没回到主页，再等 {grace:.0f}s（可能在加载）")
        end = time.time() + grace
        while time.time() < end:
            dev.check_cancel()
            if is_home(dev):
                return True
            time.sleep(0.6)

    ok = is_home(dev)
    if not ok:
        # 留一张现场图：任务接连失败时，这张图能直接看出卡在哪个界面
        try:
            shot = dev.dump("home_fail")
            if shot:
                dev.log.warning(f"  回不到主页，现场截图：{shot}")
        except Exception:
            pass
    return ok


# 「特典」按钮右上角常挂一个红点/感叹号，一挂上相似度就从 1.0 掉到 0.82，
# 用默认阈值 0.85 会直接判「找不到」。点它的时候必须放宽。
TOKUTEN_THRESHOLD = 0.78


def enter_tokuten(dev: Device, timeout: float = 8) -> bool:
    """从主页进特典页（AP 领取、派遣都在这后面）。"""
    if not dev.tap_img("btn_tokuten", timeout=timeout, threshold=TOKUTEN_THRESHOLD):
        return False
    dev.wait_stable(timeout=8)
    return True


def dismiss_ads(dev: Device, rounds: int = 4) -> int:
    """关掉游戏里那些「AP不够吗？」「活动促销」之类的广告弹窗（右上角 X）。

    这类弹窗在任何任务、任何页面都可能突然弹出（尤其 AP 见底时点挑战/扫荡），
    不关掉的话会盖住所有按钮，导航直接失败。所以放在 core 层，所有任务共用。
    返回关掉了几个。

    只认「广告独有标志」（如 ad_ap_low 的『AP不够吗？』文字）才动手，避免
    误点到长得像的正常确认框。关完弹窗要重新截图，否则下一轮用的是旧帧。
    """
    n = 0
    # AP不足/BP不足弹的是同一套促销广告：文案可能是「AP不够吗？」也可能别的，
    # 但都有蓝色[购买]按钮、倒计时、右上角X。用这些通用特征认，别只盯某句文案。
    ad_tpls = [t for t in ("ad_buy", "ad_ap_low", "ad_countdown") if dev.matcher.has(t)]
    if not ad_tpls or not dev.matcher.has("btn_ad_close"):
        return 0
    for _ in range(rounds):
        screen = dev.screen(True)
        is_ad = any(dev.matcher.find(screen, t, threshold=0.82) for t in ad_tpls)
        if not is_ad:
            break
        if dev.tap_img("btn_ad_close", timeout=2, threshold=0.85):
            dev.log.info("  关掉广告弹窗（AP/BP不足/促销）")
            n += 1
            dev.wait_stable(timeout=1.5)
        else:
            break
    return n


def close_popups(dev: Device, rounds: int = 3) -> None:
    """关掉当前界面上能关的弹窗（含 AP 回复购买框、广告）。没做对应模板的会自动跳过。"""
    cancel_ap_refill(dev)
    dismiss_ads(dev)
    for _ in range(rounds):
        screen = dev.screen(True)
        hit = False
        for tpl in ("popup_confirm", "popup_close"):
            if not dev.matcher.has(tpl):
                continue
            m = dev.matcher.find(screen, tpl, threshold=0.82)
            if m:
                dev.log.info(f"  关闭弹窗 [{tpl}]")
                dev.tap_match(m)
                hit = True
                break
        if not hit:
            return
