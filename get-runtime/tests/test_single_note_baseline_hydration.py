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


if __name__ == '__main__': unittest.main()
