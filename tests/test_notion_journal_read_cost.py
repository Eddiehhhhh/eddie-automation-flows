"""Bounded reads and real blob:none CAS/race/corruption; no live services."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_legacy_manual as manual
import get_notion_executor as engine
import get_notion_journal_runtime as runtime
import get_notion_remote_journal as journal
import get_notion_outbox as outbox
from git_object_fixture import remove_objects
from test_notion_journal_runtime import Fixture, state
import test_notion_remote_journal as git_tests
from test_notion_remote_journal import run
import test_notion_executor as engine_fixture


class CachedFixture(Fixture):
    def store(self, anchor=None, **kwargs):
        remote = journal.ImmutableJournalRemote(self)
        loader = runtime.ExactJournalLoader(self.root, remote)
        return journal.RemoteJournal(self.root, remote, anchor=anchor or {
            'commit': self.initial, 'state_blob': None,
            'state_sha256': outbox.digest(outbox.initial_state())},
            load_commit=loader, guard=self.guard, confirm=self.confirm,
            sleep=lambda seconds: None, **kwargs)


class CachedJournalTests(git_tests.JournalTests):
    """Retain the whole original real-Git journal safety suite with caching on."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.f = CachedFixture(self.temp.name); self.store = self.f.store()


class CountingFixture(CachedFixture):
    """Count each production-shaped private REST GET, including source guards."""
    def __init__(self, base):
        self.gets = Counter(); self.events = []
        super().__init__(base)
        self.initial = self.other_commit('Raw/03 Get/source.md', b'Synthetic source\n')
        self.view = journal.ImmutableJournalRemote(self)
        self.source = self.view.source_get_tree(self.initial)
        self.gets.clear(); self.events.clear()

    def metadata(self):
        self.gets['metadata'] += 1; self.events.append(('metadata',))
        return super().metadata()

    def ref(self, name):
        self.gets['ref'] += 1; self.events.append(('ref', name))
        if name in manual.PROTECTED: return manual.PROTECTED[name]
        return super().ref(name)

    def commit(self, oid):
        self.gets['commit'] += 1; self.events.append(('commit', oid))
        return super().commit(oid)

    def tree_entries(self, oid, recursive=False):
        assert recursive is False
        self.gets['tree'] += 1; self.events.append(('tree', oid))
        return super().tree_entries(oid)

    def blob(self, oid, size):
        self.gets['blob'] += 1; self.events.append(('blob', oid))
        return super().blob(oid, size)

    def guard(self, head):
        current = manual.guard(self.view)
        if current != head or self.view.source_get_tree(current) != self.source:
            return False
        self.events.append(('full_guard', head))
        return super().guard(head)

    def cas_main(self, old, new):
        self.events.append(('cas', old, new))
        return super().cas_main(old, new)

    def confirm(self, receipt):
        self.events.append(('confirm', receipt['commit']))
        return super().confirm(receipt)

    def store(self, anchor=None, **kwargs):
        self.view = journal.ImmutableJournalRemote(self)
        loader = runtime.ExactJournalLoader(self.root, self.view)
        return journal.RemoteJournal(self.root, self.view, anchor=anchor or {
            'commit': self.initial, 'state_blob': None,
            'state_sha256': outbox.digest(outbox.initial_state())},
            load_commit=loader, guard=self.guard, confirm=self.confirm,
            sleep=lambda seconds: None, **kwargs)


def synthetic_engine_state(number):
    value = engine.initial_state()
    plan = {'records': [], 'skipped_conflicts': [],
            'publication': {'initial_cohort': {'reviewed': True}},
            'synthetic_revision': number}
    value['journal'] = {'plan': plan, 'plan_sha256': outbox.digest(plan),
                        'index': 0, 'operation': {'phase': 'prepared'}}
    return value


class ReadCostTests(unittest.TestCase):
    def test_steady_cas_bounded_gets_and_single_new_object_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CountingFixture(directory); store = f.store()
            adapter = outbox.EngineStore(store); _, version = adapter.read()
            measurements = []
            for number in range(1, 5):
                f.gets.clear(); f.events.clear()
                value = synthetic_engine_state(number)
                version = adapter.compare_and_set(version, value)
                measurements.append(dict(f.gets))
                self.assertEqual(dict(f.gets), {'metadata': 12, 'ref': 25, 'commit': 1, 'tree': 4, 'blob': 1})
                self.assertLessEqual(sum(f.gets.values()), 43, f.gets)
                self.assertEqual(f.gets['commit'], 1)
                self.assertEqual(f.gets['blob'], 1)
                self.assertEqual(f.gets['tree'], 4)
                self.assertEqual(sum(x[0] == 'full_guard' for x in f.events), 4)
                cas = next(i for i, row in enumerate(f.events) if row[0] == 'cas')
                self.assertEqual(f.events[cas - 1], ('full_guard', f.calls[-1][0]))
                confirm = next(i for i, row in enumerate(f.events) if row[0] == 'confirm')
                self.assertEqual(f.events[confirm - 1], ('full_guard', f.calls[-1][1]))
                self.assertEqual(f.events[-1], ('full_guard', f.calls[-1][1]))
                self.assertEqual(adapter.outer['engine_state'], value)
                self.assertEqual(len(f.calls), number)
                self.assertEqual(len(f.receipts), number)
            self.assertEqual(measurements[1:], [measurements[1]] * 3)

    def test_cached_immutable_responses_are_copied_and_mutable_reads_are_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CountingFixture(directory); view = f.view; head = f.head()
            value = view.commit(head); value['parents'].append('f' * 40)
            self.assertNotEqual(value, view.commit(head))
            tree = view.commit(head)['tree']; rows = view.tree_entries(tree)
            rows.clear(); self.assertTrue(view.tree_entries(tree))
            f.gets.clear()
            for _ in range(2):
                view.metadata(); view.ref(journal.REF)
            self.assertEqual(f.gets, {'metadata': 2, 'ref': 2})
            f.metadata = lambda: {'full_name': journal.REPOSITORY, 'private': False}
            self.assertFalse(view.metadata()['private'])

    def test_candidate_readback_is_fresh_even_for_reused_state_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CountingFixture(directory); store = f.store()
            old = store.compare_and_set(outbox.digest(outbox.initial_state()), state())
            middle = store.compare_and_set(old, state(2))
            f.gets.clear(); f.events.clear()
            store.compare_and_set(middle, state())
            blob = journal.retention.oid(outbox.encoded(state()))
            self.assertEqual([r for r in f.events if r == ('blob', blob)], [('blob', blob)])
            self.assertEqual(f.gets['commit'], 1)
            self.assertEqual(len(f.calls), 3)

    def test_cache_eviction_is_bounded_and_never_fakes_a_read(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CountingFixture(directory)
            view = journal.ImmutableJournalRemote(f, max_cache_bytes=1)
            view.commit(f.initial); view.commit(f.initial)
            self.assertEqual(f.gets['commit'], 2)
            self.assertEqual(view.cache_bytes, 0)
            self.assertEqual(view.cache, {})

    def test_local_missing_and_corrupt_cached_commit_tree_and_blob_stop(self):
        for kind in ('commit', 'tree', 'blob', 'nested_tree'):
            for missing in (True, False):
                with self.subTest(kind=kind, missing=missing), tempfile.TemporaryDirectory() as directory:
                    f = CachedFixture(directory); store = f.store()
                    store.compare_and_set(outbox.digest(outbox.initial_state()), state())
                    receipt = f.receipts[-1]
                    if kind == 'nested_tree':
                        oid = store.local.run('rev-parse', receipt['commit'] + ':.sync').decode().strip()
                        object_type = 'tree'
                    else:
                        oid = receipt['state_blob' if kind == 'blob' else kind]
                        object_type = kind
                    raw = store.local.run('cat-file', object_type, oid)
                    path, _ = remove_objects(f.root, oid)[oid]
                    if not missing:
                        # Git cat-file accepts an object saved at a wrong OID;
                        # the loader must independently detect the mismatch.
                        changed = raw + b'X' if object_type != 'tree' else raw.replace(b'.sync', b'.synx').replace(b'get-notion', b'get-notiox')
                        path.write_bytes(zlib.compress(object_type.encode() + b' ' + str(len(changed)).encode() + b'\0' + changed))
                    before = len(f.fetch_args); calls = len(f.calls)
                    with self.assertRaises(journal.JournalFailure): store.read()
                    self.assertEqual(len(f.fetch_args), before)
                    self.assertEqual(len(f.calls), calls)

    def test_race_during_prepare_does_not_write_or_overwrite_unrelated_change(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CachedFixture(directory); store = f.store()
            build = store._build
            def race(*args):
                result = build(*args); f.other_commit(); return result
            store._build = race
            with self.assertRaises(journal.JournalFailure):
                store.compare_and_set(outbox.digest(outbox.initial_state()), state())
            self.assertEqual(f.calls, []); self.assertFalse(store.poisoned)
            store._build = build
            # Safe retry only after a proven pre-CAS failure, on latest parent.
            store.compare_and_set(outbox.digest(outbox.initial_state()), state())
            self.assertEqual(len(f.calls), 1)
            self.assertEqual(run(f.bare, 'show', 'main:unrelated/new.md'), b'Concurrent change\n')

    def test_source_change_during_confirmation_blocks_verified_return(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CountingFixture(directory); store = f.store(); normal = store.confirm
            def race(receipt):
                normal(receipt); f.other_commit('Raw/03 Get/source.md', b'Changed source\n')
                return True
            store.confirm = race
            with self.assertRaises(journal.JournalFailure):
                store.compare_and_set_verified(outbox.digest(outbox.initial_state()), state())
            self.assertTrue(store.poisoned); self.assertEqual(len(f.calls), 1)

    def test_cached_objects_never_hide_changed_protected_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CountingFixture(directory); store = f.store()
            digest = store.compare_and_set(outbox.digest(outbox.initial_state()), state())
            normal = f.ref
            protected = next(iter(manual.PROTECTED))
            f.ref = lambda name: 'f' * 40 if name == protected else normal(name)
            with self.assertRaises(journal.JournalFailure):
                store.compare_and_set(digest, state(2))
            self.assertEqual(len(f.calls), 1)
            self.assertEqual(len(f.receipts), 1)

    def test_generic_store_still_requires_post_cas_readback(self):
        class Store:
            def __init__(self): self.reads = 0
            def compare_and_set(self, expected, value): return outbox.digest(value)
            def read(self): self.reads += 1; return outbox.initial_state(), '0' * 64
        store = Store()
        with self.assertRaises(outbox.Blocked):
            outbox.confirmed_compare_and_set(store, 'f' * 64, state())
        self.assertEqual(store.reads, 1)

    def test_verified_store_result_still_must_match_exact_candidate(self):
        class Store:
            def compare_and_set_verified(self, expected, value):
                return outbox.initial_state(), outbox.digest(value)
        with self.assertRaises(outbox.Blocked):
            outbox.confirmed_compare_and_set(Store(), 'f' * 64, state())

    def test_writer_change_during_checkpoint_blocks_notion_write(self):
        fixture = engine_fixture.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.publish(); plan = fixture.plan(); original = fixture.store.compare_and_set
        active = [False]
        def checkpoint(expected, value):
            result = original(expected, value)
            operation = (value['journal'] or {}).get('operation')
            if operation and operation['phase'] == 'in_flight': active[0] = True
            return result
        fixture.store.compare_and_set = checkpoint
        def writers():
            evidence = deepcopy(fixture.policy['old_writer_evidence'])
            if active[0]: evidence['writers']['getnote-to-notion']['active_runs'] = 1
            return evidence
        result = engine.execute(plan, engine.digest(plan), fixture.publication,
            fixture.api, fixture.store, expected_store_sha=fixture.store.read()[1], writers_guard=writers)
        self.assertEqual(result['reason'], 'writer_gate_unknown')
        self.assertEqual(result['write_calls'], 0); self.assertEqual(fixture.writes(), [])


if __name__ == '__main__': unittest.main()
