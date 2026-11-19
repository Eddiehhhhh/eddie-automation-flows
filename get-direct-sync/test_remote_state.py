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
from retry import HTTPFailure, TransportFailure


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
        if method == 'PUT' and path == '/contents/' + STATE_FILE:
            if self.before_patch:
                callback, self.before_patch = self.before_patch, None
                callback()
            current_tree = self.trees[self.commits[self.head]['tree']['sha']]
            entry = next((item for item in current_tree if item['path'] == STATE_FILE), None)
            if body.get('sha') != (entry['sha'] if entry else None):
                raise HTTPFailure(409)
            if self.patch_fault == 'before':
                self.patch_fault = None
                raise TimeoutError('synthetic_unknown')
            raw = base64.b64decode(body['content'])
            sha = git_blob_sha(raw)
            self.blobs[sha] = {'sha': sha, 'encoding': 'base64', 'content': body['content']}
            rows = {item['path']: copy.deepcopy(item) for item in current_tree}
            rows[STATE_FILE] = {'path': STATE_FILE, 'mode': '100644', 'type': 'blob', 'sha': sha}
            tree = sorted(rows.values(), key=lambda item: item['path'])
            tree_sha = self.object_sha('tree', tree)
            self.trees[tree_sha] = tree
            parents = [{'sha': self.head}]
            commit = {'message': body['message'], 'tree': tree_sha, 'parents': [self.head]}
            head = self.object_sha('commit', commit)
            self.commits[head] = {'tree': {'sha': tree_sha}, 'parents': parents}
            self.head = head
            if self.patch_fault == 'after':
                self.patch_fault = None
                raise TimeoutError('synthetic_unknown')
            return {'content': {'sha': sha}, 'commit': {'sha': head, 'parents': parents}}
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
        sleeper = patch('remote_state.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)
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

    def test_state_publish_is_one_contents_write_and_file_sha_cas(self):
        state=self.state()
        path=self.directory('one')
        before=len(self.git.calls)
        state.acquire(path)
        writes=[row for row in self.git.calls[before:] if row[0]!='GET']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0][0],'PUT')
        self.assertTrue(writes[0][1].endswith('/contents/'+STATE_FILE))
        self.assertNotIn('sha',writes[0][2])
        store=Store(path,checkpoint=state.checkpoint)
        before_doc=self.git.document()
        previous_blob=git_blob_sha(canonical(before_doc).encode())
        before=len(self.git.calls)
        store.set('synthetic-intent',{'phase':'write_intent'})
        writes=[row for row in self.git.calls[before:] if row[0]!='GET']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0][0],'PUT')
        self.assertEqual(writes[0][2]['sha'],previous_blob)
        self.assertEqual(writes[0][2]['branch'],'codex/get-state-test')
        store.db.close()
        state.release()

    def test_legacy_gitdata_checkpoint_survives_contents_writer(self):
        import zlib
        from remote_state import SCHEMA
        legacy_path=self.directory('legacy')
        old=Store(legacy_path)
        old.set('legacy-intent',{'phase':'page_create_intent','value':'synthetic'})
        old.db.close()
        raw=legacy_path.read_bytes();packed=zlib.compress(raw)
        checkpoint={'encoding':'zlib+base64','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                    'compressed_sha256':hashlib.sha256(packed).hexdigest(),
                    'data':base64.b64encode(packed).decode()}
        doc={'schema':SCHEMA,'binding':{'repo':'owner/private','branch':'codex/get-state-test','namespace':'test'},
             'lease':None,'checkpoint':checkpoint}
        prefix='/repos/owner/private'
        blob=self.git.request('POST',prefix+'/git/blobs',{'encoding':'base64','content':base64.b64encode(canonical(doc).encode()).decode()})
        parent=self.git.head;tree_before=self.git.commits[parent]['tree']['sha']
        tree=self.git.request('POST',prefix+'/git/trees',{'base_tree':tree_before,
             'tree':[{'path':STATE_FILE,'mode':'100644','type':'blob','sha':blob['sha']}]})
        commit=self.git.request('POST',prefix+'/git/commits',{'message':'Synthetic legacy state',
                                     'tree':tree['sha'],'parents':[parent]})
        self.git.request('PATCH',prefix+'/git/refs/heads/codex/get-state-test',{'sha':commit['sha'],'force':False})
        before=len(self.git.calls)
        state=self.state();restored=self.directory('restored')
        self.assertTrue(state.acquire(restored)['restored'])
        self.assertEqual(restored.read_bytes(),raw)
        self.assertEqual(self.git.document()['checkpoint'],checkpoint)
        value=Store(restored)
        self.assertEqual(value.get('legacy-intent'),{'phase':'page_create_intent','value':'synthetic'})
        value.db.close();state.release()
        writes=[row for row in self.git.calls[before:] if row[0]!='GET']
        self.assertTrue(all(method=='PUT' and route.endswith('/contents/'+STATE_FILE) for method,route,body in writes))
        self.assertEqual(self.git.document()['checkpoint'],checkpoint)

    def test_branch_head_changed_before_put_prevents_any_write(self):
        state=self.state();original=self.git.request
        changed=[False]
        def request(method,path,body=None):
            if state._stage=='check_publish_ref' and not changed[0]:
                changed[0]=True
                before=self.git.head
                after=self.git.object_sha('commit',{'other':'synthetic','parent':before})
                self.git.commits[after]={'tree':copy.deepcopy(self.git.commits[before]['tree']),'parents':[{'sha':before}]}
                self.git.head=after
            return original(method,path,body)
        with patch.object(self.git,'request',request),self.assertRaisesRegex(Hold,'publish_head_changed'):
            state.acquire(self.directory('one'))
        self.assertFalse(any(method=='PUT' for method,path,body in self.git.calls))
        self.assertIsNone(state.lease)

    def test_unrelated_head_race_inside_contents_put_freezes_before_claiming_lease(self):
        def unrelated():
            before=self.git.head
            after=self.git.object_sha('commit',{'other':'synthetic','parent':before})
            self.git.commits[after]={'tree':copy.deepcopy(self.git.commits[before]['tree']),'parents':[{'sha':before}]}
            self.git.head=after
        state=self.state()
        self.git.before_patch=unrelated
        with self.assertRaisesRegex(Hold,'publish_parent_changed'):
            state.acquire(self.directory('one'))
        self.assertIsNone(state.lease)
        self.assertEqual(sum(method=='PUT' for method,path,body in self.git.calls),1)
        self.assertEqual(self.git.document()['lease']['owner'],state.owner)

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
        with self.assertRaisesRegex(Hold, 'contents_update_unconfirmed'):
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
        writes = [x for x in self.git.calls if x[0] == 'PUT']
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.git.document()['lease'], state.lease)

    def test_unknown_unapplied_ref_is_not_retried(self):
        head = self.git.head
        self.git.patch_fault = 'before'
        with self.assertRaisesRegex(Hold, 'contents_update_unconfirmed'):
            self.state().acquire(self.directory('one'))
        self.assertEqual(self.git.head, head)
        self.assertEqual(len([x for x in self.git.calls if x[0] == 'PUT']), 1)

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
        self.assertFalse(any(x[0] == 'PUT' for x in self.git.calls))

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
        self.assertFalse(any(x[0] == 'PUT' for x in self.git.calls[before:]))

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


class CheckpointDiagnosticTests(unittest.TestCase):
    setUp = Tests.setUp
    state = Tests.state
    directory = Tests.directory

    def prepared(self):
        state = self.state()
        state.acquire(self.directory('one'))
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE sample(value)')
        return state, db

    def test_http_status_and_fixed_stage_survive_without_private_details(self):
        for status in (403, 429, 503):
            for method, suffix, stage in (('GET', '/git/ref/', 'read_ref'),
                    ('GET', '/git/trees/', 'read_tree'),
                    ('PUT', '/contents/' + STATE_FILE, 'write_contents')):
                with self.subTest(status=status, stage=stage), tempfile.TemporaryDirectory() as folder:
                    git = GitStateFake()
                    state = RemoteState(git, 'owner/private', 'codex/get-state-test', 'test', 'run-1')
                    state.acquire(Path(folder).resolve() / 'state.sqlite')
                    original = git.request
                    def request(m, p, body=None):
                        if m == method and suffix in p:
                            raise HTTPFailure(status, 'https://private.invalid/?token=secret')
                        return original(m, p, body)
                    with sqlite3.connect(':memory:') as db, patch.object(git, 'request', request):
                        db.execute('CREATE TABLE sample(value)')
                        with self.assertRaises(CheckpointFailure) as caught:
                            state.checkpoint(db)
                    error = caught.exception
                    self.assertEqual(error.reason, 'checkpoint_http_' + str(status))
                    self.assertEqual(error.stage, stage)
                    self.assertNotIn('secret', repr(error) + repr(vars(error)))

    def test_publish_reuses_verified_tree_and_parent_without_extra_reads(self):
        state, db = self.prepared()
        first = len(self.git.calls)
        state.checkpoint(db)
        calls = self.git.calls[first:]
        self.assertEqual(sum('/git/trees/' in path for method,path,_ in calls), 1)
        self.assertEqual(sum('/git/commits/' in path for method,path,_ in calls), 1)
        self.assertEqual(sum(method=='PUT' for method,_,_ in calls), 1)
        self.assertIsNotNone(self.git.document()['checkpoint'])

    def test_transient_old_head_readback_reconciles_without_second_write(self):
        state, db = self.prepared()
        original = self.git.request
        stale, before = [], len(self.git.calls)
        def request(method, path, body=None):
            if method == 'GET' and '/git/ref/' in path and stale:
                return {'object': {'sha': stale.pop()}}
            old_head = self.git.head
            result = original(method, path, body)
            if method == 'PUT':
                stale.append(old_head)
            return result
        with patch.object(self.git, 'request', request):
            state.checkpoint(db)
        self.assertIsNotNone(self.git.document()['checkpoint'])
        self.assertEqual(sum(x[0] == 'PUT' for x in self.git.calls[before:]), 1)

    def test_persistent_old_head_readback_still_fails_without_second_write(self):
        state, db = self.prepared()
        original = self.git.request
        stale, before = [], len(self.git.calls)
        def request(method, path, body=None):
            if method == 'GET' and '/git/ref/' in path and stale:
                return {'object': {'sha': stale[-1]}}
            old_head = self.git.head
            result = original(method, path, body)
            if method == 'PUT':
                stale.append(old_head)
            return result
        with patch.object(self.git, 'request', request):
            with self.assertRaises(CheckpointFailure) as caught:
                state.checkpoint(db)
        self.assertEqual(caught.exception.reason, 'checkpoint_readback_old_head')
        self.assertEqual(sum(x[0] == 'PUT' for x in self.git.calls[before:]), 1)

    def test_old_head_followed_by_competing_head_is_rejected_immediately(self):
        state, db = self.prepared()
        original = state._read
        old = self.git.head
        reads = []
        def read(**kwargs):
            head, tree, doc = original(**kwargs)
            reads.append(head)
            if len(reads) == 2:
                return old, tree, doc
            if len(reads) == 3:
                return 'f' * 40, tree, doc
            return head, tree, doc
        before = len(self.git.calls)
        with patch.object(state, '_read', read), self.assertRaises(CheckpointFailure) as caught:
            state.checkpoint(db)
        self.assertEqual(caught.exception.reason, 'checkpoint_readback_other_head')
        self.assertEqual(len(reads), 3)
        self.assertEqual(sum(x[0] == 'PUT' for x in self.git.calls[before:]), 1)

    def test_lost_write_response_and_transient_old_head_reconcile_once(self):
        state, db = self.prepared()
        original = self.git.request
        old_head, reads, before = self.git.head, [], len(self.git.calls)
        def request(method, path, body=None):
            if method == 'GET' and '/git/ref/' in path and reads:
                reads.pop()
                return {'object': {'sha': old_head}}
            result = original(method, path, body)
            if method == 'PUT':
                reads.append(True)
                raise TimeoutError()
            return result
        with patch.object(self.git, 'request', request):
            state.checkpoint(db)
        self.assertIsNotNone(self.git.document()['checkpoint'])
        self.assertEqual(sum(x[0] == 'PUT' for x in self.git.calls[before:]), 1)

    def test_other_head_and_content_mismatch_remain_distinct_and_closed(self):
        for mode in ('other_head', 'content_mismatch'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                git = GitStateFake()
                state = RemoteState(git, 'owner/private', 'codex/get-state-test', 'test', 'run-1')
                state.acquire(Path(folder).resolve() / 'state.sqlite')
                original = state._read
                reads = []
                def read(**kwargs):
                    head, tree, doc = original(**kwargs)
                    reads.append(head)
                    if len(reads) == 2:
                        if mode == 'other_head':
                            head = 'f' * 40
                        else:
                            doc['lease'] = None
                    return head, tree, doc
                with sqlite3.connect(':memory:') as db, patch.object(state, '_read', read):
                    db.execute('CREATE TABLE sample(value)')
                    with self.assertRaises(CheckpointFailure) as caught:
                        state.checkpoint(db)
                self.assertEqual(caught.exception.reason, 'checkpoint_readback_' + mode)

    def test_contents_response_blob_mismatch_is_not_accepted_as_checkpoint(self):
        state,db=self.prepared();original=self.git.request
        def request(method,path,body=None):
            result=original(method,path,body)
            if method=='PUT':result['content']['sha']='f'*40
            return result
        with patch.object(self.git,'request',request),self.assertRaises(CheckpointFailure) as caught:
            state.checkpoint(db)
        self.assertEqual(caught.exception.reason,'checkpoint_blob_write_mismatch')

    def test_lease_loss_has_fixed_reason(self):
        state, db = self.prepared()
        state.lease = {'owner': state.owner, 'lease_id': 'different'}
        with self.assertRaises(CheckpointFailure) as caught:
            state.checkpoint(db)
        self.assertEqual(caught.exception.reason, 'checkpoint_lease_lost')

    def test_unrecognized_exception_details_and_reason_are_redacted(self):
        state, db = self.prepared()
        error = Hold('https://private.invalid/?token=secret')
        error.reason = 'checkpoint_private_secret'
        with patch.object(state, '_read', side_effect=error):
            with self.assertRaises(CheckpointFailure) as caught:
                state.checkpoint(db)
        self.assertEqual(caught.exception.reason, 'checkpoint_other')
        self.assertNotIn('secret', repr(caught.exception) + repr(vars(caught.exception)))

    def test_transport_failure_is_fixed_and_does_not_retry_writes(self):
        state, db = self.prepared()
        original = self.git.request
        calls = []
        def request(method, path, body=None):
            if method == 'PUT':
                calls.append(method)
                raise TransportFailure()
            return original(method, path, body)
        with patch.object(self.git, 'request', request):
            with self.assertRaises(CheckpointFailure) as caught:
                state.checkpoint(db)
        self.assertEqual(caught.exception.reason, 'checkpoint_transport_uncertain')
        self.assertEqual(caught.exception.stage, 'write_contents')
        self.assertEqual(calls, ['PUT'])


class ActionGitStateFake(GitStateFake):
    runtime_repo = 'runtime/sync'

    def __init__(self):
        super().__init__()
        self.attempts = {}
        self.actions_error = False

    def attempt(self, run_id, attempt, status):
        row = {'id': run_id, 'run_attempt': attempt, 'status': status,
               'path': '.github/workflows/get-notion-incremental.yml', 'workflow_id': 7,
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
                'GITHUB_WORKFLOW_REF': 'runtime/sync/.github/workflows/get-notion-incremental.yml@refs/heads/main',
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
                    if method == 'PUT' and route.endswith('/contents/' + STATE_FILE)]
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
                     ('path', '.github/workflows/get-direct-sync.yml'),
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
