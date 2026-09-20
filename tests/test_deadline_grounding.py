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
    assert ground_deadline(source, deadline, reference_date=date(2026, 9, 5)).accepted


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


def test_adjacent_table_row_dates_do_not_conflict():
    source = (
        "Assignment 1 | September 25, 2026\n"
        "Quiz 1 | October 2, 2026\n"
        "Assignment 2 | October 9, 2026"
    )
    deadline = MajorDeadline(
        title="Quiz 1",
        due_date="2026-10-02",
        due_time=None,
        kind="quiz",
        source_evidence="Quiz 1 | October 2, 2026",
    )

    result = ground_deadline(source, deadline, reference_date=date(2026, 9, 5))

    assert result.accepted
    assert result.locator == "line 2"


def test_exam_row_ignores_unrelated_dates_immediately_before_and_after():
    source = "Reading | Sep 24\nMidterm Exam | Sep 25\nProject | Sep 26"
    deadline = item("2026-09-25", evidence="Midterm Exam | Sep 25")

    assert ground_deadline(source, deadline, reference_date=date(2026, 9, 5)).accepted


def test_multiple_dates_in_same_logical_row_are_rejected():
    source = "Quiz 1 | October 2 or October 9, 2026"
    deadline = MajorDeadline(
        title="Quiz 1",
        due_date="2026-10-02",
        due_time=None,
        kind="quiz",
        source_evidence="Quiz 1",
    )

    result = ground_deadline(source, deadline, reference_date=date(2026, 9, 5))

    assert not result.accepted
    assert "conflicting" in result.reason


@pytest.mark.parametrize(
    ("source_date", "due_date"),
    [
        ("September 25", "2026-09-25"),
        ("Sep 25", "2026-09-25"),
        ("2026-09-25", "2026-09-25"),
        ("9/25/2026", "2026-09-25"),
        ("25/9/2026", "2026-09-25"),
        ("9/25", "2026-09-25"),
    ],
)
def test_assignment_table_date_formats(source_date, due_date):
    deadline = MajorDeadline(
        title="Assignment 1",
        due_date=due_date,
        due_time=None,
        kind="assignment",
        source_evidence=f"Assignment 1 | {source_date}",
    )
    assert ground_deadline(
        f"Assignment 1 | {source_date}",
        deadline,
        reference_date=date(2026, 9, 5),
    ).accepted


def test_evidence_copied_from_different_table_row_is_rejected():
    source = "Assignment 1 | September 25\nQuiz 1 | October 2"
    deadline = MajorDeadline(
        title="Quiz 1",
        due_date="2026-09-25",
        due_time=None,
        kind="quiz",
        source_evidence="Quiz 1",
    )

    result = ground_deadline(source, deadline, reference_date=date(2026, 9, 5))

    assert not result.accepted
    assert "date" in result.reason


@pytest.mark.parametrize(
    "value,expected",
    [
        ("14:30", "14:30"),
        ("23:59", "23:59"),
        ("noon", "12:00"),
        ("midnight", "00:00"),
        ("00:00", "00:00"),
        ("12 p.m.", "12:00"),
    ],
)
def test_explicit_time_must_be_preserved(value, expected):
    source = f"Midterm Exam: October 20, 2026 at {value}"
    assert ground_deadline(source, item(due_time=expected, evidence=source)).accepted
    assert not ground_deadline(source, item(evidence=source)).accepted
    # Omitting the time from the evidence must not bypass the local source check.
    assert not ground_deadline(source, item()).accepted
    assert not ground_deadline(source, item(due_time="01:15", evidence=source)).accepted


@pytest.mark.parametrize("value", ["24:30", "14:99", "13 PM", "noon or midnight"])
def test_invalid_or_conflicting_time_is_rejected(value):
    source = f"Midterm Exam: October 20, 2026 at {value}"
    assert not ground_deadline(source, item(evidence=source)).accepted
    assert not ground_deadline(source, item(due_time="12:00", evidence=source)).accepted
