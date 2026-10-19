"""Local runtime preflight: no upload before complete source/target validation."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import get_notion_png_runtime as x
import get_notion_png_store as storage
import get_notion_outbox as outbox
import get_notion_executor as e
from test_png_pipeline import Tests as Base
import test_notion_executor as f

class Tests(Base):
 def session(self,**kw):
  return x.Session(self.ev,self.pub,self.outer,self.transport,self.policy,e.digest(self.policy),current_commit=lambda:self.g.asset_commit,asset_guard=lambda:True,writers_guard=lambda:deepcopy(self.writers),**kw)
 def test_preflight_has_zero_cas_and_zero_external_writes(self):
  before=self.outer.read();session=self.session();report=session.prepare()
  self.assertEqual(report['upload_candidates'],3,report);self.assertEqual(report['blocked'],0,report)
  self.assertEqual(self.outer.read(),before);self.assertFalse(self.transport.attempted)
  with self.assertRaisesRegex(ValueError,'approval_missing'):session.advance_uploads()
  self.assertEqual(self.outer.read(),before)
 def test_full_unsplittable_shape_stops_note_before_target_read_and_upload(self):
  original=e.p.render_markdown;calls=0
  def render(*args,**kw):
   nonlocal calls
   out=original(*args,**kw);calls+=1
   if calls==1:
    rows,bindings,warnings=out
    rows.append(e.p.block('quote',{'rich_text':[]},[e.p.block('paragraph',{'rich_text':[e.p.text_run('ordinary')]}) for _ in range(100)]))
   return out
  with patch.object(e.p,'render_markdown',side_effect=render):report=self.session().prepare()
  self.assertEqual(report['upload_candidates'],2,report);self.assertEqual(report['reason_counts'],{'request_budget':1})
  self.assertFalse(self.transport.attempted)
 def test_unowned_existing_target_blocks_before_any_upload(self):
  self.api.make(f.N,[e.p.block('paragraph',{'rich_text':[e.p.text_run('User content')]})])
  before=self.outer.read();report=self.session().prepare()
  self.assertEqual(report['upload_candidates'],2);self.assertEqual(report['reason_counts'],{'missing_block_ownership':1})
  self.assertEqual(self.outer.read(),before);self.assertFalse(self.transport.attempted)
 def test_privacy_blocks_before_query_and_upload(self):
  original=e.content_scope.reason
  with patch.object(e.content_scope,'reason',side_effect=lambda i,b:'privacy_scope_required' if i==f.N else original(i,b)):
   report=self.session().prepare()
  self.assertEqual(report['reason_counts'],{'privacy_scope_required':1});self.assertFalse(self.transport.attempted)
 def test_asset_guard_failure_stops_before_native_read(self):
  session=self.session();session.asset_guard=lambda:False
  with self.assertRaisesRegex(ValueError,'asset_current_changed'):session.prepare()
  self.assertEqual(self.transport.calls,[]);self.assertFalse(self.transport.attempted)
 def test_verify_unattached_reads_only_same_saved_upload_id_and_no_cas(self):
  session=self.session();before=self.outer.read();reads=[]
  class ReadTransport:
   def __init__(self,base,plan,approved,data,store,**kw):self.plan=plan;self.data=data;self.store=store
   def __call__(self,request):
    self.assertions(request)
    return 200,{'object':'file_upload','id':'a'*32,'status':'uploaded','filename':self.plan['filename'],'content_type':'image/png','content_length':len(self.data),'expiry_time':'2099-01-01T00:00:00Z'}
   def assertions(self,request):
    if request['method']!='GET' or not request['url'].endswith('/file_uploads/'+'a'*32):raise AssertionError('not exact saved GET')
    reads.append(request['method'])
  with patch.object(x.multipart,'Transport',ReadTransport):session.verify_unattached()
  self.assertEqual(reads,['GET']);self.assertEqual(self.outer.read(),before)
 def test_attached_verified_upload_needs_no_expiry_get(self):
  out=self.run_it(apply=True);self.assertEqual(out['execution']['created'],1)
  session=self.session()
  with patch.object(x.multipart,'Transport',side_effect=AssertionError('attached upload should not GET expiry')):session.verify_unattached()
 def test_existing_journal_cannot_start_upload_prepare(self):
  self.adapter.asset_get=lambda _:b'changed'
  self.run_it(apply=True)
  with self.assertRaisesRegex(ValueError,'journal_busy'):self.session().prepare()

 def test_coordinator_real_upload_transport_and_expiry_before_page(self):
  import json
  uploads={};posts=[]
  initial=self.outer.value['png_assets']['uploads'][f.N];uploads['a'*32]={'object':'file_upload','id':'a'*32,'status':'uploaded','filename':initial['plan']['filename'],'content_type':'image/png','content_length':initial['plan']['binding']['bytes'],'expiry_time':'2099-01-01T00:00:00Z'}
  class Reply:
   status=200
   def __init__(inner,value):inner.value=value
   def __enter__(inner):return inner
   def __exit__(inner,*args):pass
   def read(inner,n):return json.dumps(inner.value).encode()[:n]
  class Opener:
   def open(inner,request,timeout):
    path=request.full_url.split('/v1',1)[1]
    if request.method=='POST':posts.append(path)
    if path=='/file_uploads':
     body=json.loads(request.data);uid=('%032x'%(len(uploads)+100))
     row={'object':'file_upload','id':uid,'status':'pending','filename':body['filename'],'content_type':body['content_type'],'expiry_time':'2099-01-01T00:00:00Z'};uploads[uid]=row
     return Reply(row)
    uid=path.split('/')[2];row=uploads[uid]
    if path.endswith('/send'):
     marker=request.data.split(b'\r\n\r\n',1)[1];raw=marker.rsplit(b'\r\n--',1)[0]
     row.update(status='uploaded',content_length=len(raw))
    return Reply(row)
  self.transport.opener=Opener();self.transport.sleep=lambda _:None
  session=self.session(apply=True);self.assertEqual(session.prepare()['upload_candidates'],3)
  resolver=session.advance_uploads();self.assertEqual(len(resolver.receipts),3);self.assertEqual(len(posts),4)
  with self.assertRaisesRegex(ValueError,'preparation_report_expired'):session.report()
  session.verify_unattached();self.assertEqual(len(posts),4)
  with self.assertRaisesRegex(ValueError,'approval_missing'):session.advance_uploads()
  self.assertEqual(len(posts),4)

 def test_failed_upload_round_cannot_report_preparation_zero_writes(self):
  session=self.session(apply=True);session.prepare()
  with patch.object(session,'fresh_guard',side_effect=ValueError('asset_current_changed')):
   with self.assertRaisesRegex(ValueError,'asset_current_changed'):session.advance_uploads()
  with self.assertRaisesRegex(ValueError,'preparation_report_expired'):session.report()

# Do not inherit and repeat already separately verified base test methods.
for name in list(vars(Base)):
 if name.startswith('test_') and name not in Tests.__dict__:setattr(Tests,name,None)
if __name__=='__main__':unittest.main()
