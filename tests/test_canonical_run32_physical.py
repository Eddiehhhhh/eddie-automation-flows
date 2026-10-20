"""Exact #32 no-op proof; original #26 receipt acceptance remains immutable."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'diagnostics'),str(Path(__file__).resolve().parents[1]/'scripts')]
import replay_canonical_run32_physical as replay
import get_notion_run32_recovery as r
p=replay.p
recovery=p.recovery


class Tests(unittest.TestCase):
    def test_current_order_composed_manual_and_scheduled_http_replay(self):
        for trigger in ('manual','schedule'):
            result=replay.replay(trigger)
            self.assertEqual(result['physical_requests'],17)
            self.assertEqual(result['max_physical_requests'],32)
            self.assertEqual(result['candidate_attempts_checked'],4)
            self.assertEqual(result['publication_records_verified'],1)
            self.assertEqual(result['listing_run_numbers'],list(range(32,1,-1)))
            self.assertTrue(result['execution_verification_included'])
            self.assertTrue(result['current_attempt_excluded'])
            self.assertTrue(result['log_redirect_included'])
            self.assertFalse(result['cdn_authorization_forwarded'])
            self.assertTrue(result['private_receipt_readback_required'])
            self.assertFalse(result['private_state_replayed'])
            self.assertEqual(result['network_calls'],0)
            self.assertEqual(result['canonical_proof']['run_id'],r.RUN)
            self.assertEqual(result['canonical_proof']['record_sha256'],r.GET_RECORD['record_sha256'])
            self.assertIn('/actions/runs/'+str(r.RUN)+'/attempts/1',result['api_routes'])
            for number in ('30','31'):
                row=replay.recent_attempts()[number]
                self.assertIn('/actions/runs/'+str(row['id'])+'/attempts/1',result['api_routes'])

    def test_without_exact_bridge_four_slots_cannot_reach_original_proof(self):
        original=recovery.candidate_policy
        def omit(policy,run):
            return (policy,False) if run.get('id')==r.RUN else original(policy,run)
        with patch.object(recovery,'candidate_policy',side_effect=omit):
            with self.assertRaisesRegex(p.ProvenanceFailure,'provenance_no_verified_publication'):
                replay.replay()

    def test_candidate_policy_accepts_only_exact_run_attempt_controller_and_current_target(self):
        policy=replay.fresh.policy();run=replay.fresh.fixture()['attempt']
        self.assertEqual(recovery.candidate_policy(policy,run),(r.PUBLIC_POLICY,True))
        for changes in ({'id':r.RUN+1},{'run_attempt':2},{'head_sha':'a'*40},{'event':'schedule'}):
            changed={**run,**changes}
            self.assertEqual(recovery.candidate_policy(policy,changed),(policy,False))
        for key in ('rules_sha256','get_policy_sha256','repository','workflow_id'):
            changed={**policy,key:('f'*64 if key.endswith('sha256') else 'wrong')}
            self.assertEqual(recovery.candidate_policy(changed,run),(changed,False))
        with patch.dict(r.PUBLIC_POLICY,{'code_sha':'b'*40}):
            with self.assertRaisesRegex(ValueError,'recovery_policy_changed'):
                recovery.run32_public_policy()

    def test_full_canonical_record_and_job_are_pinned(self):
        recovery.verify_original_record(deepcopy(r.GET_RECORD),r.JOB)
        for key,value in r.GET_RECORD.items():
            altered=deepcopy(r.GET_RECORD)
            altered[key]=not value if isinstance(value,bool) else value+1 if isinstance(value,int) else 'f'*len(value)
            with self.subTest(key=key),self.assertRaises(p.ProvenanceFailure):
                recovery.verify_original_record(altered,r.JOB)
        with self.assertRaises(p.ProvenanceFailure):recovery.verify_original_record(r.GET_RECORD,r.JOB+1)

    def test_original_sequence_two_receipt_is_not_relabeled_or_relaxed(self):
        record=recovery.SEQUENCE_TWO_RECORD
        receipt={'sequence':2,'policy_sha256':recovery.sequence_two_policy()['get_policy_sha256']}
        target=recovery.current_get_policy()
        self.assertTrue(recovery.accepts_receipt(record['receipt_commit'],record['receipt_sha256'],receipt,target))
        for changed in ({'sequence':3},{'policy_sha256':r.PUBLIC_POLICY['get_policy_sha256']}):
            self.assertFalse(recovery.accepts_receipt(record['receipt_commit'],record['receipt_sha256'],{**receipt,**changed},target))
        self.assertFalse(recovery.accepts_receipt('f'*40,record['receipt_sha256'],receipt,target))
        self.assertFalse(recovery.accepts_receipt(record['receipt_commit'],'f'*64,receipt,target))
        self.assertFalse(recovery.accepts_receipt(record['receipt_commit'],record['receipt_sha256'],receipt,'f'*64))

    def client(self):
        policy=replay.fresh.policy();opener=replay.Opener(policy)
        client=p.PublicGithub('synthetic-noncredential',opener=opener,clock=lambda:0,sleep=lambda _:None)
        return policy,opener,client

    def test_actual_proof_tampering_and_attempt_reread_changes_fail_closed(self):
        for kind in ('record','job','attempt','controller','reread'):
            policy,opener,client=self.client();api=opener.api
            if kind=='record':
                index=next(i for i,line in enumerate(api.lines) if ' '+p.MARKER in line)
                stamp,_=api.lines[index].split(' '+p.MARKER,1)
                value=deepcopy(r.GET_RECORD);value['observed_main_commit']='f'*40
                value.pop('record_sha256');value['record_sha256']=p.a.sha(p.a.encoded(value))
                api.lines[index]=stamp+' '+p.MARKER+p.a.encoded(value).decode()
            if kind=='job':api.jobs['jobs'][0]['id']+=1
            if kind=='attempt':api.attempt['run_attempt']=2
            if kind=='controller':api.controller+=b'\n'
            if kind=='reread':
                read=api.json;seen=0
                def drift(path):
                    nonlocal seen
                    value=read(path)
                    if path=='/actions/runs/'+str(r.RUN)+'/attempts/1':
                        seen+=1
                        if seen==2:value['updated_at']='2026-10-19T18:38:40Z'
                    return value
                api.json=drift
            with self.subTest(kind=kind),self.assertRaises(p.ProvenanceFailure):
                p.discover(client,policy,exclude_run_id=replay.fresh.SYNTHETIC_RUN,exclude_run_attempt=1)
            self.assertLessEqual(client.requests,32)

    def test_http_budget_and_denial_do_not_fallback(self):
        with patch.object(p,'MAX_REQUESTS',8):
            with self.assertRaises(p.ProvenanceFailure):replay.replay()
        policy,opener,client=self.client();normal=opener.open
        def denied(request,timeout):
            if request.full_url.endswith('/actions/jobs/'+str(r.JOB)+'/logs'):
                opener.calls.append(request.full_url);return replay.fresh.Response(b'{}',403)
            return normal(request,timeout)
        opener.open=denied
        with self.assertRaises(p.ProvenanceFailure):p.discover(client,policy)
        self.assertEqual(sum(url.endswith('/actions/jobs/'+str(r.JOB)+'/logs') for url in opener.calls),1)
        self.assertFalse(any('/actions/runs/'+str(recovery.SEQUENCE_TWO_RECORD['run_id'])+'/' in url for url in opener.calls))
        self.assertFalse(opener.cdn_auth)


if __name__=='__main__':unittest.main()
