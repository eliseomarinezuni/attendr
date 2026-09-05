"""Extract dated academic items from Canvas announcements with a local cache."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai_assistant import AIAssistant, AIInputError, AIProviderError, MajorDeadline
from .canvas_client import AcademicItem, Announcement

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class AnnouncementDatesReport:
    items: tuple[AcademicItem, ...]
    analyzed: int
    cached: int
    warnings: tuple[str, ...]


class AnnouncementDatesSync:
    def __init__(
        self,
        ai: AIAssistant,
        *,
        index_path: str | os.PathLike[str],
        app_timezone: str = "America/Toronto",
    ) -> None:
        self.ai = ai
        self.index_path = Path(index_path).expanduser().resolve()
        self.timezone = ZoneInfo(app_timezone)

    def sync(
        self, announcements: tuple[Announcement, ...]
    ) -> AnnouncementDatesReport:
        index = self._load()
        analyzed = 0
        cached = 0
        changed = False
        warnings: list[str] = []
        items: list[AcademicItem] = []
        for announcement in announcements:
            digest = sha256(
                f"{announcement.title}\n{announcement.message_text}".encode("utf-8")
            ).hexdigest()
            record = index.get(announcement.uid)
            raw_deadlines: list[dict[str, object]]
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
                    )
                except (AIInputError, AIProviderError):
                    warnings.append(
                        f"Could not inspect announcement dates: {announcement.course_name} — {announcement.title}."
                    )
                    continue
                index[announcement.uid] = {
                    "sha256": digest,
                    "deadlines": raw_deadlines,
                }
                analyzed += 1
                changed = True
            for raw in raw_deadlines:
                try:
                    deadline = MajorDeadline.model_validate(raw)
                    items.append(self._to_item(announcement, deadline))
                except (ValueError, TypeError):
                    warnings.append(
                        f"Ignored an invalid date in announcement: {announcement.title}."
                    )
        if changed:
            self._save(index)
        unique = {item.uid: item for item in items}
        return AnnouncementDatesReport(
            items=tuple(sorted(unique.values(), key=lambda item: (item.due_at, item.uid))),
            analyzed=analyzed,
            cached=cached,
            warnings=tuple(warnings),
        )

    def _to_item(self, announcement: Announcement, deadline: MajorDeadline) -> AcademicItem:
        actual_date = date.fromisoformat(deadline.due_date)
        if deadline.due_time:
            effective_date = actual_date
            effective_time = time.fromisoformat(deadline.due_time)
        else:
            effective_date = actual_date - timedelta(days=1)
            effective_time = time(23, 59)
        due_local = datetime.combine(effective_date, effective_time, tzinfo=self.timezone)
        normalized_title = " ".join(
            "".join(character if character.isalnum() else " " for character in deadline.title.casefold()).split()
        )
        uid_hash = sha256(normalized_title.encode("utf-8")).hexdigest()[:24]
        kind = deadline.kind if deadline.kind in {"exam", "quiz", "assignment"} else "assignment"
        return AcademicItem(
            uid=f"canvas:material-deadline:{announcement.course_id}:{uid_hash}",
            source="announcement_deadline",
            source_id=announcement.source_id,
            course_id=announcement.course_id,
            course_name=announcement.course_name,
            title=deadline.title,
            kind=kind,
            due_at=due_local.astimezone(UTC),
            due_at_local=due_local,
            end_at=(due_local + timedelta(hours=2 if kind == "exam" else 1)).astimezone(UTC),
            all_day=False,
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
        if not self.index_path.exists():
            return {}
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self, index: dict[str, object]) -> None:
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
