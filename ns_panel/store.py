"""本机 SQLite：配置、会话基线和运行记录。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ns_panel.diff import Conversation
from ns_panel.mentions import Mention
from ns_panel.posts import KeywordRule, Post, match_rules
from ns_panel.replies import Reply


SCHEMA = """
create table if not exists settings (
    key text primary key,
    value text not null
);
create table if not exists state (
    key text primary key,
    value text not null
);
create table if not exists conversations (
    peer_id integer primary key,
    peer_name text not null,
    content text not null,
    created_at text not null,
    message_id integer not null,
    sender_id integer not null,
    receiver_id integer not null,
    viewed integer not null,
    seen_at text not null
);
create table if not exists events (
    id integer primary key autoincrement,
    created_at text not null,
    kind text not null,
    title text not null,
    body text not null
);
create table if not exists logs (
    id integer primary key autoincrement,
    created_at text not null,
    level text not null,
    message text not null
);
create table if not exists seen_posts (
    post_id integer primary key,
    title text not null,
    link text not null,
    category text not null,
    author text not null,
    published_at text not null,
    excerpt text not null,
    seen_at text not null
);
create table if not exists keyword_hits (
    post_id integer primary key,
    title text not null,
    link text not null,
    category text not null,
    author text not null,
    published_at text not null,
    excerpt text not null,
    keywords text not null,
    status text not null,
    created_at text not null
);
create table if not exists mentions (
    mention_id integer primary key,
    commenter_id integer not null,
    commenter_name text not null,
    post_id integer not null,
    floor_id integer not null,
    title text not null,
    link text not null,
    created_at text not null,
    status text not null,
    seen_at text not null
);
create table if not exists replies (
    reply_id integer primary key,
    commenter_id integer not null,
    commenter_name text not null,
    post_id integer not null,
    floor_id integer not null,
    title text not null,
    link text not null,
    created_at text not null,
    status text not null,
    seen_at text not null
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("select value from settings where key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into settings(key, value) values(?, ?) "
                "on conflict(key) do update set value = excluded.value",
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("delete from settings where key = ?", (key,))

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("select value from state where key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into state(key, value) values(?, ?) "
                "on conflict(key) do update set value = excluded.value",
                (key, value),
            )

    def delete_state(self, key: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("delete from state where key = ?", (key,))

    def upsert_conversation(self, conv: Conversation) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                insert into conversations(
                    peer_id, peer_name, content, created_at, message_id,
                    sender_id, receiver_id, viewed, seen_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(peer_id) do update set
                    peer_name = excluded.peer_name,
                    content = excluded.content,
                    created_at = excluded.created_at,
                    message_id = excluded.message_id,
                    sender_id = excluded.sender_id,
                    receiver_id = excluded.receiver_id,
                    viewed = excluded.viewed,
                    seen_at = excluded.seen_at
                """,
                (
                    conv.peer_id,
                    conv.peer_name,
                    conv.content,
                    conv.created_at,
                    conv.message_id,
                    conv.sender_id,
                    conv.receiver_id,
                    conv.viewed,
                    now_iso(),
                ),
            )

    def conversations(self) -> dict[int, Conversation]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("select * from conversations").fetchall()
        return {row["peer_id"]: self._conversation(row) for row in rows}

    def list_conversations(self) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from conversations order by message_id desc, created_at desc"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_event(self, kind: str, title: str, body: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into events(created_at, kind, title, body) values(?, ?, ?, ?)",
                (now_iso(), kind, title, body),
            )
            conn.execute(
                "delete from events where id not in "
                "(select id from events order by id desc limit 300)"
            )

    def list_events(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 200))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from events order by id desc limit ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def count_pushes_today(self) -> int:
        today = datetime.now().astimezone().date().isoformat()
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "select count(*) as n from events where kind = 'push' and created_at like ?",
                (today + "%",),
            ).fetchone()
        return int(row["n"])

    def add_log(self, level: str, message: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "insert into logs(created_at, level, message) values(?, ?, ?)",
                (now_iso(), level, message),
            )
            conn.execute(
                "delete from logs where id not in "
                "(select id from logs order by id desc limit 500)"
            )

    def list_logs(self, limit: int = 200) -> list[dict]:
        limit = max(1, min(limit, 500))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from logs order by id desc limit ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def seen_post_ids(self) -> set[int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("select post_id from seen_posts").fetchall()
        return {int(row["post_id"]) for row in rows}

    def mark_posts_seen(self, posts: list[Post]) -> None:
        if not posts:
            return
        seen_at = now_iso()
        with self._lock, self._conn() as conn:
            for post in posts:
                conn.execute(
                    """
                    insert into seen_posts(
                        post_id, title, link, category, author, published_at, excerpt, seen_at
                    ) values(?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(post_id) do update set
                        title = excluded.title,
                        link = excluded.link,
                        category = excluded.category,
                        author = case when excluded.author != '' then excluded.author else seen_posts.author end,
                        published_at = case
                            when excluded.published_at != '' then excluded.published_at
                            else seen_posts.published_at
                        end,
                        excerpt = case
                            when length(excluded.excerpt) > length(seen_posts.excerpt) then excluded.excerpt
                            else seen_posts.excerpt
                        end
                    """,
                    (
                        post.post_id,
                        post.title,
                        post.link,
                        post.category,
                        post.author,
                        post.published_at,
                        post.excerpt,
                        seen_at,
                    ),
                )
            conn.execute(
                "delete from seen_posts where post_id not in "
                "(select post_id from seen_posts order by seen_at desc limit 5000)"
            )

    def baselined_boards(self) -> set[str]:
        raw = self.get_state("posts_boards") or ""
        return {part for part in raw.split(",") if part}

    def remember_boards(self, boards: set[str]) -> None:
        current = self.baselined_boards()
        merged = current | {board for board in boards if board}
        if merged == current:
            return
        self.set_state("posts_boards", ",".join(sorted(merged)))
        self.set_state("posts_baseline_done", "1")

    def reset_post_baseline(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("delete from state where key in ('posts_boards', 'posts_baseline_done')")
            conn.execute(
                "update keyword_hits set status = 'listed' where status = 'pending'"
            )

    def ensure_hit(self, post: Post, matched: list[str] | tuple[str, ...], status: str) -> None:
        keywords = "、".join(matched)
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "select status from keyword_hits where post_id = ?", (post.post_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    update keyword_hits set
                        title = ?, link = ?, category = ?, author = ?,
                        published_at = case when ? != '' then ? else published_at end,
                        excerpt = case when length(?) > length(excerpt) then ? else excerpt end,
                        keywords = ?
                    where post_id = ?
                    """,
                    (
                        post.title,
                        post.link,
                        post.category,
                        post.author,
                        post.published_at,
                        post.published_at,
                        post.excerpt,
                        post.excerpt,
                        keywords,
                        post.post_id,
                    ),
                )
                return
            conn.execute(
                """
                insert into keyword_hits(
                    post_id, title, link, category, author, published_at,
                    excerpt, keywords, status, created_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.post_id,
                    post.title,
                    post.link,
                    post.category,
                    post.author,
                    post.published_at,
                    post.excerpt,
                    keywords,
                    status,
                    now_iso(),
                ),
            )
            conn.execute(
                "delete from keyword_hits where post_id not in "
                "(select post_id from keyword_hits order by created_at desc limit 300)"
            )

    def set_hit_status(self, post_id: int, status: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "update keyword_hits set status = ? where post_id = ?",
                (status, post_id),
            )

    def pending_hits(self) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from keyword_hits where status = 'pending' order by post_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_hits(self, limit: int = 80) -> list[dict]:
        limit = max(1, min(limit, 200))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from keyword_hits order by created_at desc, post_id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def seen_mention_ids(self) -> set[int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("select mention_id from mentions").fetchall()
        return {int(row["mention_id"]) for row in rows}

    def ensure_mention(self, item: Mention, status: str) -> None:
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "select status from mentions where mention_id = ?",
                (item.mention_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    update mentions set
                        commenter_id = ?, commenter_name = ?, post_id = ?, floor_id = ?,
                        title = ?, link = ?, created_at = ?
                    where mention_id = ?
                    """,
                    (
                        item.commenter_id,
                        item.commenter_name,
                        item.post_id,
                        item.floor_id,
                        item.title,
                        item.link,
                        item.created_at,
                        item.mention_id,
                    ),
                )
                return
            conn.execute(
                """
                insert into mentions(
                    mention_id, commenter_id, commenter_name, post_id, floor_id,
                    title, link, created_at, status, seen_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.mention_id,
                    item.commenter_id,
                    item.commenter_name,
                    item.post_id,
                    item.floor_id,
                    item.title,
                    item.link,
                    item.created_at,
                    status,
                    now_iso(),
                ),
            )
            conn.execute(
                "delete from mentions where mention_id not in "
                "(select mention_id from mentions order by mention_id desc limit 300)"
            )

    def set_mention_status(self, mention_id: int, status: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "update mentions set status = ? where mention_id = ?",
                (status, mention_id),
            )

    def pending_mentions(self) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from mentions where status = 'pending' order by mention_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_mentions(self, limit: int = 80) -> list[dict]:
        limit = max(1, min(limit, 200))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from mentions order by mention_id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def seen_reply_ids(self) -> set[int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("select reply_id from replies").fetchall()
        return {int(row["reply_id"]) for row in rows}

    def ensure_reply(self, item: Reply, status: str) -> None:
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "select status from replies where reply_id = ?",
                (item.reply_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    update replies set
                        commenter_id = ?, commenter_name = ?, post_id = ?, floor_id = ?,
                        title = ?, link = ?, created_at = ?
                    where reply_id = ?
                    """,
                    (
                        item.commenter_id,
                        item.commenter_name,
                        item.post_id,
                        item.floor_id,
                        item.title,
                        item.link,
                        item.created_at,
                        item.reply_id,
                    ),
                )
                return
            conn.execute(
                """
                insert into replies(
                    reply_id, commenter_id, commenter_name, post_id, floor_id,
                    title, link, created_at, status, seen_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.reply_id,
                    item.commenter_id,
                    item.commenter_name,
                    item.post_id,
                    item.floor_id,
                    item.title,
                    item.link,
                    item.created_at,
                    status,
                    now_iso(),
                ),
            )
            conn.execute(
                "delete from replies where reply_id not in "
                "(select reply_id from replies order by reply_id desc limit 300)"
            )

    def set_reply_status(self, reply_id: int, status: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "update replies set status = ? where reply_id = ?",
                (status, reply_id),
            )

    def pending_replies(self) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from replies where status = 'pending' order by reply_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_replies(self, limit: int = 80) -> list[dict]:
        limit = max(1, min(limit, 200))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select * from replies order by reply_id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def backfill_keyword_hits(
        self,
        rules: list[KeywordRule],
        excludes: list[str] | None = None,
        global_boards: list[str] | None = None,
    ) -> int:
        if not rules:
            return 0
        added = 0
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "select seen_posts.* from seen_posts "
                "left join keyword_hits on keyword_hits.post_id = seen_posts.post_id "
                "where keyword_hits.post_id is null"
            ).fetchall()
            for row in rows:
                matched = match_rules(
                    Post(
                        post_id=int(row["post_id"]),
                        title=row["title"],
                        link=row["link"],
                        category=row["category"],
                        author=row["author"],
                        published_at=row["published_at"],
                        excerpt=row["excerpt"],
                    ),
                    rules,
                    excludes,
                    global_boards,
                )
                if not matched:
                    continue
                conn.execute(
                    """
                    insert into keyword_hits(
                        post_id, title, link, category, author, published_at,
                        excerpt, keywords, status, created_at
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, 'listed', ?)
                    """,
                    (
                        row["post_id"],
                        row["title"],
                        row["link"],
                        row["category"],
                        row["author"],
                        row["published_at"],
                        row["excerpt"],
                        "、".join(matched),
                        now_iso(),
                    ),
                )
                added += 1
        return added

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            peer_id=row["peer_id"],
            peer_name=row["peer_name"],
            content=row["content"],
            created_at=row["created_at"],
            message_id=row["message_id"],
            sender_id=row["sender_id"],
            receiver_id=row["receiver_id"],
            viewed=row["viewed"],
        )
