import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_runtime_bindings
with patch.object(get_private_runtime_bindings, 'value', return_value=None):
    entry = importlib.import_module('get_recurring_entry')


class Local:
    def __init__(self, parents):
        self.parents = parents

    def run(self, *args):
        commit = args[-1]
        return ' '.join([commit, *self.parents[commit]]).encode()


class Reader:
    def __init__(self, parents, trees):
        self.local = Local(parents)
        self.trees = trees

    def source_get_tree(self, commit):
        return self.trees[commit]


class MainlineHistory(unittest.TestCase):
    def setUp(self):
        self.root, self.linear, self.side, self.merge = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        self.tree, self.changed = ('1' * 40, '2' * 40)

    def test_linear_history_passes(self):
        reader = Reader({self.root: [], self.linear: [self.root]}, {self.root: self.tree, self.linear: self.tree})
        entry.unchanged_get_history(reader, self.root, self.linear)

    def test_unchanged_first_parent_merge_passes_with_different_side_parent(self):
        parents = {self.root: [], self.linear: [self.root], self.side: [self.root], self.merge: [self.linear, self.side]}
        trees = {self.root: self.tree, self.linear: self.tree, self.side: self.changed, self.merge: self.tree}
        entry.unchanged_get_history(Reader(parents, trees), self.root, self.merge)

    def test_changed_merge_result_fails(self):
        parents = {self.root: [], self.linear: [self.root], self.side: [self.root], self.merge: [self.linear, self.side]}
        trees = {self.root: self.tree, self.linear: self.tree, self.side: self.changed, self.merge: self.changed}
        with self.assertRaises(entry.EntryFailure):
            entry.unchanged_get_history(Reader(parents, trees), self.root, self.merge)

    def test_changed_first_parent_fails_even_if_merge_result_matches(self):
        parents = {self.root: [], self.linear: [self.root], self.side: [self.root], self.merge: [self.linear, self.side]}
        trees = {self.root: self.tree, self.linear: self.changed, self.side: self.tree, self.merge: self.tree}
        with self.assertRaises(entry.EntryFailure):
            entry.unchanged_get_history(Reader(parents, trees), self.root, self.merge)

    def test_octopus_merge_fails(self):
        third = 'e' * 40
        parents = {self.root: [], self.linear: [self.root], self.side: [self.root], third: [self.root], self.merge: [self.linear, self.side, third]}
        trees = {self.root: self.tree, self.linear: self.tree, self.side: self.changed, third: self.tree, self.merge: self.tree}
        with self.assertRaises(entry.EntryFailure):
            entry.unchanged_get_history(Reader(parents, trees), self.root, self.merge)

    def test_first_parent_edit_and_revert_fails(self):
        revert = 'f' * 40
        parents = {self.root: [], self.linear: [self.root], revert: [self.linear]}
        trees = {self.root: self.tree, self.linear: self.changed, revert: self.tree}
        with self.assertRaises(entry.EntryFailure):
            entry.unchanged_get_history(Reader(parents, trees), self.root, revert)

    def test_single_parent_receipt_check_stays_strict(self):
        parents = {self.merge: [self.linear, self.side]}
        with self.assertRaises(entry.EntryFailure):
            entry.single_parent(Reader(parents, {self.merge: self.tree}), self.merge)

    def test_exact_single_note_migration_is_allowed_only_for_pilot(self):
        pilot_id = '1234567890'
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        before, migration, before_tree, after_tree = migration_data
        root = '9' * 40
        reader = Reader({root: [], before: [root], migration: [before]},
                        {root: before_tree, before: before_tree, migration: after_tree})
        with patch.object(entry, 'PILOT_SOURCE_ID', pilot_id), patch.object(entry, 'PILOT_MIGRATION', migration_data):
            entry.unchanged_get_history(reader, root, migration, source_id=pilot_id)
        for source_id in (None, '1234567891'):
            with self.subTest(source_id=source_id), self.assertRaises(entry.EntryFailure):
                with patch.object(entry, 'PILOT_SOURCE_ID', pilot_id), patch.object(entry, 'PILOT_MIGRATION', migration_data):
                    entry.unchanged_get_history(reader, root, migration, source_id=source_id)

    def test_pilot_exception_does_not_skip_history_before_migration(self):
        pilot_id = '1234567890'
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        root, before, migration, before_tree, after_tree = ('9' * 40, *migration_data)
        changed_commit = '8' * 40
        reader = Reader({root: [], changed_commit: [root], before: [changed_commit], migration: [before]},
                        {root: before_tree, changed_commit: self.changed,
                         before: before_tree, migration: after_tree})
        with self.assertRaises(entry.EntryFailure):
            with patch.object(entry, 'PILOT_SOURCE_ID', pilot_id), patch.object(entry, 'PILOT_MIGRATION', migration_data):
                entry.unchanged_get_history(reader, root, migration, source_id=pilot_id)

    def test_pilot_exception_rejects_any_other_commit_or_tree(self):
        pilot_id = '1234567890'
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        before, migration, before_tree, after_tree = migration_data
        other = 'e' * 40
        for commit, tree, parent in ((other, after_tree, before),
                                     (migration, '3' * 40, before),
                                     (migration, after_tree, other)):
            with self.subTest(commit=commit, tree=tree, parent=parent), self.assertRaises(entry.EntryFailure):
                reader = Reader({before: [], migration: [parent], other: [before]},
                                {before: before_tree, migration: tree, other: after_tree})
                with patch.object(entry, 'PILOT_SOURCE_ID', pilot_id), patch.object(entry, 'PILOT_MIGRATION', migration_data):
                    entry.unchanged_get_history(reader, before, commit, source_id=pilot_id)


if __name__ == '__main__':
    unittest.main()
