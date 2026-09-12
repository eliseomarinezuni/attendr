"""Conservative deterministic deadline parsing and structural context selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from hashlib import sha256

from .ai_assistant import MajorDeadline
from .deadline_grounding import ground_deadline

DEADLINE_CONTEXT_VERSION = 1
DETERMINISTIC_EXTRACTION_VERSION = 1

_KEYWORD = re.compile(
    r"\b(?:due|deadline|assignment|quiz|exam|midterm|final|project|presentation|lab|test)\b",
    re.IGNORECASE,
)
_MONTH = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_DATE = re.compile(
    r"(?P<iso>\b\d{4}-\d{1,2}-\d{1,2}\b)|"
    r"(?P<named>\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b)|"
    r"(?P<slash>\b\d{1,2}/\d{1,2}/\d{4}\b)",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<suffix>a\.?m\.?|p\.?m\.?)\b", re.I
)
_AMBIGUOUS_DATE_JOIN = re.compile(r"(?:&|\band\b|\bor\b)\s*\d{1,2}\b", re.I)
_TITLE_SPLIT = re.compile(r"\s*(?:\||:|—|–|-|\bdue\b|\bdeadline\b)\s*", re.I)
_KIND = (
    ("exam", re.compile(r"\b(?:exam|midterm|final|test)\b", re.I)),
    ("quiz", re.compile(r"\bquiz\b", re.I)),
    ("assignment", re.compile(r"\b(?:assignment|project|presentation|lab)\b", re.I)),
)


@dataclass(frozen=True, slots=True)
class DeadlinePreprocessing:
    text: str
    candidates: tuple[MajorDeadline, ...]
    deterministic_complete: bool
    original_chars: int


def normalize_source_text(text: str) -> str:
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def deadline_cache_key(
    text: str,
    *,
    scope: str,
    model: str,
    prompt_version: int,
    course_name: str,
    schedule_context: str | None,
    academic_year: int,
) -> str:
    """Address extraction by relevant content/configuration, never source identity."""
    content_hash = sha256(normalize_source_text(text).encode()).hexdigest()
    return sha256(
        repr(
            (
                scope,
                content_hash,
                model,
                prompt_version,
                course_name.casefold(),
                schedule_context,
                academic_year,
            )
        ).encode()
    ).hexdigest()


def preprocess_deadlines(
    text: str, *, reference_date: date, max_chars: int
) -> DeadlinePreprocessing:
    normalized = normalize_source_text(text)
    lines = normalized.splitlines()
    relevant = [
        index for index, line in enumerate(lines) if _KEYWORD.search(line) or _DATE.search(line)
    ]
    selected: set[int] = set()
    for index in relevant:
        selected.add(index)
        if index and _looks_like_heading(lines[index - 1]):
            selected.add(index - 1)
        for previous in range(index - 1, -1, -1):
            if _looks_like_locator(lines[previous]):
                selected.add(previous)
                break
    units = [lines[index] for index in sorted(selected)]
    focused = _join_whole_units(units or lines, max_chars)

    candidates: list[MajorDeadline] = []
    unresolved = False
    for index in relevant:
        line = lines[index]
        if not _KEYWORD.search(line):
            continue
        parsed = _parse_line(line, reference_date)
        if parsed is None:
            unresolved = True
            continue
        if ground_deadline(normalized, parsed, reference_date=reference_date).accepted:
            candidates.append(parsed)
        else:
            unresolved = True
    unique = {candidate.model_dump_json(): candidate for candidate in candidates}
    meaningful = [line for line in lines if not _looks_like_heading(line)]
    deterministic_complete = (
        bool(candidates)
        and not unresolved
        and all(_KEYWORD.search(line) or not _DATE.search(line) for line in meaningful)
    )
    return DeadlinePreprocessing(
        focused,
        tuple(unique.values()),
        deterministic_complete,
        len(normalized),
    )


def _parse_line(line: str, reference_date: date) -> MajorDeadline | None:
    if _AMBIGUOUS_DATE_JOIN.search(line):
        return None
    matches = list(_DATE.finditer(line))
    if len(matches) != 1:
        return None
    actual = _parse_date(matches[0].group(), reference_date)
    if actual is None:
        return None
    raw_prefix = line[: matches[0].start()]
    if not _TITLE_SPLIT.search(raw_prefix):
        return None
    prefix = raw_prefix.strip(" |:—–-")
    title = _TITLE_SPLIT.split(prefix)[0].strip()
    if not title or len(title) > 100 or not _KEYWORD.search(title):
        return None
    kind = next((name for name, pattern in _KIND if pattern.search(title)), "other")
    time_match = _TIME.search(line[matches[0].end() :])
    due_time = None
    if time_match:
        hour = int(time_match.group("hour"))
        minute = int(time_match.group("minute") or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        if time_match.group("suffix").casefold().startswith("p") and hour != 12:
            hour += 12
        elif time_match.group("suffix").casefold().startswith("a") and hour == 12:
            hour = 0
        due_time = f"{hour:02d}:{minute:02d}"
    return MajorDeadline(
        title=title,
        due_date=actual.isoformat(),
        due_time=due_time,
        kind=kind,
        source_evidence=line,
    )


def _parse_date(value: str, reference_date: date) -> date | None:
    clean = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", value.casefold()).replace(",", "")
    try:
        if "-" in clean:
            return date.fromisoformat(clean)
        if "/" in clean:
            month, day, year = map(int, clean.split("/"))
            return date(year, month, day)
        parts = clean.split()
        month = _MONTH[parts[0]]
        day = int(parts[1])
        year = int(parts[2]) if len(parts) == 3 else reference_date.year
        candidate = date(year, month, day)
        if len(parts) == 2 and candidate < reference_date.replace(day=1):
            candidate = date(year + 1, month, day)
        return candidate
    except (KeyError, ValueError):
        return None


def _looks_like_heading(line: str) -> bool:
    return len(line) <= 80 and not _DATE.search(line) and (line.endswith(":") or line.istitle())


def _looks_like_locator(line: str) -> bool:
    return bool(re.fullmatch(r"\[(?:pages?|slides?|tables?)\s+[^]]+\]", line, re.I))


def _join_whole_units(units: list[str], max_chars: int) -> str:
    selected: list[str] = []
    size = 0
    for unit in units:
        added = len(unit) + (1 if selected else 0)
        if size + added > max_chars:
            if not selected:
                return unit
            break
        selected.append(unit)
        size += added
    return "\n".join(selected)
