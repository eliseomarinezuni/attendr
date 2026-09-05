#!/usr/bin/env python3
"""Sync up to three real upcoming Canvas assignments to Google Calendar."""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (  # noqa: E402
    CalendarAPIError,
    CalendarAuthenticationError,
    CalendarConfigurationError,
    CanvasAPIError,
    CanvasAuthenticationError,
    CanvasClient,
    CanvasConfigurationError,
    GoogleCalendarSync,
)


def main() -> int:
    try:
        snapshot = CanvasClient.from_env(PROJECT_ROOT / ".env").fetch_snapshot()
        assignments = tuple(
            item for item in snapshot.items if item.source == "assignment"
        )[:3]
        if not assignments:
            print(
                "No upcoming Canvas assignments were found; authenticating and "
                "preparing the calendar without adding events."
            )

        calendar = GoogleCalendarSync.from_env(PROJECT_ROOT / ".env")
        report = calendar.sync_items(assignments)
    except (
        CanvasConfigurationError,
        CanvasAuthenticationError,
        CanvasAPIError,
        CalendarConfigurationError,
        CalendarAuthenticationError,
        CalendarAPIError,
    ) as error:
        print(f"Calendar test failed: {error}", file=sys.stderr)
        return 1

    print(f"Calendar: {report.calendar_name} ({report.calendar_id})")
    for heading, entries in (
        ("Created", report.created),
        ("Updated", report.updated),
        ("Skipped/already synced", report.skipped),
    ):
        print(f"{heading}: {len(entries)}")
        for entry in entries:
            print(f"  - {entry.title}")

    print(f"Total Canvas assignments processed: {len(report.entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
