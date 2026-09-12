from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant import Announcement, AnnouncementDatesSync, AIProviderError


class FakeAI:
    def __init__(self) -> None:
        self.calls = 0

    def extract_major_deadlines(self, *args, **kwargs):
        self.calls += 1
        return [{
            "title": "Reflection",
            "due_date": "2026-09-20",
            "due_time": None,
            "kind": "assignment",
            "source_evidence": "Reflection due September 20",
        }]


class AnnouncementDatesTests(unittest.TestCase):
    @staticmethod
    def announcement(message="Reflection due September 20"):
        posted = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
        return Announcement(
            uid="canvas:announcement:1:2",
            source_id="2",
            course_id=1,
            course_name="Ethics",
            title="Reflection date",
            message_html=f"<p>{message}</p>",
            message_text=message,
            posted_at=posted,
            posted_at_local=posted,
            html_url="https://canvas.example/announcements/2",
            author_name="Professor",
            read_state="read",
        )

    def test_no_time_preserves_stated_date_and_cache_prevents_second_ai_call(self):
        announcement = self.announcement()
        with tempfile.TemporaryDirectory() as directory:
            ai = FakeAI()
            sync = AnnouncementDatesSync(ai, index_path=Path(directory) / "dates.json")
            first = sync.sync((announcement,))
            second = sync.sync((announcement,))
        self.assertEqual(first.items[0].due_at_local.isoformat(), "2026-09-20T00:00:00-04:00")
        self.assertTrue(first.items[0].all_day)
        self.assertEqual(second.cached, 1)
        self.assertEqual(ai.calls, 1)

    def test_announcement_hallucinated_date_is_rejected(self):
        announcement = self.announcement("Reflection date will be announced later")
        with tempfile.TemporaryDirectory() as directory:
            report = AnnouncementDatesSync(
                FakeAI(), index_path=Path(directory) / "dates.json"
            ).sync((announcement,))

        self.assertEqual(report.items, ())
        self.assertTrue(report.complete)
        self.assertEqual(report.blocking_warnings, ())
        self.assertTrue(any("Rejected ungrounded AI deadline" in warning for warning in report.warnings))

    def test_irrelevant_announcement_provider_failure_is_nonblocking(self):
        ai = Mock()
        ai.extract_major_deadlines.side_effect = AIProviderError(
            "temporarily unavailable", transient=True
        )
        announcement = self.announcement("Welcome to the course. Read the overview.")
        with tempfile.TemporaryDirectory() as directory:
            report = AnnouncementDatesSync(
                ai, index_path=Path(directory) / "dates.json"
            ).sync((announcement,))

        self.assertTrue(report.complete)
        self.assertTrue(report.warnings)
        self.assertEqual(report.blocking_warnings, ())

    def test_unavailable_potential_deadline_is_blocking_without_cache(self):
        ai = Mock()
        ai.extract_major_deadlines.side_effect = AIProviderError(
            "temporarily unavailable", transient=True
        )
        announcement = self.announcement("Assignment 1 is due September 25.")
        with tempfile.TemporaryDirectory() as directory:
            report = AnnouncementDatesSync(
                ai, index_path=Path(directory) / "dates.json"
            ).sync((announcement,))

        self.assertFalse(report.complete)
        self.assertTrue(report.blocking_warnings)

    def test_changed_dated_announcement_blocks_when_provider_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dates.json"
            AnnouncementDatesSync(FakeAI(), index_path=path).sync(
                (self.announcement(),)
            )
            failing_ai = Mock()
            failing_ai.extract_major_deadlines.side_effect = AIProviderError(
                "temporarily unavailable", transient=True
            )
            changed = self.announcement(
                "Reflection due September 20. Quiz due September 27."
            )
            report = AnnouncementDatesSync(
                failing_ai, index_path=path
            ).sync((changed,))

        self.assertFalse(report.complete)
        self.assertTrue(report.blocking_warnings)

    def test_identical_announcement_content_reuses_content_cache(self):
        first = self.announcement()
        second = replace(
            self.announcement(), uid="canvas:announcement:1:3", source_id="3"
        )
        with tempfile.TemporaryDirectory() as directory:
            ai = FakeAI()
            report = AnnouncementDatesSync(
                ai, index_path=Path(directory) / "dates.json"
            ).sync((first, second))

        self.assertEqual(ai.calls, 1)
        self.assertEqual(report.cached, 1)


if __name__ == "__main__":
    unittest.main()
