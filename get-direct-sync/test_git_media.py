import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core import Hold
from git_media import publish_asset


class GitMediaTests(unittest.TestCase):
    def test_publish_only_expected_blob_without_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            origin = Path(directory) / 'origin.git'
            run = subprocess.run

            def git(*args):
                return run(['git', *args], capture_output=True, check=True).stdout.decode().strip()

            git('init', '--bare', str(origin))
            git('-C', str(origin), 'config', 'uploadpack.allowFilter', 'true')
            tree = git('-C', str(origin), 'mktree')
            parent = git('-C', str(origin), '-c', 'user.name=Synthetic fixture',
                         '-c', 'user.email=fixture@example.test', 'commit-tree', tree, '-m', 'parent')
            git('-C', str(origin), 'update-ref', 'refs/heads/main', parent)

            def local_route(argv, **kwargs):
                self.assertFalse(any(arg.startswith('--force') or arg.startswith('+') for arg in argv))
                url = 'https://github.com/owner/private.git'
                return run([origin.as_uri() if arg == url else arg for arg in argv], **kwargs)

            class Metadata:
                private = True
                raced = False
                calls = 0

                def request(self, method, path):
                    if path == '/repos/owner/private':
                        return {'private': self.private}
                    self.calls += 1
                    observed = git('-C', str(origin), 'rev-parse', 'refs/heads/main')
                    if self.raced and self.calls > 1:
                        observed = '0' * 40
                    return {'object': {'type': 'commit', 'sha': observed}}

            api = Metadata()
            data = b'synthetic binary asset'
            target = 'get-direct/test/assets/' + hashlib.sha256(data).hexdigest() + '.bin'
            oid = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
            with patch('git_media.subprocess.run', side_effect=local_route):
                api.raced = True
                with self.assertRaisesRegex(Hold, 'media_git_ref_changed'):
                    publish_asset(api, 'synthetic', 'owner/private', 'main', target, data, oid)
                api.raced = False
                self.assertEqual(git('-C', str(origin), 'rev-parse', 'refs/heads/main'), parent)
                commit = publish_asset(api, 'synthetic', 'owner/private', 'main', target, data, oid)
                self.assertEqual(git('-C', str(origin), 'rev-parse', commit + '^'), parent)
                self.assertEqual(git('-C', str(origin), 'show', commit + ':' + target).encode(), data)
                self.assertEqual(publish_asset(api, 'synthetic', 'owner/private', 'main', target, data, oid), commit)
                with self.assertRaisesRegex(Hold, 'media_github_content_conflict'):
                    publish_asset(api, 'synthetic', 'owner/private', 'main', target, data, '0' * 40)
                api.private = False
                with self.assertRaisesRegex(Hold, 'private_repository_required'):
                    publish_asset(api, 'synthetic', 'owner/private', 'main', target, data, oid)
