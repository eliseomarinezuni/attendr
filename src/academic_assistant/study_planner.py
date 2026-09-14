"""Low-load study-session planning around Google Calendar availability."""

from __future__ import annotations

import os
import uuid
import time as monotonic_time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from .state_store import StateStore
from .preferences import Preferences
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
        self.plan_token: str | None = None
        self.lease_started = 0.0
        self.state_store: StateStore | None = None
        self.profile = Preferences().worker_profile()

    @classmethod
    def from_env(cls) -> StudyRemoteState | None:
        url = os.getenv("STUDY_WORKER_URL", "").strip()
        secret = os.getenv("STUDY_SYNC_SECRET", "").strip()
        if bool(url) != bool(secret):
            raise CalendarAPIError(
                "Both STUDY_WORKER_URL and STUDY_SYNC_SECRET are required for online state."
            )
        return cls(url, secret) if url and secret else None

    def acquire(self) -> None:
        self.plan_token = self.plan_token or uuid.uuid4().hex
        response = requests.post(
            f"{self.url}/api/plan/acquire",
            headers={"Authorization": f"Bearer {self.secret}"},
            json={"token": self.plan_token},
            timeout=self.timeout,
        )
        response.raise_for_status()
        self.lease_started = monotonic_time.monotonic()

    def release(self) -> None:
        if not self.plan_token:
            return
        response = requests.post(
            f"{self.url}/api/plan/release",
            headers={"Authorization": f"Bearer {self.secret}"},
            json={"token": self.plan_token},
            timeout=self.timeout,
        )
        response.raise_for_status()
        self.plan_token = None

    def ensure_lease(self) -> None:
        if not self.plan_token or monotonic_time.monotonic() - self.lease_started > 15 * 60:
            raise CalendarAPIError(
                "Study planner lease expired; stop before further calendar writes."
            )

    def flush_pending(self) -> None:
        pending = self.state_store.cache_get("pending_study_sync") if self.state_store else None
        if pending is not None:
            self._post_sessions(pending)

    def _post_sessions(self, payload: list[dict[str, Any]]) -> None:
        self.ensure_lease()
        response = requests.post(
            f"{self.url}/api/sessions/sync",
            headers={"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"},
            json={"sessions": payload, "plan_token": self.plan_token, "profile": self.profile},
            timeout=self.timeout,
        )
        response.raise_for_status()
        if self.state_store:
            self.state_store.cache_set("pending_study_sync", None)

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
                raise CalendarAPIError("Incomplete Calendar mapping; Worker state preserved.")
            deadline = self._task_deadline(item)
            if deadline is None:
                raise CalendarAPIError("Missing study-task deadline; Worker state preserved.")
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
        if self.state_store:
            self.state_store.cache_set("pending_study_sync", payload)
        self._post_sessions(payload)

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
        "assignment": StudyTemplate(
            3,
            45,
            7,
            ("Review requirements and outline", "Complete the main work", "Review and submit"),
        ),
        "project": StudyTemplate(
            4,
            45,
            14,
            (
                "Plan milestones",
                "Build the first section",
                "Complete the main work",
                "Review and submit",
            ),
        ),
        "presentation": StudyTemplate(
            3, 45, 10, ("Plan the content", "Build and rehearse", "Final rehearsal")
        ),
        "exam": StudyTemplate(
            5,
            60,
            14,
            (
                "Build a topic checklist",
                "Review core concepts",
                "Practice problems",
                "Active-recall practice",
                "Final review",
            ),
        ),
    }

    def __init__(
        self,
        service: Any,
        *,
        timezone_name: str = "America/Toronto",
        calendar_name: str = "Attendr Study Plan",
        remote: StudyRemoteState | None = None,
        now_provider: Any | None = None,
        state_store: StateStore | None = None,
        preferences: Preferences | None = None,
    ) -> None:
        self.state_store = state_store
        self.preferences = preferences or Preferences()
        self.WINDOWS = {
            int(day): tuple(
                (time(start // 60, start % 60), time(end // 60, end % 60)) for start, end in windows
            )
            for day, windows in self.preferences.study_windows.items()
        }
        self.service = service
        self.timezone = ZoneInfo(timezone_name)
        self.calendar_name = calendar_name
        self.remote = remote
        self.now_provider = now_provider or (lambda: datetime.now(UTC))

    @classmethod
    def from_env(
        cls, env_file: str | os.PathLike[str] | None = None, *, service: Any | None = None
    ) -> StudyPlanner:
        load_dotenv(dotenv_path=env_file, override=False)
        root = Path(env_file).resolve().parent if env_file else Path.cwd()
        credentials = root / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        token = root / os.getenv("GOOGLE_TOKEN_FILE", "token.json")
        if service is None:
            service = GoogleCalendarAuthenticator(credentials, token).build_service()
        return cls(
            service,
            state_store=StateStore(root / os.getenv("ATTENDR_DB", "data/attendr.db")),
            preferences=Preferences.load(root),
            timezone_name=os.getenv("APP_TIMEZONE", "America/Toronto"),
            calendar_name=os.getenv("GOOGLE_STUDY_CALENDAR_NAME", "Attendr Study Plan"),
            remote=StudyRemoteState.from_env(),
        )

    def sync(
        self, items: Iterable[AcademicItem], *, inputs_complete: bool = False
    ) -> StudyPlanReport:
        if not inputs_complete:
            raise CalendarAPIError("Incomplete task data; existing study plan preserved.")
        if not self.remote:
            return self._sync(items, inputs_complete=True)
        self.remote.state_store = self.state_store
        self.remote.profile = self.preferences.worker_profile()
        try:
            self.remote.acquire()
        except requests.RequestException:
            raise CalendarAPIError(
                "Could not acquire study planner lease; existing plan preserved."
            ) from None
        try:
            self.remote.flush_pending()
            return self._sync(items, inputs_complete=True)
        finally:
            self.remote.release()

    def _sync(
        self, items: Iterable[AcademicItem], *, inputs_complete: bool = False
    ) -> StudyPlanReport:
        if not inputs_complete:
            raise CalendarAPIError("Incomplete task data; existing study plan preserved.")
        now = self.now_provider().astimezone(self.timezone)
        tasks = tuple(
            item
            for item in items
            if item.kind in PLANNABLE_KINDS
            and item.due_at > now.astimezone(UTC)
            and item.submitted is not True
        )
        remote_state: dict[str, Any] = {}
        if self.remote:
            try:
                remote_state = self.remote.get_state()
            except (requests.RequestException, ValueError):
                raise CalendarAPIError(
                    "Online study state unavailable; existing study plan preserved."
                ) from None
            if (
                not isinstance(remote_state, dict)
                or remote_state.get("operations_pending", False) is not False
                or any(
                    not isinstance(remote_state.get(key), list)
                    or any(not isinstance(value, str) for value in remote_state[key])
                    for key in ("completed_tasks", "completed_sessions")
                )
                or not isinstance(remote_state.get("rescheduled_sessions"), dict)
            ):
                raise CalendarAPIError("Invalid online study state; existing study plan preserved.")
            for override in remote_state["rescheduled_sessions"].values():
                try:
                    start = datetime.fromisoformat(override["start"])
                    end = datetime.fromisoformat(override["end"])
                    if start.tzinfo is None or end.tzinfo is None or end <= start:
                        raise ValueError("invalid interval")
                except (KeyError, TypeError, ValueError):
                    raise CalendarAPIError(
                        "Invalid rescheduled session; existing study plan preserved."
                    ) from None
        calendar = GoogleCalendarSync(
            self.service,
            state_store=self.state_store,
            calendar_name=self.calendar_name,
            app_timezone=self.timezone.key,
            source_tag="study_plan",
            prune_missing=True,
        )
        study_calendar_id, _ = calendar.resolve_calendar()
        warnings: list[str] = []
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
        if self.remote:
            if len(sessions) > 250:
                raise CalendarAPIError(
                    "Study plan exceeds the Worker session limit; existing plan preserved."
                )
            self.remote.ensure_lease()
        report = calendar.sync_items(
            sessions, before_write=self.remote.ensure_lease if self.remote else None
        )
        if self.remote:
            try:
                self.remote.sync_sessions(sessions, report)
            except requests.RequestException:
                warnings.append(
                    "Study sessions reached Calendar but not the online button service."
                )
        return StudyPlanReport(sessions, report, tuple(warnings))

    def _load_busy(
        self, start: datetime, end: datetime, study_calendar_id: str
    ) -> list[BusyInterval]:
        try:
            calendars = []
            page_token = None
            seen_tokens = set()
            while True:
                options = {"minAccessRole": "reader", "maxResults": 250}
                if page_token:
                    options["pageToken"] = page_token
                page = self.service.calendarList().list(**options).execute()
                calendars.extend(page.get("items", []))
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
                if page_token in seen_tokens:
                    raise CalendarAPIError("Calendar pagination repeated a token.")
                seen_tokens.add(page_token)
            ids = sorted(
                {
                    str(item["id"])
                    for item in calendars
                    if item.get("id") and item["id"] != study_calendar_id
                }
            )
            responses: list[dict[str, Any]] = []
            chunk_start = start
            while chunk_start < end:
                chunk_end = min(chunk_start + timedelta(days=60), end)
                for offset in range(0, len(ids), 50):
                    batch = ids[offset : offset + 50]
                    response = (
                        self.service.freebusy()
                        .query(
                            body={
                                "timeMin": chunk_start.isoformat(),
                                "timeMax": chunk_end.isoformat(),
                                "timeZone": self.timezone.key,
                                "items": [{"id": value} for value in batch],
                            }
                        )
                        .execute()
                    )
                    coverage = response.get("calendars", {})
                    for value in batch:
                        details = coverage.get(value, {})
                        errors = details.get("errors", [])
                        if errors and all(error.get("reason") == "notFound" for error in errors):
                            coverage[value] = {
                                "busy": self._event_availability(value, chunk_start, chunk_end)
                            }
                    if any(
                        value not in coverage
                        or coverage[value].get("errors")
                        or not isinstance(coverage[value].get("busy"), list)
                        for value in batch
                    ):
                        raise CalendarAPIError(
                            "Google returned incomplete availability; existing study plan preserved."
                        )
                    responses.append(response)
                chunk_start = chunk_end
        except HttpError as error:
            raise CalendarAPIError("Could not inspect Google Calendar availability.") from error
        intervals: list[BusyInterval] = []
        for response in responses:
            for details in response.get("calendars", {}).values():
                for value in details.get("busy", []):
                    start_at = datetime.fromisoformat(value["start"].replace("Z", "+00:00"))
                    end_at = datetime.fromisoformat(value["end"].replace("Z", "+00:00"))
                    if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
                        raise CalendarAPIError("Google returned an invalid busy interval.")
                    intervals.append(BusyInterval(start_at, end_at))
        return intervals

    def _event_availability(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[dict[str, str]]:
        """Public calendars can expose events while rejecting free/busy queries."""
        intervals = []
        token = None
        seen = set()
        while True:
            page = (
                self.service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    showDeleted=False,
                    maxResults=2500,
                    pageToken=token,
                )
                .execute()
            )
            for event in page.get("items", []):
                if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                    continue
                interval = {}
                for field in ("start", "end"):
                    value = event.get(field, {})
                    if value.get("dateTime"):
                        interval[field] = value["dateTime"]
                    elif value.get("date"):
                        interval[field] = (
                            datetime.fromisoformat(value["date"])
                            .replace(tzinfo=self.timezone)
                            .isoformat()
                        )
                    else:
                        raise CalendarAPIError(
                            "Google returned an invalid calendar event interval."
                        )
                intervals.append(interval)
            token = page.get("nextPageToken")
            if not token:
                return intervals
            if token in seen:
                raise CalendarAPIError("Calendar event pagination repeated a token.")
            seen.add(token)

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
            if task.kind in self.preferences.study_minutes:
                from dataclasses import replace

                template = replace(template, minutes=self.preferences.study_minutes[task.kind])
            due = task.due_at.astimezone(self.timezone)
            for index in range(template.sessions):
                fraction = index / max(template.sessions - 1, 1)
                offset = template.lead_days - round(fraction * (template.lead_days - 1))
                ideal = due.date() - timedelta(days=offset)
                requests_to_place.append((ideal, due, task, index, template))
        requests_to_place.sort(
            key=lambda value: (
                value[1],
                -self.preferences.course_priorities.get(str(value[2].course_id), 1),
                value[0],
                value[2].uid,
                value[3],
            )
        )

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
            if (
                override
                and override[0].astimezone(self.timezone) >= now
                and override[1].astimezone(self.timezone) < due
            ):
                start = override[0].astimezone(self.timezone)
                end = override[1].astimezone(self.timezone)
            else:
                start = self._find_slot(
                    max(ideal, now.date()), due, template.minutes, allocated, used_days, now
                )
                end = (
                    (start.astimezone(UTC) + timedelta(minutes=template.minutes)).astimezone(
                        self.timezone
                    )
                    if start
                    else None
                )
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
                    if limit.astimezone(UTC).astimezone(self.timezone).replace(
                        fold=0
                    ) != limit.replace(fold=0):
                        continue
                    while candidate < limit:
                        start_utc = candidate.astimezone(UTC)
                        end_utc = start_utc + timedelta(minutes=minutes)
                        valid_local = start_utc.astimezone(self.timezone).replace(
                            fold=0
                        ) == candidate.replace(fold=0)
                        if (
                            valid_local
                            and start_utc >= now.astimezone(UTC)
                            and end_utc <= limit.astimezone(UTC)
                            and not self._overlaps(start_utc, end_utc, busy)
                        ):
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
