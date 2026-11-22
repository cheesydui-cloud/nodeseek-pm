"""Telegram Bot 发送。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class TelegramError(Exception):
    pass


def _api(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise TelegramError(_describe(detail)) from exc
    except urllib.error.URLError as exc:
        raise TelegramError(f"连不上 Telegram：{exc.reason}") from exc
    if not body.get("ok"):
        raise TelegramError(body.get("description") or "Telegram 返回失败")
    return body


def _describe(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw or "Telegram 请求失败"
    return str(data.get("description") or raw)


def send_message(token: str, chat_id: str, text: str) -> None:
    _api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
    )


def discover_chat_id(token: str) -> str:
    body = _api(token, "getUpdates")
    for item in reversed(body.get("result") or []):
        message = (
            item.get("message")
            or item.get("edited_message")
            or item.get("my_chat_member")
            or {}
        )
        chat = message.get("chat") or {}
        if "id" in chat:
            return str(chat["id"])
    raise TelegramError("没有找到会话。请先给你的机器人发送 /start，然后再获取 Chat ID。")
