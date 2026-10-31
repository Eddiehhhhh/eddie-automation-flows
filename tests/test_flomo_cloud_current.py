"""Synthetic captured sources; never access source APIs, credentials or a vault."""
import copy
from datetime import date, datetime
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import flomo_cloud_current as current
import flomo_original_source as original
import sync_notion_flomo_to_raw as shared
import iteration_incremental_audit as audit
import iteration_source_discovery as discovery

PNG=b'\x89PNG\r\nsynthetic-only-image'


def page(ident='page-one',title='Full synthetic title',created='2025-01-02'):
    return {'id':ident,'created_time':'2026-01-01T00:00:00Z','last_edited_time':'2026-10-31T12:00:00Z',
        'properties':{'Name':{'type':'title','title':[{'plain_text':title}]},
                      'Created At':{'type':'date','date':{'start':created}},
                      'Tags':{'type':'multi_select','multi_select':[{'name':'test/tag'}]}}}


def block(text='Notion original text'):
    return {'id':'block-one','type':'paragraph','has_children':False,
            'paragraph':{'rich_text':[{'plain_text':text,'annotations':{}}]}}


def memo(mid='YWJj',content='<p>Original title</p><p>Entire original body <strong>bold</strong> tail.</p>'):
    return {'slug':mid,'content':content,'created_at':'2025-01-02 01:02:03','updated_at':'2026-10-31 10:15:59',
            'files':[],'tags':['full/source'],'deleted_at':None}


class CurrentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.legacy=self.root/'Raw/01 Flomo/2025/Old.md'
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text('---\nmemo_id: "YWJj"\ncreated_at: "2025-01-02 01:02:03"\n---\nExisting legacy text.\n')
        self.legacy_before=self.legacy.read_bytes();self.notion_state={'version':1,'pages':{}}
        self.capture_notion()
    def capture_notion(self,mid='YWJj',ident='page-one',text='Notion source body',title='Full synthetic title',created='2025-01-02',image=False):
        p=page(ident,title,created);blocks=[block(text)];files={}
        if image:
            blocks.append({'id':'image-one','type':'image','has_children':False,
                'image':{'type':'external','external':{'url':'assets/test.png'}}});files['assets/test.png']=PNG
        files[mid+'.md']=shared.render_record(p,blocks,mid,[])
        files[mid+'.json']=shared.encoded({'page':p,'blocks':blocks,'materialized':{'page':p,'blocks':blocks}})
        version,hashes=shared.apply_record(self.root,mid,files,{})
        record={'notion_page_id':ident,'last_edited_time':p['last_edited_time'],'current_version':version,
                'files':hashes,'versions':{version:hashes}}
        prior=self.notion_state['pages'].get(mid)
        if prior:
            group=current.sources(prior);group[ident]=record;self.notion_state['pages'][mid]={'source_pages':group}
        else:self.notion_state['pages'][mid]=record
        shared.atomic_write(self.root/shared.STATE,shared.encoded(self.notion_state))
    def capture_original(self,values=None,downloader=lambda url:PNG):
        return original.enrich(self.root,shared,token='synthetic',signing_suffix='synthetic',max_pages=0,
            downloader=downloader,reader=lambda *args:values if values is not None else [memo()])
    def state(self):return json.loads((self.root/current.STATE).read_bytes())
    def note(self,mid='YWJj'):
        return self.root/self.state()['pages'][mid]['current_path']
    def test_readable_original_complete_body_precise_times_title_and_preserved_legacy(self):
        self.capture_original();result=current.export(self.root)
        self.assertEqual(result['written'],1);text=self.note().read_text()
        self.assertIn('Entire original body **bold** tail.',text)
        self.assertIn('2025-01-02T01:02:03+08:00',text);self.assertIn('2026-10-31T10:15:59+08:00',text)
        self.assertIn('Existing legacy text.',text);self.assertIn('Full synthetic title',text)
        self.assertIn('本次不可变快照',text)
        self.assertEqual(self.legacy.read_bytes(),self.legacy_before)
        self.assertEqual(self.note().stem,'Original-title')
        self.assertEqual(current.export(self.root)['unchanged'],1)
    def test_original_image_bytes_and_body_references_are_local_and_verified(self):
        value=memo(content='<p>Image title</p><p><img src="https://assets.invalid/image.png" alt="sample"></p>')
        value['files']=[{'url':'https://assets.invalid/image.png','name':'sample.png'}]
        self.capture_original([value]);current.export(self.root)
        note=self.note();text=note.read_text()
        targets=re.findall(r'!\[[^\]]*\]\(([^)]+)\)',text)
        self.assertTrue(targets)
        for target in targets:self.assertEqual((note.parent/target).read_bytes(),PNG)
        selected=current.active_selection(self.root)
        self.assertIn(str(self.legacy.relative_to(self.root)),selected['superseded'])
    def test_151_missing_original_notes_are_formal_readable_not_discarded_or_dated_at_midnight(self):
        self.capture_original([])
        result=current.export(self.root);text=self.note().read_text()
        self.assertEqual(result['written'],1);self.assertIn('Notion source body',text)
        self.assertIn('not_observed',text);self.assertIn('created_at: "2025-01-02"',text)
        self.assertNotIn('2025-01-02T00:00:00',text)
        self.assertFalse(result['media_complete']);self.assertTrue(result['capture_complete'])
    def test_multiple_notion_variants_keep_all_bodies_and_links_without_winner(self):
        self.capture_notion(ident='page-two',text='Different manually edited second body',title='Second source')
        self.capture_original([]);current.export(self.root);text=self.note().read_text()
        self.assertIn('Notion source body',text);self.assertIn('Different manually edited second body',text)
        self.assertIn('Notion 来源 1',text);self.assertIn('Notion 来源 2',text)
        self.assertIn('Second source',text);self.assertIn('Notion 多版本',text)
    def test_original_remains_primary_without_erasing_notion_variant_evidence(self):
        self.capture_notion(ident='page-two',text='Different body preserved in evidence')
        self.capture_original();current.export(self.root);text=self.note().read_text()
        self.assertIn('Entire original body',text);self.assertIn('Notion 来源 2',text)
        for _,record in current.sources(self.notion_state['pages']['YWJj']).items():
            rawpath=next(p for p in record['files'] if p.endswith('/YWJj.md'))
            self.assertTrue((self.root/shared.STAGE/rawpath).exists())
    def test_notion_fallback_image_is_embedded_from_saved_local_bytes(self):
        self.notion_state={'version':1,'pages':{}}
        self.capture_notion(image=True);self.capture_original([]);current.export(self.root)
        note=self.note();targets=re.findall(r'!\[[^\]]*\]\(([^)]+)\)',note.read_text())
        self.assertTrue(targets)
        self.assertEqual((note.parent/targets[0]).read_bytes(),PNG)

    def test_eleven_source_only_memos_retained_and_rendered(self):
        self.notion_state={'version':1,'pages':{}}
        shared.atomic_write(self.root/shared.STATE,shared.encoded(self.notion_state))
        self.capture_original([memo('orphan'+str(i)) for i in range(11)])
        self.assertEqual(current.export(self.root)['written'],11)
        self.assertEqual(len(self.state()['pages']),11)
        self.assertTrue(all('Entire original body' in self.note(mid).read_text() for mid in self.state()['pages']))
    def test_history_and_index_excluded_current_is_daily_input_and_physical_inventory_kept(self):
        self.capture_original();current.export(self.root)
        selected=current.active_selection(self.root);today=datetime.now(audit.SHANGHAI).date()
        with mock.patch.object(audit,'ROOT',self.root):
            self.assertIsNone(audit.candidate_from_file(self.legacy,'Raw/01 Flomo/',today,today,flomo_selection=selected))
            self.assertIsNone(audit.candidate_from_file(self.root/current.BASE/'index.md','Raw/01 Flomo/',today,today,flomo_selection=selected))
            self.assertIsNotNone(audit.candidate_from_file(self.note(),'Raw/01 Flomo/',today,today,flomo_selection=selected))
        with mock.patch.object(discovery,'ROOT',self.root):
            count=discovery.build_record('Raw/01 Flomo/',True)
        self.assertGreaterEqual(count.markdown_file_count,4)
    def test_weekly_audit_loads_selection_once_and_emits_only_current_not_legacy(self):
        self.capture_original();current.export(self.root);today=datetime.now(audit.SHANGHAI).date()
        with mock.patch.object(audit,'ROOT',self.root),mock.patch.object(discovery,'ROOT',self.root), \
             mock.patch.object(discovery,'RAW_DIR',self.root/'Raw'),mock.patch.object(discovery,'REGISTRY',self.root/'Schema/sources.md'), \
             mock.patch.object(discovery,'DEFAULT_OUTPUT',self.root/'Raw/00 Meta/discovery.json'), \
             mock.patch.object(current,'active_selection',wraps=current.active_selection) as select:
            result=audit.audit(today,7,10000)
        select.assert_called_once_with(self.root)
        self.assertEqual(result['counts']['total_candidates'],1)
        self.assertEqual(result['candidates'][0]['path'],str(self.note().relative_to(self.root)))

    def test_new_source_revision_is_new_immutable_path_one_active_current(self):
        self.capture_original();current.export(self.root);old=self.note();oldraw=old.read_bytes()
        value=memo(content='<p>New full title</p><p>Changed real body</p>');value['updated_at']='2026-11-01 03:04:05'
        self.capture_original([value]);current.export(self.root);new=self.note()
        self.assertNotEqual(old,new);self.assertEqual(old.read_bytes(),oldraw)
        selected=current.active_selection(self.root)
        self.assertEqual(selected['current'],{str(new.relative_to(self.root))})
        with mock.patch.object(audit,'ROOT',self.root),mock.patch.object(audit,'read_text_head',side_effect=AssertionError('history must not be read')):
            self.assertIsNone(audit.candidate_from_file(old,'Raw/01 Flomo/',date(2020,1,1),date(2030,1,1),flomo_selection=selected))
    def test_manual_current_edit_holds_never_overwrites_and_releases_legacy_suppression(self):
        self.capture_original();current.export(self.root);note=self.note();note.write_text(note.read_text()+'\nHuman edit.\n')
        before=note.read_bytes();result=current.export(self.root)
        self.assertEqual(result['held']['YWJj'],'owned_content_edited');self.assertEqual(note.read_bytes(),before)
        selected=current.active_selection(self.root)
        self.assertFalse(selected['superseded']);self.assertIn(str(note.relative_to(self.root)),selected['current'])
        self.assertIn(str(note.relative_to(self.root)),selected['edited'])
    def test_changed_legacy_is_not_suppressed_and_new_export_preserves_human_body(self):
        self.capture_original();current.export(self.root)
        self.legacy.write_bytes(self.legacy_before+b'\nNew human legacy thoughts.\n')
        selected=current.active_selection(self.root);self.assertFalse(selected['superseded'])
        self.assertIn(str(self.legacy.relative_to(self.root)),selected['edited'])
        current.export(self.root);self.assertIn('New human legacy thoughts.',self.note().read_text())
    def test_local_legacy_attachments_are_copied_and_rebased_not_broken(self):
        image=self.legacy.parent/'Asserts/photo.png';image.parent.mkdir();image.write_bytes(PNG)
        self.legacy.write_bytes(self.legacy_before+b'\n![local](Asserts/photo.png)\n')
        self.capture_original();current.export(self.root);note=self.note();text=note.read_text()
        target=re.search(r'!\[local\]\(([^)]+)\)',text)[1]
        self.assertEqual((note.parent/target).read_bytes(),PNG)
        image.write_bytes(b'changed source image')
        self.assertFalse(current.active_selection(self.root)['superseded'])
    def test_legacy_links_cannot_ingest_other_subsystem_configuration_as_attachment(self):
        config=self.root/'.github/private/synthetic-config.json';config.parent.mkdir(parents=True);config.write_bytes(b'synthetic private configuration')
        self.legacy.write_bytes(self.legacy_before+b'\n[config](../../../.github/private/synthetic-config.json)\n')
        self.capture_original();read=Path.read_bytes
        def checked(path):
            if path==config:raise AssertionError('must not read another subsystem configuration')
            return read(path)
        with mock.patch.object(Path,'read_bytes',checked):result=current.export(self.root)
        self.assertEqual(result['written'],1)
        self.assertFalse(any(b'synthetic private configuration' in p.read_bytes() for p in (self.root/current.BASE).rglob('*') if p.is_file()))

    def test_edited_asset_or_source_snapshot_prevents_false_legacy_suppression(self):
        value=memo();value['files']=[{'url':'https://assets.invalid/photo.png','name':'photo.png'}]
        self.capture_original([value]);current.export(self.root)
        asset=next((self.root/current.BASE/'_assets').iterdir());asset.write_bytes(b'human image edit')
        self.assertFalse(current.active_selection(self.root)['superseded'])
        self.assertEqual(current.export(self.root)['held']['YWJj'],'owned_content_edited')
    def test_degraded_original_media_keeps_legacy_active_without_discarding_current(self):
        import urllib.error
        value=memo();value['files']=[{'url':'https://assets.invalid/refused.png','name':'refused.png'}]
        self.capture_original([value],downloader=lambda url: (_ for _ in ()).throw(urllib.error.HTTPError(url,403,'denied',{},None)))
        result=current.export(self.root);selected=current.active_selection(self.root)
        self.assertEqual(result['written'],1);self.assertFalse(result['media_complete'])
        self.assertFalse(selected['superseded']);self.assertIn(str(self.note().relative_to(self.root)),selected['current'])

    def test_remote_requires_state_current_html_original_json_assets_history_and_index_blobs(self):
        value=memo();value['files']=[{'url':'https://assets.invalid/photo.png','name':'photo.png'}]
        self.capture_original([value]);current.export(self.root)
        state=self.state();record=state['pages']['YWJj']
        required={str(current.STATE),str(current.BASE/'index.md'),*current.owned_files(record),*record['source_files']}
        def blob(path):
            data=(self.root/path).read_bytes()
            return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        for omitted in [None,*sorted(required)]:
            def git(*args):
                ref=args[1];relative=ref.split(':',1)[1]
                return '0'*40 if relative==omitted else blob(relative)
            if omitted is None:self.assertTrue(current.verify_remote(self.root,git))
            else:
                with self.subTest(omitted=omitted),self.assertRaises(ValueError):current.verify_remote(self.root,git)

    def test_shared_writer_hook_is_opt_in_and_fails_private_gate_before_current_writes(self):
        with mock.patch.object(shared,'stage',return_value={'complete':True}), \
             mock.patch.object(shared,'writer_lock',return_value=nullcontext()), \
             mock.patch.object(shared.private_checkout,'verify_private_checkout'), \
             mock.patch.object(current,'export',return_value={'complete':True}) as exporter:
            shared.run(self.root,'db','key',apply=True)
            shared.run(self.root,'db','key',apply=False,current_export_enabled=True)
            exporter.assert_not_called()
            shared.run(self.root,'db','key',apply=True,current_export_enabled=True)
            exporter.assert_called_once_with(self.root)
        with mock.patch.object(shared.private_checkout,'verify_private_checkout',side_effect=ValueError('denied')), \
             mock.patch.object(current,'export') as exporter:
            with self.assertRaises(ValueError):shared.run(self.root,'db','key',apply=True,current_export_enabled=True)
            exporter.assert_not_called()

    def test_current_not_fed_back_as_legacy_or_extra_source_identity(self):
        self.capture_original();current.export(self.root)
        local,no_id=shared.local_inventory(self.root)
        self.assertEqual(local,{'YWJj':[str(self.legacy.relative_to(self.root))]})
        self.assertFalse(no_id)

    def test_full_title_kept_but_filename_utf8_bounded_and_human_readable(self):
        title='完整标题'*100
        self.capture_original([memo(content='<p>'+title+'</p><p>Body</p>')]);current.export(self.root)
        self.assertLessEqual(len(self.note().name.encode()),160)
        self.assertTrue(self.note().stem.startswith('完整标题'))
        self.assertIn(title,self.note().read_text())
    def test_no_original_no_source_date_does_not_invent_created_time(self):
        self.notion_state={'version':1,'pages':{}}
        self.capture_notion(created=None);current.export(self.root);text=self.note().read_text()
        self.assertNotIn('\ncreated_at:',text);self.assertIn('created_at_precision: "unknown"',text)
    def test_credential_in_existing_legacy_holds_only_that_memo_before_object_writes(self):
        self.legacy.write_bytes(self.legacy_before+('ghp_'+'A'*36).encode())
        self.capture_notion(mid='b2s',ident='other',text='Other safe source')
        result=current.export(self.root)
        self.assertEqual(result['held']['YWJj'],'potential_credential_in_source')
        self.assertEqual(set(self.state()['pages']),{'b2s'})
        self.assertFalse((self.root/current.BASE/'版本/YWJj').exists())
    def test_failed_state_write_does_not_pollute_other_current_receipt_and_resume(self):
        self.capture_notion(mid='b2s',ident='other');real=shared.atomic_write;failed=False
        def write(path,raw):
            nonlocal failed
            if path==self.root/current.STATE and not failed:failed=True;raise OSError('synthetic')
            return real(path,raw)
        with mock.patch.object(shared,'atomic_write',side_effect=write):result=current.export(self.root)
        self.assertEqual(result['written'],1);self.assertEqual(set(self.state()['pages']),{'b2s'})
        self.assertEqual(current.export(self.root)['written'],1)
    def test_old_raw01_and_other_sources_not_broadly_excluded(self):
        self.capture_original();current.export(self.root)
        unrelated=self.root/current.BASE/'unmapped.md';unrelated.write_text('---\ncreated_at: 2026-11-01\n---\nUnmapped evidence.')
        selected=current.active_selection(self.root)
        with mock.patch.object(audit,'ROOT',self.root):
            self.assertIsNotNone(audit.candidate_from_file(unrelated,'Raw/01 Flomo/',date(2026,11,1),date(2026,11,1),flomo_selection=selected))
    def test_all_151_original_missing_notes_and_captured_watermarks_are_preserved(self):
        self.notion_state={'version':1,'pages':{}}
        for i in range(151):self.capture_notion(mid='missing'+str(i),ident='page'+str(i),text='Complete source '+str(i))
        self.capture_original([])
        before=(self.root/shared.STATE).read_bytes()
        with mock.patch.object(shared.notion,'notion_request',side_effect=AssertionError('export must not query source API')):
            result=current.export(self.root)
        self.assertEqual(result['written'],151);self.assertTrue(result['capture_complete'])
        self.assertFalse(result['media_complete']);self.assertEqual((self.root/shared.STATE).read_bytes(),before)
        self.assertEqual(len(self.state()['pages']),151)
        self.assertIn('Complete source 150',self.note('missing150').read_text())

    def test_original_code_resource_urls_are_never_rewritten_or_dropped(self):
        url='https://assets.invalid/photo.png'
        text='<p>Title</p><pre><code>url="'+url+'"\n\nend</code></pre><p><code>'+url+'</code></p><p><img src="'+url+'"></p>'
        body=current.ReadableHTML(text,{url:'../../../_assets/saved.png'}).markdown()
        self.assertIn('url="'+url+'"\n\nend',body)
        self.assertIn('`'+url+'`',body)
        self.assertIn('![](../../../_assets/saved.png)',body)

    def test_title_with_literal_html_remains_visible_in_markdown_heading(self):
        self.capture_original([memo(content='<p>&lt;example&gt;literal&lt;/example&gt;</p>')])
        current.export(self.root)
        self.assertIn('# &lt;example&gt;literal&lt;/example&gt;',self.note().read_text())

    def test_literal_escaped_html_is_readable_not_executed_as_markdown_html(self):
        body=current.ReadableHTML('<p>&lt;example&gt;literal&lt;/example&gt;</p><pre><code>x &lt; y</code></pre>',{}).markdown()
        self.assertIn('&lt;example&gt;literal&lt;/example&gt;',body)
        self.assertIn('x < y',body)

    def test_closeout_counts_selected_current_once_and_never_import_time_or_stage_history(self):
        import iteration_closeout as closeout
        self.capture_original();current.export(self.root)
        with mock.patch.object(closeout,'ROOT',self.root):
            rows=closeout.entries_in_range(self.root/'Raw/01 Flomo','flomo',date(2020,1,1),date(2030,1,1))
            stage=closeout.entries_in_range(self.root/'Raw/02 Notion','notion',date(2020,1,1),date(2030,1,1))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0].path,self.note())
        self.assertEqual(rows[0].evidence_date,date(2025,1,2));self.assertEqual(stage,[])

    def test_filename_fixer_never_renames_cloud_owned_immutable_note(self):
        import audit_raw_filenames as naming
        self.capture_original();current.export(self.root);note=self.note();before=note.read_bytes()
        violation=naming.Violation(note,'flomo','synthetic','renamed.md')
        with mock.patch.object(naming,'ROOT',self.root):
            self.assertFalse(naming.fix_violation(violation))
            self.assertFalse(any(v.path.is_relative_to(self.root/current.BASE) for v in naming.scan_directory(self.root/'Raw/01 Flomo','flomo')))
        self.assertEqual(note.read_bytes(),before)

    def test_html_lists_code_quotes_entities_and_complete_text(self):
        source='<p>A &amp; B</p><ol><li>One</li><li>Two</li></ol><pre><code>x &lt; y\n\n\nend</code></pre><p>Tail</p><blockquote><p>Quoted third party</p></blockquote>'
        rendered=current.ReadableHTML(source,{}).markdown()
        for value in ('A & B','1. One','2. Two','x < y\n\n\nend','Tail','> Quoted third party'):self.assertIn(value,rendered)


if __name__=='__main__':unittest.main()
