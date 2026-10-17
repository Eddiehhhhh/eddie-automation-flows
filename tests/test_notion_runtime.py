from copy import deepcopy
import contextlib
import io
import unittest
from unittest.mock import patch
import get_notion_runtime as rt
import get_notion_outbox as o
import get_notion_executor as e
import test_confirmed_source as s
import test_notion_outbox as f
import test_notion_executor as ef
from test_notion_pipeline import FixtureTransport
from test_executor_live_adapter import DB

class RuntimeTests(unittest.TestCase):
 def setUp(self):
  self.f=s.Tests(methodName='test_exact_first_batch_then_noop_and_state_only');self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.store=f.Store();self.adapter=o.EngineStore(self.store);self.api=ef.API(self.adapter)
  self.request=FixtureTransport(self.api)
  self.policy={'fresh_readback_verified':True,'writers':{n:{'disabled':True,'active_runs':0} for n in ('getnote-to-notion','notion-to-getnote')}}
  self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
  self.stack.enter_context(patch.object(rt.entry,'BOOTSTRAP',{'commit':self.f.origin['commit']}))
  self.stack.enter_context(patch.object(rt,'initial_done',return_value=True))
  self.stack.enter_context(patch.object(rt.transport.native,'DATA_SOURCE_PIN',rt.transport.native.sha(ef.DS)))
  self.stack.enter_context(patch.object(rt.transport.native,'DATABASE_PIN',rt.transport.native.sha(DB)))
  self.stack.enter_context(patch.object(rt,'schema',return_value='Title'))
  original=rt.transport.native.collect
  def collect(request,**kw):return original(request,**kw) if kw else {'snapshots':{'x':{'parent_data_source':ef.DS}}}
  self.stack.enter_context(patch.object(rt.transport.native,'collect',side_effect=collect))
 def invoke(self,local,apply=False):
  # API assertions observe the actual atomically wrapped outbox engine state.
  self.api.store=o.EngineStore(self.store)
  return rt.dispatch(local,self.f.remote,None,self.f.root,self.store,self.request,tip=self.f.tip,policy_hash=s.POLICY,apply=apply,writers_guard=lambda:deepcopy(self.policy))
 def test_fresh_blob_none_prepare_creates_no_journal_then_real_dispatch_and_repeat(self):
  self.f.publication({s.N(1):'Source one'});local=self.f.clone(shallow=True)
  before=self.store.read();prepared=self.invoke(local);self.assertEqual(prepared['status'],'prepared',prepared);self.assertEqual(prepared['actions'],{'create':1});self.assertEqual(self.store.read(),before)
  applied=self.invoke(local,True);self.assertEqual(applied['execution']['created'],1,applied);self.assertEqual(applied['queued'],0)
  self.assertIn(s.N(1),self.store.value['engine_state']['receipts'])
  count=len(self.request.fixture.calls);again=self.invoke(local,True);self.assertEqual(again['status'],'queue_empty',again);self.assertEqual(len(self.request.fixture.calls),count)
  asset=self.f.git('rev-parse','HEAD:Raw/03 Get/assets/opaque.bin').decode().strip()
  self.assertTrue(e.git.OfflineGit(local.vault).local.run('cat-file','--batch-check',data=(asset+'\n').encode()).endswith(b' missing\n'))
 def test_inflight_resume_does_not_enqueue_new_publication_or_repeat_create(self):
  self.f.publication({s.N(1):'Source'});local=self.f.clone();self.api.throw='create_after'
  first=self.invoke(local,True);self.assertEqual(first['write_state'],'uncertain',first)
  old=self.store.value['engine_state']['journal'];self.api.throw=None;self.request=FixtureTransport(self.api)
  again=self.invoke(local,True);self.assertEqual(again['execution']['reason'],'uncertain_create',again);self.assertEqual(self.store.value['engine_state']['journal'],old);self.assertEqual(len([c for c in self.api.calls if c[0]=='create']),1)
 def test_zero_changes_queue_stays_empty(self):
  self.f.publication({});local=self.f.clone();out=self.invoke(local,True);self.assertEqual(out['status'],'queue_empty');self.assertFalse(self.request.fixture.calls);self.assertEqual(self.store.value['last_enqueued_publication'],self.f.tip)
 def test_apply_gates_before_credentials_network(self):
  for get_enabled,notion_enabled in ((False,False),(True,False),(False,True)):
   with self.subTest(get_enabled=get_enabled,notion_enabled=notion_enabled),patch.object(rt.recurring,'PRODUCTION_ACTIVATED',get_enabled),patch.object(rt,'PRODUCTION_ACTIVATED',notion_enabled):
    with patch.object(rt.transport,'Transport',side_effect=AssertionError('no transport')),contextlib.redirect_stdout(io.StringIO()) as output:
     code=rt.main(['--apply','--scope-from-policy'])
    self.assertEqual(code,1);self.assertIn('notion_activation_pending',output.getvalue())
    with patch.object(rt.m,'Remote',side_effect=AssertionError('no remote')):
     out=rt.run(None,None,None,apply=True)
    self.assertEqual(out['reason'],'notion_activation_pending')
 def test_schema_requires_exact_title_id_and_parent_types(self):
  data={'id':ef.DS,'parent':{'database_id':DB},'properties':{'Title':{'type':'title'},'笔记 ID':{'type':'rich_text'}}}
  with patch.object(rt,'schema',wraps=ORIGINAL_SCHEMA):
   self.assertEqual(rt.schema(lambda *a:data,ef.DS),'Title')
   for delta in ({'properties':{}},{'parent':{'database_id':'f'*32}},{'id':'f'*32}):
    with self.assertRaises(Exception):rt.schema(lambda *a:{**data,**delta},ef.DS)
 def test_manual_workflow_prepare_mode_has_no_apply_or_recovery(self):
  from pathlib import Path
  workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml').read_text()
  step=workflow.split('      - name: Prepare the bounded Notion transaction without writes')[1].split('      - name: Publish aggregate evidence only')[0]
  self.assertIn("inputs.mode == 'notion_prepare'",step);self.assertIn('test "$APPLY" != true',step);self.assertIn('test -z "$APPROVED_PAYLOAD_SHA256"',step)
  self.assertIn('get_notion_runtime.py --expected-parent "$EXPECTED_PARENT"',step)
  self.assertNotIn('--apply',step);self.assertNotIn('--scope-from-policy',step);self.assertNotIn('--recovery',step)
 def test_manual_public_log_capability_check_is_bounded_and_redacted(self):
  from pathlib import Path
  from types import SimpleNamespace
  import textwrap,json
  wf=(Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml').read_text()
  code=textwrap.dedent(wf.split("python - <<'PYPUBLIC'\n")[1].split('          PYPUBLIC')[0])
  for status in (200,403):
   calls=[]
   class Fake:
    def __init__(self,token):self.requests=0;self.asserted=token
    def once(self,url,maximum):self.requests+=1;calls.append(url);return status,{},b'PRIVATE LOG PAYLOAD'
    def logs(self,job):
     if job!=117176342381:raise AssertionError('Wrong job')
     result=self.once('fixed-public-job',4096)
     if result[0]!=200:raise ValueError('PRIVATE RESPONSE')
     return result[2]
   with patch.dict(rt.sys.modules,{'get_publication_provenance':SimpleNamespace(PublicGithub=Fake)}),patch.dict(rt.os.environ,{'GITHUB_TOKEN':'synthetic-token'}),contextlib.redirect_stdout(io.StringIO()) as output:
    if status==200:exec(compile(code,'<workflow>', 'exec'),{})
    else:
     with self.assertRaises(SystemExit) as stopped:exec(compile(code,'<workflow>','exec'),{})
     self.assertEqual(stopped.exception.code,1)
   report=json.loads(output.getvalue());self.assertEqual(report['http_statuses'],[status]);self.assertEqual(report['writes'],0);self.assertEqual(len(calls),1)
   self.assertNotIn('PRIVATE',output.getvalue());self.assertNotIn('synthetic-token',output.getvalue())
 def test_safe_report_removes_private_and_unknown_is_unknown(self):
  self.assertEqual(rt.safe_result({})['write_state'],'unknown')
  self.f.publication({s.N(1):'Source'});out=self.invoke(self.f.clone());out['PRIVATE']='secret';safe=rt.safe_result(out);self.assertNotIn('PRIVATE',str(safe));self.assertEqual(safe['status'],'prepared',safe)

class ReportTests(unittest.TestCase):
 def test_cli_executed_failed_uncertain_unknown_are_nonzero(self):
  from unittest.mock import patch
  for status,state,expected in [('verified','confirmed',0),('partial','not_attempted',0),('blocked','uncertain',1),('failed','unknown',1),('uncertain','uncertain',1),('verified','unknown',1)]:
   result={'status':'executed','execution':{'status':status},'write_state':state}
   with patch.dict(rt.os.environ,{'GITHUB_TOKEN':'existing-public'}),patch.object(rt.transport,'Transport'),patch.object(rt.old_writers,'WriterGuard'),patch.object(rt,'run',return_value=result),contextlib.redirect_stdout(io.StringIO()):
    self.assertEqual(rt.main(['--expected-parent','a'*40]),expected)
  self.assertEqual(rt.exit_status({'status':'prepared','write_state':'unknown'}),1)
 def test_safe_pipeline_failure_preserves_fixed_diagnosis_only(self):
  for reason in ('permission_denied','journal_busy'):
   value={'contract':rt.pipeline.CONTRACT,'complete_library_claimed':False,'status':'failed','stage':'native_read','reason':reason,'write_state':'not_attempted','external_write_attempts':0,'PRIVATE':'secret'}
   out=rt.safe_result(value);self.assertEqual(out['reason'],reason);self.assertEqual(out['stage'],'native_read');self.assertNotIn('PRIVATE',str(out))
  for reason in ('PRIVATE',{},[],True):
   out=rt.safe_result({'status':'failed','stage':'PRIVATE','reason':reason,'write_state':'not_attempted','external_write_attempts':True})
   self.assertEqual(out['reason'],'other');self.assertEqual(out['write_state'],'unknown');self.assertNotIn('PRIVATE',str(out))

ORIGINAL_SCHEMA=rt.schema

class WholeRuntimeTests(unittest.TestCase):
 def test_actual_partial_clone_durable_journal_native_create_and_fresh_noop(self):
  import test_notion_journal_runtime as jf
  import test_notion_remote_journal as jg
  import base64,json,tempfile
  from pathlib import Path
  case=s.Tests(methodName='test_exact_first_batch_then_noop_and_state_only');case.setUp();self.addCleanup(case.doCleanups)
  case.publication({s.N(7):'Canonical committed source'});tip=deepcopy(case.tip)
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
  class Remote(jf.Fixture):
   def ref(self,name):return super().ref(name) if name==rt.r.MAIN_REF else rt.m.PROTECTED[name]
   def tree_entries(self,tree,recursive=False):
    rows=[]
    for raw in jg.run(self.bare,'ls-tree',*(['-r'] if recursive else []),'-z',tree).split(b'\0'):
     if not raw:continue
     meta,path=raw.split(b'\t');mode,kind,oid=meta.decode().split();row={'path':path.decode(),'mode':mode,'type':kind,'sha':oid}
     if kind=='blob':row['size']=int(jg.run(self.bare,'cat-file','-s',oid))
     rows.append(row)
    return rows
   source_get_tree=rt.r.GitHubRemote.source_get_tree
   check_source_budget=rt.r.GitHubRemote.check_source_budget
   def request(self,path,**kwargs):
    oid=path.removeprefix('/git/blobs/');data=jg.run(self.bare,'cat-file','blob',oid)
    return {'sha':oid,'size':len(data),'encoding':'base64','content':base64.b64encode(data).decode()}
  remote=Remote(temp.name,seed=case.repo)
  class View:
   def read(self):
    data=jg.run(remote.bare,'show',remote.head()+':'+o.STATE_PATH);value=json.loads(data)
    return value['engine_state'],e.digest(value['engine_state'])
  api=ef.API(View());request=FixtureTransport(api);original_collect=rt.transport.native.collect
  def collect(request,**kw):return original_collect(request,**kw) if kw else {'snapshots':{'x':{'parent_data_source':ef.DS}}}
  clones=[]
  def checkout(destination,commit,token):
   remote.root=remote.clone('run-'+str(len(clones)));clones.append(remote.root)
   class Local:
    vault=remote.root
    def run(self,*args,**kw):
     if args==('config','--local','--get-all','remote.origin.url'):return (rt.r.REMOTE_URL+'\n').encode()
     return jg.run(self.vault,*args,**kw)
   return Local()
  policy={'code_sha':'a'*40,'rules_sha256':'b'*64,'get_policy_sha256':s.POLICY}
  context={'run_id':1,'run_attempt':1,'code_sha':'a'*40,'controller_sha':'c'*40}
  marker={'receipt_commit':tip['commit'],'receipt_sha256':tip['receipt_sha256'],'receipt_sequence':tip['sequence']}
  writers=lambda:{'fresh_readback_verified':True,'writers':{n:{'disabled':True,'active_runs':0} for n in ('getnote-to-notion','notion-to-getnote')}}
  before=remote.source_get_tree(remote.head());protected=jg.run(remote.bare,'rev-parse','refs/tags/protected')
  with contextlib.ExitStack() as stack:
   stack.enter_context(patch.object(rt,'PRODUCTION_ACTIVATED',True));stack.enter_context(patch.object(rt.recurring,'PRODUCTION_ACTIVATED',True))
   stack.enter_context(patch.object(rt.entry,'BOOTSTRAP',{'commit':case.origin['commit']}));stack.enter_context(patch.object(rt,'initial_done',return_value=True))
   stack.enter_context(patch.object(rt.recurring,'validate_policy',side_effect=lambda p,*a:p));stack.enter_context(patch.object(rt.provenance,'verify_execution',return_value=None));stack.enter_context(patch.object(rt.provenance,'parse_record',return_value=marker))
   stack.enter_context(patch.object(rt.r,'readback_checkout',side_effect=checkout));stack.enter_context(patch.object(rt.m,'Remote',return_value=remote))
   stack.enter_context(patch.object(rt,'schema',return_value='Title'));stack.enter_context(patch.object(rt.transport.native,'DATA_SOURCE_PIN',rt.transport.native.sha(ef.DS)));stack.enter_context(patch.object(rt.transport.native,'DATABASE_PIN',rt.transport.native.sha(DB)));stack.enter_context(patch.object(rt.transport.native,'collect',side_effect=collect))
   stream=io.StringIO();result=rt.run('synthetic-token',request,writers,apply=True,public_policy=policy,approved_public_policy_sha256='d'*64,publication_bytes=b'fixture',execution=context,checkpoint_stream=stream)
   self.assertEqual(result.get('execution',{}).get('created'),1,result)
   lines=[json.loads(x[len(rt.checkpoints.MARKER):]) for x in stream.getvalue().splitlines() if x.startswith(rt.checkpoints.MARKER)]
   self.assertGreaterEqual(len(lines),4);anchor={k:lines[-1][k] for k in ('commit','state_blob','state_sha256')}
   # The next process consumes separately verified previous checkpoint evidence.
   stack.enter_context(patch.object(rt.checkpoints,'discover_anchor',return_value={'anchor':anchor}))
   writes=len(remote.calls);notion_writes=len(api.calls);noop_stream=io.StringIO();again=rt.run('synthetic-token',FixtureTransport(api),writers,apply=True,public_policy=policy,approved_public_policy_sha256='d'*64,publication_bytes=b'fixture',execution=context,checkpoint_stream=noop_stream)
   self.assertEqual(again['status'],'queue_empty',again);self.assertEqual(len(remote.calls),writes)
   self.assertEqual(len(api.calls),notion_writes);self.assertEqual(again['journal_commits'],0)
   refreshed=[json.loads(x[len(rt.checkpoints.MARKER):]) for x in noop_stream.getvalue().splitlines() if x.startswith(rt.checkpoints.MARKER)]
   self.assertEqual(len(refreshed),1);self.assertEqual(refreshed[0]['cas_attempts'],0)
   self.assertEqual({k:refreshed[0][k] for k in ('commit','state_blob','state_sha256')},anchor)
  self.assertEqual(remote.source_get_tree(remote.head()),before);self.assertEqual(jg.run(remote.bare,'rev-parse','refs/tags/protected'),protected)
  self.assertEqual(len([call for call in api.calls if call[0]=='create']),1)
  asset=case.git('rev-parse','HEAD:Raw/03 Get/assets/opaque.bin').decode().strip()
  for clone in clones:self.assertTrue(rt.a.OfflineGit(clone).local.run('cat-file','--batch-check',data=(asset+'\n').encode()).endswith(b' missing\n'))

if __name__=='__main__':unittest.main()
