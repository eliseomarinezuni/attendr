from __future__ import annotations

import logging

from academic_assistant.logging_config import RedactedJSONFormatter, configure_logging


def test_signed_query_parameters_are_redacted() -> None:
    record = logging.LogRecord(
        "provider",
        logging.INFO,
        __file__,
        1,
        "GET https://canvas.test/file?download=1&verifier=temporary-value&key=other-value",
        (),
        None,
    )
    rendered = RedactedJSONFormatter().format(record)
    assert "temporary-value" not in rendered
    assert "other-value" not in rendered
    assert rendered.count("[REDACTED]") == 2


def test_canvas_request_debug_logging_is_disabled() -> None:
    configure_logging("test")
    assert logging.getLogger("canvasapi.requester").getEffectiveLevel() >= logging.WARNING
