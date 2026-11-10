"""Only the exact reviewed read-only probes may be excluded from recovery."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


CHECK = r'''
import copy, sys, types
from unittest.mock import patch
bindings = types.ModuleType('get_private_runtime_bindings')
bindings.value = lambda module, key: {}
sys.modules['get_private_runtime_bindings'] = bindings
import get_notion_journal_runtime as m
policy = {'repository_id': 1270807355, 'workflow_id': 387242896,
          'workflow_path': '.github/workflows/get-canonical.yml', 'head_branch': 'main'}
repo = {'id': 1270807355, 'full_name': 'Eddiehhhhh/eddie-automation-flows', 'private': False}
for ident, sha, conclusion in ((42275323017, 'eabb1ed873d4a38e492a8295f40ec2d963bb8c58', 'success'),
                               (42274751706, '970b47ebef92c422bc5449063081b0addf8cb7d4', 'failure')):
    run = {'id': ident, 'run_attempt': 1, 'repository': repo,
           'head_repository': copy.deepcopy(repo), 'workflow_id': 387242896,
           'path': policy['workflow_path'], 'head_branch': m.READ_ONLY_PROBE_BRANCH,
           'head_sha': sha, 'event': 'workflow_dispatch', 'status': 'completed',
           'conclusion': conclusion, 'created_at': '2026-11-07T00:00:00Z',
           'updated_at': '2026-11-07T00:01:00Z'}
    assert m._known_read_only_probe(run, policy)
    for key, value in (('workflow_id', 1), ('path', 'wrong'), ('head_branch', 'main'),
                       ('head_sha', '0'*40), ('event', 'push'), ('status', 'queued'),
                       ('conclusion', 'cancelled'), ('run_attempt', 2)):
        changed = copy.deepcopy(run); changed[key] = value
        try: m._known_read_only_probe(changed, policy)
        except m.CheckpointFailure: pass
        else: raise AssertionError(f'{key} must fail closed')
    for key in ('repository', 'head_repository'):
        changed = copy.deepcopy(run); changed[key] = {**repo, 'id': 1}
        try: m._known_read_only_probe(changed, policy)
        except m.CheckpointFailure: pass
        else: raise AssertionError(f'{key} must fail closed')
other = copy.deepcopy(run); other['id'] += 1
assert not m._known_read_only_probe(other, policy)
try: m._latest_run(other, policy)
except m.CheckpointFailure: pass
else: raise AssertionError('unregistered branch run must fail closed')
import get_notion_idle_rotation as rotation
import get_notion_runtime as runtime
import get_private_runtime_report as report
for error, expected in ((rotation.RotationFailure('rotation_policy_changed'), 'rotation_policy_changed'),
                        (rotation.RotationFailure('private note title'), 'journal_provenance_failed'),
                        (ValueError('private note title'), 'journal_provenance_failed')):
    try: m.fail_unexpected_provenance(error)
    except m.CheckpointFailure as caught: assert str(caught) == expected
    else: raise AssertionError('provenance failures must remain fail closed')
assert report.public({'status':'failed','reason':'rotation_policy_changed'},
                     runtime.DIAGNOSTIC_ERRORS)['reason'] == 'rotation_policy_changed'

import get_publication_provenance as provenance
assert provenance.MAX_REQUESTS == 1350
assert provenance.MAX_DISCOVERY_ATTEMPTS == 32
assert provenance.MAX_JOURNAL_PROBE_ATTEMPTS == 32
assert provenance.MAX_JOURNAL_PREVIOUS_ATTEMPTS == 32
assert provenance.MAX_REQUESTS == 2 * (12 + 2 * provenance.MAX_RUN_PAGES * provenance.RUN_PAGE_SIZE
    + provenance.MAX_DISCOVERY_ATTEMPTS * 7 + provenance.MAX_JOURNAL_PROBE_ATTEMPTS * 7
    + provenance.MAX_JOURNAL_PREVIOUS_ATTEMPTS + 3)
class Client:
    def __init__(self): self.calls=[]
    def json(self, path):
        self.calls.append(path)
        if path == '':
            return {'id':1270807355,'full_name':'Eddiehhhhh/eddie-automation-flows','private':False}
        if path == '/actions/workflows/1':
            return {'id':1,'path':'.github/workflows/get-canonical.yml'}
        if path.endswith('/jobs?per_page=100&page=1'):
            return {'total_count':0,'jobs':[]}
        if '/attempts/' in path:
            run_id=int(path.split('/actions/runs/',1)[1].split('/',1)[0])
            attempt=int(path.rsplit('/attempts/',1)[1])
            row=copy.deepcopy(next(row for row in runs if row['id']==run_id))
            row['run_attempt']=attempt
            return row
        if path.endswith('runs?per_page=30&page=1'):
            rows = runs[:30]
        elif path.endswith('runs?per_page=30&page=2'):
            rows = runs[30:60]
        elif path.endswith('runs?per_page=30&page=3'):
            rows = runs[60:]
        else:
            raise AssertionError(path)
        return {'workflow_runs':rows,'total_count':len(runs)}
    def logs(self, _value): raise AssertionError('no candidate may be authenticated after the cap')
runs = [{'id':i+1,'run_attempt':1,'event':'workflow_dispatch','status':'completed',
         'conclusion':'failure','created_at':'2026-11-10T00:00:00Z',
         'updated_at':f'2026-11-10T00:{i:02d}:00Z'} for i in range(33)]
policy={'repository_id':1270807355,'workflow_id':1,
        'workflow_path':'.github/workflows/get-canonical.yml','head_branch':'main'}
with patch('get_publication_provenance.validate_policy',return_value=policy), \
     patch.object(m,'_latest_run',side_effect=lambda row,_policy:row), \
     patch.object(m,'_known_read_only_probe',return_value=False), \
     patch.object(m,'_run_origin',return_value=None), \
     patch('get_notion_checkpoint_resume.registry',return_value=[]), \
     patch.object(m,'recover_anchor',side_effect=m.CheckpointFailure('journal_provenance_no_checkpoint')), \
     patch.object(rotation,'current_hint',return_value=None), \
     patch('get_notion_checkpoint_resume.discovery_locator',return_value=None), \
     patch('get_notion_authenticated_recovery.clean_active.discovery_locator',return_value=None):
    try:
        m.discover_anchor(Client(),public_policy=policy,approved_public_policy_sha256='0'*64,
                          notion_policy_sha256='0'*64)
    except m.CheckpointFailure as caught: assert str(caught)=='journal_provenance_budget'
    else: raise AssertionError('failed run discovery must stop at its explicit cap')

runs = [{'id':i+1,'run_attempt':2,'event':'workflow_dispatch','status':'completed',
         'conclusion':'failure','created_at':'2026-11-10T00:00:00Z',
         'updated_at':f'2026-11-10T00:{i:02d}:00Z'} for i in range(33)]
client=Client()
with patch('get_publication_provenance.validate_policy',return_value=policy), \
     patch.object(m,'_latest_run',side_effect=lambda row,_policy:row), \
     patch.object(m,'_known_read_only_probe',return_value=False), \
     patch.object(m,'_run_origin',return_value=None), \
     patch('get_notion_checkpoint_resume.registry',return_value=[]), \
     patch.object(m,'_terminal_run',side_effect=lambda row,_policy:row), \
     patch('get_notion_journal_runtime._same_run',return_value=None), \
     patch.object(m,'recover_anchor',side_effect=m.CheckpointFailure('journal_provenance_no_checkpoint')), \
     patch.object(rotation,'current_hint',return_value=None), \
     patch('get_notion_checkpoint_resume.discovery_locator',return_value=None), \
     patch('get_notion_authenticated_recovery.clean_active.discovery_locator',return_value=None):
    try:
        m.discover_anchor(client,public_policy=policy,approved_public_policy_sha256='0'*64,
                          notion_policy_sha256='0'*64)
    except m.CheckpointFailure as caught: assert str(caught)=='journal_provenance_budget'
    else: raise AssertionError('rerun ancestry probes must stop at the explicit cap')
prior_reads=sum('/attempts/1' in call for call in client.calls)
assert 0 < prior_reads <= provenance.MAX_JOURNAL_PREVIOUS_ATTEMPTS

runs = [{'id':i+1,'run_attempt':1,'event':'push','status':'completed',
         'conclusion':'failure','created_at':'2026-11-10T00:00:00Z',
         'updated_at':f'2026-11-10T00:{i:02d}:00Z'} for i in range(90)]
client=Client()
with patch('get_publication_provenance.validate_policy',return_value=policy), \
     patch.object(m,'_latest_run',side_effect=lambda row,_policy:row), \
     patch.object(m,'_known_read_only_probe',return_value=False), \
     patch.object(m,'_run_origin',return_value=None), \
     patch.object(rotation,'current_hint',return_value=None), \
     patch('get_notion_checkpoint_resume.discovery_locator',return_value=None), \
     patch('get_notion_authenticated_recovery.clean_active.discovery_locator',return_value=None):
    try:
        m.discover_anchor(client,public_policy=policy,approved_public_policy_sha256='0'*64,
                          notion_policy_sha256='0'*64)
    except m.CheckpointFailure as caught: assert str(caught)=='journal_provenance_missing'
    else: raise AssertionError('zero-job push runs cannot establish an anchor')
assert sum(call.endswith('/jobs?per_page=100&page=1') for call in client.calls)==90

row={'id':100,'run_attempt':33,'repository':repo,'head_repository':copy.deepcopy(repo),
     'workflow_id':1,'path':policy['workflow_path'],'head_branch':'main','head_sha':'a'*40,
     'event':'workflow_dispatch','status':'completed','conclusion':'failure',
     'created_at':'2026-11-10T00:00:00Z','updated_at':'2026-11-10T00:01:00Z'}
runs=[row]
client=Client()
with patch('get_publication_provenance.validate_policy',return_value=policy), \
     patch.object(rotation,'source_search',return_value=None), \
     patch('get_publication_registered_predecessor.discovery_locator',return_value=None), \
     patch('get_publication_provenance.verified_candidate',return_value=None) as proof:
    try: provenance.discover(client,policy,reserve_next_run=True)
    except provenance.ProvenanceFailure as caught: assert str(caught)=='provenance_budget_requests'
    else: raise AssertionError('shared admission discovery must stop at its explicit cap')
assert proof.call_count==provenance.MAX_DISCOVERY_ATTEMPTS
'''


class ReadOnlyProbeRuns(unittest.TestCase):
    def test_exact_reviewed_runs_are_excluded_and_all_others_fail_closed(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        env = dict(os.environ, PYTHONPATH=str(scripts))
        result = subprocess.run([sys.executable, '-c', CHECK], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
