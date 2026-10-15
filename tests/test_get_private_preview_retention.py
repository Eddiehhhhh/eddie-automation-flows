import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_preview_retention as r
from get_mirror_incremental_replay import prepare_private_preview
from get_mirror_render import CompatibleWriter
from source_sync_assets import AssetStore


class FakeRemote:
    def __init__(self, local, source):
        self.local = local
        self.refs = {r.MAIN_REF: source}
        self.writes = []
        self.reads = []
        self.info = {'full_name': r.REPOSITORY, 'private': True}
        self.uncertain = False
        self.reject = False
        self.corrupt = False
        self.race = None

    def metadata(self):
        return self.info

    def ref(self, ref):
        return self.refs.get(ref)

    def commit(self, sha):
        raw = self.local.run('cat-file', '-p', sha).decode().split('\n\n', 1)[0].splitlines()
        return {'tree': next(x.split()[1] for x in raw if x.startswith('tree ')),
                'parents': [x.split()[1] for x in raw if x.startswith('parent ')]}

    def blob(self, sha, expected_size):
        self.reads.append(sha)
        return b'wrong private bytes' if self.corrupt else self.local.blob(sha)

    def create_only(self, ref, commit):
        self.writes.append((ref, commit))
        if self.race is not None:
            self.refs[ref] = self.race
        if ref in self.refs or self.reject:
            raise RuntimeError('private token and path must not escape')
        self.refs[ref] = commit
        if self.uncertain:
            raise TimeoutError('private upstream details')


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.vault = self.work / 'vault'
        self.vault.mkdir()
        self.get = self.vault / r.GET
        (self.get / '03 State').mkdir(parents=True)
        (self.get / '03 State/manifest.json').write_text('{"notes":[]}')
        (self.vault / 'Wiki').mkdir()
        (self.vault / 'Wiki/unrelated.md').write_text('untouched non-Get content')
        (self.vault / '.gitignore').write_text('ignored-anywhere\n')
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Offline Fixture')
        self.git('config', 'user.email', 'offline@example.invalid')
        self.git('add', '.')
        self.git('commit', '-qm', 'seed')
        self.git('remote', 'add', 'origin', r.REMOTE_URL)
        self.source = self.git('rev-parse', 'HEAD').strip()
        self.local = r.LocalGit(self.vault)
        self.remote = FakeRemote(self.local, self.source)
        self.output = self.work / 'preview'
        self.note = {'id': '123', 'updated_at': '2026-10-13 08:00:00', 'created_at': '2026-10-13 08:00:00',
                     'title': 'Readable', 'content': '## Heading\n\n- First', 'note_type': 'plain_text'}

    def git(self, *args, root=None):
        return subprocess.check_output(['git', '-C', str(root or self.vault), *args], stderr=subprocess.DEVNULL, text=True)

    def preview(self):
        def factory(root):
            def no_fetch(*args, **kwargs):
                raise AssertionError('offline asset fetch forbidden')
            return CompatibleWriter(root, AssetStore(root, fetch=no_fetch))
        prepare_private_preview(self.vault, self.output, self.source,
                                lambda: ([deepcopy(self.note)], True), lambda _: deepcopy(self.note), factory)

    def backup(self):
        return r.prepare_backup(self.vault, self.source, remote=self.remote)

    def retain(self):
        return r.retain_private_preview(self.vault, self.output, self.source, remote=self.remote)

    def test_complete_backup_retention_and_exact_tree(self):
        self.preview()
        original_index = (self.vault / '.git/index').read_bytes()
        self.assertTrue(self.backup()['backup_verified'])
        result = self.retain()
        self.assertTrue(result['preview_retained'])
        self.assertEqual(result['source_status'], 'complete')
        self.assertEqual(self.remote.ref(r.MAIN_REF), self.source)
        self.assertEqual([x[0] for x in self.remote.writes], [r.BACKUP_REF, r.PREVIEW_REF])
        preview = self.remote.ref(r.PREVIEW_REF)
        before, after = self.local.tree(self.source), self.local.tree(preview)
        plan = json.loads((self.output / 'plan.json').read_text())
        changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
        self.assertEqual(changed, {x['path'] for x in plan['changes']} | {r.PLAN_PATH})
        self.assertEqual(before['Wiki/unrelated.md'], after['Wiki/unrelated.md'])
        self.assertEqual(set(self.remote.reads), {after[p][2] for p in changed})
        self.assertEqual((self.vault / '.git/index').read_bytes(), original_index)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_same_backup_and_same_preview_resume_without_push(self):
        self.preview(); self.backup(); self.retain()
        writes = list(self.remote.writes)
        self.assertEqual(self.backup()['status'], 'backup_existing_verified')
        self.assertEqual(self.retain()['status'], 'preview_existing_verified')
        self.assertEqual(self.remote.writes, writes)

    def test_existing_different_preview_is_never_overwritten(self):
        self.preview(); self.backup()
        self.remote.refs[r.PREVIEW_REF] = self.source
        with self.assertRaisesRegex(r.RetentionFailure, 'existing_preview_conflict'):
            self.retain()
        self.assertEqual(len(self.remote.writes), 1)

    def test_existing_backup_different_sha_stops(self):
        self.remote.refs[r.BACKUP_REF] = 'a' * 40
        with self.assertRaisesRegex(r.RetentionFailure, 'backup_ref_conflict'):
            self.backup()
        self.assertEqual(self.remote.writes, [])

    def test_uncertain_success_is_read_back_without_retry(self):
        self.preview(); self.remote.uncertain = True
        self.assertIn('uncertain', self.backup()['status'])
        self.assertIn('uncertain', self.retain()['status'])
        self.assertEqual(len(self.remote.writes), 2)

    def test_uncertain_failure_stops_without_retry(self):
        self.remote.reject = True
        with self.assertRaisesRegex(r.RetentionFailure, 'ref_creation_unconfirmed'):
            self.backup()
        self.assertEqual(len(self.remote.writes), 1)

    def test_create_only_ref_race_is_not_overwritten(self):
        self.remote.race = 'b' * 40
        with self.assertRaisesRegex(r.RetentionFailure, 'ref_creation_unconfirmed'):
            self.backup()
        self.assertEqual(self.remote.ref(r.BACKUP_REF), 'b' * 40)
        self.assertEqual(len(self.remote.writes), 1)

    def test_main_drift_retains_fixed_source_without_rebase(self):
        self.preview(); self.backup()
        self.remote.refs[r.MAIN_REF] = 'c' * 40
        result = self.retain()
        self.assertTrue(result['main_advanced'])
        self.assertEqual(self.remote.ref(r.MAIN_REF), 'c' * 40)
        self.assertEqual(self.remote.commit(self.remote.ref(r.PREVIEW_REF))['parents'], [self.source])
        self.assertEqual(self.remote.ref(r.BACKUP_REF), self.source)
        self.assertFalse(result['apply_ready'])

    def test_prepare_main_drift_still_blocks(self):
        self.remote.refs[r.MAIN_REF] = 'c' * 40
        with self.assertRaisesRegex(r.RetentionFailure, 'remote_main_changed'): self.backup()
        self.assertEqual(self.remote.writes, [])

    def test_plan_boolean_version_and_size_rejected(self):
        self.preview(); self.backup()
        path = self.output / 'plan.json'; original = json.loads(path.read_text())
        plan = deepcopy(original); plan['version'] = True; path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(r.RetentionFailure, 'invalid_plan'): self.retain()
        plan = deepcopy(original); plan['changes'][0]['old_bytes'] = False; path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(r.RetentionFailure, 'invalid_plan_row'): self.retain()

    def test_real_cli_prepare_retain_and_summary_with_offline_remote(self):
        self.preview()
        for operation in ('prepare', 'retain'):
            argv = [operation, '--vault', str(self.vault), '--source-commit', self.source]
            if operation == 'retain': argv += ['--preview-output', str(self.output)]
            output = io.StringIO()
            with patch.object(r, 'remote_context', return_value=(self.local, self.remote)), contextlib.redirect_stdout(output):
                self.assertEqual(r.main(argv), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value['status'], 'backup_created_verified' if operation == 'prepare' else 'preview_created_verified')
            report = self.work / (operation + '.json'); report.write_text(output.getvalue())
            result = subprocess.run([sys.executable, r.__file__, '--publish-report', str(report)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0); self.assertEqual(result.stderr, '')
            self.assertEqual(json.loads(result.stdout), value)

    def test_wrong_visibility_or_repository_stops_before_write(self):
        for info in ({'full_name': r.REPOSITORY, 'private': False}, {'full_name': 'other/repo', 'private': True}):
            self.remote.info = info
            with self.assertRaisesRegex(r.RetentionFailure, 'repository_not_private_target'):
                self.backup()
        self.assertEqual(self.remote.writes, [])

    def test_backup_required_before_retention(self):
        self.preview()
        with self.assertRaisesRegex(r.RetentionFailure, 'backup_not_verified'):
            self.retain()
        self.assertEqual(self.remote.writes, [])

    def test_partial_preview_keeps_partial_status(self):
        self.note['note_type'] = 'recorder_audio'
        self.preview(); self.backup()
        result = self.retain()
        self.assertEqual(result['source_status'], 'partial')
        self.assertEqual(result['pending'], 1)
        self.assertFalse(result['published'])
        self.assertFalse(result['apply_ready'])

    def test_deleted_unplanned_and_symlink_candidate_fail_closed(self):
        self.preview(); self.backup()
        candidate = self.output / 'candidate'
        target = candidate / '03 State/manifest.json'
        old = target.read_bytes(); target.unlink()
        with self.assertRaises(r.RetentionFailure): self.retain()
        target.write_bytes(old)
        extra = candidate / 'unplanned.md'; extra.write_text('private unplanned body')
        with self.assertRaisesRegex(r.RetentionFailure, 'unplanned_candidate_change'): self.retain()
        extra.unlink(); extra.symlink_to(self.vault / 'Wiki/unrelated.md')
        with self.assertRaisesRegex(r.RetentionFailure, 'symlink_or_alias'): self.retain()
        self.assertEqual(len(self.remote.writes), 1)

    def test_non_get_plan_and_changed_candidate_hash_rejected(self):
        self.preview(); self.backup()
        path = self.output / 'plan.json'; plan = json.loads(path.read_text())
        original = deepcopy(plan)
        plan['changes'][0]['path'] = 'Wiki/unrelated.md'; path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(r.RetentionFailure, 'outside_get_scope'): self.retain()
        path.write_text(json.dumps(original))
        file = self.output / 'candidate' / Path(original['changes'][0]['path']).relative_to(r.GET)
        file.write_text('different bytes')
        with self.assertRaisesRegex(r.RetentionFailure, 'plan_new_bytes_mismatch'): self.retain()
        self.assertEqual(len(self.remote.writes), 1)

    def test_ignored_reserved_plan_blocks(self):
        (self.vault / '.gitignore').write_text(r.PLAN_PATH + '\n')
        self.git('add', '.gitignore'); self.git('commit', '-qm', 'ignore reserved fixture')
        self.source = self.git('rev-parse', 'HEAD').strip(); self.remote.refs[r.MAIN_REF] = self.source
        self.preview(); self.backup()
        with self.assertRaisesRegex(r.RetentionFailure, 'ignored_preview_path'): self.retain()
        self.assertEqual(len(self.remote.writes), 1)

    def test_remote_blob_readback_failure_is_not_success(self):
        self.preview(); self.backup(); self.remote.corrupt = True
        with self.assertRaisesRegex(r.RetentionFailure, 'remote_blob_readback_failed'): self.retain()
        self.assertEqual(len(self.remote.writes), 2)

    def test_unsafe_git_config_grafts_replacements_and_wrong_origin(self):
        self.git('config', 'url.https://elsewhere.invalid/.insteadOf', 'https://github.com/')
        with self.assertRaisesRegex(r.RetentionFailure, 'unsafe_git_config'): r.LocalGit(self.vault)
        self.git('config', '--remove-section', 'url.https://elsewhere.invalid/')
        (self.vault / '.git/info/grafts').write_text(self.source + '\n')
        with self.assertRaisesRegex(r.RetentionFailure, 'unsafe_git_config'): r.LocalGit(self.vault)
        (self.vault / '.git/info/grafts').unlink()
        self.git('update-ref', 'refs/replace/' + self.source, self.source)
        with self.assertRaisesRegex(r.RetentionFailure, 'unsafe_git_config'): r.LocalGit(self.vault)
        self.git('update-ref', '-d', 'refs/replace/' + self.source)
        self.git('remote', 'set-url', 'origin', 'https://elsewhere.invalid/repo.git')
        with self.assertRaisesRegex(r.RetentionFailure, 'unsafe_git_config'): self.backup()

    def test_source_raw_tree_mismatch_stops(self):
        with patch.object(self.remote, 'commit', return_value={'tree': 'e' * 40, 'parents': []}):
            with self.assertRaisesRegex(r.RetentionFailure, 'invalid_source_commit'): self.backup()
        self.assertEqual(self.remote.writes, [])

    def test_no_non_get_blob_read_during_construction(self):
        self.preview(); self.backup()
        forbidden = self.local.tree(self.source)['Wiki/unrelated.md'][2]
        original = r.LocalGit.blob
        def guarded(local, blob):
            self.assertNotEqual(blob, forbidden)
            return original(local, blob)
        with patch.object(r.LocalGit, 'blob', guarded): self.retain()

    def test_size_budget_rejects_before_reading_big_file(self):
        self.preview(); self.backup()
        with patch.object(r, 'MAX_GET_BYTES', 1):
            with self.assertRaisesRegex(r.RetentionFailure, 'source_tree_budget'): self.retain()
        self.assertEqual(len(self.remote.writes), 1)


class LocalBareGitTests(unittest.TestCase):
    setUp = RetentionTests.setUp
    git = RetentionTests.git
    preview = RetentionTests.preview
    backup = RetentionTests.backup
    retain = RetentionTests.retain
    def test_actual_create_only_push_and_remote_blob_readback(self):
        self.preview()
        bare = self.work / 'remote.git'
        self.git('clone', '--bare', str(self.vault), str(bare), root=self.work)
        source_remote = self.remote
        test = self
        class LocalRemote(r.GitHubRemote):
            def metadata(self): return {'full_name': r.REPOSITORY, 'private': True}
            def ref(self, name):
                result = subprocess.run(['git', '-C', str(bare), 'show-ref', '--verify', '--hash', name], capture_output=True)
                return result.stdout.decode().strip() if result.returncode == 0 else None
            def commit(self, commit):
                text = test.git('cat-file', '-p', commit, root=bare).split('\n\n', 1)[0].splitlines()
                return {'tree': next(x.split()[1] for x in text if x.startswith('tree ')), 'parents': [x.split()[1] for x in text if x.startswith('parent ')]}
            def blob(self, blob, expected_size):
                source_remote.reads.append(blob)
                return subprocess.check_output(['git', '-C', str(bare), 'cat-file', 'blob', blob])
            def create_only(self, name, commit):
                source_remote.writes.append((name, commit))
                with patch.object(r, 'REMOTE_URL', str(bare)):
                    super().create_only(name, commit)
        self.remote = LocalRemote(self.local, 'synthetic-token')
        self.backup(); result = self.retain()
        self.assertTrue(result['preview_retained'])
        self.assertEqual(self.remote.ref(r.MAIN_REF), self.source)
        self.assertEqual(len(source_remote.writes), 2)
        self.assertTrue(source_remote.reads)
        self.assertEqual(self.retain()['status'], 'preview_existing_verified')
        self.assertEqual(len(source_remote.writes), 2)
        # Direct create-only attempted against an existing ref must fail even
        # when the supplied commit differs, without changing that ref.
        current = self.remote.ref(r.PREVIEW_REF)
        with self.assertRaises(r.RetentionFailure): self.remote.create_only(r.PREVIEW_REF, self.source)
        self.assertEqual(self.remote.ref(r.PREVIEW_REF), current)


    def test_real_sparse_checkout_fetches_only_get_blobs(self):
        bare = self.work / 'sparse-remote.git'
        self.git('clone', '--bare', str(self.vault), str(bare), root=self.work)
        self.git('config', 'uploadpack.allowFilter', 'true', root=bare)
        target = self.work / 'fresh-sparse'
        with patch.object(r, 'REMOTE_URL', bare.as_uri()), \
             patch.object(r.GitHubRemote, 'metadata', return_value={'full_name': r.REPOSITORY, 'private': True}), \
             patch.object(r.GitHubRemote, 'ref', return_value=self.source), \
             patch.object(r.GitHubRemote, 'check_source_budget') as budget:
            out = io.StringIO()
            with patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'synthetic-token'}), contextlib.redirect_stdout(out):
                result = r.main(['checkout', '--vault', str(target), '--source-commit', self.source])
            self.assertEqual(result, 0, out.getvalue())
            self.assertTrue(json.loads(out.getvalue())['checkout_verified'])
            budget.assert_called_once_with(self.source)
        self.assertTrue((target / r.GET / '03 State/manifest.json').exists())
        self.assertFalse((target / 'Wiki').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD', root=target).strip(), self.source)
        config = (target / '.git/config').read_text()
        self.assertNotIn('synthetic-token', config)
        self.assertNotIn('extraheader', config)
        forbidden = self.local.tree(self.source)['Wiki/unrelated.md'][2]
        process = subprocess.run(['git', '-C', str(target), 'cat-file', '-e', forbidden],
                                 env={**r.safe_environment(), 'GIT_NO_LAZY_FETCH': '1'}, capture_output=True)
        self.assertNotEqual(process.returncode, 0)


class TransportAndCliTests(unittest.TestCase):
    def test_token_is_only_ephemeral_environment_and_source_secrets_are_removed(self):
        with patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'private-fixture', 'GETNOTE_API_KEY': 'get-fixture',
                                     'GETNOTE_CLIENT_ID': 'client-fixture', 'GIT_TRACE': '1'}):
            environment = r.safe_environment('private-fixture')
        self.assertNotIn('PRIVATE_VAULT_TOKEN', environment)
        self.assertNotIn('GETNOTE_API_KEY', environment)
        self.assertNotIn('GETNOTE_CLIENT_ID', environment)
        self.assertNotIn('GIT_TRACE', environment)
        self.assertEqual(environment['GIT_NO_REPLACE_OBJECTS'], '1')
        self.assertEqual(environment['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertTrue(any('AUTHORIZATION: basic ' in v for v in environment.values()))

    def test_fixed_ref_and_empty_lease_never_main(self):
        from unittest.mock import Mock
        local = Mock(); remote = r.GitHubRemote(local, 'fixture-token')
        with self.assertRaisesRegex(r.RetentionFailure, 'ref_not_allowed'): remote.create_only(r.MAIN_REF, 'a' * 40)
        local.run.assert_not_called()
        remote.create_only(r.PREVIEW_REF, 'a' * 40)
        args = local.run.call_args.args
        self.assertIn('--force-with-lease=' + r.PREVIEW_REF + ':', args)
        self.assertIn(r.REMOTE_URL, args)
        self.assertNotIn('fixture-token', ' '.join(args))
        self.assertFalse(any(x == '--force' for x in args))

    def test_public_report_filters_private_values_and_is_idempotent(self):
        value = {'status': 'preview_created_verified', 'preview_retained': True, 'pending': 3,
                 'source_status': 'partial', 'source_unchanged': True, 'main_updated': False,
                 'backup_verified': True, 'preview_only': True, 'apply_ready': False, 'published': False,
                 'changed_paths': 5, 'main_advanced': False, 'path': 'private title',
                 'token': 'secret', 'commit': 'a' * 40, 'url': 'https://private.example'}
        clean = r.public_report(value)
        self.assertEqual(r.public_report(clean), clean)
        for text in ('private title', 'secret', 'a' * 40, 'https://'): self.assertNotIn(text, json.dumps(clean))
        self.assertEqual(clean['source_status'], 'partial')
        self.assertEqual(r.public_report({'status': 'retention_failed', 'error': 'private title'})['error'], 'retention_failed')
        self.assertEqual(r.public_report([])['error'], 'invalid_report')

    def test_cli_safe_error_and_partial_exit(self):
        for error in (r.RetentionFailure('not-an-allowed-private-string'), RuntimeError('private-url-token')):
            out = io.StringIO()
            with patch.object(r, 'retain_private_preview', side_effect=error), contextlib.redirect_stdout(out):
                self.assertEqual(r.main(['--vault', 'fixture', '--source-commit', 'a' * 40, '--preview-output', 'fixture-output']), 1)
            self.assertNotIn('private-', out.getvalue())
        out = io.StringIO()
        with patch.object(r, 'retain_private_preview', return_value={'status': 'preview_created_verified', 'pending': 1, 'source_status': 'partial', 'preview_retained': True, 'source_unchanged': True, 'main_updated': False, 'backup_verified': True, 'preview_only': True, 'apply_ready': False, 'published': False, 'changed_paths': 5, 'main_advanced': False}), contextlib.redirect_stdout(out):
            self.assertEqual(r.main(['--vault', 'fixture', '--source-commit', 'a' * 40, '--preview-output', 'fixture-output']), 2)
        self.assertTrue(json.loads(out.getvalue())['preview_retained'])

    def test_report_missing_or_unproven_success_stays_failed_and_summary_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.json'
            for value in (None, {'status': 'preview_created_verified'}, {'status': 'backup_existing_verified', 'backup_verified': True}):
                if value is not None: path.write_text(json.dumps(value))
                process = subprocess.run([sys.executable, r.__file__, '--publish-report', str(path)], capture_output=True, text=True)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(process.stderr, '')
                self.assertEqual(json.loads(process.stdout)['status'], 'retention_failed')

    def test_checkout_public_or_drift_stops_before_git(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'new'
            with patch.object(r.GitHubRemote, 'metadata', return_value={'full_name': r.REPOSITORY, 'private': False}), patch.object(r.subprocess, 'run') as run:
                with self.assertRaisesRegex(r.RetentionFailure, 'repository_not_private_target'): r.checkout_private(destination, 'a' * 40, token='synthetic')
                run.assert_not_called()
            with patch.object(r.GitHubRemote, 'metadata', return_value={'full_name': r.REPOSITORY, 'private': True}), patch.object(r.GitHubRemote, 'ref', return_value='b' * 40), patch.object(r.subprocess, 'run') as run:
                with self.assertRaisesRegex(r.RetentionFailure, 'remote_main_changed'): r.checkout_private(destination, 'a' * 40, token='synthetic')
                run.assert_not_called()
            self.assertFalse(destination.exists())

    def test_get_tree_metadata_budget_uses_no_non_get_blob(self):
        from unittest.mock import Mock
        remote = r.GitHubRemote(Mock(), 'synthetic')
        values = [{'truncated': False, 'tree': [{'path': 'Raw', 'mode': '040000', 'type': 'tree', 'sha': 'b' * 40}]},
                  {'truncated': False, 'tree': [{'path': '03 Get', 'mode': '040000', 'type': 'tree', 'sha': 'c' * 40}]},
                  {'truncated': False, 'tree': [{'path': 'x.md', 'mode': '100644', 'type': 'blob', 'sha': 'd' * 40, 'size': r.MAX_BLOB_BYTES + 1}]}]
        with patch.object(remote, 'commit', return_value={'tree': 'a' * 40, 'parents': []}), patch.object(remote, 'request', side_effect=values) as request:
            with self.assertRaisesRegex(r.RetentionFailure, 'source_tree_budget'): remote.check_source_budget('a' * 40)
            self.assertEqual([call.args[0] for call in request.call_args_list], ['/git/trees/' + 'a' * 40, '/git/trees/' + 'b' * 40, '/git/trees/' + 'c' * 40 + '?recursive=1'])


if __name__ == '__main__':
    unittest.main()
