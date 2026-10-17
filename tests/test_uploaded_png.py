import io,json,sys,tempfile,unittest,urllib.error
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_uploaded_png as m
from test_notion_png_evidence import Fixture

N='1234567890123456789';P='1'*32;B='2'*32;D='3'*32;U='4'*32
DATA=b'\x89PNG\r\n\x1a\n'+b'\0'*4+b'IHDR'+b'synthetic fixture'
URL='https://prod-files-secure.s3.us-west-2.amazonaws.com/a?private=one'

class Tests(unittest.TestCase):
 def setUp(self):
  pin={'md_sha256':'a'*64,'sha256':m.p.digest(DATA),'bytes':len(DATA),'git_blob':m.p.git_blob(DATA),'reference_sha256':'b'*64}
  for obj,name,value in ((m.upload,'PINS',{m.upload.sha(N.encode()):pin}),(m.upload,'DESTINATION_PIN',m.upload.sha(D.encode()))):
   q=patch.object(obj,name,value);q.start();self.addCleanup(q.stop)
  b={'note_id':N,**pin,'asset_path':'.sync/get-notion/assets/'+pin['sha256']+'.png','mime':'image/png','data_source_id':D,'source_commit':'f'*40}
  self.receipt={'upload_id':U,'plan_sha256':'c'*64,'source_binding':b,'content_length':len(DATA),'content_type':'image/png','native_bytes_verified':False,'binding_sha256':m.upload.digest(b),'kind':m.KIND,'sha256':pin['sha256'],'byte_count':len(DATA),'mime':'image/png'}
  self.receipt_sha=m.upload.digest(self.receipt)
  self.payload={'type':'file','file':{'url':URL,'expiry_time':'later'},'caption':[]}
  self.properties={'笔记 ID':{'type':'rich_text','rich_text':[{'type':'text','text':{'content':N}}]}}
  self.page={'object':'page','id':P,'archived':False,'last_edited_time':'time','parent':{'data_source_id':D},'properties':deepcopy(self.properties)}
  self.block={'object':'block','id':B,'parent':{'type':'page_id','page_id':P},'type':'image','image':deepcopy(self.payload),'has_children':False,'archived':False,'last_edited_time':'time'}
  self.snapshot={'complete':True,'identity_query_complete':True,'native_blocks_available':True,'page_id':P,'id_property':N,'exact_id_matches':[P],'last_edited_time':'time','properties_raw':deepcopy(self.properties),'parent_data_source':D,'recursive_blocks':[{'id':B,'parent_id':P,'type':'image','payload':deepcopy(self.payload),'has_children':False,'children_ids':[]}]}
  self.calls=[];self.urls=[];self.change=None
 def req(self,method,path,body):
  self.assertEqual(method,'GET');self.calls.append(path);v=deepcopy(self.page if '/pages/' in path else self.block)
  if self.change:self.change(v,len(self.calls))
  return v
 def get(self,url):self.urls.append(url);return DATA
 def bind(self,**kw):
  return m.bind_snapshot(self.snapshot,N,self.receipt,self.receipt_sha,request=self.req,asset_get=kw.get('asset_get',self.get))
 def test_verified_bytes_no_url_no_mutation(self):
  old=deepcopy(self.snapshot);out=self.bind()
  self.assertEqual(old,self.snapshot);self.assertEqual(out['asset_downloads'],1)
  self.assertEqual(out['binding']['block_id'],B);self.assertTrue(out['binding']['readback_verified'])
  self.assertEqual(out['snapshot']['recursive_blocks'][0]['payload'],m.semantic(self.receipt,[]))
  self.assertNotIn('private=one',str(out));self.assertEqual(len(self.calls),4)
 def test_exact_missing_target_does_no_reads(self):
  self.snapshot={'complete':True,'identity_query_complete':True,'exact_id_matches':[]}
  out=self.bind();self.assertIsNone(out['binding']);self.assertEqual(self.calls,[])
 def test_pinned_receipt_binding_and_schema(self):
  for mutate in (lambda x:x.update(upload_id='bad'),lambda x:x.update(extra=True),lambda x:x.update(native_bytes_verified=True),lambda x:x.update(byte_count=True),lambda x:x['source_binding'].update(asset_path='../x.png'),lambda x:x['source_binding'].update(md_sha256='d'*64)):
   old=deepcopy(self.receipt);mutate(self.receipt);self.receipt_sha=m.upload.digest(self.receipt)
   with self.assertRaisesRegex(m.Blocked,'asset_receipt_invalid'):self.bind()
   self.receipt=old;self.receipt_sha=m.upload.digest(old)
  self.assertEqual(self.calls,[])
 def test_duplicate_or_other_note_blocked_before_requests(self):
  for key,value in [('exact_id_matches',[P,'5'*32]),('id_property','9876543210987654321')]:
   old=deepcopy(self.snapshot);self.snapshot[key]=value
   with self.assertRaises(m.Blocked):self.bind()
   self.snapshot=old
  self.snapshot['recursive_blocks']*=2
  with self.assertRaisesRegex(m.Blocked,'asset_image_count'):self.bind()
  self.assertEqual(self.calls,[])
 def test_signed_url_rotation_same_pinned_bytes(self):
  self.change=lambda v,i:v['image'].update(file={'url':URL+str(i),'expiry_time':str(i)}) if v['object']=='block' else None
  out=self.bind();self.assertEqual(out['asset_downloads'],3)
  with self.assertRaisesRegex(m.Blocked,'asset_bytes_mismatch'):self.bind(asset_get=lambda _:b'wrong')
 def test_page_block_parent_caption_changes_fail(self):
  for mutate in (lambda v:v.update(id='5'*32),lambda v:v.update(last_edited_time='changed'),lambda v:v['parent'].update(page_id='5'*32),lambda v:v['image'].update(caption=[{'type':'text','text':{'content':'changed'}}])):
   self.calls=[];self.change=lambda v,i:mutate(v) if i==3 else None
   with self.assertRaises(m.Blocked):self.bind()
  self.calls=[];self.change=lambda v,i:v['properties'].update(user_data='changed') if i==4 else None
  with self.assertRaisesRegex(m.Blocked,'asset_page_changed'):self.bind()
 def test_unknown_host_no_download(self):
  self.snapshot['recursive_blocks'][0]['payload']['file']['url']='https://evil.invalid/a'
  with self.assertRaisesRegex(m.Blocked,'asset_host_unsupported'):self.bind()
  self.assertEqual(self.urls,[])
 def test_notion_permission_does_not_become_record_conflict(self):
  def denied(*args):raise RuntimeError('permission_denied')
  self.req=denied
  with self.assertRaisesRegex(m.Blocked,'^permission_denied$'):self.bind()
  self.assertEqual(self.urls,[])
 def test_download_no_credentials_and_slow_403_priority(self):
  class Response(io.BytesIO):status=200
  class Open:
   calls=[]
   def open(s,req,timeout):s.calls.append(req);return Response(DATA)
  op=Open();get=m.AssetGet(opener=op);self.bind(asset_get=get)
  self.assertIsNone(op.calls[0].get_header('Authorization'));self.assertIsNone(op.calls[0].get_header('Cookie'))
  class Denied:
   def open(s,req,timeout):raise urllib.error.HTTPError(req.full_url,403,'PRIVATE',{},None)
  ticks=iter([0,121]);get=m.AssetGet(opener=Denied(),clock=lambda:next(ticks))
  with self.assertRaisesRegex(m.Blocked,'^asset_permission_denied$'):get(URL)
  self.assertEqual(get.downloads,1);self.assertEqual(get.elapsed,121)

class ResolverTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.f=Fixture(Path(self.tmp.name)/'source');self.evidence=self.f.evidence();self.ident='100'
  self.current=lambda:self.f.run('rev-parse','HEAD').decode().strip()
  b=self.evidence.resolve(self.ident,self.f.data[self.ident],self.f.urls[self.ident],current_commit=self.current());a=b['asset']
  self.binding={'note_id':self.ident,'md_sha256':b['source']['sha256'],'asset_path':a['path'],'git_blob':a['blob'],'sha256':a['sha256'],'bytes':a['bytes'],'mime':'image/png','data_source_id':D,'source_commit':b['source_commit'],'reference_sha256':b['reference_sha256']}
  pin={k:self.binding[k] for k in ('md_sha256','sha256','bytes','git_blob','reference_sha256')}
  for obj,name,value in ((m.upload,'PINS',{m.upload.sha(self.ident.encode()):pin}),(m.upload,'DESTINATION_PIN',m.upload.sha(D.encode()))):
   q=patch.object(obj,name,value);q.start();self.addCleanup(q.stop)
  self.value={'upload_id':U,'plan_sha256':'c'*64,'source_binding':self.binding,'content_length':a['bytes'],'content_type':'image/png','native_bytes_verified':False,'binding_sha256':m.upload.digest(self.binding),'kind':m.KIND,'sha256':a['sha256'],'byte_count':a['bytes'],'mime':'image/png'}
 def resolver(self,preview=False):
  values={} if preview else {self.ident:{'receipt':self.value,'sha256':m.upload.digest(self.value)}}
  return m.Resolver(self.evidence,values,current_commit=self.current,preview=preview)
 def render(self,resolver):
  return m.p.render_markdown(self.f.data[self.ident],self.ident,{},self.f.records[self.ident]['path'],self.f.descriptor['content_tree'],reviewed_png=resolver)
 def test_exact_original_md_external_reference_and_own_tree(self):
  desc=deepcopy(self.f.descriptor);data=self.f.data[self.ident];rows,used,_=self.render(self.resolver())
  self.assertEqual(self.f.descriptor,desc);self.assertEqual(self.f.data[self.ident],data)
  image=[r for r in rows if r['type']=='image'];self.assertEqual(len(image),1)
  self.assertEqual(image[0]['payload']['upload_id'],U)
  self.assertNotIn('signature=',str(used));self.assertEqual(used['uploaded_png']['path'],self.binding['asset_path'])
 def test_preview_has_no_handle_and_both_write_encoders_reject(self):
  import get_notion_executor as engine
  import get_notion_live_transport as live
  rows,_,_=self.render(self.resolver(True));self.assertNotIn('upload_id',str(rows))
  self.assertEqual(m.preflight_blocks(rows)['write_ready'],False)
  for call in (engine.request_blocks,live.wire_blocks):
   with self.assertRaisesRegex(ValueError,'asset_write_contract_missing'):call(rows)
 def test_missing_receipt_never_means_upload_authorized(self):
  resolver=m.Resolver(self.evidence,{},current_commit=self.current)
  with self.assertRaisesRegex(m.p.Blocked,'missing_asset'):self.render(resolver)
 def test_wire_png_needs_exact_handle_byte_mapping_and_preserves_caption(self):
  import get_notion_executor as engine
  import get_notion_live_transport as live
  resolver=self.resolver();rows,_,_=self.render(resolver);request=engine.request_blocks(rows)
  with self.assertRaisesRegex(ValueError,'asset_write_contract_missing'):live.wire_blocks(request)
  wire=live.wire_blocks(request,uploaded_pngs=resolver.handles());image=[r for r in wire if r['type']=='image'][0]
  self.assertEqual(image['image']['file_upload'],{'id':U});self.assertEqual(image['image']['type'],'file_upload')
  self.assertNotIn('sha256',str(wire));self.assertNotIn('signature=',str(wire))
  wrong=resolver.handles();wrong[U]['sha256']='d'*64
  with self.assertRaisesRegex(ValueError,'asset_write_contract_missing'):live.wire_blocks(request,uploaded_pngs=wrong)
 def test_missing_asset_retry_is_exact_and_does_not_release_other_reasons(self):
  resolver=self.resolver();source=self.f.records[self.ident]
  failed={'reason':'missing_asset','source_commit':self.f.publication,'md_blob':source['blob']}
  self.assertTrue(resolver.may_retry(self.ident,source,failed))
  for reason in ('privacy_scope_required','prior_denial','unresolved_operation','request_budget'):
   self.assertFalse(resolver.may_retry(self.ident,source,{**failed,'reason':reason}))
  self.assertFalse(self.resolver(True).may_retry(self.ident,source,failed))
  self.assertFalse(resolver.may_retry(self.ident,{**source,'sha256':'e'*64},failed))
 def test_changed_source_and_arbitrary_external_reference_reject(self):
  resolver=self.resolver()
  for data,ref in ((self.f.data[self.ident]+b'changed',self.f.urls[self.ident]),(self.f.data[self.ident],'https://other.invalid/x.png')):
   with self.assertRaises(ValueError):resolver.bound(self.ident,data,ref,[])
 def test_source_or_asset_changes_stop_fresh_check(self):
  resolver=self.resolver();self.f.write(self.f.records[self.ident]['path'],b'changed');self.f.commit()
  with self.assertRaisesRegex(m.p.Blocked,'missing_asset'):self.render(resolver)
 def test_different_receipt_source_commit_rejects_before_render(self):
  self.value['source_binding']['source_commit']='e'*40
  self.value['binding_sha256']=m.upload.digest(self.value['source_binding'])
  with self.assertRaisesRegex(m.p.Blocked,'missing_asset'):self.render(self.resolver())

if __name__=='__main__':unittest.main()
