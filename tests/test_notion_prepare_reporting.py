"""Execute the real preparation shell tail; synthetic child, no network/secrets."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReportingTests(unittest.TestCase):
    def shell(self, exit_code, *, output=True):
        raw = (ROOT / '.github/workflows/source-sync-shadow.yml').read_text()
        section = raw.split('      - name: Prepare the bounded Notion transaction without writes\n', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn('          test "$APPLY" != true\n', section)
        self.assertIn('          test -z "$APPROVED_PAYLOAD_SHA256"\n', section)
        tail = section.split('          PYPUBLIC\n', 1)[1]
        tail = '\n'.join(line[10:] for line in tail.splitlines())
        value = {'status': 'failed' if exit_code else 'prepared', 'reason': 'notion_fixture', 'write_state': 'not_attempted'}
        child = ('print(' + repr(json.dumps(value)) + ');' if output else '') + 'raise SystemExit(' + str(exit_code) + ')'
        original = 'python scripts/get_notion_runtime.py --expected-parent "$EXPECTED_PARENT"'
        self.assertEqual(tail.count(original), 1)
        tail = tail.replace(original, 'python -c ' + shlex.quote(child))
        with tempfile.TemporaryDirectory() as folder:
            summary = Path(folder) / 'summary'
            env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary), EXPECTED_PARENT='a' * 40)
            tail = tail.replace('${{ runner.temp }}', folder)
            run = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + tail], cwd=ROOT, env=env, capture_output=True, text=True)
            return run, summary.read_text() if summary.exists() else '', value

    def test_failed_preparation_keeps_fixed_report_and_nonzero_exit(self):
        result, summary, value = self.shell(1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(summary), value)
        self.assertIn(json.dumps(value), result.stdout)
        self.assertNotIn('Traceback', result.stderr)

    def test_successful_preparation_keeps_report_and_success(self):
        result, summary, value = self.shell(0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(summary), value)

    def test_empty_failed_child_does_not_invent_success(self):
        result, summary, _ = self.shell(1, output=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(summary, '')
        self.assertNotIn('prepared', result.stdout)


if __name__ == '__main__': unittest.main()
