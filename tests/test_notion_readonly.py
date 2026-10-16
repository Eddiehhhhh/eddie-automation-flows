from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_readonly as r
import get_notion_sink_plan as p
import test_legacy_adapter as fixture


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, results): self.results = list(results); self.calls = []
    def open(self, request, timeout):
        self.calls.append((request, timeout)); result = self.results.pop(0)
        if isinstance(result, Exception): raise result
        return Response(result)


class TransportTests(unittest.TestCase):
    def transport(self, values):
        opener = Opener(values); waits = []
        return r.NotionReadTransport('synthetic-token', opener=opener, sleep=waits.append, clock=lambda: 1), opener, waits
    def test_only_fixed_host_read_routes(self):
        t, o, _ = self.transport([b'{"object":"list"}'])
        self.assertEqual(t('POST', '/search', {}), {'object': 'list'})
        self.assertEqual(o.calls[0][0].full_url, 'https://api.notion.com/v1/search')
        self.assertEqual(o.calls[0][0].get_header('Notion-version'), '2025-09-03')
        for method, path in [('PATCH','/pages/'+'a'*32), ('POST','/pages'), ('DELETE','/blocks/'+'b'*32), ('GET','https://evil.invalid'), ('GET','/users/me'), ('GET','/blocks/'+'a'*32+'/children?page_size=100&write=true')]:
            with self.assertRaisesRegex(r.ReadFailure, '^invalid_read_route$'): t(method,path,None)
        self.assertEqual(len(o.calls), 1)
    def test_permission_denial_never_retried_or_echoed(self):
        for code in (401, 403):
            t, o, _ = self.transport([urllib.error.HTTPError('secret-url',code,'PRIVATE BODY',{},None)])
            with self.assertRaisesRegex(r.ReadFailure, '^permission_denied$') as e: t('POST','/search',{})
            self.assertEqual(e.exception.status, code); self.assertEqual(len(o.calls), 1); self.assertEqual(t.retries, 0)
    def test_readonly_retry_bound_and_retry_after_cap(self):
        error = lambda: urllib.error.HTTPError('redacted',429,'redacted',{'Retry-After':'999'},None)
        t, o, waits = self.transport([error(), error(), b'{}'])
        self.assertEqual(t('POST','/search',{}), {}); self.assertEqual(t.retries,2); self.assertEqual(len(o.calls),3); self.assertEqual(max(waits),60)
        t, o, _ = self.transport([TimeoutError('secret')]*3)
        with self.assertRaisesRegex(r.ReadFailure,'^read_transport_failed$'): t('POST','/search',{})
        self.assertEqual(len(o.calls),3)
    def test_response_and_request_limits(self):
        t, o, _ = self.transport([b'x'*(r.MAX_RESPONSE+1)])
        with self.assertRaisesRegex(r.ReadFailure,'^response_budget$'): t('POST','/search',{})
        with self.assertRaisesRegex(r.ReadFailure,'^request_budget$'): t('POST','/search',{'x':'y'*20000})
        self.assertEqual(len(o.calls),1)
    def test_malformed_json_and_redirect_do_not_leak(self):
        t, _, _ = self.transport([b'PRIVATE BODY'])
        with self.assertRaisesRegex(r.ReadFailure,'^read_transport_failed$'): t('POST','/search',{})
        with self.assertRaisesRegex(r.ReadFailure,'^invalid_read_route$'): r.NoRedirect().redirect_request(None,None,None,None,None,None)


class GitRemote(r.git.GitHubRemote):
    def __init__(self, f): self.f=f; self.head=f.head; self.reads=[]; self.ref_reads=0
    def metadata(self): return {'full_name':r.git.REPOSITORY, 'private':True}
    def ref(self, name):
        assert name == r.git.MAIN_REF; self.ref_reads += 1; return self.head
    def commit(self, oid):
        lines=self.f.git('cat-file','-p',oid).split(b'\n\n',1)[0].decode().splitlines()
        return {'tree':next(x[5:] for x in lines if x.startswith('tree ')), 'parents':[x[7:] for x in lines if x.startswith('parent ')]}
    def tree_entries(self, oid, recursive=False):
        args=['ls-tree','-z']+(['-r'] if recursive else [])+[oid]; result=[]
        for row in self.f.git(*args).split(b'\0'):
            if not row: continue
            meta,path=row.split(b'\t',1); mode,kind,sha=meta.decode().split()
            item={'mode':mode,'type':kind,'sha':sha,'path':path.decode()}
            if kind=='blob': item['size']=int(self.f.git('cat-file','-s',sha))
            result.append(item)
        return result
    def blob(self, oid, expected):
        self.reads.append(oid); value=self.f.git('cat-file','blob',oid)
        assert len(value)==expected; return value


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LegacyAdapterTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.remote=GitRemote(self.f); self.parent=self.f.head; self.ds='a'*32
        self.snapshots={}
        for ident in ('1','2'):
            page=ident*32
            self.snapshots[ident]={'complete':True,'native_blocks_available':True,'page_id':page,'parent_data_source':self.ds,'id_property':ident,'identity_query_complete':True,'exact_id_matches':[page],'last_edited_time':'2026-10-16T00:00:00Z','properties_raw':{'笔记 ID':{'type':'rich_text','rich_text':[p.text_run(ident)]}},'recursive_blocks':[]}
        self.observation={'snapshots':self.snapshots,'report':{'blocks':0,'requests':9}}
        for obj,key,value in [(r,'CAPTURE',self.f.head),(r,'ANCHOR',self.f.anchor),(p,'DATA_SOURCE_SHA256',p.digest(self.ds)),(p,'COHORT_SHA256',p.digest(['1','2']))]:
            guard=patch.object(obj,key,value);guard.start();self.addCleanup(guard.stop)
    def run_it(self, collector=None):
        return r.run_readonly(self.remote,lambda *a: self.fail('No network'),self.parent,collector=collector or (lambda _:deepcopy(self.observation)))
    def test_actual_git_committed_two_sources_only_and_missing_ownership(self):
        before=self.f.git('show-ref'); out=self.run_it(); report=r.public_report(out)
        self.assertEqual(report['status'],'notion_readonly_verified');self.assertEqual(report['planner']['blocked'],2)
        self.assertEqual(report['reason_counts'],{'missing_block_ownership':2});self.assertEqual(len(self.remote.reads),3)
        census_oid=self.f.git('rev-parse',self.f.head+':'+r.capture.CENSUS).decode().strip()
        self.assertEqual(set(self.remote.reads),{census_oid,p.git_blob(self.f.old['1']),p.git_blob(self.f.old['2'])})
        self.assertEqual(before,self.f.git('show-ref'));self.assertFalse(report['ownership_adopted']);self.assertEqual(report['get_requests'],0)
        for secret in ('Exact source','User prefix','Old date','note_id','https://',self.ds): self.assertNotIn(secret,json.dumps(report))
        again=self.run_it();self.assertEqual(out,again)
    def test_native_collector_through_real_git_source_to_planner(self):
        from get_mirror_incremental_replay import freeze_legacy_census
        native = r.native; ids = ['1234567890123456789', '2234567890123456789']
        pages = ['1'*32, '2'*32]; db = 'b'*32; entries = {}; summaries = []
        for short, ident in zip(('1','2'), ids):
            value = self.f.old[short].replace(('note_id: "'+short+'"').encode(), ('note_id: "'+ident+'"').encode())
            (self.f.repo/r.git.GET/(short+'.md')).write_bytes(value)
            entries[ident] = {'filePath':short+'.md','gitBlob':p.git_blob(value),'sha256':p.digest(value)}
            summaries.append({**self.f.notes[short], 'id':ident})
        source = self.f.commit('two nineteen-digit source fixture')
        pending = [str(i) for i in range(90,99)]
        summaries += [x for x in self.f.summaries if x['id'] in pending]
        tree = self.f.git('rev-parse',source+':'+r.git.GET).decode().strip()
        census, census_sha = freeze_legacy_census(summaries,True,entries,pending,source,tree,'2026-10-16T07:00:00Z')
        (self.f.repo/r.capture.CENSUS).write_bytes(fixture.a.encoded(census))
        head = self.f.commit('pinned census fixture'); self.remote.head = head
        page_rows = {page:{'object':'page','id':page,'parent':{'type':'data_source_id','data_source_id':self.ds},'archived':False,'last_edited_time':'2026-10-16T00:00:00Z','properties':{'笔记 ID':{'type':'rich_text','rich_text':[p.text_run(ident)]}}} for page,ident in zip(pages,ids)}
        listing = lambda rows: {'object':'list','results':rows,'has_more':False,'next_cursor':None}
        calls = []
        def request(method,path,body):
            calls.append((method,path))
            if path == '/search': return listing([{'object':'page','id':page} for page in pages])
            if path.startswith('/pages/'): return deepcopy(page_rows[path.rsplit('/',1)[1]])
            if path == '/data_sources/'+self.ds: return {'object':'data_source','id':self.ds,'parent':{'database_id':db},'properties':{'笔记 ID':{'type':'rich_text'}}}
            if path.endswith('/query'):
                ident=body['filter']['rich_text']['equals']; return listing([deepcopy(page_rows[pages[ids.index(ident)]])])
            if path.startswith('/blocks/'): return listing([])
            self.fail('unexpected read route')
        def collector(callback): return native.collect(callback,page_pins={native.sha(page):native.sha(ident) for page,ident in zip(pages,ids)},data_source_pin=native.sha(self.ds),database_pin=native.sha(db))
        with patch.object(r,'CAPTURE',head), patch.object(r,'ANCHOR',{'census_sha256':census_sha}), patch.object(p,'COHORT_SHA256',p.digest(ids)):
            out=r.run_readonly(self.remote,request,head,collector=collector)
        report=r.public_report(out)
        self.assertEqual(report['status'],'notion_readonly_verified',report);self.assertEqual(report['native_pages'],2)
        self.assertEqual(report['reason_counts'],{'missing_block_ownership':2});self.assertEqual(report['native_requests'],len(calls))
        self.assertEqual(len(self.remote.reads),3);self.assertEqual(report['external_writes'],0)
        self.assertTrue(all(r.read_route(method,path) for method,path in calls))

    def test_denied_native_access_stops_before_source_reads(self):
        def denied(_): raise r.ReadFailure('permission_denied',403)
        out=self.run_it(denied);self.assertEqual(out['reason'],'permission_denied');self.assertEqual(out['stage'],'native_read');self.assertEqual(self.remote.reads,[])
    def test_wrong_main_and_wrong_source_hash_fail_closed(self):
        self.remote.head='f'*40;self.assertEqual(self.run_it()['reason'],'source_changed');self.assertEqual(self.remote.reads,[])
        self.remote.head=self.parent
        with patch.object(r,'ANCHOR',{**self.f.anchor,'census_sha256':'f'*64}):self.assertEqual(self.run_it()['reason'],'source_anchor')
    def test_final_main_race_does_not_claim_success(self):
        original=self.remote.ref
        def ref(name):
            result=original(name)
            return 'f'*40 if self.remote.ref_reads==3 else result
        self.remote.ref=ref;out=self.run_it();self.assertEqual(out['stage'],'final_readback');self.assertEqual(out['reason'],'source_changed')
    def test_private_exceptions_are_fixed_code_only(self):
        def bad(_): raise ValueError('PRIVATE TITLE TOKEN URL')
        out=r.public_report(self.run_it(bad));self.assertEqual(out['reason'],'other');self.assertNotIn('PRIVATE',json.dumps(out))
    def test_report_strips_extras_and_rejects_malformed(self):
        valid=self.run_it();valid['private']='secret';self.assertNotIn('private',r.public_report(valid))
        values=[None,[],{'status':'PRIVATE'},dict(valid, native_pages=True),dict(valid, native_requests=-1),dict(valid, planner={**valid['planner'],'contract':'PRIVATE'}),dict(valid, reason_counts={'PRIVATE':1}),dict(valid, source_commit='private'),dict(valid, external_writes=1),dict(valid, planner={**valid['planner'],'decision':{}})]
        for value in values:
            out=r.public_report(value);self.assertEqual(out['status'],'notion_readonly_failed');self.assertNotIn('PRIVATE',json.dumps(out))
    def test_invalid_parent_cli_never_reads_credentials_or_transport(self):
        with patch.object(r.os,'environ',{}),patch.object(r,'NotionReadTransport',side_effect=AssertionError('must not initialize')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(r.main(['--expected-parent','invalid']),1)
        self.assertEqual(json.loads(out.getvalue())['status'],'notion_readonly_failed')
    def test_workflow_readonly_scope_and_safe_report(self):
        workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml').read_text()
        step=workflow.split("if: inputs.mode == 'notion_readonly'",1)[1].split('- name:',1)[0]
        self.assertIn('secrets.NOTION_API_KEY',step);self.assertIn('secrets.PRIVATE_VAULT_TOKEN',step)
        self.assertNotIn('GETNOTE_',step);self.assertNotIn('--apply',step);self.assertNotIn('upload-artifact',workflow)
        self.assertIn('test "$APPLY" != true',step);self.assertIn('test -z "$APPROVED_PAYLOAD_SHA256"',step)
        self.assertIn('get_notion_readonly.py --publish-report',workflow);self.assertIn('contents: read',workflow)


if __name__=='__main__': unittest.main()


class PageDifferenceReportTests(unittest.TestCase):
    def test_only_fixed_page_difference_categories_are_public(self):
        import get_notion_readonly as m
        base={'status':'notion_readonly_failed','stage':'native_read','reason':'page_changed_during_read'}
        report=m.public_report({**base,'page_difference_categories':['properties','edit_metadata'],'private':'secret'})
        self.assertEqual(report['page_difference_categories'],['edit_metadata','properties']);self.assertNotIn('private',report)
        for bad in (['private-page-id'],[{}],'private'):
            report=m.public_report({**base,'page_difference_categories':bad});self.assertNotIn('private',str(report));self.assertNotIn('page_difference_categories',report)

class HistoricalRangeIntegrationTests(unittest.TestCase):
    def fixture(self):
        import test_initial_range as history
        from types import SimpleNamespace
        case=history.Tests();ev,data,snapshot,proof=case.hash_fixture()
        template=history.m.public_proof_template(proof,p.digest(proof))
        row={'path':proof['source']['path'],'md_blob':proof['source']['blob'],'data_source_id':history.DS}
        calls=[]
        def budget(commit):
            calls.append(commit)
            if commit!=proof['source']['commit']:raise AssertionError('wrong history')
            return {row['path']:('100644','blob',row['md_blob'],len(data))}
        return history,template,{history.N:row},{row['md_blob']:data},{history.N:snapshot},SimpleNamespace(check_source_budget=budget),calls
    def test_exact_historical_proof_full_native_match_and_no_private_output(self):
        h,template,mapping,blobs,snapshots,remote,calls=self.fixture()
        out=r.historical_ranges(remote,mapping,blobs,snapshots,template,p.digest(template))
        self.assertEqual(out['proofs'],1);self.assertEqual(out['candidates'],1);self.assertEqual(out['managed_blocks'],2);self.assertEqual(out['protected_blocks'],4)
        self.assertEqual(calls,[template['source_commit']])
        for secret in (h.N,h.P,'Nested title','Historical preview notice','Raw/03 Get'):self.assertNotIn(secret,json.dumps(out))
    def test_old_membership_and_current_blob_must_both_match(self):
        h,template,mapping,blobs,snapshots,remote,calls=self.fixture();remote.check_source_budget=lambda _:{}
        with self.assertRaisesRegex(r.ReadFailure,'source_anchor'):r.historical_ranges(remote,mapping,blobs,snapshots,template,p.digest(template))
        h,template,mapping,blobs,snapshots,remote,calls=self.fixture();mapping[h.N]['md_blob']='f'*40
        with self.assertRaisesRegex(r.ReadFailure,'source_anchor'):r.historical_ranges(remote,mapping,blobs,snapshots,template,p.digest(template))
    def test_human_change_is_blocked_not_owned(self):
        h,template,mapping,blobs,snapshots,remote,calls=self.fixture();snapshots[h.N]['recursive_blocks'][-2]['payload']['rich_text']=[p.text_run('Human edit')]
        out=r.historical_ranges(remote,mapping,blobs,snapshots,template,p.digest(template));self.assertEqual(out['blocked'],1);self.assertEqual(out['reason_counts'],{'no_unique_range':1});self.assertEqual(out['managed_blocks'],0)
    def test_new_aggregate_filter_rejects_forged_counts_and_keys(self):
        valid={'proofs':1,'candidates':1,'blocked':0,'unproven_notes':1,'managed_blocks':39,'protected_blocks':8,'reason_counts':{}}
        self.assertTrue(r.valid_historical(valid))
        for change in ({'proofs':True},{'candidates':2},{'reason_counts':{'PRIVATE':1}},{'extra':'PRIVATE'},{'managed_blocks':-1}):self.assertFalse(r.valid_historical(dict(valid,**change)))
