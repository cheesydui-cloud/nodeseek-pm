"""Telegram 底部按钮。只读本机记录，不请求 NodeSeek。"""

from __future__ import annotations

from ns_panel.diff import preview, talk_link
from ns_panel.posts import board_name


OWNER_HINT = "点下面的按钮查看记录。"
FOREIGN_TEXT = "这个机器人不对外。"
HELLO = "按钮已放在输入框上方。点私信、提到我、回复、关键词或状态。"

_COMMANDS = {
    "/start": "start",
    "私信": "pm",
    "提到我": "mentions",
    "回复": "replies",
    "关键词": "keywords",
    "状态": "status",
}


def reply_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "私信"}, {"text": "提到我"}, {"text": "回复"}],
            [{"text": "关键词"}, {"text": "状态"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def open_markup(links: list[tuple[str, str]]) -> dict | None:
    rows = []
    for label, url in links[:8]:
        href = (url or "").strip()
        if not href.startswith("http://") and not href.startswith("https://"):
            continue
        text = (label or "打开").strip() or "打开"
        rows.append([{"text": text[:32], "url": href}])
    if not rows:
        return None
    return {"inline_keyboard": rows}


def pm_open_links(rows) -> list[tuple[str, str]]:
    links = []
    for row in list(rows)[:8]:
        name = _name(row, "peer_name")
        label = "打开" if len(list(rows)) == 1 else f"打开 {name}"
        peer_id = _int(row, "peer_id")
        links.append((label, talk_link(peer_id)))
    return links


def titled_open_links(rows, name_key: str = "title") -> list[tuple[str, str]]:
    links = []
    items = list(rows)[:8]
    for row in items:
        title = _name(row, name_key)
        label = "打开" if len(items) == 1 else f"打开 {title}"
        links.append((label, str(_field(row, "link") or "")))
    return links


def answer_for(text: str, store, running: bool) -> dict | None:
    command = _COMMANDS.get((text or "").strip())
    if command is None:
        return None
    if command == "start":
        body = HELLO
    elif command == "pm":
        body = _pm_text(store)
    elif command == "mentions":
        body = _mention_text(store)
    elif command == "replies":
        body = _reply_text(store)
    elif command == "keywords":
        body = _keyword_text(store)
    else:
        body = _status_text(store, running)
    return {"text": _scrub(body, store), "reply_markup": reply_keyboard()}


def _pm_text(store) -> str:
    rows = store.list_conversations()[:8]
    if not rows:
        return "还没有私信。"
    lines = ["最近私信"]
    for row in rows:
        lines.extend(
            [
                "",
                f"来自：{_name(row, 'peer_name')}",
                f"时间：{row.get('created_at') or '未知'}",
                f"内容：{preview(str(row.get('content') or ''), 80) or '（没有文字）'}",
                f"打开：{talk_link(_int(row, 'peer_id'))}",
            ]
        )
    return "\n".join(lines)


def _mention_text(store) -> str:
    rows = store.list_mentions(8)
    if not rows:
        return "还没有提到我。"
    lines = ["最近提到我"]
    for row in rows:
        lines.extend(
            [
                "",
                f"{_name(row, 'commenter_name')} 在帖子里@了你",
                f"帖子：{_name(row, 'title')}",
                f"时间：{row.get('created_at') or '未知'}",
                f"打开：{row.get('link') or ''}",
            ]
        )
    return "\n".join(lines)


def _reply_text(store) -> str:
    rows = store.list_replies(8)
    if not rows:
        return "还没有回复。"
    lines = ["最近回复"]
    for row in rows:
        lines.extend(
            [
                "",
                f"{_name(row, 'commenter_name')} 回复了你的帖子",
                f"帖子：{_name(row, 'title')}",
                f"时间：{row.get('created_at') or '未知'}",
                f"打开：{row.get('link') or ''}",
            ]
        )
    return "\n".join(lines)


def _keyword_text(store) -> str:
    rows = store.list_hits(8)
    if not rows:
        return "还没有关键词帖。"
    lines = ["最近关键词"]
    for row in rows:
        lines.extend(
            [
                "",
                f"[{board_name(str(row.get('category') or ''))}] {_name(row, 'title')}",
                f"命中：{row.get('keywords') or ''}",
                f"打开：{row.get('link') or ''}",
            ]
        )
    return "\n".join(lines)


def _status_text(store, running: bool) -> str:
    from ns_panel.monitor import interval_seconds, keyword_interval_seconds

    lines = [
        "NodeSeek 监控状态",
        f"监控：{'在跑' if running else '没在跑'}",
        f"上次成功：{store.get_state('last_success') or '还没有'}",
        f"今天已推送：{store.count_pushes_today()} 条",
        f"间隔：私信 {interval_seconds(store)} 秒，关键词 {keyword_interval_seconds(store)} 秒",
    ]
    error = (store.get_state("last_error") or "").strip()
    if error:
        lines.append(f"最近的问题：{error[:200]}")
    return "\n".join(lines)


def _scrub(text: str, store) -> str:
    for key in ("tg_token", "cookie", "panel_password"):
        secret = (store.get_setting(key) or "").strip()
        if secret and secret in text:
            text = text.replace(secret, "")
    return text


def _field(row, key: str):
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, "")


def _name(row, key: str) -> str:
    return str(_field(row, key) or "").strip() or "未知"


def _int(row, key: str) -> int:
    try:
        return int(_field(row, key) or 0)
    except (TypeError, ValueError):
        return 0
