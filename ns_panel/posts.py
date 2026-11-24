"""检索 NodeSeek 新帖，并判断哪些关键词命中要推送。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from ns_panel.diff import preview, strip_html


RSS_URL = "https://rss.nodeseek.com/"
DC = {"dc": "http://purl.org/dc/elements/1.1/"}

# 空板块设置时抓这些。DeepFlood 是站外网站，不在这份列表里。
BOARDS: tuple[tuple[str, str], ...] = (
    ("daily", "日常"),
    ("tech", "技术"),
    ("info", "情报"),
    ("review", "测评"),
    ("trade", "交易"),
    ("carpool", "拼车"),
    ("promotion", "推广"),
    ("life", "生活"),
    ("dev", "Dev"),
    ("photo-share", "贴图"),
    ("expose", "曝光"),
    ("meaningless", "无意义"),
    ("sandbox", "沙盒"),
)
BOARD_NAMES = {slug: name for slug, name in BOARDS}
KNOWN_BOARDS = set(BOARD_NAMES)


@dataclass(frozen=True)
class Post:
    post_id: int
    title: str
    link: str
    category: str
    author: str
    published_at: str
    excerpt: str


@dataclass(frozen=True)
class PostPlan:
    post: Post
    matched: tuple[str, ...]
    push: bool


@dataclass(frozen=True)
class KeywordRule:
    word: str
    boards: tuple[str, ...]
    excludes: tuple[str, ...]


def board_name(slug: str) -> str:
    return BOARD_NAMES.get(slug, slug or "未分类")


def parse_keywords(text: str) -> list[str]:
    return [rule.word for rule in parse_keyword_rules(text)]


def parse_keyword_rule(text: str) -> KeywordRule:
    rule = _rule_from_chunk(text or "")
    if rule is None:
        raise ValueError("没有写关键词。例如：家宽 @交易 -已出")
    return rule


def parse_keyword_rules(text: str) -> list[KeywordRule]:
    found: list[KeywordRule] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\n,，;；]+", text or ""):
        rule = _rule_from_chunk(chunk)
        if rule is None:
            continue
        key = rule.word.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(rule)
    return found


def parse_excludes(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\n,，;；]+", text or ""):
        word = chunk.strip().lstrip("-－").strip()
        if not word or word.startswith("@"):
            continue
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(word)
    return found


def format_keyword_rules(rules: list[KeywordRule]) -> str:
    lines = []
    for rule in rules:
        parts = [rule.word]
        parts.extend(f"@{board_name(slug)}" for slug in rule.boards)
        parts.extend(f"-{word}" for word in rule.excludes)
        lines.append(" ".join(parts))
    return "\n".join(lines)


def validate_keywords(text: str) -> str | None:
    try:
        rules = parse_keyword_rules(text)
    except ValueError as exc:
        return str(exc)
    if len(rules) > 40:
        return "关键词最多 40 个。请删掉多余的，再保存。"
    for rule in rules:
        if len(rule.word) > 40:
            return "单个关键词不要超过 40 个字。"
        for word in rule.excludes:
            if len(word) > 40:
                return "单个排除词不要超过 40 个字。"
    return None


def validate_excludes(text: str) -> str | None:
    words = parse_excludes(text)
    if len(words) > 40:
        return "排除词最多 40 个。"
    for word in words:
        if len(word) > 40:
            return "单个排除词不要超过 40 个字。"
    return None


def selected_slugs(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if text in ("", "*"):
        return [slug for slug, _name in BOARDS]
    if text == "-":
        return []
    picked = []
    for part in text.split(","):
        slug = part.strip()
        if slug in KNOWN_BOARDS and slug not in picked:
            picked.append(slug)
    return picked


def normalize_boards(value) -> str:
    """返回要写入设置的板块字符串。空字符串表示全部。"""
    if value in (None, "", "*"):
        return ""
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        raise ValueError("板块格式不对")
    if not value:
        return "-"
    picked = []
    for item in value:
        slug = str(item).strip()
        if slug in KNOWN_BOARDS and slug not in picked:
            picked.append(slug)
    if not picked:
        raise ValueError("没有识别到板块")
    if len(picked) == len(BOARDS):
        return ""
    return ",".join(picked)


def match_keywords(title: str, excerpt: str, keywords: list[str]) -> list[str]:
    hay = f"{title}\n{excerpt}".casefold()
    return [word for word in keywords if word and word.casefold() in hay]


def match_rules(
    post: Post,
    rules: list[KeywordRule],
    excludes: list[str] | None = None,
    global_boards: list[str] | None = None,
) -> list[str]:
    hay = f"{post.title}\n{post.excerpt}".casefold()
    if any(word and word.casefold() in hay for word in excludes or []):
        return []
    found: list[str] = []
    for rule in rules:
        if not rule.word or rule.word.casefold() not in hay:
            continue
        if any(word.casefold() in hay for word in rule.excludes):
            continue
        allowed = rule.boards or tuple(global_boards or [])
        if allowed and post.category not in allowed:
            continue
        found.append(rule.word)
    return found


def union_slugs(selected: list[str], rules: list[KeywordRule]) -> list[str]:
    picked = list(selected)
    for rule in rules:
        for slug in rule.boards:
            if slug not in picked:
                picked.append(slug)
    return picked


def plan_posts(
    posts: list[Post],
    seen_ids: set[int],
    baseline_done: bool,
    keywords: list[str] | list[KeywordRule],
    excludes: list[str] | None = None,
    global_boards: list[str] | None = None,
) -> list[PostPlan]:
    """基线之后，只推送编号比已见帖子更大的命中。被顶上来的旧帖不会推送。"""
    rules = _as_rules(keywords)
    newest_seen = max(seen_ids) if seen_ids else 0
    plans: list[PostPlan] = []
    for post in posts:
        matched = tuple(match_rules(post, rules, excludes, global_boards))
        is_new = post.post_id not in seen_ids and post.post_id > newest_seen
        plans.append(PostPlan(post, matched, bool(matched) and baseline_done and is_new))
    return plans


def parse_rss(xml_text: str) -> list[Post]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("RSS 不是合法的 XML") from exc
    posts: list[Post] = []
    seen: set[int] = set()
    for item in root.findall("./channel/item"):
        title = _xml_text(item.find("title"))
        excerpt = strip_html(_xml_text(item.find("description")))[:800]
        link = _xml_text(item.find("link"))
        guid = _xml_text(item.find("guid"))
        category = _xml_text(item.find("category")).strip()
        author = _xml_text(item.find("dc:creator", DC))
        published = _format_pubdate(_xml_text(item.find("pubDate")))
        post_id = _post_id(guid, link)
        if not post_id or post_id in seen or not title:
            continue
        seen.add(post_id)
        posts.append(
            Post(
                post_id=post_id,
                title=strip_html(title),
                link=post_link(post_id, link),
                category=category,
                author=strip_html(author),
                published_at=published,
                excerpt=excerpt,
            )
        )
    return posts


def parse_category_html(html_text: str, category: str) -> list[Post]:
    start = html_text.find('<ul class="post-list">')
    if start < 0:
        return []
    chunk = html_text[start:]
    end = chunk.find("</ul>")
    if end > 0:
        chunk = chunk[:end]
    posts: list[Post] = []
    seen: set[int] = set()
    for part in re.split(r'<li class="post-list-item[^"]*">', chunk)[1:]:
        found = re.search(r'href="/post-(\d+)-\d+"[^>]*>(.*?)</a>', part, re.S)
        if not found:
            continue
        post_id = int(found.group(1))
        if post_id in seen:
            continue
        seen.add(post_id)
        title = strip_html(found.group(2))
        if not title:
            continue
        author = ""
        author_found = re.search(
            r'class="info-author"[\s\S]*?<a href="/space/\d+">(.*?)</a>', part
        )
        if author_found:
            author = strip_html(author_found.group(1))
        if not author:
            alt = re.search(r"<img[^>]+alt=\"([^\"]*)\"", part)
            author = strip_html(alt.group(1)) if alt else ""
        posts.append(
            Post(
                post_id=post_id,
                title=title,
                link=post_link(post_id),
                category=category,
                author=author,
                published_at="",
                excerpt="",
            )
        )
    return posts


def merge_posts(primary: list[Post], extra: list[Post]) -> list[Post]:
    """用后来的字段补齐摘要和发布时间，同一帖只留一条。"""
    merged: dict[int, Post] = {}
    for post in list(primary) + list(extra):
        old = merged.get(post.post_id)
        if old is None:
            merged[post.post_id] = post
            continue
        merged[post.post_id] = Post(
            post_id=post.post_id,
            title=post.title or old.title,
            link=post.link or old.link,
            category=post.category or old.category,
            author=post.author or old.author,
            published_at=post.published_at or old.published_at,
            excerpt=post.excerpt if len(post.excerpt) >= len(old.excerpt) else old.excerpt,
        )
    return list(merged.values())


def format_post_alerts(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = [f"NodeSeek 关键词帖子 {len(rows)} 篇" if len(rows) > 1 else "NodeSeek 关键词帖子"]
    shown = rows[:8]
    for row in shown:
        lines.extend(
            [
                "",
                f"[{board_name(str(row.get('category') or ''))}] {row.get('title') or '无标题'}",
                f"作者：{row.get('author') or '未知'}",
                f"时间：{row.get('published_at') or '未知'}",
                f"命中：{row.get('keywords') or ''}",
                f"摘要：{preview(row.get('excerpt') or '', 180) or '（列表里没有摘要）'}",
                f"打开：{row.get('link') or post_link(int(row.get('post_id') or 0))}",
            ]
        )
    if len(rows) > len(shown):
        lines.append("")
        lines.append(f"还有 {len(rows) - len(shown)} 篇，请打开本机面板的关键词页查看。")
    text = "\n".join(lines)
    if len(text) > 3900:
        return text[:3899] + "…"
    return text


def fetch_board_posts(cookie: str, boards: list[str]) -> tuple[list[Post], list[str], list[str], list[int]]:
    """只读公开 RSS。板块页面在 Cloudflare 后面，连续打开会把登录态一起拦住。

    第四项是整份公开列表的帖子编号，用来发现整页换新。公开源不按板块翻页。
    """
    del cookie
    if not boards:
        return [], ["没有选择板块"], [], []
    allowed = set(boards)
    try:
        xml_text = _fetch_rss()
        all_posts = parse_rss(xml_text)
        posts = [post for post in all_posts if post.category in allowed]
    except Exception as exc:
        return [], [f"RSS：{exc}"], [], []
    fetched = sorted({post.category for post in posts})
    return posts, [], fetched, [post.post_id for post in all_posts]


def _fetch_rss() -> str:
    from ns_panel.nodeseek import ApiError, CloudflareError, NodeSeekClient, looks_like_challenge

    client = NodeSeekClient("ns=1")
    status, content_type, text = client._request(RSS_URL)
    if looks_like_challenge(status, text, content_type):
        raise CloudflareError("公开 RSS 也被拦住了。请稍后再试。")
    if status >= 400 or not text:
        raise ApiError(f"HTTP {status}")
    return text


def post_link(post_id: int, link: str = "") -> str:
    text = (link or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if post_id:
        return f"https://www.nodeseek.com/post-{post_id}-1"
    return "https://www.nodeseek.com/"


def _post_id(guid: str, link: str) -> int | None:
    for source in (link, guid):
        found = re.search(r"post-(\d+)", source or "")
        if found:
            return int(found.group(1))
    text = (guid or "").strip()
    if text.isdigit() and int(text) > 0:
        return int(text)
    return None


def _xml_text(node) -> str:
    if node is None or node.text is None:
        return ""
    return str(node.text).strip()


def _format_pubdate(raw: str) -> str:
    if not raw:
        return ""
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return raw
    if moment.tzinfo is None:
        return moment.strftime("%Y-%m-%d %H:%M")
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def keyword_signature(rules: list[KeywordRule], excludes: list[str] | None = None) -> str:
    lines = []
    for rule in rules:
        lines.append(
            rule.word.casefold()
            + "@"
            + ",".join(rule.boards)
            + "-"
            + ",".join(word.casefold() for word in rule.excludes)
        )
    lines.append("#" + ",".join(word.casefold() for word in excludes or []))
    return "\n".join(lines)


def _rule_from_chunk(chunk: str) -> KeywordRule | None:
    parts = [part for part in chunk.split() if part]
    if not parts:
        return None
    words: list[str] = []
    boards: list[str] = []
    excludes: list[str] = []
    for part in parts:
        if part[:1] in ("@", "＠") and len(part) > 1:
            boards.append(_board_slug(part[1:]))
            continue
        if part[:1] in ("-", "－") and len(part) > 1:
            excludes.append(part[1:].strip())
            continue
        words.append(part)
    word = " ".join(words).strip()
    if not word:
        raise ValueError("有一行只有板块或排除词，没有关键词。例如：家宽 @交易 -已出")
    picked: list[str] = []
    for slug in boards:
        if slug not in picked:
            picked.append(slug)
    extra: list[str] = []
    seen: set[str] = set()
    for item in excludes:
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        extra.append(item)
    return KeywordRule(word, tuple(picked), tuple(extra))


def _as_rules(keywords: list[str] | list[KeywordRule]) -> list[KeywordRule]:
    if keywords and isinstance(keywords[0], KeywordRule):
        return list(keywords)
    return [KeywordRule(word, (), ()) for word in keywords]


def _board_slug(token: str) -> str:
    text = token.strip()
    folded = text.casefold()
    for slug in KNOWN_BOARDS:
        if slug.casefold() == folded:
            return slug
    for slug, name in BOARDS:
        if name == text or name.casefold() == folded:
            return slug
    raise ValueError(f"没有这个板块：{text}")
