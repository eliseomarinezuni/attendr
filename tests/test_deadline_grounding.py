from __future__ import annotations

from datetime import date

import pytest

from academic_assistant.ai_assistant import MajorDeadline
from academic_assistant.deadline_grounding import ground_deadline


def item(
    due_date: str = "2026-10-20",
    due_time: str | None = None,
    evidence: str = "Midterm Exam: October 20, 2026",
) -> MajorDeadline:
    return MajorDeadline(
        title="Midterm Exam",
        due_date=due_date,
        due_time=due_time,
        kind="exam",
        source_evidence=evidence,
    )


@pytest.mark.parametrize(
    ("source", "deadline"),
    [
        ("Midterm Exam: October 20, 2026", item()),
        (
            "MIDTERM EXAM — October\n20, 2026",
            item(evidence="Midterm Exam: October 20, 2026"),
        ),
        ("Midterm Exam: Oct. 20", item(evidence="Midterm Exam: Oct 20")),
        ("Midterm Exam: 2026-10-20", item(evidence="Midterm Exam: 2026-10-20")),
        ("Midterm Exam: 10/20/2026", item(evidence="Midterm Exam: 10/20/2026")),
        (
            "Midterm Exam: October 20 at 2:00 p.m.",
            item(due_time="14:00", evidence="Midterm Exam: October 20 at 2:00 PM"),
        ),
    ],
)
def test_grounded_supported_formats_are_accepted(source, deadline):
    assert ground_deadline(
        source, deadline, reference_date=date(2026, 9, 5)
    ).accepted


def test_fabricated_evidence_is_rejected():
    result = ground_deadline(
        "Midterm Exam: October 20, 2026",
        item(evidence="Final Project due October 20, 2026"),
        reference_date=date(2026, 9, 5),
    )
    assert not result.accepted
    assert "evidence" in result.reason


def test_fabricated_date_is_rejected_despite_real_evidence_anchor():
    result = ground_deadline(
        "Midterm Exam: October 20, 2026",
        item(due_date="2026-10-21", evidence="Midterm Exam"),
        reference_date=date(2026, 9, 5),
    )
    assert not result.accepted
    assert "date" in result.reason


def test_evidence_with_a_different_date_than_due_date_is_rejected():
    result = ground_deadline(
        "Midterm Exam: October 20, 2026",
        item(evidence="Midterm Exam: October 21, 2026"),
        reference_date=date(2026, 9, 5),
    )
    assert not result.accepted
    assert "evidence conflicts" in result.reason


def test_fabricated_time_is_rejected():
    result = ground_deadline(
        "Midterm Exam: October 20, 2026 at 2:00 PM",
        item(due_time="15:00", evidence="Midterm Exam: October 20, 2026"),
        reference_date=date(2026, 9, 5),
    )
    assert not result.accepted
    assert "time" in result.reason


def test_missing_year_is_safely_inferred_from_reference_date():
    result = ground_deadline(
        "Midterm Exam: October 20",
        item(evidence="Midterm Exam: October 20"),
        reference_date=date(2026, 9, 5),
    )
    assert result.accepted


def test_numbered_quiz_with_omitted_year_uses_exact_title_anchor():
    deadline = MajorDeadline(
        title="Quiz 1",
        due_date="2026-10-20",
        due_time=None,
        kind="quiz",
        source_evidence="Quiz 1: Oct. 20",
    )
    assert ground_deadline(
        "Quiz 1 — October 20, 2026", deadline, reference_date=date(2026, 9, 5)
    ).accepted


def test_ambiguous_numeric_date_is_rejected():
    result = ground_deadline(
        "Midterm Exam: 10/11/2026",
        item(due_date="2026-10-11", evidence="Midterm Exam: 10/11/2026"),
        reference_date=date(2026, 9, 5),
    )
    assert not result.accepted
    assert "ambiguous" in result.reason


def test_ambiguous_numeric_date_is_accepted_when_named_date_resolves_it():
    result = ground_deadline(
        "Midterm Exam: October 11, 2026 (10/11/2026)",
        item(due_date="2026-10-11", evidence="Midterm Exam: October 11, 2026"),
        reference_date=date(2026, 9, 5),
    )
    assert result.accepted


def test_conflicting_nearby_dates_are_rejected():
    result = ground_deadline(
        "Midterm Exam: October 20 or October 21, 2026",
        item(evidence="Midterm Exam"),
        reference_date=date(2026, 9, 5),
    )
    assert not result.accepted
    assert "conflicting" in result.reason
