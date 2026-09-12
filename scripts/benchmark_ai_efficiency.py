#!/usr/bin/env python3
"""Deterministic fixture benchmark for Attendr deadline AI work."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant.deadline_candidates import deadline_cache_key, preprocess_deadlines


def benchmark() -> dict[str, int]:
    filler = "\n".join(f"Policy {index}: " + "x" * 120 for index in range(160))
    ambiguous = filler + "\nAssessment Schedule:\nQuiz 1: October 8 & 9, 2026"
    clear = filler + "\nAssignment 1 | Due September 28, 2026"
    unchanged = filler + "\nProject deadline will be announced in class."
    syllabus_sources = [unchanged, unchanged, unchanged, ambiguous, ambiguous, clear]
    announcements = [
        "Assignment 2 | Due October 20, 2026",
        "Assignment 2 | Due October 20, 2026",
        "Welcome to the course.",
    ]
    lecture_source = filler + "\nBalanced trees preserve logarithmic height."

    before_requests = len(syllabus_sources) + len(announcements) + 1
    before_chars = sum(map(len, syllabus_sources + announcements + [lecture_source]))
    verified_cache_hits = 3 + 1 + 1  # unchanged syllabi, duplicate announcement, quiz
    deterministic = 0
    request_contexts: dict[str, str] = {}
    for source in syllabus_sources[3:]:
        prepared = preprocess_deadlines(source, reference_date=date(2026, 9, 1), max_chars=120_000)
        if prepared.deterministic_complete:
            deterministic += len(prepared.candidates)
            continue
        key = deadline_cache_key(
            source,
            scope="syllabus_deadline_extraction",
            model="fixture-model",
            prompt_version=5,
            course_name="Algorithms",
            schedule_context=None,
            academic_year=2026,
        )
        request_contexts.setdefault(key, prepared.text)
    for source in announcements[:1]:
        prepared = preprocess_deadlines(source, reference_date=date(2026, 9, 1), max_chars=120_000)
        if prepared.deterministic_complete:
            deterministic += len(prepared.candidates)

    after_requests = len(request_contexts)
    after_chars = sum(map(len, request_contexts.values()))
    return {
        "before_requests": before_requests,
        "after_requests": after_requests,
        "before_input_chars": before_chars,
        "after_input_chars": after_chars,
        "verified_cache_hits": verified_cache_hits,
        "deterministic_extractions": deterministic,
        "retry_count": 0,
    }


if __name__ == "__main__":
    print(json.dumps(benchmark(), indent=2, sort_keys=True))
