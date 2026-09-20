"""Transactional state with optional encrypted cloud checkpoints."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class StateStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)")
            version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if version is not None and version not in (1, 2, 3):
                raise ValueError(
                    "Unsupported Attendr database version; use the matching application version"
                )
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_migrations VALUES(1);
                CREATE TABLE IF NOT EXISTS sent_notifications(
                    event_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, sent_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS seen_announcements(
                    event_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, sent_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notification_versions(
                    event_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS delivery_outbox(
                    event_key TEXT NOT NULL, fingerprint TEXT NOT NULL, destination TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','sending','sent','uncertain','failed')),
                    lease_token TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    remote_id TEXT, last_error TEXT, created_at REAL NOT NULL,
                    PRIMARY KEY(event_key, fingerprint));
                CREATE TABLE IF NOT EXISTS synced_assignments(
                    calendar_id TEXT NOT NULL, uid TEXT NOT NULL, event_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, desired TEXT NOT NULL, action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(calendar_id, uid));
                CREATE TABLE IF NOT EXISTS quiz_history(
                    quiz_key TEXT PRIMARY KEY, payload TEXT NOT NULL, sent_at TEXT);
                CREATE TABLE IF NOT EXISTS extraction_cache(
                    cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imported_files(path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
                    status TEXT NOT NULL, detail TEXT);
            """)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS announcement_triage(
                    uid TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,category TEXT NOT NULL,
                    reason TEXT NOT NULL,muted INTEGER NOT NULL,triaged_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS review_cards(
                    card_id TEXT PRIMARY KEY,quiz_key TEXT NOT NULL,embed TEXT NOT NULL,
                    topic TEXT NOT NULL,due_date TEXT NOT NULL,
                    interval_days INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS idx_review_due ON review_cards(due_date,card_id);
                CREATE TABLE IF NOT EXISTS review_history(
                    card_id TEXT NOT NULL,revision INTEGER NOT NULL,rating TEXT NOT NULL,
                    graded_date TEXT NOT NULL,interval_days INTEGER NOT NULL,
                    PRIMARY KEY(card_id,revision),FOREIGN KEY(card_id) REFERENCES review_cards(card_id));
                CREATE TABLE IF NOT EXISTS review_batches(local_date TEXT PRIMARY KEY,payload TEXT NOT NULL);
                INSERT OR IGNORE INTO schema_migrations VALUES(2);
                COMMIT;
            """)
            if "expires_at" not in {
                row[1] for row in db.execute("PRAGMA table_info(delivery_outbox)")
            }:
                db.execute("ALTER TABLE delivery_outbox ADD COLUMN expires_at REAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS retired_assignments(uid TEXT PRIMARY KEY, retired_at REAL NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(3)")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self, *, checkpoint: bool = True) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        changed = False
        try:
            before = db.total_changes
            with db:
                yield db
            changed = db.total_changes != before
        finally:
            db.close()
        if changed and checkpoint:
            from .cloud_state import client_from_env

            client = client_from_env(self.path)
            if client is not None:
                client.upload(self.path)

    def was_sent(self, event_key: str, fingerprint: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM sent_notifications WHERE event_key=? AND fingerprint=?",
                    (event_key, fingerprint),
                ).fetchone()
                is not None
            )

    def mark_sent(self, event_key: str, fingerprint: str) -> None:
        with self.connect() as db:
            self._mark_sent(db, event_key, fingerprint)

    @staticmethod
    def _mark_sent(db: sqlite3.Connection, key: str, fingerprint: str) -> None:
        for table in (
            ("sent_notifications", "seen_announcements")
            if ":announcement:" in key
            else ("sent_notifications",)
        ):
            db.execute(
                f'INSERT OR REPLACE INTO {table} VALUES(?,?,datetime("now"))', (key, fingerprint)
            )

    def enqueue(
        self, key: str, fingerprint: str, destination: str, payload: dict[str, Any]
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO delivery_outbox(event_key,fingerprint,destination,payload,created_at) VALUES(?,?,?,?,?)",
                (key, fingerprint, destination, json.dumps(payload, sort_keys=True), time.time()),
            )

    def enqueue_messages(
        self,
        key: str,
        fingerprint: str,
        destination: str,
        messages: list[dict[str, Any]],
        *,
        expires_at: float | None = None,
    ) -> list[str]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT fingerprint,revision FROM notification_versions WHERE event_key=?", (key,)
            ).fetchone()
            revision = (
                previous["revision"]
                if previous and previous["fingerprint"] == fingerprint
                else (previous["revision"] + 1 if previous else 1)
            )
            db.execute(
                "INSERT OR REPLACE INTO notification_versions VALUES(?,?,?)",
                (key, fingerprint, revision),
            )
            keys = [f"{key}:revision:{revision}:part:{index}" for index in range(len(messages))]
            for part_key, message in zip(keys, messages):
                db.execute(
                    "INSERT OR IGNORE INTO delivery_outbox(event_key,fingerprint,destination,payload,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                    (
                        part_key,
                        fingerprint,
                        destination,
                        json.dumps(message, sort_keys=True),
                        time.time(),
                        expires_at,
                    ),
                )
                if expires_at is not None:
                    db.execute(
                        "UPDATE delivery_outbox SET expires_at=CASE WHEN expires_at IS NULL THEN ? ELSE MIN(expires_at,?) END WHERE event_key=? AND fingerprint=? AND status='pending'",
                        (expires_at, expires_at, part_key, fingerprint),
                    )
            return keys

    def claim(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        token = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A crashed sender may already have delivered. Never blindly re-send it.
            db.execute(
                "UPDATE delivery_outbox SET status='uncertain',last_error='Expired delivery lease' WHERE status='sending' AND lease_until<?",
                (now,),
            )
            row = db.execute(
                "UPDATE delivery_outbox SET status='sending',lease_token=?,lease_until=?,attempts=attempts+1 WHERE event_key=? AND fingerprint=? AND status='pending' RETURNING *",
                (token, now + 600, key, fingerprint),
            ).fetchone()
            return dict(row) if row else None

    def finish(
        self,
        row: dict[str, Any],
        status: str,
        remote_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as db:
            result = db.execute(
                "UPDATE delivery_outbox SET status=?,remote_id=?,last_error=?,lease_until=NULL WHERE event_key=? AND fingerprint=? AND lease_token=? AND status='sending'",
                (
                    status,
                    remote_id,
                    error,
                    row["event_key"],
                    row["fingerprint"],
                    row["lease_token"],
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("Delivery claim was lost")
            if status == "sent":
                self._mark_sent(db, row["event_key"], row["fingerprint"])

    def pending_deliveries(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            db.execute(
                "UPDATE delivery_outbox SET status='uncertain',last_error='Expired delivery lease' WHERE status='sending' AND lease_until<?",
                (time.time(),),
            )
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM delivery_outbox WHERE status='pending' ORDER BY created_at"
                )
            ]

    def discard_pending_delivery(self, key: str, fingerprint: str) -> bool:
        """Discard only an unclaimed retry; sending/uncertain rows remain untouched."""
        with self.connect() as db:
            result = db.execute(
                "DELETE FROM delivery_outbox WHERE event_key=? AND fingerprint=? AND status='pending'",
                (key, fingerprint),
            )
            return result.rowcount == 1

    def blocked_deliveries(self) -> int:
        with self.connect() as db:
            return db.execute(
                "SELECT count(*) FROM delivery_outbox WHERE status IN ('uncertain','failed')"
            ).fetchone()[0]

    def quiz(self, key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM quiz_history WHERE quiz_key=?", (key,)).fetchone()
            return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def save_quiz(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO quiz_history(quiz_key,payload) VALUES(?,?)",
                (key, json.dumps(payload)),
            )
        saved = self.quiz(key)
        assert saved is not None
        return saved["payload"]

    def complete_quiz(self, key: str) -> None:
        with self.connect() as db:
            db.execute('UPDATE quiz_history SET sent_at=datetime("now") WHERE quiz_key=?', (key,))

    def cache_get(self, key: str) -> Any:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM extraction_cache WHERE cache_key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _disposable_cache(key: str) -> bool:
        return key.startswith(("ask-extract-v1:", "lecture-text-v2:", "google-slides-text-v1:"))

    def cache_set(self, key: str, payload: Any) -> None:
        # Recomputable text can wait for the next durable write. Delivery intents,
        # confirmations and reconciliation indexes always checkpoint immediately.
        disposable = self._disposable_cache(key)
        with self.connect(checkpoint=not disposable) as db:
            db.execute(
                "INSERT OR REPLACE INTO extraction_cache VALUES(?,?)", (key, json.dumps(payload))
            )
            if disposable:
                self._evict_text_cache(db)

    def _evict_text_cache(self, db: sqlite3.Connection) -> None:
        rows = db.execute(
            "SELECT cache_key,length(CAST(payload AS BLOB)) size FROM extraction_cache ORDER BY rowid DESC"
        ).fetchall()
        used = 0
        for row in rows:
            if self._disposable_cache(row["cache_key"]):
                if used + row["size"] > 2 * 1024 * 1024:
                    db.execute(
                        "DELETE FROM extraction_cache WHERE cache_key=?",
                        (row["cache_key"],),
                    )
                else:
                    used += row["size"]

    def maintain(self) -> None:
        """Trim payloads, retaining idempotency markers and unresolved work."""
        with self.connect() as db:
            self._evict_text_cache(db)
            db.execute(
                "UPDATE delivery_outbox SET payload='{}' WHERE status='sent' AND created_at<? AND payload!='{}'",
                (time.time() - 30 * 86400,),
            )
            db.execute(
                "UPDATE quiz_history SET payload='{}' WHERE sent_at<datetime('now','-180 days') AND payload!='{}'"
            )
            db.execute("DELETE FROM runs WHERE finished_at<?", (time.time() - 90 * 86400,))
        from .cloud_state import client_from_env

        client = client_from_env(self.path)
        if client is not None:
            client.upload(self.path)

    def migrate_notifications(self, path: Path) -> None:
        if not path.exists():
            return
        path = path.resolve()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM imported_files WHERE path=?", (str(path),)).fetchone():
                return
            if path.suffix == ".json":
                value = json.loads(path.read_text())
                if value.get("version") != 1 or not isinstance(value.get("notifications"), dict):
                    raise ValueError(f"Unsupported legacy notification state: {path}")
                records = value["notifications"]
            else:
                with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as legacy:
                    records = {
                        key: {"fingerprint": fingerprint, "sent_at": sent_at}
                        for key, fingerprint, sent_at in legacy.execute(
                            "SELECT event_key,fingerprint,sent_at FROM sent_notifications"
                        )
                    }
            for key, value in records.items():
                if not isinstance(value, dict) or not isinstance(value.get("fingerprint"), str):
                    raise ValueError(f"Invalid legacy notification entry: {path}")
                db.execute(
                    "INSERT OR IGNORE INTO sent_notifications VALUES(?,?,?)",
                    (key, value["fingerprint"], value.get("sent_at", "legacy")),
                )
                if ":announcement:" in key:
                    db.execute(
                        "INSERT OR IGNORE INTO seen_announcements VALUES(?,?,?)",
                        (key, value["fingerprint"], value.get("sent_at", "legacy")),
                    )
            db.execute("INSERT INTO imported_files VALUES(?)", (str(path),))

    def calendar_intent(
        self,
        calendar_id: str,
        uid: str,
        event_id: str,
        fingerprint: str,
        desired: dict[str, Any],
        action: str,
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM synced_assignments WHERE calendar_id=? AND uid=?", (calendar_id, uid)
            ).fetchone()
            if (
                old
                and old["fingerprint"] == fingerprint
                and old["action"] == action
                and old["status"] == "pending"
                and old["event_id"] == event_id
            ):
                return dict(old)
            revision = old["revision"] + 1 if old else 1
            db.execute(
                "INSERT OR REPLACE INTO synced_assignments VALUES(?,?,?,?,?,?,?,?)",
                (
                    calendar_id,
                    uid,
                    event_id,
                    fingerprint,
                    json.dumps(desired),
                    action,
                    "pending",
                    revision,
                ),
            )
            return dict(
                db.execute(
                    "SELECT * FROM synced_assignments WHERE calendar_id=? AND uid=?",
                    (calendar_id, uid),
                ).fetchone()
            )

    def calendar_record(self, calendar_id: str, uid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM synced_assignments WHERE calendar_id=? AND uid=?", (calendar_id, uid)
            ).fetchone()
            return dict(row) if row else None

    def calendar_confirm(self, record: dict[str, Any], event_id: str, *, notify: bool) -> None:
        desired = json.loads(record["desired"])
        action = record["action"]
        key = f"calendar_updates:custom:calendar-change:{record['calendar_id']}:{record['uid']}:{record['revision']}:part:0"
        payload = {
            "username": "Attendr",
            "content": f"📅 Academic calendar item {action}",
            "embeds": [
                {
                    "title": str(desired.get("summary") or record["uid"])[:256],
                    "description": (
                        "Removed from Google Calendar."
                        if action == "deleted"
                        else "Google Calendar date: "
                        + str(
                            desired.get("start", {}).get("dateTime")
                            or desired.get("start", {}).get("date", "")
                        )
                    ),
                    "color": 0xF59E0B,
                }
            ],
            "allowed_mentions": {"parse": []},
        }
        with self.connect() as db:
            changed = db.execute(
                "UPDATE synced_assignments SET status='synced',event_id=? WHERE calendar_id=? AND uid=? AND revision=? AND status='pending'",
                (event_id, record["calendar_id"], record["uid"], record["revision"]),
            )
            if changed.rowcount and notify:
                db.execute(
                    "INSERT OR IGNORE INTO delivery_outbox(event_key,fingerprint,destination,payload,created_at) VALUES(?,?,?,?,?)",
                    (
                        key,
                        record["fingerprint"],
                        "calendar_updates",
                        json.dumps(payload),
                        time.time(),
                    ),
                )

    def migrate_quizzes(self, path: Path) -> None:
        if not path.exists():
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM imported_files WHERE path=?", (str(path.resolve()),)
            ).fetchone():
                return
            value = json.loads(path.read_text())
            if value.get("version") != 1 or not isinstance(value.get("sent"), dict):
                raise ValueError("Invalid legacy quiz state")
            for key, record in value["sent"].items():
                db.execute(
                    "INSERT OR IGNORE INTO quiz_history VALUES(?,?,?)",
                    ("lecture-quiz:" + key, "{}", record["sent_at"]),
                )
            db.execute("INSERT INTO imported_files VALUES(?)", (str(path.resolve()),))

    def retire_assignment(self, uid: str, *, restore: bool = False) -> None:
        with self.connect() as db:
            if restore:
                db.execute("DELETE FROM retired_assignments WHERE uid=?", (uid,))
            else:
                db.execute(
                    "INSERT OR REPLACE INTO retired_assignments VALUES(?,?)", (uid, time.time())
                )

    def retired_assignments(self) -> set[str]:
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT uid FROM retired_assignments")}

    def tracked_assignments(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT uid,desired FROM synced_assignments WHERE action!='deleted'"
            ).fetchall()
        unique = {}
        for row in rows:
            private = json.loads(row["desired"]).get("extendedProperties", {}).get("private", {})
            if private.get("canvas_source") == "assignment":
                unique[row["uid"]] = {
                    "uid": row["uid"],
                    "course_id": int(private["canvas_course_id"]),
                    "assignment_id": private["canvas_source_id"],
                }
        return list(unique.values())

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        with self.path.with_suffix(".lock").open("a+") as handle:
            if os.name == "nt":
                import msvcrt

                handle.write("0")
                handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise RuntimeError("Another Attendr run is using this database") from None
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("Another Attendr run is using this database") from None
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
