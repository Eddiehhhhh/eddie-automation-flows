"""Exercise a synthetic state-only receipt through the real verifier."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_hourly_duplicate_isolation as fixture

entry = fixture.runtime.entry


class StateOnlyMigration(unittest.TestCase):
    def test_generic_state_only_transition_revalidates_receipts_state_and_registry(self):
        repo = fixture.HourlyDuplicateIsolation()
        repo.setUp()
        self.addCleanup(repo.doCleanups)
        bootstrap_commit, legacy_sha, _ = repo.save_parent()
        repo.git('rm', '-q', entry.r.GET + '/' + entry.n.STATE)
        repo.git('commit', '-qm', 'synthetic bootstrap without canonical registry')
        bootstrap_commit = repo.git('rev-parse', 'HEAD').decode().strip()
        reader = fixture.c.legacy.OfflineGit(repo.root)
        bootstrap = {
            'commit': bootstrap_commit,
            'capture_anchor': repo.evidence.anchor,
            'get_tree': reader.source_get_tree(bootstrap_commit),
            'legacy_sha256': legacy_sha,
            'legacy_managed': 0,
            'legacy_conflicts': 0,
            'legacy_pending': 0,
            'existing_unmanaged': 0,
        }

        notes = {f'55555{i:05d}': fixture.note(f'55555{i:05d}', 'real validator fixture')
                 for i in range(10)}

        preview = {}
        for ident, note in notes.items():
            path = entry.n.new_path(note, ident)
            data = entry.n.render_exact(note, path)
            preview[ident] = {
                'entry': {'sourceVersion': entry.n.raw.raw_source_version(note['updated_at']),
                    'filePath': path,
                    'sourceContentHash': entry.n.raw._source_content_hash(note),
                    'sourceSummaryHash': entry.n.raw._source_content_hash(note),
                    'sourceUnknownHash': entry.n.immutable_unknown_hash(note),
                    'sourceTextFields': sorted(entry.n.text_fields(note)),
                    'origin': 'faithful-created', 'history': []},
                'bytes': data,
                'anchor': {'commit': bootstrap_commit, 'state_sha256': legacy_sha,
                           'blob': entry.r.oid(data)}}

        class Source:
            def enumerate(self):
                return list(notes.values()), True

            def detail_raw(self, ident):
                return fixture.c.legacy.encoded({'data': notes[ident]})

        rules = entry.rules_sha256()
        first, first_files = entry.prepare(reader, repo.evidence, bootstrap_commit,
            Source(), approved_rules_sha256=rules, bootstrap=bootstrap, preview=preview)
        first_receipt = entry.read_receipt(reader, first['receipt_commit'],
                                           first['receipt_sha256'], first['policy_sha256'])
        self.assertEqual(first_receipt['sequence'], 1)
        self.assertTrue(first_receipt['changed_content_ids'])
        self.assertGreater(first_receipt['counts']['new_preview_imported'], 0)

        current, legacy, registry = entry.load_states(reader, repo.evidence,
            first['candidate_commit'], first_receipt['state_sha256'],
            first_receipt['registry_sha256'])
        registry['conflictsById']['9999999999'] = 'source_unknown_fields'
        registry_path = entry.r.GET + '/' + entry.n.STATE
        registry_bytes = entry.a.encoded(registry)
        candidate = entry.n.git_candidate(reader, first['candidate_commit'],
                                          legacy, registry, {}, current)
        candidate_commit = candidate['candidate_commit']
        delta = entry.delta(reader, first['candidate_commit'], candidate_commit)
        counts = dict.fromkeys(set(entry.engine.COUNTS) - {
            'changed_paths', 'notes_changed', 'changed_bytes', 'transaction_bytes'}, 0)
        counts.update(legacy_managed=len(legacy['legacyById']),
                      legacy_conflicts=len(legacy['conflictsById']),
                      legacy_pending=len(legacy['pending_ids']),
                      new_managed=len(registry['newById']),
                      new_pending=len(registry['conflictsById']),
                      existing_unmanaged=sum(v == 'existing_unmanaged'
                                             for v in registry['reserved'].values()),
                      new_state_only=1)
        receipt = deepcopy(first_receipt)
        receipt.update(sequence=first_receipt['sequence'] + 1,
            parent_commit=first['candidate_commit'],
            previous_receipt_commit=first['receipt_commit'],
            previous_receipt_sha256=first['receipt_sha256'],
            before_get_tree=reader.source_get_tree(first['candidate_commit']),
            previous_legacy_sha256=first_receipt['state_sha256'],
            previous_registry_sha256=first_receipt['registry_sha256'],
            registry_sha256=entry.a.sha(registry_bytes),
            delta_sha256=entry.a.sha(entry.a.encoded(delta)),
            changed_content_ids=[], counts=counts,
            engine_payload_sha256=entry.a.sha(entry.a.encoded({'synthetic_state_only': True})),
            source_summary_sha256=entry.a.sha(entry.a.encoded([])),
            detail_receipts_sha256=entry.a.sha(entry.a.encoded({})),
            conflict_reason_counts=entry.n.reason_counts(registry['conflictsById']))
        migration_receipt_bytes = entry.a.encoded(receipt)
        migration, _, _ = entry.add_receipt(reader, first['candidate_commit'],
            candidate,
            {registry_path: registry_bytes}, migration_receipt_bytes)
        self.assertEqual(receipt['delta_sha256'], entry.a.sha(entry.a.encoded(
            entry.delta(reader, first['candidate_commit'], migration))))
        migration_pin = (first['candidate_commit'], migration,
            reader.source_get_tree(first['candidate_commit']), reader.source_get_tree(migration))

        with patch.object(entry, 'PILOT_MIGRATION', migration_pin):
            policy, trusted_receipt, trusted_states = entry.trust_current(reader,
                repo.evidence, migration, rules,
                {'commit': first['receipt_commit'], 'receipt_sha256': first['receipt_sha256']},
                bootstrap=bootstrap, source_id='55555')

        self.assertEqual(trusted_receipt['sequence'], 2)
        self.assertEqual(trusted_receipt['changed_content_ids'], [])
        self.assertEqual(trusted_states[2]['conflictsById']['9999999999'],
                         'source_unknown_fields')
        self.assertEqual(policy, first['policy_sha256'])


if __name__ == '__main__':
    unittest.main()
