"""Object-damage fixtures must not assume Git's incidental loose/packed layout."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import git_object_fixture as f


class ObjectFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'; self.root.mkdir()
        f.git(self.root, 'init', '-q', '-b', 'main')
        f.disable_auto_maintenance(self.root)
        f.git(self.root, 'config', 'user.name', 'Fixture')
        f.git(self.root, 'config', 'user.email', 'fixture@example.invalid')
        for name in ('a', 'b', 'keep'):
            (self.root / name).write_bytes(('Synthetic ' + name).encode())
        f.git(self.root, 'add', '.'); f.git(self.root, 'commit', '-qm', 'Fixture')
        self.ids = {name: f.git(self.root, 'rev-parse', 'HEAD:' + name).stdout.decode().strip()
                    for name in ('a', 'b', 'keep')}

    def present(self, root=None):
        return set(f.git(root or self.root, 'cat-file', '--batch-all-objects',
                         '--batch-check=%(objectname)').stdout.splitlines())

    def test_packed_multi_object_cohort_normalizes_once_and_keeps_refs_index(self):
        f.git(self.root, 'repack', '-a', '-d')
        for oid in self.ids.values():
            self.assertFalse((self.root / '.git/objects' / oid[:2] / oid[2:]).exists())
            self.assertTrue(f.git(self.root, 'cat-file', '-p', oid).stdout)
        refs = f.git(self.root, 'show-ref').stdout
        index = (self.root / '.git/index').read_bytes(); before = self.present()
        ids = [self.ids['a'], self.ids['b']]
        with patch.object(f, 'git', wraps=f.git) as calls:
            removed = f.remove_objects(self.root, *ids)
        args = [row.args[1] for row in calls.call_args_list]
        self.assertEqual(args.count('pack-objects'), 1)
        self.assertEqual(args.count('unpack-objects'), 1)
        self.assertNotIn('repack', args); self.assertNotIn('fetch', args)
        self.assertEqual(set(removed), set(ids)); f.assert_missing(self.root, *ids)
        self.assertEqual(self.present(), before - {oid.encode() for oid in ids})
        self.assertEqual(f.git(self.root, 'show-ref').stdout, refs)
        self.assertEqual((self.root / '.git/index').read_bytes(), index)
        self.assertEqual(f.git(self.root, 'cat-file', 'blob', self.ids['keep']).stdout, b'Synthetic keep')

    def test_loose_unreferenced_candidate_objects_are_not_dropped(self):
        target = f.git(self.root, 'hash-object', '-w', '--stdin', data=b'Detached receipt').stdout.decode().strip()
        peer = f.git(self.root, 'hash-object', '-w', '--stdin', data=b'Other unreachable bytes').stdout.decode().strip()
        tree = f.git(self.root, 'rev-parse', 'HEAD^{tree}').stdout.decode().strip()
        commit = f.git(self.root, 'commit-tree', tree, '-p', 'HEAD', '-m', 'Detached candidate').stdout.decode().strip()
        original = f.git(self.root, 'cat-file', '-p', commit).stdout
        before = self.present(); f.remove_objects(self.root, target, self.ids['a'])
        self.assertEqual(self.present(), before - {target.encode(), self.ids['a'].encode()})
        self.assertEqual(f.git(self.root, 'cat-file', 'blob', peer).stdout, b'Other unreachable bytes')
        self.assertEqual(f.git(self.root, 'cat-file', '-p', commit).stdout, original)

    def test_missing_member_fails_before_packing_or_deleting_other_members(self):
        f.remove_objects(self.root, self.ids['a']); before = self.present()
        with patch.object(f, 'git', wraps=f.git) as calls:
            with self.assertRaises(subprocess.CalledProcessError):
                f.remove_objects(self.root, self.ids['b'], self.ids['a'])
        self.assertNotIn('pack-objects', [row.args[1] for row in calls.call_args_list])
        self.assertEqual(self.present(), before)
        self.assertEqual(f.git(self.root, 'cat-file', 'blob', self.ids['b']).stdout, b'Synthetic b')

    def test_bad_cohort_or_corrupt_object_is_never_silently_skipped(self):
        for ids in ((), ('bad',), (self.ids['a'], self.ids['a'])):
            with self.subTest(ids=ids), self.assertRaises(AssertionError):
                f.remove_objects(self.root, *ids)
        path, original = f.remove_objects(self.root, self.ids['a'])[self.ids['a']]
        path.write_bytes(b'Broken object')
        with patch.object(f, 'git', wraps=f.git) as calls:
            with self.assertRaises(subprocess.CalledProcessError):
                f.remove_objects(self.root, self.ids['a'])
        self.assertNotIn('pack-objects', [row.args[1] for row in calls.call_args_list])
        path.write_bytes(original)
        self.assertEqual(f.git(self.root, 'cat-file', 'blob', self.ids['a']).stdout, b'Synthetic a')

    def test_real_partial_clone_keeps_missing_assets_missing_without_transport(self):
        import test_notion_remote_journal as journal
        with tempfile.TemporaryDirectory() as directory:
            fixture = journal.Fixture(directory); root = fixture.root
            asset = f.git(root, 'rev-parse', 'HEAD:asset.bin').stdout.decode().strip()
            f.assert_missing(root, asset)
            target = f.git(root, 'hash-object', '-w', '--stdin', data=b'Cached state').stdout.decode().strip()
            peer = f.git(root, 'hash-object', '-w', '--stdin', data=b'Dangling peer').stdout.decode().strip()
            f.git(root, 'config', 'user.name', 'Fixture')
            f.git(root, 'config', 'user.email', 'fixture@example.invalid')
            tree = f.git(root, 'rev-parse', 'HEAD^{tree}').stdout.decode().strip()
            candidate = f.git(root, 'commit-tree', tree, '-p', 'HEAD', '-m', 'Unreferenced candidate').stdout.decode().strip()
            candidate_bytes = f.git(root, 'cat-file', '-p', candidate).stdout
            before = self.present(root); refs = f.git(root, 'show-ref').stdout
            index = (root / '.git/index').read_bytes()
            head = (root / '.git/HEAD').read_bytes(); resolved_head = f.git(root, 'rev-parse', 'HEAD').stdout
            with patch.object(f, 'git', wraps=f.git) as calls:
                f.remove_objects(root, target)
            self.assertNotIn('fetch', [row.args[1] for row in calls.call_args_list])
            f.assert_missing(root, target, asset)
            self.assertEqual(self.present(root), before - {target.encode()})
            self.assertEqual(f.git(root, 'show-ref').stdout, refs)
            self.assertEqual((root / '.git/index').read_bytes(), index)
            self.assertEqual((root / '.git/HEAD').read_bytes(), head)
            self.assertEqual(f.git(root, 'rev-parse', 'HEAD').stdout, resolved_head)
            self.assertEqual(f.git(root, 'cat-file', 'blob', peer).stdout, b'Dangling peer')
            self.assertEqual(f.git(root, 'cat-file', '-p', candidate).stdout, candidate_bytes)

    def test_shared_fixture_repositories_disable_automatic_maintenance(self):
        import test_legacy_adapter as legacy
        import test_notion_outbox as outbox
        import test_notion_remote_journal as journal
        first = legacy.LegacyAdapterTests(); first.setUp(); self.addCleanup(first.doCleanups)
        second = outbox.Tests(); second.setUp(); self.addCleanup(second.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            third = journal.Fixture(directory)
            for root in (first.repo, second.root, third.base / 'seed', third.bare,
                         third.root, third.clone('another-clone')):
                for key, value in f.AUTO_CONFIG.items():
                    self.assertEqual(f.git(root, 'config', '--local', '--get', key).stdout.decode().strip(), value)


if __name__ == '__main__':
    unittest.main()
