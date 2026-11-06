"""CAS diagnostics must not change retry safety or reveal stderr."""
from pathlib import Path
from contextlib import ExitStack
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_legacy_manual as manual
import get_recurring_runtime as runtime
import get_private_preview_retention as retention


class CasErrorCategory(unittest.TestCase):
    def test_replaced_blob_failure_stops_before_publication(self):
        parent = 'a' * 40
        reader = types.SimpleNamespace(local=object())
        remote = types.SimpleNamespace(cas_main=Mock())
        plan = {'parent_commit': parent}
        replacements = [
            (runtime, 'validate_policy', {'return_value': {'rules_sha256': 'f' * 64}}),
            (runtime, 'validate_scope', {'return_value': None}),
            (runtime, 'context', {'return_value': {}}),
            (runtime.provenance, 'verify_execution', {'return_value': None}),
            (runtime.m, 'guard', {'return_value': parent}),
            (runtime.r, 'readback_checkout', {'return_value': object()}),
            (runtime.m, 'load_evidence', {'return_value': (reader, object(), None)}),
            (runtime, 'load_trusted_inputs', {'return_value': (None, None, None)}),
            (runtime.entry, 'prepare', {'return_value': (plan, {'Raw/03 Get/note.md': b'new'})}),
            (runtime.entry, 'public_report', {'return_value': {'status': 'recurring_local_candidate_verified', 'plan_sha256': 'b' * 64}}),
            (runtime, 'hydrate_replaced_blobs', {'side_effect': ValueError('runtime_receipt')}),
        ]
        with ExitStack() as stack:
            for target, name, options in replacements:
                stack.enter_context(patch.object(target, name, **options))
            publish = stack.enter_context(patch.object(runtime.m, 'publish'))
            with self.assertRaises(ValueError) as caught:
                runtime.run('token', object(), policy={}, approved_policy_sha256='c' * 64,
                            remote=remote, public_client=object(), apply=True, scope={},
                            approved_scope_sha256='d' * 64, run_context={}, checkpoint=lambda _: None,
                            bootstrap={'capture_anchor': {'capture_commit': 'e' * 40}})
        result = runtime.failure(caught.exception, attempt_started=False)
        self.assertEqual((result['stage'], result['write_state']), ('replaced_blob_hydration', 'not_attempted'))
        publish.assert_not_called()
        remote.cas_main.assert_not_called()

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
        plan = {'parent_commit': parent, 'candidate_commit': commit, 'transaction_bytes': 5}
        remote = types.SimpleNamespace(cas_main=Mock(side_effect=manual.CasPushFailure('missing_object',
            {'stage': 'push_exit', 'exit_code': 128, 'stdout_category': 'none', 'stderr_category': 'missing_object'})))
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
        self.assertEqual(projected['cas_diagnostic'], {'stage': 'push_exit', 'exit_code': 128,
                         'stdout_category': 'none', 'stderr_category': 'missing_object'})
        self.assertEqual(projected['cas_candidate_metrics'], {'changed_paths': 1, 'changed_bytes': 5,
                                                               'transaction_bytes': 5})
        self.assertNotIn('private-note', str(projected))

    def test_real_remote_push_failure_is_classified_without_stderr(self):
        remote = object.__new__(manual.Remote)
        remote.local = types.SimpleNamespace(command=Mock(side_effect=[
            types.SimpleNamespace(returncode=0, stdout=b'', stderr=b''),
            types.SimpleNamespace(returncode=1, stdout=b'', stderr=b'fatal: missing blob private-note')]))
        with self.assertRaises(manual.CasPushFailure) as caught:
            remote.cas_main('a' * 40, 'b' * 40)
        self.assertEqual(caught.exception.cas_error_category, 'missing_object')
        self.assertNotIn('private-note', str(caught.exception))
        self.assertEqual(remote.local.command.call_count, 2)
        self.assertEqual(caught.exception.cas_diagnostic['stage'], 'push_exit')

    def test_merge_base_failure_never_starts_push(self):
        remote = object.__new__(manual.Remote)
        remote.local = types.SimpleNamespace(command=Mock(return_value=types.SimpleNamespace(
            returncode=1, stdout=b'', stderr=b'private-note')))
        with self.assertRaises(manual.CasPushFailure) as caught:
            remote.cas_main('a' * 40, 'b' * 40)
        self.assertEqual(caught.exception.cas_diagnostic['stage'], 'merge_base')
        self.assertEqual(remote.local.command.call_count, 1)

    def test_push_exit_classifies_stdout_and_stderr_without_raw_content(self):
        remote = object.__new__(manual.Remote)
        remote.local = types.SimpleNamespace(command=Mock(side_effect=[
            types.SimpleNamespace(returncode=0, stdout=b'', stderr=b''),
            types.SimpleNamespace(returncode=1, stdout=b'! [remote rejected] private-note',
                                  stderr=b'error: failed to push private-note')]))
        with self.assertRaises(manual.CasPushFailure) as caught:
            remote.cas_main('a' * 40, 'b' * 40)
        diagnostic = caught.exception.cas_diagnostic
        self.assertEqual((diagnostic['stage'], diagnostic['exit_code'], diagnostic['stdout_category']),
                         ('push_exit', 1, 'size_or_remote_rejection'))
        self.assertEqual(diagnostic['stdout_words'], ['rejected', 'remote'])
        self.assertEqual(diagnostic['stderr_words'], ['error', 'failed', 'push'])
        self.assertNotIn('private-note', str(diagnostic))

    def test_push_timeout_and_start_errors_have_distinct_fixed_stages(self):
        for kind, stage in (('timeout', 'push_timeout'), ('spawn_error', 'push_start'), ('other', 'push_exception')):
            with self.subTest(kind=kind):
                error = retention.RetentionFailure('git_operation_failed')
                error.git_exception_kind = kind
                remote = object.__new__(manual.Remote)
                remote.local = types.SimpleNamespace(command=Mock(side_effect=[
                    types.SimpleNamespace(returncode=0, stdout=b'', stderr=b''), error]))
                with self.assertRaises(manual.CasPushFailure) as caught:
                    remote.cas_main('a' * 40, 'b' * 40)
                self.assertEqual(caught.exception.cas_diagnostic, {'stage': stage, 'exception': kind})
                self.assertEqual(remote.local.command.call_count, 2)

    def test_local_git_sanitizes_timeout_and_spawn_exceptions(self):
        local = object.__new__(retention.LocalGit)
        local.vault = Path('/unused'); local.env = {}
        for error, expected in ((subprocess.TimeoutExpired('private-note', 1), 'timeout'),
                                (OSError('private-note'), 'spawn_error'),
                                (ValueError('private-note'), 'other')):
            with self.subTest(expected=expected), patch.object(retention.subprocess, 'run', side_effect=error):
                with self.assertRaises(retention.RetentionFailure) as caught:
                    local.command('push')
                self.assertEqual(caught.exception.git_exception_kind, expected)
                self.assertNotIn('private-note', str(caught.exception))

    def test_public_diagnostic_rejects_untrusted_fields(self):
        error = manual.ManualFailure('manual_uncertain_write', 'uncertain', 1)
        error.cas_diagnostic = {'stage': 'push_exit', 'exit_code': 128, 'stdout_category': 'other',
                                'stderr_category': 'private-note', 'extra': 'private-note'}
        projected = runtime.public_result(runtime.failure(error))
        self.assertEqual(projected['cas_diagnostic'], {'stage': 'push_exit', 'exit_code': 128,
                                                       'stdout_category': 'other'})
        self.assertNotIn('private-note', str(projected))

    def test_word_hits_and_metrics_are_bounded_and_never_echo_raw_output(self):
        raw = b'fatal: remote rejected private-note https://private.invalid/path auth-token object error'
        self.assertEqual(manual.diagnostic_report.git_word_hits(raw),
                         ['auth', 'error', 'fatal', 'object', 'rejected', 'remote'])
        error = manual.ManualFailure('manual_uncertain_write', 'uncertain', 1)
        error.cas_diagnostic = {'stage': 'push_exit', 'stdout_words': ['fatal', 'private-note'],
                                'stderr_words': ['error', 'fatal'], 'extra': raw}
        error.cas_candidate_metrics = {'changed_paths': 2, 'changed_bytes': 100,
                                       'transaction_bytes': 64 * 1024 * 1024 + 1}
        projected = runtime.public_result(runtime.failure(error))
        self.assertNotIn('stdout_words', projected['cas_diagnostic'])
        self.assertEqual(projected['cas_diagnostic']['stderr_words'], ['error', 'fatal'])
        self.assertNotIn('cas_candidate_metrics', projected)
        self.assertNotIn('private-note', str(projected))
        self.assertNotIn('private.invalid', str(projected))

    def test_unrecognized_category_is_not_exposed(self):
        error = manual.ManualFailure('manual_uncertain_write', 'uncertain', 1)
        error.cas_error_category = 'private-note'
        self.assertNotIn('cas_error_category', runtime.public_result(runtime.failure(error)))


if __name__ == '__main__':
    unittest.main()
