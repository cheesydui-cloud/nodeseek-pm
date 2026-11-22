"""读取「回复我」。只拉列表，不调用标已读接口。"""

from __future__ import annotations

from dataclasses import dataclass

from ns_panel.diff import preview, strip_html
from ns_panel.mentions import list_rows, mention_link


@dataclass(frozen=True)
class Reply:
    reply_id: int
    commenter_id: int
    commenter_name: str
    post_id: int
    floor_id: int
    title: str
    created_at: str

    @property
    def link(self) -> str:
        return mention_link(self.post_id, self.floor_id)


def parse_replies(payload) -> list[Reply]:
    rows = _rows(payload)
    found: list[Reply] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        reply_id = _int(row.get("id"))
        post_id = _int(row.get("post_id"))
        if reply_id <= 0 or post_id <= 0 or reply_id in seen:
            continue
        seen.add(reply_id)
        title = strip_html(str(row.get("title") or row.get("post_title") or "")).strip() or "无标题"
        name = strip_html(str(row.get("commenter_name") or row.get("member_name") or "")).strip() or "未知"
        found.append(
            Reply(
                reply_id=reply_id,
                commenter_id=_int(row.get("commenter_id") or row.get("member_id")),
                commenter_name=name,
                post_id=post_id,
                floor_id=_int(row.get("floor_id")),
                title=title,
                created_at=str(row.get("created_at") or "").strip(),
            )
        )
    return found


def plan_replies(
    rows: list[Reply],
    seen_ids: set[int],
    baseline_done: bool,
) -> list[tuple[Reply, bool]]:
    max_seen = max(seen_ids) if seen_ids else 0
    plans: list[tuple[Reply, bool]] = []
    for row in rows:
        push = baseline_done and row.reply_id not in seen_ids and row.reply_id > max_seen
        plans.append((row, push))
    return plans


def format_reply_alerts(rows: list[dict]) -> str:
    lines = ["NodeSeek 有人回复了你的帖子" if len(rows) == 1 else f"NodeSeek 有人回复了你的帖子 {len(rows)} 条"]
    shown = rows[:8]
    for row in shown:
        lines.extend(
            [
                "",
                f"{row.get('commenter_name') or '未知'} 回复了你的帖子",
                f"帖子：{row.get('title') or '无标题'}",
                f"时间：{row.get('created_at') or '未知'}",
                f"打开：{row.get('link') or mention_link(int(row.get('post_id') or 0), int(row.get('floor_id') or 0))}",
            ]
        )
    if len(rows) > len(shown):
        lines.append("")
        lines.append(f"还有 {len(rows) - len(shown)} 条，请打开面板的回复我页查看。")
    text = "\n".join(lines)
    if len(text) > 3900:
        return preview(text, 3899) + "…"
    return text


def _rows(payload):
    return list_rows(payload, ("data", "replys", "replies", "replyList"), "回复我的接口")


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
