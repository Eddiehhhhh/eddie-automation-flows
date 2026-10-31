"""Staged duplicate snapshots must not become independent memory candidates."""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import iteration_incremental_audit as audit
import iteration_source_discovery as discovery


class StageCandidateTests(unittest.TestCase):
    def test_stage_body_and_dates_do_not_create_duplicate_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative, stage in [
                ('Raw/02 Notion/flomo-stage/current/memo.md', True),
                ('Raw/02 Notion/flomo-stage/versions/memo/old/memo.md', True),
                ('Raw/01 Flomo/2026/2026-11/memo.md', False),
            ]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('---\ncreated_at: 2026-11-01\nupdated_at: 2026-11-01\nmemo_id: synthetic\n'
                                + ('stage_only: true\n' if stage else '')
                                + '---\nSubstantial synthetic personal source text.\n')
            with mock.patch.object(audit, 'ROOT', root), mock.patch.object(discovery, 'ROOT', root), mock.patch.object(discovery, 'RAW_DIR', root / 'Raw'), mock.patch.object(discovery, 'REGISTRY', root / 'Schema/sources.md'), mock.patch.object(discovery, 'DEFAULT_OUTPUT', root / 'Raw/00 Meta/discovery.json'):
                result = audit.audit(date(2026, 11, 1), 1, 80)
                self.assertEqual(result['counts']['total_candidates'], 1)
                self.assertEqual(result['candidates'][0]['source'], 'Raw/01 Flomo/')
                self.assertEqual(result['counts']['per_source']['Raw/02 Notion/'], 0)
                inventory = discovery.build_record('Raw/02 Notion/', True)
                self.assertEqual(inventory.file_count, 2)
                self.assertEqual(inventory.markdown_file_count, 2)

    def test_stage_exclusion_precedes_path_and_mtime_fallback(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(audit, 'ROOT', Path(folder)):
            path = Path(folder) / '2026-11-01-synthetic.md'
            path.write_text('---\nstage_only: true\n---\nPersonal body without date metadata.\n')
            with mock.patch.object(audit, 'parse_date') as date_parser:
                self.assertIsNone(audit.candidate_from_file(path, 'Raw/02 Notion/', date(2026,11,1), date(2026,11,1)))
                date_parser.assert_not_called()

    def test_explicit_false_remains_eligible(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(audit, 'ROOT', Path(folder)):
            path = Path(folder) / 'synthetic.md'
            path.write_text('---\nstage_only: false\ncreated_at: 2026-11-01\n---\nPersonal text.\n')
            self.assertIsNotNone(audit.candidate_from_file(path, 'Raw/01 Flomo/', date(2026,11,1), date(2026,11,1)))


if __name__ == '__main__':
    unittest.main()
