from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_proof as proof


class MergeProofTraversalTest(unittest.TestCase):
    def records(self, commits, trees):
        remote = type('Remote', (), {'commit': lambda _self, oid: commits[oid]})()
        result = proof.PrivateRecords(remote)
        result._fixed_path_entries = lambda tree: trees[tree]
        return result

    def test_allows_merge_without_reading_second_parent_when_proof_objects_match(self):
        parent, second, merge = 'a' * 40, 'b' * 40, 'c' * 40
        tree_parent, tree_merge = 'd' * 40, 'e' * 40
        objects = {
            proof.PATHS['publication']: ('f' * 40, 123),
            proof.PATHS['journal']: ('0' * 40, 456),
        }
        result = self.records({
            merge: {'tree': tree_merge, 'parents': [parent, second]},
            parent: {'tree': tree_parent, 'parents': []},
        }, {tree_merge: objects, tree_parent: objects})

        node = result.read_node(merge)

        self.assertEqual(node['parent'], parent)
        self.assertEqual(node['objects'], objects)

    def test_rejects_merge_when_publication_object_changes(self):
        parent, second, merge = 'a' * 40, 'b' * 40, 'c' * 40
        tree_parent, tree_merge = 'd' * 40, 'e' * 40
        old = {proof.PATHS['publication']: ('f' * 40, 123)}
        new = {proof.PATHS['publication']: ('0' * 40, 124)}
        result = self.records({
            merge: {'tree': tree_merge, 'parents': [parent, second]},
            parent: {'tree': tree_parent, 'parents': []},
        }, {tree_merge: new, tree_parent: old})

        with self.assertRaises(proof.PrivateProofError):
            result.read_node(merge)

    def test_rejects_merge_when_journal_object_changes(self):
        parent, second, merge = 'a' * 40, 'b' * 40, 'c' * 40
        tree_parent, tree_merge = 'd' * 40, 'e' * 40
        old = {proof.PATHS['journal']: ('f' * 40, 123)}
        new = {proof.PATHS['journal']: ('0' * 40, 124)}
        result = self.records({
            merge: {'tree': tree_merge, 'parents': [parent, second]},
            parent: {'tree': tree_parent, 'parents': []},
        }, {tree_merge: new, tree_parent: old})

        with self.assertRaises(proof.PrivateProofError):
            result.read_node(merge)

    def test_rejects_ambiguous_parent_lists(self):
        parent, merge = 'a' * 40, 'b' * 40
        result = self.records({merge: {'tree': 'c' * 40, 'parents': [parent, parent]}},
                              {'c' * 40: {}})

        with self.assertRaises(proof.PrivateProofError):
            result.read_node(merge)


if __name__ == '__main__':
    unittest.main()
