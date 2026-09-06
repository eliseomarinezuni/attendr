"""Local-date review scheduling with explicit, revision-checked self-assessment."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo
from .state_store import StateStore


class ReviewQueue:
    def __init__(self, store: StateStore, timezone: str = "America/Toronto"):
        self.store = store
        self.timezone = ZoneInfo(timezone)

    def import_quizzes(self) -> int:
        added = 0
        with self.store.connect() as db:
            for quiz in db.execute(
                "SELECT * FROM quiz_history WHERE sent_at IS NOT NULL"
            ).fetchall():
                payload = json.loads(quiz["payload"])
                sent_at = datetime.fromisoformat(quiz["sent_at"].replace("Z", "+00:00"))
                if sent_at.tzinfo is None:
                    from datetime import timezone

                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                due = sent_at.astimezone(self.timezone).date() + timedelta(days=1)
                for index, embed in enumerate(payload.get("embeds", [])):
                    card_id = sha256(f"{quiz['quiz_key']}:{index}".encode()).hexdigest()[:20]
                    added += db.execute(
                        "INSERT OR IGNORE INTO review_cards(card_id,quiz_key,embed,topic,due_date) VALUES(?,?,?,?,?)",
                        (
                            card_id,
                            quiz["quiz_key"],
                            json.dumps(embed),
                            payload.get("content", "Quiz review"),
                            due.isoformat(),
                        ),
                    ).rowcount
        return added

    def due(self, today: date, limit: int = 3) -> list[dict]:
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM review_cards WHERE due_date<=? ORDER BY due_date,card_id LIMIT ?",
                    (today.isoformat(), limit),
                )
            ]

    def grade(self, card_id: str, rating: str, revision: int, today: date) -> int:
        if rating not in {"again", "good", "easy"}:
            raise ValueError("Rating must be again, good, or easy")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM review_cards WHERE card_id=?", (card_id,)).fetchone()
            if not row or row["revision"] != revision or row["due_date"] > today.isoformat():
                raise ValueError(
                    "Card is not due, does not exist, or this review was already graded"
                )
            interval = (
                1
                if rating == "again"
                else (
                    max(3, row["interval_days"] * 2)
                    if rating == "good"
                    else max(7, row["interval_days"] * 3)
                )
            )
            interval = min(interval, 180)
            # Grading makes unsent reminders for the old revision obsolete.
            db.execute(
                "DELETE FROM delivery_outbox WHERE status='pending' AND event_key LIKE ?",
                (f"lecture_quizzes:custom:review:%:{card_id}:{revision}:revision:%",),
            )
            db.execute(
                "INSERT INTO review_history VALUES(?,?,?,?,?)",
                (card_id, revision, rating, today.isoformat(), interval),
            )
            db.execute(
                "UPDATE review_cards SET due_date=?,interval_days=?,revision=revision+1 WHERE card_id=?",
                ((today + timedelta(days=interval)).isoformat(), interval, card_id),
            )
        return interval

    def send_due(self, notifier, today: date, limit: int = 3, *, force: bool = False) -> int:
        self.import_quizzes()
        sent = 0
        # A fixed daily selection prevents repeated scheduler runs from exceeding the daily cap.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            selection = db.execute(
                "SELECT payload FROM review_batches WHERE local_date=?", (today.isoformat(),)
            ).fetchone()
            if selection:
                cards = json.loads(selection[0])
            else:
                cards = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM review_cards WHERE due_date<=? ORDER BY due_date,card_id LIMIT ?",
                        (today.isoformat(), limit),
                    )
                ]
                if not force:
                    db.execute(
                        "INSERT INTO review_batches VALUES(?,?)",
                        (today.isoformat(), json.dumps(cards)),
                    )
        for card in cards:
            with self.store.connect() as db:
                current = db.execute(
                    "SELECT revision FROM review_cards WHERE card_id=?", (card["card_id"],)
                ).fetchone()
            if current[0] != card["revision"]:
                continue
            command = f"python scripts/review.py grade {card['card_id']} good --revision {card['revision']}"
            payload = {
                "content": f"🧠 Spaced review — try answering before revealing the solution.\nGrade with `again`, `good`, or `easy`:\n`{command}`",
                "embeds": [json.loads(card["embed"])],
                "allowed_mentions": {"parse": []},
            }
            sent += notifier.send_custom_notification(
                f"review:{today}:{card['card_id']}:{card['revision']}",
                payload,
                destination="lecture_quizzes",
                fixed_fingerprint="review",
                force=force,
            )
        return sent
