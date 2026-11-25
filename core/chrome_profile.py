"""把「你平时那个 Chrome」的登录态搬到工具自己的 profile 里。

为什么需要搬：CDP 只能驱动**带 --remote-debugging-port 启动**的浏览器。
你日常打开的 Chrome 没有开这个端口，外部程序就完全看不见它 —— 那是 Chrome
的安全设计，不是本程序偷懒。而且从 Chrome 136 起，官方明确禁止在「默认
用户目录」上开调试端口（防止恶意程序偷 cookie），所以「直接接管你正在看
的那个窗口」这条路在技术上被堵死了。

能走通的是搬登录态：Chrome 的 cookie 就躺在 profile 目录里 ——
`Local State` 放 DPAPI 加密的主密钥，`Default/Network/Cookies` 放密文。
把这两样（再加 Preferences / Local Storage）拷到工具自己的 profile，
工具起的那个 Chrome 打开就是已登录状态，连 Cloudflare 的 cf_clearance
一起带过来，省掉一次人机验证。

一个硬约束：Chrome 运行时对 Cookies 库是**独占**打开的（实测本机 19 个
chrome 进程时，连只读都报 Permission denied）。所以搬之前必须让它松手：
要么用户自己退出 Chrome，要么由调用方显式传 `close_chrome=True`。

原则：只读源目录，只写工具自己的 chrome-data/；覆盖前把旧的备份到
`chrome-data/.import-backup/<时间戳>/`，随时能退回去。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path


# 候选源：Chrome 优先，其次 Chromium / Edge（内核一样，cookie 格式也一样）
CANDIDATES: list[tuple[str, str]] = [
    ("Chrome", r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    ("Chromium", r"%LOCALAPPDATA%\Chromium\User Data"),
    ("Edge", r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
]

# 要搬的文件/目录（相对 user data 根）。Cache 之类一律不搬 —— 几百 MB 且没用。
# Network/Cookies 是新版位置，Default/Cookies 是老版；两个都试，存在的才拷。
ITEMS: list[tuple[str, str]] = [
    ("Local State", "file"),          # os_crypt 主密钥（DPAPI 加密），少了它 cookie 解不开
    ("Default/Preferences", "file"),  # 语言/时区/站点设置
    ("Default/Network/Cookies", "file"),
    ("Default/Network/Cookies-wal", "file"),
    ("Default/Network/Cookies-shm", "file"),
    ("Default/Network/Cookies-journal", "file"),
    ("Default/Cookies", "file"),      # 老版本 Chrome
    ("Default/Cookies-journal", "file"),
    ("Default/Local Storage", "dir"), # 有些站把 token 放 localStorage
]

# leveldb 的 LOCK 在拷过去之后毫无意义，而且正被占用时还会让整目录拷贝失败
SKIP = {"LOCK", "LOG", "LOG.old", "CURRENT.tmp"}


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p))


def cookie_db(root: Path) -> Path | None:
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        p = root / rel
        if p.exists():
            return p
    return None


def find_sources() -> list[tuple[str, Path]]:
    """本机能找到的浏览器 profile（只挑有 cookie 库的）。"""
    out: list[tuple[str, Path]] = []
    for name, raw in CANDIDATES:
        p = _expand(raw)
        if p.is_dir() and cookie_db(p) is not None:
            out.append((name, p))
    return out


def pick_source(prefer: str = "") -> tuple[str, Path] | None:
    """挑一个源。prefer 可以是名字（chrome/edge）或完整路径。"""
    sources = find_sources()
    if not sources:
        return None
    want = (prefer or "").strip().lower()
    if want:
        for name, p in sources:
            if want in name.lower() or want in str(p).lower():
                return name, p
    return sources[0]


def is_locked(db: Path | None) -> bool:
    """cookie 库是不是被占用（Chrome 开着就是）。

    直接试着只读打开 16 字节 —— 比枚举进程可靠：Chrome 后台进程那么多，
    按进程名判断经常误判，而文件锁是唯一的事实。
    """
    if db is None or not db.exists():
        return False
    try:
        with open(db, "rb") as f:
            f.read(16)
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def close_chrome(root: Path, name: str = "Chrome", wait: float = 12.0, log=None) -> bool:
    """请浏览器让出 cookie 库：先礼后兵。

    先发关闭消息（等同点窗口右上角，浏览器会把会话存下来，下次打开能恢复标签页）；
    等一半时间还占着就强杀 —— 后台常驻进程不吃关闭消息，不杀它不会走。
    """
    db = cookie_db(Path(root))
    exe = "msedge.exe" if name.lower() == "edge" else "chrome.exe"
    if log:
        log.info(f"请 {name} 让出 cookie 库（关掉它会话会被保存，重开能恢复标签页）")
    for cmd in (["taskkill", "/IM", exe], ["taskkill", "/F", "/IM", exe]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        except Exception:
            pass
        deadline = time.time() + wait / 2
        while time.time() < deadline:
            if not is_locked(db):
                return True
            time.sleep(0.4)
    return not is_locked(db)


def _count_cookies(db: Path) -> tuple[int, int]:
    """数一下拷过来的 cookie 有多少条、其中 ero-labs 相关多少条。

    只读打开（mode=ro）：拷过来之后这份已经没人占着了，读得动；
    真出异常也不当失败 —— 登录成功与否最终看画面。
    """
    total = ero = 0
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            total = int(con.execute("select count(*) from cookies").fetchone()[0])
            ero = int(con.execute(
                "select count(*) from cookies where host_key like '%ero-labs%'"
            ).fetchone()[0])
        finally:
            con.close()
    except Exception:
        pass
    return total, ero


def _copy_dir(src: Path, dst: Path) -> int:
    """拷目录，跳过 leveldb 的锁文件；单个文件失败不影响其它文件。"""
    size = 0
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for fn in files:
            if fn in SKIP or fn.endswith((".lock", ".tmp")):
                continue
            s, d = Path(root) / fn, dst / rel / fn
            try:
                shutil.copy2(s, d)
                size += d.stat().st_size
            except Exception:
                pass
    return size


def import_login(target: Path, source: Path | None = None, close: bool = False,
                 log=None) -> dict:
    """把源 profile 的登录态拷进 target（工具自己的 user data 目录）。

    返回里带 `need_close`：Chrome 正占着 cookie 库时先别动任何文件 ——
    只拷了 Local State 却没拷 Cookies，会把工具原来的登录态弄坏。
    """
    target = Path(target)
    if source is not None:
        src, src_name = Path(source), Path(source).name
    else:
        got = pick_source()
        if got is None:
            return {"ok": False, "hint": "没找到本机 Chrome / Edge 的用户目录"}
        src_name, src = got
    if not src.is_dir():
        return {"ok": False, "hint": f"源目录不存在：{src}"}
    if src.resolve() == target.resolve():
        return {"ok": False, "hint": "源目录和工具用的是同一个 profile，不用搬"}

    # ① 先确认 cookie 库能读。读不动 = Chrome 还开着，这时候一个字节都别写。
    if is_locked(cookie_db(src)):
        if not close:
            return {"ok": False, "need_close": True, "source": f"{src_name} · {src}",
                    "hint": f"{src_name} 正开着（它独占 cookie 库）。先完全退出 {src_name}，"
                            "或者让我帮你关掉它（标签页下次打开会自动恢复）"}
        close_chrome(src, src_name, log=log)
        if is_locked(cookie_db(src)):
            return {"ok": False, "need_close": True, "source": f"{src_name} · {src}",
                    "hint": f"{src_name} 还是占着 cookie 库，手动退出它（包括托盘里的后台进程）再试"}

    target.mkdir(parents=True, exist_ok=True)
    backup = target / ".import-backup" / time.strftime("%Y%m%d-%H%M%S")

    copied: list[str] = []
    missing: list[str] = []
    failed: list[dict] = []
    total_bytes = 0

    def _backup(d: Path, rel: str) -> None:
        b = backup / rel
        b.parent.mkdir(parents=True, exist_ok=True)
        if d.is_dir():
            shutil.copytree(d, b, dirs_exist_ok=True)
        else:
            shutil.copy2(d, b)

    for rel, kind in ITEMS:
        s, d = src / rel, target / rel
        if not s.exists():
            missing.append(rel)
            continue
        try:
            if d.exists():
                _backup(d, rel)          # 旧的那份先存着，出事能退
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink()
            d.parent.mkdir(parents=True, exist_ok=True)
            if kind == "dir":
                total_bytes += _copy_dir(s, d)
            else:
                shutil.copy2(s, d)
                total_bytes += d.stat().st_size
            copied.append(rel)
        except Exception as e:
            failed.append({"item": rel, "error": f"{type(e).__name__}: {e}"})

    db = cookie_db(target)
    total, ero = _count_cookies(db) if db else (0, 0)

    if log:
        log.info(f"已从 {src_name} 导入登录态：{len(copied)} 项 / {total_bytes / 1048576:.1f} MB"
                 f"（cookie {total} 条，其中 ero-labs {ero} 条）")
        if failed:
            log.warning(f"有 {len(failed)} 项没拷成功：{failed[0]['item']}（{failed[0]['error']}）")

    ok = db is not None and bool(copied)
    return {
        "ok": ok,
        "hint": "" if ok else "cookie 库没拷过来，看日志",
        "source": f"{src_name} · {src}",
        "backup": str(backup) if backup.exists() else "",
        "copied": copied,
        "missing": missing,
        "failed": failed,
        "bytes": total_bytes,
        "cookies": total,
        "cookies_game": ero,
    }
