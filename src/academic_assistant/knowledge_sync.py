"""Complete course snapshots for the always-online Discord assistant.

Extraction caches live in StateStore and therefore use the existing encrypted
checkpoint. A failed enumeration/extraction never publishes a partial snapshot.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from canvasapi.exceptions import CanvasException
from requests import RequestException

from .ai_assistant import PDFExtractionError, extract_pdf_text_chunks, extract_powerpoint_text_chunks
from .canvas_client import CANVAS_FILE_ID_PATTERN
from .lecture_files import lecture_file_limit
from .google_slides import SlidesAccessError


class KnowledgeSyncError(RuntimeError):
    """Internal messages must be fixed text, never provider exception bodies."""

    def __init__(self, message: str, *, code: str = "SYNC_FAILED",
                 stage: str = "configuration", status: int | None = None,
                 retries: int = 0):
        super().__init__(message)
        self.code, self.stage, self.status, self.retries = code, stage, status, retries

    def diagnostic(self) -> str:
        status = f", HTTP {self.status}" if self.status is not None else ""
        return f"{self.stage}: {self.code}{status}, retries={self.retries}"


def failure_summary(error: Exception) -> str:
    # Never stringify arbitrary exceptions, including provider subclasses.
    if type(error) is KnowledgeSyncError:
        detail = str(error) if error.code == "SYNC_FAILED" else error.diagnostic()
        return f"{detail}; prior snapshots retained"
    return "Snapshot sync failed (UNEXPECTED_ERROR); prior snapshots retained"


def source_failure(error: Exception, stage: str) -> tuple[str, int | None]:
    """Return a fixed, non-sensitive diagnostic category for source failures."""
    response = getattr(error, "response", None)
    status_value = getattr(response, "status_code", None) or getattr(error, "status_code", None)
    try:
        status = int(status_value) if status_value is not None else None
    except (TypeError, ValueError):
        status = None
    if status in {401, 403}:
        return "SOURCE_PERMISSION_DENIED", status
    if status == 404:
        return "SOURCE_NOT_FOUND", status
    if status is not None:
        return "CANVAS_HTTP_ERROR", status
    if isinstance(error, requests.Timeout):
        return "SOURCE_NETWORK_TIMEOUT", None
    if isinstance(error, requests.ConnectionError):
        return "SOURCE_NETWORK_ERROR", None
    if isinstance(error, PDFExtractionError):
        return "PDF_EXTRACTION_FAILED", None
    if isinstance(error, CanvasException):
        return "CANVAS_API_ERROR", None
    if isinstance(error, OSError):
        return "SOURCE_FILE_ERROR", None
    if isinstance(error, (TypeError, ValueError, KeyError)):
        return "MALFORMED_SOURCE", None
    return "UNEXPECTED_ERROR", None


def normalize(value: str) -> str:
    value = re.sub(r"([a-z])(\d)", r"\1 \2", value.casefold().replace("&", " and "))
    return re.sub(r"[^a-z0-9]+", " ", value.replace("'", "").replace("’", "")).strip()


def match_course(name: str, code: str, courses: list[dict]) -> dict | None:
    text = f" {normalize(name + ' ' + code)} "
    matches = [c for c in courses if any(
        f" {normalize(alias)} " in text for alias in
        [c["key"], c["name"], c.get("code", ""), *c["match"], *c.get("aliases", [])]
        if normalize(alias)
    )]
    if len(matches) > 1:
        raise KnowledgeSyncError("Canvas course matches multiple configured courses (COURSE_MATCH_AMBIGUOUS)", code="COURSE_MATCH_AMBIGUOUS", stage="course_matching")
    return matches[0] if matches else None


def chunks(text: str) -> list[str]:
    text = text.strip()
    result = []
    while text:
        end = min(len(text), 1400)
        if end < len(text):
            boundary = max(text.rfind("\n", 700, end), text.rfind(" ", 700, end))
            if boundary > 0:
                end = boundary
        result.append(text[:end])
        text = text[max(1, end - 150):].strip() if end < len(text) else ""
    return result


def record(source_id: str, title: str, source_type: str, text: str, *, url: str | None = None,
           updated_at: str | None = None, module: Any = None, deadline: str | None = None) -> dict:
    if url:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            url = None
        else:
            # Canvas file verifier/query tokens never enter cloud citations.
            url = parsed._replace(query="", fragment="").geturl()
    value = dict(id=source_id, title=title[:300], type=source_type, url=url,
                 updated_at=updated_at, module=module, deadline=deadline, chunks=chunks(text))
    value["hash"] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return value


class KnowledgeSync:
    def __init__(self, canvas: Any, store: Any, schedule: dict, directory: Path,
                 url: str, secret: str, session: Any = requests):
        if schedule.get("timezone") != "America/Toronto":
            raise KnowledgeSyncError("Course schedule timezone must be America/Toronto (INVALID_TIMEZONE)", code="INVALID_TIMEZONE", stage="configuration")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not secret:
            raise KnowledgeSyncError("Knowledge sync requires an HTTPS Worker URL and sync secret (INVALID_WORKER_CONFIG)", code="INVALID_WORKER_CONFIG", stage="configuration")
        self.canvas, self.store, self.schedule = canvas, store, schedule
        self.directory, self.url, self.secret, self.session = directory, url.rstrip("/"), secret, session

    def _text(self, material: Any) -> str:
        key = f"ask-extract-v1:{material.uid}"
        cached = self.store.cache_get(key) if self.store else None
        if isinstance(cached, dict) and cached.get("hash") == material.content_sha256:
            return cached["text"]
        if material.content_type == "application/pdf":
            text = "\n\n".join(c.text for c in extract_pdf_text_chunks(material.local_path))
        elif material.local_path.suffix.lower() == ".pptx":
            text = "\n\n".join(c.text for c in extract_powerpoint_text_chunks(material.local_path, max_chars=2_000_000))
        elif material.content_type == "text/plain":
            text = material.local_path.read_text(encoding="utf-8")
        else:
            text = self.canvas._html_to_text(material.local_path.read_text(encoding="utf-8"))
        if not text.strip():
            raise KnowledgeSyncError("A course source has no extractable text (TEXT_EXTRACTION_EMPTY)", code="TEXT_EXTRACTION_EMPTY", stage="text_extraction")
        if self.store:
            self.store.cache_set(key, {"hash": material.content_sha256, "text": text})
        return text

    def _source_text(self, material: Any) -> str:
        try:
            return self._text(material)
        except KnowledgeSyncError:
            raise
        except Exception:
            raise KnowledgeSyncError("Source text extraction failed", code="TEXT_EXTRACTION_FAILED",
                                     stage="text_extraction") from None

    def collect(self, context: Any, course: dict) -> list[dict]:
        api, summary = context.resource, context.summary
        attr = self.canvas._attr
        base = f"{self.canvas.base_url}/courses/{summary.id}"
        records: dict[str, dict] = {}
        modules: dict[tuple[str, str], list[dict]] = {}
        module_items: dict[tuple[str, str], Any] = {}
        excluded: set[tuple[str, str]] = set()
        for module in api.get_modules():
            visible = attr(module, "published", True) is not False and not attr(module, "locked_for_user", False)
            for item in module.get_module_items():
                kind = str(attr(item, "type", ""))
                key = str(attr(item, "page_url", "") if kind == "Page" else
                          attr(item, "external_url", "") if kind == "ExternalUrl" else attr(item, "content_id", ""))
                if not visible or attr(item, "published", True) is False or attr(item, "locked_for_user", False):
                    excluded.add((kind, key))
                else:
                    module_items[(kind, key)] = item
                    modules.setdefault((kind, key), []).append({
                        "name": attr(module, "name", ""), "position": attr(module, "position", None),
                        "item_position": attr(item, "position", None),
                    })
        # Never index module items explicitly reported as unpublished or inaccessible.
        def allowed(value: Any, kind: str, key: str) -> bool:
            return (kind, key) not in excluded and attr(value, "published", True) is not False and not any(
                attr(value, field, False) for field in ("locked_for_user", "hidden_for_user", "hidden", "locked")
            )

        details = self.canvas._canvas.get_course(summary.id, include=["syllabus_body"])
        syllabus = attr(details, "syllabus_body", "")
        syllabus_file_ids = set(CANVAS_FILE_ID_PATTERN.findall(str(syllabus or "")))
        if syllabus:
            records["syllabus"] = record("syllabus", "Course syllabus", "syllabus",
                self.canvas._html_to_text(syllabus), url=f"{base}/assignments/syllabus")
        pages: dict[str, Any] = {
            key: item for (kind, key), item in module_items.items() if kind == "Page" and key
        }
        try:
            pages.update({
                str(attr(page, "url", "")): page
                for page in api.get_pages()
                if attr(page, "url", "")
            })
        except (CanvasException, RequestException):
            # Canvas returns 404 when a course has no Pages tab. Accessible
            # module-linked pages above are still a complete visible inventory.
            pass
        for key, page in pages.items():
            if not key or not allowed(page, "Page", key):
                continue
            try:
                full = api.get_page(key)
            except (CanvasException, RequestException):
                # Preserve atomicity: an advertised visible module page that
                # cannot be read makes this course snapshot incomplete.
                if ("Page", key) in module_items:
                    raise
                continue
            if not allowed(full, "Page", key):
                continue
            text = self.canvas._html_to_text(str(attr(full, "body", "") or ""))
            source_id = f"page:{key}"
            records[source_id] = record(source_id, str(attr(full, "title", key)), "page", text,
                url=f"{base}/pages/{key}", updated_at=attr(full, "updated_at", None), module=modules.get(("Page", key)))
        files: dict[str, Any] = {}
        try:
            files.update({
                str(attr(file, "id", "")): file
                for file in api.get_files()
                if attr(file, "id", "")
            })
        except (CanvasException, RequestException):
            # Canvas returns 403 when the Files tab is hidden. Published files
            # linked from visible modules remain individually accessible.
            pass
        # A course can hide its Files tab while exposing a file through the
        # syllabus. Canvas still permits direct retrieval of that linked file.
        for key in syllabus_file_ids:
            if key not in files:
                files[key] = api.get_file(key)
        for (kind, key), item in module_items.items():
            if kind != "File" or not key or key in files:
                continue
            try:
                files[key] = api.get_file(key)
            except (CanvasException, RequestException):
                raise KnowledgeSyncError("A visible module file could not be read (SOURCE_UNREADABLE)", code="SOURCE_UNREADABLE", stage="source_retrieval")
        for key, file in files.items():
            name = str(attr(file, "display_name", None) or attr(file, "filename", ""))
            if not key or not allowed(file, "File", key) or Path(name).suffix.lower() not in {".pdf", ".pptx"}:
                continue
            limit = lecture_file_limit()
            if int(attr(file, "size", 0) or 0) > limit:
                # This is an explicit indexing limit, not an incomplete retrieval.
                # Keep a citation explaining the omission so search cannot imply
                # that it has read this source's contents.
                course_key = re.sub(r"[^a-zA-Z0-9_-]", "_", str(course["key"]))[:80]
                logging.getLogger("attendr.knowledge_sync").warning(
                    "Course source excluded: %s — SOURCE_TOO_LARGE, limit_bytes=%d",
                    course_key, limit,
                )
                source_id = f"file:{key}"
                records[source_id] = record(
                    source_id, name, "file",
                    f"Contents not indexed: this file exceeds the {limit // (1024 * 1024)} MiB indexing limit. "
                    "Open the original Canvas file to read it.",
                    url=f"{base}/files/{key}", module=modules.get(("File", key)),
                )
                continue
            material = self.canvas._download_lecture_file(api, summary, key, name, self.directory,
                limit, "", None, None)
            if material is None:
                raise KnowledgeSyncError("A published PDF/PowerPoint could not be downloaded safely (SOURCE_UNREADABLE)", code="SOURCE_UNREADABLE", stage="source_retrieval")
            source_id = f"file:{key}"
            records[source_id] = record(source_id, name, "pdf" if name.lower().endswith(".pdf") else "powerpoint",
                self._source_text(material), url=material.html_url,
                updated_at=material.updated_at.isoformat() if material.updated_at else None,
                module=modules.get(("File", key)))
        for (kind, key), item in module_items.items():
            if kind != "ExternalUrl" or not key or not allowed(item, kind, key):
                continue
            try:
                material = self.canvas._download_external_slides(
                    summary, key, str(attr(item, "title", "Lecture slides")), self.directory,
                    "", None, None,
                )
            except SlidesAccessError as error:
                source_id = "external:" + hashlib.sha256(key.encode()).hexdigest()
                records[source_id] = record(source_id, str(attr(item, "title", "Lecture slides")),
                    "presentation", "Contents not indexed: Google Slides read access is required.",
                    url=key, module=modules.get((kind, key)))
                logging.getLogger("attendr.knowledge_sync").warning("Linked slides excluded: %s", error.code)
                continue
            except (ValueError, OSError, RequestException):
                raise KnowledgeSyncError("Linked slides could not be read", code="SOURCE_UNREADABLE",
                                         stage="source_retrieval") from None
            if material is not None:
                source_id = f"external:{material.source_id}"
                records[source_id] = record(source_id, material.title, "presentation",
                    self._source_text(material), url=material.html_url, module=modules.get((kind, key)))
        for assignment in api.get_assignments(override_assignment_dates=True):
            key = str(attr(assignment, "id", ""))
            if not key or not allowed(assignment, "Assignment", key):
                continue
            title = str(attr(assignment, "name", "Assignment"))
            due = attr(assignment, "due_at", None)
            text = title + "\n" + self.canvas._html_to_text(str(attr(assignment, "description", "") or ""))
            if due:
                text += f"\nCanvas assignment deadline: {due}"
            source_id = f"assignment:{key}"
            records[source_id] = record(source_id, title, "assignment", text,
                url=f"{base}/assignments/{key}", updated_at=attr(assignment, "updated_at", None),
                module=modules.get(("Assignment", key)), deadline=due)
        for topic in api.get_discussion_topics(only_announcements=True):
            key = str(attr(topic, "id", ""))
            if not key or not allowed(topic, "Discussion", key):
                continue
            posted = attr(topic, "posted_at", None)
            if posted and datetime.fromisoformat(posted.replace("Z", "+00:00")) > datetime.now(timezone.utc):
                continue
            source_id = f"announcement:{key}"
            records[source_id] = record(source_id, str(attr(topic, "title", "Announcement")), "announcement",
                self.canvas._html_to_text(str(attr(topic, "message", "") or "")),
                url=f"{base}/discussion_topics/{key}", updated_at=attr(topic, "updated_at", None) or posted)
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        timetable = [f"Term: {self.schedule['term']['start_date']} to {self.schedule['term']['end_date']} (America/Toronto)."]
        timetable.extend(f"{s['type'].title()}: {days[s['weekday']]} {s['start']}–{s['end']} (America/Toronto)." for s in course["sessions"])
        timetable.extend(f"No class: {s['start']} to {s['end']} ({s['reason']})." for s in self.schedule["term"].get("no_class", []))
        records["verified-schedule"] = record("verified-schedule", "Verified course schedule", "schedule", "\n".join(timetable))
        return list(records.values())

    def sync(self) -> int:
        failures, count = [], 0
        # Resolve the complete inventory first; never let ambiguous courses overwrite one another.
        mapped = []
        seen = set()
        for context in self.canvas._get_active_course_contexts():
            course = match_course(context.summary.name, context.summary.course_code or "", self.schedule["courses"])
            if course:
                if course["key"] in seen:
                    raise KnowledgeSyncError("Multiple active Canvas courses map to the same course key (COURSE_MATCH_AMBIGUOUS)", code="COURSE_MATCH_AMBIGUOUS", stage="course_matching")
                seen.add(course["key"])
                mapped.append((context, course))
        for context, course in mapped:
            stage = "source_retrieval"
            try:
                # Revision starts before enumeration: late retries cannot overwrite a newer snapshot.
                revision = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                records = self.collect(context, course)
                stage = "snapshot_build"
                payload = {"course": {**course, "code": context.summary.course_code or "", "term": self.schedule["term"]},
                           "complete": True, "synced_at": revision, "records": records}
                # Bound each D1 value/upload while preserving every extracted chunk.
                parts = []
                for source in records:
                    for offset in range(0, max(1, len(source["chunks"])), 32):
                        part = {**source, "id": f"{source['id']}:{offset // 32}", "chunks": source["chunks"][offset:offset + 32]}
                        part["hash"] = hashlib.sha256(json.dumps({k: v for k, v in part.items() if k != "hash"}, sort_keys=True).encode()).hexdigest()
                        parts.append(part)
                if len(parts) > 20_000:
                    raise KnowledgeSyncError("Course snapshot exceeds source limit (SNAPSHOT_TOO_LARGE)", code="SNAPSHOT_TOO_LARGE", stage="snapshot_build")
                stage = "upload"
                batch, size = [], 0
                for part in parts:
                    length = len(json.dumps(part).encode())
                    if batch and size + length > 300_000:
                        self._upload("stage", {**payload, "records": batch})
                        batch, size = [], 0
                    batch.append(part)
                    size += length
                if batch:
                    self._upload("stage", {**payload, "records": batch})
                stage = "publish"
                self._upload("publish", {**payload, "records": [], "count": len(parts)})
                count += 1
            except Exception as error:
                if type(error) is KnowledgeSyncError:
                    detail = error.diagnostic()
                    unexpected = False
                else:
                    code, status = source_failure(error, stage)
                    status_text = f", HTTP {status}" if status is not None else ""
                    detail = f"{stage}: {code}{status_text}"
                    unexpected = code == "UNEXPECTED_ERROR"
                # Only configured keys and fixed diagnostics enter logs; no source data.
                key = re.sub(r"[^a-zA-Z0-9_-]", "_", str(course["key"]))[:80]
                detail = f"{key} — {detail}"
                failures.append(detail)
                logger = logging.getLogger("attendr.knowledge_sync")
                if unexpected:
                    logger.error(
                        "Course snapshot failed unexpectedly: %s, exception_type=%s",
                        detail,
                        type(error).__name__,
                    )
                else:
                    logger.error("Course snapshot failed: %s", detail)
        if failures:
            raise KnowledgeSyncError(f"{len(failures)} course snapshot(s) failed; previous complete snapshots preserved: " + "; ".join(failures))
        if not count:
            raise KnowledgeSyncError("No configured active Canvas courses were found (NO_MATCHING_COURSES)", code="NO_MATCHING_COURSES", stage="course_matching")
        return count

    def _upload(self, action: str, payload: dict) -> None:
        stage = "publish" if action == "publish" else "upload"
        for attempt in range(3):
            status = None
            try:
                response = self.session.post(f"{self.url}/api/knowledge/{action}",
                    headers={"Authorization": f"Bearer {self.secret}"}, json=payload, timeout=30)
                status = response.status_code
                if status == 200:
                    return
                code = ("WORKER_AUTH_REJECTED" if status in (401, 403) else
                        "WORKER_RATE_LIMITED" if status == 429 else
                        "WORKER_SERVER_ERROR" if status >= 500 else "WORKER_REJECTED")
                if status < 500 and status != 429:
                    raise KnowledgeSyncError("Worker rejected the course snapshot", code=code,
                                             stage=stage, status=status, retries=attempt)
            except requests.Timeout:
                code = "WORKER_TIMEOUT"
            except requests.RequestException:
                code = "WORKER_NETWORK_ERROR"
        raise KnowledgeSyncError("Course snapshot upload failed", code=code,
                                 stage=stage, status=status, retries=attempt)
