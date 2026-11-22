"""读取「@我」。只拉列表，不调用标已读接口。"""

from __future__ import annotations

from dataclasses import dataclass

from ns_panel.diff import preview, strip_html


COMMENTS_PER_PAGE = 10


@dataclass(frozen=True)
class Mention:
    mention_id: int
    commenter_id: int
    commenter_name: str
    post_id: int
    floor_id: int
    title: str
    created_at: str

    @property
    def link(self) -> str:
        return mention_link(self.post_id, self.floor_id)


def mention_link(post_id: int, floor_id: int) -> str:
    post = int(post_id)
    floor = int(floor_id or 0)
    if floor <= 0:
        return f"https://www.nodeseek.com/post-{post}-1"
    page = 1 if floor <= COMMENTS_PER_PAGE else (floor - 1) // COMMENTS_PER_PAGE + 1
    return f"https://www.nodeseek.com/post-{post}-{page}#{floor}"


def list_rows(payload, keys: tuple[str, ...], label: str) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError(f"{label}没有返回对象")
    nodes = [payload]
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nodes.append(value)
    for node in nodes:
        for key in keys + ("list", "rows", "msgArray"):
            nested = node.get(key)
            if isinstance(nested, list):
                return nested
    raise ValueError(f"{label}没有返回列表")


def parse_mentions(payload) -> list[Mention]:
    rows = list_rows(payload, ("data", "atList"), "提到我的接口")
    found: list[Mention] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mention_id = _int(row.get("id"))
        post_id = _int(row.get("post_id"))
        if mention_id <= 0 or post_id <= 0 or mention_id in seen:
            continue
        seen.add(mention_id)
        found.append(
            Mention(
                mention_id=mention_id,
                commenter_id=_int(row.get("commenter_id")),
                commenter_name=strip_html(str(row.get("commenter_name") or "")).strip() or "未知",
                post_id=post_id,
                floor_id=_int(row.get("floor_id")),
                title=strip_html(str(row.get("title") or "")).strip() or "无标题",
                created_at=str(row.get("created_at") or "").strip(),
            )
        )
    return found


def plan_mentions(
    rows: list[Mention],
    seen_ids: set[int],
    baseline_done: bool,
) -> list[tuple[Mention, bool]]:
    max_seen = max(seen_ids) if seen_ids else 0
    plans: list[tuple[Mention, bool]] = []
    for row in rows:
        push = baseline_done and row.mention_id not in seen_ids and row.mention_id > max_seen
        plans.append((row, push))
    return plans


def format_mention_alerts(rows: list[dict]) -> str:
    lines = ["NodeSeek 有人@你" if len(rows) == 1 else f"NodeSeek 有人@你 {len(rows)} 条"]
    shown = rows[:8]
    for row in shown:
        lines.extend(
            [
                "",
                f"{row.get('commenter_name') or '未知'} 在帖子里@了你",
                f"帖子：{row.get('title') or '无标题'}",
                f"时间：{row.get('created_at') or '未知'}",
                f"打开：{row.get('link') or mention_link(int(row.get('post_id') or 0), int(row.get('floor_id') or 0))}",
            ]
        )
    if len(rows) > len(shown):
        lines.append("")
        lines.append(f"还有 {len(rows) - len(shown)} 条，请打开本机面板的提到我页查看。")
    text = "\n".join(lines)
    if len(text) > 3900:
        return preview(text, 3899) + "…"
    return text


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
