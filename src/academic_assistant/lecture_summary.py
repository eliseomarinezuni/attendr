"""Render validated lecture summaries for durable Discord delivery."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from .ai_assistant import AIInputError, GroundedSummaryPoint, LectureSummary
from .lecture_content import LectureContentBundle

SUMMARY_BLUE = 0x3498DB


def lecture_summary_discord_payload(
    bundle: LectureContentBundle,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Build complete, naturally split embeds without trusting model metadata."""
    try:
        validated = LectureSummary.model_validate(summary)
    except ValidationError:
        raise AIInputError("Lecture summary data is invalid and cannot be sent.") from None

    embeds: list[dict[str, Any]] = []
    sections = (
        ("TL;DR", validated.tldr),
        ("Key concepts", validated.key_concepts),
        ("Teach it to me", validated.teaching_sections),
        ("Examples", validated.examples),
        ("Algorithms and code", validated.algorithms_or_code),
        ("Formulas", validated.formulas),
        ("Common mistakes", validated.common_mistakes),
        ("Connections", validated.connections),
        ("Learning objectives", validated.learning_objectives),
    )
    for heading, points in sections:
        for part, description in enumerate(_point_groups(points), start=1):
            title = heading if part == 1 else f"{heading} (continued)"
            embeds.append(_embed(title, description))

    source_lines = [
        (
            f"**[{source.reference_id}] {_discord_text(source.title)}** — "
            f"{_discord_text(source.location or source.extraction_method)} "
            f"(`{_discord_text(source.canvas_uid, inline=True)}`)"
        )
        for source in bundle.sources
    ]
    for part, description in enumerate(_line_groups(source_lines), start=1):
        title = "Sources" if part == 1 else "Sources (continued)"
        embeds.append(_embed(title, description))

    return {
        "username": "Attendr",
        "content": (f"📚 **{_discord_text(bundle.title)}**\n{_discord_text(validated.topic)}"),
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }


def _point_groups(points: Iterable[GroundedSummaryPoint]) -> tuple[str, ...]:
    lines = [_render_point(point) for point in points]
    return _line_groups(lines)


def _render_point(point: GroundedSummaryPoint) -> str:
    text = _discord_text(point.text)
    citation = f"**[{', '.join(point.source_ids)}]**"
    return f"{text}\n{citation}" if "```" in text else f"• {text} {citation}"


def _line_groups(lines: list[str], limit: int = 3_900) -> tuple[str, ...]:
    if not lines:
        return ()
    groups: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if len(line) > limit:
            raise AIInputError("A lecture-summary section is too long for Discord.")
        addition = len(line) + (2 if current else 0)
        if current and size + addition > limit:
            groups.append("\n\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + (2 if len(current) > 1 else 0)
    if current:
        groups.append("\n\n".join(current))
    return tuple(groups)


def _embed(title: str, description: str) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "color": SUMMARY_BLUE,
        "footer": {"text": "Attendr • Based on course materials, not a transcript"},
    }


def _discord_text(value: str, *, inline: bool = False) -> str:
    cleaned = str(value).replace("||", "｜｜").replace("\x00", "")
    cleaned = re.sub(r"(?i)\b(https?):/{2}", lambda match: f"{match[1]}:\u200b//", cleaned)
    cleaned = re.sub(r"(?i)\bwww\.", "www\u200b.", cleaned)
    if inline:
        cleaned = cleaned.replace("`", "ˋ").replace("\n", " ")
    return cleaned.strip()
