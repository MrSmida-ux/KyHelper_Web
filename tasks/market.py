"""市场免费礼物领取：主页 → 商店 → 市场 → 期间限定 → 免费礼物 → 领取 → 主页

实测到的市场规则（2560x1440）：
- 主页右下「商店 SHOP」入口，进去后点「市场」
- 市场顶部四个标签：PASS / 礼包 / 晶石 / 期间限定；免费礼物在「期间限定」
- 期间限定带红色「!」角标 = 有可领的东西；「每日3点更新」角标是那只礼物的标志
- 免费礼物卡片：底部米色条写着「免费」，红色字「限购0/1」；领完变成「限购1/1」
  且米色条消失 —— 所以判据用「免费」这张整卡按钮模板：领前 1.000、领后 0.52
- 一天只能领一次；领完再跑应跳过（没有「免费」按钮），不报错

阈值说明：
- 弹窗沿用派遣任务的 title_gift / btn_gift_confirm（同一个「礼物 / 已领取下列物品」弹窗）
- btn_free_gift 领后仍有 0.52 的底噪，所以卡 0.85 才不会误判成还能领
"""
from __future__ import annotations

import time

from core.device import Device, NotFound, Skipped
from core.flows import ensure_home

REF_W = 2560
FREE_THRESHOLD = 0.85


def _xy(dev: Device, x: int, y: int) -> tuple[int, int]:
    w = int(dev.screen().shape[1])
    k = w / REF_W
    return int(x * k), int(y * k)


def _enter_shop(dev: Device, tries: int = 3) -> None:
    """主页 → 商店，并且**确认真的进去了**。

    只「找到并点了一下」不算数：窗口被最小化 / 切到后台时这一下会被游戏吞掉，
    人还站在主页上（10:39 那次就是这样 —— 点了[商店]、窗口被最小化、9 秒后
    还在主页，报「商店页没找到 [市场] 入口」）。所以点完要等页面真的换掉。
    """
    from core.flows import is_home

    for i in range(max(1, tries)):
        dev.check_cancel()
        if not dev.tap_img("btn_shop_entry", timeout=8, threshold=0.75):
            raise NotFound("主页没找到 [商店] 按钮")
        end = time.time() + 8
        while time.time() < end:
            dev.check_cancel()
            if dev.matcher.has("btn_market") and dev.exists("btn_market", 0.78):
                return
            if not is_home(dev):
                # 已经离开主页了，多半就在商店里，只是 [市场] 这个图标还没认出来
                dev.log.info("  已离开主页，按进商店继续")
                return
            time.sleep(0.4)
        dev.log.warning(f"  点了[商店]还停在主页（第 {i + 1} 次），再点一次")
        dev.wait_stable(timeout=2)
    raise NotFound("点了 [商店] 但没进商店页（画面一直停在主页）")


def open_market(dev: Device) -> None:
    """主页 → 商店 → 市场 → 期间限定。"""
    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在某个弹窗里")

    dev.log.info("  进入商店")
    _enter_shop(dev)

    # 点「市场」和点「商店」一样会被吞：只「找到并点了一下」不算数，必须回读
    # 确认市场页真的加载出来（tab_limited 标签栏 / btn_free_gift）。
    # 11:40 那次 btn_market 相似度 0.992、点了却还停在商店首页（失败截图
    # 114044_market_no_free.png 里「市场/交换所」两个大图标都在），后面一路
    # 判成「没找到免费礼物（可能领过了）」—— 根因就是缺这个确认和重试。
    dev.log.info("  打开市场")
    entered = False
    for i in range(3):
        dev.check_cancel()
        if not dev.tap_img("btn_market", timeout=8, threshold=0.80):
            # 商店页刚打开时图标还在淡入，再给一次机会（阈值也放宽一点）
            dev.wait_stable(timeout=3)
            if not dev.tap_img("btn_market", timeout=6, threshold=0.75):
                shot = dev.dump("market_no_entry")
                raise NotFound("商店页没找到 [市场] 入口" + (f"（现场：{shot}）" if shot else ""))
        # 等市场页的标签栏真正出现，才算点进去了
        if _wait_any(dev, ("tab_limited", "btn_free_gift"), 8, 0.82):
            entered = True
            break
        dev.log.warning(f"  点了[市场]还没看到市场页标签栏（第 {i + 1} 次），再点一次")
        dev.wait_stable(timeout=2)
    if not entered:
        shot = dev.dump("market_no_enter") if dev.save_on_fail else ""
        dev.log.warning("  点了 3 次[市场]仍没进市场页"
                        + (f"（现场：{shot}）" if shot else ""))

    dev.wait_stable(timeout=4)

    # 市场页默认就停在「期间限定」，但如果上次停在其他标签，主动点过去
    if dev.matcher.has("tab_limited") and dev.exists("tab_limited", 0.85):
        if not dev.exists("btn_free_gift", FREE_THRESHOLD):
            dev.log.info("  切到「期间限定」")
            dev.tap_img("tab_limited", timeout=5, threshold=0.85)
    dev.wait_stable(timeout=4)


def _wait_any(dev: Device, names, timeout: float, threshold: float) -> bool:
    """等这几个模板任意一个出现（轮询，可被打断）。"""
    end = time.time() + timeout
    while time.time() < end:
        dev.check_cancel()
        for n in names:
            if dev.matcher.has(n) and dev.exists(n, threshold):
                return True
        time.sleep(0.3)
    return False


def find_free_gift(dev: Device, scrolls: int = 3):
    """在「期间限定」货架里找「免费」那条，返回 Match 或 None。

    免费礼物不一定在第一屏（货架会上下滚），所以没看到就往下翻几屏再找，
    找完滚回顶部，别给后面的任务留个半截页面。
    """
    hit = dev.find("btn_free_gift", FREE_THRESHOLD)
    if hit is not None:
        return hit
    for _ in range(scrolls):
        dev.check_cancel()
        dev.scroll("down", 0.55)
        dev.wait_stable(timeout=2.5)
        hit = dev.find("btn_free_gift", FREE_THRESHOLD)
        if hit is not None:
            return hit
    # 翻到底也没有：滚回顶部（列表滚过头会影响下一次进来时的判断）
    for _ in range(scrolls):
        dev.scroll("up", 0.55)
        time.sleep(0.4)
    dev.wait_stable(timeout=2)
    return dev.find("btn_free_gift", FREE_THRESHOLD)


def _cancel_purchase(dev: Device) -> bool:
    """误点到要花钱的商品时，把购买框取消掉（绝不点[确定]）。

    放在这里是因为「免费」模板和普通商品的米色价格条长得很像，万一看走眼
    点到了 60 晶石那种礼包，必须先撤回来，不能真的买。
    """
    for name in ("dlg_buy_attempts", "btn_buy_cancel", "btn_dialog_no"):
        if not dev.matcher.has(name):
            continue
        if not dev.exists(name, 0.85):
            continue
        dev.log.warning(f"  弹出购买框 [{name}] —— 不买，点[取消]")
        dev.notify("市场点到了要花晶石的商品，已取消（未扣晶石）")
        if dev.tap_img("btn_buy_cancel", timeout=3, threshold=0.85):
            time.sleep(0.8)
            return True
        if dev.tap_img("btn_dialog_no", timeout=2, threshold=0.85):
            time.sleep(0.8)
            return True
        # 兜底：左半屏那个取消位（右边红色的确定无论如何都不点）
        x, y = _xy(dev, 1056, 1150)
        dev.tap(x, y)
        time.sleep(0.8)
        return True
    return False


def claim_free(dev: Device) -> None:
    """点免费礼物，把「礼物 / 已领取下列物品」弹窗确认掉。"""
    if not dev.tap_img("btn_free_gift", timeout=6, threshold=FREE_THRESHOLD):
        raise NotFound("期间限定里没找到 [免费] 礼物")

    for i in range(10):
        dev.check_cancel()
        # 市场用的是和派遣同一个「礼物」弹窗，优先用那张实战验证过的模板
        if dev.exists("btn_gift_confirm", 0.85):
            dev.log.info("  礼物弹窗 → 确认")
            dev.tap_img("btn_gift_confirm", timeout=3, threshold=0.85)
            time.sleep(1.2)
            return
        # 兜底：用户实拍的市场礼物确认按钮（截图分辨率不同时的保险）
        if dev.matcher.has("btn_market_gift_ok") and dev.exists("btn_market_gift_ok", 0.80):
            dev.log.info("  礼物弹窗 → 确认（市场实拍模板）")
            dev.tap_img("btn_market_gift_ok", timeout=3, threshold=0.80)
            time.sleep(1.2)
            return
        # 点到的不是免费的（弹了购买框）→ 立刻取消，别留着框挡后面的任务
        if i >= 2 and _cancel_purchase(dev):
            raise Skipped("市场点到的不是免费礼物（弹了购买框），已取消")
        # 有些时候弹窗会直接跳过（比如网络慢），回到市场页也算领完
        if dev.exists("tab_limited", 0.85) and not dev.exists("title_gift", 0.80):
            return
        time.sleep(0.6)

    dev.log.warning("  领奖弹窗没等到确认按钮，继续往下走")


def job_market_free(dev: Device) -> None:
    """领取市场「期间限定」的每日免费礼物（一天一次）。"""
    open_market(dev)

    hit = find_free_gift(dev)
    if hit is None:
        shot = dev.dump("market_no_free") if dev.save_on_fail else ""
        dev.log.info("  货架上没找到「免费」（可能已经领过了）"
                     + (f"（现场：{shot}）" if shot else ""))
        dev.notify("市场免费礼物：这次没找到「免费」卡片，可能今天已经领过")
        raise Skipped("市场没找到可领的免费礼物")

    dev.log.info(f"  找到免费礼物 @({hit.cx},{hit.cy})，领取")
    claim_free(dev)

    if ensure_home(dev):
        dev.log.info("  已返回主页")
    else:
        dev.log.warning("  没能确认回到主页")


MARKET_NEEDS = [
    "btn_shop_entry", "btn_market", "tab_limited", "btn_free_gift",
    "badge_daily", "title_gift", "btn_gift_confirm", "btn_market_gift_ok", "tab_home",
    # 误点到要花钱的商品时的撤单模板
    "dlg_buy_attempts", "btn_buy_cancel", "btn_dialog_no",
]
