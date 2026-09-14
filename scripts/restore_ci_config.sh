#!/usr/bin/env bash
set -euo pipefail

python - <<'PY'
import base64
import binascii
import json
import os
import tempfile
from pathlib import Path


def restore(name: str, destination: str, *, required: bool, kind: str) -> None:
    encoded = os.environ.get(name, "")
    if not encoded:
        if required:
            raise SystemExit(f"{name} is required for this scheduled job.")
        return
    try:
        raw = base64.b64decode(encoded, validate=True)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        if kind == "credentials":
            client = value.get("installed") or value.get("web")
            if not isinstance(client, dict) or not all(
                client.get(key) for key in ("client_id", "client_secret", "auth_uri", "token_uri")
            ):
                raise ValueError
        elif kind == "token" and not all(value.get(key) for key in ("refresh_token", "client_id", "client_secret", "token_uri")):
            raise ValueError
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        expected = "Google OAuth JSON" if kind in {"credentials", "token"} else "private JSON configuration"
        raise SystemExit(f"{name} does not contain valid {expected}.") from None

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


calendar_required = os.environ.get("REQUIRE_GOOGLE_CALENDAR_AUTH", "").lower() == "true"
restore("GOOGLE_CREDENTIALS_B64", "credentials.json", required=calendar_required, kind="credentials")
restore("GOOGLE_TOKEN_B64", "token.json", required=calendar_required, kind="token")
restore("GOOGLE_SLIDES_TOKEN_B64", "google_slides_token.json", required=False, kind="token")
restore("COURSE_SCHEDULE_B64", "data/course_schedule.json", required=True, kind="json")
restore("ATTENDR_PREFERENCES_B64", "data/preferences.json", required=True, kind="json")
PY

cat > .env <<EOF
CANVAS_BASE_URL=${CANVAS_BASE_URL}
CANVAS_API_TOKEN=${CANVAS_API_TOKEN}
CANVAS_LOOKAHEAD_DAYS=30
CANVAS_ANNOUNCEMENT_DAYS=14
APP_TIMEZONE=America/Toronto
CANVAS_MATERIALS_DIR=data/materials
CANVAS_MATERIALS_INDEX=data/materials_index.json
CANVAS_MATERIAL_MAX_MB=25
CANVAS_MATERIAL_FUTURE_DAYS=550
COURSE_SCHEDULE_FILE=data/course_schedule.json
ATTENDR_PREFERENCES_FILE=data/preferences.json
ANNOUNCEMENT_DATES_INDEX=data/announcement_dates_index.json
LECTURE_QUIZ_STATE_FILE=data/lecture_quiz_state.json
LECTURE_QUIZ_RETRY_HOURS=336
LECTURE_MAX_FILE_MB=768
DISCORD_WEBHOOK_URL=${DISCORD_WEBHOOK_URL}
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
DISCORD_ANNOUNCEMENTS_CHANNEL_ID=${DISCORD_ANNOUNCEMENTS_CHANNEL_ID}
DISCORD_CALENDAR_CHANNEL_ID=${DISCORD_CALENDAR_CHANNEL_ID}
DISCORD_QUIZ_CHANNEL_ID=${DISCORD_QUIZ_CHANNEL_ID}
GOOGLE_CREDENTIALS_FILE=credentials.json
GOOGLE_TOKEN_FILE=token.json
GOOGLE_SLIDES_TOKEN_FILE=google_slides_token.json
GOOGLE_CALENDAR_ID=${GOOGLE_CALENDAR_ID:-}
GOOGLE_CALENDAR_NAME=${GOOGLE_CALENDAR_NAME:-Attendr}
GOOGLE_CREATE_CALENDAR_IF_MISSING=true
GOOGLE_MARK_SUBMITTED=true
GOOGLE_STUDY_CALENDAR_NAME=${GOOGLE_STUDY_CALENDAR_NAME:-Attendr Study Plan}
STUDY_WORKER_URL=${STUDY_WORKER_URL}
STUDY_SYNC_SECRET=${STUDY_SYNC_SECRET}
GEMINI_API_KEY=${GEMINI_API_KEY}
GEMINI_MODEL=gemini-3.6-flash
DAILY_QUIZ_TOPIC=${DAILY_QUIZ_TOPIC:-}
DAILY_TOPICS_FILE=data/daily_topics.json
EOF
chmod 600 .env
