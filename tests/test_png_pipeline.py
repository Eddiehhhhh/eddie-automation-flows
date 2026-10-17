"""Real Git → exact source evidence → native collector → page receipt fixtures."""
from copy import deepcopy
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
import get_notion_executor as e
import get_notion_pipeline as q
import get_notion_live_transport as t
import get_notion_uploaded_png as x
import get_notion_png_evidence as evidence
import test_notion_executor as f
from test_executor_live_adapter import OfficialFixture,DB
from test_notion_png_evidence import Fixture,png
import get_notion_outbox as outbox
import get_notion_png_store as pngstore
from test_notion_png_store import Store

class Transport(t.Transport):
 def __init__(self,api,data):
  super().__init__('synthetic-test-only');self.native=OfficialFixture(api);self.api=api;self.data=data;self.calls=[]
 def block(self,row,page):
  parent=row['parent_id'];kind=row['type'];payload=deepcopy(row['payload'])
  if kind=='image':payload={'type':'file','file':{'url':'https://prod-files-secure.s3.us-west-2.amazonaws.com/synthetic?signature=fixture','expiry_time':'2099-01-01T00:00:00Z'},'caption':payload.get('caption',[])}
  return {'object':'block','id':row['id'],'parent':{'type':'page_id' if parent==page else 'block_id',('page_id' if parent==page else 'block_id'):parent},'archived':False,'last_edited_time':'fixed-block-edit','has_children':row['has_children'],'type':kind,kind:payload}
 def __call__(self,method,path,body=None):
  self.calls.append((method,path));read=bool(t.ro.read_route(method,path));key=t.fingerprint(method,path,body)
  if not read:
   if key not in self.approved or key in self.attempted:raise AssertionError('unapproved fixture write')
   self.attempted.add(key)
  if method=='GET' and path.startswith('/blocks/'):
   ident=path.split('/')[2]
   for snap in self.api.pages.values():
    if path.split('?')[0].endswith('/children'):
     rows=[self.block(r,snap['page_id']) for r in snap['recursive_blocks'] if r['parent_id']==ident]
     if rows or ident==snap['page_id']:return self.native.listing(rows)
    else:
     for row in snap['recursive_blocks']:
      if row['id']==ident:return self.block(row,snap['page_id'])
  return self.native(method,path,body)

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.g=Fixture(self.root/'git');g=self.g
  g.run('reset','--hard',g.descriptor['parent']);parent=g.run('rev-parse','HEAD').decode().strip()
  g.data={};g.urls={};g.images={};g.records={}
  for k,ident in enumerate((f.N,f.N2,'3234567890123456789')):
   url='https://get-notes.umiwi.com/synthetic-'+str(k)+'.png?signature=fixture'
   data=f.md(ident,'# Heading\n\n![Caption]('+url+')\n\nTail')
   path='Raw/03 Get/notes/'+ident+'.md';g.write(path,data)
   g.data[ident]=data;g.urls[ident]=url;g.images[ident]=png(k+2,2)
   g.records[ident]={'path':path,'blob':e.p.git_blob(data),'sha256':e.digest(data)}
  g.publication=g.commit();g.parent=g.publication
  g.descriptor={'repository':e.p.REPOSITORY,'commit':g.publication,'parent':parent,'changed_content_ids':sorted(g.records),'records':deepcopy(g.records),'content_tree':{r['path']:r['blob'] for r in g.records.values()}}
  g.manifest={'contract':evidence.CONTRACT,'repository':e.p.REPOSITORY,'publication_commit':g.publication,'records':{}}
  for ident,source in g.records.items():
   data=g.images[ident];sha=e.digest(data);w,h=evidence.png_shape(data)
   g.manifest['records'][ident]={'source':source,'reference_sha256':e.digest(g.urls[ident].encode()),'asset':{'path':evidence.PREFIX+sha+'.png','blob':e.p.git_blob(data),'sha256':sha,'bytes':len(data),'mime':'image/png','width':w,'height':h}}
  g.publish();self.pub=e.GitPublication(g.root,g.publication,parent,g.publication)
  self.ev=evidence.AssetEvidence(e.git.OfflineGit(g.root),self.pub.descriptor,g.anchor,e.digest(g.anchor),g.asset_commit)
  b=self.ev.resolve(f.N,g.data[f.N],g.urls[f.N],current_commit=g.asset_commit);a=b['asset']
  binding={'note_id':f.N,'md_sha256':b['source']['sha256'],'asset_path':a['path'],'git_blob':a['blob'],'sha256':a['sha256'],'bytes':a['bytes'],'mime':'image/png','data_source_id':f.DS,'source_commit':b['source_commit'],'reference_sha256':b['reference_sha256']}
  pins={x.upload.sha(ident.encode()):{'md_sha256':r['source']['sha256'],'sha256':r['asset']['sha256'],'bytes':r['asset']['bytes'],'git_blob':r['asset']['blob'],'reference_sha256':r['reference_sha256']} for ident,r in g.manifest['records'].items()}
  for obj,key,value in ((x.upload,'PINS',pins),(x.upload,'DESTINATION_PIN',x.upload.sha(f.DS.encode()))):
   ctx=patch.object(obj,key,value);ctx.start();self.addCleanup(ctx.stop)
  self.upload={'upload_id':'a'*32,'plan_sha256':'c'*64,'source_binding':binding,'content_length':a['bytes'],'content_type':'image/png','native_bytes_verified':False,'binding_sha256':x.upload.digest(binding),'kind':x.KIND,'sha256':a['sha256'],'byte_count':a['bytes'],'mime':'image/png'}
  upload_plan=x.upload.prepare(binding,x.upload.digest(binding),self.ev.asset_tree,g.images[f.N])
  outer=pngstore.add_upload(pngstore.initialize(outbox.initial_state(),g.anchor,outbox.digest(g.anchor)),upload_plan,x.upload.digest(upload_plan))
  self.outer=Store(outer)
  upload_store=pngstore.UploadStore(self.outer,f.N,x.upload.digest(upload_plan),allow_persist=True)
  def upload_request(spec):
   return 200,{'object':'file_upload','id':'a'*32,'status':'pending' if spec['url'].endswith('/file_uploads') else 'uploaded','filename':upload_plan['filename'],'content_type':'image/png','content_length':len(g.images[f.N]),'expiry_time':'2099-01-01T00:00:00Z'}
  kernel=x.upload.Upload(upload_plan,x.upload.digest(upload_plan),g.images[f.N],upload_store,upload_request,allow_writes=True)
  kernel.advance();kernel.advance();kernel.advance()
  outer,_=self.outer.read();origin={'commit':parent,'receipt_sha256':None,'sequence':0};tip={'commit':g.publication,'receipt_sha256':'1'*64,'sequence':1}
  outer.update(origin=origin,policy_sha256='2'*64,last_enqueued_publication=tip,observed_publication_tip=tip,pending_by_id={i:{**r,'commit':g.publication,'parent':parent,'receipt_sha256':'1'*64,'sequence':1} for i,r in g.records.items()},rotation_order=sorted(g.records))
  outbox.check_state(outer);self.outer.value=outer
  self.resolver=x.Resolver(self.ev,pngstore.upload_receipts(outer),current_commit=lambda:g.asset_commit)
  def candidates(old,new):
   return {i:v for i,v in self.adapter.candidates.items() if i in new['receipts'] and old['receipts'].get(i)!=new['receipts'][i]}
  self.store=outbox.EngineStore(self.outer,attachment_provider=candidates)
  self.api=f.API(self.store);self.transport=Transport(self.api,g.images[f.N])
  self.adapter=x.SnapshotAdapter(self.resolver,self.outer.read,request=self.transport,asset_get=lambda url:g.images[f.N])
  self.attachments=self.outer.value['png_assets']['attachments']
  self.policy={'contract':q.CONTRACT,'rules_sha256':e.rules_sha256(),'data_source_id':f.DS,'data_source_pin':t.native.sha(f.DS),'database_pin':t.native.sha(DB),'title_property':'Title','allow_apply':True,'allow_create':True,'allow_archive':True,'format_accepted':True,'initial_scope_sha256':None,'plan_authorization':'standing_bounded'}
  self.writers={'fresh_readback_verified':True,'writers':{name:{'disabled':True,'active_runs':0} for name in ('getnote-to-notion','notion-to-getnote')}}
 def run_it(self,**kw):
  outer,sha=self.outer.read()
  if outer['engine_state']['journal'] is None:
   self.pub.descriptor.update(outbox_receipt_sha256='1'*64,outbox_state_sha256=sha)
  def settle(plan,approved):return outbox.settle_empty_plan(self.outer,sha,plan,approved)
  return q.run(self.pub,self.store,self.transport,self.policy,e.digest(self.policy),writers_guard=lambda:deepcopy(self.writers),snapshot_adapter=self.adapter,reviewed_png=self.resolver,empty_plan_settle=settle,**kw)
 def test_real_wire_native_bytes_create_and_second_zero_write(self):
  before=self.store.read();preview=self.run_it();self.assertEqual(preview['actions'],{'create':1,'blocked':2},preview);self.assertEqual(before,self.store.read());self.assertFalse(self.transport.attempted)
  out=self.run_it(apply=True);self.assertEqual(out['execution']['created'],1,out);self.assertEqual(len(self.transport.attempted),1);self.assertEqual(self.adapter.downloads,1)
  state,_=self.store.read();receipt=state['receipts'][f.N];binding=self.adapter.candidates[f.N]
  self.assertEqual(binding['page_id'],receipt['page_id']);self.assertIn({'id':binding['block_id'],'normalized_hash':binding['managed_hash']},receipt['managed_blocks'])
  self.assertEqual(self.outer.value['png_assets']['attachments'][f.N],binding)
  self.assertNotIn(f.N,self.outer.value['pending_by_id'])
  before=len(self.transport.calls);again=self.run_it(apply=True);self.assertEqual(again['external_write_attempts'],0,again);self.assertEqual(len(self.transport.calls),before)
 def test_acknowledged_readback_failure_resumes_without_second_create(self):
  self.adapter.asset_get=lambda url:b'wrong bytes'
  first=self.run_it(apply=True);self.assertEqual(first['write_state'],'uncertain',first)
  state,_=self.store.read();self.assertEqual(state['journal']['operation']['phase'],'acknowledged')
  saved_plan=e.encoded(state['journal']['plan']);self.assertEqual(len(self.transport.attempted),1)
  self.adapter.asset_get=lambda url:self.g.images[f.N]
  resumed=self.run_it(apply=True);self.assertEqual(resumed['execution']['created'],1,resumed)
  self.assertEqual(len(self.transport.attempted),1)
  self.assertEqual(len([c for c in self.api.calls if c[0]=='create']),1)
  self.assertEqual(e.encoded(state['journal']['plan']),saved_plan)
 def test_owned_join_requires_same_native_block_and_exact_durable_binding(self):
  out=self.run_it(apply=True);self.assertEqual(out['execution']['created'],1)
  self.attachments=self.outer.value['png_assets']['attachments']
  api=t.API(self.transport,f.DS,[f.N],data_source_pin=t.native.sha(f.DS),database_pin=t.native.sha(DB),snapshot_adapter=self.adapter,uploaded_pngs=self.resolver.handles())
  api.read_note(f.N)
  self.attachments[f.N]['upload_id']='b'*32
  with self.assertRaisesRegex(x.Blocked,'asset_receipt_invalid'):api.read_note(f.N)
  self.attachments[f.N]=deepcopy(self.adapter.candidates[f.N])
  self.api.pages[f.N]['recursive_blocks'][1]['payload']['caption']=[e.p.text_run('Human changed caption')]
  with self.assertRaisesRegex(x.Blocked,'asset_receipt_invalid'):api.read_note(f.N)
 def test_native_existing_image_without_saved_ack_or_receipt_never_adopted(self):
  rendered,_,_=e.p.render_markdown(self.g.data[f.N],f.N,{},self.g.records[f.N]['path'],self.pub.descriptor['content_tree'],reviewed_png=self.resolver)
  self.api.make(f.N,rendered);out=self.run_it()
  self.assertEqual(out['status'],'failed',out);self.assertFalse(self.transport.attempted);self.assertEqual(self.adapter.downloads,0)
 def test_failed_final_outer_cas_fresh_adapters_readback_no_second_create(self):
  original=self.outer.compare_and_set
  def fail_completion(expected,value):
   if f.N in value['engine_state']['receipts']:raise TimeoutError('synthetic interruption')
   return original(expected,value)
  self.outer.compare_and_set=fail_completion
  first=self.run_it(apply=True);self.assertEqual(first['write_state'],'uncertain',first)
  value,_=self.outer.read();self.assertEqual(value['engine_state']['journal']['operation']['phase'],'acknowledged')
  self.assertNotIn(f.N,value['png_assets']['attachments']);self.assertIn(f.N,value['pending_by_id'])
  self.outer.compare_and_set=original
  def candidates(old,new):return {i:v for i,v in self.adapter.candidates.items() if i in new['receipts'] and old['receipts'].get(i)!=new['receipts'][i]}
  self.store=outbox.EngineStore(self.outer,attachment_provider=candidates);self.api.store=self.store
  self.adapter=x.SnapshotAdapter(self.resolver,self.outer.read,request=self.transport,asset_get=lambda url:self.g.images[f.N])
  resumed=self.run_it(apply=True);self.assertEqual(resumed['execution']['created'],1,resumed)
  value,_=self.outer.read();self.assertNotIn(f.N,value['pending_by_id']);self.assertIn(f.N,value['png_assets']['attachments'])
  self.assertEqual(len([c for c in self.api.calls if c[0]=='create']),1)
 def test_missing_attachment_provider_never_commits_page_receipt(self):
  self.store=outbox.EngineStore(self.outer);self.api.store=self.store
  first=self.run_it(apply=True);self.assertEqual(first['write_state'],'uncertain',first)
  value,_=self.outer.read();self.assertNotIn(f.N,value['engine_state']['receipts']);self.assertNotIn(f.N,value['png_assets']['attachments'])
  self.assertIn(f.N,value['pending_by_id']);self.assertEqual(value['engine_state']['journal']['operation']['phase'],'acknowledged')
 def test_budget_preflight_blocks_large_structure_before_upload_handle(self):
  rows=[e.p.block('paragraph',{'rich_text':[e.p.text_run('Paragraph')]}) for _ in range(101)]
  rows[0]=e.p.block('image',{'kind':'notion_png_preflight','sha256':'a'*64,'byte_count':100,'mime':'image/png','caption':[]})
  with self.assertRaisesRegex(ValueError,'request_budget'):x.preflight_blocks(rows)
 def test_manifest_or_selected_asset_change_rejected_at_existing_asset_gate(self):
  plan=self.run_it();self.assertEqual(plan['actions'].get('create'),1)
  self.g.write(evidence.MANIFEST_PATH,b'changed');self.g.commit()
  out=self.run_it(apply=True);self.assertFalse(self.transport.attempted);self.assertEqual(out['actions'],{'blocked':3});self.assertEqual(out['reason_counts'],{'missing_asset':3});self.assertEqual(out['external_write_attempts'],0)

if __name__=='__main__':unittest.main()
