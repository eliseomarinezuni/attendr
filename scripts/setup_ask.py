#!/usr/bin/env python3
"""Create/reuse #ask and upsert /ask without replacing other guild commands."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parents[1]
API = "https://discord.com/api/v10"
COMMAND = {
    "name": "ask", "type": 1, "description": "Ask a question about one of your courses",
    "options": [{"name": "question", "description": "For my web development course, when’s my midterm?",
                 "type": 3, "required": True, "min_length": 1, "max_length": 1000}],
}


def setup(token: str, application_id: str, guild_id: str, owner_id: str, session=requests) -> str:
    def call(method: str, path: str, **kwargs):
        response = session.request(method, f"{API}{path}", headers={"Authorization": f"Bot {token}"},
                                   timeout=20, **kwargs)
        if not response.ok:
            try:
                payload = response.json()
                code = str(payload.get("code", "")) if isinstance(payload, dict) else ""
                message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
            except ValueError:
                code = message = ""
            detail = ": ".join(value for value in (code, message[:160]) if value)
            raise RuntimeError(
                f"Discord setup failed (HTTP {response.status_code})"
                + (f": {detail}" if detail else "")
            )
        return response.json()

    channels = call("GET", f"/guilds/{guild_id}/channels")
    existing = [c for c in channels if c.get("name") == "ask" and c.get("type") == 0]
    if len(existing) > 1:
        raise RuntimeError("Multiple #ask channels exist; rename the duplicates before setup")
    if existing:
        channel = existing[0]
    else:
        bot = call("GET", "/users/@me")
        channel = call("POST", f"/guilds/{guild_id}/channels", json={
            "name": "ask", "type": 0, "topic": "Ask Attendr about your courses with /ask and one natural-language question.",
            "permission_overwrites": [
                {"id": guild_id, "type": 0, "deny": "1024", "allow": "0"},
                {"id": owner_id, "type": 1, "allow": str(1024 | 2048 | 65536 | 2147483648), "deny": "0"},
                {"id": bot["id"], "type": 1, "allow": str(1024 | 2048 | 65536), "deny": "0"},
            ],
        })
    # POST with the same name/type upserts just this command; never bulk-overwrite.
    call("POST", f"/applications/{application_id}/guilds/{guild_id}/commands", json=COMMAND)
    return str(channel["id"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--upload-worker-secrets", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    required = ["DISCORD_BOT_TOKEN", "DISCORD_APPLICATION_ID", "DISCORD_OWNER_USER_ID"]
    if args.upload_worker_secrets:
        required.append("GEMINI_API_KEY")
    if any(not os.getenv(k, "").strip() for k in required):
        print("Missing configuration: " + ", ".join(k for k in required if not os.getenv(k, "").strip()), file=sys.stderr)
        return 1
    if not all(v.isdigit() for v in [args.guild_id, os.environ["DISCORD_APPLICATION_ID"], os.environ["DISCORD_OWNER_USER_ID"]]):
        print("Discord IDs must be numeric", file=sys.stderr)
        return 1
    try:
        channel = setup(os.environ["DISCORD_BOT_TOKEN"], os.environ["DISCORD_APPLICATION_ID"],
                        args.guild_id, os.environ["DISCORD_OWNER_USER_ID"])
        set_key(str(ROOT / ".env"), "DISCORD_ASK_CHANNEL_ID", channel)
        (ROOT / ".env").chmod(0o600)
        if args.upload_worker_secrets:
            from configure_cloudflare_secrets import put_secret
            put_secret("DISCORD_ASK_CHANNEL_ID", channel)
            put_secret("GEMINI_API_KEY", os.environ["GEMINI_API_KEY"])
            put_secret("GEMINI_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.6-flash"))
    except (RuntimeError, requests.RequestException) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("#ask and /ask configured. Channel ID saved in .env; no messages were sent.")
    if not args.upload_worker_secrets:
        print("Upload DISCORD_ASK_CHANNEL_ID and GEMINI_API_KEY to Worker secrets before using /ask.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
