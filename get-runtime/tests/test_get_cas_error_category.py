"""CAS diagnostics must not change retry safety or reveal stderr."""
from pathlib import Path
from contextlib import ExitStack
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_runtime_bindings
with patch.object(get_private_runtime_bindings, 'value', return_value=None):
    import get_legacy_manual as manual
    import get_recurring_runtime as runtime
    import get_private_preview_retention as retention


class CasErrorCategory(unittest.TestCase):
    def test_trusted_history_diagnostic_is_allowlisted_and_scoped(self):
        error = runtime.entry.EntryFailure('entry_foreign_get_change')
        error.runtime_stage = 'trusted_states'
        error.history_check = 'pilot_tree_pair_unmatched'
        for interval in runtime.entry.HISTORY_INTERVALS:
            error.history_interval = interval
            result = runtime.failure(error)
            self.assertEqual(result['history_interval'], interval)
            self.assertEqual(runtime.public_result(result)['history_interval'], interval)
        error.history_interval = 'private-commit-id-must-not-leak'
        self.assertNotIn('history_interval', runtime.public_result(runtime.failure(error)))
        error.history_interval = 'trusted_anchor_to_parent'
        for check in runtime.entry.HISTORY_CHECKS:
            error.history_check = check
            result = runtime.failure(error)
            self.assertEqual(result['history_check'], check)
            self.assertEqual(runtime.public_result(result)['history_check'], check)

        error.history_check = 'private-commit-id-must-not-leak'
        self.assertNotIn('history_check', runtime.public_result(runtime.failure(error)))
        error.history_check = 'pilot_tree_pair_unmatched'
        error.runtime_stage = 'receipt_provenance'
        self.assertNotIn('history_check', runtime.public_result(runtime.failure(error)))

    def test_pending_gate_stops_before_source_and_publication(self):
        parent = 'a' * 40
        remote = types.SimpleNamespace(cas_main=Mock())
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime, 'validate_policy', return_value={}))
            stack.enter_context(patch.object(runtime, 'validate_scope', return_value=None))
            stack.enter_context(patch.object(runtime, 'context', return_value={}))
            stack.enter_context(patch.object(runtime.provenance, 'verify_execution', return_value=None))
            stack.enter_context(patch.object(runtime.m, 'guard', return_value=parent))
            stack.enter_context(patch.object(runtime.staging, 'API', return_value=object()))
            stack.enter_context(patch.object(runtime.pending, 'check', side_effect=ValueError('pending_unresolved')))
            source = stack.enter_context(patch.object(runtime.entry, 'prepare'))
            publish = stack.enter_context(patch.object(runtime.m, 'publish'))
            with self.assertRaises(ValueError) as caught:
                runtime.run('token', object(), policy={}, approved_policy_sha256='c' * 64,
                            remote=remote, public_client=object(), apply=True, scope={},
                            approved_scope_sha256='d' * 64, run_context={}, checkpoint=lambda _: None)
        self.assertEqual(runtime.failure(caught.exception, attempt_started=False)['stage'], 'pending_gate')
        source.assert_not_called(); publish.assert_not_called(); remote.cas_main.assert_not_called()

    def test_staging_completes_before_one_cas_and_readback(self):
        parent, commit, tree = 'a' * 40, 'b' * 40, 'c' * 40
        files = {'Raw/03 Get/note.md': b'new'}
        plan = {'parent_commit': parent, 'candidate_commit': commit, 'candidate_tree': tree}
        state = {'main': parent}
        reader = types.SimpleNamespace(local=types.SimpleNamespace(run=lambda *args: (commit + ' ' + parent + '\n').encode()))
        remote = types.SimpleNamespace(commit=lambda oid: {'tree': tree, 'parents': [parent]})
        remote.cas_main = Mock(side_effect=lambda before, after: state.update(main=after))
        def guard(_remote, expected=None):
            if expected is not None and state['main'] != expected: raise ValueError('guard')
            return state['main']
        replacements = [
            (runtime, 'validate_policy', {'return_value': {'rules_sha256': 'f' * 64}}),
            (runtime, 'validate_scope', {'return_value': None}),
            (runtime, 'context', {'return_value': {'run_id': 123, 'run_attempt': 1}}),
            (runtime.provenance, 'verify_execution', {'return_value': None}),
            (runtime.m, 'guard', {'side_effect': guard}),
            (runtime.r, 'readback_checkout', {'return_value': object()}),
            (runtime.m, 'load_evidence', {'return_value': (reader, object(), None)}),
            (runtime, 'load_trusted_inputs', {'return_value': ({'commit': parent}, None, None)}),
            (runtime.pending, 'check', {'return_value': parent}),
            (runtime.pending, 'exact_ref', {'return_value': parent}),
            (runtime.entry, 'prepare', {'return_value': (plan, files)}),
            (runtime.ReplaySource, 'seal', {'return_value': None}),
            (runtime.entry, 'verify_plan', {'return_value': None}),
            (runtime.entry, 'public_report', {'side_effect': lambda *_: {'status': 'recurring_local_candidate_verified',
                'plan_sha256': manual.a.sha(manual.a.encoded(plan))}}),
            (runtime, 'hydrate_replaced_blobs', {'return_value': None}),
            (runtime.staging, 'API', {'return_value': object()}),
            (runtime.m, 'fetch_selected', {'return_value': files}),
            (runtime.provenance, 'publication_record', {'side_effect': ValueError('stop after verified CAS')}),
        ]
        with ExitStack() as stack:
            for target, name, options in replacements: stack.enter_context(patch.object(target, name, **options))
            staged = stack.enter_context(patch.object(runtime.staging, 'stage', side_effect=lambda *args: self.assertEqual(state['main'], parent)))
            result = runtime.run('token', object(), policy={}, approved_policy_sha256='d' * 64,
                remote=remote, public_client=object(), apply=True, scope={}, approved_scope_sha256='e' * 64,
                run_context={}, checkpoint=lambda _: None, writers_guard=lambda: None,
                bootstrap={'capture_anchor': {'capture_commit': '1' * 40}})
        staged.assert_called_once()
        remote.cas_main.assert_called_once_with(parent, commit)
        self.assertTrue(result['main_updated'])
        self.assertTrue(result['readback_verified'])
        state['main'] = parent
        remote.cas_main.reset_mock()
        with ExitStack() as stack:
            for target, name, options in replacements: stack.enter_context(patch.object(target, name, **options))
            stack.enter_context(patch.object(runtime.staging, 'stage', side_effect=runtime.staging.StageFailure('gitdata_staging_failed')))
            with self.assertRaises(runtime.staging.StageFailure) as caught:
                runtime.run('token', object(), policy={}, approved_policy_sha256='d' * 64,
                    remote=remote, public_client=object(), apply=True, scope={}, approved_scope_sha256='e' * 64,
                    run_context={}, checkpoint=lambda _: None, writers_guard=lambda: None,
                    bootstrap={'capture_anchor': {'capture_commit': '1' * 40}})
        failure = runtime.failure(caught.exception, attempt_started=False)
        self.assertEqual((failure['stage'], failure['write_state']), ('gitdata_staging', 'not_attempted'))
        remote.cas_main.assert_not_called()

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
            (runtime, 'load_trusted_inputs', {'return_value': ({'commit': parent}, None, None)}),
            (runtime.pending, 'check', {'return_value': parent}),
            (runtime.pending, 'exact_ref', {'return_value': parent}),
            (runtime.staging, 'API', {'return_value': object()}),
            (runtime.entry, 'prepare', {'return_value': (plan, {'Raw/03 Get/note.md': b'new'})}),
            (runtime.ReplaySource, 'seal', {'return_value': None}),
            (runtime.entry, 'verify_plan', {'return_value': None}),
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
