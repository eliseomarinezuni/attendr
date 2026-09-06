"""Validated user preferences shared by planning and announcement triage."""

from __future__ import annotations

import json
import os
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_WINDOWS = {
    "0": [[780, 990], [1125, 1170]],
    "1": [[1125, 1320]],
    "2": [[1215, 1320]],
    "3": [],
    "4": [[1215, 1320]],
    "5": [[660, 1080]],
    "6": [[660, 1080]],
}


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)
    # Python weekday numbers: Monday=0. Minutes after local midnight.
    study_windows: dict[str, list[tuple[int, int]]] = Field(
        default_factory=lambda: DEFAULT_WINDOWS.copy()
    )
    study_minutes: dict[str, int] = Field(default_factory=dict)
    course_priorities: dict[str, int] = Field(default_factory=dict)
    muted_topics: list[str] = Field(default_factory=list, max_length=50)
    review_enabled: bool = True
    review_daily_limit: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def validate_preferences(self):
        if set(self.study_windows) != {str(day) for day in range(7)}:
            raise ValueError("study_windows requires weekdays 0 through 6")
        for windows in self.study_windows.values():
            previous_end = -1
            for start, end in windows:
                if not 0 <= start < end <= 1439 or start < previous_end:
                    raise ValueError(
                        "Study windows must be ordered, non-overlapping, and within one day"
                    )
                previous_end = end
        if any(
            kind not in {"assignment", "quiz", "exam", "project", "presentation"}
            or not 15 <= minutes <= 180
            for kind, minutes in self.study_minutes.items()
        ):
            raise ValueError("study_minutes requires a supported task kind and 15–180 minutes")
        if any(
            not key.isdigit() or not 1 <= value <= 5
            for key, value in self.course_priorities.items()
        ):
            raise ValueError("course_priorities maps Canvas course IDs to weights 1–5")
        if any(not topic.strip() or len(topic) > 100 for topic in self.muted_topics):
            raise ValueError("Muted topics must be nonempty phrases of at most 100 characters")
        return self

    @classmethod
    def load(cls, root: Path) -> Preferences:
        configured = os.getenv("ATTENDR_PREFERENCES_FILE")
        path = Path(configured).expanduser() if configured else root / "data/preferences.json"
        if not path.is_absolute():
            path = root / path
        if not path.exists() and not configured:
            return cls()
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def worker_profile(self) -> dict:
        return {
            "windows": {
                str((int(day) + 1) % 7): windows for day, windows in self.study_windows.items()
            }
        }
