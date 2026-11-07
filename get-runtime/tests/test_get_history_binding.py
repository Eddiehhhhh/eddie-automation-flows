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
    def long_history(self, edges):
        ids = [format(i+1, '040x') for i in range(edges+1)]
        parents = {commit: [ids[i-1]] if i else [] for i, commit in enumerate(ids)}
        return ids, Reader(parents, dict.fromkeys(ids, self.tree))

    def test_long_first_parent_interval_checks_every_tree(self):
        ids, reader = self.long_history(3749)
        with patch.object(reader, 'source_get_tree', wraps=reader.source_get_tree) as reads:
            entry.unchanged_get_history(reader, ids[0], ids[-1])
        self.assertEqual({call.args[0] for call in reads.call_args_list}, set(ids))

    def test_restored_edit_beyond_old_window_is_rejected(self):
        ids, reader = self.long_history(3749)
        reader.trees[ids[100]] = self.changed
        with patch.object(entry, 'PILOT_MIGRATION', None):
            with self.assertRaisesRegex(entry.EntryFailure, 'entry_foreign_get_change'):
                entry.unchanged_get_history(reader, ids[0], ids[-1])

    def test_one_edge_past_history_window_is_rejected(self):
        ids, reader = self.long_history(entry.MAX_INTERVENING + 1)
        with self.assertRaisesRegex(entry.EntryFailure, 'entry_history_bound'):
            entry.unchanged_get_history(reader, ids[0], ids[-1])

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

    def test_exact_historical_migration_is_allowed_for_any_current_source_id(self):
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        before, migration, before_tree, after_tree = migration_data
        root = '9' * 40
        reader = Reader({root: [], before: [root], migration: [before]},
                        {root: before_tree, before: before_tree, migration: after_tree})
        with patch.object(entry, 'PILOT_MIGRATION', migration_data):
            for source_id in (None, '1234567890', '1234567891', '1234567892'):
                with self.subTest(source_id=source_id):
                    entry.unchanged_get_history(reader, root, migration, source_id=source_id)

    def test_historical_migration_allows_later_commits_for_a_new_source(self):
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        before, migration, before_tree, after_tree = migration_data
        later = 'c' * 40
        root = '9' * 40
        reader = Reader({root: [], before: [root], migration: [before], later: [migration]},
                        {root: before_tree, before: before_tree, migration: after_tree, later: after_tree})
        with patch.object(entry, 'PILOT_MIGRATION', migration_data):
            entry.unchanged_get_history(reader, root, later, source_id='1234567891')

    def test_historical_exception_does_not_allow_migration_then_revert(self):
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        before, migration, before_tree, after_tree = migration_data
        later, reverted = 'c' * 40, 'd' * 40
        reader = Reader({before: [], migration: [before], later: [migration], reverted: [later]},
                        {before: before_tree, migration: after_tree, later: after_tree, reverted: before_tree})
        with self.assertRaises(entry.EntryFailure):
            with patch.object(entry, 'PILOT_MIGRATION', migration_data):
                entry.unchanged_get_history(reader, before, reverted, source_id='1234567891')

    def test_historical_exception_does_not_skip_history_before_migration(self):
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        root, before, migration, before_tree, after_tree = ('9' * 40, *migration_data)
        changed_commit = '8' * 40
        reader = Reader({root: [], changed_commit: [root], before: [changed_commit], migration: [before]},
                        {root: before_tree, changed_commit: self.changed,
                         before: before_tree, migration: after_tree})
        with self.assertRaises(entry.EntryFailure):
            with patch.object(entry, 'PILOT_MIGRATION', migration_data):
                entry.unchanged_get_history(reader, root, migration, source_id='1234567891')

    def test_historical_exception_rejects_any_other_commit_or_tree(self):
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        before, migration, before_tree, after_tree = migration_data
        other = 'e' * 40
        for commit, tree, parent in ((other, after_tree, before),
                                     (migration, '3' * 40, before),
                                     (migration, after_tree, other)):
            with self.subTest(commit=commit, tree=tree, parent=parent), self.assertRaises(entry.EntryFailure):
                reader = Reader({before: [], migration: [parent], other: [before]},
                                {before: before_tree, migration: tree, other: after_tree})
                with patch.object(entry, 'PILOT_MIGRATION', migration_data):
                    entry.unchanged_get_history(reader, before, commit, source_id='1234567891')

    def test_exact_historical_migration_does_not_admit_an_unregistered_get_tree(self):
        migration_data = ('a' * 40, 'b' * 40, '1' * 40, '2' * 40)
        before, migration, before_tree, after_tree = migration_data
        later = 'c' * 40
        root = '9' * 40
        rogue_tree = '3' * 40
        reader = Reader({root: [], before: [root], migration: [before], later: [migration]},
                        {root: before_tree, before: before_tree, migration: after_tree, later: rogue_tree})
        with patch.object(entry, 'PILOT_MIGRATION', migration_data):
            with self.assertRaisesRegex(entry.EntryFailure, 'entry_foreign_get_change'):
                entry.unchanged_get_history(reader, root, later, source_id='1234567891')


class FailedNotionReceiptPolicy(unittest.TestCase):
    def test_only_registered_sequence_four_receipt_crosses_policy(self):
        binding = {'selection': {'receipt_commit': 'a' * 40, 'receipt_sha256': 'b' * 64},
                   'sequence': 4, 'source_policy': {'get_policy_sha256': 'c' * 64}}
        receipt = {'sequence': 4, 'policy_sha256': 'c' * 64}
        with patch.object(entry.recovery._private_bindings, 'value', return_value=binding), \
                patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {'receipt_commit': 'f' * 40}), \
                patch.object(entry.recovery, 'current_get_policy', return_value='d' * 64):
            self.assertTrue(entry.recovery.accepts_receipt('a' * 40, 'b' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('a' * 40, 'e' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('a' * 40, 'b' * 64, receipt, 'e' * 64))

    def test_only_exact_sequence_five_receipt_crosses_policy(self):
        record = {'receipt_commit': 'a' * 40, 'receipt_sha256': 'b' * 64,
                  'receipt_sequence': 5, 'policy_sha256': 'c' * 64}
        seq5 = {'record': record}
        seq4 = {'selection': {'receipt_commit': 'f' * 40, 'receipt_sha256': 'f' * 64},
                'sequence': 4, 'source_policy': {'get_policy_sha256': 'f' * 64}}
        def value(_module, key): return seq5 if key == 'SEQ5_BINDING' else seq4
        receipt = {'sequence': 5, 'policy_sha256': 'c' * 64}
        with patch.object(entry.recovery._private_bindings, 'value', side_effect=value), \
                patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {'receipt_commit': 'f' * 40}), \
                patch.object(entry.recovery, 'current_get_policy', return_value='d' * 64):
            self.assertTrue(entry.recovery.accepts_receipt('a' * 40, 'b' * 64, receipt, 'd' * 64))
            for commit, digest, value, policy in (('e' * 40, 'b' * 64, receipt, 'd' * 64),
                    ('a' * 40, 'e' * 64, receipt, 'd' * 64),
                    ('a' * 40, 'b' * 64, {'sequence': 6, 'policy_sha256': 'c' * 64}, 'd' * 64),
                    ('a' * 40, 'b' * 64, receipt, 'e' * 64)):
                self.assertFalse(entry.recovery.accepts_receipt(commit, digest, value, policy))

    def test_sequence_six_receipt_requires_exact_evidence(self):
        seq6 = {'record': {'receipt_commit': 'a' * 40, 'receipt_sha256': 'b' * 64,
                           'receipt_sequence': 6, 'policy_sha256': 'c' * 64}}
        def value(_module, key):
            if key == 'SEQ6_BINDING': return seq6
            if key == 'SEQ5_BINDING': return {}
            return {'selection': {'receipt_commit': 'f' * 40, 'receipt_sha256': 'f' * 64},
                    'sequence': 4, 'source_policy': {'get_policy_sha256': 'f' * 64}}
        with patch.object(entry.recovery._private_bindings, 'value', side_effect=value), \
                patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {'receipt_commit': 'f' * 40}), \
                patch.object(entry.recovery, 'current_get_policy', return_value='d' * 64):
            receipt = {'sequence': 6, 'policy_sha256': 'c' * 64}
            self.assertTrue(entry.recovery.accepts_receipt('a' * 40, 'b' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('a' * 40, 'b' * 64, {**receipt, 'sequence': 5}, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('e' * 40, 'b' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('a' * 40, 'e' * 64, receipt, 'd' * 64))

    def test_sequence_six_rejects_other_job_or_record(self):
        from get_publication_provenance import ProvenanceFailure
        record = {'run_id': 456, 'receipt_sequence': 6}
        seq6 = {'record': record, 'job_id': 789}
        with patch.object(entry.recovery._private_bindings, 'value',
                          side_effect=lambda _module, key: seq6 if key == 'SEQ6_BINDING' else {}):
            entry.recovery.verify_original_record(record, 789)
            with self.assertRaises(ProvenanceFailure):
                entry.recovery.verify_original_record(record, 790)
            with self.assertRaises(ProvenanceFailure):
                entry.recovery.verify_original_record({**record, 'receipt_sequence': 5}, 789)

    def test_sequence_seven_receipt_requires_exact_evidence(self):
        record = {'receipt_commit': 'a' * 40, 'receipt_sha256': 'b' * 64,
                  'receipt_sequence': 7, 'policy_sha256': 'c' * 64}
        seq7 = {'record': record}
        seq4 = {'selection': {'receipt_commit': 'f' * 40, 'receipt_sha256': 'f' * 64},
                'sequence': 4, 'source_policy': {'get_policy_sha256': 'f' * 64}}
        with patch.object(entry.recovery._private_bindings, 'value',
                          side_effect=lambda _module, key: seq7 if key == 'SEQ7_BINDING' else seq4), \
                patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {'receipt_commit': 'f' * 40}), \
                patch.object(entry.recovery, 'current_get_policy', return_value='d' * 64):
            receipt = {'sequence': 7, 'policy_sha256': 'c' * 64}
            self.assertTrue(entry.recovery.accepts_receipt('a' * 40, 'b' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('e' * 40, 'b' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('a' * 40, 'e' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt(
                'a' * 40, 'b' * 64, {**receipt, 'sequence': 6}, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('a' * 40, 'b' * 64, receipt, 'e' * 64))

    def test_sequence_seven_rejects_other_job_or_record(self):
        from get_publication_provenance import ProvenanceFailure
        record = {'run_id': 457, 'receipt_sequence': 7, 'record_sha256': 'a' * 64}
        seq7 = {'record': record, 'job_id': 790}
        with patch.object(entry.recovery._private_bindings, 'value',
                          side_effect=lambda _module, key: seq7 if key == 'SEQ7_BINDING' else {}):
            entry.recovery.verify_original_record(record, 790)
            with self.assertRaises(ProvenanceFailure):
                entry.recovery.verify_original_record(record, 791)
            with self.assertRaises(ProvenanceFailure):
                entry.recovery.verify_original_record({**record, 'record_sha256': 'b' * 64}, 790)

    def test_sequence_seven_policy_transition_requires_exact_run_identity(self):
        import get_recurring_controller as controller_module
        old = {'code_sha': 'a' * 40, 'rules_sha256': 'b' * 64, 'get_policy_sha256': 'c' * 64,
               'controller_template_sha256': 'd' * 64, 'stable': 'unchanged'}
        record = {'run_id': 124, 'run_attempt': 3, 'controller_sha': 'e' * 40}
        seq7 = {'record': record, 'event': 'schedule', 'public_policy': old,
                'public_sha256': entry.a.sha(entry.a.encoded(old))}
        new = {**old, 'code_sha': 'f' * 40, 'rules_sha256': '1' * 64, 'get_policy_sha256': '2' * 64}
        run = {'id': 124, 'run_attempt': 3, 'head_sha': 'e' * 40, 'event': 'schedule'}
        def value(_module, key): return seq7 if key == 'SEQ7_BINDING' else {}
        with patch.object(entry.recovery._private_bindings, 'value', side_effect=value), \
                patch.object(entry.recovery.historical_active, 'candidate_policy', side_effect=lambda p, r: (p, False)), \
                patch.object(entry.recovery.idle_recovery, 'candidate_policy', side_effect=lambda p, r: (p, False)), \
                patch.object(entry.recovery, 'old_public_policy', return_value=old), \
                patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {'run_id': 999}), \
                patch.object(entry.recovery, 'current_get_policy', return_value='2' * 64), \
                patch.object(entry, 'rules_sha256', return_value='1' * 64), \
                patch.object(controller_module, 'template_sha256', return_value='d' * 64), \
                patch.object(controller_module, 'historical_template_digests', return_value=()):
            self.assertEqual(entry.recovery.candidate_policy(new, run), (old, True))
            for key, value in (('id', 125), ('run_attempt', 4), ('head_sha', '0' * 40),
                               ('event', 'workflow_dispatch')):
                self.assertFalse(entry.recovery.candidate_policy(new, {**run, key: value})[1])

    def test_sequence_five_policy_transition_requires_exact_run_identity(self):
        import get_recurring_controller as controller_module
        old = {'code_sha': 'a' * 40, 'rules_sha256': 'b' * 64, 'get_policy_sha256': 'c' * 64,
               'controller_template_sha256': 'd' * 64, 'stable': 'unchanged'}
        record = {'run_id': 123, 'run_attempt': 1, 'controller_sha': 'e' * 40}
        seq5 = {'record': record, 'public_policy': old, 'public_sha256': entry.a.sha(entry.a.encoded(old))}
        new = {**old, 'code_sha': 'f' * 40, 'rules_sha256': '1' * 64, 'get_policy_sha256': '2' * 64}
        run = {'id': 123, 'run_attempt': 1, 'head_sha': 'e' * 40, 'event': 'workflow_dispatch'}
        with patch.object(entry.recovery._private_bindings, 'value', return_value=seq5), \
                patch.object(entry.recovery.historical_active, 'candidate_policy', side_effect=lambda p, r: (p, False)), \
                patch.object(entry.recovery.idle_recovery, 'candidate_policy', side_effect=lambda p, r: (p, False)), \
                patch.object(entry.recovery, 'old_public_policy', return_value=old), \
                patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {'run_id': 999}), \
                patch.object(entry.recovery, 'current_get_policy', return_value='2' * 64), \
                patch.object(entry, 'rules_sha256', return_value='1' * 64), \
                patch.object(controller_module, 'template_sha256', return_value='d' * 64), \
                patch.object(controller_module, 'historical_template_digests', return_value=()):
            self.assertEqual(entry.recovery.candidate_policy(new, run), (old, True))
            for key, value in (('id', 124), ('run_attempt', 2), ('head_sha', '0' * 40), ('event', 'schedule')):
                self.assertFalse(entry.recovery.candidate_policy(new, {**run, key: value})[1])


if __name__ == '__main__':
    unittest.main()
