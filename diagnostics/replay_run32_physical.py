"""Offline #32 exact proof through the unchanged physical HTTP gates.

The successor identity is synthetic. Historical run rows and #32 attempt/job/
marker evidence are actual public captures. The repository-wide listing was
filtered by exact workflow ID and partitioned into the workflow endpoint's
30-row pages; this is not a captured workflow-list endpoint response.
"""
from copy import deepcopy
from pathlib import Path
import base64
import hashlib
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'diagnostics'),str(ROOT/'scripts'),str(ROOT/'tests')]
import replay_run28_physical as shared
from test_notion_run32_recovery import Public, fixture
import get_notion_run32_recovery as r
p=shared.p
j=shared.j
n=shared.n
SYNTHETIC_RUN=shared.SYNTHETIC_RUN
SYNTHETIC_CONTROLLER=shared.SYNTHETIC_CONTROLLER
SYNTHETIC_RUNTIME=shared.SYNTHETIC_RUNTIME
HISTORY=ROOT/'tests/fixtures/notion-run32-discovery-history.json'
HISTORY_SHA='c8ef0ee13d33a6c4f194009b5f8e0cb1110445260c25ae669ea1e826b83fd566'


def history():
    raw=HISTORY.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=HISTORY_SHA:raise AssertionError('public history fixture changed')
    return json.loads(raw)['workflow_runs']


def policy():
    value=deepcopy(r.PUBLIC_POLICY)
    value.update(code_sha=SYNTHETIC_RUNTIME,rules_sha256=p.entry.rules_sha256(),
                 get_policy_sha256=shared.recovery.current_get_policy())
    return value


class Responses(Public):
    def __init__(self,approved,trigger='manual',public_job_log=None):
        super().__init__();self.policy=approved;self.rows=history();self.full_logs=None
        if public_job_log is not None:
            self.full_logs=Path(public_job_log).read_bytes()
            if hashlib.sha256(self.full_logs).hexdigest()!=fixture()['public_source_sha256']['full_job_logs']:
                raise AssertionError('recorded public job log digest changed')
        self.current=deepcopy(self.run)
        self.current.update(id=SYNTHETIC_RUN,run_number=33,run_attempt=1,
            head_sha=SYNTHETIC_CONTROLLER,status='in_progress',conclusion=None,
            event='schedule' if trigger=='schedule' else 'workflow_dispatch',
            created_at='2026-10-19T19:00:00Z',updated_at='2026-10-19T19:00:00Z')
    def json(self,path):
        root='/actions/runs/'+str(SYNTHETIC_RUN)
        if path in (root,root+'/attempts/1'):
            self.calls.append(path);return deepcopy(self.current)
        if path.startswith('/actions/workflows/') and '/runs?' in path:
            self.calls.append(path);page=int(path.rsplit('page=',1)[1]);rows=[self.current]+self.rows
            return {'total_count':len(rows),'workflow_runs':deepcopy(rows[(page-1)*30:page*30])}
        if path=='/contents/'+self.policy['workflow_path']+'?ref='+SYNTHETIC_CONTROLLER:
            self.calls.append(path);raw=p.controller.expected_bytes(self.policy)
            return {'type':'file','path':self.policy['workflow_path'],'encoding':'base64',
                    'content':base64.b64encode(raw).decode(),'sha':p.entry.r.oid(raw),'size':len(raw)}
        return Public.json(self,path)
    def logs(self,job):
        selected=super().logs(job)
        return self.full_logs if self.full_logs is not None else selected


class Opener(shared.Opener):
    def __init__(self,approved,trigger='manual',public_job_log=None):
        self.api=Responses(approved,trigger,public_job_log);self.calls=[];self.cdn_auth=[]


Response=shared.Response


def replay(trigger='manual',public_job_log=None):
    approved=policy();opener=Opener(approved,trigger,public_job_log)
    client=p.PublicGithub('synthetic-noncredential',opener=opener,clock=lambda:0,sleep=lambda _:None)
    execution={'run_id':SYNTHETIC_RUN,'run_attempt':1,'controller_sha':SYNTHETIC_CONTROLLER,
               'code_sha':approved['code_sha']}
    p.verify_execution(client,approved,execution,trigger)
    result=j.discover_anchor(client,public_policy=approved,
        approved_public_policy_sha256=p.a.sha(p.a.encoded(approved)),
        notion_policy_sha256=n.e.digest(n.scope_for(approved)),
        exclude_run_id=SYNTHETIC_RUN,exclude_run_attempt=1)
    if result['anchor']!=r.ANCHOR or result.get('recovery_anchor')!=r.TAG:
        raise AssertionError('unexpected checkpoint selected')
    if client.requests!=len(opener.calls) or client.requests>p.MAX_REQUESTS:
        raise AssertionError('physical request accounting mismatch')
    routes=[url[len(p.API):] if url.startswith(p.API) else 'fixture_cdn_log_download' for url in opener.calls]
    return {'test_mode':'recorded_public_metadata_and_synthetic_successor',
        'listing_provenance':'actual repository listing filtered exact workflow; reconstructed 30-row pagination',
        'log_evidence':'complete_recorded_public_job_log' if opener.api.full_logs is not None else 'selected_hash_only_markers',
        'verified':True,'trigger':trigger,'physical_requests':client.requests,
        'max_physical_requests':p.MAX_REQUESTS,'logical_discovery_reads':result['provenance_reads'],
        'response_bytes':client.bytes,'candidate_attempts_checked':result['candidate_attempts_checked'],
        'listing_run_numbers':[row['run_number'] for row in opener.api.rows],
        'selected_run_number':32,'selected_run_id':result['run_id'],
        'execution_verification_included':True,'current_attempt_excluded':True,
        'log_redirect_included':bool(opener.cdn_auth),'cdn_authorization_forwarded':any(opener.cdn_auth),
        'private_readback_required':result['private_readback_required'],
        'private_state_replayed':False,'network_calls':0,'anchor':result['anchor'],'api_routes':routes}


if __name__=='__main__':
    print(json.dumps(replay(sys.argv[1] if len(sys.argv)>1 else 'manual',
                           sys.argv[2] if len(sys.argv)>2 else None),sort_keys=True,indent=2))
