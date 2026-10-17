"""No live requests. Fidelity path tests use exact source tokens and local Git."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
import shutil
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from get_mirror_incremental_replay import (prepare_private_preview,snapshot,ParityFailure,
    raw_source_version,load_faithful_state,read_faithful_state,empty_faithful_state)
from get_mirror_render import FaithfulWriter,render
from source_sync_assets import AssetStore

class FaithfulPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name);self.vault=self.work/'vault';self.root=self.vault/'Raw/03 Get'
        (self.root/'03 State').mkdir(parents=True)
        (self.root/'03 State/manifest.json').write_text('{"notes":[],"unknown_metadata":{"keep":true}}')
        self.git('init','-q');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid');self.commit()
        self.note={'id':'123','note_type':'plain_text','title':'Readable','content':'Exact body  \n\n',
                   'created_at':'2026-04-23 18:29:55','updated_at':'2026-04-23 18:29:55'}
        self.calls=[]
    def git(self,*args):return subprocess.check_output(['git','-C',str(self.vault),*args],text=True,stderr=subprocess.DEVNULL)
    def commit(self):self.git('add','.');self.git('commit','-qm','fixture');self.head=self.git('rev-parse','HEAD').strip()
    def factory(self,root):
        def forbidden(*args,**kwargs):raise AssertionError('Unexpected binary transfer')
        return FaithfulWriter(root,AssetStore(root,fetch=forbidden))
    def preview(self,name='p',detail=None,listing=None):
        def fetch(ident):self.calls.append(ident);return deepcopy(self.note)
        return prepare_private_preview(self.vault,self.work/name,self.head,listing or (lambda:([deepcopy(self.note)],True)),detail or fetch,self.factory,faithful=True)
    def state(self,name='p'):return json.loads((self.work/name/'candidate/03 State/cloud-sync-state.json').read_text())
    def adopt(self,name='p'):
        shutil.copytree(self.work/name/'candidate',self.root,dirs_exist_ok=True);self.commit();self.calls=[]
    def legacy(self):
        path=self.root/'old.md'
        data=b'---\nnote_id: "123"\ncreated_at: "2020-01-01T00:00:00Z"\nupdated_at: "2026-04-23T10:29:55Z"\naudio_transcript: "old original"\nweb_excerpt: "old quote"\nunknown: {keep: [1, 2]}\n---\nHand edited body\n\n'
        path.write_bytes(data)
        (self.root/'03 State/manifest.json').write_text(json.dumps({'notes':[{'note_id':'123','path':'old.md','updated_at':'2026-04-23T10:29:55Z'}]}))
        self.commit();return path,data
    def test_raw_tokens_do_not_normalize_or_guess_timezone(self):
        values=['2026-04-23 18:29:55','2026-04-23T18:29:55','2026-04-23T18:29:55+08:00','2026-04-23T10:29:55Z',1776968995,1776968995.0]
        tokens=[raw_source_version(v) for v in values]
        self.assertEqual(len({json.dumps(v,sort_keys=True) for v in tokens}),len(values))
        for value in (None,True,'bad',' 2026-04-23 18:29:55',float('nan'),0,{}):
            with self.assertRaises(ParityFailure):raw_source_version(value)
    def test_new_note_exact_source_dates_and_body_then_independent_zero_detail(self):
        before=snapshot(self.root);result,counts=self.preview()
        self.assertEqual(snapshot(self.root),before);self.assertEqual(self.calls,['123'])
        state=self.state();entry=state['notesByCanonicalId']['api:123']
        text=(self.work/'p/candidate'/entry['filePath']).read_text()
        self.assertIn('created_at: "2026-04-23 18:29:55"',text);self.assertIn('updated_at: "2026-04-23 18:29:55"',text)
        self.assertNotIn('+08:00',text);self.assertIn(self.note['content'],text)
        self.assertTrue(state['local_proposal_only']);self.assertNotIn('updatedAt',entry)
        self.assertEqual(json.loads((self.work/'p/candidate/03 State/manifest.json').read_text())['unknown_metadata'],{'keep':True})
        self.adopt();result,counts=self.preview('p2');self.assertEqual(self.calls,[]);self.assertEqual(counts['changed_paths'],0)
    def test_all_legacy_fields_body_dates_preserved_without_detail_or_watermark(self):
        path,data=self.legacy();before=snapshot(self.root)
        def forbidden(_):raise AssertionError('No raw baseline; do not bulk-fetch')
        result,counts=self.preview(detail=forbidden)
        self.assertEqual(snapshot(self.root),before);self.assertEqual((self.work/'p/candidate/old.md').read_bytes(),data)
        state=self.state();self.assertEqual(state['notesByCanonicalId'],{})
        self.assertEqual(state['conflictsById']['123']['reason'],'legacy_baseline_missing')
        self.assertEqual(counts['pending'],1);self.assertEqual(result['first']['legacy_conflicts'],1)
    def test_corrupt_old_preview_watermarks_are_rejected_not_adopted(self):
        self.legacy();(self.root/'03 State/cloud-sync-state.json').write_text('{"version":1,"contract":"get-v7-cloud-mirror-1"}');self.commit()
        before=snapshot(self.root)
        with self.assertRaisesRegex(ParityFailure,'untrusted_legacy_state'):self.preview()
        self.assertEqual(snapshot(self.root),before);self.assertFalse((self.work/'p').exists());self.assertEqual(self.calls,[])
    def test_managed_source_update_has_current_body_and_one_prior_history(self):
        self.preview();self.adopt();old=self.state();entry=old['notesByCanonicalId']['api:123'];data=(self.root/entry['filePath']).read_bytes()
        self.note.update(updated_at='2026-04-24 18:29:55',content='new source version')
        result,counts=self.preview('changed');self.assertEqual(self.calls,['123'])
        next_state=self.state('changed');current=next_state['notesByCanonicalId']['api:123']
        self.assertEqual(next_state['conflictsById'],{});self.assertEqual(len(current['history']),1)
        root=self.work/'changed/candidate';self.assertEqual((root/current['history'][0]['filePath']).read_bytes(),data)
        body=(root/entry['filePath']).read_text();self.assertIn('new source version',body);self.assertNotIn('Exact body',body)
        self.assertIn('updated_at: "2026-04-24 18:29:55"',body)
        self.assertEqual(result['first']['source_updated'],1);self.assertEqual(counts['pending'],0)
        self.adopt('changed');result,counts=self.preview('repeat');self.assertEqual(counts['changed_paths'],0);self.assertEqual(self.calls,[])
        self.assertEqual(len(self.state('repeat')['notesByCanonicalId']['api:123']['history']),1)
    def test_metadata_version_update_is_exact_raw_and_preserves_prior_full_md(self):
        self.preview();self.adopt();old=self.state();entry=old['notesByCanonicalId']['api:123'];before=(self.root/entry['filePath']).read_bytes()
        self.note['updated_at']='2026-04-24 18:29:55';result,counts=self.preview('metadata')
        next_entry=self.state('metadata')['notesByCanonicalId']['api:123'];root=self.work/'metadata/candidate'
        self.assertNotEqual(entry['sourceVersion'],next_entry['sourceVersion'])
        self.assertEqual(before,(root/next_entry['history'][0]['filePath']).read_bytes())
        self.assertEqual(counts['pending'],0);self.assertEqual(self.calls,['123'])
    def test_unknown_source_field_change_is_conflict(self):
        self.note['unknown']={'original':'value'};self.preview();self.adopt();self.note['unknown']['original']='changed';self.note['updated_at']='2026-04-24 18:29:55'
        self.preview('unknown');self.assertEqual(self.state('unknown')['conflictsById']['123']['reason'],'source_unknown_changed')
    def test_user_edit_rejected_without_overwrite(self):
        self.preview();self.adopt();entry=self.state()['notesByCanonicalId']['api:123'];path=self.root/entry['filePath'];path.write_text(path.read_text()+'manual edit\n');self.commit();before=snapshot(self.root)
        with self.assertRaisesRegex(ParityFailure,'cloud_mirror_changed'):self.preview('edit')
        self.assertEqual(snapshot(self.root),before);self.assertEqual(self.calls,[])
    def test_pending_becomes_ready_next_run_but_not_inside_one_run(self):
        self.note.update(note_type='recorder_audio',audio={'original':''});self.preview();self.adopt();self.note['audio']['original']='Actual transcript.'
        _,counts=self.preview('ready');self.assertEqual(counts['pending'],0);self.assertEqual(self.calls,['123'])
        entry=self.state('ready')['notesByCanonicalId']['api:123'];body=(self.work/'ready/candidate'/entry['filePath']).read_text();self.assertIn('audio_transcript: "Actual transcript."',body)
    def test_in_run_pending_completion_cleans_entire_candidate(self):
        self.note.update(note_type='recorder_audio',audio={'original':''});calls=[]
        def fetch(_):
            calls.append(1);value=deepcopy(self.note)
            if len(calls)>1:value['audio']['original']='Became ready'
            return value
        before=snapshot(self.root)
        with self.assertRaisesRegex(ParityFailure,'pending_changed_between_passes'):self.preview(detail=fetch)
        self.assertEqual(snapshot(self.root),before);self.assertFalse((self.work/'p').exists())
    def test_unstable_list_rolls_back_new_note_and_state(self):
        calls=[]
        def listing():
            calls.append(1);note=deepcopy(self.note)
            if len(calls)>1:note['updated_at']='2026-04-24 18:29:55'
            return [note],True
        before=snapshot(self.root)
        with self.assertRaises(ParityFailure):self.preview(listing=listing)
        self.assertEqual(snapshot(self.root),before);self.assertFalse((self.work/'p').exists())
    def test_naive_detail_and_aware_list_are_not_implicitly_equal(self):
        summary=deepcopy(self.note);summary['updated_at']='2026-04-23T18:29:55+08:00'
        with self.assertRaisesRegex(ParityFailure,'source_changed_during_pass'):self.preview(listing=lambda:([summary],True))
    def test_writer_refuses_any_existing_note_before_materialization(self):
        path,data=self.legacy();writer=self.factory(self.root)
        with self.assertRaisesRegex(ValueError,'legacy_reconciliation_required'):writer.write(self.note,self.note,'old.md')
        self.assertEqual(path.read_bytes(),data)
    def test_source_sections_and_original_transcript_are_distinct(self):
        note={**self.note,'note_type':'audio','audio':{'original':'Exact transcript  \n'},'ref_content':'Source quotation','web_page':{'content':'Entire web page','excerpt':'AI web summary'}}
        body,fields=render(note,[],Path('x.md'),faithful=True)
        for value in (note['content'],note['audio']['original'],note['ref_content'],note['web_page']['content'],note['web_page']['excerpt']):self.assertIn(value,body)
        self.assertEqual(fields['audio_transcript'],note['audio']['original'])
        self.assertEqual(render(self.note,[],Path('x.md'),faithful=True)[1]['audio_transcript'],'')
    def test_private_report_stays_partial_and_excludes_raw_private_data(self):
        from source_sync_mirror_replay import safe_preview_report
        self.legacy();result,counts=self.preview()
        value={**result,**counts,'status':'private_preview_partial','secret':'private title'}
        clean=safe_preview_report(value);self.assertEqual(clean['status'],'private_preview_partial')
        self.assertEqual(clean,safe_preview_report(clean));self.assertFalse(clean['apply_ready']);self.assertTrue(clean['local_proposal_only'])
        self.assertNotIn('private title',json.dumps(clean));self.assertEqual(clean['conflicts'],1)
    def test_new_only_report_ready_for_review_never_apply(self):
        from source_sync_mirror_replay import safe_preview_report
        result,counts=self.preview();value={**result,**counts,'status':'private_preview_ready_for_review'}
        clean=safe_preview_report(value);self.assertEqual(clean['status'],'private_preview_ready_for_review');self.assertFalse(clean['apply_ready'])
    def test_duplicate_ids_and_incomplete_list_do_not_touch_source(self):
        for listing in (lambda:([self.note,self.note],True),lambda:([self.note],False)):
            before=snapshot(self.root)
            with self.assertRaises(ParityFailure):self.preview(listing=listing)
            self.assertEqual(snapshot(self.root),before)
    def test_strict_state_duplicate_keys_null_symlink_and_oversize(self):
        target=self.root/'03 State/cloud-sync-state.json'
        for value in ('null','{"a":1,"a":2}','{"a":NaN}',' '*(2*1024*1024+1)):
            target.write_text(value)
            with self.assertRaises(ParityFailure):read_faithful_state(self.root,target)
        target.unlink();target.symlink_to(self.root/'03 State/manifest.json')
        with self.assertRaises(ParityFailure):read_faithful_state(self.root,target)

    def test_source_field_disappearance_conflicts_without_stale_current_or_state_advance(self):
        self.note['web_page']={'url':'https://example.test/page','excerpt':'Prior summary','content':'Full page'}
        self.preview();self.adopt();old=self.state();entry=old['notesByCanonicalId']['api:123'];before=(self.root/entry['filePath']).read_bytes()
        self.note.update(updated_at='2026-04-24 18:29:55',content='Changed body');self.note.pop('web_page')
        result,counts=self.preview('missing');next_state=self.state('missing')
        self.assertEqual(next_state['notesByCanonicalId'],old['notesByCanonicalId']);self.assertEqual(next_state['conflictsById']['123']['reason'],'source_text_missing')
        self.assertEqual((self.work/'missing/candidate'/entry['filePath']).read_bytes(),before);self.assertEqual(counts['pending'],1)
    def test_current_audio_field_and_body_update_together_with_prior_history(self):
        self.note.update(note_type='audio',audio={'original':'Old transcript'});self.preview();self.adopt()
        self.note['audio']['original']='New transcript';self.note['updated_at']='2026-04-24 18:29:55'
        self.preview('audio');entry=self.state('audio')['notesByCanonicalId']['api:123'];body=(self.work/'audio/candidate'/entry['filePath']).read_text()
        self.assertIn('audio_transcript: "New transcript"',body);self.assertNotIn('Old transcript',body);self.assertEqual(len(entry['history']),1)
    def test_original_created_date_change_conflicts(self):
        self.preview();self.adopt();self.note['created_at']='2020-01-01 00:00:00';self.note['updated_at']='2026-04-24 18:29:55'
        self.preview('date');self.assertEqual(self.state('date')['conflictsById']['123']['reason'],'source_field_missing')
    def test_summary_fallback_change_is_hashed_and_reconciled(self):
        def detail(_):return {k:v for k,v in self.note.items() if k!='content'}
        self.preview(detail=detail);self.adopt();self.note['content']='New summary-only body';self.note['updated_at']='2026-04-24 18:29:55'
        self.preview('fallback',detail=detail);entry=self.state('fallback')['notesByCanonicalId']['api:123']
        self.assertIn('New summary-only body',(self.work/'fallback/candidate'/entry['filePath']).read_text())
    def test_summary_change_same_token_within_run_fails(self):
        calls=[]
        def listing():
            calls.append(1);value=deepcopy(self.note)
            if len(calls)>1:value['content']='Changed during pass'
            return [value],True
        def detail(_):return {k:v for k,v in self.note.items() if k!='content'}
        with self.assertRaises(ParityFailure):self.preview(detail=detail,listing=listing)
        self.assertFalse((self.work/'p').exists())
    def test_exact_crlf_content_survives_byte_readback(self):
        self.note['content']='First line\r\nSecond line\r\n';self.preview();entry=self.state()['notesByCanonicalId']['api:123']
        self.assertIn(self.note['content'].encode(),(self.work/'p/candidate'/entry['filePath']).read_bytes())
        self.adopt();self.note['content']='New first\r\nNew second\r\n';self.note['updated_at']='2026-04-24 18:29:55';self.preview('crlf')
        self.assertIn(self.note['content'].encode(),(self.work/'crlf/candidate'/entry['filePath']).read_bytes())
    def test_contradictory_faithful_report_never_falls_back_to_legacy_success(self):
        from source_sync_mirror_replay import safe_preview_report
        result,counts=self.preview();value={**result,**counts,'status':'private_preview_ready_for_review'}
        for change in ({'conflicts':1},{'conflicts':True},{'local_proposal_only':True,'pending_set_stable':False}):
            self.assertEqual(safe_preview_report({**value,**change})['status'],'failed')
    def test_faithful_retention_plan_build_is_compatible_without_remote_write(self):
        import get_private_preview_retention as r
        self.legacy();self.preview();self.git('remote','add','origin',r.REMOTE_URL);before=self.git('show-ref')
        prepared=r.build_preview(r.LocalGit(self.vault),self.work/'p',self.head)
        self.assertEqual(self.git('show-ref'),before);self.assertTrue(prepared['commit'])
        plan=json.loads((self.work/'p/plan.json').read_text());self.assertTrue(r.valid_plan_keys(plan))
        for changes in ({'state_contract':'unknown'},{'conflicts':True},{'conflicts':2},{'local_proposal_only':False},{'extra':'not allowed'}):
            self.assertFalse(r.valid_plan_keys({**plan,**changes}))
    def test_history_bytes_are_bound_before_next_source_update(self):
        self.preview();self.adopt();self.note.update(content='second',updated_at='2026-04-24 18:29:55');self.preview('second');self.adopt('second')
        entry=self.state('second')['notesByCanonicalId']['api:123'];(self.root/entry['history'][0]['filePath']).write_bytes(b'corrupt history');self.commit()
        with self.assertRaisesRegex(ParityFailure,'cloud_mirror_changed'):self.preview('bad_history')

    def test_omitted_body_or_original_source_text_never_becomes_success(self):
        self.note.update(content='Original body',ref_content='Original quotation',audio_transcript='Original explicit transcript',
                         audio={'original':'Original audio','transcript':'Explicit audio'},web_page={'content':'Entire original web page','excerpt':'Original web summary'})
        self.preview();self.adopt();prior=self.state();entry=prior['notesByCanonicalId']['api:123'];before=(self.root/entry['filePath']).read_bytes()
        original=deepcopy(self.note)
        for i,path in enumerate(('content','ref_content','audio_transcript','audio.original','audio.transcript','web_page.content','web_page.excerpt')):
            self.note=deepcopy(original);self.note['updated_at']='2026-04-24 18:29:55';parts=path.split('.');target=self.note
            for part in parts[:-1]:target=target[part]
            target.pop(parts[-1]);name='missing-'+str(i)
            self.preview(name);state=self.state(name)
            self.assertEqual(state['notesByCanonicalId'],prior['notesByCanonicalId']);self.assertEqual(state['conflictsById']['123']['reason'],'source_text_missing')
            self.assertEqual((self.work/name/'candidate'/entry['filePath']).read_bytes(),before)
    def test_empty_body_is_unresolved_without_explicit_delete_semantics(self):
        self.preview();self.adopt();self.note.update(content='',updated_at='2026-04-24 18:29:55')
        self.preview('empty');self.assertEqual(self.state('empty')['conflictsById']['123']['reason'],'source_text_missing')
    def test_invalid_source_text_type_cannot_be_certified(self):
        self.note['content']={'unexpected':'object'}
        with self.assertRaisesRegex(ParityFailure,'invalid_source_text'):self.preview()
        self.assertFalse((self.work/'p').exists())

    def test_created_time_numeric_type_is_exact_not_python_equal(self):
        self.note['created_at']=1776968995;self.preview();self.adopt()
        self.note['created_at']=1776968995.0;self.note['updated_at']='2026-04-24 18:29:55'
        self.preview('typed');self.assertEqual(self.state('typed')['conflictsById']['123']['reason'],'source_field_missing')
    def test_new_note_requires_actual_created_time(self):
        self.note.pop('created_at')
        with self.assertRaisesRegex(ParityFailure,'invalid_raw_version'):self.preview()
        self.assertFalse((self.work/'p').exists())

    def test_manifest_unknown_row_metadata_survives_managed_update(self):
        self.preview();self.adopt();path=self.root/'03 State/manifest.json';manifest=json.loads(path.read_text());manifest['notes'][0]['unknown_row_metadata']={'keep':[1,2]};path.write_text(json.dumps(manifest));self.commit()
        self.note.update(content='new body',updated_at='2026-04-24 18:29:55');self.preview('row_metadata')
        result=json.loads((self.work/'row_metadata/candidate/03 State/manifest.json').read_text())
        self.assertEqual(result['notes'][0]['unknown_row_metadata'],{'keep':[1,2]})

    def test_named_fidelity_ref_create_only_preserves_old_preview_and_backup(self):
        import get_private_preview_retention as r
        from test_get_private_preview_retention import FakeRemote
        self.preview();self.git('remote','add','origin',r.REMOTE_URL);local=r.LocalGit(self.vault);remote=FakeRemote(local,self.head)
        remote.refs[r.BACKUP_REF]=self.head;remote.refs[r.PREVIEW_REF]='e'*40
        before=dict(remote.refs);result=r.retain_private_preview(self.vault,self.work/'p',self.head,remote=remote)
        self.assertTrue(result['preview_retained']);self.assertEqual(len(remote.writes),1)
        self.assertEqual(remote.writes[0][0],'refs/heads/preview/get-fidelity-20261016')
        self.assertEqual(remote.refs[r.PREVIEW_REF],before[r.PREVIEW_REF]);self.assertEqual(remote.refs[r.BACKUP_REF],before[r.BACKUP_REF]);self.assertEqual(remote.refs[r.MAIN_REF],before[r.MAIN_REF])
        r.retain_private_preview(self.vault,self.work/'p',self.head,remote=remote);self.assertEqual(len(remote.writes),1)
    def test_existing_different_fidelity_ref_and_mismatched_backup_never_overwritten(self):
        import get_private_preview_retention as r
        from test_get_private_preview_retention import FakeRemote
        self.preview();self.git('remote','add','origin',r.REMOTE_URL);local=r.LocalGit(self.vault);remote=FakeRemote(local,self.head)
        remote.refs.update({r.BACKUP_REF:self.head,r.PREVIEW_REF:'e'*40,r.FIDELITY_PREVIEW_REF:self.head});before=dict(remote.refs)
        with self.assertRaises(r.RetentionFailure):r.retain_private_preview(self.vault,self.work/'p',self.head,remote=remote)
        self.assertEqual(remote.refs,before);self.assertEqual(remote.writes,[])
        remote.refs.pop(r.FIDELITY_PREVIEW_REF);remote.refs[r.BACKUP_REF]='b'*40;before=dict(remote.refs)
        with self.assertRaisesRegex(r.RetentionFailure,'backup_not_verified'):r.retain_private_preview(self.vault,self.work/'p',self.head,remote=remote)
        self.assertEqual(remote.refs,before);self.assertEqual(remote.writes,[])
    def test_legacy_and_fidelity_plan_targets_are_fixed_and_distinct(self):
        import get_private_preview_retention as r
        self.preview();plan=json.loads((self.work/'p/plan.json').read_text())
        self.assertEqual(r.preview_ref_for_plan(plan),r.FIDELITY_PREVIEW_REF)
        legacy={k:v for k,v in plan.items() if k not in ('state_contract','local_proposal_only','conflicts')}
        self.assertEqual(r.preview_ref_for_plan(legacy),r.PREVIEW_REF)
        with self.assertRaises(r.RetentionFailure):r.preview_ref_for_plan({**plan,'preview_ref':'refs/heads/main'})

    def historical_remote(self, change_get=False):
        import get_private_preview_retention as r
        from test_get_private_preview_retention import FakeRemote
        self.git('remote','add','origin',r.REMOTE_URL);source=self.head
        target=self.root/'changed.md' if change_get else self.vault/'unrelated.txt'
        target.write_text('new unrelated main' if not change_get else 'changed Get');self.commit();main=self.head
        self.git('checkout','--detach',source);self.head=source;local=r.LocalGit(self.vault);remote=FakeRemote(local,source)
        remote.refs.update({r.MAIN_REF:main,r.BACKUP_REF:source,r.PREVIEW_REF:'e'*40})
        remote.source_get_tree=lambda sha:local.run('rev-parse',sha+':'+r.GET).decode().strip()
        remote.check_source_budget=lambda sha:{p:(*row,len(local.blob(row[2]))) for p,row in local.tree(sha).items() if p.startswith(r.GET+'/')}
        return r,local,remote,main
    def test_historical_prepare_only_reads_existing_exact_backup_and_equal_get(self):
        r,local,remote,main=self.historical_remote();before=dict(remote.refs)
        result=r.prepare_backup(self.vault,self.head,remote=remote)
        self.assertTrue(result['main_advanced']);self.assertEqual(result['status'],'backup_existing_verified')
        self.assertEqual(remote.refs,before);self.assertEqual(remote.writes,[])
        remote.refs.pop(r.BACKUP_REF)
        with self.assertRaisesRegex(r.RetentionFailure,'remote_main_changed'):r.prepare_backup(self.vault,self.head,remote=remote)
        remote.refs[r.BACKUP_REF]='a'*40
        with self.assertRaisesRegex(r.RetentionFailure,'remote_main_changed'):r.prepare_backup(self.vault,self.head,remote=remote)
        self.assertEqual(remote.writes,[])
    def test_historical_changed_get_is_blocked_before_prepare_or_retain_write(self):
        r,local,remote,main=self.historical_remote(change_get=True);before=dict(remote.refs)
        with self.assertRaisesRegex(r.RetentionFailure,'review_get_tree_changed'):r.prepare_backup(self.vault,self.head,remote=remote)
        self.preview()
        with self.assertRaisesRegex(r.RetentionFailure,'review_get_tree_changed'):r.retain_private_preview(self.vault,self.work/'p',self.head,remote=remote)
        self.assertEqual(remote.refs,before);self.assertEqual(remote.writes,[])
    def test_historical_backup_disappearance_never_recreates_old_ref(self):
        r,local,remote,main=self.historical_remote();original=remote.ref;reads=[]
        def disappearing(ref):
            if ref==r.BACKUP_REF:
                reads.append(ref)
                if len(reads)==3:remote.refs.pop(ref,None)
            return original(ref)
        remote.ref=disappearing
        with self.assertRaisesRegex(r.RetentionFailure,'backup_not_verified'):r.prepare_backup(self.vault,self.head,remote=remote)
        self.assertNotIn(r.BACKUP_REF,remote.refs);self.assertEqual(remote.writes,[])
    def test_historical_review_ref_races_fail_closed(self):
        r,local,remote,main=self.historical_remote();original=remote.source_get_tree
        def tree(sha):
            result=original(sha);remote.refs[r.MAIN_REF]=self.head;return result
        remote.source_get_tree=tree
        with self.assertRaisesRegex(r.RetentionFailure,'remote_main_changed'):r.review_source(remote,self.head)
        remote.refs[r.MAIN_REF]=main
        def backup_race(sha):
            result=original(sha);remote.refs[r.BACKUP_REF]='b'*40;return result
        remote.source_get_tree=backup_race
        with self.assertRaisesRegex(r.RetentionFailure,'backup_ref_readback_failed'):r.review_source(remote,self.head)
        self.assertEqual(remote.writes,[])
    def test_historical_faithful_retention_keeps_source_parent_and_all_old_refs(self):
        r,local,remote,main=self.historical_remote();self.preview();before=dict(remote.refs)
        result=r.retain_private_preview(self.vault,self.work/'p',self.head,remote=remote)
        self.assertTrue(result['main_advanced']);self.assertFalse(result['apply_ready'])
        self.assertEqual(remote.commit(remote.refs[r.FIDELITY_PREVIEW_REF])['parents'],[self.head])
        self.assertEqual({k:remote.refs[k] for k in before},before)
        r.retain_private_preview(self.vault,self.work/'p',self.head,remote=remote)
        self.assertEqual(len(remote.writes),1);self.assertEqual(remote.writes[0][0],r.FIDELITY_PREVIEW_REF)
    def test_historical_checkout_real_local_fetch_and_second_guard(self):
        r,local,remote,main=self.historical_remote();source=self.head;fixture=self.vault;before=dict(remote.refs)
        class OfflineGit(r.LocalGit):
            def run(self,*args,**kwargs):
                if args and args[0]=='fetch':
                    return super().run('fetch','--depth=1','--no-tags',str(fixture),source)
                if args and args[0]=='push':raise AssertionError('Network writes forbidden')
                return super().run(*args,**kwargs)
        with patch.object(r,'GitHubRemote',return_value=remote),patch.object(r,'LocalGit',OfflineGit):
            result=r.checkout_private(self.work/'checkout',source,token='synthetic')
        checked=r.LocalGit(self.work/'checkout');checked.check_source(source)
        self.assertTrue(result['main_advanced']);self.assertEqual(remote.refs,before);self.assertEqual(remote.writes,[])
        self.assertEqual(checked.tree(source),local.tree(source))
        class RacingGit(OfflineGit):
            def check_source(self,sha):
                super().check_source(sha);remote.refs[r.BACKUP_REF]='a'*40
        with patch.object(r,'GitHubRemote',return_value=remote),patch.object(r,'LocalGit',RacingGit):
            with self.assertRaisesRegex(r.RetentionFailure,'remote_main_changed'):r.checkout_private(self.work/'race',source,token='synthetic')
        self.assertEqual(remote.writes,[])
    def test_remote_get_tree_requires_complete_unique_directory_metadata(self):
        import get_private_preview_retention as r
        remote=object.__new__(r.GitHubRemote);remote.commit=lambda sha:{'tree':'a'*40}
        for value in ({'truncated':True,'tree':[]},{'truncated':False,'tree':[]},
                      {'truncated':False,'tree':[{'path':'Raw','mode':'100644','type':'blob','sha':'b'*40}]},
                      {'truncated':False,'tree':[{'path':'Raw','mode':'040000','type':'tree','sha':'b'*40}]*2}):
            remote.request=lambda *a,**k:value
            with self.assertRaisesRegex(r.RetentionFailure,'invalid_source_tree'):remote.source_get_tree('c'*40)


class LegacyEightReadOnlyTests(unittest.TestCase):
    def fixture(self):
        old={'contract':'get-v7-cloud-mirror-1','pendingById':{str(i):{} for i in range(1,10)}}
        new={'contract':'get-source-raw-local-2','local_proposal_only':True,'pendingById':{'9':{}},'conflictsById':{}}
        docs={};details={}
        for i in range(1,9):
            ident=str(i);path=f'99 录音卡笔记/{ident}.md'
            new['conflictsById'][ident]={'filePath':path,'sourceVersion':raw_source_version('2026-10-15 00:00:00'),'reason':'legacy_baseline_missing','sourceContentHash':None}
            docs[path]=('---\nnote_id: '+json.dumps(ident)+'\nnote_type: recorder_audio\naudio_transcript: ""\n---\nOld hand-edited summary\n').encode()
            details[ident]={'id':ident,'note_type':'recorder_audio','created_at':'2026-10-14 00:00:00','updated_at':'2026-10-15 00:00:00','audio':{'original':''},'content':'AI summary is not original'}
        return old,new,docs,details
    def run_probe(self,old,new,docs,details):
        from source_sync_mirror_replay import diagnose_legacy_eight
        calls=[]
        result=diagnose_legacy_eight(old,new,docs.__getitem__,lambda ident:(calls.append(ident) or deepcopy(details[ident])))
        self.assertEqual(calls,[str(i) for i in range(1,9)])
        return result
    def test_exact_eight_empty_originals_are_not_ready_or_adopted(self):
        result=self.run_probe(*self.fixture());c=result['counts']
        self.assertEqual(c['original_missing_or_empty'],8);self.assertEqual(c['render_unavailable'],8)
        self.assertEqual(c['source_version_same_as_retained'],8);self.assertEqual(result['baseline_adopted'],0)
        self.assertFalse(result['raw_payload_retained']);self.assertFalse(result['source_content_complete_claimed'])
    def test_readiness_and_exact_field_coherence_are_separate(self):
        old,new,docs,details=self.fixture()
        for ident,detail in details.items():detail['audio']['original']='Full original\r\n'
        path=new['conflictsById']['1']['filePath'];docs[path]=docs[path].replace(b'audio_transcript: ""',b'audio_transcript: "Full original\\r\\n"')
        result=self.run_probe(old,new,docs,details);c=result['counts']
        self.assertEqual(c['original_nonempty'],8);self.assertEqual(c['original_equals_legacy_transcript'],1)
        self.assertEqual(c['whole_body_equals_original'],0);self.assertEqual(c['whole_body_equals_detail_text_render'],0)
    def test_substring_or_stripped_body_never_proves_whole_body_equality(self):
        old,new,docs,details=self.fixture()
        for ident,detail in details.items():detail['audio']['original']='Old hand-edited summary'
        self.assertEqual(self.run_probe(old,new,docs,details)['counts']['whole_body_equals_original'],0)
    def test_raw_version_differences_remain_differences(self):
        old,new,docs,details=self.fixture();details['1']['updated_at']='2026-10-15T00:00:00+08:00'
        c=self.run_probe(old,new,docs,details)['counts'];self.assertEqual(c['source_version_changed'],1)
    def test_cohort_and_identity_fail_before_unselected_detail_reads(self):
        from source_sync_mirror_replay import diagnose_legacy_eight
        old,new,docs,details=self.fixture();new['conflictsById'].pop('8')
        with self.assertRaisesRegex(ValueError,'legacy_probe_cohort'):diagnose_legacy_eight(old,new,docs.__getitem__,lambda _:self.fail('detail must not run'))
        old,new,docs,details=self.fixture();details['1']['id']='2'
        with self.assertRaisesRegex(ValueError,'legacy_probe_identity'):self.run_probe(old,new,docs,details)
    def test_safe_report_requires_final_ref_verification_and_consistent_counts(self):
        from source_sync_mirror_replay import safe_legacy_eight_report
        result=self.run_probe(*self.fixture());self.assertEqual(safe_legacy_eight_report(result)['status'],'failed')
        result['refs_unchanged']=True;result['private_body']='must not leak';result['id']='private-id'
        public=safe_legacy_eight_report(result);self.assertEqual(public['status'],'legacy_eight_diagnostic_complete');self.assertNotIn('private_body',public);self.assertNotIn('id',public)
        for key,value in [('raw_payload_retained',True),('baseline_adopted',1),('source_content_complete_claimed',True)]:
            changed=deepcopy(result);changed[key]=value;self.assertEqual(safe_legacy_eight_report(changed)['status'],'failed')
        result['counts']['original_nonempty']=1;self.assertEqual(safe_legacy_eight_report(result)['status'],'failed')
    def test_remote_entry_reads_only_fixed_private_cohort_and_no_writes(self):
        import io,os
        from contextlib import redirect_stdout
        import source_sync_mirror_replay as cli
        import get_private_preview_retention as retention
        import sync_getnote_to_raw as sync
        old,new,docs,details=self.fixture();blobs={};trees={};calls=[]
        for commit in (cli.LEGACY_SOURCE,cli.LEGACY_OLD_PREVIEW,cli.LEGACY_NEW_PREVIEW):trees[commit]={}
        def add(commit,path,data):
            sha=retention.oid(data);blobs[sha]=data;trees[commit][retention.GET+'/'+path]=('100644','blob',sha,len(data))
        add(cli.LEGACY_OLD_PREVIEW,'03 State/cloud-sync-state.json',json.dumps(old).encode())
        add(cli.LEGACY_NEW_PREVIEW,'03 State/cloud-sync-state.json',json.dumps(new).encode())
        for path,data in docs.items():add(cli.LEGACY_SOURCE,path,data);add(cli.LEGACY_NEW_PREVIEW,path,data)
        class ReadOnlyRemote:
            def metadata(self):return {'private':True}
            def ref(self,ref):return {retention.MAIN_REF:'a'*40,retention.BACKUP_REF:cli.LEGACY_SOURCE,retention.PREVIEW_REF:cli.LEGACY_OLD_PREVIEW,retention.FIDELITY_PREVIEW_REF:cli.LEGACY_NEW_PREVIEW}[ref]
            def check_source_budget(self,commit):return trees[commit]
            def blob(self,sha,size):assert len(blobs[sha])==size;return blobs[sha]
        def api(path,params,key,client,**kwargs):
            self.assertEqual(path,sync.DETAIL_PATH);self.assertEqual(params['image_quality'],'original');self.assertIn('request_policy',kwargs)
            calls.append(params['id']);return {'data':{'note':details[params['id']]}}
        output=io.StringIO()
        with patch.object(retention,'GitHubRemote',return_value=ReadOnlyRemote()),patch.object(retention,'review_source') as review,patch.object(sync,'load_credentials',return_value=('synthetic','synthetic')),patch.object(sync,'api_get',side_effect=api),redirect_stdout(output):
            self.assertEqual(cli.legacy_eight_main(),0)
        self.assertEqual(len(calls),8);self.assertEqual(len(set(calls)),8);self.assertEqual(review.call_count,2)
        report=json.loads(output.getvalue());self.assertEqual(report['status'],'legacy_eight_diagnostic_complete');self.assertNotIn('Old hand-edited',output.getvalue())
        # A ref race after read fails closed and cannot be called success.
        remote=ReadOnlyRemote();count=[0];original=remote.ref
        def race(ref):
            count[0]+=1
            return 'b'*40 if count[0]>4 and ref==retention.FIDELITY_PREVIEW_REF else original(ref)
        remote.ref=race;output=io.StringIO()
        with patch.object(retention,'GitHubRemote',return_value=remote),patch.object(retention,'review_source'),patch.object(sync,'load_credentials',return_value=('synthetic','synthetic')),patch.object(sync,'api_get',side_effect=api),redirect_stdout(output):self.assertEqual(cli.legacy_eight_main(),1)
        self.assertEqual(json.loads(output.getvalue())['status'],'failed')



class LegacyLinkReadOnlyTests(unittest.TestCase):
    def fixture(self):
        from get_private_preview_retention import oid
        from source_sync_mirror_replay import LEGACY_SOURCE
        import hashlib
        plan={'source_commit':LEGACY_SOURCE,'changes':[]};state={'contract':'get-source-raw-local-2','local_proposal_only':True,'conflictsById':{}}
        olds={};previews={};details={}
        for i in range(1,8):
            ident=str(i);path=f'Raw/03 Get/04 链接同步摘抄/{ident}.md';excerpt='Legacy private quote '+ident
            old=('---\nnote_id: '+json.dumps(ident)+'\nnote_type: link\nweb_excerpt: '+json.dumps(excerpt)+'\n---\nUser edited full body\n').encode();preview=old.replace(json.dumps(excerpt).encode(),b'""');row={'path':path,'action':'modify'}
            for data,prefix in ((old,'old'),(preview,'new')):row.update({prefix+'_blob':oid(data),prefix+'_sha256':hashlib.sha256(data).hexdigest(),prefix+'_bytes':len(data)})
            plan['changes'].append(row);olds[path]=old;previews[path]=preview
            state['conflictsById'][ident]={'filePath':path.removeprefix('Raw/03 Get/'),'sourceVersion':raw_source_version('2026-10-15 00:00:00'),'reason':'legacy_baseline_missing','sourceContentHash':None}
            details[ident]={'id':ident,'note_type':'link','created_at':'2026-10-14 00:00:00','updated_at':'2026-10-15 00:00:00','content':'Source current text','web_page':{'excerpt':'New AI summary','content':'Current article'},'ref_content':''}
        return plan,state,olds,previews,details
    def run_probe(self,plan,state,olds,previews,details):
        from source_sync_mirror_replay import diagnose_legacy_links
        calls=[];result=diagnose_legacy_links(plan,state,olds.__getitem__,previews.__getitem__,lambda ident:(calls.append(ident) or deepcopy(details[ident])))
        self.assertEqual(calls,[str(i) for i in range(1,8)]);return result
    def test_seven_omitted_excerpt_fields_remain_loss_conflicts(self):
        result=self.run_probe(*self.fixture());c=result['counts']
        self.assertEqual(c['legacy_excerpt_not_found_in_source_text'],7);self.assertEqual(c['current_web_excerpt_nonempty'],7)
        self.assertEqual(c['legacy_excerpt_full_field_match'],0);self.assertEqual(result['baseline_adopted'],0)
        self.assertFalse(result['source_content_complete_claimed']);self.assertFalse(result['raw_payload_retained'])
    def test_full_field_match_and_substring_presence_are_not_conflated(self):
        plan,state,olds,previews,details=self.fixture()
        details['1']['web_page']['excerpt']='Legacy private quote 1'
        details['2']['web_page']['content']='prefix Legacy private quote 2 suffix'
        details['3']['ref_content']='Legacy private quote 3'
        c=self.run_probe(plan,state,olds,previews,details)['counts']
        self.assertEqual(c['legacy_excerpt_full_field_match'],2);self.assertEqual(c['legacy_excerpt_verbatim_in_source_text'],3)
        self.assertEqual(c['legacy_excerpt_not_found_in_source_text'],4);self.assertEqual(c['legacy_excerpt_matches_web_excerpt'],1);self.assertEqual(c['legacy_excerpt_matches_ref_content'],1)
    def test_raw_version_not_normalized_and_no_summary_fallback(self):
        plan,state,olds,previews,details=self.fixture();details['1']['updated_at']='2026-10-15T00:00:00+08:00';details['2'].pop('web_page')
        c=self.run_probe(plan,state,olds,previews,details)['counts'];self.assertEqual(c['source_version_changed'],1);self.assertEqual(c['current_web_excerpt_nonempty'],6)
    def test_all_old_blobs_validate_before_any_get_request(self):
        from source_sync_mirror_replay import diagnose_legacy_links
        plan,state,olds,previews,details=self.fixture();olds[plan['changes'][-1]['path']]+=b'edit'
        with self.assertRaisesRegex(ValueError,'legacy_probe_mirror_changed'):diagnose_legacy_links(plan,state,olds.__getitem__,previews.__getitem__,lambda _:self.fail('no details before cohort validation'))
    def test_cohort_duplication_bad_identity_or_field_shape_fail_closed(self):
        from source_sync_mirror_replay import legacy_link_rows
        plan,state,olds,previews,details=self.fixture();plan['changes'][-1]=plan['changes'][0]
        with self.assertRaisesRegex(ValueError,'legacy_probe_cohort'):legacy_link_rows(plan)
        plan,state,olds,previews,details=self.fixture();details['1']['id']='2'
        with self.assertRaisesRegex(ValueError,'legacy_probe_identity'):self.run_probe(plan,state,olds,previews,details)
    def test_public_report_needs_refs_and_has_no_private_values(self):
        from source_sync_mirror_replay import safe_legacy_links_report
        result=self.run_probe(*self.fixture());self.assertEqual(safe_legacy_links_report(result)['status'],'failed')
        result['refs_unchanged']=True;result['body']='private secret';public=safe_legacy_links_report(result)
        self.assertEqual(public['status'],'legacy_links_diagnostic_complete');self.assertNotIn('body',public)
        result['counts']['legacy_excerpt_verbatim_in_source_text']=1;self.assertEqual(safe_legacy_links_report(result)['status'],'failed')
    def test_main_uses_seven_fixed_private_ids_without_writes(self):
        import io
        from contextlib import redirect_stdout
        import source_sync_mirror_replay as cli
        import get_private_preview_retention as retention
        import sync_getnote_to_raw as sync
        plan,state,olds,previews,details=self.fixture();trees={c:{} for c in (cli.LEGACY_SOURCE,cli.LEGACY_OLD_PREVIEW,cli.LEGACY_NEW_PREVIEW)};blobs={};calls=[]
        def add(commit,path,data):
            sha=retention.oid(data);blobs[sha]=data;trees[commit][path]=('100644','blob',sha,len(data))
        add(cli.LEGACY_OLD_PREVIEW,retention.GET+'/03 State/cloud-preview-plan.json',json.dumps(plan).encode());add(cli.LEGACY_NEW_PREVIEW,retention.GET+'/03 State/cloud-sync-state.json',json.dumps(state).encode())
        for path,data in olds.items():add(cli.LEGACY_SOURCE,path,data);add(cli.LEGACY_NEW_PREVIEW,path,data);add(cli.LEGACY_OLD_PREVIEW,path,previews[path])
        class ReadOnlyRemote:
            def metadata(self):return {'private':True}
            def ref(self,ref):return {retention.MAIN_REF:'a'*40,retention.BACKUP_REF:cli.LEGACY_SOURCE,retention.PREVIEW_REF:cli.LEGACY_OLD_PREVIEW,retention.FIDELITY_PREVIEW_REF:cli.LEGACY_NEW_PREVIEW}[ref]
            def check_source_budget(self,commit):return trees[commit]
            def blob(self,sha,size):assert len(blobs[sha])==size;return blobs[sha]
        def api(path,params,*args,**kwargs):
            self.assertEqual(path,sync.DETAIL_PATH);calls.append(params['id']);return {'data':{'note':details[params['id']]}}
        output=io.StringIO()
        with patch.object(retention,'GitHubRemote',return_value=ReadOnlyRemote()),patch.object(retention,'review_source') as review,patch.object(sync,'load_credentials',return_value=('synthetic','synthetic')),patch.object(sync,'api_get',side_effect=api),patch.object(sys,'argv',['script','--pending-diagnostic']),redirect_stdout(output):self.assertEqual(cli.main(),0)
        self.assertEqual(calls,[str(i) for i in range(1,8)]);self.assertEqual(review.call_count,2)
        report=json.loads(output.getvalue());self.assertEqual(report['status'],'legacy_links_diagnostic_complete');self.assertNotIn('Legacy private quote',output.getvalue())
        self.assertEqual(cli.safe_legacy_links_report(report),report)


if __name__=='__main__':unittest.main()
