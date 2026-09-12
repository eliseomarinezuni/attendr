"""Safe per-run AI usage counters shared by AI callers and verified caches."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import Lock


@dataclass(slots=True)
class TaskUsage:
    requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    deterministic: int = 0
    input_chars: int = 0
    retries: int = 0
    rate_limits: int = 0
    successes: int = 0
    failures: int = 0
    duration_ms: int = 0
    models: set[str] = field(default_factory=set)


class AIUsageTracker:
    def __init__(self) -> None:
        self._lock = Lock()
        self._tasks: dict[str, TaskUsage] = {}

    def reset(self) -> None:
        with self._lock:
            self._tasks.clear()

    def record(self, task: str, **values: int | str) -> None:
        with self._lock:
            usage = self._tasks.setdefault(task, TaskUsage())
            for name, value in values.items():
                if name == "model":
                    usage.models.add(str(value))
                else:
                    setattr(usage, name, getattr(usage, name) + int(value))

    def snapshot(self) -> dict[str, TaskUsage]:
        with self._lock:
            return {
                task: TaskUsage(
                    **{
                        **{
                            name: getattr(usage, name)
                            for name in TaskUsage.__dataclass_fields__
                            if name != "models"
                        },
                        "models": set(usage.models),
                    }
                )
                for task, usage in self._tasks.items()
            }

    def log_summary(self) -> None:
        logger = logging.getLogger("attendr.ai_usage")
        for task, usage in sorted(self.snapshot().items()):
            logger.info(
                "AI usage task=%s requests=%d cache_hits=%d cache_misses=%d "
                "deterministic=%d input_chars=%d approx_tokens=%d retries=%d "
                "rate_limits=%d successes=%d failures=%d duration_ms=%d models=%s",
                task,
                usage.requests,
                usage.cache_hits,
                usage.cache_misses,
                usage.deterministic,
                usage.input_chars,
                (usage.input_chars + 3) // 4,
                usage.retries,
                usage.rate_limits,
                usage.successes,
                usage.failures,
                usage.duration_ms,
                ",".join(sorted(usage.models)) or "none",
            )


AI_USAGE = AIUsageTracker()
