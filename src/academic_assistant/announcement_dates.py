"""Extract dated academic items from Canvas announcements with a local cache."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai_assistant import AIAssistant, AIInputError, AIProviderError, MajorDeadline
from .canvas_client import AcademicItem, Announcement
from .course_schedule import CourseSchedule
from .deadline_grounding import ground_deadline
from .materials_sync import CourseMaterialsSync
from .state_store import StateStore

UTC = timezone.utc
EXTRACTION_VERSION = 3
_DATE_SIGNAL = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}\b|\b\d{4}-\d{1,2}-\d{1,2}\b|"
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    re.IGNORECASE,
)
_DEADLINE_SIGNAL = re.compile(
    r"\b(?:due|deadline|exam|midterm|quiz|assignment|project|presentation|submit|test)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AnnouncementDatesReport:
    items: tuple[AcademicItem, ...]
    analyzed: int
    cached: int
    warnings: tuple[str, ...]
    complete: bool = True
    blocking_warnings: tuple[str, ...] = ()


class AnnouncementDatesSync:
    def __init__(
        self,
        ai: AIAssistant,
        *,
        index_path: str | os.PathLike[str],
        app_timezone: str = "America/Toronto",
        course_schedule: CourseSchedule | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self.state_store = state_store
        self.ai = ai
        self.index_path = Path(index_path).expanduser().resolve()
        self.timezone = ZoneInfo(app_timezone)
        self.course_schedule = course_schedule

    def sync(
        self, announcements: tuple[Announcement, ...]
    ) -> AnnouncementDatesReport:
        index = self._load()
        analyzed = 0
        cached = 0
        changed = False
        warnings: list[str] = []
        blocking_warnings: list[str] = []
        items: list[AcademicItem] = []
        current = {item.uid: item for item in announcements}
        # Retain dated corrections beyond the Canvas announcement lookback window.
        for uid, record in index.items():
            if uid not in current and isinstance(record, dict) and record.get("source"):
                source = dict(record["source"])
                for key in ("posted_at", "posted_at_local"):
                    source[key] = datetime.fromisoformat(source[key])
                current[uid] = Announcement(**source)
        for announcement in sorted(current.values(), key=lambda item: (item.posted_at, item.uid)):
            if not announcement.message_text.strip():
                continue
            digest = sha256(
                str((f"announcement-v{EXTRACTION_VERSION}", getattr(self.ai, "model", "injected"), self.timezone.key,
                     self.course_schedule.context_for_course(announcement.course_name) if self.course_schedule else None,
                     announcement.title, announcement.message_text)).encode("utf-8")
            ).hexdigest()
            record = index.get(announcement.uid)
            raw_deadlines: list[dict[str, object]]
            newly_analyzed = False
            provider_failed = False
            if isinstance(record, dict) and record.get("sha256") == digest:
                raw_deadlines = list(record.get("deadlines", []))
                cached += 1
            else:
                try:
                    raw_deadlines = self.ai.extract_major_deadlines(
                        announcement.message_text,
                        course_name=announcement.course_name,
                        source_title=f"Announcement: {announcement.title}",
                        current_date=announcement.posted_at_local.date(),
                        schedule_context=(
                            self.course_schedule.context_for_course(announcement.course_name)
                            if self.course_schedule else None
                        ),
                    )
                except (AIInputError, AIProviderError):
                    provider_failed = True
                    provider_warning = (
                        f"Could not inspect announcement dates: {announcement.course_name} — {announcement.title}."
                    )
                    warnings.append(provider_warning)
                    raw_deadlines = (
                        list(record.get("deadlines", []))
                        if isinstance(record, dict)
                        else []
                    )
                else:
                    newly_analyzed = True
                    analyzed += 1
            grounded_raw: list[dict[str, object]] = []
            for raw in raw_deadlines:
                try:
                    deadline = MajorDeadline.model_validate(raw)
                except (ValueError, TypeError):
                    warnings.append(
                        f"Ignored an invalid date in announcement: {announcement.title}."
                    )
                    continue
                result = ground_deadline(
                    announcement.message_text,
                    deadline,
                    reference_date=announcement.posted_at_local.date(),
                )
                if not result.accepted:
                    locator = f", {result.locator}" if result.locator else ""
                    warnings.append(
                        f"Rejected ungrounded AI deadline in announcement "
                        f"{announcement.course_name} — {announcement.title}: "
                        f"{deadline.title}, expected {deadline.due_date}{locator} "
                        f"({result.reason})."
                    )
                    continue
                grounded_raw.append(deadline.model_dump(mode="json"))
                items.append(self._to_item(announcement, deadline))
            if (
                provider_failed
                and self._may_contain_deadline(announcement.message_text)
            ):
                blocking_warnings.append(
                    f"Potential dated requirement could not be verified in "
                    f"{announcement.course_name} — {announcement.title}."
                )
            if newly_analyzed:
                index[announcement.uid] = {
                    "sha256": digest,
                    "deadlines": grounded_raw,
                    "source": {key: value.isoformat() if isinstance(value, datetime) else value
                               for key, value in asdict(announcement).items()},
                }
                changed = True
        if changed:
            self._save(index)
        unique = {item.uid: item for item in items}
        return AnnouncementDatesReport(
            items=tuple(sorted(unique.values(), key=lambda item: (item.due_at, item.uid))),
            analyzed=analyzed,
            cached=cached,
            warnings=tuple(warnings),
            complete=not blocking_warnings,
            blocking_warnings=tuple(blocking_warnings),
        )

    @staticmethod
    def _may_contain_deadline(text: str) -> bool:
        return bool(_DATE_SIGNAL.search(text) and _DEADLINE_SIGNAL.search(text))

    def _to_item(self, announcement: Announcement, deadline: MajorDeadline) -> AcademicItem:
        actual_date = date.fromisoformat(deadline.due_date)
        lecture = (
            self.course_schedule.lecture_for_course_on(
                announcement.course_name, actual_date
            )
            if self.course_schedule and deadline.kind == "exam"
            else None
        )
        if deadline.due_time:
            effective_date = actual_date
            effective_time = time.fromisoformat(deadline.due_time)
        elif lecture is not None:
            effective_date = actual_date
            effective_time = lecture.start
        else:
            effective_date = actual_date
            effective_time = time.min
        due_local = datetime.combine(effective_date, effective_time, tzinfo=self.timezone)
        all_day = deadline.due_time is None and lecture is None
        kind = deadline.kind if deadline.kind in {"exam", "quiz", "assignment"} else "assignment"
        end_local = (
            datetime.combine(actual_date, lecture.end, tzinfo=self.timezone)
            if lecture is not None and deadline.due_time is None
            else due_local + timedelta(hours=2 if kind == "exam" else 1)
        )
        return AcademicItem(
            uid=CourseMaterialsSync._deadline_uid(announcement.course_id, deadline.title),
            source="announcement_deadline",
            source_id=announcement.source_id,
            course_id=announcement.course_id,
            course_name=announcement.course_name,
            title=deadline.title,
            kind=kind,
            due_at=due_local.astimezone(UTC),
            due_at_local=due_local,
            end_at=None if all_day else end_local.astimezone(UTC),
            all_day=all_day,
            html_url=announcement.html_url,
            updated_at=announcement.posted_at,
            points_possible=None,
            submission_types=(),
            description_html=(
                f"Extracted from announcement '{announcement.title}'. "
                f"Actual stated date: {actual_date.isoformat()}. Evidence: {deadline.source_evidence}"
            ),
        )

    def _load(self) -> dict[str, object]:
        if self.state_store:
            value = self.state_store.cache_get("announcements")
            if value is not None:
                return value
        if not self.index_path.exists():
            return {}
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Announcement date cache is unreadable.") from error
        if not isinstance(value, dict):
            raise ValueError("Announcement date cache must contain an object.")
        return value

    def _save(self, index: dict[str, object]) -> None:
        if self.state_store:
            self.state_store.cache_set("announcements", index)
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{self.index_path.name}.", dir=self.index_path.parent, text=True
        )
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(index, output, indent=2, sort_keys=True)
                output.write("\n")
            temporary.replace(self.index_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
