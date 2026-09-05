from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from canvasapi.exceptions import CanvasException, InvalidAccessToken  # noqa: E402

from academic_assistant.canvas_client import (  # noqa: E402
    CanvasAuthenticationError,
    CanvasClient,
    CanvasConfigurationError,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeCourse(SimpleNamespace):
    def get_assignments(self, **kwargs):
        if getattr(self, "assignment_error", None):
            raise self.assignment_error
        self.assignment_kwargs = kwargs
        return iter(getattr(self, "assignments", []))

    def get_discussion_topics(self, **kwargs):
        if getattr(self, "announcement_error", None):
            raise self.announcement_error
        self.announcement_kwargs = kwargs
        return iter(getattr(self, "announcements", []))


class FakeCanvas:
    def __init__(self, courses, events=None, event_errors=None, user_error=None):
        self.courses = courses
        self.events = events or {}
        self.event_errors = event_errors or {}
        self.user_error = user_error
        self.course_kwargs = None

    def get_current_user(self):
        if self.user_error:
            raise self.user_error
        return SimpleNamespace(id=42, name="Ada Student")

    def get_courses(self, **kwargs):
        self.course_kwargs = kwargs
        return iter(self.courses)

    def get_calendar_events(self, **kwargs):
        course_id = int(kwargs["context_codes"][0].split("_")[1])
        if course_id in self.event_errors:
            raise self.event_errors[course_id]
        return iter(self.events.get(course_id, []))


def assignment(item_id, title, due_at, **overrides):
    values = {
        "id": item_id,
        "name": title,
        "due_at": due_at,
        "html_url": f"https://canvas.example/courses/1/assignments/{item_id}",
        "updated_at": "2026-09-01T10:00:00Z",
        "points_possible": 10,
        "submission_types": ["online_upload"],
        "description": "<p>Instructions</p>",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def event(item_id, title, start_at, **overrides):
    values = {
        "id": item_id,
        "title": title,
        "start_at": start_at,
        "end_at": None,
        "all_day": False,
        "html_url": f"https://canvas.example/calendar?event_id={item_id}",
        "updated_at": "2026-09-02T10:00:00Z",
        "description": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def announcement(item_id, title, posted_at, read_state="unread"):
    return SimpleNamespace(
        id=item_id,
        title=title,
        posted_at=posted_at,
        read_state=read_state,
        message="<p>Hello <strong>class</strong>.</p><script>ignore me</script>",
        html_url=f"https://canvas.example/announcements/{item_id}",
        author={"display_name": "Professor Example"},
    )


def course(course_id=1, name="Algorithms", **overrides):
    values = {
        "id": course_id,
        "name": name,
        "course_code": "CSC300",
        "html_url": f"https://canvas.example/courses/{course_id}",
        "assignments": [],
        "announcements": [],
    }
    values.update(overrides)
    return FakeCourse(**values)


class CanvasClientTests(unittest.TestCase):
    def make_client(self, fake_canvas, **overrides):
        values = {
            "lookahead_days": 30,
            "announcement_days": 14,
            "app_timezone": "America/Toronto",
            "canvas": fake_canvas,
            "now_provider": lambda: NOW,
        }
        values.update(overrides)
        return CanvasClient(
            "https://canvas.example",
            "secret-token",
            **values,
        )

    def test_snapshot_normalizes_items_ids_types_and_timezone(self):
        active_course = course(
            assignments=[
                assignment(10, "Midterm Exam", "2026-09-10T18:00:00Z"),
                assignment(
                    11,
                    "Chapter Quiz",
                    "2026-09-11T18:00:00Z",
                    submission_types=["online_quiz"],
                ),
                assignment(12, "Problem Set", "2026-09-12T18:00:00Z"),
            ],
        )
        fake = FakeCanvas(
            [active_course],
            events={1: [event(20, "Final review session", "2026-09-13T18:00:00Z")]},
        )

        snapshot = self.make_client(fake).fetch_snapshot()

        self.assertEqual(snapshot.user_name, "Ada Student")
        self.assertEqual(
            [item.kind for item in snapshot.items],
            ["exam", "quiz", "assignment", "exam"],
        )
        self.assertEqual(snapshot.items[0].uid, "canvas:assignment:1:10")
        self.assertEqual(snapshot.items[-1].uid, "canvas:event:1:20")
        self.assertEqual(snapshot.items[0].due_at_local.hour, 14)
        self.assertEqual(snapshot.items[0].due_at_local.tzname(), "EDT")
        self.assertEqual(fake.course_kwargs["enrollment_state"], "active")
        self.assertEqual(fake.course_kwargs["enrollment_type"], "student")
        self.assertTrue(active_course.assignment_kwargs["override_assignment_dates"])
        self.assertEqual(active_course.assignment_kwargs["include"], ["submission"])

    def test_upcoming_window_includes_boundaries_and_excludes_invalid_dates(self):
        active_course = course(
            assignments=[
                assignment(1, "Starts now", "2026-09-04T12:00:00Z"),
                assignment(2, "Ends window", "2026-10-04T12:00:00Z"),
                assignment(3, "Too late", "2026-10-04T12:00:01Z"),
                assignment(4, "Undated", None),
            ]
        )
        snapshot = self.make_client(FakeCanvas([active_course])).fetch_snapshot()

        self.assertEqual([item.source_id for item in snapshot.items], ["1", "2"])
        self.assertTrue(
            any("Skipped malformed assignment" in warning for warning in snapshot.warnings)
        )

    def test_all_day_event_uses_local_midnight(self):
        active_course = course()
        fake = FakeCanvas(
            [active_course],
            events={
                1: [
                    event(
                        5,
                        "Department event",
                        None,
                        all_day=True,
                        all_day_date="2026-09-20",
                    )
                ]
            },
        )

        item = self.make_client(fake).fetch_snapshot().items[0]

        self.assertTrue(item.all_day)
        self.assertEqual(item.due_at_local.isoformat(), "2026-09-20T00:00:00-04:00")

    def test_announcements_are_recent_unread_and_plain_text(self):
        active_course = course(
            announcements=[
                announcement(1, "Recent", "2026-09-03T12:00:00Z"),
                announcement(2, "Old", "2026-08-20T11:59:59Z"),
                announcement(3, "Already read", "2026-09-02T12:00:00Z", "read"),
            ]
        )

        snapshot = self.make_client(FakeCanvas([active_course])).fetch_snapshot()

        self.assertEqual([item.title for item in snapshot.announcements], ["Recent"])
        self.assertEqual(snapshot.announcements[0].message_text, "Hello class.")
        self.assertEqual(
            active_course.announcement_kwargs,
            {
                "only_announcements": True,
                "filter_by": "unread",
                "order_by": "recent_activity",
            },
        )

    def test_one_inaccessible_course_adds_warnings_without_losing_other_data(self):
        good = course(
            1,
            "Accessible",
            assignments=[assignment(1, "Homework", "2026-09-05T12:00:00Z")],
            announcements=[announcement(1, "News", "2026-09-03T12:00:00Z")],
        )
        bad = course(
            2,
            "Restricted",
            assignment_error=CanvasException("blocked"),
            announcement_error=CanvasException("blocked"),
        )
        fake = FakeCanvas(
            [good, bad],
            event_errors={2: CanvasException("blocked")},
        )

        snapshot = self.make_client(fake).fetch_snapshot()

        self.assertEqual(len(snapshot.items), 1)
        self.assertEqual(len(snapshot.announcements), 1)
        self.assertEqual(len(snapshot.warnings), 3)
        self.assertTrue(all("Restricted" in warning for warning in snapshot.warnings))

    def test_invalid_token_becomes_actionable_authentication_error(self):
        fake = FakeCanvas([], user_error=InvalidAccessToken("bad token"))
        with self.assertRaisesRegex(CanvasAuthenticationError, "rejected the API token"):
            self.make_client(fake).validate_credentials()

    def test_invalid_configuration_fails_before_any_request(self):
        with self.assertRaisesRegex(CanvasConfigurationError, "origin only"):
            CanvasClient("https://canvas.example/api/v1", "token")
        with self.assertRaisesRegex(CanvasConfigurationError, "CANVAS_API_TOKEN"):
            CanvasClient("https://canvas.example", "")
        with self.assertRaisesRegex(CanvasConfigurationError, "IANA timezone"):
            CanvasClient("https://canvas.example", "token", app_timezone="Mars/Olympus")


if __name__ == "__main__":
    unittest.main()
