"""Synthetic final selection tests; no production authority or network."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_registered_producer_locator import load_runtime
p=load_runtime()
import get_notion_idle_rotation as r

class SelectionTests(unittest.TestCase):
    def setUp(self):
        class Client: pass
        self.client=Client();self.policy={'synthetic':'scope'}
        context=patch.dict(sys.modules,{'get_publication_provenance':p})
        context.start();self.addCleanup(context.stop)
        self.addCleanup(lambda:r._OBSERVATIONS.pop(self.client,None))
    def candidate(self,run=5,seq=2,commit='f'*40,when='2026-01-01T00:00:00Z'):
        return {'run_id':run,'run_attempt':1,'receipt_sequence':seq,'record_sha256':str(run%10)*64,
                'anchor':{'commit':commit,'receipt_sha256':'1'*64},'completed_at':when}
    def seal(self,value,policy=None):
        source={'run_id':value['run_id'],'run_attempt':value['run_attempt'],
                'receipt_sequence':value['receipt_sequence'],'record_sha256':value['record_sha256'],
                'receipt_commit':value['anchor']['commit'],'receipt_sha256':value['anchor']['receipt_sha256'],
                'kind':'exact_bootstrap','controller_sha':'a'*40,'code_sha':'b'*40,
                'public_policy_sha256':'c'*64,'idle_run_id':None,'idle_run_attempt':None,'idle_record_sha256':None}
        return (p.a.sha(p.a.encoded(self.policy if policy is None else policy)),r.VerifiedSource(source,r._SOURCE_SEAL))
    def choose(self,found,observations,required=True):
        return p.select_verified_publication(self.client,self.policy,found,observations,rotation_required=required)
    def test_newer_same_receipt_does_not_displace_exact_sealed_source(self):
        old=self.candidate();new=self.candidate(6,when='2026-01-01T00:10:00Z');item=self.seal(old)
        got,history=self.choose([old,new],{(5,1):item,(6,1):None})
        self.assertEqual(got,old);self.assertIsNone(history)
        self.assertIs(r.observation_for(self.client,self.policy,old),item)
        self.assertIsNone(r.observation_for(self.client,self.policy,new))
    def test_highest_unsealed_sequence_never_falls_back(self):
        old=self.candidate();new=self.candidate(6,3)
        with self.assertRaisesRegex(r.RotationFailure,'rotation_source_unverified'):
            self.choose([old,new],{(5,1):self.seal(old)})
    def test_same_sequence_different_anchor_is_fatal(self):
        old=self.candidate();new=self.candidate(6,commit='a'*40)
        with self.assertRaisesRegex(p.ProvenanceFailure,'provenance_receipt_fork'):
            self.choose([old,new],{(5,1):self.seal(old)})
    def test_historical_fork_not_hidden_by_higher_valid_sequence(self):
        old=self.candidate();fork=self.candidate(6,commit='a'*40);new=self.candidate(7,3)
        with self.assertRaisesRegex(p.ProvenanceFailure,'provenance_receipt_fork'):
            self.choose([old,fork,new],{(7,1):self.seal(new)})
    def test_highest_exact_sealed_candidate_selected(self):
        old=self.candidate();new=self.candidate(6,3);item=self.seal(new)
        got,_=self.choose([old,new],{(5,1):self.seal(old),(6,1):item})
        self.assertEqual(got,new);self.assertIs(r.observation_for(self.client,self.policy,new),item)
    def test_no_rotation_preserves_original_freshness(self):
        old=self.candidate();new=self.candidate(6,when='2026-01-01T00:10:00Z')
        got,history=self.choose([old,new],{},False)
        self.assertEqual(got,new);self.assertIsNone(history)
    def test_wrong_run_policy_or_record_seal_is_not_fallback(self):
        old=self.candidate();new=self.candidate(6,when='2026-01-01T00:10:00Z')
        wrong_record=deepcopy(new);wrong_record['record_sha256']='0'*64
        for bad in [self.seal(old),self.seal(new,{'synthetic':'other'}),self.seal(wrong_record)]:
            with self.subTest(bad=bad[0]),self.assertRaisesRegex(r.RotationFailure,'rotation_source_unverified'):
                self.choose([old,new],{(5,1):self.seal(old),(6,1):bad})
    def test_forged_seal_fails_not_fallback(self):
        old=self.candidate();new=self.candidate(6,when='2026-01-01T00:10:00Z')
        with self.assertRaisesRegex(r.RotationFailure,'rotation_source_unverified'):
            self.choose([old,new],{(5,1):self.seal(old),(6,1):(p.a.sha(p.a.encoded(self.policy)),object())})
    def test_select_error_propagates_no_fallback(self):
        old=self.candidate()
        with patch.object(r,'select_observation',side_effect=r.RotationFailure('proof_denied')):
            with self.assertRaisesRegex(r.RotationFailure,'proof_denied'):
                self.choose([old],{(5,1):self.seal(old)})

class FullDiscoverSelectionTests(SelectionTests):
    """Exercise discover's full non-reserve control flow, with explicit synthetic
    proof boundaries. The separate private actual-metadata replay authenticates
    the production fixture; these fixtures do not claim real authorization.
    """
    def setUp(self):
        super().setUp()
        self.policy={'repository_id':7,'workflow_id':8,'workflow_path':'.github/workflows/synthetic.yml','head_branch':'main'}
    def discover(self, candidates, sealed, error=None):
        rows=[{'id':v['run_id'],'run_attempt':1,'head_branch':'main','head_sha':'a'*40,
               'event':'schedule','status':'completed','conclusion':'success',
               'updated_at':v['completed_at']} for v in candidates]
        by_id={v['run_id']:v for v in candidates};selected={}
        policy=self.policy
        class Client:
            def json(inner,path):
                if path=='':return {'id':7,'full_name':p.REPOSITORY,'private':False}
                if path=='/actions/workflows/8':return {'id':8,'path':policy['workflow_path']}
                if path=='/actions/workflows/8/runs?per_page=30&page=1':return {'total_count':len(rows),'workflow_runs':deepcopy(rows)}
                if path.startswith('/actions/runs/'):
                    return deepcopy(next(row for row in rows if str(row['id'])==path.rsplit('/',1)[1]))
                raise AssertionError(path)
        client=Client();self.addCleanup(lambda:r._OBSERVATIONS.pop(client,None))
        def observe(c,pol,run,value,**kwargs):
            if run['id'] in sealed:r._OBSERVATIONS[c]=self.seal(value)
            return value
        def verify(c,run,*args,**kwargs):
            if error is not None and run['id']==candidates[-1]['run_id']:raise p.ProvenanceFailure(error)
            return deepcopy(by_id[run['id']])
        def choose(c,pol,chosen,item,**kwargs):
            selected['run_id']=chosen['run_id']
            return original_select(c,pol,chosen,item,**kwargs)
        original_select=r.select_observation
        search={'locator':(candidates[0]['run_id'],1),'joint':False,'proposal':None}
        with patch.object(p,'check_run_identity',side_effect=lambda value,pol:value), \
             patch.object(p,'effective_candidate_policy',return_value=(policy,None,None)), \
             patch.object(p,'verified_candidate',side_effect=verify), \
             patch.object(r,'source_search',return_value=search), \
             patch.object(r,'observe_source',side_effect=observe), \
             patch.object(r,'select_observation',side_effect=choose):
            result=p.discover(client,policy,capture_history_witness=True)
        self.assertEqual(selected['run_id'],result['run_id'])
        return result
    def test_full_discover_same_receipt_keeps_own_seal(self):
        old=self.candidate();new=self.candidate(6,when='2026-01-01T00:10:00Z')
        result=self.discover([old,new],{5})
        self.assertEqual(result['run_id'],5)
        self.assertEqual(result['publication_records_verified'],2)
        self.assertIn('history_witness',result)
        self.assertEqual(result['status'],'public_provenance_verified')
    def test_full_discover_higher_unsealed_fails(self):
        with self.assertRaisesRegex(r.RotationFailure,'rotation_source_unverified'):
            self.discover([self.candidate(),self.candidate(6,3)],{5})
    def test_full_discover_fork_fails(self):
        with self.assertRaisesRegex(p.ProvenanceFailure,'provenance_receipt_fork'):
            self.discover([self.candidate(),self.candidate(6,commit='a'*40)],{5})
    def test_full_discover_verified_new_sequence_wins(self):
        result=self.discover([self.candidate(),self.candidate(6,3)],{5,6})
        self.assertEqual(result['run_id'],6)
    def test_full_discover_proof_denial_never_falls_back(self):
        with self.assertRaisesRegex(p.ProvenanceFailure,'provenance_unavailable'):
            self.discover([self.candidate(),self.candidate(6)],{5},error='provenance_unavailable')

if __name__=='__main__':unittest.main()
