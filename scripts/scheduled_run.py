#!/usr/bin/env python3
"""Run with persistent private configuration and an exclusive process lock."""

import fcntl
import os
from pathlib import Path
import subprocess
import sys
from dotenv import load_dotenv

root = Path(__file__).resolve().parents[1]
raw_home = os.getenv("ATTENDR_HOME", "")
if not raw_home or not Path(raw_home).is_absolute():
    raise SystemExit("Set ATTENDR_HOME to an absolute persistent directory outside the checkout.")
home = Path(raw_home).resolve()
if home == root or root in home.parents or not (home / ".env").is_file():
    raise SystemExit("ATTENDR_HOME must contain .env and be outside the repository checkout.")
load_dotenv(home / ".env", override=False)
for key, name in {
    "ATTENDR_DB": "attendr.db",
    "GOOGLE_CREDENTIALS_FILE": "credentials.json",
    "GOOGLE_TOKEN_FILE": "token.json",
    "CANVAS_MATERIALS_DIR": "materials",
    "CANVAS_MATERIALS_INDEX": "materials_index.json",
    "ANNOUNCEMENT_DATES_INDEX": "announcement_dates_index.json",
}.items():
    os.environ[key] = str(home / name)
if not (home / "attendr.db").is_file():
    raise SystemExit(
        "Database missing. Run scripts/state_admin.py --db PATH migrate before enabling schedules."
    )
with (home / "run.lock").open("a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another Attendr run holds the persistent state lock.")
    raise SystemExit(
        subprocess.call([sys.executable, str(root / "main.py"), *sys.argv[1:]], cwd=root)
    )
