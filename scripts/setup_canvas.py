#!/usr/bin/env python3
"""Securely configure and validate local Canvas credentials."""

from __future__ import annotations

from getpass import getpass
import os
from pathlib import Path
import re
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (  # noqa: E402
    CanvasAPIError,
    CanvasAuthenticationError,
    CanvasClient,
    CanvasConfigurationError,
)


ENV_KEY_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
DEFAULT_BASE_URL = "https://learn.ontariotechu.ca"


def _render_env(original: str, updates: dict[str, str]) -> str:
    """Replace selected environment keys without disturbing unrelated settings."""
    output: list[str] = []
    replaced: set[str] = set()

    for line in original.splitlines():
        match = ENV_KEY_PATTERN.match(line)
        key = match.group(1) if match else None
        if key in updates:
            if key not in replaced:
                output.append(f"{key}={updates[key]}")
                replaced.add(key)
            continue
        output.append(line)

    if output and output[-1] != "":
        output.append("")
    for key, value in updates.items():
        if key not in replaced:
            output.append(f"{key}={value}")

    return "\n".join(output).rstrip() + "\n"


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Atomically write .env with owner-only permissions."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    content = _render_env(original, updates)
    path.parent.mkdir(parents=True, exist_ok=True)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        temporary_path.replace(path)
        path.chmod(0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    print("Canvas credential setup")
    base_url = input(f"Canvas base URL [{DEFAULT_BASE_URL}]: ").strip()
    base_url = base_url or DEFAULT_BASE_URL
    token = getpass("Canvas API token (hidden): ").strip()
    if not token:
        print("No token entered; nothing was saved.", file=sys.stderr)
        return 1
    if "\n" in token or "\r" in token:
        print("The token contains an invalid newline; nothing was saved.", file=sys.stderr)
        return 1

    print("Validating credentials with Canvas...")
    try:
        client = CanvasClient(base_url, token)
        user_id, user_name = client.validate_credentials()
    except (CanvasConfigurationError, CanvasAuthenticationError, CanvasAPIError) as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        print("Nothing was saved.", file=sys.stderr)
        return 1

    _write_env(
        ENV_PATH,
        {
            "CANVAS_BASE_URL": client.base_url,
            "CANVAS_API_TOKEN": token,
            "CANVAS_LOOKAHEAD_DAYS": "30",
            "CANVAS_ANNOUNCEMENT_DAYS": "14",
            "APP_TIMEZONE": "America/Toronto",
        },
    )
    print(f"Connected as {user_name} (Canvas user ID {user_id}).")
    print(f"Credentials saved to {ENV_PATH} with owner-only permissions.")
    print("Next: .venv/bin/python scripts/test_canvas.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled; nothing was saved.", file=sys.stderr)
        raise SystemExit(130)
