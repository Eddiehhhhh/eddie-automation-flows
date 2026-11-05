"""Attempt-specific creation clocks returned by GitHub's Actions API."""
import importlib
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_runtime_bindings
with patch.object(get_private_runtime_bindings, 'value', return_value=None):
    provenance = importlib.import_module('get_publication_provenance')


class AttemptClock(unittest.TestCase):
    def test_rerun_creation_time_uses_its_attempt_start(self):
        listed = {
            'created_at': '2026-11-04T21:30:28Z',
            'run_started_at': '2026-11-04T21:51:19Z',
        }
        attempt = {
            'created_at': '2026-11-04T21:51:20Z',
            'run_started_at': '2026-11-04T21:51:19Z',
        }
        self.assertTrue(provenance._attempt_clock_matches(attempt, listed, 2))
        self.assertFalse(provenance._attempt_clock_matches(attempt, listed, 1))

    def test_first_attempt_creation_time_uses_run_creation(self):
        listed = {'created_at': '2026-11-04T21:30:28Z'}
        attempt = {'created_at': '2026-11-04T21:30:29Z'}
        self.assertTrue(provenance._attempt_clock_matches(attempt, listed, 1))

    def test_rerun_without_attempt_start_fails_closed(self):
        listed = {'created_at': '2026-11-04T21:30:28Z'}
        attempt = {'created_at': '2026-11-04T21:51:20Z'}
        self.assertFalse(provenance._attempt_clock_matches(attempt, listed, 2))


if __name__ == '__main__':
    unittest.main()
