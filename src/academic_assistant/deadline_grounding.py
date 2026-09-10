"""Deterministic source grounding for AI-extracted academic deadlines."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from .ai_assistant import MajorDeadline

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2,
    "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_TEMPORAL_WORDS = set(_MONTHS) | {"am", "pm", "a", "p", "at", "on"}
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class GroundingResult:
    accepted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _Word:
    value: str
    start: int
    end: int


def ground_deadline(
    source_text: str,
    deadline: MajorDeadline,
    *,
    reference_date: date | None = None,
) -> GroundingResult:
    """Prove that evidence, date, and optional time occur in one tight source context."""
    source = unicodedata.normalize("NFKC", source_text).casefold()
    evidence = unicodedata.normalize("NFKC", deadline.source_evidence).casefold()
    source_words = _words(source)
    evidence_values = [word.value for word in _words(evidence)]
    if not source_words or not evidence_values:
        return GroundingResult(False, "source evidence is absent")

    expected = date.fromisoformat(deadline.due_date)
    evidence_words = _words(evidence)
    evidence_dates, evidence_ambiguous = _dates_in_context(
        evidence, evidence_words, expected, reference_date
    )
    if evidence_ambiguous and expected not in evidence_dates:
        return GroundingResult(False, "source evidence contains an ambiguous date")
    if evidence_dates and (
        expected not in evidence_dates
        or any(candidate != expected for candidate in evidence_dates)
    ):
        return GroundingResult(False, "source evidence conflicts with the extracted date")
    expected_minutes = (
        _time_minutes(deadline.due_time) if deadline.due_time is not None else None
    )
    if expected_minutes is not None:
        evidence_times = _times_in_context(evidence, evidence_words)
        if evidence_times and (
            expected_minutes not in evidence_times
            or any(candidate != expected_minutes for candidate in evidence_times)
        ):
            return GroundingResult(False, "source evidence conflicts with the extracted time")

    match = _find_sequence(source_words, evidence_values)
    if match is None:
        anchor = [
            value for value in evidence_values
            if value not in _TEMPORAL_WORDS and not _is_number(value)
        ]
        # A semantic evidence template is allowed only when its non-temporal wording
        # is a substantial, exact contiguous phrase. Date/time are checked below.
        match = (
            _find_sequence(source_words, anchor)
            if len(anchor) >= 2 and sum(map(len, anchor)) >= 8
            else None
        )
        if match is None:
            title_values = [word.value for word in _words(deadline.title.casefold())]
            title_is_quoted = _find_sequence(evidence_words, title_values) is not None
            if (
                title_is_quoted
                and len(title_values) >= 2
                and sum(map(len, title_values)) >= 5
            ):
                match = _find_sequence(source_words, title_values)
        if match is None:
            return GroundingResult(False, "source evidence is not present")

    start, length = match
    context = source_words[max(0, start - 12): min(len(source_words), start + length + 12)]
    dates, ambiguous = _dates_in_context(source, context, expected, reference_date)
    if ambiguous and expected not in dates:
        return GroundingResult(False, "the nearby numeric date is ambiguous")
    if expected not in dates:
        return GroundingResult(False, "the extracted date is not supported nearby")
    if any(candidate != expected for candidate in dates):
        return GroundingResult(False, "nearby source text contains conflicting dates")

    if expected_minutes is not None:
        times = _times_in_context(source, context)
        if expected_minutes not in times:
            return GroundingResult(False, "the extracted time is not supported nearby")
        if any(candidate != expected_minutes for candidate in times):
            return GroundingResult(False, "nearby source text contains conflicting times")
    return GroundingResult(True, "grounded")


def _words(value: str) -> list[_Word]:
    return [_Word(match.group(), match.start(), match.end()) for match in _TOKEN.finditer(value)]


def _find_sequence(words: list[_Word], values: list[str]) -> tuple[int, int] | None:
    source_values = [word.value for word in words]
    width = len(values)
    for index in range(len(source_values) - width + 1):
        if source_values[index:index + width] == values:
            return index, width
    return None


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:st|nd|rd|th)?", value))


def _number(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", value)
    return int(match.group(1)) if match else None


def _separator(source: str, left: _Word, right: _Word) -> str:
    return source[left.end:right.start].strip()


def _dates_in_context(
    source: str,
    words: list[_Word],
    expected: date,
    reference_date: date | None,
) -> tuple[set[date], bool]:
    found: set[date] = set()
    ambiguous = False
    for index, word in enumerate(words):
        month = _MONTHS.get(word.value)
        if month is not None and index + 1 < len(words):
            day = _number(words[index + 1].value)
            if day is not None:
                year = None
                if index + 2 < len(words):
                    possible_year = _number(words[index + 2].value)
                    if possible_year is not None and 1000 <= possible_year <= 9999:
                        year = possible_year
                candidate = _make_date(year, month, day, expected, reference_date)
                if candidate is not None:
                    found.add(candidate)

        if index + 2 >= len(words):
            continue
        first = _number(word.value)
        second = _number(words[index + 1].value)
        third = _number(words[index + 2].value)
        if first is None or second is None or third is None:
            continue
        first_sep = _separator(source, word, words[index + 1])
        second_sep = _separator(source, words[index + 1], words[index + 2])
        if first_sep == second_sep == "-" and 1000 <= first <= 9999:
            candidate = _safe_date(first, second, third)
            if candidate is not None:
                found.add(candidate)
        elif first_sep == second_sep == "/" and 1000 <= third <= 9999:
            if first <= 12 and second <= 12:
                ambiguous = True
            elif first <= 12:
                candidate = _safe_date(third, first, second)
                if candidate is not None:
                    found.add(candidate)
            elif second <= 12:
                candidate = _safe_date(third, second, first)
                if candidate is not None:
                    found.add(candidate)
    return found, ambiguous


def _make_date(
    year: int | None,
    month: int,
    day: int,
    expected: date,
    reference_date: date | None,
) -> date | None:
    if year is not None:
        return _safe_date(year, month, day)
    if reference_date is None or (month, day) != (expected.month, expected.day):
        return None
    candidates = []
    for candidate_year in range(reference_date.year - 1, reference_date.year + 2):
        candidate = _safe_date(candidate_year, month, day)
        if candidate and reference_date - timedelta(days=30) <= candidate <= reference_date + timedelta(days=370):
            candidates.append(candidate)
    return expected if candidates == [expected] else None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _time_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _times_in_context(source: str, words: list[_Word]) -> set[int]:
    found: set[int] = set()
    for index, word in enumerate(words):
        hour = _number(word.value)
        if hour is None or hour > 23:
            continue
        minute = 0
        suffix_index = index + 1
        if index + 1 < len(words) and _separator(source, word, words[index + 1]) == ":":
            parsed_minute = _number(words[index + 1].value)
            if parsed_minute is None or parsed_minute > 59:
                continue
            minute = parsed_minute
            suffix_index = index + 2
        suffix = words[suffix_index].value if suffix_index < len(words) else ""
        if suffix in {"a", "p"} and suffix_index + 1 < len(words) and words[suffix_index + 1].value == "m":
            suffix += "m"
        if suffix in {"am", "pm"}:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if suffix == "pm" else 0)
            found.add(hour * 60 + minute)
        elif minute or ":" in source[word.end: words[index + 1].start if index + 1 < len(words) else word.end]:
            found.add(hour * 60 + minute)
    return found
