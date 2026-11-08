"""Schedule changes preserve byte-exact verification of the prior controller."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_recurring_controller as ctl


class SchedulePolicyTransition(unittest.TestCase):
    def test_current_and_prior_schedule_scopes_are_distinct(self):
        old = {'controller_template_sha256': ctl.PRE_MINUTE23_TEMPLATE_SHA256,
               'rules_sha256': 'a'*64, 'get_policy_sha256':'b'*64}
        new = {**old, 'controller_template_sha256': ctl.template_sha256()}
        with patch.object(ctl._private_bindings, 'value', return_value='c'*64):
            self.assertEqual(ctl.scope_for(old, 'd'*64)['schedule_minute'], 17)
            self.assertEqual(ctl.scope_for(new, 'd'*64)['schedule_minute'], 23)
        self.assertIn(b"cron: '17 * * * *'", ctl.policy_template_bytes(old))
        self.assertIn(b"cron: '23 * * * *'", ctl.policy_template_bytes(new))

    def test_fixed_archive_accepts_only_current_and_immediate_prior_template(self):
        import get_notion_journal_runtime as journal
        for digest in (ctl.template_sha256(), ctl.PRE_MINUTE23_TEMPLATE_SHA256):
            self.assertTrue(journal.fixed_template_allowed({'controller_template_sha256': digest}))
        for digest in (ctl.PRE_DECOUPLE_TEMPLATE_SHA256, '0'*64, None):
            self.assertFalse(journal.fixed_template_allowed({'controller_template_sha256': digest}))

    def test_prepare_only_allows_only_current_and_immediate_prior_template(self):
        for digest in (ctl.template_sha256(), ctl.PRE_MINUTE23_TEMPLATE_SHA256):
            self.assertTrue(ctl.current_or_prior_schedule_template({'controller_template_sha256': digest}))
        for digest in (ctl.PRE_DECOUPLE_TEMPLATE_SHA256, '0'*64, None):
            self.assertFalse(ctl.current_or_prior_schedule_template({'controller_template_sha256': digest}))

    def test_previous_template_bytes_are_hash_pinned(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller-minute17.yml.disabled'
        self.assertEqual(ctl.a.sha(path.read_bytes()), ctl.PRE_MINUTE23_TEMPLATE_SHA256)

    def test_sequence_seven_template_bytes_are_hash_pinned(self):
        from get_publication_provenance import ProvenanceFailure
        digest = '3f7b35a0348f5f0197998bf84c6d1473f5e364a8f4611a2823b6642c9263aadb'
        raw = ctl.policy_template_bytes({'controller_template_sha256': digest})
        self.assertEqual(ctl.a.sha(raw), digest)
        self.assertIn(digest, ctl.historical_template_digests())
        self.assertFalse(ctl.current_or_prior_schedule_template({'controller_template_sha256': digest}))
        with self.assertRaises(ProvenanceFailure):
            ctl.policy_template_bytes({'controller_template_sha256': '0' * 64})

    def test_sequence_seven_renders_its_bound_23_minute_schedule(self):
        import get_publication_provenance as provenance
        digest = '3f7b35a0348f5f0197998bf84c6d1473f5e364a8f4611a2823b6642c9263aadb'
        policy = {'contract': provenance.POLICY_CONTRACT, 'repository': provenance.REPOSITORY,
                  'repository_id': 123, 'workflow_id': 456,
                  'workflow_path': '.github/workflows/get-canonical.yml', 'head_branch': 'main',
                  'code_sha': 'a' * 40, 'job_name': ctl.JOB_NAME,
                  'publication_step_name': ctl.STEP_NAME, 'rules_sha256': 'b' * 64,
                  'get_policy_sha256': 'c' * 64, 'controller_template_sha256': digest}
        with patch.object(ctl._private_bindings, 'value', return_value='d' * 64):
            self.assertEqual(ctl.scope_for(policy, 'e' * 64)['schedule_minute'], 23)
            rendered = ctl.expected_bytes(policy)
        self.assertIn(b"cron: '23 * * * *'", rendered)
        self.assertNotIn(b"cron: '17 * * * *'", rendered)
        self.assertFalse(ctl.current_or_prior_schedule_template(policy))


if __name__ == '__main__': unittest.main()
