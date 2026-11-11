import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_publication_registered_predecessor as registered


class RegisteredPredecessorDiscovery(unittest.TestCase):
    def binding(self, sequence):
        return {
            'event': 'schedule',
            'public_policy': {'sequence': sequence},
            'record': {'run_id': sequence * 10, 'run_attempt': 1,
                       'controller_sha': str(sequence) * 40,
                       'receipt_sequence': sequence},
        }

    def install(self, policy, bindings_by_sequence, candidate, fail=None, rows=None):
        recovery = types.SimpleNamespace(candidate_policy=candidate)
        exact = types.SimpleNamespace(source_discovery_locator=lambda _policy: None)
        idle = types.SimpleNamespace(_forward_policy=lambda _policy, _row: rows is not None)
        provenance = types.SimpleNamespace(timestamp=lambda value: value,
                                           fail=fail or (lambda _code: None))
        modules = {
            'get_notion_checkpoint_resume': exact,
            'get_notion_idle_recovery': idle,
            'get_recurring_policy_recovery': recovery,
            'get_publication_provenance': provenance,
        }
        value = lambda _module, name: bindings_by_sequence.get(name)
        return patch.object(registered, 'registry', return_value=rows or []), \
            patch.object(registered.bindings, 'value', side_effect=value), \
            patch.dict(sys.modules, modules)

    def test_prefers_admitted_sequence_9_binding(self):
        policy = {'current': True}
        binding = self.binding(9)
        seen = []

        def candidate(current, run):
            seen.append(run['id'])
            return binding['public_policy'], current == policy

        patches = self.install(policy, {'SEQ9_BINDING': binding}, candidate)
        with patches[0], patches[1], patches[2]:
            self.assertEqual(registered.discovery_locator(policy), (90, 1))
        self.assertEqual(seen, [90])

    def test_tries_sequence_7_only_when_sequence_9_is_not_admitted(self):
        policy = {'current': True}
        seq9, seq7 = self.binding(9), self.binding(7)
        seen = []

        def candidate(current, run):
            seen.append(run['id'])
            if run['id'] == 90:
                return {'rejected': True}, False
            return seq7['public_policy'], current == policy

        patches = self.install(policy, {'SEQ9_BINDING': seq9, 'SEQ7_BINDING': seq7}, candidate)
        with patches[0], patches[1], patches[2]:
            self.assertEqual(registered.discovery_locator(policy), (70, 1))
        self.assertEqual(seen, [90, 70])

    def test_does_not_fall_back_after_admitted_binding_mismatch(self):
        policy = {'current': True}
        seq9, seq7 = self.binding(9), self.binding(7)
        seen = []

        def candidate(_current, run):
            seen.append(run['id'])
            return {'unexpected': True}, True

        def fail(code):
            raise ValueError(code)

        patches = self.install(policy, {'SEQ9_BINDING': seq9, 'SEQ7_BINDING': seq7}, candidate, fail)
        with patches[0], patches[1], patches[2]:
            with self.assertRaisesRegex(ValueError, 'provenance_record'):
                registered.discovery_locator(policy)
        self.assertEqual(seen, [90])

    def test_rejected_fixed_bindings_keep_registry_fallback(self):
        policy = {'current': True}
        seq9, seq7 = self.binding(9), self.binding(7)
        row = {'get_record': {'receipt_sequence': 6},
               'run_binding': {'id': 60, 'run_attempt': 1,
                               'updated_at': '2026-01-01T00:00:00Z'}}
        seen = []

        def candidate(_current, run):
            seen.append(run['id'])
            return {'rejected': True}, False

        patches = self.install(policy, {'SEQ9_BINDING': seq9, 'SEQ7_BINDING': seq7},
                               candidate, rows=[row])
        with patches[0], patches[1], patches[2]:
            self.assertEqual(registered.discovery_locator(policy), (60, 1))
        self.assertEqual(seen, [90, 70])


if __name__ == '__main__':
    unittest.main()
