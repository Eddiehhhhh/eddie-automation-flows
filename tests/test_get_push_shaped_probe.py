import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import get_push_shaped_probe as probe


class ShapedProbeTest(unittest.TestCase):
    def test_stage_report_classifies_both_streams_without_exposing_them(self):
        local = types.SimpleNamespace(vault='/unused', env={}, timeout=120, child_limits=None)
        result = types.SimpleNamespace(returncode=1, stdout=b'! [remote rejected] private-data',
                                       stderr=b'error: failed to push private-data')
        with patch.object(probe.subprocess, 'run', return_value=result), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(probe.run_git(local, 'update_branch', 'push'))
        row = json.loads(output.getvalue())
        self.assertEqual(row['exit_code'], 1)
        self.assertEqual(row['stdout_category'], 'size_or_remote_rejection')
        self.assertNotIn('private-data', output.getvalue())

    def test_timeout_reports_fixed_category_without_output(self):
        local = types.SimpleNamespace(vault='/unused', env={}, timeout=120, child_limits=None)
        with patch.object(probe.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private', 1, output=b'private-data')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(probe.run_git(local, 'update_branch', 'push'))
        self.assertEqual(json.loads(output.getvalue())['exception'], 'timeout')
        self.assertNotIn('private', output.getvalue())

    def test_real_multifile_push_updates_only_unique_probe_branch(self):
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
                timeout = 120
                child_limits = None
                def run(self, *args, data=None, env=None):
                    return subprocess.run(['git', '-C', str(clone), *args], input=data, env=env or self.env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
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
                    patch.object(probe.runtime, 'fetch_commit'), \
                    patch.object(probe, 'run_git', wraps=probe.run_git) as run, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                probe.main()
                self.assertIn('probe=verified', output.getvalue())
                self.assertIn('main=unchanged', output.getvalue())
                self.assertEqual([call.args[1] for call in run.call_args_list], ['seed_branch', 'merge_base', 'update_branch'])
                self.assertEqual(git('--git-dir', str(bare), 'rev-parse', 'refs/heads/main').decode().strip(), parent)
                with self.assertRaisesRegex(ValueError, 'probe_branch_exists'):
                    probe.main()
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '124'}), \
                        patch.object(probe, 'run_git', return_value=False) as failed:
                    with self.assertRaisesRegex(ValueError, 'probe_seed_unconfirmed'):
                        probe.main()
                    self.assertEqual(failed.call_count, 1)
                    self.assertIsNone(probe.branch_ref(remote, probe.branch_for('124')))
                original = probe.run_git
                def stop_update(local, stage, *args):
                    return False if stage == 'update_branch' else original(local, stage, *args)
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '125'}), \
                        patch.object(probe, 'run_git', side_effect=stop_update) as failed:
                    with self.assertRaisesRegex(ValueError, 'probe_unconfirmed'):
                        probe.main()
                    self.assertEqual([call.args[1] for call in failed.call_args_list],
                                     ['seed_branch', 'merge_base', 'update_branch'])
                    self.assertEqual(probe.branch_ref(remote, probe.branch_for('125')), parent)
                    self.assertEqual(git('--git-dir', str(bare), 'rev-parse', 'refs/heads/main').decode().strip(), parent)


if __name__ == '__main__':
    unittest.main()
