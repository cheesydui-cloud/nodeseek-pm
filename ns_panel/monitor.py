"""定时拉取私信和新帖，有新内容时推到 Telegram。"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime

from ns_panel.browser import PORT, BrowserClient, auth_mode, port_open, shared_session, _wake_target
from ns_panel.diff import (
    format_pushes,
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
    message_rows,
)
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
from ns_panel.telegram import TelegramError, send_message


def interval_seconds(store: Store) -> int:
    raw = store.get_setting("interval", "90") or "90"
    try:
        value = int(raw)
    except ValueError:
        value = 90
    return max(30, min(value, 3600))


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
        self.next_check_at: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._life:
            if self.running:
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._loop, name="nodeseek-poll", daemon=True
            )
            self._thread.start()
            self._ensure_awake()

    def stop(self) -> None:
        self._stop.set()

    def check_once(self, wait: bool = False) -> dict:
        if wait:
            self._check_lock.acquire()
        elif not self._check_lock.acquire(timeout=0.2):
            return {"ok": False, "error": "上一次检查还在进行"}
        try:
            return self._check()
        except Exception as exc:
            message = f"检查失败：{exc}"
            self.store.set_state("last_error", message)
            self.store.set_state("last_check", _now())
            self.log("error", message)
            return {"ok": False, "error": message}
        finally:
            self._check_lock.release()

    def scan_posts(self, reset: bool = False) -> dict:
        if not self._check_lock.acquire(timeout=0.2):
            return {"ok": False, "error": "上一次检查还在进行"}
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
        finally:
            self._check_lock.release()

    def log(self, level: str, message: str) -> None:
        self.store.add_log(level, message)
        print(f"[{level}] {message}", flush=True)

    def _loop(self) -> None:
        self.log("info", "监控已启动")
        try:
            while not self._stop.is_set():
                self.check_once(wait=True)
                self._maybe_heartbeat()
                interval = interval_seconds(self.store)
                self.next_check_at = time.time() + interval
                if self._stop.wait(interval):
                    break
        finally:
            self.next_check_at = None
            self.log("info", "监控已停止")

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

        cooled = self._cooling_message()
        if cooled:
            pm = {"ok": False, "error": cooled}
        else:
            pm = self._check_pm(cookie)
            if pm.get("ok"):
                pm = self._with_mentions(cookie, pm)
                pm = self._with_replies(cookie, pm)
        if not rules:
            return pm
        try:
            post = self._check_posts(cookie, rules, excludes)
        except Exception as exc:
            post = {"ok": False, "error": f"关键词检查失败：{exc}", "notified": 0}
            self._note_post_error(post["error"])
        return _combine(pm, post)

    def _check_pm(self, cookie: str) -> dict:
        try:
            client = self._make_client(cookie)
            payload = client.get_message_list()
            rows = message_rows(payload)
            account = self._account_if_needed(client)
        except AuthError as exc:
            return self._fail(str(exc), alert=True)
        except CloudflareError as exc:
            self._schedule_backoff()
            return self._fail(str(exc), alert=True)
        except (ApiError, ValueError) as exc:
            return self._fail(str(exc), alert=False)

        self_id = account.member_id if account else _optional_int(self.store.get_state("self_id"))
        if not self_id:
            self_id = client.resolve_self_id(payload)
        if not self_id:
            return self._fail("拉到了私信列表，但没能识别当前账号。请更新 Cookie 后再试。", alert=False)

        self_name = account.member_name if account and account.member_name else ""
        if not self_name:
            self_name = self_name_from_rows(rows, self_id)
        self.store.set_state("self_id", str(self_id))
        if self_name:
            self.store.set_state("self_name", self_name)

        current = parse_message_list(payload, self_id)
        previous = self.store.conversations()
        baseline_done = self.store.get_state("baseline_done") == "1"
        notify, upserts, created_baseline = select_notifications(
            previous, current, self_id, baseline_done
        )

        if created_baseline:
            for item in upserts:
                self.store.upsert_conversation(item)
            self.store.set_state("baseline_done", "1")
            self._recover()
            text = f"已建立基线，记住 {len(upserts)} 个会话。这些历史私信不会推送。"
            self.store.add_event("info", "已建立基线", text)
            self.log("info", text)
            return {"ok": True, "notified": 0, "message": text, "baseline": True}

        if notify:
            text = format_pushes(notify)
            try:
                self._send(text)
            except TelegramError as exc:
                message = f"私信已拉到，但 Telegram 发送失败：{exc}"
                self.store.set_state("last_error", message)
                self.log("error", message)
                for item in upserts:
                    self.store.upsert_conversation(item)
                return {"ok": False, "error": message, "notified": 0}
            for item in notify:
                self.store.upsert_conversation(item)
            title = notify[0].peer_name if len(notify) == 1 else f"{len(notify)} 条新私信"
            self.store.add_event("push", title, text)
            self.log("info", f"已推送 {len(notify)} 条新私信")

        for item in upserts:
            self.store.upsert_conversation(item)

        self._recover()
        if notify:
            message = f"已推送 {len(notify)} 条新私信"
        else:
            message = "检查完成，没有新私信"
        return {"ok": True, "notified": len(notify), "message": message, "baseline": False}

    def scan_mentions(self, reset: bool = False) -> dict:
        if not self._check_lock.acquire(timeout=0.2):
            return {"ok": False, "error": "上一次检查还在进行"}
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
        finally:
            self._check_lock.release()

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
        rows = parse_mentions(client.get_mentions())
        self_id = _optional_int(self.store.get_state("self_id"))
        if self_id:
            rows = [row for row in rows if row.commenter_id != self_id]
        baseline_done = self.store.get_state("mentions_baseline_done") == "1"
        plans = plan_mentions(rows, self.store.seen_mention_ids(), baseline_done)
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
            self._send(text)
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
        if not self._check_lock.acquire(timeout=0.2):
            return {"ok": False, "error": "上一次检查还在进行"}
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
        finally:
            self._check_lock.release()

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
        rows = parse_replies(client.get_replies())
        self_id = _optional_int(self.store.get_state("self_id"))
        if self_id:
            rows = [row for row in rows if row.commenter_id != self_id]
        baseline_done = self.store.get_state("replies_baseline_done") == "1"
        plans = plan_replies(rows, self.store.seen_reply_ids(), baseline_done)
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
            self._send(text)
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
        posts, errors, fetched = self.feed_fetcher(cookie, scan)
        allowed = set(scan)
        posts = [post for post in posts if post.category in allowed]
        if not posts and not fetched:
            message = "没能拉到帖子：" + ("；".join(errors[:3]) or "没有返回内容")
            self._note_post_error(message)
            return {"ok": False, "error": message, "notified": 0}
        if errors:
            self._note_post_error("部分板块没拉到：" + "；".join(errors[:3]))
        else:
            self.store.set_state("posts_last_error", "")
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
            return {"ok": True, "notified": 0, "message": text, "baseline": True, "posts_baseline": True}

        pending = self.store.pending_hits()
        if pending and self._keywords_quiet():
            message = f"关键词命中 {len(pending)} 篇。现在是免打扰时段，先记下，结束后再推。"
            if self.store.get_state("quiet_hold") != message:
                self.store.set_state("quiet_hold", message)
                self.log("info", message)
            return {"ok": True, "notified": 0, "message": message, "held": len(pending)}
        self.store.delete_state("quiet_hold")
        notified = 0
        if pending:
            text = format_post_alerts(pending)
            try:
                self._send(text)
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
        return {"ok": True, "notified": notified, "message": message, "baseline": False, "listed": listed}

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

    def _note_post_error(self, message: str) -> None:
        if self.store.get_state("posts_last_error") == message:
            return
        self.store.set_state("posts_last_error", message)
        self.log("error", message)

    def _send(self, text: str) -> None:
        token = (self.store.get_setting("tg_token") or "").strip()
        chat_id = (self.store.get_setting("tg_chat_id") or "").strip()
        if not token or not chat_id:
            raise TelegramError("还没有配置 Telegram Bot Token 或 Chat ID")
        self.sender(token, chat_id, text)

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
            if not self._check_lock.acquire(blocking=False):
                continue
            wake = False
            try:
                if not port_open(PORT, timeout=1):
                    self._awake_misses += 1
                    wake = self._awake_misses >= 2
                else:
                    try:
                        shared_session(PORT).poke()
                        self._awake_misses = 0
                    except Exception:
                        self._awake_misses += 1
                        wake = self._awake_misses >= 2
            finally:
                self._check_lock.release()
            if not wake:
                continue
            self._awake_misses = 0
            print("[info] 登录页面没有响应，正在重新打开。", flush=True)
            try:
                _wake_target(PORT)
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


def _optional_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None
