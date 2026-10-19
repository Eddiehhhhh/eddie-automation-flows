"""Real public #32 proof; synthetic transition tests are NOT private replay."""
from copy import deepcopy
from pathlib import Path
import base64
import hashlib
import json
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_run32_recovery as r
import get_notion_journal_runtime as c
import get_notion_outbox as o
import get_recurring_policy_recovery as recovery

FIXTURE=Path(__file__).resolve().parent/'fixtures/notion-run32-public-proof.json'
FIXTURE_SHA='d57c85739f246d57413c1f22ee73372c469015283e154c57bf55c81ca97b5e32'


def fixture():
    raw=FIXTURE.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=FIXTURE_SHA:raise AssertionError('public proof fixture changed')
    return json.loads(raw)


class Public:
    """Recorded public metadata/markers, reconstructed reviewed controller."""
    def __init__(self):
        values=fixture();self.run=values['run'];self.attempt=values['attempt'];self.jobs=values['jobs']
        self.lines=values['marker_lines'];self.calls=[]
        self.controller=c.controller.expected_bytes(r.PUBLIC_POLICY)
    def json(self,path):
        self.calls.append(path);policy=r.PUBLIC_POLICY;root='/actions/runs/'+str(r.RUN)
        if path=='':return deepcopy(self.run['repository'])
        if path==root:return deepcopy(self.run)
        if path==root+'/attempts/1':return deepcopy(self.attempt)
        if path==root+'/attempts/1/jobs?per_page=100&page=1':return deepcopy(self.jobs)
        if path=='/actions/workflows/'+str(policy['workflow_id']):
            return {'id':policy['workflow_id'],'path':policy['workflow_path']}
        if path=='/actions/workflows/'+str(policy['workflow_id'])+'/runs?per_page=30&page=1':
            return {'total_count':1,'workflow_runs':[deepcopy(self.run)]}
        if path=='/contents/'+policy['workflow_path']+'?ref='+self.run['head_sha']:
            raw=self.controller
            return {'type':'file','path':policy['workflow_path'],'encoding':'base64',
                    'size':len(raw),'sha':c.journal.retention.oid(raw),'content':base64.b64encode(raw).decode()}
        raise AssertionError('unexpected fixture route')
    def logs(self,job):
        self.calls.append('logs:'+str(job))
        if job!=self.jobs['jobs'][0]['id']:raise AssertionError('wrong fixture job')
        return ('\n'.join(self.lines)+'\n').encode()
    def checkpoint_records(self):
        return [json.loads(line.split(c.MARKER,1)[1]) for line in self.lines if ' '+c.MARKER in line]
    def replace_record(self,index,mutate,rehash=True):
        offset=[i for i,line in enumerate(self.lines) if ' '+c.MARKER in line][index]
        stamp,encoded=self.lines[offset].split(' '+c.MARKER,1)
        value=json.loads(encoded);mutate(value)
        if rehash:
            value.pop('record_sha256',None);value['record_sha256']=o.digest(value)
        self.lines[offset]=stamp+' '+c.MARKER+o.encoded(value).decode()


def future_policy():
    policy=deepcopy(r.PUBLIC_POLICY)
    policy.update(code_sha='9'*40,rules_sha256=o.e.rules_sha256(),get_policy_sha256=recovery.current_get_policy())
    return {'public_policy':policy,'approved_public_policy_sha256':c.provenance.a.sha(c.provenance.a.encoded(policy)),
            'notion_policy_sha256':'6'*64}


class Tests(unittest.TestCase):
    def test_exact_three_checkpoint_chain_matches_actual_verified_public_proof(self):
        api=Public();budget=c._ReadBudget(api);value=r.recover_checkpoint(budget,r.RUN,r.ATTEMPT)
        expected=fixture()['verified_proof']
        self.assertEqual({k:v for k,v in value.items() if k!='recovery_anchor'},expected)
        self.assertEqual(value['recovery_anchor'],r.TAG)
        self.assertEqual(value['checkpoint_count'],3)
        self.assertEqual(value['checkpoint'],r.CHECKPOINT)
        self.assertEqual(value['get_step_record'],r.GET_RECORD)
        self.assertEqual(value['anchor'],r.ANCHOR)
        self.assertTrue(value['private_readback_required'])
        self.assertFalse(value['get_run_success_claim'])
        records=api.checkpoint_records()
        self.assertEqual([x['sequence'] for x in records],list(range(1,4)))
        self.assertIsNone(records[0]['previous_record_sha256'])
        self.assertEqual([x['previous_record_sha256'] for x in records[1:]],
                         [x['record_sha256'] for x in records[:-1]])
        self.assertEqual(sum(path.endswith('/attempts/1') for path in api.calls),2)
        self.assertEqual(budget.requests,len(api.calls));self.assertLessEqual(budget.requests,40)
        self.assertEqual(api.run['created_at'],fixture()['run']['created_at'])
        self.assertEqual(api.attempt['created_at'],fixture()['attempt']['created_at'])

    def test_bad_run_attempt_types_and_identity_stop_before_reads(self):
        for run,attempt in ((True,1),(r.RUN,True),(str(r.RUN),1),(r.RUN+1,1),(r.RUN,2)):
            api=Public()
            with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,run,attempt)
            self.assertEqual(api.calls,[])

    def test_wrong_job_run_attempt_repository_or_controller_rejected(self):
        mutations=(lambda a:a.jobs['jobs'][0].update(id=r.JOB+1),
                   lambda a:a.jobs['jobs'][0].update(run_attempt=2),
                   lambda a:a.jobs['jobs'][0].update(run_id=r.RUN+1),
                   lambda a:a.attempt.update(id=r.RUN+1),
                   lambda a:a.attempt.update(run_attempt=2),
                   lambda a:a.attempt['head_repository'].update(private=True),
                   lambda a:setattr(a,'controller',a.controller+b'\n'))
        for mutate in mutations:
            api=Public();mutate(api)
            with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)

    def test_self_rehashed_changed_state_commit_blob_or_record_rejected(self):
        for key in ('state_sha256','state_blob','commit','tree','record_sha256'):
            api=Public()
            api.replace_record(-1,lambda value:value.update({key:'f'*len(value[key])}),rehash=key!='record_sha256')
            with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        for key,value in (('state_bytes',1),('readback_verified',False),('sequence',7),
                          ('previous_record_sha256','f'*64),('run_attempt',2),('run_id',r.RUN+1)):
            api=Public();api.replace_record(-1,lambda row:row.update({key:value}))
            with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)

    def test_missing_duplicate_reordered_or_fourth_checkpoint_rejected(self):
        api=Public();api.lines.pop()
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        api=Public();api.lines.append(api.lines[-1])
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        api=Public();api.lines[-1],api.lines[-2]=api.lines[-2],api.lines[-1]
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        api=Public();stamp,last=api.lines[-1].split(' '+c.MARKER,1);value=json.loads(last)
        value.update(sequence=4,previous_record_sha256=value['record_sha256'])
        value.pop('record_sha256');value['record_sha256']=o.digest(value)
        api.lines.append(stamp+' '+c.MARKER+o.encoded(value).decode())
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)

    def test_modified_get_marker_and_out_of_step_time_rejected(self):
        api=Public();index=next(i for i,line in enumerate(api.lines) if ' '+c.provenance.MARKER in line)
        stamp,text=api.lines[index].split(' '+c.provenance.MARKER,1);record=json.loads(text)
        record['receipt_sha256']='f'*64;record.pop('record_sha256');record['record_sha256']=c.provenance.a.sha(c.provenance.a.encoded(record))
        api.lines[index]=stamp+' '+c.provenance.MARKER+o.encoded(record).decode()
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)
        api=Public();api.lines[-1]='2026-10-19T16:00:00Z '+api.lines[-1].split(' ',1)[1]
        with self.assertRaises(c.CheckpointFailure):r.recover_checkpoint(api,r.RUN,1)

    def test_same_attempt_reread_drift_rejected(self):
        api=Public();read=api.json;seen=0
        def drift(path):
            nonlocal seen
            value=read(path)
            if path.endswith('/attempts/1'):
                seen+=1
                if seen==2:value['updated_at']='2026-10-19T18:38:40Z'
            return value
        api.json=drift
        with self.assertRaisesRegex(c.CheckpointFailure,'journal_provenance_changed'):
            r.recover_checkpoint(api,r.RUN,1)

    def test_discovery_new_policy_exact_old_controller_bridge_only(self):
        api=Public();value=c.discover_anchor(api,**future_policy())
        self.assertEqual(value['recovery_anchor'],r.TAG)
        self.assertEqual(value['anchor'],r.ANCHOR)
        self.assertLessEqual(value['provenance_reads'],40)
        for reason in ('journal_provenance_failed','journal_provenance_record',
                       'journal_provenance_budget','journal_provenance_changed',
                       'journal_provenance_repository','journal_provenance_jobs'):
            with patch.object(c,'recover_anchor',side_effect=c.CheckpointFailure(reason)),patch.object(r,'recover_checkpoint') as bridge:
                with self.assertRaisesRegex(c.CheckpointFailure,reason):c.discover_anchor(Public(),**future_policy())
                bridge.assert_not_called()

    def synthetic_state(self):
        """Synthetic codec-valid state; never the actual private #32 state."""
        import test_notion_executor as f
        state=o.initial_state();origin={'commit':'a'*40,'receipt_sha256':None,'sequence':0}
        state.update(origin=deepcopy(origin),policy_sha256=r.PUBLIC_POLICY['get_policy_sha256'],
                     last_enqueued_publication=deepcopy(origin),observed_publication_tip=deepcopy(origin))
        api=f.API(None);roots=api.make(f.N,[f.paragraph('Synthetic owned text'),f.paragraph('Synthetic protected text')])
        source={'blob':'b'*40}
        state['engine_state']['receipts'][f.N]=o.e.receipt(api.pages[f.N],f.N,f.DS,roots[:1],{'commit':'a'*40},source)
        state['engine_state']['failures'][f.N2]={'md_blob':'c'*40,'source_commit':'a'*40,
            'policy_sha256':'d'*64,'reason':'human_edit_conflict'}
        state['engine_state']['unresolved_by_id']={}
        o.check_state(state)
        return state

    def test_synthetic_idle_transition_changes_only_policy_and_preserves_all_other_fields(self):
        # Patching the anchor is explicitly unit isolation; it does NOT prove
        # that the actual private state matched the public checkpoint digest.
        state=self.synthetic_state();sha=o.digest(state);old=deepcopy(state)
        with patch.dict(r.ANCHOR,{'state_sha256':sha}):
            result=r.transition_outer(state,sha,recovery.current_get_policy())
        self.assertEqual(state,old)
        expected=deepcopy(state);expected['policy_sha256']=recovery.current_get_policy()
        self.assertEqual(result,expected)
        self.assertEqual(o.encoded({k:v for k,v in result.items() if k!='policy_sha256'}),
                         o.encoded({k:v for k,v in old.items() if k!='policy_sha256'}))
        self.assertIsNone(result['engine_state']['journal'])

    def test_synthetic_transition_rejects_unpinned_digest_nonidle_or_wrong_policy(self):
        state=self.synthetic_state();sha=o.digest(state)
        with self.assertRaises(o.Blocked):r.transition_outer(state,sha,recovery.current_get_policy())
        with patch.dict(r.ANCHOR,{'state_sha256':sha}):
            with self.assertRaises(o.Blocked):r.transition_outer(state,'f'*64,recovery.current_get_policy())
            with self.assertRaises(o.Blocked):r.transition_outer(state,sha,'f'*64)
            changed=deepcopy(state);changed['policy_sha256']='e'*64
            with self.assertRaises(o.Blocked):r.transition_outer(changed,o.digest(changed),recovery.current_get_policy())
        plan={'records':[]};active=deepcopy(state)
        active['engine_state']['journal']={'plan':plan,'plan_sha256':o.digest(plan),'index':0,'operation':None}
        active_sha=o.digest(active);o.check_state(active)
        with patch.dict(r.ANCHOR,{'state_sha256':active_sha}):
            with self.assertRaises(o.Blocked):r.transition_outer(active,active_sha,recovery.current_get_policy())
        self.assertEqual(state,self.synthetic_state())

    def test_current_policy_composed_physical_budget_with_real_historical_order(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'diagnostics'))
        import replay_run32_physical as physical
        for trigger in ('manual','schedule'):
            result=physical.replay(trigger)
            self.assertEqual(result['listing_run_numbers'],list(range(32,1,-1)))
            self.assertEqual(result['selected_run_id'],r.RUN)
            self.assertEqual(result['physical_requests'],15)
            self.assertEqual(result['max_physical_requests'],32)
            self.assertEqual(result['logical_discovery_reads'],12)
            self.assertTrue(result['execution_verification_included'])
            self.assertTrue(result['current_attempt_excluded'])
            self.assertTrue(result['log_redirect_included'])
            self.assertFalse(result['cdn_authorization_forwarded'])
            self.assertTrue(result['private_readback_required'])
            self.assertFalse(result['private_state_replayed'])
            self.assertEqual(result['network_calls'],0)

    def test_physical_budget_or_log_denial_never_tries_another_identity_or_older_anchor(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'diagnostics'))
        import replay_run32_physical as physical
        with patch.object(physical.p,'MAX_REQUESTS',8):
            with self.assertRaises(c.CheckpointFailure):physical.replay()
        approved=physical.policy();opener=physical.Opener(approved);normal=opener.open
        def denied(request,timeout):
            if request.full_url.endswith('/actions/jobs/'+str(r.JOB)+'/logs'):
                opener.calls.append(request.full_url)
                return physical.Response(b'{}',403)
            return normal(request,timeout)
        opener.open=denied
        client=physical.p.PublicGithub('synthetic-noncredential',opener=opener,clock=lambda:0,sleep=lambda _:None)
        with self.assertRaises(c.CheckpointFailure):
            c.discover_anchor(client,public_policy=approved,
                approved_public_policy_sha256=physical.p.a.sha(physical.p.a.encoded(approved)),
                notion_policy_sha256=physical.n.e.digest(physical.n.scope_for(approved)),
                exclude_run_id=physical.SYNTHETIC_RUN,exclude_run_attempt=1)
        self.assertEqual(sum(url.endswith('/actions/jobs/'+str(r.JOB)+'/logs') for url in opener.calls),1)
        self.assertFalse(any('/actions/runs/39273266609/' in url for url in opener.calls))
        self.assertFalse(opener.cdn_auth)


if __name__=='__main__':unittest.main()
