"""在灌水区发用户自己写的短回复。默认关，只走登录浏览器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ns_panel.diff import strip_html
from ns_panel.posts import BOARD_NAMES, KeywordRule, Post, _board_slug, match_rules


WATER_BOARD = "meaningless"
WATER_PATH = "/categories/meaningless"
COMMENT_PATH = "/api/content/new-comment"
MAX_DAILY = 50
DEFAULT_DAILY = 3
MIN_GAP_MINUTES = 10
MAX_GAP_MINUTES = 240
DEFAULT_GAP_MINUTES = 10
RETRY_SECONDS = 1800
MAX_AGE = timedelta(hours=48)
MAX_PHRASE_LENGTH = 80
MAX_PHRASES = 20
MIN_PHRASES = 2
STOP_WORDS = ("上限", "评论过", "太频繁", "重复")


@dataclass(frozen=True)
class WaterTarget:
    post_id: int
    title: str
    author_id: int
    active_at: datetime | None
    read_only: bool = False
    category: str = WATER_BOARD


@dataclass(frozen=True)
class CommentOutcome:
    ok: bool
    stop_today: bool
    message: str


def resolve_water_board(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return WATER_BOARD
    return _board_slug(text)


def water_path(slug: str) -> str:
    return f"/categories/{resolve_water_board(slug)}"


def water_board_name(slug: str) -> str:
    return BOARD_NAMES.get(slug, slug)


def parse_phrases(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        phrase = " ".join(line.strip().split())
        if not phrase:
            continue
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(phrase)
    return found


def validate_water_settings(enabled: bool, phrases: list[str], daily: int, gap: int) -> str | None:
    if daily < 1 or daily > MAX_DAILY:
        return f"每天条数要在 1 到 {MAX_DAILY} 之间"
    if gap < MIN_GAP_MINUTES or gap > MAX_GAP_MINUTES:
        return f"间隔要在 {MIN_GAP_MINUTES} 到 {MAX_GAP_MINUTES} 分钟之间"
    if len(phrases) > MAX_PHRASES:
        return f"短回复最多 {MAX_PHRASES} 句。"
    for phrase in phrases:
        if len(phrase) > MAX_PHRASE_LENGTH:
            return f"每句短回复不要超过 {MAX_PHRASE_LENGTH} 个字。"
    if enabled and len(phrases) < MIN_PHRASES:
        return "至少写两句短回复，才能打开水贴。同一句不连续用。"
    return None


def next_phrase(phrases: list[str], index: int) -> tuple[str, int]:
    if not phrases:
        raise ValueError("没有短回复")
    slot = index % len(phrases)
    return phrases[slot], (slot + 1) % len(phrases)


def describe_comment(payload) -> CommentOutcome:
    if not isinstance(payload, dict):
        return CommentOutcome(False, False, "评论没有返回结果")
    message = str(payload.get("message") or payload.get("msg") or "").strip()
    success = payload.get("success")
    ok = success is True or success == 1 or str(success).lower() in ("true", "1")
    stop = any(word in message for word in STOP_WORDS)
    if ok:
        return CommentOutcome(True, stop, message or "评论成功")
    if stop:
        return CommentOutcome(False, True, message or "今天先停")
    return CommentOutcome(False, False, message or "评论没有成功")


def parse_water_board(html_text: str, category: str = WATER_BOARD) -> list[WaterTarget]:
    start = (html_text or "").find('<ul class="post-list">')
    if start < 0:
        return []
    chunk = html_text[start:]
    end = chunk.find("</ul>")
    if end > 0:
        chunk = chunk[:end]
    found: list[WaterTarget] = []
    seen: set[int] = set()
    for part in re.split(r'<li class="post-list-item[^"]*">', chunk)[1:]:
        link = re.search(r'href="/post-(\d+)-\d+"[^>]*>(.*?)</a>', part, re.S)
        if not link:
            continue
        post_id = int(link.group(1))
        if post_id in seen:
            continue
        seen.add(post_id)
        title = strip_html(link.group(2))
        if not title:
            continue
        author = re.search(r'data-uid="(\d+)"', part) or re.search(r'href="/space/(\d+)"', part)
        author_id = int(author.group(1)) if author else 0
        stamp = re.search(r'datetime="([^"]+)"', part)
        item_category = category
        category_link = re.search(r'href="/categories/([^"]+)"', part)
        if category_link:
            item_category = category_link.group(1)
        found.append(
            WaterTarget(
                post_id=post_id,
                title=title,
                author_id=author_id,
                active_at=_parse_time(stamp.group(1) if stamp else ""),
                read_only="read-only" in part,
                category=item_category,
            )
        )
    return found


def pick_water_target(
    targets: list[WaterTarget],
    *,
    self_id: int,
    watered: set[int],
    now: datetime,
    rules: list[KeywordRule],
    excludes: list[str],
    boards: list[str],
    board: str = WATER_BOARD,
) -> WaterTarget | None:
    cutoff = _stamp(now) - MAX_AGE.total_seconds()
    for target in targets:
        if target.post_id in watered or target.read_only:
            continue
        if target.author_id and target.author_id == self_id:
            continue
        if target.category != board:
            continue
        active = _stamp(target.active_at) if target.active_at else None
        if active is None or active < cutoff:
            continue
        post = Post(
            post_id=target.post_id,
            title=target.title,
            link="",
            category=target.category,
            author="",
            published_at="",
            excerpt="",
        )
        if match_rules(post, rules, excludes, boards):
            continue
        return target
    return None


def water_status_line(store, now: datetime | None = None) -> str:
    if (store.get_setting("water_enabled") or "0") != "1":
        return "关着"
    today = (now or datetime.now()).date().isoformat()
    if store.get_state("water_day") != today:
        return "今天还没有"
    count = store.get_state("water_count") or "0"
    result = (store.get_state("water_result") or "").replace("\n", " ")
    parts = [f"成功 {count} 条"]
    if result:
        parts.append(result)
    if store.get_state("water_stopped") != "1":
        error = (store.get_state("water_last_error") or "").strip()
        if error:
            parts.append(error)
    return "。".join(parts)


def _parse_time(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _stamp(moment: datetime | None) -> float:
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()
