"""Read-only Canvas LMS client for courses, deadlines, and announcements."""

from __future__ import annotations

import os
import re
import tempfile
import io
import zipfile
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
from canvasapi.exceptions import CanvasException, InvalidAccessToken, Unauthorized
from dotenv import load_dotenv
from requests import RequestException
from .http_client import configure_canvas_transport
from .state_store import StateStore
from .lecture_files import SMALL_FILE_BYTES, LectureDownloadError, lecture_file_limit, stream_download
from .google_slides import authenticated_slide_text, SlidesAccessError

NUMBERED_LECTURE_PATTERN = re.compile(r"^\s*\d{1,2}[a-z]\s*[-–:]", re.IGNORECASE)
UTC = timezone.utc
EXAM_PATTERN = re.compile(r"\b(exam|midterm|final|test)\b", re.IGNORECASE)
QUIZ_PATTERN = re.compile(r"\bquiz\b", re.IGNORECASE)
SYLLABUS_FILE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(syllabus|course[\s_-]*outline|course[\s_-]*schedule|"
    r"course[\s_-]*plan)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
CANVAS_FILE_ID_PATTERN = re.compile(r"/files/(\d+)")
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
LECTURE_MATERIAL_PATTERN = re.compile(
    r"\b(lecture|lect|week|module|chapter|slides?|deck|lesson|topic|notes?)(?:\b|(?=\d|_))",
    re.IGNORECASE,
)
NON_LECTURE_MATERIAL_PATTERN = re.compile(
    r"\b(lab|laboratory|tutorial|workshop)\b", re.IGNORECASE
)
DATED_MATERIAL_PATTERN = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}\b|\b\d{4}[-_]\d{1,2}[-_]\d{1,2}\b|"
    r"\b\d{1,2}[-_]\d{1,2}\b",
    re.IGNORECASE,
)


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

    def falls_in_window(self, start: datetime, end: datetime) -> bool:
        if self.all_day:
            zone = self.due_at_local.tzinfo
            return (
                start.astimezone(zone).date()
                <= self.due_at_local.date()
                <= end.astimezone(zone).date()
            )
        return start <= self.due_at <= end


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
    complete: bool = True
    removed_uids: tuple[str, ...] = ()
    incomplete_course_ids: tuple[int, ...] = ()


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
    incomplete_course_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class LectureMaterial:
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
    module_name: str | None
    module_position: int | None
    item_position: int | None


@dataclass(frozen=True, slots=True)
class LectureMaterialDownloadReport:
    materials: tuple[LectureMaterial, ...]
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
        state_store: StateStore | None = None,
    ) -> None:
        self.state_store = state_store
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
        if canvas is None:
            configure_canvas_transport(self._canvas)
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
            state_store=StateStore((Path(env_file).resolve().parent if env_file else Path.cwd()) / os.getenv("ATTENDR_DB", "data/attendr.db")),
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
        return self._fetch_announcements(contexts, warnings, unread_only=True)

    def get_recent_announcements(self) -> tuple[Announcement, ...]:
        """Return recent announcements without changing their read state."""
        contexts = self._get_active_course_contexts()
        warnings: list[str] = []
        announcements = self._fetch_announcements(contexts, warnings, unread_only=False)
        if warnings:
            raise CanvasAPIError("Announcement dates are incomplete: " + "; ".join(warnings))
        return announcements

    def fetch_snapshot(self) -> CanvasSnapshot:
        """Fetch a consistent snapshot and retain non-fatal per-course warnings."""
        user_id, user_name = self.validate_credentials()
        contexts = self._get_active_course_contexts()
        warnings: list[str] = []
        incomplete_courses: set[int] = set()
        items = self._fetch_upcoming_items(contexts, warnings, incomplete_courses)
        removed_uids: list[str] = []
        if self.state_store:
            contexts_by_id = {context.summary.id: context for context in contexts}
            known = {item.uid: item for item in items}
            for tracked in self.state_store.tracked_assignments():
                context = contexts_by_id.get(tracked["course_id"])
                if context is None or tracked["uid"] in known:
                    continue
                try:
                    assignment = context.resource.get_assignment(tracked["assignment_id"], include=["submission"], override_assignment_dates=True)
                    if self._attr(assignment, "due_at", None) in (None, ""):
                        removed_uids.append(tracked["uid"])
                    else:
                        item = self._assignment_to_item(assignment, context.summary)
                        known[item.uid] = item
                except (CanvasException, RequestException, ValueError, TypeError):
                    # A 404 can also mask inaccessible data; preserve the prior event.
                    incomplete_courses.add(context.summary.id)
                    warnings.append(f"Could not reconcile tracked assignment in {context.summary.name}.")
            items = tuple(sorted(known.values(), key=lambda item: (item.due_at, item.uid)))
        announcements = self._fetch_announcements(
            contexts, warnings, unread_only=True, incomplete_courses=incomplete_courses
        )
        return CanvasSnapshot(
            user_id=user_id,
            user_name=user_name,
            courses=tuple(context.summary for context in contexts),
            items=items,
            announcements=announcements,
            warnings=tuple(warnings),
            fetched_at=self._now_utc(),
            complete=not incomplete_courses,
            removed_uids=tuple(removed_uids),
            incomplete_course_ids=tuple(sorted(incomplete_courses)),
        )

    def download_syllabus_materials(
        self,
        directory: str | os.PathLike[str],
        *,
        max_file_bytes: int = 25 * 1024 * 1024,
    ) -> MaterialDownloadReport:
        """Download accessible syllabus pages, PDFs, and Word documents read-only."""
        if max_file_bytes < 1:
            raise CanvasConfigurationError(
                "Canvas material size limit must be positive."
            )
        destination = Path(directory).expanduser().resolve()
        contexts = self._get_active_course_contexts()
        materials: list[SyllabusMaterial] = []
        warnings: list[str] = []
        incomplete_course_ids: set[int] = set()

        for context in contexts:
            course = context.summary
            course_directory = destination / f"course-{course.id}"
            syllabus_html = str(self._attr(context.resource, "syllabus_body", "") or "")
            linked_file_ids = set(CANVAS_FILE_ID_PATTERN.findall(syllabus_html))
            file_resources: dict[str, Any] = {}

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
                    content_types=["application/pdf", DOCX_CONTENT_TYPE],
                    sort="updated_at",
                    order="desc",
                )
                for file_resource in files:
                    source_id = str(self._attr(file_resource, "id", ""))
                    file_resources[source_id] = file_resource
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
                    is_supported = (
                        content_type in {"application/pdf", DOCX_CONTENT_TYPE}
                        or display_name.casefold().endswith((".pdf", ".docx"))
                    )
                    is_candidate = (
                        bool(SYLLABUS_FILE_PATTERN.search(display_name))
                        or source_id in linked_file_ids
                    )
                    if not is_supported or not is_candidate:
                        continue
                    material = self._syllabus_file_material(
                        file_resource,
                        course,
                        course_directory,
                        max_file_bytes,
                        warnings,
                        explicitly_linked=source_id in linked_file_ids,
                    )
                    if material:
                        materials.append(material)
            except (CanvasException, RequestException) as error:
                incomplete_course_ids.add(course.id)
                warnings.append(self._course_warning("syllabus files", course, error))

            # Canvas may omit files hidden from the Files tab even when a syllabus
            # page explicitly links them. Direct retrieval preserves that access.
            for source_id in linked_file_ids:
                if any(
                    material.course_id == course.id
                    and material.source_id == source_id
                    for material in materials
                ):
                    continue
                try:
                    file_resource = file_resources.get(source_id) or context.resource.get_file(
                        source_id
                    )
                    material = self._syllabus_file_material(
                        file_resource,
                        course,
                        course_directory,
                        max_file_bytes,
                        warnings,
                        explicitly_linked=True,
                    )
                    if material:
                        materials.append(material)
                except (CanvasException, RequestException, OSError, ValueError):
                    incomplete_course_ids.add(course.id)
                    warnings.append(
                        f"Could not download a syllabus-linked file in {course.name}."
                    )

            self._discover_syllabus_in_modules(
                context.resource,
                course,
                course_directory,
                file_resources,
                max_file_bytes,
                materials,
                warnings,
            )
            self._discover_syllabus_in_pages(
                context.resource,
                course,
                course_directory,
                file_resources,
                max_file_bytes,
                materials,
                warnings,
            )

        unique = {material.uid: material for material in materials}
        return MaterialDownloadReport(
            materials=tuple(sorted(unique.values(), key=lambda item: item.uid)),
            warnings=tuple(warnings),
            incomplete_course_ids=tuple(sorted(incomplete_course_ids)),
        )

    def _syllabus_file_material(
        self,
        file_resource: Any,
        course: CourseSummary,
        course_directory: Path,
        max_file_bytes: int,
        warnings: list[str],
        *,
        explicitly_linked: bool = False,
    ) -> SyllabusMaterial | None:
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
        is_pdf = content_type == "application/pdf" or display_name.casefold().endswith(
            ".pdf"
        )
        is_docx = content_type == DOCX_CONTENT_TYPE or display_name.casefold().endswith(
            ".docx"
        )
        if not (is_pdf or is_docx):
            return None
        if bool(self._attr(file_resource, "locked", False)):
            return None
        if bool(self._attr(file_resource, "hidden_for_user", False)) and not explicitly_linked:
            return None
        if int(self._attr(file_resource, "size", 0) or 0) > max_file_bytes:
            warnings.append(
                f"Skipped oversized syllabus file in {course.name}: {display_name}."
            )
            return None
        try:
            content = file_resource.get_contents(binary=True)
        except (CanvasException, RequestException):
            warnings.append(
                f"Could not download syllabus file in {course.name}: {display_name}."
            )
            return None
        valid_content = (
            isinstance(content, bytes)
            and len(content) <= max_file_bytes
            and (
                (is_pdf and content.lstrip().startswith(b"%PDF-"))
                or (is_docx and self._valid_docx(content))
            )
        )
        if not valid_content:
            warnings.append(
                f"Skipped invalid syllabus file in {course.name}: {display_name}."
            )
            return None
        content_type = "application/pdf" if is_pdf else DOCX_CONTENT_TYPE
        safe_name = self._safe_filename(
            display_name, fallback="syllabus.pdf" if is_pdf else "syllabus.docx"
        )
        path = course_directory / f"{source_id}-{safe_name}"
        self._atomic_write(path, content)
        return SyllabusMaterial(
            uid=f"canvas:syllabus-file:{course.id}:{source_id}",
            source_id=source_id,
            course_id=course.id,
            course_name=course.name,
            title=display_name,
            content_type=content_type,
            local_path=path,
            content_sha256=sha256(content).hexdigest(),
            updated_at=self._parse_datetime(
                self._attr(file_resource, "updated_at", None), required=False
            ),
            html_url=f"{self.base_url}/courses/{course.id}/files/{source_id}",
        )

    def _syllabus_page_material(
        self,
        page: Any,
        course: CourseSummary,
        course_directory: Path,
    ) -> SyllabusMaterial | None:
        page_url = str(
            self._attr(page, "url", None)
            or self._attr(page, "page_url", None)
            or "syllabus"
        )
        title = str(self._attr(page, "title", None) or "Course syllabus")
        html = str(self._attr(page, "body", "") or "")
        if not self._html_to_text(html):
            return None
        content = html.encode("utf-8")
        safe_name = self._safe_filename(title, fallback="syllabus-page")
        path = course_directory / f"page-{safe_name}.html"
        self._atomic_write(path, content)
        return SyllabusMaterial(
            uid=f"canvas:syllabus-content-page:{course.id}:{page_url}",
            source_id=page_url,
            course_id=course.id,
            course_name=course.name,
            title=title,
            content_type="text/html",
            local_path=path,
            content_sha256=sha256(content).hexdigest(),
            updated_at=self._parse_datetime(
                self._attr(page, "updated_at", None), required=False
            ),
            html_url=f"{self.base_url}/courses/{course.id}/pages/{page_url}",
        )

    def _discover_syllabus_in_modules(
        self,
        resource: Any,
        course: CourseSummary,
        course_directory: Path,
        file_resources: dict[str, Any],
        max_file_bytes: int,
        materials: list[SyllabusMaterial],
        warnings: list[str],
    ) -> None:
        try:
            modules = resource.get_modules(include=["items", "content_details"])
            for module in modules:
                if bool(self._attr(module, "locked_for_user", False)):
                    continue
                module_name = str(self._attr(module, "name", "") or "")
                items = self._attr(module, "items", None)
                if items is None:
                    items = module.get_module_items(include=["content_details"])
                for item in items:
                    title = str(self._attr(item, "title", "") or "")
                    if not SYLLABUS_FILE_PATTERN.search(f"{module_name} {title}"):
                        continue
                    item_type = str(self._attr(item, "type", "") or "")
                    source_id = str(
                        self._attr(item, "content_id", None)
                        or self._attr(item, "page_url", None)
                        or ""
                    )
                    try:
                        if item_type == "File" and source_id:
                            file = file_resources.get(source_id) or resource.get_file(source_id)
                            material = self._syllabus_file_material(
                                file,
                                course,
                                course_directory,
                                max_file_bytes,
                                warnings,
                                explicitly_linked=True,
                            )
                            if material:
                                materials.append(material)
                        elif item_type == "Page" and source_id:
                            page = resource.get_page(source_id)
                            material = self._syllabus_page_material(
                                page, course, course_directory
                            )
                            if material:
                                materials.append(material)
                            self._download_page_linked_syllabus_files(
                                page,
                                resource,
                                course,
                                course_directory,
                                file_resources,
                                max_file_bytes,
                                materials,
                                warnings,
                            )
                    except (CanvasException, RequestException, OSError, ValueError):
                        warnings.append(
                            f"Could not read syllabus module item in {course.name}: {title}."
                        )
        except (CanvasException, RequestException) as error:
            warnings.append(self._course_warning("syllabus modules", course, error))

    def _discover_syllabus_in_pages(
        self,
        resource: Any,
        course: CourseSummary,
        course_directory: Path,
        file_resources: dict[str, Any],
        max_file_bytes: int,
        materials: list[SyllabusMaterial],
        warnings: list[str],
    ) -> None:
        try:
            pages = resource.get_pages(sort="title")
            for summary in pages:
                title = str(self._attr(summary, "title", "") or "")
                page_url = str(
                    self._attr(summary, "url", None)
                    or self._attr(summary, "page_url", None)
                    or ""
                )
                if not page_url:
                    continue
                try:
                    page = resource.get_page(page_url)
                    html = str(self._attr(page, "body", "") or "")
                    searchable = f"{title} {self._html_to_text(html)}"
                    if not SYLLABUS_FILE_PATTERN.search(searchable):
                        continue
                    material = self._syllabus_page_material(
                        page, course, course_directory
                    )
                    if material:
                        materials.append(material)
                    self._download_page_linked_syllabus_files(
                        page,
                        resource,
                        course,
                        course_directory,
                        file_resources,
                        max_file_bytes,
                        materials,
                        warnings,
                    )
                except (CanvasException, RequestException, OSError, ValueError):
                    warnings.append(
                        f"Could not read syllabus page in {course.name}: {title}."
                    )
        except (CanvasException, RequestException) as error:
            warnings.append(self._course_warning("syllabus pages", course, error))

    def _download_page_linked_syllabus_files(
        self,
        page: Any,
        resource: Any,
        course: CourseSummary,
        course_directory: Path,
        file_resources: dict[str, Any],
        max_file_bytes: int,
        materials: list[SyllabusMaterial],
        warnings: list[str],
    ) -> None:
        html = str(self._attr(page, "body", "") or "")
        for source_id in set(CANVAS_FILE_ID_PATTERN.findall(html)):
            try:
                file = file_resources.get(source_id) or resource.get_file(source_id)
                material = self._syllabus_file_material(
                    file,
                    course,
                    course_directory,
                    max_file_bytes,
                    warnings,
                    explicitly_linked=True,
                )
                if material:
                    materials.append(material)
            except (CanvasException, RequestException, OSError, ValueError):
                warnings.append(
                    f"Could not download a syllabus-linked file in {course.name}."
                )

    def download_lecture_materials(
        self,
        directory: str | os.PathLike[str],
        *,
        excluded_course_patterns: Iterable[str] = (),
        max_file_bytes: int | None = None,
    ) -> LectureMaterialDownloadReport:
        """Download published lecture PDFs, PowerPoints, and pages from Modules."""
        max_file_bytes = max_file_bytes if max_file_bytes is not None else lecture_file_limit()
        destination = Path(directory).expanduser().resolve()
        excluded = tuple(value.casefold() for value in excluded_course_patterns)
        materials: dict[str, LectureMaterial] = {}
        warnings: list[str] = []

        for context in self._get_active_course_contexts():
            course = context.summary
            searchable = f"{course.name} {course.course_code or ''}".casefold()
            if any(value in searchable for value in excluded):
                continue
            try:
                modules = context.resource.get_modules(
                    include=["items", "content_details"]
                )
                for module in modules:
                    if self._attr(module, "published", True) is False or bool(self._attr(module, "locked_for_user", False)):
                        continue
                    module_name = str(self._attr(module, "name", "") or "")
                    module_position = self._optional_int(
                        self._attr(module, "position", None)
                    )
                    items = self._attr(module, "items", None)
                    if items is None:
                        items = module.get_module_items(include=["content_details"])
                    for item in items:
                        if self._attr(item, "published", True) is False or self._attr(item, "locked_for_user", False):
                            continue
                        title = str(self._attr(item, "title", "") or "")
                        combined = f"{module_name} {title}"
                        if not (
                            LECTURE_MATERIAL_PATTERN.search(combined)
                            or DATED_MATERIAL_PATTERN.search(combined)
                            or NUMBERED_LECTURE_PATTERN.search(title)
                        ):
                            continue
                        if NON_LECTURE_MATERIAL_PATTERN.search(combined):
                            continue
                        kind = str(self._attr(item, "type", "") or "")
                        source_id = str(
                            self._attr(item, "content_id", None)
                            or self._attr(item, "page_url", None)
                            or ""
                        )
                        position = self._optional_int(self._attr(item, "position", None))
                        material = None
                        try:
                            if kind == "File" and source_id:
                                material = self._download_lecture_file(
                                    context.resource, course, source_id, title, destination,
                                    max_file_bytes, module_name, module_position, position,
                                )
                            elif kind == "ExternalUrl":
                                material = self._download_external_slides(
                                    course, str(self._attr(item, "external_url", "")), title,
                                    destination, module_name, module_position, position,
                                )
                            elif kind == "Page" and source_id:
                                material = self._download_lecture_page(
                                    context.resource, course, source_id, title, destination,
                                    module_name, module_position, position,
                                )
                        except SlidesAccessError:
                            warnings.append(f"GOOGLE_SLIDES_AUTH_REQUIRED for course {course.id}; authorize Google Slides access.")
                            continue
                        except (CanvasException, RequestException, OSError, ValueError):
                            warnings.append(f"Lecture source unavailable for course {course.id}; remaining sources continued.")
                            continue
                        if material:
                            materials[material.uid] = material

                # Files can be available without being linked from a Module.
                files = context.resource.get_files(
                    content_types=[
                        "application/pdf",
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    ],
                    sort="updated_at",
                    order="desc",
                )
                for file in files:
                    name = str(
                        self._attr(file, "display_name", None)
                        or self._attr(file, "filename", None)
                        or ""
                    )
                    if not (
                        LECTURE_MATERIAL_PATTERN.search(name)
                        or DATED_MATERIAL_PATTERN.search(name)
                    ):
                        continue
                    if NON_LECTURE_MATERIAL_PATTERN.search(name):
                        continue
                    source_id = str(self._attr(file, "id", "") or "")
                    if not source_id:
                        continue
                    if f"canvas:lecture-file:{course.id}:{source_id}" in materials:
                        continue
                    try:
                        material = self._download_lecture_file(
                            context.resource,
                            course,
                            source_id,
                            name,
                            destination,
                            max_file_bytes,
                            "",
                            None,
                            None,
                        )
                    except (CanvasException, RequestException, OSError, ValueError):
                        warnings.append(f"Lecture source unavailable for course {course.id}; remaining sources continued.")
                        continue
                    if material and material.uid not in materials:
                        materials[material.uid] = material
            except (CanvasException, RequestException, OSError, ValueError) as error:
                warnings.append(self._course_warning("lecture modules", course, error))

        return LectureMaterialDownloadReport(
            materials=tuple(sorted(materials.values(), key=lambda value: value.uid)),
            warnings=tuple(warnings),
        )

    def _download_lecture_file(
        self,
        resource: Any,
        course: CourseSummary,
        source_id: str,
        title: str,
        destination: Path,
        limit: int,
        module_name: str,
        module_position: int | None,
        item_position: int | None,
    ) -> LectureMaterial | None:
        file = resource.get_file(source_id)
        name = str(
            self._attr(file, "display_name", None)
            or self._attr(file, "filename", None)
            or title
        )
        suffix = Path(name).suffix.casefold()
        mime = str(
            self._attr(file, "content-type", None)
            or self._attr(file, "content_type", None)
            or ""
        ).casefold()
        is_pdf = suffix == ".pdf" or mime == "application/pdf"
        is_pptx = suffix == ".pptx" or "presentationml.presentation" in mime
        if not (is_pdf or is_pptx):
            return None
        if bool(self._attr(file, "hidden_for_user", False)) or bool(
            self._attr(file, "locked", False)
        ):
            return None
        if int(self._attr(file, "size", 0) or 0) > limit:
            return None
        if int(self._attr(file, "size", 0) or 0) > SMALL_FILE_BYTES:
            return self._large_lecture_text(file, course, source_id, title, destination, limit,
                                            module_name, module_position, item_position, is_pdf)
        content = file.get_contents(binary=True)
        if not isinstance(content, bytes) or len(content) > limit:
            return None
        if is_pdf and not content.lstrip().startswith(b"%PDF-"):
            return None
        if is_pptx and not self._valid_pptx(content):
            return None
        content_type = (
            "application/pdf"
            if is_pdf
            else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )
        safe_name = self._safe_filename(name, fallback="lecture.pdf" if is_pdf else "lecture.pptx")
        path = destination / f"course-{course.id}" / "lectures" / f"{source_id}-{safe_name}"
        self._atomic_write(path, content)
        return LectureMaterial(
            uid=f"canvas:lecture-file:{course.id}:{source_id}",
            source_id=source_id,
            course_id=course.id,
            course_name=course.name,
            title=title or name,
            content_type=content_type,
            local_path=path,
            content_sha256=sha256(content).hexdigest(),
            updated_at=self._parse_datetime(self._attr(file, "updated_at", None), required=False),
            html_url=f"{self.base_url}/courses/{course.id}/files/{source_id}",
            module_name=module_name or None,
            module_position=module_position,
            item_position=item_position,
        )

    def _large_lecture_text(self, file, course, source_id, title, destination, limit,
                            module_name, module_position, item_position, is_pdf):
        from .ai_assistant import extract_pdf_text_chunks, extract_powerpoint_text_chunks
        import json
        revision = self._attr(file, "updated_at", None)
        identity = json.dumps([self.base_url, course.id, source_id, revision,
                               self._attr(file, "size", None)], sort_keys=True)
        fingerprint = sha256(identity.encode()).hexdigest()
        cache_key = "lecture-text-v2:" + sha256(f"{self.base_url}:{course.id}:{source_id}".encode()).hexdigest()
        cached = self.state_store.cache_get(cache_key) if self.state_store and revision else None
        text = cached.get("text") if isinstance(cached, dict) and cached.get("revision") == fingerprint else None
        if not isinstance(text, str) or not text.strip():
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="lecture-", dir=destination) as temporary:
                source = Path(temporary) / ("source.pdf" if is_pdf else "source.pptx")
                stream_download(str(self._attr(file, "url", "")), source, limit)
                extract = extract_pdf_text_chunks if is_pdf else extract_powerpoint_text_chunks
                text = "\n\n".join(c.text for c in extract(source, max_chars=2_000_000))
                if not text.strip() or len(text) > 2_000_000:
                    raise ValueError("Lecture text is empty or exceeds extraction limit")
            if self.state_store and revision:
                self.state_store.cache_set(cache_key, {"text": text, "revision": fingerprint})
        path = destination / f"course-{course.id}" / "lectures" / f"{source_id}-extracted.txt"
        self._atomic_write(path, text.encode())
        return LectureMaterial(
            uid=f"canvas:lecture-file:{course.id}:{source_id}", source_id=source_id,
            course_id=course.id, course_name=course.name, title=title,
            content_type="text/plain", local_path=path,
            content_sha256=sha256(text.encode()).hexdigest(),
            updated_at=self._parse_datetime(revision, required=False),
            html_url=f"{self.base_url}/courses/{course.id}/files/{source_id}",
            module_name=module_name or None, module_position=module_position,
            item_position=item_position,
        )

    def _download_external_slides(self, course, url, title, destination,
                                  module_name, module_position, item_position):
        import time as clock
        parsed = urlparse(url)
        match = re.fullmatch(r"/presentation/d/([A-Za-z0-9_-]+)(?:/.*)?", parsed.path)
        if parsed.scheme != "https" or parsed.netloc != "docs.google.com" or not match:
            return None
        document_id = match.group(1)
        canonical = f"https://docs.google.com/presentation/d/{document_id}"
        cache_key = "google-slides-text-v1:" + sha256(canonical.encode()).hexdigest()
        cached = self.state_store.cache_get(cache_key) if self.state_store else None
        text = None
        if isinstance(cached, dict) and 0 <= clock.time() - cached.get("fetched_at", 0) < 6 * 3600:
            text = cached.get("text")
        if not isinstance(text, str) or not text.strip():
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="slides-", dir=destination) as temporary:
                source = Path(temporary) / "slides.txt"
                try:
                    stream_download(canonical + "/export/txt", source, 2_000_000)
                    text = source.read_text(encoding="utf-8-sig").strip()
                except LectureDownloadError as error:
                    if error.status not in (401, 403):
                        raise
                    text = authenticated_slide_text(document_id)
                if not text or re.search(r"<(?:!doctype|html)\b", text[:500], re.I):
                    raise ValueError("Google Slides text export unavailable")
            if self.state_store:
                self.state_store.cache_set(cache_key, {"text": text, "fetched_at": clock.time()})
        path = destination / f"course-{course.id}" / "lectures" / f"google-{document_id}.txt"
        self._atomic_write(path, text.encode())
        return LectureMaterial(
            uid=f"canvas:google-slides:{course.id}:{document_id}", source_id=document_id,
            course_id=course.id, course_name=course.name, title=title,
            content_type="text/plain", local_path=path, content_sha256=sha256(text.encode()).hexdigest(),
            updated_at=None, html_url=canonical + "/edit", module_name=module_name or None,
            module_position=module_position, item_position=item_position,
        )

    def _download_lecture_page(
        self,
        resource: Any,
        course: CourseSummary,
        page_url: str,
        title: str,
        destination: Path,
        module_name: str,
        module_position: int | None,
        item_position: int | None,
    ) -> LectureMaterial | None:
        page = resource.get_page(page_url)
        html = str(self._attr(page, "body", "") or "")
        if not self._html_to_text(html):
            return None
        content = html.encode("utf-8")
        safe_name = self._safe_filename(title, fallback="lecture-page")
        path = destination / f"course-{course.id}" / "lectures" / f"page-{safe_name}.html"
        self._atomic_write(path, content)
        return LectureMaterial(
            uid=f"canvas:lecture-page:{course.id}:{page_url}",
            source_id=page_url,
            course_id=course.id,
            course_name=course.name,
            title=title,
            content_type="text/html",
            local_path=path,
            content_sha256=sha256(content).hexdigest(),
            updated_at=self._parse_datetime(self._attr(page, "updated_at", None), required=False),
            html_url=f"{self.base_url}/courses/{course.id}/pages/{page_url}",
            module_name=module_name or None,
            module_position=module_position,
            item_position=item_position,
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
        self, contexts: Iterable[_CourseContext], warnings: list[str],
        incomplete_courses: set[int] | None = None,
    ) -> tuple[AcademicItem, ...]:
        start = self._now_utc()
        end = start + timedelta(days=self.lookahead_days)
        items: list[AcademicItem] = []
        if incomplete_courses is None:
            incomplete_courses = set()

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
                        if self._attr(assignment, "due_at", None) in {None, ""}:
                            warnings.append(f"Skipped undated assignment in {course.name}.")
                            continue
                        item = self._assignment_to_item(assignment, course)
                        if item.falls_in_window(start, end):
                            items.append(item)
                    except (TypeError, ValueError) as error:
                        incomplete_courses.add(course.id)
                        warnings.append(
                            f"Skipped malformed assignment in {course.name}: {error}"
                        )
            except (CanvasException, RequestException) as error:
                incomplete_courses.add(course.id)
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
                        if item.falls_in_window(start, end):
                            items.append(item)
                    except (TypeError, ValueError) as error:
                        incomplete_courses.add(course.id)
                        warnings.append(
                            f"Skipped malformed calendar event in {course.name}: {error}"
                        )
            except (CanvasException, RequestException) as error:
                incomplete_courses.add(course.id)
                warnings.append(self._course_warning("calendar events", course, error))

        unique = {item.uid: item for item in items}
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (item.due_at, item.course_name, item.title),
            )
        )

    def _fetch_announcements(
        self,
        contexts: Iterable[_CourseContext],
        warnings: list[str],
        *,
        unread_only: bool,
        incomplete_courses: set[int] | None = None,
    ) -> tuple[Announcement, ...]:
        now = self._now_utc()
        cutoff = now - timedelta(days=self.announcement_days)
        announcements: list[Announcement] = []
        if incomplete_courses is None:
            incomplete_courses = set()

        for context in contexts:
            course = context.summary
            try:
                arguments: dict[str, Any] = {
                    "only_announcements": True,
                    "order_by": "recent_activity",
                }
                if unread_only:
                    arguments["filter_by"] = "unread"
                topics = context.resource.get_discussion_topics(**arguments)
                for topic in topics:
                    try:
                        announcement = self._topic_to_announcement(topic, course)
                        if (
                            cutoff <= announcement.posted_at <= now
                            and (
                                not unread_only
                                or announcement.read_state == "unread"
                            )
                        ):
                            announcements.append(announcement)
                    except (TypeError, ValueError) as error:
                        incomplete_courses.add(course.id)
                        warnings.append(
                            f"Skipped malformed announcement in {course.name}: {error}"
                        )
            except (CanvasException, RequestException) as error:
                incomplete_courses.add(course.id)
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
        start_at = (
            self._parse_all_day_date(self._attr(event, "all_day_date", None))
            if all_day else None
        )
        if start_at is None:
            start_at = self._parse_datetime(
                self._attr(event, "start_at", None), required=False
            )
        if start_at is None:
            all_day_date = self._attr(event, "all_day_date", None)
            start_at = self._parse_all_day_date(all_day_date)
        if start_at is None:
            raise ValueError(f"calendar event {source_id} has no start date")
        end_at = self._parse_datetime(self._attr(event, "end_at", None), required=False)
        if all_day and end_at is not None:
            # Preserve the civil-day span while anchoring it to all_day_date.
            original_start = self._parse_datetime(
                self._attr(event, "start_at", None), required=False
            ) or start_at
            days = max(1, (end_at.astimezone(self.timezone).date() - original_start.astimezone(self.timezone).date()).days)
            end_date = start_at.astimezone(self.timezone).date() + timedelta(days=days)
            end_at = datetime.combine(end_date, time.min, tzinfo=self.timezone).astimezone(UTC)
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
        posted_at = self._parse_datetime(
            self._attr(topic, "posted_at", None)
            or self._attr(topic, "created_at", None)
            or self._attr(topic, "updated_at", None)
        )
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
    def _optional_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    @staticmethod
    def _valid_pptx(content: bytes) -> bool:
        if not content.startswith(b"PK"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                total_size = sum(item.file_size for item in archive.infolist())
                return (
                    "[Content_Types].xml" in names
                    and "ppt/presentation.xml" in names
                    and total_size <= 100 * 1024 * 1024
                )
        except (OSError, zipfile.BadZipFile):
            return False

    @staticmethod
    def _valid_docx(content: bytes) -> bool:
        if not content.startswith(b"PK"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                total_size = sum(item.file_size for item in archive.infolist())
                return (
                    "[Content_Types].xml" in names
                    and "word/document.xml" in names
                    and total_size <= 100 * 1024 * 1024
                )
        except (OSError, zipfile.BadZipFile):
            return False

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
        return isinstance(error, (InvalidAccessToken, Unauthorized)) or status_code in {401, 403}

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
