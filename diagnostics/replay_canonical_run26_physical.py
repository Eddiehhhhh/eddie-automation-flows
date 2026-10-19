"""Offline current-policy canonical discovery through recorded public history.

The executing successor/controller is synthetic and explicitly labeled. All
historical metadata, sequence-two marker and older controller bytes are from
public responses. No network or private state replay is performed.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'diagnostics'),str(ROOT/'scripts'),str(ROOT/'tests')]
import replay_run28_physical as shared

p=shared.p
FIXTURE=ROOT/'tests/fixtures/canonical-run26-public-proof.json'
FIXTURE_SHA='bb02564cbe387670bb3dcbd17b167a8305a0fc3d7207328bf7ac99b5f14a62f7'


def fixture():
    raw=FIXTURE.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=FIXTURE_SHA:
        raise AssertionError('canonical public fixture changed')
    return json.loads(raw)


class Responses(shared.Responses):
    def __init__(self,approved,trigger='manual',public_job_log=None):
        super().__init__(approved,trigger)
        self.canonical=fixture()
        if public_job_log is not None:
            self.full_logs=Path(public_job_log).read_bytes()
            if hashlib.sha256(self.full_logs).hexdigest()!=self.canonical['full_job_log_sha256']:
                raise AssertionError('recorded sequence-two public log changed')
    def json(self,path):
        for row in self.canonical['rows'].values():
            root='/actions/runs/'+str(row['run']['id'])
            if path in (root,root+'/attempts/1',root+'/attempts/1/jobs?per_page=100&page=1'):
                self.calls.append(path)
                key='jobs' if '/jobs?' in path else 'attempt' if '/attempts/' in path else 'run'
                return deepcopy(row[key])
        if path.startswith('/contents/'):
            sha=path.split('?ref=',1)[1]
            if sha in self.canonical['controllers']:
                self.calls.append(path);return deepcopy(self.canonical['controllers'][sha])
        return super().json(path)
    def logs(self,job):
        if job!=p.recovery.SEQUENCE_TWO_JOB:raise AssertionError('unexpected canonical job log')
        self.calls.append('logs:'+str(job))
        return self.full_logs if self.full_logs is not None else ('\n'.join(self.canonical['marker_lines'])+'\n').encode()


class Opener(shared.Opener):
    def __init__(self,approved,trigger='manual',public_job_log=None):
        self.api=Responses(approved,trigger,public_job_log);self.calls=[];self.cdn_auth=[]


def replay(trigger='manual',public_job_log=None):
    approved=shared.policy();opener=Opener(approved,trigger,public_job_log)
    client=p.PublicGithub('synthetic-noncredential',opener=opener,clock=lambda:0,sleep=lambda _:None)
    execution={'run_id':shared.SYNTHETIC_RUN,'run_attempt':1,
               'controller_sha':shared.SYNTHETIC_CONTROLLER,'code_sha':approved['code_sha']}
    p.verify_execution(client,approved,execution,trigger)
    result=p.discover(client,approved,exclude_run_id=shared.SYNTHETIC_RUN,exclude_run_attempt=1)
    expected={k:p.recovery.SEQUENCE_TWO_RECORD['receipt_'+k] for k in ('commit','sha256')}
    expected={'commit':expected['commit'],'receipt_sha256':expected['sha256']}
    if result['anchor']!=expected or result['run_id']!=p.recovery.SEQUENCE_TWO_RECORD['run_id']:
        raise AssertionError('wrong canonical publication anchor')
    if result['receipt_sequence']!=2 or result['record_sha256']!=p.recovery.SEQUENCE_TWO_RECORD['record_sha256']:
        raise AssertionError('sequence-two exact proof was not preserved')
    if client.requests!=len(opener.calls) or client.requests>p.MAX_REQUESTS:
        raise AssertionError('physical request accounting mismatch')
    routes=[url[len(p.API):] if url.startswith(p.API) else 'fixture_cdn_log_download' for url in opener.calls]
    return {'test_mode':'recorded_public_history_and_synthetic_successor',
        'log_evidence':'complete_recorded_public_job_log' if opener.api.full_logs is not None else 'selected_hash_only_marker',
        'verified':True,'trigger':trigger,'physical_requests':client.requests,'max_physical_requests':p.MAX_REQUESTS,
        'response_bytes':client.bytes,'candidate_attempts_checked':result['candidate_runs_checked'],
        'publication_records_verified':result['publication_records_verified'],
        'listing_run_numbers':[r['run_number'] for r in opener.api.rows],
        'selected_run_number':26,'selected_run_id':result['run_id'],'selected_receipt_sequence':2,
        'record_sha256':result['record_sha256'],'anchor':result['anchor'],
        'candidate_public_policy_sha256':p.a.sha(p.a.encoded(approved)),
        'candidate_get_policy_sha256':approved['get_policy_sha256'],'candidate_rules_sha256':approved['rules_sha256'],
        'candidate_runtime_sha_is_unpublished_placeholder':True,
        'execution_verification_included':True,'current_attempt_excluded':True,
        'log_redirect_included':bool(opener.cdn_auth),'cdn_authorization_forwarded':any(opener.cdn_auth),
        'private_receipt_readback_required':True,'private_state_replayed':False,'network_calls':0,
        'api_routes':routes}


if __name__=='__main__':
    print(json.dumps(replay(sys.argv[1] if len(sys.argv)>1 else 'manual',
                           sys.argv[2] if len(sys.argv)>2 else None),sort_keys=True,indent=2))
