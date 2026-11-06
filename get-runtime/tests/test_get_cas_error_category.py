"""CAS diagnostics must not change retry safety or reveal stderr."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_legacy_manual as manual
import get_recurring_runtime as runtime


class CasErrorCategory(unittest.TestCase):
    def test_stderr_maps_only_to_fixed_categories(self):
        samples = ((b'fatal: missing blob private-note', 'missing_object'),
                   (b'shallow update not allowed private-note', 'shallow_history'),
                   (b'could not read username private-note', 'authentication_or_permission'),
                   (b'connection reset private-note', 'network'),
                   (b'remote: error GH001: large files private-note', 'size_or_remote_rejection'),
                   (b'private-note', 'other'))
        for raw, expected in samples:
            self.assertEqual(manual.cas_error_category(raw), expected)

    def test_cas_failure_retains_one_push_six_reads_and_safe_public_field(self):
        parent, commit = 'a' * 40, 'b' * 40
        plan = {'parent_commit': parent, 'candidate_commit': commit}
        remote = types.SimpleNamespace(cas_main=Mock(side_effect=manual.CasPushFailure('missing_object')))
        sleep = Mock()
        with patch.object(manual, 'guard', return_value=parent) as guard:
            with self.assertRaises(manual.ManualFailure) as caught:
                manual.publish(None, remote, plan, {'fixed': b'value'}, manual.a.sha(manual.a.encoded(plan)),
                               lambda *_: None, sleep=sleep, writers_guard=lambda: None,
                               checkpoint=lambda _: None, verifier=lambda *_: None)
        self.assertEqual(str(caught.exception), 'manual_uncertain_write')
        self.assertEqual((caught.exception.write_state, caught.exception.attempts), ('uncertain', 1))
        self.assertEqual(remote.cas_main.call_count, 1)
        self.assertEqual(guard.call_count, 9)
        self.assertEqual(sleep.call_count, 5)
        projected = runtime.public_result(runtime.failure(caught.exception, attempt_started=True))
        self.assertEqual(projected['cas_error_category'], 'missing_object')
        self.assertNotIn('private-note', str(projected))

    def test_real_remote_push_failure_is_classified_without_stderr(self):
        remote = object.__new__(manual.Remote)
        remote.local = types.SimpleNamespace(run=Mock(), command=Mock(return_value=types.SimpleNamespace(
            returncode=1, stderr=b'fatal: missing blob private-note')))
        with self.assertRaises(manual.CasPushFailure) as caught:
            remote.cas_main('a' * 40, 'b' * 40)
        self.assertEqual(caught.exception.cas_error_category, 'missing_object')
        self.assertNotIn('private-note', str(caught.exception))
        self.assertEqual(remote.local.command.call_count, 1)

    def test_unrecognized_category_is_not_exposed(self):
        error = manual.ManualFailure('manual_uncertain_write', 'uncertain', 1)
        error.cas_error_category = 'private-note'
        self.assertNotIn('cas_error_category', runtime.public_result(runtime.failure(error)))


if __name__ == '__main__':
    unittest.main()
