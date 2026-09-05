"""Google Calendar synchronization for normalized Canvas academic items."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .canvas_client import AcademicItem

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
SCOPES = (CALENDAR_SCOPE,)


class CalendarConfigurationError(ValueError):
    """Raised when local Google Calendar configuration is invalid."""


class CalendarAuthenticationError(RuntimeError):
    """Raised when Google OAuth authentication cannot be completed."""


class CalendarAPIError(RuntimeError):
    """Raised when a Google Calendar API operation fails."""


SyncAction = Literal["created", "updated", "skipped"]


@dataclass(frozen=True, slots=True)
class CalendarSyncEntry:
    canvas_uid: str
    title: str
    action: SyncAction
    google_event_id: str | None
    html_link: str | None


@dataclass(frozen=True, slots=True)
class CalendarSyncReport:
    calendar_id: str
    calendar_name: str
    entries: tuple[CalendarSyncEntry, ...]

    @property
    def created(self) -> tuple[CalendarSyncEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "created")

    @property
    def updated(self) -> tuple[CalendarSyncEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "updated")

    @property
    def skipped(self) -> tuple[CalendarSyncEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "skipped")


class GoogleCalendarAuthenticator:
    """Load cached OAuth credentials or run the installed-app browser flow."""

    def __init__(
        self,
        credentials_path: str | os.PathLike[str],
        token_path: str | os.PathLike[str],
        *,
        scopes: Iterable[str] = SCOPES,
    ) -> None:
        self.credentials_path = Path(credentials_path).expanduser().resolve()
        self.token_path = Path(token_path).expanduser().resolve()
        self.scopes = tuple(scopes)
        if not self.scopes:
            raise CalendarConfigurationError(
                "At least one Google OAuth scope is required."
            )

    def authenticate(self) -> Credentials:
        """Return valid credentials and securely persist refreshed credentials."""
        credentials: Credentials | None = None

        if self.token_path.exists():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(self.token_path), self.scopes
                )
            except (GoogleAuthError, ValueError, OSError):
                # A corrupt, revoked, or scope-incompatible cache is replaced below.
                credentials = None

        if credentials and credentials.valid:
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                self._save_token(credentials)
                return credentials
            except GoogleAuthError:
                credentials = None

        if not self.credentials_path.is_file():
            raise CalendarConfigurationError(
                f"Google OAuth credentials were not found at {self.credentials_path}. "
                "Download Desktop app credentials and save them as credentials.json."
            )

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path), self.scopes
            )
            credentials = flow.run_local_server(
                port=0,
                access_type="offline",
                prompt="consent",
            )
        except (GoogleAuthError, OSError, ValueError) as error:
            raise CalendarAuthenticationError(
                "Google OAuth failed. Verify credentials.json, the consent-screen test "
                "user, and browser access."
            ) from error

        self._save_token(credentials)
        return credentials

    def build_service(self) -> Any:
        """Create an authenticated Calendar API v3 service."""
        try:
            return build(
                "calendar",
                "v3",
                credentials=self.authenticate(),
                cache_discovery=False,
            )
        except (GoogleAuthError, OSError, ValueError) as error:
            raise CalendarAuthenticationError(
                "Could not initialize the Google Calendar client."
            ) from error

    def _save_token(self, credentials: Credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self.token_path.name}.",
                dir=self.token_path.parent,
                text=True,
            )
            temporary_path = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
                token_file.write(credentials.to_json())
            temporary_path.chmod(0o600)
            temporary_path.replace(self.token_path)
            self.token_path.chmod(0o600)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise CalendarAuthenticationError(
                f"Could not securely save the Google OAuth token at {self.token_path}."
            ) from error


class GoogleCalendarSync:
    """Create or update Google Calendar events for Canvas academic items."""

    def __init__(
        self,
        service: Any,
        *,
        calendar_id: str | None = None,
        calendar_name: str | None = None,
        create_calendar_if_missing: bool = True,
        app_timezone: str = "America/Toronto",
        mark_submitted: bool = True,
        submitted_color_id: str = "10",
        event_duration_minutes: int = 30,
    ) -> None:
        if not calendar_id and calendar_name is not None and not calendar_name.strip():
            calendar_name = None
        if event_duration_minutes < 1:
            raise CalendarConfigurationError(
                "GOOGLE_EVENT_DURATION_MINUTES must be a positive integer."
            )
        if submitted_color_id and not submitted_color_id.isdigit():
            raise CalendarConfigurationError(
                "GOOGLE_SUBMITTED_COLOR_ID must be a numeric Google Calendar color ID."
            )
        try:
            self.timezone = ZoneInfo(app_timezone)
        except ZoneInfoNotFoundError as error:
            raise CalendarConfigurationError(
                f"APP_TIMEZONE is not a valid IANA timezone: {app_timezone}"
            ) from error

        self._service = service
        self._configured_calendar_id = calendar_id.strip() if calendar_id else None
        self._calendar_name = calendar_name.strip() if calendar_name else None
        self._create_calendar_if_missing = create_calendar_if_missing
        self._mark_submitted = mark_submitted
        self._submitted_color_id = submitted_color_id
        self._event_duration = timedelta(minutes=event_duration_minutes)
        self._resolved_calendar: tuple[str, str] | None = None

    @classmethod
    def from_env(
        cls, env_file: str | os.PathLike[str] | None = None
    ) -> GoogleCalendarSync:
        """Load configuration, authenticate, and construct a calendar sync client."""
        load_dotenv(dotenv_path=env_file, override=False)
        base_directory = Path(env_file).resolve().parent if env_file else Path.cwd()
        credentials_path = cls._resolve_path(
            os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"), base_directory
        )
        token_path = cls._resolve_path(
            os.getenv("GOOGLE_TOKEN_FILE", "token.json"), base_directory
        )
        service = GoogleCalendarAuthenticator(
            credentials_path, token_path
        ).build_service()
        return cls(
            service,
            calendar_id=os.getenv("GOOGLE_CALENDAR_ID") or None,
            calendar_name=os.getenv("GOOGLE_CALENDAR_NAME") or None,
            create_calendar_if_missing=cls._read_bool(
                "GOOGLE_CREATE_CALENDAR_IF_MISSING", True
            ),
            app_timezone=os.getenv("APP_TIMEZONE", "America/Toronto"),
            mark_submitted=cls._read_bool("GOOGLE_MARK_SUBMITTED", True),
            submitted_color_id=os.getenv("GOOGLE_SUBMITTED_COLOR_ID", "10").strip(),
            event_duration_minutes=cls._read_positive_int(
                "GOOGLE_EVENT_DURATION_MINUTES", 30
            ),
        )

    def sync_items(self, items: Iterable[AcademicItem]) -> CalendarSyncReport:
        """Synchronize items, updating existing events when their content changes."""
        calendar_id, calendar_name = self.resolve_calendar()
        existing_by_uid = self._load_existing_events(calendar_id)
        entries: list[CalendarSyncEntry] = []

        # Avoid duplicate writes if the caller provides the same Canvas item twice.
        unique_items = {item.uid: item for item in items}
        for item in sorted(
            unique_items.values(), key=lambda value: (value.due_at, value.uid)
        ):
            desired = self._event_body(item)
            fingerprint = self._fingerprint(desired)
            desired["extendedProperties"]["private"]["attendr_fingerprint"] = (
                fingerprint
            )
            existing = existing_by_uid.get(item.uid)

            if existing and self._existing_fingerprint(existing) == fingerprint:
                entries.append(self._entry(item, "skipped", existing))
                continue

            try:
                if existing:
                    result = (
                        self._service.events()
                        .update(
                            calendarId=calendar_id,
                            eventId=existing["id"],
                            body=desired,
                            sendUpdates="none",
                        )
                        .execute()
                    )
                    action: SyncAction = "updated"
                else:
                    result = (
                        self._service.events()
                        .insert(
                            calendarId=calendar_id,
                            body=desired,
                            sendUpdates="none",
                        )
                        .execute()
                    )
                    action = "created"
            except HttpError as error:
                raise self._api_error(
                    f"Could not sync {item.course_name} — {item.title}", error
                ) from None

            entries.append(self._entry(item, action, result))

        return CalendarSyncReport(
            calendar_id=calendar_id,
            calendar_name=calendar_name,
            entries=tuple(entries),
        )

    def resolve_calendar(self) -> tuple[str, str]:
        """Resolve an explicit ID, a named writable calendar, or primary."""
        if self._resolved_calendar is not None:
            return self._resolved_calendar
        if self._configured_calendar_id:
            name = self._calendar_name or self._configured_calendar_id
            self._resolved_calendar = (self._configured_calendar_id, name)
            return self._resolved_calendar
        if not self._calendar_name:
            self._resolved_calendar = ("primary", "Primary")
            return self._resolved_calendar

        matches: list[Mapping[str, Any]] = []
        page_token: str | None = None
        try:
            while True:
                response = (
                    self._service.calendarList()
                    .list(minAccessRole="writer", pageToken=page_token)
                    .execute()
                )
                matches.extend(
                    calendar
                    for calendar in response.get("items", [])
                    if calendar.get("summary") == self._calendar_name
                )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as error:
            raise self._api_error(
                "Could not list writable Google calendars", error
            ) from None

        if len(matches) > 1:
            raise CalendarConfigurationError(
                f'Multiple writable calendars are named "{self._calendar_name}". '
                "Set GOOGLE_CALENDAR_ID to the intended calendar ID."
            )
        if matches:
            calendar_id = str(matches[0]["id"])
            self._resolved_calendar = (calendar_id, self._calendar_name)
            return self._resolved_calendar
        if not self._create_calendar_if_missing:
            raise CalendarConfigurationError(
                f'No writable Google calendar named "{self._calendar_name}" was found. '
                "Create it, enable GOOGLE_CREATE_CALENDAR_IF_MISSING, or use primary."
            )

        try:
            created = (
                self._service.calendars()
                .insert(
                    body={
                        "summary": self._calendar_name,
                        "timeZone": self.timezone.key,
                    }
                )
                .execute()
            )
        except HttpError as error:
            raise self._api_error(
                f'Could not create Google calendar "{self._calendar_name}"', error
            ) from None
        calendar_id = str(created["id"])
        self._resolved_calendar = (calendar_id, self._calendar_name)
        return self._resolved_calendar

    def _load_existing_events(self, calendar_id: str) -> dict[str, Mapping[str, Any]]:
        events_by_uid: dict[str, Mapping[str, Any]] = {}
        page_token: str | None = None
        try:
            while True:
                response = (
                    self._service.events()
                    .list(
                        calendarId=calendar_id,
                        privateExtendedProperty="attendr_source=canvas",
                        showDeleted=False,
                        maxResults=2500,
                        pageToken=page_token,
                    )
                    .execute()
                )
                for event in response.get("items", []):
                    private = event.get("extendedProperties", {}).get("private", {})
                    uid = private.get("canvas_uid")
                    if uid:
                        events_by_uid[str(uid)] = event
                page_token = response.get("nextPageToken")
                if not page_token:
                    return events_by_uid
        except HttpError as error:
            raise self._api_error(
                "Could not inspect existing calendar events", error
            ) from None

    def _event_body(self, item: AcademicItem) -> dict[str, Any]:
        submitted = self._mark_submitted and item.submitted is True
        summary = f"[{item.course_name}] {item.title}"
        if submitted:
            summary = f"✅ {summary}"

        due_local = item.due_at.astimezone(self.timezone)
        description_lines = [
            "Synced by Attendr.",
            f"Course: {item.course_name}",
            f"Type: {item.kind.replace('_', ' ').title()}",
            f"Due: {due_local.strftime('%Y-%m-%d %H:%M %Z')}",
        ]
        if item.points_possible is not None:
            description_lines.append(f"Points: {item.points_possible:g}")
        if item.submission_state:
            description_lines.append(f"Canvas submission: {item.submission_state}")
        if item.source in {
            "syllabus_deadline",
            "announcement_deadline",
            "university_schedule",
            "class_schedule",
        } and item.description_html:
            description_lines.append(item.description_html[:800])
        safe_url = self._safe_url(item.html_url)
        if safe_url:
            description_lines.append(f"Canvas: {safe_url}")

        private_properties = {
            "attendr_source": "canvas",
            "canvas_uid": item.uid,
            "canvas_source": item.source,
            "canvas_source_id": item.source_id,
            "canvas_course_id": str(item.course_id),
            "canvas_submission_state": item.submission_state or "unknown",
        }
        body: dict[str, Any] = {
            "summary": summary,
            "description": "\n".join(description_lines),
            "transparency": "transparent",
            "extendedProperties": {"private": private_properties},
        }
        if safe_url:
            body["source"] = {"title": "Open in Canvas", "url": safe_url}

        if item.all_day:
            start_date = due_local.date()
            end_local = item.end_at.astimezone(self.timezone) if item.end_at else None
            end_date = (
                end_local.date()
                if end_local and end_local.date() > start_date
                else start_date + timedelta(days=1)
            )
            body["start"] = {"date": start_date.isoformat()}
            body["end"] = {"date": end_date.isoformat()}
        else:
            end_at = item.end_at or (item.due_at + self._event_duration)
            body["start"] = {
                "dateTime": due_local.isoformat(),
                "timeZone": self.timezone.key,
            }
            body["end"] = {
                "dateTime": end_at.astimezone(self.timezone).isoformat(),
                "timeZone": self.timezone.key,
            }

        if submitted and self._submitted_color_id:
            body["colorId"] = self._submitted_color_id
        return body

    @staticmethod
    def _fingerprint(body: Mapping[str, Any]) -> str:
        serialized = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _existing_fingerprint(event: Mapping[str, Any]) -> str | None:
        value = (
            event.get("extendedProperties", {})
            .get("private", {})
            .get("attendr_fingerprint")
        )
        return str(value) if value else None

    @staticmethod
    def _entry(
        item: AcademicItem, action: SyncAction, event: Mapping[str, Any]
    ) -> CalendarSyncEntry:
        event_id = event.get("id")
        html_link = event.get("htmlLink")
        return CalendarSyncEntry(
            canvas_uid=item.uid,
            title=f"{item.course_name} — {item.title}",
            action=action,
            google_event_id=str(event_id) if event_id else None,
            html_link=str(html_link) if html_link else None,
        )

    @staticmethod
    def _safe_url(value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlparse(value)
        return value if parsed.scheme in {"http", "https"} and parsed.netloc else None

    @staticmethod
    def _api_error(message: str, error: HttpError) -> CalendarAPIError:
        status = getattr(getattr(error, "resp", None), "status", None)
        suffix = f" (HTTP {status})" if status else ""
        return CalendarAPIError(f"{message}{suffix}.")

    @staticmethod
    def _resolve_path(value: str, base_directory: Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else base_directory / path

    @staticmethod
    def _read_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        normalized = raw.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise CalendarConfigurationError(f"{name} must be true or false.")

    @staticmethod
    def _read_positive_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw)
        except ValueError as error:
            raise CalendarConfigurationError(
                f"{name} must be a positive integer."
            ) from error
        if value < 1:
            raise CalendarConfigurationError(f"{name} must be a positive integer.")
        return value
