"""Telegram 底部按钮。只读本机记录，不请求 NodeSeek。"""

from __future__ import annotations

from ns_panel.diff import preview, talk_link
from ns_panel.posts import (
    board_name,
    format_keyword_rules,
    parse_keyword_rule,
    parse_keyword_rules,
    validate_keywords,
)
from ns_panel.water import water_status_line


OWNER_HINT = "点下面的按钮查看记录。"
FOREIGN_TEXT = "这个机器人不对外。"
HELLO = "按钮已放在输入框上方。点私信、提到我、回复、关键词或状态。改词表请发送：词表、加 词、删 词。"

_COMMANDS = {
    "/start": "start",
    "私信": "pm",
    "提到我": "mentions",
    "回复": "replies",
    "关键词": "keywords",
    "状态": "status",
}


KEYBOARD_MARK = "2"


def keyboard_seen(chat_id: str) -> str:
    return f"{chat_id}:{KEYBOARD_MARK}"


def reply_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "私信", "style": "primary"}, {"text": "提到我"}],
            [{"text": "回复"}, {"text": "关键词"}],
            [{"text": "状态"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "点按钮查看记录",
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
    edited = None if command is not None else _edit_keywords(text, store)
    if command is None and edited is None:
        return None
    if edited is not None:
        body = edited
    elif command == "start":
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


def _latest_last(rows):
    """Telegram 打开长消息时停在底部，最新一条放最后。"""
    return list(reversed(list(rows)))


def _pm_text(store) -> str:
    rows = _latest_last(store.list_conversations()[:8])
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
    rows = _latest_last(store.list_mentions(8))
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
    rows = _latest_last(store.list_replies(8))
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
    rows = _latest_last(store.list_hits(8))
    hint = "改词表请发送：词表、加 词、删 词"
    if not rows:
        return "还没有关键词帖。\n" + hint
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
    lines.extend(["", hint])
    return "\n".join(lines)


def _edit_keywords(text: str, store) -> str | None:
    action, rest = _split_keyword_edit(text)
    if not action:
        return None
    if action == "list":
        return _rules_text(store)
    if action == "add":
        return _add_keyword(rest, store)
    return _delete_keyword(rest, store)


def _split_keyword_edit(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    for prefix, action in (("添加", "add"), ("删除", "delete"), ("词表", "list"), ("加", "add"), ("删", "delete")):
        if raw == prefix:
            return action, ""
        if raw.startswith(prefix) and raw[len(prefix) : len(prefix) + 1].isspace():
            return action, raw[len(prefix) :].strip()
    return "", ""


def _rules_text(store) -> str:
    rules = parse_keyword_rules(store.get_setting("keywords") or "")
    if not rules:
        return "还没有关键词。\n例如：加 美西家宽 @交易 -已出"
    lines = ["当前词表：", *format_keyword_rules(rules).splitlines(), "", "加 词 @板块 -排除词", "删 词"]
    return "\n".join(lines)


def _add_keyword(rest: str, store) -> str:
    if not rest:
        return "加上哪个词？例如：加 美西家宽 @交易 -已出"
    try:
        rule = parse_keyword_rule(rest)
    except ValueError as exc:
        return str(exc)
    rules = parse_keyword_rules(store.get_setting("keywords") or "")
    key = rule.word.casefold()
    replaced = any(item.word.casefold() == key for item in rules)
    updated = [rule if item.word.casefold() == key else item for item in rules]
    if not replaced:
        updated.append(rule)
    error = _save_rules(store, updated)
    if error:
        return error
    verb = "修改" if replaced else "加上"
    store.add_log("info", f"已从 Telegram {verb}关键词：{rule.word}")
    shown = format_keyword_rules([rule])
    return f"已{verb}：{shown}\n已经见过的帖不会补推。\n\n{_rules_text(store)}"


def _delete_keyword(rest: str, store) -> str:
    word = rest.strip()
    if not word:
        return "删掉哪个词？例如：删 美西家宽"
    rules = parse_keyword_rules(store.get_setting("keywords") or "")
    key = word.casefold()
    kept = [item for item in rules if item.word.casefold() != key]
    if len(kept) == len(rules):
        return f"没有这个关键词：{word}"
    error = _save_rules(store, kept)
    if error:
        return error
    store.add_log("info", f"已从 Telegram 删掉关键词：{word}")
    return f"已删掉：{word}\n\n{_rules_text(store)}"


def _save_rules(store, rules) -> str | None:
    text = format_keyword_rules(rules)
    error = validate_keywords(text)
    if error:
        return error
    store.set_setting("keywords", text)
    return None


def _status_text(store, running: bool) -> str:
    from ns_panel.monitor import interval_seconds, keyword_interval_seconds

    lines = [
        "NodeSeek 监控状态",
        f"监控：{'在跑' if running else '没在跑'}",
        f"上次成功：{store.get_state('last_success') or '还没有'}",
        f"今天已推送：{store.count_pushes_today()} 条",
        f"间隔：私信 {interval_seconds(store)} 秒，关键词 {keyword_interval_seconds(store)} 秒",
        f"今日签到：{store.get_state('checkin_result') or '还没有'}",
        f"今日水贴：{water_status_line(store)}",
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
