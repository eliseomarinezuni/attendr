#!/usr/bin/env python3
"""Safely rotate the encryption key for Attendr's remote SQLite checkpoint."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant.cloud_state import (  # noqa: E402
    CloudStateClient,
    CloudStateError,
    rotate_checkpoint,
)


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CloudStateError(f"{name} must be configured")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="New private file that will retain the old encrypted checkpoint payload.",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    url = required("STUDY_WORKER_URL").rstrip("/")
    auth_secret = required("STUDY_SYNC_SECRET")
    old_key = required("ATTENDR_STATE_OLD_KEY")
    new_key = required("ATTENDR_STATE_KEY")
    if old_key == new_key:
        raise CloudStateError("Old and new state encryption keys must be different")

    token = secrets.token_hex(16)
    headers = {"Authorization": f"Bearer {auth_secret}"}
    acquired = False
    try:
        response = requests.post(
            f"{url}/api/state-store/acquire",
            headers=headers,
            json={"token": token},
            timeout=30,
        )
        if response.status_code == 409:
            raise CloudStateError("Another Attendr run holds the state lease")
        response.raise_for_status()
        acquired = True
        revision = int(response.json()["revision"])
        old_client = CloudStateClient(url, auth_secret, old_key, token, revision)
        new_client = CloudStateClient(url, auth_secret, new_key, token, revision)
        changed = rotate_checkpoint(
            old_client,
            new_client,
            backup_path=arguments.backup.expanduser().resolve(),
        )
        print(
            "State key rotation completed and verified."
            if changed
            else "Checkpoint is already encrypted with the new state key."
        )
        return 0
    finally:
        if acquired:
            try:
                requests.post(
                    f"{url}/api/state-store/release",
                    headers=headers,
                    json={"token": token},
                    timeout=30,
                ).raise_for_status()
            except requests.RequestException as error:
                print(
                    f"State lease release failed; it will expire automatically: "
                    f"{type(error).__name__}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CloudStateError, requests.RequestException, ValueError) as error:
        raise SystemExit(str(error)) from error
