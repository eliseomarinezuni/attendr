from __future__ import annotations

import logging
import os
import sys
from unittest.mock import patch

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


def test_exception_frames_are_preserved_while_environment_secrets_are_redacted() -> None:
    secret = "fake-secret-value-for-test"
    try:
        raise RuntimeError(f"request failed with {secret}")
    except RuntimeError:
        record = logging.LogRecord(
            "attendr.pipeline",
            logging.ERROR,
            __file__,
            1,
            "Unexpected failure using %s",
            (secret,),
            sys.exc_info(),
        )

    with patch.dict(os.environ, {"attendr_test_secret": secret}):
        rendered = RedactedJSONFormatter().format(record)

    assert secret not in rendered
    assert "[REDACTED]" in rendered
    assert '"exception": "RuntimeError"' in rendered
    assert (
        '"function": "test_exception_frames_are_preserved_while_environment_secrets_are_redacted"'
        in rendered
    )
