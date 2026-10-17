import sys,unittest,json,io,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_live_transport as m
import get_notion_native as n
P='1'*32;D='2'*32;DB='3'*32;B='4'*32;N='1234567890123456789'
def listing(rows):return {'object':'list','results':rows,'has_more':False,'next_cursor':None}
def page():return {'object':'page','id':P,'archived':False,'last_edited_time':'2026-01-01','parent':{'type':'data_source_id','data_source_id':D},'properties':{'笔记 ID':{'type':'rich_text','rich_text':[{'type':'text','text':{'content':N}}]}}}
def semantic():return [{'type':'paragraph','payload':{'rich_text':[{'type':'text','text':{'content':'hello','link':None}}]},'children':[]}]
class Fake:
 def __init__(self):self.calls=[];self.missing=False;self.duplicate=False;self.changed=False;self.page_reads=0
 def __call__(self,method,path,body):
  self.calls.append((method,path,body))
  if path=='/data_sources/'+D:return {'object':'data_source','id':D,'parent':{'database_id':DB},'properties':{'笔记 ID':{'type':'rich_text'}}}
  if path.endswith('/query'):return listing([] if self.missing else [page()]*(2 if self.duplicate else 1))
  if path=='/pages/'+P:
   self.page_reads+=1;r=page();r['request_id']=str(self.page_reads)
   if self.changed and self.page_reads%2==0:r['last_edited_time']='changed'
   return r
  if method=='GET' and path.startswith('/blocks/'):return listing([{'object':'block','id':B,'parent':{'type':'page_id','page_id':P},'type':'paragraph','paragraph':{'rich_text':[]},'has_children':False,'archived':False}])
  if method=='PATCH' and path.endswith('/children'):return listing([{'object':'block','id':'5'*32}])
  if method=='PATCH':return {'object':'block','id':B,'archived':True}
  if path=='/pages':return page()
  raise AssertionError('unexpected route')
class Response(io.BytesIO):
 status=200
class Opener:
 def __init__(self,events):self.events=list(events);self.calls=[]
 def open(self,req,timeout):
  self.calls.append(req)
  event=self.events.pop(0)
  if isinstance(event,Exception):raise event
  return Response(event)
class Test(unittest.TestCase):
 def api(self,f,**kw):return m.API(f,D,[N],data_source_pin=n.sha(D),database_pin=n.sha(DB),**kw)
 def test_snapshot(self):
  f=Fake();r=self.api(f).read_note(N);self.assertTrue(r['complete']);self.assertEqual(len(r['recursive_blocks']),1);self.assertFalse(any(p=='/search' for _,p,_ in f.calls))
 def test_missing_duplicate(self):
  for missing in (True,False):
   f=Fake();f.missing=missing;f.duplicate=not missing;r=self.api(f).read_note(N);self.assertEqual(len(r['exact_id_matches']),0 if missing else 2)
 def test_edit(self):
  f=Fake();f.changed=True
  with self.assertRaisesRegex(n.Blocked,'page_changed_during_read'):self.api(f).read_note(N)
 def test_numeric_and_budget_ids(self):
  for ids in ([int(N)],[N]*51):
   with self.assertRaises(m.Failure):m.API(Fake(),D,ids,data_source_pin=n.sha(D))
 def test_writes_disabled(self):
  f=Fake();a=self.api(f);a.read_note(N)
  with self.assertRaisesRegex(m.Failure,'write_not_authorized'):a.archive_block(B)
  self.assertFalse(any(method=='PATCH' for method,_,_ in f.calls))
 def test_append_wire_and_single_attempt(self):
  f=Fake();body={'children':m.wire_blocks(semantic()),'after':B};key=m.fingerprint('PATCH','/blocks/'+P+'/children',body);a=self.api(f,approved_writes=[key]);a.read_note(N)
  self.assertEqual(a.append_blocks(P,semantic(),B),['5'*32])
  with self.assertRaisesRegex(m.Failure,'already_attempted'):a.append_blocks(P,semantic(),B)
 def test_none_is_not_tail_append(self):
  a=self.api(Fake());a.read_note(N)
  with self.assertRaisesRegex(m.Failure,'unsupported_prepend'):a.append_blocks(P,semantic(),None)
 def test_create(self):
  f=Fake();f.missing=True;body={'parent':{'type':'data_source_id','data_source_id':D},'properties':page()['properties'],'children':m.wire_blocks(semantic())};a=self.api(f,approved_writes=[m.fingerprint('POST','/pages',body)])
  self.assertEqual(a.create_page(D,N,page()['properties'],semantic()),P)
 def test_image_blocker(self):
  with self.assertRaisesRegex(m.Failure,'asset_write_contract_missing'):m.wire_blocks([{'type':'image','payload':{},'children':[]}])
 def test_read_retry_and_headers(self):
  err=urllib.error.HTTPError('hidden',429,'hidden',{'Retry-After':'1'},io.BytesIO(b'{}'));o=Opener([err,b'{"object":"page"}']);sleep=[];t=m.Transport('synthetic',opener=o,sleep=sleep.append,clock=lambda:0)
  self.assertEqual(t('GET','/pages/'+P)['object'],'page');self.assertEqual(len(o.calls),2);self.assertIn(1,sleep);self.assertEqual(o.calls[0].get_header('Notion-version'),m.VERSION)
 def test_permission_no_retry(self):
  o=Opener([urllib.error.HTTPError('hidden',403,'hidden',{},io.BytesIO(b'{}'))]);t=m.Transport('synthetic',opener=o,sleep=lambda _:None)
  with self.assertRaisesRegex(m.Failure,'permission_denied'):t('GET','/pages/'+P)
  self.assertEqual(len(o.calls),1)
 def test_write_timeout_never_retry(self):
  path='/blocks/'+B;body={'archived':True};key=m.fingerprint('PATCH',path,body);o=Opener([TimeoutError('secret')]);t=m.Transport('synthetic',opener=o,approved_writes=[key])
  with self.assertRaisesRegex(m.Failure,'write_uncertain'):t('PATCH',path,body)
  with self.assertRaisesRegex(m.Failure,'already_attempted'):t('PATCH',path,body)
  self.assertEqual(len(o.calls),1)
 def test_strict_json(self):
  o=Opener([b'{"id":1,"id":2}']);t=m.Transport('synthetic',opener=o)
  with self.assertRaisesRegex(m.Failure,'malformed_response'):t('GET','/pages/'+P)
 def test_foreign_route(self):
  o=Opener([]);t=m.Transport('synthetic',opener=o)
  with self.assertRaisesRegex(m.Failure,'invalid_route'):t('GET','https://evil.example')
  self.assertEqual(o.calls,[])
 def test_code_wire_drops_only_default_normalizer_color(self):
  row={'type':'code','payload':{'rich_text':[],'caption':[],'language':'python','color':'default'},'children':[]}
  out=m.wire_blocks([row])[0]['code'];self.assertEqual(set(out),{'rich_text','caption','language'})
  row['payload']['color']='red'
  with self.assertRaisesRegex(m.Failure,'unsupported_code_color'):m.wire_blocks([row])
 def test_native_table_wire(self):
  row={'type':'table','payload':{'table_width':1,'has_column_header':True,'has_row_header':False},'children':[{'type':'table_row','payload':{'cells':[[]]},'children':[]}]}
  out=m.wire_blocks([row])[0]['table'];self.assertEqual(out['children'][0],{'object':'block','type':'table_row','table_row':{'cells':[[]]}})
  row['children'][0]['payload']['cells']=[]
  with self.assertRaisesRegex(m.Failure,'invalid_table'):m.wire_blocks([row])
 def test_nested_list_wire(self):
  row={'type':'bulleted_list_item','payload':{'rich_text':[]},'children':semantic()}
  out=m.wire_blocks([row])[0]['bulleted_list_item'];self.assertIn('paragraph',out['children'][0]);self.assertNotIn('payload',out['children'][0])
if __name__=='__main__':unittest.main()

class SplitIntegration(unittest.TestCase):
 def test_executor_to_wire_long_runs(self):
  import get_notion_executor as executor
  run={'type':'text','text':{'content':'x'*4200,'link':{'url':'https://example.invalid'}},'annotations':{'bold':True}}
  for kind,payload,key in [('paragraph',{'rich_text':[run]},'rich_text'),('code',{'rich_text':[run],'caption':[run],'language':'plain text'},'caption'),('table_row',{'cells':[[run]]},'cells')]:
   rows=[{'type':kind,'payload':payload,'children':[]}]
   wire=m.wire_blocks(executor.request_blocks(rows))[0][kind]
   runs=wire[key][0] if key=='cells' else wire[key]
   self.assertEqual([len(x['text']['content']) for x in runs],[2000,2000,200])
   self.assertEqual(''.join(x['text']['content'] for x in runs),'x'*4200)
   self.assertTrue(all(x['annotations']['bold'] and x['text']['link']==run['text']['link'] for x in runs))
 def test_normalized_split_array_budget(self):
  rows=semantic();rows[0]['payload']['rich_text'][0]['text']['content']='x'*200001
  with self.assertRaisesRegex(m.Failure,'request_budget'):m.wire_blocks(rows)
