"""Real local Git/receipt/PNG transport recovery, never external network."""
from copy import deepcopy
import contextlib,json
import unittest
from unittest.mock import patch
import get_notion_runtime as r
import get_notion_png_expired_recovery as x
import get_notion_outbox as o
import get_notion_executor as e
import test_png_cli_remote as fixture
import test_notion_outbox as source
import test_notion_remote_journal as git

class Tests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.Tests();self.f.setUp();self.addCleanup(self.f.doCleanups);f=self.f
  original=f.request.block;bysha={e.digest(v):v for v in f.images.values()}
  def block(row,page):
   value=original(row,page)
   if row['type']=='image':value['image']['file']['url']='https://prod-files-secure.s3.us-west-2.amazonaws.com/'+f.uploads[row['payload']['file_upload']['id']]['filename'][4:-4]
   return value
  f.request.block=block
  self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
  self.stack.enter_context(patch.object(r.png_image,'AssetGet',return_value=lambda url:bysha[url.rsplit('/',1)[1]]))
  def stop_peer():
   if len(f.api.pages):raise ValueError('writer_gate_unknown')
   return deepcopy(f.writers)
  loaded=f.preload()
  result=r.dispatch(f.local,f.lr,None,f.src.root,f.outer,f.request,tip=f.src.last,policy_hash=source.P,apply=True,writers_guard=stop_peer,loaded_assets=loaded)
  self.before,self.before_sha=f.outer.read();j=self.before['engine_state']['journal']
  # The expired recovery is a fresh invocation, with a new transport latch.
  f.request.attempted.clear();f.request.attempts=0;f.request.last=None
  self.assertEqual(j['index'],1,result);self.assertEqual(j['operation']['phase'],'prepared',result)
  self.assertEqual(len(self.before['png_assets']['attachments']),1,result)
  self.ident=j['plan']['records'][1]['note_id'];self.slot=self.before['png_assets']['uploads'][self.ident]
  f.uploads[self.slot['state']['upload_id']].update(status='expired',expiry_time=None,archived=False,in_trash=False)
  row=j['plan']['records'][1]
  pins={'STATE_SHA':self.before_sha,'STATE_BYTES':len(o.encoded(self.before)),
   'STATE_COMMIT':f.outer.anchor['commit'],'STATE_BLOB':f.outer.anchor['state_blob'],
   'PLAN_SHA':u(j['plan']),'OP_SHA':u(j['operation']),'JOURNAL_SHA':u(j),'SLOT_SHA':u(self.slot),'NOTE_SHA':u(self.ident),
   'ROW_SEMANTICS_SHA':x.row_semantics(row,self.slot['state']['upload_id']),
   'PREFLIGHT_SHA':x.preflight_sha(row['rendered'],row['source'],row['properties'],self.slot['state']['upload_id'])}
  self.stack.enter_context(patch.multiple(x,**pins))
  import get_recurring_policy_recovery as recovery
  self.new_policy='c'*64
  for key,value in [('ORIGINAL_RECEIPT_COMMIT',f.src.last['commit']),('ORIGINAL_RECEIPT_SHA256',f.src.last['receipt_sha256'])]:self.stack.enter_context(patch.object(recovery,key,value))
  self.stack.enter_context(patch.object(recovery,'old_public_policy',return_value={'get_policy_sha256':source.P}));self.stack.enter_context(patch.object(recovery,'current_get_policy',return_value=self.new_policy))
  # New filtered clone is required: its local main must include the old journal.
  anchor=deepcopy(f.outer.anchor);f.rf.root=f.rf.clone('before-expired');f.local=r.r.LocalGit(f.rf.root);f.rf.load(f.rf.head());f.outer=f.rf.store(anchor);f.api.store=o.EngineStore(f.outer)
  virtual=deepcopy(self.before);virtual['policy_sha256']=self.new_policy
  desc=j['plan']['publication'];r.source_loader.load_queue_publication(f.local,f.lr,None,f.src.root,{k:desc[k] for k in ('commit','parent')}|{'receipt_sha256':desc['outbox_receipt_sha256']},self.new_policy)
  self.pub=o.QueuePublication(f.local.vault,virtual,u(virtual),resume=True)
  self.loaded=r.png_loader.load(f.local,f.lr,self.pub.descriptor,f.anchor,u(f.anchor),self.new_policy)
 def observe(self):return r.observe_expired_png(self.before,self.loaded,self.pub,self.f.request,lambda:deepcopy(self.f.writers))
 def replace(self):return self.f.outer.compare_and_set_png_expired(self.before_sha,self.new_policy,self.observe)
 def dispatch(self):
  f=self.f
  return r.dispatch(f.local,f.lr,None,f.src.root,f.outer,f.request,tip=f.src.last,policy_hash=self.new_policy,apply=True,writers_guard=lambda:deepcopy(f.writers),loaded_assets=self.loaded)
 def test_real_expired_transition_then_one_upload_one_page_and_no_duplicate(self):
  f=self.f;posts=len(f.request.attempted);pages=deepcopy(f.api.pages);count=len(f.uploads)
  candidate,sha=self.replace();self.assertEqual(len(f.request.attempted),posts)
  result=self.dispatch();self.assertEqual(result.get('execution',{}).get('created'),1,result)
  after,version=f.outer.read();self.assertEqual(len(f.uploads),count+1);self.assertEqual(len(f.api.pages),len(pages)+1)
  for ident,page in pages.items():self.assertEqual(f.api.pages[ident],page)
  self.assertIn(self.ident,after['png_assets']['attachments']);self.assertEqual(after['png_assets']['expired_recovery']['old_slot'],self.slot)
  self.assertEqual(after['png_assets']['expired_recovery']['generation'],1)
  posts=len(f.request.attempted)
  anchor=deepcopy(f.outer.anchor);f.rf.root=f.rf.clone('after-expired');f.local=r.r.LocalGit(f.rf.root);f.rf.load(f.rf.head());f.outer=f.rf.store(anchor);f.api.store=o.EngineStore(f.outer);self.loaded=f.preload()
  result=self.dispatch();self.assertEqual(len(f.request.attempted),posts,result);self.assertIsNone(f.outer.read()[0]['engine_state']['journal'])
 def test_cas_then_fresh_clone_before_any_upload_does_not_replace_again(self):
  f=self.f;candidate,sha=self.replace();posts=len(f.request.attempted);anchor=deepcopy(f.outer.anchor)
  f.rf.root=f.rf.clone('after-only-replacement');f.local=r.r.LocalGit(f.rf.root);f.rf.load(f.rf.head());f.outer=f.rf.store(anchor);f.api.store=o.EngineStore(f.outer);self.loaded=f.preload()
  with self.assertRaises(Exception):self.replace()
  self.assertEqual(len(f.request.attempted),posts)
  result=self.dispatch();self.assertEqual(result.get('execution',{}).get('created'),1,result)
 def test_not_expired_or_missing_query_blocks_without_cas(self):
  f=self.f;before=len(f.rf.calls);f.uploads[self.slot['state']['upload_id']]['status']='uploaded'
  with self.assertRaises(Exception):self.replace()
  self.assertEqual(len(f.rf.calls),before);self.assertEqual(f.outer.read()[1],self.before_sha)


 def test_whole_run_preloads_then_atomic_transition_and_verifies_only_second_page(self):
  import io,base64
  f=self.f;rf=f.rf;anchor=deepcopy(f.outer.anchor);posts=len(f.request.attempted);pages=deepcopy(f.api.pages)
  class Remote:
   def __getattr__(self,name):return getattr(rf,name)
   def ref(self,name):return rf.ref(name) if name==r.r.MAIN_REF else r.m.PROTECTED[name]
   def tree_entries(self,tree,recursive=False):
    rows=[]
    for raw in git.run(rf.bare,'ls-tree',*(['-r'] if recursive else []),'-z',tree).split(b'\0'):
     if not raw:continue
     meta,path=raw.split(b'\t');mode,kind,oid=meta.decode().split();row={'path':path.decode(),'mode':mode,'type':kind,'sha':oid}
     if kind=='blob':row['size']=int(git.run(rf.bare,'cat-file','-s',oid))
     rows.append(row)
    return rows
   source_get_tree=r.r.GitHubRemote.source_get_tree
   check_source_budget=r.r.GitHubRemote.check_source_budget
   def request(self,path,**kwargs):
    oid=path.removeprefix('/git/blobs/');data=git.run(rf.bare,'cat-file','blob',oid)
    return {'sha':oid,'size':len(data),'encoding':'base64','content':base64.b64encode(data).decode()}
  class View:
   def read(self):
    value=json.loads(git.run(rf.bare,'show',rf.head()+':'+o.STATE_PATH));return value['engine_state'],e.digest(value['engine_state'])
  f.api.store=View()
  def checkout(destination,commit,token):
   rf.root=rf.clone('whole-expired-run')
   class Local(r.r.LocalGit):
    def run(self,*args,**kw):
     if args==('config','--local','--get-all','remote.origin.url'):return (r.r.REMOTE_URL+'\n').encode()
     return super().run(*args,**kw)
   return Local(rf.root)
  policy={**r.run16_recovery.PUBLIC_POLICY,'code_sha':'a'*40,'rules_sha256':'b'*64,'get_policy_sha256':self.new_policy}
  execution={'run_id':1,'run_attempt':1,'code_sha':'a'*40,'controller_sha':'c'*40}
  marker={'receipt_commit':f.src.last['commit'],'receipt_sha256':f.src.last['receipt_sha256'],'receipt_sequence':1}
  stream=io.StringIO();before_cas=len(rf.calls)
  with contextlib.ExitStack() as stack:
   for obj,key,value in ((r,'PRODUCTION_ACTIVATED',True),(r.recurring,'PRODUCTION_ACTIVATED',True)):
    stack.enter_context(patch.object(obj,key,value))
   stack.enter_context(patch.object(r.recurring,'validate_policy',side_effect=lambda p,*args:p))
   stack.enter_context(patch.object(r.provenance,'verify_execution',return_value=None))
   stack.enter_context(patch.object(r.provenance,'parse_record',return_value=marker))
   stack.enter_context(patch.object(r.r,'readback_checkout',side_effect=checkout))
   stack.enter_context(patch.object(r.m,'Remote',return_value=Remote()))
   stack.enter_context(patch.object(r.checkpoints,'discover_anchor',return_value={'anchor':anchor,'recovery_anchor':r.run16_recovery.TAG}))
   result=r.run('synthetic-token',f.request,lambda:deepcopy(f.writers),apply=True,public_policy=policy,approved_public_policy_sha256='d'*64,publication_bytes=b'fixture',execution=execution,checkpoint_stream=stream)
  self.assertEqual(result.get('execution',{}).get('created'),1,result)
  self.assertIn('replaced_prior_plan_sha256',result);self.assertNotIn('resumed_prior_plan_sha256',result)
  self.assertEqual(len(f.api.pages),len(pages)+1)
  for ident,page in pages.items():self.assertEqual(f.api.pages[ident],page)
  first=json.loads(git.run(rf.bare,'show',rf.calls[before_cas][1]+':'+o.STATE_PATH))
  self.assertEqual(first['policy_sha256'],self.new_policy);self.assertIsNone(first['engine_state']['journal'])
  self.assertEqual(first['png_assets']['expired_recovery']['old_slot'],self.slot)
  final=json.loads(git.run(rf.bare,'show',rf.head()+':'+o.STATE_PATH))
  self.assertIn(self.ident,final['png_assets']['attachments']);self.assertIn(r.checkpoints.MARKER,stream.getvalue())
  self.assertEqual(len(f.request.attempted)-posts,3)

def u(value):return o.digest(value)
if __name__=='__main__':unittest.main()
