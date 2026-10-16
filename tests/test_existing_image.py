import sys,unittest,io,urllib.error
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_existing_image as m
P='1'*32;B='2'*32;D='3'*32;N='1234567890123456789';DATA=b'\xff\xd8\xffsynthetic\xff\xd9';URL='https://s3.us-west-2.amazonaws.com/example/a?sig=one'
class Tests(unittest.TestCase):
 def setUp(self):
  self.binding={'contract':m.projection.CONTRACT,'note_id':N,'source_path':'Raw/03 Get/note.md','reference':'_assets/a.jpg','asset_path':'Raw/03 Get/_assets/a.jpg','asset_blob':m.p.git_blob(DATA),'asset_sha256':m.p.digest(DATA),'asset_bytes':len(DATA),'image_line_sha256':'a'*64}
  self.sha=m.p.digest(self.binding)
  self.patchers=[patch.object(m,'SOURCE_PIN',self.sha),patch.object(m,'PAGE_PIN',m.p.digest(P)),patch.object(m,'MAX_BYTES',len(DATA))]
  for x in self.patchers:x.start();self.addCleanup(x.stop)
  self.payload={'type':'file','file':{'url':URL,'expiry_time':'later'},'caption':[]}
  self.page={'object':'page','id':P,'archived':False,'last_edited_time':'time','parent':{'data_source_id':D},'properties':{}}
  self.block={'object':'block','id':B,'parent':{'type':'page_id','page_id':P},'type':'image','image':deepcopy(self.payload),'has_children':False,'archived':False,'last_edited_time':'time'}
  self.snapshot={'complete':True,'identity_query_complete':True,'native_blocks_available':True,'page_id':P,'id_property':N,'exact_id_matches':[P],'last_edited_time':'time','properties_raw':{},'parent_data_source':D,'recursive_blocks':[{'id':B,'parent_id':P,'type':'image','payload':deepcopy(self.payload),'has_children':False,'children_ids':[]}]}
  self.calls=[];self.urls=[];self.change=None
 def req(self,method,path,body):
  self.calls.append((method,path));v=deepcopy(self.page if '/pages/' in path else self.block)
  if self.change:self.change(v,len(self.calls))
  return v
 def get(self,url):self.urls.append(url);return DATA
 def runbind(self,**kw):
  return m.bind_existing_image(self.snapshot,source_binding=self.binding,expected_source_sha256=self.sha,source_commit='f'*40,content_tree={self.binding['asset_path']:self.binding['asset_blob']},request=self.req,asset_get=kw.get('asset_get',self.get))
 def test_valid_immutable_normalized(self):
  old=deepcopy(self.snapshot);result=self.runbind();self.assertEqual(old,self.snapshot);self.assertEqual(result['asset_downloads'],1);self.assertEqual(result['binding']['block_id'],B);self.assertTrue(result['binding']['readback_verified'])
  payload=result['snapshot']['recursive_blocks'][0]['payload'];self.assertEqual(set(payload),{'kind','block_id','sha256','byte_count','mime','caption'});self.assertNotIn('url',str(result));self.assertEqual(len(self.calls),4)
 def test_url_rotation_same_bytes_only(self):
  def change(v,i):
   if v['object']=='block':v['image']['file']={'url':URL+str(i),'expiry_time':str(i)}
  self.change=change;out=self.runbind();self.assertEqual(out['asset_downloads'],3)
  def bad(url):return DATA if url==URL else b'wrong'
  with self.assertRaisesRegex(m.Blocked,'asset_bytes_mismatch'):self.runbind(asset_get=bad)
 def test_different_bytes(self):
  with self.assertRaisesRegex(m.Blocked,'asset_bytes_mismatch'):self.runbind(asset_get=lambda u:b'wrong')
 def test_block_parent_caption_edit_and_id_changes(self):
  for mutate in (lambda v:v.update(last_edited_time='changed'),lambda v:v.update(id='4'*32),lambda v:v['parent'].update(page_id='4'*32),lambda v:v['image'].update(caption=[{'type':'text','text':{'content':'changed'}}])):
   with self.subTest(mutate=mutate):
    self.calls=[]
    self.change=lambda v,i:mutate(v) if i==3 else None
    with self.assertRaises(m.Blocked):self.runbind()
 def test_page_change(self):
  self.change=lambda v,i:v.update(last_edited_time='changed') if i==4 else None
  with self.assertRaisesRegex(m.Blocked,'asset_page_changed'):self.runbind()
 def test_unknown_host_before_asset_get(self):
  self.snapshot['recursive_blocks'][0]['payload']['file']['url']='https://evil.invalid/asset'
  with self.assertRaisesRegex(m.Blocked,'asset_host_unsupported'):self.runbind()
  self.assertEqual(self.urls,[])
 def test_bad_source_before_reads(self):
  self.binding['asset_blob']='a'*40
  with self.assertRaises(m.Blocked):self.runbind()
  self.assertEqual(self.calls,[])
 def test_duplicate_image_no_reads(self):
  self.snapshot['recursive_blocks']*=2
  with self.assertRaisesRegex(m.Blocked,'asset_image_count'):self.runbind()
  self.assertEqual(self.calls,[])
 def test_get_403_no_retry_no_auth(self):
  class Open:
   calls=[]
   def open(s,req,timeout):s.calls.append(req);raise urllib.error.HTTPError(req.full_url,403,'PRIVATE',{},None)
  op=Open();get=m.AssetGet(opener=op)
  with self.assertRaisesRegex(m.Blocked,'asset_permission_denied'):self.runbind(asset_get=get)
  self.assertEqual(len(op.calls),1);self.assertIsNone(op.calls[0].get_header('Authorization'));self.assertEqual(get.downloads,1)
 def test_url_restrictions_redirect_and_cap(self):
  for url in ('http://s3.us-west-2.amazonaws.com/a','https://u:p@s3.us-west-2.amazonaws.com/a','https://s3.us-west-2.amazonaws.com.evil.invalid/a','https://s3.us-west-2.amazonaws.com/a#x'):
   with self.assertRaises(m.Blocked):m.checked_url(url)
  self.assertIsNone(m.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))
  class Response(io.BytesIO):
   status=200
  class Open:
   def open(s,req,timeout):return Response(b'a'*(len(DATA)+1))
  with self.assertRaisesRegex(m.Blocked,'asset_byte_budget'):m.AssetGet(opener=Open())(URL)
 def test_notion_permission_failure_propagates(self):
  for error in (RuntimeError('permission_denied'),urllib.error.HTTPError('https://api.notion.com/x',401,'PRIVATE',{},None),urllib.error.HTTPError('https://api.notion.com/x',403,'PRIVATE',{},None)):
   def denied(*a):raise error
   self.req=denied
   with self.assertRaisesRegex(m.Blocked,'^permission_denied$') as ctx:self.runbind()
   self.assertEqual(ctx.exception.asset_downloads,0);self.assertEqual(self.urls,[])
 def test_shared_attempt_byte_and_time_budgets(self):
  class Response(io.BytesIO):status=200
  class Open:
   calls=0
   def open(s,req,timeout):s.calls+=1;return Response(DATA)
  op=Open();get=m.AssetGet(opener=op);get.downloads=199
  get(URL)
  with self.assertRaisesRegex(m.Blocked,'asset_download_budget'):get(URL)
  self.assertEqual(op.calls,1);self.assertEqual(get.downloads,200)
  get=m.AssetGet(opener=op);get.response_bytes=40*1024*1024-len(DATA)+1
  with self.assertRaisesRegex(m.Blocked,'asset_download_budget'):get(URL)
  ticks=iter([0,121]);get=m.AssetGet(opener=op,clock=lambda:next(ticks))
  with self.assertRaisesRegex(m.Blocked,'asset_download_budget'):get(URL)
  self.assertEqual(get.downloads,1)
 def test_failed_attempt_counts_and_deadline(self):
  class Open:
   def open(s,req,timeout):raise RuntimeError('PRIVATE')
  ticks=iter([0,121]);get=m.AssetGet(opener=Open(),clock=lambda:next(ticks))
  with self.assertRaisesRegex(m.Blocked,'asset_download_budget'):get(URL)
  self.assertEqual(get.downloads,1);self.assertEqual(get.elapsed,121)
  with self.assertRaisesRegex(m.Blocked,'asset_download_budget'):get(URL)
  self.assertEqual(get.downloads,1)
 def test_permission_failure_wins_over_expired_deadline(self):
  for status in (401,403):
   for as_error in (True,False):
    class Response(io.BytesIO):pass
    class Open:
     def open(s,req,timeout):
      if as_error:raise urllib.error.HTTPError(req.full_url,status,'PRIVATE',{},None)
      r=Response(b'');r.status=status;return r
    ticks=iter([0,121]);get=m.AssetGet(opener=Open(),clock=lambda:next(ticks))
    with self.assertRaisesRegex(m.Blocked,'^asset_permission_denied$'):get(URL)
    self.assertEqual(get.downloads,1);self.assertEqual(get.elapsed,121)
if __name__=='__main__':unittest.main()
