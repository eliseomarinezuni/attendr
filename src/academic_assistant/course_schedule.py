"""Verified Fall 2026 timetable and Ontario Tech academic-calendar rules."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
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

    def lecture_for_course_on(
        self, canvas_name: str, day: date
    ) -> ClassSession | None:
        """Return the single scheduled lecture matching a course and date."""
        matches = [
            session
            for session in self.sessions
            if session.activity == "lecture"
            and session.weekday == day.weekday()
            and self.matches_course(session, canvas_name)
        ]
        return matches[0] if len(matches) == 1 else None

    def context_for_course(self, canvas_name: str) -> str | None:
        matching = [
            session
            for session in self.sessions
            if self.matches_course(session, canvas_name)
        ]
        if not matching:
            return None
        lines = [
            f"Fall term: {self.term_start.isoformat()} through {self.term_end.isoformat()}.",
            "Semester week 1 begins on the term start date.",
        ]
        if self.no_class_ranges:
            ranges = ", ".join(
                start.isoformat()
                if start == end
                else f"{start.isoformat()} through {end.isoformat()}"
                for start, end in self.no_class_ranges
            )
            lines.append(f"No-class dates: {ranges}.")
        weekday_names = (
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
        )
        for session in matching:
            lines.append(
                f"{session.activity.title()}: {weekday_names[session.weekday]} "
                f"{session.start.strftime('%H:%M')}-{session.end.strftime('%H:%M')}."
            )
        return "\n".join(lines)

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
                    uid=f"ontariotech:academic-date:{entry.uid}",
                    source="university_schedule",
                    source_id=entry.uid,
                    course_id=0,
                    course_name="Ontario Tech",
                    title=entry.title,
                    kind=entry.kind,
                    due_at=due_local.astimezone(UTC),
                    due_at_local=due_local,
                    end_at=None,
                    all_day=False,
                    html_url="https://registrar.ontariotechu.ca/academic-schedule/ug-academic-schedule.php",
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

    def merge_with_class_schedule(
        self, academic_items: tuple[AcademicItem, ...]
    ) -> tuple[tuple[AcademicItem, ...], frozenset[str]]:
        """Replace a lecture occurrence with an overlapping same-course exam."""
        exams = tuple(item for item in academic_items if item.kind == "exam")
        consumed_exam_uids: set[str] = set()
        class_items: list[AcademicItem] = []
        for class_item in self.scheduled_class_items():
            if class_item.kind != "lecture":
                class_items.append(class_item)
                continue
            class_end = class_item.end_at or class_item.due_at + timedelta(hours=1)
            matching = [
                exam
                for exam in exams
                if exam.uid not in consumed_exam_uids
                and exam.due_at_local.date() == class_item.due_at_local.date()
                and self._course_names_match(class_item.course_name, exam.course_name)
                and exam.due_at < class_end
                and (exam.end_at or exam.due_at + timedelta(hours=2)) > class_item.due_at
            ]
            if not matching:
                class_items.append(class_item)
                continue
            exam = sorted(matching, key=lambda item: (item.due_at, item.uid))[0]
            consumed_exam_uids.add(exam.uid)
            class_items.append(
                replace(
                    exam,
                    uid=class_item.uid,
                    source="class_schedule",
                    source_id=class_item.source_id,
                    course_name=class_item.course_name,
                    due_at=class_item.due_at,
                    due_at_local=class_item.due_at_local,
                    end_at=class_item.end_at,
                    all_day=False,
                    description_html=(
                        f"{exam.description_html or 'In-class assessment.'} "
                        "This event replaces the regularly scheduled lecture."
                    ),
                )
            )
        remaining = tuple(
            item for item in academic_items if item.uid not in consumed_exam_uids
        )
        merged = remaining + tuple(class_items)
        return (
            tuple(sorted(merged, key=lambda item: (item.due_at, item.uid))),
            frozenset(consumed_exam_uids),
        )

    def _course_names_match(self, scheduled_name: str, canvas_name: str) -> bool:
        return any(
            session.course_name == scheduled_name
            and self.matches_course(session, canvas_name)
            for session in self.sessions
        )
