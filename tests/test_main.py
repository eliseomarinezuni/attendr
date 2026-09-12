from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import main as attendr_main
from academic_assistant.logging_config import RedactedJSONFormatter


class MainRunnerTests(unittest.TestCase):
    def test_recorded_exit_codes_distinguish_degraded_from_failed(self):
        cases = (
            ([attendr_main.StepResult("A", "ok", "done")], 0, "ok"),
            (
                [
                    attendr_main.StepResult("A", "ok", "done"),
                    attendr_main.StepResult("B", "degraded", "safe warning"),
                ],
                0,
                "degraded",
            ),
            ([attendr_main.StepResult("A", "failed", "blocked")], 1, "failed"),
        )
        for results, expected_exit, expected_status in cases:
            with self.subTest(expected_status=expected_status), tempfile.TemporaryDirectory() as directory:
                store = attendr_main.StateStore(Path(directory) / "state.db")
                arguments = attendr_main.build_parser().parse_args([])
                with patch.object(attendr_main, "configure_logging"), patch.object(
                    attendr_main, "run_pipeline", return_value=results
                ):
                    status = attendr_main.run_recorded(arguments, store)
                with store.connect() as db:
                    recorded = db.execute("SELECT status FROM runs").fetchone()[0]
                self.assertEqual(status, expected_exit)
                self.assertEqual(recorded, expected_status)

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

    def test_unexpected_step_failure_logs_traceback_and_returns_safe_result(self):
        secret = "fake-production-token-value"

        def fail() -> str:
            raise RuntimeError(f"provider rejected {secret}")

        with patch.dict("os.environ", {"ATTENDR_TEST_SECRET": secret}), self.assertLogs(
            "attendr.pipeline", level="ERROR"
        ) as captured:
            result = attendr_main.run_step("Example", fail)

        self.assertEqual(
            result,
            attendr_main.StepResult("Example", "failed", "Unexpected RuntimeError"),
        )
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertIsNotNone(record.exc_info)
        rendered = RedactedJSONFormatter().format(record)
        self.assertIn('"exception": "RuntimeError"', rendered)
        self.assertIn('"function": "fail"', rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("provider rejected", result.detail)

    def test_expected_step_failure_does_not_log_a_traceback(self):
        with patch.object(attendr_main.PIPELINE_LOGGER, "exception") as log_exception:
            result = attendr_main.run_step(
                "Example", lambda: (_ for _ in ()).throw(ValueError("recoverable"))
            )

        self.assertEqual(result, attendr_main.StepResult("Example", "failed", "recoverable"))
        log_exception.assert_not_called()

    def test_unexpected_step_does_not_break_later_step_reporting(self):
        with patch.object(attendr_main.PIPELINE_LOGGER, "exception") as log_exception:
            results = [
                attendr_main.run_step(
                    "Broken", lambda: (_ for _ in ()).throw(RuntimeError("internal"))
                ),
                attendr_main.run_step("Healthy", lambda: "completed"),
            ]

        self.assertEqual([result.status for result in results], ["failed", "ok"])
        self.assertEqual(results[1].detail, "completed")
        log_exception.assert_called_once()

    def test_exception_escaping_pipeline_is_logged_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = attendr_main.StateStore(Path(directory) / "state.db")
            arguments = attendr_main.build_parser().parse_args([])
            with patch.object(attendr_main, "configure_logging"), patch.object(
                attendr_main, "run_pipeline", side_effect=RuntimeError("escaped")
            ), self.assertLogs("attendr", level="ERROR") as captured:
                status = attendr_main.run_recorded(arguments, store)

            self.assertEqual(status, 1)
            self.assertEqual(len(captured.records), 1)
            self.assertIsNotNone(captured.records[0].exc_info)
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "failed")

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
