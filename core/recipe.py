"""声明式任务：一串 YAML 步骤。

这是「录制」能成立的前提 —— 录制器产出的就是这个格式，加新功能不用写 Python。

文件放在 tasks/recipes/*.yaml，格式：

    key: mail                   # 唯一标识，英文
    name: 领取邮件               # 界面显示的名字
    desc: 主页 → 信件 → 一键领取 → 关闭
    steps:
      - note: 进入信件
      - tap_img: btn_mail       # 找到按钮就点
        timeout: 8
      - wait_stable: 6          # 等画面加载完
      - tap_img: btn_claim_all
        optional: true          # 找不到不报错，跳过继续
      - back
      - home                    # 回主页

所有命令（每条步骤只能用一个）：

  动作类
    home                     回主页
    back                     按返回键
    tap_img: 模板名           找图 → 点中心
    tap_img: {target: 模板名, offset: [dx,dy], region: [x1,y1,x2,y2]}
                             offset  找到之后按偏移点（比如点在图标旁边）
                             region  只在这块屏幕区域内找 —— 列表里有一堆长得
                                     一样的图标时，用它避开点不到的边角位置
    tap_xy: [x, y]           直接点坐标（坐标按模板基准分辨率写）
    swipe: [x1,y1,x2,y2]     滑动
    scroll: down|up          屏幕中部滚动

  等待类
    wait: 模板名              等它出现，超时算失败
    wait_any: [a, b]         等其中任意一个出现
    wait_stable: 秒           等画面停止变化
    sleep: 秒                 死等

  组织类
    repeat: {times: 3, steps: [...]}     循环
    if: 模板名 {then: [...], else: [...]} 条件分支
    if_red: {region: [x1,y1,x2,y2], min: 0.08, then: [...], else: [...]}
                             区域内红色像素占比够不够 —— 专门用来判断
                             「图标上有没有红色数字角标」。角标里的数字会变，
                             做模板只认得出某一个数字，看颜色才稳。
    note: 文字                            往日志里写一行

  通用可选参数（任何步骤都能加）
    optional: true           失败就跳过，不中断任务
    timeout: 秒               覆盖默认超时
    threshold: 0.9            覆盖默认匹配阈值
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import flows
from .device import Device, NotFound


@dataclass
class Step:
    op: str
    args: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        val = self.args.get("target", self.args.get("value", ""))
        extra = ""
        if self.args.get("optional"):
            extra = " (可选)"
        return f"{self.op} {val}{extra}".strip()


def _v(a: dict, key: str = "target", default=None):
    """取参数：先看指定 key，再退回通用 value。"""
    if key in a:
        return a[key]
    return a.get("value", default)


def parse_steps(raw: list) -> list[Step]:
    steps: list[Step] = []
    for item in raw or []:
        if isinstance(item, str):
            steps.append(Step(item.strip(), {}))
            continue
        if not isinstance(item, dict):
            raise ValueError(f"看不懂的步骤：{item!r}")
        if "op" in item:
            args = {k: v for k, v in item.items() if k != "op"}
            steps.append(Step(str(item["op"]), args))
            continue
        op, val = next(iter(item.items()))
        # 兄弟键（timeout / threshold / optional …）要一并带上，
        # 否则 `- wait: xxx` 下面再写 `threshold: 0.9` 会被悄悄丢掉。
        extra = {k: v for k, v in item.items() if k != op}
        if isinstance(val, dict):
            args = dict(val)
        elif val is None:
            args = {}
        else:
            args = {"value": val}
        args.update(extra)
        steps.append(Step(str(op), args))
    return steps


def collect_needs(steps: list[Step]) -> list[str]:
    """提取这段步骤依赖的模板名，启动前统一检查。"""
    needs: list[str] = []
    for st in steps:
        if st.op in ("tap_img", "wait"):
            name = _v(st.args, "target")
            if isinstance(name, str) and name and name not in needs:
                needs.append(name)
        elif st.op == "wait_any":
            for n in _v(st.args, "target") or []:
                if n not in needs:
                    needs.append(n)
        elif st.op == "repeat":
            for n in collect_needs(parse_steps(st.args.get("steps", []))):
                if n not in needs:
                    needs.append(n)
        elif st.op == "if":
            cond = _v(st.args, "target")
            if isinstance(cond, str) and cond and cond not in needs:
                needs.append(cond)
            for key in ("then", "else"):
                for n in collect_needs(parse_steps(st.args.get(key, []))):
                    if n not in needs:
                        needs.append(n)
        elif st.op == "if_red":
            for key in ("then", "else"):
                for n in collect_needs(parse_steps(st.args.get(key, []))):
                    if n not in needs:
                        needs.append(n)
    return needs


# ---------------------------------------------------------------- 执行
def exec_steps(dev: Device, steps: list[Step], log, depth: int = 0) -> None:
    """按顺序执行一组步骤。

    某一步「等不到东西」时，先看一眼**上一步点的那个按钮还在不在**：
    还在 = 那一下被游戏吞了（弹窗是「由小放大」弹进来的，动画那零点几秒里的
    点击不生效；录制的脚本一步接一步跑得很快，很容易正好撞上，用户看到的就是
    「点太快，第一次没关闭弹窗」）。这种情况重点一次上一步，再重试本步，
    最多两轮。按钮已经不在了就不乱点 —— 说明点击是生效的，只是下一页慢。
    """
    pad = "  " * (depth + 1)
    retries: dict[int, int] = {}
    i = 0
    while i < len(steps):
        dev.check_cancel()
        st = steps[i]
        head = f"{pad}{i + 1:02d}."
        try:
            _exec_one(dev, st, log, head, depth)
        except NotFound as e:
            if st.args.get("optional"):
                log.warning(f"{head} {st} 跳过 —— {e}")
                i += 1
                continue
            # 上一步点了「出击」进战斗、战斗还没打完 → 等它打完再重试本步。
            # 必须先于「重点上一步」：战斗中如果去重点上一步的出击按钮，
            # 有可能重复消费次数/体力。只在确实看到 battle_hud 时才等。
            if i > 0 and retries.get(f"battle{i}", 0) < 1 and _in_battle_now(dev):
                retries[f"battle{i}"] = 1
                if _wait_recorded_battle(dev, log):
                    log.warning(f"{head} 上一步进了战斗，已等它打完 → 重试本步")
                    continue
            if i > 0 and retries.get(i, 0) < 2 and _retap_prev(dev, steps[i - 1], log, pad):
                retries[i] = retries.get(i, 0) + 1
                log.warning(f"{head} {st} 没等到，重点上一步后再试（第 {retries[i]} 次）")
                continue
            raise
        i += 1


def _in_battle_now(dev) -> bool:
    """当前是否正停在战斗画面。统一走 flows.in_battle —— 认 WAVE 和 TIME
    两种 HUD（不同活动样式不同），别在这里另写一套判定漏掉某一种。"""
    try:
        dev.screen(True)
        return flows.in_battle(dev)
    except Exception:
        return False


def _wait_recorded_battle(dev: Device, log=None) -> bool:
    """当前在战斗中：等到它打完。返回是否确实等过。

    只在确实看到战斗 HUD 时被调用，战斗中按返回键会弹游戏菜单，所以只等不点。
    战斗时长跨度很大：快的几秒，慢的三五分钟（限时战斗/高难度），所以上限给
    10 分钟，且只要 HUD 还在就一直等 —— 绝不因为「等太久」提前放弃去乱点，
    否则会停在战斗中或结算页上，后面全部连锁失败。
    """
    def say(msg):
        if log is not None:
            log.info("  " + msg)

    say("录制步骤触发了一场战斗，等它打完（最长 10 分钟）…")
    deadline = time.time() + 600.0
    finished = False
    while time.time() < deadline:
        dev.check_cancel()
        dev.screen(True)
        if not flows.in_battle(dev):
            finished = True
            break
        time.sleep(1.5)
    # 打完后结算页可能晚几秒渲染。给 6 秒缓冲（期间画面可能一直在动，
    # wait_stable 会提前返回），确保下一步的「关闭」模板能找到，而不是又超时。
    if finished:
        dev.wait_stable(timeout=6)
        _end = time.time() + 3.0   # 固定短等：战斗→结算转场时画面常是静止的
        while time.time() < _end:
            dev.check_cancel()     # 这 3 秒里点停止也要能立刻停
            time.sleep(min(0.2, max(0.0, _end - time.time())))
        say("  战斗结束，继续后面的录制步骤")
    else:
        if log is not None:
            log.warning("  战斗等了 10 分钟还没结束，继续后续步骤（可能失败，请看现场）")
        dev.dump("recorded_battle_timeout")
    return True


def _settle_after_tap(dev: Device, seconds: float = 0.45) -> None:
    """点完之后多给界面一点时间把弹窗弹完/把页面翻过去。

    Device.tap 自带 0.55~1.1s 的随机间隔，但录制脚本是「找到就点、点完立刻找下一个」，
    弹窗的小幅放大动画还没结束就发下一步，游戏会把那一下吞掉。这里再补一小段
    可被打断的等待；长脚本每步多半零点四秒，比事后重试省时间。

    「录制时手动等了一场战斗、等待没被录进去」的情况由 exec_steps 在下一步
    等不到按钮时处理（见那里对 _wait_recorded_battle 的调用），不在每步都检测，
    避免普通步骤被检测拖慢。
    """
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        dev.check_cancel()
        time.sleep(min(0.15, max(0.0, end - time.time())))
    dev.invalidate()


def _retap_prev(dev: Device, prev: Step, log, pad: str) -> bool:
    """上一步点空了就再点一次（只在「目标按钮还在屏幕上」时才动手）。

    只认 tap_img：它有明确目标，能验证点击有没有生效。
    tap_xy 只是坐标，看不出点没点中，盲点可能重复消费（比如又买一次），所以不碰。
    """
    if prev.op != "tap_img":
        return False
    name = _v(prev.args)
    if not isinstance(name, str) or not name or not dev.matcher.has(name):
        return False
    th = prev.args.get("threshold")
    region = prev.args.get("region")
    box = None
    if region and len(region) >= 4:
        x1, y1, x2, y2 = (int(v) for v in region[:4])
        box = (x1, y1, max(1, x2 - x1), max(1, y2 - y1))
    m = dev.matcher.find(dev.screen(True), name, th, region=box)
    if m is None:
        return False
    off = prev.args.get("offset") or (0, 0)
    x, y = int(m.cx) + int(off[0]), int(m.cy) + int(off[1])
    log.warning(f"{pad}  上一步 [{name}] 还在画面上（点空了）→ 重点一次 @({x},{y})")
    dev.tap(x, y)
    dev.wait_stable(timeout=2.5)
    return True


def _find_in_region(dev: Device, name: str, region, timeout: float,
                    threshold) -> tuple[int, int, float]:
    """只在屏幕的一块区域里找模板，返回 (cx, cy, 相似度)。

    列表页常有几十个一模一样的图标（比如「可送礼」的感叹号），全屏找会命中
    边角那种点不到的位置。圈定区域既能避开它们，也能少算一大块像素。
    """
    x1, y1, x2, y2 = (int(v) for v in region)
    box = (x1, y1, max(1, x2 - x1), max(1, y2 - y1))
    deadline = time.time() + max(0.1, timeout)
    while True:
        dev.check_cancel()
        screen = dev.screen(True)
        # 一定要把「整屏」交给 matcher，region 只作为参数传下去。
        # 自己先裁好再传会踩坑：模板缩放是按传入画面的宽度算的，
        # 传 1960 宽的裁剪块，模板会被缩成 0.76 倍，永远匹配不上。
        m = dev.matcher.find(screen, name, threshold, region=box)
        if m is not None:
            return m.cx, m.cy, m.score
        if time.time() >= deadline:
            raise NotFound(f"区域内没找到 [{name}]：{region}")
        time.sleep(0.25)


def _exec_one(dev: Device, st: Step, log, head: str, depth: int) -> None:
    op, a = st.op, st.args

    if op == "note":
        log.info(f"{head} · {_v(a, 'value', '')}")
        return

    if op == "home":
        if not flows.ensure_home(dev):
            raise NotFound("回不到主页，可能卡在弹窗里")
        log.info(f"{head} 已回到主页")
        return

    if op == "back":
        dev.back()
        dev.wait_stable(timeout=5)
        log.info(f"{head} 返回")
        return

    if op == "tap_img":
        name = _v(a)
        if not isinstance(name, str) or not name:
            raise ValueError(f"tap_img 缺少模板名：{a}")
        if not dev.matcher.has(name):
            raise NotFound(f"模板 [{name}] 不存在")
        to = float(a.get("timeout", 6))
        th = a.get("threshold")
        off = a.get("offset") or (0, 0)
        region = a.get("region")
        if region:
            cx, cy, score = _find_in_region(dev, name, region, to, th)
        else:
            m = dev.wait(name, to, th)
            cx, cy, score = m.cx, m.cy, m.score
        x, y = int(cx) + int(off[0]), int(cy) + int(off[1])
        log.info(f"{head} 点击 [{name}] @({x},{y}) 相似度 {score:.3f}")
        dev.tap(x, y)
        _settle_after_tap(dev, float(a.get("settle", 0.45)))
        return

    if op == "tap_xy":
        pt = _v(a)
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            raise ValueError(f"tap_xy 需要 [x, y]：{a}")
        dev.tap(int(pt[0]), int(pt[1]))
        log.info(f"{head} 点击坐标 ({int(pt[0])},{int(pt[1])})")
        _settle_after_tap(dev, float(a.get("settle", 0.45)))
        if a.get("wait"):
            dev.wait_stable(timeout=float(a["wait"]))
        return

    if op == "wait":
        name = _v(a)
        if not dev.matcher.has(name):
            raise NotFound(f"模板 [{name}] 不存在")
        m = dev.wait(name, float(a.get("timeout", 10)), a.get("threshold"))
        log.info(f"{head} 出现 {m}")
        return

    if op == "wait_any":
        names = _v(a) or []
        got, m = dev.wait_any(list(names), float(a.get("timeout", 10)), a.get("threshold"))
        log.info(f"{head} 命中 {got} @({m.cx},{m.cy})")
        return

    if op == "wait_stable":
        secs = float(_v(a, "timeout", _v(a, "value", 8)) or 8)
        dev.wait_stable(timeout=secs)
        log.info(f"{head} 画面已稳定")
        return

    if op == "sleep":
        import time
        secs = float(_v(a, "value", a.get("seconds", 1)) or 1)
        log.info(f"{head} 等待 {secs:.1f}s")
        # 分段睡：录制脚本里常见 sleep 10~30s，整段睡过去按了停止要等很久才停
        end = time.time() + secs
        while time.time() < end:
            dev.check_cancel()
            time.sleep(min(0.3, max(0.0, end - time.time())))
        dev.invalidate()
        return

    if op == "swipe":
        pt = _v(a) or []
        if len(pt) < 4:
            raise ValueError(f"swipe 需要 [x1,y1,x2,y2]：{a}")
        dev.swipe(int(pt[0]), int(pt[1]), int(pt[2]), int(pt[3]),
                  int(a.get("duration", 400)))
        log.info(f"{head} 滑动")
        return

    if op == "scroll":
        direction = _v(a, "value", "down") or "down"
        dev.scroll(str(direction), float(a.get("ratio", 0.4)))
        log.info(f"{head} 滚动 {direction}")
        return

    if op == "repeat":
        times = int(a.get("times", 1))
        sub = parse_steps(a.get("steps", []))
        for n in range(times):
            dev.check_cancel()
            log.info(f"{head} 循环 {n + 1}/{times}")
            exec_steps(dev, sub, log, depth + 1)
        return

    if op == "if":
        cond = _v(a)
        th = a.get("threshold")
        if dev.exists(cond, th):
            log.info(f"{head} 条件成立 [{cond}]")
            exec_steps(dev, parse_steps(a.get("then", [])), log, depth + 1)
        else:
            log.info(f"{head} 条件不成立 [{cond}]")
            exec_steps(dev, parse_steps(a.get("else", [])), log, depth + 1)
        return

    if op == "if_red":
        region = a.get("region")
        if not region or len(region) < 4:
            raise ValueError(f"if_red 需要 region [x1,y1,x2,y2]：{a}")
        need = float(a.get("min", 0.08))
        ratio = dev.red_ratio(region)
        hit = ratio >= need
        log.info(f"{head} 红色占比 {ratio * 100:.1f}%（门槛 {need * 100:.0f}%）→ "
                 f"{'有红点' if hit else '没红点'}")
        exec_steps(dev, parse_steps(a.get("then" if hit else "else", [])), log, depth + 1)
        return

    raise ValueError(f"未知步骤命令：{op}（{head.strip()}）")


# ---------------------------------------------------------------- 载入
def load_recipe(path: str | Path) -> dict:
    p = Path(path)
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_recipe(path: str | Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, indent=2)
