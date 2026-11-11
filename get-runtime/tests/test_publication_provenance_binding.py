"""Attempt-specific creation clocks returned by GitHub's Actions API."""
import importlib
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_runtime_bindings
with patch.object(get_private_runtime_bindings, 'value', return_value=None):
    provenance = importlib.import_module('get_publication_provenance')


class AttemptClock(unittest.TestCase):
    def test_rerun_creation_time_uses_its_attempt_start(self):
        listed = {
            'created_at': '2026-11-04T21:30:28Z',
            'run_started_at': '2026-11-04T21:51:19Z',
        }
        attempt = {
            'created_at': '2026-11-04T21:51:20Z',
            'run_started_at': '2026-11-04T21:51:19Z',
        }
        self.assertTrue(provenance._attempt_clock_matches(attempt, listed, 2))
        self.assertFalse(provenance._attempt_clock_matches(attempt, listed, 1))

    def test_rerun_two_second_creation_start_gap_is_accepted(self):
        listed = {'created_at': '2026-11-05T15:04:13Z', 'run_started_at': '2026-11-05T15:28:37Z'}
        attempt = {'created_at': '2026-11-05T15:04:13Z', 'run_started_at': '2026-11-05T15:04:11Z'}
        self.assertTrue(provenance._attempt_clock_matches(attempt, listed, 2))

    def test_rerun_creation_start_gap_over_three_seconds_fails_closed(self):
        listed = {'created_at': '2026-11-05T15:04:13Z', 'run_started_at': '2026-11-05T15:28:37Z'}
        attempt = {'created_at': '2026-11-05T15:04:15Z', 'run_started_at': '2026-11-05T15:04:11Z'}
        self.assertFalse(provenance._attempt_clock_matches(attempt, listed, 2))

    def test_first_attempt_creation_time_uses_run_creation(self):
        listed = {'created_at': '2026-11-04T21:30:28Z'}
        attempt = {'created_at': '2026-11-04T21:30:29Z'}
        self.assertTrue(provenance._attempt_clock_matches(attempt, listed, 1))

    def test_rerun_without_attempt_start_fails_closed(self):
        listed = {'created_at': '2026-11-04T21:30:28Z'}
        attempt = {'created_at': '2026-11-04T21:51:20Z'}
        self.assertFalse(provenance._attempt_clock_matches(attempt, listed, 2))


class PartialGetPublication(unittest.TestCase):
    def setUp(self):
        self.run = {'status': 'completed', 'conclusion': 'failure', 'event': 'workflow_dispatch'}
        self.job = {'status': 'completed', 'conclusion': 'failure', 'steps': [
            {'name': provenance.controller.STEP_NAME, 'number': 13,
             'status': 'completed', 'conclusion': 'success'},
            {'name': 'Sync confirmed Get content to Notion', 'number': 14,
             'status': 'completed', 'conclusion': 'failure'},
            {'name': 'Post Run actions/checkout', 'number': 28,
             'status': 'completed', 'conclusion': 'success'}]}

    def test_get_success_remains_trustable_when_only_later_notion_step_fails(self):
        self.assertTrue(provenance._notion_failed_after_publication(
            self.run, self.job, {'publication_step_name': provenance.controller.STEP_NAME}))

    def test_unrelated_failure_is_not_a_publication_anchor(self):
        self.job['steps'].append({'name': 'Prepare policy', 'number': 11,
                                  'status': 'completed', 'conclusion': 'failure'})
        self.assertFalse(provenance._notion_failed_after_publication(
            self.run, self.job, {'publication_step_name': provenance.controller.STEP_NAME}))


    def test_failed_get_step_is_not_a_publication_anchor(self):
        self.job['steps'][0]['conclusion'] = 'failure'
        self.assertFalse(provenance._notion_failed_after_publication(
            self.run, self.job, {'publication_step_name': provenance.controller.STEP_NAME}))

    def test_unknown_or_misordered_notion_step_is_not_an_anchor(self):
        self.job['steps'][1]['number'] = 12
        self.assertFalse(provenance._notion_failed_after_publication(
            self.run, self.job, {'publication_step_name': provenance.controller.STEP_NAME}))

    def test_failure_between_publication_and_notion_is_not_an_anchor(self):
        self.job['steps'][0]['number'] = 12
        self.job['steps'][1]['number'] = 14
        self.job['steps'].insert(1, {'name': 'Unexpected intermediate step', 'number': 13,
                                     'status': 'completed', 'conclusion': 'failure'})
        self.assertFalse(provenance._notion_failed_after_publication(
            self.run, self.job, {'publication_step_name': provenance.controller.STEP_NAME}))

    def test_duplicate_step_number_is_not_an_anchor(self):
        self.job['steps'][2]['number'] = 14
        self.assertFalse(provenance._notion_failed_after_publication(
            self.run, self.job, {'publication_step_name': provenance.controller.STEP_NAME}))


class CandidateAttemptReadback(unittest.TestCase):
    def setUp(self):
        self.policy = {
            'repository_id': 1270807355,
            'workflow_id': 387242896,
            'workflow_path': '.github/workflows/get-canonical.yml',
            'head_branch': 'main',
        }
        repository = {'id': 1270807355, 'full_name': 'Eddiehhhhh/eddie-automation-flows', 'private': False}
        self.run = {
            'id': 42969044610, 'run_attempt': 1, 'repository': repository,
            'head_repository': repository, 'workflow_id': 387242896,
            'path': '.github/workflows/get-canonical.yml', 'head_branch': 'main',
            'head_sha': 'a' * 40, 'created_at': '2026-11-11T05:56:57Z',
            'updated_at': '2026-11-11T06:09:07Z', 'event': 'workflow_dispatch',
            'status': 'completed', 'conclusion': 'failure',
        }

    def test_readback_accepts_unchanged_get_success_with_later_notion_failure(self):
        checked = provenance.check_attempt_readback(self.run, self.run, self.policy)
        self.assertEqual(checked['conclusion'], 'failure')

    def test_readback_rejects_attempt_state_change(self):
        changed = {**self.run, 'conclusion': 'success'}
        with self.assertRaises(provenance.ProvenanceFailure):
            provenance.check_attempt_readback(changed, self.run, self.policy)


if __name__ == '__main__':
    unittest.main()
