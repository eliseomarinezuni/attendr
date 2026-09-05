#!/usr/bin/env python3
"""Live, read-only Canvas smoke test using credentials from .env."""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (  # noqa: E402
    CanvasAPIError,
    CanvasAuthenticationError,
    CanvasClient,
    CanvasConfigurationError,
)


def main() -> int:
    try:
        client = CanvasClient.from_env(PROJECT_ROOT / ".env")
        snapshot = client.fetch_snapshot()
    except (CanvasConfigurationError, CanvasAuthenticationError, CanvasAPIError) as error:
        print(f"Canvas test failed: {error}", file=sys.stderr)
        return 1

    print(f"Authenticated as: {snapshot.user_name} (ID: {snapshot.user_id})")
    print(f"\nActive courses ({len(snapshot.courses)}):")
    for course in snapshot.courses:
        code = f" [{course.course_code}]" if course.course_code else ""
        print(f"  - {course.name}{code}")

    print(f"\nUpcoming items ({len(snapshot.items)}):")
    for item in snapshot.items:
        due = item.due_at_local.strftime("%Y-%m-%d %I:%M %p %Z")
        print(f"  - {due} | {item.kind.upper()} | {item.course_name} | {item.title}")
        if item.html_url:
            print(f"    {item.html_url}")

    print(f"\nRecent unread announcements ({len(snapshot.announcements)}):")
    for announcement in snapshot.announcements:
        posted = announcement.posted_at_local.strftime("%Y-%m-%d %I:%M %p %Z")
        author = f" — {announcement.author_name}" if announcement.author_name else ""
        print(f"  - {posted} | {announcement.course_name} | {announcement.title}{author}")

    if snapshot.warnings:
        print(f"\nWarnings ({len(snapshot.warnings)}):", file=sys.stderr)
        for warning in snapshot.warnings:
            print(f"  - {warning}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
