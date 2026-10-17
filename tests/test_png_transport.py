import unittest,json,copy,urllib.error
import get_notion_png_upload as u
import get_notion_png_transport as t
import get_notion_live_transport as live
import test_png_upload as fixture
Store=fixture.Store
class Reply:
 status=200
 def __init__(self,value):self.raw=json.dumps(value).encode()
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def read(self,n):return self.raw[:n]
class Opener:
 def __init__(self,owner):self.owner=owner;self.calls=[];self.error=None
 def open(self,req,timeout):
  self.calls.append(req)
  if self.error:raise self.error
  return Reply(self.owner.response('uploaded' if req.full_url.endswith('/send') or req.method=='GET' else 'pending'))
class TransportTests(unittest.TestCase):
 setUp=fixture.Tests.setUp
 response=fixture.Tests.response
 def adapter(self,allow=True):
  self.opener=Opener(self);self.base=live.Transport('synthetic-test-token',opener=self.opener,sleep=lambda _:None,clock=lambda:1)
  return t.Transport(self.base,self.plan,u.digest(self.plan),self.data,self.store,allow_writes=allow,before_write=lambda:True)
 def engine(self,transport):
  from datetime import datetime,timezone
  return u.Upload(self.plan,u.digest(self.plan),self.data,self.store,transport,allow_writes=True,now=lambda:datetime(2030,1,1,tzinfo=timezone.utc))
 def test_real_request_multipart_and_shared_budget(self):
  a=self.adapter();e=self.engine(a);e.advance();e.advance();e.advance()
  self.assertEqual(self.base.attempts,3);self.assertGreater(self.base.bytes,0)
  r=self.opener.calls[1];self.assertEqual(r.data,u.multipart(self.plan,self.data)[0]);self.assertEqual(r.get_header('Notion-version'),u.VERSION);self.assertEqual(r.get_header('Authorization'),'Bearer synthetic-test-token')
 def test_default_disabled(self):
  a=self.adapter(False);self.store.s.update(phase='create_in_flight',request_sha256=u.fingerprint(u.create_request(self.plan)))
  with self.assertRaisesRegex(u.Blocked,'writes_disabled'):a(u.create_request(self.plan))
  self.assertEqual(self.opener.calls,[])
 def test_exact_durable_request_only(self):
  a=self.adapter();req=u.create_request(self.plan)
  with self.assertRaises(u.Blocked):a(req)
  self.store.s.update(phase='create_in_flight',request_sha256=u.fingerprint(req))
  for key,value in [('url','https://elsewhere.invalid'),('body',b'{}'),('headers',{'Notion-Version':'2026-03-11'})]:
   bad=copy.deepcopy(req);bad[key]=value
   with self.assertRaisesRegex(u.Blocked,'request_mismatch'):a(bad)
  self.assertEqual(self.opener.calls,[])
 def test_unknown_write_no_retry(self):
  a=self.adapter();self.opener.error=TimeoutError();e=self.engine(a)
  with self.assertRaises(u.Blocked):e.advance()
  with self.assertRaisesRegex(u.Blocked,'already_attempted'):a(u.create_request(self.plan))
  self.assertEqual(len(self.opener.calls),1)
 def test_permission_stops(self):
  for code in (401,403):
   self.store=Store(u.initial_state(self.plan));a=self.adapter();self.opener.error=urllib.error.HTTPError('https://api.notion.com',code,'error',{},None)
   with self.assertRaisesRegex(u.Blocked,'permission_denied'):self.engine(a).advance()
   with self.assertRaisesRegex(u.Blocked,'permission_denied'):a(u.create_request(self.plan))
   self.assertEqual(len(self.opener.calls),1)
 def test_budget_before_dispatch(self):
  for key,value in [('attempts',2000),('deadline',0),('bytes',live.ro.MAX_HTTP_BYTES)]:
   self.store=Store(u.initial_state(self.plan));a=self.adapter();setattr(self.base,key,value)
   with self.assertRaises(u.Blocked):self.engine(a).advance()
   self.assertEqual(self.opener.calls,[])
 def test_redirect_is_not_retried(self):
  a=self.adapter();self.opener.error=urllib.error.HTTPError('https://api.notion.com',302,'redirect',{'Location':'https://elsewhere.invalid'},None)
  with self.assertRaises(u.Blocked):self.engine(a).advance()
  self.assertEqual(len(self.opener.calls),1)
 def test_get_only_saved_id(self):
  a=self.adapter();e=self.engine(a);e.advance();e.advance()
  with self.assertRaisesRegex(u.Blocked,'request_mismatch'):a(u.spec('GET','/file_uploads/'+'f'*32))
  self.assertEqual(len(self.opener.calls),2)
 def test_final_journal_read_consumes_deadline_zero_dispatch(self):
  a=self.adapter();req=u.create_request(self.plan)
  self.store.s.update(phase='create_in_flight',request_sha256=u.fingerprint(req))
  clock=[1];self.base.clock=lambda:clock[0];self.base.deadline=10
  read=self.store.read;calls=[0]
  def slow_read():
   calls[0]+=1
   if calls[0]==2:clock[0]=11
   return read()
  self.store.read=slow_read
  with self.assertRaises(u.NotDispatched):a(req)
  self.assertEqual(self.opener.calls,[]);self.assertEqual(self.base.attempts,0)
 def test_final_send_expiry_rechecked_after_journal(self):
  from datetime import datetime,timezone
  a=self.adapter();e=self.engine(a);e.advance()
  body,kind=u.multipart(self.plan,self.data);req=u.spec('POST','/file_uploads/'+'2'*32+'/send',body,kind)
  self.store.s.update(phase='send_in_flight',request_sha256=u.fingerprint(req))
  a.now=lambda:datetime(2030,1,1,0,59,40,tzinfo=timezone.utc)
  before=self.base.attempts
  with self.assertRaises(u.NotDispatched):a(req)
  self.assertEqual(len(self.opener.calls),1);self.assertEqual(self.base.attempts,before)

 def test_actual_post_http_diagnostic_persisted_no_rearm(self):
  a=self.adapter();self.opener.error=urllib.error.HTTPError('https://api.notion.com',429,'limited',{},None);e=self.engine(a)
  with self.assertRaisesRegex(u.Blocked,'request_uncertain'):e.advance()
  self.assertEqual(self.store.s['write_http'],{'status':429,'request_sha256':self.store.s['request_sha256']})
  self.assertEqual(self.store.s['phase'],'create_in_flight')
  with self.assertRaisesRegex(u.Blocked,'create_uncertain_no_retry'):e.advance()
  self.assertEqual(len(self.opener.calls),1)
 def test_get_http_cannot_claim_write_status(self):
  a=self.adapter();e=self.engine(a);e.advance();e.advance();self.opener.error=urllib.error.HTTPError('https://api.notion.com',500,'bad',{},None)
  with self.assertRaises(u.Blocked):e.advance()
  self.assertNotIn('write_http',self.store.s)
 def test_permission_diagnostic_survives_new_kernel(self):
  a=self.adapter();self.opener.error=urllib.error.HTTPError('https://api.notion.com',403,'denied',{},None)
  with self.assertRaisesRegex(u.Blocked,'permission_denied'):self.engine(a).advance()
  self.assertEqual(self.store.s['write_http']['status'],403)
  with self.assertRaisesRegex(u.Blocked,'permission_denied'):self.engine(a).advance()
  self.assertEqual(len(self.opener.calls),1)
 def test_untyped_failure_does_not_invent_http(self):
  a=self.adapter();self.opener.error=TimeoutError()
  with self.assertRaises(u.Blocked):self.engine(a).advance()
  self.assertNotIn('write_http',self.store.s)

 def test_injected_typed_failure_cannot_claim_observed_http(self):
  a=self.adapter();self.opener.error=u.WriteHTTPFailure(500,u.fingerprint(u.create_request(self.plan)))
  with self.assertRaisesRegex(u.Blocked,'request_uncertain'):self.engine(a).advance()
  self.assertNotIn('write_http',self.store.s)
 def test_response_read_typed_failure_is_not_status(self):
  a=self.adapter()
  class BadReply(Reply):
   def read(inner,n):raise u.WriteHTTPFailure(500,u.fingerprint(u.create_request(self.plan)))
  self.opener.open=lambda *args,**kwargs:BadReply(self.response('pending'))
  with self.assertRaisesRegex(u.Blocked,'request_uncertain'):self.engine(a).advance()
  self.assertNotIn('write_http',self.store.s)
 def test_diagnostic_cas_failure_does_not_claim_persisted(self):
  a=self.adapter();self.opener.error=urllib.error.HTTPError('https://api.notion.com',500,'bad',{},None)
  original=self.store.compare_and_set
  def cas(v,state):
   if 'write_http' in state:raise TimeoutError()
   return original(v,state)
  self.store.compare_and_set=cas
  with self.assertRaisesRegex(u.Blocked,'durability_unconfirmed'):self.engine(a).advance()
  self.assertNotIn('write_http',self.store.s)

 def test_read_or_exit_http_error_not_write_response(self):
  for stage in ('read','exit'):
   self.store=Store(u.initial_state(self.plan));a=self.adapter()
   class BadReply(Reply):
    def read(inner,n):
     if stage=='read':raise urllib.error.HTTPError('https://api.notion.com',500,'bad',{},None)
     return super().read(n)
    def __exit__(inner,*args):
     if stage=='exit':raise urllib.error.HTTPError('https://api.notion.com',500,'bad',{},None)
   self.opener.open=lambda *args,**kwargs:BadReply(self.response('pending'))
   with self.assertRaisesRegex(u.Blocked,'request_uncertain'):self.engine(a).advance()
   self.assertNotIn('write_http',self.store.s)

if __name__=='__main__':unittest.main()
