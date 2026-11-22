"""用本机 Edge 读取私信。请求发生在真实浏览器里，Cloudflare 才认。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ns_panel.diff import infer_self_id, message_rows
from ns_panel.nodeseek import (
    ApiError,
    AuthError,
    CloudflareError,
    Member,
    extract_member,
    read_api,
)


PORT = 9337
_CACHE_AT = 0.0
_CACHE_OPEN = False
_LAUNCH_LOCK = threading.Lock()
HOME = "https://www.nodeseek.com/"
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
BROWSER_BLOCKED = "登录窗口被 Cloudflare 拦住了。请在弹出的 Edge 窗口里完成验证，不要关掉窗口。"
BROWSER_AUTH = "登录窗口还没登录 NodeSeek。请在弹出的 Edge 里登录，并保持窗口开着。"
BROWSER_ASLEEP = "登录窗口没有响应。请切到那个 Edge 看一眼，保持窗口开着。"
BROWSER_ASLEEP_DOCKER = "容器里的登录页面没有响应。程序会重新打开这个页面。"


def browser_dir() -> Path:
    override = (os.environ.get("NS_BROWSER_DIR") or "").strip()
    if override:
        folder = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "C:/Temp"
        folder = Path(base) / "nodeseek-pm" / "edge"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def browser_host() -> str:
    return (os.environ.get("NS_BROWSER_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def login_asleep_message() -> str:
    if os.environ.get("NS_RUNTIME") == "docker":
        return BROWSER_ASLEEP_DOCKER
    return BROWSER_ASLEEP


def auth_mode(store) -> str:
    mode = (store.get_setting("auth_mode") or "").strip()
    if mode in ("browser", "cookie"):
        return mode
    if os.environ.get("NS_RUNTIME") == "docker":
        return "browser"
    return "cookie"


def find_edge() -> str | None:
    override = (os.environ.get("NS_BROWSER_BIN") or "").strip()
    if override and Path(override).is_file():
        return override
    for candidate in (
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
    ):
        if candidate.is_file():
            return str(candidate)
    roots = [
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
    ]
    relatives = (
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    )
    for root in roots:
        if not root:
            continue
        for relative in relatives:
            candidate = Path(root) / relative
            if candidate.is_file():
                return str(candidate)
    return None


def browser_is_open(max_age: float = 5) -> bool:
    global _CACHE_AT, _CACHE_OPEN
    now = time.time()
    if now - _CACHE_AT < max_age:
        return _CACHE_OPEN
    _CACHE_OPEN = port_open()
    _CACHE_AT = now
    return _CACHE_OPEN


def remember_browser(open_: bool) -> None:
    global _CACHE_AT, _CACHE_OPEN
    _CACHE_OPEN = open_
    _CACHE_AT = time.time()


def port_open(port: int = PORT, timeout: float = 0.2) -> bool:
    try:
        with urllib.request.urlopen(_cdp_url(port, "/json/version"), timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def open_login_window(port: int = PORT, user_data: Path | None = None, url: str = HOME) -> None:
    with _LAUNCH_LOCK:
        if port_open(port):
            remember_browser(True)
            return
        edge = find_edge()
        if not edge:
            if os.environ.get("NS_RUNTIME") == "docker":
                raise CloudflareError("容器里没找到 Chromium，无法打开登录浏览器。")
            raise CloudflareError("这台电脑没找到 Edge 或 Chrome，无法打开登录窗口。")
        folder = user_data or browser_dir()
        folder.mkdir(parents=True, exist_ok=True)
        flags = 0
        if os.name == "nt":
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        command = [
            edge,
            f"--remote-debugging-port={port}",
            f"--remote-debugging-address={browser_host()}",
            f"--user-data-dir={folder}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
        ]
        if os.environ.get("NS_BROWSER_NO_SANDBOX") == "1":
            command.extend(["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1280,800"])
        command.append(url)
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )


def wait_for_port(port: int = PORT, seconds: float = 15) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(port, timeout=0.5):
            remember_browser(True)
            return True
        time.sleep(0.3)
    return False


class BrowserClient:
    """在已经通过验证的 Edge 页面里调用论坛接口。"""

    def __init__(self, port: int = PORT):
        self.port = port

    def available(self) -> bool:
        return port_open(self.port)

    def ensure_window(self) -> None:
        if self.available():
            return
        open_login_window(self.port)
        if not wait_for_port(self.port):
            raise CloudflareError("登录窗口没有连上。请到设置里再点一次「打开登录浏览器」。")

    def get_account(self) -> Member | None:
        self.ensure_window()
        for path in (
            "/api/account/getInfo",
            "/api/account/info",
            "/api/account/profile",
        ):
            try:
                data = self.get_json(path)
            except (AuthError, CloudflareError):
                raise
            except ApiError:
                continue
            member = extract_member(data)
            if member:
                return member
        return None

    def get_message_list(self) -> dict:
        self.ensure_window()
        return self.get_json("/api/notification/message/list")

    def get_mentions(self) -> dict:
        self.ensure_window()
        return self.get_json("/api/notification/at-me/list")

    def get_replies(self) -> dict:
        self.ensure_window()
        return self.get_json("/api/notification/reply-to-me/list")

    def resolve_self_id(self, payload) -> int | None:
        return infer_self_id(message_rows(payload))

    def get_json(self, path: str):
        status, content_type, text = self._fetch(path)
        return read_api(status, content_type, text, BROWSER_BLOCKED, BROWSER_AUTH)

    def _fetch(self, path: str) -> tuple[int, str, str]:
        expression = (
            "(async () => {"
            "const ctrl = new AbortController();"
            "const timer = setTimeout(() => ctrl.abort(), 8000);"
            "try {"
            f"const res = await fetch({json.dumps(path)}, "
            "{credentials: 'include', signal: ctrl.signal, "
            "headers: {'Accept': 'application/json, text/plain, */*'}});"
            "const text = await res.text();"
            "return JSON.stringify({status: res.status, type: res.headers.get('content-type') || '', "
            "text: text.slice(0, 500000)});"
            "} catch (err) {"
            "return JSON.stringify({status: 0, type: '', text: '', error: String(err)});"
            "} finally { clearTimeout(timer); }"
            "})()"
        )
        session = shared_session(self.port)

        def action() -> tuple[int, str, str]:
            try:
                parsed = json.loads(session.evaluate(expression))
            except json.JSONDecodeError as exc:
                raise SessionDown("登录窗口没有返回接口数据") from exc
            if parsed.get("error") or not parsed.get("status"):
                raise SessionDown(str(parsed.get("error") or "登录窗口里的请求没有返回"))
            return int(parsed.get("status") or 0), str(parsed.get("type") or ""), str(parsed.get("text") or "")

        return run_with_retry(action, lambda: _wake_target(self.port))


class SessionDown(Exception):
    pass


_SESSIONS: dict[int, "CdpSession"] = {}
_SESSIONS_LOCK = threading.Lock()


def shared_session(port: int = PORT) -> "CdpSession":
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(port)
        if session is None:
            session = CdpSession(port)
            _SESSIONS[port] = session
        return session


def run_with_retry(action, wake, attempts: int = 3):
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return action()
        except SessionDown as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            print(f"[info] 登录窗口没有立刻响应：{exc}。正在重新打开页面。", flush=True)
            wake()
    raise CloudflareError(login_asleep_message()) from last


class CdpSession:
    """连在浏览器进程上，不连会被冻住的页面。连接保持打开，页面才不会再休眠。"""

    def __init__(self, port: int):
        self.port = port
        self._lock = threading.Lock()
        self._ws = None
        self._session_id = ""
        self._target_id = ""
        self._next_id = 1

    def evaluate(self, expression: str, timeout: float = 12) -> str:
        with self._lock:
            try:
                self._ensure()
                value = self._call(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                    self._session_id,
                    timeout,
                )
            except (ApiError, CloudflareError):
                raise
            except Exception as exc:
                self._drop()
                raise SessionDown("登录窗口调试连接中断") from exc
            result = (value or {}).get("result") or {}
            if (value or {}).get("exceptionDetails"):
                raise ApiError("登录窗口执行请求失败")
            text = result.get("value")
            if not isinstance(text, str):
                self._drop()
                raise SessionDown("登录窗口没有返回文本")
            return text

    def poke(self) -> None:
        with self._lock:
            self._ensure()
            try:
                self._call(
                    "Page.setWebLifecycleState",
                    {"state": "active"},
                    self._session_id,
                    3,
                )
            except Exception:
                pass
            self._call(
                "Runtime.evaluate",
                {"expression": "1", "returnByValue": True},
                self._session_id,
                5,
            )

    def _ensure(self) -> None:
        page = _wait_page(self.port)
        if self._ws is not None and self._target_id == page.get("id") and self._session_id:
            return
        self._drop()
        version = _get_json(self.port, "/json/version")
        browser_ws = str(version.get("webSocketDebuggerUrl") or "")
        if not browser_ws:
            raise SessionDown("登录窗口没有调试地址")
        self._ws = _connect(browser_ws, 8)
        attached = self._call(
            "Target.attachToTarget",
            {"targetId": page["id"], "flatten": True},
            "",
            8,
        )
        self._session_id = str(attached.get("sessionId") or "")
        self._target_id = str(page.get("id") or "")
        if not self._session_id:
            raise SessionDown("登录窗口没有挂上页面")
        self._keep_awake()

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _keep_awake(self) -> None:
        for method, params in (
            ("Page.enable", {}),
            ("Page.setWebLifecycleState", {"state": "active"}),
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
        ):
            try:
                self._call(method, params, self._session_id, 4)
            except Exception:
                continue
        try:
            current = self._call(
                "Runtime.evaluate",
                {"expression": "location.href", "returnByValue": True},
                self._session_id,
                4,
            )
            href = str(((current or {}).get("result") or {}).get("value") or "")
        except Exception:
            href = ""
        if not href.startswith(HOME):
            try:
                self._call("Page.navigate", {"url": HOME}, self._session_id, 4)
            except Exception:
                return

    def _call(self, method: str, params: dict, session_id: str, timeout: float) -> dict:
        if self._ws is None:
            raise SessionDown("登录窗口调试连接中断")
        call_id = self._next_id
        self._next_id += 1
        payload = {"id": call_id, "method": method, "params": params}
        if session_id:
            payload["sessionId"] = session_id
        self._ws.settimeout(timeout)
        self._ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self._ws.recv())
            if message.get("id") != call_id:
                continue
            if message.get("error"):
                raise ApiError(str(message["error"].get("message") or "浏览器调试失败"))
            return message.get("result") or {}
        raise SessionDown("登录窗口超时")

    def _drop(self) -> None:
        ws = self._ws
        self._ws = None
        self._session_id = ""
        self._target_id = ""
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _wait_page(port: int) -> dict:
    deadline = time.time() + 20
    started = False
    while time.time() < deadline:
        page = _nodeseek_page(port)
        if page and page.get("id"):
            return page
        if not started:
            _open_nodeseek_tab(port)
            started = True
        time.sleep(0.4)
    raise CloudflareError("登录窗口里没有可用页面。请关掉弹出的 Edge 后，再点一次打开。")


def _nodeseek_page(port: int) -> dict | None:
    for item in _pages(port):
        if str(item.get("url") or "").startswith(HOME):
            return item
    return None


def _pages(port: int) -> list[dict]:
    targets = _get_json(port, "/json/list")
    if not isinstance(targets, list):
        return []
    return [item for item in targets if item.get("type") == "page" and item.get("id")]


def _open_nodeseek_tab(port: int) -> None:
    path = "/json/new?" + urllib.parse.quote(HOME, safe=":/")
    try:
        _put_json(port, path)
    except Exception:
        try:
            _get_json(port, path)
        except Exception:
            return


def _wake_target(port: int) -> None:
    session = _SESSIONS.get(port)
    if session is not None:
        session.close()
    if not port_open(port, timeout=1):
        if _restart_container_browser():
            wait_for_port(port, 20)
        return
    page = _nodeseek_page(port)
    target_id = str((page or {}).get("id") or "")
    if not target_id:
        _open_nodeseek_tab(port)
        time.sleep(1)
        return
    try:
        urllib.request.urlopen(_cdp_url(port, f"/json/activate/{target_id}"), timeout=3).close()
    except Exception:
        pass
    if _reload_via_cdp(port, target_id):
        time.sleep(2)
        return
    if _restart_container_browser():
        wait_for_port(port, 20)


def _reload_via_cdp(port: int, target_id: str) -> bool:
    try:
        version = _get_json(port, "/json/version")
        browser_ws = str(version.get("webSocketDebuggerUrl") or "")
        if not browser_ws:
            return False
        ws = _connect(browser_ws, 4)
    except Exception:
        return False
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Target.attachToTarget",
            "params": {"targetId": target_id, "flatten": True},
        }))
        session_id = _wait_result(ws, 1, 4).get("sessionId") or ""
        if not session_id:
            return False
        ws.send(json.dumps({
            "id": 2,
            "sessionId": session_id,
            "method": "Page.navigate",
            "params": {"url": HOME},
        }))
        _wait_result(ws, 2, 4)
        return True
    except Exception:
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _wait_result(ws, call_id: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = json.loads(ws.recv())
        if message.get("id") != call_id:
            continue
        if message.get("error"):
            raise SessionDown(str((message.get("error") or {}).get("message") or "浏览器调试失败"))
        return message.get("result") or {}
    raise SessionDown("登录窗口超时")


def _restart_container_browser() -> bool:
    if os.environ.get("NS_RUNTIME") != "docker":
        return False
    import signal

    me = os.getpid()
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if "chromium" not in command or "--type=" in command:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.4)
    folder = browser_dir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (folder / name).unlink()
        except OSError:
            pass
    return True


def _connect(ws_url: str, timeout: float):
    try:
        import websocket
    except ImportError as exc:
        raise CloudflareError("缺少 websocket-client。请重新运行启动面板。") from exc
    try:
        return websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    except TypeError:
        return websocket.create_connection(ws_url, timeout=timeout)


def _cdp_url(port: int, path: str) -> str:
    return f"http://{browser_host()}:{port}{path}"


def _get_json(port: int, path: str):
    with urllib.request.urlopen(_cdp_url(port, path), timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _put_json(port: int, path: str):
    request = urllib.request.Request(_cdp_url(port, path), method="PUT")
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read().decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


