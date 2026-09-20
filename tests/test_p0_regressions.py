from __future__ import annotations

from google.oauth2.credentials import Credentials

import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from google.auth.exceptions import RefreshError, TransportError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import main
from academic_assistant import (
    AnnouncementDatesSync,
    CalendarAPIError,
    CalendarAuthenticationError,
    CalendarConfigurationError,
    CanvasAPIError,
    CanvasClient,
    CanvasSnapshot,
    CourseSchedule,
    GoogleCalendarAuthenticator,
    GoogleCalendarSync,
    StudyPlanner,
)
from academic_assistant.http_client import CanvasSession
from academic_assistant.study_planner import StudyRemoteState
from test_calendar_sync import FakeCalendarService, academic_item
from test_canvas_client import FakeCanvas, assignment, course, event, NOW
from test_notifier import make_announcement
from academic_assistant.notifier import DiscordNotifier
from academic_assistant.ai_assistant import MajorDeadline
from academic_assistant.materials_sync import CourseMaterialsSync
from zoneinfo import ZoneInfo

UTC = timezone.utc


class OAuthSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.auth = GoogleCalendarAuthenticator(root / "credentials.json", root / "token.json")
        self.auth.credentials_path.write_text("{}")
        self.auth.token_path.write_text("{}")

    def test_valid_token_never_opens_browser(self):
        credentials = Mock(valid=True)
        with (
            patch(
                "academic_assistant.calendar_sync.Credentials.from_authorized_user_file",
                return_value=credentials,
            ),
            patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow,
        ):
            self.assertIs(self.auth.authenticate(), credentials)
            flow.from_client_secrets_file.assert_not_called()

    def test_refresh_success_is_saved_without_browser(self):
        credentials = Mock(valid=False, expired=True, refresh_token="fake")
        credentials.to_json.return_value = "{}"
        with (
            patch(
                "academic_assistant.calendar_sync.Credentials.from_authorized_user_file",
                return_value=credentials,
            ),
            patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow,
        ):
            self.assertIs(self.auth.authenticate(), credentials)
            credentials.refresh.assert_called_once()
            flow.from_client_secrets_file.assert_not_called()
        self.assertEqual(self.auth.token_path.stat().st_mode & 0o777, 0o600)

    def test_unusable_cache_never_opens_browser(self):
        cases = [
            ValueError("corrupt"),
            Mock(valid=False, expired=True, refresh_token=None),
        ]
        for value in cases:
            with (
                self.subTest(value=type(value).__name__),
                patch(
                    "academic_assistant.calendar_sync.Credentials.from_authorized_user_file",
                    side_effect=value if isinstance(value, Exception) else None,
                    return_value=value,
                ),
                patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow,
            ):
                with self.assertRaises(CalendarAuthenticationError):
                    self.auth.authenticate()
                flow.from_client_secrets_file.assert_not_called()

    def test_malformed_token_has_distinct_safe_diagnostic(self):
        with (
            patch(
                "academic_assistant.calendar_sync.Credentials.from_authorized_user_file",
                side_effect=ValueError("provider body with fake-secret-value"),
            ),
            patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow,
        ):
            with self.assertRaisesRegex(CalendarAuthenticationError, "malformed") as raised:
                self.auth.authenticate()
        self.assertNotIn("fake-secret-value", str(raised.exception))
        flow.from_client_secrets_file.assert_not_called()

    def test_scope_incompatible_token_has_distinct_safe_diagnostic(self):
        credentials = Mock(valid=True)
        credentials.has_scopes.return_value = False
        with (
            patch(
                "academic_assistant.calendar_sync.Credentials.from_authorized_user_file",
                return_value=credentials,
            ),
            patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow,
        ):
            with self.assertRaisesRegex(CalendarAuthenticationError, "required scope"):
                self.auth.authenticate()
        flow.from_client_secrets_file.assert_not_called()

    def test_missing_token_never_opens_browser(self):
        self.auth.token_path.unlink()
        with patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow:
            with self.assertRaisesRegex(CalendarAuthenticationError, "setup_google"):
                self.auth.authenticate()
            flow.from_client_secrets_file.assert_not_called()

    def test_refresh_failures_do_not_start_interactive_flow(self):
        for retryable in (False, True):
            credentials = Mock(valid=False, expired=True, refresh_token="fake")
            credentials.refresh.side_effect = RefreshError("fake failure", retryable=retryable)
            with (
                self.subTest(retryable=retryable),
                patch(
                    "academic_assistant.calendar_sync.Credentials.from_authorized_user_file",
                    return_value=credentials,
                ),
                patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow,
            ):
                with self.assertRaisesRegex(
                    CalendarAuthenticationError,
                    "temporarily" if retryable else "setup_google",
                ):
                    self.auth.authenticate()
                flow.from_client_secrets_file.assert_not_called()

    def test_explicit_setup_has_finite_browser_timeout(self):
        self.auth.token_path.unlink()
        self.auth.interactive = True
        with patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow:
            flow.from_client_secrets_file.return_value.run_local_server.return_value = Credentials(
                token="test", refresh_token="refresh"
            )
            self.auth.authenticate()
            self.assertEqual(
                flow.from_client_secrets_file.return_value.run_local_server.call_args.kwargs[
                    "timeout_seconds"
                ],
                120,
            )

    def test_interactive_setup_does_not_replace_token_without_refresh_token(self):
        original = '{"refresh_token":"still-valid-backup"}'
        self.auth.token_path.write_text(original)
        self.auth.interactive = True
        incomplete = Credentials(token="test", refresh_token=None)
        with (
            patch(
                "academic_assistant.calendar_sync.Credentials.from_authorized_user_file",
                side_effect=ValueError("malformed old token"),
            ),
            patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow,
        ):
            flow.from_client_secrets_file.return_value.run_local_server.return_value = incomplete
            with self.assertRaisesRegex(CalendarAuthenticationError, "not replaced"):
                self.auth.authenticate()
        self.assertEqual(self.auth.token_path.read_text(), original)

    def test_refresh_network_error_is_transient(self):
        credentials = Mock(valid=False, expired=True, refresh_token="fake")
        credentials.refresh.side_effect = TransportError("network unavailable")
        with (
            patch(
                "academic_assistant.calendar_sync.Credentials.from_authorized_user_file",
                return_value=credentials,
            ),
            patch("academic_assistant.calendar_sync.InstalledAppFlow") as flow,
        ):
            with self.assertRaisesRegex(CalendarAuthenticationError, "temporarily"):
                self.auth.authenticate()
            flow.from_client_secrets_file.assert_not_called()

    def test_read_only_api_preflight_does_not_mutate_calendar(self):
        service = Mock()
        authenticator = self.auth
        authenticator.verify_service(service)
        service.calendarList.return_value.list.assert_called_once_with(maxResults=1)
        service.events.assert_not_called()


class CanvasTransportTests(unittest.TestCase):
    @staticmethod
    def response(status, retry_after=None):
        return Mock(
            status_code=status,
            headers={} if retry_after is None else {"Retry-After": retry_after},
        )

    def test_retryable_statuses_are_bounded_and_honor_retry_after(self):
        for status in (429, 500, 502, 503, 504):
            with (
                self.subTest(status=status),
                patch(
                    "requests.Session.request",
                    side_effect=[self.response(status, "2"), self.response(200)],
                ) as request,
                patch("academic_assistant.http_client.time.sleep") as sleep,
            ):
                self.assertEqual(CanvasSession().get("https://canvas.example").status_code, 200)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args.kwargs["timeout"], (5, 15))
                sleep.assert_called_once_with(2)

    def test_long_rate_limit_and_auth_failures_are_not_retried(self):
        for status, header in ((429, "120"), (401, None), (403, None)):
            with (
                self.subTest(status=status),
                patch(
                    "requests.Session.request",
                    return_value=self.response(status, header),
                ) as request,
                patch("academic_assistant.http_client.time.sleep") as sleep,
            ):
                self.assertEqual(CanvasSession().get("https://canvas.example").status_code, status)
                request.assert_called_once()
                sleep.assert_not_called()

    def test_timeouts_exhaust_three_attempts(self):
        with (
            patch("requests.Session.request", side_effect=requests.Timeout) as request,
            patch("academic_assistant.http_client.time.sleep"),
        ):
            with self.assertRaises(requests.Timeout):
                CanvasSession().get("https://canvas.example")
            self.assertEqual(request.call_count, 3)

    def test_real_sdk_uses_bounded_transport(self):
        client = CanvasClient("https://canvas.example", "fake")
        with patch(
            "requests.Session.request",
            return_value=Mock(
                status_code=200,
                headers={},
                content=b"{}",
                json=lambda: {"id": 1, "name": "Test"},
            ),
        ) as request:
            self.assertEqual(client.validate_credentials(), ("1", "Test"))
            self.assertEqual(request.call_args.kwargs["timeout"], (5, 15))


class SourceSafetyTests(unittest.TestCase):
    def client(self, fake):
        return CanvasClient("https://canvas.example", "fake", canvas=fake, now_provider=lambda: NOW)

    def test_missing_due_date_is_nonfatal_and_not_a_fetch_failure(self):
        snapshot = self.client(
            FakeCanvas([course(assignments=[assignment(1, "Undated", None)])])
        ).fetch_snapshot()
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.items, ())

    def test_fetch_failure_marks_snapshot_incomplete(self):
        snapshot = self.client(
            FakeCanvas([course(assignment_error=requests.Timeout())])
        ).fetch_snapshot()
        self.assertFalse(snapshot.complete)

    def test_partial_announcements_are_not_used_for_reconciliation(self):
        with self.assertRaises(CanvasAPIError):
            self.client(
                FakeCanvas([course(announcement_error=requests.Timeout())])
            ).get_recent_announcements()

    def test_all_day_date_wins_over_utc_midnight_and_survives_today_filter(self):
        fake = FakeCanvas(
            [course()],
            events={
                1: [
                    event(
                        1,
                        "Today",
                        "2026-09-04T00:00:00Z",
                        all_day=True,
                        all_day_date="2026-09-04",
                    )
                ]
            },
        )
        snapshot = self.client(fake).fetch_snapshot()
        self.assertEqual(len(snapshot.items), 1)
        body = GoogleCalendarSync(FakeCalendarService())._event_body(snapshot.items[0])
        self.assertEqual(body["start"], {"date": "2026-09-04"})
        self.assertEqual(body["end"], {"date": "2026-09-05"})

    def test_newest_announcement_wins_in_either_input_order(self):
        old = replace(make_announcement(), uid="old", message_text="2026-09-20")
        new = replace(
            old,
            uid="new",
            posted_at=old.posted_at + timedelta(days=1),
            message_text="2026-09-25",
        )
        ai = Mock()
        ai.extract_major_deadlines.side_effect = lambda text, **kw: [
            {
                "title": "Essay",
                "due_date": text,
                "due_time": None,
                "kind": "assignment",
                "source_evidence": text,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            sync = AnnouncementDatesSync(ai, index_path=Path(tmp) / "index.json")
            for inputs in ((new, old), (old, new)):
                self.assertEqual(
                    sync.sync(inputs).items[0].due_at_local.date().isoformat(),
                    "2026-09-25",
                )

    def test_date_only_deadline_remains_visible_in_today_digest(self):
        zone = ZoneInfo("America/Toronto")
        local = datetime(2026, 9, 4, tzinfo=zone)
        item = academic_item(due_at=local.astimezone(UTC), due_at_local=local, all_day=True)
        notifier = DiscordNotifier(
            "https://discord.com/api/webhooks/1/fake", now_provider=lambda: NOW
        )
        description = notifier.daily_digest_payload([item])["embeds"][0]["description"]
        self.assertIn(item.title, description)
        self.assertIn("time not specified", description)
        with patch.object(main, "datetime") as clock:
            clock.now.return_value = NOW
            self.assertEqual(main.upcoming_items((item,)), (item,))

    def test_calendar_utc_conversion_handles_both_sides_of_dst(self):
        calendar = GoogleCalendarSync(FakeCalendarService())
        for utc, expected in (
            ("2026-03-08T06:45:00+00:00", "2026-03-08T01:45:00-05:00"),
            ("2026-03-08T07:15:00+00:00", "2026-03-08T03:15:00-04:00"),
            ("2026-11-01T05:30:00+00:00", "2026-11-01T01:30:00-04:00"),
            ("2026-11-01T06:30:00+00:00", "2026-11-01T01:30:00-05:00"),
        ):
            with self.subTest(utc=utc):
                body = calendar._event_body(academic_item(due_at=datetime.fromisoformat(utc)))
                self.assertEqual(body["start"]["dateTime"], expected)
                self.assertGreater(
                    datetime.fromisoformat(body["end"]["dateTime"]).astimezone(UTC),
                    datetime.fromisoformat(utc),
                )

    def test_explicit_extracted_exam_after_lecture_end_has_valid_duration(self):
        schedule = CourseSchedule.load(ROOT / "data/course_schedule.example.json")
        deadline = MajorDeadline(
            title="Exam",
            due_date="2026-10-27",
            due_time="18:00",
            kind="exam",
            source_evidence="Exam at 18:00",
        )
        announcement = replace(make_announcement(), course_name="Web Development")
        with tempfile.TemporaryDirectory() as tmp:
            sync = AnnouncementDatesSync(
                Mock(), index_path=Path(tmp) / "dates.json", course_schedule=schedule
            )
            item = sync._to_item(announcement, deadline)
            self.assertEqual(item.due_at_local.hour, 18)
            self.assertEqual(item.end_at - item.due_at, timedelta(hours=2))
            material = SimpleNamespace(
                course_id=1,
                course_name="Web Development",
                title="Syllabus",
                source_id="1",
                html_url=None,
                updated_at=None,
            )
            items = CourseMaterialsSync(
                Mock(), Mock(), course_schedule=schedule
            )._normalize_deadlines([(material, deadline)], datetime(2026, 9, 1).date(), [])
            self.assertEqual(items[0].end_at - items[0].due_at, timedelta(hours=2))

    def test_academic_range_keeps_last_day_with_exclusive_end(self):
        schedule = CourseSchedule.load(ROOT / "data/course_schedule.example.json")
        item = next(
            item
            for item in schedule.academic_calendar_items()
            if item.source_id == "fall-study-week"
        )
        body = GoogleCalendarSync(FakeCalendarService())._event_body(item)
        self.assertEqual(body["start"], {"date": "2026-10-13"})
        self.assertEqual(body["end"], {"date": "2026-10-19"})

    def test_distinct_same_day_assignments_survive_filter(self):
        canvas = academic_item(title="Essay")
        material = replace(canvas, uid="material", title="Problem set", source="syllabus_deadline")
        self.assertEqual(main.filter_material_duplicates((canvas,), (material,)), (material,))

    def test_ambiguous_title_match_is_not_removed(self):
        first = academic_item()
        second = replace(first, uid="other")
        material = replace(first, uid="material", source="syllabus_deadline")
        self.assertEqual(main.filter_material_duplicates((first, second), (material,)), (material,))

    def test_explicit_exam_times_survive_lecture_replacement(self):
        schedule = CourseSchedule.load(ROOT / "data/course_schedule.example.json")
        lecture = next(item for item in schedule.scheduled_class_items() if item.kind == "lecture")
        exam = replace(
            lecture,
            uid="exam",
            kind="exam",
            title="Exam",
            due_at=lecture.due_at + timedelta(minutes=20),
            due_at_local=lecture.due_at_local + timedelta(minutes=20),
            end_at=lecture.end_at + timedelta(minutes=20),
        )
        merged, _ = schedule.merge_with_class_schedule((exam,))
        result = next(item for item in merged if item.title == "Exam")
        self.assertEqual((result.due_at, result.end_at), (exam.due_at, exam.end_at))

    def test_invalid_interval_is_rejected_before_any_event_mutation(self):
        service = FakeCalendarService()
        calendar = GoogleCalendarSync(service)
        item = academic_item()
        calendar.sync_items([item])
        with self.assertRaises(CalendarConfigurationError):
            calendar.sync_items(
                [replace(item, uid="invalid", end_at=item.due_at)],
                delete_uids=[item.uid],
            )
        self.assertEqual(service.event_delete_calls, [])
        self.assertEqual(len(service.event_insert_calls), 1)


class PlannerSafetyTests(unittest.TestCase):
    def test_incomplete_inputs_cannot_mutate_calendar(self):
        service = Mock()
        with self.assertRaises(CalendarAPIError):
            StudyPlanner(service).sync((), inputs_complete=False)
        self.assertEqual(service.mock_calls, [])

    def test_remote_state_failures_cannot_mutate_calendar(self):
        for state in (
            requests.Timeout(),
            {},
            {
                "completed_tasks": [],
                "completed_sessions": [],
                "rescheduled_sessions": {"x": {"start": "bad"}},
            },
        ):
            remote, service = Mock(), Mock()
            if isinstance(state, Exception):
                remote.get_state.side_effect = state
            else:
                remote.get_state.return_value = state
            with (
                self.subTest(state=type(state).__name__),
                self.assertRaises(CalendarAPIError),
            ):
                StudyPlanner(service, remote=remote).sync((), inputs_complete=True)
            self.assertEqual(service.mock_calls, [])
            remote.sync_sessions.assert_not_called()

    def test_half_configured_remote_is_not_silently_disabled(self):
        with patch.dict(
            "os.environ",
            {"STUDY_WORKER_URL": "https://worker.example", "STUDY_SYNC_SECRET": ""},
        ):
            with self.assertRaises(CalendarAPIError):
                StudyRemoteState.from_env()

    def test_failed_google_preflight_blocks_calendar_and_study_writes_once(self):
        snapshot = CanvasSnapshot("1", "Test", (), (), (), (), NOW, complete=True)
        canvas = Mock()
        canvas.fetch_snapshot.return_value = snapshot
        canvas.get_recent_announcements.return_value = ()
        schedule = Mock(excluded_course_patterns=())
        schedule.timezone = ZoneInfo("America/Toronto")
        materials = SimpleNamespace(
            items=(),
            materials_found=0,
            materials_analyzed=0,
            cached_materials_reused=0,
            warnings=("prior state retained",),
            complete=True,
            blocking_warnings=(),
        )
        dates = SimpleNamespace(
            items=(),
            analyzed=0,
            cached=0,
            warnings=(),
            complete=True,
            blocking_warnings=(),
        )
        auth_error = CalendarAuthenticationError(
            "Google Calendar authorization is no longer usable. Run "
            "scripts/setup_google.py locally to reauthorize, then replace the "
            "GOOGLE_TOKEN_B64 GitHub Actions secret."
        )
        with (
            patch.object(main.CourseSchedule, "load", return_value=schedule),
            patch.object(main.CanvasClient, "from_env", return_value=canvas),
            patch.object(
                main.CourseMaterialsSync,
                "from_env",
                return_value=Mock(sync=Mock(return_value=materials)),
            ),
            patch.object(main.AIAssistant, "from_env", return_value=Mock()),
            patch.object(main.AnnouncementDatesSync, "sync", return_value=dates),
            patch.object(main, "GoogleCalendarAuthenticator") as authenticator,
            patch.object(main.GoogleCalendarSync, "from_env") as calendar,
            patch.object(main.StudyPlanner, "from_env") as planner,
        ):
            authenticator.return_value.build_service.side_effect = auth_error
            results = main.run_pipeline(main.build_parser().parse_args(["--sync-only"]))

        self.assertEqual(authenticator.return_value.build_service.call_count, 1)
        calendar.assert_not_called()
        planner.assert_not_called()
        by_name = {result.name: result for result in results}
        self.assertEqual(by_name["Canvas"].status, "ok")
        self.assertEqual(by_name["Materials"].status, "degraded")
        self.assertEqual(by_name["Announcement dates"].status, "ok")
        self.assertEqual(by_name["Calendar"].status, "failed")
        self.assertEqual(by_name["Study plan"].status, "failed")
        self.assertIn("GOOGLE_TOKEN_B64", by_name["Calendar"].detail)
        self.assertIn("existing study plan preserved", by_name["Study plan"].detail)

    def test_pipeline_preserves_calendar_on_each_incomplete_source(self):
        for source in ("canvas", "materials", "announcements"):
            with self.subTest(source=source), ExitStack() as stack:
                snapshot = CanvasSnapshot(
                    "1", "Test", (), (), (), (), NOW, complete=source != "canvas"
                )
                canvas = Mock()
                canvas.fetch_snapshot.return_value = snapshot
                canvas.get_recent_announcements.return_value = ()
                stack.enter_context(
                    patch.object(
                        main.CourseSchedule,
                        "load",
                        return_value=Mock(excluded_course_patterns=()),
                    )
                )
                stack.enter_context(
                    patch.object(main.CanvasClient, "from_env", return_value=canvas)
                )
                stack.enter_context(
                    patch.object(
                        main.CourseMaterialsSync,
                        "from_env",
                        return_value=Mock(
                            sync=lambda **kwargs: SimpleNamespace(
                                items=(),
                                materials_found=0,
                                materials_analyzed=0,
                                cached_materials_reused=0,
                                warnings=("failed",) if source == "materials" else (),
                                complete=source != "materials",
                                blocking_warnings=("failed",) if source == "materials" else (),
                            )
                        ),
                    )
                )
                stack.enter_context(patch.object(main.AIAssistant, "from_env", return_value=Mock()))
                stack.enter_context(
                    patch.object(
                        main.AnnouncementDatesSync,
                        "sync",
                        return_value=SimpleNamespace(
                            items=(),
                            analyzed=0,
                            cached=0,
                            warnings=("failed",) if source == "announcements" else (),
                            complete=source != "announcements",
                            blocking_warnings=("failed",) if source == "announcements" else (),
                        ),
                    )
                )
                authenticator = stack.enter_context(
                    patch.object(main, "GoogleCalendarAuthenticator")
                )
                authenticator.return_value.build_service.return_value = Mock()
                calendar = stack.enter_context(patch.object(main.GoogleCalendarSync, "from_env"))
                planner = stack.enter_context(patch.object(main.StudyPlanner, "from_env"))
                results = main.run_pipeline(main.build_parser().parse_args(["--sync-only"]))
                calendar.assert_not_called()
                planner.assert_not_called()
                self.assertEqual(
                    next(result.status for result in results if result.name == "Calendar"),
                    "failed",
                )

    def test_nonblocking_source_warnings_allow_calendar_reconciliation(self):
        snapshot = CanvasSnapshot("1", "Test", (), (), (), (), NOW, complete=True)
        canvas = Mock()
        canvas.fetch_snapshot.return_value = snapshot
        canvas.get_recent_announcements.return_value = ()
        schedule = Mock(excluded_course_patterns=())
        schedule.timezone = ZoneInfo("America/Toronto")
        schedule.academic_calendar_items.return_value = ()
        schedule.merge_with_class_schedule.side_effect = lambda items: (items, frozenset())
        materials = SimpleNamespace(
            items=(),
            materials_found=1,
            materials_analyzed=1,
            cached_materials_reused=0,
            warnings=("safely rejected AI candidate",),
            complete=True,
            blocking_warnings=(),
        )
        announcements = SimpleNamespace(
            items=(),
            analyzed=1,
            cached=0,
            warnings=("safely rejected AI candidate",),
            complete=True,
            blocking_warnings=(),
        )
        calendar_report = SimpleNamespace(created=(), updated=(), skipped=(), deleted=())

        with (
            patch.object(main.CourseSchedule, "load", return_value=schedule),
            patch.object(main.CanvasClient, "from_env", return_value=canvas),
            patch.object(
                main.CourseMaterialsSync,
                "from_env",
                return_value=Mock(sync=Mock(return_value=materials)),
            ),
            patch.object(main.AIAssistant, "from_env", return_value=Mock()),
            patch.object(main.AnnouncementDatesSync, "sync", return_value=announcements),
            patch.object(main, "GoogleCalendarAuthenticator") as authenticator,
            patch.object(main.GoogleCalendarSync, "from_env") as calendar,
        ):
            authenticator.return_value.build_service.return_value = Mock()
            calendar.return_value.sync_items.return_value = calendar_report
            results = main.run_pipeline(
                main.build_parser().parse_args(["--sync-only", "--no-study-plan"])
            )

        self.assertEqual(
            next(result.status for result in results if result.name == "Materials"),
            "degraded",
        )
        self.assertEqual(
            next(result.status for result in results if result.name == "Announcement dates"),
            "degraded",
        )
        self.assertEqual(
            next(result.status for result in results if result.name == "Calendar"),
            "ok",
        )
        calendar.return_value.sync_items.assert_called_once()
        self.assertIs(
            calendar.call_args.kwargs["service"],
            authenticator.return_value.build_service.return_value,
        )


if __name__ == "__main__":
    unittest.main()
