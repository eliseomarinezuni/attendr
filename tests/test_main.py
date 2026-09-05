from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import main as attendr_main


class MainRunnerTests(unittest.TestCase):
    def test_default_plan_runs_daily_pipeline_without_quiz(self):
        arguments = attendr_main.build_parser().parse_args([])

        self.assertEqual(
            attendr_main.resolve_plan(arguments),
            attendr_main.RunPlan(True, True, True, True, False, True, True),
        )

    def test_sync_only_disables_unrelated_steps(self):
        arguments = attendr_main.build_parser().parse_args(["--sync-only"])

        self.assertEqual(
            attendr_main.resolve_plan(arguments),
            attendr_main.RunPlan(False, True, True, False, False, False, True),
        )

    def test_no_materials_disables_syllabus_sync(self):
        arguments = attendr_main.build_parser().parse_args(["--no-materials"])

        self.assertFalse(attendr_main.resolve_plan(arguments).materials)

    def test_quiz_topic_can_come_from_environment(self):
        arguments = attendr_main.build_parser().parse_args(["--quiz-only"])

        with patch.dict("os.environ", {"DAILY_QUIZ_TOPIC": "Graph traversal"}):
            title, source = attendr_main.resolve_quiz_source(arguments)

        self.assertEqual(title, "Graph traversal")
        self.assertEqual(source, "Graph traversal")

    def test_step_failure_is_returned_instead_of_raised(self):
        def fail() -> str:
            raise ValueError("temporary failure")

        result = attendr_main.run_step("Example", fail)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.detail, "temporary failure")

    def test_live_canvas_deadline_wins_over_matching_syllabus_deadline(self):
        due = datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)
        common = {
            "course_id": 1,
            "course_name": "Algorithms",
            "title": "Midterm Exam",
            "kind": "exam",
            "due_at": due,
            "due_at_local": due,
            "end_at": None,
            "all_day": False,
            "html_url": None,
            "updated_at": None,
            "points_possible": None,
            "submission_types": (),
            "description_html": None,
        }
        canvas_item = attendr_main.AcademicItem(
            uid="canvas:assignment:1:1",
            source="assignment",
            source_id="1",
            **common,
        )
        material_item = attendr_main.AcademicItem(
            uid="canvas:material-deadline:1:x",
            source="syllabus_deadline",
            source_id="syllabus",
            **common,
        )

        filtered = attendr_main.filter_material_duplicates(
            (canvas_item,), (material_item,)
        )

        self.assertEqual(filtered, ())


if __name__ == "__main__":
    unittest.main()
