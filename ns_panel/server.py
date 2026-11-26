"""本机面板。只监听 127.0.0.1。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ns_panel import __version__
from ns_panel.browser import BrowserClient, auth_mode, browser_is_open, open_login_window, port_open, wait_for_port
from ns_panel.diff import parse_cookie
from ns_panel.monitor import (
    MAX_INTERVAL,
    MIN_INTERVAL,
    MIN_KEYWORD_INTERVAL,
    Monitor,
    interval_seconds,
    keyword_interval_seconds,
)
from ns_panel.nodeseek import AuthError, CloudflareError, NodeSeekClient, NodeSeekError, message_rows
from ns_panel.posts import (
    BOARDS,
    format_keyword_rules,
    parse_excludes,
    parse_keyword_rules,
    parse_keywords,
    selected_slugs,
    validate_excludes,
    validate_keywords,
    normalize_boards,
)
from ns_panel.water import (
    MAX_DAILY,
    WATER_BOARD,
    parse_phrases,
    resolve_water_board,
    validate_water_settings,
    water_status_line,
)
from ns_panel.schedule import in_quiet_hours, valid_hhmm
from ns_panel.store import Store
from ns_panel.botview import reply_keyboard
from ns_panel.telegram import TelegramError, discover_chat_id, send_message


STATIC = Path(__file__).resolve().parent / "static"
HOST = "127.0.0.1"
SESSION_COOKIE = "ns_session"
SESSION_SECONDS = 14 * 24 * 3600
PUBLIC_GET = {"/login", "/static/app.css", "/health"}
PUBLIC_POST = {"/api/login"}


class App:
    def __init__(self, root: Path):
        self.root = root
        self.store = Store(root / "data" / "panel.db")
        self.monitor = Monitor(self.store)
        self.sessions: dict[str, float] = {}
        self.login_failures: dict[str, tuple[int, float]] = {}
        self.session_lock = threading.Lock()
        if self.store.get_setting("enabled") == "1":
            self.monitor.start()
        self.monitor.supervise()

    def issue_session(self) -> str:
        token = secrets.token_urlsafe(32)
        expires = time.time() + SESSION_SECONDS
        with self.session_lock:
            self._drop_expired_sessions()
            self.sessions[token] = expires
        return token

    def session_valid(self, token: str) -> bool:
        if not token:
            return False
        now = time.time()
        with self.session_lock:
            expires = self.sessions.get(token)
            if not expires or expires <= now:
                self.sessions.pop(token, None)
                return False
            return True

    def drop_session(self, token: str) -> None:
        with self.session_lock:
            self.sessions.pop(token, None)

    def allow_login(self, ip: str) -> bool:
        now = time.time()
        with self.session_lock:
            _count, until = self.login_failures.get(ip, (0, 0.0))
            return until <= now

    def note_login_failure(self, ip: str) -> None:
        now = time.time()
        with self.session_lock:
            count, until = self.login_failures.get(ip, (0, 0.0))
            if until <= now:
                count = 0
            count += 1
            until = now + 60 if count >= 5 else 0.0
            self.login_failures[ip] = (count, until)

    def note_login_success(self, ip: str) -> None:
        with self.session_lock:
            self.login_failures.pop(ip, None)

    def _drop_expired_sessions(self) -> None:
        now = time.time()
        expired = [token for token, expires in self.sessions.items() if expires <= now]
        for token in expired:
            self.sessions.pop(token, None)


class PanelServer(ThreadingHTTPServer):
    def __init__(self, address, app: App):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: PanelServer

    def log_message(self, fmt: str, *args) -> None:
        return

    def _guard(self) -> bool:
        path, _query = _split(self.path)
        if self.command == "GET" and path in PUBLIC_GET:
            return True
        if self.command == "POST" and path in PUBLIC_POST:
            return True
        store = self.server.app.store
        if not panel_login_required(store):
            return True
        if self.server.app.session_valid(session_token(self.headers.get("Cookie"))):
            return True
        if panel_authorized(self.headers.get("Authorization"), store):
            return True
        if self.command == "GET" and path in ("/", "/index.html"):
            self._redirect("/login")
            return False
        body = '{"ok":false,"error":"请先登录"}'.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self) -> None:
        if not self._guard():
            return
        try:
            path, query = _split(self.path)
            if path == "/login":
                if not panel_login_required(self.server.app.store) or self.server.app.session_valid(session_token(self.headers.get("Cookie"))):
                    self._redirect("/")
                    return
                self._file("login.html", "text/html; charset=utf-8")
                return
            if path in ("/", "/index.html"):
                self._file("index.html", "text/html; charset=utf-8")
                return
            if path == "/static/app.css":
                self._file("app.css", "text/css; charset=utf-8")
                return
            if path == "/static/app.js":
                self._file("app.js", "text/javascript; charset=utf-8")
                return
            if path == "/health":
                self._json(200, health_payload(self.server.app))
                return
            if path == "/api/status":
                self._json(200, status_payload(self.server.app))
                return
            if path == "/api/settings":
                self._json(200, settings_payload(self.server.app.store))
                return
            if path == "/api/conversations":
                self._json(200, {"ok": True, "items": self.server.app.store.list_conversations()})
                return
            if path == "/api/events":
                limit = _limit(query, 40)
                self._json(200, {"ok": True, "items": self.server.app.store.list_events(limit)})
                return
            if path == "/api/logs":
                limit = _limit(query, 200)
                self._json(200, {"ok": True, "items": self.server.app.store.list_logs(limit)})
                return
            if path == "/api/posts":
                self._json(200, posts_payload(self.server.app.store))
                return
            if path == "/api/mentions":
                self._json(200, mentions_payload(self.server.app.store))
                return
            if path == "/api/replies":
                self._json(200, replies_payload(self.server.app.store))
                return
            self._json(404, {"ok": False, "error": "没有这个页面"})
        except Exception as exc:
            self._fail(exc)

    def do_POST(self) -> None:
        if not self._guard():
            return
        try:
            path, _query = _split(self.path)
            data = self._body()
            app = self.server.app
            if path == "/api/login":
                self._login(app, data)
                return
            if path == "/api/logout":
                app.drop_session(session_token(self.headers.get("Cookie")))
                self._json(200, {"ok": True}, {"Set-Cookie": cleared_session_cookie()})
                return
            if path == "/api/panel-account":
                error = update_panel_account(app.store, data)
                if error:
                    self._json(400, {"ok": False, "error": error})
                    return
                app.monitor.log("info", "已修改面板账号")
                user, _secret, _kind = panel_account(app.store)
                self._json(200, {"ok": True, "panel_user": user, "message": "账号已保存。下次登录用新密码。"})
                return
            if path == "/api/settings":
                error = update_settings(app.store, data)
                if error:
                    self._json(400, {"ok": False, "error": error})
                    return
                app.monitor.log("info", "已保存设置")
                self._json(200, settings_payload(app.store))
                return
            if path == "/api/monitor/start":
                missing = _missing_config(app.store)
                if missing:
                    self._json(400, {"ok": False, "error": missing})
                    return
                app.store.set_setting("enabled", "1")
                app.monitor.start()
                self._json(200, status_payload(app))
                return
            if path == "/api/monitor/stop":
                app.store.set_setting("enabled", "0")
                app.monitor.stop()
                self._json(200, status_payload(app))
                return
            if path == "/api/monitor/check":
                result = app.monitor.check_once(wait=False)
                code = 200 if result.get("ok") else 400
                self._json(code, result)
                return
            if path == "/api/baseline":
                app.store.set_state("baseline_done", "0")
                result = app.monitor.check_once(wait=False)
                code = 200 if result.get("ok") else 400
                self._json(code, result)
                return
            if path == "/api/test/cookie":
                self._json(200, test_cookie(app.store, data))
                return
            if path == "/api/test/telegram":
                self._json(200, test_telegram(app.store, data))
                return
            if path == "/api/telegram/discover":
                self._json(200, discover(app.store, data))
                return
            if path == "/api/browser/open":
                self._json(200, open_browser(app.store))
                return
            if path == "/api/browser/cookie":
                app.store.set_setting("auth_mode", "cookie")
                app.monitor.log("info", "已改回使用 Cookie")
                self._json(200, status_payload(app))
                return
            if path == "/api/browser/check":
                self._json(200, test_browser(app.store))
                return
            if path == "/api/keywords":
                error = update_keywords(app.store, data)
                if error:
                    self._json(400, {"ok": False, "error": error})
                    return
                app.monitor.log("info", "已保存关键词")
                self._json(200, posts_payload(app.store))
                return
            if path == "/api/posts/scan":
                result = app.monitor.scan_posts()
                code = 200 if result.get("ok") else 400
                self._json(code, result)
                return
            if path == "/api/posts/baseline":
                result = app.monitor.scan_posts(reset=True)
                code = 200 if result.get("ok") else 400
                self._json(code, result)
                return
            if path == "/api/mentions/baseline":
                result = app.monitor.scan_mentions(reset=True)
                code = 200 if result.get("ok") else 400
                self._json(code, result)
                return
            if path == "/api/replies/baseline":
                result = app.monitor.scan_replies(reset=True)
                code = 200 if result.get("ok") else 400
                self._json(code, result)
                return
            self._json(404, {"ok": False, "error": "没有这个操作"})
        except (TelegramError, NodeSeekError, ValueError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._fail(exc)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("提交的内容太大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("请求不是合法的 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    def _file(self, name: str, content_type: str) -> None:
        file_path = STATIC / name
        if not file_path.is_file():
            self._json(404, {"ok": False, "error": "页面文件缺失"})
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login(self, app: App, data: dict) -> None:
        ip = _client_ip(self)
        if not app.allow_login(ip):
            self._json(429, {"ok": False, "error": "试错太多次，请稍后再试。"})
            return
        user, secret, kind = panel_account(app.store)
        if not user or not secret:
            self._json(400, {"ok": False, "error": "当前没有设置面板密码"})
            return
        got_user = str(data.get("user") or "")
        got_password = str(data.get("password") or "")
        user_ok = len(got_user) == len(user) and secrets.compare_digest(got_user, user)
        pass_ok = password_matches(secret, kind, got_password)
        if not user_ok or not pass_ok:
            app.note_login_failure(ip)
            self._json(401, {"ok": False, "error": "账号或密码不对"})
            return
        app.note_login_success(ip)
        token = app.issue_session()
        self._json(200, {"ok": True}, {"Set-Cookie": session_cookie(token, self._https())})

    def _https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _fail(self, exc: Exception) -> None:
        traceback.print_exc()
        self._json(500, {"ok": False, "error": str(exc) or "面板内部错误"})


def health_payload(app: App) -> dict:
    return {"ok": True, "version": __version__, "running": app.monitor.running}


def status_payload(app: App) -> dict:
    store = app.store
    self_id = store.get_state("self_id")
    next_at = app.monitor.next_check_at
    return {
        "ok": True,
        "version": __version__,
        "running": app.monitor.running,
        "enabled": store.get_setting("enabled") == "1",
        "last_check": store.get_state("last_check") or "",
        "last_success": store.get_state("last_success") or "",
        "last_error": store.get_state("last_error") or "",
        "self_name": store.get_state("self_name") or "",
        "self_id": int(self_id) if self_id and self_id.isdigit() else None,
        "baseline_done": store.get_state("baseline_done") == "1",
        "today_pushes": store.count_pushes_today(),
        "conversation_count": len(store.list_conversations()),
        "next_check_at": next_at,
        "has_cookie": bool(store.get_setting("cookie")),
        "auth_mode": auth_mode(store),
        "runtime": os.environ.get("NS_RUNTIME") or "local",
        "browser_open": browser_is_open(),
        "has_telegram": bool(store.get_setting("tg_token") and store.get_setting("tg_chat_id")),
        "has_keywords": bool(parse_keywords(store.get_setting("keywords") or "")),
        "posts_baseline_done": store.get_state("posts_baseline_done") == "1",
        "posts_last_error": store.get_state("posts_last_error") or "",
        "mentions_baseline_done": store.get_state("mentions_baseline_done") == "1",
        "mentions_last_error": store.get_state("mentions_last_error") or "",
        "replies_baseline_done": store.get_state("replies_baseline_done") == "1",
        "replies_last_error": store.get_state("replies_last_error") or "",
        "quiet_active": quiet_active(store),
        "interval": interval_seconds(store),
        "keyword_interval": keyword_interval_seconds(store),
        "next_keyword_at": app.monitor.next_keyword_at,
        "login_required": panel_login_required(store),
        "water_enabled": (store.get_setting("water_enabled") or "0") == "1",
        "water_phrases_ready": len(parse_phrases(store.get_setting("water_phrases") or "")) >= 2,
        "water_status": water_status_line(store),
    }


def settings_payload(store: Store) -> dict:
    cookie = store.get_setting("cookie") or ""
    user, secret, _kind = panel_account(store)
    return {
        "ok": True,
        "has_cookie": bool(cookie),
        "cookie_length": len(cookie),
        "has_token": bool(store.get_setting("tg_token")),
        "tg_chat_id": store.get_setting("tg_chat_id") or "",
        "interval": interval_seconds(store),
        "keyword_interval": keyword_interval_seconds(store),
        "panel_user": user,
        "has_panel_password": bool(user and secret),
        "checkin_enabled": (store.get_setting("checkin_enabled", "1") or "1") != "0",
        "water_enabled": (store.get_setting("water_enabled") or "0") == "1",
        "water_daily": _water_daily(store),
        "water_gap": _water_gap(store),
        "water_phrases": store.get_setting("water_phrases") or "",
        "water_board": _water_board(store),
        "water_boards": [{"slug": slug, "name": name} for slug, name in BOARDS],
        "water_status": water_status_line(store),
        "heartbeat_enabled": (store.get_setting("heartbeat_enabled", "1") or "1") != "0",
        "heartbeat_hour": _heartbeat_hour(store),
        "quiet_enabled": (store.get_setting("quiet_enabled") or "0") == "1",
        "quiet_start": store.get_setting("quiet_start") or "23:00",
        "quiet_end": store.get_setting("quiet_end") or "08:00",
    }


def _water_board(store: Store) -> str:
    try:
        return resolve_water_board(store.get_setting("water_board"))
    except ValueError:
        return WATER_BOARD


def _water_daily(store: Store) -> int:
    try:
        value = int(store.get_setting("water_daily") or "3")
    except ValueError:
        value = 3
    return max(1, min(value, MAX_DAILY))


def _water_gap(store: Store) -> int:
    try:
        value = int(store.get_setting("water_gap") or "10")
    except ValueError:
        value = 10
    return max(10, min(value, 240))


def _bounded_water_daily(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 3
    return max(1, min(value, MAX_DAILY))


def _bounded_water_gap(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(10, min(value, 240))


def _prepare_water(store: Store, data: dict) -> str | None:
    keys = ("water_enabled", "water_daily", "water_gap", "water_phrases", "water_board")
    if not any(key in data for key in keys):
        return None
    enabled = data.get("water_enabled") if "water_enabled" in data else (store.get_setting("water_enabled") == "1")
    phrases = parse_phrases(
        str(data.get("water_phrases") if "water_phrases" in data else (store.get_setting("water_phrases") or ""))
    )
    daily = _bounded_water_daily(data.get("water_daily") if "water_daily" in data else _water_daily(store))
    gap = _bounded_water_gap(data.get("water_gap") if "water_gap" in data else _water_gap(store))
    if "water_daily" in data:
        try:
            asked = int(data.get("water_daily"))
        except (TypeError, ValueError):
            return "每天条数必须是数字"
        if asked < 1 or asked > MAX_DAILY:
            return f"每天条数要在 1 到 {MAX_DAILY} 之间"
        daily = asked
    if "water_gap" in data:
        try:
            asked_gap = int(data.get("water_gap"))
        except (TypeError, ValueError):
            return "间隔分钟必须是数字"
        if asked_gap < 10 or asked_gap > 240:
            return "间隔要在 10 到 240 分钟之间"
        gap = asked_gap
    if "water_board" in data:
        try:
            resolve_water_board(str(data.get("water_board") or ""))
        except ValueError as exc:
            return str(exc)
    return validate_water_settings(bool(enabled), phrases, daily, gap)


def update_settings(store: Store, data: dict) -> str | None:
    interval = None
    if "interval" in data and str(data.get("interval") or "").strip() != "":
        try:
            interval = int(data["interval"])
        except (TypeError, ValueError):
            return "检查间隔必须是数字"
        if interval < MIN_INTERVAL or interval > MAX_INTERVAL:
            return f"私信检查间隔要在 {MIN_INTERVAL} 到 {MAX_INTERVAL} 秒之间"
    keyword_interval = None
    if "keyword_interval" in data and str(data.get("keyword_interval") or "").strip() != "":
        try:
            keyword_interval = int(data["keyword_interval"])
        except (TypeError, ValueError):
            return "关键词检查间隔必须是数字"
        if keyword_interval < MIN_KEYWORD_INTERVAL or keyword_interval > MAX_INTERVAL:
            return f"关键词检查间隔要在 {MIN_KEYWORD_INTERVAL} 到 {MAX_INTERVAL} 秒之间"
    token_text = str(data.get("tg_token") or "")
    token = ""
    ignore_token = False
    if token_text.strip():
        token = normalize_token(token_text)
        if ":" not in token:
            # 浏览器常把面板密码填进 Token 框。已有 Token 时忽略它，避免保存间隔失败。
            if store.get_setting("tg_token"):
                ignore_token = True
                token = ""
            else:
                return "Bot Token 格式不对，一般是一串数字、冒号，再加一串字符。"
    water_error = _prepare_water(store, data)
    if water_error:
        return water_error
    if interval is not None:
        store.set_setting("interval", str(interval))
    if keyword_interval is not None:
        store.set_setting("keyword_interval", str(keyword_interval))

    cookie_text = str(data.get("cookie") or "")
    if data.get("clear_cookie") and not cookie_text.strip():
        store.delete_setting("cookie")
        store.set_state("baseline_done", "0")
        store.delete_state("self_id")
        store.delete_state("self_name")
    elif cookie_text.strip():
        parsed = parse_cookie(cookie_text)
        if len(parsed) < 8 or "=" not in parsed:
            return "Cookie 看起来不完整。请从浏览器请求标头里复制整段 Cookie。"
        previous = store.get_setting("cookie")
        store.set_setting("cookie", parsed)
        if previous != parsed:
            store.set_state("baseline_done", "0")

    if data.get("clear_token") and not token_text.strip():
        store.delete_setting("tg_token")
    elif token and not ignore_token:
        store.set_setting("tg_token", token)

    if "tg_chat_id" in data:
        chat_id = str(data.get("tg_chat_id") or "").strip()
        if chat_id:
            store.set_setting("tg_chat_id", chat_id)
    if "checkin_enabled" in data:
        store.set_setting("checkin_enabled", "1" if data.get("checkin_enabled") else "0")
    if "water_enabled" in data:
        store.set_setting("water_enabled", "1" if data.get("water_enabled") else "0")
    if "water_daily" in data:
        store.set_setting("water_daily", str(_bounded_water_daily(data.get("water_daily"))))
    if "water_gap" in data:
        store.set_setting("water_gap", str(_bounded_water_gap(data.get("water_gap"))))
    if "water_phrases" in data:
        store.set_setting("water_phrases", "\n".join(parse_phrases(str(data.get("water_phrases") or ""))))
    if "water_board" in data:
        store.set_setting("water_board", resolve_water_board(str(data.get("water_board") or "")))
    if "heartbeat_enabled" in data:
        store.set_setting("heartbeat_enabled", "1" if data.get("heartbeat_enabled") else "0")
    if "heartbeat_hour" in data and str(data.get("heartbeat_hour") or "").strip() != "":
        try:
            hour = int(data.get("heartbeat_hour"))
        except (TypeError, ValueError):
            return "存活消息的小时必须是数字"
        if hour < 0 or hour > 23:
            return "存活消息的小时要在 0 到 23 之间"
        store.set_setting("heartbeat_hour", str(hour))
    if "quiet_enabled" in data:
        store.set_setting("quiet_enabled", "1" if data.get("quiet_enabled") else "0")
    if "quiet_start" in data:
        start = str(data.get("quiet_start") or "").strip()
        if not valid_hhmm(start):
            return "免打扰开始时间要写成 23:00 这样"
        store.set_setting("quiet_start", start)
    if "quiet_end" in data:
        end = str(data.get("quiet_end") or "").strip()
        if not valid_hhmm(end):
            return "免打扰结束时间要写成 08:00 这样"
        store.set_setting("quiet_end", end)
    return None


def test_cookie(store: Store, data: dict) -> dict:
    raw = str(data.get("cookie") or "").strip()
    cookie = parse_cookie(raw) if raw else (store.get_setting("cookie") or "")
    if not cookie:
        raise ValueError("还没有 Cookie")
    client = NodeSeekClient(cookie)
    try:
        account = client.get_account()
        payload = client.get_message_list()
        rows = message_rows(payload)
    except (AuthError, CloudflareError, NodeSeekError):
        raise
    self_id = account.member_id if account else client.resolve_self_id(payload)
    name = account.member_name if account and account.member_name else ""
    if not self_id:
        raise NodeSeekError("Cookie 可用，但没能识别当前账号。")
    return {
        "ok": True,
        "self_id": self_id,
        "self_name": name,
        "conversations": len(rows),
        "message": f"Cookie 可用。账号 {name or self_id}，当前 {len(rows)} 个会话。",
    }


def test_telegram(store: Store, data: dict) -> dict:
    token, chat_id = _telegram_pair(store, data)
    send_message(
        token,
        chat_id,
        "NodeSeek 私信监控：这是一条测试消息。收到就说明 Telegram 已接通。",
        reply_keyboard(),
    )
    return {"ok": True, "message": "测试消息已发送，请看 Telegram。"}


def discover(store: Store, data: dict) -> dict:
    token_text = str(data.get("tg_token") or "").strip()
    token = normalize_token(token_text) if token_text else (store.get_setting("tg_token") or "")
    if not token:
        raise TelegramError("请先填写 Bot Token")
    chat_id = discover_chat_id(token, store.get_state("tg_last_chat_id") or "")
    return {"ok": True, "tg_chat_id": chat_id, "message": f"找到 Chat ID：{chat_id}"}


def normalize_token(raw: str) -> str:
    token = raw.strip()
    if token.lower().startswith("bot") and ":" in token[3:]:
        token = token[3:]
    return token.strip()


def _telegram_pair(store: Store, data: dict) -> tuple[str, str]:
    token_text = str(data.get("tg_token") or "").strip()
    token = normalize_token(token_text) if token_text else (store.get_setting("tg_token") or "")
    chat_id = str(data.get("tg_chat_id") or "").strip() or (store.get_setting("tg_chat_id") or "")
    if not token or not chat_id:
        raise TelegramError("请先填写 Bot Token 和 Chat ID")
    return token, chat_id


def mentions_payload(store: Store) -> dict:
    return {
        "ok": True,
        "baseline_done": store.get_state("mentions_baseline_done") == "1",
        "last_error": store.get_state("mentions_last_error") or "",
        "items": store.list_mentions(80),
    }


def replies_payload(store: Store) -> dict:
    return {
        "ok": True,
        "baseline_done": store.get_state("replies_baseline_done") == "1",
        "last_error": store.get_state("replies_last_error") or "",
        "items": store.list_replies(80),
    }


def quiet_active(store: Store) -> bool:
    if (store.get_setting("quiet_enabled") or "0") != "1":
        return False
    from datetime import datetime

    return in_quiet_hours(
        datetime.now().astimezone(),
        store.get_setting("quiet_start") or "23:00",
        store.get_setting("quiet_end") or "08:00",
    )


def _heartbeat_hour(store: Store) -> int:
    try:
        hour = int(store.get_setting("heartbeat_hour") or "9")
    except ValueError:
        return 9
    return max(0, min(hour, 23))


def posts_payload(store: Store) -> dict:
    raw_boards = store.get_setting("boards")
    slugs = selected_slugs(raw_boards)
    return {
        "ok": True,
        "keywords": store.get_setting("keywords") or "",
        "excludes": store.get_setting("excludes") or "",
        "boards": slugs,
        "boards_all": (raw_boards or "").strip() in ("", "*"),
        "available": [{"slug": slug, "name": name} for slug, name in BOARDS],
        "baseline_done": store.get_state("posts_baseline_done") == "1",
        "last_error": store.get_state("posts_last_error") or "",
        "items": store.list_hits(80),
    }


def update_keywords(store: Store, data: dict) -> str | None:
    if "keywords" in data:
        text = str(data.get("keywords") or "")
        error = validate_keywords(text)
        if error:
            return error
        store.set_setting("keywords", format_keyword_rules(parse_keyword_rules(text)))
    if "excludes" in data:
        text = str(data.get("excludes") or "")
        error = validate_excludes(text)
        if error:
            return error
        store.set_setting("excludes", "\n".join(parse_excludes(text)))
    if "boards" in data:
        try:
            store.set_setting("boards", normalize_boards(data.get("boards")))
        except ValueError as exc:
            return str(exc)
    return None


def open_browser(store: Store) -> dict:
    store.set_setting("auth_mode", "browser")
    docker = os.environ.get("NS_RUNTIME") == "docker"
    if port_open():
        if docker:
            return {"ok": True, "message": "登录浏览器已经在容器里运行。请打开网页桌面，在里面登录 NodeSeek。"}
        return {"ok": True, "message": "登录窗口已经开着。请在里面登录 NodeSeek，并保持窗口不要关。"}
    open_login_window()
    if not wait_for_port():
        if docker:
            raise CloudflareError("容器里的登录浏览器没有连上。请看 1Panel 的容器日志。")
        raise CloudflareError("没能打开登录浏览器。请确认本机安装了 Edge 或 Chrome。")
    if docker:
        return {"ok": True, "message": "已打开容器里的浏览器。请到网页桌面登录 NodeSeek。"}
    return {
        "ok": True,
        "message": "已打开 Edge。请在那个窗口登录 NodeSeek，完成验证后保持窗口开着。",
    }


def test_browser(store: Store) -> dict:
    store.set_setting("auth_mode", "browser")
    client = BrowserClient()
    account = client.get_account()
    payload = client.get_message_list()
    rows = message_rows(payload)
    self_id = account.member_id if account else client.resolve_self_id(payload)
    name = account.member_name if account and account.member_name else ""
    if not self_id:
        raise AuthError("登录窗口可用，但没能识别当前账号。请确认已经登录。")
    if self_id:
        store.set_state("self_id", str(self_id))
    if name:
        store.set_state("self_name", name)
    return {
        "ok": True,
        "self_id": self_id,
        "self_name": name,
        "conversations": len(rows),
        "message": f"登录窗口可用。账号 {name or self_id}，当前 {len(rows)} 个会话。",
    }


def _missing_config(store: Store) -> str | None:
    has_cookie = bool(store.get_setting("cookie"))
    has_keywords = bool(parse_keywords(store.get_setting("keywords") or ""))
    use_browser = auth_mode(store) == "browser"
    if not has_cookie and not has_keywords and not use_browser:
        return "请先保存 NodeSeek Cookie，或打开登录浏览器，或在关键词页保存要监控的词"
    if not store.get_setting("tg_token") or not store.get_setting("tg_chat_id"):
        return "请先在设置里保存 Telegram Bot Token 和 Chat ID"
    return None


def _split(raw: str) -> tuple[str, dict]:
    parsed = urlparse(raw)
    return parsed.path, parse_qs(parsed.query)


def _limit(query: dict, default: int) -> int:
    raw = (query.get("limit") or [str(default)])[0]
    try:
        return int(raw)
    except ValueError:
        return default


def create_httpd(app: App, host: str = HOST, port: int = 0) -> PanelServer:
    return PanelServer((host, port), app)


def panel_account(store: Store | None) -> tuple[str, str, str]:
    user = ""
    secret = ""
    if store is not None:
        user = (store.get_setting("panel_user") or "").strip()
        secret = store.get_setting("panel_password") or ""
    if user and secret.startswith("scrypt$"):
        return user, secret, "hash"
    env_user = (os.environ.get("NS_PANEL_USER") or "").strip()
    env_password = os.environ.get("NS_PANEL_PASSWORD") or ""
    return env_user, env_password, "plain"


def panel_login_required(store: Store | None) -> bool:
    user, secret, _kind = panel_account(store)
    return bool(user and secret)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(secret: str, kind: str, provided: str) -> bool:
    if kind == "hash":
        try:
            _kind, salt_hex, digest = secret.split("$", 2)
            salt = bytes.fromhex(salt_hex)
            check = hashlib.scrypt(provided.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()
        except Exception:
            return False
        return secrets.compare_digest(check, digest)
    if len(provided) != len(secret):
        return False
    return secrets.compare_digest(provided, secret)


def update_panel_account(store: Store, data: dict) -> str | None:
    user, secret, kind = panel_account(store)
    current = str(data.get("current_password") or "")
    if secret and not password_matches(secret, kind, current):
        return "当前密码不对"
    new_user = str(data.get("panel_user") or "").strip()
    if not new_user or ":" in new_user or any(char.isspace() for char in new_user) or len(new_user) > 64:
        return "账号不能为空，也不能包含空格或冒号"
    new_password = str(data.get("panel_password") or "")
    confirm = str(data.get("panel_password_confirm") or "")
    if len(new_password) < 6 or len(new_password) > 128:
        return "新密码要在 6 到 128 个字符之间"
    if new_password != confirm:
        return "两次输入的新密码不一致"
    store.set_setting("panel_user", new_user)
    store.set_setting("panel_password", hash_password(new_password))
    return None


def session_token(cookie_header: str | None) -> str:
    if not cookie_header:
        return ""
    for part in cookie_header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == SESSION_COOKIE:
            return value.strip()
    return ""


def session_cookie(token: str, https: bool) -> str:
    cookie = f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_SECONDS}"
    if https:
        cookie += "; Secure"
    return cookie


def cleared_session_cookie() -> str:
    return f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or "unknown"
    host = handler.client_address[0] if handler.client_address else ""
    return host or "unknown"


def panel_authorized(header: str | None, store: Store | None = None) -> bool:
    user, secret, kind = panel_account(store)
    if not user or not secret:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True).decode("utf-8")
    except Exception:
        return False
    got_user, sep, got_password = decoded.partition(":")
    if not sep:
        return False
    user_ok = len(got_user) == len(user) and secrets.compare_digest(got_user, user)
    return user_ok and password_matches(secret, kind, got_password)


def serve(root: Path | None = None, open_browser: bool = True) -> None:
    app = App(root or Path(__file__).resolve().parent.parent)
    host = (os.environ.get("NS_HOST") or HOST).strip() or HOST
    requested = (os.environ.get("NS_PORT") or "").strip()
    if os.environ.get("NS_OPEN_BROWSER") == "0":
        open_browser = False
    httpd = None
    port = 0
    if requested:
        try:
            port = int(requested)
            httpd = create_httpd(app, host, port)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"面板端口 {requested} 没有绑上：{exc}") from exc
    else:
        for candidate in range(8787, 8797):
            try:
                httpd = create_httpd(app, host, candidate)
                port = candidate
                break
            except OSError:
                continue
    if httpd is None:
        raise SystemExit("8787 到 8796 端口都被占用了，请关掉占用端口的程序后重试。")
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"面板地址：http://{shown}:{port}", flush=True)
    if os.environ.get("NS_RUNTIME") == "docker":
        print("网页桌面在本机 6080 端口。请在 1Panel 里做反向代理，并打开访问认证。", flush=True)
        print("停止这个容器就会停止监控。", flush=True)
    else:
        print("关闭这个窗口就会停止监控。", flush=True)
    if open_browser:
        webbrowser.open(f"http://{shown}:{port}")
    app.monitor.listen()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止。", flush=True)
    finally:
        app.monitor.stop()
        httpd.server_close()
