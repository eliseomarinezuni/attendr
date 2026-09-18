"""Validate active integrations before the pipeline performs side effects."""

from __future__ import annotations
import os
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Annotated
from pydantic import TypeAdapter


class Settings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    timezone: str = "America/Toronto"
    database: Path = Path("data/attendr.db")
    digest_hours: int = Field(default=72, gt=0, le=8760)
    alert_hours: int = Field(default=72, gt=0, le=8760)
    discord_timeout: float = Field(default=15, gt=0, le=120)
    discord_attempts: int = Field(default=4, gt=0, le=10)
    discord_wait: float = Field(default=60, ge=0, le=120)

    @field_validator("database")
    @classmethod
    def valid_database(cls, value: Path) -> Path:
        if value.suffix.casefold() == ".json":
            raise ValueError("ATTENDR_DB must name a SQLite database")
        return value.expanduser()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, KeyError):
            raise ValueError("APP_TIMEZONE must be an IANA timezone") from None
        return value

    @classmethod
    def from_env(cls, plan: object) -> Settings:
        def required(key: str) -> str:
            value = os.getenv(key, "").strip()
            if not value or "replace_with" in value.casefold():
                raise ValueError(f"{key} must be configured")
            return value

        if plan.needs_canvas:
            origin = urlparse(required("CANVAS_BASE_URL"))
            required("CANVAS_API_TOKEN")
            if (
                origin.scheme != "https"
                or not origin.hostname
                or origin.username
                or origin.password
                or origin.path not in ("", "/")
                or origin.query
                or origin.fragment
            ):
                raise ValueError("CANVAS_BASE_URL must be an HTTPS origin")
        lecture_summaries = bool(getattr(plan, "lecture_summaries", False))
        if (
            plan.announcements
            or plan.calendar
            or plan.digest
            or plan.quiz
            or plan.lecture_quizzes
            or lecture_summaries
            or getattr(plan, "review", False)
        ):
            from .notifier import DiscordNotifier

            DiscordNotifier._validate_webhook_url(required("DISCORD_WEBHOOK_URL"))
            for key in (
                "DISCORD_ANNOUNCEMENTS_CHANNEL_ID",
                "DISCORD_CALENDAR_CHANNEL_ID",
                "DISCORD_QUIZ_CHANNEL_ID",
                "DISCORD_LECTURE_SUMMARIES_CHANNEL_ID",
            ):
                if os.getenv(key):
                    DiscordNotifier._validate_channel_id(required(key))
                    required("DISCORD_BOT_TOKEN")
            if lecture_summaries:
                DiscordNotifier._validate_channel_id(
                    required("DISCORD_LECTURE_SUMMARIES_CHANNEL_ID")
                )
                required("DISCORD_BOT_TOKEN")
        if (
            plan.materials
            or plan.quiz
            or plan.lecture_quizzes
            or lecture_summaries
            or plan.calendar
            or plan.study_plan
        ):
            required("GEMINI_API_KEY")
        if plan.study_plan:
            url, secret = os.getenv("STUDY_WORKER_URL", ""), os.getenv("STUDY_SYNC_SECRET", "")
            if bool(url) != bool(secret):
                raise ValueError(
                    "STUDY_WORKER_URL and STUDY_SYNC_SECRET must be configured together"
                )
            if url:
                if os.getenv("APP_TIMEZONE", "America/Toronto") != "America/Toronto":
                    raise ValueError(
                        "Online study controls currently require APP_TIMEZONE=America/Toronto"
                    )
                parsed = urlparse(url)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("STUDY_WORKER_URL must be an HTTPS URL")
        numeric = []
        if plan.needs_canvas:
            numeric += ["CANVAS_LOOKAHEAD_DAYS", "CANVAS_ANNOUNCEMENT_DAYS"]
        if plan.materials or plan.lecture_quizzes or lecture_summaries:
            numeric += [
                "CANVAS_MATERIAL_MAX_MB",
                "CANVAS_MATERIAL_FUTURE_DAYS",
                "LECTURE_QUIZ_RETRY_HOURS",
            ]
        if plan.calendar or plan.study_plan:
            numeric += ["GOOGLE_EVENT_DURATION_MINUTES"]
        if (
            plan.materials
            or plan.quiz
            or plan.lecture_quizzes
            or lecture_summaries
            or plan.calendar
            or plan.study_plan
        ):
            numeric += ["GEMINI_MAX_INPUT_CHARS"]
        for key in numeric:
            if key in os.environ:
                try:
                    TypeAdapter(Annotated[int, Field(gt=0)]).validate_python(os.environ[key])
                except ValueError:
                    raise ValueError(f"{key} must be a positive integer") from None
        names = {
            "timezone": "APP_TIMEZONE",
            "database": "ATTENDR_DB",
            "digest_hours": "DISCORD_DIGEST_HOURS",
            "alert_hours": "DISCORD_ALERT_HOURS",
            "discord_timeout": "DISCORD_TIMEOUT_SECONDS",
            "discord_attempts": "DISCORD_MAX_ATTEMPTS",
            "discord_wait": "DISCORD_MAX_RATE_LIMIT_WAIT_SECONDS",
        }
        return cls.model_validate(
            {name: os.environ[key] for name, key in names.items() if key in os.environ}
        )
