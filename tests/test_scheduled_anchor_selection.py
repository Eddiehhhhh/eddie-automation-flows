"""Regression coverage for the observed scheduled-run provenance failures."""
from copy import deepcopy
import unittest
from test_publication_provenance import p, FakeAPI, run, record, policy


class ScheduledAnchorTests(unittest.TestCase):
    def observed(self, failures=6):
        anchor = run(1018, created_at='2026-10-18T03:20:18Z', updated_at='2026-10-18T03:37:39Z')
        attempt = {**anchor, 'created_at': '2026-10-18T03:20:19Z'}
        rows = [run(1019+i, conclusion='failure', created_at='2026-10-19T01:00:00Z',
                    updated_at='2026-10-19T01:05:00Z') for i in range(failures)]
        return FakeAPI(rows + [anchor], {1018: record(anchor)}, {(1018, 1): attempt})

    def test_observed_created_clock_and_six_failed_runs_recover_verified_anchor(self):
        api = self.observed(); result = p.discover(api, policy())
        self.assertEqual(result['run_id'], 1018)
        self.assertEqual(result['candidate_runs_checked'], 1)
        self.assertEqual(result['publication_records_verified'], 1)
        self.assertEqual(api.called.count('/actions/runs/1018/attempts/1'), 2)
        self.assertEqual(len([x for x in api.called if '/attempts/' in x]), 3)

    def test_creation_clock_tolerance_is_only_observed_forward_one_second(self):
        for stamp in ('2026-10-18T03:20:17Z', '2026-10-18T03:20:20Z'):
            api = self.observed(); api.attempts[(1018, 1)]['created_at'] = stamp
            with self.subTest(stamp=stamp), self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_run_changed'):
                p.discover(api, policy())
            self.assertFalse(any(x.startswith('logs:') for x in api.called))

    def test_same_attempt_creation_clock_mutation_remains_fatal(self):
        api = self.observed(); first = api.attempts[(1018, 1)]
        reads = [first, {**first, 'created_at': '2026-10-18T03:20:18Z'}]
        api.overrides['/actions/runs/1018/attempts/1'] = lambda: deepcopy(reads.pop(0))
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_run_changed'):
            p.discover(api, policy())

    def test_latest_creation_clock_mutation_remains_fatal(self):
        api = self.observed()
        api.overrides['/actions/runs/1018'] = {**api.runs[-1], 'created_at': '2026-10-18T03:20:19Z'}
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_run_changed'):
            p.discover(api, policy())

    def test_skipped_failures_never_supply_missing_proof(self):
        api = self.observed(); api.log_overrides[1518] = b'no publication record'
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_record_count'):
            p.discover(api, policy())

    def test_negative_selection_still_validates_repository_identity(self):
        api = self.observed(); api.runs[0]['repository']['private'] = True
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_run'):
            p.discover(api, policy())

    def test_only_failures_in_three_page_window_fail_without_attempt_requests(self):
        rows = [run(2000+i, conclusion='failure') for i in range(90)]
        api = FakeAPI(rows)
        for page in range(1, 4):
            api.overrides['/actions/workflows/21/runs?per_page=30&page=' + str(page)] = {
                'total_count': 91, 'workflow_runs': rows[(page-1)*30:page*30]}
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
            p.discover(api, policy())
        self.assertEqual(len(api.called), 5)
        self.assertFalse(any('/attempts/' in x for x in api.called))
