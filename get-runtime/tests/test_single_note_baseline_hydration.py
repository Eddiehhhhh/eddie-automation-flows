"""Single-note downloads must not hydrate unrelated immutable baselines."""
import base64
from copy import deepcopy
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
bindings = types.ModuleType('get_private_runtime_bindings')
bindings.value = lambda *args: '0' * 64
with patch.dict(sys.modules, {'get_private_runtime_bindings': bindings}):
    runtime = importlib.import_module('get_recurring_runtime')


class BaselineHydration(unittest.TestCase):
    def setUp(self):
        self.parent = 'a' * 40
        self.baseline_calls = []
        self.downloads = []
        self.imports = []
        self.legacy = {'legacyById': {}, 'conflictsById': {'111': 'held'}, 'pending_ids': ['555']}
        self.registry = {'newById': {}, 'reserved': {'111': 'legacy'}, 'scan_cursor': '999'}
        self.blobs = {}
        self.tree = {}
        for ident, kind in (('111', 'legacy'), ('222', 'legacy'), ('333', 'new'), ('444', 'new')):
            data = ('verified-baseline-' + ident).encode()
            blob = runtime.r.oid(data)
            path = '03 Notes/' + ident + '.md'
            self.blobs[blob] = data
            self.tree[runtime.r.GET + '/' + path] = ('100644', 'blob', blob)
            if kind == 'legacy':
                self.legacy['legacyById'][ident] = {'ownership': {
                    'filePath': path, 'mirror_sha256': runtime.a.sha(data)}, 'baseline_blob': blob}
            else:
                self.registry['newById'][ident] = {'filePath': path,
                    'mirror_sha256': runtime.a.sha(data), 'baseline_blob': blob}
        testcase = self
        class Local:
            def run(self, *args, data=None):
                if args[0] == 'rev-list': return b''
                if args[:3] == ('hash-object', '-w', '--stdin'):
                    testcase.imports.append(data)
                    return runtime.r.oid(data).encode() + b'\n'
                raise AssertionError(args)
        class Reader:
            local = Local()
            def baseline(self, commit, path, blob, digest):
                testcase.baseline_calls.append((commit, path, blob, digest))
                raise runtime.a.ParityFailure('baseline_missing')
            def tree(self, commit): return testcase.tree
        class Remote:
            def request(self, path, **kwargs):
                testcase.downloads.append(path)
                blob = path.rsplit('/', 1)[-1]
                data = testcase.blobs[blob]
                return {'sha': blob, 'encoding': 'base64', 'size': len(data),
                        'content': base64.b64encode(data).decode()}
        self.reader, self.remote = Reader(), Remote()

    def assert_downloads(self, ids):
        self.assertEqual({Path(call[1]).stem for call in self.baseline_calls}, set(ids))
        wanted = {runtime.r.oid(('verified-baseline-' + ident).encode()) for ident in ids}
        self.assertEqual({path.rsplit('/', 1)[-1] for path in self.downloads}, wanted)
        self.assertEqual({runtime.r.oid(data) for data in self.imports}, wanted)

    def test_selected_legacy_or_canonical_id_only_fetches_its_baseline(self):
        for ident in ('111', '333', '999'):
            with self.subTest(source_id=ident):
                self.baseline_calls.clear(); self.downloads.clear(); self.imports.clear()
                before = deepcopy((self.legacy, self.registry))
                runtime.hydrate_baselines(self.reader, self.legacy, self.registry,
                    self.parent, self.remote, source_id=ident)
                self.assert_downloads([] if ident == '999' else [ident])
                self.assertEqual((self.legacy, self.registry), before)

    def test_complete_catalog_still_fetches_all_baselines(self):
        before = deepcopy((self.legacy, self.registry))
        runtime.hydrate_baselines(self.reader, self.legacy, self.registry,
            self.parent, self.remote)
        self.assert_downloads(['111', '222', '333', '444'])
        self.assertEqual((self.legacy, self.registry), before)

    def test_publication_hydrates_only_replaced_parent_blobs(self):
        old = b'previous note'
        path = runtime.r.GET + '/03 Notes/old.md'
        new = runtime.r.GET + '/03 Notes/new.md'
        other = runtime.r.GET + '/03 Notes/unrelated.md'
        oid = runtime.r.oid(old)
        tree = {path: ('100644', 'blob', oid), other: ('100644', 'blob', runtime.r.oid(b'other'))}
        reader = types.SimpleNamespace(tree=lambda commit: tree, local=self.reader.local)
        remote = types.SimpleNamespace(check_source_budget=lambda commit: {
            path: ('100644', 'blob', oid, len(old)),
            other: ('100644', 'blob', tree[other][2], 5)})
        with patch.object(runtime.m, 'fetch_selected', return_value={path: old}) as fetch:
            runtime.hydrate_replaced_blobs(reader, remote, 'token', Path('/unused'), self.parent,
                                           {path: b'updated', new: b'new'})
        self.assertEqual(set(fetch.call_args.args[1]), {path})
        self.assertEqual(self.imports, [old])
        with patch.object(runtime.m, 'fetch_selected', return_value={path: b'wrong size'}):
            with self.assertRaises(runtime.a.ParityFailure):
                runtime.hydrate_replaced_blobs(reader, remote, 'token', Path('/unused'), self.parent,
                                               {path: b'updated'})
        with patch.object(runtime.m, 'fetch_selected', return_value={path: b'x' * len(old)}):
            with self.assertRaises(runtime.a.ParityFailure):
                runtime.hydrate_replaced_blobs(reader, remote, 'token', Path('/unused'), self.parent,
                                               {path: b'updated'})
        for bad_row in (('100644', 'blob', '0' * 40, len(old)),
                        ('100755', 'blob', oid, len(old))):
            bad_remote = types.SimpleNamespace(check_source_budget=lambda commit: {path: bad_row})
            with self.subTest(metadata=bad_row[:2]), patch.object(runtime.m, 'fetch_selected') as fetch:
                with self.assertRaises(runtime.a.ParityFailure):
                    runtime.hydrate_replaced_blobs(reader, bad_remote, 'token', Path('/unused'),
                                                   self.parent, {path: b'updated'})
                fetch.assert_not_called()
        with patch.object(runtime.m, 'fetch_selected') as fetch:
            with self.assertRaises(runtime.a.ParityFailure):
                runtime.hydrate_replaced_blobs(reader, remote, 'token', Path('/unused'), self.parent,
                                               {'Wiki/unexpected.md': b'updated'})
            fetch.assert_not_called()

    def test_pinned_migration_receipt_hydrates_only_when_not_already_anchored(self):
        anchor = {'commit': 'e' * 40}
        migration1, migration2 = 'b' * 40, 'c' * 40
        before_tree, after_tree, final_tree = '1' * 40, '2' * 40, '3' * 40
        reader = types.SimpleNamespace(
            local=self.reader.local,
            source_get_tree=lambda commit: {
                anchor['commit']: before_tree, self.parent: final_tree,
                migration1: after_tree, migration2: final_tree}[commit])
        pin = (
            (anchor['commit'], migration1, before_tree, after_tree,
             runtime.a.sha(b'receipt eight'), 8),
            (migration1, migration2, after_tree, final_tree,
             runtime.a.sha(b'receipt nine'), 9),
        )
        writable_local = object()
        with patch.object(runtime.entry, 'PILOT_MIGRATION', pin), \
             patch.object(runtime, 'fetch_commit') as fetch, \
             patch.object(runtime, 'load_receipt') as load:
            runtime.hydrate_pilot_migration_receipt(writable_local, reader, self.remote, 'token',
                Path('/unused'), anchor, self.parent, after_sequence=7)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [migration1, migration2])
        self.assertEqual([call.args[-1] for call in load.call_args_list], [migration1, migration2])

        reader.source_get_tree = lambda commit: {
            anchor['commit']: before_tree, self.parent: after_tree,
            migration1: after_tree, migration2: final_tree}[commit]
        with patch.object(runtime.entry, 'PILOT_MIGRATION', pin), \
             patch.object(runtime, 'fetch_commit') as fetch, \
             patch.object(runtime, 'load_receipt') as load:
            runtime.hydrate_pilot_migration_receipt(writable_local, reader, self.remote, 'token',
                Path('/unused'), anchor, self.parent, after_sequence=7)
        fetch.assert_called_once_with(writable_local, migration1, runtime.entry.MAX_INTERVENING + 2)
        load.assert_called_once_with(reader, self.remote, 'token', Path('/unused'), migration1)

        with patch.object(runtime.entry, 'PILOT_MIGRATION', pin), \
             patch.object(runtime, 'fetch_commit') as fetch, \
             patch.object(runtime, 'load_receipt') as load:
            reader.source_get_tree = lambda commit: {
                anchor['commit']: before_tree, self.parent: final_tree,
                migration1: after_tree, migration2: final_tree}[commit]
            runtime.hydrate_pilot_migration_receipt(writable_local, reader, self.remote, 'token',
                Path('/unused'), anchor, self.parent, after_sequence=8)
        fetch.assert_called_once_with(writable_local, migration2, runtime.entry.MAX_INTERVENING + 2)
        load.assert_called_once_with(reader, self.remote, 'token', Path('/unused'), migration2)

    def test_migration_receipt_hydration_skips_unchanged_get_tree(self):
        anchor = {'commit': 'e' * 40}
        reader = types.SimpleNamespace(source_get_tree=lambda commit: '1' * 40)
        with patch.object(runtime.entry, 'PILOT_MIGRATION', ()), \
             patch.object(runtime, 'fetch_commit') as fetch, \
             patch.object(runtime, 'load_receipt') as load:
            runtime.hydrate_pilot_migration_receipt(object(), reader, self.remote, 'token',
                Path('/unused'), anchor, self.parent, after_sequence=7)
        fetch.assert_not_called()
        load.assert_not_called()

    def test_verified_pilot_migration_becomes_effective_anchor(self):
        anchor = {'commit': 'e' * 40, 'receipt_sha256': 'a' * 64}
        migration = 'b' * 40
        before_tree, after_tree = 'c' * 40, 'd' * 40
        prior = {'sequence': 7}
        migrated = {'sequence': 8, 'parent_commit': 'f' * 40,
                    'previous_receipt_commit': anchor['commit'],
                    'previous_receipt_sha256': anchor['receipt_sha256'],
                    'changed_content_ids': [],
                    'counts': {'new_created': 0, 'new_preview_imported': 0,
                               'new_updated': 0, 'legacy_source_updated': 0}}
        raw_receipt = b'verified migration receipt'
        before = 'f' * 40
        reader = types.SimpleNamespace(
            source_get_tree=lambda commit: {
                anchor['commit']: before_tree, before: before_tree,
                migration: after_tree, self.parent: after_tree}[commit],
            read=lambda *args: raw_receipt)
        pin = (before, migration, before_tree, after_tree, runtime.a.sha(raw_receipt), 8)
        policy = {'get_policy_sha256': '8' * 64}
        with patch.object(runtime.entry, 'PILOT_MIGRATION', pin), \
             patch.object(runtime.entry, 'single_parent', return_value='f' * 40), \
             patch.object(runtime.entry, 'read_receipt', return_value=migrated):
            effective, witness = runtime.effective_anchor_after_pilot_migration(
                reader, anchor, prior, migrated, self.parent, policy, {'old': 'witness'})
        self.assertEqual(effective, {
            'commit': migration, 'receipt_sha256': runtime.a.sha(b'verified migration receipt')})
        self.assertIsNone(witness)

        changed = {**migrated, 'changed_content_ids': ['123']}
        with patch.object(runtime.entry, 'PILOT_MIGRATION', pin), \
             patch.object(runtime.entry, 'single_parent', return_value='f' * 40), \
             patch.object(runtime.entry, 'read_receipt', return_value=changed):
            with self.assertRaisesRegex(runtime.a.ParityFailure, 'runtime_receipt'):
                runtime.effective_anchor_after_pilot_migration(
                    reader, anchor, prior, changed, self.parent, policy, {'old': 'witness'})

    def test_consecutive_verified_migrations_advance_to_last_receipt_only(self):
        anchor = {'commit': 'a' * 40, 'receipt_sha256': 'f' * 64}
        before1, migration1, before2, migration2 = ('b' * 40, 'c' * 40, 'd' * 40, 'e' * 40)
        parent = '0' * 40
        tree1, tree2, tree3 = '1' * 40, '2' * 40, '3' * 40
        raw1, raw2 = b'receipt eight', b'receipt nine'
        receipt8 = {'sequence': 8, 'parent_commit': before1,
                    'previous_receipt_commit': anchor['commit'],
                    'previous_receipt_sha256': anchor['receipt_sha256'],
                    'changed_content_ids': [],
                    'counts': dict.fromkeys(('new_created', 'new_preview_imported',
                                             'new_updated', 'legacy_source_updated'), 0)}
        receipt9 = {'sequence': 9, 'parent_commit': before2,
                    'previous_receipt_commit': migration1,
                    'previous_receipt_sha256': runtime.a.sha(raw1),
                    'changed_content_ids': [],
                    'counts': dict.fromkeys(('new_created', 'new_preview_imported',
                                             'new_updated', 'legacy_source_updated'), 0)}
        reader = types.SimpleNamespace(
            source_get_tree=lambda commit: {
                anchor['commit']: tree1, before1: tree1, migration1: tree2,
                before2: tree2, migration2: tree3, parent: tree3}[commit],
            read=lambda commit, *_: {migration1: raw1, migration2: raw2}[commit])
        pins = (
            (before1, migration1, tree1, tree2, runtime.a.sha(raw1), 8),
            (before2, migration2, tree2, tree3, runtime.a.sha(raw2), 9),
        )
        with patch.object(runtime.entry, 'PILOT_MIGRATION', pins), \
             patch.object(runtime.entry, 'single_parent', side_effect=lambda _, commit:
                          {migration1: before1, migration2: before2}[commit]), \
             patch.object(runtime.entry, 'read_receipt', side_effect=[receipt8, receipt9]) as read:
            effective, witness = runtime.effective_anchor_after_pilot_migration(
                reader, anchor, {'sequence': 7}, receipt9, parent,
                {'get_policy_sha256': '8' * 64}, {'old': 'witness'})
        self.assertEqual(effective, {'commit': migration2, 'receipt_sha256': runtime.a.sha(raw2)})
        self.assertIsNone(witness)
        self.assertEqual(read.call_count, 2)

    def test_selected_baseline_readback_failure_still_stops(self):
        with patch.object(self.remote, 'request', return_value={'sha': 'wrong'}):
            with self.assertRaises(runtime.a.ParityFailure):
                runtime.hydrate_baselines(self.reader, self.legacy, self.registry,
                    self.parent, self.remote, source_id='333')
        self.assertEqual(len(self.baseline_calls), 1)
        self.assertEqual(self.imports, [])

    def test_loader_keeps_full_inventory_and_trust_inputs_but_filters_downloads(self):
        for ident in ('111', '333', '999', None):
            with self.subTest(source_id=ident):
                self.baseline_calls.clear(); self.downloads.clear(); self.imports.clear()
                current = {runtime.n.STATE: b'full registry', 'unrelated.md': b'current full tree'}
                before = deepcopy((self.legacy, self.registry))
                bootstrap = {'commit': 'b'*40}
                root = Path('/not-used-by-stubs')
                with patch.object(runtime, 'fetch_commit') as fetch, \
                     patch.object(runtime, 'load_inventory', return_value=current) as inventory, \
                     patch.object(runtime.entry, 'trust_current', return_value=(
                         'policy', None, (current, self.legacy, self.registry))) as trust:
                    result = runtime.load_trusted_inputs(self.reader.local, self.reader,
                        'evidence', self.remote, 'token', root, self.parent, 'client',
                        {'rules_sha256': 'c'*64}, bootstrap=bootstrap,
                        exclude_run_id=123, exclude_run_attempt=1, source_id=ident)
                self.assertEqual(result, (None, None, None))
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(inventory.call_count, 2)
                self.assertEqual(inventory.call_args_list[0].kwargs, {})
                self.assertEqual(inventory.call_args_list[1].kwargs, {'source_id': ident})
                trust.assert_called_once_with(self.reader, 'evidence', self.parent,
                    'c'*64, None, bootstrap=bootstrap, history_witness=None, source_id=ident)
                self.assertEqual((self.legacy, self.registry), before)
                self.assert_downloads(['111', '222', '333', '444'] if ident is None
                    else ([] if ident == '999' else [ident]))

    def test_loader_uses_verified_prior_receipt_when_loading_history(self):
        anchor = {'commit': 'b' * 40, 'receipt_sha256': 'c' * 64}
        bootstrap = {'commit': 'd' * 40}
        prior = {'sequence': 7, 'parent_commit': 'e' * 40,
                 'previous_receipt_commit': 'f' * 40}
        trusted = {'sequence': 8}
        current = {runtime.n.STATE: b'full registry'}
        policy = {'rules_sha256': 'a' * 64, 'get_policy_sha256': 'b' * 64}
        self.reader.tree = lambda commit: {runtime.entry.FULL_RECEIPT: 'present'}
        with patch.object(runtime, 'fetch_commit') as fetch, \
             patch.object(runtime, 'load_inventory', return_value=current), \
             patch.object(runtime, 'load_receipt') as load_receipt, \
             patch.object(runtime.provenance, 'discover', return_value={
                 'anchor': anchor, 'receipt_sequence': 7}), \
             patch.object(runtime.provenance, 'history_witness_required', return_value=False), \
             patch.object(runtime.entry, 'read_receipt', return_value=prior), \
             patch.object(runtime, 'hydrate_pilot_migration_receipt'), \
             patch.object(runtime.entry, 'trust_current', return_value=(
                 'policy', trusted, (current, {}, {}))), \
             patch.object(runtime, 'effective_anchor_after_pilot_migration',
                          return_value=(anchor, None)) as promote, \
             patch.object(runtime, 'hydrate_baselines'):
            result = runtime.load_trusted_inputs(self.reader.local, self.reader,
                'evidence', self.remote, 'token', Path('/not-used'), self.parent,
                'client', policy, bootstrap=bootstrap,
                exclude_run_id=123, exclude_run_attempt=1, source_id='333')

        self.assertEqual(result, (anchor, None, None))
        fetch.assert_any_call(self.reader.local, 'f' * 40)
        self.assertEqual(load_receipt.call_count, 2)
        load_receipt.assert_any_call(self.reader, self.remote, 'token',
                                     Path('/not-used'), 'f' * 40)
        promote.assert_called_once_with(self.reader, anchor, prior, trusted,
                                        self.parent, policy, None)


if __name__ == '__main__': unittest.main()
