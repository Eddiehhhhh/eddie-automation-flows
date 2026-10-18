from copy import deepcopy
import unittest
from unittest.mock import patch
import get_notion_png_runtime as runtime
import get_notion_png_store as storage
import get_notion_png_upload as upload
import get_notion_uploaded_png as image
import get_notion_pipeline as pipeline
import get_notion_executor as e
import get_notion_outbox as outbox
import test_notion_executor as f
from test_png_runtime import Tests as Base


class Tests(Base):
 def add_second(self):
  session=self.session();plan,data=session._binding(f.N2,self.g.data[f.N2])
  value,sha=self.outer.read();value=storage.add_upload(value,plan,upload.digest(plan));self.outer.compare_and_set(sha,value)
  slot=storage.UploadStore(self.outer,f.N2,upload.digest(plan),allow_persist=True)
  def request(spec):
   return 200,{'object':'file_upload','id':'b'*32,'status':'pending' if spec['url'].endswith('/file_uploads') else 'uploaded','filename':plan['filename'],'content_type':'image/png','content_length':len(data),'expiry_time':'2099-01-01T00:00:00Z'}
  kernel=upload.Upload(plan,upload.digest(plan),data,slot,request,allow_writes=True)
  for _ in range(3):kernel.advance()
  self.resolver=image.Resolver(self.ev,storage.upload_receipts(self.outer.value),current_commit=lambda:self.g.asset_commit)
  self.adapter=image.SnapshotAdapter(self.resolver,self.outer.read,request=self.transport,asset_get=lambda _:b'invalid for interrupted readback')

 def interrupt_ack(self):
  self.add_second();first=self.run_it(apply=True)
  self.assertEqual(first['execution']['reason'],'transport_read_failed',first)
  journal=self.outer.value['engine_state']['journal']
  self.assertEqual(journal['operation']['phase'],'acknowledged')
  self.assertEqual(journal['index'],0)
  return e.encoded(journal['plan'])

 def run_guarded(self,session):
  state,sha=self.outer.read()
  return pipeline.run(self.pub,self.store,self.transport,self.policy,e.digest(self.policy),apply=True,
   writers_guard=session.verify_unattached,snapshot_adapter=self.adapter,reviewed_png=self.resolver)

 def test_acknowledged_first_readback_then_expired_peer_stops_before_second_create(self):
  original=self.interrupt_ack();session=self.session();calls=[]
  class Expired:
   def advance(inner):raise ValueError('upload_expired_or_near_expiry')
  def kernel(ident,*args,**kw):calls.append(ident);return Expired()
  self.adapter.asset_get=lambda _:self.g.images[f.N]
  with patch.object(session,'_kernel',side_effect=kernel):result=self.run_guarded(session)
  state,_=self.outer.read();engine=state['engine_state']
  self.assertIn(f.N,engine['receipts']);self.assertIn(f.N,state['png_assets']['attachments'])
  self.assertNotIn(f.N,state['pending_by_id']);self.assertIn(f.N2,state['pending_by_id'])
  self.assertEqual(engine['journal']['index'],1);self.assertEqual(engine['journal']['operation']['phase'],'prepared')
  self.assertEqual(calls,[f.N2]);self.assertEqual(len([x for x in self.api.calls if x[0]=='create']),1)
  self.assertEqual(e.encoded(engine['journal']['plan']),original)
  self.assertEqual(result['execution']['created'],1,result)
  self.assertEqual(result['execution']['write_calls'],0,result)
  # Fresh process/session rechecks the peer; it cannot reuse the first-row skip.
  fresh=self.session();before=self.outer.read()
  with patch.object(fresh,'_kernel',side_effect=kernel):
   with self.assertRaisesRegex(ValueError,'upload_expired'):fresh.verify_unattached()
  self.assertEqual(self.outer.read(),before);self.assertEqual(calls,[f.N2,f.N2])

 def test_prepared_and_inflight_never_skip_expiry(self):
  self.interrupt_ack();session=self.session()
  for phase in ('prepared','in_flight'):
   self.outer.value['engine_state']['journal']['operation'].update(phase=phase,response=None)
   outbox.check_state(self.outer.value);calls=[]
   with patch.object(session,'_kernel',side_effect=lambda ident,*a,**kw:calls.append(ident) or (_ for _ in ()).throw(ValueError('upload_expired_or_near_expiry'))):
    with self.assertRaisesRegex(ValueError,'upload_expired'):session.verify_unattached()
   self.assertEqual(calls,[f.N])

 def test_ack_does_not_skip_fresh_asset_or_writer_guard(self):
  self.interrupt_ack();session=self.session();before=self.outer.read()
  session.asset_guard=lambda:False
  with self.assertRaisesRegex(ValueError,'asset_current_changed'):session.verify_unattached()
  session.asset_guard=lambda:True;session.writers_guard=lambda:{}
  with self.assertRaisesRegex(ValueError,'writer_gate_unknown'):session.verify_unattached()
  self.assertEqual(self.outer.read(),before)

 def test_missing_ack_response_and_wrong_publication_never_grant_skip(self):
  self.interrupt_ack();session=self.session();op=self.outer.value['engine_state']['journal']['operation'];op['response']=None
  with patch.object(session,'_kernel',side_effect=ValueError('upload_expired_or_near_expiry')):
   with self.assertRaises(ValueError):session.verify_unattached()
  # A current publication mismatch is caught even if a caller has valid source
  # tree bytes; the guard does not select a row from another journal plan.
  self.pub.descriptor=deepcopy(self.pub.descriptor);self.pub.descriptor['outbox_state_sha256']='f'*64
  with self.assertRaisesRegex(ValueError,'source_changed'):session.verify_unattached()

 def test_unknown_operation_and_malformed_saved_id_cannot_skip(self):
  self.interrupt_ack();session=self.session();original=deepcopy(self.outer.value)
  for changes in ({'kind':'unknown'},{'phase':'unknown'},{'response':{'id':'a'*32}}):
   self.outer.value=deepcopy(original);self.outer.value['engine_state']['journal']['operation'].update(changes)
   with self.assertRaises(Exception):session.verify_unattached()
  self.outer.value=deepcopy(original);self.outer.value['engine_state']['journal']['operation']=None;calls=[]
  with patch.object(session,'_kernel',side_effect=lambda ident,*a,**kw:calls.append(ident) or (_ for _ in ()).throw(ValueError('upload_expired_or_near_expiry'))):
   with self.assertRaisesRegex(ValueError,'upload_expired'):session.verify_unattached()
  self.assertEqual(calls,[f.N])

for name in list(vars(Base)):
 if name.startswith('test_') and name not in Tests.__dict__:setattr(Tests,name,None)
if __name__=='__main__':unittest.main()
