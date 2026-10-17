import unittest,copy,hashlib,struct
from unittest.mock import patch
from datetime import datetime,timezone
import get_notion_png_upload as m
class Store:
 def __init__(self,s):self.s=copy.deepcopy(s);self.history=[]
 def read(self):return copy.deepcopy(self.s),m.digest(self.s)
 def compare_and_set(self,v,s):
  assert v==m.digest(self.s);self.s=copy.deepcopy(s);self.history.append(s['phase']);return m.digest(s)
class Tests(unittest.TestCase):
 def setUp(self):
  self.data=b'\x89PNG\r\n\x1a\n'+b'0000IHDR'+struct.pack('>II',1,1)+b'test'
  self.b={'note_id':'123','md_sha256':'a'*64,'asset_path':'.sync/get-notion/assets/x.png','git_blob':hashlib.sha1(b'blob '+str(len(self.data)).encode()+b'\0'+self.data).hexdigest(),'sha256':m.sha(self.data),'bytes':len(self.data),'mime':'image/png','data_source_id':'1'*32,'source_commit':'b'*40,'reference_sha256':'c'*64}
  self.patch=patch.multiple(m,PINS={m.sha(b'123'):{k:self.b[k] for k in ('md_sha256','sha256','bytes','git_blob')}},DESTINATION_PIN=m.sha(('1'*32).encode()));self.patch.start();self.addCleanup(self.patch.stop)
  self.tree={self.b['asset_path']:self.b['git_blob']};self.plan=m.prepare(self.b,m.digest(self.b),self.tree,self.data);self.store=Store(m.initial_state(self.plan));self.calls=[];self.failure=None;self.status='pending'
 def response(self,status):return {'object':'file_upload','id':'2'*32,'filename':self.plan['filename'],'content_type':'image/png','content_length':len(self.data),'status':status,'expiry_time':'2030-01-01T01:00:00Z'}
 def request(self,r):
  self.calls.append(r);self.assertEqual(r['headers']['Notion-Version'],'2025-09-03');self.assertTrue(r['url'].startswith('https://api.notion.com/v1/file_uploads'))
  if r['method']=='POST':self.assertIn(self.store.s['phase'],('create_in_flight','send_in_flight'));self.assertEqual(self.store.s['request_sha256'],m.fingerprint(r))
  if self.failure:raise self.failure
  if r['url'].endswith('/send'):self.status='uploaded'
  return 200,self.response(self.status)
 def engine(self,allow=True,request=None):return m.Upload(self.plan,m.digest(self.plan),self.data,self.store,request or self.request,allow_writes=allow,now=lambda:datetime(2030,1,1,tzinfo=timezone.utc))
 def test_happy_exact_multipart_and_readback(self):
  e=self.engine();e.advance();e.advance();out=e.advance();self.assertFalse(out['receipt']['native_bytes_verified']);self.assertEqual(out['image_payload']['image']['file_upload']['id'],'2'*32);self.assertEqual([r['method'] for r in self.calls],['POST','POST','GET']);self.assertIn(self.data,self.calls[1]['body']);self.assertEqual(self.store.history,['create_in_flight','upload_id_saved','send_in_flight','uploaded_verified']);e.advance();self.assertEqual(sum(r['method']=='POST' for r in self.calls),2)
 def test_default_disabled(self):
  e=m.Upload(self.plan,m.digest(self.plan),self.data,self.store,self.request)
  with self.assertRaisesRegex(m.Blocked,'writes_disabled'):e.advance()
  self.assertEqual(self.calls,[])
 def test_wrong_anchor(self):
  with self.assertRaises(m.Blocked):m.prepare(self.b,'0'*64,self.tree,self.data)
 def test_wrong_dest_or_uncommitted_or_changed(self):
  for key,value in [('data_source_id','f'*32),('sha256','0'*64),('note_id','456'),('md_sha256','0'*64),('asset_path','Raw/03 Get/x.png')]:
   b=dict(self.b);b[key]=value
   with self.assertRaises(m.Blocked):m.prepare(b,m.digest(b),self.tree,self.data)
  with self.assertRaises(m.Blocked):m.prepare(self.b,m.digest(self.b),{},self.data)
 def test_durable_failure_has_zero_network(self):
  self.store.compare_and_set=lambda *a:None
  with self.assertRaisesRegex(m.Blocked,'durability_unconfirmed'):self.engine().advance()
  self.assertEqual(self.calls,[])
 def test_create_lost_never_retry_even_new_instance(self):
  self.failure=TimeoutError();e=self.engine()
  with self.assertRaisesRegex(m.Blocked,'request_uncertain'):e.advance()
  self.failure=None
  with self.assertRaisesRegex(m.Blocked,'create_uncertain_no_retry'):self.engine().advance()
  self.assertEqual(len(self.calls),1)
 def test_send_lost_recovers_only_get(self):
  e=self.engine();e.advance();self.failure=TimeoutError()
  with self.assertRaises(m.Blocked):e.advance()
  self.failure=None;self.status='uploaded';out=self.engine(False).advance();self.assertEqual(out['phase'],'uploaded_verified');self.assertEqual([r['method'] for r in self.calls],['POST','POST','GET'])
 def test_pending_send_never_resends_and_bounded_get(self):
  e=self.engine();e.advance();self.failure=TimeoutError()
  with self.assertRaises(m.Blocked):e.advance()
  self.failure=None
  for _ in range(3):
   with self.assertRaisesRegex(m.Blocked,'send_uncertain_read_only'):e.advance()
  with self.assertRaisesRegex(m.Blocked,'read_budget'):e.advance()
  self.assertEqual(sum(r['method']=='POST' for r in self.calls),2)
 def test_permissions_stop_instance(self):
  for code in (401,403):
   self.store=Store(m.initial_state(self.plan));calls=[]
   def req(r):calls.append(r);return code,{}
   e=self.engine(request=req)
   for _ in range(2):
    with self.assertRaisesRegex(m.Blocked,'permission_denied'):e.advance()
   self.assertEqual(len(calls),1)
 def test_permission_exception(self):
  class E(Exception):status=403
  self.failure=E('private details')
  with self.assertRaisesRegex(m.Blocked,'^permission_denied$'):self.engine().advance()
 def test_changed_upload_id_mime_size_expiry_blocks(self):
  for key,val in [('id','3'*32),('content_type','image/jpeg'),('content_length',True),('expiry_time','2020-01-01T00:00:00Z'),('status','expired')]:
   v=self.response('uploaded');v[key]=val
   with self.assertRaises(m.Blocked):self.engine().response(v,'2'*32)
 def test_request_fingerprint_covers_bytes_header_route(self):
  a=m.spec('POST','/file_uploads',b'one','application/json')
  for k,v in [('body',b'two'),('url','https://api.notion.com/v1/pages'),('headers',{'Notion-Version':'2026-03-11'})]:
   b=copy.deepcopy(a);b[k]=v;self.assertNotEqual(m.fingerprint(a),m.fingerprint(b))
 def test_missing_expiry_or_malformed_status(self):
  v=self.response('pending');v.pop('expiry_time')
  with self.assertRaisesRegex(m.Blocked,'response_invalid'):self.engine().response(v)
 def test_send_deadline_stops_before_call(self):
  e=self.engine();e.advance();self.store.s['expiry_time']='2020-01-01T00:00:00Z'
  with self.assertRaisesRegex(m.Blocked,'upload_expired_or_near_expiry'):e.advance()
  self.assertEqual(len(self.calls),1)
 def test_cas_persisted_but_ack_lost_never_calls_network(self):
  original=self.store.compare_and_set
  def lost(v,s):original(v,s);raise TimeoutError()
  self.store.compare_and_set=lost
  with self.assertRaisesRegex(m.Blocked,'durability_unconfirmed'):self.engine().advance()
  self.assertEqual(self.calls,[])
  self.store.compare_and_set=original
  with self.assertRaisesRegex(m.Blocked,'create_uncertain_no_retry'):self.engine().advance()
 def test_redirect_response_is_not_followed(self):
  calls=[]
  def r(req):calls.append(req);return 307,{'location':'https://elsewhere.invalid/'}
  with self.assertRaisesRegex(m.Blocked,'request_uncertain'):self.engine(request=r).advance()
  self.assertEqual(len(calls),1)
 def test_no_secret_in_specs_and_no_page_route(self):
  e=self.engine();e.advance();e.advance();out=e.advance()
  for r in self.calls:self.assertNotIn('Authorization',r['headers']);self.assertNotIn('/pages',r['url'])
  self.assertFalse(out['receipt']['native_bytes_verified']);self.assertEqual(out['semantic_payload']['kind'],'notion_uploaded_png')

class StrictTests(unittest.TestCase):
 setUp=Tests.setUp
 response=Tests.response
 request=Tests.request
 engine=Tests.engine
 def test_archive_flags_typed(self):
  e=self.engine()
  for key in ('archived','in_trash'):
   for value in ('true',1,{},None):
    row=self.response('uploaded');row[key]=value
    with self.assertRaisesRegex(m.Blocked,'response_invalid'):e.response(row)
   row=self.response('uploaded');row[key]=True
   with self.assertRaisesRegex(m.Blocked,'upload_unusable'):e.response(row)
   row[key]=False;e.response(row)
 def test_read_only_observation_zero_cas(self):
  e=self.engine();e.advance();e.advance();before=copy.deepcopy(self.store.s);history=list(self.store.history)
  out=self.engine(False).advance()
  self.assertEqual(out['phase'],'uploaded_verified');self.assertEqual(self.store.s,before);self.assertEqual(self.store.history,history)
 def test_direct_malformed_plan_zero_requests(self):
  for key,value in [('mime','image/jpeg'),('source_commit',None),('asset_path','Raw/03 Get/x.png'),('bytes',True),('reference_sha256',1),('note_id',123),('extra','bad')]:
   plan=copy.deepcopy(self.plan);plan['binding'][key]=value
   with self.assertRaises(m.Blocked):m.Upload(plan,m.digest(plan),self.data,self.store,self.request)
  for key,value in [('extra',1),('multipart_body_sha256','0'*64),('asset_tree_sha256',None)]:
   plan=copy.deepcopy(self.plan);plan[key]=value
   with self.assertRaises(m.Blocked):m.Upload(plan,m.digest(plan),self.data,self.store,self.request)
  self.assertEqual(self.calls,[])
 def test_phase_exact_fingerprint(self):
  e=self.engine();e.advance();saved=copy.deepcopy(self.store.s);e.advance();sent=copy.deepcopy(self.store.s)
  states=[m.initial_state(self.plan)|{'phase':'create_in_flight','request_sha256':m.fingerprint(m.create_request(self.plan))},saved,sent]
  e.advance();states.append(copy.deepcopy(self.store.s))
  for s in states:
   m.check_state(s,self.plan);s['request_sha256']='0'*64
   with self.assertRaisesRegex(m.Blocked,'journal_invalid'):m.check_state(s,self.plan)
  self.store.s=saved;self.store.s['request_sha256']='0'*64;before=len(self.calls)
  with self.assertRaisesRegex(m.Blocked,'journal_invalid'):self.engine().advance()
  self.assertEqual(len(self.calls),before)
 def test_saved_expiry_shapes_and_uploaded_null(self):
  e=self.engine();e.advance();saved=copy.deepcopy(self.store.s);e.advance();sent=copy.deepcopy(self.store.s)
  for state in (saved,sent):
   for expiry in (None,1,True,'bad','2030-01-01T01:00:00'):
    s=copy.deepcopy(state);s['expiry_time']=expiry
    with self.assertRaisesRegex(m.Blocked,'journal_invalid'):m.check_state(s,self.plan)
  self.store.s=sent
  def req(r):v=self.response('uploaded');v['expiry_time']=None;return 200,v
  self.engine(request=req).advance();self.assertIsNone(self.store.s['expiry_time']);m.check_state(self.store.s,self.plan)

 def test_diagnostic_schema_and_exact_typed_key(self):
  e=self.engine();e.advance();e.advance();state=copy.deepcopy(self.store.s)
  for bad in ({'status':True,'request_sha256':state['request_sha256']},{'status':200,'request_sha256':state['request_sha256']},{'status':500,'request_sha256':'f'*64}):
   trial=copy.deepcopy(state);trial['write_http']=bad
   with self.assertRaisesRegex(m.Blocked,'journal_invalid'):m.check_state(trial,self.plan)
  self.store=Store(m.initial_state(self.plan))
  def wrong(req):raise m.WriteHTTPFailure(500,'f'*64)
  with self.assertRaisesRegex(m.Blocked,'request_uncertain'):self.engine(request=wrong).advance()
  self.assertNotIn('write_http',self.store.s)

if __name__=='__main__':unittest.main()
