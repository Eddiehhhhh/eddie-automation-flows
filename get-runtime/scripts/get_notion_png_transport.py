"""Multipart adapter over the existing runtime transport; no credential loader."""
import urllib.request
import urllib.error
from copy import deepcopy
from datetime import datetime,timezone
import get_notion_png_upload as upload
import get_notion_live_transport as live

class Transport:
 def __init__(self,base,plan,approved_plan_sha,data,store,*,allow_writes=False,now=None,before_write=None):
  # Reuse the kernel's independent exact schema/bytes approval validation.
  upload.Upload(plan,approved_plan_sha,data,store,lambda _:None,allow_writes=allow_writes)
  self.base=base;self.plan=deepcopy(plan);self.data=data;self.store=store;self.allow=allow_writes;self.stopped=False;self.now=now or (lambda:datetime.now(timezone.utc));self.before_write=before_write
 def __call__(self,req):
  if self.stopped:upload.fail('permission_denied')
  s,version=self.store.read();upload.check_state(s,self.plan)
  if version!=upload.digest(s):upload.fail('durability_unconfirmed')
  phase=s['phase'];method=req.get('method') if isinstance(req,dict) else None
  if method=='GET':
   if phase not in ('send_in_flight','uploaded_verified'):upload.fail('invalid_route')
   expected=upload.spec('GET','/file_uploads/'+s['upload_id'])
  elif method=='POST' and self.allow:
   if phase=='create_in_flight':expected=upload.create_request(self.plan)
   elif phase=='send_in_flight':
    body,kind=upload.multipart(self.plan,self.data)
    expected=upload.spec('POST','/file_uploads/'+s['upload_id']+'/send',body,kind)
   else:upload.fail('write_not_authorized')
  else:upload.fail('writes_disabled')
  if req!=expected:upload.fail('request_mismatch')
  key=upload.fingerprint(expected);b=self.base
  if method=='POST' and (s['request_sha256']!=key or key in b.attempted):upload.fail('write_not_authorized_or_already_attempted')
  def not_dispatched(reason):
   if method=='POST':raise upload.NotDispatched(key)
   upload.fail(reason)
  if b.attempts>=2000 or b.bytes>=live.ro.MAX_HTTP_BYTES or b.clock()>=b.deadline:not_dispatched('request_budget')
  if b.last is not None:b.sleep(max(0,.4-(b.clock()-b.last)))
  if b.clock()>=b.deadline:not_dispatched('request_budget')
  # Recheck the durable authorization immediately before dispatch.
  current,current_version=self.store.read()
  if current_version!=version or upload.digest(current)!=version:upload.fail('durability_unconfirmed')
  if method=='POST':
   try:
    if not callable(self.before_write) or self.before_write() is not True:raise ValueError()
   except Exception as exc:
    if str(exc)=='permission_denied' or getattr(exc,'status',None) in (401,403):
     self.stopped=True;raise upload.NotDispatched(key,'permission_denied') from None
    raise upload.NotDispatched(key) from None
  if b.attempts>=2000 or b.bytes>=live.ro.MAX_HTTP_BYTES or b.clock()>=b.deadline:not_dispatched('request_budget')
  if method=='POST' and phase=='send_in_flight':
   upload.expiry_shape(current['expiry_time'])
   expiry=datetime.fromisoformat(current['expiry_time'].replace('Z','+00:00'))
   if (expiry-self.now()).total_seconds()<=30:not_dispatched('upload_expired_or_near_expiry')
  headers=dict(expected['headers']);headers['Authorization']='Bearer '+b._token
  request=urllib.request.Request(expected['url'],data=expected['body'] if method=='POST' else None,method=method,headers=headers)
  b.last=b.clock();b.attempts+=1
  if method=='POST':b.attempted.add(key)
  status=None
  try:
   try:opened=b.opener.open(request,timeout=min(30,max(.001,b.deadline-b.clock())))
   except urllib.error.HTTPError as exc:
    status=exc.code;exc.close()
    if type(status) is int and 400<=status<=599 and method=='POST':
     if status in (401,403):self.stopped=True
     raise upload.WriteHTTPFailure(status,key) from None
    if status in (401,403):self.stopped=True;upload.fail('permission_denied')
    upload.fail('request_uncertain')
   with opened as response:
    status=response.status
    if type(status) is int and 400<=status<=599 and method=='POST':
     if status in (401,403):self.stopped=True
     raise upload.WriteHTTPFailure(status,key)
    if status in (401,403):self.stopped=True;upload.fail('permission_denied')
    if status!=200:upload.fail('request_uncertain')
    raw=response.read(live.ro.MAX_RESPONSE+1)
   b.bytes+=len(raw)
   if len(raw)>live.ro.MAX_RESPONSE or b.bytes>live.ro.MAX_HTTP_BYTES or b.clock()>=b.deadline:upload.fail('request_budget')
   try:value=live.strict_capture_json(raw,live.ro.MAX_RESPONSE)
   except Exception:upload.fail('response_invalid')
   if not isinstance(value,dict) or value.get('object')=='error':upload.fail('response_invalid')
   return status,value
  except urllib.error.HTTPError:upload.fail('request_uncertain')
  except upload.WriteHTTPFailure as exc:
   if method=='POST' and type(status) is int and 400<=status<=599 and exc.status==status and exc.request_sha256==key:raise
   upload.fail('request_uncertain')
  except upload.Blocked:raise
  except Exception:upload.fail('request_uncertain')
