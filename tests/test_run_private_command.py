import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_private_command.py"
CANARY = "secret-title /private/path file-id-123"


class RunPrivateCommandTests(unittest.TestCase):
    def run_wrapper(self, exit_code):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "captured.txt"
            child = f"import sys; print({CANARY!r}); print({CANARY!r}, file=sys.stderr); raise SystemExit({exit_code})"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--label", "canary", "--stdout-file", str(output), "--", sys.executable, "-c", child],
                text=True,
                capture_output=True,
            )
            return result, output.read_text(encoding="utf-8"), os.stat(output).st_mode & 0o777

    def test_success_suppresses_private_output(self):
        result, captured, mode = self.run_wrapper(0)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(CANARY, result.stdout + result.stderr)
        self.assertIn(CANARY, captured)
        self.assertEqual(mode, 0o600)

    def test_failure_suppresses_private_output(self):
        result, captured, mode = self.run_wrapper(7)
        self.assertEqual(result.returncode, 7)
        self.assertNotIn(CANARY, result.stdout + result.stderr)
        self.assertIn(CANARY, captured)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
