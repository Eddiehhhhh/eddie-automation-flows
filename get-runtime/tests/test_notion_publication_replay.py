"""Read-only exact publication evidence; no new Notion write admission."""
import base64
import hashlib
from copy import deepcopy
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
bindings = types.ModuleType('get_private_runtime_bindings')
bindings.value = lambda module, key: {} if key in ('REVIEWED', 'NEWLY_REVIEWED', 'HELD') else None
original_bindings = sys.modules.get('get_private_runtime_bindings')
sys.modules['get_private_runtime_bindings'] = bindings
try:
    replay = importlib.import_module('get_notion_publication_replay')
finally:
    if original_bindings is None:
        sys.modules.pop('get_private_runtime_bindings', None)
    else:
        sys.modules['get_private_runtime_bindings'] = original_bindings
p = replay.p


class PublicationReplay(unittest.TestCase):
    def setUp(self):
        # Controller byte verification only inspects this wrapper's exact type;
        # the test client is not wrapped. Avoid loading unrelated private Notion
        # runtime bindings, without replacing controller or source verification.
        wrapper = types.SimpleNamespace(_ReadBudget=type('UnusedBudget', (), {}))
        budget_stub = patch.dict(sys.modules, {'get_notion_journal_runtime': wrapper,
                                             'get_private_runtime_bindings': bindings})
        budget_stub.start(); self.addCleanup(budget_stub.stop)
        self.policy = {'contract': p.POLICY_CONTRACT, 'repository': p.REPOSITORY,
            'repository_id': 123, 'workflow_id': 456, 'workflow_path': '.github/workflows/get-canonical.yml',
            'head_branch': 'main', 'code_sha': 'a'*40, 'job_name': p.controller.JOB_NAME,
            'publication_step_name': p.controller.STEP_NAME, 'rules_sha256': 'b'*64,
            'get_policy_sha256': 'c'*64, 'controller_template_sha256': p.controller.template_sha256()}
        self.policy_sha = p.a.sha(p.a.encoded(self.policy))
        self.controller = p.controller.expected_bytes(self.policy, scheduled=False)
        repository = {'id': 123, 'full_name': p.REPOSITORY, 'private': False}
        self.run = {'id': 100, 'run_attempt': 1, 'repository': repository,
            'head_repository': deepcopy(repository), 'workflow_id': 456,
            'path': self.policy['workflow_path'], 'head_branch': 'main', 'head_sha': 'd'*40,
            'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'failure',
            'created_at': '2026-11-06T01:00:00Z', 'updated_at': '2026-11-06T01:10:00Z'}
        self.job = {'id': 200, 'run_id': 100, 'run_attempt': 1, 'head_sha': 'd'*40,
            'name': p.controller.JOB_NAME, 'status': 'completed', 'conclusion': 'failure',
            'started_at': '2026-11-06T01:00:01Z', 'completed_at': '2026-11-06T01:09:00Z',
            'steps': [
                {'name': p.controller.STEP_NAME, 'number': 12, 'status': 'completed', 'conclusion': 'success',
                 'started_at': '2026-11-06T01:00:02Z', 'completed_at': '2026-11-06T01:05:00Z'},
                {'name': replay.NOTION_STEP, 'number': 13, 'status': 'completed', 'conclusion': 'failure',
                 'started_at': '2026-11-06T01:05:01Z', 'completed_at': '2026-11-06T01:08:00Z'}]}
        self.record = {'contract': p.CONTRACT, 'status': 'canonical_publication_verified',
            'run_id': 100, 'run_attempt': 1, 'code_sha': 'a'*40, 'rules_sha256': 'b'*64,
            'policy_sha256': 'c'*64, 'observed_main_commit': 'e'*40, 'receipt_commit': 'e'*40,
            'receipt_sha256': 'f'*64, 'receipt_sequence': 3, 'main_updated': True,
            'cas_attempts': 1, 'readback_verified': True, 'controller_sha': 'd'*40}
        self.record['record_sha256'] = p.a.sha(p.a.encoded(self.record))
        self.selection = {key: self.record[key] for key in replay.SELECTION_KEYS}
        self.raw = b'2026-11-06T01:04:59Z ' + p.MARKER.encode() + p.a.encoded(self.record)
        self.calls = []
        testcase = self
        class Client:
            def json(self, route):
                testcase.calls.append(route)
                if route in ('/actions/runs/100', '/actions/runs/100/attempts/1'):
                    return deepcopy(testcase.run)
                if route == '/actions/runs/100/attempts/1/jobs?per_page=100&page=1':
                    return {'total_count': 1, 'jobs': [deepcopy(testcase.job)]}
                if route == '/contents/' + testcase.policy['workflow_path'] + '?ref=' + 'd'*40:
                    data = testcase.controller
                    return {'type': 'file', 'path': testcase.policy['workflow_path'],
                        'size': len(data), 'encoding': 'base64',
                        'sha': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest(),
                        'content': base64.b64encode(data).decode()}
                raise AssertionError('unexpected transport: ' + route)
            def logs(self, job_id):
                testcase.assertEqual(job_id, 200)
                testcase.calls.append('logs/200')
                return testcase.raw
        self.client = Client()

    def verify(self):
        return replay.verify(self.client, self.policy, self.policy_sha, self.selection)

    def test_failed_notion_after_verified_get_returns_read_only_source_evidence(self):
        original = deepcopy(self.record)
        proof = self.verify()
        self.assertEqual(proof['record'], original)
        self.assertEqual(proof['source_run_id'], 100)
        self.assertEqual(proof['source_run_attempt'], 1)
        self.assertFalse(proof['write_ready'])
        self.assertEqual(proof['notion_outcome'], 'failure')
        self.assertEqual(self.record, original)
        self.assertFalse(any('biji' in route or 'getnote' in route for route in self.calls))
        self.assertEqual(self.calls.count('/actions/runs/100/attempts/1'), 2)
        self.assertEqual(self.calls.count('/actions/runs/100/attempts/1/jobs?per_page=100&page=1'), 2)

    def test_successful_ordinary_run_reuses_existing_publication_verifier(self):
        self.run['conclusion'] = self.job['conclusion'] = 'success'
        self.job['steps'][1]['conclusion'] = 'success'
        # Exact predecessor registrations are private, unrelated fixtures; the
        # existing success verifier still performs all origin/controller/log checks.
        with patch.object(p, 'effective_candidate_policy', return_value=(self.policy, False, False)), \
             patch.object(p.recovery.run53_recovery, 'clock_witness', return_value=None), \
             patch.object(p, 'verified_candidate', wraps=p.verified_candidate) as existing:
            result = self.verify()
        self.assertTrue(existing.called)
        self.assertEqual(result['record'], self.record)

    def test_unfinished_or_failed_get_is_rejected(self):
        for status, conclusion in [('in_progress', None), ('completed', 'failure'), ('completed', 'skipped')]:
            with self.subTest(status=status, conclusion=conclusion):
                self.job['steps'][0].update(status=status, conclusion=conclusion)
                with self.assertRaises(p.ProvenanceFailure): self.verify()
        self.assertNotIn('logs/200', self.calls)

    def test_wrong_repository_and_wrong_controller_are_rejected(self):
        original = deepcopy(self.run)
        self.run['repository']['full_name'] = 'other/repo'
        with self.assertRaises(p.ProvenanceFailure): self.verify()
        self.run = original
        self.controller += b'\n# unreviewed workflow\n'
        with self.assertRaises(p.ProvenanceFailure): self.verify()

    def test_wrong_selected_commit_receipt_record_or_attempt_is_rejected(self):
        original = deepcopy(self.selection)
        for key, value in [('receipt_commit', '0'*40), ('receipt_sha256', '0'*64),
                           ('record_sha256', '0'*64), ('run_attempt', 2)]:
            with self.subTest(key=key):
                self.selection = {**original, key: value}
                with self.assertRaises(p.ProvenanceFailure): self.verify()

    def test_record_outside_get_step_or_duplicate_is_rejected(self):
        original = self.raw
        self.raw = original.replace(b'01:04:59Z', b'01:06:00Z')
        with self.assertRaises(p.ProvenanceFailure): self.verify()
        self.raw = original + original
        with self.assertRaises(p.ProvenanceFailure): self.verify()

    def test_no_terminal_notion_failure_cannot_borrow_partial_job_exception(self):
        for conclusion in ('success', 'skipped', None):
            with self.subTest(conclusion=conclusion):
                self.job['steps'][1]['conclusion'] = conclusion
                with self.assertRaises(p.ProvenanceFailure): self.verify()

    def test_log_permission_failure_never_becomes_source_evidence(self):
        with patch.object(self.client, 'logs', side_effect=p.ProvenanceFailure('permission_denied')):
            with self.assertRaises(p.ProvenanceFailure): self.verify()

    def test_original_run_changed_on_readback_is_rejected(self):
        original = self.client.json
        count = 0
        def changed(route):
            nonlocal count
            value = original(route)
            if route == '/actions/runs/100/attempts/1':
                count += 1
                if count == 2: value['updated_at'] = '2026-11-06T01:11:00Z'
            return value
        with patch.object(self.client, 'json', side_effect=changed):
            with self.assertRaises(p.ProvenanceFailure): self.verify()

    def test_original_job_changed_on_readback_is_rejected(self):
        original = self.client.json
        count = 0
        def changed(route):
            nonlocal count
            value = original(route)
            if route.endswith('/jobs?per_page=100&page=1'):
                count += 1
                if count == 2:
                    value['jobs'][0]['steps'][0]['completed_at'] = '2026-11-06T01:04:58Z'
            return value
        with patch.object(self.client, 'json', side_effect=changed):
            with self.assertRaises(p.ProvenanceFailure): self.verify()

    def test_unapproved_policy_or_changed_record_is_rejected(self):
        with self.assertRaises(p.ProvenanceFailure):
            replay.verify(self.client, self.policy, '0'*64, self.selection)
        changed = {**self.record, 'receipt_sequence': 4}
        self.raw = b'2026-11-06T01:04:59Z ' + p.MARKER.encode() + p.a.encoded(changed)
        with self.assertRaises(p.ProvenanceFailure): self.verify()

    def _registered_binding(self):
        selection = {'run_id': 901, 'run_attempt': 1, 'record_sha256': '1'*64,
            'receipt_commit': '2'*40, 'receipt_sha256': '3'*64}
        return {'selection': selection, 'sequence': 3, 'job_id': 902,
            'source_policy': self.policy, 'source_policy_sha256': self.policy_sha}

    def _registered_proof(self, binding):
        selection = binding['selection']
        record = {'record_sha256': selection['record_sha256'],
            'receipt_commit': selection['receipt_commit'], 'receipt_sha256': selection['receipt_sha256'],
            'receipt_sequence': binding['sequence'], 'main_updated': True, 'cas_attempts': 1,
            'observed_main_commit': selection['receipt_commit'], 'readback_verified': True}
        return {'contract': replay.CONTRACT, 'record': record,
            'source_run_id': selection['run_id'], 'source_run_attempt': selection['run_attempt'],
            'source_job_id': binding['job_id'], 'source_policy_sha256': binding['source_policy_sha256'],
            'notion_outcome': 'failure'}

    def _prefer_replay(self, found=(), *, rotation_search=None, reserve=False, exclude=None,
                       proof=None, verify=None, binding=None, latest=None):
        provenance = p
        fixed = binding or self._registered_binding()
        fake_exact = types.SimpleNamespace(binding=lambda: deepcopy(fixed))
        fake_replay = types.SimpleNamespace(CONTRACT=replay.CONTRACT,
            verify=verify or (lambda *args: deepcopy(proof or self._registered_proof(latest or fixed))))
        with patch.dict(sys.modules, {'get_notion_exact_replay': fake_exact,
                                      'get_notion_publication_replay': fake_replay}), \
             patch.object(provenance.recovery, 'current_get_policy', return_value=self.policy['get_policy_sha256']), \
             patch.object(provenance, 'REGISTERED_FAILED_REPLAY_SEQUENCE', 4 if latest else -1), \
             patch.object(provenance, '_registered_failed_replay_selection', return_value=deepcopy(latest)):
            return provenance._prefer_registered_replay(self.client, self.policy, list(found),
                rotation_search=rotation_search, reserve_next_run=reserve,
                exclude_run_id=exclude[0] if exclude else None,
                exclude_run_attempt=exclude[1] if exclude else None, checked=2)

    def test_newer_failed_notion_receipt_supersedes_old_fixed_anchor(self):
        latest=self._registered_binding()
        latest['sequence']=4
        latest['selection']={**latest['selection'],'run_id':904,'record_sha256':'6'*64,
                             'receipt_commit':'7'*40,'receipt_sha256':'8'*64}
        with patch.object(p, 'verified_candidate', side_effect=AssertionError('failed job is not ordinary success')):
            result=self._prefer_replay([{'receipt_sequence':3,'anchor':{'commit':'2'*40,'receipt_sha256':'3'*64}}],latest=latest)
        self.assertEqual(result['receipt_sequence'],4)
        self.assertEqual(result['anchor'],{'commit':'7'*40,'receipt_sha256':'8'*64})
        self.assertIsNone(self._prefer_replay([result],latest=latest))
        with self.assertRaisesRegex(p.ProvenanceFailure,'provenance_receipt_fork'):
            self._prefer_replay([{'receipt_sequence':4,'anchor':{'commit':'9'*40,'receipt_sha256':'8'*64}}],latest=latest)
        with self.assertRaisesRegex(p.ProvenanceFailure,'provenance_no_verified_publication'):
            self._prefer_replay([],latest=latest,exclude=(904,1))

    def test_registered_sequence_three_supersedes_older_success_only_after_exact_replay(self):
        older = {'receipt_sequence': 2, 'anchor': {'commit': '4'*40, 'receipt_sha256': '5'*64}}
        with patch.object(p, 'verified_candidate', side_effect=AssertionError('must not reclassify source run')):
            result = self._prefer_replay([older])
        self.assertEqual((result['receipt_sequence'], result['status'], result['source_proof_contract']),
            (3, 'registered_replay_anchor_verified', replay.CONTRACT))
        self.assertEqual(result['anchor'], {'commit': '2'*40, 'receipt_sha256': '3'*64})

    def test_newer_success_and_same_sequence_fork_rules(self):
        newer = {'receipt_sequence': 4, 'anchor': {'commit': '6'*40, 'receipt_sha256': '7'*64}}
        with patch.object(p.recovery, 'current_get_policy', side_effect=AssertionError('newer success is sufficient')):
            self.assertIsNone(self._prefer_replay([newer]))
        same = {'receipt_sequence': 3, 'anchor': {'commit': '2'*40, 'receipt_sha256': '3'*64}}
        self.assertIsNone(self._prefer_replay([same]))
        fork = {'receipt_sequence': 3, 'anchor': {'commit': '8'*40, 'receipt_sha256': '9'*64}}
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_receipt_fork'):
            self._prefer_replay([fork])

    def test_rotation_reserve_and_current_attempt_never_borrow_fixed_anchor(self):
        older = {'receipt_sequence': 2, 'anchor': {'commit': '4'*40, 'receipt_sha256': '5'*64}}
        with self.assertRaisesRegex(p.ProvenanceFailure, 'rotation_source_unverified'):
            self._prefer_replay([older], rotation_search={'joint': True})
        self.assertIsNone(self._prefer_replay([older], reserve=True))
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
            self._prefer_replay([], exclude=(901, 1))

    def test_fixed_replay_requires_exact_original_proof_fields(self):
        binding = self._registered_binding()
        expected = self._registered_proof(binding)
        for field, bad in [('source_run_id', 999), ('source_run_attempt', 2),
                           ('source_job_id', 999), ('notion_outcome', 'success'),
                           ('source_policy_sha256', '0'*64)]:
            with self.subTest(field=field):
                changed = {**expected, field: bad}
                with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
                    self._prefer_replay(proof=changed)
        changed_record = {**expected, 'record': {**expected['record'], 'receipt_sha256': '0'*64}}
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
            self._prefer_replay(proof=changed_record)

    def test_fixed_binding_sequence_and_policy_must_match_current_registration(self):
        binding = self._registered_binding(); binding['sequence'] = 4
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_record'):
            self._prefer_replay([], binding=binding)
        with patch.object(p.recovery, 'current_get_policy', return_value='0'*64):
            # The helper patches this function internally for isolation, so use a
            # deliberately wrong runtime policy to exercise the production guard.
            wrong_policy = {**self.policy, 'get_policy_sha256': 'a'*64}
            fake_exact = types.SimpleNamespace(binding=lambda: self._registered_binding())
            fake_replay = types.SimpleNamespace(CONTRACT=replay.CONTRACT,
                verify=lambda *args: self._registered_proof(self._registered_binding()))
            with patch.dict(sys.modules, {'get_notion_exact_replay': fake_exact,
                                          'get_notion_publication_replay': fake_replay}), \
                 patch.object(p, 'REGISTERED_FAILED_REPLAY_SEQUENCE', -1):
                with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
                    p._prefer_registered_replay(self.client, wrong_policy, [], rotation_search=None,
                        reserve_next_run=False, exclude_run_id=None, exclude_run_attempt=None, checked=0)


if __name__ == '__main__': unittest.main()
