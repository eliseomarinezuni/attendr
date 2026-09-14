from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (  # noqa: E402
    AcademicItem,
    CalendarConfigurationError,
    GoogleCalendarAuthenticator,
    GoogleCalendarSync,
)


UTC = timezone.utc


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeCalendarListResource:
    def __init__(self, service):
        self.service = service

    def list(self, **kwargs):
        self.service.calendar_list_calls.append(kwargs)
        return FakeRequest({"items": list(self.service.calendars_by_name)})


class FakeCalendarsResource:
    def __init__(self, service):
        self.service = service

    def insert(self, **kwargs):
        self.service.calendar_insert_calls.append(kwargs)
        result = {"id": "created-calendar", **kwargs["body"]}
        self.service.calendars_by_name.append(result)
        return FakeRequest(result)


class FakeEventsResource:
    def __init__(self, service):
        self.service = service

    def list(self, **kwargs):
        self.service.event_list_calls.append(kwargs)
        return FakeRequest({"items": list(self.service.event_store.values())})

    def insert(self, **kwargs):
        self.service.event_insert_calls.append(kwargs)
        event_id = f"event-{len(self.service.event_insert_calls)}"
        result = {
            **kwargs["body"],
            "id": event_id,
            "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        }
        self.service.event_store[event_id] = result
        return FakeRequest(result)

    def update(self, **kwargs):
        self.service.event_update_calls.append(kwargs)
        event_id = kwargs["eventId"]
        result = {
            **kwargs["body"],
            "id": event_id,
            "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        }
        self.service.event_store[event_id] = result
        return FakeRequest(result)

    def delete(self, **kwargs):
        self.service.event_delete_calls.append(kwargs)
        self.service.event_store.pop(kwargs["eventId"], None)
        return FakeRequest({})


class FakeCalendarService:
    def __init__(self, calendars=()):
        self.calendars_by_name = list(calendars)
        self.event_store = {}
        self.calendar_list_calls = []
        self.calendar_insert_calls = []
        self.event_list_calls = []
        self.event_insert_calls = []
        self.event_update_calls = []
        self.event_delete_calls = []

    def calendarList(self):
        return FakeCalendarListResource(self)

    def calendars(self):
        return FakeCalendarsResource(self)

    def events(self):
        return FakeEventsResource(self)


def academic_item(**overrides) -> AcademicItem:
    due_at = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    values = {
        "uid": "canvas:assignment:7:42",
        "source": "assignment",
        "source_id": "42",
        "course_id": 7,
        "course_name": "Algorithms",
        "title": "Problem Set 1",
        "kind": "assignment",
        "due_at": due_at,
        "due_at_local": due_at,
        "end_at": None,
        "all_day": False,
        "html_url": "https://canvas.example/courses/7/assignments/42",
        "updated_at": None,
        "points_possible": 25.0,
        "submission_types": ("online_upload",),
        "description_html": None,
        "submitted": False,
        "submission_state": "unsubmitted",
    }
    values.update(overrides)
    return AcademicItem(**values)


class GoogleCalendarSyncTests(unittest.TestCase):
    def test_first_run_creates_tagged_event_and_second_run_skips_it(self):
        service = FakeCalendarService()
        calendar = GoogleCalendarSync(service, calendar_id="primary")

        first = calendar.sync_items([academic_item()])
        second = calendar.sync_items([academic_item()])

        self.assertEqual(len(first.created), 1)
        self.assertEqual(len(second.skipped), 1)
        self.assertEqual(len(service.event_insert_calls), 1)
        self.assertEqual(len(service.event_update_calls), 0)
        private = service.event_insert_calls[0]["body"]["extendedProperties"]["private"]
        self.assertEqual(private["attendr_source"], "canvas")
        self.assertEqual(private["canvas_uid"], "canvas:assignment:7:42")
        self.assertTrue(private["attendr_fingerprint"])
        self.assertEqual(
            service.event_list_calls[0]["privateExtendedProperty"],
            "attendr_source=canvas",
        )

    def test_changed_due_date_updates_existing_event(self):
        service = FakeCalendarService()
        calendar = GoogleCalendarSync(service, calendar_id="primary")
        item = academic_item()
        calendar.sync_items([item])
        changed_due = datetime(2026, 9, 11, 20, 30, tzinfo=UTC)

        report = calendar.sync_items([replace(item, due_at=changed_due, due_at_local=changed_due)])

        self.assertEqual(len(report.updated), 1)
        self.assertEqual(len(service.event_insert_calls), 1)
        self.assertEqual(len(service.event_update_calls), 1)
        self.assertEqual(
            service.event_update_calls[0]["body"]["start"]["dateTime"],
            "2026-09-11T16:30:00-04:00",
        )

    def test_submitted_assignment_gets_checkmark_and_configured_color(self):
        service = FakeCalendarService()
        calendar = GoogleCalendarSync(
            service,
            calendar_id="primary",
            mark_submitted=True,
            submitted_color_id="10",
        )

        calendar.sync_items([academic_item(submitted=True, submission_state="submitted")])

        body = service.event_insert_calls[0]["body"]
        self.assertTrue(body["summary"].startswith("✅ "))
        self.assertEqual(body["colorId"], "10")

    def test_named_calendar_is_created_when_missing(self):
        service = FakeCalendarService()
        calendar = GoogleCalendarSync(service, calendar_name="College Deadlines")

        report = calendar.sync_items([academic_item()])

        self.assertEqual(report.calendar_id, "created-calendar")
        self.assertEqual(report.calendar_name, "College Deadlines")
        self.assertEqual(
            service.calendar_insert_calls[0]["body"],
            {"summary": "College Deadlines", "timeZone": "America/Toronto"},
        )
        self.assertEqual(service.event_insert_calls[0]["calendarId"], "created-calendar")

    def test_existing_named_writable_calendar_is_reused(self):
        service = FakeCalendarService(
            calendars=[{"id": "college-id", "summary": "College Deadlines"}]
        )
        calendar = GoogleCalendarSync(service, calendar_name="College Deadlines")

        report = calendar.sync_items([academic_item()])

        self.assertEqual(report.calendar_id, "college-id")
        self.assertEqual(service.calendar_insert_calls, [])

    def test_all_day_event_uses_exclusive_next_day_end(self):
        service = FakeCalendarService()
        calendar = GoogleCalendarSync(service, calendar_id="primary")
        due_at = datetime(2026, 9, 20, 4, 0, tzinfo=UTC)

        calendar.sync_items([academic_item(due_at=due_at, due_at_local=due_at, all_day=True)])

        body = service.event_insert_calls[0]["body"]
        self.assertEqual(body["start"], {"date": "2026-09-20"})
        self.assertEqual(body["end"], {"date": "2026-09-21"})

    def test_study_calendar_prunes_only_managed_missing_events(self):
        service = FakeCalendarService()
        calendar = GoogleCalendarSync(
            service,
            calendar_id="study",
            source_tag="study_plan",
            prune_missing=True,
        )
        item = academic_item(uid="attendr:study:abc:1", source="study_plan")
        calendar.sync_items([item])

        report = calendar.sync_items([])

        self.assertEqual(len(report.deleted), 1)
        self.assertEqual(len(service.event_delete_calls), 1)
        self.assertEqual(
            service.event_list_calls[0]["privateExtendedProperty"],
            "attendr_source=study_plan",
        )

    def test_explicit_delete_removes_replaced_event_only(self):
        service = FakeCalendarService()
        calendar = GoogleCalendarSync(service, calendar_id="primary")
        replaced = academic_item(uid="canvas:material-deadline:7:midterm")
        retained = academic_item(uid="canvas:assignment:7:other")
        calendar.sync_items([replaced, retained])

        report = calendar.sync_items([retained], delete_uids={replaced.uid})

        self.assertEqual([entry.canvas_uid for entry in report.deleted], [replaced.uid])
        self.assertEqual(len(service.event_delete_calls), 1)
        self.assertEqual(len(service.event_store), 1)
        remaining = next(iter(service.event_store.values()))
        self.assertEqual(
            remaining["extendedProperties"]["private"]["canvas_uid"],
            retained.uid,
        )


class GoogleCalendarAuthenticatorTests(unittest.TestCase):
    def test_missing_desktop_credentials_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authenticator = GoogleCalendarAuthenticator(
                root / "credentials.json", root / "token.json"
            )

            with self.assertRaisesRegex(CalendarConfigurationError, "Desktop app credentials"):
                authenticator.authenticate()

    def test_token_cache_is_written_with_private_permissions(self):
        class FakeCredentials:
            @staticmethod
            def to_json():
                return '{"refresh_token":"secret-test-value"}'

        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token.json"
            authenticator = GoogleCalendarAuthenticator(
                Path(directory) / "credentials.json", token_path
            )

            authenticator._save_token(FakeCredentials())

            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("secret-test-value", token_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
