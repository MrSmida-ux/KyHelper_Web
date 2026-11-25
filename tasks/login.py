"""启动弹窗：标题页 → GAME START → 清掉登录奖励等一连串弹窗。

每天第一次进游戏时会连续弹好几个东西，实测顺序是：

    活动登录奖励（GET 盖章动画 → 右上角 NEXT）× 3 个
    → 回归登入奖励（底部米色「关闭」）
    → 游戏公告（底部橙色「关闭」）
    → 活动一览（同样是底部橙色「关闭」）
    → 主页

数量和顺序都可能变（公告只在有更新时弹，回归奖励只在久未登录时弹），
所以不写死步骤：看到 NEXT 就点 NEXT，看到「关闭」就点「关闭」，都没有就点空白处
（这游戏的弹窗大多「点哪儿都能继续」），直到认出主页为止。
"""
from __future__ import annotations

import time

from core.device import Device, NotFound
from core.flows import ensure_home, is_home

# 标题页的 GAME START：文字本身有呼吸/发光动画，模板匹配得分会在 0.51~0.99 之间跳，
# 没法当模板用，所以位置写死（基准 2560×1440），靠静态的 title_logout 判断"在不在标题页"。
GAME_START = (1305, 1175)

# 没有按钮的弹窗点这里：屏幕上方偏中的空白处。主页上这个点也不会触发任何功能
# （顶部 AP/BP 数值本来点不动），万一误点也不会把人带跑。
BLANK = (1280, 150)

# 各种「关闭」按钮：米色药丸（登入奖励面板）与橙色圆钮（公告/活动一览）
CLOSES = ("btn_announce_close", "btn_login_close")
NEXT = "btn_login_next"

# 清弹窗阶段的兜底：连续这么多轮都没东西可点，就当这是个没有按钮的弹窗，点一下空白
BLANK_AFTER = 8


def on_title(dev: Device) -> bool:
    """在标题页（右上角「登出」只在标题页出现，且不会动）。"""
    return dev.matcher.has("title_logout") and dev.exists("title_logout", 0.90)


def popup_button(dev: Device) -> str:
    """当前这一屏有没有可点的弹窗按钮，返回模板名（没有返回空串）。"""
    for name in (NEXT, *CLOSES):
        if dev.matcher.has(name) and dev.exists(name, 0.86):
            return name
    return ""


def wait_first_screen(dev: Device, limit: float = 100) -> None:
    """GAME START 之后等加载结束：要么冒出弹窗，要么真的停在主页。

    注意加载完是**先进主页、隔一秒多才弹登录奖励**，所以看到主页不能马上收工，
    得再等一小会儿看有没有弹窗压上来。
    """
    t0 = time.time()
    while time.time() - t0 < limit:
        dev.check_cancel()
        if popup_button(dev):
            dev.log.info("  加载完成，开始清启动弹窗")
            return
        if is_home(dev):
            time.sleep(3.0)
            if popup_button(dev):
                dev.log.info("  加载完成，开始清启动弹窗")
                return
            dev.log.info("  直接进到主页（今天可能没有新的登录奖励）")
            return
        time.sleep(1.0)
    raise NotFound(f"点了 GAME START 后 {limit:.0f}s 还没进到游戏里")


def home_really(dev: Device, settle: float = 3.0) -> bool:
    """认主页要等一下：弹窗常常比主页晚一秒多才出来。"""
    if not is_home(dev):
        return False
    t0 = time.time()
    while time.time() - t0 < settle:
        dev.check_cancel()
        if popup_button(dev):
            return False
        time.sleep(0.6)
    return is_home(dev)


def clear_popups(dev: Device, limit: float = 240) -> int:
    """反复点，直到认出主页。返回点掉了几次按钮（不是弹窗个数，仅用于日志）。"""
    t0 = time.time()
    taps = 0
    idle = 0
    while time.time() - t0 < limit:
        dev.check_cancel()
        if home_really(dev):
            return taps
        if on_title(dev):                       # 意外退回标题页，别在这儿死磕
            raise NotFound("清弹窗时退回了标题页")

        if dev.matcher.has(NEXT) and dev.tap_img(NEXT, timeout=1.0, threshold=0.86):
            taps += 1
            idle = 0
        else:
            for name in CLOSES:
                if dev.matcher.has(name) and dev.tap_img(name, timeout=0.6, threshold=0.88):
                    taps += 1
                    idle = 0
                    time.sleep(0.6)             # 等页面转场，别在动画里连点
                    break
            else:
                idle += 1
                if idle >= BLANK_AFTER:
                    dev.log.info("  这一屏没有可点的按钮，点一下空白处继续")
                    dev.tap(*BLANK)
                    taps += 1
                    idle = 0
        time.sleep(0.7)
    shot = dev.dump("login_popups") if dev.save_on_fail else ""
    raise NotFound(f"清了 {limit:.0f}s 还没回到主页" + (f"（现场：{shot}）" if shot else ""))


def job_login_bonus(dev: Device) -> None:
    """登录奖励领取：标题页点 GAME START，把启动弹窗一路清到主页。

    幂等：今天领过了，游戏就只弹公告/活动，清完照样到主页，不会报错。
    """
    if on_title(dev):
        dev.log.info("  在标题页，点 GAME START")
        dev.tap(*GAME_START)
        wait_first_screen(dev)
    elif popup_button(dev):
        dev.log.info("  界面上已经压着弹窗，直接清")
    elif is_home(dev) and home_really(dev):
        dev.log.info("  已经在游戏里（不是标题页），没有启动弹窗要清")
        return
    else:
        # 别的界面（游戏没开、刚连上还在渲染、或上次没退回主页）。
        # 别急着按返回键：公告/登录奖励这类弹窗经常**晚几秒**才盖上来，
        # 一眼没看到就 ensure_home，会被随后弹出的公告挡住、返回键又关不掉 →
        # 直接判失败（11-21 实测判定后 21s 截图里公告才清清楚楚，得分 0.996）。
        # 先等一会儿，期间冒出弹窗就走下面的清弹窗流程；真没有再回主页。
        dev.log.warning("  既不在标题页也不在主页，先等弹窗/主页渲染，别急着回主页")
        t0 = time.time()
        while time.time() - t0 < 25:
            dev.check_cancel()
            if on_title(dev):
                dev.log.info("  等到了标题页，点 GAME START")
                dev.tap(*GAME_START)
                wait_first_screen(dev)
                break
            if popup_button(dev):
                dev.log.info("  等到了启动弹窗，直接清")
                break
            if is_home(dev) and home_really(dev):
                dev.log.info("  等到了主页（今天可能没有启动弹窗）")
                return
            time.sleep(1.0)
        else:
            # 等满 25s 仍然是"说不清什么界面"，才按老办法回主页
            dev.log.warning("  等了 25s 仍没等到弹窗/主页，尝试回主页")
            if not ensure_home(dev):
                shot = dev.dump("login_not_home") if dev.save_on_fail else ""
                raise NotFound("既不在标题页也不在主页（游戏可能没开，或卡在某个弹窗里）"
                               + (f"（现场：{shot}）" if shot else ""))
            return

    n = clear_popups(dev)
    dev.log.info(f"  启动弹窗处理完（点了 {n} 次），确认主页")
    if not ensure_home(dev):
        raise NotFound("弹窗清完了但没回到主页")


def login_needs() -> list[str]:
    return ["title_logout", NEXT, *CLOSES, "btn_tokuten", "home_flag", "icon_rescue"]
