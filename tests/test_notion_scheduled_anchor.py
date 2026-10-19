"""Bounded checkpoint selection and exact successor compatibility; synthetic only."""
from copy import deepcopy
import unittest
import base64
from unittest.mock import patch
import test_notion_journal_attempts as fixtures
import get_notion_journal_runtime as j
import get_notion_run18_recovery as bridge
import get_recurring_policy_recovery as recovery
import get_notion_outbox as outbox

class SelectionTests(unittest.TestCase):
    setUp=fixtures.AttemptTests.setUp
    api=fixtures.AttemptTests.api
    discover=fixtures.AttemptTests.discover
    def test_six_wrong_controller_runs_do_not_hide_valid_checkpoint(self):
        oldest=self.api(); rows=[oldest]
        for ident in range(1002,1008):
            item=self.api(ident,checkpoint=False);item.row['head_sha']='f'*40
            rows.append(item)
        api=fixtures.AttemptsAPI(rows)
        raw=b'# other immutable controller\n'
        api.overrides['/contents/'+self.policy['workflow_path']+'?ref='+'f'*40]={
            'type':'file','path':self.policy['workflow_path'],'encoding':'base64',
            'size':len(raw),'sha':j.provenance.entry.r.oid(raw),'content':base64.b64encode(raw).decode()}
        found=self.discover(api)
        self.assertEqual(found['run_id'],1001)
        self.assertEqual(found['candidate_attempts_checked'],1)
        self.assertLessEqual(found['provenance_reads'],40)
    def test_failed_run_with_checkpoint_stays_eligible(self):
        api=self.api();api.row['conclusion']='failure';api.job['conclusion']='failure'
        found=self.discover(fixtures.AttemptsAPI([api]))
        self.assertEqual(found['run_id'],1001)
    def test_one_second_attempt_clock_skew_is_allowed(self):
        api=fixtures.AttemptsAPI([self.api()]);path='/actions/runs/1001/attempts/1'
        api.overrides[path]={**api.latest[1001].row,'created_at':'2026-10-16T10:00:01Z'}
        found=self.discover(api);self.assertEqual(found['run_id'],1001)
    def test_more_than_one_second_skew_fails(self):
        api=fixtures.AttemptsAPI([self.api()]);path='/actions/runs/1001/attempts/1'
        api.overrides[path]={**api.latest[1001].row,'created_at':'2026-10-16T09:59:58Z'}
        with self.assertRaises(j.CheckpointFailure):self.discover(api)
    def test_distinct_ninety_run_scan_is_physically_bounded(self):
        rows=[]
        for ident in range(1001,1091):
            item=self.api(ident,checkpoint=False);item.row['head_sha']='f'*40;rows.append(item)
        class Pages(fixtures.AttemptsAPI):
            def json(inner,path):
                if '/runs?per_page=30' in path:
                    page=int(path.rsplit('=',1)[1]);batch=rows[(page-1)*30:page*30]
                    return {'total_count':90,'workflow_runs':[deepcopy(x.row) for x in batch]}
                if path.startswith('/contents/'):
                    raw=b'# incompatible immutable controller\n'
                    return {'type':'file','path':self.policy['workflow_path'],'encoding':'base64',
                        'size':len(raw),'sha':j.provenance.entry.r.oid(raw),'content':base64.b64encode(raw).decode()}
                return super().json(path)
        api=Pages(rows)
        class Reply:
            status=200;headers={}
            def __init__(inner,value):inner.raw=j.provenance.a.encoded(value)
            def __enter__(inner):return inner
            def __exit__(inner,*args):return False
            def read(inner,n):return inner.raw[:n]
        class Opener:
            def open(inner,request,timeout):return Reply(api.json(request.full_url[len(j.provenance.API):]))
        client=j.provenance.PublicGithub('synthetic-noncredential',opener=Opener(),clock=lambda:0)
        with self.assertRaises(j.CheckpointFailure):self.discover(client)
        self.assertEqual(client.requests,j.provenance.MAX_REQUESTS)
        self.assertLess(len([x for x in api.called if '/attempts/' in x]),90)
    def test_repository_identity_is_read_once_per_discovery(self):
        api=fixtures.AttemptsAPI([self.api(),self.api(1002,checkpoint=False)])
        self.discover(api)
        self.assertEqual(api.called.count(''),1)
    def test_repository_observation_does_not_cross_discoveries(self):
        api=fixtures.AttemptsAPI([self.api()]);self.discover(api)
        api.overrides['']={'id':self.policy['repository_id'],'full_name':j.provenance.REPOSITORY,'private':True}
        with self.assertRaises(j.CheckpointFailure):self.discover(api)
    def test_attempt_repository_identity_change_is_rejected(self):
        api=fixtures.AttemptsAPI([self.api()]);row=deepcopy(api.latest[1001].row)
        row['repository']['private']=True
        api.overrides['/actions/runs/1001/attempts/1']=row
        with self.assertRaises(j.CheckpointFailure):self.discover(api)

class ExactRecoveryTests(unittest.TestCase):
    def test_sequence_two_receipt_is_exact(self):
        rec=recovery.SEQUENCE_TWO_RECORD
        value={'sequence':2,'policy_sha256':recovery.sequence_two_policy()['get_policy_sha256']}
        args=(rec['receipt_commit'],rec['receipt_sha256'],value,recovery.current_get_policy())
        self.assertTrue(recovery.accepts_receipt(*args))
        for changed in [('f'*40,args[1],value,args[3]),(args[0],'f'*64,value,args[3]),(args[0],args[1],{**value,'sequence':3},args[3]),(args[0],args[1],value,'f'*64)]:
            self.assertFalse(recovery.accepts_receipt(*changed))
    def test_sequence_two_record_cannot_mutate(self):
        rec=deepcopy(recovery.SEQUENCE_TWO_RECORD)
        recovery.verify_original_record(rec,recovery.SEQUENCE_TWO_JOB)
        for key,value in [('receipt_sequence',3),('main_updated',False),('controller_sha','f'*40)]:
            with self.assertRaises(Exception):recovery.verify_original_record({**rec,key:value},recovery.SEQUENCE_TWO_JOB)
        with self.assertRaises(Exception):recovery.verify_original_record(rec,recovery.SEQUENCE_TWO_JOB+1)
    def test_idle_policy_transition_preserves_every_other_field(self):
        value=outbox.initial_state();value['policy_sha256']=bridge.PUBLIC_POLICY['get_policy_sha256']
        # Isolate transition semantics with synthetic state; production schema
        # checks and exact immutable hash remain separately enforced.
        sha=outbox.digest(value)
        with patch.dict(bridge.ANCHOR,{'state_sha256':sha}),patch.object(outbox,'check_state'):
            result=bridge.transition_outer(value,sha,recovery.current_get_policy())
            wanted=deepcopy(value);wanted['policy_sha256']=recovery.current_get_policy()
            self.assertEqual(result,wanted);self.assertEqual(value['policy_sha256'],bridge.PUBLIC_POLICY['get_policy_sha256'])
            with self.assertRaises(Exception):bridge.transition_outer(value,'f'*64,recovery.current_get_policy())
            busy=deepcopy(value);busy['engine_state']['journal']={'pending':True}
            with patch.dict(bridge.ANCHOR,{'state_sha256':outbox.digest(busy)}):
                with self.assertRaises(Exception):bridge.transition_outer(busy,outbox.digest(busy),recovery.current_get_policy())

if __name__=='__main__':unittest.main()
