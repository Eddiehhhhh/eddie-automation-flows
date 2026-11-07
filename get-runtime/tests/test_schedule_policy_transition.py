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

    def test_previous_template_bytes_are_hash_pinned(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller-minute17.yml.disabled'
        self.assertEqual(ctl.a.sha(path.read_bytes()), ctl.PRE_MINUTE23_TEMPLATE_SHA256)


if __name__ == '__main__': unittest.main()
