import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location('acceptance', Path(__file__).parents[1] / 'scripts/cloud_acceptance.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class HealthContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 12, 7, 20, tzinfo=timezone.utc)
        self.day = '2026-10-12'
        self.sha = 'a' * 40

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value if isinstance(value, str) else json.dumps(value))

    def test_missing_evidence_is_not_success(self):
        data = a.health(self.root, self.now)
        self.assertEqual(data['promotion'], 'blocked')
        self.assertEqual(data['source_arrival'], 'blocked')
        self.assertEqual(data['local_maintenance'], 'not_checked_cloud')

    def test_before_noon_report_pending_shanghai_not_utc(self):
        self.assertEqual(a.health(self.root, datetime(2026, 10, 12, 3, tzinfo=timezone.utc))['promotion'], 'pending_schedule')
        self.assertEqual(a.health(self.root, datetime(2026, 10, 12, 4, tzinfo=timezone.utc))['promotion'], 'blocked')

    def test_stale_arrival_blocks_and_never_echoes_private_text(self):
        self.write('Raw/00 Meta/source-arrival-status.json', {'checked_date': '2026-10-11', 'overall_status': 'ok', 'registered_missing': [], 'private': 'SENSITIVE'})
        self.assertEqual(a.health(self.root, self.now)['source_arrival'], 'blocked')
        self.assertNotIn('SENSITIVE', json.dumps(a.health(self.root, self.now)))

    def test_success_requires_all_cursor_fields(self):
        self.write(f'Raw/09 ChatGPT/{self.day}-每日升格扫描.md', f'- 最终状态：`success`\n- 扫描终点：`{self.sha}`\n')
        state = {'runs': {self.day: {'effective_status': 'success'}}, 'raw_cursor': {'last_processed_head_sha': self.sha}, 'last_processed_head_sha': self.sha, 'raw_scan': {'head_sha': self.sha, 'cursor_advanced': True, 'result': 'success'}}
        self.write('Raw/09 ChatGPT/_daily-promotion-state.json', state)
        self.assertEqual(a.health(self.root, self.now)['promotion'], 'healthy')
        state['raw_scan']['cursor_advanced'] = False
        self.write('Raw/09 ChatGPT/_daily-promotion-state.json', state)
        self.assertEqual(a.health(self.root, self.now)['promotion'], 'blocked')

    def test_hermes_disabled_is_expected(self):
        self.write('Raw/00 Meta/hermes-runtime-state.json', {'status': 'disabled_by_owner'})
        self.assertEqual(a.health(self.root, self.now)['hermes_policy'], 'disabled_by_owner')


if __name__ == '__main__':
    unittest.main()
