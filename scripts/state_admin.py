#!/usr/bin/env python3
"""Migrate legacy state, back up SQLite, and reconcile uncertain deliveries."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from academic_assistant.state_store import StateStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.getenv("ATTENDR_DB", "data/attendr.db")))
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--legacy-directory", type=Path, default=Path("data"))
    sub.add_parser("list")
    sub.add_parser("maintain", help="Evict disposable payloads while preserving delivery markers")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("event_key")
    resolve.add_argument("fingerprint")
    resolve.add_argument(
        "--as",
        dest="status",
        choices=["sent", "pending"],
        required=True,
        help="Use sent after confirming delivery, or pending after confirming absence.",
    )
    retire = sub.add_parser(
        "retire-assignment", help="Retire a confirmed deleted tracked assignment"
    )
    retire.add_argument("uid")
    restore = sub.add_parser("restore-assignment", help="Resume tracking a retired assignment")
    restore.add_argument("uid")
    backup = sub.add_parser("backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command != "migrate" and not args.db.exists():
        parser.error("Database does not exist; migrate first")
    store = StateStore(args.db)
    if args.command == "migrate":
        for name in ("seen_ids.json", "notification_state.db"):
            path = args.legacy_directory / name
            if path.resolve() != store.path:
                store.migrate_notifications(path)
        store.migrate_quizzes(args.legacy_directory / "lecture_quiz_state.json")
        print("Migration complete; legacy files preserved.")
    elif args.command == "maintain":
        store.maintain()
        print("State maintenance complete.")
    elif args.command == "list":
        with store.connect() as db:
            for row in db.execute(
                "SELECT event_key,fingerprint,status,attempts,last_error FROM delivery_outbox WHERE status!='sent'"
            ):
                print(json.dumps(dict(row)))
    elif args.command in {"retire-assignment", "restore-assignment"}:
        store.retire_assignment(args.uid, restore=args.command == "restore-assignment")
        print("Assignment tracking updated; Calendar reconciles on the next run.")
    elif args.command == "resolve":
        with store.connect() as db:
            changed = db.execute(
                "UPDATE delivery_outbox SET status=?,lease_token=NULL,lease_until=NULL,last_error=NULL WHERE event_key=? AND fingerprint=? AND status IN ('uncertain','failed')",
                (args.status, args.event_key, args.fingerprint),
            )
            if changed.rowcount != 1:
                raise ValueError("No matching uncertain or failed delivery")
            if args.status == "sent":
                store._mark_sent(db, args.event_key, args.fingerprint)
    else:
        if args.destination.exists():
            raise ValueError("Backup destination already exists")
        descriptor = os.open(args.destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with store.connect() as source, sqlite3.connect(args.destination) as target:
            source.backup(target)
        print("Backup complete.")


if __name__ == "__main__":
    main()
