"""Offline actual public history + synthetic successor through real HTTP gates.

No credentials, network, private Git objects, or mutations are used. The current
run/controller identity is deliberately synthetic; historical listing metadata
and #28 hash-only log records come from recorded public responses.
"""
from copy import deepcopy
from pathlib import Path
import base64
import hashlib
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
import get_publication_provenance as p
import get_notion_journal_runtime as j
import get_notion_runtime as n
import get_notion_run28_recovery as r
import get_recurring_policy_recovery as recovery
from test_notion_run28_recovery import Public, fixture

HISTORY=ROOT/'tests/fixtures/notion-run28-discovery-history.json'
HISTORY_SHA='3a7e1622e1c1590c7c40118506647295c1a1f68195f1afa851c093eb15ef77d8'
SYNTHETIC_RUN=99999999999
SYNTHETIC_CONTROLLER='e'*40
SYNTHETIC_RUNTIME='f'*40
CDN='https://fixture.blob.core.windows.net/'


def history():
    raw=HISTORY.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=HISTORY_SHA:raise AssertionError('public history fixture changed')
    return json.loads(raw)['workflow_runs']


def policy():
    value=deepcopy(r.PUBLIC_POLICY)
    value.update(code_sha=SYNTHETIC_RUNTIME,rules_sha256=p.entry.rules_sha256(),
                 get_policy_sha256=recovery.current_get_policy())
    return value


class Responses(Public):
    def __init__(self,approved,trigger='manual',public_job_log=None):
        super().__init__();self.policy=approved;self.rows=history()
        self.full_logs=None
        if public_job_log is not None:
            self.full_logs=Path(public_job_log).read_bytes()
            expected=fixture()['public_source_sha256']['full_job_logs']
            if hashlib.sha256(self.full_logs).hexdigest()!=expected:
                raise AssertionError('recorded public job log digest changed')
        self.current=deepcopy(self.run)
        self.current.update(id=SYNTHETIC_RUN,run_number=30,run_attempt=1,
            head_sha=SYNTHETIC_CONTROLLER,status='in_progress',conclusion=None,
            event='schedule' if trigger=='schedule' else 'workflow_dispatch',
            created_at='2026-10-19T17:00:00Z',updated_at='2026-10-19T17:00:00Z')
    def json(self,path):
        root='/actions/runs/'+str(SYNTHETIC_RUN)
        if path in (root,root+'/attempts/1'):
            self.calls.append(path);return deepcopy(self.current)
        if path.startswith('/actions/workflows/') and '/runs?' in path:
            self.calls.append(path);page=int(path.rsplit('page=',1)[1])
            rows=[self.current]+self.rows
            return {'total_count':len(rows),'workflow_runs':deepcopy(rows[(page-1)*30:page*30])}
        if path=='/contents/'+self.policy['workflow_path']+'?ref='+SYNTHETIC_CONTROLLER:
            self.calls.append(path);raw=p.controller.expected_bytes(self.policy)
            return {'type':'file','path':self.policy['workflow_path'],'encoding':'base64',
                    'content':base64.b64encode(raw).decode(),'sha':p.entry.r.oid(raw),'size':len(raw)}
        return super().json(path)
    def logs(self,job):
        selected=super().logs(job)
        return self.full_logs if self.full_logs is not None else selected


class Response:
    def __init__(self,raw,status=200,headers=None):
        self.raw=raw;self.status=status;self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,maximum):return self.raw[:maximum]


class Opener:
    def __init__(self,approved,trigger='manual',public_job_log=None):
        self.api=Responses(approved,trigger,public_job_log);self.calls=[];self.cdn_auth=[]
    def open(self,request,timeout):
        url=request.full_url;self.calls.append(url)
        if request.method!='GET':raise AssertionError('replay is read-only')
        if url.startswith(CDN):
            self.cdn_auth.append(request.has_header('Authorization'))
            if self.cdn_auth[-1]:raise AssertionError('credential forwarded to fixture CDN')
            return Response(self.api.logs(int(url.rsplit('/',1)[1])))
        if not (url==p.API or url.startswith(p.API+'/')):
            raise AssertionError('unexpected URL; network is forbidden')
        if not request.has_header('Authorization'):raise AssertionError('expected synthetic runtime auth on API only')
        path=url[len(p.API):]
        if path.startswith('/actions/jobs/') and path.endswith('/logs'):
            return Response(b'',302,{'Location':CDN+path.split('/')[3]})
        return Response(p.a.encoded(self.api.json(path)))


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
    return {'test_mode':'recorded_public_history_and_synthetic_successor',
        'log_evidence':'complete_recorded_public_job_log' if opener.api.full_logs is not None else 'selected_hash_only_markers',
        'verified':True,'trigger':trigger,'physical_requests':client.requests,
        'max_physical_requests':p.MAX_REQUESTS,'logical_discovery_reads':result['provenance_reads'],
        'response_bytes':client.bytes,'candidate_attempts_checked':result['candidate_attempts_checked'],
        'listing_run_numbers':[row['run_number'] for row in opener.api.rows],
        'selected_run_number':28,'selected_run_id':result['run_id'],
        'selection_order_basis':'actual updated_at; #28 metadata is later than #29',
        'execution_verification_included':True,'current_attempt_excluded':True,
        'log_redirect_included':bool(opener.cdn_auth),'cdn_authorization_forwarded':any(opener.cdn_auth),
        'private_readback_required':result['private_readback_required'],
        'private_state_replayed':False,'network_calls':0,'anchor':result['anchor'],
        'api_routes':[url[len(p.API):] if url.startswith(p.API) else 'fixture_cdn_log_download' for url in opener.calls]}


if __name__=='__main__':
    print(json.dumps(replay(sys.argv[1] if len(sys.argv)>1 else 'manual',
                           sys.argv[2] if len(sys.argv)>2 else None),sort_keys=True,indent=2))
