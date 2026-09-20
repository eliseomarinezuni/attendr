"""Conservative exclusions shared by extraction and source verification."""

import re

_EXCLUDED = re.compile(
    r"\b(?:cancelled|canceled|example|sample|hypothetical|practice\s+only|"
    r"not\s+(?:a\s+)?(?:real\s+)?deadline|no\s+(?:exam|quiz|test)|"
    r"(?:exam|quiz|test|assignment)\s+(?:is\s+)?(?:removed|withdrawn))\b",
    re.I,
)


def excluded_deadline(text: str) -> bool:
    return bool(_EXCLUDED.search(text))
