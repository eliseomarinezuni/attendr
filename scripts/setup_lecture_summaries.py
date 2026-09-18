#!/usr/bin/env python3
"""Create/reuse private #lecture-summaries and save its ID without posting."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parents[1]
API = "https://discord.com/api/v10"
CHANNEL_NAME = "lecture-summaries"
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16
CHANNEL_ACCESS = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | READ_MESSAGE_HISTORY


def setup(
    token: str,
    guild_id: str,
    owner_id: str,
    *,
    session: Any = requests,
) -> str:
    """Create or harden the dedicated channel and return its Discord ID."""

    def call(method: str, path: str, **kwargs: Any) -> Any:
        response = session.request(
            method,
            f"{API}{path}",
            headers={"Authorization": f"Bot {token}"},
            timeout=20,
            **kwargs,
        )
        if not response.ok:
            try:
                payload = response.json()
                code = str(payload.get("code", "")) if isinstance(payload, dict) else ""
                message = str(payload.get("message", ""))[:160] if isinstance(payload, dict) else ""
            except ValueError:
                code = message = ""
            detail = ": ".join(value for value in (code, message) if value)
            raise RuntimeError(
                f"Discord channel setup failed (HTTP {response.status_code})"
                + (f": {detail}" if detail else "")
            )
        return response.json()

    channels = call("GET", f"/guilds/{guild_id}/channels")
    existing = [
        channel
        for channel in channels
        if channel.get("name") == CHANNEL_NAME and channel.get("type") == 0
    ]
    if len(existing) > 1:
        raise RuntimeError(
            "Multiple #lecture-summaries channels exist; rename duplicates before setup"
        )
    bot = call("GET", "/users/@me")
    configuration = {
        "name": CHANNEL_NAME,
        "topic": ("Source-grounded Attendr teaching summaries generated after completed lectures."),
        "permission_overwrites": [
            {"id": guild_id, "type": 0, "deny": str(VIEW_CHANNEL), "allow": "0"},
            {
                "id": owner_id,
                "type": 1,
                "allow": str(CHANNEL_ACCESS),
                "deny": "0",
            },
            {
                "id": str(bot["id"]),
                "type": 1,
                "allow": str(CHANNEL_ACCESS),
                "deny": "0",
            },
        ],
    }
    if existing:
        channel = call("PATCH", f"/channels/{existing[0]['id']}", json=configuration)
    else:
        channel = call(
            "POST",
            f"/guilds/{guild_id}/channels",
            json={"type": 0, **configuration},
        )
    return str(channel["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild-id", required=True)
    arguments = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    required = ("DISCORD_BOT_TOKEN", "DISCORD_OWNER_USER_ID")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        print(f"Missing configuration: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not arguments.guild_id.isdigit() or not os.environ["DISCORD_OWNER_USER_ID"].isdigit():
        print("Discord IDs must be numeric", file=sys.stderr)
        return 1
    try:
        channel_id = setup(
            os.environ["DISCORD_BOT_TOKEN"],
            arguments.guild_id,
            os.environ["DISCORD_OWNER_USER_ID"],
        )
        set_key(str(ROOT / ".env"), "DISCORD_LECTURE_SUMMARIES_CHANNEL_ID", channel_id)
        (ROOT / ".env").chmod(0o600)
    except (RuntimeError, requests.RequestException) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        "#lecture-summaries configured with owner/bot-only access; "
        "channel ID saved locally; no messages sent."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
