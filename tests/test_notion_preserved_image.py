from copy import deepcopy
import unittest
from unittest.mock import patch
import get_notion_executor as e
import get_notion_sink_plan as p
import get_notion_live_transport as live
import test_notion_executor as ef

class PreservedShapeTests(unittest.TestCase):
 def setUp(self):
  self.fixture=ef.Tests(methodName='test_state_only_git_commit_enqueues_nothing');self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
  self.snapshot=deepcopy(self.fixture.api.pages[ef.N]);page=self.snapshot['page_id'];self.bid='d1'*16
  self.payload={'kind':'notion_existing_image_block','block_id':self.bid,'sha256':'a'*64,'byte_count':191784,'mime':'image/jpeg','caption':[]}
  self.image={'id':self.bid,'parent_id':page,'type':'image','payload':deepcopy(self.payload),'children_ids':[],'has_children':False}
  self.snapshot['recursive_blocks'].insert(0,self.image)
 def test_native_block_identity_is_preserved_not_writable(self):
  view=e.target(self.snapshot,ef.N,ef.DS);self.assertEqual(view['nodes'][self.bid]['payload'],self.payload)
  with self.assertRaisesRegex(e.Blocked,'asset_write_contract_missing'):e.request_blocks([p.block('image',self.payload)])
  with self.assertRaisesRegex(live.Failure,'asset_write_contract_missing'):live.wire_blocks([p.block('image',self.payload)])
 def test_block_id_mismatch_and_child_are_not_accepted(self):
  for change in ('identity','children'):
   bad=deepcopy(self.snapshot)
   if change=='identity':bad['recursive_blocks'][0]['payload']['block_id']='ef'*16
   else:bad['recursive_blocks'][0]['children_ids']=['ef'*16];bad['recursive_blocks'][0]['has_children']=True
   with self.assertRaisesRegex(e.Blocked,'missing_asset'):e.target(bad,ef.N,ef.DS)
 def test_raw_api_image_is_explicit_missing_asset(self):
  self.image['payload']={'type':'file','file':{'url':'https://example.invalid/PRIVATE','expiry_time':'time'},'caption':[]}
  with self.assertRaisesRegex(e.Blocked,'missing_asset'):e.target(self.snapshot,ef.N,ef.DS)
 def test_malformed_preserved_payloads_fail_closed(self):
  for key,value in [('byte_count',True),('byte_count',0),('byte_count',p.MAX_ASSET+1),('mime','image/svg+xml'),('sha256','bad'),('caption',None),('kind','PRIVATE')]:
   payload=deepcopy(self.payload);payload[key]=value
   with self.assertRaises(Exception):p.normalize_payload('image',payload)
  payload=deepcopy(self.payload);payload['unrecognized']='PRIVATE'
  with self.assertRaises(Exception):p.normalize_payload('image',payload)
 def binding(self):return {'preserved_existing_image':{'path':'Raw/03 Get/_assets/image.jpeg','git_blob':'b'*40,'sha256':'a'*64,'preserved_block_id':self.bid,'page_id':self.snapshot['page_id'],'projection_sha256':'c'*64}}
 def test_receipt_retains_asset_only_as_protected_block(self):
  row={'blob':'e'*40};receipt=e.receipt(self.snapshot,ef.N,ef.DS,self.fixture.owned,{'commit':'f'*40},row,self.binding())
  state=e.initial_state();state['receipts'][ef.N]=receipt;e.check_state(state)
  self.assertEqual(receipt['asset_bindings'],self.binding());self.assertIn(self.bid,[r['id'] for r in receipt['protected_blocks']]);self.assertNotIn(self.bid,[r['id'] for r in receipt['managed_blocks']])
 def test_owned_or_missing_preserved_image_cannot_make_receipt(self):
  for managed in ([self.bid],self.fixture.owned):
   binding=self.binding()
   if managed==self.fixture.owned:binding['preserved_existing_image']['preserved_block_id']='fe'*16
   with self.assertRaises(Exception):e.receipt(self.snapshot,ef.N,ef.DS,managed,{'commit':'f'*40},{'blob':'e'*40},binding)
 def test_forged_asset_receipt_rejected_even_with_state_hash(self):
  receipt=e.receipt(self.snapshot,ef.N,ef.DS,self.fixture.owned,{'commit':'f'*40},{'blob':'e'*40},self.binding())
  for key,value in [('page_id','fe'*16),('git_blob','bad'),('preserved_block_id',self.fixture.owned[0]),('path','../outside')]:
   state=e.initial_state();state['receipts'][ef.N]=deepcopy(receipt);state['receipts'][ef.N]['asset_bindings']['preserved_existing_image'][key]=value
   with self.assertRaises(Exception):e.check_state(state)
if __name__=='__main__':unittest.main()

class PreservedTransactionTests(unittest.TestCase):
 def setUp(self):
  import get_notion_image_projection as projection
  self.projection=projection
  self.f=ef.Tests(methodName='test_state_only_git_commit_enqueues_nothing');self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.asset_path='Raw/03 Get/_assets/photo.jpeg';asset=self.f.repo/self.asset_path;asset.parent.mkdir();asset.write_bytes(b'\xff\xd8\xffsynthetic\xff\xd9')
  self.asset=asset.read_bytes();self.path='Raw/03 Get/notes/a.md';self.line='- Picture: ![[../_assets/photo.jpeg]]'
  self.f.source_path.write_bytes(ef.md(ef.N,'Old source\n\n'+self.line));self.base=self.f.commit('image source')
  blocks=[ef.paragraph('User prefix'),{'type':'image','payload':{'kind':'notion_existing_image_block','block_id':'a'*32,'sha256':e.digest(self.asset),'byte_count':len(self.asset),'mime':'image/jpeg','caption':[]},'children':[]},ef.paragraph('Old source'),ef.paragraph(projection.REPLACEMENT),ef.paragraph('User tail')]
  roots=self.f.api.make(ef.N,blocks);self.bid=roots[1];self.owned=roots[2:4];self.f.api.pages[ef.N]['recursive_blocks'][1]['payload']['block_id']=self.bid
  state,sha=self.f.store.read();self.f.store.compare_and_set(sha,e.initial_state())
  data=self.f.source_path.read_bytes();cohort={'commit':self.base,'records':{ef.N:{'path':self.path,'blob':p.git_blob(data),'sha256':e.digest(data)}}}
  self.f.publication=e.GitPublication(self.f.repo,self.base,self.f.parent,self.base)
  self.sb=projection.build_source_binding(data,ef.N,self.path,self.f.publication.descriptor['content_tree'],self.asset)
  self.binding={'source_binding':self.sb,'source_binding_sha256':e.digest(self.sb),'native_binding':{'kind':'notion_existing_image_block','asset_path':self.asset_path,'git_blob':p.git_blob(self.asset),'content_sha256':e.digest(self.asset),'block_id':self.bid,'page_id':self.f.api.pages[ef.N]['page_id'],'byte_count':len(self.asset),'mime':'image/jpeg','readback_verified':True}}
  self.scope={ef.N:{'page_id':self.f.api.pages[ef.N]['page_id'],'snapshot_sha256':e.target_sha(self.f.api.pages[ef.N],ef.N,ef.DS),'managed_block_ids':self.owned}}
 def prepare(self,**kw):
  state,sha=self.f.store.read();return e.prepare(self.f.publication,state,sha,{ef.N:self.f.api.read_note(ef.N)},self.f.policy,preserved_images={ef.N:self.binding},**kw)
 def adopt(self):
  plan=self.prepare(initial_scope=self.scope,expected_scope_sha=e.digest(self.scope));self.assertEqual(plan['records'][0]['action'],'noop',plan)
  self.assertEqual(self.f.run_plan(plan)['status'],'verified');self.assertEqual(self.f.writes(),[])
 def update(self):
  self.f.source_path.write_bytes(ef.md(ef.N,'Changed source\n\n'+self.line));head=self.f.commit('change text only');self.f.publication=e.GitPublication(self.f.repo,head,self.base,head)
  return self.prepare()
 def test_real_git_text_update_preserves_exact_native_image_and_noop(self):
  self.adopt();image=deepcopy(self.f.api.pages[ef.N]['recursive_blocks'][1]);props=deepcopy(self.f.api.pages[ef.N]['properties_raw']);plan=self.update();row=plan['records'][0]
  self.assertEqual(row['action'],'update',row);self.assertEqual(row['source']['blob'],p.git_blob(self.f.source_path.read_bytes()));self.assertNotIn(self.bid,row['old_roots']);self.assertTrue(all(x['type']!='image' for x in row['request_blocks']))
  result=self.f.run_plan(plan);self.assertEqual(result['updated'],1,result);self.assertEqual([r for r in self.f.api.pages[ef.N]['recursive_blocks'] if r['id']==self.bid],[image]);self.assertEqual(props,self.f.api.pages[ef.N]['properties_raw'])
  self.assertEqual(self.f.writes(),[('append',),('archive',),('archive',)])
  self.assertEqual(self.prepare()['records'],[])
 def test_timeout_append_recovers_once_image_never_archived(self):
  self.adopt();plan=self.update();self.f.api.throw='append_after';self.assertEqual(self.f.run_plan(plan)['reason'],'uncertain_append')
  self.f.api.throw=None;self.assertEqual(self.f.run_plan(plan)['updated'],1);self.assertEqual(self.f.writes().count(('append',)),1);self.assertTrue(any(x['id']==self.bid for x in self.f.api.pages[ef.N]['recursive_blocks']))
 def test_asset_changed_missing_or_new_reference_never_advances(self):
  self.adopt();before=self.f.store.read();plan=self.update()
  for mutate in ('native','source','binding'):
   with patch.dict(self.binding['native_binding'],{},clear=False):
    if mutate=='native':self.f.api.pages[ef.N]['recursive_blocks'][1]['payload']['sha256']='e'*64
    elif mutate=='source':self.f.source_path.write_bytes(self.f.source_path.read_bytes()+b'\n![extra](elsewhere)');parent=self.f.publication.descriptor['commit'];head=self.f.commit('extra');self.f.publication=e.GitPublication(self.f.repo,head,parent,head)
    else:self.binding['native_binding']['git_blob']='e'*40
    blocked=self.prepare();self.assertEqual(blocked['records'][0]['action'],'blocked');self.assertEqual(self.f.store.read(),before);self.assertEqual(self.f.writes(),[])
 def test_preserved_context_cannot_create_without_existing_image(self):
  self.f.api.pages.pop(ef.N);plan=self.prepare();self.assertEqual(plan['records'][0]['action'],'blocked');self.assertEqual(plan['records'][0]['reason'],'missing_asset');self.assertEqual(self.f.writes(),[])
 def test_image_moved_after_text_conflicts(self):
  self.adopt();self.update();rows=self.f.api.pages[ef.N]['recursive_blocks'];rows.append(rows.pop(1));self.assertEqual(self.prepare()['records'][0]['reason'],'missing_asset')
 def test_user_caption_and_other_unowned_text_preserved(self):
  self.adopt();self.f.api.pages[ef.N]['recursive_blocks'][1]['payload']['caption']=[p.text_run('User caption')]
  plan=self.update();self.assertEqual(plan['records'][0]['action'],'update');self.assertEqual(self.f.run_plan(plan)['status'],'verified')
  self.assertEqual(self.f.api.pages[ef.N]['recursive_blocks'][1]['payload']['caption'],[p.text_run('User caption')])

class NativePipelineTests(unittest.TestCase):
 def setUp(self):
  import get_notion_existing_image as binder
  import get_notion_preserved_image as bridge
  import get_notion_pipeline as pipeline
  from test_notion_pipeline import FixtureTransport
  from test_executor_live_adapter import OfficialFixture,DB
  self.binder=binder;self.bridge=bridge;self.pipeline=pipeline;self.DB=DB
  self.case=PreservedTransactionTests('test_real_git_text_update_preserves_exact_native_image_and_noop');self.case.setUp();self.addCleanup(self.case.doCleanups);self.f=self.case.f
  self.url='https://s3.us-west-2.amazonaws.com/synthetic/photo?token=PRIVATE';self.image=self.f.api.pages[ef.N]['recursive_blocks'][1]
  self.image['payload']={'type':'file','file':{'url':self.url,'expiry_time':'future'},'caption':[]}
  class Images(OfficialFixture):
   def __call__(s,method,path,body=None):
    if method=='GET' and path.startswith('/blocks/') and not path.split('?',1)[0].endswith('/children'):
     bid=path.rsplit('/',1)[1];page=next(x for x in s.api.pages.values() if any(r['id']==bid for r in x['recursive_blocks']));row=next(r for r in page['recursive_blocks'] if r['id']==bid)
     return {'object':'block','id':bid,'parent':{'type':'page_id','page_id':page['page_id']},'type':row['type'],row['type']:deepcopy(row['payload']),'has_children':False,'archived':False,'last_edited_time':page['last_edited_time']}
    if method=='PATCH':
     # The fake server computes tree positions with the normalizer; its
     # externally returned image still has the exact native file shape.
     replacements=[]
     for page in s.api.pages.values():
      for row in page['recursive_blocks']:
       if row['type']=='image':
        raw=deepcopy(row['payload']);replacements.append((row,raw));row['payload']={'kind':'notion_existing_image_block','block_id':row['id'],'sha256':p.digest(DATA),'byte_count':len(DATA),'mime':'image/jpeg','caption':raw['caption']}
     try:return super().__call__(method,path,body)
     finally:
      for row,raw in replacements:row['payload']=raw
    return super().__call__(method,path,body)
  DATA=self.case.asset
  self.transport=FixtureTransport(self.f.api);self.transport.fixture=Images(self.f.api)
  template=deepcopy(bridge.TEMPLATE);template.update(note_id_sha256=p.digest(ef.N),page_id_sha256=p.digest(self.f.api.pages[ef.N]['page_id']),source_path_sha256=p.digest(self.case.path),asset_blob=p.git_blob(self.case.asset),asset_sha256=p.digest(self.case.asset),asset_bytes=len(self.case.asset),source_binding_sha256=p.digest(self.case.sb))
  for obj,key,value in ((bridge,'TEMPLATE',template),(bridge,'TEMPLATE_SHA',p.digest(template)),(binder,'SOURCE_PIN',p.digest(self.case.sb)),(binder,'PAGE_PIN',p.digest(self.f.api.pages[ef.N]['page_id'])),(binder,'MAX_BYTES',len(self.case.asset))):
   x=patch.object(obj,key,value);x.start();self.addCleanup(x.stop)
  self.downloads=[]
  def asset_get(url):self.downloads.append(url);return self.case.asset
  self.adapter=bridge.Adapter(self.f.publication,self.transport,asset_get)
  api=live.API(self.transport,ef.DS,[ef.N],data_source_pin=live.native.sha(ef.DS),database_pin=live.native.sha(DB),snapshot_adapter=self.adapter)
  normalized=api.read_note(ef.N)
  self.scope={ef.N:{'page_id':normalized['page_id'],'snapshot_sha256':e.target_sha(normalized,ef.N,ef.DS),'managed_block_ids':self.case.owned}}
  self.policy={'contract':pipeline.CONTRACT,'rules_sha256':e.rules_sha256(),'data_source_id':ef.DS,'data_source_pin':live.native.sha(ef.DS),'database_pin':live.native.sha(DB),'title_property':'Title','allow_apply':True,'allow_create':True,'allow_archive':True,'format_accepted':True,'initial_scope_sha256':e.digest(self.scope),'plan_authorization':'standing_bounded'}
 def run_it(self,apply=False):
  return self.pipeline.run(self.f.publication,self.f.store,self.transport,self.policy,e.digest(self.policy),apply=apply,writers_guard=lambda:deepcopy(self.f.policy['old_writer_evidence']),initial_scope=self.scope,snapshot_adapter=self.adapter)
 def test_real_collector_binder_prepare_update_and_noop(self):
  before=self.f.store.read();image=deepcopy(self.image);out=self.run_it();self.assertEqual(out['actions'],{'noop':1},out);self.assertEqual(out['asset_downloads'],2);self.assertEqual(out['image_binding'],{'verified':1,'failures':{},'host':'exact_known_s3'});self.assertEqual(self.f.store.read(),before);self.assertFalse(self.transport.attempted)
  self.assertEqual(self.run_it(True)['execution']['noop'],1);self.assertFalse(self.transport.attempted)
  self.case.update();self.adapter.publication=self.f.publication;self.scope={};self.policy['initial_scope_sha256']=None
  out=self.run_it(True);self.assertEqual(out['execution']['updated'],1,out);self.assertEqual(self.image,image);self.assertEqual(len(self.transport.attempted),3);self.assertNotIn('PRIVATE',str(out));self.assertNotIn('notion_existing_image_block',str(out));self.assertGreater(out['asset_downloads'],2)
  before=len(self.downloads);again=self.run_it(True);self.assertEqual(again['selected'],0);self.assertEqual(before,len(self.downloads))
 def test_asset_failure_keeps_raw_image_blocked_no_state_change(self):
  before=self.f.store.read()
  self.adapter.asset_get=lambda url:b'wrong'
  out=self.run_it();self.assertEqual(out['actions'],{'blocked':1},out);self.assertEqual(out['reason_counts'],{'missing_asset':1});self.assertEqual(out['image_binding'],{'verified':0,'failures':{'asset_bytes_mismatch':1},'host':'exact_known_s3'});self.assertEqual(self.f.store.read(),before);self.assertEqual(out['asset_downloads'],2);self.assertFalse(self.transport.attempted)
 def test_notional_permission_denial_stops_pipeline_before_write(self):
  class Denied:
   def __call__(s,*args,**kwargs):raise live.Failure('permission_denied')
  self.adapter.request=Denied();out=self.run_it();self.assertEqual(out['status'],'failed',out);self.assertEqual(out['reason'],'permission_denied');self.assertEqual(out['asset_downloads'],1);self.assertFalse(self.transport.attempted)

 def test_report_rejects_arbitrary_image_diagnostics(self):
  for val in ({'verified':True,'failures':{}},{'verified':1,'failures':{'PRIVATE':1}},{'verified':1,'failures':{'missing_asset':1}}):
   out=self.run_it();out['image_binding']=val;clean=self.pipeline.public_report(out);self.assertEqual(clean['status'],'failed');self.assertNotIn('PRIVATE',str(clean))

 def test_permission_during_asset_read_retains_attempt_count(self):
  class AssetDenied:
   downloads=0
   def __call__(self,url):
    self.downloads+=1;raise self_case.binder.Blocked('asset_permission_denied')
  self_case=self;self.adapter.asset_get=AssetDenied();out=self.run_it();self.assertEqual(out['reason'],'asset_permission_denied');self.assertEqual(out['asset_downloads'],1);self.assertFalse(self.transport.attempted)
  import get_notion_runtime as runtime
  self.assertEqual(runtime.safe_result(out)['asset_downloads'],1)

 def test_lookalike_host_is_reported_but_never_fetched(self):
  self.image['payload']['file']['url']='https://prod-files-secure.s3.us-west-2.amazonaws.com.evil.invalid/PRIVATE?token=PRIVATE'
  before=len(self.downloads);out=self.run_it();self.assertEqual(out['actions'],{'blocked':1});self.assertEqual(out['image_binding']['host'],'other');self.assertEqual(out['image_binding']['failures'],{'asset_host_unsupported':1});self.assertEqual(len(self.downloads),before);self.assertNotIn('PRIVATE',str(out))

 def test_authenticated_prod_host_prepare_preserves_image(self):
  self.image['payload']['file']['url']='https://prod-files-secure.s3.us-west-2.amazonaws.com/PRIVATE?token=PRIVATE'
  old=deepcopy(self.image);state=self.f.store.read();before=len(self.downloads)
  out=self.run_it();self.assertEqual(out['actions'],{'noop':1},out);self.assertEqual(out['image_binding'],{'verified':1,'failures':{},'host':'exact_prod_files_secure'})
  self.assertEqual(len(self.downloads),before+1);self.assertEqual(self.image,old);self.assertEqual(self.f.store.read(),state);self.assertFalse(self.transport.attempted);self.assertNotIn('PRIVATE',str(out))
