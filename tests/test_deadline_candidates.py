from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant.deadline_candidates import deadline_cache_key, preprocess_deadlines
from academic_assistant.deadline_grounding import GroundingResult
from academic_assistant.deadline_grounding import ground_deadline


def test_clear_structured_deadline_is_deterministic_and_grounded():
    result = preprocess_deadlines(
        "Assignment 1 | Due September 28, 2026",
        reference_date=date(2026, 9, 1),
        max_chars=10_000,
    )
    assert result.deterministic_complete
    assert result.candidates[0].due_date == "2026-09-28"
    assert ground_deadline(
        result.text, result.candidates[0], reference_date=date(2026, 9, 1)
    ).accepted


def test_ambiguous_multiple_dates_fall_back_to_gemini():
    result = preprocess_deadlines(
        "Quiz 1: October 8 & 9, 2026",
        reference_date=date(2026, 9, 1),
        max_chars=10_000,
    )
    assert not result.deterministic_complete
    assert result.candidates == ()


def test_incidental_date_is_not_misclassified_as_deadline():
    result = preprocess_deadlines(
        "The final grade policy was revised September 28, 2026.",
        reference_date=date(2026, 9, 1),
        max_chars=10_000,
    )
    assert not result.deterministic_complete
    assert result.candidates == ()


def test_deterministic_candidate_still_passes_grounding(monkeypatch):
    import academic_assistant.deadline_candidates as candidates

    monkeypatch.setattr(
        candidates,
        "ground_deadline",
        lambda *args, **kwargs: GroundingResult(False, "fixture rejection"),
    )
    result = preprocess_deadlines(
        "Assignment 1 | Due September 28, 2026",
        reference_date=date(2026, 9, 1),
        max_chars=10_000,
    )
    assert not result.deterministic_complete
    assert result.candidates == ()


def test_long_syllabus_keeps_whole_relevant_rows_and_omits_unrelated_text():
    source = "\n".join(
        [
            "Course overview",
            *(f"Policy paragraph {index} " + "x" * 100 for index in range(200)),
            "Assessment Schedule:",
            "Assignment 1 | Due September 28, 2026",
            "Attendance is required",
        ]
    )
    result = preprocess_deadlines(source, reference_date=date(2026, 9, 1), max_chars=2_000)
    assert "Assignment 1 | Due September 28, 2026" in result.text
    assert "Policy paragraph 199" not in result.text
    assert len(result.text) < len(source) / 10


def test_context_reduction_preserves_page_locator():
    result = preprocess_deadlines(
        "[Page 7]\nAssessment Schedule:\nMidterm Exam: October 14, 2026",
        reference_date=date(2026, 9, 1),
        max_chars=10_000,
    )
    assert "[Page 7]" in result.text


def test_cache_key_ignores_source_identity_but_tracks_prompt_version():
    values = dict(
        text="Quiz 1: October 8 & 9, 2026",
        scope="syllabus_deadline_extraction",
        model="gemini-test",
        course_name="Algorithms",
        schedule_context=None,
        academic_year=2026,
    )
    first = deadline_cache_key(**values, prompt_version=5)
    same_content_other_path = deadline_cache_key(**values, prompt_version=5)
    changed_prompt = deadline_cache_key(**values, prompt_version=6)
    assert first == same_content_other_path
    assert first != changed_prompt


def test_explicit_times_survive_deterministic_extraction():
    from academic_assistant.ai_assistant import AIAssistant

    assistant = AIAssistant("test-key", client=object())
    for source_time, expected in [
        ("14:30", "14:30"),
        ("23:59", "23:59"),
        ("noon", "12:00"),
        ("midnight", "00:00"),
        ("00:00", "00:00"),
        ("9:05", "09:05"),
        ("12 a.m.", "00:00"),
        ("12 p.m.", "12:00"),
        ("2:30 PM", "14:30"),
    ]:
        for source in [
            f"Assignment 1 | Due September 28, 2026 at {source_time}",
            f"Assignment 1 | Due at {source_time} on September 28, 2026",
        ]:
            result = assistant.extract_major_deadlines(
                source,
                course_name="Algorithms",
                source_title="Syllabus",
                current_date=date(2026, 9, 1),
            )
            assert result[0]["due_time"] == expected
            assert assistant.last_deadline_extraction_method == "deterministic"


def test_invalid_and_conflicting_times_do_not_report_deterministic_success():
    for value in ["24:30", "14:99", "13 PM", "14:30 or 23:59", "noon or midnight"]:
        result = preprocess_deadlines(
            f"Assignment 1 | Due September 28, 2026 at {value}",
            reference_date=date(2026, 9, 1),
            max_chars=10_000,
        )
        assert not result.deterministic_complete
        assert not result.candidates


def test_cache_key_tracks_parser_and_grounding_versions(monkeypatch):
    import academic_assistant.deadline_candidates as candidates

    args = dict(
        text="Assignment due September 28 at noon",
        scope="test",
        model="test",
        prompt_version=1,
        course_name="Algorithms",
        schedule_context=None,
        academic_year=2026,
    )
    original = deadline_cache_key(**args)
    monkeypatch.setattr(candidates, "DETERMINISTIC_EXTRACTION_VERSION", 999)
    assert deadline_cache_key(**args) != original
    parser_changed = deadline_cache_key(**args)
    monkeypatch.setattr(candidates, "GROUNDING_VERSION", 999)
    assert deadline_cache_key(**args) != parser_changed
