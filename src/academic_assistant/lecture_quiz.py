"""Match completed lectures to Canvas materials and send one hybrid quiz each."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

from .ai_assistant import (
    AI_TASKS,
    AIAssistant,
    AIInputError,
    AIProviderError,
    hybrid_quiz_discord_payload,
)
from .ai_efficiency import AI_USAGE
from .canvas_client import CanvasClient, INTRODUCTORY_MATERIAL_PATTERN, LectureMaterial
from .course_schedule import ClassSession, CourseSchedule
from .lecture_content import (
    LectureContentBundle,
    LectureContentSource,
    build_lecture_content_bundle,
    extract_lecture_material,
)
from .lecture_files import lecture_file_limit
from .lecture_summary import lecture_summary_discord_payload
from .notifier import DiscordNotificationError, DiscordNotifier
from .state_store import StateStore

UTC = timezone.utc


class SummaryRecord(TypedDict):
    payload: dict[str, Any]
    content_hash: str


@dataclass(frozen=True, slots=True)
class LectureQuizReport:
    sent: int
    already_sent: int
    waiting_for_slides: int
    warnings: tuple[str, ...]
    failed: int = 0


@dataclass(frozen=True, slots=True)
class LectureReviewReport:
    summaries_sent: int
    summaries_already_sent: int
    summaries_waiting_for_slides: int
    summaries_failed: int
    summaries_deferred: int
    quizzes_sent: int
    quizzes_already_sent: int
    quizzes_waiting_for_slides: int
    quizzes_failed: int
    warnings: tuple[str, ...]


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
        max_age_minutes: int = 60,
        max_file_bytes: int | None = None,
        summary_generation_limit: int | None = None,
    ) -> None:
        self.canvas = canvas
        self.ai = ai
        self.notifier = notifier
        self.schedule = schedule
        self.materials_directory = Path(materials_directory).expanduser().resolve()
        self.state_path = Path(state_path).expanduser().resolve()
        self.store = StateStore(self.state_path) if self.state_path.suffix != ".json" else None
        self.retry_hours = retry_hours
        if max_age_minutes < 1:
            raise ValueError("Lecture review maximum age must be at least one minute.")
        self.max_age_minutes = max_age_minutes
        self.max_file_bytes = max_file_bytes if max_file_bytes is not None else lecture_file_limit()
        if summary_generation_limit is not None and summary_generation_limit < 1:
            raise ValueError("Lecture summary generation limit must be at least one.")
        self.summary_generation_limit = summary_generation_limit

    def run(self, *, now: datetime | None = None, force: bool = False) -> LectureQuizReport:
        """Backward-compatible quiz-only entry point."""
        report = self.process(
            now=now,
            force=force,
            include_summaries=False,
            include_quizzes=True,
        )
        return LectureQuizReport(
            report.quizzes_sent,
            report.quizzes_already_sent,
            report.quizzes_waiting_for_slides,
            report.warnings,
            report.quizzes_failed,
        )

    def process(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        include_summaries: bool = True,
        include_quizzes: bool = True,
    ) -> LectureReviewReport:
        """Generate selected post-lecture outputs from one shared content bundle."""
        if include_summaries and self.store is None:
            raise AIInputError("Lecture summaries require the SQLite ATTENDR_DB state store.")
        current = now or datetime.now(UTC)
        due = self.schedule.ended_lecture_sessions(current, retry_hours=self.retry_hours)
        if not force:
            cutoff = current.astimezone(UTC) - timedelta(minutes=self.max_age_minutes)
            due = tuple(
                (session, ended_at)
                for session, ended_at in due
                if cutoff <= ended_at.astimezone(UTC) <= current.astimezone(UTC)
            )
        if not due:
            return LectureReviewReport(0, 0, 0, 0, 0, 0, 0, 0, 0, ())
        state: dict[str, Any] = self._load_state() if self.store is None else {"sent": {}}
        summary_records = {
            session.session_id(ended_at.date()): self._summary_record(
                session.session_id(ended_at.date())
            )
            for session, ended_at in due
            if include_summaries and not force
        }
        summary_already = sum(
            1
            for session, ended_at in due
            if include_summaries
            and not force
            and self._summary_delivered(
                session.session_id(ended_at.date()),
                summary_records.get(session.session_id(ended_at.date())),
            )
        )
        quiz_already = sum(
            1
            for session, ended_at in due
            if include_quizzes
            and not force
            and self._already_sent(session.session_id(ended_at.date()), state)
        )
        pending = [
            (session, ended_at)
            for session, ended_at in due
            if force
            or (
                include_summaries
                and not self._summary_delivered(
                    session.session_id(ended_at.date()),
                    summary_records.get(session.session_id(ended_at.date())),
                )
            )
            or (
                include_quizzes
                and not self._already_sent(session.session_id(ended_at.date()), state)
            )
        ]
        if not pending:
            return LectureReviewReport(0, summary_already, 0, 0, 0, 0, quiz_already, 0, 0, ())
        needs_materials = any(
            force
            or (include_summaries and not summary_records.get(session.session_id(ended_at.date())))
            or (
                include_quizzes
                and (
                    not self.store
                    or not self.store.quiz(f"lecture-quiz:{session.session_id(ended_at.date())}")
                )
            )
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
        summary_sent = summary_waiting = summary_failed = summary_deferred = 0
        summary_generations = 0
        quiz_sent = quiz_waiting = quiz_failed = 0
        warnings = list(downloads.warnings)
        for session, ended_at in pending:
            session_id = session.session_id(ended_at.date())
            summary_record = summary_records.get(session_id) if not force else None
            summary_pending = include_summaries and (
                force or not self._summary_delivered(session_id, summary_record)
            )
            if (
                summary_pending
                and not summary_record
                and self.summary_generation_limit is not None
                and summary_generations >= self.summary_generation_limit
            ):
                summary_pending = False
                summary_deferred += 1
                warnings.append(
                    f"Summary deferred for session {session_id}: "
                    "quota-safe generation limit reached."
                )
            quiz_pending = include_quizzes and (force or not self._already_sent(session_id, state))
            quiz_key = f"lecture-quiz:{session_id}"
            quiz_record = self.store.quiz(quiz_key) if self.store and not force else None
            generation_needed = (summary_pending and not summary_record) or (
                quiz_pending and not quiz_record
            )
            selected: tuple[LectureMaterial, ...] = ()
            bundle = None
            if generation_needed:
                selected = self._select_materials(session, ended_at, downloads.materials)
                if selected:
                    try:
                        bundle = self._build_content_bundle(session, ended_at, selected)
                        warnings.extend(bundle.warnings)
                        if not force:
                            self._save_material_mapping(session_id, selected)
                    except (AIInputError, OSError) as error:
                        detail = type(error).__name__
                        if summary_pending and not summary_record:
                            summary_failed += 1
                            summary_pending = False
                            warnings.append(f"Summary failed for session {session_id}: {detail}")
                        if quiz_pending and not quiz_record:
                            quiz_failed += 1
                            quiz_pending = False
                            warnings.append(f"Quiz failed for session {session_id}: {detail}")
                else:
                    if summary_pending and not summary_record:
                        summary_waiting += 1
                        summary_pending = False
                    if quiz_pending and not quiz_record:
                        quiz_waiting += 1
                        quiz_pending = False
                    warnings.append(
                        f"No matching slides for session {session_id}; will retry while in the configured window."
                    )
                if not summary_pending and not quiz_pending:
                    continue

            if summary_pending:
                try:
                    if summary_record:
                        AI_USAGE.record("lecture_summary", cache_hits=1)
                        summary_payload = summary_record["payload"]
                        summary_fingerprint = summary_record["content_hash"]
                    else:
                        assert bundle is not None
                        summary_generations += 1
                        summary_payload, summary_fingerprint = self._generate_summary(
                            bundle, force=force
                        )
                    delivered = self.notifier.send_custom_notification(
                        f"lecture-summary:{session_id}",
                        summary_payload,
                        force=force,
                        destination="lecture_summaries",
                        expires_at=None
                        if force
                        else (ended_at + timedelta(minutes=self.max_age_minutes)).timestamp(),
                        fixed_fingerprint=summary_fingerprint,
                    )
                    if delivered:
                        summary_sent += 1
                    else:
                        summary_already += 1
                except (AIInputError, AIProviderError, DiscordNotificationError, OSError) as error:
                    summary_failed += 1
                    detail = (
                        str(error) if isinstance(error, AIProviderError) else type(error).__name__
                    )
                    warnings.append(f"Summary failed for session {session_id}: {detail}")

            if quiz_pending:
                try:
                    if quiz_record:
                        quiz_payload = quiz_record["payload"]
                    else:
                        assert bundle is not None
                        questions = self.ai.generate_hybrid_quiz(
                            bundle.summary_context(getattr(self.ai, "max_input_chars", 120_000))
                        )
                        quiz_payload = hybrid_quiz_discord_payload(bundle.title, questions)
                        if self.store and not force:
                            quiz_payload = self.store.save_quiz(quiz_key, quiz_payload)
                    delivered = self.notifier.send_custom_notification(
                        quiz_key,
                        quiz_payload,
                        force=force,
                        destination="lecture_quizzes",
                        expires_at=None
                        if force
                        else (ended_at + timedelta(minutes=self.max_age_minutes)).timestamp(),
                        fixed_fingerprint="session",
                    )
                    if delivered:
                        state.setdefault("sent", {})[session_id] = {
                            "sent_at": current.astimezone(UTC).isoformat(),
                            "material_uid": ",".join(item.uid for item in selected) or "cached",
                            "content_sha256": (
                                ",".join(item.content_sha256 for item in selected) or "cached"
                            ),
                        }
                        if not force:
                            if self.store:
                                self.store.complete_quiz(quiz_key)
                            else:
                                self._save_state(state)
                        quiz_sent += 1
                    else:
                        if self.store and not force:
                            self.store.complete_quiz(quiz_key)
                        quiz_already += 1
                except (AIInputError, AIProviderError, DiscordNotificationError, OSError) as error:
                    quiz_failed += 1
                    detail = (
                        str(error) if isinstance(error, AIProviderError) else type(error).__name__
                    )
                    warnings.append(f"Quiz failed for session {session_id}: {detail}")
        return LectureReviewReport(
            summary_sent,
            summary_already,
            summary_waiting,
            summary_failed,
            summary_deferred,
            quiz_sent,
            quiz_already,
            quiz_waiting,
            quiz_failed,
            tuple(warnings),
        )

    def _build_content_bundle(
        self,
        session: ClassSession,
        ended_at: datetime,
        materials: tuple[LectureMaterial, ...],
    ) -> LectureContentBundle:
        if all(isinstance(getattr(item, "local_path", None), Path) for item in materials):
            return build_lecture_content_bundle(session, ended_at, materials)
        # Compatibility for synthetic callers that supplied material-like test objects
        # before the source-preserving bundle existed. Production LectureMaterial values
        # always carry a concrete Path and use the audited extraction path above.
        text = self._materials_text(materials)
        first = materials[0]
        text_hash = sha256(text.encode("utf-8")).hexdigest()
        source = LectureContentSource(
            reference_id="S1",
            canvas_uid=str(getattr(first, "uid", "synthetic")),
            canvas_source_id=str(getattr(first, "source_id", "synthetic")),
            title=str(getattr(first, "title", "Lecture material")),
            content_hash=str(getattr(first, "content_sha256", text_hash)),
            text_hash=text_hash,
            text=text,
            location=None,
            extraction_method="synthetic",
            safe_url=None,
        )
        session_id = session.session_id(ended_at.date())
        course_name = str(getattr(first, "course_name", "Lecture"))
        return LectureContentBundle(
            course_key=str(getattr(session, "course_key", "synthetic")),
            course_name=course_name,
            session_id=session_id,
            session_date=ended_at.date().isoformat(),
            session_type=str(getattr(session, "activity", "lecture")),
            lecture_label=str(getattr(first, "title", "Lecture material")),
            sources=(source,),
            combined_text=text,
            content_hash=sha256(
                f"{session_id}:{source.content_hash}:{text_hash}".encode("utf-8")
            ).hexdigest(),
        )

    def _summary_record(self, session_id: str) -> SummaryRecord | None:
        if not self.store:
            return None
        value = self.store.cache_get(f"lecture-summary-session:v1:{session_id}")
        if not isinstance(value, dict):
            return None
        if not isinstance(value.get("payload"), dict) or not isinstance(
            value.get("content_hash"), str
        ):
            return None
        return {"payload": value["payload"], "content_hash": value["content_hash"]}

    def _summary_delivered(self, session_id: str, record: SummaryRecord | None) -> bool:
        if not self.store or not record:
            return False
        return self.store.was_sent(
            f"lecture_summaries:custom:lecture-summary:{session_id}",
            str(record["content_hash"]),
        )

    def _generate_summary(
        self, bundle: LectureContentBundle, *, force: bool
    ) -> tuple[dict[str, object], str]:
        policy = AI_TASKS["lecture_summary"]
        model = self.ai.model_for_task("lecture_summary")
        cache_identity = f"lecture-summary:v{policy.prompt_version}:{model}:{bundle.content_hash}"
        cache_key = f"lecture-summary-content:{sha256(cache_identity.encode()).hexdigest()}"
        cached = self.store.cache_get(cache_key) if self.store and not force else None
        if isinstance(cached, dict) and isinstance(cached.get("summary"), dict):
            AI_USAGE.record("lecture_summary", cache_hits=1)
            structured = cached["summary"]
            generated_at = str(cached.get("generated_at") or datetime.now(UTC).isoformat())
        else:
            AI_USAGE.record("lecture_summary", cache_misses=1)
            structured = self.ai.generate_lecture_summary(
                bundle.summary_context(self.ai.max_input_chars),
                source_texts=bundle.source_texts,
            )
            generated_at = datetime.now(UTC).isoformat()
            if self.store and not force:
                self.store.cache_set(
                    cache_key,
                    {
                        "summary": structured,
                        "content_hash": bundle.content_hash,
                        "prompt_version": policy.prompt_version,
                        "model": model,
                        "generated_at": generated_at,
                        "sources": self._summary_provenance(bundle),
                    },
                )
        payload = lecture_summary_discord_payload(bundle, structured)
        if self.store and not force:
            self.store.cache_set(
                f"lecture-summary-session:v1:{bundle.session_id}",
                {
                    "payload": payload,
                    "content_hash": bundle.content_hash,
                    "cache_key": cache_key,
                    "prompt_version": policy.prompt_version,
                    "model": model,
                    "generated_at": generated_at,
                    "sources": self._summary_provenance(bundle),
                },
            )
        return payload, bundle.content_hash

    @staticmethod
    def _summary_provenance(bundle: LectureContentBundle) -> list[dict[str, object]]:
        return [
            {
                "source_id": source.reference_id,
                "canvas_uid": source.canvas_uid,
                "canvas_source_id": source.canvas_source_id,
                "title": source.title,
                "content_hash": source.content_hash,
                "text_hash": source.text_hash,
                "location": source.location,
                "extraction_method": source.extraction_method,
            }
            for source in bundle.sources
        ]

    def _already_sent(self, session_id: str, state: dict[str, Any]) -> bool:
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
        self, override: dict[str, Any], materials: tuple[LectureMaterial, ...]
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
                    "material_uids": [str(item.uid) for item in materials],
                    "module_ids": [
                        str(value) if (value := getattr(item, "module_id", None)) else None
                        for item in materials
                    ],
                    "item_ids": [
                        str(value) if (value := getattr(item, "item_id", None)) else None
                        for item in materials
                    ],
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
        text, _, _ = extract_lecture_material(material)
        return text

    def _load_state(self) -> dict[str, Any]:
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

    def _save_state(self, state: dict[str, Any]) -> None:
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
