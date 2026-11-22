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


def load_pages(fetch, parse, identity, seen_ids: set[int], baseline_done: bool, limit: int = MAX_CATCHUP_PAGES):
    """按页往后补，直到接到已见过的编号、空页、重复页，或到达页数上限。

    第一次建立基线只读第 1 页，避免把更早的历史收进来再推送。
    """
    found = []
    seen_row: set[int] = set()
    last_sig: tuple[int, ...] | None = None
    capped = False
    for page in range(1, limit + 1):
        try:
            rows = parse(fetch(page))
        except Exception:
            if page == 1:
                raise
            break
        ids = tuple(identity(row) for row in rows)
        if page > 1 and (not ids or ids == last_sig):
            break
        for row, row_id in zip(rows, ids):
            if row_id in seen_row:
                continue
            seen_row.add(row_id)
            found.append(row)
        last_sig = ids
        if not ids or not baseline_done or not seen_ids:
            break
        if set(ids) & set(seen_ids) or max(ids) <= max(seen_ids):
            break
        if page == limit:
            capped = True
    return found, capped
