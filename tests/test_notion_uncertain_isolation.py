"""Exact quarantine leaves unknown effects unresolved; no automatic replay."""
from copy import deepcopy
from pathlib import Path
import contextlib
import unittest
from unittest.mock import patch
import get_notion_executor as e
import get_notion_plan_recovery as recovery
import get_notion_live_transport as t
import get_notion_outbox as o
import get_notion_runtime as rt
import test_notion_executor as ef
import test_notion_outbox as of
import test_notion_plan_recovery as rf


def pinned(test,state,anchor=None):
    journal=state['engine_state']['journal'];row=journal['plan']['records'][1]
    anchor=anchor or {'commit':'a'*40,'state_blob':'b'*40,'state_sha256':o.digest(state)}
    request=t.fingerprint('PATCH','/blocks/'+row['page_id']+'/children',{'children':t.wire_blocks(row['request_blocks']),'after':row['after_anchor']})
    constants={'OLD_ANCHOR':anchor,'OLD_PLAN':e.digest(journal['plan']),
        'OLD_ENGINE_RULES':journal['plan']['rules_sha256'],
        'HELD_NOTE_SHA256':e.digest(row['note_id']),'HELD_PAGE_SHA256':e.digest(row['page_id']),
        'OPERATION_SHA256':e.digest(journal['operation']),'REQUEST_FINGERPRINT':request}
    hold={'contract':'get-notion-exact-uncertain-hold-1','note_id':row['note_id'],'page_id':row['page_id'],
        'anchor':deepcopy(anchor),'run_id':recovery.OLD_RUN,'run_attempt':recovery.OLD_ATTEMPT,
        'checkpoint_sha256':recovery.OLD_CHECKPOINT,'plan_sha256':constants['OLD_PLAN'],'record_index':1,
        'operation_sha256':constants['OPERATION_SHA256'],'request_fingerprint':request,
        'reason':'uncertain_append','effects':'unresolved','retry_allowed':False}
    constants['HOLD_SHA256']=e.digest(hold)
    stack=contextlib.ExitStack();test.addCleanup(stack.close)
    for key,value in constants.items():stack.enter_context(patch.object(recovery,key,value))
    return hold


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.f=rf.RealGitResume(methodName='test_new_plan_cannot_borrow_exact_prior_rules_without_journal')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        def stop_second(kind):
            if kind=='append' and sum(c[0]=='append' for c in self.f.api.calls)==2:
                raise TimeoutError('PRIVATE UNKNOWN RESULT')
        self.f.api.hook=stop_second
        with patch.object(e,'rules_sha256',return_value=recovery.OLD_ENGINE_RULES):
            result=self.f.execute()
        self.assertEqual(result['reason'],'uncertain_append')
        self.before,self.sha=self.f.durable.read();self.held=pinned(self,self.before,deepcopy(self.f.durable.anchor))
        self.ident=self.held['note_id'];self.page=self.held['page_id']
        self.api_writes=len([x for x in self.f.api.calls if x[0]!='read'])

    def test_exact_pure_transition_keeps_evidence_and_success_receipt(self):
        original=e.encoded(self.before)
        candidate=recovery.isolate_outer(self.before,self.sha);o.check_state(candidate)
        self.assertEqual(e.encoded(self.before),original)
        self.assertEqual(candidate['engine_state']['receipts'],self.before['engine_state']['receipts'])
        self.assertNotIn(self.ident,candidate['engine_state']['receipts'])
        self.assertIsNone(candidate['engine_state']['journal'])
        self.assertEqual(candidate['engine_state']['unresolved_by_id'],{self.ident:self.held})
        restore=deepcopy(candidate);restore['engine_state'].pop('unresolved_by_id');restore['engine_state']['journal']=self.before['engine_state']['journal']
        self.assertEqual(e.encoded(restore),original)
        self.assertTrue(rt.initial_done(candidate) if set(rt.transport.native.PAGE_PINS.values())=={e.digest(ef.N),e.digest(ef.N2)} else recovery.held(candidate['engine_state'],self.ident))
        self.assertEqual(len([x for x in self.f.api.calls if x[0]!='read']),self.api_writes)

    def test_wrong_hash_or_altered_old_operation_rejected(self):
        with self.assertRaises(ValueError):recovery.isolate_outer(self.before,'0'*64)
        for field,value in (('index',0),('operation',None),('plan_sha256','0'*64)):
            changed=deepcopy(self.before);changed['engine_state']['journal'][field]=value
            with self.assertRaises(ValueError):recovery.isolate_outer(changed,e.digest(changed))

    def test_remote_cas_and_fresh_read_preserve_receipt_and_hold(self):
        candidate=recovery.isolate_outer(self.before,self.sha)
        after=self.f.durable.compare_and_set(self.sha,candidate)
        self.f.fresh();fresh,digest=self.f.durable.read()
        self.assertEqual((fresh,digest),(candidate,after))
        self.assertTrue(recovery.held(fresh['engine_state'],self.ident))
        for change in ('remove','empty','mutate'):
            bad=deepcopy(fresh)
            if change=='remove':bad['engine_state'].pop('unresolved_by_id')
            elif change=='empty':bad['engine_state']['unresolved_by_id']={}
            else:bad['engine_state']['unresolved_by_id'][self.ident]['retry_allowed']=True
            with self.assertRaises(Exception):self.f.durable.compare_and_set(digest,bad)
        self.assertEqual(self.f.durable.read()[0],fresh)

    def test_race_prevents_isolation_without_api(self):
        candidate=recovery.isolate_outer(self.before,self.sha)
        changed=deepcopy(self.before);changed['engine_state']['failures'][ef.N]={'md_blob':'1'*40,'source_commit':'2'*40,'policy_sha256':'3'*64,'reason':'other'}
        self.f.durable.compare_and_set(self.sha,changed)
        with self.assertRaises(Exception):self.f.durable.compare_and_set(self.sha,candidate)
        self.assertEqual(self.f.durable.read()[0],changed)
        self.assertEqual(len([x for x in self.f.api.calls if x[0]!='read']),self.api_writes)

    def test_retry_source_changes_and_late_effects_cannot_rearm(self):
        candidate=recovery.isolate_outer(self.before,self.sha);self.f.durable.compare_and_set(self.sha,candidate)
        self.f.api.hook=None
        # Regardless of later observed contents, held ID is excluded and the
        # old unchanged plan cannot be executed against the new trusted state.
        row=self.f.plan['records'][1];before=deepcopy(self.f.api.pages[self.ident])
        for mode in ('none','complete','partial','duplicate','user_edit'):
            snapshot=deepcopy(before)
            if mode!='none':
                blocks=deepcopy(row['request_blocks'])
                if mode=='partial':blocks=blocks[:1]
                elif mode=='duplicate':blocks=blocks+deepcopy(blocks)
                elif mode=='user_edit':blocks=[ef.paragraph('User edit')]
                new,_=self.f.api.rows(blocks,self.page)
                pos=next(i for i,x in enumerate(snapshot['recursive_blocks']) if x['id']==row['after_anchor'])+1
                snapshot['recursive_blocks'][pos:pos]=new
            self.f.api.pages[self.ident]=snapshot
            state,sha=self.f.store.read()
            plan=e.prepare(self.f.publication,state,sha,{i:self.f.api.read_note(i) for i in (ef.N,ef.N2)},self.f.f.policy,retry_conflicts=True)
            self.assertNotIn(self.ident,[r['note_id'] for r in plan['records']])
            self.assertIn(self.ident,plan['skipped_conflicts'])
            out=e.execute(self.f.plan,e.digest(self.f.plan),self.f.publication,self.f.api,self.f.store,expected_store_sha=sha,writers_guard=lambda:self.f.f.policy['old_writer_evidence'])
            self.assertIn(out['reason'],('rules_changed','state_changed','unresolved_operation'))
        self.assertEqual(len([x for x in self.f.api.calls if x[0]!='read']),self.api_writes)

    def test_page_alias_is_blocked_before_any_write(self):
        candidate=recovery.isolate_outer(self.before,self.sha)
        engine=candidate['engine_state'];self.assertTrue(recovery.held(engine,ef.N,self.page))
        self.assertFalse(recovery.held(engine,ef.N,'f'*32))
        wrong=deepcopy(engine);wrong['unresolved_by_id'][self.ident]['page_id']='f'*32
        with self.assertRaises(ValueError):recovery.held(wrong,ef.N,'f'*32)


if __name__=='__main__':unittest.main()

class QueueIsolationTests(unittest.TestCase):
 def setUp(self):
  import test_notion_runtime as f
  import test_confirmed_source as s
  self.f=f.RuntimeTests(methodName='test_fresh_blob_none_prepare_creates_no_journal_then_real_dispatch_and_repeat');self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.held_id=s.N(1)
  self.hold={'note_id':self.held_id,'page_id':'f'*32}
  stack=contextlib.ExitStack();self.addCleanup(stack.close)
  stack.enter_context(patch.object(recovery,'HELD_NOTE_SHA256',e.digest(self.held_id)))
  stack.enter_context(patch.object(recovery,'HOLD_SHA256',e.digest(self.hold)))
  self.f.store.value['engine_state']['unresolved_by_id']={self.held_id:self.hold}
  self.f.f.publication({s.N(i):'Ordinary source '+str(i) for i in range(1,11)})
 def test_ten_actual_committed_ids_retained_peer_progress_and_repeat(self):
  local=self.f.f.clone(shallow=True)
  before=self.f.store.read()
  prepared=self.f.invoke(local,False)
  self.assertEqual(prepared['actions'],{'create':9},prepared)
  self.assertEqual(self.f.store.read(),before)
  applied=self.f.invoke(local,True)
  self.assertEqual(applied['execution']['created'],9,applied)
  state=self.f.store.value
  self.assertEqual(set(state['pending_by_id']),{self.held_id})
  self.assertEqual(len(state['engine_state']['receipts']),9)
  self.assertEqual(state['engine_state']['unresolved_by_id'],{self.held_id:self.hold})
  self.assertNotIn(self.held_id,self.f.api.pages)
  calls=len(self.f.api.calls)
  repeat=self.f.invoke(local,True)
  self.assertEqual(repeat['execution']['external_write_attempts'],0,repeat)
  self.assertEqual(len(self.f.api.calls),calls)
  self.assertEqual(set(self.f.store.value['pending_by_id']),{self.held_id})
  safe=rt.safe_result(applied)
  self.assertEqual(safe['unresolved_operations'],1)
  self.assertEqual(safe['unresolved_effects'],'unknown')
