"""
Minimal Chrome DevTools Protocol client - stdlib only.

Enough of the protocol to drive a local headless browser for screenshots: connect,
evaluate JavaScript, wait for a condition, and capture the page. Written rather than
installed because the only dependency is a WebSocket client, and the client side of
the protocol is small enough to implement directly.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from pathlib import Path


class WS:
    """A WebSocket client: handshake, masked text frames, reassembled reads."""

    def __init__(self, url: str):
        assert url.startswith("ws://")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        if b"101" not in buf.split(b"\r\n")[0]:
            raise RuntimeError(f"websocket handshake failed: {buf[:200]!r}")
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def send(self, text: str) -> None:
        payload = text.encode()
        n = len(payload)
        header = b"\x81"
        if n < 126:
            header += struct.pack("!B", n | 0x80)
        elif n < 1 << 16:
            header += struct.pack("!BH", 126 | 0x80, n)
        else:
            header += struct.pack("!BQ", 127 | 0x80, n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str:
        chunks = []
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            chunks.append(self._read(length))
            if fin:
                if opcode == 0x8:
                    raise ConnectionError("websocket closed by peer")
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    """Launches a headless browser, talks CDP to its first page target."""

    BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    def __init__(self, width=1600, height=1000, port=9333, profile=None, scale=2):
        binary = self.CHROME if Path(self.CHROME).exists() else self.BRAVE
        profile = profile or "/tmp/latentstudio-cdp-profile"
        self.proc = subprocess.Popen(
            [binary, "--headless=new", f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}",           # never the real profile
             f"--window-size={width},{height}",
             f"--force-device-scale-factor={scale}",
             "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.port, self.msg = port, 0
        target = self._wait_for_target()
        self.ws = WS(target)
        self.send("Page.enable")
        self.send("Runtime.enable")

    def _wait_for_target(self, timeout=30) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json", timeout=2).read()
                for t in json.loads(raw):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.4)
        raise RuntimeError("no debuggable page target appeared")

    def send(self, method: str, **params) -> dict:
        self.msg += 1
        mid = self.msg
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def goto(self, url: str) -> None:
        self.send("Page.navigate", url=url)
        self.wait("document.readyState === 'complete'", timeout=30)

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True)
        return r.get("result", {}).get("value")

    def wait(self, expr: str, timeout=60, poll=0.3) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js(f"!!({expr})"):
                    return True
            except RuntimeError:
                pass
            time.sleep(poll)
        raise TimeoutError(f"condition never became true: {expr}")

    def shot(self, path: str | Path, full=False) -> Path:
        params = {"format": "png"}
        if full:
            params["captureBeyondViewport"] = True
        r = self.send("Page.captureScreenshot", **params)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(r["data"]))
        return p

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
