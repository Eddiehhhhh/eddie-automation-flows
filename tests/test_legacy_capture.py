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
