from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import setup_discord  # noqa: E402


class SetupDiscordTests(unittest.TestCase):
    def test_setup_saves_webhook_without_printing_it(self):
        webhook = "https://discord.com/api/webhooks/123/private-token"
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            stdout = StringIO()
            stderr = StringIO()
            with (
                patch.object(setup_discord, "ENV_PATH", env_path),
                patch.object(setup_discord, "getpass", return_value=webhook),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = setup_discord.main()

            self.assertEqual(result, 0)
            self.assertIn(f"DISCORD_WEBHOOK_URL={webhook}", env_path.read_text())
            self.assertNotIn("private-token", stdout.getvalue())
            self.assertNotIn("private-token", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
