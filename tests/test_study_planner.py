from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant import AcademicItem, BusyInterval, StudyPlanner
from test_calendar_sync import FakeCalendarService

UTC = timezone.utc


def task(uid: str, kind: str, due: datetime) -> AcademicItem:
    return AcademicItem(
        uid=uid,
        source="assignment",
        source_id=uid,
        course_id=1,
        course_name="Algorithms",
        title="Assessment",
        kind=kind,
        due_at=due,
        due_at_local=due,
        end_at=None,
        all_day=False,
        html_url=None,
        updated_at=None,
        points_possible=None,
        submission_types=(),
        description_html=None,
    )


class StudyPlannerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
        self.planner = StudyPlanner(object(), now_provider=lambda: self.now)

    def test_assignment_uses_three_short_sessions_once_per_day(self):
        due = datetime(2026, 9, 16, 23, 59, tzinfo=UTC)

        sessions, warnings = self.planner._build_sessions(
            (task("assignment-1", "assignment", due),),
            [],
            set(),
            self.now.astimezone(self.planner.timezone),
        )

        self.assertEqual(len(sessions), 3)
        self.assertEqual(warnings, ())
        self.assertEqual(len({item.due_at_local.date() for item in sessions}), 3)
        self.assertTrue(
            all((item.end_at - item.due_at) == timedelta(minutes=45) for item in sessions)
        )

    def test_exam_sessions_are_longer_and_never_use_thursday(self):
        due = datetime(2026, 9, 28, 18, 0, tzinfo=UTC)

        sessions, _ = self.planner._build_sessions(
            (task("exam-1", "exam", due),),
            [],
            set(),
            self.now.astimezone(self.planner.timezone),
        )

        self.assertEqual(len(sessions), 5)
        self.assertTrue(
            all((item.end_at - item.due_at) == timedelta(minutes=60) for item in sessions)
        )
        self.assertFalse(any(item.due_at_local.weekday() == 3 for item in sessions))

    def test_existing_calendar_busy_time_moves_session(self):
        due = datetime(2026, 9, 12, 23, 59, tzinfo=UTC)
        blocked_start = datetime(2026, 9, 8, 18, 45, tzinfo=self.planner.timezone)
        busy = [
            BusyInterval(
                blocked_start.astimezone(UTC), (blocked_start + timedelta(hours=2)).astimezone(UTC)
            )
        ]

        sessions, _ = self.planner._build_sessions(
            (task("quiz-1", "quiz", due),),
            busy,
            set(),
            self.now.astimezone(self.planner.timezone),
        )

        self.assertTrue(all(item.due_at_local != blocked_start for item in sessions))

    def test_completed_session_is_not_recreated(self):
        due = datetime(2026, 9, 16, 23, 59, tzinfo=UTC)
        study_task = task("assignment-1", "assignment", due)
        completed = {self.planner._session_uid(study_task.uid, 0)}

        sessions, _ = self.planner._build_sessions(
            (study_task,),
            [],
            completed,
            self.now.astimezone(self.planner.timezone),
        )

        self.assertEqual(len(sessions), 2)

    def test_waiting_completion_operation_does_not_block_plan_and_prevents_recreation(self):
        due = datetime(2026, 9, 16, 23, 59, tzinfo=UTC)
        study_task = task("assignment-1", "assignment", due)
        session_id = self.planner._session_uid(study_task.uid, 0)
        remote = Mock()
        remote.get_state.return_value = {
            "completed_tasks": [],
            "completed_sessions": [],
            "rescheduled_sessions": {},
            "operations": [
                {
                    "interaction_id": "123",
                    "task_uid": study_task.uid,
                    "session_id": session_id,
                    "action": "complete",
                    "status": "effects_pending",
                }
            ],
        }
        planner = StudyPlanner(FakeCalendarService(), remote=remote, now_provider=lambda: self.now)
        with patch.object(planner, "_load_busy", return_value=[]):
            report = planner.sync((study_task,), inputs_complete=True)

        self.assertEqual(len(report.sessions), 2)
        remote.sync_sessions.assert_called_once()
        remote.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
