#!/usr/bin/env python3
"""Securely add a Discord webhook URL to the local .env file."""

from __future__ import annotations

from getpass import getpass
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import DiscordConfigurationError, DiscordNotifier  # noqa: E402
from scripts.setup_canvas import _write_env  # noqa: E402


def main() -> int:
    print("Discord webhook setup")
    webhook_url = getpass("Discord webhook URL (hidden): ").strip()
    if not webhook_url:
        print("No webhook URL entered; nothing was saved.", file=sys.stderr)
        return 1
    if "\n" in webhook_url or "\r" in webhook_url:
        print("The webhook URL contains an invalid newline; nothing was saved.", file=sys.stderr)
        return 1

    try:
        # Construction validates the URL without sending a Discord message.
        DiscordNotifier(webhook_url, state_path=PROJECT_ROOT / "data/notification_state.db")
    except DiscordConfigurationError as error:
        print(f"Invalid webhook URL: {error}", file=sys.stderr)
        print("Nothing was saved.", file=sys.stderr)
        return 1

    _write_env(
        ENV_PATH,
        {
            "DISCORD_WEBHOOK_URL": webhook_url,
            "NOTIFICATION_STATE_DB": "data/notification_state.db",
            "DISCORD_TIMEOUT_SECONDS": "15",
            "DISCORD_MAX_ATTEMPTS": "4",
            "DISCORD_MAX_RATE_LIMIT_WAIT_SECONDS": "60",
        },
    )
    print(f"Webhook saved to {ENV_PATH} with owner-only permissions.")
    print("Next: .venv/bin/python scripts/test_notifier.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled; nothing was saved.", file=sys.stderr)
        raise SystemExit(130)
