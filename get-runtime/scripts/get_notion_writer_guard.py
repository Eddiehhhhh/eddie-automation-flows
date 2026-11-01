"""Fresh, fixed-target old-writer guard. No writes, retries or token discovery.

Caller injects the existing runtime GITHUB_TOKEN. Diagnostics are separate from
stable engine policy evidence. A failed or rate-limited check never falls back
to unauthenticated requests or cached evidence.
"""
import time
import urllib.error
import urllib.request
from get_mirror_incremental_replay import strict_capture_json

WRITERS=(('getnote-to-notion',259306848),('notion-to-getnote',259286783))
STATUSES=('queued','in_progress','waiting','pending','requested')
HOST='https://api.github.com'
MAX_RESPONSE=1024*1024
MAX_TOTAL=12*MAX_RESPONSE
MAX_SECONDS=120

class Blocked(RuntimeError):pass

def fail(reason):raise Blocked(reason) from None

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

class WriterGuard:
    def __init__(self,token,*,opener=None,clock=time.monotonic):
        if not isinstance(token,str) or not token or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in token):fail('missing_credentials')
        self._token=token
        self._opener=opener if opener is not None else urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        self._clock=clock
        self.diagnostics=None
    def __call__(self):
        start=self._clock();calls=0;total=0
        self.diagnostics={'complete':False,'requests':0,'response_bytes':0}
        rows={}
        try:
            for name,wid in WRITERS:
                base='/repos/Eddiehhhhh/'+name+'/actions/workflows/'+str(wid)
                for status in (None,)+STATUSES:
                    elapsed=self._clock()-start
                    if calls>=12 or elapsed>=MAX_SECONDS:fail('writer_guard_budget')
                    path=base if status is None else base+'/runs?status='+status+'&per_page=1'
                    request=urllib.request.Request(HOST+path,headers={'Authorization':'Bearer '+self._token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'},method='GET')
                    calls+=1;self.diagnostics['requests']=calls
                    try:
                        with self._opener.open(request,timeout=min(15,MAX_SECONDS-elapsed)) as response:
                            code=response.status
                            if code in (401,403):fail('writer_guard_permission')
                            if code==429 or response.headers.get('X-RateLimit-Remaining')=='0':fail('writer_guard_quota')
                            if code!=200:fail('writer_guard_http')
                            payload=response.read(MAX_RESPONSE+1)
                    except urllib.error.HTTPError as exc:
                        if exc.code in (401,403):fail('writer_guard_permission')
                        if exc.code==429:fail('writer_guard_quota')
                        fail('writer_guard_http')
                    except Blocked:raise
                    except Exception:fail('writer_guard_transport')
                    total+=len(payload);self.diagnostics['response_bytes']=total
                    if len(payload)>MAX_RESPONSE or total>MAX_TOTAL or self._clock()-start>=MAX_SECONDS:fail('writer_guard_budget')
                    try:value=strict_capture_json(payload,MAX_RESPONSE)
                    except Exception:fail('writer_guard_response')
                    if status is None:
                        if type(value.get('id')) is not int or value['id']!=wid or value.get('path')!='.github/workflows/sync.yml' or value.get('state') not in ('disabled_manually','disabled_inactivity'):fail('writer_gate_unknown')
                    elif type(value.get('total_count')) is not int or value['total_count']!=0 or value.get('workflow_runs')!=[]:fail('writer_gate_unknown')
                rows[name]={'disabled':True,'active_runs':0}
            self.diagnostics['complete']=True
            return {'fresh_readback_verified':True,'writers':rows}
        finally:
            self.diagnostics['elapsed_seconds']=max(0,self._clock()-start)
