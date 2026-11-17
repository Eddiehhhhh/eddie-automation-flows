import base64,json,os,tempfile,unittest
from core import *

def note(n='1',text='hello',updated='2026-11-17'):
 return {'note_id':n,'title':'test','content':text,'updated_at':updated,'attachments':[]}
class GitFake:
 def __init__(self):self.file=None;self.private=True;self.writes=0;self.uncertain=False;self.conflict=False
 def request(self,m,p,body=None):
  if '/contents/' not in p:return {'private':self.private}
  if m=='GET':return self.file
  self.writes+=1
  if self.conflict:raise Hold('http_409')
  if self.file and body.get('sha')!=self.file['sha']:raise Hold('http_409')
  self.file={'sha':str(self.writes),'content':body['content']}
  if self.uncertain:self.uncertain=False;raise Hold('transport_uncertain')
  return {}
class Sink:
 def __init__(self,fail=False):self.fail=fail;self.calls=0
 def sync(self,s,r=None):
  self.calls+=1
  if self.fail:raise Hold('failure')
  return {'version':s['version']}
class Tests(unittest.TestCase):
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
  with self.assertRaises(Hold):sink.sync(s)
  sink.sync(s);self.assertEqual(f.writes,1)
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
