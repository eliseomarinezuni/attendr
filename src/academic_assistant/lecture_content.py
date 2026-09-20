"""Shared, source-preserving lecture content for summaries and quizzes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser

from .ai_assistant import AIInputError, extract_pdf_text_chunks, extract_powerpoint_text_chunks
from .canvas_client import LectureMaterial
from .course_schedule import ClassSession


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored += 1
        elif not self.ignored and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1
        elif not self.ignored and tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


@dataclass(frozen=True, slots=True)
class LectureContentSource:
    reference_id: str
    canvas_uid: str
    canvas_source_id: str
    title: str
    content_hash: str
    text_hash: str
    text: str
    location: str | None
    extraction_method: str
    safe_url: str | None


@dataclass(frozen=True, slots=True)
class LectureContentBundle:
    course_key: str
    course_name: str
    session_id: str
    session_date: str
    session_type: str
    lecture_label: str
    sources: tuple[LectureContentSource, ...]
    combined_text: str
    content_hash: str
    warnings: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return f"{self.course_name} — {self.lecture_label}"

    @property
    def source_texts(self) -> dict[str, str]:
        return {source.reference_id: source.text for source in self.sources}

    def summary_context(self, limit: int) -> str:
        """Keep source boundaries and broad structural coverage within the model limit."""
        if len(self.combined_text) <= limit:
            return self.combined_text
        separator = "\n\n[... material condensed ...]\n\n"
        overhead = sum(len(_source_section(source, "")) for source in self.sources)
        overhead += max(0, len(self.sources) - 1) * len("\n\n---\n\n")
        allowance = (limit - overhead) // max(1, len(self.sources))
        if allowance < 100:
            raise AIInputError("Too many lecture sources for the configured input limit.")
        sections: list[str] = []
        for source in self.sources:
            blocks = _structural_blocks(source.text)
            chosen: dict[int, str] = {}
            used = 0
            for index in _coverage_indices(len(blocks)):
                block = blocks[index]
                size = len(block) + (len(separator) if chosen else 0)
                if used + size > allowance:
                    continue
                chosen[index] = block
                used += size
            text = (
                separator.join(chosen[index] for index in sorted(chosen))
                if chosen
                else _bounded_prefix(source.text, allowance)
            )
            sections.append(_source_section(source, text))
        value = "\n\n---\n\n".join(sections)
        if len(value) > limit:
            raise AIInputError("Lecture material cannot be reduced safely to the configured limit.")
        return value


def build_lecture_content_bundle(
    session: ClassSession,
    ended_at: datetime,
    materials: tuple[LectureMaterial, ...],
) -> LectureContentBundle:
    """Extract each unique selected source once and preserve its provenance."""
    unique: list[LectureMaterial] = []
    seen_hashes: set[str] = set()
    for material in materials:
        if material.content_sha256 in seen_hashes:
            continue
        seen_hashes.add(material.content_sha256)
        unique.append(material)
    sources: list[LectureContentSource] = []
    warnings: list[str] = []
    for material in unique:
        try:
            text, location, method = extract_lecture_material(material)
        except (AIInputError, OSError, UnicodeError) as error:
            warnings.append(
                f"Skipped unreadable lecture source {material.uid} ({type(error).__name__})."
            )
            continue
        if not text.strip():
            warnings.append(f"Skipped empty lecture source {material.uid}.")
            continue
        sources.append(
            LectureContentSource(
                reference_id=f"S{len(sources) + 1}",
                canvas_uid=material.uid,
                canvas_source_id=material.source_id,
                title=material.title,
                content_hash=material.content_sha256,
                text_hash=sha256(text.encode("utf-8")).hexdigest(),
                text=text,
                location=location,
                extraction_method=method,
                safe_url=material.html_url,
            )
        )
    if not sources:
        raise AIInputError("Canvas lecture materials contain no readable text.")
    module_names = {
        material.module_name.strip()
        for material in unique
        if material.module_name and material.module_name.strip()
    }
    label = (
        next(iter(module_names))
        if len(module_names) == 1
        else " + ".join(source.title for source in sources)
    )
    combined = "\n\n---\n\n".join(_source_section(source, source.text) for source in sources)
    identity = {
        "sources": [[source.title, source.content_hash, source.text_hash] for source in sources],
    }
    return LectureContentBundle(
        course_key=session.course_key,
        course_name=session.course_name,
        session_id=session.session_id(ended_at.date()),
        session_date=ended_at.date().isoformat(),
        session_type=session.activity,
        lecture_label=label,
        sources=tuple(sources),
        combined_text=combined,
        content_hash=sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        warnings=tuple(warnings),
    )


def _source_section(source: LectureContentSource, text: str) -> str:
    metadata = json.dumps(
        {
            "source_id": source.reference_id,
            "title": source.title,
            "location": source.location,
            "extraction_method": source.extraction_method,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"SOURCE {metadata}\nBEGIN UNTRUSTED COURSE MATERIAL\n{text}\nEND SOURCE"


def extract_lecture_material(material: LectureMaterial) -> tuple[str, str | None, str]:
    suffix = material.local_path.suffix.casefold()
    if suffix == ".txt":
        text = material.local_path.read_text(encoding="utf-8")
        return text, _marker_range(text), "cached_text"
    if suffix == ".pdf":
        chunks = extract_pdf_text_chunks(material.local_path)
        location = f"pages {chunks[0].page_start}–{chunks[-1].page_end}"
        return "\n\n".join(chunk.text for chunk in chunks), location, "pdf_text"
    if suffix == ".pptx":
        chunks = extract_powerpoint_text_chunks(material.local_path)
        location = f"slides {chunks[0].page_start}–{chunks[-1].page_end}"
        return "\n\n".join(chunk.text for chunk in chunks), location, "powerpoint_text"
    parser = _HTMLText()
    parser.feed(material.local_path.read_text(encoding="utf-8"))
    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
    if not text:
        raise AIInputError("Canvas lecture page contains no readable text.")
    return text, None, "canvas_page_text"


def _marker_range(text: str) -> str | None:
    markers = [
        (kind.casefold(), int(number))
        for kind, number in re.findall(r"\[(Page|Slide) (\d+)\]", text)
    ]
    if not markers:
        return None
    kind = markers[0][0]
    if any(item[0] != kind for item in markers):
        return None
    return f"{kind}s {markers[0][1]}–{markers[-1][1]}"


def _structural_blocks(text: str) -> list[str]:
    blocks = [
        block.strip()
        for block in re.split(r"(?=\[(?:Page|Slide) \d+\])|\n\s*\n", text)
        if block.strip()
    ]
    return blocks or [text]


def _coverage_indices(size: int) -> tuple[int, ...]:
    if size <= 12:
        return tuple(range(size))
    return tuple(dict.fromkeys(round(index * (size - 1) / 11) for index in range(12)))


def _bounded_prefix(text: str, limit: int) -> str:
    candidate = text[:limit]
    boundaries = (
        candidate.rfind("\n\n"),
        candidate.rfind(". "),
        candidate.rfind("? "),
        candidate.rfind("! "),
        candidate.rfind("\n"),
    )
    boundary = max(boundaries)
    if boundary < min(1_000, limit // 2):
        raise AIInputError(
            "Lecture material has no safe structural boundary within the model input limit."
        )
    return candidate[: boundary + 1].strip()
