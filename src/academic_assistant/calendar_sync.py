"""Google Calendar synchronization for normalized Canvas academic items."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .state_store import StateStore
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_httplib2 import AuthorizedHttp
import httplib2

from .canvas_client import AcademicItem

UTC = timezone.utc

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
SCOPES = (CALENDAR_SCOPE,)


class CalendarConfigurationError(ValueError):
    """Raised when local Google Calendar configuration is invalid."""


class CalendarAuthenticationError(RuntimeError):
    """Raised when Google OAuth authentication cannot be completed."""


class CalendarAPIError(RuntimeError):
    """Raised when a Google Calendar API operation fails."""


SyncAction = Literal["created", "updated", "skipped", "deleted"]


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

    @property
    def deleted(self) -> tuple[CalendarSyncEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "deleted")


class GoogleCalendarAuthenticator:
    """Load cached OAuth credentials or run the installed-app browser flow."""

    def __init__(
        self,
        credentials_path: str | os.PathLike[str],
        token_path: str | os.PathLike[str],
        *,
        scopes: Iterable[str] = SCOPES,
        interactive: bool = False,
    ) -> None:
        self.credentials_path = Path(credentials_path).expanduser().resolve()
        self.token_path = Path(token_path).expanduser().resolve()
        self.scopes = tuple(scopes)
        self.interactive = interactive
        if not self.scopes:
            raise CalendarConfigurationError(
                "At least one Google OAuth scope is required."
            )

    def authenticate(self) -> Credentials:
        """Return valid credentials and securely persist refreshed credentials."""
        credentials: Credentials | None = None
        token_error: str | None = None

        if self.token_path.exists():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(self.token_path), self.scopes
                )
            except (GoogleAuthError, ValueError, OSError):
                token_error = "malformed"

        if token_error and not self.interactive:
            raise CalendarAuthenticationError(
                "Google Calendar authorization file is malformed. Run "
                "scripts/setup_google.py locally to reauthorize, then replace the "
                "GOOGLE_TOKEN_B64 GitHub Actions secret."
            )

        if credentials and not credentials.has_scopes(self.scopes):
            if not self.interactive:
                raise CalendarAuthenticationError(
                    "Google Calendar authorization does not include the required scope. "
                    "Run scripts/setup_google.py locally to reauthorize, then replace "
                    "the GOOGLE_TOKEN_B64 GitHub Actions secret."
                )
            credentials = None

        if credentials and credentials.valid:
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                self._save_token(credentials)
                return credentials
            except GoogleAuthError as error:
                if isinstance(error, TransportError) or getattr(error, "retryable", False):
                    raise CalendarAuthenticationError(
                        "Google token refresh is temporarily unavailable; retry later."
                    ) from None
                if not self.interactive:
                    raise CalendarAuthenticationError(
                        "Google Calendar authorization is no longer usable. Run "
                        "scripts/setup_google.py locally to reauthorize, then replace "
                        "the GOOGLE_TOKEN_B64 GitHub Actions secret."
                    ) from None
                credentials = None

        if not self.credentials_path.is_file():
            raise CalendarConfigurationError(
                f"Google OAuth credentials were not found at {self.credentials_path}. "
                "Download Desktop app credentials and save them as credentials.json."
            )

        if not self.interactive:
            raise CalendarAuthenticationError(
                "Google Calendar authorization is missing or unusable. Run "
                "scripts/setup_google.py locally to authorize, then replace the "
                "GOOGLE_TOKEN_B64 GitHub Actions secret."
            )

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path), self.scopes
            )
            credentials = flow.run_local_server(
                port=0,
                access_type="offline",
                prompt="consent",
                timeout_seconds=120,
            )
        except (GoogleAuthError, OSError, ValueError) as error:
            raise CalendarAuthenticationError(
                "Google OAuth failed. Verify credentials.json, the consent-screen test "
                "user, and browser access."
            ) from error

        self._save_token(credentials)
        return credentials

    @staticmethod
    def verify_service(service: Any) -> None:
        """Perform one read-only Calendar request to validate API authorization."""
        try:
            service.calendarList().list(maxResults=1).execute()
        except HttpError as error:
            status = int(getattr(error.resp, "status", 0) or 0)
            if status in {401, 403}:
                raise CalendarAuthenticationError(
                    "Google Calendar authorization is rejected or lacks the required "
                    "scope. Run scripts/setup_google.py locally to reauthorize, then "
                    "replace the GOOGLE_TOKEN_B64 GitHub Actions secret."
                ) from None
            if status == 429 or status >= 500:
                raise CalendarAuthenticationError(
                    "Google Calendar authentication check is temporarily unavailable; "
                    "retry later."
                ) from None
            raise CalendarAuthenticationError(
                "Google Calendar authentication check failed safely."
            ) from None
        except (GoogleAuthError, httplib2.HttpLib2Error, OSError) as error:
            if isinstance(error, (TransportError, httplib2.HttpLib2Error, OSError)):
                raise CalendarAuthenticationError(
                    "Google Calendar authentication check is temporarily unavailable; "
                    "retry later."
                ) from None
            raise CalendarAuthenticationError(
                "Google Calendar authentication check failed safely."
            ) from None

    def build_service(self) -> Any:
        """Create an authenticated Calendar API v3 service."""
        try:
            return build(
                "calendar",
                "v3",
                http=AuthorizedHttp(self.authenticate(), http=httplib2.Http(timeout=60)),
                cache_discovery=False,
            )
        except (GoogleAuthError, OSError, ValueError) as error:
            raise CalendarAuthenticationError(
                "Could not initialize the Google Calendar client."
            ) from error

    def _save_token(self, credentials: Credentials) -> None:
        if getattr(credentials, "refresh_token", "test-double") is None:
            raise CalendarAuthenticationError(
                "Google did not return an offline refresh token; the existing token was "
                "not replaced. Run setup again and grant consent."
            )
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
        source_tag: str = "canvas",
        prune_missing: bool = False,
        state_store: StateStore | None = None,
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

        self.state_store = state_store
        self._service = service
        self._configured_calendar_id = calendar_id.strip() if calendar_id else None
        self._calendar_name = calendar_name.strip() if calendar_name else None
        self._create_calendar_if_missing = create_calendar_if_missing
        self._mark_submitted = mark_submitted
        self._submitted_color_id = submitted_color_id
        self._event_duration = timedelta(minutes=event_duration_minutes)
        self._source_tag = source_tag
        self._prune_missing = prune_missing
        self._resolved_calendar: tuple[str, str] | None = None

    @classmethod
    def from_env(
        cls, env_file: str | os.PathLike[str] | None = None, *, service: Any | None = None
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
        if service is None:
            service = GoogleCalendarAuthenticator(
                credentials_path, token_path
            ).build_service()
        return cls(
            service,
            state_store=StateStore(cls._resolve_path(os.getenv("ATTENDR_DB", "data/attendr.db"), base_directory)),
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

    @staticmethod
    def _managed_equal(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
        def normalized(body: dict[str, Any]) -> dict[str, Any]:
            result = {key: body.get(key) for key in ("summary", "description", "source", "colorId")}
            result["transparency"] = body.get("transparency", "opaque")
            result["private"] = {key: value for key, value in body.get("extendedProperties", {}).get("private", {}).items() if key != "attendr_fingerprint"}
            for key in ("start", "end"):
                value = body.get(key, {})
                result[key] = (datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")).astimezone(UTC).isoformat()
                               if value.get("dateTime") else value.get("date"))
            return result
        return normalized(existing) == normalized(desired)

    def sync_items(
        self,
        items: Iterable[AcademicItem],
        *,
        delete_uids: Iterable[str] = (),
        before_write: Callable[[], None] | None = None,
    ) -> CalendarSyncReport:
        """Synchronize items, updating existing events when their content changes."""
        calendar_id, calendar_name = self.resolve_calendar()
        existing_by_uid = self._load_existing_events(calendar_id)
        entries: list[CalendarSyncEntry] = []

        # Avoid duplicate writes if the caller provides the same Canvas item twice.
        unique_items = {item.uid: item for item in items}
        desired_by_uid = {uid: self._event_body(item) for uid, item in unique_items.items()}
        for uid in set(delete_uids) - unique_items.keys():
            existing = existing_by_uid.pop(uid, None)
            if existing is None:
                previous = self.state_store.calendar_record(calendar_id, uid) if self.state_store else None
                if previous and previous["status"] == "pending" and previous["action"] == "deleted":
                    self.state_store.calendar_confirm(previous, previous["event_id"], notify=self._source_tag == "canvas")
                continue
            intent = self.state_store.calendar_intent(calendar_id, uid, existing["id"], "deleted", existing, "deleted") if self.state_store else None
            if before_write:
                before_write()
            try:
                self._service.events().delete(
                    calendarId=calendar_id,
                    eventId=existing["id"],
                    sendUpdates="none",
                ).execute()
            except HttpError as error:
                if error.resp.status not in (404, 410):
                    raise self._api_error(
                        f"Could not remove replaced Attendr event {uid}", error
                    ) from None
            if intent:
                self.state_store.calendar_confirm(intent, existing["id"], notify=self._source_tag == "canvas")
            entries.append(
                CalendarSyncEntry(
                    canvas_uid=uid,
                    title=str(existing.get("summary") or uid),
                    action="deleted",
                    google_event_id=str(existing.get("id")) if existing.get("id") else None,
                    html_link=None,
                )
            )
        for item in sorted(
            unique_items.values(), key=lambda value: (value.due_at, value.uid)
        ):
            desired = desired_by_uid[item.uid]
            fingerprint = self._fingerprint(desired)
            desired["extendedProperties"]["private"]["attendr_fingerprint"] = (
                fingerprint
            )
            existing = existing_by_uid.get(item.uid)

            previous = self.state_store.calendar_record(calendar_id, item.uid) if self.state_store else None
            mapped_deleted = False
            if existing is None and previous and previous["action"] != "deleted":
                try:
                    mapped = self._service.events().get(calendarId=calendar_id, eventId=previous["event_id"]).execute()
                    if mapped.get("status") == "cancelled":
                        mapped_deleted = True
                    elif mapped.get("extendedProperties", {}).get("private", {}).get("canvas_uid") != item.uid:
                        raise CalendarAPIError("Mapped Google event no longer belongs to this Canvas item.")
                    else:
                        existing = mapped
                except HttpError as error:
                    if error.resp.status not in (404, 410):
                        raise self._api_error("Could not reconcile the mapped Google event", error) from None
                    mapped_deleted = not (error.resp.status == 404 and previous["status"] == "pending" and previous["action"] == "created")
            if existing and self._managed_equal(existing, desired):
                if self.state_store and previous is None:
                    adopted = self.state_store.calendar_intent(calendar_id, item.uid, existing["id"], fingerprint, desired, "adopted")
                    self.state_store.calendar_confirm(adopted, existing["id"], notify=False)
                if previous and previous["status"] == "pending" and previous["fingerprint"] == fingerprint:
                    self.state_store.calendar_confirm(previous, existing["id"], notify=self._source_tag == "canvas")
                entries.append(self._entry(item, "skipped", existing))
                continue

            action = "updated" if existing else "created"
            event_id = existing["id"] if existing else (
                previous["event_id"] if previous and previous["action"] != "deleted" and not mapped_deleted else
                sha256(f"attendr:{calendar_id}:{self._source_tag}:{item.uid}:{previous['revision'] + 1 if previous else 1}".encode()).hexdigest()
            )
            intent = self.state_store.calendar_intent(calendar_id, item.uid, event_id, fingerprint, desired, action) if self.state_store else None
            if before_write:
                before_write()
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
                    desired["id"] = event_id
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
                if not existing and error.resp.status == 409:
                    result = self._service.events().get(calendarId=calendar_id, eventId=event_id).execute()
                    if not self._managed_equal(result, desired):
                        raise CalendarAPIError("Calendar event ID conflict requires reconciliation.") from None
                else:
                    raise self._api_error(
                        f"Could not sync {item.course_name} — {item.title}", error
                    ) from None
            if intent:
                self.state_store.calendar_confirm(intent, result["id"], notify=self._source_tag == "canvas")

            entries.append(self._entry(item, action, result))

        if self._prune_missing:
            for uid, event in existing_by_uid.items():
                if uid in unique_items:
                    continue
                intent = self.state_store.calendar_intent(calendar_id, uid, event["id"], "deleted", event, "deleted") if self.state_store else None
                if before_write:
                    before_write()
                try:
                    self._service.events().delete(
                        calendarId=calendar_id,
                        eventId=event["id"],
                        sendUpdates="none",
                    ).execute()
                except HttpError as error:
                    raise self._api_error(
                        f"Could not remove obsolete Attendr event {uid}", error
                    ) from None
                if intent:
                    self.state_store.calendar_confirm(intent, event["id"], notify=self._source_tag == "canvas")
                entries.append(
                    CalendarSyncEntry(
                        canvas_uid=uid,
                        title=str(event.get("summary") or uid),
                        action="deleted",
                        google_event_id=str(event.get("id")) if event.get("id") else None,
                        html_link=None,
                    )
                )

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
                        privateExtendedProperty=f"attendr_source={self._source_tag}",
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
                        if str(uid) in events_by_uid and events_by_uid[str(uid)].get("id") != event.get("id"):
                            raise CalendarAPIError(f"Duplicate managed Calendar UID requires review: {uid}")
                        events_by_uid[str(uid)] = event
                page_token = response.get("nextPageToken")
                if not page_token:
                    return events_by_uid
        except HttpError as error:
            raise self._api_error(
                "Could not inspect existing calendar events", error
            ) from None

    def _event_body(self, item: AcademicItem) -> dict[str, Any]:
        if item.due_at.tzinfo is None or (item.end_at is not None and item.end_at.tzinfo is None):
            raise CalendarConfigurationError("Calendar event timestamps must be timezone-aware.")
        if item.end_at is not None and item.end_at <= item.due_at:
            raise CalendarConfigurationError("Calendar event end must be later than its start.")
        submitted = self._mark_submitted and item.submitted is True
        summary = f"[{item.course_name}] {item.title}"
        if submitted:
            summary = f"✅ {summary}"

        due_local = item.due_at.astimezone(self.timezone)
        description_lines = [
            "Synced by Attendr.",
            f"Course: {item.course_name}",
            f"Type: {item.kind.replace('_', ' ').title()}",
            (
                f"Stated date: {due_local.date().isoformat()} (time not specified)"
                if item.all_day else f"Due: {due_local.strftime('%Y-%m-%d %H:%M %Z')}"
            ),
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
            "attendr_source": self._source_tag,
            "canvas_uid": item.uid,
            "canvas_source": item.source,
            "canvas_source_id": item.source_id,
            "canvas_course_id": str(item.course_id),
            "canvas_submission_state": item.submission_state or "unknown",
        }
        body: dict[str, Any] = {
            "summary": summary,
            "description": "\n".join(description_lines),
            "transparency": "opaque" if self._source_tag == "study_plan" or item.kind in {"lecture", "lab", "tutorial", "class", "exam"} else "transparent",
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
