"""纯函数：解析 NodeSeek 私信，并判断哪些要推送。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Member:
    member_id: int
    member_name: str


@dataclass(frozen=True)
class Conversation:
    peer_id: int
    peer_name: str
    content: str
    created_at: str
    message_id: int
    sender_id: int
    receiver_id: int
    viewed: int


def as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def strip_html(value) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_cookie(raw: str) -> str:
    text = (raw or "").strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    text = text.replace("\r", " ").replace("\n", " ")
    parts: list[str] = []
    for part in text.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


_NAME_KEYS = (
    "member_name",
    "memberName",
    "username",
    "userName",
    "nickname",
    "nickName",
    "name",
)
_SPACE_LINK = re.compile(
    r"""href=["'](?:https://www\.nodeseek\.com)?/space/(\d+)["']([^>]*)>(.*?)</a>""",
    re.I | re.S,
)


def _member_name(node: dict) -> str:
    for key in _NAME_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def member_from_node(node) -> Member | None:
    if not isinstance(node, dict):
        return None
    raw_id = (
        node.get("member_id")
        if node.get("member_id") not in (None, "")
        else node.get("memberId")
        if node.get("memberId") not in (None, "")
        else node.get("uid")
        if node.get("uid") not in (None, "")
        else node.get("user_id")
        if node.get("user_id") not in (None, "")
        else node.get("userId")
    )
    name = _member_name(node)
    if raw_id in (None, "") and name:
        raw_id = node.get("id")
    member_id = as_int(raw_id)
    if member_id is None or member_id <= 0:
        return None
    return Member(member_id, name)


def member_from_html(html_text: str) -> Member | None:
    """从页面顶部的个人主页链接读取当前登录用户。帖子列表里的作者不算。"""
    text = html_text or ""
    end = re.search(r"</header>", text, re.I)
    if not end:
        return None
    region = text[: end.end()]
    found: list[Member] = []
    for match in _SPACE_LINK.finditer(region):
        member_id = int(match.group(1))
        if member_id <= 0:
            continue
        inner = strip_html(match.group(3)).strip().split("\n")[0].strip()
        alt = re.search(r"""alt=["']([^"']+)["']""", match.group(0))
        name = inner or (alt.group(1).strip() if alt else "")
        if len(name) > 64:
            name = ""
        found.append(Member(member_id, name))
    if not found:
        return None
    return found[-1]


def extract_member(payload) -> Member | None:
    if not isinstance(payload, dict):
        return None
    nodes = [payload]
    for key in ("data", "member", "user", "account", "profile", "result"):
        node = payload.get(key)
        if isinstance(node, dict):
            nodes.append(node)
            for inner_key in ("member", "user", "account", "profile"):
                inner = node.get(inner_key)
                if isinstance(inner, dict):
                    nodes.append(inner)
    for node in nodes:
        found = member_from_node(node)
        if found:
            return found
    return None


def message_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        raise ValueError("私信接口返回的不是 JSON 对象")
    if payload.get("success") is False:
        message = payload.get("message") or payload.get("msg") or "私信接口返回失败"
        raise ValueError(str(message))
    rows = payload.get("msgArray")
    if rows is None and isinstance(payload.get("data"), list):
        rows = payload.get("data")
    if isinstance(payload.get("data"), dict) and rows is None:
        data = payload["data"]
        rows = data.get("msgArray") or data.get("list") or data.get("rows")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("私信接口没有返回列表")
    return [row for row in rows if isinstance(row, dict)]


def infer_self_id(rows: list[dict]) -> int | None:
    participants: list[set[int]] = []
    for row in rows:
        sender = as_int(row.get("sender_id"))
        receiver = as_int(row.get("receiver_id"))
        if sender and receiver and sender > 0 and receiver > 0:
            participants.append({sender, receiver})
    if not participants:
        return None
    common = set.intersection(*participants)
    if len(common) == 1:
        return next(iter(common))
    return None


def self_id_from_thread(payload, requested_id: int | None = None) -> int | None:
    if not isinstance(payload, dict):
        return None
    talk = payload.get("talkTo") or payload.get("talk_to") or {}
    other = as_int(talk.get("member_id") if isinstance(talk, dict) else None)
    if other is None:
        other = as_int(requested_id)
    if other is None:
        return None
    found: set[int] = set()
    rows = payload.get("msgArray") or []
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("sender_id", "receiver_id"):
            value = as_int(row.get(key))
            if value and value > 0:
                found.add(value)
    found.discard(other)
    if len(found) == 1:
        return next(iter(found))
    return None


def self_name_from_rows(rows: list[dict], self_id: int) -> str:
    for row in rows:
        if as_int(row.get("sender_id")) == self_id:
            name = row.get("sender_name") or row.get("senderName")
            if isinstance(name, str) and name.strip():
                return name.strip()
        if as_int(row.get("receiver_id")) == self_id:
            name = row.get("receiver_name") or row.get("receiverName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


def _viewed(value) -> int:
    if value in (True, 1, "1", "true", "True"):
        return 1
    return 0


def parse_message_list(payload, self_id: int | None) -> list[Conversation]:
    conversations: list[Conversation] = []
    for row in message_rows(payload):
        sender_id = as_int(row.get("sender_id")) or 0
        receiver_id = as_int(row.get("receiver_id")) or 0
        sender_name = str(row.get("sender_name") or row.get("senderName") or "").strip()
        receiver_name = str(row.get("receiver_name") or row.get("receiverName") or "").strip()
        if self_id and sender_id == self_id:
            peer_id = receiver_id
            peer_name = receiver_name
        elif self_id and receiver_id == self_id:
            peer_id = sender_id
            peer_name = sender_name
        elif _viewed(row.get("viewed")) == 0 and sender_id:
            peer_id = sender_id
            peer_name = sender_name
        else:
            peer_id = receiver_id or sender_id
            peer_name = receiver_name or sender_name
        if peer_id <= 0:
            continue
        message_id = as_int(row.get("max_id")) or as_int(row.get("id")) or 0
        created_at = str(row.get("created_at") or row.get("createdAt") or "").strip()
        content = strip_html(row.get("content") or row.get("message") or "")
        conversations.append(
            Conversation(
                peer_id=peer_id,
                peer_name=peer_name or f"用户{peer_id}",
                content=content,
                created_at=created_at,
                message_id=message_id if message_id > 0 else 0,
                sender_id=sender_id,
                receiver_id=receiver_id,
                viewed=_viewed(row.get("viewed")),
            )
        )
    return conversations


def _has_id(value: int) -> bool:
    return isinstance(value, int) and value > 0


def is_newer(old: Conversation, current: Conversation) -> bool:
    if _has_id(current.message_id) and _has_id(old.message_id):
        return current.message_id > old.message_id
    return (current.created_at, current.content) != (old.created_at, old.content)


def is_incoming(conv: Conversation, self_id: int | None) -> bool:
    if self_id:
        return conv.sender_id != self_id
    return conv.viewed == 0


def select_notifications(
    previous: dict[int, Conversation],
    current: list[Conversation],
    self_id: int | None,
    baseline_done: bool,
) -> tuple[list[Conversation], list[Conversation], bool]:
    """返回 (要推送, 直接记下, 这次是否在建立基线)。"""
    if not baseline_done:
        return [], list(current), True
    notify: list[Conversation] = []
    upserts: list[Conversation] = []
    for conv in current:
        old = previous.get(conv.peer_id)
        if old is None or is_newer(old, conv):
            if is_incoming(conv, self_id):
                notify.append(conv)
                continue
        upserts.append(conv)
    notify.sort(key=lambda item: (item.message_id, item.created_at))
    return notify, upserts, False


def preview(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def talk_link(peer_id: int) -> str:
    return f"https://www.nodeseek.com/notification#/message?mode=talk&to={peer_id}"


def format_pushes(conversations: list[Conversation]) -> str:
    if not conversations:
        return ""
    if len(conversations) == 1:
        item = conversations[0]
        return "\n".join(
            [
                "NodeSeek 新私信",
                f"来自：{item.peer_name}",
                f"时间：{item.created_at or '未知'}",
                f"内容：{preview(item.content, 500) or '（没有文字）'}",
                f"打开：{talk_link(item.peer_id)}",
            ]
        )
    lines = [f"NodeSeek 新私信 {len(conversations)} 条"]
    for item in conversations:
        lines.extend(
            [
                "",
                f"来自：{item.peer_name}",
                f"时间：{item.created_at or '未知'}",
                f"内容：{preview(item.content, 180) or '（没有文字）'}",
                f"打开：{talk_link(item.peer_id)}",
            ]
        )
    text = "\n".join(lines)
    if len(text) > 3900:
        return text[:3899] + "…"
    return text
