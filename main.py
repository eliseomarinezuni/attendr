#!/usr/bin/env python3
"""Run the Attendr Canvas, Discord, Google Calendar, and Gemini pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (
    AcademicItem,
    AIAssistant,
    AnnouncementDatesSync,
    AIConfigurationError,
    AIInputError,
    AIProviderError,
    CalendarAPIError,
    CalendarAuthenticationError,
    CalendarConfigurationError,
    CanvasAPIError,
    CanvasAuthenticationError,
    CanvasClient,
    CanvasConfigurationError,
    CanvasSnapshot,
    CourseMaterialsSync,
    CourseSchedule,
    DiscordConfigurationError,
    DiscordNotificationError,
    DiscordNotifier,
    GoogleCalendarSync,
    GoogleCalendarAuthenticator,
    MaterialsConfigurationError,
    MaterialsStateError,
    LectureQuizRunner,
    StudyPlanner,
    PDFExtractionError,
    extract_pdf_text_chunks,
    quiz_discord_payload,
)
from academic_assistant.ai_efficiency import AI_USAGE

from academic_assistant.state_store import StateStore
from academic_assistant.cloud_state import CloudStateError
from academic_assistant.settings import Settings
from academic_assistant.preferences import Preferences
from academic_assistant.triage import AnnouncementTriage
from academic_assistant.review import ReviewQueue
from academic_assistant.logging_config import configure_logging

UTC = timezone.utc
KNOWN_ERRORS = (
    CloudStateError,
    AIConfigurationError,
    AIInputError,
    AIProviderError,
    CalendarAPIError,
    CalendarAuthenticationError,
    CalendarConfigurationError,
    CanvasAPIError,
    CanvasAuthenticationError,
    CanvasConfigurationError,
    DiscordConfigurationError,
    DiscordNotificationError,
    MaterialsConfigurationError,
    MaterialsStateError,
    PDFExtractionError,
    OSError,
    ValueError,
)
PIPELINE_LOGGER = logging.getLogger("attendr.pipeline")


@dataclass(frozen=True, slots=True)
class RunPlan:
    announcements: bool
    materials: bool
    calendar: bool
    digest: bool
    quiz: bool
    lecture_quizzes: bool
    study_plan: bool
    review: bool = False

    @property
    def needs_canvas(self) -> bool:
        return (
            self.announcements
            or self.materials
            or self.calendar
            or self.digest
            or self.lecture_quizzes
            or self.study_plan
        )


@dataclass(frozen=True, slots=True)
class StepResult:
    name: str
    status: str
    detail: str


def unexpected_step_result(name: str, error: Exception) -> StepResult:
    PIPELINE_LOGGER.exception("Unexpected failure in pipeline step %s", name)
    return StepResult(name, "failed", f"Unexpected {type(error).__name__}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize Canvas, Discord, Google Calendar, and daily quizzes."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--review-only",
        action="store_true",
        help="Send due spaced-repetition questions without Canvas or Gemini.",
    )
    modes.add_argument(
        "--sync-only",
        action="store_true",
        help="Only sync Canvas deadlines to Google Calendar.",
    )
    modes.add_argument(
        "--announcements-only",
        action="store_true",
        help="Only send unseen Canvas announcements.",
    )
    modes.add_argument(
        "--digest-only",
        action="store_true",
        help="Only send today's configured deadline digest.",
    )
    modes.add_argument(
        "--quiz-only",
        action="store_true",
        help="Only generate and send the daily quiz.",
    )
    modes.add_argument(
        "--lecture-quizzes",
        action="store_true",
        help="Send quizzes for lecture sessions that recently ended.",
    )
    modes.add_argument(
        "--study-plan-only",
        action="store_true",
        help="Only create or update focused study sessions.",
    )
    parser.add_argument(
        "--daily-quiz",
        action="store_true",
        help="Add a daily quiz to the normal pipeline.",
    )
    parser.add_argument(
        "--no-materials",
        action="store_true",
        help="Skip automatic Canvas syllabus download and deadline extraction.",
    )
    parser.add_argument(
        "--no-study-plan",
        action="store_true",
        help="Skip automatic focused study-session planning.",
    )
    quiz_source = parser.add_mutually_exclusive_group()
    quiz_source.add_argument("--topic", help="Lecture topic or text used for the quiz.")
    quiz_source.add_argument("--quiz-pdf", type=Path, help="Lecture PDF used for the quiz.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send Discord messages even if already sent (does not alter deduplication state).",
    )
    return parser


def resolve_plan(arguments: argparse.Namespace) -> RunPlan:
    if arguments.review_only:
        return RunPlan(False, False, False, False, False, False, False, True)
    if arguments.sync_only:
        return RunPlan(
            False,
            not arguments.no_materials,
            True,
            False,
            False,
            False,
            not arguments.no_study_plan,
        )
    if arguments.announcements_only:
        return RunPlan(True, False, False, False, False, False, False)
    if arguments.digest_only:
        return RunPlan(False, False, False, True, False, False, False)
    if arguments.quiz_only:
        return RunPlan(False, False, False, False, True, False, False)
    if arguments.lecture_quizzes:
        return RunPlan(False, False, False, False, False, True, False)
    if arguments.study_plan_only:
        return RunPlan(False, not arguments.no_materials, False, False, False, False, True)
    return RunPlan(
        True,
        not arguments.no_materials,
        True,
        True,
        bool(arguments.daily_quiz),
        True,
        not arguments.no_study_plan,
    )


def upcoming_items(items: tuple[AcademicItem, ...], hours: int = 72) -> tuple[AcademicItem, ...]:
    now = datetime.now(UTC)
    cutoff = now + timedelta(hours=hours)
    return tuple(item for item in items if item.falls_in_window(now, cutoff))


def filter_material_duplicates(
    canvas_items: tuple[AcademicItem, ...],
    material_items: tuple[AcademicItem, ...],
) -> tuple[AcademicItem, ...]:
    """Prefer live Canvas dates when a syllabus describes the same deadline."""

    def identity(item: AcademicItem) -> tuple[int, str]:
        return item.course_id, re.sub(r"[^a-z0-9]+", " ", item.title.casefold()).strip()

    canvas_counts = Counter(identity(item) for item in canvas_items)
    material_counts = Counter(identity(item) for item in material_items)
    selected: list[AcademicItem] = []
    for material in material_items:
        key = identity(material)
        duplicate = bool(key[1]) and canvas_counts[key] == material_counts[key] == 1
        if not duplicate:
            selected.append(material)
    return tuple(selected)


def prefer_announcement_dates(
    syllabus_items: tuple[AcademicItem, ...],
    announcement_items: tuple[AcademicItem, ...],
) -> tuple[AcademicItem, ...]:
    """Newest announcement replaces a syllabus item with the same semantic ID."""
    merged = {item.uid: item for item in syllabus_items}
    for item in sorted(
        announcement_items,
        key=lambda value: value.updated_at or datetime.min.replace(tzinfo=UTC),
    ):
        merged[item.uid] = item
    return tuple(sorted(merged.values(), key=lambda value: (value.due_at, value.uid)))


def resolve_quiz_source(arguments: argparse.Namespace) -> tuple[str, str]:
    if arguments.topic and arguments.topic.strip():
        return arguments.topic.strip(), arguments.topic.strip()
    if arguments.quiz_pdf:
        chunks = extract_pdf_text_chunks(arguments.quiz_pdf)
        text = "\n\n".join(chunk.text for chunk in chunks)
        return arguments.quiz_pdf.stem, text

    configured_topic = os.getenv("DAILY_QUIZ_TOPIC", "").strip()
    if configured_topic:
        return configured_topic, configured_topic

    raw_path = Path(os.getenv("DAILY_TOPICS_FILE", "data/daily_topics.json")).expanduser()
    topics_path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
    if topics_path.is_file():
        try:
            topics = json.loads(topics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Daily topics file is invalid: {topics_path}") from error
        if not isinstance(topics, dict):
            raise ValueError(f"Daily topics file must contain a JSON object: {topics_path}")
        timezone_name = os.getenv("APP_TIMEZONE", "America/Toronto")
        try:
            local_date = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"APP_TIMEZONE is invalid: {timezone_name}") from error
        topic = topics.get(local_date) or topics.get("default")
        if isinstance(topic, str) and topic.strip():
            return topic.strip(), topic.strip()

    raise AIInputError(
        "No quiz topic is configured. Use --topic, --quiz-pdf, DAILY_QUIZ_TOPIC, "
        "or add today's date to data/daily_topics.json."
    )


def run_step(name: str, operation: Callable[[], str]) -> StepResult:
    try:
        return StepResult(name, "ok", operation())
    except KNOWN_ERRORS as error:
        return StepResult(name, "failed", str(error))
    except Exception as error:  # noqa: BLE001 - isolate independent pipeline steps.
        return unexpected_step_result(name, error)


def run_pipeline(arguments: argparse.Namespace) -> list[StepResult]:
    plan = resolve_plan(arguments)
    preferences = Preferences.load(PROJECT_ROOT)
    results: list[StepResult] = []
    snapshot: CanvasSnapshot | None = None
    canvas_client: CanvasClient | None = None
    material_items: tuple[AcademicItem, ...] = ()
    announcement_date_items: tuple[AcademicItem, ...] = ()
    notifier: DiscordNotifier | None = None
    schedule: CourseSchedule | None = None
    inputs_complete = True
    google_service: Any | None = None
    google_auth_error: CalendarAuthenticationError | CalendarConfigurationError | None = None

    if plan.calendar or plan.study_plan:
        credentials_path = (
            PROJECT_ROOT
            / Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")).expanduser()
        )
        token_path = PROJECT_ROOT / Path(os.getenv("GOOGLE_TOKEN_FILE", "token.json")).expanduser()
        try:
            authenticator = GoogleCalendarAuthenticator(credentials_path, token_path)
            google_service = authenticator.build_service()
            authenticator.verify_service(google_service)
        except (CalendarAuthenticationError, CalendarConfigurationError) as error:
            google_auth_error = error

    schedule_path = PROJECT_ROOT / os.getenv("COURSE_SCHEDULE_FILE", "data/course_schedule.json")
    if plan.needs_canvas:
        try:
            schedule = CourseSchedule.load(schedule_path)
            if schedule.timezone.key != os.getenv("APP_TIMEZONE", "America/Toronto"):
                raise ValueError("Course schedule timezone must match APP_TIMEZONE")
        except (OSError, ValueError, KeyError, TypeError) as error:
            inputs_complete = False
            results.append(StepResult("Schedule", "failed", str(error)))

    def get_notifier() -> DiscordNotifier:
        nonlocal notifier
        if notifier is None:
            notifier = DiscordNotifier.from_env(PROJECT_ROOT / ".env")
        return notifier

    if plan.needs_canvas:
        try:
            canvas_client = CanvasClient.from_env(PROJECT_ROOT / ".env")
            snapshot = canvas_client.fetch_snapshot()
            for warning in snapshot.warnings:
                logging.getLogger("attendr.snapshot").warning("%s", warning)
            if schedule is not None:
                excluded_ids = {
                    course.id
                    for course in snapshot.courses
                    if any(
                        pattern in f"{course.name} {course.course_code or ''}".casefold()
                        for pattern in schedule.excluded_course_patterns
                    )
                }
                snapshot = replace(
                    snapshot,
                    courses=tuple(
                        course for course in snapshot.courses if course.id not in excluded_ids
                    ),
                    items=tuple(
                        item for item in snapshot.items if item.course_id not in excluded_ids
                    ),
                    announcements=tuple(
                        item
                        for item in snapshot.announcements
                        if item.course_id not in excluded_ids
                    ),
                    complete=(
                        snapshot.complete
                        if not snapshot.incomplete_course_ids
                        else not (set(snapshot.incomplete_course_ids) - excluded_ids)
                    ),
                )
            inputs_complete = inputs_complete and snapshot.complete
            results.append(
                StepResult(
                    "Canvas",
                    "ok" if snapshot.complete else "failed",
                    f"{len(snapshot.courses)} courses, {len(snapshot.items)} upcoming "
                    f"items, {len(snapshot.announcements)} unread announcements, "
                    f"{len(snapshot.warnings)} warnings",
                )
            )
        except KNOWN_ERRORS as error:
            inputs_complete = False
            results.append(StepResult("Canvas", "failed", str(error)))
        except Exception as error:  # noqa: BLE001 - isolate independent pipeline steps.
            inputs_complete = False
            results.append(unexpected_step_result("Canvas", error))

    if (
        plan.materials
        and os.getenv("ATTENDR_ASK_SYNC", "").lower() == "true"
        and canvas_client is not None
    ):
        try:
            from academic_assistant.knowledge_sync import KnowledgeSync, failure_summary

            knowledge_store = StateStore(os.environ["ATTENDR_DB"])
            knowledge_schedule = json.loads(
                (
                    PROJECT_ROOT / os.getenv("COURSE_SCHEDULE_FILE", "data/course_schedule.json")
                ).read_text()
            )
            count = KnowledgeSync(
                canvas_client,
                knowledge_store,
                knowledge_schedule,
                PROJECT_ROOT / "data/materials/ask",
                os.getenv("STUDY_WORKER_URL", ""),
                os.getenv("STUDY_SYNC_SECRET", ""),
            ).sync()
            results.append(
                StepResult("Course search", "ok", f"{count} course snapshots synchronized")
            )
        except Exception as error:
            # Search snapshots are independently atomic and retain their previous
            # published version. Their outage must not invalidate calendar inputs.
            results.append(StepResult("Course search", "degraded", failure_summary(error)))

    if plan.announcements:
        if snapshot is None:
            results.append(StepResult("Announcements", "skipped", "Canvas data unavailable"))
        else:

            def send_announcements() -> str:
                sent = 0
                skipped = 0
                failures = 0
                muted = 0
                triage = AnnouncementTriage(
                    preferences,
                    StateStore(PROJECT_ROOT / os.getenv("ATTENDR_DB", "data/attendr.db")),
                )
                for announcement in snapshot.announcements:
                    try:
                        decision = triage.classify(announcement)
                        if decision.muted and not arguments.force:
                            muted += 1
                            continue
                        if get_notifier().send_announcement_alert(
                            announcement, force=arguments.force, triage=decision
                        ):
                            sent += 1
                        else:
                            skipped += 1
                    except (DiscordConfigurationError, DiscordNotificationError):
                        failures += 1
                if failures:
                    raise DiscordNotificationError(
                        f"{failures} announcement alert(s) failed; successful alerts were saved."
                    )
                return f"{sent} sent, {skipped} already seen, {muted} muted by preferences"

            results.append(run_step("Announcements", send_announcements))
            if not arguments.announcements_only:

                def send_assignment_alerts() -> str:
                    sent = 0
                    for item in upcoming_items(
                        snapshot.items, int(os.getenv("DISCORD_ALERT_HOURS", "72"))
                    ):
                        if item.submitted is not True:
                            sent += get_notifier().send_assignment_alert(
                                item, force=arguments.force
                            )
                    return f"{sent} deadline alert(s) sent"

                results.append(run_step("Assignment alerts", send_assignment_alerts))

    if plan.materials:
        if canvas_client is None or snapshot is None:
            results.append(StepResult("Materials", "skipped", "Canvas data unavailable"))
        else:
            try:
                materials_report = CourseMaterialsSync.from_env(
                    canvas_client, PROJECT_ROOT / ".env"
                ).sync(active_course_ids={course.id for course in snapshot.courses})
                inputs_complete = inputs_complete and materials_report.complete
                for warning in materials_report.warnings:
                    logging.getLogger("attendr.materials_report").warning("%s", warning)
                allowed_course_ids = {course.id for course in snapshot.courses}
                allowed_material_items = tuple(
                    item for item in materials_report.items if item.course_id in allowed_course_ids
                )
                material_items = filter_material_duplicates(snapshot.items, allowed_material_items)
                duplicate_count = len(allowed_material_items) - len(material_items)
                results.append(
                    StepResult(
                        "Materials",
                        (
                            "failed"
                            if not materials_report.complete
                            else "degraded"
                            if materials_report.warnings
                            else "ok"
                        ),
                        f"{materials_report.materials_found} syllabus source(s), "
                        f"{materials_report.materials_analyzed} analyzed, "
                        f"{materials_report.cached_materials_reused} cached, "
                        f"{len(material_items)} calendar deadline(s), "
                        f"{duplicate_count} live-Canvas duplicate(s), "
                        f"{len(materials_report.warnings)} warnings",
                    )
                )
            except KNOWN_ERRORS as error:
                inputs_complete = False
                results.append(StepResult("Materials", "failed", str(error)))
            except Exception as error:  # noqa: BLE001 - isolate pipeline steps.
                inputs_complete = False
                results.append(unexpected_step_result("Materials", error))

    if (plan.calendar or plan.study_plan) and canvas_client is not None and snapshot is not None:
        try:
            allowed_course_ids = {course.id for course in snapshot.courses}
            recent_announcements = canvas_client.get_recent_announcements(
                course_ids=allowed_course_ids
            )
            recent_announcements = tuple(
                item for item in recent_announcements if item.course_id in allowed_course_ids
            )
            date_report = AnnouncementDatesSync(
                AIAssistant.from_env(PROJECT_ROOT / ".env"),
                index_path=PROJECT_ROOT
                / os.getenv(
                    "ANNOUNCEMENT_DATES_INDEX",
                    "data/announcement_dates_index.json",
                ),
                state_store=StateStore(PROJECT_ROOT / os.getenv("ATTENDR_DB", "data/attendr.db")),
                app_timezone=os.getenv("APP_TIMEZONE", "America/Toronto"),
                course_schedule=schedule,
            ).sync(recent_announcements)
            inputs_complete = inputs_complete and date_report.complete
            for warning in date_report.warnings:
                logging.getLogger("attendr.date_report").warning("%s", warning)
            announcement_date_items = tuple(
                item
                for item in date_report.items
                if snapshot is not None
                and item.course_id in {course.id for course in snapshot.courses}
            )
            results.append(
                StepResult(
                    "Announcement dates",
                    (
                        "failed"
                        if not date_report.complete
                        else "degraded"
                        if date_report.warnings
                        else "ok"
                    ),
                    f"{date_report.analyzed} analyzed, {date_report.cached} cached, "
                    f"{len(date_report.items)} dated item(s), {len(date_report.warnings)} warnings",
                )
            )
        except KNOWN_ERRORS as error:
            inputs_complete = False
            results.append(StepResult("Announcement dates", "failed", str(error)))
        except Exception as error:
            inputs_complete = False
            results.append(unexpected_step_result("Announcement dates", error))

    if plan.calendar:
        if snapshot is None:
            results.append(StepResult("Calendar", "skipped", "Canvas data unavailable"))
        elif google_auth_error is not None:
            results.append(StepResult("Calendar", "failed", str(google_auth_error)))
        elif not inputs_complete:
            results.append(
                StepResult(
                    "Calendar", "failed", "Incomplete source data; existing calendar preserved"
                )
            )
        else:

            def sync_calendar() -> str:
                canvas_items = snapshot.items
                derived = prefer_announcement_dates(material_items, announcement_date_items)
                derived = filter_material_duplicates(canvas_items, derived)
                university_dates = (
                    schedule.academic_calendar_items() if schedule is not None else ()
                )
                academic_items = canvas_items + derived + university_dates
                replaced_exam_uids: frozenset[str] = frozenset()
                if schedule is not None and plan.materials:
                    calendar_items, replaced_exam_uids = schedule.merge_with_class_schedule(
                        academic_items
                    )
                else:
                    calendar_items = academic_items
                report = GoogleCalendarSync.from_env(
                    PROJECT_ROOT / ".env", service=google_service
                ).sync_items(
                    calendar_items,
                    delete_uids=replaced_exam_uids | frozenset(snapshot.removed_uids),
                )
                return (
                    f"{len(report.created)} created, {len(report.updated)} updated, "
                    f"{len(report.skipped)} unchanged, {len(report.deleted)} removed"
                )

            results.append(run_step("Calendar", sync_calendar))

    if plan.study_plan:
        if snapshot is None:
            results.append(StepResult("Study plan", "skipped", "Canvas data unavailable"))
        elif google_auth_error is not None:
            results.append(
                StepResult(
                    "Study plan",
                    "failed",
                    f"Google Calendar dependency unavailable; existing study plan preserved. {google_auth_error}",
                )
            )
        elif not inputs_complete or not plan.materials:
            results.append(
                StepResult(
                    "Study plan",
                    "failed" if not inputs_complete else "skipped",
                    "Complete Canvas, material, and announcement inputs required; existing study plan preserved",
                )
            )
        else:

            def sync_study_plan() -> str:
                derived = prefer_announcement_dates(material_items, announcement_date_items)
                derived = filter_material_duplicates(snapshot.items, derived)
                report = StudyPlanner.from_env(PROJECT_ROOT / ".env", service=google_service).sync(
                    snapshot.items + derived, inputs_complete=inputs_complete
                )
                return (
                    f"{len(report.sessions)} active sessions; "
                    f"{len(report.calendar.created)} created, "
                    f"{len(report.calendar.updated)} updated, "
                    f"{len(report.calendar.skipped)} unchanged, "
                    f"{len(report.calendar.deleted)} removed, "
                    f"{len(report.warnings)} warnings"
                )

            results.append(run_step("Study plan", sync_study_plan))

    if plan.calendar or plan.announcements or plan.quiz or plan.lecture_quizzes or plan.review:
        results.append(
            run_step(
                "Delivery outbox", lambda: f"{get_notifier().flush_pending()} recovered delivery(s)"
            )
        )

    if plan.digest:
        if snapshot is None:
            results.append(StepResult("Digest", "skipped", "Canvas data unavailable"))
        else:
            deadlines = upcoming_items(
                snapshot.items + prefer_announcement_dates(material_items, announcement_date_items),
                int(os.getenv("DISCORD_DIGEST_HOURS", "72")),
            )
            if not deadlines:
                results.append(
                    StepResult("Digest", "skipped", "No deadlines in configured digest window")
                )
            else:

                def send_digest() -> str:
                    sent = get_notifier().send_daily_digest(
                        deadlines,
                        hours=int(os.getenv("DISCORD_DIGEST_HOURS", "72")),
                        force=arguments.force,
                    )
                    return "sent" if sent else "already sent today"

                results.append(run_step("Digest", send_digest))

    if plan.quiz:
        try:
            quiz_store = StateStore(PROJECT_ROOT / os.getenv("ATTENDR_DB", "data/attendr.db"))
            quiz_day = (
                datetime.now(ZoneInfo(os.getenv("APP_TIMEZONE", "America/Toronto")))
                .date()
                .isoformat()
            )
            previous_quiz = (
                quiz_store.quiz(f"daily-quiz:{quiz_day}") if not arguments.force else None
            )
            quiz_title, quiz_source = (
                ("Cached quiz", "") if previous_quiz else resolve_quiz_source(arguments)
            )
        except AIInputError as error:
            status = "failed" if arguments.quiz_only else "skipped"
            results.append(StepResult("Quiz", status, str(error)))
        else:

            def generate_quiz() -> str:
                store = StateStore(PROJECT_ROOT / os.getenv("ATTENDR_DB", "data/attendr.db"))
                local_date = datetime.now(
                    ZoneInfo(os.getenv("APP_TIMEZONE", "America/Toronto"))
                ).date()
                key = f"daily-quiz:{local_date.isoformat()}"
                cached = store.quiz(key) if not arguments.force else None
                if cached and cached["sent_at"]:
                    return "already sent today"
                if cached:
                    payload = cached["payload"]
                else:
                    questions = AIAssistant.from_env(PROJECT_ROOT / ".env").generate_quiz(
                        quiz_source, num_questions=3
                    )
                    payload = quiz_discord_payload(quiz_title, questions)
                    if not arguments.force:
                        payload = store.save_quiz(key, payload)
                sent = get_notifier().send_custom_notification(
                    key,
                    payload,
                    force=arguments.force,
                    destination="lecture_quizzes",
                    fixed_fingerprint="daily",
                )
                if not arguments.force:
                    store.complete_quiz(key)
                return "3 questions sent" if sent else "already sent today"

            results.append(run_step("Quiz", generate_quiz))

    if plan.lecture_quizzes:
        if canvas_client is None or schedule is None:
            results.append(
                StepResult("Lecture quizzes", "skipped", "Canvas or schedule unavailable")
            )
        else:

            def send_lecture_quizzes() -> str:
                runner = LectureQuizRunner(
                    canvas_client,
                    AIAssistant.from_env(PROJECT_ROOT / ".env"),
                    get_notifier(),
                    schedule,
                    materials_directory=PROJECT_ROOT
                    / os.getenv("CANVAS_MATERIALS_DIR", "data/materials"),
                    state_path=PROJECT_ROOT / os.getenv("ATTENDR_DB", "data/attendr.db"),
                    retry_hours=int(os.getenv("LECTURE_QUIZ_RETRY_HOURS", "336")),
                )
                report = runner.run(force=arguments.force)
                for warning in report.warnings:
                    logging.getLogger("attendr.lecture_quiz").warning("%s", warning)
                if report.failed:
                    raise AIProviderError(
                        f"{report.failed} lecture quiz(es) failed; {report.sent} sent. "
                        "Unsent sessions remain eligible for retry."
                    )
                return (
                    f"{report.sent} sent, {report.already_sent} already sent, "
                    f"{report.waiting_for_slides} waiting for slides, "
                    f"{len(report.warnings)} warnings"
                )

            results.append(run_step("Lecture quizzes", send_lecture_quizzes))

    if preferences.review_enabled and (
        plan.review or (plan.announcements and not arguments.announcements_only)
    ):

        def send_reviews() -> str:
            store = StateStore(PROJECT_ROOT / os.getenv("ATTENDR_DB", "data/attendr.db"))
            queue = ReviewQueue(store, os.getenv("APP_TIMEZONE", "America/Toronto"))
            today = datetime.now(queue.timezone).date()
            sent = queue.send_due(
                get_notifier(), today, preferences.review_daily_limit, force=arguments.force
            )
            return f"{sent} review question(s) sent"

        results.append(run_step("Spaced review", send_reviews))
    return results


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if (arguments.topic or arguments.quiz_pdf) and not (
        arguments.daily_quiz or arguments.quiz_only
    ):
        parser.error("--topic and --quiz-pdf require --daily-quiz or --quiz-only")

    configure_logging()
    try:
        settings = Settings.from_env(resolve_plan(arguments))
        Preferences.load(PROJECT_ROOT)
    except ValueError as error:
        logging.getLogger("attendr").error("Configuration validation failed: %s", error)
        return 1
    os.environ["ATTENDR_DB"] = str((PROJECT_ROOT / settings.database).resolve())
    store = StateStore(os.environ["ATTENDR_DB"])
    with store.run_lock():
        return run_recorded(arguments, store)


def run_recorded(arguments: argparse.Namespace, store: StateStore) -> int:
    store.migrate_quizzes(
        PROJECT_ROOT / os.getenv("LECTURE_QUIZ_STATE_FILE", "data/lecture_quiz_state.json")
    )
    run_id = uuid.uuid4().hex
    configure_logging(run_id)
    AI_USAGE.reset()
    logging.getLogger("attendr").info("Run started")
    with store.connect() as db:
        db.execute(
            "INSERT INTO runs(run_id,started_at,status) VALUES(?,?,'running')",
            (run_id, time.time()),
        )
    try:
        results = run_pipeline(arguments)
    except Exception:
        logging.getLogger("attendr").exception("Run failed: %s", run_id)
        AI_USAGE.log_summary()
        with store.connect() as db:
            db.execute(
                "UPDATE runs SET finished_at=?,status='failed' WHERE run_id=?",
                (time.time(), run_id),
            )
        return 1
    run_status = (
        "failed"
        if any(result.status == "failed" for result in results)
        else "degraded"
        if any(result.status == "degraded" for result in results)
        else "ok"
    )
    with store.connect() as db:
        db.execute(
            "UPDATE runs SET finished_at=?,status=?,detail=? WHERE run_id=?",
            (
                time.time(),
                run_status,
                json.dumps([{"step": result.name, "status": result.status} for result in results]),
                run_id,
            ),
        )
    for result in results:
        print(f"[{result.status.upper():8}] {result.name}: {result.detail}")
    AI_USAGE.log_summary()
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(f"## Attendr run: {run_status}\n\n")
            summary.write("| Step | Status | Detail |\n|---|---|---|\n")
            for result in results:
                detail = result.detail.replace("|", "\\|").replace("\n", " ")
                summary.write(f"| {result.name} | {result.status} | {detail} |\n")
            usage = AI_USAGE.snapshot()
            summary.write("\n### AI efficiency\n\n")
            summary.write(
                "| Task | Requests | Cache hits | Deterministic | Input chars | Retries | Rate limits |\n"
            )
            summary.write("|---|---:|---:|---:|---:|---:|---:|\n")
            for task, metrics in sorted(usage.items()):
                summary.write(
                    f"| {task} | {metrics.requests} | {metrics.cache_hits} | "
                    f"{metrics.deterministic} | {metrics.input_chars} | "
                    f"{metrics.retries} | {metrics.rate_limits} |\n"
                )
    return 1 if any(result.status == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
