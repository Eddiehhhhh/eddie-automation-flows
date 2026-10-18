"""Pinned bridge tests: synthetic state only, no private content or network."""
from copy import deepcopy
from contextlib import ExitStack
import unittest
from unittest.mock import patch
import get_notion_run15_recovery as r
import get_notion_pipeline as q
import get_notion_journal_runtime as c
import get_notion_outbox as o
import test_notion_journal_runtime as cf
import test_publication_provenance as pf

class Tests(unittest.TestCase):
    def proof(self):
        return {'anchor':deepcopy(r.ANCHOR),'job_id':r.JOB,'checkpoint_count':15,
            'checkpoint':deepcopy(r.CHECKPOINT),'get_step_record':deepcopy(r.GET_RECORD),
            'private_readback_required':True}
    def test_original_public_policy_valid(self):
        c.provenance.validate_policy(r.PUBLIC_POLICY,r.PUBLIC_SHA)
    def test_checkpoint_exact_fields(self):
        client=object()
        with patch.object(c,'recover_anchor',return_value=self.proof()) as call:
            self.assertEqual(r.recover_checkpoint(client,r.RUN,1)['recovery_anchor'],r.TAG)
        self.assertIs(call.call_args.args[0],client)
        for key in self.proof():
            bad=self.proof();bad[key]=None
            with patch.object(c,'recover_anchor',return_value=bad),self.assertRaises(Exception):r.recover_checkpoint(client,r.RUN,1)
        for run,attempt in ((r.RUN+1,1),(r.RUN,2),(True,1),(r.RUN,True)):
            with patch.object(c,'recover_anchor') as call,self.assertRaises(Exception):r.recover_checkpoint(client,run,attempt)
            call.assert_not_called()
    def fixture(self):
        plan={'rules_sha256':r.ENGINE_RULES,'records':[{'action':'create'}]}
        op={'kind':'create','phase':'acknowledged','response':'1'*32}
        return {'policy_sha256':r.PUBLIC_POLICY['get_policy_sha256'],
            'engine_state':{'journal':{'index':0,'operation':op,'plan':plan,'plan_sha256':o.digest(plan)},
                'receipts':{str(i):{} for i in range(4)},'unresolved_by_id':{'held':{}}},
            'pending_by_id':{str(i):{} for i in range(7)},
            'png_assets':{'uploads':{str(i):{'state':{'phase':'uploaded_verified','upload_id':str(i)}} for i in range(2)},'attachments':{}}}
    def pins(self,s):
        stack=ExitStack();self.addCleanup(stack.close);j=s['engine_state']['journal']
        for key,value in {'PLAN_SHA':o.digest(j['plan']),'OP_SHA':o.digest(j['operation']),
            'STATE_BYTES':len(o.encoded(s)),'ANCHOR':{**r.ANCHOR,'state_sha256':o.digest(s)}}.items():stack.enter_context(patch.object(r,key,value))
        stack.enter_context(patch.object(o,'check_state'))
    def test_only_outer_policy_changes_and_new_checkpoint_cannot_reapply(self):
        s=self.fixture();self.pins(s);sha=o.digest(s)
        self.assertTrue(r.validate_state(s,sha));changed=r.transition_outer(s,sha,'f'*64)
        self.assertEqual([k for k in s if not o.e.same(s[k],changed[k])],['policy_sha256'])
        self.assertFalse(r.validate_state(changed,o.digest(changed)))
        with self.assertRaisesRegex(ValueError,'state_invalid'):r.transition_outer(changed,o.digest(changed),'e'*64)
        self.assertTrue(r.allows(changed['engine_state']['journal']['plan'],changed['engine_state']['journal']))
    def test_saved_response_upload_receipt_hold_pending_changes_reject(self):
        s=self.fixture();self.pins(s)
        changes=[lambda x:x['engine_state']['journal']['operation'].update(response='2'*32),
          lambda x:x['png_assets']['uploads']['0']['state'].update(upload_id='changed'),
          lambda x:x['engine_state']['receipts'].pop('0'),
          lambda x:x['engine_state']['unresolved_by_id'].clear(),
          lambda x:x['pending_by_id'].pop('0'),
          lambda x:x['engine_state']['journal'].update(index=1),
          lambda x:x['engine_state']['journal']['operation'].update(phase='prepared')]
        for change in changes:
            bad=deepcopy(s);change(bad)
            self.assertFalse(r.validate_state(bad,o.digest(bad)))
            with self.assertRaisesRegex(ValueError,'state_invalid'):r.transition_outer(bad,o.digest(bad),'f'*64)
    def test_no_prepare_exception_or_modified_plan(self):
        s=self.fixture();self.pins(s);j=s['engine_state']['journal'];p=j['plan']
        self.assertTrue(r.allows(p,j));self.assertFalse(r.allows(p,None))
        bad=deepcopy(p);bad['records'][0]['action']='noop'
        self.assertFalse(r.allows(bad,{**j,'plan':bad,'plan_sha256':o.digest(bad)}))
    def test_shared_bounded_discovery_and_noncontroller_failure_stop(self):
        old=pf.policy();new=pf.policy(code_sha='9'*40);api=cf.LogsAPI(old,'');api.row['conclusion']='success'
        result={**self.proof(),'run_binding':c.provenance.run_binding(api.row),'latest_binding':c.provenance.run_binding(api.row),'recovery_anchor':r.TAG}
        args={'public_policy':new,'approved_public_policy_sha256':c.provenance.a.sha(c.provenance.a.encoded(new)),'notion_policy_sha256':'d'*64}
        with patch.object(r,'RUN',1001),patch.object(r,'recover_checkpoint',return_value=result) as call:
            out=c.discover_anchor(api,**args)
        self.assertIs(call.call_args.args[0].client,api);self.assertEqual(out['recovery_anchor'],r.TAG)
        for reason in ('journal_provenance_failed','journal_provenance_record','journal_provenance_budget'):
            with patch.object(r,'RUN',1001),patch.object(c,'recover_anchor',side_effect=c.CheckpointFailure(reason)),patch.object(r,'recover_checkpoint') as call,self.assertRaises(c.CheckpointFailure):c.discover_anchor(api,**args)
            call.assert_not_called()

if __name__=='__main__':unittest.main()

class RealSavedPage(unittest.TestCase):
    def test_new_exception_resumes_saved_page_without_recreate(self):
        import test_notion_run10_recovery as old
        import get_notion_executor as e
        import get_notion_pipeline as q
        import get_notion_plan_recovery as prior
        import get_notion_live_transport as t
        import test_notion_executor as ef
        from test_executor_live_adapter import DB
        from test_notion_pipeline import FixtureTransport
        f=old.RealGitBridge();f.setUp();self.addCleanup(f.doCleanups)
        outer,sha=f.transition();f.fresh();outer,sha=f.durable.read()
        f.pub=o.QueuePublication(f.remote.root,outer,sha,resume=True)
        policy={'contract':q.CONTRACT,'rules_sha256':e.rules_sha256(),'data_source_id':ef.DS,
            'data_source_pin':t.native.sha(ef.DS),'database_pin':t.native.sha(DB),'title_property':'Title',
            'allow_apply':True,'allow_create':True,'allow_archive':True,'format_accepted':True,
            'initial_scope_sha256':None,'plan_authorization':'standing_bounded'}
        saved=outer['engine_state']['journal']['operation']['response'];writes=f.writes()
        with patch.object(r,'PLAN_SHA',e.digest(f.plan)),patch.object(r,'ENGINE_RULES',f.plan['rules_sha256']),patch.object(prior,'allows_run10',return_value=False):
            result=q.run(f.pub,f.store,FixtureTransport(f.api),policy,e.digest(policy),
                writers_guard=lambda:deepcopy(f.f.policy()['old_writer_evidence']),apply=True)
        self.assertIn('execution',result,result);self.assertEqual(result['execution']['status'],'verified',result)
        self.assertEqual(f.writes(),writes+1)
        self.assertEqual(f.api.pages[f.ids[1]]['page_id'],saved)
        f.fresh();state,_=f.durable.read();self.assertIsNone(state['engine_state']['journal'])
        self.assertEqual(state['engine_state']['unresolved_by_id'],f.before['engine_state']['unresolved_by_id'])
