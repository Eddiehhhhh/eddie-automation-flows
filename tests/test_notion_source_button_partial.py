from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
import test_notion_sources as f
m=f.m


class ButtonAPI(f.API):
    def __init__(self,pages=None):
        super().__init__(pages);self.version_calls=[];self.markdown={};self.gap=f.uid(900)
        self.blocks[f.uid(100)]={'results':[{'id':f.uid(800),'type':'column_list','column_list':{},'has_children':True}],'has_more':False,'next_cursor':None}
        self.blocks[f.uid(800)]={'results':[
            {'id':self.gap,'type':'unsupported','has_children':False},
            {'id':f.uid(901),'type':'paragraph','paragraph':{'rich_text':[f.rt('已知正文完整保留',annotations={'bold':True})]},'has_children':False},
            {'id':f.uid(902),'type':'image','image':{'type':'external','external':{'url':'https://files.example.invalid/original.png'},'caption':[]},'has_children':False},
            {'id':f.uid(903),'type':'child_database','child_database':{'title':'子库引用不代表内容已保存'},'has_children':False}], 'has_more':False,'next_cursor':None}
        self.response={'object':'page_markdown','id':f.uid(100),'markdown':'已知正文完整保留\n<unknown url="https://app.notion.com/p/'+f.uid(100).replace('-','')+'#'+self.gap.replace('-','')+'" alt="button" />','truncated':True,'unknown_block_ids':[self.gap],'request_id':f.uid(999)}
    def __call__(self,method,path,payload=None,*,api_version=None):
        self.version_calls.append((method,path,api_version))
        if '/markdown?' in path:
            self.calls.append((method,path,deepcopy(payload)));return deepcopy(self.response)
        return super().__call__(method,path,payload)


class Partial(unittest.TestCase):
    setUp=f.Sources.setUp
    tearDown=f.Sources.tearDown
    collect=f.Sources.collect
    state=f.Sources.state
    current=f.Sources.current
    def run_api(self,api=None,**kw):return self.collect(api or ButtonAPI(),downloader=lambda _:b'\x89PNG\r\nreal-fixture',**kw)
    def test_partial_saves_full_original_known_body_property_envelopes_and_asset(self):
        api=ButtonAPI();report=self.run_api(api);path,entry=self.current();raw=json.loads((path.parent/'source.json').read_bytes());text=path.read_text()
        self.assertEqual(report['written'],1);self.assertEqual(report['partial_written'],1);self.assertFalse(report['selected_scope_complete']);self.assertFalse(entry['capture_complete']);self.assertTrue(entry['readable_partial'])
        self.assertEqual(raw['original_page'],api.pages[0]);self.assertEqual(set(raw['property_responses']),{'note','title'});self.assertEqual(raw['markdown_v2']['response'],api.response)
        self.assertEqual(raw['blocks'][0]['_children'][0],api.blocks[f.uid(800)]['results'][0]);self.assertEqual(entry['incomplete_gaps'][0]['block_id'],api.gap)
        self.assertIn('**已知正文完整保留**',text);self.assertIn('完整属性正文',text);self.assertIn('标签及操作未能保存',text);self.assertIn('不是完整备份',text);self.assertIn('capture_complete: false',text)
        self.assertEqual(next(path.parent.glob('assets/*')).read_bytes(),b'\x89PNG\r\nreal-fixture');self.assertNotIn('https://files.example.invalid',text)
        self.assertEqual([v for _,p,v in api.version_calls if '/markdown?' in p],[m.MARKDOWN_API_VERSION]);self.assertTrue(all(v is None for _,p,v in api.version_calls if '/markdown?' not in p))
        coverage=self.state()['sources']['source1']['coverage'];self.assertEqual((coverage['captured'],coverage['partial_saved'],coverage['unread_observed']),(0,1,0));self.assertFalse(coverage['complete'])
        index=(self.root/m.BASE/'index.md').read_text();self.assertIn('部分保存 1',index);self.assertIn('按钮仍有读取缺口',index)
    def test_same_partial_no_duplicate_history_and_does_not_spend_next_page_slot(self):
        api=ButtonAPI([f.page(100),f.page(101)]);first=self.run_api(api,config=f.settings(max_pages=1));self.assertEqual(first['partial_written'],1)
        path,entry=self.current();prior=deepcopy(entry);api.calls.clear()
        second=self.run_api(api,config=f.settings(max_pages=1));self.assertEqual(second['partial_unchanged'],1);self.assertEqual(second['written'],1);self.assertEqual(second['page_attempts'],1)
        state=self.state()['sources']['source1'];self.assertEqual(state['pages'][f.uid(100)],prior);self.assertEqual(len(state['pages'][f.uid(100)]['versions']),1)
        self.assertFalse(any('/markdown?' in p for _,p,_ in api.calls));self.assertFalse(second['selected_scope_complete']);self.assertEqual(state['coverage']['partial_saved'],1);self.assertEqual(state['coverage']['captured'],1)
    def test_many_saved_partials_use_one_rotating_probe_and_do_not_starve_tail(self):
        class Many(ButtonAPI):
            def __init__(self):
                super().__init__([f.page(i) for i in (100,101,102)])
                self.blocks={};self.responses={}
                for row in self.pages:
                    ident=row['id'];gap=f.uid(int(ident[:8],16)+900)
                    self.blocks[ident]={'results':[{'id':gap,'type':'unsupported','has_children':False}], 'has_more':False,'next_cursor':None}
                    self.responses[ident]={'object':'page_markdown','id':ident,'markdown':'<unknown url="https://app.notion.com/p/'+ident.replace('-','')+'#'+gap.replace('-','')+'" alt="button" />','truncated':True,'unknown_block_ids':[gap]}
            def __call__(self,method,path,payload=None,**kw):
                if '/markdown?' in path:self.response=self.responses[path.split('/')[2]]
                return super().__call__(method,path,payload,**kw)
        api=Many();self.assertEqual(self.run_api(api)['partial_written'],3)
        api.pages.append(f.page(103));api.calls.clear()
        report=self.run_api(api,config=f.settings(max_requests=9,max_pages=1));self.assertEqual(report['partial_unchanged'],3);self.assertEqual(report['written'],1);self.assertEqual(report['api_requests'],8)
        self.assertFalse(any('/pages/'+f.uid(i) in path for _,path,_ in api.calls for i in (101,102)))
        api.calls.clear();self.run_api(api)
        self.assertIn(('GET','/pages/'+f.uid(101),None),api.calls);self.assertNotIn(('GET','/pages/'+f.uid(100),None),api.calls)

    def test_same_edited_partial_property_change_recaptures_and_retains_old_bytes(self):
        api=ButtonAPI();self.run_api(api);old,entry=self.current();oldraw=old.read_bytes()
        api.pages[0]['properties']['Note']['rich_text']=[f.rt('源属性新值')]
        report=self.run_api(api);new,entry=self.current();self.assertEqual(report['partial_written'],1);self.assertNotEqual(old,new);self.assertEqual(old.read_bytes(),oldraw);self.assertIn('源属性新值',new.read_text());self.assertEqual(len(entry['versions']),2)
    def test_source_fix_can_become_complete_without_deleting_partial_or_legacy(self):
        legacy=self.root/'Raw/02 Notion/01 Databases/manual.md';legacy.parent.mkdir(parents=True);legacy.write_text('人工旧稿')
        api=ButtonAPI();self.run_api(api);old,_=self.current();oldraw=old.read_bytes()
        api.pages[0]['last_edited_time']='2026-11-01T02:03:04.000Z';api.blocks[f.uid(800)]['results']=api.blocks[f.uid(800)]['results'][1:]
        report=self.run_api(api);new,entry=self.current();self.assertTrue(report['selected_scope_complete']);self.assertTrue(entry['capture_complete']);self.assertNotIn('readable_partial',entry);self.assertEqual(old.read_bytes(),oldraw);self.assertEqual(legacy.read_text(),'人工旧稿');self.assertEqual(len(entry['versions']),2)
    def test_all_other_unknown_types_or_unmatched_ids_or_page_hold(self):
        for mutate in (
            lambda x:x.update(id=f.uid(101)),lambda x:x.update(unknown_block_ids=[]),
            lambda x:x.update(unknown_block_ids=[f.uid(900),f.uid(900)]),
            lambda x:x.update(markdown=x['markdown'].replace('alt="button"','alt="embed"')),
            lambda x:x.update(markdown=x['markdown'].replace(f.uid(100).replace('-',''),f.uid(101).replace('-',''))),
            lambda x:x.update(markdown=x['markdown'].replace('app.notion.com','notion.com.evil.invalid')),
            lambda x:x.update(markdown=x['markdown'].replace('#','#prefix')),
            lambda x:x.update(markdown=x['markdown']+x['markdown']),
            lambda x:x.update(markdown=x['markdown'].replace('alt="button"','alt="button" extra="unverified"')),
            lambda x:x.update(truncated='true')):
            with self.subTest(mutate=mutate):
                api=ButtonAPI();mutate(api.response);report=self.run_api(api)
                self.assertEqual(report['held'][f.uid(100)],'unsupported_source_block');self.assertFalse(self.state()['sources']['source1']['pages'])
    def test_partial_credentials_in_markdown_or_raw_button_never_persist(self):
        secret='ntn_'+'A'*42
        for place in ('markdown','button'):
            api=ButtonAPI()
            if place=='markdown':api.response['markdown']+=secret
            else:api.blocks[f.uid(800)]['results'][0]['opaque_original']=secret
            report=self.run_api(api);self.assertEqual(report['written'],0);self.assertTrue(report['held'])
            for path in self.root.rglob('*'):
                if path.is_file() and '.git' not in path.parts:self.assertNotIn(secret.encode(),path.read_bytes())
    def test_partial_during_read_source_race_and_asset_failure_do_not_write(self):
        api=ButtonAPI();calls=[0]
        def changed(value):
            calls[0]+=1
            if calls[0]>1:value['properties']['Note']['rich_text']=[f.rt('人工并发改变')]
            return value
        api.page_hook=changed;report=self.run_api(api);self.assertEqual(report['held'][f.uid(100)],'source_changed_during_capture');self.assertFalse(self.state()['sources']['source1']['pages'])
        report=self.collect(ButtonAPI(),downloader=lambda _:(_ for _ in ()).throw(ValueError('asset_failed')));self.assertEqual(report['held'][f.uid(100)],'asset_failed');self.assertFalse(self.state()['sources']['source1']['pages'])
    def test_partial_remote_verifier_reads_source_markdown_and_real_fixture_asset(self):
        self.run_api();git=lambda *args:subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True).stdout
        git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid');git('add','--',str(m.BASE),str(m.STATE));git('commit','-qm','fixture partial');git('branch','-M','main')
        remote=Path(self.tmp.name+'-remote.git');subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        try:
            git('remote','add','origin',str(remote));git('push','-q','origin','main');self.assertTrue(m.verify_remote(self.root))
            path,_=self.current();asset=next(path.parent.glob('assets/*'));asset.write_bytes(b'wrong bytes')
            with self.assertRaisesRegex(ValueError,'source_remote_readback_failed'):m.verify_remote(self.root)
        finally:
            import shutil;shutil.rmtree(remote)

    def test_partial_budget_and_manual_current_still_fail_closed(self):
        report=self.run_api(config=f.settings(max_requests=6));self.assertEqual(report['soft_exit'],'request_budget');self.assertFalse(self.state()['sources']['source1']['pages'])
        self.run_api();path,_=self.current();path.write_text('保留人工修订');report=self.run_api();self.assertEqual(report['held'][f.uid(100)],'source_owned_content_edited');self.assertEqual(path.read_text(),'保留人工修订')


class SDK(unittest.TestCase):
    def test_per_request_api_version_and_default_compatibility(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response;response.read.return_value=b'{}'
        with patch.object(m.notion.urllib.request,'urlopen',return_value=response) as send:
            m.notion.notion_request('synthetic','GET','/pages/synthetic',api_version='2026-03-11');m.notion.notion_request('synthetic','GET','/pages/synthetic')
        self.assertEqual(send.call_args_list[0].args[0].get_header('Notion-version'),'2026-03-11');self.assertEqual(send.call_args_list[1].args[0].get_header('Notion-version'),m.notion.NOTION_VERSION)
    def test_observer_counts_actual_retries_and_reset_on_error(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response;response.read.return_value=b'{}';seen=[]
        err=urllib.error.HTTPError('https://api.notion.com/synthetic',503,'synthetic',{},io.BytesIO(b'{}'))
        prior=m.notion.REQUEST_OBSERVER.get();token=m.notion.REQUEST_OBSERVER.set(lambda method,path:seen.append((method,path)));retry=m.notion.READ_RETRIES.set(1)
        try:
            with patch.object(m.notion.urllib.request,'urlopen',side_effect=[err,response]),patch.object(m.notion.time,'sleep'):
                m.notion.notion_request('synthetic','GET','/pages/synthetic')
            self.assertEqual(len(seen),2)
            stop=m.notion.REQUEST_OBSERVER.set(lambda *_:(_ for _ in ()).throw(m.BudgetExceeded('request_budget')))
            try:
                with patch.object(m.notion.urllib.request,'urlopen') as send:
                    with self.assertRaises(m.BudgetExceeded):m.notion.notion_request('synthetic','GET','/pages/synthetic')
                    send.assert_not_called()
            finally:m.notion.REQUEST_OBSERVER.reset(stop)
        finally:m.notion.READ_RETRIES.reset(retry);m.notion.REQUEST_OBSERVER.reset(token)
        self.assertIs(m.notion.REQUEST_OBSERVER.get(),prior)
    def test_generic_disables_ambient_observer_to_avoid_double_budget_and_restores(self):
        seen=[];token=m.notion.REQUEST_OBSERVER.set(lambda *args:seen.append(args))
        try:
            with tempfile.TemporaryDirectory() as tmp,patch.object(m.notion.time,'sleep'):
                subprocess.run(['git','init','-q',tmp],check=True)
                api=f.API()
                def sdk(key,method,path,payload=None,**kwargs):
                    self.assertIsNone(m.notion.REQUEST_OBSERVER.get());return api(method,path,payload,**kwargs)
                with patch.object(m.notion,'notion_request',side_effect=sdk):
                    report=m.collect(Path(tmp),f.settings(),'synthetic',apply=False)
                self.assertEqual(report['api_requests'],len(api.calls));self.assertEqual(seen,[]);self.assertIsNotNone(m.notion.REQUEST_OBSERVER.get())
        finally:m.notion.REQUEST_OBSERVER.reset(token)

if __name__=='__main__':unittest.main()
