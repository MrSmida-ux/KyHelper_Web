"""每日任务奖励领取 —— 循环点「全部领取」直到按钮灰显，再回主页。

为什么不用录制脚本：每天可领取的任务批次不固定（实测 1~6 批），旧录制脚本
把「全部领取 → 确认」写死了两轮（rec_04/rec_05 其实和 rec_02/rec_03 是同一处
按钮的另一种样式），领完第一批按钮就灰了，脚本还在傻等第二个「全部领取」，
6 秒超时 → 误判成失败。

这里改成状态驱动：只要「全部领取」还是亮态就一直领，灰显了就正常收尾回主页。
领 0 批（今天已经领过）也不是失败。

灰显判定实测：亮态模板匹配 0.96~1.00，灰显只有 0.79~0.80，阈值 0.90 能干净分开。
"""
from __future__ import annotations

import time

from core.device import Device, NotFound
from core.flows import ensure_home

# 进任务页：底部导航的「任务」入口（第一张录制模板就是它）
TASK_ENTRY = "rec_任务奖励领取_01"
# 「全部领取」按钮的两种样式（不同页签底色不同：米色 / 深色），都是同一个按钮位置
CLAIM_BTNS = ("rec_任务奖励领取_02", "rec_任务奖励领取_04")
# 领取确认弹窗的「确认」按钮两种样式
CONFIRM_BTNS = ("rec_任务奖励领取_03", "rec_任务奖励领取_05")
# 亮态/灰显的分界：亮 0.96~1.00，灰 0.79~0.80
CLAIM_TH = 0.90
# 「礼物 / 已领取下列物品」弹窗的确认（有些批次领完会弹这个）
GIFT_OK_BTNS = ("btn_gift_confirm", "btn_mail_reward_ok", "gift_result_ok")
# 保险丝：真要一天领几十批也不该，超过就停，避免按钮识别抽风时死循环
MAX_BATCHES = 12

NEEDS = [TASK_ENTRY, *CLAIM_BTNS, *CONFIRM_BTNS, "btn_gift_confirm"]


def _claim_point(dev: Device):
    """找一个亮态「全部领取」，返回 (cx, cy, score)；全灰显返回 None。"""
    dev.screen(True)                      # 强制取新帧，别拿点之前的旧画面判
    best = None
    for name in CLAIM_BTNS:
        if not dev.matcher.has(name):
            continue
        m = dev.matcher.find(dev.screen(), name, CLAIM_TH)
        if m is not None and (best is None or m.score > best[2]):
            best = (int(m.cx), int(m.cy), float(m.score))
    return best


def _clear_confirm(dev: Device) -> bool:
    """点掉领取后的确认弹窗。返回这一批是否见到了确认框。

    确认框有几种样式：任务页自己的「确认」(03/05)，或通用的「礼物/已领取」
    弹窗确认。见到就点、点完取新帧再看，直到没有为止（最多几轮防死循环）。
    """
    seen = False
    for _ in range(4):
        dev.check_cancel()
        dev.screen(True)
        hit = None
        for name in CONFIRM_BTNS + GIFT_OK_BTNS:
            if not dev.matcher.has(name):
                continue
            m = dev.matcher.find(dev.screen(), name, 0.88)
            if m is not None:
                hit = (name, int(m.cx), int(m.cy))
                break
        if hit is None:
            return seen
        name, x, y = hit
        dev.log.info(f"  确认领取 [{name}] @({x},{y})")
        dev.tap(x, y)
        seen = True
        dev.wait_stable(timeout=2.0)
    return seen


def job_task_reward(dev: Device) -> None:
    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在某个弹窗里")

    dev.log.info("  进入任务页")
    if not dev.tap_img(TASK_ENTRY, timeout=8):
        raise NotFound("主页没找到 [任务] 入口")
    dev.wait_stable(timeout=3)

    got = 0
    for _ in range(MAX_BATCHES):
        dev.check_cancel()
        pt = _claim_point(dev)
        if pt is None:
            break                          # 按钮灰显了 → 领干净了
        x, y, score = pt
        dev.log.info(f"  第 {got + 1} 批：点「全部领取」@({x},{y}) 相似度 {score:.3f}")
        dev.tap(x, y)
        _clear_confirm(dev)
        got += 1

    if got == 0:
        dev.log.info("  「全部领取」是灰的，今天没有可领的任务奖励，跳过")
    else:
        dev.log.info(f"  共领取 {got} 批任务奖励")

    # 领完回主页。回不去不算这个任务的失败（后面任务会自己 ensure_home），
    # 但记一笔，方便排查是不是卡在哪个确认框里。
    if not ensure_home(dev):
        dev.log.warning("  领完没能回到主页，后面的任务会再试一次")
