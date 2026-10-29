"""Synthetic locator contracts; no credentials or physical network."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_idle_rotation as r


def hint():
    return {'contract': r.CONTRACT,
        'execution': {'run_id': 7, 'run_attempt': 1, 'controller_sha': 'a'*40,
            'code_sha': 'b'*40, 'public_policy_sha256': 'c'*64,
            'notion_policy_sha256': 'd'*64, 'rules_sha256': 'e'*64},
        'source': {'run_id': 5, 'run_attempt': 1, 'controller_sha': 'a'*40,
            'code_sha': 'b'*40, 'public_policy_sha256': 'c'*64,
            'receipt_commit': 'f'*40, 'receipt_sha256': '1'*64,
            'receipt_sequence': 2, 'record_sha256': '2'*64,
            'idle_run_id': 5, 'idle_run_attempt': 1,
            'idle_record_sha256': '3'*64, 'kind': 'authenticated_idle'},
        'state_sha256': '4'*64, 'state_blob': '5'*40, 'idle_acceptance': None}


class HintTests(unittest.TestCase):
    def test_roundtrip(self):
        value = hint()
        self.assertEqual(r.parse_message(r.message(value)), value)

    def test_old_message_is_not_authority(self):
        self.assertIsNone(r.parse_message(r.SUBJECT))

    def test_malformed_marked_message_never_falls_back(self):
        for message in ('bad '+r.PREFIX+'{}', r.SUBJECT+'\n\n'+r.PREFIX+'{}',
                        r.message(hint())+'\n', r.message(hint())+r.PREFIX):
            with self.subTest(message=message[:20]), self.assertRaises(r.RotationFailure):
                r.parse_message(message)

    def test_duplicate_json_key(self):
        text=r.message(hint()).replace('"contract":', '"contract":"bad","contract":', 1)
        with self.assertRaises(r.RotationFailure): r.parse_message(text)

    def test_source_and_idle_cannot_mix_runs(self):
        for key,value in [('idle_run_id',6),('idle_run_attempt',2),('kind','latest')]:
            item=hint();item['source'][key]=value
            with self.subTest(key=key),self.assertRaises(r.RotationFailure):r.check_hint(item)

    def test_bool_attempt_rejected(self):
        item=hint();item['execution']['run_attempt']=True
        with self.assertRaises(r.RotationFailure):r.check_hint(item)

    def test_unknown_fields_rejected(self):
        for key in ('future', 'token', 'url'):
            item=hint();item[key]='x'
            with self.assertRaises(r.RotationFailure):r.check_hint(item)

    def test_public_witness_constructor_rejects(self):
        with self.assertRaises(r.RotationFailure):r.VerifiedSource(hint()['source'],object())
        with self.assertRaises(r.RotationFailure):r.JointCanonicalProof({}, {}, object())


class RealCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.git('init','-q')
        self.tree=self.git('mktree',data=b'').strip().decode()
        self.parent='9'*40
        self.date='2026-01-01T00:00:00Z'
        epoch=int(datetime(2026,1,1,tzinfo=timezone.utc).timestamp())
        self.raw=('tree '+self.tree+'\nparent '+self.parent+'\n'
            'author Get journal <get-journal@users.noreply.github.com> '+str(epoch)+' +0000\n'
            'committer Get journal <get-journal@users.noreply.github.com> '+str(epoch)+' +0000\n\n'
            +r.message(hint())).encode()
        self.commit=self.git('hash-object','-t','commit','-w','--stdin',data=self.raw).strip().decode()
        self.envelope={'sha':self.commit,'tree':{'sha':self.tree},'parents':[{'sha':self.parent}],
            'author':{'name':r.IDENTITY[0],'email':r.IDENTITY[1],'date':self.date},
            'committer':{'name':r.IDENTITY[0],'email':r.IDENTITY[1],'date':self.date},
            'message':r.message(hint()).rstrip('\n')}

    def git(self,*args,data=None):
        return subprocess.check_output(['git','-C',str(self.root),*args],input=data,stderr=subprocess.DEVNULL)

    def test_real_raw_oid_matches(self):
        actual=r.commit_envelope(self.envelope,self.commit)
        self.assertEqual(actual,{'commit':self.commit,'parent':self.parent,'tree':self.tree,'hint':hint(),'committed_at':self.date})
        self.assertEqual(self.git('cat-file','commit',self.commit),self.raw)

    def test_same_hint_copied_into_other_commit_rejected(self):
        item=deepcopy(self.envelope);item['parents'][0]['sha']='8'*40
        with self.assertRaises(r.RotationFailure):r.commit_envelope(item,self.commit)

    def test_changed_tree_parent_author_clock_rejected(self):
        for field in ('tree','parent','author','date'):
            item=deepcopy(self.envelope)
            if field=='tree':item['tree']['sha']='8'*40
            elif field=='parent':item['parents'][0]['sha']='8'*40
            elif field=='author':item['author']['name']='other'
            else:item['committer']['date']='2026-01-01T00:00:01Z'
            with self.subTest(field=field),self.assertRaises(r.RotationFailure):r.commit_envelope(item,self.commit)

    def test_merge_rejected(self):
        item=deepcopy(self.envelope);item['parents'].append({'sha':'8'*40})
        with self.assertRaises(r.RotationFailure):r.commit_envelope(item,self.commit)

    def test_changed_state_digest_rejected(self):
        item=deepcopy(self.envelope);value=hint();value['state_sha256']='8'*64
        item['message']=r.message(value)
        with self.assertRaises(r.RotationFailure):r.commit_envelope(item,self.commit)

    def test_signed_or_non_utc_original_oid_not_reconstructed(self):
        raw=self.raw.replace(b'+0000',b'+0100')
        item=deepcopy(self.envelope);item['sha']=r.oid(raw,'commit')
        with self.assertRaises(r.RotationFailure):r.commit_envelope(item,item['sha'])


if __name__=='__main__':unittest.main()


# These isolated adapters test the new causal/budget protocol, not native
# provenance. Full original source/current/runtime probes are separate.
import contextlib
import importlib.util
from types import SimpleNamespace as NS
from unittest.mock import patch


@contextlib.contextmanager
def causal_fixture():
    before={'engine_state':{'journal':{'plan':{'records':[{'note_id':'synthetic'}],
        'historical_only':True},'index':1,'operation':None},'receipts':{'synthetic':{'readback_verified':True}}},
        'rotation_order':[],'other':'preserved'}
    plan=before['engine_state']['journal']['plan']
    before['engine_state']['journal']['plan_sha256']=r.sha(plan)
    after=deepcopy(before);after['engine_state']['journal']=None
    def check(x):
        if not isinstance(x,dict) or set(x)!=set(before):raise ValueError('state')
    codec=NS(check_state=check,digest=r.sha,encoded=r.encoded)
    class Records:
        limit=16
        def guard(self):self.guards+=1;return self.nodes[0]['commit']
        def node(self,i):return self.nodes[i] if i<len(self.nodes) else None
        def object(self,node,kind):return self.raw[node['commit']]
    proof=NS(PrivateRecords=Records,PATHS={'journal':'state'},MAX_OBJECT=1000000,
        strict=lambda raw,limit:json.loads(raw))
    rows=r.receipt_set(before['engine_state'],plan)
    marker={'contract':'get-notion-native-idle-acceptance-1','plan_sha256':r.sha(plan),
        'before_state_sha256':r.sha(before),'after_engine_sha256':r.sha(after['engine_state']),
        'receipt_set_sha256':r.sha(rows),'records':1,'before_commit':'1'*40}
    record=Records();record.guards=0
    record.raw={'1'*40:r.encoded(before),'2'*40:r.encoded(after),'3'*40:r.encoded(after)}
    def node(commit,parent,marked=False):
        raw=record.raw[commit];n={'commit':commit,'parent':parent,'tree':'9'*40,'objects':{'state':(r.oid(raw),len(raw))}}
        if marked:
            h=hint();h.update(state_sha256=r.sha(raw),state_blob=r.oid(raw),idle_acceptance=deepcopy(marker))
            n['locator']={'commit':commit,'parent':parent,'tree':n['tree'],'hint':h}
        return n
    record.nodes=[node('3'*40,'2'*40),node('2'*40,'1'*40,True),node('1'*40,None)]
    with patch.dict(sys.modules,{'get_notion_outbox':codec,'get_private_proof':proof}):
        yield NS(before=before,after=after,record=record,marker=marker,envelope=record.nodes[1]['locator'])


class CausalOriginTests(unittest.TestCase):
    def test_exact_clear_parent_and_unchanged_successor(self):
        with causal_fixture() as x:
            self.assertEqual(r.verify_idle_origin(x.record,x.envelope,x.after['engine_state']),x.before)
            self.assertEqual(x.record.guards,2)
    def test_arbitrary_ancestor_same_engine_not_authority(self):
        with causal_fixture() as x:
            x.envelope['hint']['idle_acceptance']['before_commit']='8'*40
            with self.assertRaises(r.RotationFailure):r.verify_idle_origin(x.record,x.envelope,x.after['engine_state'])
    def test_changed_before_receipt_source_or_other_state_rejected(self):
        for field in ('state','receipt','plan','parent'):
            with self.subTest(field=field),causal_fixture() as x:
                if field=='parent':x.record.nodes[1]['parent']='7'*40
                else:
                    value=deepcopy(x.before)
                    if field=='state':value['other']='changed'
                    elif field=='receipt':value['engine_state']['receipts']['synthetic']['extra']=True
                    else:value['engine_state']['journal']['plan']['records'].append({'note_id':'extra'})
                    x.record.raw['1'*40]=r.encoded(value)
                with self.assertRaises(r.RotationFailure):r.verify_idle_origin(x.record,x.envelope,x.after['engine_state'])
    def test_wrong_plan_receipt_count_marker_rejected(self):
        for key in ('plan_sha256','receipt_set_sha256','before_state_sha256','records'):
            with self.subTest(key=key),causal_fixture() as x:
                x.envelope['hint']['idle_acceptance'][key]=2 if key=='records' else '8'*64
                with self.assertRaises(r.RotationFailure):r.verify_idle_origin(x.record,x.envelope,x.after['engine_state'])
    def test_copied_marker_over_intervening_engine_change_rejected(self):
        with causal_fixture() as x:
            later=deepcopy(x.after);later['engine_state']['receipts']['synthetic']['extra']=True
            x.record.raw['3'*40]=r.encoded(later)
            top=x.record.nodes[0];top['objects']['state']=(r.oid(r.encoded(later)),len(r.encoded(later)))
            h=deepcopy(x.envelope['hint']);h.update(state_sha256=r.sha(later),state_blob=r.oid(r.encoded(later)))
            top['locator']={'commit':top['commit'],'parent':top['parent'],'tree':top['tree'],'hint':h}
            with self.assertRaises(r.RotationFailure):r.verify_idle_origin(x.record,top['locator'],x.after['engine_state'])
    def test_forged_state_change_without_marked_commit_rejected(self):
        with causal_fixture() as x:
            x.record.nodes[1].pop('locator')
            with self.assertRaises(r.RotationFailure):r.verify_idle_origin(x.record,x.envelope,x.after['engine_state'])
    def test_partial_active_or_unknown_cannot_clear(self):
        for change in ({'index':0},{'operation':{'phase':'prepared'}},{'operation':{'phase':'in_flight'}}, {'operation':{'phase':'unknown'}}):
            with causal_fixture() as x:
                x.before['engine_state']['journal'].update(change)
                with self.assertRaises(r.RotationFailure):r.clear_after(x.before)
    def test_only_original_skipped_conflict_rotation_permitted(self):
        with causal_fixture() as x:
            p=x.before['engine_state']['journal']['plan'];p.pop('historical_only');p['skipped_conflicts']=['a']
            x.before['rotation_order']=['a','b'];after=r.clear_after(x.before)
            self.assertEqual(after['rotation_order'],['b','a']);self.assertEqual(after['other'],'preserved')


@contextlib.contextmanager
def real_transport():
    # Real HTTP authorization/physical accounting code with a synthetic opener;
    # only unrelated native schema/module pins are replaced in this unit scope.
    native=NS(encoded=r.encoded,sha=r.sha,DATA_SOURCE_PIN='1'*32,DATABASE_PIN='2'*32)
    ro=NS(read_route=lambda method,path:method=='GET' and path=='/synthetic',NoRedirect=lambda:None,ReadFailure=RuntimeError,MAX_RESPONSE=16*1024*1024,MAX_HTTP_BYTES=64*1024*1024)
    modules={'get_notion_native':native,'get_notion_readonly':ro}
    path=Path(r.__file__).with_name('get_notion_live_transport.py')
    spec=importlib.util.spec_from_file_location('get_notion_live_transport',path);m=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,modules):
        spec.loader.exec_module(m)
        with patch.dict(sys.modules,{'get_notion_live_transport':m}):yield m


class NativeBudgetTests(unittest.TestCase):
    def request(self,m):
        clock=[0.0];calls=[]
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return b'{}'
        class Opener:
            def open(self,*args,**kwargs):calls.append(args[0].method);return Response()
        req=m.Transport('synthetic',opener=Opener(),sleep=lambda n:clock.__setitem__(0,clock[0]+n),clock=lambda:clock[0])
        return req,clock,calls
    def test_same_transport_counts_continue_through_closure(self):
        with real_transport() as m:
            req,clock,calls=self.request(m);req('GET','/synthetic');v=r.ClosureReservation(req);req.closure_reservation=v
            req('GET','/synthetic');v.begin();req('GET','/synthetic');v.end()
            self.assertEqual(req.attempts,3);self.assertEqual(req.bytes,6);self.assertEqual(calls,['GET']*3)
            self.assertIs(v.request,req)
    def test_remaining_reservation_attempt_bytes_deadline_zero_http(self):
        for field,value in [('attempts',1744),('bytes',48*1024*1024),('time',1380)]:
            with self.subTest(field=field),real_transport() as m:
                req,clock,calls=self.request(m);v=r.ClosureReservation(req);req.closure_reservation=v
                if field=='time':clock[0]=value
                else:setattr(req,field,value)
                with self.assertRaises(m.Failure):req('GET','/synthetic')
                self.assertEqual(calls,[])
    def test_active_reservation_rejects_all_writes_even_authorized(self):
        with real_transport() as m:
            req,clock,calls=self.request(m);key=m.fingerprint('POST','/pages',{});req.approved=frozenset([key])
            v=r.ClosureReservation(req);req.closure_reservation=v;v.begin()
            with self.assertRaises(m.NotDispatched):req('POST','/pages',{})
            self.assertEqual(calls,[]);self.assertEqual(req.attempts,0)
    def test_closure_attempt_bytes_time_bounds_and_no_counter_reset(self):
        for field,delta in [('attempts',1996),('bytes',64*1024*1024-20),('time',1500)]:
            with self.subTest(field=field),real_transport() as m:
                req,clock,calls=self.request(m);req.attempts=4;req.bytes=20;v=r.ClosureReservation(req);v.begin()
                if field=='time':clock[0]+=delta
                else:setattr(req,field,getattr(req,field)+delta)
                with self.assertRaises(r.RotationFailure):v.check()
                v.end();self.assertGreaterEqual(req.attempts,4);self.assertGreaterEqual(req.bytes,20)
    def test_invalid_or_reversed_clock_counter_rejects(self):
        for field,value in [('attempts',-1),('bytes',-1),('attempts',True),('time',float('nan')),('time',float('inf'))]:
            with self.subTest(field=field),real_transport() as m:
                req,clock,calls=self.request(m)
                if field=='time':clock[0]=value
                else:setattr(req,field,value)
                with self.assertRaises(r.RotationFailure):r.ClosureReservation(req)
        with real_transport() as m:
            req,clock,calls=self.request(m);clock[0]=1;v=r.ClosureReservation(req);v.begin();clock[0]=0
            with self.assertRaises(r.RotationFailure):v.check()
    def test_postphysical_budget_exhaustion_is_uncertain_not_notdispatched(self):
        with real_transport() as m:
            req,clock,calls=self.request(m);key=m.fingerprint('POST','/pages',{});req.approved=frozenset([key])
            req.attempts=1743;req.closure_reservation=r.ClosureReservation(req)
            with self.assertRaises(m.Failure) as caught:req('POST','/pages',{})
            self.assertNotIsInstance(caught.exception,m.NotDispatched)
            self.assertEqual(str(caught.exception),'write_uncertain');self.assertEqual(calls,['POST'])
            self.assertEqual(req.attempts,1744);self.assertIn(key,req.attempted)

class FailedReadAccounting(unittest.TestCase):
    request=NativeBudgetTests.request
    def test_partial_read_is_conservatively_charged_and_not_write_retried(self):
        import http.client
        with real_transport() as m:
            req,clock,calls=self.request(m);key=m.fingerprint('POST','/pages',{});req.approved=frozenset([key])
            class Broken:
                status=200
                def __enter__(self):return self
                def __exit__(self,*a):pass
                def read(self,n):raise http.client.IncompleteRead(b'synthetic',n)
            def opened(*args,**kw):calls.append('POST');return Broken()
            req.opener=NS(open=opened);req.closure_reservation=r.ClosureReservation(req)
            with self.assertRaises(m.Failure) as caught:req('POST','/pages',{})
            self.assertEqual(str(caught.exception),'write_uncertain');self.assertEqual(calls,['POST'])
            self.assertEqual(req.bytes,16*1024*1024+1);self.assertEqual(req.attempts,1)
    def test_close_failure_preserves_completed_bytes(self):
        with real_transport() as m:
            req,clock,calls=self.request(m)
            class Broken:
                status=200
                def __enter__(self):return self
                def __exit__(self,*a):raise ValueError('synthetic close')
                def read(self,n):return b'{}'
            req.opener=NS(open=lambda *a,**kw:Broken());req.closure_reservation=r.ClosureReservation(req)
            with self.assertRaises(m.Failure):req('GET','/synthetic')
            self.assertEqual(req.bytes,2);self.assertEqual(req.attempts,1)

    def test_http_error_partial_read_charged_and_one_open(self):
        import http.client,urllib.error
        with real_transport() as m:
            req,clock,calls=NativeBudgetTests.request(self,m)
            class ErrorBody:
                def read(self,n):raise http.client.IncompleteRead(b'synthetic',n)
                def close(self):pass
            def opened(*args,**kw):
                calls.append('GET');raise urllib.error.HTTPError('https://invalid.invalid',503,'synthetic',{},ErrorBody())
            req.opener=NS(open=opened);req.closure_reservation=r.ClosureReservation(req)
            with self.assertRaises(http.client.IncompleteRead):req('GET','/synthetic')
            self.assertEqual(calls,['GET']);self.assertEqual(req.bytes,16*1024*1024+1)
    def test_http_error_close_failure_keeps_body_charge_no_retry(self):
        import urllib.error
        with real_transport() as m:
            req,clock,calls=NativeBudgetTests.request(self,m)
            class ErrorBody:
                def read(self,n):return b'{}'
                def close(self):raise ValueError('synthetic close')
            def opened(*args,**kw):
                calls.append('GET');raise urllib.error.HTTPError('https://invalid.invalid',503,'synthetic',{},ErrorBody())
            req.opener=NS(open=opened);req.closure_reservation=r.ClosureReservation(req)
            with self.assertRaises(ValueError):req('GET','/synthetic')
            self.assertEqual(calls,['GET']);self.assertEqual(req.bytes,2)

class ReservationRemainderTests(unittest.TestCase):
    request=NativeBudgetTests.request
    def test_more_than_256_reads_share_original_remaining_budget(self):
        with real_transport() as m:
            req,clock,calls=self.request(m);v=r.ClosureReservation(req);req.closure_reservation=v;v.begin()
            for _ in range(257):req('GET','/synthetic')
            self.assertEqual(req.attempts,257);self.assertEqual(len(calls),257);self.assertEqual(req.bytes,514)
            v.end();self.assertEqual(req.attempts,257);self.assertEqual(req.deadline,1500)
    def test_ordinary_threshold_does_not_discard_reserved_remaining(self):
        with real_transport() as m:
            req,clock,calls=self.request(m);v=r.ClosureReservation(req);req.closure_reservation=v;req.attempts=1744
            with self.assertRaises(m.Failure):req('GET','/synthetic')
            self.assertEqual(calls,[]);v.begin();req('GET','/synthetic');v.end()
            self.assertEqual(req.attempts,1745);self.assertEqual(calls,['GET'])
    def test_repeated_begin_end_never_resets_deadline_or_counts(self):
        with real_transport() as m:
            req,clock,calls=self.request(m);v=r.ClosureReservation(req);req.closure_reservation=v
            for _ in range(3):v.begin();req('GET','/synthetic');v.end()
            clock[0]=1500
            with self.assertRaises(r.RotationFailure):v.begin()
            self.assertFalse(v.active);self.assertIsNone(v.start);self.assertEqual(req.attempts,3);self.assertEqual(req.deadline,1500)

class SelectedObservationTests(unittest.TestCase):
    """Pure selection binding; no fixture creates authentication authority."""
    def setUp(self):
        class Client: pass
        self.client=Client();self.policy={'synthetic':'scope'}
        self.pub=NS(a=NS(sha=r.sha,encoded=r.encoded))
        self.context=patch.dict(sys.modules,{'get_publication_provenance':self.pub})
        self.context.start();self.addCleanup(self.context.stop)
        self.addCleanup(lambda:r._OBSERVATIONS.pop(self.client,None))
    def observation(self,run=5,sequence=2):
        v=hint()['source'];v.update(kind='exact_bootstrap',idle_run_id=None,idle_run_attempt=None,idle_record_sha256=None,
            run_id=run,receipt_sequence=sequence)
        w=r.VerifiedSource(v,r._SOURCE_SEAL)
        item=(r.sha(self.policy),w)
        chosen={'run_id':run,'run_attempt':v['run_attempt'],'receipt_sequence':sequence,'record_sha256':v['record_sha256'],
            'anchor':{'commit':v['receipt_commit'],'receipt_sha256':v['receipt_sha256']}}
        return item,chosen
    def test_final_selection_not_last_observation(self):
        selected,chosen=self.observation(6,3);old,_=self.observation(5,2)
        r._OBSERVATIONS[self.client]=old
        self.assertIsNone(r.select_observation(self.client,self.policy,chosen,selected,required=True))
        self.assertIs(r.observation_for(self.client,self.policy,chosen),selected)
    def test_unsealed_highest_candidate_cannot_fall_back_older(self):
        old,_=self.observation();r._OBSERVATIONS[self.client]=old
        _,chosen=self.observation(6,3)
        with self.assertRaises(r.RotationFailure):r.select_observation(self.client,self.policy,chosen,None,required=True)
        self.assertNotIn(self.client,r._OBSERVATIONS)
    def test_same_run_mismatched_authority_fields_reject(self):
        for key in ('run_attempt','receipt_sequence','record_sha256','anchor'):
            item,chosen=self.observation();changed=deepcopy(chosen)
            changed[key]=({'commit':'0'*40,'receipt_sha256':'0'*64} if key=='anchor' else
                '0'*64 if key=='record_sha256' else 99)
            with self.subTest(key=key),self.assertRaises(r.RotationFailure):
                r.select_observation(self.client,self.policy,changed,item,required=True)
            self.assertNotIn(self.client,r._OBSERVATIONS)
    def test_policy_mismatch_never_reuses_seal(self):
        item,chosen=self.observation()
        with self.assertRaises(r.RotationFailure):
            r.select_observation(self.client,{'synthetic':'other'},chosen,item,required=True)

class HistoryBundleTests(unittest.TestCase):
    def setUp(self):
        from dataclasses import make_dataclass
        self.W=make_dataclass('SyntheticOriginalWitness',('receipt_commit','receipt_sha256','receipt_sequence','observed_commit','record_sha256'),frozen=True)
        self.entry=NS(VerifiedHistoryWitness=self.W,fail=r.fail)
        self.context=patch.dict(sys.modules,{'get_recurring_entry':self.entry});self.context.start();self.addCleanup(self.context.stop)
        self.current=self.W('a'*40,'b'*64,3,'c'*40,'d'*64)
        self.prior=self.W('e'*40,'f'*64,2,'1'*40,'d'*64)
    def test_two_exact_intervals_and_unknown_full_chain_fallback(self):
        bundle=r.VerifiedHistoryBundle((self.current,self.prior),r._JOINT_SEAL)
        self.assertIs(r.history_interval(bundle,ancestor=self.current.receipt_commit),self.current)
        self.assertIs(r.history_interval(bundle,ancestor=self.prior.receipt_commit),self.prior)
        self.assertIsNone(r.history_interval(bundle,ancestor='9'*40))
        self.assertIs(r.history_interval(bundle,observed=self.prior.observed_commit),self.prior)
    def test_not_constructible_from_dict_or_wrong_seal(self):
        for values,seal in (((self.current,),object()),(({'receipt_commit':'a'*40},),r._JOINT_SEAL)):
            with self.assertRaises(r.RotationFailure):r.VerifiedHistoryBundle(values,seal)
        with self.assertRaises(r.RotationFailure):r.history_interval({'intervals':(self.current,)},ancestor='a'*40)
    def test_bounded_unique_ordered_same_producer(self):
        for intervals in ((),(self.current,)*2,(self.prior,self.current),
            (self.current,self.prior,self.prior),
            (self.current,self.W('e'*40,'f'*64,2,'1'*40,'0'*64)),
            (self.current,self.W('e'*40,'f'*64,2,'c'*40,'d'*64)),
            (self.W('a'*40,'b'*64,True,'c'*40,'d'*64),)):
            with self.subTest(intervals=intervals),self.assertRaises(r.RotationFailure):r.VerifiedHistoryBundle(intervals,r._JOINT_SEAL)
    def test_intervals_are_immutable(self):
        bundle=r.VerifiedHistoryBundle((self.current,self.prior),r._JOINT_SEAL)
        with self.assertRaises(AttributeError):bundle.intervals=(self.prior,)
        with self.assertRaises(AttributeError):bundle.seal=object()
