"""详情页「使用N个」扫荡数量调节 —— 遗迹和主线共用。

这个网页版的调节条有四个坑，全是实测踩出来的：

1. **热区偏上**：加号圆钮画在 (2206,869)，但点圆心/下缘一律没反应，只有
   y≈825~865（圆的上半部）才认。原来点 (2186,848) 落在左上边缘，十次里只中
   一两次 —— 于是「4 次扫荡」实际只扫 1 次，日志还报 4/4。
2. **连点会被吞**：同一个坐标连着点，第二次经常不认。要挪几个像素再点。
3. **数字刷新滞后**：长按 2 秒期间数字还是旧值，下一次点击才刷出来。所以
   必须回读「使用N个」里的数字核对，固定 sleep 会误判成「点不动」。
4. **数量会被记住**：上一次把数量调成 2，下次进同一个详情页（甚至换个难度）
   还是 2。所以**每次扫荡前都必须归 1**，不能因为「这次只要 1 次」就跳过
   归位 —— 这正是「明明只要 1 次、每次都扫 2 次」的根因。

归 1 的做法：先点 RESET；RESET 有时点不动（或整条控件因为体力只够 1 次而
变灰），就用减号一路探 —— 每点一次减号回读数字，数字变了说明还没到 1；
连点两个不同落点都不变，才认定已经在 1。RESET 是否生效不再靠模板亮暗判断。
"""
from __future__ import annotations

import time

from core.device import Device

REF_W = 2560

# 调节条（基准 2560×1440）：RESET / − / [使用N个] / + / MAX
# 坐标是**实测热区**，不是图标几何中心（见模块开头第 1 条）
SWEEP_RESET_POINTS = ((1623, 852), (1623, 838), (1608, 850))
SWEEP_PLUS_POINTS = ((2210, 845), (2226, 850), (2196, 840), (2232, 843))
SWEEP_MINUS_POINTS = ((1755, 852), (1755, 838), (1740, 848), (1770, 845))
SWEEP_USE_X, SWEEP_USE_Y = 1975, 828
# 「使用N个」那串字（含数字）的位置，用来判断数量有没有真的变
SWEEP_COUNT_BOX = (1840, 838, 280, 54)

# 数字真变了的判定：这块 UI 是静止的，空转两帧差 0 个像素；
# 数字每涨/减一次有 200~340 个像素跳变。60 是留足余量后的门槛。
COUNT_CHANGED_PX = 60


def tap_xy(dev: Device, x: int, y: int) -> None:
    """按基准坐标点一下（画面由 Capture 归一到 2560×1440，不用再换算）。"""
    dev.tap(x, y)


def use_point(dev: Device) -> tuple[int, int]:
    return SWEEP_USE_X, SWEEP_USE_Y


def count_sig(dev: Device):
    """「使用N个」区域的小灰度指纹。数字变了它一定变。"""
    import cv2

    img = dev.screen(True)
    x, y, w, h = SWEEP_COUNT_BOX
    crop = img[y:y + h, x:x + w]
    return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype("float32")


def count_delta(a, b) -> int:
    return int((abs(a - b) > 25).sum())


def wait_change(dev: Device, base, timeout: float = 2.0):
    """等「使用N个」里的数字变一次。返回 (变了没, 新的指纹)。"""
    end = time.time() + timeout
    cur = base
    while time.time() < end:
        dev.check_cancel()
        time.sleep(0.18)
        cur = count_sig(dev)
        if count_delta(base, cur) > COUNT_CHANGED_PX:
            return True, cur
    return False, cur


def plus_score(dev: Device) -> float:
    """加号当前匹配分：亮色真值 1.000、误配减号 0.870、灰态 0.59。"""
    m = dev.find("btn_sweep_plus", 0.0)
    return 0.0 if m is None else float(m.score)


def minus_score(dev: Device) -> float:
    """减号匹配分（btn_sweep_minus 模板裁的是灰态，归 1 后 0.999）。"""
    m = dev.find("btn_sweep_minus", 0.0)
    return 0.0 if m is None else float(m.score)


def _nudge_away(dev: Device) -> None:
    """把指针挪开：网页版偶尔会把「按下」状态留住，同位置再点就没反应。"""
    dev.move_to(1280, 760)
    time.sleep(0.15)


def _reset_by_button(dev: Device) -> bool:
    """点 RESET 归 1，用「数字有没有变」+「加减亮暗」两个信号确认。

    只看减号是不是灰的不够：体力只够 1 次时整条控件都可能变灰，那时数量
    可能还记着 2 —— 靠那个判据会误判成「已经在 1」，于是又扫了 2 次。
    """
    for x, y in SWEEP_RESET_POINTS:
        dev.check_cancel()
        base = count_sig(dev)
        tap_xy(dev, x, y)
        changed, _ = wait_change(dev, base, 1.3)
        if changed:
            return True                      # 数字变了 → 确实归 1 了
        # 数字没变：只有「加号亮着 + 减号灰着」才能肯定是 1
        # （加号亮 = 体力够 2 次；减号灰 = 已经在 1，减不动）
        ms, ps = minus_score(dev), plus_score(dev)
        if ms >= 0.95 and ps >= 0.95:
            return True
        if ms >= 0.95:
            break                            # 减号已经是灰的，再点 RESET 也没意义 → 交给减号探底
        _nudge_away(dev)
    return False


def _probe_minus_to_one(dev: Device) -> bool:
    """用减号把数量一路探到 1（RESET 点不动时的兜底）。

    返回 True = 已确认在 1。判定方式：连续用两个不同落点点减号，数字都不变。
    数字没变只有两种可能：已经在 1（减号按不动），或者点被吞了 —— 所以换个
    落点再点一次，两次都不变才认定到 1。
    """
    for i in range(20):
        dev.check_cancel()
        base = count_sig(dev)
        tap_xy(dev, *SWEEP_MINUS_POINTS[i % len(SWEEP_MINUS_POINTS)])
        changed, _ = wait_change(dev, base, 1.5)
        if changed:
            continue
        _nudge_away(dev)
        # 换两个落点再确认（点被吞时换位置通常就好了；不必把四个落点全试一遍）
        for p in SWEEP_MINUS_POINTS[1:3]:
            dev.check_cancel()
            base = count_sig(dev)
            tap_xy(dev, *p)
            changed, _ = wait_change(dev, base, 1.5)
            if changed:
                break
            _nudge_away(dev)
        if not changed:
            return True
    return False


def reset_to_one(dev: Device) -> bool:
    """把扫荡数量归到 1，返回是否确认成功。

    不管这次要扫几次都要调 —— 游戏会记住上一次设的数量。
    """
    if _reset_by_button(dev):
        return True
    dev.log.info("  RESET 没生效，改用减号一路退到 1")
    return _probe_minus_to_one(dev)


def set_count(dev: Device, count: int) -> int:
    """把扫荡数量调到 count，返回**实际能调到的次数**。

    返回 0 表示基线没确认（调节条点不动），调用方按「单次扫荡」处理。
    """
    if not reset_to_one(dev):
        dev.log.warning("  数量没法归到 1，基线不确定")
        return 0

    if count <= 1:
        return 1

    ps = plus_score(dev)
    if ps < 0.95:
        dev.log.info(f"  + 号是灰的（匹配 {ps:.2f}），体力只够 1 次，按单次扫")
        return 1

    got = 1
    base = count_sig(dev)
    for _ in range(count - 1):
        stepped = False
        for attempt in range(3):
            dev.check_cancel()
            x, y = SWEEP_PLUS_POINTS[attempt % len(SWEEP_PLUS_POINTS)]
            tap_xy(dev, x, y)
            ok, cur = wait_change(dev, base)
            if ok:
                base, got, stepped = cur, got + 1, True
                break
            _nudge_away(dev)
        if not stepped:
            dev.log.warning(f"  加到 {got} 次就点不动了（多半是体力只够这么多）")
            break

    # 收尾再看一眼：网页版的数字慢半拍，晚了才刷出来的那一下也算数
    ok, base = wait_change(dev, base, 1.2)
    if ok:
        got += 1
        dev.log.info("  有一次点击是延迟生效的，按实际次数算")

    # 多出来的用减号收回去（同样要回读核对）
    while got > count:
        trimmed = False
        for i in range(4):
            dev.check_cancel()
            tap_xy(dev, *SWEEP_MINUS_POINTS[i % len(SWEEP_MINUS_POINTS)])
            ok, cur = wait_change(dev, base, 1.8)
            if ok:
                base, got, trimmed = cur, got - 1, True
                break
            _nudge_away(dev)
        if not trimmed:
            dev.log.warning(f"  多调了 {got - count} 次但减不回去，就按 {got} 次扫")
            break
    return got
