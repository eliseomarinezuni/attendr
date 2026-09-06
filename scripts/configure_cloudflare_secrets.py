#!/usr/bin/env python3
"""Configure Attendr Worker secrets without displaying credential values."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import dotenv_values, set_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = PROJECT_ROOT / "worker"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--worker-url", required=True)
    result.add_argument("--guild-id", required=True)
    result.add_argument("--channel-id", required=True)
    return result


def required(configuration: dict[str, str | None], name: str) -> str:
    value = str(configuration.get(name) or "").strip()
    if not value or value.startswith("replace_with"):
        raise RuntimeError(f"Missing {name} in .env.")
    return value


def json_file(configuration: dict[str, str | None], env_name: str, fallback: str) -> dict:
    raw = Path(str(configuration.get(env_name) or fallback)).expanduser()
    path = raw if raw.is_absolute() else PROJECT_ROOT / raw
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read {path.name}.") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} does not contain a JSON object.")
    return value


def discord_owner(bot_token: str, guild_id: str) -> str:
    response = requests.get(
        f"https://discord.com/api/v10/guilds/{guild_id}",
        headers={"Authorization": f"Bot {bot_token}"},
        timeout=20,
    )
    if response.status_code != 200:
        raise RuntimeError(
            "Discord could not confirm the server owner. Verify that Attendr is installed."
        )
    owner_id = str(response.json().get("owner_id") or "").strip()
    if not owner_id:
        raise RuntimeError("Discord did not return a server owner ID.")
    return owner_id


def put_secret(name: str, value: str) -> None:
    try:
        subprocess.run(
            ["npx", "wrangler", "secret", "put", name],
            cwd=WORKER_ROOT,
            input=value,
            text=True,
            stdout=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"Could not upload Cloudflare secret {name}.") from error
    print(f"Set {name}")


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    env_path = PROJECT_ROOT / ".env"
    configuration = dict(dotenv_values(env_path))
    try:
        parsed = urlparse(arguments.worker_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("--worker-url must be an HTTPS deployment URL.")
        if not arguments.guild_id.isdigit() or not arguments.channel_id.isdigit():
            raise RuntimeError("Discord IDs must be numeric.")
        bot_token = required(configuration, "DISCORD_BOT_TOKEN")
        owner_id = discord_owner(bot_token, arguments.guild_id)
        oauth_client = json_file(
            configuration, "GOOGLE_CREDENTIALS_FILE", "credentials.json"
        ).get("installed", {})
        oauth_token = json_file(
            configuration, "GOOGLE_TOKEN_FILE", "token.json"
        )
        if not isinstance(oauth_client, dict):
            raise RuntimeError("credentials.json is not a Desktop app credential.")

        sync_secret = str(configuration.get("STUDY_SYNC_SECRET") or "").strip()
        if not sync_secret:
            sync_secret = secrets.token_urlsafe(48)

        local_values = {
            "DISCORD_STUDY_CHANNEL_ID": arguments.channel_id,
            "DISCORD_OWNER_USER_ID": owner_id,
            "STUDY_SYNC_SECRET": sync_secret,
            "STUDY_WORKER_URL": arguments.worker_url.rstrip("/"),
        }
        cloudflare_values = {
            "DISCORD_APPLICATION_ID": required(configuration, "DISCORD_APPLICATION_ID"),
            "DISCORD_PUBLIC_KEY": required(configuration, "DISCORD_PUBLIC_KEY"),
            "DISCORD_BOT_TOKEN": bot_token,
            "DISCORD_STUDY_CHANNEL_ID": arguments.channel_id,
            "DISCORD_OWNER_USER_ID": owner_id,
            "STUDY_SYNC_SECRET": sync_secret,
            "GOOGLE_CLIENT_ID": str(oauth_client.get("client_id") or ""),
            "GOOGLE_CLIENT_SECRET": str(oauth_client.get("client_secret") or ""),
            "GOOGLE_REFRESH_TOKEN": str(oauth_token.get("refresh_token") or ""),
        }
        for name, value in cloudflare_values.items():
            if not value:
                raise RuntimeError(f"Google OAuth data is missing {name}.")
        for name, value in cloudflare_values.items():
            put_secret(name, value)
        for name, value in local_values.items():
            set_key(str(env_path), name, value, quote_mode="always")
        env_path.chmod(0o600)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    print("Cloudflare secrets configured; no values were displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
