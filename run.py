"""命令行入口。

    python run.py                 起控制面板（默认，推荐）
    python run.py --probe         环境自检：浏览器、画面、识别、帧率
    python run.py --shot          存一张当前画面
    python run.py -l              列出任务
    python run.py -t mail -t ruins  只跑指定任务
    python run.py --dry            只识别不点击

控制面板也可以用浏览器直接开 http://127.0.0.1:8765 —— 它就是个网页。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import Cancelled, NotFound, bootstrap, paths  # noqa: E402
from core.log import save_shot, setup  # noqa: E402
from tasks import load_jobs, run_jobs  # noqa: E402
from tasks.registry import load_options, resolved_options  # noqa: E402

BOOT = """
  双穹幻想 Burst · 网页版日常助手
  -----------------------------------------
"""


def _mk(cfg, log):
    game, cap, matcher, dev = bootstrap.build(cfg, ROOT, log)
    return game, cap, matcher, dev


def cmd_probe() -> int:
    cfg = bootstrap.load_config(ROOT)
    log = setup("INFO", str(ROOT / "logs"))
    print(BOOT)
    game, cap, matcher, dev = _mk(cfg, log)

    print(f"模板目录   {matcher.dir}（{len(list(matcher.dir.glob('*.png')))} 张）")
    print(f"浏览器     {game.chrome or '未找到'}")
    print(f"调试端口   {game.port}")
    print(f"用户目录   {game.user_data}")

    t0 = time.time()
    ok = game.prepare(force=True)
    print(f"连接画面   {'成功' if ok else '失败'}  {time.time() - t0:.1f}s  状态={game.status} {game.hint}")
    if not ok:
        return 2

    print(f"驱动方式   {game.mode}")
    print(f"虚拟视口   {game.vw}×{game.vh}")
    print(f"游戏区域   x={game.geo['x']:.0f} y={game.geo['y']:.0f} "
          f"{game.geo['w']:.0f}×{game.geo['h']:.0f}")
    if not cap.start():
        print("画面流    启动失败")
        return 3
    got = cap.wait_new(0, timeout=6)
    print(f"首帧       {'到手' if got else '超时'}")

    time.sleep(2.0)
    img = cap.frame(force=True)
    print(f"画面尺寸   {img.shape[1]}×{img.shape[0]}" if img is not None else "画面尺寸   —")
    print(f"画面帧率   {cap.fps_now()} fps（screencast）")

    # 识别性能：拿主页锚点试一下
    tpl = (cfg.get("game") or {}).get("home_template", "btn_tokuten")
    if matcher.has(tpl):
        gray = cap.gray(force=True)
        n = 20
        t0 = time.time()
        for _ in range(n):
            matcher.find_gray(gray, tpl)
        per = (time.time() - t0) / n * 1000
        hit = matcher.find_gray(gray, tpl)
        print(f"识别耗时   {per:.1f} ms/次（{tpl}）")
        print(f"主页锚点   {hit if hit else '没找到（可能不在主页，正常）'}")
    else:
        print(f"识别耗时   跳过（缺模板 {tpl}）")

    cap.stop()
    return 0


def cmd_shot() -> int:
    cfg = bootstrap.load_config(ROOT)
    log = setup("INFO", str(ROOT / "logs"))
    game, cap, matcher, dev = _mk(cfg, log)
    if not game.prepare(force=True):
        print(f"连不上画面：{game.hint}")
        return 2
    cap.start()
    img = None
    for _ in range(40):
        cap.wait_new(0, timeout=0.4)
        img = cap.frame(force=True)
        if img is not None:
            break
    if img is None:
        print("没拿到画面")
        return 3
    p = save_shot(img, "manual", str(ROOT / "logs" / "shots"))
    print(f"已保存：{p}  {img.shape[1]}×{img.shape[0]}")
    cap.stop()
    return 0


def cmd_list() -> int:
    jobs = load_jobs(ROOT)
    opts = load_options(ROOT)
    for j in jobs:
        sel = f"  {resolved_options(j, opts)}" if j.options else ""
        print(f"  {j.key:22s} {j.name}{sel}")
    print(f"\n共 {len(jobs)} 个任务")
    return 0


def cmd_run(keys: list[str], dry: bool) -> int:
    cfg = bootstrap.load_config(ROOT)
    log = setup("INFO", str(ROOT / "logs"))
    game, cap, matcher, dev = _mk(cfg, log)
    if dry:
        dev.tap = lambda x, y, jitter=True: log.info(f"  [dry] tap ({x},{y})")
        dev.swipe = lambda *a, **k: log.info("  [dry] swipe")
        dev.back = lambda: log.info("  [dry] back")

    if not game.prepare(force=True):
        print(f"连不上画面：{game.hint}")
        return 2
    cap.start()
    cap.wait_new(0, timeout=6)

    jobs = load_jobs(ROOT)
    want = [j for j in jobs if j.key in keys] or jobs
    opts = load_options(ROOT)
    opts_map = {j.key: resolved_options(j, opts) for j in want if j.options}
    print(BOOT)
    ok, fail = run_jobs(dev, want, opts_map=opts_map)
    print(f"\n成功 {ok} / 失败 {fail}")
    cap.stop()
    return 0 if fail == 0 else 1


def main() -> int:
    # 打包成 exe 双击时，当前目录可能是桌面，先切到程序目录再动相对路径
    paths.chdir_app()
    # 首次运行把 config.yaml / 模板 / 录制任务铺到可写目录（命令行模式也要）
    paths.ensure_layout()
    ap = argparse.ArgumentParser(description="双穹幻想 Burst 网页版助手")
    ap.add_argument("--probe", action="store_true", help="环境自检")
    ap.add_argument("--shot", action="store_true", help="存一张当前画面")
    ap.add_argument("-l", "--list", action="store_true", help="列出任务")
    ap.add_argument("-t", "--task", action="append", default=[], help="只跑指定任务（可多次）")
    ap.add_argument("--dry", action="store_true", help="只识别不点击")
    ap.add_argument("--no-ui", action="store_true", help="不走界面，直接跑任务")
    args = ap.parse_args()

    if args.probe:
        return cmd_probe()
    if args.shot:
        return cmd_shot()
    if args.list:
        return cmd_list()
    if args.task or args.no_ui:
        return cmd_run(args.task, args.dry)

    import gui_server
    gui_server.main(open_browser=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
