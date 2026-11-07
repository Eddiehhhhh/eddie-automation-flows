"""Real Git history checks must not cache every full repository tree."""
import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_runtime_bindings
with patch.object(get_private_runtime_bindings, 'value', return_value=None):
    entry = importlib.import_module('get_recurring_entry')
a = entry.a


class GetTreeOid(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='get-tree-oid-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.env = {**a.retention.safe_environment(), 'GIT_AUTHOR_NAME': 'Fixture',
                    'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                    'GIT_COMMITTER_NAME': 'Fixture',
                    'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
        self.git('init', '-q')
        self.body = self.git('hash-object', '-w', '--stdin', data=b'fixture note\n')
        self.get_tree = self.tree([('100644', 'blob', self.body, 'note.md')])
        self.project_tree = self.tree([
            ('100644', 'blob', self.body, 'design-%04d.txt' % index) for index in range(200)])
        self.base = self.commit(self.get_tree)
        self.reader = a.OfflineGit(self.root)

    def git(self, *args, data=None):
        return subprocess.check_output(['git', '-C', str(self.root), *args],
                                       input=data, env=self.env, stderr=subprocess.PIPE).decode().strip()

    def tree(self, rows):
        return self.git('mktree', data=''.join('%s %s %s\t%s\n' % row for row in rows).encode())

    def commit(self, get_object, parent=None, *, mode='040000', kind='tree', serial=0):
        raw = self.tree([] if get_object is None else [(mode, kind, get_object, '03 Get')])
        stamp = self.git('hash-object', '-w', '--stdin', data=str(serial).encode())
        root = self.tree([('040000', 'tree', raw, 'Raw'),
                          ('040000', 'tree', self.project_tree, 'project'),
                          ('100644', 'blob', stamp, 'unrelated.txt')])
        return self.git('commit-tree', root, *(['-p', parent] if parent else []),
                        data=b'fixture history\n')

    def test_real_history_keeps_get_oid_and_does_not_cache_full_trees(self):
        tip = self.base
        for index in range(1, 25):
            tip = self.commit(self.get_tree, tip, serial=index)
        with patch.object(self.reader, 'tree', wraps=self.reader.tree) as full_tree:
            entry.unchanged_get_history(self.reader, self.base, tip)
        self.assertEqual(self.reader.source_get_tree(tip), self.get_tree)
        self.assertEqual(full_tree.call_count, 0)
        self.assertEqual(self.reader.cache, {})

    def test_edit_then_restore_is_still_rejected_by_real_history_walker(self):
        altered = self.tree([('100644', 'blob', self.body, 'changed.md')])
        middle = self.commit(altered, self.base)
        restored = self.commit(self.get_tree, middle)
        self.assertEqual(self.reader.source_get_tree(self.base),
                         self.reader.source_get_tree(restored))
        with patch.object(entry, 'PILOT_MIGRATION', None):
            with self.assertRaisesRegex(a.ParityFailure, 'entry_foreign_get_change'):
                entry.unchanged_get_history(self.reader, self.base, restored)
        self.assertEqual(self.reader.cache, {})

    def test_get_node_must_be_a_tree(self):
        for mode, kind, object_id in [('100644', 'blob', self.body),
                                     ('120000', 'blob', self.body),
                                     ('160000', 'commit', self.base)]:
            with self.subTest(mode=mode):
                bad = self.commit(object_id, self.base, mode=mode, kind=kind)
                with self.assertRaises(a.ParityFailure):
                    self.reader.source_get_tree(bad)
        self.assertEqual(self.reader.cache, {})

    def test_duplicate_raw_or_get_entry_is_rejected(self):
        def duplicate_tree(name, first, second):
            prefix = b'40000 ' + name + b'\0'
            return self.git('hash-object', '-t', 'tree', '--literally', '-w', '--stdin',
                            data=prefix + bytes.fromhex(first) + prefix + bytes.fromhex(second))

        raw = self.tree([('040000', 'tree', self.get_tree, '03 Get')])
        other_get = self.tree([('100644', 'blob', self.body, 'other.md')])
        other_raw = self.tree([('040000', 'tree', other_get, '03 Get')])
        duplicate_get = duplicate_tree(b'03 Get', self.get_tree, other_get)
        roots = {'Raw': duplicate_tree(b'Raw', raw, other_raw),
                 '03 Get': self.tree([('040000', 'tree', duplicate_get, 'Raw')])}
        for name, root in roots.items():
            with self.subTest(name=name):
                commit = self.git('commit-tree', root, '-p', self.base, data=b'duplicate fixture\n')
                with self.assertRaisesRegex(a.ParityFailure, 'adapter_source_tree'):
                    self.reader.source_get_tree(commit)
        self.assertEqual(self.reader.cache, {})

    def test_noncommit_missing_and_malformed_inputs_are_rejected(self):
        for value in (self.body, self.get_tree, '0' * 40, 'HEAD', self.base + ':Raw'):
            with self.subTest(value=value):
                with self.assertRaises((a.ParityFailure, a.retention.RetentionFailure)):
                    self.reader.source_get_tree(value)
        missing = self.commit(None, self.base)
        with self.assertRaisesRegex(a.ParityFailure, 'adapter_source_tree'):
            self.reader.source_get_tree(missing)
        self.assertEqual(self.reader.cache, {})

    def test_tree_mode_pointing_to_actual_blob_is_rejected_at_either_level(self):
        for name in (b'Raw', b'03 Get'):
            with self.subTest(name=name):
                bad = self.git('hash-object', '-t', 'tree', '--literally', '-w', '--stdin',
                               data=b'40000 ' + name + b'\0' + bytes.fromhex(self.body))
                root = bad if name == b'Raw' else self.tree([('040000', 'tree', bad, 'Raw')])
                commit = self.git('commit-tree', root, '-p', self.base, data=b'wrong object type\n')
                with self.assertRaisesRegex(a.ParityFailure, 'adapter_source_tree'):
                    self.reader.source_get_tree(commit)
        self.assertEqual(self.reader.cache, {})

    def test_full_tree_and_body_read_keep_existing_validation_and_cache(self):
        self.assertEqual(self.reader.source_get_tree(self.base), self.get_tree)
        self.assertEqual(self.reader.cache, {})
        inventory = self.reader.tree(self.base)
        self.assertEqual(len(inventory), 202)
        self.assertEqual(inventory['Raw/03 Get/note.md'], ('100644', 'blob', self.body))
        self.assertEqual(set(self.reader.cache), {self.base})
        self.assertEqual(self.reader.read(self.base, 'Raw/03 Get/note.md'), b'fixture note\n')
        with self.assertRaisesRegex(a.ParityFailure, 'adapter_missing_blob'):
            self.reader.read(self.base, 'Raw/03 Get/absent.md')


if __name__ == '__main__':
    unittest.main()
