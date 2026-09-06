#!/usr/bin/env python3
"""Run the Attendr Canvas, Discord, Google Calendar, and Gemini pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    MaterialsConfigurationError,
    MaterialsStateError,
    LectureQuizRunner,
    StudyPlanner,
    PDFExtractionError,
    extract_pdf_text_chunks,
    send_quiz_to_discord,
)

UTC = timezone.utc
KNOWN_ERRORS = (
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


@dataclass(frozen=True, slots=True)
class RunPlan:
    announcements: bool
    materials: bool
    calendar: bool
    digest: bool
    quiz: bool
    lecture_quizzes: bool
    study_plan: bool

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize Canvas, Discord, Google Calendar, and daily quizzes."
    )
    modes = parser.add_mutually_exclusive_group()
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
        help="Only send today's 48-hour deadline digest.",
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
    quiz_source.add_argument(
        "--quiz-pdf", type=Path, help="Lecture PDF used for the quiz."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send Discord messages even if already sent (does not alter deduplication state).",
    )
    return parser


def resolve_plan(arguments: argparse.Namespace) -> RunPlan:
    if arguments.sync_only:
        return RunPlan(False, not arguments.no_materials, True, False, False, False, not arguments.no_study_plan)
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


def upcoming_items(
    items: tuple[AcademicItem, ...], hours: int = 48
) -> tuple[AcademicItem, ...]:
    now = datetime.now(UTC)
    cutoff = now + timedelta(hours=hours)
    return tuple(item for item in items if now <= item.due_at <= cutoff)


def filter_material_duplicates(
    canvas_items: tuple[AcademicItem, ...],
    material_items: tuple[AcademicItem, ...],
) -> tuple[AcademicItem, ...]:
    """Prefer live Canvas dates when a syllabus describes the same deadline."""
    normalized_canvas = [
        (
            item.course_id,
            item.due_at_local.date(),
            item.kind,
            re.sub(r"[^a-z0-9]+", " ", item.title.casefold()).strip(),
        )
        for item in canvas_items
    ]
    selected: list[AcademicItem] = []
    for material in material_items:
        title = re.sub(r"[^a-z0-9]+", " ", material.title.casefold()).strip()
        duplicate = any(
            course_id == material.course_id
            and (
                canvas_title == title
                or (due_date == material.due_at_local.date() and kind == material.kind)
            )
            for course_id, due_date, kind, canvas_title in normalized_canvas
        )
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

    raw_path = Path(
        os.getenv("DAILY_TOPICS_FILE", "data/daily_topics.json")
    ).expanduser()
    topics_path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
    if topics_path.is_file():
        try:
            topics = json.loads(topics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Daily topics file is invalid: {topics_path}") from error
        if not isinstance(topics, dict):
            raise ValueError(
                f"Daily topics file must contain a JSON object: {topics_path}"
            )
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
        return StepResult(name, "failed", f"Unexpected {type(error).__name__}")


def run_pipeline(arguments: argparse.Namespace) -> list[StepResult]:
    plan = resolve_plan(arguments)
    results: list[StepResult] = []
    snapshot: CanvasSnapshot | None = None
    canvas_client: CanvasClient | None = None
    material_items: tuple[AcademicItem, ...] = ()
    announcement_date_items: tuple[AcademicItem, ...] = ()
    notifier: DiscordNotifier | None = None
    schedule: CourseSchedule | None = None

    schedule_path = PROJECT_ROOT / os.getenv(
        "COURSE_SCHEDULE_FILE", "data/course_schedule.json"
    )
    if plan.needs_canvas:
        try:
            schedule = CourseSchedule.load(schedule_path)
        except (OSError, ValueError, KeyError, TypeError) as error:
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
                )
            results.append(
                StepResult(
                    "Canvas",
                    "ok",
                    f"{len(snapshot.courses)} courses, {len(snapshot.items)} upcoming "
                    f"items, {len(snapshot.announcements)} unread announcements, "
                    f"{len(snapshot.warnings)} warnings",
                )
            )
        except KNOWN_ERRORS as error:
            results.append(StepResult("Canvas", "failed", str(error)))
        except Exception as error:  # noqa: BLE001 - isolate independent pipeline steps.
            results.append(
                StepResult("Canvas", "failed", f"Unexpected {type(error).__name__}")
            )

    if plan.announcements:
        if snapshot is None:
            results.append(
                StepResult("Announcements", "skipped", "Canvas data unavailable")
            )
        else:

            def send_announcements() -> str:
                sent = 0
                skipped = 0
                failures = 0
                for announcement in snapshot.announcements:
                    try:
                        if get_notifier().send_announcement_alert(
                            announcement, force=arguments.force
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
                return f"{sent} sent, {skipped} already seen"

            results.append(run_step("Announcements", send_announcements))

    if plan.materials:
        if canvas_client is None or snapshot is None:
            results.append(
                StepResult("Materials", "skipped", "Canvas data unavailable")
            )
        else:
            try:
                materials_report = CourseMaterialsSync.from_env(
                    canvas_client, PROJECT_ROOT / ".env"
                ).sync()
                allowed_course_ids = {course.id for course in snapshot.courses}
                allowed_material_items = tuple(
                    item
                    for item in materials_report.items
                    if item.course_id in allowed_course_ids
                )
                material_items = filter_material_duplicates(
                    snapshot.items, allowed_material_items
                )
                duplicate_count = len(allowed_material_items) - len(material_items)
                results.append(
                    StepResult(
                        "Materials",
                        "ok",
                        f"{materials_report.materials_found} syllabus source(s), "
                        f"{materials_report.materials_analyzed} analyzed, "
                        f"{materials_report.cached_materials_reused} cached, "
                        f"{len(material_items)} calendar deadline(s), "
                        f"{duplicate_count} live-Canvas duplicate(s), "
                        f"{len(materials_report.warnings)} warnings",
                    )
                )
            except KNOWN_ERRORS as error:
                results.append(StepResult("Materials", "failed", str(error)))
            except Exception as error:  # noqa: BLE001 - isolate pipeline steps.
                results.append(
                    StepResult(
                        "Materials", "failed", f"Unexpected {type(error).__name__}"
                    )
                )

    if plan.calendar and canvas_client is not None and snapshot is not None:
        try:
            recent_announcements = canvas_client.get_recent_announcements()
            allowed_course_ids = {course.id for course in snapshot.courses}
            recent_announcements = tuple(
                item
                for item in recent_announcements
                if item.course_id in allowed_course_ids
            )
            date_report = AnnouncementDatesSync(
                AIAssistant.from_env(PROJECT_ROOT / ".env"),
                index_path=PROJECT_ROOT
                / os.getenv(
                    "ANNOUNCEMENT_DATES_INDEX",
                    "data/announcement_dates_index.json",
                ),
                app_timezone=os.getenv("APP_TIMEZONE", "America/Toronto"),
                course_schedule=schedule,
            ).sync(recent_announcements)
            announcement_date_items = date_report.items
            results.append(
                StepResult(
                    "Announcement dates",
                    "ok",
                    f"{date_report.analyzed} analyzed, {date_report.cached} cached, "
                    f"{len(date_report.items)} dated item(s), {len(date_report.warnings)} warnings",
                )
            )
        except KNOWN_ERRORS as error:
            results.append(StepResult("Announcement dates", "failed", str(error)))

    if plan.calendar:
        if snapshot is None:
            results.append(StepResult("Calendar", "skipped", "Canvas data unavailable"))
        else:

            def sync_calendar() -> str:
                canvas_items = snapshot.items
                derived = prefer_announcement_dates(
                    material_items, announcement_date_items
                )
                derived = filter_material_duplicates(canvas_items, derived)
                university_dates = (
                    schedule.academic_calendar_items() if schedule is not None else ()
                )
                academic_items = canvas_items + derived + university_dates
                replaced_exam_uids: frozenset[str] = frozenset()
                if schedule is not None:
                    calendar_items, replaced_exam_uids = (
                        schedule.merge_with_class_schedule(academic_items)
                    )
                else:
                    calendar_items = academic_items
                report = GoogleCalendarSync.from_env(PROJECT_ROOT / ".env").sync_items(
                    calendar_items, delete_uids=replaced_exam_uids
                )
                items_by_uid = {item.uid: item for item in calendar_items}
                for entry in report.created + report.updated + report.deleted:
                    changed_item = items_by_uid.get(entry.canvas_uid)
                    action_text = {
                        "created": "added to",
                        "updated": "updated in",
                        "deleted": "removed from",
                    }[entry.action]
                    detail = (
                        f"Current time: {changed_item.due_at_local.strftime('%Y-%m-%d %I:%M %p %Z')}"
                        if changed_item is not None
                        else "The obsolete or replaced calendar entry was removed."
                    )
                    get_notifier().send_custom_notification(
                        f"calendar-change:{entry.action}:{entry.canvas_uid}",
                        {
                            "username": "Attendr",
                            "content": f"📅 **Academic calendar item {entry.action}**",
                            "embeds": [
                                {
                                    "title": entry.title[:256],
                                    "description": (
                                        f"This item was {action_text} Google Calendar.\n"
                                        f"{detail}"
                                    ),
                                    "color": 0xF59E0B,
                                }
                            ],
                            "allowed_mentions": {"parse": []},
                        },
                        destination="calendar_updates",
                    )
                return (
                    f"{len(report.created)} created, {len(report.updated)} updated, "
                    f"{len(report.skipped)} unchanged, {len(report.deleted)} removed"
                )

            results.append(run_step("Calendar", sync_calendar))

    if plan.study_plan:
        if snapshot is None:
            results.append(StepResult("Study plan", "skipped", "Canvas data unavailable"))
        else:
            def sync_study_plan() -> str:
                derived = prefer_announcement_dates(
                    material_items, announcement_date_items
                )
                derived = filter_material_duplicates(snapshot.items, derived)
                report = StudyPlanner.from_env(PROJECT_ROOT / ".env").sync(
                    snapshot.items + derived
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

    if plan.digest:
        if snapshot is None:
            results.append(StepResult("Digest", "skipped", "Canvas data unavailable"))
        else:
            deadlines = upcoming_items(
                snapshot.items
                + prefer_announcement_dates(material_items, announcement_date_items),
                48,
            )
            if not deadlines:
                results.append(
                    StepResult("Digest", "skipped", "No deadlines in next 48 hours")
                )
            else:

                def send_digest() -> str:
                    sent = get_notifier().send_daily_digest(
                        deadlines, hours=48, force=arguments.force
                    )
                    return "sent" if sent else "already sent today"

                results.append(run_step("Digest", send_digest))

    if plan.quiz:
        try:
            quiz_title, quiz_source = resolve_quiz_source(arguments)
        except AIInputError as error:
            status = "failed" if arguments.quiz_only else "skipped"
            results.append(StepResult("Quiz", status, str(error)))
        else:

            def generate_quiz() -> str:
                questions = AIAssistant.from_env(PROJECT_ROOT / ".env").generate_quiz(
                    quiz_source, num_questions=3
                )
                sent = send_quiz_to_discord(
                    get_notifier(), quiz_title, questions, force=arguments.force
                )
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
                    state_path=PROJECT_ROOT
                    / os.getenv(
                        "LECTURE_QUIZ_STATE_FILE", "data/lecture_quiz_state.json"
                    ),
                    retry_hours=int(os.getenv("LECTURE_QUIZ_RETRY_HOURS", "30")),
                )
                report = runner.run(force=arguments.force)
                return (
                    f"{report.sent} sent, {report.already_sent} already sent, "
                    f"{report.waiting_for_slides} waiting for slides, "
                    f"{len(report.warnings)} warnings"
                )

            results.append(run_step("Lecture quizzes", send_lecture_quizzes))

    return results


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if (arguments.topic or arguments.quiz_pdf) and not (
        arguments.daily_quiz or arguments.quiz_only
    ):
        parser.error("--topic and --quiz-pdf require --daily-quiz or --quiz-only")

    results = run_pipeline(arguments)
    for result in results:
        print(f"[{result.status.upper():7}] {result.name}: {result.detail}")
    return 1 if any(result.status == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
