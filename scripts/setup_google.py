#!/usr/bin/env python3
"""Explicit interactive Google OAuth setup; scheduled runs never open a browser."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant.calendar_sync import (
    CalendarAuthenticationError,
    CalendarConfigurationError,
    GoogleCalendarAuthenticator,
)
from academic_assistant.google_slides import SLIDES_SCOPE
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lecture-slides", action="store_true", help="Also grant read-only Google Slides access"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the existing Calendar authorization without opening a browser",
    )
    parser.add_argument(
        "--reauthorize",
        action="store_true",
        help="Force a new Calendar offline refresh token even if the cached access token works",
    )
    args = parser.parse_args()
    if args.check and (args.lecture_slides or args.reauthorize):
        parser.error("--check cannot be combined with --lecture-slides or --reauthorize")
    if args.reauthorize and args.lecture_slides:
        parser.error("--reauthorize and --lecture-slides cannot be combined")
    authenticator = GoogleCalendarAuthenticator(
        ROOT / Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")).expanduser(),
        ROOT / Path(os.getenv("GOOGLE_TOKEN_FILE", "token.json")).expanduser(),
        interactive=True,
    )
    if args.check:
        authenticator.interactive = False
        try:
            service = authenticator.build_service()
            authenticator.verify_service(service)
        except (CalendarAuthenticationError, CalendarConfigurationError) as error:
            print(f"Google Calendar OAuth: CHECK FAILED\n{error}", file=sys.stderr)
            return 1
        print("Google Calendar OAuth: OK\nCalendar API authentication: OK")
        return 0
    try:
        if args.lecture_slides:
            authenticator.token_path = (
                ROOT
                / Path(
                    os.getenv("GOOGLE_SLIDES_TOKEN_FILE", "google_slides_token.json")
                ).expanduser()
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(authenticator.credentials_path), [SLIDES_SCOPE]
            )
            credentials = flow.run_local_server(
                port=0, access_type="offline", prompt="select_account consent", timeout_seconds=120
            )
            if not isinstance(credentials, Credentials):
                raise CalendarAuthenticationError("Expected user OAuth credentials")
            authenticator._save_token(credentials)
        elif args.reauthorize:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(authenticator.credentials_path), authenticator.scopes
            )
            credentials = flow.run_local_server(
                port=0,
                access_type="offline",
                prompt="select_account consent",
                timeout_seconds=120,
            )
            if not isinstance(credentials, Credentials):
                raise CalendarAuthenticationError("Expected user OAuth credentials")
            authenticator._save_token(credentials)
        else:
            authenticator.authenticate()
    except (CalendarAuthenticationError, CalendarConfigurationError, OSError, ValueError):
        print(
            "Google authorization failed safely. Verify credentials.json, browser "
            "access, and OAuth consent-screen configuration.",
            file=sys.stderr,
        )
        return 1
    print("Google Calendar authorization succeeded.\n")
    print("If using GitHub Actions, replace GOOGLE_TOKEN_B64 with the base64")
    print("encoding of the newly generated token.json.")
    print("If using study buttons, refresh and verify the Worker's separate OAuth copy with:")
    print("  .venv/bin/python scripts/configure_cloudflare_secrets.py --google-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
