from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import setup_canvas  # noqa: E402


class SetupCanvasTests(unittest.TestCase):
    def test_write_env_replaces_token_preserves_other_values_and_locks_permissions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "CANVAS_API_TOKEN=old-secret\nDISCORD_WEBHOOK_URL=keep-me\n",
                encoding="utf-8",
            )

            setup_canvas._write_env(
                path,
                {
                    "CANVAS_BASE_URL": "https://canvas.example",
                    "CANVAS_API_TOKEN": "new-secret",
                },
            )

            content = path.read_text(encoding="utf-8")
            self.assertNotIn("old-secret", content)
            self.assertIn("CANVAS_API_TOKEN=new-secret", content)
            self.assertIn("DISCORD_WEBHOOK_URL=keep-me", content)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_setup_never_prints_token(self):
        class FakeClient:
            def __init__(self, base_url, token):
                self.base_url = base_url

            def validate_credentials(self):
                return "42", "Ada Student"

        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            stdout = StringIO()
            stderr = StringIO()
            with (
                patch.object(setup_canvas, "ENV_PATH", path),
                patch.object(setup_canvas, "CanvasClient", FakeClient),
                patch.object(setup_canvas, "input", return_value=""),
                patch.object(setup_canvas, "getpass", return_value="top-secret-token"),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = setup_canvas.main()

            self.assertEqual(result, 0)
            self.assertNotIn("top-secret-token", stdout.getvalue())
            self.assertNotIn("top-secret-token", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
