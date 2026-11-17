import base64,copy,json,os,tempfile,unittest
from core import *

def note(n='1',text='hello',updated='2026-11-17'):
 return {'note_id':n,'title':'test','content':text,'updated_at':updated,'attachments':[]}
class GitFake:
 def __init__(self):self.file=None;self.private=True;self.writes=0;self.uncertain=False;self.conflict=False;self.commits={};self.calls=[];self.stale_after_write=False;self.stale=None
 def request(self,m,p,body=None):
  self.calls.append((m,p))
  if '/contents/' not in p:return {'private':self.private}
  if m=='GET':
   ref=urllib.parse.parse_qs(urllib.parse.urlsplit(p).query).get('ref',[''])[0]
   if ref in self.commits:return copy.deepcopy(self.commits[ref])
   return copy.deepcopy(self.stale if self.stale_after_write and self.writes else self.file)
  self.writes+=1
  if self.conflict:raise Hold('http_409')
  if self.file and body.get('sha')!=self.file['sha']:raise Hold('http_409')
  self.stale=copy.deepcopy(self.file)
  raw=base64.b64decode(body['content']);sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
  self.file={'sha':sha,'content':body['content']}
  commit_body=('tree '+sha+'\nauthor Test <test@example.invalid> 0 +0000\ncommitter Test <test@example.invalid> 0 +0000\n\nWrite '+str(self.writes)+'\n').encode()
  commit=hashlib.sha1(b'commit '+str(len(commit_body)).encode()+b'\0'+commit_body).hexdigest();self.commits[commit]=copy.deepcopy(self.file)
  if self.uncertain:self.uncertain=False;raise Hold('transport_uncertain')
  return {'commit':{'sha':commit},'content':{'sha':sha}}
class Sink:
 def __init__(self,fail=False):self.fail=fail;self.calls=0
 def sync(self,s,r=None):
  self.calls+=1
  if self.fail:raise Hold('failure')
  return {'version':s['version']}
class Tests(unittest.TestCase):
 def test_verified_signature_only_change_keeps_version_and_original(self):
  import copy
  n=note();n['attachments']=[{'type':'image','url':'https://mediacdn.umiwi.com/a.png?Expires=1&OSSAccessKeyId=a&Signature=x&size=%2f#fragment'}]
  newer=copy.deepcopy(n);newer['attachments'][0]['url']=newer['attachments'][0]['url'].replace('Expires=1','Expires=2').replace('Signature=x','Signature=y')
  original=canonical(newer);self.assertEqual(snapshot(n)['version'],snapshot(newer)['version']);self.assertEqual(canonical(newer),original)
  store=Store(':memory:');store.accept([snapshot(n)]);store.accept([snapshot(newer)]);self.assertEqual(store.pending_count(),0)
  for change in ('path','query','content','unknown','host','link','partial','case'):
   a=copy.deepcopy(n);b=copy.deepcopy(newer)
   if change=='path':b['attachments'][0]['url']=b['attachments'][0]['url'].replace('/a.png','/b.png')
   elif change=='query':b['attachments'][0]['url']=b['attachments'][0]['url'].replace('%2f','%2F')
   elif change=='content':b['content']='edited'
   elif change=='unknown':b['future_field']={'value':True}
   elif change=='host':
    for v in (a,b):v['attachments'][0]['url']=v['attachments'][0]['url'].replace('mediacdn.umiwi.com','get-notes.umiwi.com')
   elif change=='link':
    for v in (a,b):v['attachments'][0]['type']='link'
   elif change=='partial':
    for v in (a,b):v['attachments'][0]['url']=v['attachments'][0]['url'].replace('&OSSAccessKeyId=a','')
   elif change=='case':
    for v in (a,b):v['attachments'][0]['url']=v['attachments'][0]['url'].replace('Expires','expires')
   with self.subTest(change=change):
    self.assertNotEqual(snapshot(a)['version'],snapshot(b)['version'])
    st=Store(':memory:');st.accept([snapshot(a)]);st.accept([snapshot(b)]);self.assertEqual(st.pending_count(),1)
 def test_same_journal_and_hold_do_not_rewrite(self):
  checkpoints=[];store=Store(':memory:',checkpoint=lambda db:checkpoints.append(db.total_changes));store.set('a',{'x':1,'y':2});count=len(checkpoints);changes=store.db.total_changes
  store.set('a',{'y':2,'x':1});self.assertEqual((len(checkpoints),store.db.total_changes),(count,changes));store._held('notion','1','v',Hold('transport_uncertain'));changes=store.db.total_changes
  store._held('notion','1','v',Hold('transport_uncertain'));self.assertEqual(store.db.total_changes,changes)
 def test_bounded_drain_fair_fifo_and_restart(self):
  class Selective(Sink):
   def __init__(self):super().__init__();self.seen=[]
   def sync(self,s,r=None):
    self.seen.append((s['id'],s['version']))
    if s['id']!='11':raise Hold('transport_uncertain')
    return super().sync(s,r)
  with tempfile.TemporaryDirectory() as d:
   path=d+'/s';store=Store(path);store.accept([]);store.accept([snapshot(note(str(i))) for i in range(1,12)]);first=snapshot(note('11'));second=snapshot(note('11',text='second'));store.accept([second])
   sink=Selective();store.drain({'github':sink,'notion':Sink()},limit=10);self.assertEqual(len(sink.seen),10);self.assertNotIn('11',[x[0] for x in sink.seen]);store.db.close()
   store=Store(path);sink=Selective();store.drain({'github':sink,'notion':Sink()},limit=1);self.assertEqual(sink.seen,[('11',first['version'])]);self.assertEqual(store.pending_count(),11)
   for _ in range(12):store.drain({'github':sink,'notion':Sink()},limit=1)
   self.assertEqual([v for nid,v in sink.seen if nid=='11'],[first['version'],second['version']]);self.assertEqual(store.pending_count(),10)
 def test_paused_pending_never_reaches_either_sink(self):
  for limit in (None,1,10):
   with self.subTest(limit=limit):
    store=Store(':memory:');store.accept([]);first=snapshot(note('1'));store.accept([first,snapshot(note('2'))]);store.accept([snapshot(note('1',text='new'))])
    a=Sink();b=Sink();result=store.drain({'github':a,'notion':b},limit=limit,paused_ids={'1'})
    self.assertEqual((a.calls,b.calls),(1,1));self.assertEqual(result['pending'],2)
    self.assertIsNone(store.receipt('github','1'));self.assertEqual(store.db.execute('SELECT count(*) FROM pending WHERE id=?',('1',)).fetchone()[0],2)
    store.drain({'github':a,'notion':b},limit=10,paused_ids={'1'});self.assertEqual((a.calls,b.calls),(1,1))
    store.drain({'github':a,'notion':b},limit=10);self.assertEqual(store.pending_count(),0)
 def test_baseline_new_modified_independent(self):
  store=Store(':memory:');a=Sink();b=Sink(True)
  store.accept([snapshot(note())]);self.assertEqual(store.drain({'github':a,'notion':b})['github']['verified'],0)
  store.accept([snapshot(note(text='edit')),snapshot(note('2'))]);store.drain({'github':a,'notion':b})
  self.assertEqual(store.db.execute('select count(*) from pending').fetchone()[0],2)
  b.fail=False;store.drain({'github':a,'notion':b});self.assertEqual(store.db.execute('select count(*) from pending').fetchone()[0],0)
 def test_persistent_recovery(self):
  with tempfile.TemporaryDirectory() as d:
   p=d+'/state.sqlite';store=Store(p);store.accept([]);store.accept([snapshot(note())]);store.db.close()
   store=Store(p);self.assertEqual(store.drain({'github':Sink(),'notion':Sink()})['notion']['verified'],1)
 def test_link_pointer_does_not_require_binary_archival(self):
  n=note();n['note_type']='link';n['attachments']=[{'type':'link','url':'https://article.example.test/post'}]
  self.assertFalse(requires_binary_assets(snapshot(n)))

 def test_audio_type_without_url_never_completes_as_text(self):
  for kind in ('audio','local_audio'):
   store=Store(':memory:');store.accept([]);n=note();n['note_type']=kind;n['audio']={'original':'synthetic transcript'}
   store.accept([snapshot(n)]);result=store.drain({'github':Sink(),'notion':Sink()})
   self.assertEqual(result['pending'],1)
   self.assertEqual(result['github']['held'],1)

 def test_asset_note_never_falsely_completed(self):
  store=Store(':memory:');store.accept([]);n=note();n['attachments']=[{'url':'https://example.test/a'}];store.accept([snapshot(n)])
  result=store.drain({'github':Sink(),'notion':Sink()})
  self.assertEqual(result['github']['held'],1);self.assertEqual(result['github']['verified'],0)
  self.assertEqual(store.db.execute('select count(*) from pending').fetchone()[0],1)
 def test_target_binding(self):
  store=Store(':memory:');store.bind({'target':'a'});store.bind({'target':'a'})
  with self.assertRaises(Hold):store.bind({'target':'b'})
 def test_old_schema_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=d+'/s';db=sqlite3.connect(p);db.execute('create table pending(id text primary key,snapshot text)');db.close()
   with self.assertRaises(Hold):Store(p)
 def test_fifo_preserves_uncertain_version(self):
  store=Store(':memory:');store.accept([])
  s1=snapshot(note(text='one'));s2=snapshot(note(text='two'))
  store.accept([s1]);a=Sink();b=Sink(True);store.drain({'github':a,'notion':b});store.accept([s2])
  self.assertEqual(store.db.execute('select count(*) from pending').fetchone()[0],2)
  store.drain({'github':a,'notion':b});self.assertEqual(store.receipt('github','1')['version'],s1['version'])
  b.fail=False;store.drain({'github':a,'notion':b});self.assertEqual(store.receipt('notion','1')['version'],s2['version'])
 def test_empty_sinks_cannot_clear(self):
  store=Store(':memory:');store.accept([]);store.accept([snapshot(note())])
  with self.assertRaises(Hold):store.drain({})
  self.assertEqual(store.db.execute('select count(*) from pending').fetchone()[0],1)
 def test_recording_missing_transcript_held(self):
  n=note();n['note_type']='recorder_audio'
  with self.assertRaises(Hold):snapshot(n)
 def test_blob_fallback(self):
  class Blob(GitFake):
   def request(self,m,p,body=None):
    if '/git/blobs/' in p:return {'sha':'abc','encoding':'base64','content':base64.b64encode(b'full').decode()}
    return super().request(m,p,body)
  self.assertEqual(GitHubSink(Blob(),'owner/private','main','test').content({'sha':'abc','encoding':'none'}),b'full')
 def test_durable_notion_journal(self):
  with tempfile.TemporaryDirectory() as d:
   p=d+'/state.sqlite';store=Store(p);store.set('notion:test',{'phase':'page_create_intent','version':'v'});store.db.close()
   self.assertEqual(Store(p).get('notion:test')['phase'],'page_create_intent')
 def test_duplicate_scan_does_not_advance(self):
  store=Store(':memory:')
  with self.assertRaises(Hold):store.accept([snapshot(note()),snapshot(note())])
  self.assertEqual(store.db.execute('select count(*) from meta').fetchone()[0],0)
 def test_disappearance_never_deletes(self):
  store=Store(':memory:');store.accept([snapshot(note())]);store.accept([])
  self.assertEqual(store.db.execute('select count(*) from inventory').fetchone()[0],1)
 def test_github_create_replay_edit_conflict(self):
  f=GitFake();sink=GitHubSink(f,'owner/private','main','isolated');s=snapshot(note())
  r=sink.sync(s);sink.sync(s,r);self.assertEqual(f.writes,1)
  r=sink.sync(snapshot(note(text='new')),r);self.assertEqual(f.writes,2)
  f.file['sha']='human-edit'
  with self.assertRaises(Hold):sink.sync(snapshot(note(text='third')),r)
 def test_github_uncertain_create(self):
  f=GitFake();f.uncertain=True;sink=GitHubSink(f,'owner/private','main','isolated');s=snapshot(note())
  self.assertEqual(sink.sync(s)['version'],s['version'])
  sink.sync(s);self.assertEqual(f.writes,1)
 def test_github_success_reads_immutable_commit_when_branch_is_stale(self):
  f=GitFake();f.stale_after_write=True;sink=GitHubSink(f,'owner/private','main','isolated')
  result=sink.sync(snapshot(note()))
  self.assertEqual(result['sha'],f.file['sha']);self.assertEqual(f.writes,1)
  self.assertTrue(any('ref='+commit in path for method,path in f.calls for commit in f.commits if method=='GET'))
 def test_github_update_reads_new_commit_even_if_branch_still_has_previous_bytes(self):
  f=GitFake();sink=GitHubSink(f,'owner/private','main','test');first=sink.sync(snapshot(note()));original=f.request
  def request(method,path,body=None):
   result=original(method,path,body)
   if method=='PUT':f.stale_after_write=True
   return result
  f.request=request;updated=snapshot(note(text='changed complete body'))
  second=sink.sync(updated,first)
  self.assertEqual(second['version'],updated['version']);self.assertNotEqual(second['sha'],first['sha']);self.assertEqual(f.writes,2)
 def test_github_immutable_large_object_readback_downloads_exact_blob(self):
  f=GitFake();original=f.request;blob_reads=[]
  def request(method,path,body=None):
   if '/git/blobs/' in path:
    blob_reads.append(path);return {**copy.deepcopy(f.file),'encoding':'base64'}
   result=original(method,path,body)
   if method=='GET' and any('ref='+commit in path for commit in f.commits):return {'sha':result['sha'],'encoding':'none','content':''}
   return result
  f.request=request;s=snapshot(note(text='full'*400000))
  self.assertEqual(GitHubSink(f,'owner/private','main','test').sync(s)['version'],s['version'])
  self.assertEqual(len(blob_reads),1);self.assertEqual(f.writes,1)
 def test_github_response_identity_is_required_before_readback(self):
  for response in ({},{'commit':{'sha':'bad'},'content':{'sha':'0'*40}},{'commit':[],'content':None},'wrong_blob'):
   with self.subTest(response=response):
    f=GitFake();original=f.request
    def request(method,path,body=None):
     result=original(method,path,body)
     if method=='PUT':
      if response=='wrong_blob':result['content']['sha']='0'*40;return result
      return response
     return result
    f.request=request
    with self.assertRaisesRegex(Hold,'github_write_response_invalid'):GitHubSink(f,'owner/private','main','test').sync(snapshot(note()))
    self.assertEqual(f.writes,1)
 def test_github_unknown_put_only_one_branch_read_and_no_second_put(self):
  f=GitFake();f.stale_after_write=True;f.uncertain=True
  with self.assertRaisesRegex(Hold,'github_write_requires_reconcile'):GitHubSink(f,'owner/private','main','test').sync(snapshot(note()))
  self.assertEqual(f.writes,1)
  reads=[path for method,path in f.calls if method=='GET' and '/contents/' in path]
  self.assertEqual(len(reads),2);self.assertTrue(all('ref=main' in path for path in reads))
 def test_github_immutable_readback_compares_full_bytes_and_blob_identity(self):
  for tamper in ('body','sha'):
   with self.subTest(tamper=tamper):
    f=GitFake();original=f.request
    def request(method,path,body=None):
     result=original(method,path,body)
     if method=='GET' and any('ref='+commit in path for commit in f.commits):
      if tamper=='body':result['content']=base64.b64encode(b'tampered').decode()
      else:result['sha']='f'*40
     return result
    f.request=request
    with self.assertRaisesRegex(Hold,'github_readback_mismatch'):GitHubSink(f,'owner/private','main','test').sync(snapshot(note()))
    self.assertEqual(f.writes,1)
 def test_public_blocked(self):
  f=GitFake();f.private=False
  with self.assertRaises(Hold):GitHubSink(f,'owner/repo','main','x').sync(snapshot(note()))
  self.assertEqual(f.writes,0)
 def test_sha_conflict_retains_receipt(self):
  f=GitFake();sink=GitHubSink(f,'owner/repo','main','x');s=snapshot(note());r=sink.sync(s);f.conflict=True
  with self.assertRaises(Hold):sink.sync(snapshot(note(text='edit')),r)
  self.assertEqual(base64.b64decode(f.file['content']),canonical({'owner':'get-direct-v1/x',**s}).encode())
 def test_whole_content_and_attachment_preserved(self):
  from render import render_text
  n=note(text='长'*210000);n['attachments']=[{'url':'https://example.test/asset?signature=keep'}];n['audio']={'original':'转录'*30000};s=snapshot(n)
  text=render_text(s);self.assertIn(n['content'],text);self.assertIn(n['audio']['original'],text);self.assertIn('?signature=keep',text)
 def test_get_documented_contract(self):
  class API:
   def __init__(self):self.paths=[]
   def request(self,m,p,b=None):
    self.paths.append(p)
    if '/detail?' in p:
     nid=urllib.parse.parse_qs(urllib.parse.urlsplit(p).query)['id'][0]
     return {'success':True,'data':{'note':note(nid)}}
    second='cursor=' in p
    return {'success':True,'data':{'notes':[note('2' if second else '1')],'has_more':not second,'cursor':'next'}}
  api=API();s=GetSource(api).scan();self.assertEqual(len(s),2);self.assertIn('image_quality=original',api.paths[1])
 def test_known_missing_transcript_can_be_durably_held_without_losing_other_ids(self):
  class API:
   def request(self,m,p,b=None):
    bad=note('1');bad['note_type']='recorder_audio';bad['audio']={'sentences':[]}
    good=note('2')
    if '/detail?' in p:return {'success':True,'data':{'note':bad if 'id=1' in p else good}}
    return {'success':True,'data':{'notes':[bad,good],'has_more':False}}
  held=[];result=GetSource(API()).scan(on_note_hold=held.append)
  self.assertEqual([s['id'] for s in result],['2'])
  self.assertEqual(held[0]['id'],'1');self.assertEqual(held[0]['reason'],'recording_transcript_unavailable')
  self.assertIn('note',held[0])
  with self.assertRaisesRegex(Hold,'transcript'):GetSource(API()).scan()

 def test_selected_history_still_reads_full_list_but_only_chosen_details(self):
  class API:
   def __init__(self):self.details=0;self.lists=0
   def request(self,m,p,b=None):
    if '/detail?' in p:self.details+=1;return {'success':True,'data':{'note':note('1')}}
    self.lists+=1;second='cursor=' in p
    return {'success':True,'data':{'notes':[note('2' if second else '1')],'has_more':not second,'cursor':'next'}}
  api=API();source=GetSource(api);items=source.scan(select_note=lambda n:identity(n)=='1')
  self.assertEqual([s['id'] for s in items],['1']);self.assertEqual((api.lists,api.details),(2,1))
  self.assertEqual(source.observed_count,2)

 def test_incomplete_scan_no_accept(self):
  class API:
   def request(self,m,p,b=None):return {'success':True,'data':{'notes':[],'has_more':True,'cursor':'cycle'}}
  with self.assertRaises(Hold):GetSource(API()).scan()
 def test_changed_during_scan(self):
  class API:
   def request(self,m,p,b=None):
    return {'success':True,'data':{'note':note(updated='new')}} if '/detail' in p else {'success':True,'data':{'notes':[note()],'has_more':False}}
  with self.assertRaises(Hold):GetSource(API()).scan()
 def test_host_and_redirect(self):
  with self.assertRaises(Hold):HTTP('https://evil.test',{})
  with self.assertRaises(Hold):NoRedirect().redirect_request(None,None,None,None,None,None)
if __name__=='__main__':unittest.main()
