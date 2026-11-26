import base64
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ns_panel.mentions import format_mention_alerts, mention_link, parse_mentions, plan_mentions
from ns_panel.diff import (
    Conversation,
    extract_member,
    format_pushes,
    infer_self_id,
    parse_cookie,
    parse_message_list,
    select_notifications,
    self_id_from_thread,
    strip_html,
)
from ns_panel.botview import answer_for, open_markup, pm_open_links
from ns_panel.monitor import Monitor, alert_hint
from ns_panel.browser import BrowserDown, SessionDown, auth_mode, find_edge, login_asleep_message, run_with_retry
from ns_panel.nodeseek import AuthError, CloudflareError, describe_checkin, looks_like_challenge
from datetime import datetime

from ns_panel.posts import Post, parse_category_html, parse_keyword_rules, parse_keywords, parse_rss, plan_posts
from ns_panel.replies import format_reply_alerts, parse_replies
from ns_panel.schedule import heartbeat_due, in_quiet_hours
from ns_panel.water import (
    WaterTarget,
    describe_comment,
    parse_phrases,
    parse_water_board,
    pick_water_target,
)
from ns_panel.nodeseek import NodeSeekClient
from ns_panel.pages import notification_path
from ns_panel.server import App, create_httpd, panel_authorized
from ns_panel.watch import watch_once
from ns_panel.store import Store


def message(sender_id, sender_name, receiver_id, receiver_name, content, created_at, max_id, viewed=0):
    return {
        "sender_id": sender_id,
        "sender_name": sender_name,
        "receiver_id": receiver_id,
        "receiver_name": receiver_name,
        "content": content,
        "created_at": created_at,
        "max_id": max_id,
        "viewed": viewed,
    }


PAYLOAD = {
    "success": True,
    "msgArray": [
        message(2, "hunlun", 9, "me", "<b>收到</b>，谢谢！", "2026-11-08 09:32:09", 100, 0),
        message(9, "me", 3, "kagent93", "好的", "2026-11-08 10:00:00", 80, 1),
    ],
}


class DiffTest(unittest.TestCase):
    def test_strip_and_cookie(self):
        self.assertEqual(strip_html("<p>你好<br>世界</p>"), "你好\n世界")
        self.assertEqual(parse_cookie("Cookie: a=1; b=2\n"), "a=1; b=2")

    def test_account_and_self_id(self):
        member = extract_member({"success": True, "data": {"member_id": "9", "member_name": "me"}})
        self.assertEqual((member.member_id, member.member_name), (9, "me"))
        self.assertEqual(infer_self_id(PAYLOAD["msgArray"]), 9)
        self.assertEqual(
            self_id_from_thread(
                {"talkTo": {"member_id": 2}, "msgArray": [{"sender_id": 2, "receiver_id": 9}]},
                2,
            ),
            9,
        )

    def test_baseline_then_incoming_then_quiet(self):
        current = parse_message_list(PAYLOAD, 9)
        notify, upserts, baseline = select_notifications({}, current, 9, False)
        self.assertEqual(notify, [])
        self.assertTrue(baseline)
        self.assertEqual(len(upserts), 2)

        previous = {item.peer_id: item for item in upserts}
        changed = parse_message_list(
            {
                "success": True,
                "msgArray": [
                    message(2, "hunlun", 9, "me", "又来一条", "2026-11-08 12:00:00", 120, 0),
                    message(9, "me", 3, "kagent93", "我先回了", "2026-11-08 12:01:00", 130, 1),
                ],
            },
            9,
        )
        notify, _upserts, baseline = select_notifications(previous, changed, 9, True)
        self.assertFalse(baseline)
        self.assertEqual([item.peer_name for item in notify], ["hunlun"])
        self.assertIn("又来一条", format_pushes(notify))
        self.assertNotIn("<b>", format_pushes(notify))

        stored = {item.peer_id: item for item in changed}
        again, _upserts, _baseline = select_notifications(stored, changed, 9, True)
        self.assertEqual(again, [])

    def test_cloudflare_detection(self):
        self.assertTrue(looks_like_challenge(403, "<html>Just a moment", "text/html"))
        self.assertFalse(looks_like_challenge(200, '{"success": true}', "application/json"))

    def test_ca_bundle_path_is_ascii(self):
        try:
            from ns_panel.nodeseek import ca_bundle
        except Exception as exc:
            self.skipTest(str(exc))
        path = ca_bundle()
        path.encode("ascii")
        self.assertTrue(Path(path).is_file())


class RetryTest(unittest.TestCase):
    def test_retry_wakes_once_then_succeeds(self):
        calls = {"action": 0, "wake": 0}

        def action():
            calls["action"] += 1
            if calls["action"] == 1:
                raise SessionDown("asleep")
            return "ok"

        def wake():
            calls["wake"] += 1

        self.assertEqual(run_with_retry(action, wake), "ok")
        self.assertEqual(calls, {"action": 2, "wake": 1})

    def test_retry_gives_up_with_clear_error(self):
        with self.assertRaises(BrowserDown) as caught:
            run_with_retry(lambda: (_ for _ in ()).throw(SessionDown("asleep")), lambda: None)
        self.assertIn("登录窗口没有响应", str(caught.exception))

    def test_docker_asleep_message_points_at_container(self):
        os.environ["NS_RUNTIME"] = "docker"
        try:
            self.assertIn("重新打开", login_asleep_message())
            self.assertIn("网页桌面", alert_hint())
            self.assertNotIn("本机", login_asleep_message())
            self.assertNotIn("本机", alert_hint())
        finally:
            os.environ.pop("NS_RUNTIME", None)


class FakeClient:
    def __init__(self, cookie, box):
        self.cookie = cookie
        self.box = box

    def get_account(self):
        if self.box.get("error"):
            raise self.box["error"]
        return self.box.get("account")

    def get_message_list(self, page=1):
        self.box["list_calls"] = self.box.get("list_calls", 0) + 1
        self.box.setdefault("message_pages", []).append(page)
        if self.box.get("list_error") and page == 1:
            raise self.box["list_error"]
        pages = self.box.get("message_payloads")
        if isinstance(pages, dict):
            return pages.get(page, {"success": True, "msgArray": []})
        if page > 1:
            return {"success": True, "msgArray": []}
        return self.box["payload"]

    def get_mentions(self, page=1):
        self.box["mention_calls"] = self.box.get("mention_calls", 0) + 1
        self.box.setdefault("mention_pages", []).append(page)
        if self.box.get("mention_error") and page == 1:
            raise self.box["mention_error"]
        pages = self.box.get("mention_payloads")
        if isinstance(pages, dict):
            return pages.get(page, {"success": True, "data": []})
        if page > 1:
            return {"success": True, "data": []}
        return self.box.get("mentions", {"success": True, "data": []})

    def get_replies(self, page=1):
        self.box["reply_calls"] = self.box.get("reply_calls", 0) + 1
        self.box.setdefault("reply_pages", []).append(page)
        if self.box.get("reply_error") and page == 1:
            raise self.box["reply_error"]
        pages = self.box.get("reply_payloads")
        if isinstance(pages, dict):
            return pages.get(page, {"success": True, "data": []})
        if page > 1:
            return {"success": True, "data": []}
        return self.box.get("replies", {"success": True, "data": []})

    def resolve_self_id(self, _payload):
        return self.box.get("self_id", 9)

    def check_in(self):
        self.box["checkin_calls"] = self.box.get("checkin_calls", 0) + 1
        if self.box.get("checkin_error"):
            raise self.box["checkin_error"]
        return self.box.get("checkin", {"success": True, "message": "签到成功", "gain": 5})

    def list_water_targets(self, board=None):
        self.box["water_list_calls"] = self.box.get("water_list_calls", 0) + 1
        self.box.setdefault("water_boards", []).append(board)
        if self.box.get("water_list_error"):
            raise self.box["water_list_error"]
        return list(self.box.get("water_targets") or [])

    def comment(self, post_id, content):
        self.box["comment_calls"] = self.box.get("comment_calls", 0) + 1
        self.box.setdefault("comments", []).append((post_id, content))
        if self.box.get("comment_error"):
            raise self.box["comment_error"]
        payload = self.box.get("comment_payload")
        if isinstance(payload, list):
            return payload[min(self.box["comment_calls"] - 1, len(payload) - 1)]
        return payload or {"success": True, "message": "评论成功"}


def mention_row(
    mention_id,
    commenter_id=2,
    name="hunlun",
    post_id=100,
    floor_id=3,
    title="测试帖",
    created_at="2026-11-08 09:00:00",
):
    return {
        "id": mention_id,
        "viewed": 1,
        "comment_id": mention_id,
        "floor_id": floor_id,
        "created_at": created_at,
        "commenter_id": commenter_id,
        "title": title,
        "post_id": post_id,
        "commenter_name": name,
    }


class MentionParseTest(unittest.TestCase):
    def test_link_uses_ten_comments_per_page(self):
        self.assertEqual(mention_link(88, 3), "https://www.nodeseek.com/post-88-1#3")
        self.assertEqual(mention_link(88, 11), "https://www.nodeseek.com/post-88-2#11")
        self.assertEqual(mention_link(88, 185), "https://www.nodeseek.com/post-88-19#185")
        self.assertEqual(mention_link(88, 0), "https://www.nodeseek.com/post-88-1")

    def test_parse_strips_html_and_skips_bad_rows(self):
        rows = parse_mentions(
            {
                "success": True,
                "data": [
                    mention_row(10, name="<b>hunlun</b>", title="<i>标题</i>"),
                    {"id": 0, "post_id": 1},
                    "bad",
                    mention_row(10),
                ],
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].commenter_name, "hunlun")
        self.assertEqual(rows[0].title, "标题")
        self.assertEqual(rows[0].link, "https://www.nodeseek.com/post-100-1#3")
        listed = parse_mentions({"atList": [mention_row(11, title="另一条")]})
        self.assertEqual(listed[0].mention_id, 11)

    def test_plan_pushes_only_newer_unseen_ids(self):
        old, newer, older = parse_mentions(
            {"data": [mention_row(10), mention_row(20), mention_row(5)]}
        )
        first = plan_mentions([old], set(), False)
        self.assertEqual([push for _item, push in first], [False])
        second = plan_mentions([old, newer, older], {10}, True)
        pushed = [item.mention_id for item, push in second if push]
        self.assertEqual(pushed, [20])
        caught_up = plan_mentions([older, newer], {10, 100}, True, since=10)
        self.assertEqual([item.mention_id for item, push in caught_up if push], [20])
        blocked = plan_mentions([newer], {10, 100}, True)
        self.assertEqual([item.mention_id for item, push in blocked if push], [])

    def test_alert_text_has_link_and_no_markup(self):
        text = format_mention_alerts(
            [
                {
                    "commenter_name": "hunlun",
                    "title": "帖子",
                    "created_at": "2026-11-08 09:00:00",
                    "link": "https://www.nodeseek.com/post-100-1#3",
                }
            ]
        )
        self.assertIn("hunlun 在帖子里@了你", text)
        self.assertIn("https://www.nodeseek.com/post-100-1#3", text)
        self.assertNotIn("<", text)


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "panel.db")
        self.store.set_setting("cookie", "session=abc123")
        self.store.set_setting("tg_token", "1:token")
        self.store.set_setting("tg_chat_id", "99")
        self.sent = []
        self.box = {"payload": PAYLOAD, "account": extract_member({"member_id": 9, "member_name": "me"})}
        self.monitor = Monitor(
            self.store,
            client_factory=lambda cookie: FakeClient(cookie, self.box),
            sender=lambda token, chat_id, text: self.sent.append((token, chat_id, text)),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_baseline_skips_history_and_then_pushes_once(self):
        first = self.monitor.check_once(wait=True)
        self.assertTrue(first["baseline"])
        self.assertEqual(self.sent, [])

        self.box["payload"] = {
            "success": True,
            "msgArray": [
                message(2, "hunlun", 9, "me", "新消息", "2026-11-08 13:00:00", 140, 0),
            ],
        }
        second = self.monitor.check_once(wait=True)
        self.assertEqual(second["notified"], 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("hunlun", self.sent[0][2])

        third = self.monitor.check_once(wait=True)
        self.assertEqual(third["notified"], 0)
        self.assertEqual(len(self.sent), 1)

    def test_auth_alert_is_sent_once(self):
        self.box["error"] = AuthError("Cookie 已失效或未登录。请重新复制 Cookie。")
        self.monitor.check_once(wait=True)
        self.monitor.check_once(wait=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Cookie 已失效", self.sent[0][2])

    def test_failed_push_is_retried(self):
        self.monitor.check_once(wait=True)
        self.box["payload"] = {
            "success": True,
            "msgArray": [message(2, "hunlun", 9, "me", "再试一次", "2026-11-08 14:00:00", 150, 0)],
        }

        def fail_once(token, chat_id, text):
            if not self.sent:
                self.sent.append("fail")
                raise __import__("ns_panel.telegram", fromlist=["TelegramError"]).TelegramError("timeout")
            self.sent.append((token, chat_id, text))

        self.monitor.sender = fail_once
        failed = self.monitor.check_once(wait=True)
        self.assertFalse(failed["ok"])
        retried = self.monitor.check_once(wait=True)
        self.assertEqual(retried["notified"], 1)

    def test_browser_down_retries_soon_and_alerts_after_three_minutes(self):
        self.box["list_error"] = BrowserDown("登录窗口没有响应。请切到那个 Edge 看一眼，保持窗口开着。")
        first = self.monitor.check_once(wait=True)
        self.assertFalse(first["ok"])
        self.assertEqual(first["retry_in"], 20)
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.store.get_state("cf_until"))
        self.store.set_state("browser_down_since", str(time.time() - 180))
        second = self.monitor.check_once(wait=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("登录窗口没有响应", self.sent[0][2])
        self.monitor.check_once(wait=True)
        self.assertEqual(len(self.sent), 1)

    def test_cloudflare_waits_and_alerts_once(self):
        self.box["list_error"] = CloudflareError("Cloudflare 拦住了请求。")
        first = self.monitor.check_once(wait=True)
        self.assertFalse(first["ok"])
        self.assertEqual(len(self.sent), 1)
        calls = self.box["list_calls"]
        second = self.monitor.check_once(wait=True)
        self.assertFalse(second["ok"])
        self.assertEqual(self.box["list_calls"], calls)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_waits_for_two_successes(self):
        self.box["list_error"] = CloudflareError("Cloudflare 拦住了请求。")
        self.monitor.check_once(wait=True)
        self.box.pop("list_error")
        self.store.delete_state("cf_until")
        self.monitor.check_once(wait=True)
        self.assertEqual(len(self.sent), 1)
        self.monitor.check_once(wait=True)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("已恢复", self.sent[1][2])

    def test_browser_mode_skips_cookie_client(self):
        self.store.set_setting("auth_mode", "browser")
        used = []

        def factory():
            used.append("browser")
            return FakeClient("browser", self.box)

        self.monitor.browser_client_factory = factory
        result = self.monitor.check_once(wait=True)
        self.assertTrue(result["ok"])
        self.assertEqual(used, ["browser", "browser", "browser"])
        self.assertEqual(self.box["list_calls"], 1)
        self.assertEqual(self.box["mention_calls"], 1)
        self.assertEqual(self.box["reply_calls"], 1)

    def test_empty_mentions_keep_pm_messages(self):
        first = self.monitor.check_once(wait=True)
        self.assertEqual(first["message"], "已建立基线，记住 2 个会话。这些历史私信不会推送。")
        self.assertEqual(self.box["mention_calls"], 1)
        second = self.monitor.check_once(wait=True)
        self.assertEqual(second["message"], "检查完成，没有新私信")
        self.assertEqual(self.sent, [])

    def test_mentions_baseline_then_push_only_newer(self):
        self.box["mentions"] = {"success": True, "data": [mention_row(10, title="旧帖")]}
        first = self.monitor.check_once(wait=True)
        self.assertEqual(self.sent, [])
        self.assertIn("已记住当前 1 条@", first["message"])
        self.assertEqual(self.store.list_mentions()[0]["status"], "listed")

        self.box["mentions"] = {
            "success": True,
            "data": [
                mention_row(10, title="旧帖"),
                mention_row(5, title="更早的帖"),
                mention_row(30, name="alice", title="新帖", floor_id=11),
                mention_row(40, commenter_id=9, name="me", title="自己@自己"),
            ],
        }
        second = self.monitor.check_once(wait=True)
        self.assertEqual(second["notified"], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("alice", self.sent[0][2])
        self.assertIn("新帖", self.sent[0][2])
        self.assertIn("post-100-2#11", self.sent[0][2])
        self.assertNotIn("更早的帖", self.sent[0][2])
        self.assertNotIn("自己@自己", self.sent[0][2])
        statuses = {row["mention_id"]: row["status"] for row in self.store.list_mentions()}
        self.assertEqual(statuses[30], "sent")
        self.assertEqual(statuses[10], "listed")
        self.assertNotIn(40, statuses)

        self.monitor.check_once(wait=True)
        self.assertEqual(len(self.sent), 1)

    def test_mention_fetch_error_does_not_fail_pm(self):
        self.box["mention_error"] = CloudflareError("提到接口被拦")
        first = self.monitor.check_once(wait=True)
        self.assertTrue(first["ok"])
        self.assertEqual(first["message"], "已建立基线，记住 2 个会话。这些历史私信不会推送。")
        self.assertIn("提到接口被拦", self.store.get_state("mentions_last_error"))
        self.assertEqual(self.store.get_state("last_error") or "", "")
        self.assertEqual(self.sent, [])

    def test_failed_mention_push_is_retried(self):
        self.monitor.check_once(wait=True)
        self.box["mentions"] = {"success": True, "data": [mention_row(30, name="alice", title="新帖")]}

        calls = {"n": 0}

        def fail_once(token, chat_id, text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise __import__("ns_panel.telegram", fromlist=["TelegramError"]).TelegramError("timeout")
            self.sent.append((token, chat_id, text))

        self.monitor.sender = fail_once
        failed = self.monitor.check_once(wait=True)
        self.assertTrue(failed["ok"])
        self.assertIn("Telegram 发送失败", self.store.get_state("mentions_last_error"))
        self.assertEqual(self.store.list_mentions()[0]["status"], "pending")
        retried = self.monitor.check_once(wait=True)
        self.assertEqual(retried.get("mentions_notified"), 1)
        self.assertEqual(self.store.list_mentions()[0]["status"], "sent")
        self.assertEqual(self.store.get_state("mentions_last_error"), "")

    def test_replies_baseline_then_push_only_newer(self):
        self.box["replies"] = {"success": True, "replys": [mention_row(10, title="旧帖")]}
        first = self.monitor.check_once(wait=True)
        self.assertIn("已记住当前 1 条回复", first["message"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.list_replies()[0]["status"], "listed")

        self.box["replies"] = {
            "data": [
                mention_row(10, title="旧帖"),
                mention_row(8, title="更早的回复"),
                mention_row(30, name="alice", title="新回复", floor_id=11),
                mention_row(40, commenter_id=9, name="me", title="自己回复"),
            ]
        }
        second = self.monitor.check_once(wait=True)
        self.assertEqual(second.get("replies_notified"), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("alice 回复了你的帖子", self.sent[0][2])
        self.assertIn("https://www.nodeseek.com/post-100-2#11", self.sent[0][2])
        self.assertNotIn("自己回复", self.sent[0][2])
        self.assertNotIn("更早的回复", self.sent[0][2])

    def test_heartbeat_sends_once_after_the_hour(self):
        self.monitor.clock = lambda: datetime(2026, 11, 22, 8, 30)
        self.monitor._maybe_heartbeat()
        self.assertEqual(self.sent, [])
        self.monitor.clock = lambda: datetime(2026, 11, 22, 9, 5)
        self.monitor._maybe_heartbeat()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("监控还在跑", self.sent[0][2])
        self.assertNotIn("token", self.sent[0][2])
        self.monitor._maybe_heartbeat()
        self.assertEqual(len(self.sent), 1)

    def test_checkin_sends_once_per_day(self):
        done, text = describe_checkin({"success": "true", "message": "签到成功", "gain": 5, "continuous": 3})
        self.assertTrue(done)
        self.assertIn("获得 5", text)
        self.assertIn("连续 3", text)
        already, _already_text = describe_checkin({"success": False, "message": "今天已经签到过了"})
        self.assertTrue(already)
        failed, failed_text = describe_checkin({"success": False, "message": "请先登录"})
        self.assertFalse(failed)
        self.assertEqual(failed_text, "请先登录")

        self.monitor.clock = lambda: datetime(2026, 11, 25, 0, 5)
        self.monitor._maybe_checkin()
        self.assertEqual(self.box["checkin_calls"], 1)
        self.assertEqual(self.store.get_state("checkin_day"), "2026-11-25")
        self.assertIn("NodeSeek 签到", self.sent[-1][2])
        self.assertIn("获得 5", self.sent[-1][2])
        self.assertNotIn("session", self.sent[-1][2])
        self.monitor._maybe_checkin()
        self.assertEqual(self.box["checkin_calls"], 1)

    def test_checkin_failure_waits_and_already_signed_counts(self):
        self.monitor.clock = lambda: datetime(2026, 11, 25, 1, 0)
        self.box["checkin_error"] = CloudflareError("被 Cloudflare 拦住")
        self.monitor._maybe_checkin()
        self.assertEqual(self.box["checkin_calls"], 1)
        self.assertIsNone(self.store.get_state("checkin_day"))
        self.assertEqual(self.store.get_state("last_error") or "", "")
        self.assertIn("Cloudflare", self.store.get_state("checkin_last_error"))
        self.monitor._maybe_checkin()
        self.assertEqual(self.box["checkin_calls"], 1)

        self.store.delete_state("checkin_retry_at")
        self.box["checkin_error"] = None
        self.box["checkin"] = {"success": False, "message": "今日已签到"}
        self.monitor._maybe_checkin()
        self.assertEqual(self.store.get_state("checkin_day"), "2026-11-25")
        self.assertIn("已签", self.sent[-1][2])
        self.assertIsNone(self.store.get_state("checkin_retry_at"))

    def test_checkin_can_be_turned_off_and_skips_without_login(self):
        self.store.set_setting("checkin_enabled", "0")
        self.monitor._maybe_checkin()
        self.assertNotIn("checkin_calls", self.box)

        self.store.set_setting("checkin_enabled", "1")
        self.store.delete_setting("cookie")
        self.monitor._maybe_checkin()
        self.assertNotIn("checkin_calls", self.box)
        self.assertIn("还没有登录", self.store.get_state("checkin_last_error"))
        self.monitor._maybe_checkin()
        self.assertEqual(self.store.get_state("checkin_last_error"), "还没有登录，今天的签到先跳过。")

    def test_checkin_stays_done_when_telegram_fails(self):
        from ns_panel.telegram import TelegramError

        def fail(_token, _chat_id, _text):
            raise TelegramError("timeout")

        self.monitor.sender = fail
        self.monitor.clock = lambda: datetime(2026, 11, 25, 2, 0)
        self.monitor._maybe_checkin()
        self.assertEqual(self.store.get_state("checkin_day"), "2026-11-25")
        self.assertIn("获得 5", self.store.get_state("checkin_result"))

    def _arm_water(self, daily="1"):
        self.store.set_setting("auth_mode", "browser")
        self.store.set_setting("water_enabled", "1")
        self.store.set_setting("water_phrases", "路过看看\n学习了")
        self.store.set_setting("water_daily", daily)
        self.store.set_setting("checkin_enabled", "0")
        self.store.set_state("self_id", "9")
        self.monitor.browser_client_factory = lambda: FakeClient("browser", self.box)
        self.monitor.clock = lambda: datetime(2026, 11, 26, 9, 0)
        self.box["water_targets"] = [
            WaterTarget(15, "随便聊聊", 5, datetime(2026, 11, 26, 8, 0)),
        ]

    def test_water_is_off_by_default_and_waits_for_checkin(self):
        self.monitor._maybe_water()
        self.assertNotIn("comment_calls", self.box)
        self._arm_water()
        self.store.set_setting("checkin_enabled", "1")
        self.monitor._maybe_water()
        self.assertNotIn("comment_calls", self.box)
        self.store.set_state("checkin_day", "2026-11-26")
        self.monitor._maybe_water()
        self.assertEqual(self.box["comment_calls"], 1)

    def test_water_posts_one_then_stops_at_the_daily_cap(self):
        self._arm_water("1")
        self.monitor._maybe_water()
        self.assertEqual(self.box["comments"], [(15, "路过看看")])
        self.assertEqual(self.store.get_state("water_stopped"), "1")
        self.assertEqual(self.store.get_state("last_error") or "", "")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("NodeSeek 水贴", self.sent[0][2])
        self.assertIn("今天发了 1 条", self.sent[0][2])
        self.monitor._maybe_water()
        self.assertEqual(self.box["comment_calls"], 1)

    def test_water_skips_own_trade_keyword_and_old_posts(self):
        self._arm_water("3")
        self.store.set_setting("keywords", "家宽")
        now = datetime(2026, 11, 26, 9, 0)
        self.box["water_targets"] = [
            WaterTarget(11, "自己的帖", 9, now),
            WaterTarget(12, "只读", 2, now, read_only=True),
            WaterTarget(13, "旧帖", 3, datetime(2026, 11, 20, 0, 0)),
            WaterTarget(14, "美西家宽出", 4, now),
            WaterTarget(16, "交易帖", 6, now, category="trade"),
            WaterTarget(15, "随便聊聊", 5, now),
            WaterTarget(17, "再聊一句", 8, now),
        ]
        self.monitor._maybe_water()
        self.assertEqual(self.box["comments"], [(15, "路过看看")])
        self.assertEqual(self.sent, [])
        self.store.delete_state("water_next_at")
        self.monitor._maybe_water()
        self.assertEqual(self.box["comments"][1], (17, "学习了"))

    def test_water_site_limit_stops_until_tomorrow_without_touching_inbox_error(self):
        self._arm_water("3")
        self.box["comment_payload"] = {"success": False, "message": "今日水贴次数已达上限"}
        self.monitor._maybe_water()
        self.assertEqual(self.store.get_state("water_stopped"), "1")
        self.assertEqual(self.store.get_state("water_count"), "0")
        self.assertIn("上限", self.sent[0][2])
        self.assertEqual(self.store.get_state("last_error") or "", "")
        self.store.delete_state("water_next_at")
        self.monitor._maybe_water()
        self.assertEqual(self.box["comment_calls"], 1)

    def test_water_failure_waits_and_cookie_mode_does_not_comment(self):
        self._arm_water("3")
        self.box["comment_error"] = CloudflareError("被 Cloudflare 拦住")
        self.monitor._maybe_water()
        self.assertEqual(self.box["comment_calls"], 1)
        self.assertIsNone(self.store.get_state("water_stopped"))
        self.assertIn("Cloudflare", self.store.get_state("water_last_error"))
        self.assertEqual(self.store.get_state("last_error") or "", "")
        self.monitor._maybe_water()
        self.assertEqual(self.box["comment_calls"], 1)

        self.store.delete_state("water_next_at")
        self.store.delete_state("water_last_error")
        self.store.set_setting("auth_mode", "cookie")
        self.box.pop("comment_error")
        self.monitor._maybe_water()
        self.assertEqual(self.box["comment_calls"], 1)
        self.assertIn("登录浏览器", self.store.get_state("water_last_error"))

    def test_water_uses_the_selected_board(self):
        self._arm_water("1")
        self.store.set_setting("water_board", "日常")
        now = datetime(2026, 11, 26, 9, 0)
        self.box["water_targets"] = [
            WaterTarget(15, "灌水", 5, now, category="meaningless"),
            WaterTarget(21, "日常帖", 4, now, category="daily"),
        ]
        self.monitor._maybe_water()
        self.assertEqual(self.box["water_boards"], ["daily"])
        self.assertEqual(self.box["comments"], [(21, "路过看看")])

    def test_water_new_day_ignores_yesterdays_stop(self):
        self._arm_water("2")
        self.store.set_state("water_day", "2026-11-25")
        self.store.set_state("water_stopped", "1")
        self.store.set_state("water_count", "2")
        self.box["water_targets"] = [
            WaterTarget(15, "随便聊聊", 5, datetime(2026, 11, 26, 8, 0)),
            WaterTarget(17, "再聊一句", 8, datetime(2026, 11, 26, 8, 10)),
        ]
        self.monitor._maybe_water()
        self.assertEqual(self.box["comments"], [(15, "路过看看")])
        self.assertIsNone(self.store.get_state("water_stopped"))
        self.store.delete_state("water_next_at")
        self.monitor._maybe_water()
        self.assertEqual(self.box["comments"][1], (17, "学习了"))
        self.assertEqual(self.store.get_state("water_stopped"), "1")
        self.assertEqual(self.store.get_state("water_count"), "2")

    def test_baseline_reads_only_first_page(self):
        self.box["mention_payloads"] = {
            1: {"data": [mention_row(10, title="当前")]},
            2: {"data": [mention_row(1, title="更早的历史")]},
        }
        self.box["reply_payloads"] = {
            1: {"data": [mention_row(10, title="当前回复")]},
            2: {"data": [mention_row(1, title="更早的回复")]},
        }
        self.box["message_payloads"] = {
            1: PAYLOAD,
            2: {"msgArray": [message(4, "old", 9, "me", "历史", "2026-11-01 01:00:00", 10, 0)]},
        }
        first = self.monitor.check_once(wait=True)
        self.assertTrue(first["baseline"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.box["message_pages"], [1])
        self.assertEqual(self.box["mention_pages"], [1])
        self.assertEqual(self.box["reply_pages"], [1])
        self.assertNotIn(1, self.store.seen_mention_ids())

    def test_mention_on_later_page_is_pushed_without_history(self):
        self.box["mentions"] = {"data": [mention_row(10, title="旧帖")]}
        self.monitor.check_once(wait=True)
        self.box["mention_pages"].clear()
        self.box["mention_payloads"] = {
            1: {"data": [mention_row(40, title="最新")]},
            2: {"data": [mention_row(30, name="alice", title="被挤下去的新@"), mention_row(10, title="旧帖")]},
            3: {"data": [mention_row(1, title="历史")]},
        }
        second = self.monitor.check_once(wait=True)
        self.assertEqual(self.box["mention_pages"], [1, 2])
        self.assertEqual(second.get("mentions_notified"), 2)
        text = "\n".join(item[2] for item in self.sent)
        self.assertIn("最新", text)
        self.assertIn("被挤下去的新@", text)
        self.assertNotIn("历史", text)
        self.assertNotIn("markViewed", text)

    def test_old_private_message_on_later_page_is_not_pushed(self):
        self.monitor.check_once(wait=True)
        self.assertEqual(self.sent, [])
        watermark = self.store.get_state("pm_watermark")
        self.assertTrue(watermark)
        self.box["message_pages"].clear()
        self.box["message_payloads"] = {
            1: {"msgArray": [message(8, "new", 9, "me", "刚来的", "2026-11-08 18:00:00", 200, 0)]},
            2: {
                "msgArray": [
                    message(2, "hunlun", 9, "me", "还是那条", "2026-11-08 09:32:09", 100, 0),
                    message(4, "old", 9, "me", "更早的历史", "2026-11-01 01:00:00", 10, 0),
                    message(6, "late", 9, "me", "挡住之后才看到", "2026-11-08 17:00:00", 180, 0),
                ]
            },
        }
        second = self.monitor.check_once(wait=True)
        self.assertEqual(second["notified"], 2)
        text = "\n".join(item[2] for item in self.sent)
        self.assertIn("刚来的", text)
        self.assertIn("挡住之后才看到", text)
        self.assertNotIn("更早的历史", text)
        self.assertEqual(self.box["message_pages"], [1, 2, 3])

    def test_known_peer_on_later_page_is_still_pushed(self):
        self.monitor.check_once(wait=True)
        self.box["message_pages"].clear()
        self.box["message_payloads"] = {
            1: {"msgArray": [message(2, "hunlun", 9, "me", "第一页的新私信", "2026-11-08 18:00:00", 210, 0)]},
            2: {"msgArray": [message(3, "kagent93", 9, "me", "挤到第二页", "2026-11-08 17:00:00", 190, 0)]},
            3: {"msgArray": [message(4, "old", 9, "me", "旧的", "2026-11-01 01:00:00", 10, 0)]},
        }
        second = self.monitor.check_once(wait=True)
        self.assertEqual(self.box["message_pages"], [1, 2, 3])
        text = "\n".join(item[2] for item in self.sent)
        self.assertIn("第一页的新私信", text)
        self.assertIn("挤到第二页", text)
        self.assertNotIn("旧的", text)
        self.assertEqual(second["notified"], 2)
        markup = open_markup(pm_open_links([
            {"peer_id": 2, "peer_name": "hunlun"},
            {"peer_id": 3, "peer_name": "kagent93"},
        ]))
        self.assertIn("nodeseek.com", markup["inline_keyboard"][0][0]["url"])

    def test_unfinished_private_message_catchup_keeps_watermark_and_resumes(self):
        self.monitor.check_once(wait=True)
        watermark = self.store.get_state("pm_watermark")
        self.box["message_pages"].clear()
        payloads = {}
        for page in range(1, 6):
            payloads[page] = {
                "msgArray": [message(20 + page, f"p{page}", 9, "me", f"第{page}页", "2026-11-08 12:00:00", 300 + page, 0)]
            }
        payloads[6] = {
            "msgArray": [message(40, "late", 9, "me", "第六页才看到", "2026-11-08 11:30:00", 280, 0)]
        }
        payloads[7] = {"msgArray": [message(4, "old", 9, "me", "旧的", "2026-11-01 01:00:00", 10, 0)]}
        self.box["message_payloads"] = payloads
        self.monitor.check_once(wait=True)
        self.assertEqual(self.store.get_state("pm_watermark"), watermark)
        self.assertEqual(self.store.get_state("pm_resume_page"), "6")
        self.assertEqual(self.box["message_pages"], [1, 2, 3, 4, 5])
        self.box["message_pages"].clear()
        self.sent.clear()
        self.monitor.check_once(wait=True)
        text = "\n".join(item[2] for item in self.sent)
        self.assertIn("第六页才看到", text)
        self.assertIsNone(self.store.get_state("pm_resume_page"))
        self.assertGreater(self.store.get_state("pm_watermark"), watermark)

    def test_mention_catchup_resumes_until_old_id(self):
        self.box["mentions"] = {"data": [mention_row(10, title="旧帖")]}
        self.monitor.check_once(wait=True)
        self.box["mention_pages"].clear()
        self.sent.clear()
        payloads = {page: {"data": [mention_row(100 + page, title=f"新{page}")]} for page in range(1, 6)}
        payloads[6] = {"data": [mention_row(50, name="alice", title="补上的"), mention_row(10, title="旧帖")]}
        self.box["mention_payloads"] = payloads
        first = self.monitor.check_once(wait=True)
        self.assertEqual(first.get("mentions_notified"), 5)
        self.assertEqual(self.store.get_state("mentions_resume_page"), "6")
        self.assertEqual(self.store.get_state("mentions_sync_until"), "10")
        self.box["mention_pages"].clear()
        self.sent.clear()
        second = self.monitor.check_once(wait=True)
        self.assertEqual(self.box["mention_pages"], [1, 6])
        self.assertEqual(second.get("mentions_notified"), 1)
        self.assertIn("补上的", self.sent[0][2])
        self.assertIsNone(self.store.get_state("mentions_resume_page"))

    def test_reply_catchup_resumes_until_old_id(self):
        self.box["replies"] = {"replys": [mention_row(10, title="旧帖")]}
        self.monitor.check_once(wait=True)
        self.box["reply_pages"].clear()
        self.sent.clear()
        payloads = {page: {"data": [mention_row(100 + page, title=f"新{page}")]} for page in range(1, 6)}
        payloads[6] = {"data": [mention_row(50, name="alice", title="补上的回复"), mention_row(10, title="旧帖")]}
        self.box["reply_payloads"] = payloads
        first = self.monitor.check_once(wait=True)
        self.assertEqual(first.get("replies_notified"), 5)
        self.assertEqual(self.store.get_state("replies_resume_page"), "6")
        self.box["reply_pages"].clear()
        self.sent.clear()
        second = self.monitor.check_once(wait=True)
        self.assertEqual(self.box["reply_pages"], [1, 6])
        self.assertEqual(second.get("replies_notified"), 1)
        self.assertIn("补上的回复", self.sent[0][2])
        self.assertIsNone(self.store.get_state("replies_resume_page"))

    def test_telegram_buttons_read_local_rows_and_hide_secrets(self):
        self.store.upsert_conversation(
            parse_message_list(PAYLOAD, 9)[0]
        )
        self.store.upsert_conversation(
            Conversation(4, "newerpeer", "刚到的", "2026-11-24 18:00:00", 900, 4, 9, 0)
        )
        self.store.set_state("last_error", "Cookie session=abc123 失效")
        view = answer_for("私信", self.store, False)
        self.assertLess(view["text"].find("hunlun"), view["text"].find("newerpeer"))
        self.assertIn("https://www.nodeseek.com/notification", view["text"])
        keyboard = view["reply_markup"]["keyboard"]
        self.assertEqual([button["text"] for button in keyboard[0]], ["私信", "提到我"])
        self.assertEqual([button["text"] for button in keyboard[1]], ["回复", "关键词"])
        self.assertEqual([button["text"] for button in keyboard[2]], ["状态"])
        self.assertEqual(keyboard[0][0]["style"], "primary")
        self.assertNotIn("style", keyboard[1][0])
        self.assertEqual(view["reply_markup"]["input_field_placeholder"], "点按钮查看记录")
        self.assertEqual(answer_for("状态", self.store, False)["text"].count("session=abc123"), 0)
        status = answer_for("状态", self.store, False)["text"]
        self.assertIn("没在跑", status)
        self.assertIn("今日签到：还没有", status)
        self.assertIn("今日水贴：关着", status)
        self.assertNotIn("水贴", " ".join(button["text"] for row in keyboard for button in row))
        self.assertIsNone(answer_for("随便说", self.store, False))
        self.assertEqual(answer_for("回复", self.store, True)["text"], "还没有回复。")

    def test_telegram_can_add_replace_and_delete_keywords(self):
        added = answer_for("加 美西家宽 @交易 -已出", self.store, False)
        self.assertIn("已加上：美西家宽 @交易 -已出", added["text"])
        self.assertIn("不会补推", added["text"])
        self.assertEqual(self.store.get_setting("keywords"), "美西家宽 @交易 -已出")
        replaced = answer_for("添加 美西家宽 @拼车 -已出", self.store, False)
        self.assertIn("已修改：美西家宽 @拼车 -已出", replaced["text"])
        self.assertNotIn("@交易", self.store.get_setting("keywords"))
        missing = answer_for("删 没有的词", self.store, False)
        self.assertIn("没有这个关键词", missing["text"])
        self.assertIn("美西家宽", self.store.get_setting("keywords"))
        removed = answer_for("删除 美西家宽", self.store, False)
        self.assertIn("已删掉：美西家宽", removed["text"])
        self.assertEqual(self.store.get_setting("keywords"), "")
        bad = answer_for("加 家宽 @火星", self.store, False)
        self.assertIn("没有这个板块", bad["text"])
        self.assertEqual(self.store.get_setting("keywords"), "")
        self.store.set_setting("keywords", "\n".join(f"词{i}" for i in range(40)))
        full = answer_for("加 再来一个", self.store, False)
        self.assertIn("最多 40 个", full["text"])
        self.assertEqual(len(self.store.get_setting("keywords").splitlines()), 40)

    def test_dead_poll_thread_restarts_only_while_enabled(self):
        self.store.set_setting("enabled", "1")
        self.monitor.start()
        self.assertTrue(self.monitor.running)
        self.monitor.stop()
        self.monitor._thread.join(timeout=2)
        self.assertFalse(self.monitor.running)
        self.store.set_setting("enabled", "0")
        self.monitor.ensure_started()
        self.assertFalse(self.monitor.running)
        self.store.set_setting("enabled", "1")
        self.monitor.ensure_started()
        self.assertTrue(self.monitor.running)
        self.monitor.stop()
        self.monitor._thread.join(timeout=2)


class RuntimeTest(unittest.TestCase):
    def test_find_edge_uses_override(self):
        path = Path(tempfile.gettempdir()) / "ns-browser-bin-test"
        path.write_text("", encoding="utf-8")
        os.environ["NS_BROWSER_BIN"] = str(path)
        try:
            self.assertEqual(find_edge(), str(path))
        finally:
            os.environ.pop("NS_BROWSER_BIN", None)
            path.unlink(missing_ok=True)

    def test_docker_defaults_to_browser_login(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(Path(tmp.name) / "panel.db")
        os.environ["NS_RUNTIME"] = "docker"
        try:
            self.assertEqual(auth_mode(store), "browser")
            store.set_setting("auth_mode", "cookie")
            self.assertEqual(auth_mode(store), "cookie")
        finally:
            os.environ.pop("NS_RUNTIME", None)
            tmp.cleanup()

    def test_panel_password_rejects_anonymous(self):
        os.environ["NS_PANEL_USER"] = "admin"
        os.environ["NS_PANEL_PASSWORD"] = "secret-pass"
        try:
            self.assertFalse(panel_authorized(None))
            self.assertFalse(panel_authorized("Basic YWRtaW46d3Jvbmc="))
            token = base64.b64encode(b"admin:secret-pass").decode("ascii")
            self.assertTrue(panel_authorized(f"Basic {token}"))
        finally:
            os.environ.pop("NS_PANEL_USER", None)
            os.environ.pop("NS_PANEL_PASSWORD", None)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name))
        self.httpd = create_httpd(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_pages_and_settings_hide_secrets(self):
        status, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("开始监控", html)
        self.assertIn("获取 Chat ID", html)
        self.assertIn("打开登录浏览器", html)
        self.assertIn("面板账号", html)
        self.assertIn("退出登录", html)
        self.assertIn("提到我", html)
        self.assertIn("docker-desktop-note", html)
        self.assertIn("每天自动签到", html)
        self.assertIn("水贴", html)
        self.assertIn("每天在所选板块发短回复", html)
        self.assertIn('id="water-board"', html)
        self.assertIn('id="water-daily" name="water_daily" type="number" min="1" max="50"', html)
        status, css = self.get("/static/app.css")
        self.assertEqual(status, 200)
        self.assertIn(".sidebar", css)
        code, body = self.post("/api/settings", {"cookie": "Cookie: session=abcdef", "interval": 45, "tg_chat_id": "42"})
        self.assertEqual(code, 200)
        self.assertNotIn("session", json.dumps(body))
        self.assertEqual(body["cookie_length"], len("session=abcdef"))
        self.assertEqual(body["interval"], 45)
        code, short = self.post("/api/settings", {"interval": 10})
        self.assertEqual(code, 200)
        self.assertEqual(short["interval"], 10)
        code, rejected = self.post("/api/settings", {"interval": 5})
        self.assertEqual(code, 400)
        self.assertIn("10", rejected["error"])
        code, keywords = self.post("/api/settings", {"keyword_interval": 60})
        self.assertEqual(code, 200)
        self.assertEqual(keywords["keyword_interval"], 60)
        code, rejected = self.post("/api/settings", {"keyword_interval": 10})
        self.assertEqual(code, 400)
        self.assertIn("30", rejected["error"])
        self.assertEqual(body["tg_chat_id"], "42")
        self.assertTrue(body["checkin_enabled"])
        code, off = self.post("/api/settings", {"checkin_enabled": False})
        self.assertEqual(code, 200)
        self.assertFalse(off["checkin_enabled"])
        code, kept_off = self.post("/api/settings", {"interval": 30})
        self.assertEqual(code, 200)
        self.assertFalse(kept_off["checkin_enabled"])
        code, on = self.post("/api/settings", {"checkin_enabled": True})
        self.assertEqual(code, 200)
        self.assertTrue(on["checkin_enabled"])
        self.assertFalse(on["water_enabled"])
        code, missing = self.post("/api/settings", {"water_enabled": True, "water_phrases": "只有一句"})
        self.assertEqual(code, 400)
        self.assertIn("两句", missing["error"])
        code, tight = self.post("/api/settings", {"water_enabled": True, "water_gap": 5, "water_phrases": "甲\n乙"})
        self.assertEqual(code, 400)
        self.assertIn("10", tight["error"])
        code, watered = self.post(
            "/api/settings",
            {"water_enabled": True, "water_daily": 3, "water_gap": 10, "water_phrases": "路过看看\n学习了"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(watered["water_enabled"])
        self.assertEqual(watered["water_daily"], 3)
        self.assertEqual(watered["water_phrases"], "路过看看\n学习了")
        code, many = self.post(
            "/api/settings",
            {"water_enabled": True, "water_daily": 20, "water_gap": 20, "water_phrases": "路过看看\n学习了"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(many["water_daily"], 20)
        self.assertEqual(many["water_board"], "meaningless")
        code, boarded = self.post(
            "/api/settings",
            {"water_board": "日常"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(boarded["water_board"], "daily")
        names = [item["name"] for item in boarded["water_boards"]]
        self.assertIn("内版", names)
        self.assertIn("日常", names)
        code, unknown = self.post("/api/settings", {"water_board": "不存在"})
        self.assertEqual(code, 400)
        self.assertIn("没有这个板块", unknown["error"])
        code, too_many = self.post(
            "/api/settings",
            {"water_enabled": True, "water_daily": 51, "water_phrases": "甲\n乙"},
        )
        self.assertEqual(code, 400)
        self.assertIn("50", too_many["error"])
        code, kept_water = self.post("/api/settings", {"interval": 40})
        self.assertEqual(code, 200)
        self.assertTrue(kept_water["water_enabled"])
        self.assertEqual(kept_water["water_board"], "daily")
        code, status_body = self.post("/api/monitor/start", {})
        self.assertEqual(code, 400)
        self.assertIn("Telegram", status_body["error"])
        code, saved = self.post("/api/settings", {"tg_token": "123:abc", "interval": 20})
        self.assertEqual(code, 200)
        self.assertTrue(saved["has_token"])
        code, kept = self.post("/api/settings", {"tg_token": "not-a-token", "interval": 15, "keyword_interval": 45})
        self.assertEqual(code, 200)
        self.assertEqual(kept["interval"], 15)
        self.assertEqual(kept["keyword_interval"], 45)
        self.assertTrue(kept["has_token"])
        _status, page_status = self.get("/api/status")
        data = json.loads(page_status)
        self.assertFalse(data["running"])
        self.assertNotIn("session", page_status)
        status, posts_page = self.get("/api/posts")
        self.assertEqual(status, 200)
        self.assertNotIn("session", posts_page)
        self.assertIn("日常", posts_page)
        code, saved = self.post("/api/keywords", {"keywords": "vps，便宜", "boards": ["trade"]})
        self.assertEqual(code, 200)
        self.assertEqual(saved["keywords"], "vps\n便宜")
        self.assertEqual(saved["boards"], ["trade"])
        self.assertNotIn("session", json.dumps(saved))
        status, mentions_page = self.get("/api/mentions")
        self.assertEqual(status, 200)
        self.assertNotIn("session", mentions_page)
        self.assertIn("baseline_done", mentions_page)


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <title>NodeSeek</title>
    <item>
      <title><![CDATA[便宜 VPS 出]]></title>
      <description><![CDATA[日本线路，年付]]></description>
      <link>https://www.nodeseek.com/post-1040390-1</link>
      <guid isPermaLink="false">1040390</guid>
      <category><![CDATA[trade]]></category>
      <dc:creator><![CDATA[朝阳群众]]></dc:creator>
      <pubDate>Sun, 08 Nov 2026 15:16:55 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

CATEGORY_HTML = """
<ul class="post-list">
  <li class="post-list-item">
    <img alt="酒神">
    <div class="post-title"><a href="/post-88-1">交易版块规定</a></div>
    <span class="info-author"><a href="/space/9">酒神</a></span>
  </li>
  <li class="post-list-item">
    <div class="post-title"><a href="/post-1040402-1">新的拼车帖</a></div>
    <span class="info-author"><a href="/space/3">kagent93</a></span>
  </li>
</ul>
"""


def make_post(post_id, title, excerpt="", category="daily"):
    return Post(
        post_id=post_id,
        title=title,
        link=f"https://www.nodeseek.com/post-{post_id}-1",
        category=category,
        author="tester",
        published_at="2026-11-08 12:00",
        excerpt=excerpt,
    )


WATER_HTML = """
<ul class="post-list">
  <li class="post-list-item">
    <a href="/space/9"><img alt="me" data-uid="9"></a>
    <div class="post-title"><a href="/post-11-1">自己的帖</a></div>
    <time datetime="2026-11-26T01:00:00.000Z"></time>
    <a href="/categories/meaningless">无意义</a>
  </li>
  <li class="post-list-item">
    <img data-uid="2">
    <div class="post-title"><a href="/post-12-1">只读帖</a> <span class="nsk-badge read-only">只读</span></div>
    <time datetime="2026-11-26T01:00:00.000Z"></time>
    <a href="/categories/meaningless">无意义</a>
  </li>
  <li class="post-list-item">
    <img data-uid="4">
    <div class="post-title"><a href="/post-15-1" target="">随便聊聊</a></div>
    <a href="/post-15-1#1"><time datetime="2026-11-25T16:55:53.000Z">8h ago</time></a>
    <a href="/categories/meaningless">无意义</a>
  </li>
  <li class="post-list-item">
    <img data-uid="6">
    <div class="post-title"><a href="/post-16-1">交易帖</a></div>
    <time datetime="2026-11-26T01:00:00.000Z"></time>
    <a href="/categories/trade">交易</a>
  </li>
</ul>
"""


class WaterParseTest(unittest.TestCase):
    def test_board_parser_keeps_author_activity_and_read_only(self):
        found = parse_water_board(WATER_HTML)
        self.assertEqual([item.post_id for item in found], [11, 12, 15, 16])
        self.assertEqual(found[0].author_id, 9)
        self.assertTrue(found[1].read_only)
        self.assertFalse(found[2].read_only)
        self.assertEqual(found[2].category, "meaningless")
        self.assertEqual(found[3].category, "trade")
        self.assertIsNotNone(found[2].active_at)
        chosen = pick_water_target(
            found,
            self_id=9,
            watered=set(),
            now=datetime(2026, 11, 26, 9, 0, tzinfo=found[2].active_at.tzinfo),
            rules=[],
            excludes=[],
            boards=["meaningless"],
        )
        self.assertEqual(chosen.post_id, 15)

    def test_comment_result_stops_on_site_limits(self):
        self.assertTrue(describe_comment({"success": True, "message": "评论成功"}).ok)
        stopped = describe_comment({"success": False, "message": "今日水贴次数已达上限"})
        self.assertTrue(stopped.stop_today)
        self.assertFalse(stopped.ok)
        for word in ("评论过", "太频繁", "重复"):
            self.assertTrue(describe_comment({"success": False, "message": word}).stop_today)
        retry = describe_comment({"success": False, "message": "请稍后再试"})
        self.assertFalse(retry.ok)
        self.assertFalse(retry.stop_today)
        phrases = parse_phrases("  路过看看  \n路过看看\n\n学习了 ")
        self.assertEqual(phrases, ["路过看看", "学习了"])


class PostParseTest(unittest.TestCase):
    def test_keywords_rss_and_plan(self):
        self.assertEqual(parse_keywords("vps，便宜\nvps; 年付"), ["vps", "便宜", "年付"])
        posts = parse_rss(RSS)
        self.assertEqual(posts[0].post_id, 1040390)
        self.assertEqual(posts[0].category, "trade")
        self.assertEqual(posts[0].author, "朝阳群众")
        self.assertIn("日本线路", posts[0].excerpt)
        html_posts = parse_category_html(CATEGORY_HTML, "trade")
        self.assertEqual([item.post_id for item in html_posts], [88, 1040402])
        self.assertEqual(html_posts[1].author, "kagent93")

        current = [make_post(10, "旧的 VPS"), make_post(11, "闲聊")]
        first = plan_posts(current, set(), False, ["vps"])
        self.assertEqual([item.push for item in first], [False, False])
        seen = {10, 11}
        newer = [make_post(12, "又一台 VPS"), make_post(9, "更旧的 VPS 被顶上来")]
        second = plan_posts(newer, seen, True, ["vps"])
        self.assertEqual([item.push for item in second], [True, False])
        self.assertEqual(plan_posts([make_post(13, "hello")], seen, True, ["VPS"])[0].push, False)

    def test_keyword_rules_limit_board_and_exclude(self):
        rules = parse_keyword_rules("美西家宽 @交易 -已出\n人找车 @拼车")
        self.assertEqual(rules[0].boards, ("trade",))
        self.assertEqual(rules[0].excludes, ("已出",))
        self.assertEqual(rules[1].boards, ("carpool",))
        posts = [
            make_post(1, "美西家宽来了", category="trade"),
            make_post(2, "美西家宽闲聊", category="daily"),
            make_post(3, "美西家宽已出", category="trade"),
            make_post(4, "美西家宽出掉", category="trade"),
            make_post(5, "人找车", category="carpool"),
        ]
        plans = plan_posts(posts, set(), True, rules, ["出掉"], ["trade", "daily", "carpool"])
        pushed = [item.post.post_id for item in plans if item.push]
        self.assertEqual(pushed, [1, 5])
        inner = parse_keyword_rules("成人 @内版\n灌水 @无意义")
        self.assertEqual(inner[0].boards, ("meaningless",))
        self.assertEqual(inner[1].boards, ("meaningless",))


class ScheduleTest(unittest.TestCase):
    def test_quiet_window_crosses_midnight(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 11, 22, 23, 0), "23:00", "08:00"))
        self.assertTrue(in_quiet_hours(datetime(2026, 11, 23, 7, 59), "23:00", "08:00"))
        self.assertFalse(in_quiet_hours(datetime(2026, 11, 23, 8, 0), "23:00", "08:00"))
        self.assertFalse(in_quiet_hours(datetime(2026, 11, 22, 22, 59), "23:00", "08:00"))
        self.assertTrue(heartbeat_due(datetime(2026, 11, 22, 9, 0), 9, ""))
        self.assertFalse(heartbeat_due(datetime(2026, 11, 22, 8, 59), 9, ""))
        self.assertFalse(heartbeat_due(datetime(2026, 11, 22, 10, 0), 9, "2026-11-22"))

    def test_reply_parser_accepts_replys_field(self):
        rows = parse_replies({"replys": [mention_row(7, name="<b>bob</b>", title="<i>回帖</i>")]})
        self.assertEqual(rows[0].commenter_name, "bob")
        self.assertEqual(rows[0].title, "回帖")
        text = format_reply_alerts(
            [{"commenter_name": "bob", "title": "回帖", "created_at": "2026-11-22 09:00", "link": rows[0].link, "post_id": 100, "floor_id": 3}]
        )
        self.assertIn("bob 回复了你的帖子", text)
        self.assertNotIn("<", text)


class KeywordMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "panel.db")
        self.store.set_setting("cookie", "session=abc123")
        self.store.set_setting("tg_token", "1:token")
        self.store.set_setting("tg_chat_id", "99")
        self.store.set_setting("keywords", "vps")
        self.store.set_setting("boards", "daily")
        self.sent = []
        self.box = {
            "payload": PAYLOAD,
            "account": extract_member({"member_id": 9, "member_name": "me"}),
            "posts": [make_post(20, "历史 VPS 帖")],
            "fetched": ["daily"],
        }
        self.feed_calls = []

        def feed(_cookie, boards):
            self.feed_calls.append(list(boards))
            return list(self.box["posts"]), list(self.box.get("errors") or []), list(self.box["fetched"])

        self.monitor = Monitor(
            self.store,
            client_factory=lambda cookie: FakeClient(cookie, self.box),
            sender=lambda token, chat_id, text: self.sent.append((token, chat_id, text)),
            feed_fetcher=feed,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_post_baseline_then_push_once_and_ignore_old(self):
        first = self.monitor.scan_posts()
        self.assertTrue(first["posts_baseline"])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.store.list_hits()), 1)
        self.assertEqual(self.store.list_hits()[0]["status"], "listed")

        self.box["posts"] = [make_post(21, "新出 VPS"), make_post(19, "旧 VPS 被顶上来")]
        second = self.monitor.scan_posts()
        self.assertEqual(second["notified"], 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("新出 VPS", self.sent[0][2])
        self.assertNotIn("旧 VPS", self.sent[0][2])

        third = self.monitor.scan_posts()
        self.assertEqual(third["notified"], 0)
        self.assertEqual(len(self.sent), 1)

    def test_quiet_hours_hold_keyword_until_morning(self):
        self.store.set_setting("quiet_enabled", "1")
        self.store.set_setting("quiet_start", "23:00")
        self.store.set_setting("quiet_end", "08:00")
        self.monitor.clock = lambda: datetime(2026, 11, 22, 23, 30)
        self.monitor.scan_posts()
        self.box["posts"] = [make_post(21, "夜里的 VPS")]
        held = self.monitor.scan_posts()
        self.assertEqual(held["held"], 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.pending_hits()[0]["title"], "夜里的 VPS")
        self.monitor.clock = lambda: datetime(2026, 11, 23, 8, 0)
        sent = self.monitor.scan_posts()
        self.assertEqual(sent["notified"], 1)
        self.assertIn("夜里的 VPS", self.sent[0][2])

    def test_keyword_change_lists_seen_post_without_push(self):
        self.monitor.scan_posts()
        self.store.set_setting("keywords", "历史")
        self.store.delete_state("keywords_sig")
        result = self.monitor.scan_posts()
        self.assertEqual(result["notified"], 0)
        self.assertEqual(self.sent, [])
        hits = self.store.list_hits()
        self.assertTrue(any(item["status"] == "listed" and "历史" in item["keywords"] for item in hits))

    def test_rss_connect_failure_backs_off_and_logs_once(self):
        self.box["posts"] = []
        self.box["fetched"] = []
        self.box["errors"] = [
            "RSS：请求 NodeSeek 失败：curl: (28) Failed to connect to rss.nodeseek.com:443 after 21257 ms"
        ]
        first = self.monitor._scheduled_keywords()
        self.assertFalse(first["ok"])
        self.assertIn("过几分钟再试", first["error"])
        calls = len(self.feed_calls)
        self.box["errors"] = [
            "RSS：请求 NodeSeek 失败：curl: (28) Failed to connect to rss.nodeseek.com:443 after 19999 ms"
        ]
        second = self.monitor._scheduled_keywords()
        self.assertEqual(len(self.feed_calls), calls)
        self.assertIn("过几分钟再试", second["error"])
        logged = [row["message"] for row in self.store.list_logs() if "RSS" in row["message"] or "连不上" in row["message"]]
        self.assertEqual(logged, ["没能拉到帖子：公开 RSS 暂时连不上，过几分钟再试。"])
        self.store.set_state("rss_until", str(time.time() - 1))
        self.box["errors"] = []
        self.box["posts"] = [make_post(21, "新出 VPS")]
        self.box["fetched"] = ["daily"]
        self.monitor.scan_posts()
        self.assertIsNone(self.store.get_state("rss_until"))

    def test_feed_error_does_not_fail_private_message_check(self):
        self.monitor.check_once(wait=True)
        self.box["posts"] = []
        self.box["fetched"] = []
        self.box["errors"] = ["HTTP 503"]
        result = self.monitor.check_once(wait=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "检查完成，没有新私信")
        self.assertIn("HTTP 503", result["posts_error"])
        self.assertEqual(self.sent, [])

    def test_rss_window_rollover_alerts_once_and_overlap_pulls_in(self):
        def feed(_cookie, _boards):
            return list(self.box["posts"]), [], list(self.box["fetched"]), list(self.box.get("window") or [])

        self.monitor.feed_fetcher = feed
        self.box["window"] = [1, 2, 3]
        self.monitor.scan_posts()
        self.box["window"] = [3, 4, 5]
        overlapped = self.monitor.scan_posts()
        self.assertTrue(overlapped["pull_in"])
        self.assertEqual(self.sent, [])

        self.box["window"] = [10, 11, 12]
        self.box["posts"] = [make_post(21, "新出 VPS")]
        self.monitor.scan_posts()
        gap = [text for _token, _chat, text in self.sent if "整页换新" in text]
        self.assertEqual(len(gap), 1)
        self.assertNotIn("1:token", gap[0])
        self.monitor.scan_posts()
        gap = [text for _token, _chat, text in self.sent if "整页换新" in text]
        self.assertEqual(len(gap), 1)


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NS_PANEL_USER"] = "admin"
        os.environ["NS_PANEL_PASSWORD"] = "secret-pass"
        self.app = App(Path(self.tmp.name))
        self.httpd = create_httpd(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        os.environ.pop("NS_PANEL_USER", None)
        os.environ.pop("NS_PANEL_PASSWORD", None)
        self.tmp.cleanup()

    def open(self, path, payload=None, cookie=""):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read().decode("utf-8"), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), exc.headers

    def test_login_page_and_account_change(self):
        self.app.store.set_setting("tg_token", "123:secret-token")
        self.app.store.set_setting("cookie", "session=hidden-cookie")
        status, body, _headers = self.open("/health")
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["ok"], True)
        self.assertEqual(set(health), {"ok", "version", "running"})
        self.assertNotIn("secret-token", body)
        self.assertNotIn("hidden-cookie", body)
        self.assertNotIn("secret-pass", body)

        status, body, headers = self.open("/")
        self.assertEqual(status, 200)
        self.assertIn("登录这个面板", body)
        self.assertNotIn("开始监控", body)
        self.assertNotIn("WWW-Authenticate", str(headers))

        status, body, _headers = self.open("/api/login", {"user": "admin", "password": "wrong-pass"})
        self.assertEqual(status, 401)
        self.assertNotIn("wrong-pass", body)
        self.assertIn("账号或密码不对", body)

        status, body, headers = self.open("/api/login", {"user": "admin", "password": "secret-pass"})
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie") or ""
        self.assertIn("ns_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        session = cookie.split(";", 1)[0]

        status, body, _headers = self.open("/api/settings", cookie=session)
        self.assertEqual(status, 200)
        settings = json.loads(body)
        self.assertEqual(settings["panel_user"], "admin")
        self.assertNotIn("secret-pass", body)
        self.assertNotIn('"panel_password"', body)

        status, body, _headers = self.open("/api/panel-account", {
            "panel_user": "jaking",
            "current_password": "secret-pass",
            "panel_password": "easy-one",
            "panel_password_confirm": "nope",
        }, cookie=session)
        self.assertEqual(status, 400)
        self.assertIn("不一致", body)

        status, body, _headers = self.open("/api/panel-account", {
            "panel_user": "jaking",
            "current_password": "secret-pass",
            "panel_password": "easy-one",
            "panel_password_confirm": "easy-one",
        }, cookie=session)
        self.assertEqual(status, 200)
        self.assertNotIn("easy-one", body)
        self.assertIn("jaking", body)
        stored = self.app.store.get_setting("panel_password") or ""
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn("easy-one", stored)

        status, _body, _headers = self.open("/api/login", {"user": "admin", "password": "secret-pass"})
        self.assertEqual(status, 401)
        status, _body, headers = self.open("/api/login", {"user": "jaking", "password": "easy-one"})
        self.assertEqual(status, 200)
        self.assertIn("ns_session=", headers.get("Set-Cookie") or "")


class CatchUpPathTest(unittest.TestCase):
    def test_paths_stay_on_list_endpoints(self):
        self.assertEqual(notification_path("mention", 1), "/api/notification/at-me/list")
        self.assertEqual(notification_path("mention", 2), "/api/notification/at-me/list?page=2")
        self.assertEqual(notification_path("reply", 3), "/api/notification/reply-to-me/list?page=3")
        self.assertEqual(notification_path("message", 2), "/api/notification/message/list?page=2")
        self.assertNotIn("markViewed", notification_path("mention", 4))

        class Recording(NodeSeekClient):
            def __init__(self):
                super().__init__("a=b")
                self.paths = []

            def get_json(self, path):
                self.paths.append(path)
                return {"data": []}

        client = Recording()
        client.get_mentions(2)
        client.get_replies(1)
        client.get_message_list(3)
        self.assertEqual(
            client.paths,
            [
                "/api/notification/at-me/list?page=2",
                "/api/notification/reply-to-me/list",
                "/api/notification/message/list?page=3",
            ],
        )
        self.assertFalse(any("markViewed" in path for path in client.paths))


class WatchTest(unittest.TestCase):
    def test_down_alert_is_sent_once_and_hides_token(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(Path(tmp.name) / "panel.db")
        store.set_setting("tg_token", "123:secret-token")
        store.set_setting("tg_chat_id", "99")
        store.set_setting("enabled", "1")
        sent = []
        logs = []

        def opener(_request, timeout=5):
            del timeout
            raise urllib.error.URLError("down")

        def sender(token, chat_id, text):
            sent.append((token, chat_id, text))

        state = {"fails": 0, "thread_fails": 0, "alert": ""}
        for _ in range(3):
            watch_once(store.path, "http://panel/health", state, opener, sender, logs.append, fails_needed=3)
        self.assertEqual(len(sent), 1)
        self.assertIn("没有响应", sent[0][2])
        self.assertNotIn("secret-token", sent[0][2])
        self.assertNotIn("secret-token", "\n".join(logs))
        watch_once(store.path, "http://panel/health", state, opener, sender, logs.append, fails_needed=3)
        self.assertEqual(len(sent), 1)

        class Response:
            status = 200

            def read(self, _limit=4000):
                return b'{"ok": true, "version": "0.6.0", "running": true}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        recovered = watch_once(
            store.path,
            "http://panel/health",
            state,
            lambda _request, timeout=5: Response(),
            sender,
            logs.append,
            fails_needed=3,
        )
        self.assertEqual(recovered["alert"], "")
        self.assertIn("已恢复", sent[1][2])
        self.assertNotIn("secret-token", sent[1][2])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
