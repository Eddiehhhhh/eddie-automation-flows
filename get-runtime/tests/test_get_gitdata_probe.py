import base64
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_gitdata_probe as probe


class GitDataProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()
        git('init', '-q')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'initial').write_text('initial')
        git('add', 'initial')
        git('commit', '-qm', 'initial')
        self.parent = git('rev-parse', 'HEAD')
        self.values = {}
        self.calls = []
        self.fail_at = None
        self.wrong_at = None
        self.drift = False
        self.refs = {}
        self.push_calls = []
        self.push_fails = False
        self.commit_payload = None
        original = probe.git
        def capture(repo, *args, **kwargs):
            if args[:3] == ('remote', 'get-url', 'origin'):
                return 'https://github.com/' + probe.REPO + '.git'
            if args[0] == 'push':
                self.push_calls.append(args)
                if self.push_fails:
                    raise probe.ProbeFailure('git_push', 'exit_128')
                ref = args[-1].split(':', 1)[1]
                self.refs[ref]['object']['sha'] = self.values['commit-tree']
                return ''
            result = original(repo, *args, **kwargs)
            if args[0] in ('hash-object', 'write-tree', 'commit-tree'):
                self.values[args[0]] = result
            return result
        self.capture = patch.object(probe, 'git', side_effect=capture)
        self.capture.start()
        self.addCleanup(self.capture.stop)

    def call(self, method, path, payload=None, missing=False):
        self.calls.append((method, path))
        if path == self.fail_at:
            raise ValueError('uncertain')
        if path == self.wrong_at:
            return {'sha': '0' * 40}
        if path == '':
            return {'private': True, 'full_name': probe.REPO}
        if path == '/git/ref/heads/main':
            sha = 'f' * 40 if self.drift and self.values.get('commit-tree') else self.parent
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': sha}}
        if path.startswith('/git/ref/heads/codex/'):
            return self.refs.get('refs/' + path.removeprefix('/git/ref/'))
        if path == '/git/blobs':
            return {'sha': self.values['hash-object']}
        if path == '/git/trees':
            return {'sha': self.values['write-tree']}
        if path == '/git/commits':
            self.commit_payload = payload
            return {'sha': self.values['commit-tree']}
        if path == '/git/refs':
            self.refs[payload['ref']] = {'ref': payload['ref'], 'object': {'type': 'commit', 'sha': payload['sha']}}
            return self.refs[payload['ref']]
        if path.startswith('/git/commits/'):
            return {'sha': self.values['commit-tree'], 'tree': {'sha': self.values['write-tree']},
                    'parents': [{'sha': self.parent}]}
        if path.startswith('/contents/'):
            return {'path': path.split('/contents/', 1)[1].split('?')[0],
                    'sha': self.values['hash-object'], 'type': 'file'}
        if path.startswith('/git/blobs/'):
            return {'sha': self.values['hash-object'], 'encoding': 'base64',
                    'content': base64.b64encode(probe.CONTENT).decode()}
        raise AssertionError(path)

    def test_local_objects_match_and_one_leased_push_updates_only_probe_ref(self):
        result = probe.run(self, self.repo, '123', '1')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual([path for method, path in self.calls if method == 'POST'],
                         ['/git/blobs', '/git/trees', '/git/commits', '/git/refs', '/git/refs'])
        self.assertEqual(result['git_push'], 'verified')
        push_ref = 'refs/heads/codex/get-gitpush-probe-123-1'
        self.assertEqual(self.push_calls, [('push', '--porcelain', '--no-verify',
            '--force-with-lease=' + push_ref + ':' + self.parent, 'origin',
            self.values['commit-tree'] + ':' + push_ref)])
        self.assertEqual(self.refs[push_ref]['object']['sha'], self.values['commit-tree'])
        self.assertEqual(probe.git(self.repo, 'rev-parse', 'HEAD'), self.parent)
        raw = subprocess.check_output(['git', '-C', str(self.repo), 'cat-file', 'commit', self.values['commit-tree']])
        self.assertEqual(raw.partition(b'\n\n')[2], (probe.MESSAGE + '\n').encode())
        self.assertEqual(self.commit_payload['message'].encode(), raw.partition(b'\n\n')[2])

    def test_uncertain_write_is_not_retried_and_never_creates_ref(self):
        self.fail_at = '/git/trees'
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            probe.run(self, self.repo, '123', '1')
        self.assertEqual(self.calls.count(('POST', '/git/trees')), 1)
        self.assertNotIn(('POST', '/git/refs'), self.calls)

    def test_main_movement_stops_before_staging_ref(self):
        self.drift = True
        with self.assertRaisesRegex(ValueError, 'parent_changed'):
            probe.run(self, self.repo, '123', '1')
        self.assertNotIn(('POST', '/git/refs'), self.calls)

    def test_uncertain_push_is_not_retried_or_sent_to_main(self):
        self.push_fails = True
        with self.assertRaises(probe.ProbeFailure):
            probe.run(self, self.repo, '123', '1')
        self.assertEqual(len(self.push_calls), 1)
        self.assertNotIn('refs/heads/main', self.push_calls[0][-1])

    def test_existing_push_probe_ref_stops_before_any_write(self):
        ref = 'refs/heads/codex/get-gitpush-probe-123-1'
        self.refs[ref] = {'ref': ref, 'object': {'type': 'commit', 'sha': self.parent}}
        with self.assertRaisesRegex(ValueError, 'ref_exists'):
            probe.run(self, self.repo, '123', '1')
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))
        self.assertEqual(self.push_calls, [])

    def test_object_id_mismatch_stops_before_staging(self):
        for path in ('/git/blobs', '/git/trees', '/git/commits'):
            with self.subTest(path=path):
                self.calls.clear()
                self.wrong_at = path
                with self.assertRaisesRegex(ValueError, 'mismatch'):
                    probe.run(self, self.repo, '123', '1')
                self.assertNotIn(('POST', '/git/refs'), self.calls)

    def test_diagnostics_are_fixed_and_never_echo_private_errors(self):
        self.assertEqual(probe.diagnostic(ValueError('blob_mismatch'))['reason'], 'blob_mismatch')
        self.assertEqual(probe.diagnostic(ValueError('private note'))['reason'], 'unexpected')
        self.assertNotIn('private note', str(probe.diagnostic(probe.ProbeFailure('private note', 'private note'))))
        with patch.object(probe.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(128, ['private note'])):
            with self.assertRaises(probe.ProbeFailure) as caught:
                probe.git(self.repo, 'read-tree', self.parent)
        self.assertEqual(probe.diagnostic(caught.exception),
                         {'status': 'failed', 'stage': 'git_read_tree', 'reason': 'exit_128'})

    def test_http_write_failure_reports_fixed_stage_status_and_is_not_retried(self):
        api = probe.API('test token')
        api.opener.open = Mock(side_effect=urllib.error.HTTPError('https://private', 403, 'private note', {}, None))
        with self.assertRaises(probe.ProbeFailure) as caught:
            api.call('POST', '/git/trees', {'tree': []})
        self.assertEqual(probe.diagnostic(caught.exception),
                         {'status': 'failed', 'stage': 'tree_create', 'reason': 'http_error', 'http_status': 403})
        api.opener.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
