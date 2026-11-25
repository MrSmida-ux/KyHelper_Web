"""打包成「不装 Python 也能跑」的 Windows 程序。

    python build.py            # 只打包，产出 release/KyHelper_Web/
    python build.py --zip      # 打包 + 压成 dist/KyHelper_Web-<版本>-win64.zip

产物是**目录版**（onedir），不是单文件 exe：
  - 单文件每次启动都要把 opencv/numpy 解压到临时目录，首次 5~10 秒，目录版秒开；
  - 目录版杀毒软件误报少，出问题也能直接看 _internal 里的东西。

打包用的是 `.venv-build` 里的干净环境（只装了 opencv/numpy/PyYAML/pyinstaller），
不用系统里的全局 Python —— 那里面有一堆无关包（还有会把 stdlib 顶掉的 pathlib
backport），打出来又大又容易出怪问题。

目录约定（`core/paths.py`）：
    exe 同级的 config.yaml / logs/ / assets/templates/ / tasks/recipes/ 可写，
    内置资源在 _internal/ 里只读；首次运行会把模板与录制任务复制到外面一份。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "release"
WORK = ROOT / "build"
NAME = "KyHelper_Web"
VENV_PY = ROOT / ".venv-build" / "Scripts" / "python.exe"

# 打进包里的资源：exe 里读取，首次运行时再复制到可写目录
DATA = [
    ("assets/templates", "assets/templates"),   # 354 张识别模板
    ("web", "web"),                             # 控制台前端
    ("tasks/recipes", "tasks/recipes"),         # 录制任务 + _embedded
    ("config.yaml", "."),                       # 默认配置，可直接改
    ("README.md", "."),                         # 出问题时能查
]

EXCLUDES = [
    "tkinter", "matplotlib", "pandas", "scipy", "pytest", "setuptools",
    "PIL", "IPython", "notebook", "torch", "tensorflow", "av",
]


def version() -> str:
    p = ROOT / "VERSION"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "1.0.0"


def pick_python() -> Path:
    if VENV_PY.exists():
        return VENV_PY
    print("[!] 没找到 .venv-build，用当前解释器（可能把无关包也打进去）")
    return Path(sys.executable)


def check_env(py: Path) -> None:
    code = ("import PyInstaller, cv2, numpy, yaml;"
            "print('pyinstaller', PyInstaller.__version__, '| cv2', cv2.__version__)")
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit("[x] 构建环境不完整：先装 pyinstaller/opencv/numpy/PyYAML")
    print(f"[i] 构建环境 {py}\n    {r.stdout.strip()}")


def clean() -> None:
    """只清 build/ 中间产物；release/ 里已有成品就别动（删上千文件容易被安全策略拦）。"""
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    if DIST.exists():
        print(f"[!] {DIST} 已存在，直接覆盖同名文件；想干净重来请手动删掉它")


def build(py: Path) -> Path:
    cmd = [
        str(py), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--console",                      # 保留控制台：出问题能看到报错，关掉窗口即退出
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
        "--paths", str(ROOT),
    ]
    for src, dst in DATA:
        p = ROOT / src
        if not p.exists():
            raise SystemExit(f"[x] 缺少资源 {src}")
        cmd += ["--add-data", f"{p}{os.pathsep}{dst}"]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(str(ROOT / "run.py"))

    print("[i] PyInstaller 开始，3~5 分钟…")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"[x] 打包失败（{r.returncode}）")

    out = DIST / NAME
    if not (out / f"{NAME}.exe").exists():
        raise SystemExit("[x] 没生成 exe")
    print(f"[✓] 打包完成 {time.time() - t0:.0f}s → {out}")
    return out


def write_readme(out: Path) -> None:
    # 配置也放一份在根目录：第一次运行前就能改，不用等它自动解出来
    cfg = ROOT / "config.yaml"
    if cfg.exists():
        shutil.copyfile(cfg, out / "config.yaml")
    (out / "使用说明.txt").write_text(
        "双穹幻想 Burst 网页版助手\r\n"
        "========================\r\n\r\n"
        "1. 双击 KyHelper_Web.exe 启动，会自动打开控制面板并拉起一个独立的 Chrome。\r\n"
        "   第一次要在那个 Chrome 窗口里登录一次，之后不用再登（登录态存在 chrome-data\\）。\r\n"
        "2. 需要本机装有 Chrome 或 Edge（任一即可），程序不自己带浏览器。\r\n"
        "3. 配置在同目录 config.yaml，日志在 logs\\，双击运行前别删这两个。\r\n"
        "4. 想换识别模板：把 png 丢进 assets\\templates\\，文件名跟原来一致就会覆盖。\r\n"
        "5. 关掉黑色控制台窗口 = 退出程序；任务运行中也可以点面板上的「停止」。\r\n\r\n"
        "杀掉进程：任务管理器里结束 KyHelper_Web.exe。\r\n"
        "详细的机制说明见同目录 README.md。\r\n",
        encoding="utf-8")


def size_of(p: Path) -> str:
    n = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return f"{n / 1024 / 1024:.0f} MB"


def make_zip(out: Path) -> Path:
    base = DIST / f"{NAME}-{version()}-win64"
    print("[i] 压缩中…")
    z = shutil.make_archive(str(base), "zip", root_dir=str(DIST), base_dir=NAME)
    return Path(z)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包免 Python 的 Windows 版本")
    ap.add_argument("--zip", action="store_true", help="额外生成 release 用的 zip")
    ap.add_argument("--keep", action="store_true", help="不清理 build/ 中间文件")
    args = ap.parse_args()

    print(f"== 打包 {NAME} v{version()} ==")
    py = pick_python()
    check_env(py)
    if not args.keep:
        clean()
    out = build(py)
    write_readme(out)
    print(f"[i] 目录 {size_of(out)}：{out}")

    if args.zip:
        z = make_zip(out)
        print(f"[✓] 压缩包 {z.stat().st_size / 1024 / 1024:.0f} MB → {z}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
