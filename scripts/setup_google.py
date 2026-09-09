#!/usr/bin/env python3
"""Explicit interactive Google OAuth setup; scheduled runs never open a browser."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant.calendar_sync import GoogleCalendarAuthenticator, SCOPES
from academic_assistant.google_slides import SLIDES_SCOPE
from google_auth_oauthlib.flow import InstalledAppFlow


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lecture-slides", action="store_true", help="Also grant read-only Google Slides access")
    args = parser.parse_args()
    authenticator = GoogleCalendarAuthenticator(
        ROOT
        / Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")).expanduser(),
        ROOT / Path(os.getenv("GOOGLE_TOKEN_FILE", "token.json")).expanduser(),
        interactive=True,
    )
    if args.lecture_slides:
        flow = InstalledAppFlow.from_client_secrets_file(str(authenticator.credentials_path), [*SCOPES, SLIDES_SCOPE])
        credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent", timeout_seconds=120)
        authenticator._save_token(credentials)
    else:
        authenticator.authenticate()
    print(
        "Google token saved. Update the scheduled OAuth secret if you use GitHub Actions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
