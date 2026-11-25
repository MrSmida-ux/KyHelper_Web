"""日常任务定义 —— 双穹幻想 Burst

这里放「内置任务」：逻辑比较复杂、用代码写更清楚的那些。
简单任务不用写代码 —— 用 GUI 里的「录制」直接生成 tasks/recipes/*.yaml。

加新任务的两种方式：
  A. 录制（推荐）：GUI 点「+ 新建任务」→ 照着走一遍 → 保存
  B. 写代码：照 job_stamina 的样子写函数，再追加到 JOBS
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from core.device import Cancelled, Device, NotFound, Skipped
from core.flows import close_popups, dismiss_ads, ensure_home, enter_tokuten  # noqa: F401  （外部还会用到）
from .dispatch import DISPATCH_NEEDS, job_dispatch
from .market import MARKET_NEEDS, job_market_free
from .paladin import NEEDS as PALADIN_NEEDS, job_paladin_gift
from .rescue import (
    AID_NEEDS, DIFF_OPTION, ELEMENT_OPTION, ROUND_OPTION, START_NEEDS,
    TIMES_OPTION, job_rescue_aid, job_rescue_start,
)
from .ruins import (
    ABIL_OPTION, ATTR_OPTION, DIFF_OPTION as RUINS_DIFF_OPTION,
    RUINS_NEEDS, RUINS_OPTION, SWEEP_OPTION, job_ruins,
)
from .main_story import (
    CHAPTER_OPTION, DIFF_OPTION as STORY_DIFF_OPTION,
    MAIN_STORY_NEEDS, SWEEP_OPTION as STORY_SWEEP_OPTION, job_main_story,
)
from .labyrinth import (
    DIFF_OPTION as MAZE_DIFF_OPTION, NEEDS as MAZE_NEEDS,
    TARGET_OPTION as MAZE_TARGET_OPTION, BUFF_OPTION as MAZE_BUFF_OPTION,
    MODE_OPTION as MAZE_MODE_OPTION,
    RETRY_OPTION as MAZE_RETRY_OPTION, job_labyrinth,
)
from .summon import NEEDS as SUMMON_NEEDS, job_free_summon
from .task_reward import NEEDS as TASK_REWARD_NEEDS, job_task_reward
from .yicezhan import NEEDS as YICE_NEEDS, job_yice

# 主页锚点：特典按钮只在主页出现，用来判断"我现在是不是在主页"
# 别用底部导航栏当锚点 —— 底部导航在特典页/记录页也都存在，判不出来
HOME = "btn_tokuten"

# 连续这么多个任务都失败，就判定游戏卡住了，停止后面的任务（不是崩，是止损）
CONSECUTIVE_FAIL_LIMIT = 3


@dataclass
class Job:
    key: str
    name: str
    # 带选项的任务 fn 收两个参数 (dev, opts)，opts 是 {选项key: [值, ...]}
    fn: Callable[..., None]
    needs: list[str] = field(default_factory=list)
    desc: str = ""
    source: str = "code"     # code | recipe
    path: str = ""           # recipe 任务的 yaml 路径
    enabled: bool = True
    # 执行前要让用户勾的东西（属性、难度之类）。空表 = 没得选，直接跑。
    options: list[dict] = field(default_factory=list)

    @property
    def deletable(self) -> bool:
        return self.source == "recipe"

    @property
    def has_options(self) -> bool:
        return bool(self.options)


# ---------------------------------------------------------------- 缩放 100%
def job_zoom_100(dev: Device) -> None:
    """把网页缩放拨到 100%，并重新对齐画面（日常第一步）。

    缩放是浏览器进程级的快捷键，只能发系统按键（会抢一下前台 1~2 秒），所以
    单独做成一个任务、放在最前面：确认画面尺寸对了，后面所有截图识别才准。
    dev.s 是 GameSession，zoom_and_settle 和面板「缩放100%」按钮是同一套逻辑。
    """
    s = getattr(dev, "s", None)
    if s is None or not hasattr(s, "zoom_and_settle"):
        dev.log.warning("  当前入口不支持缩放任务（命令行/旧版服务端），跳过")
        return
    dev.log.info("  把网页缩放拨到 100%（会抢一下前台）…")
    r = s.zoom_and_settle()
    if not r.get("ok"):
        # 缩放没成功不抛错：可能本来就是 100%（zoom_to_100 会返回 moved=False），
        # 也可能窗口没拿到焦点。后面任务照跑，交给用户看日志决定。
        dev.log.warning("  缩放没成功：" + (r.get("hint") or "未知原因") + "，继续后面的任务")
        return
    dev.log.info("  已缩放到 100% 并重新对齐画面")


# ---------------------------------------------------------------- 体力领取
def job_stamina(dev: Device) -> None:
    """领取 AP（特典 → AP 领取 → 一并领取）。

    幂等：今天领过了再跑一次也不会出错 —— 点「一并领取」不会有"回复成功"提示，
    代码按"今日已领取"处理，直接关掉弹窗回主页。
    """
    if not ensure_home(dev):
        raise NotFound("回不到主页，可能卡在某个弹窗里")

    dev.log.info("  进入特典")
    if not enter_tokuten(dev):
        raise NotFound("主页没找到 [特典] 按钮")

    dev.log.info("  打开 AP 领取")
    if not dev.tap_img("card_ap_claim", timeout=8):
        raise NotFound("特典页没找到 [AP 领取] 卡片")
    dev.wait_stable(timeout=8)

    # 到了领取弹窗就该看到「一并领取」；看不到说明页面没打开
    if not dev.exists("btn_claim_all", threshold=0.82):
        dev.log.warning("  没进到领取弹窗，退回主页")
        ensure_home(dev)
        return

    dev.tap_img("btn_claim_all", timeout=4, threshold=0.82)

    # 领到了会弹「AP回复 / 已回复AP」；都领过则什么都不弹
    try:
        dev.wait("dlg_ap_recovered", timeout=5)
        dev.log.info("  ✓ AP 回复成功")
        dev.tap_img("btn_ap_confirm", timeout=4)
    except NotFound:
        dev.log.info("  今日已领取（没有出现回复提示）")

    # 关掉弹窗，最多试 4 轮：先点「关闭」，点不到就按返回
    for _ in range(4):
        if dev.tap_img("btn_claim_close", timeout=2):
            dev.wait_stable(timeout=5)
            break
        dev.back()
        dev.wait_stable(timeout=4)

    if ensure_home(dev):
        dev.log.info("  已返回主页")
    else:
        dev.log.warning("  没能确认回到主页")


# ---------------------------------------------------------------- 任务表
JOBS: list[Job] = [
    Job(
        "zoom_100",
        "缩放100%",
        job_zoom_100,
        desc="第一步：把网页缩放拨到 100% 并重新对齐画面（会抢一下前台 1~2 秒）",
    ),
    # 「登录奖励领取」已移除：GAME START 的呼吸/发光动画让模板识别一直不成功，
    # 这个任务每次都卡在标题页，反而拖慢日常。游戏入口请手动点（登录状态会记住）。
    # 逻辑保留在 tasks/login.py，以后要恢复把上面两项 import 和这个 Job 加回来即可。
    Job(
        "stamina",
        "体力领取",
        job_stamina,
        needs=[
            "btn_tokuten", "card_ap_claim", "btn_claim_all",
            "dlg_ap_recovered", "btn_ap_confirm", "btn_claim_close", "tab_home",
        ],
        desc="特典页领取每天的 AP，重复运行不会出错",
    ),
    Job(
        "dispatch",
        "派遣领取",
        job_dispatch,
        needs=DISPATCH_NEEDS,
        desc="特典 → 派遣，领已归来的奖励并用同队伍再派一轮；没归来就跳过",
    ),
    Job(
        "market_free",
        "市场免费礼物",
        job_market_free,
        needs=MARKET_NEEDS,
        desc="商店 → 市场 → 期间限定，领每天的免费礼物；领过就跳过",
    ),
    Job(
        "paladin_gift",
        "圣骑士送羁绊礼物",
        job_paladin_gift,
        needs=PALADIN_NEEDS,
        desc="圣骑士 → 带感叹号的角色 → 羁绊，只送糖果；遇到要送花的角色就换下一个，"
             "最多试 6 个，都不行则跳过并提示",
    ),
    Job(
        "rescue_aid",
        "救援战·援助",
        job_rescue_aid,
        needs=AID_NEEDS,
        desc="参加别人的救援战：清单里挑一场，默认队伍打完领奖；可设次数连打",
        options=[ELEMENT_OPTION, DIFF_OPTION, TIMES_OPTION],
    ),
    Job(
        "rescue_start",
        "救援战·发起",
        job_rescue_start,
        needs=START_NEEDS,
        desc="自己发起救援战：冒险 → 特殊关卡 → 救援战，打完领奖；可开自动周回连打",
        options=[ELEMENT_OPTION, DIFF_OPTION, ROUND_OPTION],
    ),
    Job(
        "ruins",
        "遗迹关卡",
        job_ruins,
        needs=RUINS_NEEDS,
        desc="冒险 → 遗迹，五种遗迹×属性/能力×难度多选，扫荡券优先，体力不足跳过",
        options=[RUINS_OPTION, ATTR_OPTION, ABIL_OPTION, RUINS_DIFF_OPTION, SWEEP_OPTION],
    ),
    Job(
        "main_story",
        "主线冒险",
        job_main_story,
        needs=MAIN_STORY_NEEDS,
        desc="冒险 → 主线故事，Normal(1-1-1)/Hard(填章节如16-3-3)，扫荡券优先，体力不足跳过",
        options=[STORY_DIFF_OPTION, CHAPTER_OPTION, STORY_SWEEP_OPTION],
    ),
    Job(
        "free_summon",
        "免费召唤",
        job_free_summon,
        needs=SUMMON_NEEDS,
        desc="召唤 → LIST，带感叹号的卡池逐个点进去抽掉每天一次的免费抽，抽完回主页",
    ),
    Job(
        "labyrinth",
        "迷宫挑战",
        job_labyrinth,
        needs=MAZE_NEEDS,
        desc="冒险→特殊关卡→迷宫。方式二选一：跳过=点SKIP白嫖(十几秒)；"
             "手动=从左到右开门找指定王，匹配上才开打，逐层逐房打完到通关回主页。"
             "游戏每天只给一次，所以合并成一个任务。默认 手动+骑士王+Hard",
        options=[MAZE_MODE_OPTION, MAZE_TARGET_OPTION, MAZE_DIFF_OPTION,
                 MAZE_BUFF_OPTION, MAZE_RETRY_OPTION],
    ),
    Job(
        "yicezhan",
        "异策战",
        job_yice,
        needs=YICE_NEEDS,
        desc="冒险 → 期间限定 → 异策战，带感叹号的 BOSS 全部打完（第一场真打，之后 SKIP），回主页",
    ),
    Job(
        "任务奖励领取",
        "每日任务奖励领取",
        job_task_reward,
        needs=TASK_REWARD_NEEDS,
        desc="任务页 →「全部领取」→ 确认，循环领到按钮灰显为止（每天批次 1~6 不定），"
             "领完回主页；当天已领过则跳过，不误判失败",
        source="code",
    ),
]


def run_jobs(
    dev: Device,
    jobs: list[Job],
    on_job: Callable[[int, int, Job], None] | None = None,
    on_result: Callable[[Job, str, str], None] | None = None,
    opts_map: dict[str, dict] | None = None,
) -> tuple[int, int]:
    """按顺序执行。单个任务失败不中断整体 —— 日常脚本最怕一个卡住全崩。

    on_result 拿到的是状态字符串：ok / fail / skip。
    skip 表示「没活儿可干」（清单刷不出战、BOSS 已被讨伐），不算失败。

    但**连续**失败要收手：游戏卡在加载动画/弹窗里时，每个任务都会在
    「回不到主页」这一步撞死，一个接一个报错，既没用又刷屏。
    """
    ok = fail = 0
    streak = 0
    opts_map = opts_map or {}
    for i, job in enumerate(jobs, 1):
        dev.check_cancel()
        dev.log.info(f"[{i}/{len(jobs)}] {job.name}")
        # 每个任务开始前先清一次广告弹窗（AP/BP不足的促销窗会盖住所有按钮）
        dismiss_ads(dev)
        if on_job:
            on_job(i, len(jobs), job)
        try:
            if job.has_options:
                job.fn(dev, opts_map.get(job.key) or {})
            else:
                job.fn(dev)
            ok += 1
            streak = 0
            dev.log.info("  完成")
            if on_result:
                on_result(job, "ok", "")
        except Cancelled:
            dev.log.warning("  已停止")
            raise
        except Skipped as e:
            # 没活儿不等于出错：救援清单刷不出战、BOSS 已被讨伐都走这里
            streak = 0
            dev.log.info(f"  跳过：{e}")
            if on_result:
                on_result(job, "skip", str(e) or "跳过")
            try:
                ensure_home(dev)
            except Cancelled:
                raise
            except Exception:
                pass
        except NotFound as e:
            fail += 1
            streak += 1
            dev.log.warning(f"  失败：{e}")
            if on_result:
                on_result(job, "fail", str(e))
            try:
                ensure_home(dev)
            except Cancelled:
                raise
            except Exception:
                pass
        except Exception as e:
            fail += 1
            streak += 1
            dev.log.error(f"  异常：{type(e).__name__}: {e}")
            if on_result:
                on_result(job, "fail", f"{type(e).__name__}: {e}")

        if streak >= CONSECUTIVE_FAIL_LIMIT:
            msg = f"连续 {streak} 个任务失败，游戏可能卡住了（加载动画/弹窗）"
            dev.log.error(f"  {msg}，后面的任务先不跑")
            dev.notify(f"{msg}，已停下，麻烦看一眼游戏画面")
            if on_result:
                for rest in jobs[i:]:
                    on_result(rest, "skip", "未执行：前面连续失败已中止")
            break
    return ok, fail
