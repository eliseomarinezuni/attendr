#!/usr/bin/env python3
"""Upload Attendr configuration to GitHub Actions without printing secret values."""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", required=True, help="GitHub repository as OWNER/NAME")
    return result


def set_secret(repository: str, name: str, value: str) -> None:
    try:
        subprocess.run(
            ["gh", "secret", "set", name, "--repo", repository],
            input=value.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Could not set GitHub secret {name}.") from error
    print(f"Set {name}")


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if shutil.which("gh") is None:
        print("GitHub CLI (gh) is not installed.", file=sys.stderr)
        return 1

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        print(".env was not found.", file=sys.stderr)
        return 1
    configuration = dotenv_values(env_path)

    required = (
        "CANVAS_BASE_URL",
        "CANVAS_API_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "GEMINI_API_KEY",
    )
    missing = [
        name for name in required if not str(configuration.get(name) or "").strip()
    ]
    if missing:
        print(f"Missing .env values: {', '.join(missing)}", file=sys.stderr)
        return 1

    secrets = {name: str(configuration[name]).strip() for name in required}
    for optional in (
        "GOOGLE_CALENDAR_NAME",
        "GOOGLE_STUDY_CALENDAR_NAME",
        "DAILY_QUIZ_TOPIC",
        "STUDY_WORKER_URL",
        "STUDY_SYNC_SECRET",
        "DISCORD_BOT_TOKEN",
        "DISCORD_ANNOUNCEMENTS_CHANNEL_ID",
        "DISCORD_CALENDAR_CHANNEL_ID",
        "DISCORD_QUIZ_CHANNEL_ID",
    ):
        value = str(configuration.get(optional) or "").strip()
        if value:
            secrets[optional] = value

    for env_name, default_name, secret_name in (
        ("GOOGLE_CREDENTIALS_FILE", "credentials.json", "GOOGLE_CREDENTIALS_B64"),
        ("GOOGLE_TOKEN_FILE", "token.json", "GOOGLE_TOKEN_B64"),
    ):
        raw_path = Path(str(configuration.get(env_name) or default_name)).expanduser()
        path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
        if not path.is_file():
            print(f"Required OAuth file was not found: {path}", file=sys.stderr)
            return 1
        secrets[secret_name] = base64.b64encode(path.read_bytes()).decode("ascii")

    try:
        for name, value in secrets.items():
            set_secret(arguments.repo, name, value)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Configured {len(secrets)} encrypted repository secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
