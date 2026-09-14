from __future__ import annotations
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from academic_assistant.calendar_sync import GoogleCalendarSync, CalendarAPIError
from academic_assistant.delivery import discord_messages
from academic_assistant.notifier import DiscordNotifier, DiscordNotificationError
from academic_assistant.settings import Settings
from academic_assistant.state_store import StateStore
from academic_assistant.study_planner import StudyPlanner
from test_calendar_sync import FakeCalendarService, academic_item
from test_notifier import FakeResponse, FakeSession, WEBHOOK_URL


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "attendr.db")


def test_only_one_sender_can_claim_across_connections(store):
    store.enqueue("event", "hash", "announcements", {"content": "hello"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: StateStore(store.path).claim("event", "hash"), range(8)))
    assert sum(value is not None for value in results) == 1


def test_crashed_send_is_uncertain_and_never_automatically_reclaimed(store):
    store.enqueue("event", "hash", "announcements", {"content": "hello"})
    store.claim("event", "hash")
    with store.connect() as db:
        db.execute("UPDATE delivery_outbox SET lease_until=?", (time.time() - 1,))
    assert store.claim("event", "hash") is None
    assert store.blocked_deliveries() == 1


def test_payload_is_immutable_for_same_delivery(store):
    store.enqueue("event", "hash", "announcements", {"content": "first"})
    store.enqueue("event", "hash", "announcements", {"content": "second"})
    assert json.loads(store.claim("event", "hash")["payload"])["content"] == "first"


def test_legacy_migration_idempotent_and_fails_transactionally(store, tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "notifications": {"announcements:announcement:1": {"fingerprint": "old"}},
            }
        )
    )
    store.migrate_notifications(path)
    store.mark_sent("announcements:announcement:1", "new")
    store.migrate_notifications(path)
    assert store.was_sent("announcements:announcement:1", "new")
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"version": 1, "notifications": {"good": {"fingerprint": "ok"}, "bad": None}})
    )
    with pytest.raises(ValueError):
        store.migrate_notifications(path)
    assert not store.was_sent("good", "ok")


def test_quiz_payload_survives_restart_and_cannot_be_regenerated_over_it(store):
    store.save_quiz("daily:2026-09-06", {"content": "first quiz"})
    restarted = StateStore(store.path)
    assert restarted.save_quiz("daily:2026-09-06", {"content": "replacement"}) == {
        "content": "first quiz"
    }
    restarted.complete_quiz("daily:2026-09-06")
    assert store.quiz("daily:2026-09-06")["sent_at"]


def test_lost_discord_response_does_not_repeat(store):
    session = FakeSession([requests.Timeout(), FakeResponse()])
    notifier = DiscordNotifier(WEBHOOK_URL, state_path=store.path, session=session)
    with pytest.raises(DiscordNotificationError):
        notifier.send_custom_notification("test", {"content": "hello"})
    with pytest.raises(DiscordNotificationError):
        notifier.send_custom_notification("test", {"content": "hello"})
    assert len(session.calls) == 1
    assert store.blocked_deliveries() == 1


def test_partial_quiz_send_resumes_only_unsent_message(store):
    session = FakeSession([FakeResponse(), FakeResponse(429, {"retry_after": 1000})])
    notifier = DiscordNotifier(WEBHOOK_URL, state_path=store.path, session=session, max_attempts=1)
    payload = {"embeds": [{"description": "x" * 3500}, {"description": "y" * 3500}]}
    with pytest.raises(DiscordNotificationError):
        notifier.send_custom_notification("quiz", payload)
    session.responses.append(FakeResponse())
    assert notifier.send_custom_notification("quiz", payload)
    assert len(session.calls) == 3
    assert session.calls[-1][1]["json"]["embeds"][0]["description"].startswith("y")
    assert not notifier.send_custom_notification("quiz", payload)


def test_split_preserves_spoiler_text_and_discord_limits():
    payload = {
        "embeds": [
            {
                "title": "quiz",
                "description": "q" * 1500,
                "fields": [{"name": "Answer", "value": "||" + "a" * 1550 + "||"}],
            }
            for _ in range(5)
        ]
    }
    messages = discord_messages(payload)
    assert len(messages) >= 3
    for message in messages:
        size = 0
        for embed in message["embeds"]:
            size += len(embed["title"]) + len(embed["description"])
            for field in embed["fields"]:
                assert len(field["value"]) <= 1024
                assert field["value"].startswith("||") and field["value"].endswith("||")
                size += len(field["name"]) + len(field["value"])
        assert size <= 6000


def test_manual_calendar_edits_are_repaired(store):
    service = FakeCalendarService()
    sync = GoogleCalendarSync(service, state_store=store)
    sync.sync_items([academic_item()])
    next(iter(service.event_store.values()))["summary"] = "accidental edit"
    report = sync.sync_items([academic_item()])
    assert len(report.updated) == 1
    assert next(iter(service.event_store.values()))["summary"] != "accidental edit"


def test_calendar_write_succeeds_but_local_confirmation_crashes(store):
    service = FakeCalendarService()
    sync = GoogleCalendarSync(service, state_store=store)
    with patch.object(store, "calendar_confirm", side_effect=OSError("disk unavailable")):
        with pytest.raises(OSError):
            sync.sync_items([academic_item()])
    assert len(service.event_insert_calls) == 1
    assert not store.pending_deliveries()
    sync.sync_items([academic_item()])
    assert len(service.event_insert_calls) == 1
    assert len(store.pending_deliveries()) == 1
    sync.sync_items([academic_item()])
    assert len(store.pending_deliveries()) == 1


def test_calendar_partial_failure_keeps_earlier_delivery_intent(store):
    service = FakeCalendarService()
    sync = GoogleCalendarSync(service, state_store=store)
    original = store.calendar_confirm
    count = 0

    def confirm(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("disk unavailable")
        return original(*args, **kwargs)

    with patch.object(store, "calendar_confirm", side_effect=confirm):
        with pytest.raises(OSError):
            sync.sync_items([academic_item(), academic_item(uid="second")])
    assert len(store.pending_deliveries()) == 1
    sync.sync_items([academic_item(), academic_item(uid="second")])
    assert len(service.event_insert_calls) == 2
    assert len(store.pending_deliveries()) == 2


def test_calendar_ids_are_deterministic_and_valid(store):
    first, second = FakeCalendarService(), FakeCalendarService()
    GoogleCalendarSync(first).sync_items([academic_item()])
    GoogleCalendarSync(second).sync_items([academic_item()])
    event_id = first.event_insert_calls[0]["body"]["id"]
    assert event_id == second.event_insert_calls[0]["body"]["id"]
    assert 5 <= len(event_id) <= 1024 and set(event_id) <= set("0123456789abcdefghijklmnopqrstuv")


def test_freebusy_reads_every_page_and_batches_at_fifty():
    service = Mock()
    service.calendarList().list().execute.side_effect = [
        {"items": [{"id": str(i)} for i in range(50)], "nextPageToken": "next"},
        {"items": [{"id": "50"}]},
    ]
    calls = []

    def query(*, body):
        calls.append(body)
        return SimpleNamespace(
            execute=lambda: {"calendars": {item["id"]: {"busy": []} for item in body["items"]}}
        )

    service.freebusy().query.side_effect = query
    planner = object.__new__(StudyPlanner)
    planner.service, planner.timezone = service, SimpleNamespace(key="America/Toronto")
    start = datetime(2026, 9, 6, tzinfo=timezone.utc)
    assert planner._load_busy(start, start + timedelta(days=1), "study") == []
    assert [len(call["items"]) for call in calls] == [50, 1]


@pytest.mark.parametrize(
    "coverage", [{}, {"primary": {"errors": [{"reason": "forbidden"}]}}, {"primary": {}}]
)
def test_freebusy_incomplete_coverage_fails_closed(coverage):
    service = Mock()
    service.calendarList().list().execute.return_value = {"items": [{"id": "primary"}]}
    service.freebusy().query().execute.return_value = {"calendars": coverage}
    planner = object.__new__(StudyPlanner)
    planner.service, planner.timezone = service, SimpleNamespace(key="America/Toronto")
    start = datetime(2026, 9, 6, tzinfo=timezone.utc)
    with pytest.raises(CalendarAPIError):
        planner._load_busy(start, start + timedelta(days=1), "study")


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0"])
def test_settings_reject_invalid_timeouts(value):
    with pytest.raises(ValueError):
        Settings(discord_timeout=value)


def test_invalid_timezone_rejected():
    with pytest.raises(ValueError):
        Settings(timezone="not/a-zone")


def test_run_lock_prevents_overlapping_process_entry(store):
    with store.run_lock():
        with pytest.raises(RuntimeError, match="Another Attendr"):
            with StateStore(store.path).run_lock():
                pass


def test_legacy_calendar_event_is_adopted_without_notification(store):
    service = FakeCalendarService()
    GoogleCalendarSync(service).sync_items([academic_item()])
    GoogleCalendarSync(service, state_store=store).sync_items([academic_item()])
    assert len(store.tracked_assignments()) == 1
    assert store.pending_deliveries() == []


def test_cached_lecture_quiz_delivers_without_material_download(store):
    from academic_assistant.lecture_quiz import LectureQuizRunner

    store.save_quiz("lecture-quiz:session", {"content": "cached quiz"})
    schedule = Mock()
    schedule.ended_lecture_sessions.return_value = [
        (Mock(session_id=Mock(return_value="session")), datetime.now(timezone.utc))
    ]
    canvas, ai = Mock(), Mock()
    notifier = DiscordNotifier(
        WEBHOOK_URL, state_path=store.path, session=FakeSession([FakeResponse()])
    )
    runner = LectureQuizRunner(
        canvas, ai, notifier, schedule, materials_directory=store.path.parent, state_path=store.path
    )
    assert runner.run().sent == 1
    canvas.download_lecture_materials.assert_not_called()
    ai.generate_hybrid_quiz.assert_not_called()
    assert runner.run().already_sent == 1


def test_force_lecture_quiz_does_not_consume_quiz_state(store):
    from academic_assistant.lecture_quiz import LectureQuizRunner

    schedule = Mock()
    schedule.ended_lecture_sessions.return_value = [
        (Mock(session_id=Mock(return_value="session")), datetime.now(timezone.utc))
    ]
    canvas, ai, notifier = Mock(), Mock(), Mock()
    canvas.download_lecture_materials.return_value = SimpleNamespace(warnings=(), materials=())
    runner = LectureQuizRunner(
        canvas, ai, notifier, schedule, materials_directory=store.path.parent, state_path=store.path
    )
    material = SimpleNamespace(
        uid="material", course_name="Math", title="Slides", content_sha256="a" * 64
    )
    with (
        patch.object(runner, "_select_materials", return_value=(material,)),
        patch.object(runner, "_materials_text", return_value="slides"),
        patch(
            "academic_assistant.lecture_quiz.hybrid_quiz_discord_payload",
            return_value={"content": "forced quiz"},
        ),
    ):
        assert runner.run(force=True).sent == 1
    assert store.quiz("lecture-quiz:session") is None


def test_tracked_assignment_reconciles_submission_after_lookahead(store):
    from academic_assistant.canvas_client import CanvasClient, CourseSummary, _CourseContext

    item = academic_item()
    service = FakeCalendarService()
    GoogleCalendarSync(service, state_store=store).sync_items([item])
    canvas = CanvasClient("https://canvas.example", "fake", canvas=Mock(), state_store=store)
    resource = Mock()
    resource.get_assignment.return_value = {
        "id": "42",
        "name": "Problem Set 1",
        "due_at": "2020-01-01T12:00:00Z",
        "submission": {"workflow_state": "submitted"},
    }
    context = _CourseContext(summary=CourseSummary(7, "Algorithms", None, None), resource=resource)
    with (
        patch.object(canvas, "validate_credentials", return_value=("1", "User")),
        patch.object(canvas, "_get_active_course_contexts", return_value=(context,)),
        patch.object(canvas, "_fetch_upcoming_items", return_value=()),
        patch.object(canvas, "_fetch_announcements", return_value=()),
    ):
        snapshot = canvas.fetch_snapshot()
    assert snapshot.complete
    assert len(snapshot.items) == 1 and snapshot.items[0].submitted is True


def test_tracked_assignment_404_preserves_calendar(store):
    from canvasapi.exceptions import ResourceDoesNotExist
    from academic_assistant.canvas_client import CanvasClient, CourseSummary, _CourseContext

    GoogleCalendarSync(FakeCalendarService(), state_store=store).sync_items([academic_item()])
    canvas = CanvasClient("https://canvas.example", "fake", canvas=Mock(), state_store=store)
    resource = Mock()
    resource.get_assignment.side_effect = ResourceDoesNotExist("not found")
    context = _CourseContext(summary=CourseSummary(7, "Algorithms", None, None), resource=resource)
    with (
        patch.object(canvas, "validate_credentials", return_value=("1", "User")),
        patch.object(canvas, "_get_active_course_contexts", return_value=(context,)),
        patch.object(canvas, "_fetch_upcoming_items", return_value=()),
        patch.object(canvas, "_fetch_announcements", return_value=()),
    ):
        snapshot = canvas.fetch_snapshot()
    assert not snapshot.complete and not snapshot.removed_uids


def test_pending_insert_retries_same_id_after_timeout_and_404(store):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    service = Mock()
    service.events().list().execute.return_value = {"items": []}
    service.events().get().execute.side_effect = HttpError(Response({"status": "404"}), b"{}")
    ids = []

    def insert(**kwargs):
        ids.append(kwargs["body"]["id"])

        def execute():
            if len(ids) == 1:
                raise requests.Timeout("lost connection")
            return kwargs["body"]

        return SimpleNamespace(execute=execute)

    service.events().insert.side_effect = insert
    sync = GoogleCalendarSync(service, state_store=store)
    with pytest.raises(requests.Timeout):
        sync.sync_items([academic_item()])
    sync.sync_items([academic_item()])
    assert len(ids) == 2 and ids[0] == ids[1]
    assert len(store.pending_deliveries()) == 1


def test_legacy_env_paths_import_into_shared_database(tmp_path):
    import os

    legacy = tmp_path / "old.db"
    StateStore(legacy).mark_sent("event", "fingerprint")
    with patch.dict(
        os.environ,
        {"DISCORD_WEBHOOK_URL": WEBHOOK_URL, "NOTIFICATION_STATE_DB": str(legacy)},
        clear=True,
    ):
        notifier = DiscordNotifier.from_env(tmp_path / "absent.env")
    assert notifier.state.path == tmp_path / "data" / "attendr.db"
    assert notifier.state.was_sent("event", "fingerprint")


def test_database_newer_than_application_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY); INSERT INTO schema_migrations VALUES(999);"
        )
    with pytest.raises(ValueError, match="Unsupported"):
        StateStore(path)


def test_notification_content_can_change_back_to_previous_value(store):
    session = FakeSession([FakeResponse() for _ in range(4)])
    notifier = DiscordNotifier(WEBHOOK_URL, state_path=store.path, session=session)
    for content in ("A", "B", "A", "B"):
        assert notifier.send_custom_notification("mutable-announcement", {"content": content})
        assert not notifier.send_custom_notification("mutable-announcement", {"content": content})
    assert len(session.calls) == 4


def test_public_calendar_notfound_falls_back_to_paginated_events():
    from zoneinfo import ZoneInfo

    service = Mock()
    service.calendarList().list().execute.return_value = {"items": [{"id": "holidays"}]}
    service.freebusy().query().execute.return_value = {
        "calendars": {"holidays": {"errors": [{"reason": "notFound"}]}}
    }
    service.events().list().execute.side_effect = [
        {"items": [{"transparency": "transparent"}], "nextPageToken": "next"},
        {"items": [{"start": {"date": "2026-09-07"}, "end": {"date": "2026-09-08"}}]},
    ]
    planner = object.__new__(StudyPlanner)
    planner.service, planner.timezone = service, ZoneInfo("America/Toronto")
    start = datetime(2026, 9, 6, tzinfo=timezone.utc)
    result = planner._load_busy(start, start + timedelta(days=3), "study")
    assert len(result) == 1
    assert result[0].start.isoformat() == "2026-09-07T00:00:00-04:00"
    assert result[0].end.isoformat() == "2026-09-08T00:00:00-04:00"


def test_failed_lecture_quiz_is_reported_and_remains_retryable(store):
    from academic_assistant.ai_assistant import AIProviderError
    from academic_assistant.lecture_quiz import LectureQuizRunner

    schedule = Mock()
    schedule.ended_lecture_sessions.return_value = [
        (Mock(session_id=Mock(return_value="session")), datetime.now(timezone.utc))
    ]
    canvas, ai, notifier = Mock(), Mock(), Mock()
    canvas.download_lecture_materials.return_value = SimpleNamespace(warnings=(), materials=())
    ai.generate_hybrid_quiz.side_effect = AIProviderError(
        "Gemini returned an empty response for hybrid quiz generation."
    )
    runner = LectureQuizRunner(
        canvas, ai, notifier, schedule, materials_directory=store.path.parent, state_path=store.path
    )
    with (
        patch.object(runner, "_select_materials", return_value=(Mock(),)),
        patch.object(runner, "_materials_text", return_value="lecture"),
    ):
        for _ in range(2):
            report = runner.run()
            assert report.failed == 1
            assert report.sent == 0
            assert "empty response" in report.warnings[0]
    assert ai.generate_hybrid_quiz.call_count == 2
    notifier.send_custom_notification.assert_not_called()
    assert store.quiz("lecture-quiz:session") is None
