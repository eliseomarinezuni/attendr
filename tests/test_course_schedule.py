from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant import AcademicItem, CourseSchedule

UTC = timezone.utc


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

    def test_course_context_contains_only_matching_verified_sessions(self):
        context = self.schedule.context_for_course(
            "CSCI 3070U XLIST Analys. & Design of Algorithms"
        )

        self.assertIsNotNone(context)
        self.assertIn("Lecture: Wednesday 15:40-17:00", context)
        self.assertIn("Lecture: Friday 15:40-17:00", context)
        self.assertIn("Tutorial: Tuesday 11:10-12:30", context)
        self.assertIn("2026-10-13 through 2026-10-18", context)
        self.assertNotIn("Thursday 12:40", context)

    def test_course_context_returns_none_for_unmatched_course(self):
        self.assertIsNone(self.schedule.context_for_course("Unrelated Course"))

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

    def test_midterm_replaces_overlapping_course_lecture(self):
        start = datetime(2026, 11, 19, 11, 10, tzinfo=self.schedule.timezone)
        midterm = AcademicItem(
            uid="canvas:material-deadline:40515:midterm",
            source="syllabus_deadline",
            source_id="outline",
            course_id=40515,
            course_name="202609 - Eth., Law & Soc. Imp of Comp.",
            title="Midterm",
            kind="exam",
            due_at=start.astimezone(UTC),
            due_at_local=start,
            end_at=None,
            all_day=False,
            html_url=None,
            updated_at=None,
            points_possible=None,
            submission_types=(),
            description_html="Extracted from course outline.",
        )

        merged, removed = self.schedule.merge_with_class_schedule((midterm,))

        replacement = next(
            item
            for item in merged
            if item.due_at_local.date().isoformat() == "2026-11-19"
            and item.course_name.startswith("Ethics")
        )
        self.assertEqual(replacement.title, "Midterm")
        self.assertEqual(replacement.uid.split(":")[1], "class-session")
        self.assertEqual(replacement.due_at_local.strftime("%H:%M"), "11:10")
        self.assertEqual(
            replacement.end_at.astimezone(self.schedule.timezone).strftime("%H:%M"),
            "14:00",
        )
        self.assertEqual(removed, frozenset({midterm.uid}))
        self.assertEqual(len(merged), 156)


if __name__ == "__main__":
    unittest.main()
