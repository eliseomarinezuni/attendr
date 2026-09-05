#!/usr/bin/env python3
"""Send one real Canvas assignment and announcement to Discord."""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (  # noqa: E402
    CanvasAPIError,
    CanvasAuthenticationError,
    CanvasClient,
    CanvasConfigurationError,
    DiscordConfigurationError,
    DiscordNotificationError,
    DiscordNotifier,
)


def main() -> int:
    try:
        snapshot = CanvasClient.from_env(PROJECT_ROOT / ".env").fetch_snapshot()
        notifier = DiscordNotifier.from_env(PROJECT_ROOT / ".env")
    except (
        CanvasConfigurationError,
        CanvasAuthenticationError,
        CanvasAPIError,
        DiscordConfigurationError,
    ) as error:
        print(f"Notifier test setup failed: {error}", file=sys.stderr)
        return 1

    assignment = next(
        (item for item in snapshot.items if item.source == "assignment"), None
    )
    announcement = snapshot.announcements[0] if snapshot.announcements else None

    if assignment is None:
        print(
            "No upcoming Canvas assignment was found in the configured look-ahead window.",
            file=sys.stderr,
        )
        return 1
    if announcement is None:
        print(
            "No recent unread Canvas announcement was found. Mark one unread in Canvas "
            "and run this test again.",
            file=sys.stderr,
        )
        return 1

    try:
        # Force makes this explicit smoke test repeatable without changing dedupe state.
        notifier.send_assignment_alert(assignment, force=True)
        print(f"Sent assignment test: {assignment.course_name} — {assignment.title}")
        notifier.send_announcement_alert(announcement, force=True)
        print(
            f"Sent announcement test: {announcement.course_name} — {announcement.title}"
        )
    except DiscordNotificationError as error:
        print(f"Discord test failed: {error}", file=sys.stderr)
        return 1

    print("Check the configured Discord channel for both embeds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
