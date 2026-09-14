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
from .canvas_client import CanvasClient, INTRODUCTORY_MATERIAL_PATTERN, LectureMaterial
from .lecture_files import lecture_file_limit
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
    failed: int = 0


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
        retry_hours: int = 336,
        max_file_bytes: int | None = None,
    ) -> None:
        self.canvas = canvas
        self.ai = ai
        self.notifier = notifier
        self.schedule = schedule
        self.materials_directory = Path(materials_directory).expanduser().resolve()
        self.state_path = Path(state_path).expanduser().resolve()
        self.store = StateStore(self.state_path) if self.state_path.suffix != ".json" else None
        self.retry_hours = retry_hours
        self.max_file_bytes = max_file_bytes if max_file_bytes is not None else lecture_file_limit()

    def run(self, *, now: datetime | None = None, force: bool = False) -> LectureQuizReport:
        current = now or datetime.now(UTC)
        due = self.schedule.ended_lecture_sessions(current, retry_hours=self.retry_hours)
        if not due:
            return LectureQuizReport(0, 0, 0, ())
        state = self._load_state() if self.store is None else {"sent": {}}
        pending = [
            (session, ended_at)
            for session, ended_at in due
            if force or not self._already_sent(session.session_id(ended_at.date()), state)
        ]
        already = len(due) - len(pending)
        if not pending:
            return LectureQuizReport(0, already, 0, ())
        needs_materials = any(
            force
            or not self.store
            or not self.store.quiz(f"lecture-quiz:{session.session_id(ended_at.date())}")
            for session, ended_at in pending
        )
        downloads = (
            self.canvas.download_lecture_materials(
                self.materials_directory,
                excluded_course_patterns=self.schedule.excluded_course_patterns,
                max_file_bytes=self.max_file_bytes,
                module_policy=self.schedule.lecture_module_policy,
            )
            if needs_materials
            else SimpleNamespace(materials=(), warnings=())
        )
        sent = waiting = failed = 0
        warnings = list(downloads.warnings)
        for session, ended_at in pending:
            session_id = session.session_id(ended_at.date())
            if session_id in state.get("sent", {}) and not force:
                already += 1
                continue
            key = f"lecture-quiz:{session_id}"
            cached = self.store.quiz(key) if self.store and not force else None
            selected = (
                self._select_materials(session, ended_at, downloads.materials) if not cached else ()
            )
            if not selected and not cached:
                waiting += 1
                warnings.append(
                    f"No matching slides for session {session_id}; will retry while in the configured window."
                )
                continue
            try:
                key = f"lecture-quiz:{session_id}"
                cached = self.store.quiz(key) if self.store and not force else None
                if cached:
                    payload = cached["payload"]
                else:
                    source = self._materials_text(selected)
                    questions = self.ai.generate_hybrid_quiz(source)
                    title = self._materials_title(selected)
                    payload = hybrid_quiz_discord_payload(title, questions)
                    if self.store and not force:
                        payload = self.store.save_quiz(key, payload)
                delivered = self.notifier.send_custom_notification(
                    key,
                    payload,
                    force=force,
                    destination="lecture_quizzes",
                    fixed_fingerprint="session",
                )
                if delivered:
                    state.setdefault("sent", {})[session_id] = {
                        "sent_at": current.astimezone(UTC).isoformat(),
                        "material_uid": ",".join(item.uid for item in selected)
                        if selected
                        else "cached",
                        "content_sha256": ",".join(item.content_sha256 for item in selected)
                        if selected
                        else "cached",
                    }
                    if not force:
                        if self.store:
                            self._save_material_mapping(session_id, selected)
                            self.store.complete_quiz(key)
                        else:
                            self._save_state(state)
                    sent += 1
                else:
                    if self.store and not force:
                        self.store.complete_quiz(key)
                    already += 1
            except (AIInputError, AIProviderError, DiscordNotificationError, OSError) as error:
                failed += 1
                detail = str(error) if isinstance(error, AIProviderError) else type(error).__name__
                warnings.append(f"Quiz failed for session {session_id}: {detail}")
        return LectureQuizReport(sent, already, waiting, tuple(warnings), failed)

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
        selected = self._select_materials(session, ended_at, materials)
        return selected[0] if selected else None

    def _select_materials(
        self,
        session: ClassSession,
        ended_at: datetime,
        materials: tuple[LectureMaterial, ...],
    ) -> tuple[LectureMaterial, ...]:
        day = ended_at.date()
        week = self.schedule.teaching_week(day)
        lecture = self.schedule.lecture_number(session, day)
        course_materials = tuple(
            material
            for material in materials
            if self.schedule.matches_course(session, material.course_name)
        )
        session_id = session.session_id(day)
        override = self.schedule.lecture_material_override(session, day)
        if override is not None:
            return self._override_materials(override, course_materials)
        cached = self._mapped_materials(session_id, course_materials)
        if cached:
            return cached
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
        structurally_allowed: list[LectureMaterial] = []
        for material in course_materials:
            text = f"{material.module_name or ''} {material.title}".casefold()
            score = 0
            numbered = re.match(r"^\s*(\d{1,2})([a-z])\s*[-–:]", material.title, re.I)
            if numbered:
                weekly = sorted(
                    (
                        s
                        for s in self.schedule.sessions
                        if s.course_key == session.course_key and s.activity == "lecture"
                    ),
                    key=lambda s: (s.weekday, s.start),
                )
                slot = weekly.index(session) if session in weekly else -1
                if int(numbered[1]) != week or ord(numbered[2].lower()) - ord("a") != slot:
                    continue
                score += 120
            explicit_lecture = re.search(r"\b(?:lecture|lect)[\s_-]*(\d+)\b", text)
            if explicit_lecture and int(explicit_lecture[1]) != lecture:
                continue
            if any(
                re.search(r"(?<!\d)" + re.escape(token) + r"(?!\d)", text) for token in date_tokens
            ):
                score += 100
            if re.search(rf"\bweek\s*0?{week}\b", text):
                score += 60
            if re.search(rf"\b(?:lecture|lect)\s*0?{lecture}\b", text):
                score += 80
            if INTRODUCTORY_MATERIAL_PATTERN.search(text):
                if week != 1 or lecture != 1:
                    continue
                score += 90
            structurally_allowed.append(material)
            if material.module_position == week:
                score += 35
            if material.item_position == lecture:
                score += 20
            updated = material.updated_at or datetime.min.replace(tzinfo=UTC)
            candidates.append((score, updated, material))
        if not candidates:
            return self._ordered_module_materials(
                lecture, course_materials, tuple(structurally_allowed)
            )
        score, _, selected = max(candidates, key=lambda value: (value[0], value[1]))
        if score < 60:
            return self._ordered_module_materials(
                lecture, course_materials, tuple(structurally_allowed)
            )
        module_key = self._module_key(selected)
        if module_key is None:
            return (selected,)
        bundled = tuple(
            material for _, _, material in candidates if self._module_key(material) == module_key
        )
        return self._sort_materials(bundled)

    @staticmethod
    def _module_key(material: LectureMaterial) -> tuple[object, ...] | None:
        module_id = getattr(material, "module_id", None)
        if module_id:
            return (getattr(material, "course_id", None), "id", str(module_id))
        module_name = getattr(material, "module_name", None)
        module_position = getattr(material, "module_position", None)
        if module_name and module_position is not None:
            return (
                getattr(material, "course_id", None),
                "position",
                module_position,
                module_name.casefold(),
            )
        return None

    @staticmethod
    def _sort_materials(materials: tuple[LectureMaterial, ...]) -> tuple[LectureMaterial, ...]:
        return tuple(
            sorted(
                materials,
                key=lambda item: (
                    getattr(item, "item_position", None) is None,
                    getattr(item, "item_position", None) or 0,
                    getattr(item, "title", "").casefold(),
                ),
            )
        )

    def _ordered_module_materials(
        self,
        lecture: int,
        materials: tuple[LectureMaterial, ...],
        allowed: tuple[LectureMaterial, ...],
    ) -> tuple[LectureMaterial, ...]:
        groups: dict[tuple[object, ...], list[LectureMaterial]] = {}
        positions: dict[tuple[object, ...], int] = {}
        for material in materials:
            key = self._module_key(material)
            position = getattr(material, "module_position", None)
            if key is None or position is None:
                continue
            groups.setdefault(key, []).append(material)
            positions[key] = position
        ordered = sorted(groups, key=lambda key: (positions[key], str(key)))
        if len({positions[key] for key in ordered}) != len(ordered):
            return ()
        if lecture < 1 or lecture > len(ordered):
            return ()
        allowed_ids = {id(item) for item in allowed}
        selected = tuple(item for item in groups[ordered[lecture - 1]] if id(item) in allowed_ids)
        return self._sort_materials(selected)

    def _override_materials(
        self, override: dict[str, object], materials: tuple[LectureMaterial, ...]
    ) -> tuple[LectureMaterial, ...]:
        module_id = str(override.get("module_id", "")).strip()
        module_name = str(override.get("module", "")).strip().casefold()
        item_ids = {str(value) for value in override.get("item_ids", [])}
        source_ids = {str(value) for value in override.get("source_ids", [])}
        selected = tuple(
            material
            for material in materials
            if (
                (not module_id or str(getattr(material, "module_id", "") or "") == module_id)
                and (
                    not module_name
                    or str(getattr(material, "module_name", "") or "").strip().casefold()
                    == module_name
                )
                and (not item_ids or str(getattr(material, "item_id", "") or "") in item_ids)
                and (not source_ids or str(getattr(material, "source_id", "") or "") in source_ids)
            )
        )
        if not any((module_id, module_name, item_ids, source_ids)):
            return ()
        keys = {self._module_key(item) for item in selected}
        if module_name and len(keys) > 1:
            return ()
        return self._sort_materials(selected)

    def _mapped_materials(
        self, session_id: str, materials: tuple[LectureMaterial, ...]
    ) -> tuple[LectureMaterial, ...]:
        if not self.store:
            return ()
        value = self.store.cache_get(f"lecture-material-map:v1:{session_id}")
        if not isinstance(value, dict) or not isinstance(value.get("material_uids"), list):
            return ()
        by_uid = {getattr(item, "uid", None): item for item in materials}
        selected = tuple(by_uid.get(uid) for uid in value["material_uids"])
        if not selected or any(item is None for item in selected):
            return ()
        return tuple(item for item in selected if item is not None)

    def _save_material_mapping(
        self, session_id: str, materials: tuple[LectureMaterial, ...]
    ) -> None:
        if self.store and materials:
            self.store.cache_set(
                f"lecture-material-map:v1:{session_id}",
                {
                    "material_uids": [item.uid for item in materials],
                    "module_ids": [getattr(item, "module_id", None) for item in materials],
                    "item_ids": [getattr(item, "item_id", None) for item in materials],
                },
            )

    @classmethod
    def _materials_text(cls, materials: tuple[LectureMaterial, ...]) -> str:
        sections = [
            f"Material: {material.title}\n{cls._material_text(material)}" for material in materials
        ]
        text = "\n\n---\n\n".join(section for section in sections if section.strip())
        if not text.strip():
            raise AIInputError("Canvas lecture materials contain no readable text.")
        return text

    @staticmethod
    def _materials_title(materials: tuple[LectureMaterial, ...]) -> str:
        first = materials[0]
        module_names = {
            getattr(item, "module_name", None)
            for item in materials
            if getattr(item, "module_name", None)
        }
        subject = (
            next(iter(module_names))
            if len(module_names) == 1
            else " + ".join(item.title for item in materials)
        )
        return f"{first.course_name} — {subject}"

    @staticmethod
    def _material_text(material: LectureMaterial) -> str:
        if material.local_path.suffix.casefold() == ".txt":
            return material.local_path.read_text(encoding="utf-8")
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
            if (
                not isinstance(value, dict)
                or value.get("version") != 1
                or not isinstance(value.get("sent"), dict)
            ):
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
