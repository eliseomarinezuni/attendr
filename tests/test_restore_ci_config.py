from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/restore_ci_config.sh"


def encoded(value: dict[str, object]) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def environment() -> dict[str, str]:
    values = {
        "CANVAS_BASE_URL": "https://canvas.example.edu",
        "CANVAS_API_TOKEN": "test-canvas-value",
        "DISCORD_WEBHOOK_URL": "",
        "DISCORD_BOT_TOKEN": "test-discord-value",
        "DISCORD_ANNOUNCEMENTS_CHANNEL_ID": "1",
        "DISCORD_CALENDAR_CHANNEL_ID": "2",
        "DISCORD_QUIZ_CHANNEL_ID": "3",
        "STUDY_WORKER_URL": "https://worker.example.test",
        "STUDY_SYNC_SECRET": "test-worker-value",
        "GEMINI_API_KEY": "test-gemini-value",
        "REQUIRE_GOOGLE_CALENDAR_AUTH": "true",
    }
    values["GOOGLE_CREDENTIALS_B64"] = encoded(
        {
            "installed": {
                "client_id": "test-client-id",
                "client_secret": "test-client-value",
                "auth_uri": "https://accounts.example.test/auth",
                "token_uri": "https://accounts.example.test/token",
            }
        }
    )
    values["GOOGLE_TOKEN_B64"] = encoded(
        {
            "refresh_token": "test-refresh-value",
            "client_id": "test-client-id",
            "client_secret": "test-client-value",
            "token_uri": "https://accounts.example.test/token",
        }
    )
    return {**os.environ, **values}


def run_restore(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_valid_google_oauth_files_are_private_and_not_printed(tmp_path):
    env = environment()
    result = run_restore(tmp_path, env)
    assert result.returncode == 0
    assert result.stdout == ""
    assert "test-refresh-value" not in result.stderr
    assert (
        json.loads((tmp_path / "token.json").read_text())["refresh_token"] == "test-refresh-value"
    )
    assert (tmp_path / "token.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_invalid_base64_fails_without_writing_token(tmp_path):
    env = environment()
    env["GOOGLE_TOKEN_B64"] = "not valid base64!"
    result = run_restore(tmp_path, env)
    assert result.returncode != 0
    assert "does not contain valid Google OAuth JSON" in result.stderr
    assert not (tmp_path / "token.json").exists()


def test_invalid_json_fails_without_echoing_decoded_value(tmp_path):
    env = environment()
    invalid = "not-json-private-value"
    env["GOOGLE_TOKEN_B64"] = base64.b64encode(invalid.encode()).decode()
    result = run_restore(tmp_path, env)
    assert result.returncode != 0
    assert invalid not in result.stdout + result.stderr
    assert not (tmp_path / "token.json").exists()


def test_invalid_credentials_json_fails_before_pipeline(tmp_path):
    env = environment()
    env["GOOGLE_CREDENTIALS_B64"] = encoded({"installed": {"client_id": "incomplete"}})
    result = run_restore(tmp_path, env)
    assert result.returncode != 0
    assert "GOOGLE_CREDENTIALS_B64 does not contain valid Google OAuth JSON" in result.stderr
    assert not (tmp_path / "credentials.json").exists()


def test_missing_required_token_fails_immediately(tmp_path):
    env = environment()
    env.pop("GOOGLE_TOKEN_B64")
    result = run_restore(tmp_path, env)
    assert result.returncode != 0
    assert "GOOGLE_TOKEN_B64 is required" in result.stderr
    assert not (tmp_path / "token.json").exists()
