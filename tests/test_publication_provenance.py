"""Synthetic public API/log boundary tests and real-Git provenance integration."""
from copy import deepcopy
import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_publication_provenance as p
from get_publication_provenance import a, entry
import test_recurring_entry as git_fixture


def policy(**changes):
    value = {'contract': p.POLICY_CONTRACT, 'repository': p.REPOSITORY, 'repository_id': 100,
             'workflow_id': 21, 'workflow_path': '.github/workflows/get-canonical.yml',
             'head_branch': 'main', 'code_sha': 'a' * 40, 'job_name': 'canonical-get',
             'publication_step_name': 'Emit verified canonical publication anchor',
             'rules_sha256': 'b' * 64, 'get_policy_sha256': 'c' * 64,
             'controller_template_sha256': p.controller.template_sha256()}
    return {**value, **changes}


def run(ident=1001, attempt=1, minute=5, **changes):
    repo = {'id': 100, 'full_name': p.REPOSITORY, 'private': False}
    value = {'id': ident, 'run_attempt': attempt, 'workflow_id': 21,
             'path': '.github/workflows/get-canonical.yml', 'head_sha': 'a' * 40,
             'head_branch': 'main', 'repository': repo, 'head_repository': deepcopy(repo),
             'event': 'schedule', 'status': 'completed', 'conclusion': 'success',
             'created_at': '2026-10-16T10:00:00Z', 'updated_at': '2026-10-16T10:' + str(minute).zfill(2) + ':00Z'}
    return {**value, **changes}


def record(row=None, **changes):
    row = row or run()
    value = {'contract': p.CONTRACT, 'status': 'canonical_publication_verified',
             'run_id': row['id'], 'run_attempt': row['run_attempt'], 'code_sha': 'a' * 40, 'controller_sha': row['head_sha'],
             'rules_sha256': 'b' * 64, 'policy_sha256': 'c' * 64,
             'observed_main_commit': 'd' * 40, 'receipt_commit': 'd' * 40,
             'receipt_sha256': 'e' * 64, 'receipt_sequence': 1,
             'main_updated': True, 'cas_attempts': 1, 'readback_verified': True}
    value.update(changes); value.pop('record_sha256', None)
    return {**value, 'record_sha256': a.sha(a.encoded(value))}


class FakeAPI:
    def __init__(self, runs=None, records=None, attempts=None):
        self.runs = runs or [run()]
        self.records = records or {row['id']: record(row) for row in self.runs}
        self.attempts = dict(attempts or {}); self.job_attempts = {}
        self.called = []; self.overrides = {}; self.log_overrides = {}; self.policy = policy()

    def json(self, path):
        self.called.append(path)
        if path in self.overrides:
            value = self.overrides[path]
            return value() if callable(value) else deepcopy(value)
        if path.startswith('/contents/'):
            raw = p.controller.expected_bytes(self.policy)
            return {'type': 'file', 'path': self.policy['workflow_path'], 'encoding': 'base64',
                    'size': len(raw), 'sha': p.entry.r.oid(raw), 'content': base64.b64encode(raw).decode()}
        if path == '':
            return {'id': 100, 'full_name': p.REPOSITORY, 'private': False}
        if path == '/actions/workflows/21':
            return {'id': 21, 'path': '.github/workflows/get-canonical.yml'}
        if path.startswith('/actions/workflows/21/runs?'):
            return {'total_count': len(self.runs), 'workflow_runs': deepcopy(self.runs)}
        pieces = path.split('/')
        ident = int(pieces[3]); row = next(row for row in self.runs if row['id'] == ident)
        if '/attempts/' in path:
            attempt = int(pieces[5])
            row = self.attempts.get((ident, attempt), row)
            if row['run_attempt'] != attempt: raise KeyError((ident, attempt))
        if '/jobs?' in path:
            job_id = ident + 500 + (row['run_attempt'] - 1) * 100000
            self.job_attempts[job_id] = (ident, row['run_attempt'])
            return {'total_count': 1, 'jobs': [{'id': job_id, 'run_id': ident, 'run_attempt': row['run_attempt'],
                'head_sha': row['head_sha'], 'name': 'canonical-get', 'status': 'completed', 'conclusion': 'success',
                'completed_at': row['updated_at'], 'steps': [{'name': 'Emit verified canonical publication anchor',
                    'status': 'completed', 'conclusion': 'success', 'number': 5}]}]}
        return deepcopy(row)

    def logs(self, job_id):
        self.called.append('logs:' + str(job_id))
        if job_id in self.log_overrides:
            return self.log_overrides[job_id]
        ident, attempt = self.job_attempts.get(job_id, (job_id - 500, 1))
        value = self.records.get((ident, attempt), self.records.get(ident))
        return ('2026-10-16T10:04:59.1234567Z ' + p.MARKER + a.encoded(value).decode()).encode()


class ProvenanceTests(unittest.TestCase):
    def test_policy_is_exact_independently_pinned_input(self):
        value = policy(); expected = a.sha(a.encoded(value))
        self.assertEqual(p.validate_policy(value, expected), value)
        for bad in ({**value, 'repository': 'Other/repo'}, {**value, 'workflow_path': '../workflow.yml'},
                    {**value, 'code_sha': 'private note'}, {**value, 'extra': 'private'}):
            with self.assertRaises(a.ParityFailure):
                p.validate_policy(bad, expected)
        with self.assertRaises(a.ParityFailure):
            p.validate_policy(value, 'f' * 64)

    def test_exact_run_attempt_success_and_single_public_hash_record(self):
        api = FakeAPI(); result = p.discover(api, policy())
        self.assertEqual(result['anchor'], {'commit': 'd' * 40, 'receipt_sha256': 'e' * 64})
        self.assertEqual(result['run_attempt'], 1); self.assertEqual(result['job_id'], 1501)
        self.assertEqual(result['credentials_used'], 0)
        self.assertIn('/actions/runs/1001/attempts/1/jobs?per_page=100&page=1', api.called)
        self.assertEqual(api.called[-1], '/actions/runs/1001')

    def test_latest_sequence_wins_even_when_older_run_is_rerun(self):
        rows = [run(1002, minute=5), run(1001, attempt=2, minute=9)]
        api = FakeAPI(rows, {1002: record(rows[0], receipt_sequence=2),
                            1001: record(rows[1], receipt_sequence=3, receipt_commit='f' * 40, observed_main_commit='f' * 40)})
        result = p.discover(api, policy())
        self.assertEqual(result['run_id'], 1001); self.assertEqual(result['run_attempt'], 2)
        self.assertEqual(result['receipt_sequence'], 3)

    def test_duplicate_sequence_with_conflicting_anchor_fails(self):
        rows = [run(1001), run(1002)]
        api = FakeAPI(rows, {1001: record(rows[0]), 1002: record(rows[1], receipt_sha256='f' * 64)})
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_receipt_fork'):
            p.discover(api, policy())

    def test_private_or_recreated_repository_and_wrong_workflow_fail(self):
        for path, value in [('', {'id': 101, 'full_name': p.REPOSITORY, 'private': False}),
                            ('', {'id': 100, 'full_name': p.REPOSITORY, 'private': True}),
                            ('/actions/workflows/21', {'id': 21, 'path': '.github/workflows/foreign.yml'})]:
            api = FakeAPI(); api.overrides[path] = value
            with self.assertRaises(p.ProvenanceFailure):
                p.discover(api, policy())
            self.assertFalse(any(x.startswith('logs:') for x in api.called))

    def test_foreign_code_branch_fork_event_and_non_success_never_anchor(self):
        cases = [{'head_branch': 'other'}, {'event': 'pull_request'},
                 {'conclusion': 'failure'}, {'head_repository': {'id': 101, 'full_name': p.REPOSITORY, 'private': False}}]
        for changes in cases:
            api = FakeAPI([run(**changes)])
            with self.assertRaises(p.ProvenanceFailure):
                p.discover(api, policy())
            self.assertFalse(any(x.startswith('logs:') for x in api.called))

    def test_only_exact_current_run_attempt_is_excluded(self):
        api = FakeAPI()
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
            p.discover(api, policy(), exclude_run_id=1001, exclude_run_attempt=1)
        self.assertFalse(any('/attempts/' in x for x in api.called))

    def test_new_rerun_during_log_retrieval_does_not_inherit_original_success(self):
        api = FakeAPI(); api.overrides['/actions/runs/1001'] = run(attempt=2, minute=6, status='in_progress', conclusion=None)
        result = p.discover(api, policy())
        self.assertEqual(result['run_attempt'], 1)
        self.assertEqual(api.called.count('/actions/runs/1001/attempts/1'), 2)

    def test_mismatched_attempt_endpoint_fails_before_logs(self):
        api = FakeAPI(); api.overrides['/actions/runs/1001/attempts/1'] = run(attempt=2)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_run_changed'):
            p.discover(api, policy())
        self.assertFalse(any(x.startswith('logs:') for x in api.called))

    def test_record_prepare_unconfirmed_tampered_foreign_or_duplicate_is_rejected(self):
        cases = [record(status='recurring_local_candidate_verified'), record(readback_verified=False),
                 record(run_attempt=2), record(code_sha='f' * 40), record(rules_sha256='a' * 64),
                 {**record(), 'record_sha256': 'a' * 64}, {**record(), 'private_title': 'secret'}]
        for value in cases:
            api = FakeAPI(records={1001: value})
            with self.assertRaises(a.ParityFailure):
                p.discover(api, policy())
        api = FakeAPI(); line = p.publication_line(record()).encode() + b'\n'
        for raw in (b'No publication happened', line + line, b'\xff', line + b'\n' + p.MARKER.encode() + b'{}'):
            api.log_overrides[1501] = raw
            with self.assertRaises(a.ParityFailure):
                p.discover(api, policy())

    def test_noop_record_keeps_receipt_commit_separate_from_observed_main(self):
        value = record(main_updated=False, cas_attempts=0, observed_main_commit='f' * 40)
        result = p.discover(FakeAPI(records={1001: value}), policy())
        self.assertEqual(result['anchor']['commit'], 'd' * 40)

    def test_publication_line_cannot_echo_arbitrary_private_values(self):
        with self.assertRaises(a.ParityFailure):
            p.publication_line(record(code_sha='private note title'))

    def test_job_wrong_id_sha_status_or_ambiguous_steps_fail(self):
        path = '/actions/runs/1001/attempts/1/jobs?per_page=100&page=1'
        for changes in ({'run_id': 999}, {'head_sha': 'f' * 40}, {'status': 'in_progress'}, {'run_attempt': 2}):
            api = FakeAPI(); value = api.json(path); value['jobs'][0].update(changes); api.overrides[path] = value
            with self.assertRaises(p.ProvenanceFailure):
                p.discover(api, policy())
        api = FakeAPI(); value = api.json(path); value['jobs'][0]['steps'] *= 2; api.overrides[path] = value
        with self.assertRaises(p.ProvenanceFailure):
            p.discover(api, policy())

    def test_nonpublication_mode_is_not_an_anchor(self):
        api = FakeAPI(); path = '/actions/runs/1001/attempts/1/jobs?per_page=100&page=1'
        value = api.json(path); value['jobs'][0]['steps'][0]['conclusion'] = 'skipped'; api.overrides[path] = value
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
            p.discover(api, policy())
        self.assertFalse(any(x.startswith('logs:') for x in api.called))

    def test_job_pagination_and_log_size_limits_fail_closed(self):
        api = FakeAPI(); path = '/actions/runs/1001/attempts/1/jobs?per_page=100&page=1'
        api.overrides[path] = {'total_count': 101, 'jobs': []}
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_job_bound'):
            p.discover(api, policy())
        api = FakeAPI(); api.log_overrides[1501] = b'x' * (p.MAX_LOG + 1)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'):
            p.discover(api, policy())

    def test_candidates_and_requests_are_bounded(self):
        rows = [run(1000 + i, minute=i) for i in range(1, 9)]
        api = FakeAPI(rows); result = p.discover(api, policy())
        self.assertEqual(result['candidate_runs_checked'], p.MAX_CANDIDATES)
        self.assertEqual(len([x for x in api.called if x.startswith('logs:')]), p.MAX_CANDIDATES)


class AttemptRecoveryTests(unittest.TestCase):
    def rerun(self, **changes):
        first = run()
        latest = run(attempt=2, minute=9, conclusion='failure', **changes)
        return FakeAPI([latest], {1001: record(first)}, {(1001, 1): first})

    def test_new_run_recovers_successful_first_attempt_after_failed_second(self):
        api = self.rerun()
        result = p.discover(api, policy(), exclude_run_id=1002, exclude_run_attempt=1)
        self.assertEqual((result['run_id'], result['run_attempt']), (1001, 1))
        self.assertEqual(result['candidate_runs_checked'], 2)
        self.assertEqual(result['publication_records_verified'], 1)
        self.assertNotIn('status=success', ''.join(api.called))
        self.assertEqual(api.called.count('/actions/runs/1001/attempts/1'), 2)
        self.assertEqual(len([x for x in api.called if x.startswith('logs:')]), 1)
        self.assertFalse(any('/attempts/2/jobs' in x for x in api.called))

    def test_same_run_in_progress_second_attempt_can_recover_first(self):
        first = run(); current = run(attempt=2, minute=9, status='in_progress', conclusion=None)
        api = FakeAPI([current], {1001: record(first)}, {(1001, 1): first})
        result = p.discover(api, policy(), exclude_run_id=1001, exclude_run_attempt=2)
        self.assertEqual(result['run_attempt'], 1)
        self.assertEqual(result['candidate_runs_checked'], 1)
        self.assertNotIn('/actions/runs/1001/attempts/2', api.called)

    def test_in_progress_other_run_rerun_can_recover_first(self):
        first = run(); current = run(attempt=2, minute=9, status='in_progress', conclusion=None)
        api = FakeAPI([current], {1001: record(first)}, {(1001, 1): first})
        result = p.discover(api, policy(), exclude_run_id=2000, exclude_run_attempt=1)
        self.assertEqual(result['run_attempt'], 1)

    def test_both_exclusion_fields_are_required_and_validated_before_reads(self):
        for args in ({'exclude_run_id': 1001}, {'exclude_run_attempt': 1},
                     {'exclude_run_id': 1001, 'exclude_run_attempt': 0},
                     {'exclude_run_id': True, 'exclude_run_attempt': 1}):
            api = FakeAPI()
            with self.assertRaises(p.ProvenanceFailure): p.discover(api, policy(), **args)
            self.assertEqual(api.called, [])

    def test_original_attempt_mutation_after_logs_is_rejected(self):
        for change in ({'updated_at': '2026-10-16T10:06:00Z'}, {'conclusion': 'failure'},
                       {'run_attempt': 2}, {'head_sha': 'f' * 40},
                       {'repository': {'id': 100, 'full_name': p.REPOSITORY, 'private': True}}):
            api = self.rerun(); rows = [run(), run(**change)]
            api.overrides['/actions/runs/1001/attempts/1'] = lambda: rows.pop(0)
            with self.assertRaises(p.ProvenanceFailure): p.discover(api, policy())
            self.assertIn('logs:1501', api.called)

    def test_latest_foreign_controller_identity_or_attempt_regression_rejected(self):
        for change in ({'head_sha': 'f' * 40}, {'id': 2000}, {'path': '.github/workflows/foreign.yml'},
                       {'event': 'workflow_dispatch'}, {'created_at': '2026-10-16T09:00:00Z'},
                       {'run_attempt': 1}, {'updated_at': '2026-10-16T10:08:00Z'},
                       {'head_repository': {'id': 999, 'full_name': p.REPOSITORY, 'private': False}}):
            api = self.rerun()
            api.overrides['/actions/runs/1001'] = {**api.runs[0], **change}
            with self.assertRaises(p.ProvenanceFailure): p.discover(api, policy())
            self.assertIn('logs:1501', api.called)

    def test_historical_attempt_with_different_controller_is_rejected_before_logs(self):
        api = self.rerun(); api.attempts[(1001, 1)] = run(head_sha='f' * 40)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_run_changed'):
            p.discover(api, policy())
        self.assertFalse(any(x.startswith('logs:') for x in api.called))

    def test_second_attempt_cannot_borrow_first_record_even_when_both_successful(self):
        api = self.rerun(); api.runs[0]['conclusion'] = 'success'
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_record'):
            p.discover(api, policy())
        self.assertFalse(any('/attempts/1' in x for x in api.called))

    def test_failed_original_attempt_never_authorizes_get_publication(self):
        api = self.rerun(); api.attempts[(1001, 1)] = run(conclusion='failure')
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
            p.discover(api, policy())
        self.assertFalse(any(x.startswith('logs:') for x in api.called))

    def test_four_attempt_window_never_falls_through_to_fifth(self):
        rows = {(1001, i): run(attempt=i, conclusion='failure') for i in range(2, 6)}
        rows[(1001, 1)] = run()
        api = FakeAPI([rows[(1001, 5)]], {1001: record()}, rows)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
            p.discover(api, policy())
        self.assertEqual(len([x for x in api.called if '/attempts/' in x]), 4)
        self.assertNotIn('/actions/runs/1001/attempts/1', api.called)

    def test_same_controller_is_verified_once_per_discovery_not_persistently(self):
        api = FakeAPI([run(1000 + i) for i in range(4)])
        for _ in range(2): p.discover(api, policy())
        self.assertEqual(len([x for x in api.called if x.startswith('/contents/')]), 2)
        self.assertEqual(len([x for x in api.called if x.startswith('logs:')]), 8)

    def test_execution_plus_three_pages_four_candidates_fits_original_physical_cap(self):
        rows = [run(1000 + i) for i in range(90)]
        api = FakeAPI(rows)
        execution = {'run_id': 2000, 'run_attempt': 1, 'code_sha': 'a' * 40, 'controller_sha': 'a' * 40}
        api.overrides['/actions/runs/2000/attempts/1'] = run(2000, status='in_progress', conclusion=None)
        for page in range(1, 4):
            api.overrides['/actions/workflows/21/runs?per_page=30&page=' + str(page)] = {
                'total_count': 90, 'workflow_runs': rows[(page - 1) * 30:page * 30]}
        class Opener:
            def open(self, request, timeout):
                url = request.full_url
                if url.startswith('https://logs.blob.core.windows.net/'):
                    return Response(200, api.logs(int(url.rsplit('/', 1)[1])))
                if not url.startswith(p.API): raise AssertionError('unexpected host')
                path = url[len(p.API):]
                if path.startswith('/actions/jobs/') and path.endswith('/logs'):
                    job_id = int(path.split('/')[3])
                    return Response(302, headers={'Location': 'https://logs.blob.core.windows.net/' + str(job_id)})
                return Response(200, a.encoded(api.json(path)))
        client = p.PublicGithub('synthetic-existing-runtime-token', opener=Opener())
        p.verify_execution(client, policy(), execution, 'schedule')
        result = p.discover(client, policy(), exclude_run_id=2000, exclude_run_attempt=1)
        self.assertEqual(result['candidate_runs_checked'], 4)
        self.assertEqual(result['publication_records_verified'], 4)
        self.assertEqual(client.requests, 32)
        self.assertEqual(len([x for x in api.called if '/runs?per_page=' in x]), 3)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'):
            client.json('')
        self.assertEqual(client.requests, 32)

    def test_anonymous_cli_failure_has_safe_report_instead_of_nameerror(self):
        with patch('builtins.print') as output:
            result = p.main(['--policy', '/no/such/policy', '--approved-policy-sha256', 'a' * 64])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.call_args.args[0]),
                         {'status': 'public_provenance_failed', 'new_permissions': 0, 'credentials_used': 0})


class Response:
    def __init__(self, status, body=b'{}', headers=None):
        self.status = status; self.body = body; self.headers = headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, cap): return self.body[:cap]


class TransportTests(unittest.TestCase):
    def client(self, responses):
        class Opener:
            def __init__(self): self.requests = []
            def open(self, request, timeout):
                self.requests.append(request)
                return responses.pop(0)
        opener = Opener()
        return p.AnonymousGithub(opener=opener, sleep=lambda _: None), opener

    def test_no_authorization_cookie_or_ambient_token_and_safe_redirect_only(self):
        client, opener = self.client([Response(302, headers={'Location': 'https://example.blob.core.windows.net/public/log?signed=ephemeral'}), Response(200, b'logs')])
        with patch.dict('os.environ', {'GITHUB_TOKEN': 'must-not-be-read', 'PRIVATE_VAULT_TOKEN': 'must-not-be-read'}):
            self.assertEqual(client.logs(123), b'logs')
        for request in opener.requests:
            headers = {k.lower(): v for k, v in request.header_items()}
            self.assertNotIn('authorization', headers); self.assertNotIn('cookie', headers)
            self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(client.requests, 2)

    def test_json_root_read_has_fixed_public_origin(self):
        client, opener = self.client([Response(200)])
        self.assertEqual(client.json(''), {})
        self.assertEqual(opener.requests[0].full_url, p.API)

    def test_redirect_http_foreign_userinfo_and_second_redirect_fail(self):
        for location in ('http://example.blob.core.windows.net/log', 'https://evil.example/log',
                         'https://user:secret@example.blob.core.windows.net/log', 'https://example.blob.core.windows.net:444/log'):
            client, _ = self.client([Response(302, headers={'Location': location})])
            with self.assertRaises(p.ProvenanceFailure): client.logs(123)
            self.assertEqual(client.requests, 1)
        client, _ = self.client([Response(302, headers={'Location': 'https://example.blob.core.windows.net/log'}), Response(302)])
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_logs_unavailable'): client.logs(123)
        self.assertEqual(client.requests, 2)

    def test_retry_only_recoverable_transient_errors_not_denials_or_rate_limits(self):
        client, _ = self.client([Response(503), Response(200)])
        self.assertEqual(client.json(''), {}); self.assertEqual(client.requests, 2)
        for code in (401, 403, 404, 410, 429):
            client, _ = self.client([Response(code)])
            with self.assertRaises(p.ProvenanceFailure): client.json('')
            self.assertEqual(client.requests, 1)

    def test_response_budget_and_deadline(self):
        client, _ = self.client([Response(200, b'x' * (p.MAX_JSON + 1))])
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'): client.json('')
        client, _ = self.client([Response(200)]); client.requests = p.MAX_REQUESTS
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'): client.json('')
        client, _ = self.client([Response(200)]); client.elapsed = p.MAX_SECONDS + 1
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'): client.json('')


class RealGitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = git_fixture.RecurringEntryTests(methodName='test_bootstrap_one_direct_candidate_contains_receipt_preserves_refs_and_index')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def data(self, plan, files):
        value = p.publication_record(self.f.reader, self.f.evidence, plan, files,
            {'main_updated': bool(files), 'cas_attempts': int(bool(files)), 'readback_verified': True},
            self.f.rules, run_id=1001, run_attempt=1, code_sha='a' * 40, bootstrap=self.f.bootstrap)
        pol = policy(rules_sha256=self.f.rules, get_policy_sha256=plan['policy_sha256'])
        api = FakeAPI(records={1001: value}); api.policy = pol
        return value, pol, api

    def test_verified_public_record_drives_next_entry_without_manual_receipt_hash(self):
        plan, files = self.f.prepare(); value, pol, api = self.data(plan, files)
        self.assertEqual(p.parse_record(p.publication_line(value).encode(), pol, run()), value)
        source = self.f.f.source()
        again, changes = p.prepare_with_public_provenance(self.f.reader, self.f.evidence, plan['candidate_commit'], source,
            api, pol, a.sha(a.encoded(pol)), bootstrap=self.f.bootstrap)
        self.assertEqual(changes, {}); self.assertEqual(again['receipt_commit'], plan['candidate_commit'])
        self.assertEqual(source.ids, []); self.assertEqual(source.lists, 1)

    def test_stale_public_anchor_cannot_skip_a_later_private_get_transaction(self):
        first, files = self.f.prepare(); _, pol, api = self.data(first, files)
        notes = deepcopy(self.f.f.notes); notes['200']['content'] = 'Newer source'
        second, _ = self.f.prepare(first['candidate_commit'], self.f.anchor(first), self.f.f.source(notes))
        source = self.f.f.source(notes)
        with self.assertRaisesRegex(entry.EntryFailure, 'entry_foreign_get_change'):
            p.prepare_with_public_provenance(self.f.reader, self.f.evidence, second['candidate_commit'], source,
                api, pol, a.sha(a.encoded(pol)), bootstrap=self.f.bootstrap)
        self.assertEqual(source.lists, 0)

    def test_first_exact_bootstrap_does_not_need_prior_run_or_network(self):
        pol = policy(rules_sha256=self.f.rules, get_policy_sha256=entry.policy_sha256(self.f.rules, self.f.bootstrap))
        class NoNetwork:
            def json(self, path): raise AssertionError('No external provenance exists before first publication')
        plan, files = p.prepare_with_public_provenance(self.f.reader, self.f.evidence, self.f.parent, self.f.f.source(),
            NoNetwork(), pol, a.sha(a.encoded(pol)), preview=self.f.f.preview, bootstrap=self.f.bootstrap)
        self.assertEqual(plan['proposed_content_ids'], ['200', '201']); self.assertIn(entry.FULL_RECEIPT, files)

    def test_unconfirmed_result_cannot_emit_publication_anchor(self):
        plan, files = self.f.prepare()
        with self.assertRaises(entry.EntryFailure):
            p.publication_record(self.f.reader, self.f.evidence, plan, files,
                {'main_updated': True, 'cas_attempts': 1, 'readback_verified': False},
                self.f.rules, run_id=1001, run_attempt=1, code_sha='a' * 40, bootstrap=self.f.bootstrap)


if __name__ == '__main__': unittest.main()
