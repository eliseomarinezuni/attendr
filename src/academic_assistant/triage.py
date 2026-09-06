"""Explainable announcement triage; critical notices bypass mute preferences."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from .canvas_client import Announcement
from .preferences import Preferences
from .state_store import StateStore


@dataclass(frozen=True)
class TriageDecision:
    category: str
    reason: str
    muted: bool = False


class AnnouncementTriage:
    def __init__(self, preferences: Preferences, store: StateStore):
        self.preferences = preferences
        self.store = store

    def classify(self, announcement: Announcement) -> TriageDecision:
        text = f"{announcement.title}\n{announcement.message_text}".casefold()
        if re.search(
            r"\b(deadline|due|exam|midterm|final|cancelled|canceled|rescheduled|extension|urgent|room change)\b",
            text,
        ):
            decision = TriageDecision(
                "Action required", "Contains a deadline, exam, or schedule-change notice"
            )
        elif re.search(r"\b(assignment|quiz|submit|submission|register|required)\b", text):
            decision = TriageDecision(
                "Coursework", "Contains coursework or participation instructions"
            )
        else:
            muted = next(
                (
                    phrase
                    for phrase in self.preferences.muted_topics
                    if phrase.casefold().strip() in text
                ),
                None,
            )
            decision = TriageDecision(
                "Information",
                f"Matched your mute phrase: {muted}" if muted else "General course information",
                muted is not None,
            )
        fingerprint = sha256((text + self.preferences.model_dump_json()).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute(
                'INSERT OR REPLACE INTO announcement_triage VALUES(?,?,?,?,?,datetime("now"))',
                (
                    announcement.uid,
                    fingerprint,
                    decision.category,
                    decision.reason,
                    int(decision.muted),
                ),
            )
        return decision
