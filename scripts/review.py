#!/usr/bin/env python3
"""List or grade spaced-repetition cards locally; no external requests."""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from academic_assistant.review import ReviewQueue
from academic_assistant.state_store import StateStore


def main():
    load_dotenv(Path(os.getenv("ATTENDR_HOME", str(ROOT))) / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    default_db = (
        Path(os.environ["ATTENDR_HOME"]) / "attendr.db"
        if os.getenv("ATTENDR_HOME")
        else ROOT / Path(os.getenv("ATTENDR_DB", "data/attendr.db")).expanduser()
    )
    parser.add_argument("--db", type=Path, default=default_db)

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    grade = commands.add_parser("grade")
    grade.add_argument("card_id")
    grade.add_argument("rating", choices=["again", "good", "easy"])
    grade.add_argument("--revision", type=int, required=True)
    args = parser.parse_args()
    if not args.db.exists():
        parser.error("Database does not exist; use the same database as the scheduled runner")
    store = StateStore(args.db)
    with store.run_lock():
        queue = ReviewQueue(store, os.getenv("APP_TIMEZONE", "America/Toronto"))
        today = datetime.now(queue.timezone).date()
        queue.import_quizzes()
        if args.command == "list":
            for card in queue.due(today, 100):
                print(
                    f"{card['card_id']} revision={card['revision']} due={card['due_date']} {card['topic']}"
                )
        else:
            try:
                days = queue.grade(args.card_id, args.rating, args.revision, today)
            except ValueError as error:
                parser.error(str(error))
            print(f"Next review in {days} day(s).")


if __name__ == "__main__":
    main()
