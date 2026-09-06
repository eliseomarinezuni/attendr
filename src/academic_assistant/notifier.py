"""Discord webhook notifications with rate limiting and persistent deduplication."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

from .canvas_client import AcademicItem, Announcement, CanvasSnapshot

UTC = timezone.utc
DISCORD_BLUE = 0x5865F2
ASSIGNMENT_GREEN = 0x57F287
EXAM_RED = 0xED4245
QUIZ_GOLD = 0xFEE75C
ANNOUNCEMENT_PURPLE = 0x9B59B6
DIGEST_BLUE = 0x3498DB


class DiscordConfigurationError(ValueError):
    """Raised when Discord notification configuration is invalid."""


class DiscordNotificationError(RuntimeError):
    """Raised when Discord does not accept a notification."""


class DiscordRateLimitError(DiscordNotificationError):
    """Raised when a rate limit cannot be retried safely in this run."""


@dataclass(frozen=True, slots=True)
class NotificationReport:
    assignments_sent: int
    assignments_skipped: int
    announcements_sent: int
    announcements_skipped: int
    digest_sent: bool


class _TextExtractor(HTMLParser):
    """Convert untrusted Canvas HTML into compact plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in {"p", "div", "li", "tr"}:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self._parts).split())


class NotificationState:
    """Track successfully sent notifications without storing message contents."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()

    def was_sent(self, event_key: str, fingerprint: str) -> bool:
        self._initialize()
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT fingerprint FROM sent_notifications WHERE event_key = ?",
                (event_key,),
            ).fetchone()
        return row is not None and row[0] == fingerprint

    def mark_sent(self, event_key: str, fingerprint: str) -> None:
        self._initialize()
        sent_at = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO sent_notifications (event_key, fingerprint, sent_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    sent_at = excluded.sent_at
                """,
                (event_key, fingerprint, sent_at),
            )

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_notifications (
                    event_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                )
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            # Some non-POSIX filesystems do not support chmod.
            pass


class NotificationStateStore(Protocol):
    """Storage contract used by the notifier's idempotency flow."""

    def was_sent(self, event_key: str, fingerprint: str) -> bool: ...

    def mark_sent(self, event_key: str, fingerprint: str) -> None: ...


class JsonNotificationState:
    """Git-friendly notification state with atomic updates."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()

    def was_sent(self, event_key: str, fingerprint: str) -> bool:
        record = self._load()["notifications"].get(event_key)
        return isinstance(record, dict) and record.get("fingerprint") == fingerprint

    def mark_sent(self, event_key: str, fingerprint: str) -> None:
        state = self._load()
        state["notifications"][event_key] = {
            "fingerprint": fingerprint,
            "sent_at": datetime.now(UTC).isoformat(),
        }
        self._save(state)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "notifications": {}}
        try:
            decoded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DiscordConfigurationError(
                f"Notification state is unreadable: {self.path}"
            ) from error
        if (
            not isinstance(decoded, dict)
            or decoded.get("version") != 1
            or not isinstance(decoded.get("notifications"), dict)
        ):
            raise DiscordConfigurationError(
                f"Notification state has an unsupported format: {self.path}"
            )
        return decoded

    def _save(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent, text=True
            )
            temporary_path = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, indent=2, sort_keys=True)
                state_file.write("\n")
            temporary_path.chmod(0o600)
            temporary_path.replace(self.path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise DiscordConfigurationError(
                f"Notification state could not be saved: {self.path}"
            ) from error


class DiscordNotifier:
    """Format and route academic notifications to dedicated Discord channels."""

    def __init__(
        self,
        webhook_url: str,
        *,
        bot_token: str | None = None,
        channel_ids: Mapping[str, str] | None = None,
        state_path: str | os.PathLike[str] = "data/notification_state.db",
        app_timezone: str = "America/Toronto",
        timeout_seconds: float = 15.0,
        max_attempts: int = 4,
        max_rate_limit_wait_seconds: float = 60.0,
        session: requests.Session | Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._webhook_url = self._validate_webhook_url(webhook_url)
        self._bot_token = (bot_token or "").strip()
        self._channel_ids = {
            str(name): self._validate_channel_id(value)
            for name, value in (channel_ids or {}).items()
            if str(value).strip()
        }
        if timeout_seconds <= 0:
            raise DiscordConfigurationError(
                "Discord timeout must be greater than zero."
            )
        if max_attempts < 1:
            raise DiscordConfigurationError(
                "Discord max attempts must be at least one."
            )
        if max_rate_limit_wait_seconds < 0:
            raise DiscordConfigurationError(
                "Discord maximum rate-limit wait cannot be negative."
            )
        try:
            self.timezone = ZoneInfo(app_timezone)
        except ZoneInfoNotFoundError as error:
            raise DiscordConfigurationError(
                f"APP_TIMEZONE is not a valid IANA timezone: {app_timezone}"
            ) from error

        self.state: NotificationStateStore = (
            JsonNotificationState(state_path)
            if Path(state_path).suffix.casefold() == ".json"
            else NotificationState(state_path)
        )
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_rate_limit_wait_seconds = max_rate_limit_wait_seconds
        self._session = session or requests.Session()
        self._sleep = sleep
        self._now_provider = now_provider or (lambda: datetime.now(UTC))

    @classmethod
    def from_env(
        cls, env_file: str | os.PathLike[str] | None = None
    ) -> DiscordNotifier:
        """Create a notifier from environment variables."""
        load_dotenv(dotenv_path=env_file, override=False)
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        base_directory = Path(env_file).resolve().parent if env_file else Path.cwd()
        configured_state = (
            os.getenv("NOTIFICATION_STATE_FILE")
            or os.getenv("NOTIFICATION_STATE_DB")
            or "data/notification_state.db"
        )
        raw_state_path = Path(configured_state).expanduser()
        state_path = (
            raw_state_path
            if raw_state_path.is_absolute()
            else base_directory / raw_state_path
        )
        return cls(
            webhook_url,
            bot_token=os.getenv("DISCORD_BOT_TOKEN") or None,
            channel_ids={
                "announcements": os.getenv("DISCORD_ANNOUNCEMENTS_CHANNEL_ID", ""),
                "calendar_updates": os.getenv("DISCORD_CALENDAR_CHANNEL_ID", ""),
                "lecture_quizzes": os.getenv("DISCORD_QUIZ_CHANNEL_ID", ""),
            },
            state_path=state_path,
            app_timezone=os.getenv("APP_TIMEZONE", "America/Toronto"),
            timeout_seconds=cls._read_positive_float("DISCORD_TIMEOUT_SECONDS", 15.0),
            max_attempts=cls._read_positive_int("DISCORD_MAX_ATTEMPTS", 4),
            max_rate_limit_wait_seconds=cls._read_nonnegative_float(
                "DISCORD_MAX_RATE_LIMIT_WAIT_SECONDS", 60.0
            ),
        )

    def send_assignment_alert(self, item: AcademicItem, *, force: bool = False) -> bool:
        """Send one assignment/exam/quiz alert; return False when already sent."""
        payload = self.assignment_payload(item)
        return self._send_once(
            f"assignment:{item.uid}", payload, force=force,
            destination="announcements",
        )

    def send_announcement_alert(
        self, announcement: Announcement, *, force: bool = False
    ) -> bool:
        """Send one announcement alert; return False when already sent."""
        payload = self.announcement_payload(announcement)
        return self._send_once(
            f"announcement:{announcement.uid}", payload, force=force,
            destination="announcements",
        )

    def send_custom_notification(
        self,
        event_key: str,
        payload: Mapping[str, Any],
        *,
        force: bool = False,
        destination: str = "announcements",
    ) -> bool:
        """Send an application-defined payload using the existing safe webhook flow."""
        event_key = event_key.strip()
        if not event_key:
            raise ValueError("Custom notification event_key cannot be empty.")
        return self._send_once(
            f"custom:{event_key}", payload, force=force, destination=destination
        )

    def send_daily_digest(
        self,
        items: Sequence[AcademicItem],
        *,
        hours: int = 72,
        force: bool = False,
    ) -> bool:
        """Send at most one deadline digest per local date."""
        if hours < 1:
            raise ValueError("Digest hours must be at least one.")
        now = self._now_utc()
        payload = self.daily_digest_payload(items, hours=hours, now=now)
        local_date = now.astimezone(self.timezone).date().isoformat()
        event_key = f"digest:{local_date}:{hours}h"
        return self._send_once(
            event_key,
            payload,
            force=force,
            fixed_fingerprint="daily",
            destination="announcements",
        )

    def notify_snapshot(
        self,
        snapshot: CanvasSnapshot,
        *,
        alert_hours: int = 72,
        digest_hours: int = 72,
        send_digest: bool = True,
    ) -> NotificationReport:
        """Notify unseen near-term deadlines and unread announcements."""
        if alert_hours < 1:
            raise ValueError("Alert hours must be at least one.")
        now = self._now_utc()
        deadline = now + timedelta(hours=alert_hours)
        eligible_items = [
            item for item in snapshot.items if now <= item.due_at <= deadline
        ]

        assignments_sent = 0
        assignments_skipped = 0
        for item in eligible_items:
            if self.send_assignment_alert(item):
                assignments_sent += 1
            else:
                assignments_skipped += 1

        announcements_sent = 0
        announcements_skipped = 0
        for announcement in snapshot.announcements:
            if self.send_announcement_alert(announcement):
                announcements_sent += 1
            else:
                announcements_skipped += 1

        digest_sent = (
            self.send_daily_digest(snapshot.items, hours=digest_hours)
            if send_digest
            else False
        )
        return NotificationReport(
            assignments_sent=assignments_sent,
            assignments_skipped=assignments_skipped,
            announcements_sent=announcements_sent,
            announcements_skipped=announcements_skipped,
            digest_sent=digest_sent,
        )

    def assignment_payload(self, item: AcademicItem) -> dict[str, Any]:
        """Build a Discord-safe rich embed for one academic deadline."""
        kind_label = {
            "exam": "Exam",
            "quiz": "Quiz",
            "calendar_event": "Course Event",
        }.get(item.kind, "Assignment")
        due = item.due_at_local.strftime("%A, %B %d at %I:%M %p %Z")
        points = (
            self._format_points(item.points_possible)
            if item.points_possible is not None
            else "Not specified"
        )
        embed: dict[str, Any] = {
            "title": self._truncate(item.title, 256),
            "description": f"**Upcoming {kind_label}**",
            "color": self._item_color(item.kind),
            "fields": [
                {
                    "name": "Course",
                    "value": self._truncate(item.course_name, 1024),
                    "inline": False,
                },
                {"name": "Due", "value": due, "inline": True},
                {"name": "Points", "value": points, "inline": True},
            ],
            "timestamp": item.due_at.astimezone(UTC).isoformat(),
            "footer": {"text": "Academic Assistant • Canvas"},
        }
        safe_url = self._safe_link(item.html_url)
        if safe_url:
            embed["url"] = safe_url
        return self._payload(embed)

    def announcement_payload(self, announcement: Announcement) -> dict[str, Any]:
        """Build a Discord-safe announcement embed with sanitized body text."""
        body = self.strip_html(announcement.message_html)
        if not body:
            body = announcement.message_text.strip() or "No announcement body provided."
        author = announcement.author_name or "Instructor"
        posted = announcement.posted_at_local.strftime("%B %d, %Y at %I:%M %p %Z")
        embed: dict[str, Any] = {
            "title": self._truncate(announcement.title, 256),
            "description": self._truncate(body, 1000),
            "color": ANNOUNCEMENT_PURPLE,
            "fields": [
                {
                    "name": "Course",
                    "value": self._truncate(announcement.course_name, 1024),
                    "inline": False,
                },
                {
                    "name": "Posted by",
                    "value": self._truncate(author, 1024),
                    "inline": True,
                },
                {"name": "Posted", "value": posted, "inline": True},
            ],
            "timestamp": announcement.posted_at.astimezone(UTC).isoformat(),
            "footer": {"text": "Academic Assistant • Canvas announcement"},
        }
        safe_url = self._safe_link(announcement.html_url)
        if safe_url:
            embed["url"] = safe_url
        return self._payload(embed)

    def daily_digest_payload(
        self,
        items: Sequence[AcademicItem],
        *,
        hours: int = 72,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Build one chronological digest for the requested future window."""
        if now is not None and now.tzinfo is None:
            raise ValueError("Digest now value must be timezone-aware.")
        current = now.astimezone(UTC) if now else self._now_utc()
        deadline = current + timedelta(hours=hours)
        pending = sorted(
            (item for item in items if current <= item.due_at <= deadline),
            key=lambda item: (item.due_at, item.course_name, item.title),
        )
        if pending:
            lines = [self._digest_line(item) for item in pending]
            description = self._fit_lines(lines, 3800)
        else:
            description = f"No Canvas deadlines in the next {hours} hours."

        embed = {
            "title": f"Deadline digest • next {hours} hours",
            "description": description,
            "color": DIGEST_BLUE,
            "footer": {
                "text": f"Generated {current.astimezone(self.timezone).strftime('%b %d, %Y %I:%M %p %Z')}"
            },
        }
        return self._payload(embed)

    def _send_once(
        self,
        event_key: str,
        payload: Mapping[str, Any],
        *,
        force: bool,
        fixed_fingerprint: str | None = None,
        destination: str = "announcements",
    ) -> bool:
        fingerprint = fixed_fingerprint or self._fingerprint(payload)
        routed_key = f"{destination}:{event_key}"
        if not force and self.state.was_sent(routed_key, fingerprint):
            return False
        self._post(payload, destination)
        # Forced test sends remain repeatable and do not consume the real alert state.
        if not force:
            self.state.mark_sent(routed_key, fingerprint)
        return True

    def _post(self, payload: Mapping[str, Any], destination: str) -> None:
        channel_id = self._channel_ids.get(destination)
        use_bot = bool(self._bot_token and channel_id)
        url = (
            f"https://discord.com/api/v10/channels/{channel_id}/messages"
            if use_bot
            else self._webhook_url
        )
        outgoing = dict(payload)
        if use_bot:
            outgoing.pop("username", None)
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._session.post(
                    url,
                    params=None if use_bot else {"wait": "true"},
                    headers=(
                        {"Authorization": f"Bot {self._bot_token}"}
                        if use_bot
                        else None
                    ),
                    json=outgoing,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException:
                if attempt == self.max_attempts:
                    raise DiscordNotificationError(
                        "Discord could not be reached after repeated attempts."
                    ) from None
                self._sleep(float(2 ** (attempt - 1)))
                continue

            if 200 <= response.status_code < 300:
                return
            if response.status_code == 429:
                retry_after = self._retry_after_seconds(response)
                if (
                    attempt == self.max_attempts
                    or retry_after > self.max_rate_limit_wait_seconds
                ):
                    raise DiscordRateLimitError(
                        "Discord rate-limited the webhook; the notification remains unsent "
                        "and will be retried on the next run."
                    )
                self._sleep(retry_after)
                continue
            if 500 <= response.status_code < 600 and attempt < self.max_attempts:
                self._sleep(float(2 ** (attempt - 1)))
                continue
            raise DiscordNotificationError(
                f"Discord rejected the webhook request with HTTP {response.status_code}. "
                "Check that Attendr can still post in the configured channel."
            )

        raise DiscordNotificationError("Discord notification failed unexpectedly.")

    @staticmethod
    def strip_html(value: str) -> str:
        extractor = _TextExtractor()
        extractor.feed(value or "")
        extractor.close()
        return extractor.text()

    @staticmethod
    def _payload(embed: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "username": "Academic Assistant",
            "embeds": [dict(embed)],
            "allowed_mentions": {"parse": []},
        }

    @staticmethod
    def _validate_webhook_url(value: str) -> str:
        value = value.strip()
        if not value:
            raise DiscordConfigurationError("DISCORD_WEBHOOK_URL is missing or empty.")
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        trusted_host = hostname in {
            "discord.com",
            "discordapp.com",
        } or hostname.endswith((".discord.com", ".discordapp.com"))
        path_parts = [part for part in parsed.path.split("/") if part]
        valid_path = len(path_parts) == 4 and path_parts[:2] == ["api", "webhooks"]
        if (
            parsed.scheme != "https"
            or not trusted_host
            or not valid_path
            or parsed.query
            or parsed.fragment
        ):
            raise DiscordConfigurationError(
                "DISCORD_WEBHOOK_URL must be the complete HTTPS URL copied from Discord."
            )
        return value

    @staticmethod
    def _validate_channel_id(value: str) -> str:
        channel_id = str(value).strip()
        if not channel_id.isdigit():
            raise DiscordConfigurationError(
                "Discord channel IDs must contain digits only."
            )
        return channel_id

    @staticmethod
    def _safe_link(value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlparse(value)
        return value if parsed.scheme in {"http", "https"} and parsed.netloc else None

    @staticmethod
    def _fingerprint(payload: Mapping[str, Any]) -> str:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _retry_after_seconds(response: Any) -> float:
        value: Any = None
        try:
            body = response.json()
            if isinstance(body, dict):
                value = body.get("retry_after")
        except (ValueError, TypeError):
            pass
        if value is None:
            value = response.headers.get("Retry-After", 1.0)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 1.0

    def _now_utc(self) -> datetime:
        current = self._now_provider()
        if current.tzinfo is None:
            raise DiscordConfigurationError(
                "now_provider must return a timezone-aware datetime."
            )
        return current.astimezone(UTC)

    @staticmethod
    def _item_color(kind: str) -> int:
        return {
            "exam": EXAM_RED,
            "quiz": QUIZ_GOLD,
            "assignment": ASSIGNMENT_GREEN,
        }.get(kind, DISCORD_BLUE)

    @staticmethod
    def _format_points(value: float) -> str:
        numeric_value = float(value)
        return (
            str(int(numeric_value))
            if numeric_value.is_integer()
            else f"{numeric_value:g}"
        )

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        value = value.strip()
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 1)].rstrip() + "…"

    def _digest_line(self, item: AcademicItem) -> str:
        due = item.due_at_local.strftime("%a %b %d, %I:%M %p")
        label = item.kind.replace("_", " ").title()
        title = self._truncate(item.title, 140)
        course = self._truncate(item.course_name, 100)
        safe_url = self._safe_link(item.html_url)
        linked_title = f"[{title}]({safe_url})" if safe_url else title
        return f"• **{due}** — {linked_title}\n  {course} · {label}"

    @staticmethod
    def _fit_lines(lines: Iterable[str], limit: int) -> str:
        selected: list[str] = []
        current_length = 0
        omitted = 0
        for line in lines:
            added = len(line) + (1 if selected else 0)
            if current_length + added > limit - 40:
                omitted += 1
                continue
            selected.append(line)
            current_length += added
        result = "\n".join(selected)
        if omitted:
            result += f"\n…and {omitted} more deadline{'s' if omitted != 1 else ''}."
        return result

    @staticmethod
    def _read_positive_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw)
        except ValueError as error:
            raise DiscordConfigurationError(
                f"{name} must be a positive integer."
            ) from error
        if value < 1:
            raise DiscordConfigurationError(f"{name} must be a positive integer.")
        return value

    @staticmethod
    def _read_positive_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = float(raw)
        except ValueError as error:
            raise DiscordConfigurationError(
                f"{name} must be greater than zero."
            ) from error
        if value <= 0:
            raise DiscordConfigurationError(f"{name} must be greater than zero.")
        return value

    @staticmethod
    def _read_nonnegative_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = float(raw)
        except ValueError as error:
            raise DiscordConfigurationError(f"{name} cannot be negative.") from error
        if value < 0:
            raise DiscordConfigurationError(f"{name} cannot be negative.")
        return value
