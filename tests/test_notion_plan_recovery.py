"""Exact old-plan exception, public proof pins and real-Git interrupted resume."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_plan_recovery as recovery
import get_notion_executor as e
import get_notion_outbox as o
import get_notion_journal_runtime as checkpoints
import test_notion_executor as ef
import test_notion_remote_journal as jf
import test_notion_journal_runtime as cf
import test_publication_provenance as pf


class RecoveryPins(unittest.TestCase):
    def test_old_public_policy_is_independently_fixed(self):
        p=checkpoints.provenance
        self.assertEqual(p.a.sha(p.a.encoded(recovery.OLD_PUBLIC_POLICY)),recovery.OLD_PUBLIC_SHA)
        p.validate_policy(recovery.OLD_PUBLIC_POLICY,recovery.OLD_PUBLIC_SHA)

    def test_plan_requires_exact_persistent_copy_not_a_new_prepare(self):
        plan={'rules_sha256':recovery.OLD_ENGINE_RULES,'records':[]}
        journal={'plan':deepcopy(plan),'plan_sha256':e.digest(plan)}
        with patch.object(recovery,'OLD_PLAN',e.digest(plan)):
            self.assertTrue(recovery.allows(plan,journal))
            self.assertFalse(recovery.allows(plan,None))
            self.assertFalse(recovery.allows({**plan,'records':[{}]},journal))
            self.assertFalse(recovery.allows(plan,{**journal,'plan_sha256':'0'*64}))
            self.assertFalse(recovery.allows(plan,{**journal,'plan':{**plan,'rules_sha256':'0'*64}}))
        self.assertFalse(recovery.allows(plan,journal))

    def proof(self):
        return {'anchor':deepcopy(recovery.OLD_ANCHOR),'job_id':recovery.OLD_JOB,
            'checkpoint_count':recovery.OLD_CHECKPOINT_COUNT,'checkpoint':{'record_sha256':recovery.OLD_CHECKPOINT,
                'controller_sha':recovery.OLD_CONTROLLER},'get_step_record':deepcopy(recovery.OLD_GET),
            'private_readback_required':True}

    def test_checkpoint_pin_and_same_injected_client(self):
        client=object();proof=self.proof()
        with patch.object(checkpoints,'recover_anchor',return_value=proof) as call:
            result=recovery.recover_checkpoint(client,recovery.OLD_RUN,1)
        self.assertEqual(result['anchor'],recovery.OLD_ANCHOR)
        self.assertIs(call.call_args.args[0],client)
        self.assertEqual(call.call_args.kwargs['approved_public_policy_sha256'],recovery.OLD_PUBLIC_SHA)
        for key in ('anchor','checkpoint','get_step_record','job_id','checkpoint_count','private_readback_required'):
            bad=self.proof();bad[key]={} if isinstance(bad[key],dict) else None
            with patch.object(checkpoints,'recover_anchor',return_value=bad),self.assertRaises(checkpoints.CheckpointFailure):
                recovery.recover_checkpoint(client,recovery.OLD_RUN,1)
        with patch.object(checkpoints,'recover_anchor') as call,self.assertRaises(checkpoints.CheckpointFailure):
            recovery.recover_checkpoint(client,recovery.OLD_RUN,2)
        call.assert_not_called()

    def test_permission_failure_is_never_converted_or_retried(self):
        failure=checkpoints.CheckpointFailure('journal_provenance_failed')
        with patch.object(checkpoints,'recover_anchor',side_effect=failure) as call:
            with self.assertRaises(checkpoints.CheckpointFailure) as caught:
                recovery.recover_checkpoint(object(),recovery.OLD_RUN,1)
        self.assertIs(caught.exception,failure);self.assertEqual(call.call_count,1)

    def test_discovery_old_branch_only_on_exact_controller_mismatch(self):
        old=pf.policy();new=pf.policy(code_sha='9'*40)
        api=cf.LogsAPI(old,'');api.row['conclusion']='success'
        returned={'anchor':deepcopy(recovery.OLD_ANCHOR),'run_binding':checkpoints.provenance.run_binding(api.row),'latest_binding':checkpoints.provenance.run_binding(api.row),
            'recovery_anchor':'exact_initial_transaction_v1','private_readback_required':True}
        with patch.object(recovery,'OLD_RUN',1001),patch.object(recovery,'recover_checkpoint',return_value=returned) as call:
            out=checkpoints.discover_anchor(api,public_policy=new,
                approved_public_policy_sha256=checkpoints.provenance.a.sha(checkpoints.provenance.a.encoded(new)),notion_policy_sha256='d'*64)
        self.assertEqual(out['anchor'],recovery.OLD_ANCHOR);self.assertEqual(call.call_count,1)
        self.assertIs(call.call_args.args[0].client,api)
        self.assertLessEqual(out['provenance_reads'],40)
        for reason in ('journal_provenance_failed','journal_provenance_record','journal_provenance_budget'):
            with patch.object(recovery,'OLD_RUN',1001),patch.object(checkpoints,'recover_anchor',side_effect=checkpoints.CheckpointFailure(reason)),patch.object(recovery,'recover_checkpoint') as call:
                with self.assertRaises(checkpoints.CheckpointFailure):
                    checkpoints.discover_anchor(api,public_policy=new,
                        approved_public_policy_sha256=checkpoints.provenance.a.sha(checkpoints.provenance.a.encoded(new)),notion_policy_sha256='d'*64)
            call.assert_not_called()


class RealGitResume(unittest.TestCase):
    def setUp(self):
        self.f=ef.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        (self.f.repo/'asset.bin').write_bytes(b'inherited asset stays absent')
        self.f.parent=self.f.commit('asset')
        self.second=self.f.source_path.parent/'b.md';self.second.write_bytes(ef.md(ef.N2,'Second reviewed text'))
        self.f.publish('Fresh reviewed text')
        self.remote=jf.Fixture(self.f.root,seed=self.f.repo)
        data=self.f.source_path.read_bytes()
        jf.run(self.remote.root,'hash-object','-w','--stdin',data=data)
        jf.run(self.remote.root,'hash-object','-w','--stdin',data=self.second.read_bytes())
        commit=self.f.publication.descriptor['commit']
        self.cohort={'commit':commit,'records':deepcopy(self.f.publication.descriptor['records'])}
        self.publication=e.GitPublication(self.remote.root,commit,commit,commit,
            initial_cohort=self.cohort,expected_initial_sha=e.digest(self.cohort))
        self.durable=self.remote.store();self.store=o.EngineStore(self.durable)
        self.api=ef.API(self.store)
        roots=self.api.make(ef.N,[ef.paragraph('User prefix')]+[ef.paragraph('Old '+str(i)) for i in range(10)]+[ef.paragraph('User tail')])
        self.scope={ef.N:{'page_id':self.api.pages[ef.N]['page_id'],
            'snapshot_sha256':e.target_sha(self.api.pages[ef.N],ef.N,ef.DS),'managed_block_ids':roots[1:-1]}}
        second_roots=self.api.make(ef.N2,[ef.paragraph('Second old text')])
        self.scope[ef.N2]={'page_id':self.api.pages[ef.N2]['page_id'],
            'snapshot_sha256':e.target_sha(self.api.pages[ef.N2],ef.N2,ef.DS),'managed_block_ids':second_roots}
        state,sha=self.store.read()
        with patch.object(e,'rules_sha256',return_value=recovery.OLD_ENGINE_RULES):
            self.plan=e.prepare(self.publication,state,sha,{ident:self.api.read_note(ident) for ident in (ef.N,ef.N2)},self.f.policy,
                initial_scope=self.scope,expected_scope_sha=e.digest(self.scope))
        self.plan_bytes=e.encoded(self.plan);self.before=deepcopy(self.api.pages[ef.N])

    def execute(self):
        return e.execute(self.plan,e.digest(self.plan),self.publication,self.api,self.store,
            expected_store_sha=self.store.read()[1],writers_guard=lambda:deepcopy(self.f.policy['old_writer_evidence']))

    def fresh(self):
        anchor=deepcopy(self.durable.anchor)
        self.remote.root=self.remote.clone('resume-'+str(len(self.remote.receipts)))
        jf.run(self.remote.root,'hash-object','-w','--stdin',data=self.f.source_path.read_bytes())
        jf.run(self.remote.root,'hash-object','-w','--stdin',data=self.second.read_bytes())
        commit=self.publication.descriptor['commit']
        self.publication=e.GitPublication(self.remote.root,commit,commit,commit,
            initial_cohort=self.cohort,expected_initial_sha=e.digest(self.cohort))
        self.durable=self.remote.store(anchor);self.store=o.EngineStore(self.durable);self.api.store=self.store

    def test_prepared_eighth_new_code_resume_checkpoint_interrupt_and_new_plan(self):
        real=self.store.compare_and_set
        def stop(expected,value):
            op=(value.get('journal') or {}).get('operation')
            if op and op['kind']=='archive' and op['phase']=='in_flight' and op['archive_index']==8:
                raise OSError('fixture stops before dispatch')
            return real(expected,value)
        with patch.object(e,'rules_sha256',return_value=recovery.OLD_ENGINE_RULES),patch.object(self.store,'compare_and_set',side_effect=stop):
            first=self.execute()
        self.assertEqual(first['reason'],'journal_failed')
        state,_=self.store.read();self.assertEqual(state['journal']['operation']['phase'],'prepared')
        self.assertEqual(state['journal']['operation']['archive_index'],8)
        self.assertEqual(len([x for x in self.api.calls if x[0]=='append']),1)
        self.assertEqual(len([x for x in self.api.calls if x[0]=='archive']),8)
        self.fresh()
        with patch.object(recovery,'OLD_PLAN',e.digest(self.plan)):
            self.api.throw='archive_after';interrupted=self.execute()
            self.assertEqual(interrupted['reason'],'uncertain_archive')
            self.assertEqual(e.encoded(self.store.read()[0]['journal']['plan']),self.plan_bytes)
            self.fresh();self.api.throw=None;final=self.execute()
            self.assertEqual(final['status'],'verified',final)
        state,sha=self.store.read();self.assertIsNone(state['journal'])
        self.assertEqual(len([x for x in self.api.calls if x[0]=='append']),2)
        self.assertEqual(len([x for x in self.api.calls if x[0]=='archive']),11)
        self.assertEqual(self.api.pages[ef.N]['properties_raw'],self.before['properties_raw'])
        self.assertEqual(self.api.pages[ef.N]['recursive_blocks'][0],self.before['recursive_blocks'][0])
        self.assertEqual(self.api.pages[ef.N]['recursive_blocks'][-1],self.before['recursive_blocks'][-1])
        fresh=e.prepare(self.publication,state,sha,{ident:self.api.read_note(ident) for ident in (ef.N,ef.N2)},self.f.policy)
        self.assertEqual(fresh['rules_sha256'],e.rules_sha256());self.assertEqual(fresh['records'],[])
        self.assertFalse(recovery.allows(self.plan,state['journal']))
        asset=jf.run(self.remote.bare,'rev-parse','main:asset.bin').decode().strip()
        self.assertNotEqual(self.publication.repo.local.command('cat-file','-e',asset).returncode,0)

    def test_new_plan_cannot_borrow_exact_prior_rules_without_journal(self):
        with patch.object(recovery,'OLD_PLAN',e.digest(self.plan)):
            out=self.execute()
        self.assertEqual(out['reason'],'rules_changed');self.assertFalse([x for x in self.api.calls if x[0]!='read'])

    def test_modified_payload_with_rehashed_journal_is_not_the_approved_plan(self):
        old=deepcopy(self.plan);self.plan['records'][0]['request_blocks'][0]['payload']['rich_text'][0]['text']['content']='Different'
        value,sha=self.store.read();value['journal']={'plan':self.plan,'plan_sha256':e.digest(self.plan),'index':0,'operation':None}
        self.store.compare_and_set(sha,value)
        with patch.object(recovery,'OLD_PLAN',e.digest(old)):
            out=self.execute()
        self.assertEqual(out['reason'],'rules_changed');self.assertFalse([x for x in self.api.calls if x[0]!='read'])


if __name__=='__main__':unittest.main()
