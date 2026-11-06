import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import get_push_probe as probe


class ProbeTest(unittest.TestCase):
    def test_branch_name_rejects_untrusted_input(self):
        self.assertEqual(probe.branch_for('123'), 'refs/heads/codex/get-push-probe-123')
        for value in ('', '0', '1/evil', '../main', '1' * 21):
            with self.assertRaises(ValueError):
                probe.branch_for(value)

    def test_actual_push_creates_only_new_branch_and_refuses_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bare = root / 'remote.git'; seed = root / 'seed'; clone = root / 'clone'
            def git(*args, cwd=None, data=None):
                return subprocess.run(['git', *args], cwd=cwd, input=data, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, check=True).stdout
            git('init', '--bare', str(bare)); git('init', str(seed))
            git('config', 'user.name', 'Test', cwd=seed); git('config', 'user.email', 'test@example.invalid', cwd=seed)
            (seed / 'existing').write_text('original'); git('add', 'existing', cwd=seed)
            git('commit', '-m', 'initial', cwd=seed); git('branch', '-M', 'main', cwd=seed)
            git('push', str(bare), 'HEAD:refs/heads/main', cwd=seed)
            git('clone', '--depth=1', '--branch', 'main', 'file://' + str(bare), str(clone))
            parent = git('rev-parse', 'HEAD', cwd=clone).decode().strip()
            class Local:
                vault = clone
                env = os.environ.copy()
                def run(self, *args, data=None, env=None):
                    return subprocess.run(['git', '-C', str(clone), *args], input=data, env=env or self.env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
                def command(self, *args):
                    return subprocess.run(['git', '-C', str(clone), *args], env=self.env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            class Remote:
                def request(self, suffix, missing=False):
                    ref = 'refs/' + suffix.removeprefix('/git/ref/')
                    r = subprocess.run(['git', '--git-dir', str(bare), 'show-ref', '--verify', '--hash', ref],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    return None if r.returncode else {'ref': ref, 'object': {'type': 'commit', 'sha': r.stdout.decode().strip()}}
                def commit(self, commit):
                    tree, parents = git('--git-dir', str(bare), 'show', '-s', '--format=%T%n%P', commit).decode().strip().splitlines()
                    return {'tree': tree, 'parents': parents.split()}
                def blob(self, blob, size):
                    return git('--git-dir', str(bare), 'cat-file', '-p', blob)
            remote = Remote()
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'PRIVATE_VAULT_TOKEN': 'dummy'}), \
                    patch.object(probe.manual, 'Remote', return_value=remote), \
                    patch.object(probe.manual, 'guard', return_value=parent), \
                    patch.object(probe.retention, 'readback_checkout', return_value=Local()), \
                    patch.object(probe.retention, 'REMOTE_URL', str(bare)), \
                    patch.object(probe.runtime, 'fetch_commit'), contextlib.redirect_stdout(io.StringIO()) as output:
                probe.main()
                self.assertIn('probe=verified', output.getvalue())
                self.assertEqual(git('--git-dir', str(bare), 'rev-parse', 'refs/heads/main').decode().strip(), parent)
                self.assertIsNotNone(probe.branch_ref(remote, probe.branch_for('123')))
                with self.assertRaisesRegex(ValueError, 'probe_branch_exists'):
                    probe.main()
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '124'}), \
                        patch.object(Local, 'command', side_effect=RuntimeError('transport')) as push, \
                        patch.object(probe.time, 'sleep'):
                    with self.assertRaisesRegex(ValueError, 'probe_unconfirmed'):
                        probe.main()
                    self.assertEqual(push.call_count, 1)
                    self.assertIsNone(probe.branch_ref(remote, probe.branch_for('124')))


if __name__ == '__main__':
    unittest.main()
