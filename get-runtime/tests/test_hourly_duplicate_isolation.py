"""Exercise the real hourly dispatcher and Git candidate, without a network."""
from copy import deepcopy
import hashlib
import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
bindings = types.ModuleType('get_private_runtime_bindings')
bindings.value = lambda *args: '0' * 64
with patch.dict(sys.modules, {'get_private_runtime_bindings': bindings}):
    c = importlib.import_module('get_canonical_writer')
    runtime = importlib.import_module('get_recurring_runtime')


def note(ident, content='original', version=1760000000000):
    return {'note_id': ident, 'title': 'note-' + ident, 'note_type': 'plain_text',
            'content': content, 'created_at': 1750000000000, 'updated_at': version}


class HourlyDuplicateIsolation(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='get-hourly-isolation-test-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.git('init', '-q')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.evidence = types.SimpleNamespace(anchor={'capture_commit': 'a'*40},
            census={'eligible': {}, 'pending_ids': [], 'summaries': {}},
            initial={}, conflicts={}, source={}, old={})
        self.current = {}
        self.notes = {}
        self.owner_rows = {}

    def git(self, *args, data=None):
        return subprocess.run(['git', '-C', str(self.root), *args], input=data,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def duplicate(self, ident, managed=False, legacy=False):
        source = note(ident)
        path = c.new_path(source, ident)
        data = c.render_exact(source, path)
        self.current[path] = data
        self.current[path[:-3] + '-shadow.md'] = data + b'\nshadow retained\n'
        self.notes[ident] = source
        if managed:
            self.owner_rows[ident] = c.record(source, source, path, data)
        if legacy:
            ownership = {key: None for key in c.legacy.IMMUTABLE}
            ownership.update(identity=ident, filePath=path, owned_spans=[],
                ownership_shape_sha256=c.legacy.owned.ownership_shape_hash([]),
                mirror_sha256=c.legacy.sha(data), source_version=c.raw.raw_source_version(source['updated_at']))
            self.evidence.initial[ident] = ownership
            self.evidence.old[ident] = data
            self.evidence.source[ident] = source
            self.evidence.census['eligible'][ident] = {'filePath': path}
            self.evidence.census['summaries'][ident] = source
        return path

    def save_parent(self):
        state = c.legacy.initial_state(self.evidence, self.current)
        legacy_sha = c.legacy.sha(c.legacy.encoded(state))
        # Reproduce the already-deployed single-note registry shape. The hourly
        # run must accept this existing state without reclassifying reserved IDs.
        registry = c.initial_registry(self.evidence, state, legacy_sha,
            self.current, only_id='9999999999')
        registry['newById'] = deepcopy(self.owner_rows)
        for ident in self.owner_rows:
            registry['reserved'].pop(ident, None)
        self.current[c.legacy.STATE] = c.legacy.encoded(state)
        self.current[c.STATE] = c.legacy.encoded(registry)
        for path, data in self.current.items():
            dest = self.root / c.legacy.retention.GET / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        self.git('add', '.')
        # Materialize case-distinct paths through Git itself, so this test also
        # covers the production Linux tree on a case-insensitive Mac volume.
        for path, data in self.current.items():
            blob = self.git('hash-object', '-w', '--stdin', data=data).decode().strip()
            self.git('update-index', '--add', '--cacheinfo', '100644,' + blob + ',' +
                     c.legacy.retention.GET + '/' + path)
        self.git('commit', '-qm', 'synthetic source')
        parent = self.git('rev-parse', 'HEAD').decode().strip()
        return parent, legacy_sha, c.legacy.sha(self.current[c.STATE])

    def run_prepare(self, parent, legacy_sha, registry_sha, new_id='55555'):
        sources = deepcopy(self.notes)
        sources[new_id] = note(new_id, 'fresh new content')
        return c.prepare(c.legacy.OfflineGit(self.root), parent, self.evidence,
            legacy_sha, registry_sha, list(sources.values()), True,
            {ident: value for ident, value in sources.items() if ident != '22222'},
            selected_new=[new_id])

    def test_real_prepare_keeps_duplicates_and_creates_unique_note_then_updates(self):
        managed_path = self.duplicate('11111', managed=True)
        self.duplicate('22222')
        legacy_path = self.duplicate('33333', legacy=True)
        parent, lsha, rsha = self.save_parent()
        before = deepcopy(self.current)
        # Both changed duplicate IDs must be held before either renderer writes.
        self.notes['11111'] = note('11111', 'changed managed', 1760000001000)
        self.notes['33333'] = note('33333', 'changed legacy', 1760000001000)
        plan, states, counts = self.run_prepare(parent, lsha, rsha)
        self.assertEqual(states['registry']['conflictsById']['11111'], 'identity_reserved')
        self.assertEqual(states['legacy']['conflictsById']['33333'], 'legacy_structure_ambiguous')
        self.assertEqual(states['registry']['newById']['11111'], self.owner_rows['11111'])
        self.assertEqual(states['legacy']['legacyById']['33333']['ownership']['filePath'], legacy_path)
        self.assertEqual(counts['new_created'], 1)
        candidate = plan['candidate_commit']
        reader = c.legacy.OfflineGit(self.root)
        actual = c.git_inventory(reader, candidate)
        for path, data in before.items():
            if path not in (c.STATE, c.legacy.STATE): self.assertEqual(actual[path], data)
        self.assertEqual(self.git('rev-parse', 'HEAD').decode().strip(), parent)
        self.assertNotIn(managed_path, {row['path'] for row in plan['changes']})
        # Resume through real registry validation against the generated commit.
        sources = deepcopy(self.notes)
        sources['55555'] = note('55555', 'edited fresh content', 1760000002000)
        second, second_states, second_counts = c.prepare(reader, candidate, self.evidence,
            plan['legacy_sha256'], plan['registry_sha256'], list(sources.values()),
            True, {ident: value for ident, value in sources.items() if ident != '22222'})
        self.assertEqual(second_counts['new_updated'], 1)
        path = second_states['registry']['newById']['55555']['filePath']
        self.assertIn(b'edited fresh content', c.git_inventory(reader, second['candidate_commit'])[path])
        # Replay identical source creates no duplicate or content changes.
        third, _, third_counts = c.prepare(reader, second['candidate_commit'], self.evidence,
            second['legacy_sha256'], second['registry_sha256'], list(sources.values()), True,
            {ident: value for ident, value in sources.items() if ident != '22222'})
        self.assertEqual(third_counts['new_unchanged'], 1)
        self.assertEqual(third['changes'], [])

    def test_registered_duplicate_owner_edited_fails_before_any_candidate(self):
        path = self.duplicate('11111', managed=True)
        parent, lsha, rsha = self.save_parent()
        target = self.root / c.legacy.retention.GET / path
        target.write_bytes(target.read_bytes() + b'edited outside ownership\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'unowned edit')
        parent = self.git('rev-parse', 'HEAD').decode().strip()
        with self.assertRaisesRegex(c.raw.ParityFailure, 'canonical_duplicate_path'):
            self.run_prepare(parent, lsha, rsha)

    def test_unregistered_identity_tree_change_remains_rejected(self):
        self.duplicate('11111', managed=True)
        parent, lsha, rsha = self.save_parent()
        new = self.root / c.legacy.retention.GET / '03 纯文本记录/foreign.md'
        new.write_bytes(b'---\nnote_id: 77777\n---\nforeign\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'unregistered input')
        parent = self.git('rev-parse', 'HEAD').decode().strip()
        with self.assertRaisesRegex(c.raw.ParityFailure, 'canonical_reserved_changed'):
            self.run_prepare(parent, lsha, rsha)

    def test_pinned_case_pair_keeps_existing_reserved_class_and_exact_hash_guard(self):
        ident = 'f5536f23631233577ac2a37f15fa0e09'
        owner, shadow, _, _ = c.SCOPED_CASE_DUPLICATES[ident]
        data = ('---\nnote_id: ' + ident + '\n---\ncase pair\n').encode()
        pin = (owner, shadow, c.legacy.retention.oid(data), hashlib.sha256(data).hexdigest())
        self.current.update({owner: data, shadow: data})
        with patch.dict(c.SCOPED_CASE_DUPLICATES, {ident: pin}):
            parent, lsha, rsha = self.save_parent()
            plan, states, _ = self.run_prepare(parent, lsha, rsha)
            self.assertEqual(states['registry']['reserved'][ident], 'existing_unmanaged')
            actual = c.git_inventory(c.legacy.OfflineGit(self.root), plan['candidate_commit'])
            self.assertEqual(actual[owner], data)
            self.assertEqual(actual[shadow], data)
            changed = data + b'changed\n'
            blob = self.git('hash-object', '-w', '--stdin', data=changed).decode().strip()
            self.git('update-index', '--cacheinfo', '100644,' + blob + ',' +
                     c.legacy.retention.GET + '/' + shadow)
            self.git('commit', '-qm', 'case shadow changed')
            bad_parent = self.git('rev-parse', 'HEAD').decode().strip()
            with self.assertRaisesRegex(c.raw.ParityFailure, 'canonical_reserved_changed'):
                self.run_prepare(bad_parent, lsha, rsha)

    def test_unique_invalid_source_is_held_and_never_rendered(self):
        self.duplicate('11111', managed=True)
        parent, lsha, rsha = self.save_parent()
        invalid = note('55555', 'cannot reinterpret structured content')
        invalid['note_url'] = 'https://foreign.invalid/not-this-note'
        plan, states, counts = c.prepare(c.legacy.OfflineGit(self.root), parent,
            self.evidence, lsha, rsha, list(self.notes.values()) + [invalid], True,
            {'55555': invalid}, selected_new=['55555'])
        self.assertEqual(counts['new_created'], 0)
        self.assertEqual(states['registry']['conflictsById']['55555'], 'source_unknown_fields')
        self.assertNotIn('55555', states['registry']['newById'])
        self.assertTrue(all(row['path'] in (c.STATE, c.legacy.STATE) for row in plan['changes']))

    def test_hourly_inventory_and_engine_dispatcher_with_real_selection_and_readback(self):
        self.duplicate('11111', managed=True)
        self.duplicate('22222')
        self.duplicate('33333', legacy=True)
        parent, lsha, rsha = self.save_parent()
        reader = c.legacy.OfflineGit(self.root)
        # Only the network boundary is replaced. All blobs already exist in
        # this real repository; a network fetch is an unexpected test failure.
        class Remote:
            def check_source_budget(self, commit):
                return {path: (*row, len(reader.local.blob(row[2])))
                        for path, row in reader.tree(commit).items()}
        current = runtime.load_inventory(reader, Remote(), None,
            self.root / 'inventory-cache', parent)
        self.assertEqual(current, self.current)
        sources = deepcopy(self.notes)
        sources['11111'] = note('11111', 'changed managed', 1760000001000)
        sources['33333'] = note('33333', 'changed legacy', 1760000001000)
        sources['55555'] = note('55555', 'new via hourly dispatcher')
        calls = []
        class Source:
            def enumerate(self): return list(sources.values()), True
            def detail_raw(self, ident):
                calls.append(ident)
                return c.legacy.encoded({'data': sources[ident]})
        engine = runtime.engine
        plan, files = engine.prepare(reader, self.evidence, parent, current,
            Source(), legacy_sha=lsha, registry_sha=rsha)
        self.assertEqual(plan['counts']['new_created'], 1)
        self.assertEqual(calls, ['11111', '33333', '55555'])
        engine.verify_plan(reader, plan, files)
        before_states = runtime.entry.load_states(reader, self.evidence, parent, lsha, rsha)
        after_states = runtime.entry.load_states(reader, self.evidence,
            plan['candidate_commit'], plan['state_sha256'], plan['registry_sha256'])
        runtime.entry.check_counts(plan['counts'], after_states)
        self.assertEqual(runtime.entry.transition(reader, self.evidence, parent,
            plan['candidate_commit'], before_states, after_states), ['55555'])
        for path, data in self.current.items():
            if path not in (c.STATE, c.legacy.STATE):
                self.assertEqual(after_states[0][path], data)


if __name__ == '__main__': unittest.main()
