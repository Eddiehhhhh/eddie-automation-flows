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

    def test_unbound_historical_tree_transition_has_fixed_diagnostic(self):
        later = 'f' * 40
        reader = Reader({self.root: [], self.linear: [self.root], later: [self.linear]},
                        {self.root: self.tree, self.linear: self.tree, later: self.changed})
        migration = ('8' * 40, '9' * 40, '3' * 40, '4' * 40)
        with patch.object(entry, 'PILOT_MIGRATION', migration):
            with self.assertRaisesRegex(entry.EntryFailure, 'entry_foreign_get_change') as caught:
                entry.unchanged_get_history(reader, self.root, later)
        self.assertEqual(caught.exception.history_check, 'pilot_tree_pair_unmatched')

    def assert_history_check(self, expected, reader, ancestor, descendant, migration):
        with patch.object(entry, 'PILOT_MIGRATION', migration):
            with self.assertRaisesRegex(entry.EntryFailure, 'entry_foreign_get_change') as caught:
                entry.unchanged_get_history(reader, ancestor, descendant)
        self.assertEqual(caught.exception.history_check, expected)

    def test_missing_or_malformed_pilot_binding_has_fixed_diagnostic(self):
        later = 'f' * 40
        reader = Reader({self.root: [], later: [self.root]},
                        {self.root: self.tree, later: self.changed})
        self.assert_history_check('pilot_migration_unconfigured', reader, self.root, later, None)
        self.assert_history_check('pilot_migration_malformed', reader, self.root, later, ())

    def test_pilot_tree_divergence_has_fixed_diagnostic(self):
        middle, later, migration = '5' * 40, '6' * 40, '7' * 40
        reader = Reader({self.root: [], middle: [self.root], later: [middle]},
                        {self.root: self.tree, middle: '3' * 40, later: self.changed})
        self.assert_history_check('pilot_tree_diverged', reader, self.root, later,
                                  (self.root, migration, self.tree, self.changed))

    def test_pilot_parent_mismatch_has_fixed_diagnostic(self):
        other, migration = '5' * 40, '6' * 40
        reader = Reader({self.root: [], self.linear: [self.root], other: [self.root],
                         migration: [other]},
                        {self.root: self.tree, self.linear: self.tree, other: self.tree,
                         migration: self.changed})
        self.assert_history_check('pilot_parent_mismatch', reader, self.root, migration,
                                  (self.root, migration, self.tree, self.changed))

    def test_second_pilot_start_is_rejected_with_fixed_diagnostic(self):
        older_change, middle, later = '5' * 40, '6' * 40, '7' * 40
        reader = Reader({self.root: [], older_change: [self.root], middle: [older_change],
                         later: [middle]},
                        {self.root: self.tree, older_change: self.changed,
                         middle: self.tree, later: self.changed})
        self.assert_history_check('pilot_start_disallowed', reader, self.root, later,
                                  (middle, later, self.tree, self.changed))

    def test_merge_first_parent_get_change_has_fixed_diagnostic(self):
        parents = {self.root: [], self.side: [self.root], self.merge: [self.side, self.root]}
        trees = {self.root: self.tree, self.side: self.changed, self.merge: self.tree}
        self.assert_history_check('merge_first_parent_tree_mismatch', Reader(parents, trees),
                                  self.root, self.merge, None)

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
    def test_only_pinned_pilot_migration_receipt_crosses_policy(self):
        migration = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        seq4 = {'selection': {'receipt_commit': 'f' * 40, 'receipt_sha256': 'f' * 64},
                'sequence': 4, 'source_policy': {'get_policy_sha256': 'f' * 64}}
        def value(module, key):
            if module == 'get_recurring_entry' and key == 'PILOT_MIGRATION': return migration
            if module == 'get_publication_provenance': return seq4
            return {}
        receipt = {'sequence': 8, 'policy_sha256': 'c' * 64}
        with patch.object(entry.recovery._private_bindings, 'value', side_effect=value), \
                patch.object(entry.recovery, 'current_get_policy', return_value='d' * 64), \
                patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {
                    'receipt_commit': 'f' * 40, 'receipt_sha256': 'f' * 64}), \
                patch.object(entry.recovery, 'ORIGINAL_RECEIPT_COMMIT', 'f' * 40), \
                patch.object(entry.recovery.idle_recovery, 'accepts_receipt', return_value=False):
            self.assertTrue(entry.recovery.accepts_receipt('b' * 40, 'e' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('e' * 40, 'e' * 64, receipt, 'd' * 64))
            self.assertFalse(entry.recovery.accepts_receipt('b' * 40, 'e' * 64, receipt, 'f' * 64))

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

    def test_sequence_nine_binding_requires_exact_run_job_record_and_receipt(self):
        from get_publication_provenance import ProvenanceFailure
        import get_recurring_controller as controller_module
        record = {'run_id': 459, 'run_attempt': 1, 'controller_sha': 'e' * 40,
                  'receipt_commit': 'a' * 40, 'receipt_sha256': 'b' * 64,
                  'receipt_sequence': 9, 'policy_sha256': 'c' * 64,
                  'record_sha256': 'f' * 64}
        old = {'code_sha': '1' * 40, 'rules_sha256': '2' * 64,
               'get_policy_sha256': '3' * 64,
               'controller_template_sha256': '4' * 64, 'stable': 'unchanged'}
        seq9 = {'record': record, 'job_id': 791, 'public_policy': old,
                'public_sha256': entry.a.sha(entry.a.encoded(old)), 'event': 'schedule'}
        new = {**old, 'code_sha': '5' * 40, 'rules_sha256': '6' * 64,
               'get_policy_sha256': '7' * 64}
        run = {'id': 459, 'run_attempt': 1, 'head_sha': 'e' * 40, 'event': 'schedule'}

        def value(_module, key):
            if key == 'SEQ9_BINDING': return seq9
            if key == 'FAILED_NOTION_REPLAY_4':
                return {'selection': {'receipt_commit': '8' * 40,
                                      'receipt_sha256': '8' * 64},
                        'sequence': 4, 'source_policy': {'get_policy_sha256': '8' * 64}}
            return {}

        with patch.object(entry.recovery._private_bindings, 'value', side_effect=value), \
             patch.object(entry.recovery.historical_active, 'candidate_policy', return_value=(new, False)), \
             patch.object(entry.recovery.idle_recovery, 'candidate_policy', return_value=(new, False)), \
             patch.object(entry.recovery, 'old_public_policy', return_value=old), \
             patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {
                 'run_id': 999, 'receipt_commit': 'x' * 40,
                 'receipt_sha256': 'x' * 64, 'get_policy_sha256': 'x' * 64}), \
             patch.object(entry.recovery, 'current_get_policy', return_value='7' * 64), \
             patch.object(entry, 'rules_sha256', return_value='6' * 64), \
             patch.object(controller_module, 'template_sha256', return_value='4' * 64), \
             patch.object(entry.recovery.idle_recovery, 'accepts_receipt', return_value=False):
            self.assertEqual(entry.recovery.candidate_policy(new, run), (old, True))
            entry.recovery.verify_original_record(record, 791)
            self.assertTrue(entry.recovery.accepts_receipt(
                'a' * 40, 'b' * 64, {'sequence': 9, 'policy_sha256': 'c' * 64}, '7' * 64))
            self.assertFalse(entry.recovery.accepts_receipt(
                'd' * 40, 'b' * 64, {'sequence': 9, 'policy_sha256': 'c' * 64}, '7' * 64))
            self.assertFalse(entry.recovery.accepts_receipt(
                'a' * 40, '9' * 64, {'sequence': 9, 'policy_sha256': 'c' * 64}, '7' * 64))
            self.assertFalse(entry.recovery.accepts_receipt(
                'a' * 40, 'b' * 64, {'sequence': 8, 'policy_sha256': 'c' * 64}, '7' * 64))
            self.assertFalse(entry.recovery.accepts_receipt(
                'a' * 40, 'b' * 64, {'sequence': 9, 'policy_sha256': '9' * 64}, '7' * 64))
            self.assertFalse(entry.recovery.accepts_receipt(
                'a' * 40, 'b' * 64, {'sequence': 9, 'policy_sha256': 'c' * 64}, '9' * 64))
            with self.assertRaises(ProvenanceFailure):
                entry.recovery.verify_original_record(record, 792)
            with self.assertRaises(ProvenanceFailure):
                entry.recovery.verify_original_record({**record, 'record_sha256': '0' * 64}, 791)
            for key, wrong in (('id', 460), ('run_attempt', 2),
                               ('head_sha', '0' * 40), ('event', 'workflow_dispatch')):
                self.assertFalse(entry.recovery.candidate_policy(
                    new, {**run, key: wrong})[1])

    def test_sequence_seven_policy_transition_requires_exact_run_identity(self):
        import get_recurring_controller as controller_module
        old = {'code_sha': 'a' * 40, 'rules_sha256': 'b' * 64, 'get_policy_sha256': 'c' * 64,
               'controller_template_sha256': '3f7b35a0348f5f0197998bf84c6d1473f5e364a8f4611a2823b6642c9263aadb', 'stable': 'unchanged'}
        record = {'run_id': 124, 'run_attempt': 3, 'controller_sha': 'e' * 40}
        seq7 = {'record': record, 'event': 'schedule', 'public_policy': old,
                'public_sha256': entry.a.sha(entry.a.encoded(old))}
        new = {**old, 'code_sha': 'f' * 40, 'rules_sha256': '1' * 64,
               'get_policy_sha256': '2' * 64, 'controller_template_sha256': 'd' * 64}
        run = {'id': 124, 'run_attempt': 3, 'head_sha': 'e' * 40, 'event': 'schedule'}
        def value(_module, key): return seq7 if key == 'SEQ7_BINDING' else {}
        with patch.object(entry.recovery._private_bindings, 'value', side_effect=value), \
                patch.object(entry.recovery.historical_active, 'candidate_policy', side_effect=lambda p, r: (p, False)), \
                patch.object(entry.recovery.idle_recovery, 'candidate_policy', side_effect=lambda p, r: (p, False)), \
                patch.object(entry.recovery, 'old_public_policy', return_value=old), \
                patch.object(entry.recovery, 'SEQUENCE_TWO_RECORD', {'run_id': 999}), \
                patch.object(entry.recovery, 'current_get_policy', return_value='2' * 64), \
                patch.object(entry, 'rules_sha256', return_value='1' * 64), \
                patch.object(controller_module, 'template_sha256', return_value='d' * 64):
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

    def test_exact_state_only_transition_verifies_receipt_and_advances_state_anchor(self):
        anchor, before, migration, parent = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        before_tree, after_tree = '1' * 40, '2' * 40
        migration_data = (before, migration, before_tree, after_tree)
        parents = {anchor: [], before: [anchor], migration: [before], parent: [migration]}
        trees = {anchor: before_tree, before: before_tree, migration: after_tree, parent: after_tree}
        reader = Reader(parents, trees)
        raw_receipt = b'bound seq8 receipt'
        reader.read = lambda commit, path, limit: raw_receipt if commit == migration and path == entry.FULL_RECEIPT else None
        previous = {'sequence': 7, 'state_sha256': '3' * 64, 'registry_sha256': '4' * 64}
        next_receipt = {'sequence': 8, 'parent_commit': before,
                        'previous_receipt_commit': anchor,
                        'previous_receipt_sha256': 'f' * 64,
                        'state_sha256': '5' * 64, 'registry_sha256': '6' * 64,
                        'changed_content_ids': [],
                        'counts': dict.fromkeys(('new_created', 'new_preview_imported',
                                                 'new_updated', 'legacy_source_updated'), 0)}
        old_states, new_states = ({'old': 1}, {'old': 2}, {'old': 3}), ({'new': 1}, {'new': 2}, {'new': 3})
        bootstrap = {'commit': 'e' * 40}
        anchor_value = {'commit': anchor, 'receipt_sha256': 'f' * 64}
        with patch.object(entry, 'PILOT_MIGRATION', migration_data), \
                patch.object(entry, 'rules_sha256', return_value='7' * 64), \
                patch.object(entry, 'policy_sha256', return_value='8' * 64), \
                patch.object(entry, 'bootstrap_states', return_value=old_states), \
                patch.object(entry, 'verify_trusted_publication', side_effect=[(previous, old_states), (next_receipt, new_states)]) as verify, \
                patch.object(entry, 'load_states', return_value=new_states) as load:
            result = entry.trust_current(reader, object(), parent, '7' * 64, anchor_value,
                                         bootstrap=bootstrap)
        self.assertEqual(result, ('8' * 64, next_receipt, new_states))
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(verify.call_args_list[1].args[2], {
            'commit': migration, 'receipt_sha256': entry.a.sha(raw_receipt)})
        self.assertEqual(load.call_args.args[2:5],
                         (parent, next_receipt['state_sha256'], next_receipt['registry_sha256']))

    def test_state_only_transition_rejects_content_write_receipt(self):
        anchor, before, migration, parent = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        before_tree, after_tree = '1' * 40, '2' * 40
        migration_data = (before, migration, before_tree, after_tree)
        reader = Reader({anchor: [], before: [anchor], migration: [before], parent: [migration]},
                        {anchor: before_tree, before: before_tree, migration: after_tree, parent: after_tree})
        reader.read = lambda *args: b'bound migration receipt'
        previous = {'sequence': 7, 'state_sha256': '3' * 64, 'registry_sha256': '4' * 64}
        content_receipt = {'sequence': 8, 'parent_commit': before,
                           'previous_receipt_commit': anchor,
                           'previous_receipt_sha256': 'f' * 64,
                           'changed_content_ids': ['1234567890'],
                           'counts': {'new_created': 1, 'new_preview_imported': 0,
                                      'new_updated': 0, 'legacy_source_updated': 0}}
        with patch.object(entry, 'PILOT_MIGRATION', migration_data), \
                patch.object(entry, 'rules_sha256', return_value='7' * 64), \
                patch.object(entry, 'policy_sha256', return_value='8' * 64), \
                patch.object(entry, 'bootstrap_states', return_value=({}, {}, {})), \
                patch.object(entry, 'verify_trusted_publication', side_effect=[(previous, ({}, {}, {})), (content_receipt, ({}, {}, {}))]), \
                patch.object(entry, 'load_states') as load:
            with self.assertRaisesRegex(entry.EntryFailure, 'entry_receipt_anchor'):
                entry.trust_current(reader, object(), parent, '7' * 64,
                                    {'commit': anchor, 'receipt_sha256': 'f' * 64},
                                    bootstrap={'commit': 'e' * 40})
        load.assert_not_called()

    def test_invalid_state_only_receipt_stops_before_loading_current_state(self):
        anchor, before, migration, parent = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        before_tree, after_tree = '1' * 40, '2' * 40
        reader = Reader({anchor: [], before: [anchor], migration: [before], parent: [migration]},
                        {anchor: before_tree, before: before_tree, migration: after_tree, parent: after_tree})
        reader.read = lambda *args: b'bad transition receipt'
        previous = {'sequence': 7, 'state_sha256': '3' * 64, 'registry_sha256': '4' * 64}
        with patch.object(entry, 'PILOT_MIGRATION', (before, migration, before_tree, after_tree)), \
                patch.object(entry, 'rules_sha256', return_value='7' * 64), \
                patch.object(entry, 'policy_sha256', return_value='8' * 64), \
                patch.object(entry, 'bootstrap_states', return_value=({}, {}, {})), \
                patch.object(entry, 'verify_trusted_publication', side_effect=[(previous, ({}, {}, {})), entry.EntryFailure('entry_foreign_delta')]), \
                patch.object(entry, 'load_states') as load:
            with self.assertRaisesRegex(entry.EntryFailure, 'entry_foreign_delta'):
                entry.trust_current(reader, object(), parent, '7' * 64,
                                    {'commit': anchor, 'receipt_sha256': 'f' * 64},
                                    bootstrap={'commit': 'e' * 40})
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
