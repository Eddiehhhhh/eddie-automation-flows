import unittest

from core import Hold
from trigger import SOURCES, verify


class TriggerTests(unittest.TestCase):
    def event(self, name='Audit Raw filenames', kind='schedule'):
        return {'repository': {'full_name': 'owner/runtime'}, 'workflow_run': {
            'name': name, 'event': kind, 'status': 'completed', 'head_branch': 'main',
            'head_repository': {'full_name': 'owner/runtime'}, 'id': 123, 'run_attempt': 1,
            'conclusion': 'failure'}}

    def test_direct_manual_and_schedule_still_work(self):
        for kind in ('schedule', 'workflow_dispatch'):
            self.assertEqual(verify(kind, {}, 'owner/runtime'), {'trigger': kind})

    def test_scheduled_clocks_wake_independently_of_business_conclusion(self):
        for name, kind in SOURCES.items():
            for conclusion in ('success', 'failure', 'cancelled'):
                event = self.event(name, kind)
                event['workflow_run']['conclusion'] = conclusion
                result = verify('workflow_run', event, 'owner/runtime')
                self.assertEqual(result['source_run_id'], 123)
                self.assertEqual(result['source_event'], kind)

    def test_manual_other_business_and_wrong_source_type_are_rejected(self):
        for name, kind in [('Audit Raw filenames', 'workflow_dispatch'),
                           ('Get cloud sync wake', 'schedule'), ('Unknown', 'schedule')]:
            with self.assertRaises(Hold):
                verify('workflow_run', self.event(name, kind), 'owner/runtime')

    def test_cross_repo_fork_branch_unfinished_and_invalid_run_are_rejected(self):
        mutations = [('head_branch', 'feature'), ('head_repository', {'full_name': 'fork/runtime'}),
                     ('head_repository', None), ('status', 'in_progress'), ('id', True),
                     ('id', 0), ('run_attempt', 0), ('run_attempt', '1')]
        for field, value in mutations:
            event = self.event()
            event['workflow_run'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(Hold):
                verify('workflow_run', event, 'owner/runtime')
        event = self.event()
        event['repository']['full_name'] = 'other/runtime'
        with self.assertRaises(Hold):
            verify('workflow_run', event, 'owner/runtime')

    def test_missing_unknown_events_and_recursive_get_source_are_rejected(self):
        for kind, event in [('push', {}), ('workflow_run', {}),
                            ('workflow_run', self.event('Direct Get dual destination sync'))]:
            with self.assertRaises(Hold):
                verify(kind, event, 'owner/runtime')


if __name__ == '__main__':
    unittest.main()
