"""Real receipt/partial clone/RemoteJournal with a local fake Notion endpoint."""
import unittest,json,contextlib
from copy import deepcopy
from unittest.mock import patch
import get_notion_runtime as r
import get_notion_png_evidence as ev
import get_notion_png_loader as loader
import get_notion_outbox as o
import get_notion_executor as e
import test_notion_outbox as source
import test_notion_remote_journal as remote
import test_notion_png_loader as lf
import test_notion_executor as ef
from test_notion_png_evidence import png
from test_png_pipeline import Transport
from test_executor_live_adapter import DB
class Tests(unittest.TestCase):
 def setUp(self):
  self.src=source.Tests();self.src.setUp();self.addCleanup(self.src.doCleanups);s=self.src
  original_write=s.write
  s.write=lambda path,data:original_write(r.entry.r.GET+'/'+r.entry.a.STATE if path=='Raw/03 Get/03 State/legacy.json' else path,data)
  self.ids=[source.N(i) for i in range(3)];self.images={i:png(k+2,2) for k,i in enumerate(self.ids)}
  self.urls={i:'https://get-notes.umiwi.com/synthetic-'+str(k)+'.png?fixture=x' for k,i in enumerate(self.ids)}
  s.publication({i:'![Caption]('+self.urls[i]+')\n\n'+('---\n\n'*110 if k==2 else 'Ordinary body') for k,i in enumerate(self.ids)});state=s.ingest();desc=s.pub().descriptor
  records={}
  for i in self.ids:
   data=self.images[i];sha=e.digest(data);w,h=ev.png_shape(data);path=ev.PREFIX+sha+'.png';s.write(path,data)
   records[i]={'source':desc['records'][i],'reference_sha256':e.digest(self.urls[i].encode()),'asset':{'path':path,'blob':e.p.git_blob(data),'sha256':sha,'bytes':len(data),'mime':'image/png','width':w,'height':h}}
  manifest={'contract':ev.CONTRACT,'repository':e.p.REPOSITORY,'publication_commit':desc['commit'],'records':records};raw=ev.encoded(manifest);s.write(ev.MANIFEST_PATH,raw);asset_commit=s.commit()
  self.anchor={'contract':ev.CONTRACT,'repository':e.p.REPOSITORY,'publication_commit':desc['commit'],'asset_commit':asset_commit,'manifest_path':ev.MANIFEST_PATH,'manifest_blob':e.p.git_blob(raw),'manifest_sha256':e.digest(raw)}
  s.write(o.STATE_PATH,o.encoded(state));s.commit();(s.root/'remote-fixture').mkdir();self.rf=remote.Fixture(s.root/'remote-fixture',seed=s.root)
  self.lr=lf.Remote(self.rf.bare)
  self.lr.check_source_budget=lambda commit:{path:(*row,int(remote.run(self.rf.bare,'cat-file','-s',row[2]))) for path,row in self.lr.reader.tree(commit).items() if path.startswith(r.r.GET+'/')}
  self.local=r.r.LocalGit(self.rf.root);self.rf.load(self.rf.head())
  blob=remote.run(self.rf.bare,'rev-parse',self.rf.initial+':'+o.STATE_PATH).decode().strip()
  self.outer=self.rf.store({'commit':self.rf.initial,'state_blob':blob,'state_sha256':o.digest(state)})
  self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
  pins={r.png_upload.sha(i.encode()):{'md_sha256':v['source']['sha256'],'sha256':v['asset']['sha256'],'bytes':v['asset']['bytes'],'git_blob':v['asset']['blob'],'reference_sha256':v['reference_sha256']} for i,v in records.items()}
  for obj,key,val in ((r,'PNG_ASSET_ANCHOR',self.anchor),(r,'PNG_ASSET_ANCHOR_SHA256',e.digest(self.anchor)),(r.png_upload,'PINS',pins),(r.png_upload,'DESTINATION_PIN',r.png_upload.sha(ef.DS.encode())),(r.transport.native,'DATA_SOURCE_PIN',r.transport.native.sha(ef.DS)),(r.transport.native,'DATABASE_PIN',r.transport.native.sha(DB)),(r.entry,'BOOTSTRAP',{'commit':s.origin['commit']})):
   self.stack.enter_context(patch.object(obj,key,val))
  self.stack.enter_context(patch.object(e.content_scope,'reason',return_value=None));self.stack.enter_context(patch.object(r,'initial_done',return_value=True));self.stack.enter_context(patch.object(r,'schema',return_value='Title'))
  original_collect=r.transport.native.collect
  self.stack.enter_context(patch.object(r.transport.native,'collect',side_effect=lambda request,**kw:original_collect(request,**kw) if kw else {'snapshots':{'x':{'parent_data_source':ef.DS}}}))
  self.api=ef.API(o.EngineStore(self.outer));self.request=Transport(self.api,self.images[self.ids[0]])
  self.writers={'fresh_readback_verified':True,'writers':{n:{'disabled':True,'active_runs':0} for n in ('getnote-to-notion','notion-to-getnote')}}
  self.uploads={};owner=self
  class Reply:
   status=200
   def __init__(self,row):self.row=row
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self,n):return json.dumps(self.row).encode()[:n]
  class Opener:
   def open(self,request,timeout):
    path=request.full_url.split('/v1',1)[1]
    if path=='/file_uploads':
     body=json.loads(request.data);uid='%032x'%(len(owner.uploads)+100);owner.uploads[uid]={'object':'file_upload','id':uid,'status':'pending','filename':body['filename'],'content_type':'image/png','expiry_time':'2099-01-01T00:00:00Z'};return Reply(owner.uploads[uid])
    uid=path.split('/')[2];row=owner.uploads[uid]
    if path.endswith('/send'):row.update(status='uploaded',content_length=len(request.data.split(b'\r\n\r\n',1)[1].rsplit(b'\r\n--',1)[0]))
    return Reply(row)
  self.request.opener=Opener();self.request.sleep=lambda _:None
 def preload(self):
  state,sha=self.outer.read();first=state['pending_by_id'][state['rotation_order'][0]]
  r.source_loader.load_queue_publication(self.local,self.lr,None,self.src.root,{k:first[k] for k in ('commit','parent','receipt_sha256')},state['policy_sha256'])
  pub=o.QueuePublication(self.local.vault,state,sha)
  return loader.load(self.local,self.lr,pub.descriptor,self.anchor,e.digest(self.anchor),state['policy_sha256'])
 def test_preload_before_real_journal_cas_then_prepare(self):
  loaded=self.preload();state,sha=self.outer.read()
  # A real outer CAS after preload advances remote main while fixed local
  # source refs remain unchanged. This is the ordering required by migration.
  changed=deepcopy(state);changed['rotation_order']=list(reversed(changed['rotation_order']))
  o.confirmed_compare_and_set(self.outer,sha,changed)
  self.assertNotEqual(self.rf.head(),loaded.snapshot_head);loaded.before_write()
  count=len(self.rf.calls)
  out=r.dispatch(self.local,self.lr,None,self.src.root,self.outer,self.request,tip=self.src.last,policy_hash=source.P,apply=False,writers_guard=lambda:deepcopy(self.writers),loaded_assets=loaded)
  self.assertEqual(out['status'],'png_prepared',out);self.assertEqual(out['png_preparation']['upload_candidates'],2,out);self.assertEqual(out['png_preparation']['reason_counts'],{'request_budget':1})
  self.assertEqual(len(self.rf.calls),count);self.assertFalse(self.uploads);self.assertFalse(self.request.attempted)
 def test_two_creates_then_fresh_clone_capacity_only_zero_post(self):
  loaded=self.preload();original=self.request.block
  bysha={e.digest(data):data for data in self.images.values()}
  def block(row,page):
   value=original(row,page)
   if row['type']=='image':value['image']['file']['url']='https://prod-files-secure.s3.us-west-2.amazonaws.com/'+self.uploads[row['payload']['file_upload']['id']]['filename'][4:-4]
   return value
  self.request.block=block
  with patch.object(r.png_image,'AssetGet',return_value=lambda url:bysha[url.rsplit('/',1)[1]]):
   result=r.dispatch(self.local,self.lr,None,self.src.root,self.outer,self.request,tip=self.src.last,policy_hash=source.P,apply=True,writers_guard=lambda:deepcopy(self.writers),loaded_assets=loaded)
  self.assertEqual(result['execution']['created'],2,result);self.assertEqual(len(self.uploads),2)
  state,sha=self.outer.read();self.assertEqual(len(state['png_assets']['attachments']),2);self.assertEqual(len(state['pending_by_id']),1)
  posts=len(self.request.attempted);cas=len(self.rf.calls);anchor=deepcopy(self.outer.anchor)
  self.rf.root=self.rf.clone('fresh-capacity');self.local=r.r.LocalGit(self.rf.root);self.rf.load(self.rf.head());self.outer=self.rf.store(anchor);self.api.store=o.EngineStore(self.outer)
  loaded=self.preload()
  result=r.dispatch(self.local,self.lr,None,self.src.root,self.outer,self.request,tip=self.src.last,policy_hash=source.P,apply=True,writers_guard=lambda:deepcopy(self.writers),loaded_assets=loaded)
  safe=r.safe_result(result)
  self.assertEqual(safe['png']['reason_counts'],{'request_budget':1},safe);self.assertEqual(len(self.request.attempted),posts);self.assertEqual(r.exit_status(safe),0,safe)
 def test_whole_run_three_policy_bridge_real_migration_cas(self):
  import io,base64,tempfile
  import test_notion_journal_runtime as jf
  import get_recurring_policy_recovery as recovery
  old_policy='b'*64;new_policy='c'*64
  old=deepcopy(self.src.store.value);old['policy_sha256']=old_policy
  self.src.write(o.STATE_PATH,o.encoded(old));self.src.commit()
  folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
  class Remote(jf.Fixture):
   def ref(self,name):return super().ref(name) if name==r.r.MAIN_REF else r.m.PROTECTED[name]
   def tree_entries(self,tree,recursive=False):
    rows=[]
    for raw in remote.run(self.bare,'ls-tree',*(['-r'] if recursive else []),'-z',tree).split(b'\0'):
     if not raw:continue
     meta,path=raw.split(b'\t');mode,kind,oid=meta.decode().split();row={'path':path.decode(),'mode':mode,'type':kind,'sha':oid}
     if kind=='blob':row['size']=int(remote.run(self.bare,'cat-file','-s',oid))
     rows.append(row)
    return rows
   source_get_tree=r.r.GitHubRemote.source_get_tree
   check_source_budget=r.r.GitHubRemote.check_source_budget
   def request(self,path,**kwargs):
    oid=path.removeprefix('/git/blobs/');data=remote.run(self.bare,'cat-file','blob',oid)
    return {'sha':oid,'size':len(data),'encoding':'base64','content':base64.b64encode(data).decode()}
  rf=Remote(folder.name,seed=self.src.root)
  state_blob=remote.run(rf.bare,'rev-parse',rf.initial+':'+o.STATE_PATH).decode().strip()
  anchor={'commit':rf.initial,'state_blob':state_blob,'state_sha256':o.digest(old)}
  class View:
   def read(self):
    value=json.loads(remote.run(rf.bare,'show',rf.head()+':'+o.STATE_PATH));return value['engine_state'],e.digest(value['engine_state'])
  self.api.store=View()
  def checkout(destination,commit,token):
   rf.root=rf.clone('whole-run')
   class Local(r.r.LocalGit):
    def run(self,*args,**kw):
     if args==('config','--local','--get-all','remote.origin.url'):return (r.r.REMOTE_URL+'\n').encode()
     return super().run(*args,**kw)
   return Local(rf.root)
  policy={'code_sha':'a'*40,'rules_sha256':'b'*64,'get_policy_sha256':new_policy}
  context={'run_id':1,'run_attempt':1,'code_sha':'a'*40,'controller_sha':'c'*40}
  marker={'receipt_commit':self.src.last['commit'],'receipt_sha256':self.src.last['receipt_sha256'],'receipt_sequence':1}
  original=self.request.block;bysha={e.digest(data):data for data in self.images.values()}
  def block(row,page):
   value=original(row,page)
   if row['type']=='image':value['image']['file']['url']='https://prod-files-secure.s3.us-west-2.amazonaws.com/'+self.uploads[row['payload']['file_upload']['id']]['filename'][4:-4]
   return value
  self.request.block=block
  with contextlib.ExitStack() as stack:
   for obj,key,value in ((r,'PRODUCTION_ACTIVATED',True),(r.recurring,'PRODUCTION_ACTIVATED',True),(recovery,'ORIGINAL_RECEIPT_COMMIT',self.src.last['commit']),(recovery,'ORIGINAL_RECEIPT_SHA256',self.src.last['receipt_sha256'])):stack.enter_context(patch.object(obj,key,value))
   stack.enter_context(patch.object(recovery,'old_public_policy',return_value={'get_policy_sha256':source.P}));stack.enter_context(patch.object(recovery,'current_get_policy',return_value=new_policy))
   # Replace only external trust anchors with exact synthetic fixture anchors;
   # actual transition, receipt compatibility predicate, loader, queue, dispatch,
   # RemoteJournal CAS and native readback all run unmodified.
   stack.enter_context(patch.object(r.png_recovery,'validate_state',side_effect=lambda value,sha:sha==o.digest(old) and e.same(value,old)))
   stack.enter_context(patch.object(r.recurring,'validate_policy',side_effect=lambda p,*args:p));stack.enter_context(patch.object(r.provenance,'verify_execution',return_value=None));stack.enter_context(patch.object(r.provenance,'parse_record',return_value=marker))
   stack.enter_context(patch.object(r.r,'readback_checkout',side_effect=checkout));stack.enter_context(patch.object(r.m,'Remote',return_value=rf))
   stack.enter_context(patch.object(r.checkpoints,'discover_anchor',return_value={'anchor':anchor,'recovery_anchor':r.png_recovery.TAG}))
   stack.enter_context(patch.object(r.png_image,'AssetGet',return_value=lambda url:bysha[url.rsplit('/',1)[1]]))
   result=r.run('synthetic-token',self.request,lambda:deepcopy(self.writers),apply=True,public_policy=policy,approved_public_policy_sha256='d'*64,publication_bytes=b'fixture',execution=context,checkpoint_stream=io.StringIO())
  self.assertEqual(result.get('execution',{}).get('created'),2,result)
  self.assertGreater(len(rf.calls),1);first=json.loads(remote.run(rf.bare,'show',rf.calls[0][1]+':'+o.STATE_PATH))
  expected=deepcopy(old);expected['policy_sha256']=new_policy;self.assertEqual(first,expected)
