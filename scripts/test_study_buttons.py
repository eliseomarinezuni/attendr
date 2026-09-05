#!/usr/bin/env python3
"""Post a temporary, live Discord study-button test without exposing secrets."""

from __future__ import annotations

import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import GoogleCalendarAuthenticator, GoogleCalendarSync

UTC = timezone.utc


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    worker = os.getenv("STUDY_WORKER_URL", "").strip().rstrip("/")
    secret = os.getenv("STUDY_SYNC_SECRET", "").strip()
    if not worker or not secret:
        print("The Worker configuration is missing.", file=sys.stderr)
        return 1

    credentials = PROJECT_ROOT / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    token = PROJECT_ROOT / os.getenv("GOOGLE_TOKEN_FILE", "token.json")
    service = GoogleCalendarAuthenticator(credentials, token).build_service()
    calendar = GoogleCalendarSync(
        service,
        calendar_name=os.getenv("GOOGLE_STUDY_CALENDAR_NAME", "Attendr Study Plan"),
        app_timezone=os.getenv("APP_TIMEZONE", "America/Toronto"),
        source_tag="study_plan",
    )
    calendar_id, _ = calendar.resolve_calendar()
    start = datetime.now(UTC) + timedelta(minutes=1)
    end = start + timedelta(minutes=30)
    session_id = f"attendr:study:{secrets.token_hex(10)}:999"
    event = service.events().insert(
        calendarId=calendar_id,
        body={
            "summary": "Attendr button test",
            "description": "Temporary end-to-end test. Press Session complete.",
            "start": {"dateTime": start.isoformat(), "timeZone": "America/Toronto"},
            "end": {"dateTime": end.isoformat(), "timeZone": "America/Toronto"},
            "transparency": "opaque",
            "extendedProperties": {
                "private": {
                    "canvas_uid": session_id,
                    "attendr_source": "study_plan",
                }
            },
        },
    ).execute()
    event_id = str(event["id"])
    headers = {"Authorization": f"Bearer {secret}"}
    try:
        sync = requests.post(
            f"{worker}/api/sessions/sync",
            headers=headers,
            json={
                "replace": False,
                "sessions": [{
                    "session_id": session_id,
                    "task_uid": "attendr:test:button-controls",
                    "title": "Verify Attendr's Discord buttons",
                    "course_name": "Attendr setup",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "task_due_at": (start + timedelta(days=1)).isoformat(),
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                }],
            },
            timeout=30,
        )
        sync.raise_for_status()
        reminder = requests.post(
            f"{worker}/api/reminders/run", headers=headers, timeout=30
        )
        reminder.raise_for_status()
    except requests.RequestException:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        print("The live button test failed; its temporary event was removed.", file=sys.stderr)
        return 1
    print("Test alert sent to #study-sessions. Press Session complete to finish the test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
