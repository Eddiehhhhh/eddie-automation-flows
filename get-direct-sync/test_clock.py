import unittest
from clock import arm, dispatch


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '123',
                    'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                    'GET_CLOCK_VALIDATION_TICKS': '2'}
        self.calls = []
        self.entries = [{'id': 123, 'status': 'in_progress'}]
        self.timer = 1
        self.uncertain = False

    def api(self, path, body=None):
        self.calls.append((path, body))
        if '/environments/' in path:
            return {'protection_rules': [{'type': 'wait_timer', 'wait_timer': self.timer}]}
        if body is not None:
            self.entries.append({'id': 124, 'status': 'queued', 'display_title': 'Get clock 123'})
            if self.uncertain:
                raise TimeoutError()
            return None
        return {'workflow_runs': list(self.entries)}

    def test_server_timer_rearms_once_and_decrements_acceptance_ticks(self):
        self.assertEqual(arm(self.env, self.api), 'next_tick_verified')
        posts = [body for _, body in self.calls if body]
        self.assertEqual(posts, [{'ref': 'main', 'inputs': {'validation_ticks': '1', 'parent_run': '123'}}])
        self.assertEqual(arm(self.env, self.api), 'already_armed')
        self.assertEqual(len([b for _, b in self.calls if b]), 1)

    def test_uncertain_dispatch_is_read_back_without_second_post(self):
        self.uncertain = True
        self.assertEqual(arm(self.env, self.api), 'next_tick_verified')
        self.assertEqual(len([b for _, b in self.calls if b]), 1)

    def test_missing_timer_invalid_input_or_branch_never_dispatch(self):
        for timer, field, value in [(0, 'GET_CLOCK_VALIDATION_TICKS', '2'),
                                    (1, 'GET_CLOCK_VALIDATION_TICKS', '-1'),
                                    (1, 'GITHUB_REF', 'refs/heads/topic')]:
            self.timer = timer
            env = {**self.env, field: value}
            with self.assertRaises(ValueError):
                arm(env, self.api)
        self.assertFalse(any(b for _, b in self.calls))

    def test_recovery_checks_hourly_policy_before_rearming(self):
        self.env['GET_CLOCK_VALIDATION_TICKS'] = '0'
        self.env['GITHUB_EVENT_NAME'] = 'workflow_run'
        self.timer = 60
        self.assertEqual(arm(self.env, self.api, recover=True), 'next_tick_verified')
        self.assertTrue(any(p.endswith('/environments/get-clock-hourly') for p, _ in self.calls))
        self.assertEqual(self.calls[-2][1]['inputs']['validation_ticks'], '0')

    def test_sync_dispatch_is_explicit_and_reconciles_uncertain_response(self):
        entries = []
        posts = []
        def api(path, body=None):
            self.assertIn('/get-notion-incremental.yml/', path)
            if body:
                posts.append(body)
                entries.append({'display_title': 'Direct Get clock-123'})
                raise TimeoutError()
            return {'workflow_runs': entries}
        inputs = {'mode': 'sync', 'batch_size': '10', 'dispatch_key': 'clock-123'}
        self.assertEqual(dispatch(api, 'repos/owner/repo', 'get-notion-incremental.yml',
                                 'Direct Get clock-123', inputs), 'next_tick_verified')
        self.assertEqual(dispatch(api, 'repos/owner/repo', 'get-notion-incremental.yml',
                                 'Direct Get clock-123', inputs), 'successor_already_recorded')
        self.assertEqual(posts, [{'ref': 'main', 'inputs': inputs}])

    def test_broken_hourly_timer_never_restarts_from_recovery(self):
        self.timer = 0
        with self.assertRaises(ValueError):
            arm(self.env, self.api, recover=True)
        self.assertFalse(any(b for _, b in self.calls))

    def test_unconfirmed_post_fails_closed_and_completed_successor_is_not_replayed(self):
        def api(path, body=None):
            if body:
                return None
            return self.api(path, body)
        with self.assertRaises(RuntimeError):
            arm(self.env, api, pause=lambda _: None)
        self.entries.append({'id': 124, 'status': 'completed', 'display_title': 'Get clock 123'})
        self.assertEqual(arm(self.env, self.api), 'successor_already_recorded')


if __name__ == '__main__':
    unittest.main()
