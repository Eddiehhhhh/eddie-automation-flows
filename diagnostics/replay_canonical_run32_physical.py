"""Current ordered canonical discovery, exact #32 no-op proof, real HTTP gates.

The shared #32 replay states the source limitations of reconstructed listing
pagination and synthetic successor identity. No network/private readback occurs.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'diagnostics'),str(ROOT/'scripts'),str(ROOT/'tests')]
import replay_run32_physical as fresh
import replay_canonical_run26_physical as old
p=fresh.p
FIXTURE=ROOT/'tests/fixtures/notion-run32-recent-attempts.json'
FIXTURE_SHA='c456c08ab00b6232635b88cb2988557d37502490ca7b3a063bbd093fbf5f0ff4'


def recent_attempts():
    raw=FIXTURE.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=FIXTURE_SHA:raise AssertionError('public attempt fixture changed')
    return json.loads(raw)['attempts']


class Responses(fresh.Responses):
    def __init__(self,approved,trigger='manual',public_job_log=None):
        super().__init__(approved,trigger,public_job_log)
        self.old=old.Responses(approved,trigger);self.recent=recent_attempts()
    def json(self,path):
        for row in self.recent.values():
            if path=='/actions/runs/'+str(row['id'])+'/attempts/1':return deepcopy(row)
        if path.startswith('/contents/'):
            sha=path.split('?ref=',1)[1]
            if sha in self.old.canonical['controllers']:return deepcopy(self.old.canonical['controllers'][sha])
        for row in self.old.canonical['rows'].values():
            root='/actions/runs/'+str(row['run']['id'])
            if path in (root,root+'/attempts/1',root+'/attempts/1/jobs?per_page=100&page=1'):
                return self.old.json(path)
        if (path.startswith('/actions/runs/'+str(old.shared.r.RUN))
                or path=='/contents/'+self.policy['workflow_path']+'?ref='+old.shared.r.CHECKPOINT['controller_sha']):
            return self.old.json(path)
        return super().json(path)
    def logs(self,job):
        if job==p.recovery.SEQUENCE_TWO_JOB:return self.old.logs(job)
        return super().logs(job)


class Opener(fresh.Opener):
    def __init__(self,approved,trigger='manual',public_job_log=None):
        self.api=Responses(approved,trigger,public_job_log);self.calls=[];self.cdn_auth=[]


def replay(trigger='manual',public_job_log=None):
    approved=fresh.policy();opener=Opener(approved,trigger,public_job_log)
    client=p.PublicGithub('synthetic-noncredential',opener=opener,clock=lambda:0,sleep=lambda _:None)
    execution={'run_id':fresh.SYNTHETIC_RUN,'run_attempt':1,'controller_sha':fresh.SYNTHETIC_CONTROLLER,
               'code_sha':approved['code_sha']}
    p.verify_execution(client,approved,execution,trigger)
    result=p.discover(client,approved,exclude_run_id=fresh.SYNTHETIC_RUN,exclude_run_attempt=1)
    expected={'commit':p.recovery.SEQUENCE_TWO_RECORD['receipt_commit'],
              'receipt_sha256':p.recovery.SEQUENCE_TWO_RECORD['receipt_sha256']}
    if (result['anchor']!=expected or result['run_id']!=fresh.r.RUN or result['job_id']!=fresh.r.JOB
            or result['record_sha256']!=fresh.r.GET_RECORD['record_sha256'] or result['receipt_sequence']!=2):
        raise AssertionError('exact #32 canonical proof changed')
    if client.requests!=len(opener.calls) or client.requests>p.MAX_REQUESTS:
        raise AssertionError('physical request accounting mismatch')
    return {'verified':True,'trigger':trigger,'physical_requests':client.requests,
        'max_physical_requests':p.MAX_REQUESTS,'candidate_attempts_checked':result['candidate_runs_checked'],
        'publication_records_verified':result['publication_records_verified'],
        'listing_run_numbers':[row['run_number'] for row in opener.api.rows],
        'listing_provenance':'actual repository listing filtered exact workflow; reconstructed 30-row pagination',
        'log_evidence':'complete_recorded_public_job_log' if public_job_log is not None else 'selected_hash_only_markers',
        'execution_verification_included':True,'current_attempt_excluded':True,
        'log_redirect_included':bool(opener.cdn_auth),'cdn_authorization_forwarded':any(opener.cdn_auth),
        'network_calls':0,'private_receipt_readback_required':True,'private_state_replayed':False,
        'candidate_runtime_sha_is_unpublished_placeholder':True,'canonical_proof':result,
        'api_routes':[u[len(p.API):] if u.startswith(p.API) else 'fixture_cdn_log_download' for u in opener.calls]}


if __name__=='__main__':
    print(json.dumps(replay(sys.argv[1] if len(sys.argv)>1 else 'manual',
                           sys.argv[2] if len(sys.argv)>2 else None),sort_keys=True,indent=2))
