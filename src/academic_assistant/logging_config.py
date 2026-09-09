"""JSON logs without credential values or provider response bodies."""

import json
import logging
import os
import re
import traceback


_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:access_token|auth|key|signature|token|verifier)=)[^&\s\"']+"
)


class RedactedJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = _SENSITIVE_QUERY.sub(r"\1[REDACTED]", record.getMessage())
        value = {
            "level": record.levelname,
            "source": record.name,
            "message": message,
            "run_id": getattr(record, "run_id", None),
        }
        if record.exc_info:
            value["exception"] = record.exc_info[0].__name__
            value["frames"] = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        text = json.dumps(value)
        for key, secret in os.environ.items():
            if (
                any(marker in key for marker in ("TOKEN", "SECRET", "API_KEY", "WEBHOOK"))
                and len(secret) >= 6
            ):
                text = text.replace(secret, "[REDACTED]").replace(
                    json.dumps(secret)[1:-1], "[REDACTED]"
                )
        return text


def configure_logging(run_id: str | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactedJSONFormatter())

    def add_context(record: logging.LogRecord) -> bool:
        record.run_id = run_id
        return True

    handler.addFilter(add_context)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # canvasapi logs signed download URLs at INFO. Application-level summaries
    # retain failures without exposing temporary access parameters.
    logging.getLogger("canvasapi.requester").setLevel(logging.WARNING)
