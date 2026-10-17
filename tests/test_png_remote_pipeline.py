"""Actual partial clones and local-bare CAS for PNG/page receipt atomicity."""
from copy import deepcopy
import unittest
import get_notion_executor as e
import get_notion_outbox as outbox
import get_notion_png_evidence as evidence
import get_notion_png_store as storage
import get_notion_uploaded_png as image
import test_notion_remote_journal as remote
import test_png_pipeline as base
import test_notion_executor as f

class Tests(base.Tests):
 def setUp(self):
  super().setUp()
  self.g.write(outbox.STATE_PATH,outbox.encoded(self.outer.value));self.g.commit()
  self.rf=remote.Fixture(self.root,seed=self.g.root)
  self.inherited=remote.run(self.rf.bare,'rev-parse',self.rf.initial+':inherited.bin').decode().strip()
  state_blob=remote.run(self.rf.bare,'rev-parse',self.rf.initial+':'+outbox.STATE_PATH).decode().strip()
  anchor={'commit':self.rf.initial,'state_blob':state_blob,'state_sha256':outbox.digest(self.outer.value)}
  self.install(anchor)
 def install(self,anchor):
  rf=self.rf;rf.load(rf.head())
  # Import only the selected immutable manifest, MD and three PNG blobs.
  selected={r['blob'] for r in self.g.records.values()}|{r['asset']['blob'] for r in self.g.manifest['records'].values()}|{self.g.anchor['manifest_blob']}
  for oid in selected:
   raw=remote.run(rf.bare,'cat-file','blob',oid)
   self.assertEqual(remote.run(rf.root,'hash-object','-w','--stdin',data=raw).decode().strip(),oid)
  self.outer=rf.store(anchor)
  parent=self.g.descriptor['parent'];self.pub=e.GitPublication(rf.root,self.g.publication,parent,self.g.publication)
  head=remote.run(rf.root,'rev-parse','refs/heads/main').decode().strip()
  self.ev=evidence.AssetEvidence(e.git.OfflineGit(rf.root),self.pub.descriptor,self.g.anchor,e.digest(self.g.anchor),head)
  value,_=self.outer.read()
  if value['engine_state']['journal'] is not None:
   desc=value['engine_state']['journal']['plan']['publication']
   self.pub.descriptor.update({k:desc[k] for k in ('outbox_receipt_sha256','outbox_state_sha256')})
  self.resolver=image.Resolver(self.ev,storage.upload_receipts(value),current_commit=lambda:head)
  def candidates(old,new):return {i:v for i,v in self.adapter.candidates.items() if i in new['receipts'] and old['receipts'].get(i)!=new['receipts'][i]}
  self.store=outbox.EngineStore(self.outer,attachment_provider=candidates);self.api.store=self.store
  self.adapter=image.SnapshotAdapter(self.resolver,self.outer.read,request=self.transport,asset_get=lambda _:self.g.images[f.N])
  self.before=(remote.run(rf.root,'show-ref'),remote.run(rf.root,'rev-parse','HEAD'),(rf.root/'.git/index').read_bytes())
 def unchanged(self):
  rf=self.rf
  self.assertEqual((remote.run(rf.root,'show-ref'),remote.run(rf.root,'rev-parse','HEAD'),(rf.root/'.git/index').read_bytes()),self.before)
  self.assertEqual(remote.run(rf.root,'cat-file','--batch-check',data=(self.inherited+'\n').encode()),(self.inherited+' missing\n').encode())
  self.assertEqual(remote.run(rf.bare,'show','main:inherited.bin'),b'Never downloaded inherited bytes')
 def test_actual_partial_clone_atomic_completion_and_fresh_noop(self):
  result=self.run_it(apply=True);self.assertIn('execution',result,result);self.assertEqual(result['execution']['created'],1,result)
  value,sha=self.outer.read();self.assertIn(f.N,value['png_assets']['attachments']);self.assertNotIn(f.N,value['pending_by_id'])
  self.assertIn(f.N,value['engine_state']['receipts']);self.assertEqual(len(self.transport.attempted),1)
  self.unchanged();anchor=deepcopy(self.outer.anchor);writes=len(self.rf.calls)
  self.rf.root=self.rf.clone('fresh-noop');self.install(anchor)
  count=len(self.transport.calls);result=self.run_it(apply=True)
  self.assertEqual(result['external_write_attempts'],0,result);self.assertEqual(len(self.transport.calls),count)
  self.assertEqual(self.outer.read()[1],sha);self.assertEqual(len(self.rf.calls),writes);self.unchanged()
 def test_failed_receipt_cas_fresh_clone_ack_only_no_recreate(self):
  original=self.outer.compare_and_set_verified
  def stop(expected,value):
   if f.N in value['engine_state']['receipts']:raise TimeoutError('synthetic completion interruption')
   return original(expected,value)
  self.outer.compare_and_set_verified=stop
  result=self.run_it(apply=True);self.assertEqual(result['write_state'],'uncertain',result)
  value,_=self.outer.read();self.assertEqual(value['engine_state']['journal']['operation']['phase'],'acknowledged')
  self.assertNotIn(f.N,value['png_assets']['attachments']);self.assertIn(f.N,value['pending_by_id'])
  self.unchanged();anchor=deepcopy(self.outer.anchor)
  self.rf.root=self.rf.clone('fresh-ack');self.install(anchor)
  result=self.run_it(apply=True);self.assertIn('execution',result,result);self.assertEqual(result['execution']['created'],1,result)
  self.assertEqual(len([row for row in self.api.calls if row[0]=='create']),1)
  value,_=self.outer.read();self.assertIn(f.N,value['png_assets']['attachments']);self.assertNotIn(f.N,value['pending_by_id']);self.unchanged()

 def test_actual_remote_not_dispatched_reset_and_fresh_clone(self):
  import get_notion_png_upload as upload
  import get_notion_png_transport as wire
  import get_notion_live_transport as live
  ident=f.N2;data=self.g.data[ident]
  b=self.ev.resolve(ident,data,self.g.urls[ident],current_commit=self.rf.initial);asset=b['asset']
  binding={'note_id':ident,'md_sha256':b['source']['sha256'],'asset_path':asset['path'],'git_blob':asset['blob'],'sha256':asset['sha256'],'bytes':asset['bytes'],'mime':'image/png','data_source_id':f.DS,'source_commit':b['source_commit'],'reference_sha256':b['reference_sha256']}
  plan=upload.prepare(binding,upload.digest(binding),self.ev.asset_tree,self.g.images[ident])
  state,sha=self.outer.read();state=storage.add_upload(state,plan,upload.digest(plan))
  outbox.confirmed_compare_and_set(self.outer,sha,state);before,before_sha=self.outer.read();calls=len(self.rf.calls)
  slot=storage.UploadStore(self.outer,ident,upload.digest(plan),allow_persist=True)
  class Opener:
   calls=0
   def open(inner,*args,**kw):inner.calls+=1;raise AssertionError('no opener expected')
  opener=Opener();transport=live.Transport('synthetic',opener=opener,clock=lambda:1,sleep=lambda _:None);transport.attempts=2000
  request=wire.Transport(transport,plan,upload.digest(plan),self.g.images[ident],slot,allow_writes=True,before_write=lambda:True)
  kernel=upload.Upload(plan,upload.digest(plan),self.g.images[ident],slot,request,allow_writes=True)
  with self.assertRaisesRegex(upload.Blocked,'^request_not_dispatched$'):kernel.advance()
  self.assertEqual(opener.calls,0);self.assertEqual(self.outer.read(),(before,before_sha));self.assertEqual(len(self.rf.calls),calls+2)
  with self.assertRaisesRegex(upload.Blocked,'^request_not_dispatched$'):kernel.advance()
  self.assertEqual(len(self.rf.calls),calls+2);self.unchanged()
  anchor=deepcopy(self.outer.anchor);self.rf.root=self.rf.clone('fresh-upload-reset');self.install(anchor)
  slot=storage.UploadStore(self.outer,ident,upload.digest(plan),allow_persist=True)
  self.assertEqual(slot.read()[0]['phase'],'prepared');self.assertIsNone(slot._not_dispatched);self.unchanged()

for name in list(vars(base.Tests)):
 if name.startswith('test_') and name not in Tests.__dict__:setattr(Tests,name,None)
if __name__=='__main__':unittest.main()
