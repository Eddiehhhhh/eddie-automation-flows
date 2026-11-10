import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_publication_registered_predecessor as registered


class RegisteredPredecessorDiscovery(unittest.TestCase):
    def test_selects_exact_recovery_binding_before_bounded_scan(self):
        policy = {'current': True}
        public_policy = {'previous': True}
        binding = {
            'event': 'schedule',
            'public_policy': public_policy,
            'record': {'run_id': 42543146071, 'run_attempt': 1,
                       'controller_sha': '9' * 40, 'receipt_sequence': 9},
        }
        recovery = types.SimpleNamespace()
        seen = []

        def candidate(current, run):
            seen.append(run)
            return public_policy, current == policy and run == {
                'id': 42543146071, 'run_attempt': 1,
                'head_sha': '9' * 40, 'event': 'schedule',
            }

        recovery.candidate_policy = candidate
        exact = types.SimpleNamespace(source_discovery_locator=lambda _policy: None)
        idle = types.SimpleNamespace(_forward_policy=lambda _policy, _row: False)
        modules = {
            'get_notion_checkpoint_resume': exact,
            'get_notion_idle_recovery': idle,
            'get_recurring_policy_recovery': recovery,
        }
        with patch.object(registered, 'registry', return_value=[]), \
             patch.object(registered.bindings, 'value', return_value=binding), \
             patch.dict(sys.modules, modules):
            self.assertEqual(registered.discovery_locator(policy), (42543146071, 1))
        self.assertEqual(len(seen), 1)

    def test_rejected_fixed_binding_falls_back_to_registry(self):
        binding = {
            'event': 'schedule',
            'public_policy': {'previous': True},
            'record': {'run_id': 42543146071, 'run_attempt': 1,
                       'controller_sha': '9' * 40, 'receipt_sequence': 9},
        }
        row = {'get_record': {'receipt_sequence': 7},
               'run_binding': {'id': 7, 'run_attempt': 1, 'updated_at': '2026-01-01T00:00:00Z'}}
        recovery = types.SimpleNamespace(candidate_policy=lambda _policy, _run: ({'other': True}, True))
        exact = types.SimpleNamespace(source_discovery_locator=lambda _policy: None)
        idle = types.SimpleNamespace(_forward_policy=lambda _policy, _row: True)
        provenance = types.SimpleNamespace(timestamp=lambda value: value)
        modules = {
            'get_notion_checkpoint_resume': exact,
            'get_notion_idle_recovery': idle,
            'get_recurring_policy_recovery': recovery,
            'get_publication_provenance': provenance,
        }
        with patch.object(registered, 'registry', return_value=[row]), \
             patch.object(registered.bindings, 'value', return_value=binding), \
             patch.dict(sys.modules, modules):
            self.assertEqual(registered.discovery_locator({}), (7, 1))


if __name__ == '__main__':
    unittest.main()
