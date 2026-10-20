"""Actual public proof replay; synthetic private journals only in this suite."""
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import base64
import hashlib
import json
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_run53_recovery as r
import get_notion_journal_runtime as c
import get_notion_outbox as o
import get_notion_executor as e
import get_notion_create_batches as b
import get_notion_pipeline as q
import get_notion_runtime as runtime
import get_recurring_policy_recovery as recovery
import get_private_preview_retention as retention

FIXTURE=Path(__file__).parent/'fixtures/notion-run53-public-proof.json'
FIXTURE_SHA='5898227be9aeb2c0fc05fc8988da9d5d8a494391b1900a749410ae38f42efa1d'


class Public:
    def __init__(self):
        raw=FIXTURE.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==FIXTURE_SHA
        self.data=json.loads(raw)
        self.run=self.data['run'];self.attempt=self.data['attempt']
        self.jobs=self.data['jobs'];self.lines=self.data['marker_lines'];self.calls=[]
    def json(self,path):
        self.calls.append(path); root='/actions/runs/'+str(r.RUN)
        if path=='':return deepcopy(self.run['repository'])
        if path==root:return deepcopy(self.run)
        if path==root+'/attempts/2':return deepcopy(self.attempt)
        if path==root+'/attempts/2/jobs?per_page=100&page=1':return deepcopy(self.jobs)
        if path=='/actions/workflows/'+str(r.PUBLIC_POLICY['workflow_id']):
            return {'id':r.PUBLIC_POLICY['workflow_id'],'path':r.PUBLIC_POLICY['workflow_path']}
        if path=='/actions/workflows/'+str(r.PUBLIC_POLICY['workflow_id'])+'/runs?per_page=30&page=1':
            return {'total_count':1,'workflow_runs':[deepcopy(self.run)]}
        if path=='/contents/'+r.PUBLIC_POLICY['workflow_path']+'?ref='+r.GET_RECORD['controller_sha']:
            raw=self.data['controller'].encode()
            return {'type':'file','path':r.PUBLIC_POLICY['workflow_path'],'encoding':'base64',
                    'size':len(raw),'sha':retention.oid(raw),'content':base64.b64encode(raw).decode()}
        raise AssertionError('unexpected public fixture route: '+path)
    def logs(self,job):
        assert job==r.JOB
        self.calls.append('logs:'+str(job))
        return ('\n'.join(self.lines)+'\n').encode()


def future_policy():
    p=deepcopy(r.PUBLIC_POLICY)
    p.update(code_sha='9'*40,rules_sha256=__import__('get_recurring_entry').rules_sha256(),
             get_policy_sha256=recovery.current_get_policy(),controller_template_sha256=c.controller.template_sha256())
    return p


class PublicProof(unittest.TestCase):
    def setUp(self):
        self.activation=patch.object(r,'ACTIVATED',True);self.activation.start();self.addCleanup(self.activation.stop)
    def test_real_public_nine_checkpoint_proof_and_directional_clock(self):
        result=r.recover_checkpoint(Public(),r.RUN,r.ATTEMPT)
        self.assertEqual(result['anchor'],r.ANCHOR)
        self.assertEqual(result['checkpoint'],r.CHECKPOINT)
        self.assertEqual(result['checkpoint_count'],9)
        self.assertEqual(result['clock_witness'],{'attempt':r.ATTEMPT_CLOCK,'alias':r.ALIAS_CLOCK})
        self.assertTrue(result['private_readback_required'])
        self.assertFalse(result['get_run_success_claim'])
        self.assertIsNone(r.clock_witness(r.ALIAS_CLOCK,r.ATTEMPT_CLOCK))
        self.assertIsNone(r.clock_witness(c.provenance.run_binding(r.ATTEMPT_CLOCK)|{'run_started_at':None},r.ALIAS_CLOCK))
    def test_real_public_discovery_crosses_only_pinned_old_controller(self):
        policy=future_policy()
        result=c.discover_anchor(Public(),public_policy=policy,
            approved_public_policy_sha256=c.provenance.a.sha(c.provenance.a.encoded(policy)),notion_policy_sha256='6'*64)
        self.assertEqual(result['anchor'],r.ANCHOR)
        self.assertEqual(result['recovery_anchor'],r.TAG)
        self.assertLessEqual(result['provenance_reads'],40)
    def test_canonical_publication_uses_same_clock_exception_and_existing_receipt(self):
        api=Public();result=c.provenance.verified_candidate(api,api.run,future_policy())
        self.assertEqual(result['anchor'],{'commit':r.GET_RECORD['receipt_commit'],'receipt_sha256':r.GET_RECORD['receipt_sha256']})
        self.assertEqual(result['receipt_sequence'],2)
        self.assertEqual(result['clock_witness'],{'attempt':r.ATTEMPT_CLOCK,'alias':r.ALIAS_CLOCK})
    def test_all_raw_clock_fields_mandatory_in_both_directions(self):
        for key in r.ATTEMPT_CLOCK:
            for side in ('attempt','alias'):
                a,z=deepcopy(r.ATTEMPT_CLOCK),deepcopy(r.ALIAS_CLOCK)
                value=a if side=='attempt' else z
                value.pop(key)
                self.assertIsNone(r.clock_witness(a,z),(side,key))
                value[key]='changed'
                self.assertIsNone(r.clock_witness(a,z),(side,key))
        with patch.object(r,'ACTIVATED',False):
            self.assertIsNone(r.clock_witness(r.ATTEMPT_CLOCK,r.ALIAS_CLOCK))
            with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(Public(),r.RUN,2)
    def test_changed_raw_endpoint_or_original_reread_fails_both_verifiers(self):
        for field,value in (('created_at','2026-10-20T06:45:29Z'),('updated_at','2026-10-20T07:06:14Z'),
                ('run_started_at','2026-10-20T06:45:28Z'),('run_attempt',3),('event','workflow_dispatch')):
            for endpoint in ('attempt','alias','reread'):
                for canonical in (False,True):
                    with self.subTest(field=field,endpoint=endpoint,canonical=canonical):
                        api=Public();read=api.json;seen=0
                        def drift(path):
                            nonlocal seen
                            result=read(path)
                            if path.endswith('/attempts/2'):
                                seen+=1
                                if endpoint=='attempt' or endpoint=='reread' and seen==2:result[field]=value
                            if endpoint=='alias' and path=='/actions/runs/'+str(r.RUN):result[field]=value
                            return result
                        api.json=drift
                        with self.assertRaises(Exception):
                            if canonical:c.provenance.verified_candidate(api,api.run,future_policy())
                            else:r.recover_checkpoint(api,r.RUN,2)
    def test_discovery_requires_both_checked_clock_projections(self):
        result=r.recover_checkpoint(Public(),r.RUN,2)
        policy=future_policy()
        args={'public_policy':policy,'approved_public_policy_sha256':c.provenance.a.sha(c.provenance.a.encoded(policy)),
              'notion_policy_sha256':'6'*64}
        for mutation in ('missing_witness','missing_started','wrong_direction','changed_projection'):
            bad=deepcopy(result)
            if mutation=='missing_witness':bad.pop('clock_witness')
            if mutation=='missing_started':bad['run_binding'].pop('run_started_at')
            if mutation=='wrong_direction':bad['clock_witness']={'attempt':r.ALIAS_CLOCK,'alias':r.ATTEMPT_CLOCK}
            if mutation=='changed_projection':bad['clock_witness']['alias']['updated_at']='2026-10-20T07:06:14Z'
            with patch.object(r,'recover_checkpoint',return_value=bad),self.assertRaises(c.CheckpointFailure):
                c.discover_anchor(Public(),**args)

    def test_general_one_second_tolerance_not_widened(self):
        a=deepcopy(r.ATTEMPT_CLOCK);z=deepcopy(r.ALIAS_CLOCK)
        a['id']=z['id']=1001
        with self.assertRaises(c.CheckpointFailure):c._same_run(a,z,cross_endpoint=True)
    def test_exact_job_record_count_checkpoint_and_get_record_are_pinned(self):
        base=r.recover_checkpoint(Public(),r.RUN,2)
        for key in ('anchor','job_id','job_sha256','checkpoint_count','checkpoint','get_step_record','private_readback_required','clock_witness'):
            bad=deepcopy(base);bad[key]=None
            with patch.object(c,'recover_anchor',return_value=bad),self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(object(),r.RUN,2)
    def test_success_gates_not_relaxed(self):
        for target in ('attempt','job','get_step'):
            api=Public()
            if target=='attempt':api.attempt['conclusion']='failure'
            elif target=='job':api.jobs['jobs'][0]['conclusion']='failure'
            else:
                next(x for x in api.jobs['jobs'][0]['steps'] if x['name']==r.PUBLIC_POLICY['publication_step_name'])['conclusion']='failure'
            with self.assertRaises(Exception):r.recover_checkpoint(api,r.RUN,2)
            with self.assertRaises(Exception):c.provenance.verified_candidate(api,api.run,future_policy())


class BridgeFlow(unittest.TestCase):
    """Real codec/source/engine, three synthetic batches and one verified prefix."""
    def setUp(self):
        import test_notion_runtime as rt
        import test_notion_create_batches as batches
        import test_confirmed_source as source
        self.rt=rt;self.batches=batches;self.source=source
        self.case=rt.RuntimeTests(methodName='test_zero_changes_queue_stays_empty')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(r,'ACTIVATED',True))
        self.stack.enter_context(patch.object(recovery,'current_get_policy',return_value=source.POLICY))
        self.ident=source.N(8)
        self.case.f.publication({self.ident:'Synthetic old immutable long-page source'})
        self.local=self.case.f.clone(shallow=True)
        self.case.api=batches.API(self.case.adapter)
        self.reset_transport()
        with patch.object(e.p,'render_markdown',return_value=(batches.rendered(268,False),{},[])),\
                patch.object(e.creation_properties,'eligible',return_value=False),\
                patch.object(b,'resources',side_effect=lambda api,store,value,row,index,**kw:index==0):
            out=self.case.invoke(self.local,True)
        self.assertEqual(out['execution']['reason'],'batch_boundary_yield',out)
        state,sha=self.case.store.read()
        journal=state['engine_state']['journal']
        journal['plan']['rules_sha256']='f'*64
        journal['plan_sha256']=e.digest(journal['plan'])
        state['policy_sha256']='b'*64
        self.case.store.compare_and_set(sha,state)
        self.before,self.sha=self.case.store.read()
        self.plan=deepcopy(journal['plan']);self.old_plan_bytes=e.encoded(self.plan)
        op=journal['operation'];raw=o.encoded(self.before)
        for key,value in {'PUBLIC_POLICY':{**r.PUBLIC_POLICY,'get_policy_sha256':'b'*64},
                'ANCHOR':{**r.ANCHOR,'state_sha256':self.sha,'state_blob':retention.oid(raw)},
                'STATE_BYTES':len(raw),'PLAN_SHA':e.digest(self.plan),'PLAN_BYTES':len(e.encoded(self.plan)),
                'OP_SHA':e.digest(op),'ENGINE_RULES':'f'*64,'VERIFIED_BATCHES':1,'TOTAL_BATCHES':3,
                'PREFIX_SHA':e.digest(op['batches']),'PAGE_SHA':e.digest(op['page_id']),
                'PROPERTIES_SHA':op['properties_sha256']}.items():
            self.stack.enter_context(patch.object(r,key,value))
        self.saved_page=op['page_id']
    def reset_transport(self):
        self.case.request=self.rt.FixtureTransport(self.case.api)
        self.case.request.fixture=self.batches.PagedOfficialFixture(self.case.api)
    def transition(self):
        candidate=r.transition_outer(self.before,self.sha,recovery.current_get_policy())
        return o.confirmed_compare_and_set(self.case.store,self.sha,candidate)
    def invoke(self):
        self.reset_transport()
        return self.case.invoke(self.local,True)
    def writes(self):return [x[0] for x in self.case.api.calls if x[0]!='read']
    def test_transition_changes_one_field_and_resume_only_remaining_appends_then_idle(self):
        self.assertTrue(r.validate_state(self.before,self.sha))
        after,_=self.transition()
        self.assertEqual([k for k in after if after[k]!=self.before[k]],['policy_sha256'])
        self.assertEqual(e.encoded(after['engine_state']['journal']['plan']),self.old_plan_bytes)
        self.assertFalse(r.validate_state(after,o.digest(after)))
        result=self.invoke()
        self.assertEqual(result['execution']['status'],'verified',result)
        self.assertEqual(self.writes(),['create','append','append'])
        self.assertEqual(self.case.api.pages[self.ident]['page_id'],self.saved_page)
        final,_=self.case.store.read();self.assertIsNone(final['engine_state']['journal'])
        self.assertEqual(final['pending_by_id'],{})
        self.assertNotIn(self.ident,final['engine_state'].get('property_receipts',{}))
        self.assertEqual(final['engine_state']['receipts'][self.ident]['creation_plan_sha256'],r.PLAN_SHA)
        for _ in range(2):self.assertEqual(self.invoke()['status'],'queue_empty')
        self.assertEqual(self.writes(),['create','append','append'])
    def test_terminal_checkpoint_interruption_clears_without_any_duplicate_effect(self):
        self.transition();save=self.case.store.compare_and_set;interrupted=False
        def stop(sha,value):
            nonlocal interrupted
            result=save(sha,value)
            j=value['engine_state']['journal']
            if not interrupted and j is not None and j['index']==1 and j['operation'] is None:
                interrupted=True
                raise TimeoutError('synthetic interruption after terminal checkpoint')
            return result
        with patch.object(self.case.store,'compare_and_set',side_effect=stop):first=self.invoke()
        self.assertTrue(interrupted,first)
        state,_=self.case.store.read();j=state['engine_state']['journal']
        self.assertTrue(r.allows(j['plan'],j))
        count=len(self.writes());again=self.invoke()
        self.assertEqual(again['execution']['status'],'verified',again)
        self.assertEqual(len(self.writes()),count)
        self.assertIsNone(self.case.store.read()[0]['engine_state']['journal'])
    def test_unknown_append_is_never_blindly_duplicated(self):
        self.transition();self.case.api.throw='append_before'
        result=self.invoke();self.assertEqual(result['execution']['reason'],'uncertain_append',result)
        self.case.api.throw=None
        for _ in range(2):
            result=self.invoke();self.assertEqual(result['execution']['reason'],'uncertain_append',result)
        self.assertEqual(self.writes(),['create','append'])
    def test_unknown_applied_append_is_reconciled_not_duplicated(self):
        self.transition();self.case.api.throw='append_after'
        first=self.invoke();self.assertEqual(first['execution']['reason'],'uncertain_append',first)
        self.case.api.throw=None
        result=self.invoke();self.assertEqual(result['execution']['status'],'verified',result)
        self.assertEqual(self.writes(),['create','append','append'])
    def test_modified_private_pin_plan_prefix_page_and_state_fail_closed(self):
        for key,bad in (('STATE_BYTES',r.STATE_BYTES+1),('OP_SHA','e'*64),('PAGE_SHA','e'*64),
                ('PROPERTIES_SHA','e'*64),('PREFIX_SHA','e'*64),('PLAN_BYTES',r.PLAN_BYTES+1),
                ('ENGINE_RULES','e'*64),('VERIFIED_BATCHES',2)):
            with patch.object(r,key,bad):self.assertFalse(r.validate_state(self.before,self.sha),key)
        with patch.dict(r.ANCHOR,{'state_blob':'0'*40}):self.assertFalse(r.validate_state(self.before,self.sha))
        with patch.object(r,'ACTIVATED',False):self.assertFalse(r.validate_state(self.before,self.sha))
        self.assertFalse(r.allows(self.plan,None))
        changed=deepcopy(self.before);changed['engine_state']['journal']['index']=1
        self.assertFalse(r.validate_state(changed,o.digest(changed)))
        for value in ('e'*64,'b'*64):
            with self.assertRaises(o.Blocked):r.transition_outer(self.before,self.sha,value)
        self.assertEqual(self.writes(),['create'])
    def test_source_or_human_edit_stops_before_remaining_writes(self):
        self.transition()
        self.case.api.pages[self.ident]['recursive_blocks'][0]['payload']['rich_text'][0]['text']['content']='Human edit'
        result=self.invoke();self.assertEqual(result['execution']['status'],'blocked',result)
        self.assertEqual(self.writes(),['create'])
    def test_new_plan_after_drain_restores_creation_metadata_scope(self):
        self.transition();self.assertEqual(self.invoke()['execution']['status'],'verified')
        import test_notion_properties as props
        next_id=self.source.N(9)
        with patch.object(self.source,'md',return_value=props.source(next_id)):
            self.case.f.publication({next_id:'Synthetic next metadata-rich note'})
        self.local=self.case.f.clone(shallow=True)
        with patch.object(e.p,'render_markdown',return_value=(self.batches.rendered(150,False),{},[])):
            result=self.invoke()
        self.assertEqual(result['execution']['status'],'verified',result)
        receipt=self.case.store.read()[0]['engine_state']['property_receipts'][next_id]
        self.assertEqual(receipt['manifest_sha256'],e.digest(e.creation_properties.SCOPE))
        self.assertEqual(self.writes().count('create'),2)
    def remote_store(self):
        import tempfile
        import test_notion_remote_journal as jf
        folder=tempfile.TemporaryDirectory(dir=self.case.f.root);self.addCleanup(folder.cleanup)
        fixture=jf.Fixture(folder.name)
        store=fixture.store();store.compare_and_set(store.read()[1],self.before)
        self.case.store=store
        return fixture,store
    def test_confirmed_cas_acceptance_after_timeout_resumes_same_page(self):
        fixture,store=self.remote_store();fixture.mode='accepted_throw'
        after,sha=self.transition()
        self.assertEqual(after['policy_sha256'],recovery.current_get_policy())
        self.assertEqual(len(fixture.receipts),2)
        fixture.mode=None
        self.assertEqual(self.invoke()['execution']['status'],'verified')
        self.assertEqual(self.writes(),['create','append','append'])
    def test_rejected_or_unconfirmed_transition_never_reaches_notion(self):
        fixture,store=self.remote_store();anchor=deepcopy(store.anchor);fixture.mode='reject'
        with self.assertRaises(Exception):self.transition()
        self.assertEqual(self.writes(),['create'])
        self.case.store=fixture.store(anchor)
        self.assertEqual(self.case.store.read()[0],self.before)
        fixture.mode=None;fixture.confirm_value=False
        with self.assertRaises(Exception):self.transition()
        self.assertEqual(self.writes(),['create'])

    def test_all_optional_modes_reject_original_and_successor_canonical_journal(self):
        for modes in (({},None,None),(None,{},None),(None,None,{})):
            with self.assertRaisesRegex(ValueError,'notion_journal_busy'):
                runtime.recovery_mode_guard(self.before,{'recovery_anchor':r.TAG},modes)
            after,_=self.case.store.read()
            with self.assertRaisesRegex(ValueError,'notion_journal_busy'):
                runtime.recovery_mode_guard(after,{},modes)
        runtime.recovery_mode_guard(self.before,{'recovery_anchor':r.TAG},(None,None,None))
        self.assertEqual(self.writes(),['create'])

    def test_source_revalidation_failure_stops_before_any_remaining_effect(self):
        self.transition()
        with patch.object(o.QueuePublication,'check',side_effect=e.Blocked('source_changed')):
            with self.assertRaisesRegex(e.Blocked,'source_changed'):
                self.invoke()
        self.assertEqual(self.writes(),['create'])

    def test_old_rules_are_not_accepted_as_a_fresh_preparation(self):
        self.transition()
        state,sha=self.case.store.read();state['engine_state']['journal']=None
        self.case.store.compare_and_set(sha,state)
        adapter=o.EngineStore(self.case.store)
        pub=o.QueuePublication(self.local.vault,self.case.store.read()[0],self.case.store.read()[1])
        result=e.execute(self.plan,r.PLAN_SHA,pub,None,adapter,
            expected_store_sha=adapter.read()[1],writers_guard=lambda:self.case.policy)
        self.assertEqual(result.get('reason'),'rules_changed',result)
        self.assertEqual(self.writes(),['create'])
