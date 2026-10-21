"""Only three pinned PNGs; injected I/O, no network or credentials in this module."""
import get_private_runtime_bindings as _private_bindings
import hashlib,json,re,struct
from copy import deepcopy
from datetime import datetime,timezone

PINS = _private_bindings.value('get_notion_png_upload', 'PINS')
# Separate exact reviewed cohort. Legacy PINS and its plan tree stay unchanged.
SEQ2_PINS = _private_bindings.value('get_notion_png_upload', 'SEQ2_PINS')
LEGACY_MANIFEST = '.sync/get-notion/assets/reviewed-png-manifest-v1.json'
SEQ2_MANIFEST = '.sync/get-notion/assets/reviewed-png-manifest-v2.json'

def pins_for_manifest(path):
 if path == LEGACY_MANIFEST:return PINS
 if path == SEQ2_MANIFEST:return SEQ2_PINS
 fail('unreviewed_asset')

def reviewed_pin(note_sha):
 if note_sha in PINS and note_sha in SEQ2_PINS:fail('invalid_binding')
 return PINS.get(note_sha,SEQ2_PINS.get(note_sha))

def byte_limit(note_sha):
 # The sole above-legacy-cap allowance belongs to an exact reviewed new pin.
 pin=SEQ2_PINS.get(note_sha)
 return max(512000,pin['bytes']) if pin is not None else 512000

def valid_semantic_size(content_sha,byte_count):
 if type(byte_count) is not int or byte_count<=0:return False
 return byte_count<=512000 or any(p['bytes']==byte_count and p['sha256']==content_sha for p in SEQ2_PINS.values())

def maximum_read_bytes():
 return max([512000]+[p['bytes'] for p in SEQ2_PINS.values()])

def reviewed_oversize(data):
 return any(p['bytes']==len(data) and p['sha256']==sha(data)
            for p in SEQ2_PINS.values() if p['bytes']>512000)

VERSION='2025-09-03'
DESTINATION_PIN = _private_bindings.value('get_notion_png_upload', 'DESTINATION_PIN')
class Blocked(ValueError):pass
class NotDispatched(Exception):
 def __init__(self,request_sha256,reason="request_not_dispatched"):
  self.request_sha256=request_sha256
  self.reason=reason
  super().__init__('request_not_dispatched')
class WriteHTTPFailure(Exception):
 """Only the physical adapter may report an actual POST HTTP error."""
 def __init__(self,status,request_sha256):
  self.status=status;self.request_sha256=request_sha256
  super().__init__('write_http_observed')
def fail(reason):raise Blocked(reason) from None
def encoded(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def sha(v):return hashlib.sha256(v).hexdigest()
def digest(v):return sha(encoded(v))
def uuid(v):
 if not isinstance(v,str) or not re.fullmatch('[0-9a-f]{32}|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}',v):fail('invalid_identity')
 return v.replace('-','')
def valid_hash(v,n=64):
 if not isinstance(v,str) or not re.fullmatch('[0-9a-f]{'+str(n)+'}',v):fail('invalid_binding')
def binding_schema(binding,data):
 try:
  keys={'note_id','md_sha256','asset_path','git_blob','sha256','bytes','mime','data_source_id','source_commit','reference_sha256'}
  if not isinstance(binding,dict) or set(binding)!=keys:fail('invalid_binding')
  b=deepcopy(binding)
  if not isinstance(b['note_id'],str) or not re.fullmatch('[0-9]+',b['note_id']):fail('invalid_binding')
  for k in ('md_sha256','sha256','reference_sha256'):valid_hash(b[k])
  for k in ('git_blob','source_commit'):valid_hash(b[k],40)
  pin=reviewed_pin(sha(b['note_id'].encode()))
  if pin is None or any(b[k]!=pin[k] for k in pin):fail('unreviewed_asset')
  if sha(uuid(b['data_source_id']).encode())!=DESTINATION_PIN:fail('wrong_destination')
  if type(b['bytes']) is not int or b['mime']!='image/png' or not isinstance(data,bytes) or len(data)!=b['bytes'] or sha(data)!=b['sha256']:fail('asset_changed')
  path=b['asset_path']
  if not isinstance(path,str) or not path.startswith('.sync/get-notion/assets/') or '\\' in path or any(x in ('','..','.') for x in path.split('/')) or not path.endswith('.png'):fail('invalid_binding')
  if hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()!=b['git_blob']:fail('asset_not_committed')
  if data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR':fail('asset_type')
  b['data_source_id']=uuid(b['data_source_id'])
  return b
 except Blocked:raise
 except Exception:fail('invalid_binding')
def prepare(binding,expected_binding_sha,content_tree,data):
 try:
  if digest(binding)!=expected_binding_sha:fail('approval_missing')
  b=binding_schema(binding,data)
  if not isinstance(content_tree,dict) or content_tree.get(b['asset_path'])!=b['git_blob']:fail('asset_not_committed')
  plan={'contract':'three-png-upload-1','binding':b,'filename':'get-'+b['sha256']+'.png','version':VERSION,'asset_tree_sha256':digest(content_tree)}
  plan['multipart_body_sha256']=sha(multipart(plan,data)[0])
  return plan
 except Blocked:raise
 except Exception:fail('invalid_binding')
def expiry_shape(value):
 try:
  if not isinstance(value,str):fail('journal_invalid')
  dt=datetime.fromisoformat(value.replace('Z','+00:00'))
  if dt.tzinfo is None or dt.utcoffset() is None:fail('journal_invalid')
 except Blocked:raise
 except Exception:fail('journal_invalid')
def create_request(plan):
 return spec('POST','/file_uploads',encoded({'mode':'single_part','filename':plan['filename'],'content_type':'image/png'}),'application/json')
def send_fingerprint(plan,ident):
 req=spec('POST','/file_uploads/'+ident+'/send',content_type='multipart/form-data; boundary=getpng'+plan['binding']['sha256'])
 return digest({k:v for k,v in req.items() if k!='body'}|{'body_sha256':plan['multipart_body_sha256']})
def initial_state(plan):return {'contract':'three-png-journal-1','plan_sha256':digest(plan),'phase':'prepared','upload_id':None,'expiry_time':None,'request_sha256':None}
def check_state(s,plan):
 if not isinstance(s,dict) or set(s) not in (set(initial_state(plan)),set(initial_state(plan))|{'write_http'}) or s['contract']!='three-png-journal-1' or s['plan_sha256']!=digest(plan):fail('journal_invalid')
 phase=s['phase']
 if phase not in ('prepared','create_in_flight','upload_id_saved','send_in_flight','uploaded_verified'):fail('journal_invalid')
 if phase in ('prepared','create_in_flight'):
  if s['upload_id'] is not None or s['expiry_time'] is not None:fail('journal_invalid')
 else:uuid(s['upload_id'])
 if phase=='prepared':
  if s['request_sha256'] is not None:fail('journal_invalid')
 else:
  expected=fingerprint(create_request(plan)) if phase in ('create_in_flight','upload_id_saved') else send_fingerprint(plan,s['upload_id'])
  if s['request_sha256']!=expected:fail('journal_invalid')
 if phase in ('upload_id_saved','send_in_flight'):expiry_shape(s['expiry_time'])
 if phase=='uploaded_verified' and s['expiry_time'] is not None:expiry_shape(s['expiry_time'])
 if 'write_http' in s:
  h=s['write_http']
  if not isinstance(h,dict) or set(h)!={'status','request_sha256'} or type(h['status']) is not int or not 400<=h['status']<=599 or h['request_sha256']!=s['request_sha256'] or phase not in ('create_in_flight','send_in_flight','uploaded_verified'):fail('journal_invalid')
 return deepcopy(s)

def spec(method,path,body=b'',content_type=None):
 h={'Notion-Version':VERSION}
 if content_type:h['Content-Type']=content_type
 return {'method':method,'url':'https://api.notion.com/v1'+path,'headers':h,'body':body}
def fingerprint(s):return digest({k:v for k,v in s.items() if k!='body'}|{'body_sha256':sha(s['body'])})
def multipart(plan,data):
 boundary='getpng'+plan['binding']['sha256'];name=plan['filename']
 body=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
 return body,'multipart/form-data; boundary='+boundary
class Upload:
 """store is per-asset durable CAS. request(spec) returns (HTTP status, JSON dict).
 Transport must enforce fixed host, no redirects, bounded response/time and no
 automatic write retries; it alone injects the existing runtime credential.
 """
 def __init__(self,plan,approved_plan_sha,data,store,request,*,allow_writes=False,now=None):
  try:
   if digest(plan)!=approved_plan_sha:fail('approval_missing')
   if type(allow_writes) is not bool:fail('approval_missing')
   if not isinstance(plan,dict) or set(plan)!={'contract','binding','filename','version','asset_tree_sha256','multipart_body_sha256'}:fail('invalid_binding')
   binding_schema(plan['binding'],data)
   valid_hash(plan['asset_tree_sha256']);valid_hash(plan['multipart_body_sha256'])
   if plan['version']!=VERSION or plan['contract']!='three-png-upload-1' or plan['filename']!='get-'+plan['binding']['sha256']+'.png' or plan['multipart_body_sha256']!=sha(multipart(plan,data)[0]):fail('invalid_binding')
  except Blocked:raise
  except Exception:fail('invalid_binding')
  self.plan=deepcopy(plan);self.data=data;self.store=store;self.request=request;self.allow=allow_writes
  self.now=now or (lambda:datetime.now(timezone.utc));self.reads=0;self.stopped=False;self.nd_stopped=False
 def load(self):
  s,v=self.store.read()
  check_state(s,self.plan)
  if digest(s)!=v or not isinstance(s,dict) or s['contract']!='three-png-journal-1' or s['plan_sha256']!=digest(self.plan):fail('journal_invalid')
  if s['phase'] not in ('prepared','create_in_flight','upload_id_saved','send_in_flight','uploaded_verified'):fail('journal_invalid')
  if s['phase'] in ('prepared','create_in_flight'):
   if s['upload_id'] is not None:fail('journal_invalid')
  else:uuid(s['upload_id'])
  return deepcopy(s),v
 def save(self,s,v):
  if not self.allow:fail('writes_disabled')
  check_state(s,self.plan)
  expected=digest(s)
  try:
   actual=self.store.compare_and_set(v,deepcopy(s));check,version=self.store.read()
   if actual!=expected or version!=expected or digest(check)!=expected:fail('durability_unconfirmed')
  except Blocked:raise
  except Exception:fail('durability_unconfirmed')
  return expected
 def call(self,req):
  if self.stopped:fail('permission_denied')
  try:status,value=self.request(deepcopy(req))
  except NotDispatched:raise
  except WriteHTTPFailure as exc:
   key=fingerprint(req)
   if req['method']!='POST' or type(exc.status) is not int or not 400<=exc.status<=599 or exc.request_sha256!=key:fail('request_uncertain')
   denied=exc.status in (401,403)
   if denied:self.stopped=True
   try:
    s,v=self.load()
    if s['phase'] not in ('create_in_flight','send_in_flight') or s['request_sha256']!=key:fail('journal_invalid')
    s['write_http']={'status':exc.status,'request_sha256':key};self.save(s,v)
   finally:
    if denied:fail('permission_denied')
   fail('request_uncertain')
  except Exception as exc:
   if getattr(exc,'status',None) in (401,403) or str(exc)=='permission_denied':self.stopped=True;fail('permission_denied')
   fail('request_uncertain')
  if status in (401,403):self.stopped=True;fail('permission_denied')
  if type(status) is not int or status!=200:fail('request_uncertain')
  if not isinstance(value,dict):fail('response_invalid')
  return value
 def dispatch(self,req,before,inflight_sha):
  try:return self.call(req)
  except NotDispatched as exc:
   if type(exc) is not NotDispatched or exc.request_sha256!=fingerprint(req) or 'write_http' in before:fail('request_uncertain')
   self.nd_stopped=True
   if exc.reason=='permission_denied':self.stopped=True
   reset=getattr(self.store,'reset_not_dispatched',None)
   if not callable(reset):fail('permission_denied' if self.stopped else 'durability_unconfirmed')
   try:
    actual=reset(inflight_sha,deepcopy(before),exc)
    restored,version=self.store.read();check_state(restored,self.plan)
    if actual!=digest(before) or version!=digest(before) or encoded(restored)!=encoded(before):fail('durability_unconfirmed')
   except Blocked:
    if self.stopped:fail('permission_denied')
    raise
   except Exception:fail('permission_denied' if self.stopped else 'durability_unconfirmed')
   self.nd_stopped=True
   if exc.reason=='permission_denied':self.stopped=True;fail('permission_denied')
   fail('request_not_dispatched')
 def response(self,value,upload_id=None):
  if value.get('object')!='file_upload':fail('response_invalid')
  ident=uuid(value.get('id'))
  if upload_id and ident!=uuid(upload_id):fail('upload_identity_changed')
  if value.get('status') not in ('pending','uploaded','expired','failed'):fail('response_invalid')
  for key in ('archived','in_trash'):
   if key in value and type(value[key]) is not bool:fail('response_invalid')
  if value.get('status') in ('expired','failed') or value.get('archived') is True or value.get('in_trash') is True:fail('upload_unusable')
  if value.get('filename')!=self.plan['filename'] or value.get('content_type')!='image/png':fail('upload_metadata_changed')
  if value['status']=='uploaded' and (type(value.get('content_length')) is not int or value['content_length']!=len(self.data)):fail('upload_metadata_changed')
  if 'expiry_time' not in value:fail('response_invalid')
  expiry=value.get('expiry_time')
  if expiry is not None:
   try:
    dt=datetime.fromisoformat(expiry.replace('Z','+00:00'))
    if dt.tzinfo is None or (dt-self.now()).total_seconds()<=30:fail('upload_expired_or_near_expiry')
   except Blocked:raise
   except Exception:fail('response_invalid')
  return ident,expiry
 def advance(self):
  if self.stopped:fail('permission_denied')
  if self.nd_stopped:fail('request_not_dispatched')
  s,v=self.load();phase=s['phase']
  if s.get('write_http',{}).get('status') in (401,403):self.stopped=True;fail('permission_denied')
  if phase=='create_in_flight':fail('create_uncertain_no_retry')
  if phase=='prepared':
   if not self.allow:fail('writes_disabled')
   req=create_request(self.plan)
   before=deepcopy(s)
   s.update(phase='create_in_flight',request_sha256=fingerprint(req));v=self.save(s,v)
   value=self.dispatch(req,before,v);ident,expiry=self.response(value)
   if value['status']!='pending' or expiry is None:fail('response_invalid')
   s.update(phase='upload_id_saved',upload_id=ident,expiry_time=expiry);v=self.save(s,v)
   return {'phase':s['phase'],'upload_id':ident}
  if phase=='upload_id_saved':
   if not self.allow:fail('writes_disabled')
   # Revalidate saved deadline before sending any bytes.
   self.response({'object':'file_upload','id':s['upload_id'],'status':'pending','filename':self.plan['filename'],'content_type':'image/png','expiry_time':s['expiry_time']})
   body,kind=multipart(self.plan,self.data);req=spec('POST','/file_uploads/'+s['upload_id']+'/send',body,kind)
   before=deepcopy(s)
   s.update(phase='send_in_flight',request_sha256=fingerprint(req));v=self.save(s,v)
   value=self.dispatch(req,before,v);self.response(value,s['upload_id'])
   # Even a successful send is independently retrieved on next advance.
   return {'phase':'send_in_flight','status_observed':value['status']}
  if self.reads>=3:fail('read_budget')
  self.reads+=1;value=self.call(spec('GET','/file_uploads/'+s['upload_id']));ident,expiry=self.response(value,s['upload_id'])
  if value['status']!='uploaded':fail('send_uncertain_read_only')
  if phase!='uploaded_verified' and self.allow:s.update(phase='uploaded_verified',expiry_time=expiry);self.save(s,v)
  return {'phase':'uploaded_verified','receipt':{'upload_id':ident,'plan_sha256':digest(self.plan),'source_binding':deepcopy(self.plan['binding']),'content_length':len(self.data),'content_type':'image/png','native_bytes_verified':False,'binding_sha256':digest(self.plan['binding']),'kind':'notion_uploaded_png','sha256':self.plan['binding']['sha256'],'byte_count':len(self.data),'mime':'image/png'},'semantic_payload':{'kind':'notion_uploaded_png','upload_id':ident,'sha256':self.plan['binding']['sha256'],'byte_count':len(self.data),'mime':'image/png','caption':[]},'image_payload':{'type':'image','image':{'type':'file_upload','file_upload':{'id':ident},'caption':[]}}}
