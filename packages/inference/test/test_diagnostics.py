import json
import tempfile
import unittest
from pathlib import Path

from diagnostics import startup_diagnostics


class StartupDiagnosticsTests(unittest.TestCase):
    def test_private_access_is_omitted_and_log_tokens_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "status.json").write_text(json.dumps({
                "state": "starting", "resourcesReleased": False,
                "access": {"engineToken": "private-credential"},
            }))
            (root / "engine.log").write_text(
                "api_key=private-credential\nstartup pending " + "a" * 64 + "\n"
            )
            report = startup_diagnostics(root)
            self.assertIn("starting", report)
            self.assertIn("startup pending", report)
            self.assertNotIn("private-credential", report)
            self.assertNotIn("a" * 64, report)

    def test_partial_status_is_reported_without_masking_the_startup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "status.json").write_text("{")
            self.assertIn("JSONDecodeError", startup_diagnostics(directory))
