from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import sync_notion_sources_to_raw as m


def uid(number):return f'{number:08x}-1111-4111-8111-111111111111'
def rt(text,**kw):return {'type':'text','text':{'content':text},'plain_text':text,**kw}
def page(number=100,source=1,text='完整属性正文',blocks=False):
    return {'id':uid(number),'parent':{'type':'database_id','database_id':uid(source)},'created_time':'2026-01-02T03:04:05.000Z','last_edited_time':'2026-10-30T04:05:06.000Z',
        'properties':{'Name':{'id':'title','type':'title','title':[rt('完整标题')]},'Note':{'id':'note','type':'rich_text','rich_text':[rt(text)]},'Date':{'id':'date','type':'date','date':{'start':'2026-10-29','end':None,'time_zone':None}}}}
def limits(**values):return {'max_pages':50,'per_source_pages':50,'max_seconds':900,'max_requests':500,'max_query_pages':20,**values}
def settings(sources=1,**values):return {'sources':[{'key':'source'+str(i),'database_id':uid(i),'date_property':'Date','name':'合成来源'+str(i),'owner':'generic'} for i in range(1,sources+1)],'batch':limits(**values)}
def prop_response(prop):
    kind=prop['type'];rows=[{'object':'property_item','id':prop['id'],'type':kind,kind:item} for item in prop[kind]]
    return {'object':'list','type':'property_item','results':rows,'has_more':False,'next_cursor':None,'property_item':{'id':prop['id'],'type':kind,'next_url':None}}


class API:
    def __init__(self,pages=None):
        self.pages=pages or [page()];self.calls=[];self.blocks={};self.query_hook=None;self.page_hook=None
    def __call__(self,method,path,payload=None,*,api_version=None):
        self.calls.append((method,path,deepcopy(payload)))
        if method=='POST':
            if self.query_hook:return self.query_hook(method,path,payload)
            source=path.split('/')[2];rows=[deepcopy(p) for p in self.pages if p['parent']['database_id']==source]
            return {'results':rows,'has_more':False,'next_cursor':None}
        parts=path.split('?')[0].split('/')
        if parts[1]=='databases':return {'id':parts[2],'properties':{'Name':{'type':'title'},'Date':{'type':'date'},'添加日期':{'type':'date'},'日期':{'type':'date'}}}
        if parts[1]=='blocks':return deepcopy(self.blocks.get(parts[2],{'results':[],'has_more':False,'next_cursor':None}))
        if parts[1]=='pages':
            if len(parts)>3 and parts[3]=='markdown':return {}
            original=next(p for p in self.pages if p['id']==parts[2])
            if len(parts)>3:
                prop=next(v for v in original['properties'].values() if v['id']==parts[4]);return prop_response(prop)
            value=deepcopy(original)
            if self.page_hook:value=self.page_hook(value)
            return value
        raise AssertionError('unhandled synthetic request')


class Sources(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);subprocess.run(['git','init','-q',str(self.root)],check=True)
        self.guard=patch.object(m.cloud_private_checkout,'verify_private_checkout');self.guard.start()
    def tearDown(self):self.guard.stop();self.tmp.cleanup()
    def collect(self,api=None,config=None,**kw):return m.collect(self.root,config or settings(),'synthetic',request=api or API(),apply=True,**kw)
    def state(self):return json.loads((self.root/m.STATE).read_bytes())
    def current(self):
        record=next(iter(self.state()['sources']['source1']['pages'].values()));return self.root/m.BASE/record['current_path'],record
    def test_full_source_property_envelopes_and_readable_attributes(self):
        report=self.collect();self.assertTrue(report['selected_scope_complete']);path,record=self.current();text=path.read_text()
        self.assertIn('完整属性正文',text);self.assertIn('source_date: "2026-10-29"',text);self.assertIn('2026-01-02T03:04:05.000Z',text);self.assertIn('2026-10-30T04:05:06.000Z',text)
        data=json.loads((path.parent/'source.json').read_bytes());self.assertEqual(data['original_page'],page());self.assertEqual(set(data['property_responses']),{'note','title'});self.assertEqual(record['property_proof_version'],1)
        self.assertIn('stage_only: true',(self.root/m.BASE/'index.md').read_text());self.assertTrue(m.eligible_path(self.root,path));self.assertFalse(m.eligible_path(self.root,path.parent/'source.json'));self.assertFalse(m.eligible_path(self.root,self.root/m.BASE/'index.md'))
    def test_dryrun_zero_objects_and_state(self):
        report=m.collect(self.root,settings(),'synthetic',request=API(),apply=False)
        self.assertFalse((self.root/m.STATE).exists());self.assertFalse((self.root/m.BASE).exists());self.assertFalse(report['selected_scope_complete'])
    def test_private_repository_guard_zero_writes(self):
        for reason in ('public_repository','wrong_repository','missing_proof'):
            with patch.object(m.cloud_private_checkout,'verify_private_checkout',side_effect=ValueError(reason)):
                with self.assertRaises(ValueError):self.collect()
            self.assertFalse((self.root/m.BASE).exists());self.assertFalse((self.root/m.STATE).exists())
    def test_budget_soft_exit_persists_first_page_and_resumes(self):
        api=API([page(100),page(101),page(102)]);config=settings(max_pages=1)
        report=self.collect(api,config);self.assertEqual(report['written'],1);self.assertEqual(report['soft_exit'],'page_budget');self.assertFalse(report['selected_scope_complete'])
        first=self.current()[1]['current_path'];api.calls.clear();second=self.collect(api,config)
        self.assertEqual(second['written'],1);self.assertFalse(any(method=='POST' for method,path,payload in api.calls));self.assertTrue((self.root/m.BASE/first).exists())
        third=self.collect(api,config);self.assertEqual(third['written'],1);self.assertTrue(third['selected_scope_complete']);self.assertEqual(len(self.state()['sources']['source1']['pages']),3)
    def test_unchanged_complete_receipt_skips_hydration(self):
        self.collect();api=API();report=self.collect(api)
        self.assertEqual(report['written'],0);self.assertEqual(report['unchanged'],1);self.assertEqual(len(api.calls),2);self.assertTrue(report['selected_scope_complete'])
    def test_old_unproved_receipt_is_not_complete_skip(self):
        self.collect();state=self.state();state['sources']['source1']['pages'][uid(100)]['property_proof_version']=0;(self.root/m.STATE).write_bytes(m.encoded(state))
        api=API();report=self.collect(api);self.assertEqual(report['written'],1);self.assertTrue(any('/properties/' in path for method,path,payload in api.calls))
    def test_manual_current_edit_holds_not_overwritten_and_stays_readable(self):
        self.collect();path,record=self.current();path.write_text('人工内容');report=self.collect(API([page(text='源已改')]))
        self.assertFalse(report['selected_scope_complete']);self.assertEqual(path.read_text(),'人工内容');self.assertIn(uid(100),report['held']);self.assertTrue(m.eligible_path(self.root,path));self.assertIn(str(path.relative_to(self.root)),m.active_selection(self.root)['edited'])
    def test_history_preserved_only_state_current_eligible(self):
        self.collect();old,record=self.current();api=API([page(text='新版')]);api.pages[0]['last_edited_time']='2026-10-31T04:05:06.000Z';self.collect(api)
        new,record=self.current();self.assertNotEqual(old,new);self.assertTrue(old.exists());self.assertFalse(m.eligible_path(self.root,old));self.assertTrue(m.eligible_path(self.root,new));self.assertEqual(len(record['versions']),2)
    def test_credentials_hold_one_without_writing_value(self):
        fake='ghp_'+'A'*36;report=self.collect(API([page(100,text=fake),page(101,text='正常')]))
        self.assertEqual(report['held'][uid(100)],'potential_credential_in_source');self.assertEqual(report['written'],1)
        for path in (self.root/m.BASE).rglob('*'):
            if path.is_file():self.assertNotIn(fake.encode(),path.read_bytes())
    def test_parent_change_does_not_capture(self):
        api=API();api.page_hook=lambda value:{**value,'parent':{'database_id':uid(2)}};report=self.collect(api)
        self.assertEqual(report['held'][uid(100)],'source_parent_identity_changed');self.assertFalse(self.state()['sources']['source1']['pages'])
    def test_source_change_after_body_read_holds(self):
        api=API();calls=[0]
        def change(value):
            calls[0]+=1
            if calls[0]>1:value['last_edited_time']='2026-11-01T00:00:00.000Z'
            return value
        api.page_hook=change;report=self.collect(api);self.assertEqual(report['held'][uid(100)],'source_changed_during_capture');self.assertFalse(self.state()['sources']['source1']['pages'])
    def test_pagination_missing_cursor_never_commits_rows_and_can_recover(self):
        api=API();api.query_hook=lambda *_:{'results':[page()],'has_more':True,'next_cursor':None};report=self.collect(api)
        self.assertEqual(report['held']['source1'],'source_inventory_cursor_incomplete');self.assertFalse(self.state()['sources']['source1']['inventory']['records'])
        self.assertEqual(self.collect(API())['written'],1)
    def test_inventory_query_budget_persists_cursor_and_resumes(self):
        api=API();api.query_hook=lambda method,path,payload:({'results':[page(100)],'has_more':True,'next_cursor':'synthetic-cursor'} if 'start_cursor' not in payload else {'results':[page(101)],'has_more':False,'next_cursor':None})
        config=settings(max_query_pages=1);report=self.collect(api,config);self.assertFalse(report['selected_scope_complete']);self.assertFalse(self.state()['sources']['source1']['inventory']['complete'])
        api.pages=[page(100),page(101)];report=self.collect(api,config);self.assertEqual(report['written'],2);self.assertTrue(report['selected_scope_complete'])
    def test_block_nested_pagination_and_unsupported_failclosed(self):
        api=API();api.blocks[uid(100)]={'results':[{'id':uid(800),'type':'paragraph','paragraph':{'rich_text':[rt('完整页面内容',annotations={'bold':True})]},'has_children':True}],'has_more':False,'next_cursor':None}
        api.blocks[uid(800)]={'results':[{'id':uid(801),'type':'paragraph','paragraph':{'rich_text':[rt('子内容')]},'has_children':False}],'has_more':False,'next_cursor':None}
        self.collect(api);text=self.current()[0].read_text();self.assertIn('**完整页面内容**',text);self.assertIn('子内容',text)
        other=API([page(101)]);other.blocks[uid(101)]={'results':[{'id':uid(900),'type':'unsupported'}],'has_more':False,'next_cursor':None}
        self.assertEqual(self.collect(other)['held'][uid(101)],'unsupported_source_block')
    def test_real_bytes_materialized_property_cover_and_body_image(self):
        api=API();url='https://files.example.invalid/opaque';api.pages[0]['cover']={'type':'external','external':{'url':url}}
        api.pages[0]['properties']['Files']={'id':'files','type':'files','files':[{'name':'原图.png','type':'external','external':{'url':url}}]}
        api.blocks[uid(100)]={'results':[{'id':uid(900),'type':'image','image':{'type':'external','external':{'url':url},'caption':[]},'has_children':False}],'has_more':False,'next_cursor':None}
        seen=[];raw=b'\x89PNG\r\nfixture'
        def download(target):seen.append(target);return raw
        self.collect(api,downloader=download);path,record=self.current();text=path.read_text();self.assertNotIn(url,text);self.assertEqual(seen,[url]);asset=next(path.parent.glob('assets/*'));self.assertEqual(asset.suffix,'.png');self.assertEqual(asset.read_bytes(),raw);self.assertIn('assets/'+asset.name,text)
    def test_literal_markdown_image_downloaded_without_api_auth(self):
        api=API([page(text='![](https://files.example.invalid/source.png)')]);self.collect(api,downloader=lambda url:b'\x89PNGfixture')
        path,_=self.current();self.assertNotIn('![](https://',path.read_text());self.assertEqual(len(list(path.parent.glob('assets/*'))),1)
    def test_attachment_failure_and_budget_traceable(self):
        api=API();api.pages[0]['properties']['Files']={'id':'files','type':'files','files':[{'name':'a.bin','type':'external','external':{'url':'https://files.example.invalid/a'}}]}
        with patch.object(m.shared,'MAX_ASSET_BYTES',2):report=self.collect(api,downloader=lambda url:b'123')
        self.assertEqual(report['held'][uid(100)],'asset_file_budget');self.assertFalse(self.state()['sources']['source1']['pages'])
    def test_private_index_manual_edits_hold(self):
        self.collect();path=self.root/m.BASE/'index.md';path.write_text('人工入口');report=self.collect()
        self.assertEqual(report['held']['index'],'source_index_edited');self.assertEqual(path.read_text(),'人工入口');self.assertFalse(report['selected_scope_complete'])
    def test_flomo_strict_page_memo_routes_pending_and_mixed_page_captured(self):
        config=settings();config['sources'][0]['owner']='flomo';api=API([page(100),page(101)])
        api.pages[0]['properties']['Link']={'id':'link','type':'url','url':'https://flomoapp.com/mine?memo_id=synthetic-memo'}
        report=self.collect(api,config);self.assertEqual(report['written'],1);self.assertIn(uid(100),report['delegated']);self.assertEqual(report['delegated'][uid(100)]['reason'],'delegated_property_proof_pending');self.assertFalse(report['selected_scope_complete']);self.assertNotIn(uid(100),self.state()['sources']['source1']['pages'])
    def test_private_remote_state_all_blobs_include_index_assets(self):
        self.collect();git=lambda *args:subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True).stdout
        git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid');git('add','--',str(m.BASE),str(m.STATE));git('commit','-qm','fixture');git('branch','-M','main')
        remote=Path(self.tmp.name+'-remote.git');subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        try:
            git('remote','add','origin',str(remote));git('push','-q','origin','main');self.assertTrue(m.verify_remote(self.root))
            index=self.root/m.BASE/'index.md';index.write_text(index.read_text()+'\nmanual index text\n')
            with self.assertRaisesRegex(ValueError,'source_remote_readback_failed'):m.verify_remote(self.root)
        finally:
            import shutil;shutil.rmtree(remote)

    def test_request_budget_preserves_checkpoint_without_partial_object(self):
        report=self.collect(config=settings(max_requests=3))
        self.assertEqual(report['soft_exit'],'request_budget');self.assertFalse(self.state()['sources']['source1']['pages'])
        self.assertFalse(list((self.root/m.BASE).glob('versions/**/*')))
        self.assertEqual(self.collect()['written'],1)
    def test_time_budget_soft_exit(self):
        ticks=iter([0,0,0,0,4,4,4]);budget=m.Budget(limits(max_seconds=4),clock=lambda:next(ticks,4))
        report=self.collect(budget=budget)
        self.assertEqual(report['soft_exit'],'time_budget');self.assertFalse(report['selected_scope_complete']);self.assertTrue((self.root/m.STATE).exists())
    def test_inventory_refresh_drops_old_green_before_incomplete_query(self):
        self.collect();api=API();api.query_hook=lambda *_:{'results':[page()],'has_more':True,'next_cursor':'synthetic'}
        report=self.collect(api,settings(max_query_pages=1));self.assertFalse(report['selected_scope_complete']);self.assertFalse(report['configured_scope_complete']);self.assertFalse(self.state()['sources']['source1']['coverage']['complete'])
    def test_get_newer_source_version_updates_inventory_not_infinite_rehydrate(self):
        api=API()
        def newer(value):value['last_edited_time']='2026-10-31T04:05:06.000Z';return value
        api.page_hook=newer;report=self.collect(api);self.assertTrue(report['selected_scope_complete'])
        record=self.state()['sources']['source1'];self.assertEqual(record['inventory']['records'][0]['last_edited_time'],'2026-10-31T04:05:06.000Z')
    def test_new_flomo_identity_after_inventory_routes_before_write(self):
        api=API();config=settings();config['sources'][0]['owner']='flomo'
        def change(value):value['properties']['Link']={'id':'link','type':'url','url':'https://flomoapp.com/mine?memo_id=synthetic-new'};return value
        api.page_hook=change;report=self.collect(api,config);self.assertEqual(report['written'],0);self.assertIn(uid(100),report['delegated']);self.assertFalse(self.state()['sources']['source1']['pages'])
    def test_legacy_manual_notes_never_written_or_suppressed(self):
        legacy=self.root/'Raw/02 Notion/01 Databases/旧稿/pages/旧.md';legacy.parent.mkdir(parents=True);raw=b'handwritten';legacy.write_bytes(raw)
        self.collect();self.assertEqual(legacy.read_bytes(),raw);self.assertTrue(m.eligible_path(self.root,legacy))
    def test_current_audit_uses_source_dates_not_import_and_keeps_full_workout_text(self):
        from datetime import date
        import iteration_incremental_audit as audit
        import iteration_closeout as closeout
        self.collect();path,_=self.current()
        with patch.object(audit,'ROOT',self.root),patch.object(closeout,'ROOT',self.root):
            candidate=audit.candidate_from_file(path,'Raw/02 Notion/',date(2026,10,29),date(2026,10,29))
            self.assertEqual(candidate.evidence_field,'source_date');self.assertEqual(candidate.source_identities,['notion:'+uid(100)]);self.assertEqual(candidate.source_representation,'original_record')
            self.assertIsNone(audit.candidate_from_file(path,'Raw/02 Notion/',date(2026,11,1),date(2026,11,1)))
            self.assertEqual(closeout.entry_from_path(path,'notion').evidence_date,date(2026,10,29))
            entries=closeout.entries_in_range(self.root/m.BASE,'notion',date(2026,10,29),date(2026,10,29));self.assertEqual(len(entries),1);self.assertIn('完整属性正文',entries[0].body)
        identities=audit.source_identities({'source_type':'workout_daily','session_page_ids':json.dumps([uid(100),uid(101)])});self.assertEqual(identities,['notion:'+uid(100),'notion:'+uid(101)])
    def test_materialized_image_url_must_be_https_even_with_custom_downloader(self):
        api=API([page(text='![](http://files.example.invalid/a.png)')]);download=lambda _:self.fail('unsafe download called')
        report=self.collect(api,downloader=download);self.assertEqual(report['held'][uid(100)],'unsafe_source_asset_url')
    def test_batched_readback_constant_git_processes(self):
        self.collect(API([page(100),page(101),page(102)]));git=lambda *args:subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True).stdout
        git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid');git('add','--',str(m.BASE),str(m.STATE));git('commit','-qm','fixture');git('branch','-M','main')
        remote=Path(self.tmp.name+'-remote.git');subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        try:
            git('remote','add','origin',str(remote));git('push','-q','origin','main')
            with patch.object(m.subprocess,'run',wraps=subprocess.run) as run:self.assertTrue(m.verify_remote(self.root))
            self.assertEqual(len(run.call_args_list),3)
        finally:
            import shutil;shutil.rmtree(remote)

    def test_owner_neutral_markers_and_original_json_unchanged(self):
        api=API([page(text='<!--nh:synthetic-body-->')]);api.pages[0]['properties']['Name']['title']=[rt('标题 <!--nh:synthetic-title-->')]
        self.collect(api);path,_=self.current();text=path.read_text();index=(self.root/m.BASE/'index.md').read_text()
        self.assertNotIn('<!--nh:',text);self.assertNotIn('<!--nh:',index);self.assertIn('<!--source-notion:',text)
        for name in ('id','messages','syncedIds','burnSyncedIds'):self.assertNotIn('\n'+name+':',text.split('\n---\n',1)[0])
        original=json.loads((path.parent/'source.json').read_bytes())['original_page'];self.assertEqual(original['properties']['Note']['rich_text'][0]['text']['content'],'<!--nh:synthetic-body-->')
    def test_metadata_fingerprint_detects_formula_change_and_ignores_signed_expiry(self):
        first=page();second=deepcopy(first);second['properties']['Note']['rich_text']=[rt('新属性值')]
        self.assertNotEqual(m.metadata_fingerprint(first),m.metadata_fingerprint(second))
        first['cover']={'type':'file','file':{'url':'https://files.example.invalid/a.png?X-Amz-Signature=synthetic-one','expiry_time':'2026-01-01'}}
        second=deepcopy(first);second['cover']['file']['url']='https://files.example.invalid/a.png?X-Amz-Signature=synthetic-two';second['cover']['file']['expiry_time']='2026-01-02'
        self.assertEqual(m.metadata_fingerprint(first),m.metadata_fingerprint(second))
    def test_index_source_and_receipts_have_complete_materialized_evidence(self):
        self.collect();path,record=self.current();value=json.loads((path.parent/'source.json').read_bytes())
        self.assertIn('materialized',value);self.assertIn('materialized_assets',value);self.assertEqual(record['renderer_version'],m.RENDERER_VERSION)

    def test_lossless_api_encoding_rejects_invalid_utf8(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response;response.read.return_value=b'{"name":"\xff"}'
        with patch.object(m.notion.urllib.request,'urlopen',return_value=response):
            with self.assertRaises(UnicodeDecodeError):m.notion.notion_request('synthetic','GET','/pages/'+uid(100))
    def test_rate_limit_soft_exits_without_replaying_source_reads(self):
        import urllib.error
        calls=[]
        def limited(method,path,payload=None):
            calls.append(path)
            try:raise urllib.error.HTTPError('https://api.notion.com/synthetic',429,'synthetic',{},None)
            except urllib.error.HTTPError as exc:raise RuntimeError('synthetic') from exc
        report=self.collect(limited);self.assertEqual(report['soft_exit'],'source_rate_limited');self.assertEqual(len(calls),1);self.assertFalse(report['selected_scope_complete'])

    def test_whole_job_deadline_reserves_persistence_without_new_api_calls(self):
        with patch.dict(os.environ,{'NOTION_SOURCE_JOB_DEADLINE':str(int(m.time.time())+299)}):
            api=API();report=self.collect(api)
        self.assertEqual(report['soft_exit'],'job_budget_reserved_for_persistence');self.assertEqual(len(api.calls),0)
        self.assertTrue((self.root/m.STATE).exists());self.assertTrue((self.root/m.BASE/'index.md').exists());self.assertFalse(report['selected_scope_complete'])

    def test_route_hold_repair_probes_same_page_without_inventory_rescan(self):
        api=API([page(100,text='ghp_'+'A'*36),page(101,text='已完成的另一条')]);first=self.collect(api)
        self.assertEqual(first['written'],1);self.assertIn('route_hold',self.state()['sources']['source1']['inventory']['records'][0])
        other=deepcopy(self.state()['sources']['source1']['pages'][uid(101)])
        api.pages[0]['properties']['Note']['rich_text']=[rt('源中已经修好')];api.pages[0]['last_edited_time']='2026-11-01T01:02:03.000Z';api.calls.clear()
        report=self.collect(api)
        self.assertEqual(report['written'],1);self.assertFalse(report['held']);self.assertTrue(report['selected_scope_complete'])
        self.assertFalse(any(method=='POST' for method,path,payload in api.calls));self.assertIn(('GET','/pages/'+uid(100),None),api.calls)
        state=self.state()['sources']['source1'];self.assertNotIn('route_hold',state['inventory']['records'][0]);self.assertEqual(state['pages'][uid(101)],other)
        self.assertEqual(state['pages'][uid(100)]['last_edited_time'],'2026-11-01T01:02:03.000Z')
    def test_route_hold_probe_still_bad_or_moved_is_bounded_and_never_adopted(self):
        api=API([page(100,text='ghp_'+'A'*36),page(101)]);self.collect(api);other=deepcopy(self.state()['sources']['source1']['pages'][uid(101)]);api.calls.clear()
        report=self.collect(api,settings(max_pages=1));self.assertEqual(report['held'][uid(100)],'potential_credential_in_source')
        self.assertEqual(api.calls,[('GET','/pages/'+uid(100),None)]);self.assertEqual(report['page_attempts'],1)
        api.pages[0]['parent']['database_id']=uid(2);api.calls.clear();report=self.collect(api,settings(max_pages=1))
        self.assertEqual(report['held'][uid(100)],'source_parent_identity_changed');self.assertEqual(api.calls,[('GET','/pages/'+uid(100),None)])
        state=self.state()['sources']['source1'];self.assertNotIn(uid(100),state['pages']);self.assertEqual(state['pages'][uid(101)],other)

    def test_route_hold_fixed_memo_identity_delegates_instead_of_generic_copy(self):
        api=API([page(100),page(101)]);config=settings();config['sources'][0]['owner']='flomo'
        api.pages[0]['properties']['Link']={'id':'link','type':'url','url':'https://flomoapp.com/mine?memo_id=invalid%20memo'}
        self.collect(api,config);api.pages[0]['properties']['Link']['url']='https://flomoapp.com/mine?memo_id=synthetic-repaired';api.calls.clear()
        report=self.collect(api,config);self.assertFalse(report['held']);self.assertEqual(report['written'],0)
        self.assertEqual(report['delegated'][uid(100)]['memo_id'],'synthetic-repaired');self.assertFalse(report['delegated'][uid(100)]['verified'])
        self.assertNotIn(uid(100),self.state()['sources']['source1']['pages']);self.assertEqual(api.calls,[('GET','/pages/'+uid(100),None)])


class Scope(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def save(self,path,value):p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(m.encoded(value))
    def fixture(self):
        legacy={'databases':{uid(i):{'database_id':uid(i),'database_name':'合成库'+str(i),'date_property':'Date','pages':{}} for i in range(1,12)}}
        cfg={'version':1,'enabled':True,'sources':[{'key':'source'+str(i),'database_id':uid(i),'date_property':'Date','owner':'flomo' if i==1 else 'generic'} for i in range(1,12)]}
        self.save(m.LEGACY_SCOPE,legacy);self.save(Path('.github/state/notion-flomo-config.json'),{'database_id':uid(1)});self.save(m.CONFIG,cfg);return cfg
    def test_missing_config_off(self):self.assertFalse(m.config(self.root)['enabled'])
    def test_exact_eleven_and_date_owner_mapping(self):
        cfg=self.fixture();self.assertEqual(len(m.config(self.root)['sources']),11)
        for mutate in (lambda v:v['sources'].append({'key':'extra','database_id':uid(12)}),lambda v:v['sources'][1].update(database_id=uid(99)),lambda v:v['sources'][0].update(owner='generic'),lambda v:v['sources'][0].update(date_property='Import date')):
            bad=deepcopy(cfg);mutate(bad);self.save(m.CONFIG,bad)
            with self.assertRaises(ValueError):m.config(self.root)
    def test_budget_limits_not_unbounded(self):
        cfg=self.fixture();cfg['batch']={'max_pages':0};self.save(m.CONFIG,cfg)
        with self.assertRaisesRegex(ValueError,'invalid_source_budget'):m.config(self.root)

class FilmOwner(unittest.TestCase):
    def test_existing_owner_separate_namespace_same_capture_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);subprocess.run(['git','init','-q',str(root)],check=True)
            legacy=root/'Raw/14 豆瓣影视/旧摘要.md';legacy.parent.mkdir(parents=True);raw=m.notion.render_frontmatter({'source_type':'douban_movie','database_id':uid(20),'page_id':uid(100)})+'\n旧摘要';legacy.write_text(raw)
            api=API([page(100,source=20,text='完整影视属性')]);api.pages[0]['properties']['日期']=api.pages[0]['properties'].pop('Date')
            setting={'sources':[{'key':'douban','database_id':uid(20),'name':'豆瓣影视','date_properties':['添加日期','日期'],'owner':'generic'}],'batch':limits()}
            with patch.object(m.cloud_private_checkout,'verify_private_checkout'):
                report=m.collect(root,setting,'synthetic',apply=True,request=api,base=m.DOUBAN_BASE,state_file=m.DOUBAN_STATE)
            self.assertTrue(report['selected_scope_complete']);state=json.loads((root/m.DOUBAN_STATE).read_bytes());record=state['sources']['douban']['pages'][uid(100)];path=root/m.DOUBAN_BASE/record['current_path']
            self.assertIn('完整影视属性',path.read_text());self.assertIn('source_date_property: "日期"',path.read_text());self.assertEqual(legacy.read_text(),raw)
            self.assertFalse((root/m.STATE).exists());self.assertTrue(m.eligible_path(root,path));self.assertFalse(m.eligible_path(root,path.parent/'source.json'))
    def test_dedicated_config_never_accepts_new_database(self):
        fixture=Scope();fixture.setUp()
        try:
            cfg=fixture.fixture();legacy=fixture.root/'Raw/14 豆瓣影视/旧摘要.md';legacy.parent.mkdir(parents=True);legacy.write_text(m.notion.render_frontmatter({'source_type':'douban_movie','database_id':uid(20)}))
            cfg['dedicated_douban']={'enabled':True,'database_id':uid(20),'date_properties':['添加日期','日期']};fixture.save(m.CONFIG,cfg)
            self.assertEqual(m.douban_source_settings(fixture.root)['sources'][0]['database_id'],uid(20));self.assertEqual(len(m.config(fixture.root)['sources']),11)
            cfg['dedicated_douban']['database_id']=uid(99);fixture.save(m.CONFIG,cfg)
            with self.assertRaisesRegex(ValueError,'douban_source_not_existing_owner'):m.douban_source_settings(fixture.root)
        finally:fixture.tearDown()


class Workflow(unittest.TestCase):
    def test_source_mode_partial_persistence_independent_config_and_cancel(self):
        from types import SimpleNamespace
        import re
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/sync-notion-personal-data.yml').read_text()
        steps={p.splitlines()[0]:p for p in text.split('      - name: ')[1:]}
        def allowed(name,key='morning',cancelled=False,outcomes=None,enabled=True):
            statuses={'private_checkout':'success','source_selection':'skipped','sources_config':'success','film_source_config':'success','sources_requested':'skipped','sources_sync':'failure','film_source_sync':'success','sources_commit':'success','sources_readback':'success'};statuses.update(outcomes or {})
            model=SimpleNamespace(**{k:SimpleNamespace(outcome=v) for k,v in statuses.items()})
            model.sources_config.outputs=SimpleNamespace(enabled='true' if enabled else '')
            model.film_source_config.outputs=SimpleNamespace(enabled='true')
            expression=steps[name].split('        if: ${{ ',1)[1].split(' }}',1)[0].replace('!cancelled()','not cancelled()')
            expression=re.sub(r'!inputs\.', 'not inputs.',expression).replace('&&','and').replace('||','or')
            return eval(expression,{'__builtins__':{}},{'steps':model,'inputs':SimpleNamespace(notion_sources_only=True,notion_source_key=key,full_sync=False,flomo_stage_only=False,raw16_only=False,messages_only=False),'github':SimpleNamespace(event_name='workflow_dispatch'),'cancelled':lambda:cancelled})
        config='Read private complete source configuration';film='Read private complete film source configuration';capture='Capture complete Notion originals privately';commit='Persist complete originals and durable progress'
        self.assertTrue(allowed(config));self.assertFalse(allowed(config,cancelled=True));self.assertFalse(allowed(film));self.assertTrue(allowed(film,key='douban'));self.assertFalse(allowed(config,key='douban'))
        self.assertTrue(allowed(capture,outcomes={'film_source_config':'failure'}));self.assertTrue(allowed('Capture complete film originals through existing caller',outcomes={'sources_config':'failure'}))
        self.assertTrue(allowed(commit));self.assertFalse(allowed(commit,cancelled=True));self.assertTrue(allowed('Report incomplete original source capture'))
        self.assertLess(text.index('name: Commit Raw updates'),text.index('name: Capture complete Notion originals privately'))
        for path in (m.BASE,m.STATE,m.DOUBAN_BASE,m.DOUBAN_STATE):self.assertIn(str(path),steps[commit])
        for name in ('Sync podcast notes','Sync workout daily notes','Sync douban books','Read private flomo stage configuration','Read private article source configuration','Read private message source configuration'):
            self.assertIn('!inputs.notion_sources_only',steps[name])
        self.assertIn('continue-on-error: true',steps[config]);self.assertIn('continue-on-error: true',steps[film]);self.assertNotIn('success()',steps[capture])
        self.assertEqual(text.count('- cron:'),2);self.assertIn('group: eddie-private-vault-writers',text)


if __name__=='__main__':unittest.main()
