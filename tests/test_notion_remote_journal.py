"""Real local bare Git, filtered clones, synthetic journal states; no network."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_remote_journal as j
import get_notion_outbox as e


def run(root, *args, data=None):
    env = j.retention.safe_environment()
    env.update(GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
               GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid',
               GIT_NO_LAZY_FETCH='1')
    return subprocess.run(['git', '-c', 'protocol.file.allow=always', '-C', str(root), *args],
        input=data, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def state(number=1):
    value = e.initial_state()
    plan = {'records': [], 'synthetic_revision': number}
    value['engine_state']['journal'] = {'plan': plan, 'plan_sha256': e.digest(plan), 'index': 0,
                        'operation': {'phase': 'prepared'}}
    return value


class Fixture:
    def __init__(self, base, seed=None):
        self.base = Path(base)
        if seed is None:
            seed = self.base / 'seed'; seed.mkdir()
            run(seed, 'init', '-q', '-b', 'main')
            (seed / 'keep.txt').write_bytes(b'Synthetic independent content\n')
            (seed / 'asset.bin').write_bytes(bytes(range(256)) * 2048)
            run(seed, 'add', '.'); run(seed, 'commit', '-qm', 'Initial fixture')
        self.bare = self.base / 'remote.git'
        run(self.base, 'clone', '-q', '--bare', str(seed), str(self.bare))
        run(self.bare, 'config', 'uploadpack.allowFilter', 'true')
        self.initial = self.head()
        run(self.bare, 'update-ref', 'refs/tags/protected', self.initial)
        self.root = self.clone('clone')
        self.calls = []; self.guards = []; self.receipts = []
        self.mode = None; self.stale = 0; self.previous = None
        self.bad_commit = None; self.bad_blob = False
        self.confirm_value = True; self.guard_value = True

    def clone(self, name):
        root = self.base / name
        run(self.base, 'clone', '-q', '--no-checkout', '--filter=blob:none',
            self.bare.as_uri(), str(root))
        run(root, 'read-tree', 'HEAD')
        return root

    def head(self):
        return run(self.bare, 'rev-parse', j.REF).decode().strip()

    def metadata(self):
        return {'full_name': j.REPOSITORY, 'private': True}

    def ref(self, name):
        assert name == j.REF
        if self.stale:
            self.stale -= 1; return self.previous
        if self.mode == 'foreign_ref' and self.calls: return 'f' * 40
        return self.head()

    def commit(self, head):
        out = {'tree': run(self.bare, 'rev-parse', head + '^{tree}').decode().strip(),
               'parents': run(self.bare, 'rev-list', '--parents', '-n', '1', head).decode().split()[1:]}
        if self.calls and head == self.calls[-1][1]:
            if self.bad_commit == 'tree': out['tree'] = 'f' * 40
            if self.bad_commit == 'parent': out['parents'] = []
        return out

    def blob(self, blob, size):
        out = run(self.bare, 'cat-file', 'blob', blob)
        assert len(out) == size
        if self.bad_blob and self.calls: return out + b'X'
        return out

    def cas_main(self, old, new):
        self.calls.append((old, new)); self.previous = old
        if self.mode == 'reject': raise TimeoutError('DO NOT EXPOSE PRIVATE ERROR')
        run(self.bare, 'fetch', '-q', '--no-tags', '--no-write-fetch-head', str(self.root), new)
        run(self.bare, 'update-ref', j.REF, new, old)
        if self.mode == 'stale': self.stale = 2
        if self.mode == 'stale_forever': self.stale = 50
        if self.mode == 'accepted_throw':
            self.stale = 2; raise TimeoutError('DO NOT EXPOSE PRIVATE ERROR')

    def load(self, head):
        run(self.root, 'fetch', '-q', '--filter=blob:none', '--no-tags',
            '--no-write-fetch-head', 'origin', head)
        row = run(self.root, 'ls-tree', head, '--', j.STATE_PATH).decode().strip()
        if row:
            blob = row.split()[2]
            size = int(run(self.bare, 'cat-file', '-s', blob))
            if size > j.MAX_STATE: raise ValueError('bounded loader')
            data = run(self.bare, 'cat-file', 'blob', blob)
            assert run(self.root, 'hash-object', '-w', '--stdin', data=data).decode().strip() == blob

    def guard(self, head):
        self.guards.append(head)
        return self.guard_value

    def confirm(self, receipt):
        assert receipt['commit'] == self.head()
        raw = run(self.bare, 'show', receipt['commit'] + ':' + j.STATE_PATH)
        assert e.digest(raw) == receipt['state_sha256']
        self.receipts.append(deepcopy(receipt))
        if isinstance(self.confirm_value, Exception): raise self.confirm_value
        return self.confirm_value

    def store(self, anchor=None, **kwargs):
        return j.RemoteJournal(self.root, self, anchor=anchor or {
            'commit': self.initial, 'state_blob': None, 'state_sha256': e.digest(e.initial_state())},
            load_commit=self.load, guard=self.guard, confirm=self.confirm,
            sleep=lambda seconds: None, **kwargs)

    def other_commit(self, path='unrelated/new.md', data=b'Concurrent change\n'):
        parent = self.head()
        with tempfile.TemporaryDirectory() as folder:
            env = j.retention.safe_environment()
            env.update(GIT_INDEX_FILE=str(Path(folder) / 'index'),
                GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
            def cmd(*args, data=None):
                return subprocess.run(['git', '-C', str(self.bare), *args], input=data,
                    env=env, check=True, capture_output=True).stdout
            cmd('read-tree', parent)
            blob = cmd('hash-object', '-w', '--stdin', data=data).decode().strip()
            cmd('update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + path)
            tree = cmd('write-tree').decode().strip()
            new = cmd('commit-tree', tree, '-p', parent, '-m', 'Concurrent fixture').decode().strip()
            cmd('update-ref', j.REF, new, parent)
        return new


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)
        self.store = self.f.store()

    def apply(self, value=None):
        value = value or state()
        return self.store.compare_and_set(self.store.read()[1], value)

    def test_initial_multiple_journals_fresh_clone_resume(self):
        self.assertEqual(self.store.read(), (e.initial_state(), e.digest(e.initial_state())))
        first = self.apply(); second = self.apply(state(2))
        self.assertNotEqual(first, second); self.assertEqual(len(self.f.calls), 2)
        anchor = {k: self.f.receipts[-1][k] for k in ('commit', 'state_blob', 'state_sha256')}
        self.f.root = self.f.clone('fresh-clone')
        resumed = self.f.store(anchor)
        self.assertEqual(resumed.read(), (state(2), second))
        self.assertEqual(resumed.compare_and_set(second, state(3)), e.digest(state(3)))

    def test_old_anchor_cannot_resume_advanced_journal(self):
        self.apply()
        with self.assertRaises(j.JournalFailure): self.f.store().read()

    def test_unrelated_descendant_is_preserved(self):
        head = self.f.other_commit()
        self.apply()
        self.assertEqual(self.f.calls[0][0], head)
        self.assertEqual(run(self.f.bare, 'show', 'main:unrelated/new.md'), b'Concurrent change\n')

    def test_journal_changed_then_restored_stops(self):
        self.apply(); anchor = deepcopy(self.store.anchor)
        self.f.other_commit(j.STATE_PATH, e.encoded(state(99)))
        self.f.other_commit(j.STATE_PATH, e.encoded(state()))
        with self.assertRaisesRegex(j.JournalFailure, 'state_changed'): self.store.read()
        self.assertEqual(self.store.anchor, anchor)

    def test_expected_digest_mismatch_no_cas(self):
        with self.assertRaisesRegex(j.JournalFailure, 'state_changed'):
            self.store.compare_and_set('f' * 64, state())
        self.assertEqual(self.f.calls, [])

    def test_wrong_external_anchor_never_trusts_observed_state(self):
        anchor = deepcopy(self.store.anchor); anchor['state_sha256'] = 'f' * 64
        with self.assertRaisesRegex(j.JournalFailure, 'state_changed'): self.f.store(anchor).read()

    def test_private_repository_and_guard_required(self):
        self.f.metadata = lambda: {'full_name': j.REPOSITORY, 'private': False}
        with self.assertRaises(j.JournalFailure): self.apply()
        self.assertEqual(self.f.calls, [])

    def test_guard_failure_blocks_before_cas(self):
        self.f.guard_value = False
        with self.assertRaises(j.JournalFailure): self.apply()
        self.assertEqual(self.f.calls, [])

    def test_stale_old_ref_readonly_poll_no_second_cas(self):
        self.f.mode = 'stale'; self.apply()
        self.assertEqual(len(self.f.calls), 1); self.assertEqual(len(self.f.receipts), 1)

    def test_accepted_push_exception_readback_confirms_once(self):
        self.f.mode = 'accepted_throw'; self.apply()
        self.assertEqual(len(self.f.calls), 1); self.assertEqual(len(self.f.receipts), 1)

    def test_rejected_or_forever_stale_never_retries(self):
        for mode in ('reject', 'stale_forever'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                f = Fixture(directory); f.mode = mode; store = f.store(poll_attempts=2)
                with self.assertRaises(j.JournalFailure) as caught:
                    store.compare_and_set(e.digest(e.initial_state()), state())
                self.assertEqual(caught.exception.outcome, 'unknown')
                self.assertEqual(len(f.calls), 1); self.assertEqual(f.receipts, [])
                with self.assertRaises(j.JournalFailure): store.read()
                with self.assertRaises(j.JournalFailure): store.compare_and_set(e.digest(state()), state(2))
                self.assertEqual(len(f.calls), 1)

    def test_foreign_head_stops_immediately(self):
        self.f.mode = 'foreign_ref'
        with self.assertRaises(j.JournalFailure): self.apply()
        self.assertEqual(len(self.f.calls), 1); self.assertEqual(self.f.receipts, [])

    def test_wrong_readback_tree_parent_or_blob_never_confirms(self):
        for mode in ('tree', 'parent', 'blob'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                f = Fixture(directory); store = f.store()
                if mode == 'blob': f.bad_blob = True
                else: f.bad_commit = mode
                with self.assertRaises(j.JournalFailure):
                    store.compare_and_set(e.digest(e.initial_state()), state())
                self.assertEqual(len(f.calls), 1); self.assertEqual(f.receipts, [])

    def test_callback_requires_true_and_preserves_verified_recovery(self):
        for result in (False, None, 1, RuntimeError('PRIVATE SECRET')):
            with self.subTest(result=type(result).__name__), tempfile.TemporaryDirectory() as directory:
                f = Fixture(directory); f.confirm_value = result; store = f.store()
                with self.assertRaises(j.JournalFailure) as caught:
                    store.compare_and_set(e.digest(e.initial_state()), state())
                error = caught.exception
                self.assertEqual(str(error), 'journal_failed')
                self.assertEqual(error.public_report(), {'status': 'unknown', 'stage': 'confirm'})
                self.assertEqual(error.recovery['commit'], f.head())
                self.assertEqual(error.recovery['state_sha256'], e.digest(state()))
                self.assertTrue(error.recovery['readback_verified'])
                with self.assertRaises(j.JournalFailure): store.read()

    def test_noop_callback_failure_also_locks_instance(self):
        self.apply(); self.f.confirm_value = False
        with self.assertRaises(j.JournalFailure): self.apply()
        self.assertTrue(self.store.poisoned); self.assertEqual(len(self.f.calls), 1)

    def test_head_moves_after_confirmation_cannot_unlock_api(self):
        original = self.store.confirm
        def confirm(receipt):
            original(receipt); self.f.other_commit(); return True
        self.store.confirm = confirm
        with self.assertRaises(j.JournalFailure) as caught: self.apply()
        self.assertEqual(caught.exception.outcome, 'unknown')
        self.assertTrue(self.store.poisoned)
        self.assertTrue(caught.exception.recovery['readback_verified'])
        self.assertEqual(len(self.f.calls), 1)

    def test_readback_transport_failure_is_unknown_without_retry(self):
        original = self.f.ref
        def ref(name):
            if self.f.calls: raise TimeoutError('PRIVATE SECRET')
            return original(name)
        self.f.ref = ref
        with self.assertRaises(j.JournalFailure) as caught: self.apply()
        self.assertEqual(str(caught.exception), 'journal_failed')
        self.assertEqual(caught.exception.outcome, 'unknown')
        self.assertEqual(len(self.f.calls), 1); self.assertEqual(self.f.receipts, [])

    def test_only_five_bounded_delays_after_single_attempt(self):
        delays = []; self.store.sleep = delays.append; self.f.mode = 'reject'
        with self.assertRaises(j.JournalFailure): self.apply()
        self.assertEqual(delays, [1, 2, 4, 8, 16])
        self.assertEqual(len(self.f.calls), 1)

    def test_noop_uses_readback_confirmation_without_commit(self):
        self.apply(); digest = self.apply()
        self.assertEqual(digest, e.digest(state())); self.assertEqual(len(self.f.calls), 1)
        self.assertEqual(self.f.receipts[-1]['cas_attempts'], 0)

    def test_initial_empty_state_is_persisted_before_return(self):
        self.apply(e.initial_state())
        self.assertEqual(len(self.f.calls), 1)
        self.assertEqual(run(self.f.bare, 'show', 'main:' + j.STATE_PATH), e.encoded(e.initial_state()))

    def test_commit_and_distinct_blob_byte_budgets(self):
        self.store = self.f.store(max_commits=1)
        self.apply()
        with self.assertRaisesRegex(j.JournalFailure, 'request_budget'): self.apply(state(2))
        self.assertEqual(len(self.f.calls), 1)
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(directory); store = f.store(max_batch_bytes=len(e.encoded(state())) - 1)
            with self.assertRaisesRegex(j.JournalFailure, 'request_budget'):
                store.compare_and_set(e.digest(e.initial_state()), state())
            self.assertEqual(f.calls, [])

    def test_reused_distinct_blob_is_charged_once(self):
        budget = len(e.encoded(state())) + len(e.encoded(state(2)))
        self.store = self.f.store(max_batch_bytes=budget)
        self.apply(); self.apply(state(2)); self.apply()
        self.assertEqual(self.store.new_blob_bytes, budget)
        self.assertEqual(len(self.f.calls), 3)

    def test_state_32mib_limit_before_cas(self):
        value = state(); value['engine_state']['journal']['operation']['large'] = 'x' * j.MAX_STATE
        with self.assertRaisesRegex(j.JournalFailure, 'request_budget'):
            self.store.compare_and_set(e.digest(e.initial_state()), value)
        self.assertEqual(self.f.calls, [])

    def test_inherited_asset_stays_missing_and_local_refs_index_untouched(self):
        blob = run(self.f.bare, 'rev-parse', 'main:asset.bin').decode().strip()
        missing = (blob + ' missing\n').encode()
        local = self.store.local
        self.assertEqual(local.run('cat-file', '--batch-check', data=(blob + '\n').encode()), missing)
        refs = local.run('show-ref'); head = local.run('rev-parse', 'HEAD')
        index = (self.f.root / '.git/index').read_bytes()
        protected = run(self.f.bare, 'rev-parse', 'refs/tags/protected')
        self.apply()
        self.assertEqual(local.run('cat-file', '--batch-check', data=(blob + '\n').encode()), missing)
        self.assertEqual(local.run('show-ref'), refs); self.assertEqual(local.run('rev-parse', 'HEAD'), head)
        self.assertEqual((self.f.root / '.git/index').read_bytes(), index)
        self.assertEqual(run(self.f.bare, 'rev-parse', 'refs/tags/protected'), protected)
        changed = run(self.f.bare, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'main').decode().splitlines()
        self.assertEqual(changed, [j.STATE_PATH])

    def test_state_input_is_not_mutated(self):
        value = state(); before = deepcopy(value); self.apply(value)
        self.assertEqual(value, before)

    def test_same_instance_concurrent_call_rejected(self):
        self.store.lock.acquire()
        try:
            with self.assertRaisesRegex(j.JournalFailure, 'journal_busy'): self.store.read()
            with self.assertRaisesRegex(j.JournalFailure, 'journal_busy'):
                self.store.compare_and_set(e.digest(e.initial_state()), state())
        finally: self.store.lock.release()
        self.assertEqual(self.f.calls, [])

    def test_constructor_rejects_missing_anchor_or_unbounded_budgets(self):
        for change in ({'anchor': {}}, {'max_commits': 401},
                       {'max_batch_bytes': j.MAX_BATCH_BYTES + 1}, {'poll_attempts': 7}):
            with self.subTest(change=change):
                if 'anchor' in change:
                    with self.assertRaises(j.JournalFailure):
                        j.RemoteJournal(self.f.root, self.f, anchor={}, load_commit=self.f.load,
                            guard=self.f.guard, confirm=self.f.confirm)
                else:
                    with self.assertRaises(j.JournalFailure): self.f.store(**change)


class ExecutorOrderingTests(unittest.TestCase):
    def setUp(self):
        import test_notion_executor as fixture
        self.engine_fixture = fixture.Tests(methodName=
            'test_real_git_update_journal_before_writes_preserves_unowned_and_properties')
        self.engine_fixture.setUp()
        self.addCleanup(self.engine_fixture.doCleanups)
        f = self.engine_fixture
        initial, _ = f.store.read()
        f.publish()
        desc = f.publication.descriptor
        outer = e.initial_state(); outer['engine_state'] = initial
        receipt_hash = 'e' * 64
        outer.update(origin={'commit': desc['parent'], 'receipt_sha256': None, 'sequence': 0},
            policy_sha256='f' * 64,
            last_enqueued_publication={'commit': desc['commit'], 'receipt_sha256': receipt_hash, 'sequence': 1},
            observed_publication_tip={'commit': desc['commit'], 'receipt_sha256': receipt_hash, 'sequence': 1},
            pending_by_id={ident: {**row, 'commit': desc['commit'], 'parent': desc['parent'],
                'receipt_sha256': receipt_hash, 'sequence': 1} for ident, row in desc['records'].items()},
            rotation_order=list(desc['records']))
        e.check_state(outer); digest = e.digest(outer)
        desc.update(outbox_receipt_sha256=receipt_hash, outbox_state_sha256=digest)
        path = f.repo / j.STATE_PATH; path.parent.mkdir(parents=True)
        path.write_bytes(e.encoded(outer)); f.commit('Seed synthetic outbox')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.remote = Fixture(self.temp.name, seed=f.repo)
        blob = run(self.remote.bare, 'rev-parse', 'main:' + j.STATE_PATH).decode().strip()
        self.store = self.remote.store({'commit': self.remote.initial,
            'state_blob': blob, 'state_sha256': digest})
        f.store = e.EngineStore(self.store); f.api.store = f.store

    def test_executor_remote_journal_confirmed_before_each_notion_write(self):
        f = self.engine_fixture
        observed = []
        def before_write(kind):
            receipt = self.remote.receipts[-1]
            self.assertEqual(receipt['commit'], self.remote.head())
            self.assertTrue(receipt['readback_verified'])
            value, digest = self.store.read()
            self.assertEqual(receipt['state_sha256'], digest)
            self.assertEqual(value['engine_state']['journal']['operation']['phase'], 'in_flight')
            observed.append(kind)
        f.api.hook = before_write
        result = f.run_plan(f.plan())
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(observed, ['append', 'archive'])
        self.assertEqual(result['updated'], 1)
        self.assertIsNone(self.store.read()[0]['engine_state']['journal'])
        self.assertEqual(len(self.remote.calls), len(self.remote.receipts))

    def test_failed_inflight_confirmation_prevents_notion_api_write(self):
        f = self.engine_fixture
        normal = self.store.confirm
        def confirm(receipt):
            normal(receipt)
            raw = run(self.remote.bare, 'show', receipt['commit'] + ':' + j.STATE_PATH)
            operation = json.loads(raw)['engine_state']['journal']['operation']
            return operation is None or operation['phase'] != 'in_flight'
        self.store.confirm = confirm
        result = f.run_plan(f.plan())
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'journal_failed')
        self.assertEqual(result['external_write_attempts'], 0)
        self.assertEqual(f.writes(), [])
        self.assertTrue(self.store.poisoned)
        self.assertEqual(self.store.recovery['commit'], self.remote.head())


if __name__ == '__main__':
    unittest.main()
