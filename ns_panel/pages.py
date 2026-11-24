"""通知列表翻页。只读 list，不调用标已读。"""

from __future__ import annotations


MAX_CATCHUP_PAGES = 5

_PATHS = {
    "message": "/api/notification/message/list",
    "mention": "/api/notification/at-me/list",
    "reply": "/api/notification/reply-to-me/list",
}


def notification_path(kind: str, page: int = 1) -> str:
    base = _PATHS[kind]
    number = int(page or 1)
    if number <= 1:
        return base
    return f"{base}?page={number}"


def round_pages(resume_page: int, limit: int = MAX_CATCHUP_PAGES) -> list[int]:
    """每一轮最多 limit 页。续翻时仍先看第 1 页，再从上次停下的页接着走。"""
    resume = int(resume_page or 1)
    if resume <= 1:
        return list(range(1, limit + 1))
    pages = [1]
    page = resume
    while len(pages) < limit:
        if page not in pages:
            pages.append(page)
        page += 1
    return pages


def reached_old(ids: tuple[int, ...] | list[int], seen_ids: set[int], sync_until: int, catching_up: bool) -> bool:
    if not ids:
        return True
    if catching_up:
        if sync_until > 0:
            return any(item <= sync_until for item in ids)
        return False
    if not seen_ids:
        return True
    return bool(set(ids) & set(seen_ids)) or max(ids) <= max(seen_ids)


def load_pages(
    fetch,
    parse,
    identity,
    seen_ids: set[int],
    baseline_done: bool,
    resume_page: int = 1,
    sync_until: int = 0,
    limit: int = MAX_CATCHUP_PAGES,
):
    """返回 (记录, 是否接到旧记录, 下一页)。

    第一次建立基线只读第 1 页。补漏没接上时，下一轮从 next_page 继续，每轮仍最多 limit 页。
    """
    if not baseline_done:
        return list(parse(fetch(1))), True, 1

    catching_up = int(resume_page or 1) > 1
    found = []
    seen_row: set[int] = set()
    last_sig: tuple[int, ...] | None = None
    finished = False
    next_page = 2
    fetched = 0
    for page in round_pages(resume_page, limit):
        try:
            rows = list(parse(fetch(page)))
        except Exception:
            if page == 1 and not found:
                raise
            return found, False, page
        ids = tuple(identity(row) for row in rows)
        if fetched and (not ids or ids == last_sig):
            if not ids:
                return found, True, 1
            return found, False, page
        for row, row_id in zip(rows, ids):
            if row_id in seen_row:
                continue
            seen_row.add(row_id)
            found.append(row)
        last_sig = ids
        fetched += 1
        if reached_old(ids, seen_ids, sync_until, catching_up):
            return found, True, 1
        next_page = page + 1
    return found, finished, next_page
