from copy import deepcopy
import io
import json
import unittest
import subprocess
from pathlib import Path
import urllib.error
from unittest.mock import patch
import test_notion_sources as f
import test_notion_source_button_partial as buttons
m=f.m
BAD='https://files.example.invalid/denied.jpg';GOOD='https://files.example.invalid/body.png';PNG=b'\x89PNG\r\nactual-fixture-content'
def failure(code=403):return urllib.error.HTTPError(BAD,code,'private error reason',{},io.BytesIO(b'private source body'))
class AssetAPI(f.API):
    def __init__(self,pages=None):
        super().__init__(pages);self.pages[0]['cover']={'type':'external','external':{'url':BAD}}
        self.blocks[self.pages[0]['id']]={'results':[{'id':f.uid(901),'type':'paragraph','paragraph':{'rich_text':[f.rt('known body survives')]},'has_children':False},{'id':f.uid(902),'type':'image','image':{'type':'external','external':{'url':GOOD},'caption':[]},'has_children':False}],'has_more':False,'next_cursor':None}
class Assets(unittest.TestCase):
    setUp=f.Sources.setUp
    tearDown=f.Sources.tearDown
    state=f.Sources.state
    current=f.Sources.current
    def collect(self,api=None,download=None,config=None):
        def denied(url):
            if url==BAD:raise failure()
            return PNG
        return m.collect(self.root,config or f.settings(),'synthetic',apply=True,request=api or AssetAPI(),downloader=download or denied)
    def test_cover403_does_not_discard_properties_body_or_other_real_asset(self):
        api=AssetAPI();report=self.collect(api);path,receipt=self.current();saved=json.loads((path.parent/'source.json').read_bytes());text=path.read_text()
        self.assertEqual((report['written'],report['partial_written']),(1,1));self.assertEqual(report['asset_http_failures'],{'asset_http_403':1});self.assertFalse(report['held']);self.assertFalse(report['selected_scope_complete']);self.assertFalse(receipt['capture_complete']);self.assertTrue(receipt['readable_partial'])
        self.assertEqual(saved['original_page'],api.pages[0]);self.assertEqual(set(saved['property_responses']),{'note','title'});self.assertEqual(saved['blocks'],api.blocks[f.uid(100)]['results']);self.assertEqual(saved['asset_failure_sources'][0]['url'],BAD)
        self.assertNotIn('private source body',(path.parent/'source.json').read_text());self.assertNotIn('private error reason',(path.parent/'source.json').read_text())
        self.assertIn('完整属性正文',text);self.assertIn('known body survives',text);self.assertIn('asset_http_403',text);self.assertIn('不是完整备份',text);self.assertNotIn(BAD,text);self.assertNotIn(GOOD,text);self.assertEqual(next(path.parent.glob('assets/*')).read_bytes(),PNG)
        self.assertEqual((self.state()['sources']['source1']['coverage']['captured'],self.state()['sources']['source1']['coverage']['partial_saved'],self.state()['sources']['source1']['coverage']['unread_observed']),(0,1,0))
    def test_same_failure_retries_bounded_without_duplicate_versions_then_recovers_without_edit(self):
        api=AssetAPI();self.collect(api);old,prior=self.current();oldraw=old.read_bytes();report=self.collect(api)
        self.assertEqual(report['asset_retries'],1);self.assertEqual(report['written'],0);self.assertEqual(report['partial_unchanged'],1);self.assertEqual(self.current()[1],prior);self.assertEqual(len(prior['versions']),1)
        recovered=self.collect(api,download=lambda _:PNG);path,entry=self.current();self.assertEqual(recovered['written'],1);self.assertTrue(recovered['selected_scope_complete']);self.assertTrue(entry['capture_complete']);self.assertFalse(entry.get('readable_partial'));self.assertEqual(entry['last_edited_time'],prior['last_edited_time']);self.assertEqual(old.read_bytes(),oldraw);self.assertNotEqual(path,old);self.assertEqual(len(entry['versions']),2)
    def test_permanent_first_bad_cover_maxpages1_does_not_starve_unread_tail(self):
        api=AssetAPI([f.page(100),f.page(101),f.page(102)]);config=f.settings(max_pages=1)
        first=self.collect(api,config=config);self.assertEqual(first['partial_written'],1)
        second=self.collect(api,config=config);self.assertEqual(second['written'],1);self.assertEqual(second['asset_retries'],0);self.assertIn(f.uid(101),self.state()['sources']['source1']['pages'])
        third=self.collect(api,config=config);self.assertEqual(third['written'],1);self.assertIn(f.uid(102),self.state()['sources']['source1']['pages']);self.assertEqual(third['asset_retries'],0)
        fourth=self.collect(api,config=config);self.assertEqual(fourth['asset_retries'],1);self.assertEqual(fourth['written'],0);self.assertEqual(len(self.state()['sources']['source1']['pages'][f.uid(100)]['versions']),1)
    def test_failed_body_image_and_file_property_are_not_exposed_as_working_links(self):
        api=AssetAPI();api.blocks[f.uid(100)]['results'][1]['image']['external']['url']=BAD;api.blocks[f.uid(100)]['results'][1]['image']['caption']=[f.rt('known image caption',annotations={'bold':True})]
        api.pages[0]['properties']['Files']={'id':'files','type':'files','files':[{'name':'original.jpg','type':'external','external':{'url':BAD}}]};seen=[]
        def download(url):seen.append(url);raise failure(404)
        self.collect(api,download=download);path,entry=self.current();text=path.read_text();self.assertEqual(seen,[BAD]);self.assertNotIn(BAD,text);self.assertNotIn('![]()',text);self.assertNotIn('](.assets',text);self.assertIn('original.jpg（附件未保存：asset_http_404）',text);self.assertIn('**known image caption**',text);self.assertIn('附件未保存：asset_http_404',text);self.assertEqual(len(entry['incomplete_gaps']),3)
    def test_literal_image_html_and_file_failures_remove_bad_embeds(self):
        api=AssetAPI();api.pages[0]['cover']=None;api.blocks[f.uid(100)]['results']=[];api.pages[0]['properties']['Note']['rich_text']=[f.rt('![]('+BAD+')\n<img src="'+BAD+'" alt="known image alt">\n📎 [file]('+BAD+')')]
        self.collect(api,download=lambda _:(_ for _ in ()).throw(failure()));path,entry=self.current();text=path.read_text();self.assertNotIn(BAD,text);self.assertNotIn('![](',text);self.assertNotIn('<img ',text);self.assertIn('known image alt',text);self.assertIn('asset_http_403',text);self.assertIn('asset_gap_count: 1',text)
        before=deepcopy(entry);report=self.collect(api,download=lambda _:(_ for _ in ()).throw(failure()));self.assertEqual(report['written'],0);self.assertEqual(self.current()[1],before)
    def test_shared_failed_structured_and_literal_url_is_attempted_only_once(self):
        api=AssetAPI();api.blocks[f.uid(100)]['results']=[];api.pages[0]['properties']['Note']['rich_text']=[f.rt('![]('+BAD+')')];seen=[]
        def download(url):seen.append(url);raise failure()
        self.collect(api,download=download);self.assertEqual(seen,[BAD]);self.assertEqual(len(self.current()[1]['incomplete_gaps']),2);self.assertNotIn(BAD,self.current()[0].read_text())

    def test_buttons_plus_failed_asset_keep_both_original_proofs(self):
        api=buttons.ButtonAPI();api.pages[0]['cover']={'type':'external','external':{'url':BAD}}
        report=self.collect(api);path,entry=self.current();raw=json.loads((path.parent/'source.json').read_bytes());self.assertEqual(raw['markdown_v2']['response'],api.response);self.assertEqual({g['type'] for g in entry['incomplete_gaps']},{'asset','button'});self.assertIn('按钮也有读取缺口',path.read_text());self.assertEqual(entry['button_partial_proof_version'],1);self.assertEqual(entry['asset_partial_proof_version'],1)
    def test_unsafe_asset_credentials_race_and_size_remain_hard_holds(self):
        for problem in ('unsafe_asset_address','unsafe_asset_url','asset_file_budget','potential_credential_in_source'):
            report=self.collect(download=lambda _:(_ for _ in ()).throw(ValueError(problem)));self.assertEqual(report['held'][f.uid(100)],problem);self.assertFalse(self.state()['sources']['source1']['pages'])
        api=AssetAPI();count=[0]
        def changed(page):
            count[0]+=1
            if count[0]>1:page['last_edited_time']='2026-11-02T00:00:00Z'
            return page
        api.page_hook=changed;report=self.collect(api);self.assertEqual(report['held'][f.uid(100)],'source_changed_during_capture');self.assertFalse(self.state()['sources']['source1']['pages'])
    def test_successful_asset_change_without_parent_edit_does_not_wrongly_noop(self):
        api=AssetAPI();self.collect(api);old,receipt=self.current();report=self.collect(api,download=lambda url:(_ for _ in ()).throw(failure()) if url==BAD else PNG+b'new data');self.assertEqual(report['partial_written'],1);self.assertNotEqual(old,self.current()[0]);self.assertEqual(len(self.current()[1]['versions']),2)
    def test_direct_http_error_safe_enum_does_not_include_source_or_exception_text(self):
        error=failure();self.assertEqual(m.safe_failure(error),'source_http_403');self.assertNotIn(BAD,m.safe_failure(error));self.assertNotIn('private',m.safe_failure(error))
        try:raise RuntimeError('private wrapped exception') from error
        except RuntimeError as exc:self.assertEqual(m.safe_failure(exc),'source_http_403')
    def test_asset_partial_remote_verification_keeps_good_bytes_and_detects_change(self):
        self.collect();git=lambda *args:subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True).stdout
        git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid');git('add','--',str(m.BASE),str(m.STATE));git('commit','-qm','asset partial fixture');git('branch','-M','main')
        remote=Path(self.tmp.name+'-remote.git');subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        try:
            git('remote','add','origin',str(remote));git('push','-q','origin','main');self.assertTrue(m.verify_remote(self.root))
            asset=next(self.current()[0].parent.glob('assets/*'));asset.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'source_remote_readback_failed'):m.verify_remote(self.root)
        finally:
            import shutil;shutil.rmtree(remote)
    def test_metadata_change_is_retained_but_http_request_id_churn_does_not_duplicate(self):
        api=AssetAPI();api.pages[0]['request_id']='synthetic-request1';self.collect(api);old,prior=self.current();api.pages[0]['request_id']='synthetic-request2';report=self.collect(api);self.assertEqual(report['written'],0);self.assertEqual(self.current()[1],prior)
        api.pages[0]['archived']=True;report=self.collect(api);new,entry=self.current();self.assertEqual(report['partial_written'],1);self.assertNotEqual(old,new);self.assertTrue(json.loads((new.parent/'source.json').read_bytes())['original_page']['archived'])
    def test_full_property_envelope_change_without_parent_edit_does_not_wrongly_noop(self):
        class Hydrated(AssetAPI):
            changed=False
            def __call__(self,method,path,payload=None,**kw):
                if self.changed and '/properties/note?' in path:
                    prop=deepcopy(self.pages[0]['properties']['Note']);prop['rich_text']=[f.rt('new fully hydrated original text')];return f.prop_response(prop)
                return super().__call__(method,path,payload,**kw)
        api=Hydrated();self.collect(api);old,prior=self.current();api.changed=True;report=self.collect(api);new,entry=self.current();self.assertEqual(report['partial_written'],1);self.assertNotEqual(old,new);self.assertIn('new fully hydrated original text',new.read_text());self.assertEqual(entry['last_edited_time'],prior['last_edited_time'])

    def test_request_budget_on_retry_retains_old_partial_and_all_bytes(self):
        api=AssetAPI();self.collect(api);path,prior=self.current();old=path.read_bytes();report=self.collect(api,config=f.settings(max_requests=4));self.assertEqual(report['soft_exit'],'request_budget');self.assertEqual(path.read_bytes(),old);self.assertEqual(self.current()[1],prior)

if __name__=='__main__':unittest.main()
