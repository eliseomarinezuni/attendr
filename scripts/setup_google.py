#!/usr/bin/env python3
"""Explicit interactive Google OAuth setup; scheduled runs never open a browser."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant.calendar_sync import GoogleCalendarAuthenticator


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    GoogleCalendarAuthenticator(
        ROOT
        / Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")).expanduser(),
        ROOT / Path(os.getenv("GOOGLE_TOKEN_FILE", "token.json")).expanduser(),
        interactive=True,
    ).authenticate()
    print(
        "Google token saved. Update the scheduled OAuth secret if you use GitHub Actions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
