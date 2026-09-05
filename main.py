#!/usr/bin/env python3
"""Run the Attendr Canvas, Discord, Google Calendar, and Gemini pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (
    AcademicItem,
    AIAssistant,
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
    DiscordConfigurationError,
    DiscordNotificationError,
    DiscordNotifier,
    GoogleCalendarSync,
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
    PDFExtractionError,
    OSError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class RunPlan:
    announcements: bool
    calendar: bool
    digest: bool
    quiz: bool

    @property
    def needs_canvas(self) -> bool:
        return self.announcements or self.calendar or self.digest


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
    parser.add_argument(
        "--daily-quiz",
        action="store_true",
        help="Add a daily quiz to the normal pipeline.",
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
        return RunPlan(False, True, False, False)
    if arguments.announcements_only:
        return RunPlan(True, False, False, False)
    if arguments.digest_only:
        return RunPlan(False, False, True, False)
    if arguments.quiz_only:
        return RunPlan(False, False, False, True)
    return RunPlan(True, True, True, bool(arguments.daily_quiz))


def upcoming_items(
    snapshot: CanvasSnapshot, hours: int = 48
) -> tuple[AcademicItem, ...]:
    now = datetime.now(UTC)
    cutoff = now + timedelta(hours=hours)
    return tuple(item for item in snapshot.items if now <= item.due_at <= cutoff)


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
    notifier: DiscordNotifier | None = None

    def get_notifier() -> DiscordNotifier:
        nonlocal notifier
        if notifier is None:
            notifier = DiscordNotifier.from_env(PROJECT_ROOT / ".env")
        return notifier

    if plan.needs_canvas:
        try:
            snapshot = CanvasClient.from_env(PROJECT_ROOT / ".env").fetch_snapshot()
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

    if plan.calendar:
        if snapshot is None:
            results.append(StepResult("Calendar", "skipped", "Canvas data unavailable"))
        else:

            def sync_calendar() -> str:
                assignments = tuple(
                    item for item in snapshot.items if item.source == "assignment"
                )
                report = GoogleCalendarSync.from_env(PROJECT_ROOT / ".env").sync_items(
                    assignments
                )
                return (
                    f"{len(report.created)} created, {len(report.updated)} updated, "
                    f"{len(report.skipped)} unchanged"
                )

            results.append(run_step("Calendar", sync_calendar))

    if plan.digest:
        if snapshot is None:
            results.append(StepResult("Digest", "skipped", "Canvas data unavailable"))
        else:
            deadlines = upcoming_items(snapshot, 48)
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
