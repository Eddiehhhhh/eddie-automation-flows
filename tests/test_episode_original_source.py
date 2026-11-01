"""Synthetic Episode originals; no network, real source IDs or credentials."""
from copy import deepcopy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import sync_notion_sources_to_raw as m
import sync_my_podcast_to_raw as episode
import test_notion_sources as fixtures
from test_notion_sources import API, uid, page


class EpisodeOriginals(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);subprocess.run(['git','init','-q',str(self.root)],check=True)
        self.scope=fixtures.Scope();self.scope.root=self.root;self.cfg=self.scope.fixture()
        self.cfg['dedicated_episode']={'enabled':True};self.save_config()
        for target,name,value in [(episode,'EPISODE_DATABASE_ID',uid(20)),(m.cloud_private_checkout,'verify_private_checkout',None)]:
            item=patch.object(target,name,value) if value is not None else patch.object(target,name)
            item.start();self.addCleanup(item.stop)
        self.old=self.root/'Raw/13 小宇宙/2026/2026-01/old.md';self.old.parent.mkdir(parents=True)
        self.old.write_text('---\nepisode_page_id: "'+uid(100)+'"\ntitle: "Legacy original"\n---\nHuman body\n')
        self.index=self.root/'Raw/13 小宇宙/索引.md';self.index.write_text('Original legacy index\n')
        self.saved={self.old:self.old.read_bytes(),self.index:self.index.read_bytes()}
        self.addCleanup(lambda:[self.assertEqual(path.read_bytes(),raw) for path,raw in self.saved.items()])

    def save_config(self):self.scope.save(m.CONFIG,self.cfg)
    def source(self,number=100):
        value=page(number,20);value['properties']['日期']=value['properties'].pop('Date');return value
    def collect(self,api=None,**kw):return m.run_episode_source(self.root,'invalid-fixture',apply=True,request=api or API([self.source()]),**kw)
    def state(self):return json.loads((self.root/m.EPISODE_STATE).read_bytes())
    def current(self,ident=None):
        record=self.state()['sources']['episode']['pages'][ident or uid(100)]
        return self.root/m.EPISODE_BASE/record['current_path'],record

    def test_defaults_off_exact_existing_caller_identity_date_and_filter_not_configurable(self):
        self.cfg.pop('dedicated_episode');self.save_config();self.assertIsNone(m.episode_source_settings(self.root))
        self.cfg['dedicated_episode']={'enabled':True};self.save_config()
        settings=m.episode_source_settings(self.root)
        self.assertEqual(settings['sources'][0]['database_id'],episode.EPISODE_DATABASE_ID)
        self.assertEqual(settings['sources'][0]['date_property'],'日期')
        self.assertEqual(len(m.config(self.root)['sources']),11)
        for override in ('database_id','query_filter','date_property'):
            self.cfg['dedicated_episode'][override]='other';self.save_config()
            with self.assertRaisesRegex(ValueError,'invalid_episode_source_configuration'):m.episode_source_settings(self.root)
            self.cfg['dedicated_episode'].pop(override)
        self.assertFalse((self.root/m.EPISODE_STATE).exists())

    def test_full_property_and_transcript_binary_asset_capture_preserves_all_legacy_notes(self):
        value=self.source();raw=b'synthetic original transcript bytes\n'
        value['properties']['逐字稿']={'id':'transcript','type':'files','files':[
            {'name':'transcript.txt','type':'file','file':{'url':'https://assets.invalid/transcript.txt'}}]}
        api=API([value]);report=self.collect(api,downloader=lambda url:raw)
        self.assertTrue(report['selected_scope_complete']);self.assertEqual(report['written'],1)
        posts=[(path,payload) for method,path,payload in api.calls if method=='POST']
        self.assertEqual(len(posts),1);self.assertEqual(posts[0][0],'/databases/'+uid(20)+'/query')
        self.assertEqual(posts[0][1]['filter'],episode.LISTENED_FILTER)
        state=self.state()['sources']['episode'];self.assertEqual(state['query_filter'],episode.LISTENED_FILTER)
        path,record=self.current();data=json.loads((path.parent/'source.json').read_bytes())
        self.assertEqual(data['original_page'],value);self.assertEqual(set(data['property_responses']),{'title','note'})
        self.assertEqual(record['property_proof_version'],1)
        asset=next(p for p in path.parent.rglob('*') if p.is_file() and p.read_bytes()==raw)
        self.assertEqual(hashlib.sha256(asset.read_bytes()).hexdigest(),hashlib.sha256(raw).hexdigest())
        relative_links=re.findall(r'\]\((assets/[^)]+)\)',path.read_text())
        self.assertTrue(relative_links)
        from urllib.parse import unquote
        self.assertTrue(all((path.parent/unquote(relative)).is_file() for relative in relative_links))
        self.assertIn('source_date: "2026-10-29"',path.read_text());self.assertIn('完整属性正文',path.read_text())
        self.assertTrue(m.eligible_path(self.root,path));self.assertFalse(m.eligible_path(self.root,self.root/m.EPISODE_BASE/'index.md'))
        self.assertFalse((self.root/m.BASE).exists());self.assertFalse((self.root/m.STATE).exists())

    def test_max_two_progress_resumes_and_completed_versions_skip_hydration(self):
        api=API([self.source(100),self.source(101),self.source(102)])
        first=self.collect(api,max_pages=2);self.assertEqual(first['written'],2);self.assertFalse(first['selected_scope_complete'])
        old={ident:deepcopy(value) for ident,value in self.state()['sources']['episode']['pages'].items()}
        api.calls.clear();second=self.collect(api,max_pages=2);self.assertEqual(second['written'],1);self.assertTrue(second['selected_scope_complete'])
        self.assertFalse(any(method=='POST' for method,path,payload in api.calls))
        for ident,record in old.items():self.assertEqual(record,self.state()['sources']['episode']['pages'][ident])
        third=self.collect(api,max_pages=2);self.assertEqual(third['written'],0);self.assertEqual(third['unchanged'],3)

    def test_filter_identity_change_cannot_reuse_existing_inventory_or_capture(self):
        self.collect(max_pages=1);path=self.root/m.EPISODE_STATE;state=self.state();state['sources']['episode']['query_filter']={'wrong':'scope'};path.write_bytes(m.encoded(state));before=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'source_query_scope_changed'):self.collect()
        self.assertEqual(path.read_bytes(),before)
        settings=m.episode_source_settings(self.root)
        with self.assertRaisesRegex(ValueError,'episode_query_scope_not_authorized'):
            m.collect(self.root,settings,'fixture',request=API(),apply=True,base=m.EPISODE_BASE,state_file=m.EPISODE_STATE,query_filter={})

    def test_changed_note_preserves_history_and_manual_current_is_not_overwritten(self):
        api=API([self.source()]);self.collect(api);old,receipt=self.current()
        api.pages[0]['last_edited_time']='2026-11-01T00:00:00Z';self.collect(api)
        current,_=self.current();self.assertNotEqual(current,old);self.assertTrue(old.exists())
        self.assertFalse(m.eligible_path(self.root,old));self.assertTrue(m.eligible_path(self.root,current))
        current.write_text('Human edit');result=self.collect(api)
        self.assertFalse(result['selected_scope_complete']);self.assertEqual(current.read_text(),'Human edit');self.assertTrue(m.eligible_path(self.root,current))

    def test_legacy_rebuild_index_and_identity_scans_ignore_only_cloud_owned_namespace(self):
        self.collect();cloud=self.root/m.EPISODE_BASE/'unreferenced.md';cloud.write_text('---\nepisode_page_id: "'+uid(100)+'"\ntitle: "Cloud evidence"\n---\n')
        with patch.object(episode,'ROOT',self.root),patch.object(episode,'OUTPUT_ROOT',self.root/'Raw/13 小宇宙'),patch.object(episode,'LEGACY_OUTPUT_ROOTS',[]):
            self.assertEqual(episode.all_episode_files(uid(100)),[self.old])
            self.assertEqual(episode.find_existing_file_by_episode_id(self.root/'Raw/13 小宇宙',uid(100)),self.old)
            # Capture generated bytes without altering the preserved old index.
            with patch.object(episode,'write_verified_text') as write:episode.rebuild_index()
            generated=write.call_args.args[1];self.assertIn('episode_count: 1',generated);self.assertNotIn('Cloud evidence',generated)

    def test_existing_caller_original_only_routes_without_running_legacy_sync_or_index(self):
        args=['sync','--original-source-only','--max-pages','2']
        with patch.object(sys,'argv',args),patch.object(episode,'ROOT',self.root),patch.object(episode,'load_notion_api_key',return_value='fixture'),patch.object(m,'run_episode_source',return_value={'written':1,'partial_written':0,'unchanged':0,'partial_unchanged':0,'selected_scope_complete':False,'soft_exit':'page_budget'}) as run,patch.object(episode,'current_episode_pages') as scan,patch.object(episode,'rebuild_index') as index:
            with self.assertRaises(SystemExit) as error:episode.main()
            self.assertEqual(error.exception.code,2);run.assert_called_once_with(self.root,'fixture',apply=True,max_pages=2);scan.assert_not_called();index.assert_not_called()

    def test_remote_verifier_and_active_consumers_include_exact_new_base_state(self):
        self.collect();path,receipt=self.current()
        import iteration_incremental_audit as audit
        with patch.object(audit,'ROOT',self.root):
            candidate=audit.candidate_from_file(path,'Raw/13 小宇宙/',datetime.date(2026,1,1),datetime.date(2026,12,31))
        self.assertIsNotNone(candidate);self.assertEqual(candidate.source_identities,['notion:'+uid(100)])
        self.assertIn(str(path.relative_to(self.root)),m.all_active_selection(self.root)['current'])
        with patch.object(sys,'argv',['sync','--verify-remote-state']),patch.dict(os.environ,{'VAULT_ROOT':str(self.root)}),patch.object(m,'verify_remote') as verify:
            m.main()
        verify.assert_called_once_with(self.root,m.EPISODE_BASE,m.EPISODE_STATE)

    def test_episode_workflow_reuses_writer_and_persists_partial_content_with_state(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/sync-notion-personal-data.yml').read_text()
        steps={p.splitlines()[0]:p for p in text.split('      - name: ')[1:]}
        stage=steps['Capture complete Episode originals through existing caller']
        self.assertIn('--original-source-only --max-pages "$NOTION_SOURCE_MAX_PAGES"',stage)
        self.assertIn('!cancelled()',stage);self.assertIn('continue-on-error: true',stage)
        config=steps['Read private complete Episode source configuration'];self.assertIn("inputs.notion_source_key == 'episode'",config)
        commit=steps['Persist complete originals and durable progress']
        self.assertIn(str(m.EPISODE_BASE),commit);self.assertIn(str(m.EPISODE_STATE),commit)
        self.assertIn("steps.episode_source_sync.outcome == 'failure'",commit)
        self.assertLess(text.index('name: Commit Raw updates'),text.index('name: Capture complete Episode originals'))
        self.assertIn("steps.episode_source_sync.outcome == 'failure'",steps['Report incomplete original source capture'])
        self.assertEqual(text.count('- cron:'),2);self.assertIn('group: eddie-private-vault-writers',text)

if __name__=='__main__':unittest.main()
