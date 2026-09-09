from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from docx import Document

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (
    CourseSchedule,
    CourseMaterialsSync,
    MaterialDownloadReport,
    SyllabusMaterial,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class FakeCanvas:
    def __init__(self, materials, warnings=(), incomplete_course_ids=()):
        self.materials = materials
        self.warnings = warnings
        self.incomplete_course_ids = incomplete_course_ids

    def download_syllabus_materials(self, directory, *, max_file_bytes):
        return MaterialDownloadReport(
            tuple(self.materials), tuple(self.warnings), tuple(self.incomplete_course_ids)
        )


class FakeAI:
    def __init__(self, deadlines):
        self.deadlines = deadlines
        self.calls = []

    def extract_major_deadlines(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return self.deadlines


def deadline(date_value="2026-10-20", time_value="13:30"):
    return {
        "title": "Midterm Exam",
        "due_date": date_value,
        "due_time": time_value,
        "kind": "exam",
        "source_evidence": "Midterm Exam: October 20 at 1:30 PM",
    }


class CourseMaterialsSyncTests(unittest.TestCase):
    def make_material(self, path, *, uid="canvas:syllabus-page:1", digest=None):
        content = path.read_bytes()
        return SyllabusMaterial(
            uid=uid,
            source_id="syllabus-page",
            course_id=1,
            course_name="Algorithms",
            title="Canvas syllabus page",
            content_type="text/html",
            local_path=path,
            content_sha256=digest or sha256(content).hexdigest(),
            updated_at=None,
            html_url="https://canvas.example/courses/1/assignments/syllabus",
        )

    def test_changed_material_is_analyzed_then_unchanged_hash_uses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html_path = root / "syllabus.html"
            html_path.write_text("<p>Midterm Exam: October 20, 2026 at 1:30 PM</p>")
            material = self.make_material(html_path)
            ai = FakeAI([deadline()])
            first = CourseMaterialsSync(
                FakeCanvas([material]),
                ai,
                materials_directory=root / "materials",
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync()

            cached_ai = FakeAI([])
            second = CourseMaterialsSync(
                FakeCanvas([material]),
                cached_ai,
                materials_directory=root / "materials",
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync()

            self.assertEqual(first.materials_analyzed, 1)
            self.assertEqual(len(first.items), 1)
            self.assertEqual(first.items[0].kind, "exam")
            self.assertEqual(
                first.items[0].due_at_local.isoformat(), "2026-10-20T13:30:00-04:00"
            )
            self.assertEqual(second.cached_materials_reused, 1)
            self.assertEqual(cached_ai.calls, [])
            self.assertEqual(second.items[0].uid, first.items[0].uid)

    def test_untimed_midterm_uses_matching_lecture_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html_path = root / "syllabus.html"
            html_path.write_text("<p>Midterm October 27, 2026</p>")
            material = replace(
                self.make_material(html_path),
                course_name="202699 - Web Dev - EXMP-3030",
            )
            report = CourseMaterialsSync(
                FakeCanvas([material]),
                FakeAI([deadline("2026-10-27", None)]),
                index_path=root / "index.json",
                course_schedule=CourseSchedule.load(
                    PROJECT_ROOT / "data/course_schedule.json"
                ),
                now_provider=lambda: NOW,
            ).sync()

            self.assertEqual(
                report.items[0].due_at_local.isoformat(),
                "2026-10-27T12:40:00-04:00",
            )
            self.assertEqual(
                report.items[0].end_at.astimezone(report.items[0].due_at_local.tzinfo).strftime("%H:%M"),
                "14:00",
            )

    def test_conflicting_dates_for_same_exam_are_not_sent_to_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "one.html"
            second_path = root / "two.html"
            first_path.write_text("<p>Midterm October 20</p>")
            second_path.write_text("<p>Midterm October 21</p>")
            first = self.make_material(first_path, uid="canvas:syllabus-page:1")
            second = self.make_material(second_path, uid="canvas:syllabus-file:1:2")

            class ConflictingAI:
                def extract_major_deadlines(self, text, **kwargs):
                    return [deadline("2026-10-21" if "21" in text else "2026-10-20")]

            report = CourseMaterialsSync(
                FakeCanvas([first, second]),
                ConflictingAI(),
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync()

            self.assertEqual(report.items, ())
            self.assertTrue(
                any("Conflicting syllabus dates" in item for item in report.warnings)
            )

    def test_word_syllabus_extracts_paragraphs_and_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "outline.docx"
            document = Document()
            document.add_paragraph("Course schedule")
            table = document.add_table(rows=1, cols=2)
            table.cell(0, 0).text = "Assignment 1"
            table.cell(0, 1).text = "October 16, 2026"
            document.save(path)
            material = SyllabusMaterial(
                uid="canvas:syllabus-file:1:9",
                source_id="9",
                course_id=1,
                course_name="Algorithms",
                title="outline.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                local_path=path,
                content_sha256=sha256(path.read_bytes()).hexdigest(),
                updated_at=None,
                html_url=None,
            )
            ai = FakeAI([deadline("2026-10-16", None)])
            report = CourseMaterialsSync(
                FakeCanvas([material]),
                ai,
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync()
        self.assertIn("Assignment 1 | October 16, 2026", ai.calls[0][0])
        self.assertEqual(report.items[0].due_at_local.isoformat(), "2026-10-16T00:00:00-04:00")

    def test_optional_route_failure_is_complete_when_course_has_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "syllabus.html"
            path.write_text("<p>Course outline</p>")
            material = self.make_material(path)
            report = CourseMaterialsSync(
                FakeCanvas(
                    [material],
                    warnings=("Could not retrieve syllabus files",),
                    incomplete_course_ids=(1,),
                ),
                FakeAI([]),
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync(active_course_ids={1})

        self.assertTrue(report.complete)

    def test_hidden_files_tab_retains_missing_cached_source_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.html"
            old_path.write_text("<p>Midterm Exam: October 20, 2026 at 1:30 PM</p>")
            old = self.make_material(
                old_path, uid="canvas:syllabus-file:1:old"
            )
            first = CourseMaterialsSync(
                FakeCanvas([old]),
                FakeAI([deadline()]),
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync(active_course_ids={1})
            self.assertTrue(first.complete)

            current_path = root / "current.html"
            current_path.write_text("<p>Current course outline</p>")
            current = self.make_material(
                current_path, uid="canvas:syllabus-page:1"
            )
            second = CourseMaterialsSync(
                FakeCanvas(
                    [current],
                    warnings=("Could not retrieve syllabus files",),
                    incomplete_course_ids=(1,),
                ),
                FakeAI([]),
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync(active_course_ids={1})

        self.assertTrue(second.complete)
        self.assertEqual(len(second.items), 1)
        self.assertTrue(any("cached deadlines retained" in item for item in second.warnings))

    def test_complete_scan_retires_removed_cached_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.html"
            old_path.write_text("<p>Midterm Exam: October 20, 2026 at 1:30 PM</p>")
            old = self.make_material(
                old_path, uid="canvas:syllabus-file:1:old"
            )
            CourseMaterialsSync(
                FakeCanvas([old]),
                FakeAI([deadline()]),
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync(active_course_ids={1})

            current_path = root / "current.html"
            current_path.write_text("<p>Current course outline</p>")
            current = self.make_material(
                current_path, uid="canvas:syllabus-page:1"
            )
            report = CourseMaterialsSync(
                FakeCanvas([current]),
                FakeAI([]),
                index_path=root / "index.json",
                now_provider=lambda: NOW,
            ).sync(active_course_ids={1})

            index_text = (root / "index.json").read_text()

        self.assertTrue(report.complete)
        self.assertEqual(report.items, ())
        self.assertNotIn("canvas:syllabus-file:1:old", index_text)


if __name__ == "__main__":
    unittest.main()
