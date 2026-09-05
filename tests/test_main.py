from __future__ import annotations

import sys
import unittest
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
            attendr_main.RunPlan(True, True, True, False),
        )

    def test_sync_only_disables_unrelated_steps(self):
        arguments = attendr_main.build_parser().parse_args(["--sync-only"])

        self.assertEqual(
            attendr_main.resolve_plan(arguments),
            attendr_main.RunPlan(False, True, False, False),
        )

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


if __name__ == "__main__":
    unittest.main()
