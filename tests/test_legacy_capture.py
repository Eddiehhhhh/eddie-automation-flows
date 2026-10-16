"""Offline durable capture/typed evidence/CAS and budget regression tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_legacy_capture as c
from get_mirror_incremental_replay import *

class CaptureTests(unittest.TestCase):
    def fixture(self,n=1,missing=()):
        notes=[];paths={};old={}
        for i in range(1,n+1):
            ident=str(i);note={'id':ident,'note_type':'plain_text','title':'Exact','content':'Current source\r\nSecond\n','created_at':1776968995,'updated_at':1776968995.0,'unknown':{'keep':[True,None,1,1.0]}}
            if ident not in missing:notes.append(note)
            data=('---\nnote_id: "'+ident+'"\nnote_type: "plain_text"\ncreated_at: "Legacy untouched"\nunknown: {"keep":true}\n---\nOld body\r\n').encode()
            old[ident]=data;paths[ident]={'filePath':ident+'.md','gitBlob':c.retention.oid(data),'sha256':c.digest(data)}
        census,h=freeze_legacy_census(notes,True,paths,[], 'a'*40,'b'*40,'2026-10-16T07:00:00Z')
        return notes,census,h,old
    def observe(self,census,h,old,note=None):
        detail=deepcopy(note or census['summaries']['1'])
        raw=capture_json_bytes({'success':True,'data':{'note':detail,'unknown_sibling':{'value':'keep'}},'unknown_envelope':'retain'})
        return make_legacy_observation(census,h,'1',raw,old['1'],'2026-10-16T07:10:00Z')
    def test_strict_json_and_typed_exact_text(self):
        v=strict_capture_json(b'{"i":1,"f":1.0,"text":" x\\r\\ny ","unknown":{"x":null}}')
        self.assertIs(type(v['i']),int);self.assertIs(type(v['f']),float);self.assertEqual(v['text'],' x\r\ny ')
        for raw in (b'{"a":1,"a":2}',b'{"a":NaN}',b'{"a":Infinity}',b'{"a":1e999}',b'{"a":1.0000000000000000000001}',b'{"a":1e-999}',b'{"a":"\xff"}',b'{"a":"\\ud800"}',b'[]',b'{} trailing'):
            with self.subTest(raw=raw),self.assertRaises(ParityFailure):strict_capture_json(raw)
    def test_invalid_census_and_pending_overlap_rejected_even_rehashed(self):
        _,census,h,old=self.fixture()
        for change in ({'current_completeness_claimed':True},{'pending_ids':['1']},{'source_missing_ids':['1']},{'extra':'x'}):
            bad=deepcopy(census);bad.update(change)
            with self.assertRaises(ParityFailure):make_legacy_observation(bad,c.digest(capture_json_bytes(bad)),'1',b'{}',old['1'],'2026-10-16T07:00:00Z')
    def test_missing_source_is_deferred_without_blocking_other_records(self):
        _,census,h,old=self.fixture(2,missing=('2',))
        self.assertEqual(census['source_missing_ids'],['2'])
        record=self.observe(census,h,old);self.assertEqual(record['disposition'],'observed_unreconciled')
    def test_identity_and_old_hash_checked(self):
        _,census,h,old=self.fixture();bad=old['1'].replace(b'"1"',b'"2"')
        with self.assertRaises(ParityFailure):make_legacy_observation(census,h,'1',b'{}',bad,'2026-10-16T07:00:00Z')
        census['eligible']['1'].update(gitBlob=c.retention.oid(bad),sha256=c.digest(bad));h=c.digest(capture_json_bytes(census))
        with self.assertRaisesRegex(ParityFailure,'capture_mirror_identity'):make_legacy_observation(census,h,'1',b'{}',bad,'2026-10-16T07:00:00Z')
    def test_complete_payload_and_presence_map_are_retained(self):
        _,census,h,old=self.fixture();note=deepcopy(census['summaries']['1']);note.update(ref_content=None,audio_transcript='',audio={'original':'Actual'},web_page={'excerpt':123})
        record=self.observe(census,h,old,note)
        self.assertEqual(record['payload']['data']['unknown_sibling'],{'value':'keep'});self.assertEqual(record['payload']['unknown_envelope'],'retain')
        self.assertEqual(record['source_text_fields']['ref_content'],'null');self.assertEqual(record['source_text_fields']['audio_transcript'],'empty');self.assertEqual(record['source_text_fields']['web_page.content'],'absent');self.assertEqual(record['source_text_fields']['web_page.excerpt'],'unsupported_type')
        self.assertEqual(record['disposition'],'source_shape_unsupported')
        verify_legacy_observation(census,h,record,c.digest(capture_json_bytes(record)))
    def test_raw_versions_changed_and_unavailable_stay_unadopted(self):
        _,census,h,old=self.fixture();note=deepcopy(census['summaries']['1']);note['updated_at']=1776968995
        record=self.observe(census,h,old,note);self.assertEqual(record['disposition'],'source_version_changed');self.assertFalse(record['mirror_adopted'])
        note['updated_at']=1776968995.0;note['note_type']='recorder_audio';note['audio']={'original':''}
        self.assertEqual(self.observe(census,h,old,note)['disposition'],'source_transcript_unavailable')
    def test_receipt_metadata_tamper_rejected(self):
        _,census,h,old=self.fixture();r=self.observe(census,h,old);receipt=c.digest(capture_json_bytes(r));r['captured_at']='2026-10-16T08:00:00Z'
        with self.assertRaisesRegex(ParityFailure,'capture_receipt_changed'):verify_legacy_observation(census,h,r,receipt)
    def test_proposal_is_precise_preserves_header_and_is_blocked(self):
        _,census,h,old=self.fixture();r=self.observe(census,h,old);p=c.proposal(r,census,old['1']);d=p['byte_diff']
        self.assertTrue(p['blocked_from_apply']);self.assertIsNone(p['ownership_map']);self.assertFalse(p['historical_source_update_claimed'])
        candidate=old['1'][:d['start']]+d['new_utf8'].encode()+old['1'][d['start']+d['old_length']:]
        self.assertEqual(c.digest(candidate),d['result_sha256']);self.assertIn(b'created_at: "Legacy untouched"',candidate);self.assertIn(b'unknown: {"keep":true}',candidate)
        self.assertEqual(old['1'][d['start']:],b'Old body\r\n')
    def runner(self,n=1,failure=None,missing=()):
        notes,census,h,old=self.fixture(n,missing);calls=[];commits=[]
        class Source:
            def detail_raw(_,ident):
                calls.append(ident)
                if ident==failure:raise RuntimeError('private upstream text')
                return capture_json_bytes({'data':census['summaries'][ident]})
        ledger=c.initial_ledger(census,h);records={}
        return census,ledger,records,Source(),old,calls,commits
    def test_batches_50_plus_resume_zero_detail_and_no_duplicate_snapshots(self):
        census,ledger,records,source,old,calls,commits=self.runner(51)
        ledger,result=c.capture_batches(census,ledger,records,source,old,commits.append)
        self.assertEqual(result['captured'],51);self.assertEqual(len(commits),3);self.assertEqual(ledger['capture_batches'],2);self.assertTrue(ledger['finalized'])
        self.assertEqual(sum(p.startswith(c.PREFIX+'observations/') for batch in commits for p in batch),51)
        self.assertEqual(c.validate_ledger(census,ledger,records),[])
        before=len(calls);before_commits=len(commits)
        ledger,again=c.capture_batches(census,ledger,records,source,old,commits.append)
        self.assertEqual(len(calls),before);self.assertEqual(len(commits),before_commits);self.assertEqual(again['fresh_details'],0)
    def test_source_error_flushes_prior_validated_receipts_and_stops(self):
        census,ledger,records,source,old,calls,commits=self.runner(4,failure='3')
        ledger,result=c.capture_batches(census,ledger,records,source,old,commits.append)
        self.assertEqual(calls,['1','2','3']);self.assertEqual(set(records),{'1','2'});self.assertEqual(len(commits),1);self.assertEqual(result['status'],'capture_partial');self.assertEqual(result['error'],'capture_source_failed')
    def test_missing_source_does_not_consume_detail_request(self):
        census,ledger,records,source,old,calls,commits=self.runner(2,missing=('2',))
        ledger,result=c.capture_batches(census,ledger,records,source,old,commits.append)
        self.assertEqual(calls,['1']);self.assertEqual(result['source_missing'],1);self.assertEqual(result['remaining'],0)
    def test_batch_byte_cap_splits_without_dropping_captured_payload(self):
        census,ledger,records,source,old,calls,commits=self.runner(3)
        with patch.object(c,'MAX_BATCH',6*c.MIB+5000):ledger,result=c.capture_batches(census,ledger,records,source,old,commits.append)
        self.assertEqual(result['captured'],3);self.assertEqual(calls,['1','2','3'])
        self.assertTrue(all(sum(map(len,b.values()))<=6*c.MIB+5000 for b in commits))
    def test_commit_cap_stops_before_another_source_read(self):
        census,ledger,records,source,old,calls,commits=self.runner(51)
        with patch.object(c,'MAX_BATCHES',1):ledger,result=c.capture_batches(census,ledger,records,source,old,commits.append)
        self.assertEqual(len(calls),50);self.assertEqual(result['remaining'],1);self.assertEqual(result['error'],'capture_commit_cap');self.assertEqual(len(commits),1)
    def test_raw_budget_stops_before_get_read_and_retains_census(self):
        census,ledger,records,source,old,calls,commits=self.runner()
        with patch.object(c,'MAX_RAW',ledger['raw_bytes']+c.MAX_RESPONSE-1):ledger,result=c.capture_batches(census,ledger,records,source,old,commits.append)
        self.assertEqual(calls,[]);self.assertEqual(result['error'],'capture_raw_budget');self.assertIn(c.CENSUS,commits[0])
    def test_public_report_never_exposes_payload_ids_or_claims_apply(self):
        census,ledger,records,source,old,calls,commits=self.runner()
        ledger,result=c.capture_batches(census,ledger,records,source,old,commits.append);result.update(private='secret',payload={'id':'1234'},main_updated=True,mirror_adopted=1923)
        clean=c.public_report(result)
        self.assertNotIn('secret',json.dumps(clean));self.assertNotIn('1234',json.dumps(clean));self.assertFalse(clean['main_updated']);self.assertEqual(clean['mirror_adopted'],0)
        bad=deepcopy(result);bad['remaining']=1;self.assertEqual(c.public_report(bad)['status'],'failed')

class SourceTests(unittest.TestCase):
    def transport(self,responses):
        class T:
            LIST_PATH='/list';DETAIL_PATH='/detail';calls=[]
            @classmethod
            def api_get(cls,path,params,key,client,**kwargs):
                cls.calls.append((path,params,kwargs));return responses.pop(0)
        return T
    def test_strict_list_and_raw_detail_use_existing_policy_and_limits(self):
        t=self.transport([b'{"data":{"notes":[{"id":"1"}],"has_more":true,"cursor":"x"}}',b'{"data":{"notes":[{"id":"2"}],"has_more":false}}',b'{"data":{"id":"1","text":"Exact\\r\\n"}}'])
        policy=object();s=c.Source('fake','fake',policy,transport=t)
        self.assertEqual(s.enumerate()[0],[{'id':'1'},{'id':'2'}]);self.assertEqual(s.detail_raw('1'),b'{"data":{"id":"1","text":"Exact\\r\\n"}}')
        self.assertTrue(all(x[2]['capture_raw'] and x[2]['request_policy'] is s.policy and x[2]['max_response_bytes']==2*c.MIB for x in t.calls))
    def test_list_failure_contracts(self):
        cases=[ [b'{"data":{"notes":[],"has_more":true,"cursor":"a"}}'], [b'{"data":{"notes":[{"id":"1"}]}}'], [b'{"data":{"notes":[{"id":"1"},{"id":"1"}],"has_more":false}}'], [b'{"data":{"notes":[{"id":"1"}],"has_more":true,"cursor":"x"}}',b'{"data":{"notes":[{"id":"2"}],"has_more":true,"cursor":"x"}}'] ]
        for values in cases:
            with self.subTest(values=values),self.assertRaises(ParityFailure):c.Source('fake','fake',object(),transport=self.transport(values)).enumerate()

class GitCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.repo=self.root/'repo';self.repo.mkdir()
        def git(*args):return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.DEVNULL).decode().strip()
        self.git=git;git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
        (self.repo/'keep.txt').write_text('Unchanged non-Get');(self.repo/'Raw/03 Get').mkdir(parents=True);(self.repo/'Raw/03 Get/old.md').write_text('Untouched old note')
        git('add','.');git('commit','-qm','base');self.head=git('rev-parse','HEAD');self.local=c.retention.LocalGit(self.repo)
        _,self.census,_,_=CaptureTests().fixture();self.census['source_commit']=self.head
        self.census['get_tree']=git('rev-parse','HEAD:Raw/03 Get')
        local=self.local;census=self.census;parent=self
        class Remote:
            current=None;writes=[];failures=0;uncertain_success=False;foreign=False
            def metadata(_):return {'full_name':c.retention.REPOSITORY,'private':True}
            def ref(_,name):
                if name==c.REF:return _.current
                if name==c.retention.MAIN_REF:return parent.head
                return c.PROTECTED[name]
            def source_get_tree(_,commit):return census['get_tree']
            def cas(_,expected,commit):
                _.writes.append((expected,commit))
                if _.foreign:_.current='f'*40;raise RuntimeError('race')
                if _.failures:_.failures-=1;raise RuntimeError('temporary')
                if _.current!=expected:raise RuntimeError('stale')
                _.current=commit
                if _.uncertain_success:raise RuntimeError('response lost')
            def commit(_,commit):
                return {'parents':[line[7:].decode() for line in local.run('cat-file','-p',commit).split(b'\n\n',1)[0].splitlines() if line.startswith(b'parent ')], 'tree':parent.git('rev-parse',commit+'^{tree}')}
        self.remote=Remote();self.files={c.CENSUS:b'{}\n',c.LEDGER:b'{"value":1}\n'}
    def readback(self,commit,files):return {p:self.local.blob(self.local.tree(commit)[p][2]) for p in files}
    def publish(self):return c.publish_batch(self.local,self.remote,self.census,None,self.files,'synthetic',self.readback)
    def test_create_and_cas_modify_only_scoped_evidence(self):
        sha=self.publish();after=self.local.tree(sha);before=self.local.tree(self.head)
        self.assertEqual({p for p in before if before[p]!=after.get(p)},set());self.assertEqual(set(after)-set(before),set(self.files))
        files={c.LEDGER:b'{"value":2}\n'};second=c.publish_batch(self.local,self.remote,self.census,sha,files,'synthetic',self.readback)
        self.assertEqual(self.remote.commit(second)['parents'],[sha]);self.assertEqual(self.git('rev-parse','HEAD'),self.head)
    def test_uncertain_success_checks_receipt_without_second_write(self):
        self.remote.uncertain_success=True;sha=self.publish();self.assertEqual(len(self.remote.writes),1);self.assertEqual(self.remote.current,sha)
    def test_confirmed_no_write_retries_same_prepared_commit(self):
        self.remote.failures=1;sha=self.publish();self.assertEqual(len(self.remote.writes),2);self.assertEqual(self.remote.writes[0],self.remote.writes[1])
    def test_foreign_head_is_never_overwritten(self):
        self.remote.foreign=True
        with self.assertRaisesRegex(ParityFailure,'capture_uncertain_write'):self.publish()
        self.assertEqual(len(self.remote.writes),1);self.assertEqual(self.remote.current,'f'*40)
    def test_readback_failure_never_repeats_write(self):
        with self.assertRaisesRegex(ParityFailure,'capture_readback'):c.publish_batch(self.local,self.remote,self.census,None,self.files,'synthetic',lambda *args:{})
        self.assertEqual(len(self.remote.writes),1)
    def test_changed_protected_ref_stops_before_write(self):
        prior=self.remote.ref
        self.remote.ref=lambda name:'f'*40 if name==c.retention.BACKUP_REF else prior(name)
        with self.assertRaisesRegex(ParityFailure,'capture_refs_changed'):self.publish()
        self.assertEqual(self.remote.writes,[])
    def test_non_scope_file_rejected_before_commit(self):
        self.files['other.txt']=b'bad'
        with self.assertRaisesRegex(ParityFailure,'capture_scope_changed'):self.publish()
        self.assertEqual(self.remote.writes,[])

class PostCasReadbackTests(unittest.TestCase):
    def test_exact_previous_ref_is_read_only_retried_then_verified(self):
        remote=type('Remote',(),{})();remote.metrics={}
        answers=iter(['a'*40,'a'*40,'b'*40]);remote.ref=lambda _:next(answers)
        with patch.object(c,'guard') as guard,patch.object(c.time,'sleep') as sleep:
            c.verify_published_ref(remote,'a'*40,'b'*40)
        self.assertEqual(remote.metrics,{'readback_ref_attempts':3,'readback_stale_ref_observations':2})
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[1,2]);self.assertEqual(guard.call_count,2)
    def test_stale_previous_head_is_bounded_and_foreign_head_stops_immediately(self):
        for actual,code,count in [('a'*40,'capture_readback_ref_stale',6),('c'*40,'capture_readback_ref_changed',1)]:
            remote=type('Remote',(),{'ref':lambda _,name:actual})()
            with patch.object(c,'guard'),patch.object(c.time,'sleep') as sleep,self.assertRaisesRegex(ParityFailure,code):
                c.verify_published_ref(remote,'a'*40,'b'*40)
            self.assertEqual(remote.metrics['readback_ref_attempts'],count)
            self.assertEqual(sum(call.args[0] for call in sleep.call_args_list),31 if count==6 else 0)
    def test_missing_existing_head_and_transport_have_distinct_safe_errors(self):
        remote=type('Remote',(),{'ref':lambda _,name:None})()
        with self.assertRaisesRegex(ParityFailure,'capture_readback_ref_changed'):c.verify_published_ref(remote,'a'*40,'b'*40)
        def fail(*_):raise RuntimeError('Private remote error')
        remote.ref=fail
        with self.assertRaisesRegex(ParityFailure,'capture_readback_ref_transport'):c.verify_published_ref(remote,'a'*40,'b'*40)
        remote.commit=fail
        with self.assertRaisesRegex(ParityFailure,'capture_readback_commit_transport'):c.readback_commit(remote,'b'*40)
    def test_post_push_old_ref_does_not_repeat_cas_or_payload_read(self):
        fixture=GitCaptureTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        remote=fixture.remote;actual_ref=remote.ref;old_reads=[2]
        def ref(name):
            if name==c.REF and remote.writes and old_reads[0]:old_reads[0]-=1;return None
            return actual_ref(name)
        remote.ref=ref
        with patch.object(c.time,'sleep'),patch.object(fixture,'readback',wraps=fixture.readback) as readback:
            commit=fixture.publish()
        self.assertEqual(len(remote.writes),1);self.assertEqual(readback.call_count,1);self.assertEqual(remote.current,commit)
        self.assertEqual(remote.metrics['readback_stale_ref_observations'],2)
    def test_parent_and_payload_mismatches_stop_after_one_write(self):
        fixture=GitCaptureTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.remote.commit=lambda _: {'parents':['f'*40],'tree':'e'*40}
        with self.assertRaisesRegex(ParityFailure,'capture_readback_parent'):fixture.publish()
        self.assertEqual(len(fixture.remote.writes),1)
    def test_blob_size_hash_pack_and_capability_failures_are_distinct(self):
        import source_sync_pack_seed as pack
        data=b'exact';blob=c.retention.oid(data);meta={c.LEDGER:('100644','blob',blob,len(data))}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'blob';path.write_bytes(data)
            for replacement,code in ((b'shorter','capture_readback_blob_size'),(b'other','capture_readback_blob_hash')):
                path.write_bytes(replacement)
                with patch.object(pack,'capabilities',return_value=2),patch.object(c.retention,'fetch_readback_blobs',return_value=({blob:path},10)),self.assertRaisesRegex(ParityFailure,code):c.fetch_blobs(None,meta,'synthetic',Path(folder)/'out')
            for target,code in (('capabilities','capture_readback_pack_capabilities'),('pack','capture_readback_pack')):
                with patch.object(pack,'capabilities',side_effect=RuntimeError('private') if target=='capabilities' else None,return_value=2),patch.object(c.retention,'fetch_readback_blobs',side_effect=RuntimeError('private')),self.assertRaisesRegex(ParityFailure,code):c.fetch_blobs(None,meta,'synthetic',Path(folder)/'out')
    def test_safe_public_readback_diagnostics_and_counts(self):
        for error in ('capture_readback_ref_stale','capture_readback_ref_changed','capture_readback_parent','capture_readback_tree','capture_readback_blob_size','capture_readback_blob_hash','capture_readback_payload','capture_readback_pack'):
            clean=c.public_report({'error':error,'readback_ref_attempts':6,'readback_stale_ref_observations':5,'private':'secret'})
            self.assertEqual(clean['error'],error);self.assertEqual(clean['readback_ref_attempts'],6);self.assertNotIn('private',clean)

class TransportTests(unittest.TestCase):
    def test_capture_incomplete_read_retries_and_charges_partial_bytes(self):
        import http.client
        import sync_getnote_to_raw as sync
        from source_sync_inventory import ResponseBudget
        class Response:
            headers={}
            def __init__(self,parts):self.parts=list(parts)
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read1(self,n):
                item=self.parts.pop(0)
                if isinstance(item,Exception):raise item
                return item
        class Opener:
            def __init__(self):self.calls=0
            def open(self,*args,**kwargs):
                self.calls+=1
                return Response([http.client.IncompleteRead(b'{"bad',10)]) if self.calls==1 else Response([b'{"data":{"id":"1"}}',b''])
        class Policy:
            calls=0
            def before_request(self):self.calls+=1
        opener=Opener();policy=Policy();budget=ResponseBudget(1000)
        with patch.object(sync.urllib.request,'build_opener',return_value=opener),patch.object(sync.time,'sleep'):
            raw=sync.api_get(sync.DETAIL_PATH,{'id':'1'},'synthetic','synthetic',max_response_bytes=100,response_budget=budget,request_policy=policy,capture_raw=True)
        self.assertEqual(opener.calls,2);self.assertEqual(policy.calls,2);self.assertEqual(budget.used,len(raw)+5)
    def test_malformed_capture_json_not_retried(self):
        import io
        import sync_getnote_to_raw as sync
        from source_sync_inventory import ResponseBudget
        class Response(io.BytesIO):
            headers={}
        class Opener:
            calls=0
            def open(self,*args,**kwargs):self.calls+=1;return Response(b'{"duplicate":1,"duplicate":2}')
        opener=Opener()
        with patch.object(sync.urllib.request,'build_opener',return_value=opener),self.assertRaises(ParityFailure):sync.api_get(sync.DETAIL_PATH,{'id':'1'},'synthetic','synthetic',max_response_bytes=100,response_budget=ResponseBudget(1000),capture_raw=True)
        self.assertEqual(opener.calls,1)
    def test_compact_number_canonicalization_does_not_expand_budget(self):
        raw=b'{"unknown":['+b','.join([b'1e15']*100000)+b']}'
        data=strict_capture_json(raw);encoded=capture_json_bytes(data)
        self.assertLessEqual(len(encoded),len(raw)+1);self.assertEqual(strict_capture_json(encoded),data)
        self.assertIs(type(strict_capture_json(b'{"updated_at":1e15}')['updated_at']),float)

class RunIntegrationTests(unittest.TestCase):
    def test_actual_fixed_cohort_initial_run_and_verified_resume_without_source_reads(self):
        """Exercises the actual 1931→1923 selection and complete run orchestration."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=root/'repo';repo.mkdir()
            def git(*args,data=None):return subprocess.check_output(['git','-C',str(repo),*args],input=data,stderr=subprocess.DEVNULL)
            git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            get=repo/c.retention.GET;get.mkdir(parents=True)
            (repo/'unrelated.md').write_text('Keep exact non-Get content')
            conflicts={};summaries=[]
            for i in range(1,1932):
                ident=str(i);path=ident+'.md';(get/path).write_text('---\nnote_id: "'+ident+'"\nnote_type: "plain_text"\ncreated_at: "Keep legacy"\n---\nOld preserved body\n')
                conflicts[ident]={'filePath':path,'reason':'legacy_baseline_missing','sourceContentHash':None}
                summaries.append({'id':ident,'note_type':'plain_text','title':'Source','content':'Fresh captured '+ident,'created_at':1776968995,'updated_at':1776968995})
            git('add','.');git('commit','-qm','base');main=git('rev-parse','HEAD').decode().strip();get_tree=git('rev-parse','HEAD:Raw/03 Get').decode().strip()
            local=c.retention.LocalGit(repo);actual_run=local.run
            # Already have real Git objects; network is forbidden in this test.
            local.run=lambda *args,**kwargs: b'' if args[0]=='fetch' else actual_run(*args,**kwargs)
            blobs={};managed={}
            def add(data):
                blob=git('hash-object','-w','--stdin',data=data).decode().strip();blobs[blob]=data;return ('100644','blob',blob,len(data))
            def metadata(commit):
                out={}
                for row in git('ls-tree','-rz','-l',commit,'--',c.retention.GET).split(b'\0'):
                    if not row:continue
                    bits,path=row.split(b'\t',1);mode,kind,blob,size=bits.decode().split();out[path.decode()]=(mode,kind,blob,int(size))
                return out
            source_meta=metadata(main);preview_meta=dict(source_meta);old_meta=dict(source_meta)
            for i in range(10):
                ident=str(900000+i);path='managed-'+ident+'.md';data=('---\nnote_id: "'+ident+'"\n---\nManaged exact\n').encode()
                preview_meta[c.retention.GET+'/'+path]=add(data);managed['api:'+ident]={'origin':'faithful-created','filePath':path,'mirrorSha256':c.digest(data)}
            state={'contract':'get-source-raw-local-2','local_proposal_only':True,'conflictsById':conflicts,'notesByCanonicalId':managed,'pendingById':{'999999':{}}}
            prior={'pendingById':{str(i):{} for i in range(1,9)}}
            sp=c.retention.GET+'/03 State/cloud-sync-state.json';preview_meta[sp]=add(capture_json_bytes(state));old_meta[sp]=add(capture_json_bytes(prior))
            class Remote:
                current=None;writes=[]
                def __init__(self,*args):self.local=local
                def metadata(self):return {'full_name':c.retention.REPOSITORY,'private':True}
                def ref(self,name):return self.current if name==c.REF else (main if name==c.retention.MAIN_REF else c.PROTECTED[name])
                def source_get_tree(self,commit):return get_tree if commit in (main,c.LEGACY_SOURCE) else commit
                def check_source_budget(self,commit):return source_meta if commit==main else (preview_meta if commit==c.LEGACY_NEW_PREVIEW else old_meta)
                def tree_entries(self,revision,recursive=False):
                    if revision==get_tree:return []
                    return [{'path':p.removeprefix(c.retention.GET+'/'),'mode':v[0],'type':v[1],'sha':v[2],'size':v[3]} for p,v in metadata(revision).items()]
                def blob(self,blob,size):
                    data=blobs.get(blob)
                    if data is None:data=local.blob(blob)
                    if len(data)!=size:raise AssertionError('size mismatch')
                    return data
                def commit(self,commit):
                    raw=local.run('cat-file','-p',commit)
                    return {'tree':local.run('rev-parse',commit+'^{tree}').decode().strip(),'parents':[line[7:].decode() for line in raw.split(b'\n\n',1)[0].splitlines() if line.startswith(b'parent ')]}
                def cas(self,expected,commit):
                    if self.current!=expected:raise AssertionError('CAS mismatch')
                    self.writes.append((expected,commit));self.current=commit
            remote=Remote()
            class Source:
                list_calls=0;detail_calls=[]
                def enumerate(self):
                    self.list_calls+=1;raw=capture_json_bytes({'data':{'notes':summaries,'has_more':False}})
                    self.list_receipts=[{'sequence':1,'wire_bytes':len(raw),'wire_sha256':c.digest(raw),'semantic_sha256':c.digest(raw),'items_sha256':c.digest(capture_json_bytes(summaries)),'ids':[n['id'] for n in summaries],'has_more':False}]
                    return deepcopy(summaries),True
                def detail_raw(self,ident):self.detail_calls.append(ident);return capture_json_bytes({'data':summaries[int(ident)-1]})
            source=Source()
            def fetch(remote,meta,token,dest):return {p:remote.blob(row[2],row[3]) for p,row in meta.items()}
            with patch.object(c,'Remote',return_value=remote),patch.object(c.retention,'readback_checkout',return_value=local),patch.object(c,'fetch_blobs',side_effect=fetch):
                result=c.run('synthetic',source,root)
                self.assertEqual(result['captured'],1923);self.assertEqual(result['capture_batches'],39);self.assertEqual(len(remote.writes),40)
                self.assertEqual(source.list_calls,1);self.assertEqual(len(set(source.detail_calls)),1923);self.assertFalse(set(str(i) for i in range(1,9))&set(source.detail_calls))
                before=deepcopy(remote.writes);second=c.run('synthetic',source,root)
                self.assertEqual(second['captured'],1923);self.assertEqual(second['fresh_details'],0);self.assertEqual(source.list_calls,1);self.assertEqual(len(source.detail_calls),1923);self.assertEqual(remote.writes,before)
                from types import SimpleNamespace
                verified=c.run('synthetic',SimpleNamespace(capture_metrics={}),root,verify_only=True)
                self.assertEqual(verified['status'],'retained_capture_verified');self.assertEqual(verified['fresh_details'],0);self.assertEqual(remote.writes,before)
                self.assertEqual(c.public_report(verified)['status'],'retained_capture_verified')
            self.assertEqual(local.tree(main),{p:v for p,v in local.tree(remote.current).items() if not p.startswith(c.PREFIX)})
            self.assertEqual(git('rev-parse','HEAD').decode().strip(),main)

class AdditionalCaptureGuards(unittest.TestCase):
    def test_rehashed_shortened_cohort_cannot_resume(self):
        _,census,_,old=CaptureTests().fixture(2)
        candidates={i:{'filePath':v['filePath']} for i,v in census['eligible'].items()}
        meta={c.retention.GET+'/'+v['filePath']:('100644','blob',v['gitBlob'],len(old[i])) for i,v in census['eligible'].items()}
        del census['eligible']['2']
        with self.assertRaisesRegex(ParityFailure,'capture_cohort'):c.validate_authorized_cohort(census,candidates,[],meta)
    def test_all_selected_missing_still_retains_census_and_final_manifest(self):
        _,census,_,old=CaptureTests().fixture(2)
        census['summaries']={'999':{'id':'999','updated_at':1776968995}};census['source_missing_ids']=['1','2'];h=c.digest(capture_json_bytes(census));validate_legacy_census(census)
        class Source:
            def detail_raw(self,*args):raise AssertionError('Unexpected Get read')
        commits=[];ledger,result=c.capture_batches(census,c.initial_ledger(census,h),{},Source(),old,commits.append)
        self.assertEqual(result['source_missing'],2);self.assertEqual(result['captured'],0);self.assertEqual(len(commits),2);self.assertIn(c.CENSUS,commits[0]);self.assertTrue(ledger['finalized'])
    def test_total_budget_flushes_census_but_prevents_detail_read(self):
        census,ledger,records,source,old,calls,commits=CaptureTests().runner()
        with patch.object(c,'MAX_TOTAL',6*c.MIB):ledger,result=c.capture_batches(census,ledger,records,source,old,commits.append)
        self.assertEqual(calls,[]);self.assertEqual(result['error'],'capture_total_budget');self.assertEqual(len(commits),1)
    def test_current_main_tree_cannot_disguise_a_different_pinned_source_tree(self):
        _,census,_,_=CaptureTests().fixture()
        class Remote:
            def metadata(self):return {'full_name':c.retention.REPOSITORY,'private':True}
            def ref(self,name):return 'c'*40 if name==c.retention.MAIN_REF else c.PROTECTED[name]
            def source_get_tree(self,commit):return census['get_tree'] if commit=='c'*40 else 'd'*40
        with self.assertRaisesRegex(ParityFailure,'capture_source_changed'):c.guard(Remote(),census)
    def test_history_rejects_more_than_fifty_records_in_one_commit(self):
        fixture=GitCaptureTests();fixture.setUp()
        try:
            files={c.CENSUS:b'{}\n',c.LEDGER:b'{}\n',**{c.record_path(str(i)):b'{}\n' for i in range(1,52)}}
            head=c.make_commit(fixture.local,fixture.head,files)
            class Remote:
                def source_get_tree(self,commit):return commit
                def tree_entries(self,commit,recursive):return [{'path':p.removeprefix(c.retention.GET+'/'),'mode':v[0],'type':v[1],'sha':v[2],'size':len(fixture.local.blob(v[2]))} for p,v in fixture.local.tree(commit).items() if p.startswith(c.PREFIX)]
            with self.assertRaisesRegex(ParityFailure,'capture_invalid_ledger'):c.verify_scope_history(fixture.local,head,fixture.census,{'capture_batches':1,'finalized':False,'retained_bytes':sum(map(len,files.values()))},Remote())
        finally:fixture.doCleanups()

class ReportAndPageReceiptTests(unittest.TestCase):
    def test_malformed_public_report_returns_fixed_failure(self):
        for error in ({},[],True,None,3.14):
            self.assertEqual(c.public_report({'error':error})['status'],'failed')
    def test_list_wire_receipts_cover_exact_frozen_items(self):
        notes,census,h,old=CaptureTests().fixture(2)
        raw=capture_json_bytes({'data':{'notes':notes,'has_more':False}})
        receipt={'sequence':1,'wire_bytes':len(raw),'wire_sha256':c.digest(raw),'semantic_sha256':c.digest(raw),'items_sha256':c.digest(capture_json_bytes(notes)),'ids':['1','2'],'has_more':False}
        census['page_receipts']=[receipt];validate_legacy_census(census)
        census['page_receipts'][0]['ids']=['1']
        with self.assertRaises(ParityFailure):validate_legacy_census(census)

class PartialCloneCaptureTests(unittest.TestCase):
    def test_object_only_clone_commit_and_cas_without_old_blob_fetch_or_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);src=root/'source';src.mkdir();bare=root/'remote.git'
            def git(where,*args,**kwargs):
                return subprocess.check_output(['git','-C',str(where),*args],stderr=subprocess.DEVNULL,**kwargs)
            git(src,'init','-q');git(src,'config','user.name','Fixture');git(src,'config','user.email','fixture@example.invalid')
            (src/'Raw/03 Get').mkdir(parents=True);(src/'Raw/03 Get/old.md').write_text('Old note remains byte exact')
            (src/'non-Get.txt').write_text('Non-Get stays unchanged');git(src,'add','.');git(src,'commit','-qm','base')
            base=git(src,'rev-parse','HEAD').decode().strip();old_blob=git(src,'rev-parse','HEAD:Raw/03 Get/old.md').decode().strip()
            subprocess.check_call(['git','clone','-q','--bare',str(src),str(bare)],stderr=subprocess.DEVNULL)
            git(bare,'config','uploadpack.allowFilter','true');git(bare,'config','uploadpack.allowAnySHA1InWant','true')
            original_refs=git(bare,'show-ref');url=bare.as_uri()
            with patch.object(c.retention,'REMOTE_URL',url):
                local=c.retention.readback_checkout(root/'partial',base,'synthetic-fixture-token')
                local.env.pop('EMAIL',None)
                for key in ('GIT_AUTHOR_NAME','GIT_AUTHOR_EMAIL','GIT_COMMITTER_NAME','GIT_COMMITTER_EMAIL'):
                    local.env.pop(key,None)
                self.assertNotEqual(local.command('config','user.name').returncode,0)
                self.assertNotEqual(local.command('cat-file','-e',old_blob).returncode,0)
                # Actual former production failure, not a mocked Git command.
                local.run('read-tree',base)
                b=local.run('hash-object','-w','--stdin',data=b'{}').decode().strip()
                local.run('update-index','--add','--cacheinfo','100644,'+b+','+c.CENSUS)
                self.assertNotEqual(local.command('write-tree').returncode,0)
                index_before=(local.vault/'.git/index').read_bytes()
                first=c.make_commit(local,base,{c.CENSUS:b'{}',c.LEDGER:b'{"batch":1}'})
                self.assertEqual((local.vault/'.git/index').read_bytes(),index_before)
                remote=c.Remote(local,'synthetic-fixture-token');remote.cas(None,first)
                self.assertEqual(git(bare,'rev-parse',c.REF).decode().strip(),first)
                second=c.make_commit(local,first,{c.LEDGER:b'{"batch":2}'})
                remote.cas(first,second)
                self.assertEqual(git(bare,'show',second+':'+c.LEDGER),b'{"batch":2}')
                self.assertEqual(local.tree(base),{p:v for p,v in local.tree(second).items() if not p.startswith(c.PREFIX)})
                self.assertNotEqual(local.command('cat-file','-e',old_blob).returncode,0)
                self.assertEqual(git(bare,'show-ref').replace((second+' '+c.REF+'\n').encode(),b''),original_refs)
                self.assertEqual(local.run('rev-parse','HEAD').decode().strip(),base)
    def test_git_preparation_error_reports_fixed_stage_without_private_stderr(self):
        fixture=GitCaptureTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        original=fixture.local.run
        def failed(*args,**kwargs):
            if args[0]=='write-tree':raise c.retention.RetentionFailure('git_operation_failed')
            return original(*args,**kwargs)
        with patch.object(fixture.local,'run',side_effect=failed):
            with self.assertRaisesRegex(ParityFailure,'^capture_git_tree_write$'):
                c.make_commit(fixture.local,fixture.head,fixture.files)
        self.assertEqual(c.public_report({'error':'capture_git_tree_write'})['error'],'capture_git_tree_write')

class FreshResumeGitCaptureTests(unittest.TestCase):
    def test_each_batch_and_final_ledger_resume_in_fresh_object_only_clone(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);src=root/'source';src.mkdir();bare=root/'remote.git'
            def git(where,*args,**kwargs):return subprocess.check_output(['git','-C',str(where),*args],stderr=subprocess.DEVNULL,**kwargs)
            git(src,'init','-q');git(src,'config','user.name','Fixture');git(src,'config','user.email','fixture@example.invalid')
            (src/'Raw/03 Get').mkdir(parents=True);(src/'Raw/03 Get/old.md').write_text('Original old note')
            (src/'non-Get.txt').write_text('Outside capture');git(src,'add','.');git(src,'commit','-qm','base')
            base=git(src,'rev-parse','HEAD').decode().strip();old_blob=git(src,'rev-parse','HEAD:Raw/03 Get/old.md').decode().strip()
            subprocess.check_call(['git','clone','-q','--bare',str(src),str(bare)],stderr=subprocess.DEVNULL)
            git(bare,'config','uploadpack.allowFilter','true');git(bare,'config','uploadpack.allowAnySHA1InWant','true');original_refs=git(bare,'show-ref')
            with patch.object(c.retention,'REMOTE_URL',bare.as_uri()):
                first_local=c.retention.readback_checkout(root/'first',base,'synthetic-fixture-token')
                first_files={c.CENSUS:capture_json_bytes({'census':'immutable'}),c.LEDGER:capture_json_bytes({'records':list(range(50)),'finalized':False})}
                first_files.update({c.record_path(str(i)):capture_json_bytes({'fixture_record':i,'content':'retained body '+str(i)}) for i in range(1,51)})
                first=c.make_commit(first_local,base,first_files);c.Remote(first_local,'synthetic-fixture-token').cas(None,first)
                prior_ledger=first_files[c.LEDGER];prior_observation_blob=c.retention.oid(first_files[c.record_path('1')])
                for step in ('second','final'):
                    parent=first if step=='second' else second
                    local=c.retention.readback_checkout(root/step,parent,'synthetic-fixture-token')
                    self.assertNotEqual(local.command('cat-file','-e',c.retention.oid(prior_ledger)).returncode,0)
                    self.assertNotEqual(local.command('cat-file','-e',prior_observation_blob).returncode,0)
                    ledger=capture_json_bytes({'records':list(range(100)),'finalized':step=='final'})
                    files={c.LEDGER:ledger}
                    if step=='second':files.update({c.record_path(str(i)):capture_json_bytes({'fixture_record':i,'content':'retained body '+str(i)}) for i in range(51,101)})
                    candidate=c.make_commit(local,parent,files);remote=c.Remote(local,'synthetic-fixture-token')
                    # Actual former production failure: a fresh resume has no
                    # prior mutable ledger blob for pack-objects' delta base.
                    with self.assertRaisesRegex(c.retention.RetentionFailure,'git_operation_failed'):remote.cas(parent,candidate)
                    self.assertEqual(git(bare,'rev-parse',c.REF).decode().strip(),parent)
                    verified=git(bare,'show',parent+':'+c.LEDGER)
                    self.assertEqual(verified,prior_ledger)
                    c.retain_verified_ledger_blob(local,verified,c.retention.oid(verified))
                    remote.cas(parent,candidate)
                    self.assertEqual(git(bare,'rev-parse',c.REF).decode().strip(),candidate)
                    self.assertEqual(git(bare,'show',candidate+':'+c.LEDGER),ledger)
                    self.assertNotEqual(local.command('cat-file','-e',old_blob).returncode,0)
                    self.assertNotEqual(local.command('cat-file','-e',prior_observation_blob).returncode,0)
                    if step=='second':second=candidate
                    else:final=candidate
                    prior_ledger=ledger
                for path,data in first_files.items():
                    if path!=c.LEDGER:self.assertEqual(git(bare,'show',final+':'+path),data)
                self.assertEqual(git(bare,'show-ref').replace((final+' '+c.REF+'\n').encode(),b''),original_refs)
                self.assertEqual(git(bare,'show',final+':Raw/03 Get/old.md'),b'Original old note')
                self.assertEqual(git(bare,'show',final+':non-Get.txt'),b'Outside capture')
    def test_ledger_import_rejects_changed_or_over_budget_bytes_before_git(self):
        from unittest.mock import Mock
        local=Mock()
        for data,oid in ((b'changed','a'*40),(b'x'*(c.MIB+1),c.retention.oid(b'x'*(c.MIB+1)))):
            with self.assertRaisesRegex(ParityFailure,'capture_invalid_ledger'):c.retain_verified_ledger_blob(local,data,oid)
        local.run.assert_not_called()

class ReconciliationAggregateTests(unittest.TestCase):
    def fixture(self,*,note_changes=None,summary=None,body=None,extra_fields=None,duplicate_header=False):
        note={'id':'1','note_type':'plain_text','title':'Exact','content':'Current source','created_at':1776968995,'updated_at':1776968995.0}
        note.update(note_changes or {})
        fields={'note_id':'1','note_type':note['note_type'],'created_at':'Original date bytes','updated_at':'Old date bytes','unknown':{'preserve':True},**(extra_fields or {})}
        summary=note['content'] if summary is None else summary
        body=c.REVIEW_DUPLICATE_BODY if body is None else body+'\n\n'
        text='---\n'+'\n'.join(k+': '+json.dumps(v,ensure_ascii=False) for k,v in fields.items())+'\n---\n\n# Exact\n\n## 🧠 摘要\n'+summary+'\n\n## 📝 正文\n'+body+'## 🔗 来源\nSource metadata\n\n## 📎 附件\nNo attachments\n'
        if duplicate_header:text+='## 🧠 摘要\nAmbiguous duplicate\n'
        old={'1':text.encode()};paths={'1':{'filePath':'1.md','gitBlob':c.retention.oid(old['1']),'sha256':c.digest(old['1'])}}
        census,h=freeze_legacy_census([note],True,paths,[],'a'*40,'b'*40,'2026-10-16T07:00:00Z')
        record=make_legacy_observation(census,h,'1',capture_json_bytes({'data':{'note':note}}),old['1'],'2026-10-16T07:10:00Z')
        records={'1':{'observation':record,'proposal':c.proposal(record,census,old['1'])}}
        return census,records,old
    def test_exact_whole_legacy_section_coherence_preserves_metadata(self):
        args=self.fixture();before=deepcopy(args);v=c.reconciliation_counts(*args)
        self.assertEqual(v['review_legacy_content_section_exact'],1);self.assertEqual(v['review_known_source_text_all_accounted'],1)
        self.assertEqual(v['review_whole_body_different'],1);self.assertEqual(v['review_known_legacy_text_regions_extra'],0)
        self.assertEqual(v['review_frontmatter_bytes_preserved'],1);self.assertEqual(args,before)
    def test_substring_and_whitespace_are_not_whole_section_equality(self):
        for summary in ('Extra Current source','Current source extra',' Current source','Current source '):
            v=c.reconciliation_counts(*self.fixture(summary=summary))
            self.assertEqual(v['review_legacy_content_section_exact'],0);self.assertEqual(v['review_known_source_text_partial'],1);self.assertEqual(v['review_known_legacy_text_regions_extra'],1)
    def test_duplicate_headers_are_ambiguous(self):
        v=c.reconciliation_counts(*self.fixture(duplicate_header=True))
        self.assertEqual(v['review_legacy_section_ambiguous'],1);self.assertEqual(v['review_known_source_text_partial'],1)
    def test_full_source_field_coverage_keeps_extra_excerpt_and_references_separate(self):
        args=self.fixture(note_changes={'web_page':{'content':'Unrepresented complete article'},'attachments':[{'url':'https://example.invalid/asset'}]},extra_fields={'web_excerpt':'Original excerpt retained','attachments':['old-reference']})
        v=c.reconciliation_counts(*args)
        self.assertEqual(v['review_legacy_content_section_exact'],1);self.assertEqual(v['review_known_source_text_partial'],1)
        self.assertEqual(v['review_known_legacy_text_regions_extra'],1);self.assertEqual(v['review_asset_reference_review_required'],1)
    def test_audio_original_and_summary_are_separately_whole_matched(self):
        v=c.reconciliation_counts(*self.fixture(note_changes={'note_type':'recorder_audio','audio':{'original':'Exact original','play_url':'https://example.invalid/audio'}},body='Exact original'))
        self.assertEqual(v['review_known_source_text_all_accounted'],1);self.assertEqual(v['review_known_legacy_text_regions_extra'],0);self.assertEqual(v['review_asset_reference_review_required'],1)
    def test_empty_and_unsupported_source_fields_are_not_coherence_success(self):
        empty=c.reconciliation_counts(*self.fixture(note_changes={'content':''}))
        self.assertEqual(empty['review_known_source_text_empty'],1)
        unsupported=c.reconciliation_counts(*self.fixture(note_changes={'audio':{'original':{'text':'Unsupported'}}}))
        self.assertEqual(unsupported['review_known_source_text_partial'],1)
    def test_unsupported_parent_shapes_never_count_as_source_text_accounted(self):
        for change in ({'audio':'unsupported'},{'web_page':['unmapped article']},{'audio':[]}):
            v=c.reconciliation_counts(*self.fixture(note_changes=change))
            self.assertEqual(v['review_known_source_text_all_accounted'],0);self.assertEqual(v['review_known_source_text_partial'],1)
    def test_legacy_only_references_are_read_from_preserved_header(self):
        for fields in ({'audio_url':'https://example.invalid/old'},{'web_url':'https://example.invalid/old'},{'attachments':['old-file.png']}):
            v=c.reconciliation_counts(*self.fixture(extra_fields=fields))
            self.assertEqual(v['review_asset_reference_review_required'],1)
    def test_other_legacy_regions_never_claimed_classified_or_safe_to_drop(self):
        args=self.fixture();v=c.reconciliation_counts(*args)
        self.assertEqual(v['review_known_legacy_text_regions_extra'],0)
        self.assertEqual(v['review_other_legacy_regions_unclassified'],1)
        self.assertFalse(c.public_report({})['legacy_regions_complete_claimed'])
    def test_public_aggregate_counts_are_complete_bounded_and_never_adoption(self):
        counts=c.reconciliation_counts(*self.fixture())
        base={'status':'capture_complete_for_review','eligible':1,'captured':1,'remaining':0,'source_missing':0,'capture_batches':1,'raw_bytes':0,'retained_bytes':0,'refs_verified':True,'readback_verified':True,**counts,'private_payload':'must disappear','mirror_adopted':1,'apply_ready':True}
        clean=c.public_report(base);self.assertEqual(clean['status'],'capture_complete_for_review');self.assertNotIn('private_payload',clean);self.assertEqual(clean['mirror_adopted'],0);self.assertFalse(clean['apply_ready'])
        self.assertEqual(c.public_report(clean),clean)
        for change in ({'review_whole_body_exact':1},{'review_frontmatter_bytes_preserved':0},{'review_known_source_text_partial':True},{'review_known_legacy_text_regions_extra':-1}):
            bad=c.public_report({**base,**change});self.assertEqual(bad['status'],'failed');self.assertEqual(bad['error'],'capture_invalid_report')
        bad=dict(base);bad.pop(c.REVIEW_KEYS[0]);self.assertEqual(c.public_report(bad)['status'],'failed')

class VerifyOnlyCliTests(unittest.TestCase):
    def test_verification_cli_never_loads_source_credentials_and_filters_result(self):
        import sync_getnote_to_raw as sync
        result={'status':'retained_capture_verified','verification_only':True,'eligible':2,'captured':1,'remaining':1,'source_missing':0,'capture_batches':1,'raw_bytes':100,'retained_bytes':200,'refs_verified':True,'readback_verified':True}
        with patch.object(sync,'load_credentials',side_effect=AssertionError('Source credentials forbidden')),patch.object(c,'Source',side_effect=AssertionError('Get transport forbidden')),patch.object(c,'run',return_value=result) as run,patch('builtins.print') as output:
            self.assertEqual(c.main(['--verify-only']),0)
        self.assertTrue(run.call_args.kwargs['verify_only']);self.assertFalse(hasattr(run.call_args.args[1],'detail_raw'))
        clean=json.loads(output.call_args.args[0]);self.assertEqual(clean['status'],'retained_capture_verified');self.assertTrue(clean['verification_only'])
        self.assertEqual(c.public_report(clean),clean)
