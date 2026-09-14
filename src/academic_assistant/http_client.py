"""Bounded transport for the Canvas SDK, including its file downloads."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests


class CanvasSession(requests.Session):
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        # SDK methods do not accept a requests timeout; enforce it at transport level.
        kwargs["timeout"] = (5, 15)
        attempts = 3 if method.upper() in {"GET", "HEAD"} else 1
        for attempt in range(attempts):
            try:
                response = super().request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt + 1 == attempts:
                    raise
                time.sleep(2**attempt)
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                return response
            delay = self._retry_delay(response.headers.get("Retry-After"), 2**attempt)
            if delay > 15:
                return response
            response.close()
            time.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(value: str | None, fallback: float) -> float:
        if value is None:
            return fallback
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return fallback
        return max(0, delay) if math.isfinite(delay) else float("inf")


def configure_canvas_transport(canvas: Any) -> None:
    # canvasapi 3.x exposes no transport injection API. Keep this SDK seam isolated.
    requester = getattr(canvas, "_Canvas__requester", None)
    if requester is None or not hasattr(requester, "_session"):
        raise RuntimeError("Unsupported Canvas SDK transport; refusing unbounded requests.")
    requester._session.close()
    requester._session = CanvasSession()
