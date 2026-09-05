#!/usr/bin/env python3
"""Set Attendr's Discord interactions endpoint without displaying credentials."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    worker_url = os.getenv("STUDY_WORKER_URL", "").strip().rstrip("/")
    if not token or not worker_url:
        print("DISCORD_BOT_TOKEN or STUDY_WORKER_URL is missing.", file=sys.stderr)
        return 1

    response = requests.patch(
        "https://discord.com/api/v10/applications/@me",
        headers={"Authorization": f"Bot {token}"},
        json={"interactions_endpoint_url": f"{worker_url}/interactions"},
        timeout=30,
    )
    if response.status_code != 200:
        try:
            detail = str(response.json().get("message") or "Unknown validation error")
        except ValueError:
            detail = "Unknown validation error"
        print(
            f"Discord rejected the interaction endpoint (HTTP {response.status_code}): {detail}",
            file=sys.stderr,
        )
        return 1
    configured = str(response.json().get("interactions_endpoint_url") or "")
    if configured != f"{worker_url}/interactions":
        print("Discord did not retain the interaction endpoint.", file=sys.stderr)
        return 1
    print("Discord interaction endpoint configured and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
