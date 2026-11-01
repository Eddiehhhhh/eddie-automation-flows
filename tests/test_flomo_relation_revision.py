"""Synthetic source/cache revisions. No network or actual source data."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import sync_notion_flomo_to_raw as m
import flomo_cloud_current as current
import test_flomo_property_proof as fixtures


class RelationRevision(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ProofTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root
        self.target='12345678-1234-1234-1234-1234567890ab'
        self.fixture.rows[0]['properties']['日记']['relation']=[{'id':self.target}]
    def old_capture(self,**kwargs):
        real=m.render_record
        def old(*args,**kw):kw['property_renderer_revision']=1;return real(*args,**kw)
        with patch.object(m,'PROPERTY_RENDERER_REVISION',1),patch.object(m,'render_record',side_effect=old):
            result=self.fixture.run_sync(property_proof_enabled=True,**kwargs)
        self.assertTrue(result['complete']);return deepcopy(self.fixture.record())
    def stage_md(self,record):return self.root/m.STAGE/next(p for p in record['files'] if p.endswith('/memo-a.md'))
    def snapshot_bytes(self,record):return (self.root/m.STAGE/next(p for p in record['files'] if p.endswith('/memo-a.json'))).read_bytes()

    def test_same_source_cached_revision_preserves_json_assets_and_old_versions_without_hydration(self):
        self.fixture.block_rows.append({'id':'asset-block','type':'image','image':{'type':'external','external':{'url':'https://assets.invalid/source.png'}}})
        old=self.old_capture(downloader=lambda url:b'synthetic-image')
        raw=self.snapshot_bytes(old);old_files={p:(self.root/m.STAGE/p).read_bytes() for p in old['files']}
        self.fixture.calls.clear()
        result=self.fixture.run_sync(property_proof_enabled=True,max_pages=2,downloader=lambda url:self.fail('cached asset redownload'))
        self.assertEqual((result['written'],result['renderer_rewritten'],result['property_hydrated']),(1,1,0))
        new=self.fixture.record();self.assertEqual(new['property_proof_version'],1);self.assertEqual(new['property_renderer_revision'],2)
        self.assertEqual(self.snapshot_bytes(new),raw);self.assertNotEqual(new['current_version'],old['current_version'])
        self.assertTrue(set(old['versions'])<set(new['versions']))
        self.assertTrue(all((self.root/m.STAGE/p).read_bytes()==value for p,value in old_files.items()))
        self.assertEqual([p for method,p in self.fixture.calls],['/databases/synthetic-db/query','/pages/page-a','/pages/page-a'])
        self.assertIn('https://www.notion.so/'+self.target.replace('-',''),self.stage_md(new).read_text())
        self.assertNotIn('https://www.notion.so/'+self.target.replace('-',''),self.stage_md(old).read_text())
        self.fixture.calls.clear();again=self.fixture.run_sync(property_proof_enabled=True)
        self.assertEqual((again['written'],again['unchanged']),(0,1));self.assertEqual(len(self.fixture.calls),1)

    def test_existing_proof_without_renderer_field_is_revision_one_and_can_upgrade(self):
        old=self.old_capture();path=self.root/m.STATE;state=json.loads(path.read_bytes())
        state['pages']['memo-a'].pop('property_renderer_revision');path.write_bytes(m.encoded(state))
        self.fixture.calls.clear();result=self.fixture.run_sync(property_proof_enabled=True,max_pages=2)
        self.assertEqual(result['renderer_rewritten'],1);self.assertEqual(result['property_hydrated'],0)
        self.assertEqual(self.fixture.record()['property_renderer_revision'],2)
        self.assertEqual(self.snapshot_bytes(self.fixture.record()),self.snapshot_bytes(old))

    def test_default_unproved_and_revision_one_reading_unchanged(self):
        page=self.fixture.rows[0]
        plain=m.render_record(page,[],'memo-a',[]).decode()
        self.assertNotIn('Notion 关联',plain);self.assertNotIn('property_renderer_revision',plain)
        old=m.render_record(page,[],'memo-a',[],include_text_properties=True,property_renderer_revision=1).decode()
        self.assertNotIn('Notion 关联',old)

    def test_verified_cache_upgrade_precedes_unproved_historical_backfill(self):
        self.old_capture();self.fixture.rows.append(fixtures.source('memo-b','page-b'))
        self.fixture.run_sync()  # legacy body capture for the second source
        result=self.fixture.run_sync(property_proof_enabled=True,max_pages=1)
        self.assertEqual(result['renderer_rewritten'],1);self.assertEqual(result['property_hydrated'],0)
        self.assertEqual(self.fixture.record()['property_renderer_revision'],2)
        self.assertNotIn('property_proof_version',self.fixture.record('memo-b'))

    def test_fresh_input_keeps_priority_over_cache_revision(self):
        self.old_capture();self.fixture.rows.append(fixtures.source('memo-b','page-b'))
        result=self.fixture.run_sync(property_proof_enabled=True,max_pages=1)
        self.assertEqual(result['property_hydrated'],1);self.assertEqual(result['renderer_rewritten'],0)
        self.assertEqual(self.fixture.record()['property_renderer_revision'],1)
        self.assertEqual(self.fixture.record('memo-b')['property_renderer_revision'],2)

    def test_source_changed_uses_safe_fresh_capture_instead_of_stale_cache(self):
        old=self.old_capture();self.fixture.rows[0]['properties']['Tags']['multi_select']=[]
        self.fixture.calls.clear();result=self.fixture.run_sync(property_proof_enabled=True)
        self.assertEqual(result['property_hydrated'],1);self.assertEqual(result['renderer_rewritten'],0)
        self.assertTrue(any('/properties/' in path for method,path in self.fixture.calls))
        self.assertNotEqual(self.snapshot_bytes(old),self.snapshot_bytes(self.fixture.record()))

    def test_incomplete_or_manually_changed_cache_holds_without_progress(self):
        self.old_capture();state_path=self.root/m.STATE
        state=json.loads(state_path.read_bytes());record=state['pages']['memo-a']
        relative=next(p for p in record['files'] if p.endswith('/memo-a.json'));path=self.root/m.STAGE/relative
        original=path.read_bytes();data=json.loads(original);data['property_capture']['property_responses']['text-prop'][0]['has_more']=True
        path.write_bytes(m.encoded(data));before=state_path.read_bytes()
        result=self.fixture.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held']['memo-a'],'stage_edited');self.assertEqual(state_path.read_bytes(),before)
        path.write_bytes(original)
        with patch.object(m,'cached_proof_files',side_effect=ValueError('incomplete_property_proof')):
            result=self.fixture.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held']['memo-a'],'incomplete_property_proof');self.assertEqual(state_path.read_bytes(),before)

    def test_hash_matched_but_incomplete_saved_response_is_not_reused_as_proof(self):
        self.old_capture();path=self.root/m.STATE;state=json.loads(path.read_bytes());record=state['pages']['memo-a']
        relative=next(name for name in record['files'] if name.endswith('/memo-a.json'))
        source_path=self.root/m.STAGE/relative;snapshot=json.loads(source_path.read_bytes())
        snapshot['property_capture']['property_responses']['text-prop'][0]['has_more']=True
        raw=m.encoded(snapshot);source_path.write_bytes(raw);checksum=m.digest(raw)
        record['files'][relative]=checksum
        for files in record['versions'].values():
            if relative in files:files[relative]=checksum
        path.write_bytes(m.encoded(state));before=path.read_bytes();self.fixture.calls.clear()
        result=self.fixture.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held']['memo-a'],'incomplete_property_proof')
        self.assertEqual(path.read_bytes(),before);self.assertFalse(any('/properties/' in p for method,p in self.fixture.calls))

    def test_cached_json_credential_gate_runs_before_new_object_or_progress(self):
        self.old_capture();path=self.root/m.STATE;state=json.loads(path.read_bytes());record=state['pages']['memo-a']
        relative=next(name for name in record['files'] if name.endswith('/memo-a.json'))
        source_path=self.root/m.STAGE/relative;snapshot=json.loads(source_path.read_bytes())
        snapshot['synthetic_provider_debug']='secret_'+'Z'*40
        raw=m.encoded(snapshot);source_path.write_bytes(raw);record['files'][relative]=m.digest(raw)
        for files in record['versions'].values():
            if relative in files:files[relative]=m.digest(raw)
        path.write_bytes(m.encoded(state));before=path.read_bytes()
        result=self.fixture.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held']['memo-a'],'potential_credential_in_source');self.assertEqual(path.read_bytes(),before)
        self.assertEqual(result['written'],0)

    def test_final_source_change_during_cache_revision_keeps_prior_state(self):
        self.old_capture();before=(self.root/m.STATE).read_bytes();real=self.fixture.request;reads=[]
        def change(key,method,path,payload=None):
            value=real(key,method,path,payload)
            if path=='/pages/page-a':
                reads.append(path)
                if len(reads)==2:value['properties']['Tags']['multi_select']=[]
            return value
        with patch.object(m.notion,'notion_request',side_effect=change):result=self.fixture.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held']['memo-a'],'source_changed_during_read');self.assertEqual((self.root/m.STATE).read_bytes(),before)

    def test_current_follows_stage_revision_without_global_renderer_bump_and_old_facts_remain(self):
        old=self.old_capture();current.export(self.root)
        state=json.loads((self.root/current.STATE).read_bytes());prior=state['pages']['memo-a'];old_note=self.root/prior['current_path']
        self.assertEqual(m.notion.extract_frontmatter_value(old_note.read_text(),'current_renderer_version'),'1')
        self.fixture.run_sync(property_proof_enabled=True);result=current.export(self.root)
        self.assertEqual(result['written'],1)
        after=json.loads((self.root/current.STATE).read_bytes())['pages']['memo-a'];self.assertNotEqual(after['current_path'],prior['current_path'])
        self.assertIn('"page-a": 2',(self.root/after['current_path']).read_text())
        self.assertTrue(old_note.exists());self.assertFalse(current.eligible_path(self.root,old_note,current.active_selection(self.root)))
        self.assertTrue(current.eligible_path(self.root,self.root/after['current_path'],current.active_selection(self.root)))
        body=current.notion_body(self.root,'memo-a',self.fixture.record(),{}, {})[2]
        self.assertIn('https://www.notion.so/'+self.target.replace('-',''),body)
        self.assertEqual(self.fixture.record()['last_edited_time'],old['last_edited_time'])

if __name__=='__main__':unittest.main()
