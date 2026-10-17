"""Exact idle run13 bridge; synthetic public API and real local bare Git only."""
from copy import deepcopy
import base64
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import get_notion_png_recovery as r
import get_notion_journal_runtime as c
import get_notion_outbox as o
import get_notion_executor as e
import get_notion_plan_recovery as holds
import test_notion_executor as ef
import test_notion_remote_journal as jf
import test_notion_journal_runtime as cf
import test_publication_provenance as pf


class Public:
    def __init__(self):
        policy=r.PUBLIC_POLICY;repo={'id':policy['repository_id'],'full_name':policy['repository'],'private':False}
        self.run={'id':r.RUN,'run_attempt':r.ATTEMPT,'workflow_id':policy['workflow_id'],'path':policy['workflow_path'],
            'head_sha':r.CHECKPOINT['controller_sha'],'head_branch':'main','repository':repo,'head_repository':deepcopy(repo),
            'event':'schedule','status':'completed','conclusion':'success','created_at':'2026-10-17T17:15:00Z','updated_at':'2026-10-17T17:25:25Z'}
        self.job={'id':r.JOB,'run_id':r.RUN,'run_attempt':1,'head_sha':self.run['head_sha'],'name':policy['job_name'],
            'status':'completed','conclusion':'success','started_at':'2026-10-17T17:15:01Z','completed_at':r.COMPLETED_AT,
            'steps':[{'name':policy['publication_step_name'],'number':5,'status':'completed','conclusion':'success',
                      'started_at':'2026-10-17T17:16:00Z','completed_at':'2026-10-17T17:20:00Z'},
                     {'name':c.NOTION_STEP,'number':6,'status':'completed','conclusion':'success',
                      'started_at':'2026-10-17T17:20:01Z','completed_at':'2026-10-17T17:25:23Z'}]}
        self.record=deepcopy(r.CHECKPOINT);self.get=deepcopy(r.GET_RECORD);self.calls=[];self.overrides={};self.scheduled=True
    def json(self,path):
        self.calls.append(path)
        if path in self.overrides:
            value=self.overrides[path]
            if isinstance(value,Exception):raise value
            return deepcopy(value)
        if path=='':return deepcopy(self.run['repository'])
        policy=r.PUBLIC_POLICY;root='/actions/runs/'+str(r.RUN)
        if path in (root,root+'/attempts/1'):return deepcopy(self.run)
        if path==root+'/attempts/1/jobs?per_page=100&page=1':return {'total_count':1,'jobs':[deepcopy(self.job)]}
        if path=='/actions/workflows/'+str(policy['workflow_id']):return {'id':policy['workflow_id'],'path':policy['workflow_path']}
        if path=='/actions/workflows/'+str(policy['workflow_id'])+'/runs?per_page=5&page=1':return {'total_count':1,'workflow_runs':[deepcopy(self.run)]}
        if path=='/contents/'+policy['workflow_path']+'?ref='+self.run['head_sha']:
            raw=c.controller.expected_bytes(policy,scheduled=self.scheduled)
            return {'type':'file','path':policy['workflow_path'],'encoding':'base64','size':len(raw),'sha':c.journal.retention.oid(raw),'content':base64.b64encode(raw).decode()}
        raise AssertionError('unexpected synthetic route')
    def logs(self,job):
        assert job==r.JOB;self.calls.append('logs')
        return ('2026-10-17T17:20:00Z '+c.provenance.MARKER+o.encoded(self.get).decode()+'\n'
                +'2026-10-17T17:25:20Z '+c.MARKER+o.encoded(self.record).decode()+'\n').encode()


def current_policy():return {**r.PUBLIC_POLICY,'code_sha':'9'*40,'rules_sha256':'8'*64,'get_policy_sha256':'7'*64}


class PublicTests(unittest.TestCase):
    def test_real_validator_exact_pins_success_schedule_and_same_client(self):
        c.provenance.validate_policy(r.PUBLIC_POLICY,r.PUBLIC_SHA)
        api=Public();budget=c._ReadBudget(api);result=r.recover_checkpoint(budget,r.RUN,1)
        self.assertEqual(result['recovery_anchor'],r.TAG);self.assertEqual(result['anchor'],r.ANCHOR)
        self.assertEqual(result['checkpoint'],r.CHECKPOINT);self.assertEqual(result['get_step_record'],r.GET_RECORD)
        self.assertEqual(len(api.calls),budget.requests);self.assertLessEqual(budget.requests,40)
        self.assertEqual(sum(x.endswith('/attempts/1') for x in api.calls),2)
    def test_non_success_run_job_notion_or_manual_template_rejected(self):
        cases=[('run','status','in_progress'),('run','conclusion','failure'),('run','conclusion','cancelled'),
               ('run','event','workflow_dispatch'),('job','conclusion','failure'),('job','completed_at','2026-10-17T17:25:23Z'),
               ('step','conclusion','failure'),('step','conclusion','skipped')]
        for target,key,value in cases:
            with self.subTest(target=target,key=key,value=value):
                api=Public();row=api.run if target=='run' else api.job if target=='job' else api.job['steps'][1];row[key]=value
                with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        api=Public();api.scheduled=False
        with self.assertRaisesRegex(c.CheckpointFailure,'other_controller'):r.recover_checkpoint(api,r.RUN,1)
    def test_mutated_hash_valid_checkpoint_or_get_record_still_rejected(self):
        for attr,key,value in [('record','state_sha256','f'*64),('record','commit','e'*40),('get','observed_main_commit','e'*40)]:
            api=Public();row=getattr(api,attr);row[key]=value;row.pop('record_sha256');row['record_sha256']=(o.digest(row) if attr=='record' else c.provenance.a.sha(c.provenance.a.encoded(row)))
            with self.assertRaisesRegex(c.CheckpointFailure,'record'):r.recover_checkpoint(api,r.RUN,1)
    def test_exact_identifiers_and_permission_fail_without_fallback(self):
        for run,attempt in ((True,1),(r.RUN,True),(r.RUN+1,1),(r.RUN,2)):
            api=Public()
            with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,run,attempt)
            self.assertEqual(api.calls,[])
        api=Public();api.overrides['']=PermissionError('private fixture')
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        self.assertEqual(api.calls,[''])
    def test_discovery_shared_budget_and_no_other_error_fallback(self):
        policy=current_policy();args={'public_policy':policy,'approved_public_policy_sha256':c.provenance.a.sha(c.provenance.a.encoded(policy)),'notion_policy_sha256':'6'*64}
        api=Public();out=c.discover_anchor(api,**args)
        self.assertEqual(out['recovery_anchor'],r.TAG);self.assertLessEqual(out['provenance_reads'],40)
        self.assertEqual(out['provenance_reads'],len(api.calls))
        for reason in ('journal_provenance_failed','journal_provenance_record','journal_provenance_budget'):
            with patch.object(c,'recover_anchor',side_effect=c.CheckpointFailure(reason)),patch.object(r,'recover_checkpoint') as call:
                with self.assertRaises(c.CheckpointFailure):c.discover_anchor(Public(),**args)
                call.assert_not_called()

    def test_normal_new_checkpoint_does_not_invoke_old_policy_recovery(self):
        policy=current_policy();api=Public()
        result={'anchor':{'commit':'f'*40,'state_blob':'e'*40,'state_sha256':'d'*64},
                'run_binding':c.provenance.run_binding(api.run)}
        with patch.object(c,'recover_anchor',return_value=result),patch.object(r,'recover_checkpoint') as old:
            observed=c.discover_anchor(api,public_policy=policy,
                approved_public_policy_sha256=c.provenance.a.sha(c.provenance.a.encoded(policy)),notion_policy_sha256='6'*64)
        old.assert_not_called();self.assertEqual(observed['anchor'],result['anchor'])
        self.assertNotIn('recovery_anchor',observed)


def synthetic():
    value=o.initial_state();value.update(origin={'commit':'a'*40,'receipt_sha256':None,'sequence':0},
        policy_sha256=r.PUBLIC_POLICY['get_policy_sha256'],last_enqueued_publication={'commit':'b'*40,'receipt_sha256':'c'*64,'sequence':1},
        observed_publication_tip={'commit':'b'*40,'receipt_sha256':'c'*64,'sequence':1})
    api=ef.API(None)
    for i in range(1,5):
        ident=str(1000000000000000000+i);roots=api.make(ident,[ef.paragraph('Synthetic owned text')])
        source={'blob':e.p.git_blob(ef.md(ident,'Synthetic source'))}
        value['engine_state']['receipts'][ident]=e.receipt(api.pages[ident],ident,ef.DS,roots,{'commit':'b'*40},source)
    for i in range(5,12):
        ident=str(1000000000000000000+i);value['pending_by_id'][ident]={'commit':'b'*40,'parent':'a'*40,'receipt_sha256':'c'*64,'sequence':1,
            'path':'Raw/03 Get/synthetic/'+ident+'.md','blob':'d'*40,'sha256':'e'*64}
    value['rotation_order']=list(reversed(sorted(value['pending_by_id'])))
    hold={'note_id':'1999999999999999999','page_id':'f'*32,'retry_allowed':False,'effects':'unresolved'}
    value['engine_state']['unresolved_by_id']={hold['note_id']:hold}
    return value,hold


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state,self.hold=synthetic();self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(holds,'HELD_NOTE_SHA256',e.digest(self.hold['note_id'])))
        self.stack.enter_context(patch.object(holds,'HOLD_SHA256',e.digest(self.hold)))
        o.check_state(self.state);self.sha=o.digest(self.state)
        self.stack.enter_context(patch.object(r,'ANCHOR',{**r.ANCHOR,'state_sha256':self.sha}))
        self.stack.enter_context(patch.object(r,'STATE_BYTES',len(o.encoded(self.state))))
    def test_single_field_exact_transition_preserves_receipts_queue_order_hold(self):
        before=deepcopy(self.state);after=r.transition_outer(self.state,self.sha,'f'*64)
        self.assertEqual(self.state,before);self.assertEqual([k for k in before if before[k]!=after[k]],['policy_sha256'])
        self.assertFalse(r.validate_state(after,o.digest(after)))
        with self.assertRaises(ValueError):r.transition_outer(after,o.digest(after),'e'*64)
    def test_state_drift_null_journal_and_old_policy_are_mandatory(self):
        for key in ('rotation_order','pending_by_id','engine_state','policy_sha256','observed_publication_tip'):
            bad=deepcopy(self.state);bad[key]={}
            with self.assertRaises(ValueError):r.transition_outer(bad,o.digest(bad),'f'*64)
        for value in ('short',None,True,r.PUBLIC_POLICY['get_policy_sha256']):
            with self.assertRaises(Exception):r.transition_outer(self.state,self.sha,value)
        with self.assertRaises(ValueError):r.transition_outer(self.state,'0'*64,'f'*64)
        bad=deepcopy(self.state);bad['engine_state']['journal']={'plan':{},'plan_sha256':o.digest({}),'index':0,'operation':None}
        with self.assertRaises(ValueError):r.transition_outer(bad,o.digest(bad),'f'*64)
    def test_actual_filtered_git_new_checkpoint_fresh_resume_and_old_anchor_rejected(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);git=jf.Fixture(tmp.name);store=git.store()
        store.compare_and_set(store.read()[1],self.state);before,sha=store.read();old_anchor=deepcopy(store.anchor)
        self.stack.enter_context(patch.object(r,'ANCHOR',old_anchor))
        candidate=r.transition_outer(before,sha,'f'*64);policy=pf.policy(get_policy_sha256='f'*64);context=cf.context(policy);stream=io.StringIO()
        emitter=c.CheckpointEmitter(stream,context=context)
        store.confirm=lambda receipt:git.confirm(receipt) and emitter(receipt)
        refs=jf.run(git.root,'show-ref');index=(git.root/'.git/index').read_bytes();head=jf.run(git.root,'rev-parse','HEAD')
        asset=jf.run(git.bare,'rev-parse','main:asset.bin').decode().strip();calls=len(git.calls)
        observed,newsha=o.confirmed_compare_and_set(store,sha,candidate)
        self.assertEqual(len(git.calls),calls+1);self.assertEqual(observed,candidate)
        self.assertEqual(jf.run(git.root,'show-ref'),refs);self.assertEqual((git.root/'.git/index').read_bytes(),index);self.assertEqual(jf.run(git.root,'rev-parse','HEAD'),head)
        api=cf.LogsAPI(policy,stream.getvalue());get=pf.record(api.row,policy_sha256='f'*64)
        api.raw=('2026-10-16T10:00:59Z '+c.provenance.publication_line(get)+'\n').encode()+api.raw.split(b'\n',1)[1]
        proof=c.recover_anchor(api,run_id=1001,run_attempt=1,
            public_policy=policy,approved_public_policy_sha256=context['public_policy_sha256'],notion_policy_sha256=context['notion_policy_sha256'])
        self.assertNotIn('recovery_anchor',proof);self.assertEqual(proof['anchor'],store.anchor)
        git.root=git.clone('fresh-png-policy');fresh=git.store(proof['anchor'])
        self.assertEqual(fresh.read(),(candidate,newsha))
        with self.assertRaises(Exception):git.store(old_anchor).read()
        with self.assertRaises(ValueError):r.transition_outer(*fresh.read(),'e'*64)
        self.assertEqual(jf.run(git.root,'cat-file','--batch-check',data=(asset+'\n').encode()),(asset+' missing\n').encode())
        self.assertEqual(len(git.calls),calls+1)
    def test_competing_state_or_unconfirmed_checkpoint_never_establishes_new_anchor(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);git=jf.Fixture(tmp.name);store=git.store()
        store.compare_and_set(store.read()[1],self.state);before,sha=store.read();candidate=r.transition_outer(before,sha,'f'*64)
        changed=deepcopy(before);changed['rotation_order'].reverse();store.compare_and_set(sha,changed)
        with self.assertRaises(Exception):o.confirmed_compare_and_set(store,sha,candidate)
        self.assertEqual(store.read()[0],changed)
        # Fresh independent backend: successful remote CAS with absent retained
        # checkpoint is unknown and cannot return a trusted migrated state.
        store=git.store(store.anchor);store.confirm=lambda receipt:False
        with self.assertRaises(Exception):store.compare_and_set(o.digest(changed),{**changed,'policy_sha256':'f'*64})
        self.assertTrue(store.poisoned)

if __name__=='__main__':unittest.main()
