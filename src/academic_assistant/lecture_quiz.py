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
from types import SimpleNamespace

from .ai_assistant import (
    AIAssistant,
    AIInputError,
    AIProviderError,
    extract_pdf_text_chunks,
    extract_powerpoint_text_chunks,
    hybrid_quiz_discord_payload,
)
from .canvas_client import CanvasClient, LectureMaterial
from .course_schedule import ClassSession, CourseSchedule
from .state_store import StateStore
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
        self.store = StateStore(self.state_path) if self.state_path.suffix != ".json" else None
        self.retry_hours = retry_hours
        self.max_file_bytes = max_file_bytes

    def run(self, *, now: datetime | None = None, force: bool = False) -> LectureQuizReport:
        current = now or datetime.now(UTC)
        due = self.schedule.ended_lecture_sessions(current, retry_hours=self.retry_hours)
        if not due:
            return LectureQuizReport(0, 0, 0, ())
        state = self._load_state() if self.store is None else {"sent": {}}
        pending = [(session, ended_at) for session, ended_at in due
                   if force or not self._already_sent(session.session_id(ended_at.date()), state)]
        already = len(due) - len(pending)
        if not pending:
            return LectureQuizReport(0, already, 0, ())
        needs_materials = any(force or not self.store or not self.store.quiz(f"lecture-quiz:{session.session_id(ended_at.date())}") for session, ended_at in pending)
        downloads = (self.canvas.download_lecture_materials(
            self.materials_directory,
            excluded_course_patterns=self.schedule.excluded_course_patterns,
            max_file_bytes=self.max_file_bytes,
        ) if needs_materials else SimpleNamespace(materials=(), warnings=()))
        sent = waiting = 0
        warnings = list(downloads.warnings)
        for session, ended_at in pending:
            session_id = session.session_id(ended_at.date())
            if session_id in state.get("sent", {}) and not force:
                already += 1
                continue
            key = f"lecture-quiz:{session_id}"
            cached = self.store.quiz(key) if self.store and not force else None
            material = self._select_material(session, ended_at, downloads.materials) if not cached else None
            if material is None and not cached:
                waiting += 1
                continue
            try:
                key = f"lecture-quiz:{session_id}"
                cached = self.store.quiz(key) if self.store and not force else None
                if cached:
                    payload = cached["payload"]
                else:
                    source = self._material_text(material)
                    questions = self.ai.generate_hybrid_quiz(source)
                    title = f"{material.course_name} — {material.title}"
                    payload = hybrid_quiz_discord_payload(title, questions)
                    if self.store and not force:
                        payload = self.store.save_quiz(key, payload)
                delivered = self.notifier.send_custom_notification(
                    key, payload, force=force, destination="lecture_quizzes",
                    fixed_fingerprint="session",
                )
                if delivered:
                    state.setdefault("sent", {})[session_id] = {
                        "sent_at": current.astimezone(UTC).isoformat(),
                        "material_uid": material.uid if material else "cached",
                        "content_sha256": material.content_sha256 if material else "cached",
                    }
                    if not force:
                        if self.store:
                            self.store.complete_quiz(key)
                        else:
                            self._save_state(state)
                    sent += 1
                else:
                    if self.store and not force:
                        self.store.complete_quiz(key)
                    already += 1
            except (AIInputError, AIProviderError, DiscordNotificationError, OSError) as error:
                warnings.append(
                    f"Quiz failed for session {session_id}: {type(error).__name__}."
                )
        return LectureQuizReport(sent, already, waiting, tuple(warnings))

    def _already_sent(self, session_id: str, state: dict[str, object]) -> bool:
        if self.store:
            cached = self.store.quiz(f"lecture-quiz:{session_id}")
            return bool(cached and cached["sent_at"])
        return session_id in state.get("sent", {})

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
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("sent"), dict):
                raise ValueError("Unsupported lecture quiz state")
            return value
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Lecture quiz state is unreadable") from error

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
