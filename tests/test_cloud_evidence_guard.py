import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cloud_evidence_guard import freshness, inspect, inspect_promotion, inspect_source

NOW = datetime(2026, 10, 16, 17, 10, tzinfo=timezone.utc)
SHA = 'a' * 40
POLICY = {'kind': 'owner_message', 'reference': 'verified-message-id'}


def source():
    return {'id': 'example', 'mode': 'production', 'max_age_hours': 24,
            'workflow': {'repository': 'example/repo', 'path': '.github/workflows/sync.yml'},
            'run': {'id': 123, 'url': 'https://github.com/example/repo/actions/runs/123',
                    'path': '.github/workflows/sync.yml', 'head_sha': SHA,
                    'created_at': '2026-10-16T16:00:00Z', 'status': 'completed', 'conclusion': 'success'},
            'artifacts': [{'path': 'Raw/index.md', 'commit_sha': SHA, 'blob_sha': SHA,
                           'generated_at': '2026-10-16T16:01:00Z', 'kind': 'source_index'}],
            'coverage': {'status': 'metadata_only', 'count': 500}}


class CloudEvidenceTest(unittest.TestCase):
    def test_success_is_not_full_sync(self):
        result = inspect_source(source(), NOW)
        self.assertEqual(result['execution'], 'latest_run_succeeded')
        self.assertFalse(result['coverage']['full_sync_proven_by_adapter'])
        self.assertIn('coverage_metadata_only', result['gaps'])

    def test_complete_claim_still_not_reverified(self):
        data = source(); data['coverage']['status'] = 'complete'
        self.assertFalse(inspect_source(data, NOW)['coverage']['full_sync_proven_by_adapter'])

    def test_unknown_control_plane_not_failure(self):
        data = {'schema_version': 1, 'sources': [source()], 'scheduler_control': {'status': 'unavailable'}}
        result = inspect(data, NOW)
        self.assertEqual(result['overall'], 'partial')
        self.assertEqual(result['scheduler_control']['status'], 'unavailable')

    def test_paused_without_run_expected(self):
        result = inspect_source({'id': 'daily', 'mode': 'paused', 'policy_evidence': POLICY}, NOW)
        self.assertEqual(result['expectation'], 'paused_expected')
        self.assertEqual(result['gaps'], [])

    def test_archive_expected(self):
        result = inspect_source({'id': 'retired', 'mode': 'archive_only', 'policy_evidence': POLICY}, NOW)
        self.assertEqual(result['expectation'], 'archive_only_expected')

    def test_unproven_pause_unknown(self):
        result = inspect_source({'id': 'daily', 'mode': 'paused'}, NOW)
        self.assertEqual(result['expectation'], 'policy_unverified')

    def test_manual_not_missing_sync(self):
        result = inspect_source({'id': 'manual', 'mode': 'manual_only', 'policy_evidence': POLICY}, NOW)
        self.assertEqual(result['execution'], 'not_expected')

    def test_next_monday_pending_future(self):
        result = inspect_source({'id': 'weekly', 'mode': 'production',
                                 'next_due_at': '2026-10-19T09:00:00+08:00'}, NOW)
        self.assertEqual(result['expectation'], 'pending_future')
        self.assertEqual(result['issues'], [])

    def test_preview_success_not_production(self):
        data = source(); data['mode'] = 'preview'
        self.assertEqual(inspect_source(data, NOW)['expectation'], 'preview_only')

    def test_bad_run_link_is_unverified(self):
        data = source(); data['run']['url'] = 'https://example.com/not-a-run'
        self.assertEqual(inspect_source(data, NOW)['execution'], 'invalid_run_evidence')

    def test_wrong_workflow_is_unverified(self):
        data = source(); data['run']['path'] = 'another.yml'
        self.assertEqual(inspect_source(data, NOW)['execution'], 'invalid_run_evidence')

    def test_failed_production_actionable(self):
        data = source(); data['run']['conclusion'] = 'failure'
        self.assertEqual(inspect({'schema_version': 1, 'sources': [data]}, NOW)['overall'], 'attention')

    def test_pending_not_success(self):
        data = source(); data['run']['status'] = 'in_progress'; data['run']['conclusion'] = None
        self.assertEqual(inspect_source(data, NOW)['execution'], 'in_progress')

    def test_failed_partial_production_not_hidden(self):
        data = source(); data['mode'] = 'partial_production'; data['run']['conclusion'] = 'failure'
        self.assertEqual(inspect({'schema_version': 1, 'sources': [data]}, NOW)['overall'], 'attention')

    def test_queued_is_not_running(self):
        for status in ['queued', 'waiting', 'requested', 'pending']:
            data = source(); data['run']['status'] = status; data['run']['conclusion'] = None
            self.assertEqual(inspect_source(data, NOW)['execution'], status)

    def test_missing_or_unknown_status_is_not_running(self):
        for status in [None, '', 'unknown', 'surprising_status']:
            data = source(); data['run']['status'] = status
            result = inspect_source(data, NOW)
            self.assertEqual(result['execution'], 'unverified')
            self.assertIn('workflow_status_unverified', result['gaps'])

    def test_wrong_artifact_snapshot_is_not_current_arrival(self):
        result = inspect_source(source(), NOW, 'b' * 40)
        self.assertEqual(result['artifacts'][0]['arrival'], 'historical_version_observed')
        self.assertIn('artifact_snapshot_mismatch', result['gaps'])

    def test_old_source_content_separate_from_new_index(self):
        data = source(); data['artifacts'][0]['latest_source_at'] = '2026-09-23T00:00:00+08:00'
        item = inspect_source(data, NOW)['artifacts'][0]
        self.assertEqual(item['freshness']['status'], 'within_sla')
        self.assertEqual(item['source_content_freshness']['status'], 'observed_no_sla')

    def test_read_timestamp_never_used_as_generation(self):
        data = source(); del data['artifacts'][0]['generated_at']
        data['artifacts'][0]['observed_at'] = NOW.isoformat()
        self.assertEqual(inspect_source(data, NOW)['artifacts'][0]['freshness']['status'], 'unknown')

    def test_invalid_blob_unknown(self):
        data = source(); data['artifacts'][0]['blob_sha'] = 'short'
        self.assertEqual(inspect_source(data, NOW)['artifacts'][0]['arrival'], 'unverified')

    def test_stale(self):
        self.assertEqual(freshness('2026-10-14T00:00:00Z', NOW, 24)['status'], 'stale')

    def test_explicit_sla_stale_is_actionable(self):
        data = source(); data['artifacts'][0]['generated_at'] = '2026-10-14T00:00:00Z'
        self.assertIn('artifact_stale_under_explicit_sla', inspect_source(data, NOW)['issues'])
        data['run']['created_at'] = '2026-10-14T00:00:00Z'
        self.assertIn('workflow_run_stale_under_explicit_sla', inspect_source(data, NOW)['issues'])

    def test_no_sla_stale_not_invented(self):
        data = source(); data.pop('max_age_hours'); data['artifacts'][0]['generated_at'] = '2020-01-01T00:00:00Z'
        self.assertEqual(inspect_source(data, NOW)['issues'], [])

    def test_future_timestamp(self):
        self.assertEqual(freshness('2026-10-19T00:00:00Z', NOW, 24)['status'], 'future_timestamp')

    def test_naive_timestamp_unknown(self):
        self.assertEqual(freshness('2026-10-16T00:00:00', NOW, 24)['status'], 'unknown')

    def test_no_sla_not_fresh(self):
        self.assertEqual(freshness('2026-10-16T16:00:00Z', NOW)['status'], 'observed_no_sla')

    def test_pause_overrides_old_missing_report(self):
        result = inspect_promotion({'mode': 'paused', 'policy_evidence': POLICY}, NOW)
        self.assertEqual(result['status'], 'paused_expected')

    def test_missing_report_unknown_control(self):
        self.assertEqual(inspect_promotion({}, NOW)['status'], 'unverified')

    def test_missing_report_after_noon_verified_enabled(self):
        now = datetime(2026, 10, 16, 7, 20, tzinfo=timezone.utc)
        self.assertEqual(inspect_promotion({'scheduler_control': 'enabled_verified'}, now)['status'], 'missing_report')

    def test_missing_report_before_noon(self):
        now = datetime(2026, 10, 16, 3, 20, tzinfo=timezone.utc)
        self.assertEqual(inspect_promotion({'scheduler_control': 'enabled_verified'}, now)['status'], 'pending_schedule')

    def test_success_cursor_consistency(self):
        data = {'report': f'- 最终状态：`success`\n- 扫描终点：`{SHA}`',
                'state': {'runs': {'2026-10-17': {'effective_status': 'success'}},
                          'last_processed_head_sha': SHA,
                          'raw_cursor': {'last_processed_head_sha': SHA},
                          'raw_scan': {'head_sha': SHA, 'cursor_advanced': True, 'result': 'success'}}}
        self.assertEqual(inspect_promotion(data, NOW)['status'], 'consistent_reported_success')
        data['state']['raw_scan']['cursor_advanced'] = False
        self.assertEqual(inspect_promotion(data, NOW)['status'], 'cursor_mismatch')

    def test_conflicting_formal_report_rejected(self):
        data = {'report': '- 最终状态：`success`\n- 执行结果：`failed`'}
        self.assertEqual(inspect_promotion(data, NOW)['status'], 'invalid_report')

    def test_input_unchanged(self):
        data = {'schema_version': 1, 'sources': [source()]}
        before = copy.deepcopy(data); inspect(data, NOW)
        self.assertEqual(data, before)

    def test_duplicate_source_rejected(self):
        with self.assertRaises(ValueError):
            inspect({'schema_version': 1, 'sources': [source(), source()]}, NOW)

    def test_cli_private_output_and_counts_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); evidence = root / 'evidence.json'; output = root / 'result.private.json'
            evidence.write_text(json.dumps({'schema_version': 1, 'sources': [source()]}))
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/cloud_evidence_guard.py'),
                       '--evidence', str(evidence), '--output', str(output), '--now', NOW.isoformat()]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Raw/', result.stdout)
            self.assertNotIn(SHA, result.stdout)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            first = output.read_bytes()
            second = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_bytes(), first)

    def test_cli_rejects_output_in_vault(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); evidence = root / 'evidence.json'
            evidence.write_text(json.dumps({'schema_version': 1, 'sources': [source()]}))
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/cloud_evidence_guard.py'),
                       '--evidence', str(evidence), '--output', str(root / 'formal.private.json'), '--vault', str(root)],
                       text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'formal.private.json').exists())

    def test_registry_cannot_silently_omit_sources(self):
        with self.assertRaises(ValueError):
            inspect({'schema_version': 1, 'sources': [source()],
                     'snapshot': {'registered_paths': ['example', 'forgotten']}}, NOW)

    def test_invalid_sla_not_fresh(self):
        self.assertEqual(freshness('2026-10-16T16:00:00Z', NOW, float('nan'))['status'], 'invalid_sla')


if __name__ == '__main__':
    unittest.main()
