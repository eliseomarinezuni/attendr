from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant.canvas_client import (
    AcademicItem,
    Announcement,
    CanvasSnapshot,
)
from academic_assistant.notifier import (
    DiscordConfigurationError,
    DiscordNotificationError,
    DiscordNotifier,
    DiscordQuietHoursError,
    DiscordRateLimitError,
)
from academic_assistant.state_store import StateStore

UTC = timezone.utc
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WEBHOOK_URL = "https://discord.com/api/webhooks/123456/test-token"


class FakeResponse:
    def __init__(self, status_code=204, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def make_item(**overrides):
    due_at = NOW + timedelta(hours=24)
    values = {
        "uid": "canvas:assignment:1:10",
        "source": "assignment",
        "source_id": "10",
        "course_id": 1,
        "course_name": "Algorithms",
        "title": "Problem Set 1",
        "kind": "assignment",
        "due_at": due_at,
        "due_at_local": due_at.astimezone(),
        "end_at": None,
        "all_day": False,
        "html_url": "https://canvas.example/courses/1/assignments/10",
        "updated_at": NOW,
        "points_possible": 20.0,
        "submission_types": ("online_upload",),
        "description_html": "<p>Solve all problems.</p>",
    }
    values.update(overrides)
    return AcademicItem(**values)


def make_announcement(**overrides):
    posted_at = NOW - timedelta(hours=1)
    values = {
        "uid": "canvas:announcement:1:20",
        "source_id": "20",
        "course_id": 1,
        "course_name": "Algorithms",
        "title": "Room update",
        "message_html": (
            "<p>Hello <strong>class</strong>.</p>"
            "<script>alert('secret')</script><p>@everyone New room.</p>"
        ),
        "message_text": "Hello class. @everyone New room.",
        "posted_at": posted_at,
        "posted_at_local": posted_at.astimezone(),
        "html_url": "https://canvas.example/courses/1/announcements/20",
        "author_name": "Professor Example",
        "read_state": "unread",
    }
    values.update(overrides)
    return Announcement(**values)


class DiscordNotifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "state.db"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def make_notifier(self, responses=None, **overrides):
        session = overrides.pop(
            "session",
            FakeSession(responses if responses is not None else [FakeResponse()]),
        )
        sleeps = []
        now_provider = overrides.pop("now_provider", lambda: NOW)
        notifier = DiscordNotifier(
            WEBHOOK_URL,
            state_path=self.state_path,
            app_timezone="America/Toronto",
            session=session,
            sleep=sleeps.append,
            now_provider=now_provider,
            **overrides,
        )
        return notifier, session, sleeps

    def test_assignment_embed_contains_required_fields_and_disables_mentions(self):
        notifier, _, _ = self.make_notifier()
        payload = notifier.assignment_payload(make_item())
        embed = payload["embeds"][0]

        self.assertEqual(embed["title"], "Problem Set 1")
        self.assertEqual(embed["url"], "https://canvas.example/courses/1/assignments/10")
        self.assertEqual(embed["fields"][0]["value"], "Algorithms")
        self.assertEqual(embed["fields"][2]["value"], "20")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_announcement_embed_removes_html_and_active_content(self):
        notifier, _, _ = self.make_notifier()
        payload = notifier.announcement_payload(make_announcement())
        description = payload["embeds"][0]["description"]

        self.assertEqual(description, "Hello class. @everyone New room.")
        self.assertNotIn("<", description)
        self.assertNotIn("alert", description)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_digest_only_lists_deadlines_inside_requested_window(self):
        notifier, _, _ = self.make_notifier()
        inside = make_item()
        outside = make_item(
            uid="canvas:assignment:1:11",
            source_id="11",
            title="Later work",
            due_at=NOW + timedelta(hours=73),
            due_at_local=(NOW + timedelta(hours=73)).astimezone(),
        )

        payload = notifier.daily_digest_payload([outside, inside], hours=72, now=NOW)
        description = payload["embeds"][0]["description"]

        self.assertIn("Problem Set 1", description)
        self.assertNotIn("Later work", description)

    def test_unchanged_alert_is_sent_once_but_changed_alert_is_sent_again(self):
        notifier, session, _ = self.make_notifier([FakeResponse(), FakeResponse()])
        item = make_item()

        self.assertTrue(notifier.send_assignment_alert(item))
        self.assertFalse(notifier.send_assignment_alert(item))
        self.assertTrue(
            notifier.send_assignment_alert(replace(item, title="Problem Set 1 updated"))
        )
        self.assertEqual(len(session.calls), 2)

    def test_forced_test_send_does_not_consume_deduplication_state(self):
        notifier, session, _ = self.make_notifier([FakeResponse(), FakeResponse()])
        item = make_item()

        self.assertTrue(notifier.send_assignment_alert(item, force=True))
        self.assertTrue(notifier.send_assignment_alert(item))
        self.assertEqual(len(session.calls), 2)

    def test_quiet_hours_block_all_delivery_without_enqueuing_or_marking_sent(self):
        quiet_now = datetime(2026, 9, 18, 6, 16, tzinfo=UTC)  # 02:16 Toronto
        notifier, session, _ = self.make_notifier(
            quiet_hours_start=0,
            quiet_hours_end=9,
            now_provider=lambda: quiet_now,
        )
        item = make_item()

        with self.assertRaisesRegex(DiscordQuietHoursError, "09:00 America/Toronto"):
            notifier.send_assignment_alert(item, force=True)

        self.assertEqual(session.calls, [])
        self.assertEqual(StateStore(self.state_path).pending_deliveries(), [])
        self.assertFalse(
            StateStore(self.state_path).was_sent(
                f"announcements:assignment:{item.uid}",
                notifier._fingerprint(notifier.assignment_payload(item)),
            )
        )

    def test_quiet_hours_end_is_an_inclusive_delivery_boundary(self):
        allowed_now = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)  # 09:00 Toronto
        notifier, session, _ = self.make_notifier(
            quiet_hours_start=0,
            quiet_hours_end=9,
            now_provider=lambda: allowed_now,
        )

        self.assertTrue(notifier.send_assignment_alert(make_item()))
        self.assertEqual(len(session.calls), 1)

    def test_quiet_hours_leave_pending_outbox_unclaimed(self):
        quiet_now = datetime(2026, 9, 18, 6, 16, tzinfo=UTC)
        store = StateStore(self.state_path)
        store.enqueue_messages(
            "announcements:custom:pending",
            "fingerprint",
            "announcements",
            ({"content": "pending"},),
        )
        notifier, session, _ = self.make_notifier(
            quiet_hours_start=0,
            quiet_hours_end=9,
            now_provider=lambda: quiet_now,
        )

        with self.assertRaises(DiscordQuietHoursError):
            notifier.flush_pending()

        self.assertEqual(session.calls, [])
        self.assertEqual(len(store.pending_deliveries()), 1)

    def test_stale_lecture_retry_is_discarded_instead_of_sent_late(self):
        store = StateStore(self.state_path)
        store.enqueue_messages(
            "lecture_quizzes:custom:lecture-quiz:old",
            "fingerprint",
            "lecture_quizzes",
            ({"content": "old quiz"},),
        )
        with store.connect() as database:
            database.execute(
                "UPDATE delivery_outbox SET created_at=?",
                (NOW.timestamp() - 61 * 60,),
            )
        notifier, session, _ = self.make_notifier()

        self.assertEqual(notifier.flush_pending(), 0)
        self.assertEqual(session.calls, [])
        self.assertEqual(store.pending_deliveries(), [])

    def test_rate_limit_uses_retry_after_then_retries(self):
        notifier, session, sleeps = self.make_notifier(
            [FakeResponse(429, {"retry_after": 1.25}), FakeResponse(204)]
        )

        self.assertTrue(notifier.send_announcement_alert(make_announcement()))
        self.assertEqual(sleeps, [1.25])
        self.assertEqual(len(session.calls), 2)

    def test_long_rate_limit_stops_without_marking_notification_seen(self):
        notifier, session, _ = self.make_notifier(
            [FakeResponse(429, {"retry_after": 120}), FakeResponse(204)],
            max_rate_limit_wait_seconds=60,
        )
        item = make_item()

        with self.assertRaises(DiscordRateLimitError):
            notifier.send_assignment_alert(item)
        self.assertTrue(notifier.send_assignment_alert(item))
        self.assertEqual(len(session.calls), 2)

    def test_error_message_does_not_expose_webhook_secret(self):
        notifier, _, _ = self.make_notifier([FakeResponse(404)])

        with self.assertRaises(DiscordNotificationError) as caught:
            notifier.send_assignment_alert(make_item())
        self.assertNotIn("test-token", str(caught.exception))

    def test_daily_digest_is_only_sent_once_per_local_day(self):
        notifier, session, _ = self.make_notifier([FakeResponse()])

        self.assertTrue(notifier.send_daily_digest([make_item()]))
        self.assertFalse(notifier.send_daily_digest([make_item(title="Changed")]))
        self.assertEqual(len(session.calls), 1)

    def test_json_state_persists_deduplication_across_notifier_instances(self):
        state_path = Path(self.temporary_directory.name) / "seen_ids.json"
        first_session = FakeSession([FakeResponse()])
        second_session = FakeSession([])
        first = DiscordNotifier(
            WEBHOOK_URL,
            state_path=state_path,
            session=first_session,
            now_provider=lambda: NOW,
        )
        second = DiscordNotifier(
            WEBHOOK_URL,
            state_path=state_path,
            session=second_session,
            now_provider=lambda: NOW,
        )

        self.assertTrue(first.send_announcement_alert(make_announcement()))
        self.assertFalse(second.send_announcement_alert(make_announcement()))
        self.assertEqual(len(first_session.calls), 1)
        self.assertEqual(len(second_session.calls), 0)

    def test_snapshot_flow_sends_each_alert_once(self):
        notifier, session, _ = self.make_notifier([FakeResponse(), FakeResponse(), FakeResponse()])
        snapshot = CanvasSnapshot(
            user_id="42",
            user_name="Ada Student",
            courses=(),
            items=(make_item(),),
            announcements=(make_announcement(),),
            warnings=(),
            fetched_at=NOW,
        )

        first = notifier.notify_snapshot(snapshot)
        second = notifier.notify_snapshot(snapshot)

        self.assertEqual(first.assignments_sent, 1)
        self.assertEqual(first.announcements_sent, 1)
        self.assertTrue(first.digest_sent)
        self.assertEqual(second.assignments_skipped, 1)
        self.assertEqual(second.announcements_skipped, 1)
        self.assertFalse(second.digest_sent)
        self.assertEqual(len(session.calls), 3)

    def test_webhook_validation_rejects_non_discord_and_query_urls(self):
        with self.assertRaises(DiscordConfigurationError):
            DiscordNotifier("https://example.com/api/webhooks/1/token")
        with self.assertRaises(DiscordConfigurationError):
            DiscordNotifier(f"{WEBHOOK_URL}?leak=yes")

    def test_bot_routes_each_notification_to_its_named_channel(self):
        session = FakeSession([FakeResponse(200), FakeResponse(200)])
        notifier = DiscordNotifier(
            WEBHOOK_URL,
            state_path=self.state_path,
            bot_token="private-test-token",
            channel_ids={
                "announcements": "111111111111111111",
                "lecture_quizzes": "222222222222222222",
            },
            session=session,
            now_provider=lambda: NOW,
        )

        notifier.send_announcement_alert(make_announcement())
        notifier.send_custom_notification(
            "quiz:test",
            {"username": "Attendr", "content": "Quiz"},
            destination="lecture_quizzes",
        )

        self.assertEqual(
            session.calls[0][0],
            "https://discord.com/api/v10/channels/111111111111111111/messages",
        )
        self.assertEqual(
            session.calls[1][0],
            "https://discord.com/api/v10/channels/222222222222222222/messages",
        )
        self.assertNotIn("username", session.calls[1][1]["json"])
        self.assertEqual(
            session.calls[1][1]["headers"],
            {"Authorization": "Bot private-test-token"},
        )


if __name__ == "__main__":
    unittest.main()
