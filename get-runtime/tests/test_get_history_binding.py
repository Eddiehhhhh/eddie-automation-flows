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


if __name__ == '__main__':
    unittest.main()
