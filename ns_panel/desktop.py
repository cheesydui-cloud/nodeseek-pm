"""把容器里的网页桌面接到面板同一个地址上。"""

from __future__ import annotations

import os
import select
import socket
from http import client as http_client

DESKTOP_PREFIX = "/desktop"
DESKTOP_PAGE = "/desktop/vnc.html?autoconnect=1&resize=scale&path=desktop/websockify"

_SKIP_RESPONSE = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def desktop_address() -> tuple[str, int]:
    host = (os.environ.get("NS_DESKTOP_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    raw = (os.environ.get("NS_DESKTOP_PORT") or "6080").strip()
    try:
        port = int(raw)
    except ValueError:
        port = 6080
    if port < 1 or port > 65535:
        port = 6080
    return host, port


def desktop_upstream_path(path: str, query: str = "") -> str | None:
    if not path.startswith(DESKTOP_PREFIX + "/"):
        return None
    parts = [part for part in path[len(DESKTOP_PREFIX):].split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    target = "/" + "/".join(parts)
    if query:
        target += "?" + query
    return target


def unavailable_page() -> bytes:
    return (
        "<!doctype html><meta charset=\"utf-8\"><title>网页桌面</title>"
        "<p>网页桌面还没准备好。请等几秒再打开。本机面板没有网页桌面，只有容器里才有。</p>"
    ).encode("utf-8")


def serve_desktop(handler, path: str, query: str) -> None:
    if path in (DESKTOP_PREFIX, DESKTOP_PREFIX + "/"):
        handler._redirect(DESKTOP_PAGE)
        return
    target = desktop_upstream_path(path, query)
    if target is None:
        handler._json(404, {"ok": False, "error": "没有这个页面"})
        return
    if _is_websocket(handler):
        _tunnel(handler, target)
        return
    _proxy_http(handler, target)


def _is_websocket(handler) -> bool:
    upgrade = (handler.headers.get("Upgrade") or "").lower()
    connection = (handler.headers.get("Connection") or "").lower()
    return "websocket" in upgrade and "upgrade" in connection


def _proxy_http(handler, target: str) -> None:
    host, port = desktop_address()
    conn = http_client.HTTPConnection(host, port, timeout=8)
    try:
        conn.request("GET", target, headers={"Host": f"{host}:{port}", "Accept": handler.headers.get("Accept") or "*/*"})
        response = conn.getresponse()
        body = response.read()
        status = response.status
        headers = response.getheaders()
    except OSError:
        _unavailable(handler)
        return
    finally:
        conn.close()
    handler.send_response(status)
    for key, value in headers:
        if key.lower() in _SKIP_RESPONSE:
            continue
        if key.lower() == "location" and value.startswith("/") and not value.startswith(DESKTOP_PREFIX + "/"):
            value = DESKTOP_PREFIX + value
        handler.send_header(key, value)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _tunnel(handler, target: str) -> None:
    handler.close_connection = True
    host, port = desktop_address()
    try:
        upstream = socket.create_connection((host, port), timeout=8)
    except OSError:
        _unavailable(handler)
        return
    try:
        upstream.settimeout(8)
        upstream.sendall(_websocket_request(handler, host, port, target))
        head = _read_headers(upstream)
        if not head:
            _unavailable(handler)
            return
        handler.wfile.write(head)
        handler.wfile.flush()
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            return
        _copy(handler.connection, upstream)
    except OSError:
        return
    finally:
        upstream.close()


def _websocket_request(handler, host: str, port: int, target: str) -> bytes:
    lines = [
        f"GET {target} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
    ]
    for name in ("Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Protocol", "Sec-WebSocket-Extensions"):
        value = handler.headers.get(name)
        if value:
            lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii", "surrogateescape")


def _read_headers(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            break
    return data


def _copy(client: socket.socket, upstream: socket.socket) -> None:
    client.setblocking(False)
    upstream.setblocking(False)
    pair = [client, upstream]
    while True:
        readable, _, _ = select.select(pair, [], [], 300)
        if not readable:
            continue
        for sock in readable:
            other = upstream if sock is client else client
            try:
                chunk = sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            if not chunk:
                return
            _send_all(other, chunk)


def _send_all(sock: socket.socket, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        try:
            sent = sock.send(view)
        except (BlockingIOError, InterruptedError):
            select.select([], [sock], [], 30)
            continue
        if sent <= 0:
            raise OSError("网页桌面连接已断开")
        view = view[sent:]


def _unavailable(handler) -> None:
    body = unavailable_page()
    handler.send_response(503)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)
