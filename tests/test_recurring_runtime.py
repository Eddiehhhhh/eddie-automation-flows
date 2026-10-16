"""Live orchestration tested with real Git objects and injected network only."""
from copy import deepcopy
from contextlib import ExitStack
import json
import os
import subprocess
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_recurring_runtime as rt
from get_recurring_runtime import a, entry, engine, m, n, provenance as p, r
import test_recurring_entry as git_fixture
import test_publication_provenance as api_fixture


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = git_fixture.RecurringEntryTests(methodName='test_bootstrap_one_direct_candidate_contains_receipt_preserves_refs_and_index')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.reader = self.f.reader; self.local = self.reader.local; self.parent = self.f.parent
        self.bootstrap = self.f.bootstrap; self.rules = entry.rules_sha256()
        self.policy = api_fixture.policy(rules_sha256=self.rules, get_policy_sha256=entry.policy_sha256(self.rules, self.bootstrap))
        self.policy_hash = a.sha(a.encoded(self.policy))
        self.scope = {'contract': rt.SCOPE_CONTRACT, 'production_approved': True,
            'public_policy_sha256': self.policy_hash, 'rules_sha256': self.rules,
            'get_policy_sha256': self.policy['get_policy_sha256'], 'private_repository': r.REPOSITORY,
            'main_ref': r.MAIN_REF, 'max_new': 50, 'schedule_minute': 17,
            'receipt_path': entry.FULL_RECEIPT, 'sink': 'same-controller-confirmed-content-only', 'permissions': 'existing-only',
            'notion_scope_contract': 'get-notion-standing-runtime-1', 'notion_batch_max': 50,
            'notion_journal_path': '.sync/get-notion/state.json',
            'notion_initial_proof_sha256': 'f3b616ec734324e785f4258482fb961ee937161316375ad8b28ea29c7d60f11e'}
        self.scope_hash = a.sha(a.encoded(self.scope))
        self.context = {'run_id': 1002, 'run_attempt': 1, 'code_sha': self.policy['code_sha'], 'controller_sha': 'c' * 40}
        self.store = {oid: self.local.blob(oid) for mode, kind, oid in self.reader.tree(self.parent).values() if kind == 'blob'}
        outer = self
        class Remote:
            def __init__(self): self.head = outer.parent; self.calls = []; self.reads = []; self.local = outer.local
            def metadata(self): return {'full_name': r.REPOSITORY, 'private': True}
            def ref(self, name): return self.head if name == r.MAIN_REF else m.PROTECTED[name]
            def check_source_budget(self, commit):
                self.reads.append(commit)
                return {path: (*row, len(outer.blob(row[2]))) for path, row in outer.reader.tree(commit).items() if path.startswith(r.GET + '/')}
            def cas_main(self, old, new):
                assert old == self.head
                self.calls.append((old, new)); self.head = new
            def commit(self, commit):
                return {'tree': outer.local.run('rev-parse', commit + '^{tree}').decode().strip(),
                        'parents': outer.local.run('rev-list', '--parents', '-n', '1', commit).decode().split()[1:]}
            def source_get_tree(self, commit): return outer.reader.source_get_tree(commit)
            def tree_entries(self, *args, **kwargs): return outer.reader.tree_entries(*args, **kwargs)
        self.remote = Remote(); self.fetches = []; self.events = []; self.commit_fetches = []

    def blob(self, oid):
        return self.store[oid] if oid in self.store else self.local.blob(oid)

    def fetch(self, remote, metadata, token, directory):
        self.fetches.extend(metadata)
        return {path: self.blob(row[2]) for path, row in metadata.items()}

    def patches(self, *, real_evidence=False):
        stack = ExitStack(); self.addCleanup(stack.close)
        real_run = self.local.run
        def git(*args, **kwargs):
            if args[0] == 'fetch':
                self.commit_fetches.append(args); return b''
            return real_run(*args, **kwargs)
        stack.enter_context(patch.object(self.local, 'run', side_effect=git))
        stack.enter_context(patch.object(r, 'readback_checkout', return_value=self.local))
        stack.enter_context(patch.object(m, 'fetch_selected', side_effect=self.fetch))
        stack.enter_context(patch.object(m, 'load_canonical_inputs', return_value=(self.f.f.current, self.f.f.preview)))
        if not real_evidence:
            stack.enter_context(patch.object(m, 'load_evidence', return_value=(self.reader, self.f.evidence, self.f.f.current)))
        else:
            stack.enter_context(patch.object(m.c, 'fetch_blobs', side_effect=self.fetch))
            candidates = {i: {'filePath': v['filePath']} for i, v in self.f.evidence.census['eligible'].items()}
            stack.enter_context(patch.object(m.c, 'authorized_cohort', return_value=({}, candidates, set(self.f.evidence.census['pending_ids']), {}, {})))
        return stack

    def call(self, *, apply=False, source=None, api=None, **kwargs):
        api = api or api_fixture.FakeAPI(); api.policy = self.policy
        api.overrides['/actions/runs/1002/attempts/1'] = api_fixture.run(1002, head_sha=self.context['controller_sha'], status='in_progress', conclusion=None, event='workflow_dispatch')
        return rt.run('test-placeholder', source or self.f.f.source(), policy=self.policy,
            approved_policy_sha256=self.policy_hash, remote=self.remote, public_client=api,
            apply=apply, scope=self.scope if apply else None, approved_scope_sha256=self.scope_hash if apply else None,
            run_context=self.context if apply else None, bootstrap=self.bootstrap,
            checkpoint=lambda plan: self.events.append('checkpoint'), writers_guard=lambda: self.events.append('writers_guard'), **kwargs)

    def activated(self):
        change = patch.object(rt, 'PRODUCTION_ACTIVATED', True); change.start(); self.addCleanup(change.stop)

    def previous(self, notes=None):
        plan, files = self.f.prepare(source=self.f.f.source(notes) if notes else None)
        publication = {'main_updated': True, 'cas_attempts': 1, 'readback_verified': True}
        record = p.publication_record(self.reader, self.f.evidence, plan, files, publication, self.rules,
            run_id=1001, run_attempt=1, code_sha=self.policy['code_sha'], bootstrap=self.bootstrap)
        self.remote.head = plan['candidate_commit']
        return plan, files, api_fixture.FakeAPI(records={1001: record})

    def test_prepare_real_partial_objects_no_cas_no_public_anchor_lookup(self):
        asset = self.reader.tree(self.parent)[r.GET + '/assets/inherited.bin'][2]
        object_path = self.f.f.f.repo / '.git/objects' / asset[:2] / asset[2:]
        object_path.unlink(); refs = self.f.f.f.git('show-ref')
        self.patches(); api = api_fixture.FakeAPI(); source = self.f.f.source()
        result = self.call(source=source, api=api)
        self.assertEqual(result['status'], 'recurring_candidate_verified'); self.assertEqual(self.remote.calls, [])
        self.assertEqual(api.called, []); self.assertEqual(source.ids, ['200', '201'])
        self.assertEqual(result['opaque_fields'], 0); self.assertFalse(result['complete_source_rendering_claimed'])
        self.assertFalse(object_path.exists()); self.assertFalse(any('/assets/' in path for path in self.fetches))
        self.assertEqual(refs, self.f.f.f.git('show-ref'))
        self.assertTrue(any('--depth=1026' in row and '--filter=blob:none' in row for row in self.commit_fetches))

    def test_loads_real_capture_evidence_before_shared_engine(self):
        self.patches(real_evidence=True)
        result = self.call()
        self.assertEqual(result['status'], 'recurring_candidate_verified'); self.assertEqual(result['legacy_pending'], 9)
        self.assertIn(m.c.CENSUS, self.fetches); self.assertIn(m.c.LEDGER, self.fetches)

    def test_real_file_transport_blob_none_clone_materializes_only_selected_bytes(self):
        source_reader = self.reader; source_local = self.local; source_repo = self.f.f.f.repo
        # Local Git's own upload-pack implements a real promisor partial clone.
        # No external URL, token, HTTP request or publication is used.
        self.f.f.f.git('config', 'uploadpack.allowFilter', 'true')
        for commit in self.f.f.f.git('rev-list', '--all').decode().splitlines():
            for mode, kind, oid in source_reader.tree(commit).values():
                if kind == 'blob' and oid not in self.store:
                    self.store[oid] = source_local.blob(oid)
        clone = self.f.f.f.base / 'partial-clone'
        subprocess.run(['git', '-c', 'protocol.file.allow=always', 'clone', '--quiet',
                        '--filter=blob:none', '--no-checkout', source_repo.as_uri(), str(clone)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.reader = a.OfflineGit(clone); self.local = self.reader.local
        self.remote.tree_entries = source_reader.tree_entries
        asset = self.reader.tree(self.parent)[r.GET + '/assets/inherited.bin'][2]
        self.assertTrue(self.local.run('cat-file', '--batch-check', data=(asset + '\n').encode()).endswith(b' missing\n'))
        self.patches(real_evidence=True)
        result = self.call()
        self.assertEqual(result['status'], 'recurring_candidate_verified')
        self.assertTrue(self.local.run('cat-file', '--batch-check', data=(asset + '\n').encode()).endswith(b' missing\n'))
        self.assertFalse(any('/assets/' in path for path in self.fetches))
        self.assertEqual(self.remote.calls, [])

    def test_cli_default_wires_existing_source_and_readonly_runtime(self):
        import sync_getnote_to_raw as sync
        policy_path = self.f.f.f.base / 'public-policy.json'; policy_path.write_bytes(a.encoded(self.policy))
        source = object()
        with patch.object(entry, 'BOOTSTRAP', self.bootstrap), patch.object(m, 'disabled_writers') as guard, patch.object(sync, 'load_credentials', return_value=('existing-key', 'existing-client')) as credentials, patch.object(m.c, 'Source', return_value=source) as constructor, patch.object(rt, 'run', return_value={'status': 'recurring_candidate_verified'}) as run, patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'existing-token', 'GITHUB_TOKEN': 'existing-public-token'}), patch('builtins.print'):
            code = rt.main(['--policy', str(policy_path), '--approved-policy-sha256', self.policy_hash])
        self.assertEqual(code, 0); guard.assert_called_once_with(token='existing-public-token'); credentials.assert_called_once_with()
        self.assertEqual(constructor.call_args.args[:2], ('existing-key', 'existing-client'))
        self.assertEqual(run.call_args.args, ('existing-token', source)); self.assertFalse(run.call_args.kwargs['apply'])
        self.assertIsNone(run.call_args.kwargs['on_publication']); self.assertIsNone(run.call_args.kwargs['checkpoint'])
        self.assertIsInstance(run.call_args.kwargs['public_client'], rt.provenance.PublicGithub)
        with patch.object(m, 'disabled_writers') as final_guard:
            run.call_args.kwargs['writers_guard']()
        final_guard.assert_called_once_with(token='existing-public-token')

    def test_pending_apply_stops_before_remote_or_source(self):
        source = self.f.f.source()
        with patch.object(self.remote, 'metadata', side_effect=AssertionError('network prohibited')):
            with self.assertRaisesRegex(entry.EntryFailure, 'runtime_activation_pending'):
                self.call(apply=True, source=source)
        self.assertEqual(source.lists, 0)

    def test_cli_pending_apply_stops_before_policy_credentials_and_network(self):
        import sync_getnote_to_raw as sync
        with patch.object(sync, 'load_credentials', side_effect=AssertionError('credentials prohibited')), patch.object(m, 'disabled_writers', side_effect=AssertionError('network prohibited')), patch('builtins.print') as output:
            code = rt.main(['--apply', '--policy', '/does/not/exist', '--approved-policy-sha256', 'f' * 64])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.call_args[0][0])['error'], 'runtime_activation_pending')

    def test_apply_once_reads_back_then_emits_then_private_sink(self):
        self.patches(); self.activated(); emitted = []; handed = []
        def emit(line):
            self.assertEqual(len(self.remote.calls), 1); emitted.append(line); self.events.append('emit')
        def sink(handoff):
            self.assertEqual(len(emitted), 1); handed.append(handoff); self.events.append('sink')
        result = self.call(apply=True, on_publication=emit, sink=sink)
        self.assertEqual(result['status'], 'recurring_transaction_verified'); self.assertEqual(result['cas_attempts'], 1)
        self.assertEqual(result['sink_status'], 'callback_returned'); self.assertIsNone(result['notion_writes']); self.assertEqual(handed[0]['changed_content_ids'], ['200', '201'])
        self.assertEqual(self.events, ['writers_guard', 'checkpoint', 'emit', 'sink'])
        self.assertEqual(json.loads(emitted[0][len(p.MARKER):]), result['publication_record'])
        self.assertNotIn('changed_content_ids', json.dumps(result)); self.assertNotIn('Synthetic', json.dumps(result))

    def test_sink_failure_preserves_get_record_and_discoverable_success(self):
        self.patches(); self.activated(); lines = []
        def sink(_): raise RuntimeError('Private sink failure body')
        result = self.call(apply=True, on_publication=lines.append, sink=sink)
        self.assertTrue(result['readback_verified']); self.assertTrue(result['main_updated'])
        self.assertEqual(result['sink_status'], 'partial'); self.assertEqual(result['publication_output'], 'confirmed')
        self.assertEqual(len(lines), 1); self.assertNotIn('Private', json.dumps(result))
        api = api_fixture.FakeAPI([api_fixture.run(1002, head_sha=self.context['controller_sha'])], {1002: result['publication_record']}); api.policy = self.policy
        verified = p.discover(api, self.policy)
        self.assertEqual(verified['anchor']['commit'], self.remote.head)

    def test_marker_callback_failure_preserves_confirmed_get_and_skips_sink(self):
        self.patches(); self.activated(); handed = []
        def emit(_): raise OSError('log output failure')
        result = self.call(apply=True, on_publication=emit, sink=handed.append)
        self.assertTrue(result['main_updated']); self.assertTrue(result['readback_verified'])
        self.assertEqual(result['publication_output'], 'failed'); self.assertEqual(handed, [])
        self.assertIn('publication_record', result)

    def test_auto_anchor_noop_no_second_cas_and_zero_sink(self):
        first, _, api = self.previous(); self.patches(); self.activated(); handed = []
        result = self.call(apply=True, api=api, sink=handed.append)
        self.assertEqual(result['changed_paths'], 0); self.assertEqual(self.remote.calls, []); self.assertEqual(handed, [])
        self.assertEqual(result['sink_status'], 'no_content_change'); self.assertEqual(result['source_details'], 0)
        self.assertEqual(result['publication_record']['receipt_commit'], first['candidate_commit'])
        self.assertIn('logs:1501', api.called)

    def test_same_run_retry_passes_exact_exclusion_and_recovers_confirmed_first(self):
        first, _, api = self.previous(); self.patches(); self.activated()
        original = api_fixture.run(event='workflow_dispatch')
        current = api_fixture.run(attempt=2, minute=9, event='workflow_dispatch', status='in_progress', conclusion=None)
        api.runs = [current]; api.attempts = {(1001, 1): original, (1001, 2): current}
        self.context = {'run_id': 1001, 'run_attempt': 2, 'code_sha': self.policy['code_sha'], 'controller_sha': original['head_sha']}
        with patch.object(p, 'discover', wraps=p.discover) as discover:
            result = self.call(apply=True, api=api)
        self.assertEqual(discover.call_args.kwargs, {'exclude_run_id': 1001, 'exclude_run_attempt': 2})
        self.assertEqual(result['changed_paths'], 0); self.assertEqual(self.remote.calls, [])
        self.assertEqual(result['publication_record']['receipt_commit'], first['candidate_commit'])
        self.assertEqual(result['publication_record']['run_attempt'], 2)
        self.assertIn('logs:1501', api.called)
        # The new attempt's marker is earned only after its own exact readback.
        self.assertTrue(result['publication_record']['readback_verified'])

    def test_state_only_update_has_receipt_and_no_sink(self):
        _, _, api = self.previous(); self.patches(); self.activated(); handed = []
        notes = deepcopy(self.f.f.notes); notes['200']['version'] = 1
        result = self.call(apply=True, api=api, source=self.f.f.source(notes), sink=handed.append)
        self.assertEqual(result['notes_changed'], 0); self.assertEqual(result['changed_paths'], 2)
        self.assertEqual(len(self.remote.calls), 1); self.assertEqual(handed, [])
        self.assertEqual(result['publication_record']['receipt_sequence'], 2)

    def test_missing_receipt_and_registry_blobs_are_explicitly_fetched(self):
        first, files, api = self.previous()
        for path, data in files.items():
            oid = r.oid(data); self.store[oid] = data
            object_path = self.f.f.f.repo / '.git/objects' / oid[:2] / oid[2:]
            if object_path.exists(): object_path.unlink()
        self.patches(); result = self.call(api=api)
        self.assertEqual(result['changed_paths'], 0)
        self.assertIn(entry.FULL_RECEIPT, self.fetches)
        self.assertIn(r.GET + '/' + n.STATE, self.fetches)
        self.assertEqual(result['receipt_sha256'], first['receipt_sha256'])

    def test_disconnect_after_cas_is_read_back_once_before_marker(self):
        self.patches(); self.activated(); original = self.remote.cas_main; lines = []
        def disconnected(old, new):
            original(old, new)
            raise OSError('Connection lost after accepted update')
        with patch.object(self.remote, 'cas_main', side_effect=disconnected):
            result = self.call(apply=True, on_publication=lines.append)
        self.assertEqual(result['status'], 'recurring_transaction_verified')
        self.assertEqual(len(self.remote.calls), 1); self.assertEqual(len(lines), 1)

    def test_unrelated_notion_journal_is_preserved(self):
        first, _, api = self.previous()
        self.remote.head = self.f.commit_change(first['candidate_commit'], {'.sync/get-notion/state.json': b'Private unrelated journal'})
        self.patches(); self.activated(); result = self.call(apply=True, api=api)
        self.assertEqual(result['changed_paths'], 0)
        self.assertEqual(result['publication_record']['observed_main_commit'], self.remote.head)
        self.assertEqual(result['publication_record']['receipt_commit'], first['candidate_commit'])

    def test_foreign_get_change_and_stale_anchor_stop_before_source(self):
        first, _, api = self.previous(); path = r.GET + '/assets/new.bin'
        self.remote.head = self.f.commit_change(first['candidate_commit'], {path: b'Foreign'})
        self.patches(); source = self.f.f.source()
        with self.assertRaisesRegex(entry.EntryFailure, 'entry_foreign_get_change'):
            self.call(api=api, source=source)
        self.assertEqual(source.lists, 0); self.assertEqual(self.remote.calls, [])

    def test_wrong_scope_rules_context_stop_before_network(self):
        self.activated()
        with patch.object(self.remote, 'metadata', side_effect=AssertionError('network prohibited')):
            for key, value in [('max_new', 51), ('private_repository', 'Other/repo'), ('sink', 'arbitrary')]:
                original = self.scope; self.scope = {**original, key: value}; self.scope_hash = a.sha(a.encoded(self.scope))
                with self.assertRaisesRegex(entry.EntryFailure, 'runtime_scope'): self.call(apply=True)
                self.scope = original
            self.scope_hash = a.sha(a.encoded(self.scope)); self.context['code_sha'] = 'f' * 40
            with self.assertRaisesRegex(entry.EntryFailure, 'runtime_context'): self.call(apply=True)

    def test_foreign_current_controller_stops_before_private_metadata_or_source(self):
        self.activated(); source = self.f.f.source(); api = api_fixture.FakeAPI()
        api.overrides['/contents/' + self.policy['workflow_path'] + '?ref=' + self.context['controller_sha']] = {'type': 'file', 'path': self.policy['workflow_path'], 'encoding': 'base64', 'size': 0, 'sha': r.oid(b''), 'content': ''}
        with patch.object(self.remote, 'metadata', side_effect=AssertionError('private network prohibited')):
            with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_mismatch'):
                self.call(apply=True, source=source, api=api)
        self.assertEqual(source.lists, 0)

    def test_expected_parent_race_stops_without_source(self):
        self.patches(); source = self.f.f.source()
        with self.assertRaisesRegex(m.ManualFailure, 'manual_parent_changed'):
            self.call(source=source, expected_parent='f' * 40)
        self.assertEqual(source.lists, 0); self.assertEqual(self.remote.calls, [])

    def test_missing_outcome_is_unknown_and_checkpoint_exception_is_uncertain(self):
        self.assertEqual(rt.failure()['write_state'], 'unknown')
        self.assertIsNone(rt.failure()['main_updated'])
        value = rt.failure(RuntimeError('private error'), attempt_started=True)
        self.assertEqual(value['write_state'], 'uncertain'); self.assertIsNone(value['main_updated'])
        self.assertNotIn('private', json.dumps(value))

    def test_readback_failure_cannot_emit_or_enqueue(self):
        self.patches(); self.activated(); lines = []; handed = []
        with patch.object(self.remote, 'commit', return_value={'tree': 'f' * 40, 'parents': []}):
            with self.assertRaises(m.ManualFailure) as caught:
                self.call(apply=True, on_publication=lines.append, sink=handed.append)
        self.assertEqual(caught.exception.write_state, 'confirmed')
        self.assertEqual(len(self.remote.calls), 1); self.assertEqual(lines, []); self.assertEqual(handed, [])

    def test_prepare_disallows_sink_and_emission(self):
        with patch.object(self.remote, 'metadata', side_effect=AssertionError('network prohibited')):
            with self.assertRaisesRegex(entry.EntryFailure, 'runtime_scope'): self.call(sink=lambda _: None)
            with self.assertRaisesRegex(entry.EntryFailure, 'runtime_scope'): self.call(on_publication=lambda _: None)


if __name__ == '__main__':
    unittest.main()
