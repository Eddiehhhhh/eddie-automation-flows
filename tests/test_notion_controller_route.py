"""Offline single-controller routing and immutable private-input tests."""
import contextlib
from copy import deepcopy
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_controller_route as route
import get_notion_executor as e
import get_notion_historical as h
import get_notion_historical_input as inputs
import get_notion_properties_runtime as properties
import get_notion_runtime as runtime
import get_notion_historical_runtime as historical_runtime
import get_notion_outbox as outbox

N = '1234567890123456789'
DS = 'd' * 32
ROOT = Path(__file__).resolve().parents[1]


def manifest(mode):
    source = {'path': 'Raw/03 Get/notes/' + N + '.md', 'blob': 'a' * 40, 'sha256': 'b' * 64}
    if mode == 'property-apply':
        return {'contract': properties.tx.CONTRACT, 'source_commit': 'c' * 40,
                'data_source_id': DS, 'schema_sha256': 'f' * 64,
                'fields': sorted(properties.props.FIELDS), 'fill_empty': True,
                'records': {N: {**source, 'privacy_review_sha256': 'e' * 64}}, 'registry': None}
    target = {'page_id': 'e' * 32}
    if mode == 'historical-apply':
        target.update(snapshot_sha256='f' * 64,
                      placeholder_blocks=[{'id': 'f' * 32, 'normalized_hash': 'a' * 64}])
    return {'contract': h.CAPTURE_CONTRACT if mode == 'historical-capture' else h.CONTRACT,
            'source_commit': 'c' * 40, 'data_source_id': DS, 'database_id': 'b' * 32,
            'audit_sha256': 'e' * 64, 'user_scope_sha256': 'f' * 64,
            'records': {N: {**source, 'mode': 'existing_placeholder', 'target': target,
                'review': {'source_sha256': source['sha256'], 'review_evidence_sha256': 'a' * 64,
                           'destination_data_source_id': DS, 'credential_free': True,
                           'transmission_approved': True, 'media_free': True}}}}


class Remote:
    def __init__(self, value):
        self.raw = e.encoded(value); self.expected = e.digest(value)
        self.anchor = {'commit': 'a' * 40, 'blob': e.p.git_blob(self.raw),
                       'path': inputs.PREFIX + self.expected + '.json', 'size': len(self.raw)}
        self.meta = {'full_name': inputs.remote_module.REPOSITORY, 'private': True}
        self.rows = [{'path': self.anchor['path'], 'mode': '100644',
                      'type': 'blob', 'sha': self.anchor['blob']}]
        self.calls = []
    def metadata(self):
        self.calls.append(('metadata',)); return self.meta
    def commit(self, commit):
        assert commit == self.anchor['commit']
        self.calls.append(('commit', commit)); return {'tree': 'b' * 40}
    def tree_entries(self, tree, recursive=False):
        assert tree == 'b' * 40 and recursive is True
        self.calls.append(('tree', tree)); return self.rows
    def blob(self, blob, size):
        assert blob == self.anchor['blob'] and size == self.anchor['size']
        self.calls.append(('blob', blob, size)); return self.raw


class RouteTests(unittest.TestCase):
    def registered(self, mode):
        remote = Remote(manifest(mode)); stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        module, name = {
            'historical-capture': (h, 'APPROVED_CAPTURE_SCOPES'),
            'historical-apply': (h, 'APPROVED_MANIFESTS'),
            'property-apply': (properties, 'APPROVED_MANIFESTS'),
        }[mode]
        stack.enter_context(patch.object(module, name, frozenset({remote.expected})))
        stack.enter_context(patch.object(inputs, 'INPUT_ANCHORS', {remote.expected: remote.anchor}))
        return remote

    @staticmethod
    def env(mode='canonical', expected='', event='workflow_dispatch', apply='true'):
        return {'GITHUB_EVENT_NAME': event, 'APPLY': apply,
                'NOTION_MODE': mode, 'NOTION_INPUT_SHA256': expected}

    def test_all_activation_registries_remain_empty(self):
        self.assertEqual(inputs.INPUT_ANCHORS, {})
        for registry in (h.APPROVED_MANIFESTS, h.APPROVED_CAPTURE_SCOPES, properties.APPROVED_MANIFESTS):
            self.assertEqual(registry, frozenset())

    def test_canonical_default_manual_preview_and_every_schedule(self):
        with patch.object(inputs, 'anchor_for', side_effect=AssertionError('no input')):
            for event, apply in [('workflow_dispatch', False), ('workflow_dispatch', True), ('schedule', True)]:
                self.assertIsNone(route.preflight(event, apply, 'canonical', ''))
            with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'schedule', 'APPLY': 'true'}, clear=True):
                self.assertEqual(route.selection(), ('canonical', '', None))

    def test_invalid_event_apply_mode_and_canonical_input_fail_closed(self):
        cases = [('push', True, 'canonical', ''), ('schedule', False, 'canonical', ''),
                 ('workflow_dispatch', 1, 'canonical', ''), ('workflow_dispatch', 'true', 'canonical', ''),
                 ('workflow_dispatch', True, 'foreign', ''), ('workflow_dispatch', True, '', ''),
                 ('workflow_dispatch', True, 'canonical', 'a' * 64),
                 ('workflow_dispatch', True, 'canonical', None)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(Exception):
                route.preflight(*args)

    def test_bounded_modes_require_manual_apply_own_allowlist_and_anchor(self):
        for mode in route.MODES[1:]:
            remote = Remote(manifest(mode))
            with self.subTest(mode=mode), self.assertRaisesRegex(Exception, 'approval_missing'):
                route.preflight('workflow_dispatch', True, mode, remote.expected)
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(h, 'APPROVED_CAPTURE_SCOPES', {remote.expected}))
                stack.enter_context(patch.object(h, 'APPROVED_MANIFESTS', {remote.expected}))
                stack.enter_context(patch.object(properties, 'APPROVED_MANIFESTS', {remote.expected}))
                with self.assertRaisesRegex(Exception, 'approval_missing'):
                    route.preflight('workflow_dispatch', True, mode, remote.expected)
                stack.enter_context(patch.object(inputs, 'INPUT_ANCHORS', {remote.expected: remote.anchor}))
                for event, apply in [('schedule', True), ('workflow_dispatch', False)]:
                    with self.assertRaisesRegex(Exception, 'approval_missing'):
                        route.preflight(event, apply, mode, remote.expected)
                self.assertEqual(route.preflight('workflow_dispatch', True, mode, remote.expected),
                                 manifest(mode)['contract'])

    def test_capture_registration_never_authorizes_body_or_property_apply(self):
        remote = self.registered('historical-capture')
        for mode in ('historical-apply', 'property-apply'):
            with self.assertRaisesRegex(Exception, 'approval_missing'):
                route.preflight('workflow_dispatch', True, mode, remote.expected)

    def test_bad_registration_rejected_before_private_metadata(self):
        remote = self.registered('historical-capture')
        changes = [('commit', 'main'), ('blob', 'x' * 40), ('path', 'arbitrary.json'),
                   ('size', True), ('size', 0), ('size', 256 * 1024 + 1)]
        for key, value in changes:
            with self.subTest(key=key, value=value), patch.object(
                    inputs, 'INPUT_ANCHORS', {remote.expected: {**remote.anchor, key: value}}):
                with self.assertRaises(Exception):
                    route.preflight('workflow_dispatch', True, 'historical-capture', remote.expected)
        self.assertFalse(remote.calls)

    def test_all_three_exact_private_contracts_are_readable(self):
        for mode in route.MODES[1:]:
            remote = Remote(manifest(mode))
            with self.subTest(mode=mode), patch.object(inputs, 'INPUT_ANCHORS', {remote.expected: remote.anchor}):
                self.assertEqual(inputs.read(remote, remote.expected, contract=manifest(mode)['contract']), remote.raw)
                self.assertEqual(remote.calls, [('metadata',), ('commit', remote.anchor['commit']),
                    ('tree', 'b' * 40), ('blob', remote.anchor['blob'], remote.anchor['size'])])

    def test_input_contract_cannot_be_swapped_between_registered_modes(self):
        remote = self.registered('historical-capture')
        for contract in (h.CONTRACT, properties.tx.CONTRACT, 'unknown'):
            with self.subTest(contract=contract), self.assertRaisesRegex(Exception, 'approval_missing'):
                inputs.read(remote, remote.expected, contract=contract)

    def test_input_wrong_repository_public_tree_and_blob_are_rejected(self):
        for kind in ('repository', 'public', 'path', 'mode', 'type', 'blob', 'duplicate', 'bytes', 'size'):
            remote = Remote(manifest('historical-capture'))
            if kind == 'repository': remote.meta['full_name'] = 'other/repository'
            elif kind == 'public': remote.meta['private'] = False
            elif kind == 'duplicate': remote.rows *= 2
            elif kind == 'bytes': remote.raw += b'\n'
            elif kind == 'size': remote.anchor['size'] += 1
            else: remote.rows[0][{'blob': 'sha'}.get(kind, kind)] = 'foreign'
            with self.subTest(kind=kind), patch.object(inputs, 'INPUT_ANCHORS', {remote.expected: remote.anchor}):
                with self.assertRaises(Exception): inputs.read(remote, remote.expected)

    def test_unknown_contract_and_self_hashed_noncanonical_json_are_rejected(self):
        for raw in (e.encoded({'contract': 'foreign'}), b'{ "contract": "foreign" }'):
            remote = Remote({'contract': 'foreign'}); remote.raw = raw
            remote.anchor.update(blob=e.p.git_blob(raw), size=len(raw))
            with patch.object(inputs, 'INPUT_ANCHORS', {remote.expected: remote.anchor}):
                with self.assertRaises(Exception): inputs.read(remote, remote.expected)

    def test_preflight_never_constructs_remote_or_runtime(self):
        for mode, expected, result in [('canonical', '', 0), ('historical-capture', 'a' * 64, 1)]:
            with self.subTest(mode=mode), patch.dict(os.environ, self.env(mode, expected), clear=True), \
                    patch.object(inputs.remote_module, 'GitHubRemote', side_effect=AssertionError('no credentials')), \
                    patch.object(runtime, 'main', side_effect=AssertionError('no runtime')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(route.main(['--preflight']), result)

    def test_every_manual_route_calls_same_cli_with_0600_input_then_removes_it(self):
        for mode in route.MODES[1:]:
            remote = self.registered(mode); paths = []
            with tempfile.TemporaryDirectory() as folder:
                def invoke(argv):
                    flag = {'historical-capture': 'historical-capture',
                            'historical-apply': 'historical-manifest',
                            'property-apply': 'property-manifest'}[mode]
                    path = Path(argv[argv.index('--' + flag) + 1]); paths.append(path)
                    self.assertEqual(path.parent, Path(folder))
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(path.read_bytes(), remote.raw)
                    self.assertEqual(argv[argv.index('--approved-' + flag + '-sha256') + 1], remote.expected)
                    self.assertIn('--apply', argv); self.assertIn('--scope-from-policy', argv)
                    self.assertEqual(argv[argv.index('--publication-file') + 1], 'confirmed')
                    return 7
                with patch.dict(os.environ, {**self.env(mode, remote.expected), 'RUNNER_TEMP': folder}, clear=True), \
                        patch.object(inputs.remote_module, 'GitHubRemote', return_value=remote), \
                        patch.object(runtime, 'main', side_effect=invoke):
                    self.assertEqual(route.main(['--policy', 'policy', '--approved-policy-sha256', 'a' * 64,
                        '--publication-file', 'confirmed', '--apply', '--scope-from-policy']), 7)
                self.assertTrue(paths); self.assertFalse(paths[0].exists())
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_runtime_exception_still_removes_private_file(self):
        remote = self.registered('property-apply')
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {**self.env('property-apply', remote.expected), 'RUNNER_TEMP': folder}, clear=True), \
                patch.object(inputs.remote_module, 'GitHubRemote', return_value=remote), \
                patch.object(runtime, 'main', side_effect=RuntimeError('private message')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(route.main(['--policy', 'policy', '--approved-policy-sha256', 'a' * 64,
                '--publication-file', 'confirmed', '--apply', '--scope-from-policy']), 1)
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual(output.getvalue(), 'notion_route_blocked\n')

    def test_invalid_route_or_missing_runtime_arguments_never_construct_remote(self):
        with patch.dict(os.environ, self.env('historical-apply', 'a' * 64), clear=True), \
                patch.object(inputs.remote_module, 'GitHubRemote', side_effect=AssertionError('no credentials')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(route.main([]), 1)
        remote = self.registered('property-apply')
        for tail in ([], ['--apply'], ['--scope-from-policy']):
            with patch.dict(os.environ, self.env('property-apply', remote.expected), clear=True), \
                    patch.object(inputs.remote_module, 'GitHubRemote', side_effect=AssertionError('no credentials')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(route.main(['--policy', 'policy', '--approved-policy-sha256', 'a' * 64,
                    '--publication-file', 'confirmed'] + tail), 1)

    def test_controller_keeps_one_writer_and_preflight_before_any_get_secret(self):
        import yaml
        text = (ROOT / '.github/workflow-templates/get-canonical-controller.yml.disabled').read_text()
        doc = yaml.safe_load(text); steps = doc['jobs']['canonical-get']['steps']
        self.assertEqual(set(doc['jobs']), {'canonical-get'})
        self.assertEqual(doc['concurrency'], {'group': 'eddie-private-vault-writers', 'cancel-in-progress': False})
        index = next(i for i, step in enumerate(steps) if step.get('name') == 'Validate bounded Notion route before Get')
        for step in steps[:index + 1]:
            self.assertNotIn('secrets.', str(step))
        self.assertLess(index, next(i for i, step in enumerate(steps) if step.get('id') == 'get_sync'))
        self.assertEqual(text.count('python scripts/get_notion_runtime.py'), 1)
        self.assertEqual(text.count("cron: '17 * * * *'"), 1)
        for step in steps:
            self.assertNotIn('${{ inputs.', step.get('run', ''))
        inputs_schema = doc[True]['workflow_dispatch']['inputs']
        self.assertEqual(inputs_schema['notion_mode']['default'], 'canonical')
        self.assertEqual(inputs_schema['notion_mode']['options'], list(route.MODES))
        self.assertEqual(set(inputs_schema), {'apply', 'notion_mode', 'notion_input_sha256'})

    def test_every_regression_workflow_installs_locked_parser_before_tests(self):
        for name in ('get-notion-properties-offline.yml', 'test-weread-canonical-paths.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn("'requirements-notion-properties.txt'", text)
            self.assertLess(text.index('--require-hashes --only-binary=:all: --no-deps'),
                            text.index('-m unittest'))

    def historical_fixture(self):
        import test_notion_historical as fixture
        import test_notion_outbox as outer
        case = fixture.Tests(methodName='test_historical_append_timeout_resumes_without_reappend')
        case.setUp(); self.addCleanup(case.doCleanups); case.historical()
        store = outer.Store()
        return case, store

    def test_capture_rejects_even_its_own_active_body_journal_before_loading(self):
        case, store = self.historical_fixture()
        plan = case.historical_plan()
        store.value['engine_state']['journal'] = {
            'plan': plan, 'plan_sha256': e.digest(plan), 'index': 0, 'operation': None}
        candidate = deepcopy(case.manifest); candidate['contract'] = h.CAPTURE_CONTRACT
        candidate['records'][N]['target'] = {'page_id': candidate['records'][N]['target']['page_id']}
        before = store.read()
        with patch.object(historical_runtime.source_loader, 'Loader', side_effect=AssertionError('no source read')):
            with self.assertRaisesRegex(Exception, 'journal_busy'):
                historical_runtime.capture(None, None, None, None, store, None, candidate=candidate,
                    expected=e.digest(candidate), policy_hash='a' * 64, apply=False,
                    writers_guard=lambda: case.policy['old_writer_evidence'])
        self.assertEqual(store.read(), before)

    def test_historical_rejects_canonical_and_foreign_body_journals(self):
        case, store = self.historical_fixture()
        for foreign in ('canonical', 'other-manifest'):
            plan = case.historical_plan()
            if foreign == 'canonical': plan['historical_only'] = False
            else: plan['historical_manifest_sha256'] = 'f' * 64
            store.value['engine_state']['journal'] = {
                'plan': plan, 'plan_sha256': e.digest(plan), 'index': 0, 'operation': None}
            before = store.read()
            with patch.object(runtime.transport.native, 'DATA_SOURCE_PIN', runtime.transport.native.sha(DS)), \
                    patch.object(runtime.transport.native, 'DATABASE_PIN', runtime.transport.native.sha('b' * 32)), \
                    patch.object(historical_runtime.source_loader, 'Loader', side_effect=AssertionError('no source read')):
                with self.assertRaisesRegex(Exception, 'journal_busy'):
                    historical_runtime.dispatch(None, None, None, None, store, None,
                        manifest=case.manifest, expected=e.digest(case.manifest),
                        policy_hash='a' * 64, apply=False, writers_guard=lambda: case.policy['old_writer_evidence'])
            self.assertEqual(store.read(), before)

    def test_historical_resumes_only_completely_matching_manifest_without_reappend(self):
        case, store = self.historical_fixture()
        plan = case.historical_plan(); case.api.throw = 'append_after'
        self.assertEqual(case.run_plan(plan)['reason'], 'uncertain_append')
        case.api.throw = None
        store.value['engine_state'] = case.store.read()[0]
        case.api.store = outbox.EngineStore(store)
        expected = e.digest(case.manifest)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(h, 'APPROVED_MANIFESTS', {expected}))
            stack.enter_context(patch.object(runtime.transport.native, 'DATA_SOURCE_PIN', runtime.transport.native.sha(DS)))
            stack.enter_context(patch.object(runtime.transport.native, 'DATABASE_PIN', runtime.transport.native.sha('b' * 32)))
            stack.enter_context(patch.object(historical_runtime.source_loader, 'Loader'))
            stack.enter_context(patch.object(historical_runtime, 'API', return_value=case.api))
            stack.enter_context(patch.object(runtime, 'schema', return_value='Title'))
            def call(apply):
                return historical_runtime.dispatch(SimpleNamespace(vault=case.repo), None, None, case.root,
                    store, runtime.transport.Transport('synthetic-only'), manifest=case.manifest,
                    expected=expected, policy_hash='a' * 64, apply=apply,
                    writers_guard=lambda: deepcopy(case.policy['old_writer_evidence']))
            pending = store.read()
            self.assertEqual(call(False)['status'], 'prepared')
            self.assertEqual(store.read(), pending)
            # A matching top-level digest cannot conceal a changed embedded manifest.
            active = store.value['engine_state']['journal']
            active['plan']['historical_manifest']['audit_sha256'] = 'f' * 64
            active['plan_sha256'] = e.digest(active['plan'])
            with self.assertRaisesRegex(Exception, 'journal_busy'): call(True)
            store.value = deepcopy(pending[0])
            result = call(True)
            self.assertEqual(result['execution']['status'], 'verified', result)
        self.assertIsNone(store.value['engine_state']['journal'])
        self.assertEqual(case.writes(), [('append',)])

    def test_property_same_hash_with_changed_embedded_manifest_cannot_resume(self):
        import test_notion_properties_runtime as fixture
        case = fixture.Tests(methodName='test_saved_property_transaction_resumes_read_only_after_uncertain_write')
        case.setUp(); self.addCleanup(case.doCleanups)
        with patch.object(properties, 'APPROVED_MANIFESTS', {case.sha}):
            case.f.mode = 'after'
            self.assertEqual(case.call(True)['execution']['reason'], 'uncertain_properties')
            case.f.mode = None
            active = case.store.value['engine_state']['journal']
            active['plan']['property_manifest']['schema_sha256'] = 'f' * 64
            active['plan_sha256'] = e.digest(active['plan'])
            before = case.store.read()
            with self.assertRaisesRegex(Exception, 'journal_busy'): case.call(True)
            self.assertEqual(case.store.read(), before)
            self.assertEqual(len(case.f.calls), 1)


if __name__ == '__main__':
    unittest.main()
