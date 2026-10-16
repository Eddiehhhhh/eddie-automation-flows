"""Offline recurring entry: real Git trees and the shared simulated CAS publisher."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_recurring_entry as e
from get_recurring_entry import engine, a, m, n, r
import test_legacy_manual as fixture


class RecurringEntryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CanonicalPrepareTests(methodName='test_fixed_preview_only_exact_candidate_preserves_every_existing_path')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.f.summaries = [v for v in self.f.f.summaries if v.get('id') != '77']
        self.reader = self.f.reader; self.evidence = self.f.evidence; self.parent = self.f.parent
        registry = n.initial_registry(self.evidence, self.f.legacy, self.f.sha, self.f.current)
        self.bootstrap = {'commit': self.parent, 'get_tree': self.reader.source_get_tree(self.parent),
                          'legacy_sha256': self.f.sha, 'capture_anchor': self.evidence.anchor,
                          'legacy_managed': len(self.f.legacy['legacyById']),
                          'legacy_conflicts': len(self.f.legacy['conflictsById']),
                          'legacy_pending': len(self.f.legacy['pending_ids']),
                          'existing_unmanaged': sum(v == 'existing_unmanaged' for v in registry['reserved'].values())}
        self.rules = e.rules_sha256()

    def prepare(self, parent=None, anchor=None, source=None, trigger='manual'):
        return e.prepare(self.reader, self.evidence, parent or self.parent, source or self.f.source(),
                         approved_rules_sha256=self.rules, anchor=anchor,
                         preview=self.f.preview if anchor is None else None,
                         bootstrap=self.bootstrap, trigger=trigger)

    def anchor(self, plan):
        # A test-only substitute for the independently read-back publication
        # result. Real code must never obtain external trust by reading this repo.
        return {'commit': plan['candidate_commit'], 'receipt_sha256': plan['receipt_sha256']}

    def commit_change(self, parent, values):
        self.f.f.git('checkout', '-q', '--detach', parent)
        for path, data in values.items():
            target = self.f.f.repo / path
            if data is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        return self.f.f.commit('Synthetic foreign or archive change')

    def check(self, plan, files):
        e.verify_plan(self.reader, self.evidence, plan, files, self.rules, bootstrap=self.bootstrap)

    def test_bootstrap_one_direct_candidate_contains_receipt_preserves_refs_and_index(self):
        refs = self.f.f.git('show-ref'); index = self.f.f.git('ls-files', '--stage')
        plan, files = self.prepare()
        self.assertEqual(plan['counts']['new_preview_imported'], 2)
        self.assertEqual(len(files), 4)
        self.assertEqual(e.single_parent(self.reader, plan['candidate_commit']), self.parent)
        self.assertEqual(plan['proposed_content_ids'], ['200', '201'])
        receipt = json.loads(files[e.FULL_RECEIPT])
        self.assertEqual(receipt['sequence'], 1)
        self.assertEqual(receipt['previous_legacy_sha256'], self.f.sha)
        self.assertEqual(receipt['previous_receipt_commit'], self.parent)
        self.assertEqual(self.f.f.git('show-ref'), refs); self.assertEqual(self.f.f.git('ls-files', '--stage'), index)
        self.check(plan, files)

    def test_manual_and_schedule_are_same_engine_same_payload(self):
        manual, files = self.prepare(trigger='manual')
        scheduled, other = self.prepare(trigger='schedule')
        self.assertEqual(manual, scheduled); self.assertEqual(files, other)

    def test_noop_has_no_new_snapshot_or_receipt_or_sink_ids(self):
        first, _ = self.prepare(); source = self.f.source()
        plan, files = self.prepare(first['candidate_commit'], self.anchor(first), source)
        self.assertEqual(files, {}); self.assertEqual(plan['candidate_commit'], first['candidate_commit'])
        self.assertEqual(source.ids, []); self.assertEqual(source.lists, 1)
        result = e.confirmed_private_handoff(self.reader, self.evidence, plan, files,
            {'main_updated': False, 'cas_attempts': 0, 'readback_verified': True}, self.rules, bootstrap=self.bootstrap)
        self.assertEqual(result['changed_content_ids'], [])

    def test_no_external_anchor_cannot_self_bootstrap_from_current_state(self):
        first, _ = self.prepare(); source = self.f.source()
        with self.assertRaisesRegex(e.EntryFailure, 'entry_foreign_get_change'):
            self.prepare(first['candidate_commit'], source=source)
        self.assertEqual(source.lists, 0); self.assertEqual(source.ids, [])

    def test_rules_and_bootstrap_fail_before_any_source_read(self):
        for changes in ({'get_tree': 'a' * 40}, {'legacy_managed': 1152}):
            source = self.f.source()
            with self.assertRaises(a.ParityFailure):
                e.prepare(self.reader, self.evidence, self.parent, source, approved_rules_sha256=self.rules,
                          preview=self.f.preview, bootstrap={**self.bootstrap, **changes})
            self.assertEqual(source.lists, 0)
        source = self.f.source()
        with self.assertRaisesRegex(e.EntryFailure, 'entry_rules_changed'):
            e.prepare(self.reader, self.evidence, self.parent, source, approved_rules_sha256='f' * 64,
                      preview=self.f.preview, bootstrap=self.bootstrap)
        self.assertEqual(source.lists, 0)

    def test_unrelated_archive_advance_is_preserved_and_receipt_parent_is_current(self):
        parent = self.commit_change(self.parent, {'unrelated.txt': b'Advanced archive'})
        plan, files = self.prepare(parent)
        self.assertEqual(self.reader.read(plan['candidate_commit'], 'unrelated.txt'), b'Advanced archive')
        self.assertEqual(plan['parent_commit'], parent)
        self.assertEqual(json.loads(files[e.FULL_RECEIPT])['parent_commit'], parent)
        self.assertNotIn('unrelated.txt', files)
        archive2 = self.commit_change(plan['candidate_commit'], {'unrelated.txt': b'Another archive'})
        again, writes = self.prepare(archive2, self.anchor(plan))
        self.assertEqual(writes, {}); self.assertEqual(again['candidate_commit'], archive2)
        self.assertEqual(again['receipt_commit'], plan['candidate_commit'])
        self.assertEqual(e.public_report(again, writes)['receipt_commit'], plan['candidate_commit'])
        self.check(again, writes)

    def test_foreign_note_state_receipt_and_asset_changes_stop_before_source(self):
        first, files = self.prepare()
        for path, data in [(r.GET + '/1.md', b'Foreign note'),
                           (r.GET + '/' + n.STATE, b'Foreign registry'),
                           (e.FULL_RECEIPT, b'Foreign receipt'),
                           (r.GET + '/assets/new.bin', b'Foreign asset')]:
            parent = self.commit_change(first['candidate_commit'], {path: data})
            source = self.f.source()
            with self.assertRaisesRegex(e.EntryFailure, 'entry_foreign_get_change'):
                self.prepare(parent, self.anchor(first), source)
            self.assertEqual(source.lists, 0)

    def test_edit_then_revert_is_still_a_foreign_get_change(self):
        first, _ = self.prepare(); path = r.GET + '/1.md'
        original = self.reader.read(first['candidate_commit'], path)
        edited = self.commit_change(first['candidate_commit'], {path: b'Foreign'})
        reverted = self.commit_change(edited, {path: original})
        self.assertEqual(self.reader.source_get_tree(first['candidate_commit']), self.reader.source_get_tree(reverted))
        with self.assertRaisesRegex(e.EntryFailure, 'entry_foreign_get_change'):
            self.prepare(reverted, self.anchor(first))

    def test_receipt_rehash_does_not_replace_independent_anchor(self):
        first, files = self.prepare(); receipt = json.loads(files[e.FULL_RECEIPT]); receipt['state_sha256'] = 'c' * 64
        bad = self.commit_change(first['candidate_commit'], {e.FULL_RECEIPT: a.encoded(receipt)})
        with self.assertRaisesRegex(e.EntryFailure, 'entry_receipt_anchor'):
            self.prepare(bad, {'commit': bad, 'receipt_sha256': first['receipt_sha256']})

    def test_complete_delta_catches_same_transaction_foreign_write(self):
        first, files = self.prepare()
        values = {**files, 'unrelated.txt': b'Unexpected writer-side archive edit'}
        forged = self.commit_change(self.parent, values)
        # Even if an external caller incorrectly pinned the forged commit, the
        # unchanged receipt cannot bless an unrecorded path in its transaction.
        with self.assertRaisesRegex(e.EntryFailure, 'entry_foreign_delta'):
            self.prepare(forged, {'commit': forged, 'receipt_sha256': first['receipt_sha256']})

    def test_second_content_change_uses_chain_and_selected_details_only(self):
        first, _ = self.prepare(); notes = deepcopy(self.f.notes)
        notes['200']['content'] = 'Changed exact text'; source = self.f.source(notes)
        plan, files = self.prepare(first['candidate_commit'], self.anchor(first), source)
        receipt = json.loads(files[e.FULL_RECEIPT])
        self.assertEqual(receipt['sequence'], 2); self.assertEqual(receipt['previous_receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(source.ids, ['200']); self.assertEqual(plan['proposed_content_ids'], ['200'])
        third, values = self.prepare(plan['candidate_commit'], self.anchor(plan), self.f.source(notes))
        self.assertEqual(values, {})

    def test_state_only_receipt_has_no_content_ids_and_no_sink_enqueue(self):
        first, _ = self.prepare(); notes = deepcopy(self.f.notes); notes['200']['version'] = 1
        plan, files = self.prepare(first['candidate_commit'], self.anchor(first), self.f.source(notes))
        self.assertEqual(set(files), {r.GET + '/' + n.STATE, e.FULL_RECEIPT})
        self.assertEqual(plan['counts']['new_state_only'], 1); self.assertEqual(plan['proposed_content_ids'], [])
        handoff = e.confirmed_private_handoff(self.reader, self.evidence, plan, files,
            {'main_updated': True, 'cas_attempts': 1, 'readback_verified': True}, self.rules, bootstrap=self.bootstrap)
        self.assertEqual(handoff['changed_content_ids'], [])

    def test_bounded_rotation_and_pending_retention_remain_in_engine(self):
        first, _ = self.prepare(); notes = deepcopy(self.f.notes)
        for i in range(300, 355):
            ident = str(i); notes[ident] = {**notes['201'], 'note_id': ident, 'content': 'Synthetic ' + ident}
        notes['300']['web_page'] = {'unsupported_nested': 'Unrepresentable source field'}
        source = self.f.source(notes); second, _ = self.prepare(first['candidate_commit'], self.anchor(first), source)
        self.assertEqual(second['counts']['new_selected'], 50); self.assertEqual(second['counts']['new_remaining'], 5)
        self.assertEqual(second['counts']['new_pending'], 1); self.assertEqual(len(source.ids), 50)
        third, _ = self.prepare(second['candidate_commit'], self.anchor(second), self.f.source(notes))
        self.assertEqual(third['counts']['new_managed'], 56); self.assertEqual(third['counts']['new_pending'], 1)
        self.assertEqual(third['counts']['legacy_pending'], 9)

    def test_public_output_contains_counts_hashes_only_and_no_confirmed_ids(self):
        plan, files = self.prepare(); report = e.public_report(plan, files); text = json.dumps(report)
        self.assertEqual(report['notes_changed'], 2); self.assertEqual(report['confirmed_content_ids'], 0)
        self.assertFalse(report['main_updated']); self.assertFalse(report['schedule_enabled'])
        for private in ('Readable summary', 'Recording', 'changed_content_ids', r.GET, 'proposed_content_ids'):
            self.assertNotIn(private, text)

    def test_verified_publisher_one_cas_and_only_after_readback_private_handoff(self):
        plan, files = self.prepare(); f = fixture.ManualTests(methodName='test_no_source_change_adopts_only_state_without_claiming_updates')
        f.setUp(); self.addCleanup(f.doCleanups); f.f = self.f.f; f.parent = self.parent
        remote = f.remote(self.parent, 'uncertain_stale')
        result = m.publish(self.reader, remote, plan, files, a.sha(a.encoded(plan)),
            lambda commit, values: dict(values), sleep=lambda _: None, writers_guard=lambda: None,
            checkpoint=lambda _: None, verifier=lambda rd, p, fs: e.verify_plan(rd, self.evidence, p, fs, self.rules, bootstrap=self.bootstrap))
        self.assertEqual(len(remote.calls), 1)
        private = e.confirmed_private_handoff(self.reader, self.evidence, plan, files, result, self.rules, bootstrap=self.bootstrap)
        self.assertEqual(private['changed_content_ids'], ['200', '201'])
        for bad in ({}, {'main_updated': True, 'cas_attempts': 1, 'readback_verified': False},
                    {'main_updated': False, 'cas_attempts': 0, 'readback_verified': True}):
            with self.assertRaisesRegex(e.EntryFailure, 'entry_publication_unconfirmed'):
                e.confirmed_private_handoff(self.reader, self.evidence, plan, files, bad, self.rules, bootstrap=self.bootstrap)

    def test_readback_failure_never_produces_confirmed_handoff(self):
        plan, files = self.prepare(); f = fixture.ManualTests(methodName='test_no_source_change_adopts_only_state_without_claiming_updates')
        f.setUp(); self.addCleanup(f.doCleanups); f.f = self.f.f; f.parent = self.parent; remote = f.remote(self.parent)
        with self.assertRaises(m.ManualFailure):
            m.publish(self.reader, remote, plan, files, a.sha(a.encoded(plan)), lambda commit, values: {},
                sleep=lambda _: None, writers_guard=lambda: None, checkpoint=lambda _: None,
                verifier=lambda rd, p, fs: e.verify_plan(rd, self.evidence, p, fs, self.rules, bootstrap=self.bootstrap))
        self.assertEqual(len(remote.calls), 1)

    def test_cli_is_local_only_no_apply_and_template_is_inactive(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / 'scripts/get_recurring_entry.py').read_text()
        template = root / '.github/workflow-templates/get-canonical-hourly.yml.disabled'
        self.assertTrue(template.exists()); text = template.read_text()
        self.assertIn('17 * * * *', text); self.assertIn('if: ${{ false }}', text)
        self.assertNotIn('\n  schedule:', text); self.assertNotIn('secrets.', text)
        self.assertNotIn("add_argument('--apply'", script)
        self.assertFalse((root / '.github/workflows/get-canonical-hourly.yml').exists())
        self.assertNotIn('load_credentials(', script)
        self.assertNotIn('m.Remote(', script)

    def test_snapshot_details_missing_is_not_silently_a_404(self):
        source = e.SnapshotSource({'summaries': [], 'terminal': True, 'details': {}})
        with self.assertRaisesRegex(e.EntryFailure, 'entry_source_detail_missing'):
            source.detail_raw('200')
        source = e.SnapshotSource({'summaries': [], 'terminal': True, 'details': {'200': None}})
        with self.assertRaisesRegex(RuntimeError, 'GetNote HTTP 404'):
            source.detail_raw('200')

    def test_separate_notion_journal_is_preserved_outside_get(self):
        first, _ = self.prepare()
        parent = self.commit_change(first['candidate_commit'], {'.sync/get-notion/state.json': b'{"synthetic":true}\n'})
        notes = deepcopy(self.f.notes); notes['200']['content'] = 'Later content'
        plan, _ = self.prepare(parent, self.anchor(first), self.f.source(notes))
        self.assertEqual(self.reader.read(plan['candidate_commit'], '.sync/get-notion/state.json'), b'{"synthetic":true}\n')

    def test_404_keeps_original_nine_and_separate_new_conflict_without_sink_ids(self):
        first, _ = self.prepare(); notes = deepcopy(self.f.notes); notes['200']['content'] = 'Changed but unavailable'
        source = self.f.source(notes); source.detail_raw = lambda ident: (_ for _ in ()).throw(RuntimeError('GetNote HTTP 404'))
        plan, files = self.prepare(first['candidate_commit'], self.anchor(first), source)
        self.assertEqual(plan['counts']['legacy_pending'], 9)
        self.assertEqual(plan['counts']['details_unavailable'], 1)
        self.assertEqual(plan['counts']['new_pending'], 1)
        self.assertEqual(plan['proposed_content_ids'], [])
        self.assertEqual(set(files), {r.GET + '/' + n.STATE, e.FULL_RECEIPT})

    def test_inherited_missing_asset_is_preserved_without_materialization(self):
        path = r.GET + '/assets/inherited.bin'; blob = self.reader.tree(self.parent)[path][2]
        obj = self.f.f.repo / '.git/objects' / blob[:2] / blob[2:]
        obj.unlink(); plan, _ = self.prepare()
        self.assertFalse(obj.exists())
        self.assertEqual(self.reader.tree(plan['candidate_commit'])[path], ('100644', 'blob', blob))

    def test_receipt_only_snapshot_is_rejected(self):
        first, files = self.prepare(); receipt = json.loads(files[e.FULL_RECEIPT])
        receipt.update(sequence=2, parent_commit=first['candidate_commit'], previous_receipt_commit=first['candidate_commit'],
                       previous_receipt_sha256=first['receipt_sha256'], previous_legacy_sha256=first['state_sha256'],
                       previous_registry_sha256=first['registry_sha256'], before_get_tree=self.reader.source_get_tree(first['candidate_commit']),
                       delta_sha256=a.sha(a.encoded([])), changed_content_ids=[])
        raw = a.encoded(receipt); forged = self.commit_change(first['candidate_commit'], {e.FULL_RECEIPT: raw})
        with self.assertRaisesRegex(e.EntryFailure, 'entry_receipt_only_commit'):
            self.prepare(forged, {'commit': forged, 'receipt_sha256': a.sha(raw)})

    def test_public_report_rejects_private_or_malformed_injected_fields(self):
        plan, files = self.prepare()
        for bad in ({**plan, 'counts': {**plan['counts'], 'private source title': 1}},
                    {**plan, 'candidate_commit': 'private title'},
                    {**plan, 'receipt_sha256': 'private note'}):
            report = e.public_report(bad, files)
            self.assertEqual(report['status'], 'recurring_local_candidate_failed')
            self.assertNotIn('private', json.dumps(report))

    def test_local_cli_writes_private_artifacts_and_public_counts_only(self):
        snapshot = self.f.f.base / 'source.json'; output = self.f.f.base / 'entry-output'
        notes = {**self.f.f.notes, **self.f.notes}
        snapshot.write_bytes(a.encoded({'summaries': self.f.f.summaries + list(self.f.notes.values()), 'terminal': True, 'details': notes}))
        original = e.prepare
        def prepared(*args, **kwargs):
            return original(*args, **kwargs, bootstrap=self.bootstrap)
        stdout = io.StringIO(); refs = self.f.f.git('show-ref')
        with patch.object(e, 'BOOTSTRAP', self.bootstrap), patch.object(e, 'prepare', side_effect=prepared), patch.object(n, 'preview_records', return_value=self.f.preview), contextlib.redirect_stdout(stdout):
            result = e.main(['--repo', str(self.f.f.repo), '--parent', self.parent, '--source-snapshot', str(snapshot),
                             '--output', str(output), '--approved-rules-sha256', self.rules, '--trigger', 'schedule'])
        self.assertEqual(result, 0); report = json.loads(stdout.getvalue())
        self.assertEqual(report['status'], 'recurring_local_candidate_verified')
        self.assertEqual({p.name for p in output.iterdir()}, {'plan.json', 'state.json'})
        self.assertEqual((output / 'plan.json').stat().st_mode & 0o077, 0)
        self.assertEqual(json.loads((output / 'state.json').read_text())['confirmed_content_ids'], [])
        self.assertEqual(self.f.f.git('show-ref'), refs)
        self.assertNotIn('Readable summary', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
