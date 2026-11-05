from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_runtime_report as report


class PrivateRuntimeReport(unittest.TestCase):
    def candidate(self, **scope):
        return report.public({
            'status': 'recurring_candidate_verified',
            'main_updated': False, 'cas_attempts': 0, 'local_proposal_only': True,
            'apply_ready': False, 'schedule_enabled': False, 'asset_downloads': 0,
            'notion_writes': 0, 'confirmed_content_ids': 0,
            'opaque_source_fields_rendered': False,
            'complete_source_rendering_claimed': False,
            'readback_verified': True, 'sink_status': 'not_requested', **scope,
        })

    def test_candidate_projection_preserves_only_validated_scope(self):
        self.assertEqual(self.candidate(source_scope='single_note', catalog_complete=False),
                         {**report.PREPARE, 'source_scope': 'single_note', 'catalog_complete': False})
        self.assertEqual(self.candidate(source_scope='complete_catalog', catalog_complete=True),
                         {**report.PREPARE, 'source_scope': 'complete_catalog', 'catalog_complete': True})
        invalid = self.candidate(source_scope='single_note', catalog_complete=True)
        self.assertEqual(invalid['status'], 'blocked')
        self.assertNotIn('source_scope', invalid)
        self.assertNotIn('catalog_complete', invalid)
        self.assertEqual(self.candidate(source_scope='single_note', catalog_complete=0)['status'], 'blocked')
        self.assertEqual(self.candidate(source_scope=None, catalog_complete=None)['status'], 'blocked')

    def test_failure_projection_allows_single_detail_phase(self):
        projected = report.public({'status': 'recurring_failed',
                                   'source_diagnostic_code': 'request_failed',
                                   'source_phase': 'single_detail'})
        self.assertEqual(projected['source_phase'], 'single_detail')


if __name__ == '__main__':
    unittest.main()
