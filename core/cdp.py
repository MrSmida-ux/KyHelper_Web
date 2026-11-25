"""极简 Chrome DevTools Protocol 客户端（零第三方依赖）。

为什么自己写：目标机只保证有 opencv/numpy，多一个 websocket 依赖就多一条
装不上的路。CDP 用得到的协议面很窄 —— 握手、发文本帧、收文本帧、回 pong，
一百多行足够，比拉依赖稳。

对外只有三个东西：
    Browser(url)      连接 http://127.0.0.1:9222
    CDP(ws_url)       一条页面/iframe 会话，call() 发命令，wait() 等事件
    CDPError

线程模型：一个后台线程专读 socket，把带 id 的响应对号入座，事件丢进队列。
call() 在调用线程里阻塞等结果，超时抛 CDPError（不卡死进程）。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.request
from urllib.parse import urlparse


class CDPError(RuntimeError):
    pass


# ------------------------------------------------------------------ WebSocket
class _WS:
    """够用就好的 WebSocket 客户端：文本帧 + 分片 + ping/pong。"""

    def __init__(self, url: str, timeout: float = 10.0):
        u = urlparse(url)
        host, port = u.hostname, u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(None)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        # 读到空行为止，顺便校验 101
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手被拒绝")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if "101" not in head.split("\r\n")[0]:
            raise CDPError(f"WebSocket 握手失败：{head.splitlines()[0]}")
        self._rest = buf.split(b"\r\n\r\n", 1)[1]
        self._lock = threading.Lock()
        self.closed = False

    # ---- 低层 ----
    def _read_exact(self, n: int) -> bytes:
        if self._rest:
            take = self._rest[:n]
            self._rest = self._rest[n:]
            if len(take) == n:
                return take
            n -= len(take)
        else:
            take = b""
        parts = [take]
        while n > 0:
            chunk = self.sock.recv(min(65536, n))
            if not chunk:
                raise CDPError("连接已关闭")
            parts.append(chunk)
            n -= len(chunk)
        return b"".join(parts)

    def recv(self) -> tuple[int, bytes]:
        """返回 (opcode, payload)。opcode 8 = 关闭，9 = ping，10 = pong。"""
        frag: list[bytes] = []
        op = 0
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(ln) if ln else b""
            if mask:
                data = bytes(c ^ mask[i & 3] for i, c in enumerate(data))
            if opcode < 8:                      # 数据帧（0=续帧）
                if opcode:
                    op = opcode
                frag.append(data)
                if fin:
                    return op, b"".join(frag)
                continue
            return opcode, data                 # 控制帧

    def send(self, text: str) -> None:
        payload = text.encode()
        n = len(payload)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        with self._lock:
            self.sock.sendall(bytes(head) + masked)

    def pong(self, data: bytes) -> None:
        n = len(data)
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i & 3] for i, c in enumerate(data))
        with self._lock:
            self.sock.sendall(bytes([0x8A, 0x80 | n]) + mask + masked)

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


# ------------------------------------------------------------------ CDP 会话
class CDP:
    def __init__(self, ws_url: str, timeout: float = 15.0):
        self.ws = _WS(ws_url, timeout=timeout)
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._events: list[dict] = []
        self._ev_lock = threading.Lock()
        self._ev_cv = threading.Condition(self._ev_lock)
        self.session_id: str | None = None
        self.error = ""
        self._stop = False
        self._t = threading.Thread(target=self._reader, name="cdp-reader", daemon=True)
        self._t.start()

    # ---- 读线程 ----
    def _reader(self) -> None:
        while not self._stop:
            try:
                op, data = self.ws.recv()
            except Exception as e:
                self.error = str(e)
                break
            if op == 8:
                self.error = "会话被关闭"
                break
            if op == 9:
                try:
                    self.ws.pong(data)
                except Exception:
                    pass
                continue
            if op != 1:
                continue
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None:
                with self._lock:
                    slot = self._pending.pop(mid, None)
                if slot is not None:
                    slot["msg"] = msg
                    slot["ev"].set()
                continue
            with self._ev_lock:
                self._events.append(msg)
                if len(self._events) > 400:
                    del self._events[:200]
                self._ev_cv.notify_all()
        # 收尾：所有等待者都醒
        with self._lock:
            pend = list(self._pending.values())
            self._pending.clear()
        for slot in pend:
            slot["ev"].set()
        with self._ev_lock:
            self._ev_cv.notify_all()

    # ---- 发送 ----
    def call(self, method: str, params: dict | None = None, timeout: float = 20.0,
             session_id: str | None = None) -> dict:
        if self.error:
            raise CDPError(self.error)
        with self._lock:
            self._id += 1
            mid = self._id
            slot = {"ev": threading.Event(), "msg": None}
            self._pending[mid] = slot
        msg = {"id": mid, "method": method, "params": params or {}}
        sid = session_id if session_id is not None else self.session_id
        if sid:
            msg["sessionId"] = sid
        try:
            self.ws.send(json.dumps(msg))
        except Exception as e:
            with self._lock:
                self._pending.pop(mid, None)
            raise CDPError(f"发送失败：{e}") from e
        if not slot["ev"].wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise CDPError(f"{method} 超时 {timeout:.0f}s")
        resp = slot["msg"]
        if not resp:
            raise CDPError(f"{method} 无响应（{self.error or '连接断开'}）")
        if "error" in resp:
            err = resp["error"]
            raise CDPError(f"{method}: {err.get('message')} {err.get('data', '')}".strip())
        return resp.get("result", {}) or {}

    def evaluate(self, expr: str, timeout: float = 10.0, session_id: str | None = None):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
            "awaitPromise": True, "userGesture": True,
        }, timeout=timeout, session_id=session_id)
        res = r.get("result", {}) or {}
        if r.get("exceptionDetails"):
            raise CDPError(str(r["exceptionDetails"].get("text") or "JS 异常"))
        return res.get("value")

    # ---- 事件 ----
    def wait_event(self, method: str, timeout: float = 10.0,
                   sid: str | None = None) -> dict | None:
        deadline = time.time() + timeout
        with self._ev_lock:
            while True:
                for i, ev in enumerate(self._events):
                    if ev.get("method") == method and (sid is None or ev.get("sessionId") == sid):
                        return self._events.pop(i)
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._ev_cv.wait(left)

    def take_events(self, method: str, limit: int = 50) -> list[dict]:
        with self._ev_lock:
            hit = [e for e in self._events if e.get("method") == method]
            if hit:
                self._events = [e for e in self._events if e.get("method") != method]
            return hit[:limit]

    def close(self) -> None:
        self._stop = True
        self.ws.close()


# ------------------------------------------------------------------ 浏览器入口
class Browser:
    """http://127.0.0.1:9222 这一侧的 HTTP 接口。"""

    def __init__(self, port: int = 9222, host: str = "127.0.0.1"):
        self.port = port
        self.host = host

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _get(self, path: str, timeout: float = 3.0):
        # 本地请求绝不能走系统代理（127.0.0.1 会被代理打成 502）
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(self.base + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def version(self) -> dict:
        return self._get("/json/version")

    def alive(self) -> bool:
        try:
            self.version()
            return True
        except Exception:
            return False

    def targets(self) -> list[dict]:
        try:
            return self._get("/json/list")
        except Exception:
            return []

    def new_tab(self, url: str) -> dict:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"{self.base}/json/new?{url}", method="PUT")
        with op.open(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def close_tab(self, target_id: str) -> None:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            req = urllib.request.Request(f"{self.base}/json/close/{target_id}", method="GET")
            op.open(req, timeout=3).read()
        except Exception:
            pass
