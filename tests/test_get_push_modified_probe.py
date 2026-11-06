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

import get_push_modified_probe as probe


class ModifiedBlobProbeTest(unittest.TestCase):
    def test_fixed_words_never_echo_private_output(self):
        local = types.SimpleNamespace(vault='/unused', env={}, timeout=120, child_limits=None)
        result = types.SimpleNamespace(returncode=1, stdout=b'remote rejected private-note',
                                       stderr=b'fatal fetch pack https://private.invalid/path')
        with patch.object(probe.subprocess, 'run', return_value=result), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(probe.run_git(local, 'update_branch', 'push'))
        value = json.loads(output.getvalue())
        self.assertEqual(value['stdout_words'], ['rejected', 'remote'])
        self.assertEqual(value['stderr_words'], ['fatal', 'fetch', 'pack'])
        self.assertNotIn('private', output.getvalue())
        self.assertNotIn('https', output.getvalue())

    def test_existing_old_blob_stops_before_push(self):
        local = types.SimpleNamespace(run=lambda *args, **kwargs: b'abc blob\n')
        with self.assertRaisesRegex(ValueError, 'old_blobs_not_all_missing'):
            probe.require_missing(local, ['abc'])

    def test_hydration_rejects_wrong_blob_before_import(self):
        paths = [probe.PREFIX + 'note-%02d.md' % index for index in range(20)]
        oids = [probe.retention.oid(probe.synthetic(index, probe.NOTE_BYTES)) for index in range(20)]
        values = {path: probe.synthetic(index, probe.NOTE_BYTES) for index, path in enumerate(paths)}
        values[paths[0]] = b'incorrect'
        local = types.SimpleNamespace(run=Mock())
        with patch.object(probe.manual, 'fetch_selected', return_value=values), \
                self.assertRaisesRegex(ValueError, 'old_blob_readback_mismatch'):
            probe.hydrate_old(local, object(), 'dummy', Path('/unused'), paths, oids)
        local.run.assert_not_called()

    def test_hydration_imports_exact_twenty_blobs_into_same_store(self):
        paths = [probe.PREFIX + 'note-%02d.md' % index for index in range(20)]
        values = {path: probe.synthetic(index, probe.NOTE_BYTES) for index, path in enumerate(paths)}
        oids = [probe.retention.oid(values[path]) for path in paths]
        class Local:
            def __init__(self):
                self.objects = {}
            def run(self, *args, data=None):
                if args[0] == 'hash-object':
                    oid = probe.retention.oid(data); self.objects[oid] = data
                    return (oid + '\n').encode()
                if args[0] == 'cat-file':
                    return ''.join(oid + ' blob ' + str(len(self.objects[oid])) + '\n'
                                   for oid in data.decode().splitlines()).encode()
                raise AssertionError('unexpected command')
        local = Local()
        with patch.object(probe.manual, 'fetch_selected', return_value=values) as fetch:
            probe.hydrate_old(local, object(), 'dummy', Path('/unused'), paths, oids)
        self.assertEqual(set(local.objects), set(oids))
        metadata = fetch.call_args.args[1]
        self.assertEqual(metadata, {path: ('100644', 'blob', oid, probe.NOTE_BYTES)
                                    for path, oid in zip(paths, oids)})

    def test_actual_shallow_modified_blob_push_only_changes_probe_branch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bare = root / 'remote.git'; seed = root / 'seed'; clone = root / 'clone'
            def git(*args, cwd=None, data=None, env=None):
                return subprocess.run(['git', *args], cwd=cwd, input=data, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
            git('init', '--bare', str(bare)); git('--git-dir', str(bare), 'config', 'uploadpack.allowFilter', 'true')
            git('--git-dir', str(bare), 'config', 'uploadpack.allowAnySHA1InWant', 'true')
            git('init', str(seed)); git('config', 'user.name', 'Test', cwd=seed)
            git('config', 'user.email', 'test@example.invalid', cwd=seed)
            (seed / 'keep').write_text('original'); git('add', 'keep', cwd=seed)
            git('commit', '-m', 'main', cwd=seed); git('branch', '-M', 'main', cwd=seed)
            git('push', str(bare), 'HEAD:refs/heads/main', cwd=seed)
            main = git('rev-parse', 'HEAD', cwd=seed).decode().strip()
            target = seed / probe.PREFIX; target.mkdir(parents=True)
            for index in range(20):
                (target / ('note-%02d.md' % index)).write_bytes(probe.synthetic(index, probe.NOTE_BYTES))
            git('add', probe.PREFIX, cwd=seed); git('commit', '-m', 'source probe', cwd=seed)
            source = git('rev-parse', 'HEAD', cwd=seed).decode().strip()
            git('push', str(bare), 'HEAD:' + probe.SOURCE_REF, cwd=seed)
            git('init', str(clone)); git('remote', 'add', 'origin', 'file://' + str(bare), cwd=clone)
            git('config', 'remote.origin.promisor', 'true', cwd=clone)
            git('config', 'remote.origin.partialclonefilter', 'blob:none', cwd=clone)
            environment = dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
            class Local:
                vault = clone
                env = environment
                timeout = 120
                child_limits = None
                def run(self, *args, data=None, env=None):
                    return subprocess.run(['git', '-C', str(clone), *args], input=data, env=env or self.env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
            class Remote:
                def request(self, suffix, missing=False):
                    ref = 'refs/' + suffix.removeprefix('/git/ref/')
                    result = subprocess.run(['git', '--git-dir', str(bare), 'show-ref', '--verify', '--hash', ref],
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    return None if result.returncode else {'ref': ref, 'object': {'type': 'commit', 'sha': result.stdout.decode().strip()}}
                def commit(self, commit):
                    tree, parents = git('--git-dir', str(bare), 'show', '-s', '--format=%T%n%P', commit).decode().strip().splitlines()
                    return {'tree': tree, 'parents': parents.split()}
                def blob(self, oid, size):
                    return git('--git-dir', str(bare), 'cat-file', '-p', oid)
            remote = Remote()
            def fetch(local, commit, depth):
                local.run('fetch', '--depth=1', '--filter=blob:none', '--no-tags', 'origin', commit)
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'PRIVATE_VAULT_TOKEN': 'dummy'}), \
                    patch.object(probe, 'SOURCE_COMMIT', source), \
                    patch.object(probe.manual, 'Remote', return_value=remote), \
                    patch.object(probe.manual, 'guard', return_value=main), \
                    patch.object(probe.retention, 'readback_checkout', return_value=Local()), \
                    patch.object(probe.retention, 'REMOTE_URL', 'file://' + str(bare)), \
                    patch.object(probe.runtime, 'fetch_commit', side_effect=fetch), \
                    patch.object(probe, 'run_git', wraps=probe.run_git) as run, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                failed_without_old_blobs = False
                try:
                    probe.main()
                except ValueError as error:
                    if str(error) != 'probe_unconfirmed':
                        raise
                    failed_without_old_blobs = True
                self.assertIn('old_blobs_missing=20', output.getvalue())
                self.assertIn('"old_blobs_missing_before_push":20', output.getvalue())
                self.assertIn('"changed_paths":20', output.getvalue())
                self.assertEqual([call.args[1] for call in run.call_args_list], ['seed_branch', 'merge_base', 'update_branch'])
                self.assertEqual(git('--git-dir', str(bare), 'rev-parse', 'refs/heads/main').decode().strip(), main)
                self.assertEqual(git('--git-dir', str(bare), 'rev-parse', probe.SOURCE_REF).decode().strip(), source)
                with self.assertRaisesRegex(ValueError, 'probe_ref_changed'):
                    probe.main()
                if failed_without_old_blobs:
                    self.assertIn('probe_ref=parent', output.getvalue())
                    self.assertIn('source_branch=unchanged', output.getvalue())
                    self.assertIn('main=unchanged', output.getvalue())
                    run.reset_mock()
                    def fetch_selected(_remote, metadata, _token, _directory):
                        self.assertEqual(len(metadata), 20)
                        return {path: git('--git-dir', str(bare), 'cat-file', '-p', row[2])
                                for path, row in metadata.items()}
                    with patch.dict(os.environ, {'GITHUB_RUN_ID': '124'}), \
                            patch.object(probe.manual, 'fetch_selected', side_effect=fetch_selected) as selected:
                        probe.main(hydrate=True)
                    self.assertEqual(selected.call_count, 1)
                    self.assertIn('old_blobs_hydrated=20', output.getvalue())
                    self.assertIn('"old_blobs_present_before_push":20', output.getvalue())
                    self.assertIn('probe=verified', output.getvalue())
                    self.assertEqual([call.args[1] for call in run.call_args_list], ['seed_branch', 'merge_base', 'update_branch'])
                    self.assertEqual(git('--git-dir', str(bare), 'rev-parse', 'refs/heads/main').decode().strip(), main)
                    print('A/B: missing old blobs failed; hydrated old blobs succeeded', file=__import__('sys').stderr)
                else:
                    self.assertIn('probe=verified', output.getvalue())


if __name__ == '__main__':
    unittest.main()
