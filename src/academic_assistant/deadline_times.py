"""Shared recognition of explicit deadline times without guessing missing times."""

import re

_TIME = re.compile(
    r"(?<![\w:])(?:"
    r"(?P<named>noon|midnight)|"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<suffix>a\.?m\.?|p\.?m\.?)|"
    r"(?P<hour24>\d{1,2}):(?P<minute24>\d{2})"
    r")(?![\w:])",
    re.I,
)


def explicit_times(source: str) -> tuple[set[int], bool]:
    """Return minutes since midnight and whether a time-shaped value is invalid."""
    found: set[int] = set()
    invalid = False
    for match in _TIME.finditer(source):
        if match["named"]:
            found.add(720 if match["named"].casefold() == "noon" else 0)
            continue
        suffix = match["suffix"]
        hour = int(match["hour"] if suffix else match["hour24"])
        minute = int((match["minute"] if suffix else match["minute24"]) or 0)
        if minute > 59 or not (1 <= hour <= 12 if suffix else 0 <= hour <= 23):
            invalid = True
            continue
        if suffix:
            hour = hour % 12 + (12 if suffix.casefold().startswith("p") else 0)
        found.add(hour * 60 + minute)
    return found, invalid
