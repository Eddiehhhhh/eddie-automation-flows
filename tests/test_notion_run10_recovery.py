"""Exact run10 policy bridge; only synthetic data and actual filtered local Git."""
from copy import deepcopy
from pathlib import Path
import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch
import get_notion_plan_recovery as r
import get_notion_executor as e
import get_notion_outbox as o
import get_notion_journal_runtime as c
import get_notion_pipeline as q
import get_notion_live_transport as t
import test_notion_executor as ef
import test_notion_outbox as of
import test_notion_remote_journal as jf
import test_notion_journal_runtime as cf
import test_publication_provenance as pf
from test_notion_pipeline import FixtureTransport
from test_executor_live_adapter import DB


class Pins(unittest.TestCase):
    def proof(self):
        return {'anchor':deepcopy(r.RUN10_ANCHOR),'job_id':r.RUN10_JOB,
            'checkpoint_count':r.RUN10_CHECKPOINT_COUNT,
            'checkpoint':{'record_sha256':r.RUN10_CHECKPOINT,'controller_sha':r.RUN10_CONTROLLER},
            'get_step_record':deepcopy(r.RUN10_GET),'private_readback_required':True}

    def test_public_policy_and_plan_are_exact_independent_pins(self):
        c.provenance.validate_policy(r.RUN10_PUBLIC_POLICY,r.RUN10_PUBLIC_SHA)
        plan={'rules_sha256':r.RUN10_ENGINE_RULES,'records':[]}
        journal={'plan':deepcopy(plan),'plan_sha256':e.digest(plan)}
        self.assertFalse(r.allows_run10(plan,journal))
        with patch.object(r,'RUN10_PLAN',e.digest(plan)):
            self.assertTrue(r.allows_run10(plan,journal))
            self.assertFalse(r.allows_run10(plan,None))
            for bad in ({**journal,'plan_sha256':'0'*64},{**journal,'plan':{**plan,'records':[{}]}}):
                self.assertFalse(r.allows_run10(plan,bad))
            self.assertFalse(r.allows_run10({**plan,'rules_sha256':e.rules_sha256()},journal))

    def test_proof_pins_and_same_client_no_permission_fallback(self):
        client=object()
        with patch.object(c,'recover_anchor',return_value=self.proof()) as call:
            result=r.recover_run10_checkpoint(client,r.RUN10_RUN,r.RUN10_ATTEMPT)
        self.assertIs(call.call_args.args[0],client)
        self.assertEqual(call.call_args.kwargs['public_policy'],r.RUN10_PUBLIC_POLICY)
        self.assertEqual(result['recovery_anchor'],'exact_run10_acknowledged_create_v1')
        for key in self.proof():
            bad=self.proof();bad[key]=None
            with patch.object(c,'recover_anchor',return_value=bad),self.assertRaises(Exception):
                r.recover_run10_checkpoint(client,r.RUN10_RUN,r.RUN10_ATTEMPT)
        for run,attempt in ((r.RUN10_RUN,2),(r.RUN10_RUN+1,1),(True,1),(r.RUN10_RUN,True)):
            with patch.object(c,'recover_anchor') as call,self.assertRaises(c.CheckpointFailure):
                r.recover_run10_checkpoint(client,run,attempt)
            call.assert_not_called()
        failure=c.CheckpointFailure('journal_provenance_failed')
        with patch.object(c,'recover_anchor',side_effect=failure) as call,self.assertRaises(c.CheckpointFailure) as caught:
            r.recover_run10_checkpoint(client,r.RUN10_RUN,1)
        self.assertIs(caught.exception,failure);self.assertEqual(call.call_count,1)

    def test_discovery_reuses_shared_budget_only_exact_controller_mismatch(self):
        old=pf.policy();new=pf.policy(code_sha='9'*40);api=cf.LogsAPI(old,'');api.row['conclusion']='success'
        result={**self.proof(),'run_binding':c.provenance.run_binding(api.row),'latest_binding':c.provenance.run_binding(api.row),
            'recovery_anchor':'exact_run10_acknowledged_create_v1'}
        args={'public_policy':new,'approved_public_policy_sha256':c.provenance.a.sha(c.provenance.a.encoded(new)),'notion_policy_sha256':'d'*64}
        with patch.object(r,'RUN10_RUN',1001),patch.object(r,'recover_run10_checkpoint',return_value=result) as call:
            out=c.discover_anchor(api,**args)
        self.assertIs(call.call_args.args[0].client,api)
        self.assertEqual(out['provenance_reads'],call.call_args.args[0].requests)
        self.assertLessEqual(out['provenance_reads'],40)
        for reason in ('journal_provenance_failed','journal_provenance_record','journal_provenance_budget'):
            with patch.object(r,'RUN10_RUN',1001),patch.object(c,'recover_anchor',side_effect=c.CheckpointFailure(reason)),patch.object(r,'recover_run10_checkpoint') as call,self.assertRaises(c.CheckpointFailure):
                c.discover_anchor(api,**args)
            call.assert_not_called()
        seen=[]
        def exhaust(bounded,*args):
            seen.append(bounded)
            for _ in range(40):bounded.json('/actions/runs/1001')
        with patch.object(r,'RUN10_RUN',1001),patch.object(r,'recover_run10_checkpoint',side_effect=exhaust),self.assertRaisesRegex(c.CheckpointFailure,'journal_provenance_budget'):
            c.discover_anchor(api,**args)
        self.assertEqual(seen[0].requests,40);self.assertIs(seen[0].client,api)


class RealGitBridge(unittest.TestCase):
    def setUp(self):
        self.f=of.Tests(methodName='test_real_create_dequeues_atomically_and_repeat_does_not_redeliver')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.write('asset.bin',b'Unrelated blob must stay missing')
        self.ids=[of.N(i) for i in (1,2,3)]
        self.f.publication(dict(zip(self.ids,['First text','Second text','Third text'])))
        self.f.ingest()
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.remote=jf.Fixture(temp.name,seed=self.f.root)
        self.hydrate()
        self.durable=self.remote.store();self.store=o.EngineStore(self.durable)
        self.hold={'note_id':of.N(99),'page_id':'f'*32,'retry_allowed':False}
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(r,'HELD_NOTE_SHA256',e.digest(self.hold['note_id'])))
        self.stack.enter_context(patch.object(r,'HOLD_SHA256',e.digest(self.hold)))
        initial=deepcopy(self.f.store.value);initial['engine_state']['unresolved_by_id']={self.hold['note_id']:self.hold}
        self.durable.compare_and_set(self.durable.read()[1],initial)
        outer,sha=self.durable.read();self.pub=o.QueuePublication(self.remote.root,outer,sha)
        self.api=ef.API(self.store);inner,version=self.store.read()
        with patch.object(e,'rules_sha256',return_value=r.RUN10_ENGINE_RULES):
            self.plan=e.prepare(self.pub,inner,version,{ident:self.api.read_note(ident) for ident in self.ids},self.f.policy())
        self.plan_bytes=e.encoded(self.plan)
        read=self.api.read_note
        def interrupted(ident):
            if ident==self.ids[1] and ident in self.api.pages:raise OSError('Synthetic interrupted readback')
            return read(ident)
        with patch.object(e,'rules_sha256',return_value=r.RUN10_ENGINE_RULES),patch.object(self.api,'read_note',side_effect=interrupted):
            result=self.execute()
        self.assertEqual(result['status'],'blocked')
        outer,sha=self.durable.read();op=outer['engine_state']['journal']['operation']
        self.assertEqual((outer['engine_state']['journal']['index'],op['kind'],op['phase']),(1,'create','acknowledged'))
        self.assertIsNotNone(op['response']);self.assertEqual(self.writes(),2)
        # The fixture's source receipt already has the successor policy. Its old
        # queue envelope stands in for the separately authenticated predecessor.
        outer['policy_sha256']='b'*64
        self.durable.compare_and_set(sha,outer)
        self.before,self.sha=self.durable.read();self.anchor=deepcopy(self.durable.anchor)
        for key,value in {'RUN10_ANCHOR':self.anchor,'RUN10_PLAN':e.digest(self.plan),
                'RUN10_STATE_BYTES':len(e.encoded(self.before)),
                'RUN10_PUBLIC_POLICY':{**r.RUN10_PUBLIC_POLICY,'get_policy_sha256':'b'*64}}.items():
            self.stack.enter_context(patch.object(r,key,value))

    def hydrate(self):
        for path in [o.entry.FULL_RECEIPT]+['Raw/03 Get/notes/'+ident+'.md' for ident in self.ids]:
            jf.run(self.remote.root,'hash-object','-w','--stdin',data=(self.f.root/path).read_bytes())

    def fresh(self):
        anchor=deepcopy(self.durable.anchor)
        self.remote.root=self.remote.clone('fresh-'+str(len(self.remote.receipts)))
        self.hydrate();self.durable=self.remote.store(anchor);self.store=o.EngineStore(self.durable);self.api.store=self.store

    def writes(self):return sum(call[0]=='create' for call in self.api.calls)

    def execute(self):
        return e.execute(self.plan,e.digest(self.plan),self.pub,self.api,self.store,
            expected_store_sha=self.store.read()[1],writers_guard=lambda:deepcopy(self.f.policy()['old_writer_evidence']))

    def transition(self):
        self.assertTrue(r.validate_run10_state(self.before,self.sha))
        candidate=r.transition_run10_outer(self.before,self.sha,of.P)
        self.assertEqual([k for k in self.before if self.before[k]!=candidate[k]],['policy_sha256'])
        observed,sha=o.confirmed_compare_and_set(self.durable,self.sha,candidate)
        self.assertEqual((observed,sha),(candidate,o.digest(candidate)))
        self.assertFalse(r.validate_run10_state(observed,sha))
        return observed,sha

    def test_acknowledged_readback_then_third_create_and_fresh_noop(self):
        # Interrupt exactly after the new-policy checkpoint and before any
        # native read. Recover its public proof normally, then use a fresh clone.
        template=c.controller.template_bytes()
        if c.NOTION_STEP.encode() not in template:
            template+=b'\n      - name: Sync confirmed Get content to Notion\n        id: notion_sync\n        continue-on-error: true\n        run: python scripts/get_notion_runtime.py\n'
        with patch.object(c.controller,'template_bytes',return_value=template):
            policy=pf.policy();context=cf.context(policy);stream=io.StringIO()
            emitter=c.CheckpointEmitter(stream,context=context)
            self.durable.confirm=lambda receipt:self.remote.confirm(receipt) and emitter(receipt)
            before_calls=len(self.remote.calls);before_native=len(self.api.calls)
            outer,sha=self.transition()
            self.assertEqual(len(self.api.calls),before_native)
            proof=c.recover_anchor(cf.LogsAPI(policy,stream.getvalue()),run_id=1001,run_attempt=1,
                public_policy=policy,approved_public_policy_sha256=context['public_policy_sha256'],notion_policy_sha256=context['notion_policy_sha256'])
        self.assertNotIn('recovery_anchor',proof)
        self.assertEqual(proof['anchor'],self.durable.anchor)
        self.assertEqual(proof['checkpoint']['public_policy_sha256'],context['public_policy_sha256'])
        with self.assertRaises(Exception):self.remote.store(self.anchor).read()
        self.durable=self.remote.store(proof['anchor'])
        self.assertEqual(len(self.remote.calls),before_calls+1)
        self.assertEqual(e.encoded(outer['engine_state']),e.encoded(self.before['engine_state']))
        self.assertEqual(outer['pending_by_id'],self.before['pending_by_id'])
        self.fresh();outer,sha=self.durable.read();self.pub=o.QueuePublication(self.remote.root,outer,sha,resume=True)
        result=self.execute();self.assertEqual(result['status'],'verified',result)
        self.assertEqual(result['created'],2);self.assertEqual(result['write_calls'],1);self.assertEqual(self.writes(),3)
        self.assertEqual(self.api.pages[self.ids[1]]['page_id'],self.before['engine_state']['journal']['operation']['response'])
        self.fresh();outer,sha=self.durable.read();self.assertIsNone(outer['engine_state']['journal'])
        self.assertEqual(outer['engine_state']['unresolved_by_id'],self.before['engine_state']['unresolved_by_id'])
        self.assertEqual(len(outer['engine_state']['receipts']),3);self.assertEqual(outer['pending_by_id'],{})
        chain={'repository':o.p.REPOSITORY,'policy_sha256':of.P,'origin':self.f.origin,'after':self.f.last,'tip':self.f.last,'publications':[]}
        self.assertEqual(o.enqueue(self.remote.root,outer,sha,chain,o.digest(chain)),outer)
        with self.assertRaisesRegex(o.Blocked,'outbox_empty'):o.QueuePublication(self.remote.root,outer,sha)
        self.assertEqual(self.writes(),3)
        asset=jf.run(self.remote.bare,'rev-parse','main:asset.bin').decode().strip()
        self.assertTrue(e.git.OfflineGit(self.remote.root).local.run('cat-file','--batch-check',data=(asset+'\n').encode()).endswith(b' missing\n'))

    def test_current_pipeline_accepts_saved_old_plan_without_duplicate_create(self):
        outer,sha=self.transition();self.pub=o.QueuePublication(self.remote.root,outer,sha,resume=True)
        policy={'contract':q.CONTRACT,'rules_sha256':e.rules_sha256(),'data_source_id':ef.DS,
            'data_source_pin':t.native.sha(ef.DS),'database_pin':t.native.sha(DB),'title_property':'Title',
            'allow_apply':True,'allow_create':True,'allow_archive':True,'format_accepted':True,
            'initial_scope_sha256':None,'plan_authorization':'standing_bounded'}
        result=q.run(self.pub,self.store,FixtureTransport(self.api),policy,e.digest(policy),
            writers_guard=lambda:deepcopy(self.f.policy()['old_writer_evidence']),apply=True)
        self.assertEqual(result['status'],'executed',result)
        self.assertEqual(result['execution']['status'],'verified',result);self.assertEqual(self.writes(),3)

    def test_transition_wrong_hash_field_policy_or_second_attempt_is_rejected(self):
        for sha in ('0'*64,self.sha[:-1]+('0' if self.sha[-1]!='0' else '1')):
            with self.assertRaises(ValueError):r.transition_run10_outer(self.before,sha,of.P)
        for key in ('pending_by_id','rotation_order','engine_state','last_enqueued_publication'):
            bad=deepcopy(self.before);bad[key]={}
            with self.assertRaises(ValueError):r.transition_run10_outer(bad,e.digest(bad),of.P)
        for field in ('plan','response','hold','pending'):
            bad=deepcopy(self.before)
            if field=='plan':bad['engine_state']['journal']['plan']['rules_sha256']='0'*64
            elif field=='response':bad['engine_state']['journal']['operation']['response']='e'*32
            elif field=='hold':bad['engine_state']['unresolved_by_id'][self.hold['note_id']]['retry_allowed']=True
            else:bad['pending_by_id'].pop(self.ids[2])
            with self.assertRaises(ValueError):r.transition_run10_outer(bad,e.digest(bad),of.P)
        for policy in ('b'*64,'short',None,True):
            with self.assertRaises(Exception):r.transition_run10_outer(self.before,self.sha,policy)
        outer,sha=self.transition()
        with self.assertRaises(ValueError):r.transition_run10_outer(outer,sha,'b'*64)

    def test_cas_conflict_never_drops_pending_or_hold(self):
        candidate=r.transition_run10_outer(self.before,self.sha,of.P)
        changed=deepcopy(self.before);changed['rotation_order'].reverse()
        self.durable.compare_and_set(self.sha,changed)
        with self.assertRaises(Exception):o.confirmed_compare_and_set(self.durable,self.sha,candidate)
        self.assertEqual(self.durable.read()[0],changed);self.assertEqual(self.writes(),2)

    def test_original_plan_bytes_and_current_source_are_still_required(self):
        outer,sha=self.transition();self.pub=o.QueuePublication(self.remote.root,outer,sha,resume=True)
        self.remote.other_commit('Raw/03 Get/notes/'+self.ids[2]+'.md',of.md(self.ids[2],'Changed source'))
        self.fresh();outer,sha=self.durable.read()
        with self.assertRaisesRegex(e.Blocked,'source_changed'):o.QueuePublication(self.remote.root,outer,sha,resume=True)
        self.assertEqual(e.encoded(outer['engine_state']['journal']['plan']),self.plan_bytes)
        self.assertEqual(self.writes(),2)

    def test_unchanged_source_unrelated_commit_and_resume_under_new_anchor(self):
        self.transition();self.remote.other_commit();self.fresh()
        outer,sha=self.durable.read();self.pub=o.QueuePublication(self.remote.root,outer,sha,resume=True)
        self.assertEqual(self.execute()['status'],'verified');self.assertEqual(self.writes(),3)
        self.assertEqual(jf.run(self.remote.bare,'show','main:unrelated/new.md'),b'Concurrent change\n')

if __name__=='__main__':unittest.main()
