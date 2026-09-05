from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant import CourseSchedule


class CourseScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schedule = CourseSchedule.load(ROOT / "data/course_schedule.json")

    def test_thursday_ethics_ends_at_two(self):
        ethics = next(
            item
            for item in self.schedule.sessions
            if item.course_key == "ethics" and item.weekday == 3
        )
        self.assertEqual(ethics.start.isoformat(timespec="minutes"), "11:10")
        self.assertEqual(ethics.end.isoformat(timespec="minutes"), "14:00")

    def test_labs_and_tutorials_never_become_due_quizzes(self):
        now = datetime(2026, 9, 8, 19, 0, tzinfo=self.schedule.timezone)
        due = self.schedule.ended_lecture_sessions(now)
        self.assertFalse(any(item.activity != "lecture" for item, _ in due))

    def test_study_week_suppresses_all_sessions(self):
        now = datetime(2026, 10, 15, 22, 0, tzinfo=self.schedule.timezone)
        self.assertEqual(self.schedule.ended_lecture_sessions(now), ())

    def test_no_time_academic_dates_are_previous_day_at_1159(self):
        item = next(
            value
            for value in self.schedule.academic_calendar_items()
            if value.source_id == "lectures-begin"
        )
        self.assertEqual(item.due_at_local.isoformat(), "2026-09-07T23:59:00-04:00")

    def test_full_term_expands_all_class_types_and_skips_no_class_week(self):
        items = self.schedule.scheduled_class_items()

        self.assertEqual(len(items), 156)
        self.assertEqual({item.kind for item in items}, {"lecture", "lab", "tutorial"})
        self.assertFalse(
            any(
                item.due_at_local.date().isoformat() in {
                    "2026-10-12",
                    "2026-10-13",
                    "2026-10-14",
                    "2026-10-15",
                    "2026-10-16",
                }
                for item in items
            )
        )
        ethics = next(
            item
            for item in items
            if item.course_name.startswith("Ethics")
            and item.due_at_local.date().isoformat() == "2026-09-10"
        )
        self.assertEqual(ethics.due_at_local.strftime("%H:%M"), "11:10")
        self.assertEqual(ethics.end_at.astimezone(self.schedule.timezone).strftime("%H:%M"), "14:00")


if __name__ == "__main__":
    unittest.main()
