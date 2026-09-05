"""Canvas syllabus download, Gemini deadline extraction, and calendar normalization."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ai_assistant import (
    AIAssistant,
    AIInputError,
    AIProviderError,
    MajorDeadline,
    PDFExtractionError,
    extract_pdf_text_chunks,
)
from .canvas_client import AcademicItem, CanvasClient, SyllabusMaterial

UTC = timezone.utc


class MaterialsConfigurationError(ValueError):
    """Raised when syllabus synchronization configuration is invalid."""


class MaterialsStateError(RuntimeError):
    """Raised when the material extraction cache cannot be read or written."""


class CachedMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    course_id: int
    course_name: str
    title: str
    deadlines: list[MajorDeadline]


class MaterialsIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    sources: dict[str, CachedMaterial] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MaterialsSyncReport:
    materials_found: int
    materials_analyzed: int
    cached_materials_reused: int
    items: tuple[AcademicItem, ...]
    warnings: tuple[str, ...]


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


class CourseMaterialsSync:
    """Synchronize syllabus sources and return grounded calendar items."""

    def __init__(
        self,
        canvas: CanvasClient,
        ai: AIAssistant,
        *,
        materials_directory: str | os.PathLike[str] = "data/materials",
        index_path: str | os.PathLike[str] = "data/materials_index.json",
        app_timezone: str = "America/Toronto",
        max_file_bytes: int = 25 * 1024 * 1024,
        future_days: int = 550,
        now_provider: Any | None = None,
    ) -> None:
        if max_file_bytes < 1:
            raise MaterialsConfigurationError("Material size limit must be positive.")
        if future_days < 1:
            raise MaterialsConfigurationError(
                "Material future window must be positive."
            )
        try:
            self.timezone = ZoneInfo(app_timezone)
        except ZoneInfoNotFoundError as error:
            raise MaterialsConfigurationError(
                f"APP_TIMEZONE is not a valid IANA timezone: {app_timezone}"
            ) from error
        self.canvas = canvas
        self.ai = ai
        self.materials_directory = Path(materials_directory).expanduser().resolve()
        self.index_path = Path(index_path).expanduser().resolve()
        self.max_file_bytes = max_file_bytes
        self.future_days = future_days
        self._now_provider = now_provider or (lambda: datetime.now(UTC))

    @classmethod
    def from_env(
        cls,
        canvas: CanvasClient,
        env_file: str | os.PathLike[str] | None = None,
    ) -> CourseMaterialsSync:
        load_dotenv(dotenv_path=env_file, override=False)
        base_directory = Path(env_file).resolve().parent if env_file else Path.cwd()
        materials_directory = cls._resolve_path(
            os.getenv("CANVAS_MATERIALS_DIR", "data/materials"), base_directory
        )
        index_path = cls._resolve_path(
            os.getenv("CANVAS_MATERIALS_INDEX", "data/materials_index.json"),
            base_directory,
        )
        max_megabytes = cls._read_positive_int("CANVAS_MATERIAL_MAX_MB", 25)
        return cls(
            canvas,
            AIAssistant.from_env(env_file),
            materials_directory=materials_directory,
            index_path=index_path,
            app_timezone=os.getenv("APP_TIMEZONE", "America/Toronto"),
            max_file_bytes=max_megabytes * 1024 * 1024,
            future_days=cls._read_positive_int("CANVAS_MATERIAL_FUTURE_DAYS", 550),
        )

    def sync(self) -> MaterialsSyncReport:
        downloads = self.canvas.download_syllabus_materials(
            self.materials_directory, max_file_bytes=self.max_file_bytes
        )
        index = self._load_index()
        analyzed = 0
        reused = 0
        changed = False
        warnings = list(downloads.warnings)
        extracted: list[tuple[SyllabusMaterial, MajorDeadline]] = []
        local_today = self._now_local().date()

        for material in downloads.materials:
            cached = index.sources.get(material.uid)
            if cached and cached.content_sha256 == material.content_sha256:
                deadlines = cached.deadlines
                reused += 1
            else:
                try:
                    source_text = self._material_text(material)
                    raw_deadlines = self.ai.extract_major_deadlines(
                        source_text,
                        course_name=material.course_name,
                        source_title=material.title,
                        current_date=local_today,
                    )
                    deadlines = [
                        MajorDeadline.model_validate(item) for item in raw_deadlines
                    ]
                except (
                    AIInputError,
                    AIProviderError,
                    PDFExtractionError,
                    ValidationError,
                ):
                    warnings.append(
                        f"Could not extract deadlines from {material.course_name}: "
                        f"{material.title}."
                    )
                    continue
                index.sources[material.uid] = CachedMaterial(
                    content_sha256=material.content_sha256,
                    course_id=material.course_id,
                    course_name=material.course_name,
                    title=material.title,
                    deadlines=deadlines,
                )
                analyzed += 1
                changed = True

            for deadline in deadlines:
                extracted.append((material, deadline))

        if changed:
            self._save_index(index)

        items = self._normalize_deadlines(extracted, local_today, warnings)
        return MaterialsSyncReport(
            materials_found=len(downloads.materials),
            materials_analyzed=analyzed,
            cached_materials_reused=reused,
            items=items,
            warnings=tuple(warnings),
        )

    def _material_text(self, material: SyllabusMaterial) -> str:
        if material.content_type == "application/pdf":
            chunks = extract_pdf_text_chunks(material.local_path)
            return "\n\n".join(chunk.text for chunk in chunks)
        try:
            raw = material.local_path.read_text(encoding="utf-8")
        except OSError as error:
            raise AIInputError(
                f"Could not read downloaded material: {material.title}"
            ) from error
        parser = _HTMLTextExtractor()
        parser.feed(raw)
        parser.close()
        text = parser.text()
        if not text:
            raise AIInputError(
                f"Downloaded material contains no text: {material.title}"
            )
        return text

    def _normalize_deadlines(
        self,
        extracted: list[tuple[SyllabusMaterial, MajorDeadline]],
        local_today: date,
        warnings: list[str],
    ) -> tuple[AcademicItem, ...]:
        grouped: dict[str, list[tuple[SyllabusMaterial, MajorDeadline]]] = {}
        last_date = local_today + timedelta(days=self.future_days)
        earliest_date = local_today - timedelta(days=1)

        for material, deadline in extracted:
            deadline_date = date.fromisoformat(deadline.due_date)
            if not earliest_date <= deadline_date <= last_date:
                continue
            uid = self._deadline_uid(material.course_id, deadline.title)
            grouped.setdefault(uid, []).append((material, deadline))

        items: list[AcademicItem] = []
        for uid, candidates in grouped.items():
            unique_dates = {deadline.due_date for _, deadline in candidates}
            if len(unique_dates) > 1:
                material, deadline = candidates[0]
                warnings.append(
                    f"Conflicting syllabus dates for {material.course_name}: "
                    f"{deadline.title}; calendar event skipped."
                )
                continue
            material, deadline = max(
                candidates, key=lambda candidate: candidate[1].due_time is not None
            )
            deadline_date = date.fromisoformat(deadline.due_date)
            parsed_time = (
                time.fromisoformat(deadline.due_time) if deadline.due_time else time.min
            )
            due_local = datetime.combine(
                deadline_date, parsed_time, tzinfo=self.timezone
            )
            all_day = deadline.due_time is None
            duration = timedelta(hours=2 if deadline.kind == "exam" else 1)
            normalized_kind = (
                deadline.kind
                if deadline.kind in {"exam", "quiz", "assignment"}
                else "assignment"
            )
            items.append(
                AcademicItem(
                    uid=uid,
                    source="syllabus_deadline",
                    source_id=material.source_id,
                    course_id=material.course_id,
                    course_name=material.course_name,
                    title=deadline.title,
                    kind=normalized_kind,
                    due_at=due_local.astimezone(UTC),
                    due_at_local=due_local,
                    end_at=None if all_day else (due_local + duration).astimezone(UTC),
                    all_day=all_day,
                    html_url=material.html_url,
                    updated_at=material.updated_at,
                    points_possible=None,
                    submission_types=(),
                    description_html=(
                        f"Extracted from {material.title}. "
                        f"Evidence: {deadline.source_evidence}"
                    ),
                )
            )
        return tuple(sorted(items, key=lambda item: (item.due_at, item.uid)))

    def _load_index(self) -> MaterialsIndex:
        if not self.index_path.exists():
            return MaterialsIndex()
        try:
            return MaterialsIndex.model_validate_json(
                self.index_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as error:
            raise MaterialsStateError(
                f"Material extraction cache is invalid: {self.index_path}"
            ) from error

    def _save_index(self, index: MaterialsIndex) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self.index_path.name}.",
                dir=self.index_path.parent,
                text=True,
            )
            temporary_path = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(index.model_dump_json(indent=2))
                output.write("\n")
            temporary_path.replace(self.index_path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise MaterialsStateError(
                f"Material extraction cache could not be saved: {self.index_path}"
            ) from error

    def _now_local(self) -> datetime:
        current = self._now_provider()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise MaterialsConfigurationError(
                "now_provider must return a timezone-aware datetime."
            )
        return current.astimezone(self.timezone)

    @staticmethod
    def _deadline_uid(course_id: int, title: str) -> str:
        normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
        digest = sha256(normalized_title.encode("utf-8")).hexdigest()[:24]
        return f"canvas:material-deadline:{course_id}:{digest}"

    @staticmethod
    def _resolve_path(value: str, base_directory: Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else base_directory / path

    @staticmethod
    def _read_positive_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw)
        except ValueError as error:
            raise MaterialsConfigurationError(f"{name} must be positive.") from error
        if value < 1:
            raise MaterialsConfigurationError(f"{name} must be positive.")
        return value
