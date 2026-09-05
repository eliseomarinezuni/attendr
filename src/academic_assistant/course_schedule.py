"""Verified Fall 2026 timetable and Example University academic-calendar rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .canvas_client import AcademicItem

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class ClassSession:
    course_key: str
    course_name: str
    course_match: tuple[str, ...]
    weekday: int
    start: time
    end: time
    activity: Literal["lecture", "lab", "tutorial"]

    def session_id(self, day: date) -> str:
        return f"{day.isoformat()}:{self.course_key}:{self.end.strftime('%H%M')}"


@dataclass(frozen=True, slots=True)
class AcademicDate:
    uid: str
    title: str
    start_date: date
    end_date: date | None
    kind: str


class CourseSchedule:
    def __init__(self, data: dict[str, Any]) -> None:
        self.timezone = ZoneInfo(str(data["timezone"]))
        term = data["term"]
        self.term_start = date.fromisoformat(term["start_date"])
        self.term_end = date.fromisoformat(term["end_date"])
        self.no_class_ranges = tuple(
            (date.fromisoformat(item["start"]), date.fromisoformat(item["end"]))
            for item in term.get("no_class", [])
        )
        self.excluded_course_patterns = tuple(
            str(value).casefold() for value in data.get("excluded_course_patterns", [])
        )
        self.sessions = tuple(
            ClassSession(
                course_key=str(course["key"]),
                course_name=str(
                    course.get("name")
                    or str(course["key"]).replace("-", " ").title()
                ),
                course_match=tuple(str(value).casefold() for value in course["match"]),
                weekday=int(session["weekday"]),
                start=time.fromisoformat(session["start"]),
                end=time.fromisoformat(session["end"]),
                activity=session["type"],
            )
            for course in data["courses"]
            for session in course["sessions"]
        )
        self.academic_dates = tuple(
            AcademicDate(
                uid=str(item["uid"]),
                title=str(item["title"]),
                start_date=date.fromisoformat(item["start_date"]),
                end_date=(
                    date.fromisoformat(item["end_date"])
                    if item.get("end_date")
                    else None
                ),
                kind=str(item.get("kind", "administrative")),
            )
            for item in data.get("academic_dates", [])
        )

    @classmethod
    def load(cls, path: str | Path) -> CourseSchedule:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def is_no_class_day(self, day: date) -> bool:
        return any(start <= day <= end for start, end in self.no_class_ranges)

    def ended_lecture_sessions(
        self, now: datetime, *, retry_hours: int = 30
    ) -> tuple[tuple[ClassSession, datetime], ...]:
        local_now = now.astimezone(self.timezone)
        found: list[tuple[ClassSession, datetime]] = []
        for offset in range(0, 3):
            day = local_now.date() - timedelta(days=offset)
            if not self.term_start <= day <= self.term_end or self.is_no_class_day(day):
                continue
            for session in self.sessions:
                if session.activity != "lecture" or session.weekday != day.weekday():
                    continue
                ended_at = datetime.combine(day, session.end, tzinfo=self.timezone)
                if ended_at <= local_now <= ended_at + timedelta(hours=retry_hours):
                    found.append((session, ended_at))
        return tuple(sorted(found, key=lambda value: value[1]))

    def matches_course(self, session: ClassSession, canvas_name: str) -> bool:
        name = canvas_name.casefold()
        return any(value in name for value in session.course_match)

    def lecture_number(self, session: ClassSession, day: date) -> int:
        number = 0
        cursor = self.term_start
        while cursor <= day:
            if not self.is_no_class_day(cursor):
                number += sum(
                    candidate.activity == "lecture"
                    and candidate.course_key == session.course_key
                    and candidate.weekday == cursor.weekday()
                    for candidate in self.sessions
                )
            cursor += timedelta(days=1)
        return number

    def teaching_week(self, day: date) -> int:
        return ((day - self.term_start).days // 7) + 1

    def academic_calendar_items(self) -> tuple[AcademicItem, ...]:
        """Create advance reminder events at 11:59 PM the day before each date."""
        items: list[AcademicItem] = []
        for entry in self.academic_dates:
            reminder_day = entry.start_date - timedelta(days=1)
            due_local = datetime.combine(reminder_day, time(23, 59), tzinfo=self.timezone)
            description = f"Academic date: {entry.start_date.isoformat()}"
            if entry.end_date and entry.end_date != entry.start_date:
                description += f" through {entry.end_date.isoformat()}"
            items.append(
                AcademicItem(
                    uid=f"example-university:academic-date:{entry.uid}",
                    source="university_schedule",
                    source_id=entry.uid,
                    course_id=0,
                    course_name="Example University",
                    title=entry.title,
                    kind=entry.kind,
                    due_at=due_local.astimezone(UTC),
                    due_at_local=due_local,
                    end_at=None,
                    all_day=False,
                    html_url="https://example.edu/academic-calendar",
                    updated_at=None,
                    points_possible=None,
                    submission_types=(),
                    description_html=description,
                )
            )
        return tuple(items)

    def scheduled_class_items(self) -> tuple[AcademicItem, ...]:
        """Expand the timetable into every lecture, lab, and tutorial event."""
        items: list[AcademicItem] = []
        day = self.term_start
        while day <= self.term_end:
            if not self.is_no_class_day(day):
                for session in self.sessions:
                    if session.weekday != day.weekday():
                        continue
                    start_local = datetime.combine(day, session.start, tzinfo=self.timezone)
                    end_local = datetime.combine(day, session.end, tzinfo=self.timezone)
                    source_id = (
                        f"{day.isoformat()}:{session.course_key}:"
                        f"{session.activity}:{session.start.strftime('%H%M')}"
                    )
                    items.append(
                        AcademicItem(
                            uid=f"attendr:class-session:{source_id}",
                            source="class_schedule",
                            source_id=source_id,
                            course_id=0,
                            course_name=session.course_name,
                            title=session.activity.title(),
                            kind=session.activity,
                            due_at=start_local.astimezone(UTC),
                            due_at_local=start_local,
                            end_at=end_local.astimezone(UTC),
                            all_day=False,
                            html_url=None,
                            updated_at=None,
                            points_possible=None,
                            submission_types=(),
                            description_html=(
                                "Scheduled from the verified Fall 2026 timetable."
                            ),
                        )
                    )
            day += timedelta(days=1)
        return tuple(sorted(items, key=lambda item: (item.due_at, item.uid)))
