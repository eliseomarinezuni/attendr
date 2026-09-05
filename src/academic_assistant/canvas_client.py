"""Read-only Canvas LMS client for courses, deadlines, and announcements."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from canvasapi import Canvas
from canvasapi.exceptions import CanvasException, InvalidAccessToken
from dotenv import load_dotenv
from requests import RequestException

UTC = timezone.utc
EXAM_PATTERN = re.compile(r"\b(exam|midterm|final|test)\b", re.IGNORECASE)
QUIZ_PATTERN = re.compile(r"\bquiz\b", re.IGNORECASE)
SYLLABUS_FILE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(syllabus|course[\s_-]*outline|course[\s_-]*schedule|"
    r"course[\s_-]*plan)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
CANVAS_FILE_ID_PATTERN = re.compile(r"/files/(\d+)")


class CanvasConfigurationError(ValueError):
    """Raised when local Canvas configuration is missing or invalid."""


class CanvasAuthenticationError(RuntimeError):
    """Raised when Canvas rejects the configured credentials."""


class CanvasAPIError(RuntimeError):
    """Raised when a required Canvas request cannot be completed."""


@dataclass(frozen=True, slots=True)
class CourseSummary:
    id: int
    name: str
    course_code: str | None
    html_url: str | None


@dataclass(frozen=True, slots=True)
class AcademicItem:
    uid: str
    source: str
    source_id: str
    course_id: int
    course_name: str
    title: str
    kind: str
    due_at: datetime
    due_at_local: datetime
    end_at: datetime | None
    all_day: bool
    html_url: str | None
    updated_at: datetime | None
    points_possible: float | None
    submission_types: tuple[str, ...]
    description_html: str | None
    submitted: bool | None = None
    submission_state: str | None = None


@dataclass(frozen=True, slots=True)
class Announcement:
    uid: str
    source_id: str
    course_id: int
    course_name: str
    title: str
    message_html: str
    message_text: str
    posted_at: datetime
    posted_at_local: datetime
    html_url: str | None
    author_name: str | None
    read_state: str


@dataclass(frozen=True, slots=True)
class CanvasSnapshot:
    user_id: str
    user_name: str
    courses: tuple[CourseSummary, ...]
    items: tuple[AcademicItem, ...]
    announcements: tuple[Announcement, ...]
    warnings: tuple[str, ...]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SyllabusMaterial:
    uid: str
    source_id: str
    course_id: int
    course_name: str
    title: str
    content_type: str
    local_path: Path
    content_sha256: str
    updated_at: datetime | None
    html_url: str | None


@dataclass(frozen=True, slots=True)
class MaterialDownloadReport:
    materials: tuple[SyllabusMaterial, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CourseContext:
    summary: CourseSummary
    resource: Any


class _PlainTextHTMLParser(HTMLParser):
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


class CanvasClient:
    """Fetch normalized, read-only academic data from one Canvas account."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        lookahead_days: int = 30,
        announcement_days: int = 14,
        app_timezone: str = "America/Toronto",
        canvas: Any | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = self._validate_base_url(base_url)
        if not api_token or not api_token.strip():
            raise CanvasConfigurationError("CANVAS_API_TOKEN is missing or empty.")
        if lookahead_days < 1:
            raise CanvasConfigurationError("CANVAS_LOOKAHEAD_DAYS must be at least 1.")
        if announcement_days < 1:
            raise CanvasConfigurationError(
                "CANVAS_ANNOUNCEMENT_DAYS must be at least 1."
            )

        try:
            self.timezone = ZoneInfo(app_timezone)
        except ZoneInfoNotFoundError as error:
            raise CanvasConfigurationError(
                f"APP_TIMEZONE is not a valid IANA timezone: {app_timezone}"
            ) from error

        self.lookahead_days = lookahead_days
        self.announcement_days = announcement_days
        self._canvas = (
            canvas if canvas is not None else Canvas(self.base_url, api_token.strip())
        )
        self._now_provider = now_provider or (lambda: datetime.now(UTC))

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = None) -> CanvasClient:
        """Create a client from environment variables, optionally loading a .env file."""
        load_dotenv(dotenv_path=env_file, override=False)
        base_url = os.getenv("CANVAS_BASE_URL", "")
        api_token = os.getenv("CANVAS_API_TOKEN", "")
        lookahead_days = cls._read_positive_int("CANVAS_LOOKAHEAD_DAYS", 30)
        announcement_days = cls._read_positive_int("CANVAS_ANNOUNCEMENT_DAYS", 14)
        app_timezone = os.getenv("APP_TIMEZONE", "America/Toronto")
        return cls(
            base_url,
            api_token,
            lookahead_days=lookahead_days,
            announcement_days=announcement_days,
            app_timezone=app_timezone,
        )

    def validate_credentials(self) -> tuple[str, str]:
        """Return the authenticated user's ID and display name."""
        try:
            user = self._canvas.get_current_user()
        except InvalidAccessToken as error:
            raise CanvasAuthenticationError(
                "Canvas rejected the API token. Generate a new token in Account > Settings."
            ) from error
        except (CanvasException, RequestException) as error:
            if self._is_auth_error(error):
                raise CanvasAuthenticationError(
                    "Canvas authentication failed. Check CANVAS_BASE_URL and CANVAS_API_TOKEN."
                ) from error
            raise CanvasAPIError(
                "Could not connect to Canvas while validating credentials."
            ) from error

        user_id = str(self._attr(user, "id", "unknown"))
        user_name = str(
            self._attr(user, "name", None)
            or self._attr(user, "short_name", None)
            or "Unknown Canvas user"
        )
        return user_id, user_name

    def get_active_courses(self) -> tuple[CourseSummary, ...]:
        """Return normalized active student courses."""
        return tuple(context.summary for context in self._get_active_course_contexts())

    def get_upcoming_items(self) -> tuple[AcademicItem, ...]:
        """Return upcoming assignments and course calendar events."""
        contexts = self._get_active_course_contexts()
        warnings: list[str] = []
        return self._fetch_upcoming_items(contexts, warnings)

    def get_recent_unread_announcements(self) -> tuple[Announcement, ...]:
        """Return recent announcements that Canvas reports as unread."""
        contexts = self._get_active_course_contexts()
        warnings: list[str] = []
        return self._fetch_announcements(contexts, warnings)

    def fetch_snapshot(self) -> CanvasSnapshot:
        """Fetch a consistent snapshot and retain non-fatal per-course warnings."""
        user_id, user_name = self.validate_credentials()
        contexts = self._get_active_course_contexts()
        warnings: list[str] = []
        items = self._fetch_upcoming_items(contexts, warnings)
        announcements = self._fetch_announcements(contexts, warnings)
        return CanvasSnapshot(
            user_id=user_id,
            user_name=user_name,
            courses=tuple(context.summary for context in contexts),
            items=items,
            announcements=announcements,
            warnings=tuple(warnings),
            fetched_at=self._now_utc(),
        )

    def download_syllabus_materials(
        self,
        directory: str | os.PathLike[str],
        *,
        max_file_bytes: int = 25 * 1024 * 1024,
    ) -> MaterialDownloadReport:
        """Download accessible syllabus pages and syllabus-like PDFs read-only."""
        if max_file_bytes < 1:
            raise CanvasConfigurationError(
                "Canvas material size limit must be positive."
            )
        destination = Path(directory).expanduser().resolve()
        contexts = self._get_active_course_contexts()
        materials: list[SyllabusMaterial] = []
        warnings: list[str] = []

        for context in contexts:
            course = context.summary
            course_directory = destination / f"course-{course.id}"
            syllabus_html = str(self._attr(context.resource, "syllabus_body", "") or "")
            linked_file_ids = set(CANVAS_FILE_ID_PATTERN.findall(syllabus_html))

            if self._html_to_text(syllabus_html):
                body = syllabus_html.encode("utf-8")
                path = course_directory / "canvas-syllabus.html"
                self._atomic_write(path, body)
                materials.append(
                    SyllabusMaterial(
                        uid=f"canvas:syllabus-page:{course.id}",
                        source_id="syllabus-page",
                        course_id=course.id,
                        course_name=course.name,
                        title="Canvas syllabus page",
                        content_type="text/html",
                        local_path=path,
                        content_sha256=sha256(body).hexdigest(),
                        updated_at=None,
                        html_url=(
                            f"{self.base_url}/courses/{course.id}/assignments/syllabus"
                        ),
                    )
                )

            try:
                files = context.resource.get_files(
                    content_types=["application/pdf"],
                    sort="updated_at",
                    order="desc",
                )
                for file_resource in files:
                    source_id = str(self._attr(file_resource, "id", ""))
                    display_name = str(
                        self._attr(file_resource, "display_name", None)
                        or self._attr(file_resource, "filename", None)
                        or f"Canvas file {source_id}"
                    )
                    content_type = str(
                        self._attr(file_resource, "content-type", None)
                        or self._attr(file_resource, "content_type", None)
                        or ""
                    ).casefold()
                    is_pdf = (
                        content_type == "application/pdf"
                        or display_name.casefold().endswith(".pdf")
                    )
                    is_candidate = (
                        bool(SYLLABUS_FILE_PATTERN.search(display_name))
                        or source_id in linked_file_ids
                    )
                    if not is_pdf or not is_candidate:
                        continue
                    if bool(
                        self._attr(file_resource, "hidden_for_user", False)
                    ) or bool(self._attr(file_resource, "locked", False)):
                        continue
                    declared_size = int(self._attr(file_resource, "size", 0) or 0)
                    if declared_size > max_file_bytes:
                        warnings.append(
                            f"Skipped oversized syllabus PDF in {course.name}: "
                            f"{display_name}."
                        )
                        continue
                    try:
                        content = file_resource.get_contents(binary=True)
                    except (CanvasException, RequestException):
                        warnings.append(
                            f"Could not download syllabus PDF in {course.name}: "
                            f"{display_name}."
                        )
                        continue
                    if not isinstance(content, bytes) or len(content) > max_file_bytes:
                        warnings.append(
                            f"Skipped invalid or oversized syllabus PDF in {course.name}: "
                            f"{display_name}."
                        )
                        continue
                    if not content.lstrip().startswith(b"%PDF-"):
                        warnings.append(
                            f"Skipped non-PDF syllabus content in {course.name}: "
                            f"{display_name}."
                        )
                        continue

                    safe_name = self._safe_filename(
                        display_name, fallback="syllabus.pdf"
                    )
                    path = course_directory / f"{source_id}-{safe_name}"
                    self._atomic_write(path, content)
                    materials.append(
                        SyllabusMaterial(
                            uid=f"canvas:syllabus-file:{course.id}:{source_id}",
                            source_id=source_id,
                            course_id=course.id,
                            course_name=course.name,
                            title=display_name,
                            content_type="application/pdf",
                            local_path=path,
                            content_sha256=sha256(content).hexdigest(),
                            updated_at=self._parse_datetime(
                                self._attr(file_resource, "updated_at", None),
                                required=False,
                            ),
                            html_url=(
                                f"{self.base_url}/courses/{course.id}/files/{source_id}"
                            ),
                        )
                    )
            except (CanvasException, RequestException) as error:
                warnings.append(self._course_warning("syllabus files", course, error))

        unique = {material.uid: material for material in materials}
        return MaterialDownloadReport(
            materials=tuple(sorted(unique.values(), key=lambda item: item.uid)),
            warnings=tuple(warnings),
        )

    def _get_active_course_contexts(self) -> tuple[_CourseContext, ...]:
        try:
            resources = self._canvas.get_courses(
                enrollment_state="active",
                enrollment_type="student",
                include=["term", "syllabus_body"],
            )
            contexts = []
            for course in resources:
                course_id = int(self._attr(course, "id"))
                name = str(
                    self._attr(course, "name", None)
                    or self._attr(course, "course_code", None)
                    or f"Course {course_id}"
                )
                contexts.append(
                    _CourseContext(
                        summary=CourseSummary(
                            id=course_id,
                            name=name,
                            course_code=self._optional_str(
                                self._attr(course, "course_code", None)
                            ),
                            html_url=self._optional_str(
                                self._attr(course, "html_url", None)
                            ),
                        ),
                        resource=course,
                    )
                )
            return tuple(
                sorted(contexts, key=lambda item: item.summary.name.casefold())
            )
        except (CanvasException, RequestException) as error:
            if self._is_auth_error(error):
                raise CanvasAuthenticationError(
                    "Canvas could not list courses. Check the token and Canvas domain."
                ) from error
            raise CanvasAPIError("Could not retrieve active Canvas courses.") from error

    def _fetch_upcoming_items(
        self, contexts: Iterable[_CourseContext], warnings: list[str]
    ) -> tuple[AcademicItem, ...]:
        start = self._now_utc()
        end = start + timedelta(days=self.lookahead_days)
        items: list[AcademicItem] = []

        for context in contexts:
            course = context.summary
            try:
                assignments = context.resource.get_assignments(
                    bucket="upcoming",
                    order_by="due_at",
                    override_assignment_dates=True,
                    include=["submission"],
                )
                for assignment in assignments:
                    try:
                        item = self._assignment_to_item(assignment, course)
                        if start <= item.due_at <= end:
                            items.append(item)
                    except (TypeError, ValueError) as error:
                        warnings.append(
                            f"Skipped malformed assignment in {course.name}: {error}"
                        )
            except (CanvasException, RequestException) as error:
                warnings.append(self._course_warning("assignments", course, error))

            try:
                events = self._canvas.get_calendar_events(
                    type="event",
                    context_codes=[f"course_{course.id}"],
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                )
                for event in events:
                    try:
                        item = self._calendar_event_to_item(event, course)
                        if start <= item.due_at <= end:
                            items.append(item)
                    except (TypeError, ValueError) as error:
                        warnings.append(
                            f"Skipped malformed calendar event in {course.name}: {error}"
                        )
            except (CanvasException, RequestException) as error:
                warnings.append(self._course_warning("calendar events", course, error))

        unique = {item.uid: item for item in items}
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (item.due_at, item.course_name, item.title),
            )
        )

    def _fetch_announcements(
        self, contexts: Iterable[_CourseContext], warnings: list[str]
    ) -> tuple[Announcement, ...]:
        now = self._now_utc()
        cutoff = now - timedelta(days=self.announcement_days)
        announcements: list[Announcement] = []

        for context in contexts:
            course = context.summary
            try:
                topics = context.resource.get_discussion_topics(
                    only_announcements=True,
                    filter_by="unread",
                    order_by="recent_activity",
                )
                for topic in topics:
                    try:
                        announcement = self._topic_to_announcement(topic, course)
                        if (
                            cutoff <= announcement.posted_at <= now
                            and announcement.read_state == "unread"
                        ):
                            announcements.append(announcement)
                    except (TypeError, ValueError) as error:
                        warnings.append(
                            f"Skipped malformed announcement in {course.name}: {error}"
                        )
            except (CanvasException, RequestException) as error:
                warnings.append(self._course_warning("announcements", course, error))

        unique = {announcement.uid: announcement for announcement in announcements}
        return tuple(
            sorted(unique.values(), key=lambda item: item.posted_at, reverse=True)
        )

    def _assignment_to_item(
        self, assignment: Any, course: CourseSummary
    ) -> AcademicItem:
        source_id = str(self._attr(assignment, "id"))
        title = str(self._attr(assignment, "name", "Untitled assignment"))
        due_at = self._parse_datetime(self._attr(assignment, "due_at", None))
        if due_at is None:
            raise ValueError(f"assignment {source_id} has no due date")
        submission_types = tuple(self._attr(assignment, "submission_types", ()) or ())
        submission = self._attr(assignment, "submission", None)
        submission_state = self._optional_str(
            self._attr(submission, "workflow_state", None)
        )
        submitted_at = self._attr(submission, "submitted_at", None)
        submitted = None
        if submission is not None:
            submitted = bool(submitted_at) or submission_state in {
                "submitted",
                "graded",
                "pending_review",
                "complete",
            }
        return AcademicItem(
            uid=f"canvas:assignment:{course.id}:{source_id}",
            source="assignment",
            source_id=source_id,
            course_id=course.id,
            course_name=course.name,
            title=title,
            kind=self._classify(title, submission_types, source="assignment"),
            due_at=due_at,
            due_at_local=due_at.astimezone(self.timezone),
            end_at=None,
            all_day=False,
            html_url=self._optional_str(self._attr(assignment, "html_url", None)),
            updated_at=self._parse_datetime(
                self._attr(assignment, "updated_at", None), required=False
            ),
            points_possible=self._optional_float(
                self._attr(assignment, "points_possible", None)
            ),
            submission_types=submission_types,
            description_html=self._optional_str(
                self._attr(assignment, "description", None)
            ),
            submitted=submitted,
            submission_state=submission_state,
        )

    def _calendar_event_to_item(
        self, event: Any, course: CourseSummary
    ) -> AcademicItem:
        source_id = str(self._attr(event, "id"))
        title = str(self._attr(event, "title", "Untitled calendar event"))
        all_day = bool(self._attr(event, "all_day", False))
        start_at = self._parse_datetime(
            self._attr(event, "start_at", None), required=False
        )
        if start_at is None:
            all_day_date = self._attr(event, "all_day_date", None)
            start_at = self._parse_all_day_date(all_day_date)
        if start_at is None:
            raise ValueError(f"calendar event {source_id} has no start date")
        end_at = self._parse_datetime(self._attr(event, "end_at", None), required=False)
        return AcademicItem(
            uid=f"canvas:event:{course.id}:{source_id}",
            source="calendar_event",
            source_id=source_id,
            course_id=course.id,
            course_name=course.name,
            title=title,
            kind=self._classify(title, (), source="calendar_event"),
            due_at=start_at,
            due_at_local=start_at.astimezone(self.timezone),
            end_at=end_at,
            all_day=all_day,
            html_url=self._optional_str(
                self._attr(event, "html_url", None) or self._attr(event, "url", None)
            ),
            updated_at=self._parse_datetime(
                self._attr(event, "updated_at", None), required=False
            ),
            points_possible=None,
            submission_types=(),
            description_html=self._optional_str(self._attr(event, "description", None)),
        )

    def _topic_to_announcement(self, topic: Any, course: CourseSummary) -> Announcement:
        source_id = str(self._attr(topic, "id"))
        posted_at = self._parse_datetime(self._attr(topic, "posted_at", None))
        if posted_at is None:
            raise ValueError(f"announcement {source_id} has no posted date")
        message_html = str(self._attr(topic, "message", "") or "")
        author = self._attr(topic, "author", None)
        author_name = None
        if isinstance(author, dict):
            author_name = self._optional_str(
                author.get("display_name") or author.get("name")
            )
        author_name = author_name or self._optional_str(
            self._attr(topic, "user_name", None)
        )
        return Announcement(
            uid=f"canvas:announcement:{course.id}:{source_id}",
            source_id=source_id,
            course_id=course.id,
            course_name=course.name,
            title=str(self._attr(topic, "title", "Untitled announcement")),
            message_html=message_html,
            message_text=self._html_to_text(message_html),
            posted_at=posted_at,
            posted_at_local=posted_at.astimezone(self.timezone),
            html_url=self._optional_str(self._attr(topic, "html_url", None)),
            author_name=author_name,
            read_state=str(self._attr(topic, "read_state", "unread")),
        )

    def _now_utc(self) -> datetime:
        current = self._now_provider()
        if current.tzinfo is None:
            raise CanvasConfigurationError(
                "now_provider must return a timezone-aware datetime."
            )
        return current.astimezone(UTC)

    @staticmethod
    def _validate_base_url(value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise CanvasConfigurationError("CANVAS_BASE_URL is missing or empty.")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CanvasConfigurationError(
                "CANVAS_BASE_URL must be a complete URL such as https://school.instructure.com."
            )
        if (
            parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise CanvasConfigurationError(
                "CANVAS_BASE_URL must be the Canvas origin only, without /api/v1 or course paths."
            )
        return value

    @staticmethod
    def _read_positive_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw)
        except ValueError as error:
            raise CanvasConfigurationError(
                f"{name} must be a positive integer."
            ) from error
        if value < 1:
            raise CanvasConfigurationError(f"{name} must be a positive integer.")
        return value

    @staticmethod
    def _attr(resource: Any, name: str, default: Any = None) -> Any:
        if isinstance(resource, dict):
            return resource.get(name, default)
        return getattr(resource, name, default)

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return float(value) if value is not None else None

    @staticmethod
    def _parse_datetime(value: Any, *, required: bool = True) -> datetime | None:
        if value in {None, ""}:
            if required:
                raise ValueError("missing datetime")
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"datetime has no timezone: {value}")
        return parsed.astimezone(UTC)

    def _parse_all_day_date(self, value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        parsed_date = (
            value if isinstance(value, date) else date.fromisoformat(str(value))
        )
        return datetime.combine(parsed_date, time.min, tzinfo=self.timezone).astimezone(
            UTC
        )

    @staticmethod
    def _classify(title: str, submission_types: Iterable[str], *, source: str) -> str:
        if EXAM_PATTERN.search(title):
            return "exam"
        if "online_quiz" in submission_types or QUIZ_PATTERN.search(title):
            return "quiz"
        return "calendar_event" if source == "calendar_event" else "assignment"

    @staticmethod
    def _html_to_text(value: str) -> str:
        parser = _PlainTextHTMLParser()
        parser.feed(value)
        parser.close()
        return parser.text()

    @staticmethod
    def _safe_filename(value: str, *, fallback: str) -> str:
        filename = Path(value.replace("\\", "/")).name
        filename = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")
        return filename[:180] or fallback

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent
            )
            temporary_path = Path(raw_path)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
            temporary_path.replace(path)
        except OSError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _is_auth_error(error: BaseException) -> bool:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None) or getattr(
            error, "status_code", None
        )
        return status_code in {401, 403}

    @staticmethod
    def _course_warning(
        resource_name: str, course: CourseSummary, error: BaseException
    ) -> str:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None) or getattr(
            error, "status_code", None
        )
        suffix = f" (HTTP {status_code})" if status_code else ""
        return f"Could not retrieve {resource_name} for {course.name}{suffix}."
