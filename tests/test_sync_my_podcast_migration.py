"""Offline date migration tests with synthetic notes and disposable Git before blobs."""
import datetime as dt
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import test_sync_my_podcast_cursor as fixtures

sync = fixtures.sync


class MigrationTests(unittest.TestCase):
    setUp = fixtures.CursorTests.setUp
    set_state = fixtures.CursorTests.set_state
    run_main = fixtures.CursorTests.run_main
    unchanged = fixtures.CursorTests.unchanged
    bodies = fixtures.CursorTests.bodies

    def source(self, date='2026-02-03', number=1, title=None):
        item = fixtures.page(number)
        item['properties']['日期'] = {'type': 'date', 'date': {'start': date}}
        if title is not None:
            item['properties']['Title']['title'][0]['plain_text'] = title
        return item

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Synthetic recoverable before')

    def seed(self, date='2026-01-03', number=1, title=None, commit=True):
        item = self.source(date, number, title)
        old = sync.sync_one('synthetic', item['id'], False, {}, item)
        sync.rebuild_index()
        self.before_index = (self.output / '索引.md').read_bytes()
        if commit:
            self.git('init', '-q')
            self.commit()
        return old

    def run_changed(self, date='2026-02-03', number=1, title=None):
        self.query.return_value = fixtures.response([self.source(date, number, title)])
        self.run_main('--all-current')

    def old_index_state(self):
        self.unchanged()
        self.assertEqual((self.output / '索引.md').read_bytes(), self.before_index)

    def test_month_change_moves_only_note_after_verification_and_keeps_git_before(self):
        old = self.seed()
        before = old.read_bytes()
        self.run_changed()
        self.assertFalse(old.exists())
        self.assertTrue(old.parent.exists())  # No directory deletion is claimed.
        target, = self.bodies()
        self.assertEqual(target.parent, self.output / '2026/2026-02')
        self.assertEqual(self.git('show', f'HEAD:{old.relative_to(self.root).as_posix()}'), before)
        self.assertIn('2026/2026-02/', (self.output / '索引.md').read_text())
        self.assertNotIn('2026/2026-01/', (self.output / '索引.md').read_text())
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_year_change_uses_listening_date_not_published_date(self):
        old = self.seed('2025-12-31')
        self.run_changed('2026-01-01')
        self.assertFalse(old.exists())
        self.assertEqual(self.bodies()[0].parent, self.output / '2026/2026-01')

    def test_timezone_month_boundary_is_local(self):
        old = self.seed('2026-01-31T15:59:00Z')
        self.run_changed('2026-01-31T16:00:00Z')
        self.assertFalse(old.exists())
        self.assertEqual(self.bodies()[0].parent, self.output / '2026/2026-02')

    def test_same_month_date_change_updates_without_rename(self):
        old = self.seed()
        with mock.patch.object(sync, 'git_read', side_effect=AssertionError('No migration')):
            self.run_changed('2026-01-09')
        self.assertEqual(self.bodies(), [old])
        self.assertIn('2026-01-09', old.read_text())

    def test_same_date_title_change_keeps_existing_path(self):
        old = self.seed()
        self.run_changed('2026-01-03', title='Renamed synthetic title')
        self.assertEqual(self.bodies(), [old])
        self.assertIn('Renamed synthetic title', old.read_text())

    def test_prior_bug_metadata_already_new_month_can_move_old_directory(self):
        old = self.seed()
        old.write_bytes(old.read_bytes().replace(b'2026-01-03', b'2026-02-03'))
        self.commit()
        self.run_changed()
        self.assertFalse(old.exists())
        self.assertEqual(self.bodies()[0].parent, self.output / '2026/2026-02')

    def test_legacy_root_requires_separately_scoped_owner_repair(self):
        old = self.seed()
        legacy = self.legacy / old.relative_to(self.output)
        legacy.parent.mkdir(parents=True)
        old.rename(legacy)
        self.commit()
        before = legacy.read_bytes()
        with mock.patch.object(sync, 'git_read', side_effect=AssertionError('No cross-root move')):
            with self.assertRaisesRegex(RuntimeError, 'legacy episode root'):
                self.run_changed()
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(len(self.bodies()), 0)
        self.old_index_state()

    def test_original_renderer_without_additive_attachment_fields_is_compatible(self):
        old = self.seed()
        data = old.read_text().replace('transcript_file_names: |-\n  []\n', '').replace('transcript_file_count: 0\n', '')
        old.write_text(data)
        self.commit()
        self.run_changed()
        self.assertFalse(old.exists())

    def test_same_title_different_id_never_overwrites_other_note(self):
        old = self.seed(title='Shared synthetic title')
        other = self.source(number=2, title='Shared synthetic title')
        other_path = sync.sync_one('synthetic', other['id'], False, {}, other)
        other_bytes = other_path.read_bytes()
        self.run_changed(title='Shared synthetic title')
        self.assertFalse(old.exists())
        self.assertEqual(other_path.read_bytes(), other_bytes)
        self.assertEqual(len(self.bodies()), 2)
        ids = {sync.parse_frontmatter(p.read_text())['episode_page_id'] for p in self.bodies()}
        self.assertEqual(ids, {fixtures.page(1)['id'], fixtures.page(2)['id']})

    def test_same_short_id_collision_stops_with_all_before_files_preserved(self):
        old = self.seed(title='Shared synthetic title')
        for number in (2, 3):
            item = self.source(number=number, title='Shared synthetic title')
            sync.sync_one('synthetic', item['id'], False, {}, item)
        before = {p: p.read_bytes() for p in self.bodies()}
        with self.assertRaisesRegex(RuntimeError, 'identity conflict'):
            self.run_changed(title='Shared synthetic title')
        self.assertEqual(before, {p: p.read_bytes() for p in self.bodies()})
        self.old_index_state()
        self.assertTrue(old.exists())

    def test_untracked_old_note_stops(self):
        old = self.seed(commit=False)
        self.git('init', '-q')
        self.git('add', '.github')
        self.git('-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'State only')
        with self.assertRaises(RuntimeError): self.run_changed()
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_no_git_repository_stops(self):
        old = self.seed(commit=False)
        with self.assertRaises(RuntimeError): self.run_changed()
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_dirty_old_body_stops_without_any_note_write(self):
        old = self.seed()
        old.write_bytes(old.read_bytes() + b'UNCOMMITTED HUMAN EDIT\n')
        before = old.read_bytes()
        with self.assertRaises(RuntimeError): self.run_changed()
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_committed_body_change_is_unknown_and_stops(self):
        old = self.seed()
        old.write_bytes(old.read_bytes() + b'COMMITTED HUMAN EDIT\n')
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'content changed or unknown'): self.run_changed()
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_staged_change_even_when_worktree_matches_head_stops(self):
        old = self.seed()
        before = old.read_bytes()
        old.write_bytes(before + b'STAGED HUMAN EDIT\n')
        self.git('add', str(old.relative_to(self.root)))
        old.write_bytes(before)
        with self.assertRaises(RuntimeError): self.run_changed()
        self.assertEqual(old.read_bytes(), before)
        self.old_index_state()

    def test_unknown_extra_frontmatter_field_stops(self):
        old = self.seed()
        old.write_bytes(old.read_bytes().replace(b'---\n', b'---\nhuman_field: "keep"\n', 1))
        self.commit()
        with self.assertRaises(RuntimeError): self.run_changed()
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_present_modified_additive_attachment_metadata_is_not_ignored(self):
        old = self.seed()
        old.write_bytes(old.read_bytes().replace(b'transcript_file_count: 0', b'transcript_file_count: 99'))
        self.commit()
        with self.assertRaises(RuntimeError): self.run_changed()
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_unknown_or_wrong_managed_markers_stop(self):
        old = self.seed()
        old.write_bytes(old.read_bytes().replace(b'xiaoyuzhou_episode', b'human_note'))
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'Unmanaged'): self.run_changed()
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_id_embedded_in_untrusted_string_is_not_exact_identity(self):
        old = self.seed()
        old.write_bytes(old.read_bytes().replace(fixtures.page(1)['id'].encode(), ('prefix-' + fixtures.page(1)['id']).encode()))
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'Unmanaged'): self.run_changed()
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_source_content_and_date_change_together_conservatively_stop(self):
        old = self.seed()
        with mock.patch.object(sync, 'build_body', return_value='CHANGED SOURCE BODY\n'):
            with self.assertRaisesRegex(RuntimeError, 'content changed or unknown'): self.run_changed()
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_duplicate_old_paths_stop_instead_of_selecting_first(self):
        old = self.seed()
        copy = self.output / '2025/2025-12/Another.md'
        copy.parent.mkdir(parents=True)
        copy.write_bytes(old.read_bytes())
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'): self.run_changed()
        self.assertEqual(set(self.bodies()), {old, copy})
        self.old_index_state()

    def test_third_copy_across_legacy_root_is_not_hidden(self):
        old = self.seed()
        for root, month in [(self.output, '2026-02'), (self.legacy, '2026-01')]:
            copy = root / '2026' / month / 'Copy.md'
            copy.parent.mkdir(parents=True)
            copy.write_bytes(old.read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'legacy episode root'): self.run_changed()
        self.old_index_state()

    def test_wrong_content_at_existing_same_id_destination_stops(self):
        old = self.seed()
        target = self.output / '2026/2026-02' / old.name
        target.parent.mkdir(parents=True)
        target.write_bytes(old.read_bytes().replace(b'2026-01-03', b'2026-02-03') + b'UNKNOWN TARGET EDIT\n')
        before = target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'destination conflict'): self.run_changed()
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(old.exists())
        self.old_index_state()

    def test_wrong_date_at_existing_destination_is_not_accepted_as_retry(self):
        old = self.seed()
        target = self.output / '2026/2026-02' / old.name
        target.parent.mkdir(parents=True)
        target.write_bytes(old.read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'destination conflict'): self.run_changed()
        self.assertTrue(old.exists())
        self.old_index_state()

    def test_unmanaged_existing_destination_is_untouched(self):
        old = self.seed()
        target = self.output / '2026/2026-02' / old.name
        target.parent.mkdir(parents=True)
        target.write_text('UNMANAGED NOTE\n')
        with self.assertRaisesRegex(RuntimeError, 'Unmanaged'):
            self.run_changed()
        self.assertEqual(target.read_text(), 'UNMANAGED NOTE\n')
        self.assertTrue(old.exists())
        self.old_index_state()

    def test_symlink_old_note_stops_without_following_or_deleting(self):
        old = self.seed()
        saved = self.root / 'saved.md'
        old.rename(saved)
        old.symlink_to(saved)
        before = saved.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Symlink'): self.run_changed()
        self.assertTrue(old.is_symlink())
        self.assertEqual(saved.read_bytes(), before)
        self.old_index_state()

    def test_symlink_destination_directory_stops(self):
        old = self.seed()
        elsewhere = self.root / 'unmanaged'
        elsewhere.mkdir()
        (self.output / '2026/2026-02').symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'): self.run_changed()
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertTrue(old.exists())
        self.old_index_state()

    def test_unregistered_flat_old_path_stops(self):
        old = self.seed()
        flat = self.output / old.name
        old.rename(flat)
        self.commit()
        with self.assertRaisesRegex(RuntimeError, 'Unmanaged'): self.run_changed()
        self.assertTrue(flat.exists())
        self.old_index_state()

    def test_new_write_failure_keeps_old_note_index_and_state(self):
        old = self.seed()
        before = old.read_bytes()
        with mock.patch.object(sync.os, 'link', side_effect=OSError('Synthetic write failure')):
            with self.assertRaises(OSError): self.run_changed()
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()

    def test_readback_failure_never_removes_old_or_advances(self):
        old = self.seed()
        real_link = sync.os.link
        def corrupt(src, dst):
            real_link(src, dst)
            Path(dst).write_text('UNKNOWN POSTWRITE CONTENT\n')
        with mock.patch.object(sync.os, 'link', side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'readback'): self.run_changed()
        self.assertTrue(old.exists())
        self.old_index_state()
        # Unknown result is a blocker, not an automatic delete/retry.
        with self.assertRaises(RuntimeError): self.run_changed()
        self.assertTrue(old.exists())

    def test_concurrent_destination_creation_is_not_overwritten(self):
        old = self.seed()
        real_link = sync.os.link
        def raced(src, dst):
            Path(dst).write_text('CONCURRENT UNMANAGED FILE\n')
            real_link(src, dst)
        with mock.patch.object(sync.os, 'link', side_effect=raced):
            with self.assertRaises(FileExistsError): self.run_changed()
        self.assertTrue(old.exists())
        self.assertIn('CONCURRENT UNMANAGED FILE', next(p for p in self.bodies() if p != old).read_text())
        self.old_index_state()

    def test_unlink_failure_leaves_full_target_and_retry_deduplicates(self):
        old = self.seed()
        original = old.read_bytes()
        real_unlink = Path.unlink
        def fail_old(path, *args, **kwargs):
            if path == old: raise OSError('Synthetic unlink failure')
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', new=fail_old):
            with self.assertRaises(OSError): self.run_changed()
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(len(self.bodies()), 2)
        target = next(p for p in self.bodies() if p != old)
        completed = target.read_bytes()
        self.old_index_state()
        self.clock += dt.timedelta(hours=1)
        self.run_changed()
        self.assertEqual(self.bodies(), [target])
        self.assertEqual(target.read_bytes(), completed)  # Retry does not clobber a verified target.
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_unlink_performed_then_raises_can_resume_without_second_delete(self):
        old = self.seed()
        real_unlink = Path.unlink
        def unknown(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if path == old: raise OSError('Unknown after unlink')
            return result
        with mock.patch.object(Path, 'unlink', new=unknown):
            with self.assertRaises(OSError): self.run_changed()
        self.assertFalse(old.exists())
        self.old_index_state()
        self.run_changed()
        self.assertEqual(len(self.bodies()), 1)
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_index_failure_after_successful_move_can_resume(self):
        old = self.seed()
        with mock.patch.object(sync, 'rebuild_index', side_effect=OSError('Index failure')):
            with self.assertRaises(OSError): self.run_changed()
        self.assertFalse(old.exists())
        self.old_index_state()
        self.run_changed()
        self.assertEqual(len(self.bodies()), 1)
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_old_changed_after_new_write_is_preserved_and_retry_stops(self):
        old = self.seed()
        real_write = sync.write_verified_text
        def mutate_old(path, content, **kwargs):
            real_write(path, content, **kwargs)
            old.write_bytes(old.read_bytes() + b'CONCURRENT OLD EDIT\n')
        with mock.patch.object(sync, 'write_verified_text', side_effect=mutate_old):
            with self.assertRaisesRegex(RuntimeError, 'changed during migration'): self.run_changed()
        self.assertIn(b'CONCURRENT OLD EDIT', old.read_bytes())
        self.old_index_state()
        with self.assertRaises(RuntimeError): self.run_changed()
        self.assertTrue(old.exists())

    def test_head_changed_after_new_write_stops_old_removal(self):
        old = self.seed()
        real_write = sync.write_verified_text
        def change_head(path, content, **kwargs):
            real_write(path, content, **kwargs)
            unrelated = self.root / 'unrelated.txt'
            unrelated.write_text('Synthetic unrelated update')
            self.git('add', 'unrelated.txt')
            self.git('-c', 'user.name=Synthetic', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'New HEAD')
        with mock.patch.object(sync, 'write_verified_text', side_effect=change_head):
            with self.assertRaisesRegex(RuntimeError, 'changed during migration'): self.run_changed()
        self.assertTrue(old.exists())
        self.old_index_state()

    def test_destination_changed_during_git_recheck_keeps_old(self):
        old = self.seed()
        real_before = sync.recoverable_before
        calls = 0
        def mutate(path, data):
            nonlocal calls
            result = real_before(path, data)
            calls += 1
            if calls == 2:
                target = next(p for p in self.bodies() if p != old)
                target.write_text('CONCURRENT TARGET EDIT\n')
            return result
        with mock.patch.object(sync, 'recoverable_before', side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, 'changed during migration'): self.run_changed()
        self.assertTrue(old.exists())
        self.old_index_state()

    def test_unmanaged_sibling_and_empty_directories_are_never_deleted(self):
        old = self.seed()
        sibling = old.parent / 'human.txt'
        sibling.write_text('Human content')
        self.run_changed()
        self.assertFalse(old.exists())
        self.assertEqual(sibling.read_text(), 'Human content')
        self.assertTrue(old.parent.exists())

    def test_migration_dry_run_has_no_writes_or_git_commands(self):
        old = self.seed()
        before = {p: p.read_bytes() for p in [old, self.output/'索引.md', self.state]}
        self.query.return_value = fixtures.response([self.source()])
        with mock.patch.object(sync, 'git_read', side_effect=AssertionError('Read-only preview')):
            self.run_main('--all-current', '--dry-run')
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(self.bodies(), [old])

    def test_page_identity_mismatch_stops_before_any_change(self):
        old = self.seed()
        with self.assertRaisesRegex(RuntimeError, 'Source episode identity conflict'):
            sync.sync_one('synthetic', fixtures.page(1)['id'], False, {}, self.source(number=2))
        self.assertEqual(self.bodies(), [old])
        self.old_index_state()


if __name__ == '__main__':
    unittest.main()
