"""定时拉取私信和新帖，有新内容时推到 Telegram。"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from ns_panel.botview import (
    FOREIGN_TEXT,
    HELLO,
    answer_for,
    open_markup,
    pm_open_links,
    keyboard_seen,
    reply_keyboard,
    titled_open_links,
)
from ns_panel.browser import PORT, BrowserClient, BrowserDown, auth_mode, port_open, shared_session, _wake_target
from ns_panel.diff import (
    format_pushes,
    is_newer,
    parse_message_list,
    select_notifications,
    self_name_from_rows,
)
from ns_panel.mentions import format_mention_alerts, parse_mentions, plan_mentions
from ns_panel.nodeseek import (
    ApiError,
    AuthError,
    CloudflareError,
    NodeSeekClient,
    describe_checkin,
    message_rows,
)
from ns_panel.pages import load_pages, round_pages
from ns_panel.posts import (
    fetch_board_posts,
    format_post_alerts,
    keyword_signature,
    parse_excludes,
    parse_keyword_rules,
    plan_posts,
    selected_slugs,
    union_slugs,
)
from ns_panel.replies import format_reply_alerts, parse_replies, plan_replies
from ns_panel.schedule import heartbeat_due, in_quiet_hours
from ns_panel.store import Store
from ns_panel.telegram import TelegramError, get_updates, send_message
from ns_panel.water import (
    RETRY_SECONDS as WATER_RETRY_SECONDS,
    describe_comment,
    next_phrase,
    parse_phrases,
    pick_water_target,
)


MIN_INTERVAL = 10
MIN_KEYWORD_INTERVAL = 30
MAX_INTERVAL = 3600
RSS_GAP_SECONDS = 6 * 3600
RSS_PULL_OVERLAP = 4
RSS_BACKOFF_SECONDS = 300
RSS_BACKOFF_MAX = 600
RSS_WAIT = "没能拉到帖子：公开 RSS 暂时连不上，过几分钟再试。"
SUPERVISE_SECONDS = 15
BROWSER_RETRY_SECONDS = 20
BROWSER_ALERT_SECONDS = 180
AWAKE_MISSES = 9
CHECK_MARK = "/tmp/ns-checking"


def _water_daily(store: Store) -> int:
    try:
        value = int(store.get_setting("water_daily") or "3")
    except ValueError:
        value = 3
    return max(1, min(value, 5))


def _water_gap_minutes(store: Store) -> int:
    try:
        value = int(store.get_setting("water_gap") or "10")
    except ValueError:
        value = 10
    return max(10, min(value, 240))


def _bounded_interval(raw: str | None, default: int, minimum: int) -> int:
    try:
        value = int(raw or "")
    except ValueError:
        value = default
    return max(minimum, min(value, MAX_INTERVAL))


def interval_seconds(store: Store) -> int:
    return _bounded_interval(store.get_setting("interval", "90"), 90, MIN_INTERVAL)


def keyword_interval_seconds(store: Store) -> int:
    return _bounded_interval(store.get_setting("keyword_interval", "60"), 60, MIN_KEYWORD_INTERVAL)


class Monitor:
    def __init__(
        self,
        store: Store,
        client_factory=NodeSeekClient,
        sender=send_message,
        feed_fetcher=None,
        browser_client_factory=BrowserClient,
        clock=None,
    ):
        self.store = store
        self.client_factory = client_factory
        self.sender = sender
        self.feed_fetcher = feed_fetcher or fetch_board_posts
        self.browser_client_factory = browser_client_factory
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._check_lock = threading.Lock()
        self._life = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._supervisor: threading.Thread | None = None
        self._listener: threading.Thread | None = None
        self._listen_stop = threading.Event()
        self._pm_catchup_done = True
        self.next_check_at: float | None = None
        self.next_keyword_at: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._life:
            self._start_locked()

    def ensure_started(self) -> None:
        with self._life:
            if self.store.get_setting("enabled") != "1":
                return
            if self.running:
                return
            self.log("info", "监控线程停了，正在重新启动。")
            self._start_locked()

    def supervise(self) -> None:
        with self._life:
            if self._supervisor is not None and self._supervisor.is_alive():
                return
            self._supervisor = threading.Thread(
                target=self._supervise_loop, name="nodeseek-supervise", daemon=True
            )
            self._supervisor.start()

    def _start_locked(self) -> None:
        if self.running:
            return
        self._generation += 1
        generation = self._generation
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, args=(generation,), name="nodeseek-poll", daemon=True
        )
        self._thread.start()
        self._ensure_awake()

    def listen(self) -> None:
        with self._life:
            if self._listener is not None and self._listener.is_alive():
                return
            self._listen_stop = threading.Event()
            self._listener = threading.Thread(
                target=self._listen_loop, name="nodeseek-telegram", daemon=True
            )
            self._listener.start()

    def stop(self) -> None:
        with self._life:
            self._stop.set()
            self._listen_stop.set()

    def check_once(self, wait: bool = False) -> dict:
        with self._check_hold(None if wait else 0.2) as held:
            if not held:
                return {"ok": False, "error": "上一次检查还在进行"}
            try:
                return self._check()
            except Exception as exc:
                message = f"检查失败：{exc}"
                self.store.set_state("last_error", message)
                self.store.set_state("last_check", _now())
                self.log("error", message)
                return {"ok": False, "error": message}

    def scan_posts(self, reset: bool = False) -> dict:
        with self._check_hold(0.2) as held:
            if not held:
                return {"ok": False, "error": "上一次检查还在进行"}
            return self._scan_posts_held(reset)

    def _scan_posts_held(self, reset: bool) -> dict:
        try:
            if reset:
                self.store.reset_post_baseline()
                self.log("info", "已清除帖子基线。这次检索不会推送当前帖子。")
            rules, excludes = self._keyword_rules()
            if not rules:
                return {"ok": False, "error": "还没有填写关键词"}
            cookie = (self.store.get_setting("cookie") or "").strip()
            return self._check_posts(cookie, rules, excludes)
        except Exception as exc:
            message = f"关键词检查失败：{exc}"
            self._note_post_error(message)
            return {"ok": False, "error": message, "notified": 0}

    def log(self, level: str, message: str) -> None:
        self.store.add_log(level, message)
        print(f"[{level}] {message}", flush=True)

    def _supervise_loop(self) -> None:
        while True:
            time.sleep(SUPERVISE_SECONDS)
            try:
                self.ensure_started()
            except Exception as exc:
                print(f"[error] 监控线程没有重新启动：{exc}", flush=True)

    def _loop(self, generation: int) -> None:
        self.log("info", "监控已启动")
        next_inbox = 0.0
        next_keyword = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now >= next_inbox:
                    result = self._safe_call(self._check_inbox)
                    if not result.get("busy"):
                        delay = interval_seconds(self.store)
                        retry_in = result.get("retry_in")
                        if isinstance(retry_in, int) and 0 < retry_in < delay:
                            delay = retry_in
                        next_inbox = time.time() + delay
                if now >= next_keyword:
                    result = self._safe_call(self._scheduled_keywords)
                    if not result.get("busy"):
                        delay = keyword_interval_seconds(self.store)
                        if result.get("pull_in"):
                            delay = MIN_KEYWORD_INTERVAL
                        next_keyword = time.time() + delay
                self._maybe_heartbeat()
                self._maybe_checkin()
                self._maybe_water()
                self.next_check_at = next_inbox
                self.next_keyword_at = next_keyword
                wait = min(next_inbox, next_keyword) - time.time()
                if self._stop.wait(0.2 if wait < 0.2 else wait):
                    break
        except Exception as exc:
            self.log("error", f"监控线程退出：{exc}")
        finally:
            if generation != self._generation:
                return
            self.next_check_at = None
            self.next_keyword_at = None
            if self._stop.is_set():
                self.log("info", "监控已停止")
            else:
                self.log("error", "监控线程意外退出")

    def _safe_call(self, fn) -> dict:
        with self._check_hold(0.2) as held:
            if not held:
                return {"ok": False, "busy": True, "error": "上一次检查还在进行"}
            try:
                return fn()
            except Exception as exc:
                message = f"检查失败：{exc}"
                self.store.set_state("last_error", message)
                self.store.set_state("last_check", _now())
                self.log("error", message)
                return {"ok": False, "error": message}

    @contextmanager
    def _check_hold(self, timeout: float | None):
        if timeout is None:
            acquired = self._check_lock.acquire()
        elif timeout == 0:
            acquired = self._check_lock.acquire(blocking=False)
        else:
            acquired = self._check_lock.acquire(timeout=timeout)
        if not acquired:
            yield False
            return
        self._mark_checking(True)
        try:
            yield True
        finally:
            self._mark_checking(False)
            self._check_lock.release()

    def _mark_checking(self, active: bool) -> None:
        if os.environ.get("NS_RUNTIME") != "docker":
            return
        try:
            if active:
                with open(CHECK_MARK, "w", encoding="utf-8") as handle:
                    handle.write(str(int(time.time())))
            elif os.path.exists(CHECK_MARK):
                os.remove(CHECK_MARK)
        except OSError:
            pass

    def _check(self) -> dict:
        self.store.set_state("last_check", _now())
        rules, excludes = self._keyword_rules()
        cookie = (self.store.get_setting("cookie") or "").strip()
        use_browser = auth_mode(self.store) == "browser"
        if not cookie and not rules and not use_browser:
            message = "还没有填写 NodeSeek Cookie"
            self.store.set_state("last_error", message)
            return {"ok": False, "error": message}
        if not cookie and not use_browser:
            post = self._check_posts("", rules, excludes)
            if post.get("ok"):
                self.store.set_state("last_error", "")
                self.store.set_state("last_success", _now())
            return post
        inbox = self._check_inbox()
        if not rules:
            return inbox
        try:
            post = self._check_posts(cookie, rules, excludes)
        except Exception as exc:
            post = {"ok": False, "error": f"关键词检查失败：{exc}", "notified": 0}
            self._note_post_error(post["error"])
        return _combine(inbox, post)

    def _check_inbox(self) -> dict:
        self.store.set_state("last_check", _now())
        cookie = (self.store.get_setting("cookie") or "").strip()
        use_browser = auth_mode(self.store) == "browser"
        if not cookie and not use_browser:
            return {"ok": True, "skipped": True, "notified": 0, "message": "未登录，跳过私信"}
        cooled = self._cooling_message()
        if cooled:
            return {"ok": False, "error": cooled}
        pm = self._check_pm(cookie)
        if pm.get("ok"):
            pm = self._with_mentions(cookie, pm)
            pm = self._with_replies(cookie, pm)
        return pm

    def _scheduled_keywords(self) -> dict:
        cooled = self._rss_cooling_message()
        if cooled:
            return {"ok": False, "error": cooled, "notified": 0}
        rules, excludes = self._keyword_rules()
        if not rules:
            return {"ok": True, "skipped": True, "notified": 0}
        cookie = (self.store.get_setting("cookie") or "").strip()
        use_browser = auth_mode(self.store) == "browser"
        post = self._check_posts("" if not cookie and not use_browser else cookie, rules, excludes)
        if not cookie and not use_browser and post.get("ok"):
            self.store.set_state("last_error", "")
            self.store.set_state("last_success", _now())
        return post

    def _check_pm(self, cookie: str) -> dict:
        try:
            client = self._make_client(cookie)
            first = client.get_message_list(page=1)
            rows = message_rows(first)
            account = self._account_if_needed(client)
        except BrowserDown as exc:
            return self._note_browser_down(str(exc))
        except AuthError as exc:
            return self._fail(str(exc), alert=True)
        except CloudflareError as exc:
            self._schedule_backoff()
            return self._fail(str(exc), alert=True)
        except (ApiError, ValueError) as exc:
            return self._fail(str(exc), alert=False)

        self_id = account.member_id if account else _optional_int(self.store.get_state("self_id"))
        if not self_id:
            self_id = client.resolve_self_id(first)
        if not self_id:
            return self._fail("拉到了私信列表，但没能识别当前账号。请更新 Cookie 后再试。", alert=False)

        self_name = account.member_name if account and account.member_name else ""
        if not self_name:
            self_name = self_name_from_rows(rows, self_id)
        self.store.set_state("self_id", str(self_id))
        if self_name:
            self.store.set_state("self_name", self_name)

        previous = self.store.conversations()
        baseline_done = self.store.get_state("baseline_done") == "1"
        watermark = self.store.get_state("pm_watermark") or ""
        if baseline_done and not watermark and previous:
            watermark = _message_watermark(previous.values())
            if watermark:
                self.store.set_state("pm_watermark", watermark)
        caught, self._pm_catchup_done = self._catch_up_messages(
            client, first, self_id, previous, baseline_done, watermark
        )
        if not self._pm_catchup_done:
            self._note_catchup_cap("私信")
        eligible = []
        silent = []
        for conv, page in caught:
            if _silent_history(conv, page, previous, watermark, baseline_done):
                silent.append(conv)
            else:
                eligible.append(conv)
        notify, upserts, created_baseline = select_notifications(
            previous, eligible, self_id, baseline_done
        )
        upserts.extend(silent)

        if created_baseline:
            for item in upserts:
                self.store.upsert_conversation(item)
            self._raise_watermark(upserts)
            self.store.set_state("baseline_done", "1")
            self._recover()
            text = f"已建立基线，记住 {len(upserts)} 个会话。这些历史私信不会推送。"
            self.store.add_event("info", "已建立基线", text)
            self.log("info", text)
            return {"ok": True, "notified": 0, "message": text, "baseline": True}

        if notify:
            text = format_pushes(notify)
            try:
                self._send(text, open_markup(pm_open_links(notify)))
            except TelegramError as exc:
                message = f"私信已拉到，但 Telegram 发送失败：{exc}"
                self.store.set_state("last_error", message)
                self.log("error", message)
                for item in upserts:
                    self.store.upsert_conversation(item)
                self._raise_watermark(upserts)
                return {"ok": False, "error": message, "notified": 0}
            for item in notify:
                self.store.upsert_conversation(item)
            self._raise_watermark(list(notify) + list(upserts))
            title = notify[0].peer_name if len(notify) == 1 else f"{len(notify)} 条新私信"
            self.store.add_event("push", title, text)
            self.log("info", f"已推送 {len(notify)} 条新私信")

        for item in upserts:
            self.store.upsert_conversation(item)
        if not notify:
            self._raise_watermark(upserts)

        self._recover()
        if notify:
            message = f"已推送 {len(notify)} 条新私信"
        else:
            message = "检查完成，没有新私信"
        return {"ok": True, "notified": len(notify), "message": message, "baseline": False}

    def scan_mentions(self, reset: bool = False) -> dict:
        with self._check_hold(0.2) as held:
            if not held:
                return {"ok": False, "error": "上一次检查还在进行"}
            return self._scan_mentions_held(reset)

    def _scan_mentions_held(self, reset: bool) -> dict:
        try:
            if reset:
                self.store.delete_state("mentions_baseline_done")
                self.log("info", "已清除@基线。这次检查不会推送当前提到。")
            cookie = (self.store.get_setting("cookie") or "").strip()
            use_browser = auth_mode(self.store) == "browser"
            if not cookie and not use_browser:
                return {"ok": False, "error": "还没有登录 NodeSeek"}
            return self._check_mentions(cookie)
        except Exception as exc:
            message = f"@检查失败：{exc}"
            self._note_mention_error(message)
            return {"ok": False, "error": message, "notified": 0}

    def _with_mentions(self, cookie: str, pm: dict) -> dict:
        try:
            extra = self._check_mentions(cookie)
        except Exception as exc:
            message = f"@检查失败：{exc}"
            self._note_mention_error(message)
            extra = {"ok": False, "error": message, "notified": 0}
        merged = dict(pm)
        if extra.get("error"):
            merged["mentions_error"] = extra["error"]
        message = extra.get("message") or ""
        if message and (extra.get("notified") or extra.get("baseline")):
            merged["message"] = f"{merged.get('message') or ''} {message}".strip()
            merged["mentions_notified"] = extra.get("notified", 0)
        return merged

    def _check_mentions(self, cookie: str) -> dict:
        client = self._make_client(cookie)
        baseline_done = self.store.get_state("mentions_baseline_done") == "1"
        since = _optional_int(self.store.get_state("mentions_sync_until")) or 0
        rows = self._load_notice(
            "mentions",
            lambda page: client.get_mentions(page=page),
            parse_mentions,
            lambda row: row.mention_id,
            self.store.seen_mention_ids() if baseline_done else set(),
            baseline_done,
            "@",
        )
        self_id = _optional_int(self.store.get_state("self_id"))
        if self_id:
            rows = [row for row in rows if row.commenter_id != self_id]
        plans = plan_mentions(rows, self.store.seen_mention_ids(), baseline_done, since)
        for item, push in plans:
            self.store.ensure_mention(item, "pending" if push else "listed")
        self.store.set_state("mentions_last_error", "")
        if not baseline_done:
            self.store.set_state("mentions_baseline_done", "1")
            if not rows:
                return {"ok": True, "notified": 0}
            text = f"已记住当前 {len(rows)} 条@。这些历史提到不会推送。"
            self.store.add_event("info", "已建立@基线", text)
            self.log("info", text)
            return {"ok": True, "notified": 0, "message": text, "baseline": True}

        pending = self.store.pending_mentions()
        if not pending:
            return {"ok": True, "notified": 0, "message": "检查完成，没有新的@"}
        text = format_mention_alerts(pending)
        try:
            self._send(text, open_markup(titled_open_links(pending, "commenter_name")))
        except TelegramError as exc:
            message = f"@已找到，但 Telegram 发送失败：{exc}"
            self._note_mention_error(message)
            return {"ok": False, "error": message, "notified": 0}
        for row in pending:
            self.store.set_mention_status(int(row["mention_id"]), "sent")
        title = str(pending[0].get("commenter_name") or "有人") if len(pending) == 1 else f"{len(pending)} 条@"
        self.store.add_event("push", f"{title} @了你", text)
        self.log("info", f"已推送 {len(pending)} 条@")
        return {"ok": True, "notified": len(pending), "message": f"已推送 {len(pending)} 条@"}

    def scan_replies(self, reset: bool = False) -> dict:
        with self._check_hold(0.2) as held:
            if not held:
                return {"ok": False, "error": "上一次检查还在进行"}
            return self._scan_replies_held(reset)

    def _scan_replies_held(self, reset: bool) -> dict:
        try:
            if reset:
                self.store.delete_state("replies_baseline_done")
                self.log("info", "已清除回复基线。这次检查不会推送当前回复。")
            cookie = (self.store.get_setting("cookie") or "").strip()
            use_browser = auth_mode(self.store) == "browser"
            if not cookie and not use_browser:
                return {"ok": False, "error": "还没有登录 NodeSeek"}
            return self._check_replies(cookie)
        except Exception as exc:
            message = f"回复检查失败：{exc}"
            self._note_reply_error(message)
            return {"ok": False, "error": message, "notified": 0}

    def _with_replies(self, cookie: str, pm: dict) -> dict:
        try:
            extra = self._check_replies(cookie)
        except Exception as exc:
            message = f"回复检查失败：{exc}"
            self._note_reply_error(message)
            extra = {"ok": False, "error": message, "notified": 0}
        merged = dict(pm)
        if extra.get("error"):
            merged["replies_error"] = extra["error"]
        message = extra.get("message") or ""
        if message and (extra.get("notified") or extra.get("baseline")):
            merged["message"] = f"{merged.get('message') or ''} {message}".strip()
            merged["replies_notified"] = extra.get("notified", 0)
        return merged

    def _check_replies(self, cookie: str) -> dict:
        client = self._make_client(cookie)
        baseline_done = self.store.get_state("replies_baseline_done") == "1"
        since = _optional_int(self.store.get_state("replies_sync_until")) or 0
        rows = self._load_notice(
            "replies",
            lambda page: client.get_replies(page=page),
            parse_replies,
            lambda row: row.reply_id,
            self.store.seen_reply_ids() if baseline_done else set(),
            baseline_done,
            "回复",
        )
        self_id = _optional_int(self.store.get_state("self_id"))
        if self_id:
            rows = [row for row in rows if row.commenter_id != self_id]
        plans = plan_replies(rows, self.store.seen_reply_ids(), baseline_done, since)
        for item, push in plans:
            self.store.ensure_reply(item, "pending" if push else "listed")
        self.store.set_state("replies_last_error", "")
        if not baseline_done:
            self.store.set_state("replies_baseline_done", "1")
            if not rows:
                return {"ok": True, "notified": 0}
            text = f"已记住当前 {len(rows)} 条回复。这些历史回复不会推送。"
            self.store.add_event("info", "已建立回复基线", text)
            self.log("info", text)
            return {"ok": True, "notified": 0, "message": text, "baseline": True}

        pending = self.store.pending_replies()
        if not pending:
            return {"ok": True, "notified": 0, "message": "检查完成，没有新的回复"}
        text = format_reply_alerts(pending)
        try:
            self._send(text, open_markup(titled_open_links(pending, "commenter_name")))
        except TelegramError as exc:
            message = f"回复已找到，但 Telegram 发送失败：{exc}"
            self._note_reply_error(message)
            return {"ok": False, "error": message, "notified": 0}
        for row in pending:
            self.store.set_reply_status(int(row["reply_id"]), "sent")
        title = str(pending[0].get("commenter_name") or "有人") if len(pending) == 1 else f"{len(pending)} 条回复"
        self.store.add_event("push", f"{title} 回复了你", text)
        self.log("info", f"已推送 {len(pending)} 条回复")
        return {"ok": True, "notified": len(pending), "message": f"已推送 {len(pending)} 条回复"}

    def _note_reply_error(self, message: str) -> None:
        if self.store.get_state("replies_last_error") == message:
            return
        self.store.set_state("replies_last_error", message)
        self.log("error", message)

    def _note_mention_error(self, message: str) -> None:
        if self.store.get_state("mentions_last_error") == message:
            return
        self.store.set_state("mentions_last_error", message)
        self.log("error", message)

    def _check_posts(self, cookie: str, rules: list, excludes: list[str]) -> dict:
        boards = selected_slugs(self.store.get_setting("boards"))
        scan = union_slugs(boards, rules)
        if not scan:
            message = "还没有选择要监控的板块"
            self._note_post_error(message)
            return {"ok": False, "error": message, "notified": 0}
        fetched_result = self.feed_fetcher(cookie, scan)
        posts, errors, fetched = fetched_result[0], fetched_result[1], fetched_result[2]
        window_ids = fetched_result[3] if len(fetched_result) > 3 else None
        allowed = set(scan)
        posts = [post for post in posts if post.category in allowed]
        if not posts and not fetched:
            message = "没能拉到帖子：" + ("；".join(errors[:3]) or "没有返回内容")
            if _rss_connect_failure(message):
                self._schedule_rss_backoff()
            message = self._note_post_error(message)
            return {"ok": False, "error": message, "notified": 0}
        pull_in = False
        if window_ids is not None:
            pull_in = self._note_rss_window(window_ids)
        if errors:
            self._note_post_error("部分板块没拉到：" + "；".join(errors[:3]))
        else:
            self.store.set_state("posts_last_error", "")
            self.store.delete_state("posts_error_kind")
            self.store.delete_state("rss_until")
            self.store.set_state("rss_streak", "0")
            self.store.set_state("posts_last_success", _now())

        baseline_done = self.store.get_state("posts_baseline_done") == "1"
        plans = plan_posts(posts, self.store.seen_post_ids(), baseline_done, rules, excludes, boards)
        self.store.mark_posts_seen([item.post for item in plans])
        listed = 0
        for item in plans:
            if not item.matched:
                continue
            status = "pending" if item.push else "listed"
            self.store.ensure_hit(item.post, item.matched, status)
            if not item.push:
                listed += 1
        self.store.remember_boards(set(fetched))
        signature = keyword_signature(rules, excludes)
        if self.store.get_state("keywords_sig") != signature:
            self.store.backfill_keyword_hits(rules, excludes, boards)
            self.store.set_state("keywords_sig", signature)
        if not baseline_done:
            self.store.set_state("posts_baseline_done", "1")
            hit_count = len(self.store.list_hits(200))
            text = f"已记住当前帖子。这些历史帖不会推送。当前命中 {hit_count} 条，列在关键词页。"
            self.store.add_event("info", "已建立帖子基线", text)
            self.log("info", text)
            return {
                "ok": True,
                "notified": 0,
                "message": text,
                "baseline": True,
                "posts_baseline": True,
                "pull_in": pull_in,
            }

        pending = self.store.pending_hits()
        if pending and self._keywords_quiet():
            message = f"关键词命中 {len(pending)} 篇。现在是免打扰时段，先记下，结束后再推。"
            if self.store.get_state("quiet_hold") != message:
                self.store.set_state("quiet_hold", message)
                self.log("info", message)
            return {"ok": True, "notified": 0, "message": message, "held": len(pending), "pull_in": pull_in}
        self.store.delete_state("quiet_hold")
        notified = 0
        if pending:
            text = format_post_alerts(pending)
            try:
                self._send(text, open_markup(titled_open_links(pending, "title")))
            except TelegramError as exc:
                message = f"关键词帖子已找到，但 Telegram 发送失败：{exc}"
                self._note_post_error(message)
                return {"ok": False, "error": message, "notified": 0, "listed": listed}
            for row in pending:
                self.store.set_hit_status(int(row["post_id"]), "sent")
            notified = len(pending)
            title = str(pending[0].get("title") or "关键词帖子") if len(pending) == 1 else f"{len(pending)} 篇关键词帖子"
            self.store.add_event("push", title, text)
            self.log("info", f"已推送 {len(pending)} 篇关键词帖子")

        if notified:
            message = f"已推送 {notified} 篇关键词帖子"
        else:
            message = "检索完成，没有新的关键词帖子"
        return {
            "ok": True,
            "notified": notified,
            "message": message,
            "baseline": False,
            "listed": listed,
            "pull_in": pull_in,
        }

    def _catch_up_messages(
        self, client, first: dict, self_id: int, previous: dict, baseline_done: bool, watermark: str
    ):
        del previous
        first_rows = [(conv, 1) for conv in parse_message_list(first, self_id)]
        if not baseline_done:
            self.store.delete_state("pm_resume_page")
            return first_rows, True
        if not watermark:
            self.store.delete_state("pm_resume_page")
            return first_rows, True
        resume = _optional_int(self.store.get_state("pm_resume_page")) or 1
        pages = [(first, 1)]
        finished = _pm_page_old(parse_message_list(first, self_id), watermark)
        next_page = 1 if finished else 2
        if not finished:
            last_sig = _peer_signature(first, self_id)
            planned = [page for page in round_pages(resume, 5) if page > 1]
            for page in planned:
                try:
                    payload = client.get_message_list(page=page)
                except (AuthError, CloudflareError, ApiError, ValueError):
                    finished = False
                    next_page = page
                    break
                signature = _peer_signature(payload, self_id)
                if not signature:
                    finished = True
                    next_page = 1
                    break
                if signature == last_sig:
                    finished = False
                    next_page = page
                    break
                pages.append((payload, page))
                last_sig = signature
                if _pm_page_old(parse_message_list(payload, self_id), watermark):
                    finished = True
                    next_page = 1
                    break
                next_page = page + 1
            else:
                if planned:
                    finished = False
        if finished:
            self.store.delete_state("pm_resume_page")
        else:
            self.store.set_state("pm_resume_page", str(max(next_page, 2)))
        best: dict[int, tuple] = {}
        for payload, page in pages:
            for conv in parse_message_list(payload, self_id):
                old = best.get(conv.peer_id)
                if old is None or is_newer(old[0], conv):
                    best[conv.peer_id] = (conv, page)
                elif not is_newer(conv, old[0]) and page < old[1]:
                    best[conv.peer_id] = (conv, page)
        return list(best.values()), finished

    def _load_notice(self, kind: str, fetch, parse, identity, seen_ids: set[int], baseline_done: bool, label: str):
        resume_key = f"{kind}_resume_page"
        until_key = f"{kind}_sync_until"
        resume = _optional_int(self.store.get_state(resume_key)) or 1
        sync_until = _optional_int(self.store.get_state(until_key)) or 0
        if not baseline_done:
            resume = 1
            sync_until = 0
        rows, finished, next_page = load_pages(
            fetch,
            parse,
            identity,
            seen_ids,
            baseline_done,
            resume_page=resume,
            sync_until=sync_until,
        )
        if not baseline_done or finished:
            self.store.delete_state(resume_key)
            self.store.delete_state(until_key)
        else:
            if not self.store.get_state(until_key):
                self.store.set_state(until_key, str(max(seen_ids) if seen_ids else 0))
            self.store.set_state(resume_key, str(max(next_page, 2)))
            self._note_catchup_cap(label)
        return rows

    def _raise_watermark(self, items) -> None:
        if not self._pm_catchup_done:
            return
        mark = _message_watermark(items)
        current = self.store.get_state("pm_watermark") or ""
        if mark and mark > current:
            self.store.set_state("pm_watermark", mark)

    def _note_catchup_cap(self, kind: str) -> None:
        message = f"{kind}列表这一轮翻了 5 页，还没接到上次的记录。下一轮会接着翻。"
        if self.store.get_state("catchup_cap") == message:
            return
        self.store.set_state("catchup_cap", message)
        self.log("error", message)

    def _note_rss_window(self, window_ids) -> bool:
        current = []
        for item in window_ids:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number > 0:
                current.append(number)
        if not current:
            return False
        raw = self.store.get_state("rss_window_ids") or ""
        previous = {int(part) for part in raw.split(",") if part.strip().isdigit()}
        self.store.set_state("rss_window_ids", ",".join(str(item) for item in current))
        if not previous:
            return False
        overlap = previous & set(current)
        if not overlap:
            self._alert_rss_gap()
        return len(overlap) <= RSS_PULL_OVERLAP

    def _alert_rss_gap(self) -> None:
        now = time.time()
        try:
            last = float(self.store.get_state("rss_gap_alert_at") or "0")
        except ValueError:
            last = 0.0
        if now - last < RSS_GAP_SECONDS:
            return
        self.store.set_state("rss_gap_alert_at", str(now))
        message = (
            "公开帖子列表整页换新了，中间的帖子可能没被看到。"
            "关键词间隔可以调到 30 秒。公开源每次只有最新约 20 篇，不能按板块往下翻。"
        )
        self.log("error", message)
        try:
            self._send("NodeSeek 关键词监控\n" + message)
        except TelegramError as exc:
            self.log("error", f"关键词漏报提醒没有发出去：{exc}")
            return
        self.store.add_event("alert", "关键词可能漏了", message)

    def _make_client(self, cookie: str):
        if auth_mode(self.store) == "browser":
            return self.browser_client_factory()
        if not cookie:
            raise AuthError("还没有填写 NodeSeek Cookie")
        return self.client_factory(cookie)

    def _account_if_needed(self, client):
        if self.store.get_state("self_id"):
            return None
        try:
            return client.get_account()
        except CloudflareError:
            return None
        except ApiError:
            return None

    def _cooling_message(self) -> str | None:
        raw = self.store.get_state("cf_until") or ""
        try:
            until = float(raw)
        except ValueError:
            return None
        if time.time() >= until:
            return None
        return self.store.get_state("last_error") or "Cloudflare 拦截后正在等待，避免连续请求。"

    def _schedule_backoff(self) -> None:
        try:
            streak = int(self.store.get_state("cf_streak") or "0")
        except ValueError:
            streak = 0
        streak += 1
        self.store.set_state("cf_streak", str(streak))
        wait = min(120 * streak, 600)
        self.store.set_state("cf_until", str(time.time() + wait))

    def _keyword_rules(self):
        rules = parse_keyword_rules(self.store.get_setting("keywords") or "")
        excludes = parse_excludes(self.store.get_setting("excludes") or "")
        return rules, excludes

    def _keywords_quiet(self) -> bool:
        if (self.store.get_setting("quiet_enabled") or "0") != "1":
            return False
        start = self.store.get_setting("quiet_start") or "23:00"
        end = self.store.get_setting("quiet_end") or "08:00"
        return in_quiet_hours(self.clock(), start, end)

    def _maybe_heartbeat(self) -> None:
        if (self.store.get_setting("heartbeat_enabled", "1") or "1") == "0":
            return
        try:
            hour = int(self.store.get_setting("heartbeat_hour") or "9")
        except ValueError:
            hour = 9
        hour = max(0, min(hour, 23))
        now = self.clock()
        if not heartbeat_due(now, hour, self.store.get_state("heartbeat_day") or ""):
            return
        if not (self.store.get_setting("tg_token") or "").strip() or not (self.store.get_setting("tg_chat_id") or "").strip():
            return
        name = self.store.get_state("self_name") or "当前账号"
        error = (self.store.get_state("last_error") or "").strip()
        if error:
            text = f"NodeSeek 监控还在跑，但最近检查有问题。\n账号：{name}\n问题：{error}"
        else:
            text = f"NodeSeek 监控还在跑。\n账号：{name}\n今天已推送 {self.store.count_pushes_today()} 条。"
        try:
            self._send(text)
        except TelegramError as exc:
            self.log("error", f"每日存活消息没有发出去：{exc}")
            return
        self.store.set_state("heartbeat_day", now.date().isoformat())
        self.store.add_event("info", "监控还在跑", text)
        self.log("info", "已发送每日存活消息")

    def _maybe_checkin(self) -> None:
        if (self.store.get_setting("checkin_enabled", "1") or "1") == "0":
            return
        today = self.clock().date().isoformat()
        if self.store.get_state("checkin_day") == today:
            return
        raw = self.store.get_state("checkin_retry_at") or ""
        try:
            if time.time() < float(raw):
                return
        except ValueError:
            pass
        if self._cooling_message():
            return
        with self._check_hold(0) as held:
            if not held:
                return
            self._checkin_once(today)

    def _checkin_once(self, today: str) -> None:
        cookie = (self.store.get_setting("cookie") or "").strip()
        use_browser = auth_mode(self.store) == "browser"
        if not cookie and not use_browser:
            self._note_checkin("还没有登录，今天的签到先跳过。")
            return
        try:
            payload = self._make_client(cookie).check_in()
        except (AuthError, CloudflareError, ApiError, ValueError) as exc:
            self._note_checkin(str(exc))
            return
        done, text = describe_checkin(payload)
        if not done:
            self._note_checkin(text)
            return
        self.store.set_state("checkin_day", today)
        self.store.delete_state("checkin_retry_at")
        self.store.set_state("checkin_result", text[:200])
        self.store.delete_state("checkin_last_error")
        self.store.add_event("info", "每日签到", text)
        self.log("info", f"每日签到：{text}")
        try:
            self._send("NodeSeek 签到\n" + text)
        except TelegramError as exc:
            self.log("error", f"签到结果没有发出去：{exc}")

    def _note_checkin(self, message: str) -> None:
        self.store.set_state("checkin_retry_at", str(time.time() + 1800))
        if self.store.get_state("checkin_last_error") == message:
            return
        self.store.set_state("checkin_last_error", message)
        self.log("error", f"签到还没完成：{message}")

    def _maybe_water(self) -> None:
        if (self.store.get_setting("water_enabled") or "0") != "1":
            return
        today = self.clock().date().isoformat()
        if self.store.get_state("water_day") != today:
            self.store.delete_state("water_stopped")
        if self.store.get_state("water_day") == today and self.store.get_state("water_stopped") == "1":
            return
        if not self._checkin_attempted(today):
            return
        raw = self.store.get_state("water_next_at") or ""
        try:
            if time.time() < float(raw):
                return
        except ValueError:
            pass
        if self._cooling_message():
            return
        with self._check_hold(0) as held:
            if not held:
                return
            self._water_once(today)

    def _checkin_attempted(self, today: str) -> bool:
        if (self.store.get_setting("checkin_enabled", "1") or "1") == "0":
            return True
        if self.store.get_state("checkin_day") == today:
            return True
        return bool(self.store.get_state("checkin_last_error") or self.store.get_state("checkin_retry_at"))

    def _water_once(self, today: str) -> None:
        if auth_mode(self.store) != "browser":
            self._note_water("水贴只走登录浏览器。请到设置打开登录浏览器。")
            return
        phrases = parse_phrases(self.store.get_setting("water_phrases") or "")
        if len(phrases) < 2:
            self._note_water("短回复至少要两句，水贴先停着。")
            return
        self_raw = self.store.get_state("self_id") or ""
        if not self_raw.isdigit():
            self._note_water("还没识别当前账号，水贴先等一次成功检查。")
            return
        daily = _water_daily(self.store)
        count = self._water_count(today)
        if count >= daily:
            self._finish_water(today, count, "今天的条数已经发完。")
            return
        try:
            client = self.browser_client_factory()
            targets = client.list_water_targets()
        except (AuthError, CloudflareError, ApiError, ValueError) as exc:
            self._note_water(str(exc))
            return
        rules, excludes = self._keyword_rules()
        chosen = pick_water_target(
            targets,
            self_id=int(self_raw),
            watered=self._water_ids(today),
            now=self.clock(),
            rules=rules,
            excludes=excludes,
            boards=selected_slugs(self.store.get_setting("boards")),
        )
        if chosen is None:
            self._note_water("灌水区这页没有能发的帖，过半小时再看。")
            return
        try:
            index = int(self.store.get_state("water_phrase_index") or "0")
        except ValueError:
            index = 0
        phrase, next_index = next_phrase(phrases, index)
        try:
            payload = client.comment(chosen.post_id, phrase)
        except (AuthError, CloudflareError, ApiError, ValueError) as exc:
            self._note_water(str(exc))
            return
        outcome = describe_comment(payload)
        if outcome.ok:
            count += 1
            ids = self._water_ids(today)
            ids.append(chosen.post_id)
            self.store.set_state("water_day", today)
            self.store.set_state("water_count", str(count))
            self.store.set_state("water_ids", ",".join(str(item) for item in ids))
            self.store.set_state("water_phrase_index", str(next_index))
            self.store.set_state("water_result", f"今天已发 {count} 条。站点：{outcome.message}"[:300])
            self.store.delete_state("water_last_error")
            self.store.delete_state("water_stopped")
            self.log("info", f"水贴已发 1 条，今天 {count} 条。")
        if outcome.stop_today or count >= daily:
            self._finish_water(today, count, outcome.message)
            return
        if not outcome.ok:
            self._note_water(outcome.message)
            return
        self.store.set_state("water_next_at", str(time.time() + _water_gap_minutes(self.store) * 60))

    def _finish_water(self, today: str, count: int, site_message: str) -> None:
        self.store.set_state("water_day", today)
        self.store.set_state("water_stopped", "1")
        self.store.set_state("water_count", str(count))
        self.store.delete_state("water_next_at")
        self.store.delete_state("water_last_error")
        text = f"今天发了 {count} 条，已停到明天。"
        if site_message:
            text = text + "\n站点：" + site_message
        self.store.set_state("water_result", text[:300])
        self.store.add_event("info", "水贴", text)
        self.log("info", f"水贴：{text.replace(chr(10), ' ')}")
        try:
            self._send("NodeSeek 水贴\n" + text)
        except TelegramError as exc:
            self.log("error", f"水贴结果没有发出去：{exc}")

    def _note_water(self, message: str) -> None:
        self.store.set_state("water_next_at", str(time.time() + WATER_RETRY_SECONDS))
        if self.store.get_state("water_last_error") == message:
            return
        self.store.set_state("water_last_error", message)
        self.log("error", f"水贴还没完成：{message}")

    def _water_count(self, today: str) -> int:
        if self.store.get_state("water_day") != today:
            return 0
        try:
            return int(self.store.get_state("water_count") or "0")
        except ValueError:
            return 0

    def _water_ids(self, today: str) -> list[int]:
        if self.store.get_state("water_day") != today:
            return []
        found = []
        for part in (self.store.get_state("water_ids") or "").split(","):
            if part.strip().isdigit():
                found.append(int(part))
        return found

    def _note_post_error(self, message: str) -> str:
        shown = RSS_WAIT if _rss_connect_failure(message) else message
        kind = "rss-connect" if shown == RSS_WAIT else shown
        self.store.set_state("posts_last_error", shown)
        if self.store.get_state("posts_error_kind") == kind:
            return shown
        self.store.set_state("posts_error_kind", kind)
        self.log("error", shown)
        return shown

    def _note_browser_down(self, message: str) -> dict:
        now = time.time()
        raw = self.store.get_state("browser_down_since") or ""
        try:
            since = float(raw)
        except ValueError:
            since = 0.0
        if since <= 0:
            since = now
            self.store.set_state("browser_down_since", str(now))
        changed = self.store.get_state("last_error") != message
        self.store.set_state("last_error", message)
        self.store.set_state("ok_streak", "0")
        if changed:
            self.log("error", message)
        if now - since >= BROWSER_ALERT_SECONDS:
            self._alert(message)
        return {"ok": False, "error": message, "retry_in": BROWSER_RETRY_SECONDS}

    def _rss_cooling_message(self) -> str | None:
        raw = self.store.get_state("rss_until") or ""
        try:
            until = float(raw)
        except ValueError:
            return None
        if time.time() >= until:
            return None
        return self.store.get_state("posts_last_error") or RSS_WAIT

    def _schedule_rss_backoff(self) -> None:
        try:
            streak = int(self.store.get_state("rss_streak") or "0")
        except ValueError:
            streak = 0
        streak += 1
        self.store.set_state("rss_streak", str(streak))
        wait = min(RSS_BACKOFF_SECONDS * streak, RSS_BACKOFF_MAX)
        self.store.set_state("rss_until", str(time.time() + wait))

    def _send(self, text: str, reply_markup: dict | None = None) -> None:
        token = (self.store.get_setting("tg_token") or "").strip()
        chat_id = (self.store.get_setting("tg_chat_id") or "").strip()
        if not token or not chat_id:
            raise TelegramError("还没有配置 Telegram Bot Token 或 Chat ID")
        if reply_markup is None:
            self.sender(token, chat_id, text)
            return
        try:
            self.sender(token, chat_id, text, reply_markup)
        except TypeError:
            self.sender(token, chat_id, text)

    def _listen_loop(self) -> None:
        while not self._listen_stop.is_set():
            try:
                self._listen_once()
            except Exception:
                self._note_listen_error()
                self._listen_stop.wait(5)

    def _listen_once(self) -> None:
        token = (self.store.get_setting("tg_token") or "").strip()
        chat_id = (self.store.get_setting("tg_chat_id") or "").strip()
        if not token or not chat_id:
            self._listen_stop.wait(5)
            return
        if self.store.get_state("tg_keyboard_for") != keyboard_seen(chat_id):
            try:
                self._send(HELLO, reply_keyboard())
            except TelegramError:
                self._note_listen_error()
                self._listen_stop.wait(15)
                return
            self.store.set_state("tg_keyboard_for", keyboard_seen(chat_id))
            self.store.delete_state("tg_listen_error")
        offset = _optional_int(self.store.get_state("tg_update_offset")) or 0
        try:
            updates = get_updates(token, offset)
        except TelegramError:
            self._note_listen_error()
            self._listen_stop.wait(5)
            return
        self.store.delete_state("tg_listen_error")
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            self._handle_telegram(update, token, chat_id)
            if isinstance(update_id, int):
                self.store.set_state("tg_update_offset", str(update_id + 1))

    def _handle_telegram(self, update, token: str, owner_chat_id: str) -> None:
        if not isinstance(update, dict):
            return
        message = update.get("message") or {}
        chat = message.get("chat") if isinstance(message, dict) else {}
        if not isinstance(chat, dict):
            return
        incoming = str(chat.get("id") or "")
        if not incoming:
            return
        text = str(message.get("text") or "").strip()
        if chat.get("type") == "private" and (incoming == owner_chat_id or text == "/start" or text.startswith("/start ")):
            self.store.set_state("tg_last_chat_id", incoming)
        if incoming != owner_chat_id:
            if chat.get("type") == "private":
                try:
                    self.sender(token, incoming, FOREIGN_TEXT)
                except TelegramError:
                    self._note_listen_error()
            return
        answer = answer_for(text, self.store, self.running)
        if answer is None:
            return
        try:
            self.sender(token, owner_chat_id, answer["text"], answer.get("reply_markup"))
        except TypeError:
            self.sender(token, owner_chat_id, answer["text"])
        except TelegramError:
            self._note_listen_error()

    def _note_listen_error(self) -> None:
        message = "Telegram 按钮没有连上。"
        if self.store.get_state("tg_listen_error") == message:
            return
        self.store.set_state("tg_listen_error", message)
        self.log("error", message)

    def _fail(self, message: str, alert: bool) -> dict:
        changed = self.store.get_state("last_error") != message
        self.store.set_state("last_error", message)
        self.store.set_state("ok_streak", "0")
        if changed:
            self.log("error", message)
        if alert:
            self._alert(message)
        return {"ok": False, "error": message}

    def _ensure_awake(self) -> None:
        if os.environ.get("NS_RUNTIME") != "docker":
            return
        thread = getattr(self, "_awake_thread", None)
        if thread is not None and thread.is_alive():
            return
        self._awake_misses = 0
        self._awake_thread = threading.Thread(
            target=self._awake_loop, name="browser-awake", daemon=True
        )
        self._awake_thread.start()

    def _awake_loop(self) -> None:
        while True:
            time.sleep(20)
            if auth_mode(self.store) != "browser":
                continue
            with self._check_hold(0) as held:
                if not held:
                    continue
                wake = False
                if not port_open(PORT, timeout=1):
                    self._awake_misses += 1
                    wake = self._awake_misses >= AWAKE_MISSES
                else:
                    try:
                        shared_session(PORT).poke()
                        self._awake_misses = 0
                    except Exception:
                        self._awake_misses += 1
                        wake = self._awake_misses >= AWAKE_MISSES
                if not wake:
                    continue
                self._awake_misses = 0
                print("[info] 登录页面连续几分钟没有响应，正在重新打开。", flush=True)
                try:
                    _wake_target(PORT, restart_process=True)
                except Exception as exc:
                    print(f"[info] 重新打开登录页面没有成功：{exc}", flush=True)

    def _alert(self, message: str) -> None:
        if self.store.get_state("alert_message") == message:
            return
        self.store.set_state("alert_message", message)
        text = "NodeSeek 私信监控异常\n" + message + "\n" + alert_hint()
        try:
            self._send(text)
        except TelegramError as exc:
            self.log("error", f"异常提醒没有发出去：{exc}")
            return
        self.store.add_event("alert", "监控异常", text)

    def _recover(self) -> None:
        self.store.delete_state("cf_until")
        self.store.delete_state("browser_down_since")
        self.store.set_state("last_error", "")
        self.store.set_state("last_success", _now())
        try:
            streak = int(self.store.get_state("ok_streak") or "0")
        except ValueError:
            streak = 0
        streak += 1
        self.store.set_state("ok_streak", str(streak))
        if not self.store.get_state("alert_message"):
            return
        if streak < 2:
            return
        self.store.set_state("cf_streak", "0")
        token = (self.store.get_setting("tg_token") or "").strip()
        chat_id = (self.store.get_setting("tg_chat_id") or "").strip()
        if not token or not chat_id:
            self.store.delete_state("alert_message")
            return
        name = self.store.get_state("self_name") or "当前账号"
        text = f"NodeSeek 私信监控已恢复，当前账号：{name}"
        try:
            self._send(text)
        except TelegramError as exc:
            self.log("error", f"恢复提醒没有发出去：{exc}")
            return
        self.store.delete_state("alert_message")
        self.store.add_event("info", "监控已恢复", text)


def _rss_connect_failure(message: str) -> bool:
    text = message.lower()
    if "rss" not in text and "rss.nodeseek.com" not in text:
        return False
    needles = (
        "could not connect",
        "failed to connect",
        "timed out",
        "timeout",
        "curl: (6)",
        "curl: (7)",
        "curl: (28)",
        "name or service not known",
        "temporary failure in name resolution",
    )
    return any(needle in text for needle in needles)


def alert_hint() -> str:
    if os.environ.get("NS_RUNTIME") == "docker":
        return "请打开网页桌面查看登录浏览器。程序会自动重开页面。"
    return "请打开本机监控面板处理。"


def _now() -> str:
    from ns_panel.store import now_iso

    return now_iso()


def _combine(pm: dict, post: dict) -> dict:
    merged = dict(pm)
    merged["posts_notified"] = post.get("notified", 0)
    if post.get("error"):
        merged["posts_error"] = post["error"]
    post_message = post.get("message") or ""
    if post_message and (post.get("notified") or post.get("posts_baseline") or post.get("baseline")):
        base = merged.get("message") or ""
        merged["message"] = f"{base} {post_message}".strip()
    return merged


def _peer_signature(payload, self_id: int) -> tuple[int, ...]:
    return tuple(item.peer_id for item in parse_message_list(payload, self_id))


def _pm_page_old(conversations, watermark: str) -> bool:
    if not conversations or not watermark:
        return not conversations
    return all((not item.created_at) or item.created_at <= watermark for item in conversations)


def _message_watermark(items) -> str:
    times = [item.created_at for item in items if getattr(item, "created_at", "")]
    return max(times) if times else ""


def _silent_history(conv, page: int, previous: dict, watermark: str, baseline_done: bool) -> bool:
    """第 2 页及以后、上次没见过、时间不新于水位的会话是历史，不推送。"""
    if not baseline_done or page <= 1 or conv.peer_id in previous:
        return False
    if not watermark or not conv.created_at:
        return True
    return conv.created_at <= watermark


def _optional_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None
