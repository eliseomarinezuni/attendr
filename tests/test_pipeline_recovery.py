"""Pipeline-level regression tests with all external integrations stubbed."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import main
from academic_assistant.canvas_client import CanvasSnapshot, CourseSummary
from academic_assistant.preferences import Preferences
from academic_assistant.course_schedule import CourseSchedule
from test_calendar_sync import academic_item
from test_audit_completion import schedule_data


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTENDR_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("STUDY_WORKER_URL", "")
    monkeypatch.setattr(main.Preferences, "load", lambda *args: Preferences())
    schedule = CourseSchedule(schedule_data())
    monkeypatch.setattr(main.CourseSchedule, "load", lambda *args: schedule)
    now = datetime.now(timezone.utc)
    items = tuple(
        academic_item(
            uid=f"item:{i}",
            course_id=i,
            due_at=now + timedelta(hours=2),
            due_at_local=now + timedelta(hours=2),
        )
        for i in (1, 2)
    )
    courses = tuple(
        CourseSummary(id=i, name=f"Course {i}", course_code=f"C{i}", html_url=None) for i in (1, 2)
    )
    snapshot = CanvasSnapshot("1", "User", courses, items, (), (), now)
    canvas = Mock()
    canvas.fetch_snapshot.return_value = snapshot
    canvas.get_recent_announcements.return_value = ()
    monkeypatch.setattr(main.CanvasClient, "from_env", lambda *a, **k: canvas)
    auth = Mock()
    monkeypatch.setattr(main, "GoogleCalendarAuthenticator", lambda *a, **k: auth)
    materials = NS(
        items=(),
        materials_found=1,
        materials_analyzed=1,
        cached_materials_reused=0,
        warnings=(),
        complete=True,
        blocking_warnings=(),
    )
    material_sync = Mock(sync=Mock(return_value=materials))
    monkeypatch.setattr(main.CourseMaterialsSync, "from_env", lambda *a, **k: material_sync)
    dates = Mock(
        sync=Mock(return_value=NS(items=(), analyzed=1, cached=0, warnings=(), complete=True))
    )
    monkeypatch.setattr(main, "AnnouncementDatesSync", lambda *a, **k: dates)
    ai = Mock()
    monkeypatch.setattr(main.AIAssistant, "from_env", lambda *a, **k: ai)
    calendar_report = NS(created=(), updated=(), skipped=(), deleted=())
    calendar = Mock(sync_items=Mock(return_value=calendar_report))
    monkeypatch.setattr(main.GoogleCalendarSync, "from_env", lambda *a, **k: calendar)
    planner = Mock(sync=Mock(return_value=NS(sessions=(), calendar=calendar_report, warnings=())))
    monkeypatch.setattr(main.StudyPlanner, "from_env", lambda *a, **k: planner)
    notifier = Mock(
        flush_pending=Mock(return_value=0),
        send_assignment_alert=Mock(return_value=True),
        send_custom_notification=Mock(return_value=True),
    )
    monkeypatch.setattr(main.DiscordNotifier, "from_env", lambda *a, **k: notifier)
    reviews = Mock(
        process=Mock(
            return_value=NS(
                warnings=(),
                summaries_failed=0,
                summaries_waiting_for_slides=0,
                summaries_deferred=0,
                summaries_sent=1,
                summaries_already_sent=0,
                quizzes_failed=0,
                quizzes_waiting_for_slides=0,
                quizzes_sent=1,
                quizzes_already_sent=0,
            )
        )
    )
    monkeypatch.setattr(main, "LectureQuizRunner", lambda *a, **k: reviews)
    return NS(
        canvas=canvas,
        snapshot=snapshot,
        calendar=calendar,
        planner=planner,
        materials=material_sync,
        dates=dates,
        notifier=notifier,
        reviews=reviews,
        ai=ai,
    )


def test_one_failed_course_does_not_freeze_healthy_calendar(pipeline):
    pipeline.canvas.fetch_snapshot.return_value = replace(
        pipeline.snapshot, complete=False, incomplete_course_ids=(2,)
    )
    results = main.run_pipeline(main.build_parser().parse_args(["--sync-only"]))
    call = pipeline.calendar.sync_items.call_args
    assert call is not None
    assert all(item.course_id != 2 for item in call.args[0])
    assert call.kwargs["protected_course_ids"] == {2}
    assert "class_schedule" not in call.kwargs["authoritative_sources"]
    assert next(result for result in results if result.name == "Calendar").status == "ok"
    # Planner still needs complete availability/task data before global replacement.
    pipeline.planner.sync.assert_not_called()


def test_complete_default_run_keeps_independent_outputs(pipeline):
    results = main.run_pipeline(main.build_parser().parse_args([]))
    assert all(result.status in {"ok", "skipped"} for result in results)
    pipeline.notifier.send_daily_digest.assert_called_once()
    pipeline.reviews.process.assert_called_once()
    assert {
        "class_schedule",
        "university_schedule",
        "syllabus_deadline",
    } == pipeline.calendar.sync_items.call_args.kwargs["authoritative_sources"]


@pytest.mark.parametrize("component", ["materials", "dates", "canvas", "reviews"])
@pytest.mark.parametrize(
    "error", [ValueError("invalid source"), RuntimeError("unexpected failure")]
)
def test_dependency_failures_preserve_calendar_but_do_not_abort_reporting(
    pipeline, component, error
):
    target = getattr(pipeline, component)
    method = {
        "materials": "sync",
        "dates": "sync",
        "canvas": "fetch_snapshot",
        "reviews": "process",
    }[component]
    getattr(target, method).side_effect = error
    results = main.run_pipeline(main.build_parser().parse_args([]))
    assert any(result.status == "failed" for result in results)
    if component in {"materials", "dates", "canvas"}:
        pipeline.calendar.sync_items.assert_not_called()
    else:
        pipeline.calendar.sync_items.assert_called_once()
    assert any(result.name == "Delivery outbox" for result in results)


def test_cached_daily_quiz_never_regenerates_after_delivery(pipeline, monkeypatch):
    monkeypatch.setattr(main, "quiz_discord_payload", lambda *a, **k: {"content": "quiz"})
    args = main.build_parser().parse_args(["--quiz-only", "--topic", "Trees"])
    main.run_pipeline(args)
    main.run_pipeline(args)
    pipeline.ai.generate_quiz.assert_called_once()
    pipeline.notifier.send_custom_notification.assert_called_once()
