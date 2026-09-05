"""Match completed lectures to Canvas materials and send one hybrid quiz each."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from .ai_assistant import (
    AIAssistant,
    AIInputError,
    AIProviderError,
    extract_pdf_text_chunks,
    extract_powerpoint_text_chunks,
    send_hybrid_quiz_to_discord,
)
from .canvas_client import CanvasClient, LectureMaterial
from .course_schedule import ClassSession, CourseSchedule
from .notifier import DiscordNotificationError, DiscordNotifier

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class LectureQuizReport:
    sent: int
    already_sent: int
    waiting_for_slides: int
    warnings: tuple[str, ...]


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class LectureQuizRunner:
    def __init__(
        self,
        canvas: CanvasClient,
        ai: AIAssistant,
        notifier: DiscordNotifier,
        schedule: CourseSchedule,
        *,
        materials_directory: str | os.PathLike[str],
        state_path: str | os.PathLike[str],
        retry_hours: int = 30,
        max_file_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.canvas = canvas
        self.ai = ai
        self.notifier = notifier
        self.schedule = schedule
        self.materials_directory = Path(materials_directory).expanduser().resolve()
        self.state_path = Path(state_path).expanduser().resolve()
        self.retry_hours = retry_hours
        self.max_file_bytes = max_file_bytes

    def run(self, *, now: datetime | None = None, force: bool = False) -> LectureQuizReport:
        current = now or datetime.now(UTC)
        due = self.schedule.ended_lecture_sessions(current, retry_hours=self.retry_hours)
        if not due:
            return LectureQuizReport(0, 0, 0, ())
        downloads = self.canvas.download_lecture_materials(
            self.materials_directory,
            excluded_course_patterns=self.schedule.excluded_course_patterns,
            max_file_bytes=self.max_file_bytes,
        )
        state = self._load_state()
        sent = already = waiting = 0
        warnings = list(downloads.warnings)
        for session, ended_at in due:
            session_id = session.session_id(ended_at.date())
            if session_id in state.get("sent", {}) and not force:
                already += 1
                continue
            material = self._select_material(session, ended_at, downloads.materials)
            if material is None:
                waiting += 1
                continue
            try:
                source = self._material_text(material)
                questions = self.ai.generate_hybrid_quiz(source)
                title = f"{material.course_name} — {material.title}"
                delivered = send_hybrid_quiz_to_discord(
                    self.notifier,
                    title,
                    questions,
                    event_key=f"lecture-quiz:{session_id}",
                    force=force,
                )
                if delivered:
                    state.setdefault("sent", {})[session_id] = {
                        "sent_at": current.astimezone(UTC).isoformat(),
                        "material_uid": material.uid,
                        "content_sha256": material.content_sha256,
                    }
                    self._save_state(state)
                    sent += 1
                else:
                    already += 1
            except (AIInputError, AIProviderError, DiscordNotificationError, OSError) as error:
                warnings.append(
                    f"Quiz failed for {material.course_name}: {type(error).__name__}."
                )
        return LectureQuizReport(sent, already, waiting, tuple(warnings))

    def _select_material(
        self,
        session: ClassSession,
        ended_at: datetime,
        materials: tuple[LectureMaterial, ...],
    ) -> LectureMaterial | None:
        day = ended_at.date()
        week = self.schedule.teaching_week(day)
        lecture = self.schedule.lecture_number(session, day)
        date_tokens = {
            day.isoformat().casefold(),
            day.strftime("%b %d").casefold(),
            day.strftime("%B %d").casefold(),
            day.strftime("%m-%d"),
            f"{day.strftime('%b')} {day.day}".casefold(),
            f"{day.strftime('%B')} {day.day}".casefold(),
            f"{day.month}-{day.day}",
            f"{day.month}/{day.day}",
        }
        if day.month == 9:
            date_tokens.add(f"sept {day.day}")
        candidates: list[tuple[int, datetime, LectureMaterial]] = []
        for material in materials:
            if not self.schedule.matches_course(session, material.course_name):
                continue
            text = f"{material.module_name or ''} {material.title}".casefold()
            score = 0
            if any(token in text for token in date_tokens):
                score += 100
            if re.search(rf"\bweek\s*0?{week}\b", text):
                score += 60
            if re.search(rf"\b(?:lecture|lect)\s*0?{lecture}\b", text):
                score += 80
            if material.module_position == week:
                score += 35
            if material.item_position == lecture:
                score += 20
            updated = material.updated_at or datetime.min.replace(tzinfo=UTC)
            candidates.append((score, updated, material))
        if not candidates:
            return None
        score, _, selected = max(candidates, key=lambda value: (value[0], value[1]))
        return selected if score >= 20 else None

    @staticmethod
    def _material_text(material: LectureMaterial) -> str:
        if material.local_path.suffix.casefold() == ".pdf":
            chunks = extract_pdf_text_chunks(material.local_path)
        elif material.local_path.suffix.casefold() == ".pptx":
            chunks = extract_powerpoint_text_chunks(material.local_path)
        else:
            parser = _HTMLText()
            parser.feed(material.local_path.read_text(encoding="utf-8"))
            text = " ".join(" ".join(parser.parts).split())
            if not text:
                raise AIInputError("Canvas lecture page contains no readable text.")
            return text
        return "\n\n".join(chunk.text for chunk in chunks)

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {"version": 1, "sent": {}}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"version": 1, "sent": {}}
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "sent": {}}

    def _save_state(self, state: dict[str, object]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", dir=self.state_path.parent, text=True
        )
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(state, output, indent=2, sort_keys=True)
                output.write("\n")
            temporary.replace(self.state_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
