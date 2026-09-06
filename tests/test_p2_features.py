from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import os

import pytest
from academic_assistant.preferences import Preferences
from academic_assistant.review import ReviewQueue
from academic_assistant.triage import AnnouncementTriage
from academic_assistant.state_store import StateStore
from academic_assistant.study_planner import StudyPlanner
from academic_assistant.notifier import DiscordNotifier
from academic_assistant.settings import Settings
from test_notifier import make_announcement, FakeSession, FakeResponse, WEBHOOK_URL
from test_study_planner import task


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "attendr.db")


def seed_quiz(store, count=5):
    store.save_quiz(
        "daily-quiz:2026-10-31",
        {
            "content": "Trees",
            "embeds": [
                {
                    "title": f"Question {index}",
                    "description": "Explain balancing",
                    "fields": [{"name": "Reveal", "value": "||answer||"}],
                }
                for index in range(count)
            ],
        },
    )
    with store.connect() as db:
        db.execute("UPDATE quiz_history SET sent_at='2026-11-01T02:00:00+00:00'")


def test_review_import_uses_local_civil_date_and_is_idempotent(store):
    seed_quiz(store)
    queue = ReviewQueue(store)
    assert queue.import_quizzes() == 5
    assert queue.import_quizzes() == 0
    assert not queue.due(date(2026, 10, 31))
    assert len(queue.due(date(2026, 11, 1), 10)) == 5


def test_unsent_quiz_does_not_create_reviews(store):
    store.save_quiz("unsent", {"embeds": [{"title": "not yet sent"}]})
    assert ReviewQueue(store).import_quizzes() == 0


@pytest.mark.parametrize(("rating", "interval"), [("again", 1), ("good", 3), ("easy", 7)])
def test_grades_schedule_and_reject_duplicate_grade(store, rating, interval):
    seed_quiz(store, 1)
    queue = ReviewQueue(store)
    queue.import_quizzes()
    card = queue.due(date(2026, 11, 1))[0]
    assert queue.grade(card["card_id"], rating, 1, date(2026, 11, 1)) == interval
    with pytest.raises(ValueError):
        queue.grade(card["card_id"], rating, 1, date(2026, 11, 1))
    assert not queue.due(date(2026, 11, 1))
    assert queue.due(date(2026, 11, 1) + timedelta(days=interval))[0]["revision"] == 2


def test_grade_early_or_invalid_rating_preserves_state(store):
    seed_quiz(store, 1)
    queue = ReviewQueue(store)
    queue.import_quizzes()
    card = queue.due(date(2026, 11, 1))[0]
    for rating, today in [("good", date(2026, 10, 31)), ("invalid", date(2026, 11, 1))]:
        with pytest.raises(ValueError):
            queue.grade(card["card_id"], rating, 1, today)
    assert queue.due(date(2026, 11, 1))[0]["revision"] == 1


def test_daily_review_limit_and_no_implicit_learning(store):
    seed_quiz(store)
    queue = ReviewQueue(store)
    session = FakeSession([FakeResponse() for _ in range(3)])
    notifier = DiscordNotifier(WEBHOOK_URL, state_path=store.path, session=session)
    today = date(2026, 11, 1)
    assert queue.send_due(notifier, today, 3) == 3
    assert queue.send_due(notifier, today, 3) == 0
    assert len(queue.due(today, 10)) == 5
    first = queue.due(today)[0]
    queue.grade(first["card_id"], "good", 1, today)
    assert queue.send_due(notifier, today, 3) == 0
    assert len(session.calls) == 3


def test_forced_review_does_not_consume_daily_selection_or_delivery(store):
    seed_quiz(store, 1)
    queue = ReviewQueue(store)
    notifier = DiscordNotifier(
        WEBHOOK_URL, state_path=store.path, session=FakeSession([FakeResponse(), FakeResponse()])
    )
    assert queue.send_due(notifier, date(2026, 11, 1), force=True) == 1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM review_batches").fetchone()[0] == 0
    assert queue.send_due(notifier, date(2026, 11, 1)) == 1


def test_mute_is_explicit_and_critical_content_bypasses_it(store):
    triage = AnnouncementTriage(Preferences(muted_topics=["newsletter"]), store)
    informational = make_announcement(
        title="Weekly newsletter", message_text="Campus photos and stories"
    )
    assert triage.classify(informational).muted
    for body in ["The exam deadline changed", "Submit this assignment", "The class is cancelled"]:
        decision = triage.classify(replace(informational, message_text=body))
        assert not decision.muted
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM announcement_triage").fetchone()[0] == 1


def test_default_triage_never_mutes(store):
    assert not AnnouncementTriage(Preferences(), store).classify(make_announcement()).muted


@pytest.mark.parametrize(
    "overrides",
    [
        {"study_windows": {"0": [[600, 900]]}},
        {"study_windows": {str(day): [[800, 600]] for day in range(7)}},
        {"study_windows": {str(day): [[600, 900], [800, 1000]] for day in range(7)}},
        {"study_minutes": {"exam": 0}},
        {"course_priorities": {"1": 9}},
        {"muted_topics": [" "]},
    ],
)
def test_preferences_reject_invalid_configuration(overrides):
    with pytest.raises(ValueError):
        Preferences(**overrides)


def test_configured_study_windows_and_durations_are_used():
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    preferences = Preferences(
        study_windows={str(day): [(600, 660)] for day in range(7)}, study_minutes={"assignment": 30}
    )
    planner = StudyPlanner(object(), preferences=preferences)
    sessions, warnings = planner._build_sessions(
        (task("assignment", "assignment", now + timedelta(days=9)),),
        [],
        set(),
        now.astimezone(planner.timezone),
    )
    assert sessions and not warnings
    assert all(
        item.due_at_local.hour == 10 and item.end_at - item.due_at == timedelta(minutes=30)
        for item in sessions
    )
    assert preferences.worker_profile()["windows"]["1"] == [(600, 660)]


def test_high_priority_course_gets_contested_slot():
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    preferences = Preferences(
        study_windows={str(day): [(600, 645)] for day in range(7)}, course_priorities={"2": 5}
    )
    planner = StudyPlanner(object(), preferences=preferences)
    due = now + timedelta(days=2)
    low = task("a-low", "assignment", due)
    high = replace(task("z-high", "assignment", due), course_id=2)
    sessions, _ = planner._build_sessions((low, high), [], set(), now.astimezone(planner.timezone))
    assert sessions[0].course_id == 2


def test_review_only_needs_no_canvas_google_or_gemini():
    import main

    plan = main.resolve_plan(main.build_parser().parse_args(["--review-only"]))
    assert plan.review and not plan.needs_canvas
    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": WEBHOOK_URL}, clear=True):
        Settings.from_env(plan)


def test_schema_upgrade_preserves_existing_notifications(store):
    store.mark_sent("event", "fingerprint")
    with store.connect() as db:
        db.execute("DELETE FROM schema_migrations WHERE version=2")
        for table in ("review_history", "review_cards", "review_batches", "announcement_triage"):
            db.execute(f"DROP TABLE {table}")
    upgraded = StateStore(store.path)
    assert upgraded.was_sent("event", "fingerprint")
    with upgraded.connect() as db:
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2


def test_grading_cancels_unsent_reminder_for_old_revision(store):
    seed_quiz(store, 1)
    queue = ReviewQueue(store)
    notifier = DiscordNotifier(
        WEBHOOK_URL,
        state_path=store.path,
        session=FakeSession([FakeResponse(429, {"retry_after": 1000})]),
        max_attempts=1,
    )
    from academic_assistant.notifier import DiscordNotificationError

    with pytest.raises(DiscordNotificationError):
        queue.send_due(notifier, date(2026, 11, 1))
    assert len(store.pending_deliveries()) == 1
    card = queue.due(date(2026, 11, 1))[0]
    queue.grade(card["card_id"], "good", card["revision"], date(2026, 11, 1))
    assert store.pending_deliveries() == []


def test_triage_presentation_does_not_repeat_legacy_notification(store):
    announcement = make_announcement()
    session = FakeSession([FakeResponse()])
    notifier = DiscordNotifier(WEBHOOK_URL, state_path=store.path, session=session)
    assert notifier.send_announcement_alert(announcement)
    decision = AnnouncementTriage(Preferences(), store).classify(announcement)
    assert not notifier.send_announcement_alert(announcement, triage=decision)
    assert len(session.calls) == 1


def test_review_only_pipeline_does_not_initialize_other_integrations(store, tmp_path):
    import main

    seed_quiz(store, 1)
    with store.connect() as db:
        db.execute("UPDATE quiz_history SET sent_at='2000-01-01 12:00:00'")
    notifier = DiscordNotifier(
        WEBHOOK_URL, state_path=store.path, session=FakeSession([FakeResponse()])
    )
    args = main.build_parser().parse_args(["--review-only"])
    with (
        patch.object(main, "PROJECT_ROOT", tmp_path),
        patch.dict(os.environ, {"ATTENDR_DB": str(store.path)}, clear=True),
        patch.object(main.DiscordNotifier, "from_env", return_value=notifier),
        patch.object(
            main.CanvasClient, "from_env", side_effect=AssertionError("Canvas must not be used")
        ),
        patch.object(
            main.AIAssistant, "from_env", side_effect=AssertionError("Gemini must not be used")
        ),
    ):
        results = main.run_pipeline(args)
    assert all(result.status == "ok" for result in results)
    assert (
        next(result for result in results if result.name == "Spaced review").detail
        == "1 review question(s) sent"
    )


def test_nonexistent_spring_clock_window_is_not_scheduled():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Toronto")
    preferences = Preferences(
        study_windows={str(day): ([(120, 180)] if day == 6 else []) for day in range(7)}
    )
    planner = StudyPlanner(object(), preferences=preferences)
    now = datetime(2026, 3, 8, 0, tzinfo=tz)
    assert planner._find_slot(now.date(), now + timedelta(days=1), 30, [], set(), now) is None


def test_fall_clock_change_keeps_real_session_duration():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Toronto")
    preferences = Preferences(
        study_windows={str(day): ([(90, 150)] if day == 6 else []) for day in range(7)},
        study_minutes={"assignment": 60},
    )
    planner = StudyPlanner(object(), preferences=preferences)
    now = datetime(2026, 11, 1, 0, tzinfo=tz)
    sessions, _ = planner._build_sessions(
        (task("fall", "assignment", now + timedelta(days=2)),), [], set(), now
    )
    assert len(sessions) == 1
    assert sessions[0].end_at - sessions[0].due_at == timedelta(minutes=60)
    assert sessions[0].due_at_local.hour == 1
