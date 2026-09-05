"""Low-load study-session planning around Google Calendar availability."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from .calendar_sync import (
    CalendarAPIError,
    CalendarSyncReport,
    GoogleCalendarAuthenticator,
    GoogleCalendarSync,
)
from .canvas_client import AcademicItem

UTC = timezone.utc
PLANNABLE_KINDS = {"assignment", "quiz", "exam", "project", "presentation"}


@dataclass(frozen=True, slots=True)
class StudyTemplate:
    sessions: int
    minutes: int
    lead_days: int
    objectives: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BusyInterval:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class StudyPlanReport:
    sessions: tuple[AcademicItem, ...]
    calendar: CalendarSyncReport
    warnings: tuple[str, ...]


class StudyRemoteState:
    """Small authenticated bridge to the always-online interaction worker."""

    def __init__(self, url: str, secret: str, *, timeout: int = 15) -> None:
        self.url = url.rstrip("/")
        self.secret = secret
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> StudyRemoteState | None:
        url = os.getenv("STUDY_WORKER_URL", "").strip()
        secret = os.getenv("STUDY_SYNC_SECRET", "").strip()
        return cls(url, secret) if url and secret else None

    def get_state(self) -> dict[str, Any]:
        response = requests.get(
            f"{self.url}/api/state",
            headers={"Authorization": f"Bearer {self.secret}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def sync_sessions(
        self,
        sessions: Iterable[AcademicItem],
        report: CalendarSyncReport,
    ) -> None:
        events = {entry.canvas_uid: entry for entry in report.entries}
        payload = []
        for item in sessions:
            event = events.get(item.uid)
            if not event or not event.google_event_id:
                continue
            deadline = self._task_deadline(item)
            if deadline is None:
                continue
            payload.append(
                {
                    "session_id": item.uid,
                    "task_uid": item.source_id,
                    "title": item.title,
                    "course_name": item.course_name,
                    "start": item.due_at.isoformat(),
                    "end": item.end_at.isoformat() if item.end_at else None,
                    "task_due_at": deadline,
                    "calendar_id": report.calendar_id,
                    "event_id": event.google_event_id,
                }
            )
        response = requests.post(
            f"{self.url}/api/sessions/sync",
            headers={
                "Authorization": f"Bearer {self.secret}",
                "Content-Type": "application/json",
            },
            json={"sessions": payload},
            timeout=self.timeout,
        )
        response.raise_for_status()

    @staticmethod
    def _task_deadline(item: AcademicItem) -> str | None:
        marker = "Deadline: "
        if marker not in item.description_html:
            return None
        value = item.description_html.rsplit(marker, 1)[1].removesuffix(".").strip()
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            return None


class StudyPlanner:
    """Create a minimal, conflict-free set of study blocks."""

    WINDOWS = {
        0: ((time(13, 0), time(16, 30)), (time(18, 45), time(19, 30))),
        1: ((time(18, 45), time(22, 0)),),
        2: ((time(20, 15), time(22, 0)),),
        3: (),
        4: ((time(20, 15), time(22, 0)),),
        5: ((time(11, 0), time(18, 0)),),
        6: ((time(11, 0), time(18, 0)),),
    }
    TEMPLATES = {
        "quiz": StudyTemplate(2, 30, 4, ("Review key concepts", "Active-recall practice")),
        "assignment": StudyTemplate(3, 45, 7, ("Review requirements and outline", "Complete the main work", "Review and submit")),
        "project": StudyTemplate(4, 45, 14, ("Plan milestones", "Build the first section", "Complete the main work", "Review and submit")),
        "presentation": StudyTemplate(3, 45, 10, ("Plan the content", "Build and rehearse", "Final rehearsal")),
        "exam": StudyTemplate(5, 60, 14, ("Build a topic checklist", "Review core concepts", "Practice problems", "Active-recall practice", "Final review")),
    }

    def __init__(
        self,
        service: Any,
        *,
        timezone_name: str = "America/Toronto",
        calendar_name: str = "Attendr Study Plan",
        remote: StudyRemoteState | None = None,
        now_provider: Any | None = None,
    ) -> None:
        self.service = service
        self.timezone = ZoneInfo(timezone_name)
        self.calendar_name = calendar_name
        self.remote = remote
        self.now_provider = now_provider or (lambda: datetime.now(UTC))

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = None) -> StudyPlanner:
        load_dotenv(dotenv_path=env_file, override=False)
        root = Path(env_file).resolve().parent if env_file else Path.cwd()
        credentials = root / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        token = root / os.getenv("GOOGLE_TOKEN_FILE", "token.json")
        service = GoogleCalendarAuthenticator(credentials, token).build_service()
        return cls(
            service,
            timezone_name=os.getenv("APP_TIMEZONE", "America/Toronto"),
            calendar_name=os.getenv("GOOGLE_STUDY_CALENDAR_NAME", "Attendr Study Plan"),
            remote=StudyRemoteState.from_env(),
        )

    def sync(self, items: Iterable[AcademicItem]) -> StudyPlanReport:
        now = self.now_provider().astimezone(self.timezone)
        tasks = tuple(
            item
            for item in items
            if item.kind in PLANNABLE_KINDS
            and item.due_at > now.astimezone(UTC)
            and item.submitted is not True
        )
        calendar = GoogleCalendarSync(
            self.service,
            calendar_name=self.calendar_name,
            app_timezone=self.timezone.key,
            source_tag="study_plan",
            prune_missing=True,
        )
        study_calendar_id, _ = calendar.resolve_calendar()
        remote_state: dict[str, Any] = {}
        warnings: list[str] = []
        if self.remote:
            try:
                remote_state = self.remote.get_state()
            except (requests.RequestException, ValueError):
                warnings.append("The online study-session state was temporarily unavailable.")
        completed_tasks = set(remote_state.get("completed_tasks", []))
        completed_sessions = set(remote_state.get("completed_sessions", []))
        rescheduled_sessions = remote_state.get("rescheduled_sessions", {})
        if not isinstance(rescheduled_sessions, dict):
            rescheduled_sessions = {}
        tasks = tuple(item for item in tasks if item.uid not in completed_tasks)

        if tasks:
            latest_due = max(item.due_at for item in tasks)
        else:
            latest_due = now.astimezone(UTC) + timedelta(days=1)
        busy = self._load_busy(
            now.astimezone(UTC), latest_due + timedelta(days=1), study_calendar_id
        )
        sessions, planning_warnings = self._build_sessions(
            tasks, busy, completed_sessions, now, rescheduled_sessions
        )
        warnings.extend(planning_warnings)
        report = calendar.sync_items(sessions)
        if self.remote:
            try:
                self.remote.sync_sessions(sessions, report)
            except requests.RequestException:
                warnings.append("Study sessions reached Calendar but not the online button service.")
        return StudyPlanReport(sessions, report, tuple(warnings))

    def _load_busy(
        self, start: datetime, end: datetime, study_calendar_id: str
    ) -> list[BusyInterval]:
        try:
            calendars = self.service.calendarList().list(
                minAccessRole="reader", maxResults=250
            ).execute().get("items", [])
            ids = [
                str(item["id"])
                for item in calendars
                if item.get("id") and item.get("id") != study_calendar_id
            ][:50]
            responses: list[dict[str, Any]] = []
            chunk_start = start
            while chunk_start < end:
                chunk_end = min(chunk_start + timedelta(days=60), end)
                responses.append(
                    self.service.freebusy().query(
                        body={
                            "timeMin": chunk_start.isoformat(),
                            "timeMax": chunk_end.isoformat(),
                            "timeZone": self.timezone.key,
                            "items": [{"id": value} for value in ids],
                        }
                    ).execute()
                )
                chunk_start = chunk_end
        except HttpError as error:
            raise CalendarAPIError("Could not inspect Google Calendar availability.") from error
        intervals: list[BusyInterval] = []
        for response in responses:
            for details in response.get("calendars", {}).values():
                for value in details.get("busy", []):
                    intervals.append(
                        BusyInterval(
                            datetime.fromisoformat(value["start"].replace("Z", "+00:00")),
                            datetime.fromisoformat(value["end"].replace("Z", "+00:00")),
                        )
                    )
        return intervals

    def _build_sessions(
        self,
        tasks: tuple[AcademicItem, ...],
        busy: list[BusyInterval],
        completed_sessions: set[str],
        now: datetime,
        rescheduled_sessions: dict[str, Any] | None = None,
    ) -> tuple[tuple[AcademicItem, ...], tuple[str, ...]]:
        requests_to_place: list[tuple[date, datetime, AcademicItem, int, StudyTemplate]] = []
        for task in tasks:
            template = self.TEMPLATES[task.kind]
            due = task.due_at.astimezone(self.timezone)
            for index in range(template.sessions):
                fraction = index / max(template.sessions - 1, 1)
                offset = template.lead_days - round(fraction * (template.lead_days - 1))
                ideal = due.date() - timedelta(days=offset)
                requests_to_place.append((ideal, due, task, index, template))
        requests_to_place.sort(key=lambda value: (value[0], value[1], value[2].uid, value[3]))

        overrides: dict[str, tuple[datetime, datetime]] = {}
        for session_id, value in (rescheduled_sessions or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                override_start = datetime.fromisoformat(str(value["start"]))
                override_end = datetime.fromisoformat(str(value["end"]))
            except (KeyError, TypeError, ValueError):
                continue
            if override_start.tzinfo and override_end.tzinfo and override_start < override_end:
                overrides[str(session_id)] = (override_start, override_end)

        allocated = list(busy) + [
            BusyInterval(start.astimezone(UTC), end.astimezone(UTC))
            for start, end in overrides.values()
        ]
        used_days: set[date] = {
            start.astimezone(self.timezone).date() for start, _ in overrides.values()
        }
        sessions: list[AcademicItem] = []
        warnings: list[str] = []
        for ideal, due, task, index, template in requests_to_place:
            uid = self._session_uid(task.uid, index)
            if uid in completed_sessions:
                continue
            override = overrides.get(uid)
            if override and override[0].astimezone(self.timezone) >= now and override[1].astimezone(self.timezone) < due:
                start = override[0].astimezone(self.timezone)
                end = override[1].astimezone(self.timezone)
            else:
                start = self._find_slot(max(ideal, now.date()), due, template.minutes, allocated, used_days, now)
                end = start + timedelta(minutes=template.minutes) if start else None
            if start is None:
                warnings.append(f"No acceptable study slot was available for {task.title}.")
                continue
            assert end is not None
            allocated.append(BusyInterval(start.astimezone(UTC), end.astimezone(UTC)))
            used_days.add(start.date())
            objective = template.objectives[index]
            sessions.append(
                AcademicItem(
                    uid=uid,
                    source="study_plan",
                    source_id=task.uid,
                    course_id=task.course_id,
                    course_name=task.course_name,
                    title=f"{objective} — {task.title}",
                    kind="study_session",
                    due_at=start.astimezone(UTC),
                    due_at_local=start,
                    end_at=end.astimezone(UTC),
                    all_day=False,
                    html_url=task.html_url,
                    updated_at=task.updated_at,
                    points_possible=None,
                    submission_types=(),
                    description_html=f"Study objective: {objective}. Deadline: {due.isoformat()}.",
                )
            )
        return tuple(sorted(sessions, key=lambda item: item.due_at)), tuple(dict.fromkeys(warnings))

    def _find_slot(
        self,
        first_day: date,
        due: datetime,
        minutes: int,
        busy: list[BusyInterval],
        used_days: set[date],
        now: datetime,
    ) -> datetime | None:
        day = first_day
        final_day = due.date() - timedelta(days=1)
        while day <= final_day:
            if day not in used_days:
                for window_start, window_end in self.WINDOWS[day.weekday()]:
                    candidate = datetime.combine(day, window_start, tzinfo=self.timezone)
                    limit = datetime.combine(day, window_end, tzinfo=self.timezone)
                    while candidate + timedelta(minutes=minutes) <= limit:
                        if candidate >= now and not self._overlaps(candidate, candidate + timedelta(minutes=minutes), busy):
                            return candidate
                        candidate += timedelta(minutes=15)
            day += timedelta(days=1)
        return None

    @staticmethod
    def _overlaps(start: datetime, end: datetime, busy: list[BusyInterval]) -> bool:
        start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
        return any(start_utc < item.end and end_utc > item.start for item in busy)

    @staticmethod
    def _session_uid(task_uid: str, index: int) -> str:
        digest = sha256(task_uid.encode("utf-8")).hexdigest()[:20]
        return f"attendr:study:{digest}:{index + 1}"
