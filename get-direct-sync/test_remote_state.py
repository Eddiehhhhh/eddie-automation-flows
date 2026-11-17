"""In-memory Git graph and SQLite tests; no credentials or network."""
import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core import CheckpointFailure, Hold, Store, canonical, snapshot
from remote_state import RemoteState, STATE_FILE, git_blob_sha, unpack_checkpoint
from test_core import Sink, note


class GitStateFake:
    def __init__(self):
        self.calls = []
        self.private = True
        self.blobs, self.trees, self.commits = {}, {}, {}
        self.before_patch = None
        self.patch_fault = None
        tree = self.object_sha('tree', [])
        self.trees[tree] = []
        self.head = self.object_sha('commit', {'tree': tree, 'parents': []})
        self.commits[self.head] = {'tree': {'sha': tree}, 'parents': []}

    @staticmethod
    def object_sha(kind, value):
        return hashlib.sha1((kind + ':' + canonical(value)).encode()).hexdigest()

    def ancestor(self, before, after):
        if before == after:
            return True
        return any(self.ancestor(before, x['sha']) for x in self.commits[after]['parents'])

    def document(self):
        tree = self.trees[self.commits[self.head]['tree']['sha']]
        entry = next(x for x in tree if x['path'] == STATE_FILE)
        return json.loads(base64.b64decode(self.blobs[entry['sha']]['content']))

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        path = path.removeprefix('/repos/owner/private')
        if path == '':
            return {'private': self.private, 'default_branch': 'main'}
        if path.startswith('/git/ref/'):
            return {'object': {'sha': self.head}}
        if path.startswith('/git/blobs/'):
            return copy.deepcopy(self.blobs[path.split('/')[-1]])
        if path.startswith('/git/trees/'):
            return {'tree': copy.deepcopy(self.trees[path.split('/')[-1]]), 'truncated': False}
        if path.startswith('/git/commits/'):
            return copy.deepcopy(self.commits[path.split('/')[-1]])
        if method == 'POST' and path == '/git/blobs':
            raw = base64.b64decode(body['content'])
            sha = git_blob_sha(raw)
            self.blobs[sha] = {'sha': sha, 'encoding': 'base64', 'content': body['content']}
            return {'sha': sha}
        if method == 'POST' and path == '/git/trees':
            tree = {x['path']: x for x in copy.deepcopy(self.trees[body['base_tree']])}
            tree.update({x['path']: x for x in body['tree']})
            value = sorted(tree.values(), key=lambda x: x['path'])
            sha = self.object_sha('tree', value)
            self.trees[sha] = value
            return {'sha': sha}
        if method == 'POST' and path == '/git/commits':
            sha = self.object_sha('commit', body)
            self.commits[sha] = {'tree': {'sha': body['tree']},
                                 'parents': [{'sha': p} for p in body['parents']]}
            return {'sha': sha}
        if method == 'PATCH' and path.startswith('/git/refs/'):
            self.assert_nonforce(body)
            if self.before_patch:
                callback, self.before_patch = self.before_patch, None
                callback()
            if not self.ancestor(self.head, body['sha']):
                raise Hold('http_422')
            if self.patch_fault == 'before':
                self.patch_fault = None
                raise TimeoutError('synthetic_unknown')
            self.head = body['sha']
            if self.patch_fault == 'after':
                self.patch_fault = None
                raise TimeoutError('synthetic_unknown')
            return {'object': {'sha': self.head}}
        raise AssertionError((method, path))

    @staticmethod
    def assert_nonforce(body):
        assert body['force'] is False


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.git = GitStateFake()

    def state(self, owner='run-1', namespace='test'):
        return RemoteState(self.git, 'owner/private', 'codex/get-state-test', namespace, owner)

    def directory(self, name):
        p = self.root / name
        p.mkdir(mode=0o700)
        return p / 'state.sqlite'

    def test_roundtrip_checkpoint_and_intent_survives_new_worker(self):
        first = self.state()
        path = self.directory('one')
        self.assertFalse(first.acquire(path)['restored'])
        store = Store(path, checkpoint=first.checkpoint)
        store.bind({'namespace': 'test'})
        store.accept([])
        store.accept([snapshot(note())])
        intent = {'phase': 'page_create_intent', 'version': 'synthetic'}
        store.set('notion:test', intent)
        self.assertEqual(store.pending_count(), 1)
        store.db.close()
        first.release()
        second = self.state('run-2')
        path2 = self.directory('two')
        self.assertTrue(second.acquire(path2)['restored'])
        restored = Store(path2, checkpoint=second.checkpoint)
        self.assertEqual(restored.get('notion:test'), intent)
        self.assertEqual(restored.pending_count(), 1)
        self.assertEqual(restored.db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        restored.db.close()
        second.release()
        self.assertIsNone(self.git.document()['lease'])

    def test_racing_owners_only_one_wins(self):
        a, b = self.state('run-a'), self.state('run-b')
        self.git.before_patch = lambda: b.acquire(self.directory('b'))
        with self.assertRaisesRegex(Hold, 'ref_update_unconfirmed'):
            a.acquire(self.directory('a'))
        self.assertEqual(self.git.document()['lease']['owner'], 'run-b')
        self.assertIsNone(a.lease)
        b.release()

    def test_old_lease_is_not_stolen_even_by_same_owner_name(self):
        first = self.state()
        first.acquire(self.directory('one'))
        head = self.git.head
        for name in ('run-2', 'run-1'):
            with self.assertRaisesRegex(Hold, 'owned_no_takeover'):
                self.state(name).acquire(self.directory(name))
        self.assertEqual(self.git.head, head)
        first.release()

    def test_unknown_ref_response_is_read_back_without_second_patch(self):
        self.git.patch_fault = 'after'
        state = self.state()
        state.acquire(self.directory('one'))
        writes = [x for x in self.git.calls if x[0] == 'PATCH']
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.git.document()['lease'], state.lease)

    def test_unknown_unapplied_ref_is_not_retried(self):
        head = self.git.head
        self.git.patch_fault = 'before'
        with self.assertRaisesRegex(Hold, 'ref_update_unconfirmed'):
            self.state().acquire(self.directory('one'))
        self.assertEqual(self.git.head, head)
        self.assertEqual(len([x for x in self.git.calls if x[0] == 'PATCH']), 1)

    def test_checkpoint_failure_after_first_receipt_stops_second_sink(self):
        fail = [False]
        def checkpoint(db):
            if fail[0] and db.execute('SELECT count(*) FROM receipts').fetchone()[0]:
                raise OSError('synthetic storage failure')
        store = Store(':memory:', checkpoint=checkpoint)
        store.accept([])
        store.accept([snapshot(note())])
        a, b = Sink(), Sink()
        fail[0] = True
        with self.assertRaises(CheckpointFailure):
            store.drain({'github': a, 'notion': b})
        self.assertEqual((a.calls, b.calls), (1, 0))
        with self.assertRaises(CheckpointFailure):
            store.drain({'github': a, 'notion': b})
        self.assertEqual((a.calls, b.calls), (1, 0))

    def test_journal_checkpoint_error_prevents_target_write(self):
        fail = [False]
        def checkpoint(db):
            if fail[0] and db.execute("SELECT 1 FROM journal WHERE k='intent'").fetchone():
                raise OSError('synthetic unavailable')
        store = Store(':memory:', checkpoint=checkpoint)
        store.accept([])
        store.accept([snapshot(note())])
        calls = []
        class Writer:
            def sync(self, s, receipt):
                store.set('intent', {'phase': 'intent'})
                calls.append('external-write')
        fail[0] = True
        other = Sink()
        with self.assertRaises(CheckpointFailure):
            store.drain({'github': Writer(), 'notion': other})
        self.assertEqual(calls, [])
        self.assertEqual(other.calls, 0)

    def test_historical_batches_are_idempotent_and_keep_receipts(self):
        store = Store(':memory:')
        s = snapshot(note())
        store.accept([s])
        self.assertEqual(store.enqueue([s])['enqueued'], 1)
        self.assertEqual(store.enqueue([s])['enqueued'], 0)
        self.assertEqual(store.drain({'github': Sink(), 'notion': Sink()})['pending'], 0)
        before = store.receipt('github', s['id'])
        self.assertEqual(store.enqueue([s])['enqueued'], 0)
        self.assertEqual(store.receipt('github', s['id']), before)
        self.assertEqual(store.pending_count(), 0)

    def test_replayed_old_history_cannot_roll_back_new_delivery(self):
        store = Store(':memory:')
        first = snapshot(note())
        second = snapshot(note(text='newer', updated='2026-11-18'))
        store.accept([first])
        store.enqueue([first])
        store.drain({'github': Sink(), 'notion': Sink()})
        store.accept([second])
        store.drain({'github': Sink(), 'notion': Sink()})
        self.assertEqual(store.enqueue([first])['enqueued'], 0)
        self.assertEqual(store.receipt('notion', first['id'])['version'], second['version'])
        self.assertEqual(store.pending_count(), 0)

    def test_undelivered_stale_history_batch_is_rejected(self):
        store = Store(':memory:')
        old = snapshot(note())
        current = snapshot(note(text='newer', updated='2026-11-18'))
        store.accept([current])
        with self.assertRaisesRegex(Hold, 'history_snapshot_not_current'):
            store.enqueue([old])
        self.assertEqual(store.pending_count(), 0)

    def test_history_requires_existing_baseline(self):
        with self.assertRaisesRegex(Hold, 'requires_baseline'):
            Store(':memory:').enqueue([snapshot(note())])

    def test_held_reason_is_durable_and_private_exception_is_redacted(self):
        path = self.directory('one')
        store = Store(path)
        store.accept([])
        store.accept([snapshot(note())])
        class PrivateError:
            def sync(self, s, receipt):
                raise RuntimeError('https://signed.invalid/private?token=synthetic-secret')
        result = store.drain({'github': PrivateError(), 'notion': Sink()})
        self.assertEqual(result['pending'], 1)
        store.db.close()
        restored = Store(path)
        rows = restored.db.execute('SELECT reason FROM held').fetchall()
        self.assertEqual(rows, [('unexpected_sink_failure',)])
        restored._held('github','1','v',Hold('lowercase_private_secret'))
        self.assertEqual(restored.db.execute("SELECT reason FROM held WHERE version='v'").fetchone()[0], 'unexpected_sink_failure')
        restored.db.close()

    def test_binding_and_public_repo_blocked(self):
        state = self.state()
        state.acquire(self.directory('one'))
        state.release()
        with self.assertRaisesRegex(Hold, 'binding_mismatch'):
            self.state('run-2', 'different').acquire(self.directory('two'))
        self.git.private = False
        with self.assertRaisesRegex(Hold, 'private_state_branch'):
            self.state('run-3').acquire(self.directory('three'))

    def test_default_branch_and_existing_local_file_blocked(self):
        with self.assertRaises(Hold):
            RemoteState(self.git, 'owner/private', 'main', 'test', 'run-1')
        path = self.directory('one')
        path.write_bytes(b'keep')
        with self.assertRaisesRegex(Hold, 'empty_path'):
            self.state().acquire(path)
        self.assertEqual(path.read_bytes(), b'keep')
        self.assertFalse(any(x[0] == 'PATCH' for x in self.git.calls))

    def test_release_requires_exact_lease_not_display_owner(self):
        state = self.state()
        state.acquire(self.directory('one'))
        forged = self.state()
        forged.lease = {'owner': 'run-1', 'lease_id': 'other'}
        with self.assertRaisesRegex(Hold, 'release_not_owner'):
            forged.release()
        self.assertEqual(self.git.document()['lease'], state.lease)
        state.release()

    def test_remote_blob_corruption_is_rejected_before_lock_or_restore(self):
        state = self.state()
        state.acquire(self.directory('one'))
        state.release()
        entry = self.git.trees[self.git.commits[self.git.head]['tree']['sha']][0]
        self.git.blobs[entry['sha']]['content'] = base64.b64encode(b'changed').decode()
        before = len(self.git.calls)
        with self.assertRaisesRegex(Hold, 'blob_invalid'):
            self.state('run-2').acquire(self.directory('two'))
        self.assertFalse(any(x[0] == 'PATCH' for x in self.git.calls[before:]))

    def test_compressed_limit_fails_without_publishing_new_checkpoint(self):
        state = self.state()
        path = self.directory('one')
        state.acquire(path)
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE sample(value)')
        head = self.git.head
        with patch('remote_state.MAX_COMPRESSED', 16):
            with self.assertRaises(CheckpointFailure):
                state.checkpoint(db)
        self.assertEqual(self.git.head, head)
        self.assertIsNone(self.git.document()['checkpoint'])
        db.close()

    def test_mutations_are_checkpointed_and_lease_loss_stops_targets(self):
        state = self.state()
        path = self.directory('one')
        state.acquire(path)
        store = Store(path, checkpoint=state.checkpoint)
        store.bind({'target': 'test'})
        store.accept([])
        store.set('intent', {'phase': 'write'})
        raw = unpack_checkpoint(self.git.document()['checkpoint'])
        self.assertTrue(raw.startswith(b'SQLite format 3\0'))
        state.release()
        a, b = Sink(), Sink()
        with self.assertRaises(CheckpointFailure):
            store.accept([snapshot(note())])
        self.assertEqual((a.calls, b.calls), (0, 0))
        store.db.close()


class ActionGitStateFake(GitStateFake):
    runtime_repo = 'runtime/sync'

    def __init__(self):
        super().__init__()
        self.attempts = {}
        self.actions_error = False

    def attempt(self, run_id, attempt, status):
        row = {'id': run_id, 'run_attempt': attempt, 'status': status,
               'path': '.github/workflows/get-direct-sync.yml', 'workflow_id': 7,
               'repository': {'full_name': self.runtime_repo, 'id': 50, 'fork': False},
               'head_repository': {'full_name': self.runtime_repo, 'id': 50, 'fork': False},
               'head_sha': 'd' * 40, 'conclusion': 'failure' if status == 'completed' else None}
        self.attempts[(run_id, attempt)] = row
        return row

    def request(self, method, path, body=None):
        match = re.fullmatch('/repos/runtime/sync/actions/runs/([0-9]+)/attempts/([0-9]+)', path)
        if match:
            self.calls.append((method, path, copy.deepcopy(body)))
            assert method == 'GET' and body is None
            if self.actions_error:
                raise OSError('synthetic control-plane unavailable')
            return copy.deepcopy(self.attempts.get((int(match[1]), int(match[2]))))
        return super().request(method, path, body)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.git = ActionGitStateFake()
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def state(self, owner):
        return RemoteState(self.git, 'owner/private', 'codex/get-state-test', 'test', owner)

    def path(self, name):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        return directory / 'state.sqlite'

    def context(self, run_id=200, attempt=1):
        return {'GITHUB_REPOSITORY': 'runtime/sync',
                'GITHUB_WORKFLOW_REF': 'runtime/sync/.github/workflows/get-direct-sync.yml@refs/heads/main',
                'GITHUB_RUN_ID': str(run_id), 'GITHUB_RUN_ATTEMPT': str(attempt)}

    def seed(self, owner='get-direct-100-1'):
        old = self.state(owner)
        path = self.path('old')
        old.acquire(path)
        store = Store(path, checkpoint=old.checkpoint)
        store.set('notion:synthetic', {'phase': 'page_create_intent', 'version': 'synthetic'})
        store.db.close()
        return old, copy.deepcopy(self.git.document()['checkpoint'])

    def control(self, current_run=200, current_attempt=1, old_status='completed'):
        self.git.attempt(100, 1, old_status)
        self.git.attempt(current_run, current_attempt, 'in_progress')

    def test_completed_attempt_recovers_checkpoint_with_auditable_commit(self):
        old, checkpoint = self.seed()
        self.control()
        new = self.state('get-direct-200-1')
        path = self.path('new')
        with patch.dict(os.environ, self.context(), clear=True):
            self.assertTrue(new.acquire(path)['restored'])
        self.assertEqual(self.git.document()['checkpoint'], checkpoint)
        self.assertEqual(self.git.document()['lease']['owner'], new.owner)
        self.assertEqual(new.recovery_evidence['previous_run_id'], 100)
        self.assertEqual(new.recovery_evidence['previous_attempt'], 1)
        self.assertEqual(new.recovery_evidence['previous_status'], 'completed')
        self.assertEqual(new.recovery_evidence['checkpoint_sha256'], checkpoint['sha256'])
        messages = [body['message'] for method, route, body in self.git.calls
                    if method == 'POST' and route.endswith('/git/commits')]
        recovery = [message for message in messages if message.startswith('Recover completed Get sync run ')]
        self.assertEqual(len(recovery), 1)
        self.assertIn('100 attempt 1', recovery[0])
        self.assertIn('status=completed', recovery[0])
        self.assertIn(checkpoint['sha256'], recovery[0])
        self.assertNotIn('synthetic', recovery[0])
        restored = Store(path)
        self.assertEqual(restored.get('notion:synthetic')['phase'], 'page_create_intent')
        restored.db.close()
        with self.assertRaisesRegex(Hold, 'release_not_owner'):
            old.release()
        new.release()

    def test_running_owner_remains_held(self):
        self.seed()
        self.control(old_status='in_progress')
        head = self.git.head
        with patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
            self.state('get-direct-200-1').acquire(self.path('new'))
        self.assertEqual(self.git.head, head)

    def test_unknown_owner_never_authorizes_recovery(self):
        self.seed('other-program-100-1')
        self.control()
        head = self.git.head
        with patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
            self.state('get-direct-200-1').acquire(self.path('new'))
        self.assertEqual(self.git.head, head)
        self.assertFalse(any('/actions/runs/' in path for _, path, _ in self.git.calls))

    def test_missing_official_context_never_recovers(self):
        self.seed()
        self.control()
        head = self.git.head
        with self.assertRaises(Hold):
            self.state('get-direct-200-1').acquire(self.path('new'))
        self.assertEqual(self.git.head, head)

    def test_wrong_repo_workflow_or_fork_never_recovers(self):
        self.seed()
        self.control()
        baseline = copy.deepcopy(self.git.attempts)
        mutations = [('path', '.github/workflows/other.yml'),
                     ('repository', {'full_name': 'other/repo', 'id': 50, 'fork': False}),
                     ('head_repository', {'full_name': 'other/fork', 'id': 51, 'fork': True}),
                     ('head_repository', {'full_name': 'runtime/sync', 'id': 50, 'fork': True})]
        for index, (field, value) in enumerate(mutations):
            self.git.attempts = copy.deepcopy(baseline)
            self.git.attempts[(100, 1)][field] = value
            head = self.git.head
            with self.subTest(field=field), patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
                self.state('get-direct-200-1').acquire(self.path('bad' + str(index)))
            self.assertEqual(self.git.head, head)

    def test_current_owner_must_match_official_running_attempt(self):
        self.seed()
        self.control()
        for index, owner in enumerate(('get-direct-201-1', 'get-direct-200-2', 'local-session')):
            head = self.git.head
            with patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
                self.state(owner).acquire(self.path('new' + str(index)))
            self.assertEqual(self.git.head, head)
        self.git.attempts[(200, 1)]['status'] = 'completed'
        with patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
            self.state('get-direct-200-1').acquire(self.path('completed-current'))

    def test_rerun_attempt_uses_completed_prior_attempt_not_latest_run(self):
        self.seed()
        self.control(current_run=100, current_attempt=2)
        new = self.state('get-direct-100-2')
        with patch.dict(os.environ, self.context(100, 2), clear=True):
            new.acquire(self.path('retry'))
        self.assertEqual(new.recovery_evidence['previous_attempt'], 1)
        self.assertEqual(new.recovery_evidence['current_attempt'], 2)
        routes = [path for method, path, _ in self.git.calls if '/actions/runs/' in path]
        self.assertIn('/repos/runtime/sync/actions/runs/100/attempts/1', routes)
        self.assertIn('/repos/runtime/sync/actions/runs/100/attempts/2', routes)
        new.release()

    def test_attempt_response_must_match_requested_attempt(self):
        self.seed()
        self.control()
        self.git.attempts[(100, 1)]['run_attempt'] = 2
        head = self.git.head
        with patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
            self.state('get-direct-200-1').acquire(self.path('new'))
        self.assertEqual(self.git.head, head)

    def test_competing_recovery_owners_only_one_acquires(self):
        self.seed()
        self.control()
        self.git.attempt(201, 1, 'in_progress')
        a, b = self.state('get-direct-200-1'), self.state('get-direct-201-1')
        path_b = self.path('b')
        def compete():
            with patch.dict(os.environ, self.context(201, 1), clear=True):
                b.acquire(path_b)
        self.git.before_patch = compete
        with patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
            a.acquire(self.path('a'))
        self.assertIsNone(a.lease)
        self.assertEqual(self.git.document()['lease'], b.lease)
        b.release()

    def test_control_plane_unavailable_keeps_lease_and_checkpoint(self):
        self.seed()
        self.control()
        self.git.actions_error = True
        head = self.git.head
        before = copy.deepcopy(self.git.document())
        with patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
            self.state('get-direct-200-1').acquire(self.path('new'))
        self.assertEqual(self.git.head, head)
        self.assertEqual(self.git.document(), before)

    def test_other_workflow_env_and_url_are_rejected(self):
        self.seed()
        self.control()
        for index, ref in enumerate(('runtime/sync/.github/workflows/other.yml@refs/heads/main',
                                     'https://api.github.com/repos/runtime/sync/actions/runs/100')):
            env = self.context()
            env['GITHUB_WORKFLOW_REF'] = ref
            head = self.git.head
            with patch.dict(os.environ, env, clear=True), self.assertRaises(Hold):
                self.state('get-direct-200-1').acquire(self.path('new' + str(index)))
            self.assertEqual(self.git.head, head)

    def test_unknown_or_mismatched_control_metadata_never_clears_lease(self):
        self.seed()
        self.control()
        baseline = copy.deepcopy(self.git.attempts)
        cases = [((100, 1), 'status', None), ((100, 1), 'id', 101),
                 ((100, 1), 'workflow_id', 8), ((200, 1), 'head_sha', 'unknown'),
                 ((200, 1), 'path', '.github/workflows/other.yml'),
                 ((200, 1), 'head_repository', None),
                 ((100, 1), 'head_repository', {'full_name': 'runtime/sync', 'id': 51, 'fork': False}),
                 ((200, 1), 'repository', {'full_name': 'runtime/sync', 'id': 50, 'fork': True})]
        for index, (key, field, value) in enumerate(cases):
            self.git.attempts = copy.deepcopy(baseline)
            self.git.attempts[key][field] = value
            before = copy.deepcopy(self.git.document())
            head = self.git.head
            with self.subTest(key=key, field=field), patch.dict(os.environ, self.context(), clear=True), self.assertRaises(Hold):
                self.state('get-direct-200-1').acquire(self.path('bad-metadata' + str(index)))
            self.assertEqual(self.git.head, head)
            self.assertEqual(self.git.document(), before)

    def test_explicit_legacy_prefix_and_github_fallback_remain_bound_to_runs(self):
        self.seed('get-run-get-direct-100-1')
        self.control()
        new = self.state('github-200-1')
        with patch.dict(os.environ, self.context(), clear=True):
            new.acquire(self.path('new'))
        self.assertEqual(new.recovery_evidence['previous_run_id'], 100)
        self.assertEqual(new.recovery_evidence['current_run_id'], 200)
        new.release()


if __name__ == '__main__':
    unittest.main()
