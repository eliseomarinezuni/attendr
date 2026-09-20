#!/usr/bin/env python3
"""Run Attendr on an ephemeral host with an encrypted D1 state checkpoint."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
import signal

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from academic_assistant.cloud_state import CloudStateClient, CloudStateError


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CloudStateError(f"{name} must be configured")
    return value


def heartbeat(url: str, secret: str, name: str, status: str) -> None:
    try:
        response = requests.post(
            f"{url}/api/automation/heartbeat",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "name": name,
                "status": status,
                "run_id": os.getenv("GITHUB_RUN_ID", secrets.token_hex(8)),
            },
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        # Heartbeats improve recovery but must not prevent the actual assistant run.
        print(f"Automation heartbeat failed: {type(error).__name__}", file=sys.stderr)


def recorded_status(database: Path, exit_code: int) -> str:
    if exit_code != 0 or not database.is_file():
        return "failure"
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT status FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return row[0] if row and row[0] in {"success", "ok", "degraded"} else "success"
    except sqlite3.Error:
        return "success"


def run_with_lease(client: CloudStateClient, command: list[str]) -> int:
    """Renew independently of child writes; terminate the entire run on lease loss."""
    client.request("POST", "/api/state-store/renew")
    process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
    next_renewal = time.monotonic() + 300
    try:
        while True:
            try:
                return process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                if time.monotonic() >= next_renewal:
                    client.request("POST", "/api/state-store/renew")
                    next_renewal = time.monotonic() + 300
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        raise


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    url = required("STUDY_WORKER_URL").rstrip("/")
    secret = required("STUDY_SYNC_SECRET")
    key = required("ATTENDR_STATE_KEY")
    if secret == key:
        raise CloudStateError("ATTENDR_STATE_KEY must be independent from STUDY_SYNC_SECRET")
    token = secrets.token_hex(16)
    headers = {"Authorization": f"Bearer {secret}"}
    response = requests.post(
        f"{url}/api/state-store/acquire", headers=headers, json={"token": token}, timeout=30
    )
    if response.status_code == 409:
        raise CloudStateError("Another scheduled Attendr run holds the state lease")
    response.raise_for_status()
    revision = int(response.json()["revision"])
    database = Path(os.getenv("RUNNER_TEMP", "/tmp")) / "attendr.db"
    client = CloudStateClient(url, secret, key, token, revision)
    automation_name = (
        "lecture-quizzes"
        if {"--lecture-quizzes", "--lecture-summaries"} & set(sys.argv[1:])
        else "academic"
    )
    final_status = "failure"
    heartbeat(url, secret, automation_name, "started")
    try:
        restored = client.download(database)
        os.environ.update(
            {
                "ATTENDR_DB": str(database),
                "ATTENDR_STATE_URL": url,
                "ATTENDR_STATE_SECRET": secret,
                "ATTENDR_STATE_KEY": key,
                "ATTENDR_STATE_LEASE": token,
                "ATTENDR_STATE_REVISION": str(client.revision),
            }
        )
        if not restored:
            print("Initializing the first encrypted state checkpoint")
        result = run_with_lease(client, [sys.executable, str(ROOT / "main.py"), *sys.argv[1:]])
        final_status = recorded_status(database, result)
        if final_status == "ok":
            final_status = "success"
        return result
    finally:
        heartbeat(url, secret, automation_name, final_status)
        try:
            requests.post(
                f"{url}/api/state-store/release",
                headers=headers,
                json={"token": token},
                timeout=30,
            ).raise_for_status()
        except requests.RequestException as error:
            print(
                f"State lease release failed; it will expire automatically: {error}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CloudStateError, requests.RequestException) as error:
        raise SystemExit(str(error)) from error
