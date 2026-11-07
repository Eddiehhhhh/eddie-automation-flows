"""Single-note publication with a pre-existing, non-generated owner file."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_hourly_duplicate_isolation as fixture

entry = fixture.runtime.entry
canon = fixture.c


class SingleTransitionIsolation(unittest.TestCase):
    def setUp(self):
        repo = self.repo = fixture.HourlyDuplicateIsolation()
        repo.setUp()
        self.addCleanup(repo.doCleanups)
        old = fixture.note('11111')
        self.rel = canon.new_path(old, '11111')
        generated = canon.render_exact(old, self.rel)
        repo.owner_rows['11111'] = canon.record(old, old, self.rel, generated)
        repo.current[self.rel] = generated
        _, self.lsha, self.rsha = repo.save_parent()
        # The source ID still identifies this note. Its newer header uses YAML,
        # whereas the stored generator baseline uses JSON field values.
        self.preserved = b'---\nnote_id: 11111\nnote_type: plain_text\n---\nprior edit\n'
        self.parent = self.commit_edit(self.rel, self.preserved)
        self.reader = canon.legacy.OfflineGit(repo.root)
        self.before = entry.load_states(self.reader, repo.evidence, self.parent,
                                        self.lsha, self.rsha, source_id='55555')
        self.target = '55555'
        source_note = fixture.note(self.target, 'new via real Git candidate')
        class Source:
            def enumerate(self, ident):
                assert ident == source_note['note_id']
                return [source_note], {'contract': 'get-single-note-source-scope-1',
                                       'note_id': ident, 'complete_catalog': False}
            def detail_raw(self, ident):
                assert ident == source_note['note_id']
                return canon.legacy.encoded({'data': source_note})
        self.source = Source()

    def commit_edit(self, rel, data):
        path = self.repo.root / entry.r.GET / rel
        path.write_bytes(data)
        self.repo.git('add', '.')
        self.repo.git('commit', '-qm', 'edit synthetic input')
        return self.repo.git('rev-parse', 'HEAD').decode().strip()

    def prepare_candidate(self):
        plan, _ = entry.engine.prepare(self.reader, self.repo.evidence, self.parent,
            self.before[0], self.source, legacy_sha=self.lsha, registry_sha=self.rsha,
            source_id=self.target)
        after = entry.load_states(self.reader, self.repo.evidence, plan['candidate_commit'],
            plan['state_sha256'], plan['registry_sha256'], source_id=self.target)
        return plan['candidate_commit'], after

    def test_prepare_and_reverify_own_receipt_independent_of_later_selected_id(self):
        # The earlier externally trusted checkpoint is supplied by this fixture.
        # The newly produced commit, receipt, entire delta and readback are real.
        bootstrap = {'commit': self.parent}
        rules = entry.rules_sha256()
        policy = entry.policy_sha256(rules, bootstrap)
        previous = {'sequence': 4, 'state_sha256': self.lsha, 'registry_sha256': self.rsha}
        anchor = {'commit': self.parent, 'receipt_sha256': 'a' * 64}
        original = entry.read_receipt
        def receipt(reader, commit, digest, approved):
            if commit == self.parent and digest == anchor['receipt_sha256']:
                return previous
            return original(reader, commit, digest, approved)
        with patch.object(entry, 'trust_current', return_value=(policy, previous, self.before)), \
             patch.object(entry, 'bootstrap_states', return_value=self.before), \
             patch.object(entry, 'read_receipt', side_effect=receipt):
            plan, files = entry.prepare(self.reader, self.repo.evidence, self.parent,
                self.source, approved_rules_sha256=rules, anchor=anchor,
                bootstrap=bootstrap, source_id=self.target)
            self.assertEqual(plan['proposed_content_ids'], [self.target])
            self.assertNotIn(entry.r.GET + '/' + self.rel, files)
            proof = {'commit': plan['candidate_commit'], 'receipt_sha256': plan['receipt_sha256']}
            for next_target in (None, '11111', self.target):
                obj, after = entry.verify_trusted_publication(self.reader, self.repo.evidence,
                    proof, policy, bootstrap, source_id=next_target)
                self.assertEqual(obj['changed_content_ids'], [self.target])
                self.assertEqual(after[0][self.rel], self.preserved)
                self.assertEqual(after[2]['newById']['11111'], self.before[2]['newById']['11111'])

    def test_baseline_reads_are_scoped_and_corrupt_or_missing_target_cannot_write(self):
        with patch.object(self.reader, 'baseline', side_effect=AssertionError('unrelated baseline')):
            self.prepare_candidate()
        old = fixture.note('11111')
        scope = {'contract': 'get-single-note-source-scope-1', 'note_id': '11111',
                 'complete_catalog': False}
        for target, terminal in (('11111', scope), (None, True)):
            with self.subTest(target=target), patch.object(self.reader, 'baseline',
                    return_value=b'invalid header') as fetch, self.assertRaises(canon.raw.ParityFailure):
                canon.prepare(self.reader, self.parent, self.repo.evidence, self.lsha, self.rsha,
                              [old], terminal, {}, only_id=target)
            self.assertEqual(fetch.call_args.args[1], self.rel)
        with patch.object(self.reader, 'baseline', side_effect=canon.raw.ParityFailure('baseline_missing')):
            plan, states, _ = canon.prepare(self.reader, self.parent, self.repo.evidence,
                self.lsha, self.rsha, [old], scope, {}, only_id='11111')
        self.assertEqual(states['registry']['conflictsById']['11111'], 'baseline_missing')
        self.assertNotIn(self.rel, [change['path'] for change in plan['changes']])

    def test_only_verified_recurring_entry_accepts_its_one_receipt_path(self):
        path = entry.r.GET + '/one.md'
        plan = {'single_source_id': self.target, 'single_source_path': path,
                'source_scope': 'single_note', 'catalog_complete': False,
                'changes': [{'path': path}, {'path': entry.FULL_RECEIPT}]}
        with self.assertRaises(entry.m.ManualFailure):
            entry.engine.verify_single_scope(plan)
        entry.engine.verify_single_scope(plan, with_recurring_receipt=True)
        for extra in (entry.FULL_RECEIPT + '.bak', entry.r.GET + '/other.md'):
            with self.subTest(extra=extra), self.assertRaises(entry.m.ManualFailure):
                entry.engine.verify_single_scope({**plan, 'changes': plan['changes'] + [{'path': extra}]},
                                                 with_recurring_receipt=True)

    def test_full_target_state_only_and_forged_registry_or_file_fail(self):
        candidate, after = self.prepare_candidate()
        def check(tip, states, scope):
            return entry.transition(self.reader, self.repo.evidence, self.parent, tip,
                                    self.before, states, source_id=scope)
        with self.assertRaises(canon.raw.ParityFailure):
            check(candidate, after, None)
        with self.assertRaises(canon.raw.ParityFailure):
            check(candidate, after, '11111')
        with self.assertRaises(canon.raw.ParityFailure):
            check(self.parent, self.before, self.target)
        for change in ('old_row', 'new_hash', 'unrelated_bytes', 'new_owner'):
            bad = deepcopy(after)
            if change == 'old_row':
                bad[2]['newById']['11111']['mirror_sha256'] = '0' * 64
            elif change == 'new_hash':
                bad[2]['newById'][self.target]['mirror_sha256'] = '0' * 64
            elif change == 'unrelated_bytes':
                bad[0][self.rel] += b'forged'
            else:
                bad[2]['newById']['88888'] = {**bad[2]['newById'][self.target], 'filePath': 'another.md'}
            with self.subTest(change=change), self.assertRaises(canon.raw.ParityFailure):
                check(candidate, bad, self.target)
        self.repo.git('checkout', '-q', '--detach', candidate)
        altered = self.commit_edit(self.rel, self.preserved + b'changed during publication')
        with self.assertRaises(canon.raw.ParityFailure):
            check(altered, after, self.target)

    def test_full_sync_preserves_only_unchanged_reported_local_conflict(self):
        candidate, after = self.prepare_candidate()
        held = deepcopy(after)
        for reason in ('local_note_edited', 'source_missing', 'identity_reserved'):
            with self.subTest(reason=reason):
                held[2]['conflictsById']['11111'] = reason
                self.assertEqual(entry.transition(self.reader, self.repo.evidence, self.parent, candidate,
                                                 self.before, held), [self.target])
        for change in ('row', 'bytes', 'missing_conflict'):
            bad = deepcopy(held)
            if change == 'row':
                bad[2]['newById']['11111']['mirror_sha256'] = '0' * 64
            elif change == 'bytes':
                bad[0][self.rel] += b'changed'
            else:
                del bad[2]['conflictsById']['11111']
            with self.subTest(change=change), self.assertRaises(canon.raw.ParityFailure):
                entry.transition(self.reader, self.repo.evidence, self.parent, candidate,
                                 self.before, bad)


if __name__ == '__main__':
    unittest.main()
