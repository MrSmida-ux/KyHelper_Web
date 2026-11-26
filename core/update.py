"""自动更新：查 GitHub Release → 下载 zip → 退出程序让 PowerShell 换包重启。

为什么用 Release 而不是 git pull：
    免安装版是 PyInstaller 的目录版（KyHelper_Web.exe + _internal），用户机器上
    没有 git，也没有 Python。能换的只有「压缩包解开覆盖」这一条路。

为什么换包要另起一个进程：
    Windows 上正在运行的 exe 和被它映射的 _internal/*.pyd 都锁着，自己删不掉自己。
    所以下载完写一个 update.ps1，由它等本进程退出，再把 _internal 和 exe 换掉、
    重新拉起新版本。脚本全程只用 ASCII，路径靠参数传，避免中文路径的编码坑。

换包范围（**非常重要**）：
    只换 KyHelper_Web.exe 和 _internal/ —— 内置的 web/、assets/templates、
    tasks/recipes、config.yaml 都在 _internal 里，运行时由 core/paths.ensure_layout
    把「缺的」补到外面可写目录。这样：
      · 用户改过的 config.yaml、裁过的模板、录的任务不会被新版冲掉；
      · 新版新增的模板会在下次启动时自动铺出来（ensure_layout 只补缺的）。
    外面的 logs/、chrome-data/（登录态）、update/ 一律不碰。

仓库私有/改名的情况：
    GitHub API 会返回 404。这里当「检查不到」处理，不报错、不影响使用；
    等仓库重新 public 后下一次轮询自然就通了。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from core import paths

DEFAULT_REPO = "MrSmida-ux/KyHelper_Web"
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
UA = "KyHelper-Web-Updater/1.0 (+https://github.com/MrSmida-ux/KyHelper_Web)"
EXE_NAME = "KyHelper_Web.exe"

# PowerShell 换包脚本。纯 ASCII（不含中文），路径/进程号全部走命令行参数。
_PS1 = r"""param(
  [int]$ProcId = 0,
  [string]$App = "",
  [string]$Zip = ""
)
$ErrorActionPreference = "Continue"
$log = Join-Path $App "logs\update.log"

function W([string]$m) {
  $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
  try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 } catch { }
}

W "==== update start  pid=$ProcId"
W "app=$App"
W "zip=$Zip"

# 1) wait for the app to exit (max 90s), then let handles settle
if ($ProcId -gt 0) {
  for ($i = 0; $i -lt 180; $i++) {
    $p = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if (-not $p) { break }
    Start-Sleep -Milliseconds 500
  }
}
Start-Sleep -Milliseconds 1500

# 2) unzip into update\unpack
$stage = Join-Path $App "update\unpack"
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
  Expand-Archive -LiteralPath $Zip -DestinationPath $stage -Force -ErrorAction Stop
} catch {
  W "unzip failed: $($_.Exception.Message)"
  exit 1
}

# 3) locate program root inside the package (normally unpack\KyHelper_Web\)
$src = $null
foreach ($d in @(Get-ChildItem -LiteralPath $stage -Directory)) {
  if (Test-Path -LiteralPath (Join-Path $d.FullName "KyHelper_Web.exe")) { $src = $d.FullName; break }
}
if (-not $src -and (Test-Path -LiteralPath (Join-Path $stage "KyHelper_Web.exe"))) { $src = $stage }
if (-not $src) { W "no KyHelper_Web.exe in package"; exit 1 }

$newInt = Join-Path $src "_internal"
if (-not (Test-Path -LiteralPath $newInt)) { W "no _internal in package"; exit 1 }

# 4) move old files into update\backup (same volume, instant), then put new ones in
$bk = Join-Path $App "update\backup"
if (Test-Path -LiteralPath $bk) { Remove-Item -LiteralPath $bk -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Path $bk -Force | Out-Null

$exe = Join-Path $App "KyHelper_Web.exe"
$int = Join-Path $App "_internal"
$oldExe = Join-Path $bk "KyHelper_Web.exe"
$oldInt = Join-Path $bk "_internal"

try {
  if (Test-Path -LiteralPath $int) { Move-Item -LiteralPath $int -Destination $oldInt -Force -ErrorAction Stop }
  if (Test-Path -LiteralPath $exe) { Move-Item -LiteralPath $exe -Destination $oldExe -Force -ErrorAction Stop }
  Move-Item -LiteralPath $newInt -Destination $int -Force -ErrorAction Stop
  Copy-Item -LiteralPath (Join-Path $src "KyHelper_Web.exe") -Destination $exe -Force -ErrorAction Stop
  # loose files (README / VERSION / docs...) follow the update; config.yaml is kept
  Get-ChildItem -LiteralPath $src -File | Where-Object { $_.Name -ne "KyHelper_Web.exe" -and $_.Name -ne "config.yaml" } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $App $_.Name) -Force -ErrorAction SilentlyContinue
  }
} catch {
  W "swap failed: $($_.Exception.Message) -- rolling back"
  if (Test-Path -LiteralPath $int) { Remove-Item -LiteralPath $int -Recurse -Force -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $exe) { Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $oldInt) { Move-Item -LiteralPath $oldInt -Destination $int -Force -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $oldExe) { Move-Item -LiteralPath $oldExe -Destination $exe -Force -ErrorAction SilentlyContinue }
  W "rolled back, restarting old version"
  Start-Process -FilePath $exe -WorkingDirectory $App
  exit 1
}

W "swap done, restarting"
Start-Process -FilePath $exe -WorkingDirectory $App -ArgumentList "--updated"

# 5) cleanup: keep backup for manual rollback, drop unpack dir and zip
Start-Sleep -Seconds 3
Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
W "==== update done"
"""


# ---------------------------------------------------------------- 版本号
def _clean(v: str) -> str:
    v = (v or "").strip()
    return v[1:] if v[:1] in ("v", "V") else v


def version_key(v: str) -> tuple[int, ...]:
    """"1.2.3-beta1" → (1,2,3)；取不出数字就是 (0,)。"""
    head = re.split(r"[-+]", _clean(v))[0]
    nums = re.findall(r"\d+", head)
    return tuple(int(x) for x in nums[:4]) or (0,)


def newer(a: str, b: str) -> bool:
    ka, kb = version_key(a), version_key(b)
    n = max(len(ka), len(kb))
    return ka + (0,) * (n - len(ka)) > kb + (0,) * (n - len(kb))


def current_version() -> str:
    """版本号优先级：可写目录 VERSION（用户看得见、也能改）→ 包内 VERSION → 代码常量。"""
    for p in (paths.app_dir() / "VERSION", paths.res_dir() / "VERSION"):
        try:
            v = _clean(p.read_text(encoding="utf-8"))
            if v:
                return v
        except OSError:
            continue
    try:
        from core.version import VERSION
        return _clean(VERSION)
    except Exception:
        return "0.0.0"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _open(url: str, timeout: float = 8.0, headers: dict | None = None):
    """先按系统代理走，失败再直连一次。

    国内机器上 https_proxy 经常是「设了但代理没开」，那种情况 urllib 会直接报错；
    直连一次能救回来。反过来（必须走代理才通）也覆盖到了。
    """
    hd = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    hd.update(headers or {})
    req = urllib.request.Request(url, headers=hd)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except (urllib.error.URLError, OSError):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)


def _pick_asset(assets: list[dict]) -> dict | None:
    """在 Release 附件里挑 Windows 免安装包。

    不认死文件名：用户可能把附件改成「KyHelper_Web-1.0.0-win64」这种**没有扩展名**
    的名字（Release 页面照样能下），所以按特征打分，而不是 endswith(".zip")：
      · zip 包 +40，名字带 win/win64/windows +30，带项目名 +10
      · 源码包（Source code / .tar.gz / .7z）直接 -100，别去下它
    分数够才返回；全是源码包就当「这个 Release 没附程序包」。
    """
    items = [a for a in (assets or []) if a.get("browser_download_url")]
    if not items:
        return None

    def score(a: dict) -> int:
        n = str(a.get("name", "")).lower()
        s = 0
        if n.endswith(".zip"):
            s += 40
        if "win64" in n or "win" in n or "windows" in n:
            s += 30
        if "kyhelper" in n:
            s += 10
        if "source" in n or n.endswith((".tar.gz", ".tar", ".tar.xz", ".7z", ".rar")):
            s -= 100
        return s

    best = max(items, key=score)
    return best if score(best) >= 30 else None


def fetch_latest(repo: str = DEFAULT_REPO, timeout: float = 8.0) -> dict:
    """查最新 Release。所有异常都变成 {"ok": False, "hint": ...}，绝不抛出去。"""
    cur = current_version()
    out = {
        "ok": False, "current": cur, "latest": "", "has_update": False,
        "notes": "", "url": "", "asset": None, "size": 0, "published": "",
        "hint": "", "checked_at": int(time.time()), "repo": repo,
        "can_apply": False, "frozen": frozen(),
    }
    api = API_LATEST.format(repo=repo)
    try:
        with _open(api, timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            out["hint"] = "仓库暂不可访问（未公开或已改名），先跳过"
        elif e.code in (403, 429):
            out["hint"] = "GitHub 限流了，过一会儿再试"
        else:
            out["hint"] = f"检查失败：HTTP {e.code}"
        return out
    except Exception as e:
        out["hint"] = f"连不上 GitHub（{type(e).__name__}）"
        return out

    try:
        data = json.loads(raw or "{}")
    except Exception:
        out["hint"] = "GitHub 返回的不是 JSON"
        return out
    if not isinstance(data, dict) or not data.get("tag_name"):
        out["hint"] = "没解析到版本号"
        return out

    latest = _clean(str(data.get("tag_name")))
    asset = _pick_asset(data.get("assets") or [])
    out.update({
        "ok": True,
        "latest": latest,
        "has_update": bool(latest) and newer(latest, cur),
        "notes": str(data.get("body") or "")[:4000],
        "url": str(data.get("html_url") or ""),
        "published": str(data.get("published_at") or ""),
        "asset": ({"name": asset.get("name"), "url": asset.get("browser_download_url"),
                   "size": int(asset.get("size") or 0)} if asset else None),
        "size": int(asset.get("size") or 0) if asset else 0,
    })
    if out["has_update"] and not asset:
        out["hint"] = "有新版本，但这个 Release 没附 Windows 压缩包"
    elif out["has_update"] and not frozen():
        out["hint"] = "源码运行：git pull 更新，别用这里的自动更新"
    elif out["has_update"]:
        out["can_apply"] = os.name == "nt"
        if not out["can_apply"]:
            out["hint"] = "自动换包目前只支持 Windows"
    return out


# ---------------------------------------------------------------- 更新器
class Updater:
    """后台轮询 + 前台下载换包。UI 只读 snapshot()，不会因为网络卡住。"""

    def __init__(self, root: Path, log, repo: str = DEFAULT_REPO,
                 enabled: bool = True, interval_hours: float = 6.0) -> None:
        self.root = Path(root)
        self.log = log
        self.repo = repo or DEFAULT_REPO
        self.enabled = bool(enabled)
        self.interval = max(0.5, float(interval_hours or 6)) * 3600
        self.info = {
            "ok": False, "current": current_version(), "latest": "", "has_update": False,
            "notes": "", "url": "", "asset": None, "size": 0, "published": "",
            "hint": "", "checked_at": 0, "repo": self.repo,
            "can_apply": False, "frozen": frozen(),
            "enabled": self.enabled,
            # 下载/换包状态：idle → downloading → ready → restarting，失败为 failed
            "stage": "idle", "progress": 0, "got": 0, "total": 0, "err": "",
        }
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._applying = False
        self.on_exit = None            # 换包前由 GUI 注册：停服务 + 退出进程

    # ---------- 对外 ----------
    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.info))   # 深拷贝，别让前端线程读到半截

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="update-check", daemon=True)
        self._thread.start()

    def check_now(self) -> dict:
        """同步查一次（面板上点「检查更新」）。网络慢最多也就 8 秒。"""
        if not self.enabled:
            with self._lock:
                self.info.update({"ok": False, "hint": "已在 config.yaml 里关掉更新检查"})
            return self.snapshot()
        res = fetch_latest(self.repo)
        with self._lock:
            # 下载/换包进行中别让旧结果盖掉状态
            stage = self.info.get("stage", "idle")
            self.info.update(res)
            if stage in ("downloading", "ready", "restarting"):
                self.info["stage"] = stage
        self._log_result(res)
        return self.snapshot()

    def apply_async(self) -> dict:
        """下载并换包。返回后进度看 snapshot()['stage']。"""
        with self._lock:
            if self._applying:
                return {"ok": False, "hint": "正在更新中，别急"}
            info = dict(self.info)
        if not info.get("enabled"):
            return {"ok": False, "hint": "更新检查已关闭"}
        if not frozen():
            return {"ok": False, "hint": "源码运行请用 git pull"}
        if os.name != "nt":
            return {"ok": False, "hint": "自动换包只支持 Windows"}
        if not info.get("has_update"):
            return {"ok": False, "hint": "已经是最新版本"}
        asset = info.get("asset") or {}
        if not asset.get("url"):
            return {"ok": False, "hint": "这个 Release 没有可下载的压缩包"}

        self._applying = True
        with self._lock:
            self.info.update({"stage": "downloading", "progress": 0, "got": 0,
                              "total": int(asset.get("size") or 0), "err": ""})
        threading.Thread(target=self._worker, args=(asset,), name="update-apply",
                         daemon=True).start()
        return {"ok": True, "stage": "downloading"}

    # ---------- 内部 ----------
    def _loop(self) -> None:
        time.sleep(4.0)          # 先让面板和游戏连起来，别一启动就抢网络
        while True:
            try:
                with self._lock:
                    due = time.time() - float(self.info.get("checked_at") or 0) >= self.interval
                    busy = self.info.get("stage") in ("downloading", "ready", "restarting")
                if due and not busy:
                    res = fetch_latest(self.repo)
                    with self._lock:
                        keep = {k: self.info.get(k) for k in
                                ("stage", "progress", "got", "total", "err")}
                        self.info.update(res)
                        self.info.update(keep)
                    if res.get("ok"):
                        self._log_result(res)
                    else:
                        self.log.debug(f"更新检查跳过：{res.get('hint')}")
            except Exception as e:
                self.log.debug(f"更新检查异常：{type(e).__name__}: {e}")
            time.sleep(300)

    def _log_result(self, res: dict) -> None:
        if not res.get("ok"):
            self.log.info(f"更新检查：{res.get('hint') or '失败'}")
        elif res.get("has_update"):
            self.log.info(f"发现新版本 v{res['latest']}（当前 v{res['current']}），"
                          f"可在面板顶栏点版本号更新")
        else:
            self.log.info(f"已是最新版本 v{res['current']}")

    def _upgrade_dir(self) -> Path:
        d = self.root / "update"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _set(self, **kw) -> None:
        with self._lock:
            self.info.update(kw)

    def _download(self, url: str, dest: Path) -> None:
        with _open(url, timeout=20, headers={"Accept": "*/*"}) as r:
            total = int(r.headers.get("Content-Length") or 0)
            self._set(total=total or self.info.get("size") or 0)
            got = 0
            last = 0.0
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if now - last > 0.25:
                        last = now
                        pct = int(got * 100 / total) if total else 0
                        self._set(got=got, progress=min(99, pct) if total else 0)

    def _worker(self, asset: dict) -> None:
        try:
            # 统一落成 .zip：附件可能被改名成没扩展名的文件，而 Expand-Archive
            # 只认 .zip 结尾，这里替它把名字摆正。
            ver = re.sub(r"[^\w.\-]+", "", str(self.info.get("latest") or "new"))
            dest = self._upgrade_dir() / f"KyHelper_Web-{ver}.zip"
            self.log.info(f"开始下载 v{ver}：{asset.get('name')}"
                          f"（{int(asset.get('size') or 0) / 1024 / 1024:.0f} MB）")
            self._download(str(asset["url"]), dest)
            self._set(progress=100, got=dest.stat().st_size,
                      total=dest.stat().st_size)

            if not self._verify(dest):
                dest.unlink(missing_ok=True)
                raise RuntimeError("压缩包不完整（可能没下完或不是发布包）")

            ps1 = self._upgrade_dir() / "update.ps1"
            ps1.write_text(_PS1, encoding="ascii")
            self._spawn(ps1, dest)
            self._set(stage="restarting", progress=100)
            self.log.info("新版本已就绪，正在退出并重启（约 10 秒）")
            time.sleep(1.2)
            if self.on_exit:
                self.on_exit()
        except Exception as e:
            self.log.warning(f"更新失败：{type(e).__name__}: {e}")
            self._set(stage="failed", err=f"{type(e).__name__}: {e}")
            self._applying = False

    @staticmethod
    def _verify(zip_path: Path) -> bool:
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
                if not any(n.endswith(EXE_NAME) for n in names):
                    return False
                if not any(n.endswith("_internal/") or "_internal/" in n for n in names):
                    return False
                bad = z.testzip()
                return bad is None
        except Exception:
            return False

    def _spawn(self, ps1: Path, zip_path: Path) -> None:
        exe = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
        if not os.path.exists(exe):
            exe = "powershell"
        args = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(ps1),
                "-ProcId", str(os.getpid()),
                "-App", str(self.root),
                "-Zip", str(zip_path)]
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, close_fds=True, cwd=str(self.root),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
