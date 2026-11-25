"""免费召唤：把每个卡池**每天一次**的免费抽都点掉。

用户描述的流程（也是这个任务的实现顺序）：

    主页 → 召唤 → 左侧 LIST 展开 → 有感叹号的卡池说明可能能免费抽
    → 点进去 → 收起 LIST → 点那个带感叹号的「免费」按钮 → 画符文 / 看动画
    → 关闭召唤结果 → 再展开 LIST 看下一个 → 列表里没感叹号了就回主页

两个关键判断：

1. **感叹号 = 有免费抽**。列表里卡池左侧的粉色圆形感叹号，和详情页底部那个
   免费按钮右上角的感叹号是同一个图标，只是大小不同（列表 80px、按钮 60px）。
   所以做了两个模板，别混用 —— 尺寸差一倍，模板匹配不会命中。
2. **免费按钮位置不固定**。异界召唤是右数第二个，武装召唤是最左边。所以不按
   固定坐标点，而是找按钮上那个感叹号，再往左下偏一点落到按钮中心。

每个卡池当天抽过一次后，感叹号会消失，所以循环到「列表里找不到感叹号」为止，
不用自己记次数 —— 这也是「每天最多一次免费」的天然保证。

3. **不要用标题模板判断「到没到召唤页」**。召唤页的封面/标题每周都换
   （STEPUP、活动、联动轮着来），`title_summon` 只认录制那一期。所以判据
   换成「左边 LIST 能不能展开」—— 这也是紧接着就要做的动作，一次到位。
"""
from __future__ import annotations

import time

from core.device import Device, NotFound, Skipped
from core.flows import ensure_home

REF_W = 2560

# 列表收起时 LIST 页签贴在左边缘；展开后同一个页签跑到右边 —— 两个位置都得记。
# 左边这个页签在不同封面上会上下浮动几十像素，多备两个落点。
LIST_OPEN_PTS = ((60, 340), (52, 400), (60, 450))
LIST_CLOSE_PT = (2204, 340)
# 徽章 → 卡池卡片中心 / 徽章 → 免费按钮中心（实测两个卡池都对得上）
CARD_OFF = (280, 20)
FREE_BTN_OFF = (-120, 60)
# 底部按钮区，找「免费按钮上的感叹号」只看这一条，避免跟列表里的徽章混
BUTTON_REGION = (1200, 1030, 1360, 200)
# 列表里卡片的纵向范围（上面是三个分类标题，下面是底部导航）
CARD_Y = (360, 1030)

BADGE = "summon_free_badge"          # 列表卡片左侧（80px）
BADGE_BTN = "summon_free_badge_btn"  # 免费按钮右上角（60px）

NEEDS = [
    "tab_home", "tab_summon", "title_summon", "summon_list_head",
    BADGE, BADGE_BTN, "title_summon_result", "btn_summon_close",
    "hint_summon_rune",
]


def _xy(dev: Device, x: float, y: float) -> tuple[int, int]:
    """把 2560 宽的基准坐标换算到当前画面。"""
    k = dev.screen().shape[1] / REF_W
    return int(x * k), int(y * k)


def _find_in(dev: Device, name: str, region, th: float):
    """在屏幕的一块区域里找模板。

    一定要把整屏交给 matcher，region 只作为参数传下去 —— 自己先裁好再传的话，
    模板缩放按裁剪块宽度算，会缩成零点几倍，永远匹配不上。
    """
    return dev.matcher.find(dev.screen(True), name, th, region=region)


def _list_open(dev: Device) -> bool:
    """列表是不是展开着 —— 用「特别召唤」这个分类标题判断，只有展开时才在。"""
    return dev.exists("summon_list_head", 0.90)


def _open_list(dev: Device) -> bool:
    """展开左侧 LIST。开合动画很快，直接轮询标题出现，比等 wait_stable 快好几秒。

    召唤页每周换封面（STEPUP/活动/联动），顶部标题每次都不一样 —— 所以
    「到没到召唤页」不听标题的，就以「LIST 能不能展开」为准。
    """
    if _list_open(dev):
        return True
    for pt in LIST_OPEN_PTS:
        x, y = _xy(dev, *pt)
        dev.tap(x, y)
        t0 = time.time()
        while time.time() - t0 < 3.5:
            dev.check_cancel()
            dev.screen(True)
            if _list_open(dev):
                return True
            time.sleep(0.2)
    return False


def _close_list(dev: Device) -> None:
    if not _list_open(dev):
        return
    x, y = _xy(dev, *LIST_CLOSE_PT)
    dev.tap(x, y)
    t0 = time.time()
    while time.time() - t0 < 4:
        dev.check_cancel()
        dev.screen(True)
        if not _list_open(dev):
            return
        time.sleep(0.2)


def _scan(dev: Device) -> list:
    """列表里所有带感叹号的卡池，按从上到下排序。"""
    dev.screen(True)             # 先刷一帧，别在旧画面上找
    hits = dev.find_all(BADGE, 0.90)
    hits = [m for m in hits if CARD_Y[0] <= m.cy <= CARD_Y[1]]
    return sorted(hits, key=lambda m: (m.cx, m.cy))


def _free_button(dev: Device):
    m = _find_in(dev, BADGE_BTN, BUTTON_REGION, 0.92)
    if m is not None:
        return m
    # 有的卡池按钮位置更靠左/靠右，区域没框到时放宽到整条底部栏再找一次
    return _find_in(dev, BADGE_BTN, (600, 1030, 1900, 220), 0.92)


def _draw_rune(dev: Device) -> None:
    """在魔法阵里画一道符文。画什么都行，游戏中只认「有没有画」。

    一笔从中间往左上带过去就够了；实测 0.5 秒的滑动能正常触发。
    """
    x1, y1 = _xy(dev, 1000, 700)
    x2, y2 = _xy(dev, 1600, 420)
    dev.log.info("  提示要在魔法阵里画符文，划一笔")
    dev.swipe(x1, y1, x2, y2, 500)


# 结果页右下角[关闭]常出现的位置（基准坐标），先在这块找，找不到再全屏
CLOSE_ZONE = (1700, 1140, 840, 300)
# 实测这张模板在真实结果页上的匹配分只有 0.918~0.92（窗口缩放/压缩导致），
# 阈值卡 0.92 会刚好差 0.001 认不出来 → 误判「动画还在放」去点 SKIP。
# 0.85 稳过；实测旁边橙色的[再召唤]对这张模板 <0.7，不会误点。
CLOSE_TH = 0.85
# 抽卡动画右上角的 SKIP。这个按钮只在动画期间存在，位置固定，没有模板
# （深灰条 + 白字，和各种背景叠在一起，模板匹配不如固定落点稳）。
SKIP_PT = (2286, 67)


def _close_button(dev: Device):
    m = _find_in(dev, "btn_summon_close", CLOSE_ZONE, CLOSE_TH)
    if m is not None:
        return m
    return dev.find("btn_summon_close", CLOSE_TH)


def _tap_skip(dev: Device) -> None:
    x, y = _xy(dev, *SKIP_PT)
    dev.log.info("  结果动画还在放 → 点右上角 SKIP")
    dev.tap(x, y)


def _close_result(dev: Device, first=None) -> bool:
    """点[关闭] 并确认结果页真的关掉了（面板消失）。没关就再点，最多 3 次。"""
    for i in range(3):
        m = first if i == 0 else _close_button(dev)
        if m is None:
            return True
        dev.log.info(f"  点[关闭] @({m.cx},{m.cy})")
        dev.tap(m.cx, m.cy)
        dev.wait_stable(timeout=3)
        dev.screen(True)
    if _close_button(dev) is None:
        return True
    dev.dump("summon_close_stuck")
    dev.log.warning("  点了[关闭]但结果页还在（现场截图在 logs/fails）")
    return False


def _pull(dev: Device, limit: float = 90) -> bool:
    """点完免费按钮之后：必要时画符文 → 等结果 → 动画就 SKIP → 点[关闭]。

    返回 True 表示这一抽完整走完了（结果页已关掉）。

    这里有两个坑，都是实测踩出来的：

    1. **「召唤结果」横幅一开场就出来了，但面板和右下角[关闭]要等动画放完才滑进来。**
       旧代码一看到横幅就去找[关闭]，找不到就判「没等到结果页」直接放弃。用户那次
       的现场截图（logs/fails/103949_summon_no_close.png）就是动画刚开始、画面还黑着
       的那一帧：横幅有了、SKIP 有了、面板还完全没有。
       现在：横幅出现只当「进到结果流程」，接着轮询等[关闭]；等的时候点右上角
       SKIP 把动画跳过去（这游戏的召唤动画直接跳过就行）。
    2. **[关闭]点完必须确认真的关了。** 关不掉的话一个弹窗会把后面所有任务全卡死
       （用户遇到的「免费召唤完不点关闭，接着一个接一个报错」就是这么来的）。

    画符文这一步不是每次都有：同一个卡池第一次抽会要求画，之后（包括这一轮里的
    其它卡池）有时直接进动画。所以是「看见提示才画」，最多重画 3 次 ——
    划一笔没被游戏认出来时，光标要重新起笔。
    """
    draws = 0
    skips = 0
    last_skip = 0.0
    entered = False
    deadline = time.time() + limit
    while time.time() < deadline:
        dev.check_cancel()
        dev.screen(True)            # 强制取新帧，不然会一直看旧画面

        m = _close_button(dev)
        if m is not None:
            dev.log.info("  召唤结果页已出来")
            return _close_result(dev, m)
        if dev.exists("title_summon_result", 0.90):
            # 「召唤结果」横幅一开场就出现，而且最终结果页上也一直挂着 ——
            # 它不能证明「动画还在播」。真正的判据是 [关闭] 按钮出没出来：
            # 上面已经用 CLOSE_TH=0.85 找过一次，走到这里说明这一帧确实还没有。
            # 面板从横幅出现到滑入完成要几秒，这里先再等 2 秒 + 复核一次 [关闭]，
            # 别一看到横幅就急着点 SKIP（用户反馈「点完免费抽 1 秒就点右上角，
            # 其实动画早跳过了」）。只有复核仍没有关闭按钮，才点一次 SKIP。
            entered = True
            if skips < 1 and time.time() - last_skip > 1.2:
                # 用当前已读的这一帧起算，等 2 秒让面板/按钮渲染出来
                deadline_skip = time.time() + 2.0
                while time.time() < deadline_skip:
                    dev.check_cancel()
                    time.sleep(0.3)
                    dev.screen(True)
                    wait_close = _close_button(dev)
                    if wait_close is not None:
                        dev.log.info("  召唤结果页已出来")
                        return _close_result(dev, wait_close)
                _tap_skip(dev)
                skips += 1
                last_skip = time.time()
                time.sleep(0.6)
                continue
        elif draws < 3 and dev.exists("hint_summon_rune", 0.90):
            _draw_rune(dev)
            draws += 1
            time.sleep(1.0)
            continue
        elif not entered and time.time() - last_skip > 6 and skips < 2:
            # 点了免费按钮后好一会儿没任何提示：多半是抽卡动画已经在放
            # （画符文的提示框一闪而过没抓到）。盲点一次 SKIP —— 它上面是
            # 纯背景，点空也不会误触别的东西。
            _tap_skip(dev)
            skips += 1
            last_skip = time.time()

        time.sleep(0.35)

    dev.dump("summon_no_result")
    dev.log.warning(f"  等了 {limit:.0f} 秒也没等到[关闭]（现场截图在 logs/fails）")
    return False


# ---------------------------------------------------------------- 任务
def job_free_summon(dev: Device) -> None:
    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在弹窗里")
    if not dev.tap_img("tab_summon", timeout=10, threshold=0.88):
        raise NotFound("主页底部没找到[召唤]")
    dev.wait_stable(timeout=5)

    # 召唤页每周换封面，「召唤」标题模板认不住新图 —— 按用户的要求，直接
    # 靠「左边 LIST 能不能展开」判断到没到召唤页（这也正是下面要用的操作）。
    if not _open_list(dev):
        dev.log.info("  进召唤页后 LIST 没展开，再点一次[召唤]")
        dev.tap_img("tab_summon", timeout=5, threshold=0.88)
        dev.wait_stable(timeout=4)
        if not _open_list(dev):
            shot = dev.dump("summon_list_closed") if dev.save_on_fail else ""
            raise NotFound("进了召唤页但左边 LIST 打不开"
                           + (f"（现场：{shot}）" if shot else ""))

    tried: set[tuple[int, int]] = set()
    done = 0
    for _ in range(12):          # 卡池再多也不会有 12 个带免费
        if not _open_list(dev):
            dev.log.warning("  左边 LIST 打不开，收工")
            break

        todo = [m for m in _scan(dev)
                if (round(m.cx / 20), round(m.cy / 20)) not in tried]
        if not todo:
            dev.log.info("  列表里已经没有带感叹号的卡池了")
            break

        b = todo[0]
        tried.add((round(b.cx / 20), round(b.cy / 20)))
        dev.log.info(f"  卡池 @({b.cx},{b.cy}) 带感叹号，进去看看")

        x, y = _xy(dev, b.cx + CARD_OFF[0], b.cy + CARD_OFF[1])
        dev.tap(x, y)
        # 点卡片只是把背后的页面换掉，列表不会自己收起来，得再点一次 LIST
        dev.wait_stable(timeout=2.5)
        _close_list(dev)
        dev.wait_stable(timeout=5)

        fb = _free_button(dev)
        if fb is None:
            # 卡池页可能还在加载（换过封面的大图要解码），多等几秒再看
            t0 = time.time()
            while fb is None and time.time() - t0 < 6:
                dev.check_cancel()
                time.sleep(0.6)
                dev.screen(True)
                fb = _free_button(dev)
        if fb is None:
            dev.log.info("  这个卡池底部没有带感叹号的免费按钮，跳过")
            continue

        bx, by = _xy(dev, fb.cx + FREE_BTN_OFF[0], fb.cy + FREE_BTN_OFF[1])
        dev.log.info(f"  点免费抽 @({bx},{by})")
        dev.tap(bx, by)
        if not _pull(dev):
            # 走之前先尽量把弹窗清掉：留着它，后面每个任务都会撞在同一处
            _tap_skip(dev)
            time.sleep(1.2)
            _close_result(dev)
            dev.notify("免费召唤点了但没等到[关闭]按钮，先停下，麻烦你看一眼")
            raise Skipped("免费抽点了没走到结果页收尾（现场截图在 logs/fails）")
        done += 1
        dev.log.info(f"  第 {done} 个卡池抽完")

    if not done:
        _close_list(dev)
        if not ensure_home(dev):
            raise NotFound("回不到主页")
        raise Skipped("今天没有可免费抽的卡池（列表里没有感叹号）")

    dev.log.info(f"  本轮共抽了 {done} 个卡池的免费抽")
    _close_list(dev)
    if not ensure_home(dev):
        raise NotFound("抽完了但回不到主页")
