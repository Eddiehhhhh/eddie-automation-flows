"""Self-contained synthetic locator regression, no private capsule or network.

Loads unchanged function ASTs from the actual source under test, not copied
implementations. Controller/policy/opaque-private-proof adapters are explicit
synthetic seams; run/attempt/job/log/record/alias and discovery code are real.
Private registered-proof and current-Get-history integration are separate gates.
"""
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
# Standalone pre-release placement; published location is tests/test_*.py.
if not (ROOT / 'scripts').is_dir():
    ROOT = ROOT / 'runtime'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def checked(value, width):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(width) + '}', value):
        raise ValueError('synthetic_hash_invalid')
    return value


def load_runtime():
    module = ModuleType('synthetic_locator_provenance')
    module.__dict__.update(deepcopy=deepcopy, datetime=datetime, timedelta=timedelta,
                          timezone=timezone, re=re, time=time, urllib=urllib)
    module.a = SimpleNamespace(ParityFailure=ValueError, checked=checked,
        encoded=encoded, sha=lambda raw: hashlib.sha256(raw).hexdigest())
    module.m = SimpleNamespace(strict_capture_json=lambda raw, maximum: json.loads(raw))
    module.HISTORY_WITNESS = None
    module.recovery = SimpleNamespace(candidate_policy=lambda policy, run: (policy, False),
        run53_recovery=SimpleNamespace(clock_witness=lambda run, listed: None))
    module.registered_predecessor = SimpleNamespace(discovery_locator=lambda policy: None,
        candidate_policy=lambda policy, run: (policy, False))
    module.private_proof = SimpleNamespace(expand_for=lambda client, raw, **kw: raw)
    def controller(client, sha, policy, **kw):
        if client.json('/synthetic-controller/' + sha) != {'approved': True}:
            module.fail('provenance_controller_mismatch')
    module.controller = SimpleNamespace(verify_remote=controller)
    names = {'ProvenanceFailure', 'fail', 'positive', 'timestamp', 'InspectRedirect',
        '_PublicGithub', 'PublicGithub', 'parse_record', 'verified_prepare_only',
        'check_run_origin', 'check_run_identity', 'check_run', 'run_identity',
        'run_binding', 'run_semantic_binding', 'effective_candidate_policy',
        'verified_candidate', 'discover'}
    constants = {'REPOSITORY', 'API', 'CONTRACT', 'MARKER', 'MAX_JSON', 'MAX_LOG',
        'MAX_TOTAL', 'MAX_REQUESTS', 'MAX_RUN_PAGES', 'RUN_PAGE_SIZE',
        'MAX_CANDIDATES', 'MAX_SECONDS', 'RECORD_KEYS'}
    path = ROOT / 'scripts/get_publication_provenance.py'
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body if
        isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names or
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets)]
    assert {node.name for node in nodes if isinstance(node, (ast.FunctionDef, ast.ClassDef))} == names
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), module.__dict__)
    return module


def policy(p):
    return {'repository': p.REPOSITORY, 'repository_id': 100, 'workflow_id': 21,
        'workflow_path': '.github/workflows/synthetic.yml', 'head_branch': 'main',
        'job_name': 'synthetic-get', 'publication_step_name': 'Emit synthetic publication',
        'code_sha': 'a' * 40, 'rules_sha256': 'b' * 64, 'get_policy_sha256': 'c' * 64}


def run(p, ident=1001, attempt=1, minute=5, **changes):
    repo = {'id': 100, 'full_name': p.REPOSITORY, 'private': False}
    return {'id': ident, 'run_attempt': attempt, 'workflow_id': 21,
        'path': '.github/workflows/synthetic.yml', 'head_sha': 'a' * 40,
        'head_branch': 'main', 'repository': repo, 'head_repository': deepcopy(repo),
        'event': 'schedule', 'status': 'completed', 'conclusion': 'success',
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:' + str(minute).zfill(2) + ':00Z', **changes}


def record(p, row, **changes):
    value = {'contract': p.CONTRACT, 'status': 'canonical_publication_verified',
        'run_id': row['id'], 'run_attempt': row['run_attempt'], 'controller_sha': row['head_sha'],
        'code_sha': 'a' * 40, 'rules_sha256': 'b' * 64, 'policy_sha256': 'c' * 64,
        'observed_main_commit': 'd' * 40, 'receipt_commit': 'd' * 40,
        'receipt_sha256': 'e' * 64, 'receipt_sequence': 1, 'main_updated': True,
        'cas_attempts': 1, 'readback_verified': True, **changes}
    return {**value, 'record_sha256': p.a.sha(encoded(value))}


class API:
    def __init__(self, p, rows):
        self.p = p; self.rows = rows; self.calls = []; self.skipped = set()
        self.records = {row['id']: record(p, row) for row in rows}
        self.overrides = {}; self.attempts = {}; self.jobs = {}
    def json(self, path):
        self.calls.append(path)
        if path in self.overrides:
            value = self.overrides[path]
            if isinstance(value, Exception): raise value
            return deepcopy(value)
        if path == '': return {'id': 100, 'full_name': self.p.REPOSITORY, 'private': False}
        if path == '/actions/workflows/21': return {'id': 21, 'path': policy(self.p)['workflow_path']}
        if path.startswith('/actions/workflows/21/runs?'):
            page = int(path.rsplit('=', 1)[1])
            return {'total_count': len(self.rows), 'workflow_runs': deepcopy(self.rows[(page-1)*30:page*30])}
        if path.startswith('/synthetic-controller/'): return {'approved': True}
        ident = int(path.split('/')[3]); row = next(r for r in self.rows if r['id'] == ident)
        if '/attempts/' in path:
            attempt = int(path.split('/')[5]); row = self.attempts.get((ident, attempt), row)
        if '/jobs?' in path:
            job = ident + row['run_attempt'] * 100000; self.jobs[job] = (ident, row['run_attempt'])
            return {'total_count': 1, 'jobs': [{'id': job, 'run_id': ident,
                'run_attempt': row['run_attempt'], 'head_sha': row['head_sha'],
                'name': 'synthetic-get', 'status': 'completed', 'conclusion': 'success',
                'completed_at': row['updated_at'], 'steps': [{'name': policy(self.p)['publication_step_name'],
                'status': 'completed', 'conclusion': 'skipped' if ident in self.skipped else 'success'}]}]}
        return deepcopy(row)
    def logs(self, job):
        self.calls.append('logs:' + str(job)); ident, attempt = self.jobs[job]
        value = self.records.get((ident, attempt), self.records[ident])
        return self.p.MARKER.encode() + encoded(value)


class LocatorTests(unittest.TestCase):
    def setUp(self): self.p = load_runtime()
    def discover(self, api, **kw):
        with patch.object(self.p.registered_predecessor, 'discovery_locator', return_value=(1001, 1)):
            return self.p.discover(api, policy(self.p), **kw)
    def test_historical_9_and_95_use_only_four_original_slots(self):
        for count in (9, 95):
            rows = [run(self.p, 2000+i, minute=9) for i in range(count)] + [run(self.p, minute=1)]
            api = API(self.p, rows); api.skipped = {r['id'] for r in rows[:-1]}
            result = self.discover(api)
            self.assertEqual((result['run_id'], result['candidate_runs_checked']), (1001, 4))
            self.assertEqual(sum('/jobs?' in call for call in api.calls), 4)
            self.assertLessEqual(sum('/runs?per_page=' in call for call in api.calls), 3)
    def test_concurrent_inprogress_run_charges_slot_and_self_is_excluded(self):
        rows = [run(self.p, 2001, minute=9, status='in_progress', conclusion=None),
                run(self.p, 2002, minute=8, status='in_progress', conclusion=None),
                run(self.p, 2003, minute=7), run(self.p, 2004, minute=6), run(self.p, minute=1)]
        api = API(self.p, rows); api.skipped = {2003, 2004}
        result = self.discover(api, exclude_run_id=2002, exclude_run_attempt=1)
        self.assertEqual((result['run_id'], result['candidate_runs_checked']), (1001, 4))
        self.assertIn('/actions/runs/2001/attempts/1', api.calls)
        self.assertNotIn('/actions/runs/2002/attempts/1', api.calls)
    def test_reserve_uses_one_of_original_three_slots(self):
        result = self.discover(API(self.p, [run(self.p)]), reserve_next_run=True)
        self.assertEqual(result, {'status': 'public_provenance_capacity_verified', 'candidate_runs_checked': 1})
    def test_exact_hint_self_exclusion(self):
        api = API(self.p, [run(self.p)])
        with self.assertRaisesRegex(ValueError, 'provenance_no_verified_publication'):
            self.discover(api, exclude_run_id=1001, exclude_run_attempt=1)
        self.assertFalse(any('/attempts/' in call for call in api.calls))
    def test_newer_sequence_wins(self):
        rows = [run(self.p, 1002, minute=9), run(self.p, minute=1)]; api = API(self.p, rows)
        api.records[1002] = record(self.p, rows[0], receipt_sequence=2)
        result = self.discover(api); self.assertEqual((result['run_id'], result['candidate_runs_checked']), (1002, 2))
    def test_same_sequence_fork_rejected(self):
        rows = [run(self.p, 1002, minute=9), run(self.p, minute=1)]; api = API(self.p, rows)
        api.records[1002] = record(self.p, rows[0], receipt_sha256='f'*64)
        with self.assertRaisesRegex(ValueError, 'provenance_receipt_fork'): self.discover(api)
    def test_skipped_locator_cannot_fall_back(self):
        api = API(self.p, [run(self.p, 1002, minute=9), run(self.p)]); api.skipped.add(1001)
        with self.assertRaisesRegex(ValueError, 'provenance_no_verified_publication'): self.discover(api)
        self.assertNotIn('/actions/runs/1002/attempts/1', api.calls)
    def test_missing_or_denied_locator_cannot_fall_back(self):
        for message in ('provenance_unavailable', 'provenance_transport'):
            api = API(self.p, [run(self.p)]); api.overrides['/actions/runs/1001'] = self.p.ProvenanceFailure(message)
            with self.assertRaisesRegex(ValueError, message): self.discover(api)
    def test_alias_changed_since_listing_rejected(self):
        api = API(self.p, [run(self.p)]); api.overrides['/actions/runs/1001'] = run(self.p, attempt=2)
        with self.assertRaisesRegex(ValueError, 'provenance_run_changed'): self.discover(api)
    def test_forged_record_and_foreign_original_rejected(self):
        api = API(self.p, [run(self.p)]); api.records[1001]['record_sha256'] = 'f'*64
        with self.assertRaisesRegex(ValueError, 'provenance_record'): self.discover(api)
        api = API(self.p, [run(self.p)]); api.overrides['/actions/runs/1001/attempts/1'] = run(self.p, head_branch='foreign')
        with self.assertRaisesRegex(ValueError, 'provenance_run'): self.discover(api)
    def test_wrong_repository_or_workflow_rejected(self):
        for path, value in [('', {'id': 101, 'full_name': self.p.REPOSITORY, 'private': False}),
                            ('', {'id': 100, 'full_name': self.p.REPOSITORY, 'private': True}),
                            ('/actions/workflows/21', {'id': 21, 'path': 'foreign'})]:
            api = API(self.p, [run(self.p)]); api.overrides[path] = value
            with self.assertRaises(ValueError): self.discover(api)
    def test_failed_rerun_does_not_replace_original_attempt(self):
        first = run(self.p); latest = run(self.p, attempt=2, minute=9, conclusion='failure')
        api = API(self.p, [latest]); api.attempts[(1001, 1)] = first; api.records[1001] = record(self.p, first)
        result = self.discover(api); self.assertEqual((result['run_attempt'], result['candidate_runs_checked']), (1, 2))
    def test_original_shared_transport_stops_at_40(self):
        class Response:
            status = 200; headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, maximum): return b'{}'
        opener = SimpleNamespace(open=lambda request, timeout: Response())
        client = self.p.PublicGithub('synthetic-token', opener=opener)
        for _ in range(40): client.json('')
        with self.assertRaisesRegex(ValueError, 'provenance_budget'): client.json('')
        self.assertEqual(client.requests, 40)
    def test_locator_helper_only_selects_compatible_registered_coordinate(self):
        p = self.p; module = ModuleType('synthetic_registry'); module.registry = lambda: [
            {'compatible': True, 'get_record': {'receipt_sequence': 1}, 'run_binding': run(p)},
            {'compatible': True, 'get_record': {'receipt_sequence': 2}, 'run_binding': run(p, 1002)},
            {'compatible': False, 'get_record': {'receipt_sequence': 99}, 'run_binding': run(p, 1003)}]
        source = ROOT / 'scripts/get_publication_registered_predecessor.py'
        node = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'discovery_locator')
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), module.__dict__)
        idle = SimpleNamespace(_forward_policy=lambda policy, row: row['compatible'])
        with patch.dict('sys.modules', {'get_publication_provenance': p, 'get_notion_idle_recovery': idle}):
            self.assertEqual(module.discovery_locator(policy(p)), (1002, 1))
            module.registry = lambda: []
            self.assertIsNone(module.discovery_locator(policy(p)))


if __name__ == '__main__':
    unittest.main()
