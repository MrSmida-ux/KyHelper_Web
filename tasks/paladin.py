"""圣骑士羁绊礼物：给「能送糖果」的角色送 1 个糖果。

用户定的规则（2026-11-17）：
    主页 → 圣骑士 → 点带感叹号的角色 → 羁绊页签
      · 是「礼物」列表（能看到糖果）→ 点最左边的糖果 → 确定 → 完成
      · 是「突破」面板（要花束 0/1，送不了）→ 不送，点面板右边的「›」换下一个角色
    最多试 6 个角色；6 个都不行就整轮跳过，并在日志和通知里说清楚。

为什么弃用原来录制的 paladin_gift.yaml：
  1. 原来用 `if: gift_badge` 判断「羁绊页签上有感叹号」。但角色详情页上
     被动能力 / 技能 / 装备 / 羁绊**四个页签都是同一个粉色感叹号**，而模板只有
     36×36，同一张图上得分 0.86~0.90，正好压在 0.88 门槛上 —— 于是「明明有
     感叹号也不送」。判断「有没有礼物列表」比判断「有没有感叹号」实在得多。
  2. 原来只会点列表里第一个感叹号卡片，换不了角色。「换一个能送糖果的」本质上
     是逐角色试错，录制表达不出这个分支。

几个实测坐标（2560×1440 基准，跟模板同一套坐标）：
  · 卡片右上角的感叹号 → 卡片中心：往左下偏 (115, 115)
  · 详情页右侧的「›」换角色箭头：(2445, 695)，在「礼物」和「突破」两种面板上都在
  · 「赠送礼物」弹窗的确定按钮：模板 gift_btn_ok，实测 0.999
"""
from __future__ import annotations

import time

from core.device import Device, NotFound, Skipped
from core.flows import ensure_home

# ---------------------------------------------------------------- 模板
TAB_PALADIN = "tab_paladin"      # 主页底部「圣骑士」
BADGE = "gift_badge"             # 卡片右上角的粉色感叹号（列表里 36px，得分 0.98）
BOND_TAB = "tab_bond"            # 详情页最右边「羁绊」页签（未选中态）
CANDY = "gift_item_candy"        # 礼物列表最左边的糖果
BTN_OK = "gift_btn_ok"           # 弹窗「确定」
BTN_MIN = "gift_btn_min"         # 数量 MIN
BTN_PLUS = "gift_btn_plus"       # 数量 +

NEEDS = [TAB_PALADIN, BADGE, BOND_TAB, CANDY, BTN_OK, BTN_MIN, BTN_PLUS, "tab_home"]

# ---------------------------------------------------------------- 固定位置
MAX_TRIES = 6                    # 最多试几个角色（用户定的）
CARD_DX, CARD_DY = -115, 115     # 感叹号 → 卡片中心
ARROW_PT = (2445, 695)           # 详情页右侧「›」= 下一个角色
BOND_PT = (2324, 62)             # 羁绊页签（模板没命中时的兜底坐标）

# 区域一律 (x, y, w, h)。注意 Matcher 的 region 只是「优先搜索」，找不到会退回
# 全屏，所以用 _find() 包一层，命中点必须真的落在框里才算。
LIST_BOX = (440, 260, 1990, 860)     # 角色卡片网格（避开左侧菜单和底部导航）
NAV_BAR = (0, 1210, 2560, 230)       # 底部导航条整排：圣骑士只在这里找，别让可更换的主页立绘误命中
BOND_BOX = (2100, 10, 460, 110)      # 页签行最右边那一段
GIFT_BOX = (1000, 740, 1300, 260)    # 礼物列表那一行
PANEL_BOX = (400, 60, 1700, 460)     # 右侧信息面板（判断有没有换角色用）
OK_BOX = (600, 1050, 1400, 380)      # 弹窗底部按钮区
NUM_BOX = (700, 620, 1200, 220)      # 弹窗里的数量调节条


def _in_box(box, cx: int, cy: int) -> bool:
    x, y, w, h = box
    return x <= cx <= x + w and y <= cy <= y + h


def _find(dev: Device, name: str, box, threshold: float):
    """只在指定区域里找模板；找到的位置跑到框外就不算。"""
    dev.screen(True)                      # 强制取新帧，别在旧画面上找
    m = dev.matcher.find(dev.screen(), name, threshold, region=box)
    if m is not None and _in_box(box, m.cx, m.cy):
        return m
    return None


def _wait_for(dev: Device, name: str, box, threshold: float, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        dev.check_cancel()
        m = _find(dev, name, box, threshold)
        if m is not None:
            return m
        time.sleep(0.3)
    return None


def _wait_gone(dev: Device, name: str, box, threshold: float, timeout: float) -> bool:
    """等某个模板从区域里消失（弹窗关掉）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dev.check_cancel()
        if _find(dev, name, box, threshold) is None:
            return True
        time.sleep(0.3)
    return False


def _panel_sig(dev: Device):
    """右侧信息面板的粗指纹，用来确认「换角色」真的换掉了。

    为什么不用立绘：立绘有呼吸之类的待机动画，同一角色隔几秒也会差 1~2；
    右侧面板是静态 UI，同一角色两帧差 0.0，换角色 6~10（实测），分辨得很干净。
    """
    import cv2

    img = dev.screen(True)
    crop = img[PANEL_BOX[1]:PANEL_BOX[1] + PANEL_BOX[3],
               PANEL_BOX[0]:PANEL_BOX[0] + PANEL_BOX[2]]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (32, 20), interpolation=cv2.INTER_AREA).astype("float32")


def _changed(before, after) -> float:
    return float(abs(before - after).mean())


# ---------------------------------------------------------------- 列表 / 页签
def _scan(dev: Device, threshold: float = 0.95) -> list:
    """角色列表里所有带感叹号的卡片，按阅读顺序（先行后列）排。"""
    dev.screen(True)
    hits = [m for m in dev.matcher.find_all(dev.screen(), BADGE, threshold)
            if _in_box(LIST_BOX, m.cx, m.cy)]
    return sorted(hits, key=lambda m: (m.cy // 80, m.cx))


def _wait_list(dev: Device, timeout: float = 12) -> list:
    """等角色列表出来（第一行卡片上出现感叹号就算出来了）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dev.check_cancel()
        hits = _scan(dev)
        if hits:
            return hits
        time.sleep(0.4)
    return []


def _ensure_bond_tab(dev: Device) -> bool:
    """停在羁绊页签上。已经在羁绊上时模板不命中（选中态长得不一样），不会乱点。"""
    m = _find(dev, BOND_TAB, BOND_BOX, 0.94)
    if m is not None:
        dev.tap(m.cx, m.cy)
    else:
        dev.tap(*BOND_PT)
    dev.wait_stable(timeout=5)
    return True


# ---------------------------------------------------------------- 送礼
def _send_candy(dev: Device, m) -> bool:
    """点糖果 → 「赠送礼物」弹窗 → 确定 → 等礼物列表回来。"""
    dev.tap(m.cx, m.cy)
    ok = _wait_for(dev, BTN_OK, OK_BOX, 0.90, 8)
    if ok is None:
        dev.dump("gift_no_dialog")
        dev.log.warning("  点了糖果但没弹出「赠送礼物」（现场截图在 logs/shots）")
        return False

    dev.tap(ok.cx, ok.cy)
    # 先确认弹窗真的关掉了，再判「礼物列表回来了」。弹窗就压在礼物列表上面，
    # 弹窗没关就去找糖果，会在半透明遮罩底下命中一个「假回来」，白高兴。
    closed = _wait_gone(dev, BTN_OK, OK_BOX, 0.90, 6)
    if not closed:
        dev.log.info("  弹窗没关掉，把数量从 MIN 调到 1 再确定")
        for name in (BTN_MIN, BTN_PLUS):
            b = _find(dev, name, NUM_BOX, 0.90)
            if b is not None:
                dev.tap(b.cx, b.cy)
                time.sleep(0.45)
        b = _find(dev, BTN_OK, OK_BOX, 0.90)
        if b is not None:
            dev.tap(b.cx, b.cy)
        closed = _wait_gone(dev, BTN_OK, OK_BOX, 0.90, 8)
    if not closed:
        dev.dump("gift_dialog_stuck")
        dev.log.warning("  点了确定但弹窗没关（现场截图在 logs/shots）")
        return False

    # 送出去后有一次花瓣演出，礼物列表回来才算真的成功
    deadline = time.time() + 12
    while time.time() < deadline:
        dev.check_cancel()
        if _find(dev, CANDY, GIFT_BOX, 0.86) is not None:
            return True
        time.sleep(0.4)
    dev.dump("gift_not_closed")
    dev.log.warning("  点了确定，但礼物列表没回来（现场截图在 logs/shots）")
    return False


def _next_character(dev: Device) -> bool:
    """点详情页右侧的「›」换下一个角色。返回 False 表示换不动了。"""
    before = _panel_sig(dev)
    for dx in (0, -18, 18):
        dev.tap(ARROW_PT[0] + dx, ARROW_PT[1])
        dev.wait_stable(timeout=5)
        if _changed(before, _panel_sig(dev)) >= 2.5:
            return True
    return False


# ---------------------------------------------------------------- 任务
def job_paladin_gift(dev: Device) -> None:
    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在某个弹窗里")

    # 阈值从 0.85 降到 0.75：11-20 起这个入口图标加了待机光效，实测匹配分从
    # 原来的 0.95+ 掉到 0.838，正好压在旧门槛下，导致"明明在主页却说没找到"。
    # 区分度实测：本入口 0.838，底部其它图标最高 0.488，中间空档很大，
    # 0.75 不会误认（详见 logs/fails/033520_timeout_tab_paladin.png）。
    # 关键：只在底部导航条 NAV_BAR 里找并校验命中点真的落在框内。不能用
    # dev.tap_img —— 它是全屏匹配，主页立绘每个玩家都能换，万一立绘上出现
    # 相似图案就会点错地方；_find 的 region + _in_box 双重保证只在那一排里点。
    tab = _wait_for(dev, TAB_PALADIN, NAV_BAR, 0.75, 10)
    if tab is None:
        raise NotFound("主页底部没找到[圣骑士]")
    dev.log.info(f"  → 点击 [{TAB_PALADIN}] @({tab.cx},{tab.cy}) 相似度={tab.score:.3f}")
    dev.tap(tab.cx, tab.cy)

    cards = _wait_list(dev, 14)
    if not cards:
        raise Skipped("圣骑士列表里没有带感叹号的角色，今天不用送")

    # 进第一个带感叹号的角色。列表最上面那张卡片有时被「养成强化中」这类
    # 横幅压着，按下去没反应 —— 那就还停在列表上，换下一张再试。
    entered = False
    for b in cards[:4]:
        dev.log.info(f"  点带感叹号的角色 @({b.cx},{b.cy})")
        dev.tap(b.cx + CARD_DX, b.cy + CARD_DY)
        dev.wait_stable(timeout=8)
        if not _scan(dev):          # 列表上的感叹号都看不到了 = 已经进详情页
            entered = True
            break
        dev.log.warning("  点了卡片还停在列表上，换一张")
    if not entered:
        raise NotFound("点了几张角色卡片都没能进到详情页")

    done = 0
    skipped: list[str] = []
    for i in range(1, MAX_TRIES + 1):
        dev.check_cancel()
        _ensure_bond_tab(dev)

        candy = _find(dev, CANDY, GIFT_BOX, 0.88)
        if candy is not None:
            dev.log.info(f"  第 {i} 个角色有糖果，送 1 个")
            if _send_candy(dev, candy):
                done += 1
                dev.log.info("  ✓ 已送出 1 个糖果")
                break
            # 弹窗没走完就别再往下试了：这时候页面上压着一层弹窗，
            # 换角色、找糖果全都会被它挡住，继续试只会白点。
            skipped.append(f"第{i}个（送礼没成功）")
            break
        else:
            dev.log.info(f"  第 {i} 个角色的羁绊页没有糖果（需要送花/已满），换下一个")
            skipped.append(f"第{i}个（要送花）")

        if i == MAX_TRIES:
            break
        if not _next_character(dev):
            dev.log.warning("  换不到下一个角色了（可能已经是列表最后一个）")
            break

    if not done:
        msg = f"试了 {len(skipped)} 个角色都送不了糖果：{'、'.join(skipped)}"
        dev.notify(msg + "，本轮先跳过")
        ensure_home(dev)
        raise Skipped(msg)

    if ensure_home(dev):
        dev.log.info("  已返回主页")
    else:
        dev.log.warning("  送完了但没能确认回到主页")
