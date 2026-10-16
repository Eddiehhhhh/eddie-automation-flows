from copy import deepcopy
import unittest
from unittest.mock import patch
import get_notion_pipeline as q
import get_notion_executor as e
import get_notion_live_transport as t
import test_notion_executor as f
from test_executor_live_adapter import OfficialFixture,DB

class FixtureTransport(t.Transport):
 def __init__(self,api):super().__init__('synthetic-test-only');self.fixture=OfficialFixture(api)
 def __call__(self,method,path,body=None):
  read=bool(t.ro.read_route(method,path));key=t.fingerprint(method,path,body)
  if not read:
   if key not in self.approved or key in self.attempted:raise AssertionError('unapproved fixture write')
   self.attempted.add(key)
  return self.fixture(method,path,body)

class Tests(unittest.TestCase):
 def setUp(self):
  self.f=f.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties');self.f.setUp();self.addCleanup(self.f.doCleanups);self.f.publish('New source')
  self.transport=FixtureTransport(self.f.api)
  self.policy={'contract':q.CONTRACT,'rules_sha256':e.rules_sha256(),'data_source_id':f.DS,'data_source_pin':t.native.sha(f.DS),'database_pin':t.native.sha(DB),'title_property':'Title','allow_apply':False,'allow_create':True,'allow_archive':True,'format_accepted':True,'initial_scope_sha256':None,'plan_authorization':'standing_bounded'}
 def run_it(self,**kwargs):return q.run(self.f.publication,self.f.store,self.transport,self.policy,e.digest(self.policy),writers_guard=lambda:deepcopy(self.f.policy['old_writer_evidence']),**kwargs)
 def test_prepare_actual_native_no_journal_or_writes(self):
  before=self.f.store.read();out=self.run_it();self.assertEqual(out['status'],'prepared',out);self.assertEqual(out['actions'],{'update':1});self.assertEqual(self.f.store.read(),before);self.assertFalse(self.transport.attempted)
 def test_apply_false_policy_rejected_before_any_reads(self):
  out=self.run_it(apply=True);self.assertEqual(out['reason'],'approval_missing');self.assertFalse(self.transport.fixture.calls);self.assertFalse(self.transport.attempted)
 def test_trusted_policy_apply_true_native_update_and_repeat(self):
  self.policy['allow_apply']=True;out=self.run_it(apply=True);self.assertEqual(out['status'],'executed',out);self.assertEqual(out['execution']['updated'],1);self.assertEqual(out['write_state'],'confirmed');self.assertEqual(len(self.transport.attempted),2)
  calls=len(self.transport.fixture.calls);again=self.run_it(apply=True);self.assertEqual(again['selected'],0);self.assertEqual(again['execution']['write_state'],'not_attempted');self.assertEqual(len(self.transport.fixture.calls),calls)
 def test_invalid_external_hash_and_rules_before_reads(self):
  out=q.run(self.f.publication,self.f.store,self.transport,self.policy,'0'*64,writers_guard=lambda:None);self.assertEqual(out['reason'],'approval_missing');self.assertFalse(self.transport.fixture.calls)
  self.policy['rules_sha256']='0'*64;self.assertEqual(self.run_it()['reason'],'rules_changed');self.assertFalse(self.transport.fixture.calls)
 def test_unowned_blocks_create_peer_without_adopting(self):
  state,sha=self.f.store.read();state['receipts']={};self.f.store.compare_and_set(sha,state)
  self.f.parent=self.f.git('rev-parse','HEAD').decode().strip();(self.f.source_path.parent/'b.md').write_bytes(f.md(f.N2,'New peer'));self.f.publish('Changed source')
  self.policy['allow_apply']=True;out=self.run_it(apply=True);self.assertEqual(out['actions'],{'blocked':1,'create':1});self.assertEqual(out['execution']['created'],1);self.assertEqual(out['execution']['blocked'],1)
 def test_read_failure_no_journal_no_writes(self):
  before=self.f.store.read()
  with patch.object(self.transport.fixture,'page',side_effect=RuntimeError('PRIVATE')):out=self.run_it()
  self.assertEqual(out['status'],'failed');self.assertEqual(out['write_state'],'not_attempted');self.assertNotIn('PRIVATE',str(out));self.assertEqual(self.f.store.read(),before)
 def test_append_unknown_resume_only_readback_no_second_append(self):
  self.policy['allow_apply']=True;self.f.api.throw='append_after';out=self.run_it(apply=True)
  self.assertEqual(out['execution']['status'],'blocked');self.assertEqual(out['write_state'],'uncertain');self.assertEqual(len(self.transport.attempted),1)
  self.f.api.throw=None;self.transport=FixtureTransport(self.f.api);out=self.run_it(apply=True);self.assertEqual(out['execution']['status'],'verified',out);self.assertEqual(len(self.transport.attempted),1)
  self.assertEqual(len([c for c in self.f.api.calls if c[0]=='append']),1)
 def test_prior_uncertain_journal_read_failure_remains_unknown(self):
  self.policy['allow_apply']=True;self.f.api.throw='append_after';self.run_it(apply=True)
  self.f.api.throw=None;self.transport=FixtureTransport(self.f.api)
  with patch.object(self.transport.fixture,'page',side_effect=RuntimeError('PRIVATE')):out=self.run_it()
  self.assertEqual(out['write_state'],'unknown',out);self.assertIsNone(out['external_write_attempts']);self.assertNotIn('PRIVATE',str(out))
 def test_prior_uncertain_prepare_and_guard_failure_do_not_claim_zero(self):
  self.policy['allow_apply']=True;self.f.api.throw='append_after';self.run_it(apply=True);self.f.api.throw=None
  self.transport=FixtureTransport(self.f.api);out=self.run_it();self.assertEqual(out['status'],'prepared',out);self.assertEqual(out['write_state'],'unknown');self.assertIsNone(out['external_write_attempts'])
  out=q.run(self.f.publication,self.f.store,self.transport,self.policy,e.digest(self.policy),writers_guard=lambda:None);self.assertEqual(out['write_state'],'unknown')
 def test_exact_plan_requires_external_digest(self):
  self.policy.update(allow_apply=True,plan_authorization='exact_plan');out=self.run_it(apply=True)
  self.assertEqual(out['reason'],'approval_missing');self.assertFalse(self.transport.fixture.calls)
  prepared=self.run_it();out=self.run_it(apply=True,approved_plan_sha256='0'*64);self.assertEqual(out['reason'],'approval_missing');self.assertFalse(self.transport.attempted)
  out=self.run_it(apply=True,approved_plan_sha256=prepared['plan_sha256']);self.assertEqual(out['execution']['updated'],1,out)
 def test_explicit_create_recovery_reaches_engine_without_second_create(self):
  del self.f.api.pages[f.N];state,sha=self.f.store.read();state['receipts']={};self.f.store.compare_and_set(sha,state)
  self.policy['allow_apply']=True;self.f.api.throw='create_after';out=self.run_it(apply=True);self.assertEqual(out['execution']['reason'],'uncertain_create',out)
  self.f.api.throw=None;self.transport=FixtureTransport(self.f.api);out=self.run_it(apply=True);self.assertEqual(out['execution']['reason'],'uncertain_create',out)
  state,_=self.f.store.read();plan=state['journal']['plan'];native=t.API(self.transport,f.DS,[f.N],data_source_pin=t.native.sha(f.DS),database_pin=t.native.sha(DB)).read_note(f.N)
  recovery={'plan_sha256':e.digest(plan),'note_id':f.N,'page_id':native['page_id'],'snapshot_sha256':e.target_sha(native,f.N,f.DS)}
  out=self.run_it(apply=True,create_recovery=recovery,approved_recovery_sha=e.digest(recovery));self.assertEqual(out['execution']['status'],'verified',out);self.assertEqual(len([x for x in self.f.api.calls if x[0]=='create']),1)
 def test_initial_scope_is_exact_and_current_target_must_match(self):
  state,sha=self.f.store.read();state['receipts']={};self.f.store.compare_and_set(sha,state)
  native=t.API(self.transport,f.DS,[f.N],data_source_pin=t.native.sha(f.DS),database_pin=t.native.sha(DB)).read_note(f.N)
  scope={f.N:{'page_id':native['page_id'],'snapshot_sha256':e.target_sha(native,f.N,f.DS),'managed_block_ids':self.f.owned}}
  out=self.run_it(initial_scope=scope);self.assertEqual(out['reason'],'approval_missing',out)
  self.policy['initial_scope_sha256']=e.digest(scope);out=self.run_it(initial_scope=scope);self.assertEqual(out['actions'],{'update':1},out)
  self.f.api.pages[f.N]['properties_raw']['Human']={'type':'number','number':3}
  out=self.run_it(initial_scope=scope);self.assertEqual(out['actions'],{'blocked':1},out);self.assertFalse(self.transport.attempted)
 def empty_fixture(self):
  self.policy['allow_apply']=True;self.run_it(apply=True)
  # A genuine already-verified zero-record repeat; descriptor marker exercises
  # the pipeline's outbox-only settlement hook without changing source content.
  real=e.prepare
  def prepare(*args,**kwargs):
   plan=real(*args,**kwargs);plan['publication']['outbox_receipt_sha256']='a'*64;return plan
  return patch.object(e,'prepare',side_effect=prepare),patch.object(self.f.publication,'check',return_value=None)
 def test_empty_outbox_prepare_never_settles_and_apply_once(self):
  one,two=self.empty_fixture();calls=[]
  def settle(plan,sha):self.assertEqual(e.digest(plan),sha);calls.append(sha);return 'b'*64
  with one,two:
   prepared=self.run_it(empty_plan_settle=settle);self.assertEqual(prepared['selected'],0);self.assertFalse(calls)
   applied=self.run_it(apply=True,empty_plan_settle=settle);self.assertEqual(applied['status'],'executed',applied);self.assertEqual(len(calls),1)
 def test_empty_outbox_missing_or_failed_settle_is_not_success(self):
  one,two=self.empty_fixture()
  with one,two:
   result=self.run_it(apply=True);self.assertEqual(result['status'],'failed',result);self.assertEqual(result['stage'],'queue_settle')
   def bad(plan,sha):raise ValueError('PRIVATE')
   result=self.run_it(apply=True,empty_plan_settle=bad);self.assertEqual(result['status'],'failed');self.assertNotIn('PRIVATE',str(result))
 def test_report_whitelist_and_unknown_default(self):
  out=self.run_it();out['PRIVATE']='content';self.assertNotIn('PRIVATE',q.public_report(out));self.assertEqual(q.public_report({})['write_state'],'unknown')
  for bad in ({'actions':{'bad':1}},{'reason_counts':{'PRIVATE':1}},{'get_requests':False},{'selected':True}):
   value=self.run_it();value.update(bad);self.assertEqual(q.public_report(value)['status'],'failed')
if __name__=='__main__':unittest.main()
