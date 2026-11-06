import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_gitdata_staging as staging


class Local:
    def __init__(self, repo): self.repo = repo
    def run(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL)


class Staging(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name); self.local = Local(self.repo)
        def git(*args, env=None):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()
        git('init', '-q'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'initial').write_text('initial')
        git('add', 'initial'); git('commit', '-qm', 'initial')
        self.parent = git('rev-parse', 'HEAD')
        path = staging.r.GET + '/03 State/canonical-sync-receipt.json'
        self.files = {path: b'fixed receipt\n', staging.r.GET + '/03 Notes/note.md': b'fixed note\n'}
        env = dict(os.environ, GIT_INDEX_FILE=str(self.repo / 'candidate-index'),
            GIT_AUTHOR_NAME=staging.NAME, GIT_AUTHOR_EMAIL=staging.EMAIL,
            GIT_COMMITTER_NAME=staging.NAME, GIT_COMMITTER_EMAIL=staging.EMAIL)
        timestamp = git('show', '-s', '--format=%ct', self.parent)
        env.update(GIT_AUTHOR_DATE='@' + timestamp + ' +0000', GIT_COMMITTER_DATE='@' + timestamp + ' +0000')
        git('read-tree', self.parent, env=env)
        for file_path, data in self.files.items():
            blob = staging.r.oid(data)
            subprocess.check_output(['git', '-C', str(self.repo), 'hash-object', '-w', '--stdin'], input=data)
            git('update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + file_path, env=env)
        tree = git('write-tree', env=env)
        commit = git('commit-tree', tree, '-p', self.parent, '-m', staging.MESSAGE.rstrip('\n'), env=env)
        self.plan = {'parent_commit': self.parent, 'candidate_commit': commit, 'candidate_tree': tree,
                     'changes': [{'path': file_path} for file_path in self.files]}
        self.reader = type('Reader', (), {'local': self.local})()
        self.ref = None; self.calls = []; self.wrong = None; self.uncertain = False; self.moved = False
        self.ref_unconfirmed = False; self.move_after_blob = False
        self.anchor = self.parent; self.main = self.parent; self.push_calls = []; self.push_uncertain = False; self.push_unconfirmed = False; self.delete_uncertain = False
        self.remote = type('Remote', (), {})()
        self.remote.local = self.local
        def command(*args):
            self.push_calls.append(args)
            if args[-1].startswith(':'):
                if self.delete_uncertain: raise RuntimeError('unknown')
                self.ref = None
                return type('Result', (), {'returncode': 0})()
            if self.push_unconfirmed: raise RuntimeError('unknown')
            self.anchor = self.plan['candidate_commit']
            if self.push_uncertain: raise RuntimeError('unknown')
        self.local.command = command
        self.remote.metadata = lambda: {'full_name': staging.r.REPOSITORY, 'private': True}
        self.remote.ref = lambda name: ('f' * 40 if self.moved else self.main) if name == staging.r.MAIN_REF else staging.m.PROTECTED[name]
        self.remote.commit = lambda oid: {'tree': tree, 'parents': [self.parent]}
        self.remote.check_source_budget = lambda oid: {p: ('100644', 'blob', staging.r.oid(data), len(data)) for p, data in self.files.items()}
        self.readback = lambda oid, selected: dict(self.files)
        self.context = {'run_id': 123, 'run_attempt': 1}

    def call(self, method, path, payload=None, missing=False):
        self.calls.append((method, path))
        if method == 'GET':
            if path.endswith('/heads/codex/get-pending'):
                return {'ref': staging.pending.REF, 'object': {'type': 'commit', 'sha': self.anchor}}
            return self.ref
        if path == self.wrong: return {'sha': '0' * 40}
        if path == '/git/blobs':
            if self.move_after_blob: self.moved = True
            return {'sha': staging.r.oid(base64.b64decode(payload['content']))}
        if path == '/git/trees': return {'sha': self.plan['candidate_tree']}
        if path == '/git/commits': return {'sha': self.plan['candidate_commit']}
        if path == '/git/refs':
            self.ref = {'ref': payload['ref'], 'object': {'type': 'commit', 'sha': payload['sha']}}
            if self.ref_unconfirmed: self.ref = None
            if self.uncertain: raise staging.StageFailure('private error')
            return self.ref
        raise AssertionError(path)

    def run_stage(self):
        staging.stage(self, self.reader, self.remote, self.plan, self.files, self.context, self.readback, self.parent)

    def test_stages_only_exact_verified_candidate_without_main_write(self):
        self.run_stage()
        self.assertEqual([p for method, p in self.calls if method == 'POST'],
                         ['/git/blobs', '/git/blobs', '/git/trees', '/git/commits', '/git/refs'])
        self.assertEqual(self.remote.ref(staging.r.MAIN_REF), self.parent)
        self.assertEqual(len(self.push_calls), 1)
        self.assertIn('--force-with-lease=' + staging.pending.REF + ':' + self.parent, self.push_calls[0])

    def test_object_mismatch_and_existing_ref_stop_without_ref_write(self):
        for wrong in ('/git/blobs', '/git/trees', '/git/commits'):
            self.calls.clear(); self.wrong = wrong
            with self.subTest(wrong=wrong), self.assertRaises(staging.StageFailure): self.run_stage()
            self.assertNotIn(('POST', '/git/refs'), self.calls)
        self.wrong = None; self.ref = {'ref': 'existing'}; self.calls.clear()
        with self.assertRaises(staging.StageFailure): self.run_stage()
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))
        self.ref = {'ref': 'refs/heads/codex/get-stage-123',
                    'object': {'type': 'commit', 'sha': self.plan['candidate_commit']}}
        self.calls.clear()
        with self.assertRaises(staging.StageFailure): self.run_stage()
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    def test_uncertain_ref_only_reads_back_never_retries(self):
        self.uncertain = True
        self.run_stage()
        self.assertEqual(self.calls.count(('POST', '/git/refs')), 1)
        self.ref = None; self.calls.clear(); self.push_calls.clear(); self.anchor = self.parent
        self.uncertain = False; self.push_uncertain = True
        self.run_stage()
        self.assertEqual(len(self.push_calls), 1)
        self.push_uncertain = False
        self.ref = None; self.calls.clear(); self.ref_unconfirmed = True
        self.anchor = self.parent
        with self.assertRaises(staging.StageFailure): self.run_stage()
        self.assertEqual(self.calls.count(('POST', '/git/refs')), 1)

    def test_unconfirmed_fixed_ref_update_never_retries_or_reaches_main(self):
        self.push_unconfirmed = True
        with self.assertRaisesRegex(ValueError, 'pending_unresolved'): self.run_stage()
        self.assertEqual(len(self.push_calls), 1)
        self.assertEqual(self.anchor, self.parent)
        self.assertEqual(self.remote.ref(staging.r.MAIN_REF), self.parent)

    def test_cleanup_only_own_verified_run_ref_after_main_publication(self):
        self.run_stage(); self.main = self.plan['candidate_commit']; self.push_calls.clear()
        self.assertEqual(staging.cleanup_run_ref(self, self.remote, self.context, self.main), 'removed')
        ref = 'refs/heads/codex/get-stage-123'
        self.assertEqual(self.push_calls, [('push', '--porcelain', '--no-verify',
            '--force-with-lease=' + ref + ':' + self.main, staging.r.REMOTE_URL, ':' + ref)])
        self.assertEqual(self.anchor, self.main)

    def test_uncertain_cleanup_never_retries_and_keeps_fixed_ref(self):
        self.run_stage(); self.main = self.plan['candidate_commit']; self.push_calls.clear()
        self.delete_uncertain = True
        self.assertEqual(staging.cleanup_run_ref(self, self.remote, self.context, self.main), 'retained_or_unknown')
        self.assertEqual(len(self.push_calls), 1)
        self.assertIsNotNone(self.ref)
        self.assertEqual(self.anchor, self.main)

    def test_main_change_and_readback_mismatch_stop(self):
        self.moved = True
        with self.assertRaises(Exception): self.run_stage()
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))
        self.moved = False; self.readback = lambda *_: {}
        with self.assertRaises(staging.StageFailure): self.run_stage()

    def test_main_move_during_staging_stops_before_ref(self):
        self.move_after_blob = True
        with self.assertRaises(Exception): self.run_stage()
        self.assertNotIn(('POST', '/git/refs'), self.calls)

    def test_metadata_mismatch_and_main_move_after_ref_stop_before_return(self):
        self.remote.check_source_budget = lambda _: {next(iter(self.files)): ('100644', 'blob', '0' * 40, len(next(iter(self.files.values()))))}
        with self.assertRaises(staging.StageFailure): self.run_stage()
        self.assertEqual(self.calls.count(('POST', '/git/refs')), 1)
        self.ref = None; self.calls.clear()
        self.remote.check_source_budget = lambda _: {p: ('100644', 'blob', staging.r.oid(data), len(data)) for p, data in self.files.items()}
        def readback(*_):
            self.moved = True
            return dict(self.files)
        self.readback = readback
        with self.assertRaises(Exception): self.run_stage()

    def test_plan_scope_rejected_before_writes(self):
        self.plan['changes'] = [{'path': 'Wiki/unexpected.md'}]
        with self.assertRaises(staging.StageFailure): self.run_stage()
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    def test_http_permission_timeout_and_memory_fail_without_retry_or_leaks(self):
        for exc in (urllib.error.HTTPError('https://private', 403, 'secret', {}, None), TimeoutError('secret')):
            api = staging.API('token')
            api.opener.open = Mock(side_effect=exc)
            with self.assertRaises(staging.StageFailure) as caught:
                api.call('POST', '/git/blobs', {'content': 'fixed'})
            self.assertNotIn('secret', str(caught.exception))
            api.opener.open.assert_called_once()
        api = staging.API('token')
        with patch.object(staging.json, 'dumps', side_effect=MemoryError('secret')):
            with self.assertRaises(MemoryError): api.call('POST', '/git/blobs', {'content': 'fixed'})

    def test_blob_request_accepts_valid_object_larger_than_one_mebibyte(self):
        data = b'x' * (1024 * 1024 + 1)
        api = staging.API('token')
        response = type('Response', (), {'status': 201, '__enter__': lambda self: self,
            '__exit__': lambda self, *_: None, 'read': lambda self, _: b'{"sha":"fixed"}'})()
        api.opener.open = Mock(return_value=response)
        result = api.call('POST', '/git/blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
        self.assertEqual(result, {'sha': 'fixed'})
        self.assertGreater(len(api.opener.open.call_args.args[0].data), 1024 * 1024)


if __name__ == '__main__': unittest.main()
