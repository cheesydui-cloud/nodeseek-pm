"""读取 NodeSeek 自己的私信列表。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ns_panel.diff import (
    Member,
    extract_member,
    infer_self_id,
    message_rows,
    self_id_from_thread,
)


class NodeSeekError(Exception):
    pass


class AuthError(NodeSeekError):
    pass


class CloudflareError(NodeSeekError):
    pass


class ApiError(NodeSeekError):
    pass


BASE = "https://www.nodeseek.com"
_CA_BUNDLE: str | None = None
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def looks_like_challenge(status: int, text: str, content_type: str) -> bool:
    head = (text or "")[:800].lower()
    if "just a moment" in head or "cf-challenge" in head or "attention required" in head:
        return True
    if status in (403, 503) and "html" in (content_type or "").lower():
        return True
    if status == 403 and (text or "").lstrip().startswith("<"):
        return True
    return False


COOKIE_BLOCKED = (
    "Cloudflare 拦住了请求。复制出来的 Cookie 和这个程序对不上，过一会儿就会失效。"
    "请到面板设置里点「打开登录浏览器」，在弹出的窗口登录一次，并保持窗口开着。"
)
COOKIE_AUTH = "Cookie 已失效或未登录。请重新复制 Cookie，或改用登录浏览器。"


def is_auth_failure(payload, status: int) -> bool:
    if status == 401:
        return True
    if not isinstance(payload, dict):
        return False
    message = str(payload.get("message") or payload.get("msg") or "")
    if payload.get("status") == 404 and "USER NOT FOUND" in message.upper():
        return True
    if any(word in message for word in ("未登录", "请登录", "登录失效", "请先登录")):
        return True
    return False


def read_api(status: int, content_type: str, text: str, blocked: str, unauthorized: str):
    if looks_like_challenge(status, text, content_type):
        raise CloudflareError(blocked)
    try:
        data = _loads(text)
    except ValueError as exc:
        raise ApiError(f"接口没有返回 JSON（HTTP {status}）") from exc
    if is_auth_failure(data, status):
        raise AuthError(unauthorized)
    if status >= 400:
        raise ApiError(f"接口返回 HTTP {status}")
    return data


def _is_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def ca_bundle() -> str:
    """curl 在 Windows 上读不了带中文的证书路径，必要时拷到纯英文目录。"""
    global _CA_BUNDLE
    if _CA_BUNDLE:
        return _CA_BUNDLE
    import certifi

    source = certifi.where()
    if _is_ascii(source):
        _CA_BUNDLE = source
        return source

    candidates: list[Path] = []
    for key in ("TEMP", "TMP", "LOCALAPPDATA", "PROGRAMDATA"):
        value = os.environ.get(key)
        if value and _is_ascii(value):
            candidates.append(Path(value) / "nodeseek-pm")
    windows = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if windows and _is_ascii(windows):
        candidates.append(Path(windows) / "Temp" / "nodeseek-pm")
    candidates.append(Path("C:/Temp/nodeseek-pm"))

    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / "cacert.pem"
            source_stat = Path(source).stat()
            if not target.exists() or target.stat().st_size != source_stat.st_size:
                shutil.copyfile(source, target)
            _CA_BUNDLE = str(target)
            return _CA_BUNDLE
        except OSError:
            continue
    _CA_BUNDLE = source
    return source


class NodeSeekClient:
    def __init__(self, cookie: str):
        self.cookie = cookie

    def get_account(self) -> Member | None:
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

    def get_message_list(self, page: int = 1) -> dict:
        from ns_panel.pages import notification_path

        return self.get_json(notification_path("message", page))

    def get_mentions(self, page: int = 1) -> dict:
        from ns_panel.pages import notification_path

        return self.get_json(notification_path("mention", page))

    def get_replies(self, page: int = 1) -> dict:
        from ns_panel.pages import notification_path

        return self.get_json(notification_path("reply", page))

    def resolve_self_id(self, payload) -> int | None:
        rows = message_rows(payload)
        inferred = infer_self_id(rows)
        if inferred:
            return inferred
        candidates: list[int] = []
        for row in rows:
            for key in ("sender_id", "receiver_id"):
                value = row.get(key)
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0 and number not in candidates:
                    candidates.append(number)
            if len(candidates) >= 2:
                break
        for member_id in candidates[:2]:
            try:
                data = self.get_json(f"/api/notification/message/with/{member_id}")
            except ApiError:
                continue
            found = self_id_from_thread(data, member_id)
            if found:
                return found
        return None

    def get_json(self, path: str):
        url = path if path.startswith("http") else BASE + path
        status, content_type, text = self._request(url)
        return read_api(status, content_type, text, COOKIE_BLOCKED, COOKIE_AUTH)

    def post_json(self, path: str, referer: str | None = None):
        url = path if path.startswith("http") else BASE + path
        status, content_type, text = self._request(url, method="POST", referer=referer)
        return read_api(status, content_type, text, COOKIE_BLOCKED, COOKIE_AUTH)

    def check_in(self) -> dict:
        return self.post_json("/api/attendance?random=false", referer=BASE + "/board")

    def _request(self, url: str, method: str = "GET", referer: str | None = None) -> tuple[int, str, str]:
        try:
            from curl_cffi import requests as crequests
        except ImportError as exc:
            raise NodeSeekError(
                "缺少 curl_cffi。请先运行：python -m pip install -r requirements.txt"
            ) from exc
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": BASE,
            "Referer": referer or (BASE + "/notification"),
            "User-Agent": USER_AGENT,
            "Cookie": self.cookie,
        }
        if method == "POST":
            headers["Content-Length"] = "0"
        last_error: Exception | None = None
        call = crequests.post if method == "POST" else crequests.get
        for impersonate in ("chrome131", "chrome124", "chrome120", "chrome110"):
            try:
                response = call(
                    url,
                    headers=headers,
                    impersonate=impersonate,
                    timeout=25,
                    verify=ca_bundle(),
                )
                return (
                    response.status_code,
                    response.headers.get("content-type", ""),
                    response.text or "",
                )
            except Exception as exc:
                last_error = exc
                if "impersonat" in str(exc).lower():
                    continue
                break
        raise ApiError(f"请求 NodeSeek 失败：{last_error}")


def describe_checkin(payload) -> tuple[bool, str]:
    """返回 (今天是否不用再试, 给用户看的一句)。"""
    if not isinstance(payload, dict):
        return False, "签到没有返回结果"
    message = str(payload.get("message") or payload.get("msg") or "").strip()
    success = payload.get("success")
    ok = success is True or str(success).lower() == "true"
    already = any(word in message for word in ("已签", "签过", "重复签", "已经签", "签到过"))
    if not ok and not already:
        return False, message or "签到没有成功"
    text = message or ("今天已经签过了" if already else "签到成功")
    extras = []
    for key, label in (("gain", "获得"), ("current", "当前"), ("continuous", "连续")):
        value = payload.get(key)
        if value not in (None, ""):
            extras.append(f"{label} {value}")
    if extras:
        text = text.rstrip("。") + "。" + "，".join(extras)
    return True, text


def _loads(text: str):
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
