"""Regression coverage for the remaining September reliability audit."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
import subprocess

import pytest

from academic_assistant.ai_assistant import MajorDeadline
from academic_assistant.cloud_state import CloudStateClient, CloudStateError
from academic_assistant.course_schedule import CourseSchedule
from academic_assistant.deadline_candidates import preprocess_deadlines
from academic_assistant.deadline_grounding import ground_deadline
from academic_assistant.notifier import DiscordNotifier, DiscordNotificationError
from academic_assistant.state_store import StateStore
from academic_assistant.calendar_sync import GoogleCalendarSync
from scripts.cloud_run import run_with_lease
from test_calendar_sync import FakeCalendarService, academic_item
from test_notifier import FakeResponse, FakeSession, WEBHOOK_URL

UTC = timezone.utc


@pytest.mark.parametrize(
    "suffix", ["— cancelled", "— canceled", "(example only, not a real deadline)"]
)
def test_excluded_deadlines_cannot_escape_through_ai_or_cache(suffix):
    source = f"Midterm Exam: October 20, 2026 {suffix}"
    parsed = preprocess_deadlines(source, reference_date=date(2026, 9, 1), max_chars=1000)
    assert parsed.deterministic_complete and not parsed.candidates
    deadline = MajorDeadline(
        title="Midterm Exam",
        due_date="2026-10-20",
        kind="exam",
        source_evidence="Midterm Exam: October 20, 2026",
    )
    assert not ground_deadline(source, deadline).accepted


def schedule_data():
    return {
        "timezone": "America/Toronto",
        "term": {"start_date": "2026-09-21", "end_date": "2026-09-21"},
        "courses": [
            {
                "key": "web",
                "name": "Web",
                "match": ["web"],
                "sessions": [{"weekday": 0, "start": "13:00", "end": "14:00", "type": "lecture"}],
            }
        ],
    }


def test_authoritative_no_class_correction_removes_future_managed_lecture():
    data = schedule_data()
    service = FakeCalendarService()
    sync = GoogleCalendarSync(service, calendar_id="test")
    sync.sync_items(CourseSchedule(data).scheduled_class_items())
    data["term"]["no_class"] = [{"start": "2026-09-21", "end": "2026-09-21"}]
    sync.sync_items(
        CourseSchedule(data).scheduled_class_items(),
        authoritative_sources={"class_schedule"},
        now=datetime(2026, 9, 20, tzinfo=UTC),
    )
    assert not service.event_store


def test_pruning_preserves_past_unscanned_and_failed_course_events():
    service = FakeCalendarService()
    sync = GoogleCalendarSync(service, calendar_id="test")
    items = [
        academic_item(uid="past", source="syllabus_deadline"),
        academic_item(
            uid="protected",
            source="syllabus_deadline",
            course_id=2,
            due_at=datetime(2027, 1, 1, tzinfo=UTC),
        ),
        academic_item(
            uid="unscanned", source="assignment", due_at=datetime(2027, 1, 1, tzinfo=UTC)
        ),
    ]
    sync.sync_items(items)
    sync.sync_items(
        [],
        authoritative_sources={"syllabus_deadline"},
        protected_course_ids={2},
        now=datetime(2026, 9, 20, tzinfo=UTC),
    )
    assert len(service.event_store) == 3


@pytest.mark.parametrize(
    "field,value", [("weekday", 7), ("weekday", True), ("end", "12:00"), ("type", "unknown")]
)
def test_invalid_timetable_rejected(field, value):
    data = schedule_data()
    data["courses"][0]["sessions"][0][field] = value
    with pytest.raises(ValueError):
        CourseSchedule(data)


def test_expiry_is_persisted_and_rate_limit_retry_stops_at_deadline(tmp_path):
    now = [datetime(2026, 9, 20, 12, tzinfo=UTC)]
    session = FakeSession([FakeResponse(429, {"retry_after": 2}), FakeResponse()])
    notifier = DiscordNotifier(
        WEBHOOK_URL,
        state_path=tmp_path / "state.db",
        session=session,
        now_provider=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)),
    )
    with pytest.raises(DiscordNotificationError, match="expired"):
        notifier.send_custom_notification(
            "lecture",
            {"content": "review"},
            destination="lecture_quizzes",
            expires_at=now[0].timestamp() + 1,
        )
    assert len(session.calls) == 1
    assert notifier.state.blocked_deliveries() == 0


def test_lecture_outbox_uses_absolute_expiry_not_enqueue_time(tmp_path):
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    session = FakeSession([])
    notifier = DiscordNotifier(
        WEBHOOK_URL, state_path=tmp_path / "state.db", session=session, now_provider=lambda: now
    )
    notifier.state.enqueue_messages(
        "lecture", "f", "lecture_quizzes", [{"content": "old"}], expires_at=now.timestamp() - 1
    )
    assert notifier.flush_pending() == 0
    assert not session.calls


def test_disposable_cache_eviction_batches_uploads_without_losing_markers(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.db")
    client = Mock()
    monkeypatch.setattr("academic_assistant.cloud_state.client_from_env", lambda path: client)
    store.cache_set("pending_study_sync", {"durable": True})
    client.upload.reset_mock()
    for index in range(4):
        store.cache_set(f"ask-extract-v1:{index}", {"text": "x" * 700_000})
    client.upload.assert_not_called()
    assert store.cache_get("ask-extract-v1:0") is None
    assert store.cache_get("pending_study_sync") == {"durable": True}
    store.mark_sent("event", "f")
    client.upload.assert_called_once()
    store.maintain()
    assert store.was_sent("event", "f")


def test_explicit_assignment_retirement_is_reversible(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.retire_assignment("canvas:assignment:1:2")
    assert store.retired_assignments() == {"canvas:assignment:1:2"}
    store.retire_assignment("canvas:assignment:1:2", restore=True)
    assert not store.retired_assignments()


def test_lease_loss_stops_child_process_group(monkeypatch):
    client = Mock()
    client.request.side_effect = [None, CloudStateError("lost")]
    child = Mock(pid=123)
    child.wait.side_effect = [subprocess.TimeoutExpired("cmd", 30), 1]
    child.poll.return_value = None
    monkeypatch.setattr("scripts.cloud_run.subprocess.Popen", lambda *a, **k: child)
    clock = iter([0, 301])
    monkeypatch.setattr("scripts.cloud_run.time.monotonic", lambda: next(clock))
    kill = Mock()
    monkeypatch.setattr("scripts.cloud_run.os.killpg", kill)
    with pytest.raises(CloudStateError):
        run_with_lease(client, ["test"])
    kill.assert_called_once()
    assert client.request.call_count == 2


def test_oversized_checkpoint_fails_before_upload(monkeypatch):
    client = CloudStateClient("https://test", "secret", "key", "a" * 32, 0)
    monkeypatch.setattr(
        "academic_assistant.cloud_state.zlib.compress", lambda data: b"x" * (8 * 1024 * 1024)
    )
    client.request = Mock()
    with pytest.raises(CloudStateError, match="limit"):
        client._upload_plaintext(b"small")
    client.request.assert_not_called()


def test_large_quiz_context_keeps_all_sources_within_budget():
    from academic_assistant.lecture_content import LectureContentSource, LectureContentBundle

    sources = tuple(
        LectureContentSource(
            reference_id=f"S{i}",
            canvas_uid=str(i),
            canvas_source_id=str(i),
            title=f"Slides {i}",
            content_hash="hash",
            text_hash="hash",
            location=None,
            extraction_method="text",
            safe_url=None,
            text="\n\n".join(f"[Slide {j}]\n" + "A useful concept. " * 40 for j in range(300)),
        )
        for i in range(3)
    )
    bundle = LectureContentBundle(
        "course",
        "Course",
        "session",
        "2026-09-20",
        "lecture",
        "Slides",
        sources,
        "\n".join(s.text for s in sources),
        "hash",
    )
    value = bundle.summary_context(120_000)
    assert len(value) <= 120_000
    assert all(f'"source_id": "S{i}"' in value for i in range(3))
    assert "[Slide 0]" in value and "[Slide 299]" in value


def test_smoke_test_holds_planner_lease_only_during_session_sync(monkeypatch):
    from scripts import test_study_buttons as smoke
    from types import SimpleNamespace

    monkeypatch.setattr(smoke, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("STUDY_WORKER_URL", "https://worker.test")
    monkeypatch.setenv("STUDY_SYNC_SECRET", "secret")
    service = Mock()
    service.events.return_value.insert.return_value.execute.return_value = {"id": "event"}
    monkeypatch.setattr(
        smoke, "GoogleCalendarAuthenticator", lambda *a, **k: Mock(build_service=lambda: service)
    )
    monkeypatch.setattr(
        smoke,
        "GoogleCalendarSync",
        lambda *a, **k: Mock(resolve_calendar=lambda: ("study", "Study")),
    )
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(smoke.requests, "post", post)
    assert smoke.main() == 0
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == ["acquire", "sync", "release", "run"]
    assert (
        calls[0][1]["json"]["token"]
        == calls[1][1]["json"]["plan_token"]
        == calls[2][1]["json"]["token"]
    )


def test_unknown_legacy_lecture_expiry_is_discarded_but_daily_quiz_is_preserved(tmp_path):
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    session = FakeSession([FakeResponse()])
    notifier = DiscordNotifier(
        WEBHOOK_URL, state_path=tmp_path / "state.db", session=session, now_provider=lambda: now
    )
    notifier.state.enqueue_messages(
        "lecture_quizzes:custom:lecture-quiz:old",
        "f",
        "lecture_quizzes",
        [{"content": "old lecture"}],
    )
    notifier.state.enqueue_messages(
        "lecture_quizzes:custom:daily-quiz:today", "f", "lecture_quizzes", [{"content": "daily"}]
    )
    assert notifier.flush_pending() == 1
    assert session.calls[0][1]["json"]["content"] == "daily"


def test_retry_cannot_extend_original_delivery_expiry(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.enqueue_messages("key", "f", "lecture_quizzes", [{"content": "review"}], expires_at=100)
    store.enqueue_messages("key", "f", "lecture_quizzes", [{"content": "review"}], expires_at=200)
    assert store.pending_deliveries()[0]["expires_at"] == 100
