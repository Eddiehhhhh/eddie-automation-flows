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

    def test_provenance_check_projection_accepts_only_fixed_names(self):
        value = {'status': 'recurring_failed', 'error': 'provenance_run',
                 'provenance_check': 'run_origin'}
        self.assertEqual(report.public(value, allowed_codes=('provenance_run',))['provenance_check'], 'run_origin')
        value['provenance_check'] = 'private note title'
        self.assertNotIn('provenance_check', report.public(value, allowed_codes=('provenance_run',)))

    def test_scope_failure_projects_only_fixed_check_names(self):
        projected = report.public({'status': 'recurring_failed', 'error': 'runtime_scope',
                                   'scope_check': 'scope_keys_or_hash'}, allowed_codes=('runtime_scope',))
        self.assertEqual(projected['scope_check'], 'scope_keys_or_hash')
        private = report.public({'status': 'recurring_failed', 'error': 'runtime_scope',
                                 'scope_check': 'private note identifier'}, allowed_codes=('runtime_scope',))
        self.assertNotIn('scope_check', private)
        malformed = report.public({'status': 'recurring_failed', 'error': 'runtime_scope',
                                   'scope_check': []}, allowed_codes=('runtime_scope',))
        self.assertNotIn('scope_check', malformed)
        runtime = Path(__file__).resolve().parents[1] / 'scripts/get_recurring_runtime.py'
        self.assertNotRegex(runtime.read_text(), r"\bfail\(['\"]runtime_scope['\"]\)")

    def test_gitdata_api_diagnostic_projects_only_allowlisted_fields(self):
        value = {'status': 'recurring_failed', 'error': 'gitdata_staging_failed',
                 'stage': 'pending_gate', 'gitdata_api_diagnostic': {
                     'operation': 'stage_refs', 'failure': 'http_error', 'http_status': 403}}
        self.assertEqual(report.public(value, allowed_codes=('gitdata_staging_failed',),
                                       allowed_stages=('pending_gate',)), {
            'contract': report.CONTRACT, 'status': 'recurring_failed',
            'business_health_asserted': False, 'complete_library_claimed': False,
            'error': 'gitdata_staging_failed', 'stage': 'pending_gate',
            'gitdata_api_diagnostic': {'operation': 'stage_refs', 'failure': 'http_error',
                                       'http_status': 403}})
        value['gitdata_api_diagnostic'] = {'operation': 'blob_create', 'failure': 'http_error', 'http_status': 403}
        self.assertEqual(report.public(value)['gitdata_api_diagnostic'],
                         {'operation': 'blob_create', 'failure': 'http_error', 'http_status': 403})
        value['gitdata_api_diagnostic'] = {'operation': 'blob_create', 'failure': 'guard_rejected'}
        self.assertEqual(report.public(value)['gitdata_api_diagnostic'],
                         {'operation': 'blob_create', 'failure': 'guard_rejected'})
        value['gitdata_api_diagnostic'].update(url='private', body='private')
        self.assertNotIn('gitdata_api_diagnostic', report.public(value))
        del value['gitdata_api_diagnostic']['url']; del value['gitdata_api_diagnostic']['body']
        value['gitdata_api_diagnostic']['operation'] = 'private/path'
        self.assertNotIn('gitdata_api_diagnostic', report.public(value))

    def test_notion_journal_failure_stage_is_fixed_and_allowlisted(self):
        for stage in ('journal_baseline', 'journal_discovery', 'journal_readback'):
            projected = report.public({'status': 'failed', 'stage': stage, 'reason': 'approval_missing'},
                                      allowed_stages=('journal_baseline', 'journal_discovery', 'journal_readback'))
            self.assertEqual(projected['stage'], stage)
        private = report.public({'status': 'failed', 'stage': 'journal / private path', 'reason': 'approval_missing'},
                                allowed_stages=('journal_baseline', 'journal_discovery', 'journal_readback'))
        self.assertNotIn('stage', private)


if __name__ == '__main__':
    unittest.main()
