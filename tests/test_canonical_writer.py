from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_canonical_writer as n
import get_legacy_adapter as a
import get_mirror_incremental_replay as raw
import test_legacy_adapter as fixtures

class CanonicalTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LegacyAdapterTests(methodName='test_capture_history_and_exact_first_adoption_state_only');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.current={i+'.md':v for i,v in self.f.old.items()};self.legacy=a.initial_state(self.f.evidence,self.current);self.legacy_sha=a.sha(a.encoded(self.legacy))
        self.state=n.initial_registry(self.f.evidence,self.legacy,self.legacy_sha,self.current)
    def note(self,ident='200',**changes):
        return {'note_id':ident,'note_type':'plain_text','title':'Fresh note','content':'Complete fresh text\r\nwith spacing','created_at':'2026-10-16 01:02:03','updated_at':'2026-10-16 01:02:04','note_url':'https://www.biji.com/note/'+ident,**changes}
    def plan(self,notes,selected=(),state=None,current=None,details=None,preview=None):
        state=state or self.state;current=current or self.current
        return n.propose_new(self.f.evidence,self.legacy,self.legacy_sha,state,a.sha(a.encoded(state)),current,self.f.summaries+list(notes.values()),True,notes if details is None else details,selected_new=selected,preview=preview,allow_fresh_preview=True)
    def preview(self,note,path=None):
        ident=note['note_id'];path=path or n.CATEGORIES[note['note_type']]+'/preview.md';data=n.render(note,[],Path(path),faithful=True)[0].encode()
        state=raw.empty_faithful_state();state['notesByCanonicalId']['api:'+ident]={'sourceVersion':raw.raw_source_version(note['updated_at']),'filePath':path,'mirrorSha256':a.sha(data),'sourceContentHash':raw._source_content_hash(note),'sourceSummaryHash':raw._source_content_hash(note),'sourceUnknownHash':raw._source_unknown_hash(note),'sourceTextFields':raw.source_text_fields(note),'origin':'faithful-created','history':[]}
        payload=a.encoded(state);values={a.retention.GET+'/'+n.PREVIEW_STATE:payload,a.retention.GET+'/'+path:data}
        class Reader:
            def read(_,commit,path,maximum):
                if commit!='a'*40:raise AssertionError('Unpinned preview')
                result=values[path]
                if len(result)>maximum:raise AssertionError('Unbounded')
                return result
        return n.preview_records(Reader(),'a'*40,a.sha(payload)),values
    def standard_metadata(self):
        return {'children_count':0,'version':0,'entry_type':'ai','display_title':'Alternate complete source title',
          'timestamps':{'created_at':'2026-10-16 01:02:03','edited_at':'2026-10-16 01:02:04','record_updated_at':'2026-10-16 01:02:04'},
          'content_state':{'content':'full','ref_content':'full'},'metadata_state':{'reference':'complete','topics':'complete'},
          'chapter_timeline':{'items':[],'source':'summary_markdown_rules'},'meeting_todos':{'items':[],'source':'summary_markdown_rules'},'reference':None,
          'tags':[{'id':'tag-synthetic','name':'Exact tag','type':'manual'}],'topics':[{'id':17,'name':'Topic'}],
          'attachments':[{'type':'audio','url':'https://example.invalid/a.mp3','title':'Exact recording title','size':12}]}
    def test_real_api_metadata_shape_preserved_and_preview_bytes_unchanged(self):
        note=self.note(**self.standard_metadata());preview,_=self.preview(note)
        state,writes,counts=self.plan({'200':note},['200'],preview=preview)
        self.assertEqual(counts['preview_imported'],1);self.assertEqual(next(iter(writes.values())),preview['200']['bytes'])
        self.assertEqual(state['newById']['200']['source_metadata'],self.standard_metadata())
        repeat,changes,_=self.plan({'200':note},state=state,current={**self.current,**writes},details={})
        self.assertEqual(repeat,state);self.assertEqual(changes,{})
    def test_known_metadata_update_preserved_without_blocking_changed_content(self):
        note=self.note(**self.standard_metadata());state,writes,_=self.plan({'200':note},['200']);current={**self.current,**writes}
        changed=deepcopy(note);changed.update(content='Actual later content',updated_at='2026-10-16 01:02:05',version=1,display_title='Later display title')
        changed['timestamps']['edited_at']='2026-10-16 01:02:05';changed['tags'][0]['id']='later-tag'
        state,changes,counts=self.plan({'200':changed},state=state,current=current)
        self.assertEqual(counts['updated'],1);self.assertEqual(state['newById']['200']['source_metadata'],n.source_metadata(changed));self.assertIn(b'Actual later content',next(iter(changes.values())))
    def test_structured_source_text_and_incomplete_payload_defer_without_adoption(self):
        cases=[('chapter_timeline',{'items':[{'text':'Never drop a chapter'}],'source':'source'}),('meeting_todos',{'items':['Never drop task'],'source':'source'}),('reference',{'text':'Never drop quote'}),('content_state',{'content':'partial','ref_content':'full'}),('metadata_state',{'reference':'complete','topics':'partial'}),('timestamps',{'created_at':None})]
        for key,value in cases:
            with self.subTest(key=key):
                note=self.note(**self.standard_metadata());note[key]=value;state,writes,counts=self.plan({'200':note},['200'])
                self.assertEqual(writes,{});self.assertEqual(counts['conflicted'],1);self.assertNotIn('200',state['newById'])
    def test_timed_recording_metadata_preserved_with_full_original(self):
        note=self.note(note_type='recorder_audio',**self.standard_metadata())
        note['audio']={'original':'Complete original transcript','sentences':[{'end_ms':1000,'start_ms':0,'files':[],'is_me':False,'speaker_id':1,'speaker_name':'Speaker','text':'Exact timed words'}]}
        note['chapter_timeline']={'items':[{'start_ms':0,'title':'Exact chapter'}],'source':'summary_markdown_rules'}
        note['timeline']={'moments':[],'resources':[],'schema_version':2,'version':1};note['attachments'][0]['duration']=1000
        preview,_=self.preview(note);state,writes,counts=self.plan({'200':note},['200'],preview=preview)
        self.assertEqual(counts['preview_imported'],1);self.assertEqual(state['newById']['200']['source_metadata'],n.source_metadata(note));self.assertEqual(next(iter(writes.values())),preview['200']['bytes'])
        note['audio']['sentences'][0]['files']=[{'url':'https://example.invalid/asset'}]
        state,writes,counts=self.plan({'200':note},['200']);self.assertEqual(writes,{});self.assertEqual(state['conflictsById']['200'],'asset_materialization_required')
    def test_existing_metadata_loss_conflicts_without_advancing_state(self):
        note=self.note(**self.standard_metadata());state,writes,_=self.plan({'200':note},['200']);current={**self.current,**writes}
        for field in ('display_title','version','tags','attachments','timestamps'):
            changed=deepcopy(note);changed.pop(field);changed['updated_at']='2026-10-16 01:02:05';changed['content']='Changed content'
            after,updates,_=self.plan({'200':changed},state=state,current=current)
            self.assertEqual(updates,{});self.assertEqual(after['newById']['200'],state['newById']['200']);self.assertEqual(after['conflictsById']['200'],'source_field_missing')
    def test_cached_metadata_collections_are_validated_before_unchanged_fast_path(self):
        note=self.note(**self.standard_metadata());state,writes,_=self.plan({'200':note},['200']);current={**self.current,**writes}
        for field,bad in [('tags','bad'),('topics',[{'name':'valid','id':{}}]),('attachments',[{'type':'audio','url':'https://example.invalid/a','size':False}])]:
            forged=deepcopy(state);forged['newById']['200']['source_metadata'][field]=bad
            with self.assertRaises((ValueError,raw.ParityFailure)):self.plan({'200':note},state=forged,current=current,details={})
    def test_fixed_reason_counts_do_not_publish_unknown_values(self):
        counts=n.reason_counts({'200':'source_metadata_shape','201':'private payload'});self.assertEqual(counts,{'source_metadata_shape':1,'other':1});self.assertTrue(n.valid_reason_counts(counts,2));self.assertFalse(n.valid_reason_counts({'private payload':1},1))
    def test_one_identity_registry_reserves_legacy_conflicts_pending_and_unknown_mirrors(self):
        self.assertEqual(self.state['reserved']['1'],'legacy');self.assertEqual(self.state['reserved']['3'],'blocked');self.assertEqual(self.state['reserved']['90'],'pending')
        note=self.note('300');current={**self.current,'03 纯文本记录/external.md':n.render(note,[],Path('x'),faithful=True)[0].encode()}
        state=n.initial_registry(self.f.evidence,self.legacy,self.legacy_sha,current);self.assertEqual(state['reserved']['300'],'existing_unmanaged')
    def test_new_note_candidate_preserves_typed_dates_text_and_existing_bytes(self):
        note=self.note();state,changes,counts=self.plan({'200':note},['200'])
        self.assertEqual(counts['created'],1);self.assertEqual(len(changes),1);data=next(iter(changes.values()))
        self.assertIn(note['content'].encode(),data);self.assertIn(b'2026-10-16 01:02:03',data)
        self.assertEqual(self.current,{i+'.md':v for i,v in self.f.old.items()});self.assertEqual(state['newById']['200']['created_at'],raw.raw_source_version(note['created_at']))
    def test_unchanged_repeat_produces_identical_state_and_no_snapshots(self):
        note=self.note();state,changes,_=self.plan({'200':note},['200']);current={**self.current,**changes}
        again,updates,counts=self.plan({'200':note},state=state,current=current,details={})
        self.assertEqual(again,state);self.assertEqual(updates,{});self.assertEqual(counts['unchanged'],1);self.assertNotIn('history',state['newById']['200'])
    def test_exact_preview_import_uses_original_bytes_and_immutable_anchor(self):
        note=self.note(note_type='recorder_audio',audio={'original':'Full original transcript','play_url':'https://example.invalid/audio.mp3'},attachments=[{'type':'audio','url':'https://example.invalid/audio.mp3'}]);preview,_=self.preview(note)
        state,changes,counts=self.plan({'200':note},['200'],preview=preview)
        self.assertEqual(counts['preview_imported'],1);self.assertEqual(next(iter(changes.values())),preview['200']['bytes']);self.assertEqual(state['newById']['200']['preview_anchor'],preview['200']['anchor'])
    def test_changed_preview_creates_fresh_absent_note_and_missing_detail_conflicts(self):
        note=self.note();preview,_=self.preview(note);note['content']='Changed after preview'
        state,changes,counts=self.plan({'200':note},['200'],preview=preview);self.assertEqual(counts['created'],1);self.assertIn(b'Changed after preview',next(iter(changes.values())));self.assertNotEqual(next(iter(changes.values())),preview['200']['bytes']);self.assertEqual(state['newById']['200']['preview_anchor'],preview['200']['anchor'])
        repeat,writes,again=self.plan({'200':note},state=state,current={**self.current,**changes},details={});self.assertEqual(writes,{});self.assertEqual(state,repeat)
        state,changes,counts=self.plan({'200':self.note()},['200'],preview=preview,details={});self.assertEqual(changes,{});self.assertEqual(counts['detail_required'],1)
    def test_reserved_identity_cannot_gain_second_writer(self):
        state,changes,counts=self.plan({},['1','3','90']);self.assertEqual(changes,{});self.assertEqual(counts['conflicted'],3);self.assertEqual(set(state['newById']),set())
    def test_new_record_updates_only_hash_bound_files_and_preserves_other_record(self):
        notes={'200':self.note(),'201':self.note('201')};state,changes,_=self.plan(notes,notes);current={**self.current,**changes}
        edited=state['newById']['200']['filePath'];current[edited]+=b'User added content'
        updated={i:{**v,'content':'Updated source '+i,'updated_at':'2026-10-16 01:02:05'} for i,v in notes.items()}
        again,writes,counts=self.plan(updated,state=state,current=current)
        self.assertEqual(again['newById']['200'],state['newById']['200']);self.assertNotIn(edited,writes);self.assertEqual(counts['updated'],1);self.assertEqual(counts['conflicted'],1)
    def test_missing_body_or_created_date_type_change_is_conflict(self):
        note=self.note(created_at=1776968995);state,changes,_=self.plan({'200':note},['200']);current={**self.current,**changes}
        for update in ({'content':''},{'created_at':1776968995.0}):
            updated={**note,**update,'updated_at':'2026-10-16 01:02:05'};again,writes,_=self.plan({'200':updated},state=state,current=current)
            self.assertEqual(writes,{});self.assertEqual(again['newById']['200'],state['newById']['200'])
    def test_unknown_fields_or_unmaterialized_binary_assets_defer_individually(self):
        notes={'200':self.note(extra_text='Never silently discard'),'201':self.note('201',attachments=[{'type':'image','url':'https://example.invalid/image.png'}]),'202':self.note('202')}
        state,changes,counts=self.plan(notes,notes);self.assertEqual(counts['created'],2);self.assertEqual(counts['conflicted'],1);self.assertEqual(set(state['newById']),{'200','202'})
    def test_opaque_roundtrip_collision_and_immutable_changes(self):
        opaque={'opaque_top_level':{'private_key':[None,True,1,1.0,'https://example.invalid/no-fetch']},'extra':False}
        note=self.note(**opaque);state,writes,_=self.plan({'200':note},['200']);current={**self.current,**writes}
        self.assertEqual(state['newById']['200']['source_metadata']['opaque_top_level'],opaque)
        self.assertEqual(n.opaque_counts(state),{'opaque_records':1,'opaque_fields':2})
        self.assertNotIn(b'private_key',next(iter(writes.values())))
        self.assertIn('源扩展字段已原样保存在私有同步记录中'.encode(),next(iter(writes.values())))
        self.assertEqual(n.text_fields(note),n.text_fields(self.note()))
        again,updates,_=self.plan({'200':note},state=state,current=current,details={})
        self.assertEqual(again,state);self.assertEqual(updates,{})
        for change in ({'extra':True},{'extra':0},{'extra':None},{'new_field':1}):
            changed={**note,**change,'updated_at':'2026-10-16 01:02:05'}
            after,updates,_=self.plan({'200':changed},state=state,current=current)
            self.assertEqual(updates,{});self.assertEqual(after['conflictsById']['200'],'source_unknown_changed')
        changed=deepcopy(note);changed.pop('extra');changed['updated_at']='2026-10-16 01:02:05'
        after,updates,_=self.plan({'200':changed},state=state,current=current)
        self.assertEqual(after['conflictsById']['200'],'source_unknown_changed')
    def test_opaque_cache_type_budget_and_known_gate(self):
        note=self.note(extra={'x':1});state,writes,_=self.plan({'200':note},['200']);current={**self.current,**writes}
        for bad in (None,[],{'content':'collision'},{'x':float('nan')},{'x':('tuple',)},{'x':'z'*n.MAX_OPAQUE}):
            forged=deepcopy(state);forged['newById']['200']['source_metadata']['opaque_top_level']=bad
            with self.assertRaises((ValueError,raw.ParityFailure)):self.plan({'200':note},state=forged,current=current,details={})
        for bad in ({'content':False},{'audio':{'unmapped':'text'}},{'content_state':{'content':'partial','ref_content':'full'}}):
            after,updates,counts=self.plan({'200':self.note(extra=1,**bad)},['200'])
            self.assertEqual(updates,{});self.assertEqual(counts['conflicted'],1)
        after,updates,_=self.plan({'200':self.note(extra='x'*n.MAX_OPAQUE)},['200'])
        self.assertEqual(updates,{});self.assertEqual(after['conflictsById']['200'],'source_metadata_shape')
    def test_duplicate_mirror_identity_and_untrusted_state_fail_closed(self):
        current={**self.current,'03 纯文本记录/duplicate.md':self.current['1.md']}
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_duplicate_identity'):n.initial_registry(self.f.evidence,self.legacy,self.legacy_sha,current)
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_state_hash'):n.validate_registry(self.state,'f'*64,self.f.evidence,self.legacy,self.legacy_sha,self.current)
    def test_empty_plain_text_remains_explicit_and_recording_without_original_pending(self):
        notes={'200':self.note(content=''),'201':self.note('201',note_type='recorder_audio',content='Summary only',audio={})}
        state,changes,counts=self.plan(notes,notes);self.assertEqual(counts['created'],1);self.assertIn('（空）'.encode(),next(iter(changes.values())));self.assertEqual(state['conflictsById']['201'],'source_transcript_pending')
    def test_source_disappearance_keeps_prior_record_and_bytes(self):
        note=self.note();state,changes,_=self.plan({'200':note},['200']);again,writes,counts=self.plan({},state=state,current={**self.current,**changes})
        self.assertEqual(writes,{});self.assertEqual(again['newById'],state['newById']);self.assertEqual(again['conflictsById']['200'],'source_missing')

    def adopted(self):
        state,root,parent,_=self.f.adopt();self.legacy=state;self.legacy_sha=a.sha(a.encoded(state))
        return parent
    def transaction(self,parent,notes,selected=(),legacy_sha=None,registry_sha=None,details=None,summaries=None):
        return n.prepare(self.f.reader,parent,self.f.evidence,legacy_sha or self.legacy_sha,registry_sha,self.f.summaries+list(notes.values()) if summaries is None else summaries,True,notes if details is None else details,selected_new=selected)
    def test_real_git_new_repeat_and_update_single_candidate_preserves_refs_assets(self):
        parent=self.adopted();refs=self.f.git('show-ref');index=self.f.git('ls-files','--stage');note=self.note()
        plan,states,counts=self.transaction(parent,{'200':note},['200'])
        self.assertEqual(len(plan['changes']),2);self.assertEqual(counts['new_created'],1)
        self.assertEqual(self.f.git('show-ref'),refs);self.assertEqual(self.f.git('ls-files','--stage'),index)
        before=self.f.reader.tree(parent);after=self.f.reader.tree(plan['candidate_commit'])
        for path,row in before.items():self.assertEqual(after[path],row)
        again,_,counts=self.transaction(plan['candidate_commit'],{'200':note},registry_sha=plan['registry_sha256'],details={})
        self.assertEqual(again['candidate_commit'],plan['candidate_commit']);self.assertEqual(again['changes'],[])
        update={**note,'content':'Actual later source update','updated_at':'2026-10-16 01:02:09'}
        changed,states,counts=self.transaction(plan['candidate_commit'],{'200':update},registry_sha=plan['registry_sha256'])
        self.assertEqual(counts['new_updated'],1);self.assertEqual(len(changed['changes']),2)
        path=states['registry']['newById']['200']['filePath'];self.assertIn(b'Actual later source update',self.f.reader.read(changed['candidate_commit'],a.retention.GET+'/'+path))
        repeat,_,_=self.transaction(changed['candidate_commit'],{'200':update},registry_sha=changed['registry_sha256'],details={});self.assertEqual(repeat['changes'],[])
    def test_real_git_mixed_legacy_and_new_changes_share_one_transaction(self):
        parent=self.adopted();note=self.note();summaries=deepcopy(self.f.summaries)+[note]
        for item in summaries:
            if item.get('id')=='1':item.update(content='Legacy update',updated_at=1776968996)
        details={'1':{**self.f.notes['1'],'content':'Legacy update','updated_at':1776968996},'200':note}
        plan,states,counts=self.transaction(parent,{'200':note},['200'],details=details,summaries=summaries)
        self.assertEqual(counts['legacy_source_updated'],1);self.assertEqual(counts['new_created'],1);self.assertEqual(len(plan['changes']),4)
        data=self.f.reader.read(plan['candidate_commit'],a.retention.GET+'/1.md');self.assertIn(b'Old date bytes',data);self.assertIn(b'Legacy update',data)
        self.assertEqual(states['registry']['reserved']['1'],'legacy')
    def test_legacy_conflict_does_not_change_registry_owner_or_block_new(self):
        parent=self.adopted();summaries=deepcopy(self.f.summaries)+[self.note()]
        for item in summaries:
            if item.get('id')=='1':item['updated_at']=1776968996
        plan,states,counts=self.transaction(parent,{'200':self.note()},['200'],summaries=summaries)
        self.assertEqual(counts['legacy_conflicted'],1);self.assertEqual(counts['new_created'],1);self.assertEqual(states['registry']['reserved']['1'],'legacy')
        self.assertEqual(states['legacy']['legacyById']['1'],self.legacy['legacyById']['1'])
    def test_complete_current_identity_scan_cannot_omit_path_or_use_different_parent(self):
        parent=self.adopted();current=n.git_inventory(self.f.reader,parent);registry=n.initial_registry(self.f.evidence,self.legacy,self.legacy_sha,current)
        incomplete={p:v for p,v in current.items() if p!='3.md'}
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_current_set'):n.git_candidate(self.f.reader,parent,self.legacy,registry,{},incomplete)
        current['3.md']+=b'wrong parent bytes'
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_parent_changed'):n.git_candidate(self.f.reader,parent,self.legacy,registry,{},current)
    def test_transaction_never_bootstraps_missing_formal_legacy_state(self):
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_legacy_state_missing'):self.transaction(self.f.source,{},[])
    def test_uncaptured_registry_hash_and_unexpected_detail_are_rejected(self):
        parent=self.adopted()
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_state_hash'):self.transaction(parent,{},registry_sha='a'*64)
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_details'):self.transaction(parent,{},details={'900':self.note('900')})
    def test_shadowed_nested_transcript_and_attachment_metadata_defer(self):
        for extra in ({'audio':{'original':'Original','transcript':'Nested'},'audio_transcript':'Different top-level'}, {'attachments':[{'type':'link','url':'https://example.invalid','caption':'Not represented'}]}, {'tags':[{'name':'Name','description':'Not represented'}]}):
            state,changes,counts=self.plan({'200':self.note(**extra)},['200']);self.assertEqual(changes,{});self.assertEqual(counts['conflicted'],1)
    def test_missing_new_baseline_isolated_while_clean_new_record_updates(self):
        from unittest.mock import patch
        parent=self.adopted();notes={'200':self.note(),'201':self.note('201')};plan,states,_=self.transaction(parent,notes,notes)
        original=self.f.reader.baseline
        def missing(prior,path,blob,digest):
            if path==states['registry']['newById']['200']['filePath']:raise raw.ParityFailure('baseline_missing')
            return original(prior,path,blob,digest)
        changed={i:{**note,'updated_at':'2026-10-16 01:02:09','content':'New '+i} for i,note in notes.items()}
        with patch.object(self.f.reader,'baseline',side_effect=missing):result,new,counts=self.transaction(plan['candidate_commit'],changed,registry_sha=plan['registry_sha256'])
        self.assertEqual(counts['new_updated'],1);self.assertEqual(new['registry']['conflictsById']['200'],'baseline_missing');self.assertEqual(new['registry']['newById']['200'],states['registry']['newById']['200'])
    def test_summary_text_cannot_disappear_through_partial_nested_detail(self):
        summary=self.note(audio={'original':'Present in list','transcript':'Other text'});detail={**summary,'audio':{'original':'Present in list'}}
        state,changes,counts=self.plan({'200':summary},['200'],details={'200':detail});self.assertEqual(changes,{});self.assertEqual(state['conflictsById']['200'],'source_text_missing')
    def test_string_tag_or_trimmed_name_is_not_silently_dropped(self):
        for tags in (['Keep tag'],):
            state,changes,_=self.plan({'200':self.note(tags=tags)},['200']);self.assertEqual(changes,{});self.assertEqual(state['conflictsById']['200'],'source_unknown_fields')
    def test_whitespace_source_text_disappearance_does_not_advance(self):
        note=self.note(content=' \r\n ');state,changes,_=self.plan({'200':note},['200'])
        again,writes,_=self.plan({'200':{**note,'content':'','updated_at':'2026-10-16 01:02:09'}},state=state,current={**self.current,**changes})
        self.assertEqual(writes,{});self.assertEqual(again['newById'],state['newById'])
    def test_cli_materializes_only_private_plan_and_states_and_retries_exactly(self):
        import contextlib,io
        from unittest.mock import patch
        parent=self.adopted();source=self.f.base/'source.json';out=self.f.base/'canonical-output';source.write_bytes(a.encoded({'summaries':self.f.summaries+[self.note()],'terminal':True,'details':{'200':self.note()},'selected_new':['200']}))
        argv=['canonical','--repo',str(self.f.repo),'--output',str(out),'--source-snapshot',str(source),'--parent',parent,'--legacy-sha256',self.legacy_sha]
        for key,value in self.f.anchor.items():argv+=['--'+key.replace('_','-'),value]
        refs=self.f.git('show-ref')
        for _ in range(2):
            stdout=io.StringIO()
            with patch.object(sys,'argv',argv),contextlib.redirect_stdout(stdout):self.assertEqual(n.main(),0)
            report=json.loads(stdout.getvalue());self.assertEqual(report['new_created'],1);self.assertEqual(report['changed_paths'],2);self.assertNotIn('Complete fresh',stdout.getvalue());self.assertNotIn('Fresh note',stdout.getvalue())
        self.assertEqual({p.name for p in out.iterdir()},{'plan.json','state.json'});self.assertEqual(self.f.git('show-ref'),refs)
    def test_git_candidate_corrupt_new_blob_readback_stops_before_commit(self):
        from unittest.mock import patch
        parent=self.adopted();current=n.git_inventory(self.f.reader,parent);registry=n.initial_registry(self.f.evidence,self.legacy,self.legacy_sha,current)
        refs=self.f.git('show-ref')
        with patch.object(self.f.reader.local,'blob',return_value=b'Corrupt'):
            with self.assertRaisesRegex(raw.ParityFailure,'canonical_readback'):n.git_candidate(self.f.reader,parent,self.legacy,registry,{},current)
        self.assertEqual(self.f.git('show-ref'),refs)
    def test_new_title_text_remains_exact_and_ambiguous_mirror_headers_fail(self):
        note=self.note(title='  Keep\r\nexact title  ');state,changes,_=self.plan({'200':note},['200']);data=next(iter(changes.values()));self.assertEqual(n.json_header(data)['title'],note['title'])
        for header in (b'---\r\n',b'\xef\xbb\xbf---\n'):
            current={**self.current,'unknown.md':header+b'note_id: "200"\n---\n'}
            with self.assertRaises((raw.ParityFailure,ValueError)):n.inventory(current)
    def test_rehashed_registry_cannot_steal_authoritative_legacy_or_pending_identity(self):
        for ident in ('1','3','90'):
            note=self.note(ident);path='03 纯文本记录/stolen.md';data=n.render_exact(note,path);state=deepcopy(self.state);state['reserved'].pop(ident);state['newById'][ident]=n.record(note,note,path,data)
            with self.assertRaisesRegex(raw.ParityFailure,'canonical_owner_overlap'):n.validate_registry(state,a.sha(a.encoded(state)),self.f.evidence,self.legacy,self.legacy_sha,self.current)
    def test_registry_is_bound_to_actual_external_legacy_hash(self):
        state=deepcopy(self.state);state['legacy_state_sha256']='a'*64
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_legacy_binding'):n.validate_registry(state,a.sha(a.encoded(state)),self.f.evidence,self.legacy,self.legacy_sha,self.current)
    def test_title_text_repeated_in_body_does_not_block_exact_generation(self):
        note=self.note(title='Title',content='title: "Title"\nOther source text');state,changes,counts=self.plan({'200':note},['200']);self.assertEqual(counts['created'],1);self.assertIn(note['content'].encode(),next(iter(changes.values())))
    def test_object_only_candidate_does_not_require_inherited_asset_bytes(self):
        parent=self.adopted();path=a.retention.GET+'/assets/inherited.bin';blob=self.f.reader.tree(parent)[path][2]
        # Git may store inherited objects in packs. Construct that layout,
        # then unpack this isolated fixture before removing only the asset.
        self.assertEqual(self.f.git('cat-file','-t',blob).strip(),b'blob')
        self.f.git('repack','-a','-d')
        packs=list((self.f.repo/'.git/objects/pack').glob('*.pack'))
        self.assertTrue(packs)
        packed=[p.read_bytes() for p in packs]
        for p in (self.f.repo/'.git/objects/pack').iterdir():p.unlink()
        for data in packed:self.f.git('unpack-objects','-r',data=data)
        obj=self.f.repo/'.git/objects'/blob[:2]/blob[2:]
        self.assertEqual(self.f.git('cat-file','-t',blob).strip(),b'blob')
        self.assertTrue(obj.exists());obj.unlink()
        self.assertEqual(self.f.git('cat-file','--batch-check',data=(blob+'\n').encode()).decode().strip(),blob+' missing')
        refs=self.f.git('show-ref');plan,states,counts=self.transaction(parent,{'200':self.note()},['200'])
        self.assertEqual(counts['new_created'],1);self.assertEqual(self.f.reader.tree(plan['candidate_commit'])[path],self.f.reader.tree(parent)[path]);self.assertFalse(obj.exists());self.assertEqual(self.f.git('show-ref'),refs)
        retry,_,_=self.transaction(plan['candidate_commit'],{'200':self.note()},registry_sha=plan['registry_sha256'],details={});self.assertEqual(retry['changes'],[]);self.assertFalse(obj.exists())
    def test_coerced_boolean_child_or_metadata_shapes_are_conflicts(self):
        for change in ({'is_child_note':'false'},{'children_ids':[{'text':'Child source data'}]},{'parent_id':{'x':'Y'}},{'audio':{'play_url':['Wrong shape']}},{'audio_duration':True}):
            state,changes,counts=self.plan({'200':self.note(**change)},['200']);self.assertEqual(changes,{});self.assertEqual(counts['conflicted'],1)
    def test_forged_cached_version_or_creation_token_conflicts_without_write(self):
        note=self.note();state,changes,_=self.plan({'200':note},['200']);current={**self.current,**changes}
        for key in ('source_version','created_at'):
            bad=deepcopy(state);bad['newById']['200'][key]=raw.raw_source_version('2026-10-16 01:02:08')
            source={**note,'updated_at':'2026-10-16 01:02:08'} if key=='source_version' else note
            again,writes,counts=self.plan({'200':source},state=bad,current=current,details={});self.assertEqual(writes,{});self.assertEqual(again['conflictsById']['200'],'canonical_baseline_version')
    def test_generated_note_type_change_defers_without_recategorizing(self):
        note=self.note();state,changes,_=self.plan({'200':note},['200']);updated={**note,'note_type':'link','updated_at':'2026-10-16 01:02:08'}
        again,writes,_=self.plan({'200':updated},state=state,current={**self.current,**changes});self.assertEqual(writes,{});self.assertEqual(again['conflictsById']['200'],'source_note_type_changed')
    def test_title_tags_and_topics_preserve_all_exact_human_text(self):
        note=self.note(title='  Exact\r\ntitle ',tags=[{'name':'First\r\nsecond'},{'name':'Keep spaces '},{'name':''}],topics=[{'name':' Topic '}]);state,changes,_=self.plan({'200':note},['200']);header=n.json_header(next(iter(changes.values())))
        self.assertEqual(header['title'],note['title']);self.assertEqual(header['tags'],[x['name'] for x in note['tags']]);self.assertEqual(header['topics'],[x['name'] for x in note['topics']])
    def test_utf8_filename_bound_preserves_full_title_field(self):
        ident='2'*64;note=self.note(ident,title='中'*80);state,changes,counts=self.plan({ident:note},[ident]);path=next(iter(changes));self.assertLessEqual(len(Path(path).name.encode()),255);self.assertEqual(n.json_header(changes[path])['title'],note['title']);self.assertEqual(counts['created'],1)
    def test_existing_uppercase_or_long_markdown_extension_reserves_identity(self):
        for suffix in ('.MD','.Md','.markdown','.MARKDOWN'):
            current={**self.current,'03 纯文本记录/existing'+suffix:n.render_exact(self.note(),'ignored')}
            state=n.initial_registry(self.f.evidence,self.legacy,self.legacy_sha,current)
            _,changes,counts=self.plan({'200':self.note()},['200'],state=state,current=current);self.assertEqual(changes,{});self.assertEqual(counts['conflicted'],1)
    def test_git_scan_reads_uppercase_markdown_and_blocks_duplicate_creation(self):
        parent=self.adopted();target=self.f.repo/a.retention.GET/'existing.MD';target.write_bytes(n.render_exact(self.note(),'ignored'));parent=self.f.commit('existing uppercase note');self.f.reader=a.OfflineGit(self.f.repo)
        plan,states,counts=self.transaction(parent,{'200':self.note()},['200']);self.assertEqual(counts['new_created'],0);self.assertEqual(counts['new_conflicted'],1);self.assertEqual([x['path'] for x in plan['changes']],[n.STATE])
    def test_owner_budget_conflicts_without_emitting_unresumable_state(self):
        from unittest.mock import patch
        with patch.object(n,'MAX_RECORDS',len(self.state['reserved'])):
            state,changes,counts=self.plan({'200':self.note()},['200']);self.assertEqual(changes,{});self.assertEqual(counts['conflicted'],1);self.assertEqual(state['conflictsById']['200'],'canonical_budget')
            n.validate_registry(state,a.sha(a.encoded(state)),self.f.evidence,self.legacy,self.legacy_sha,self.current)

    def test_opaque_existing_id_is_reserved_verbatim_never_numeric_selected_or_owned(self):
        ident='aBcD'*8;current={**self.current,'opaque.MD':('---\nnote_id: "'+ident+'"\n---\nOpaque historical text').encode()}
        state=n.initial_registry(self.f.evidence,self.legacy,self.legacy_sha,current)
        self.assertEqual(state['reserved'][ident],'existing_unmanaged');self.assertNotIn(ident.lower(),state['reserved'])
        n.validate_registry(state,a.sha(a.encoded(state)),self.f.evidence,self.legacy,self.legacy_sha,current)
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_identity'):self.plan({},[ident],state=state,current=current)
        bad=deepcopy(state);bad['reserved'].pop(ident);bad['newById'][ident]={}
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_identity'):n.validate_registry(bad,a.sha(a.encoded(bad)),self.f.evidence,self.legacy,self.legacy_sha,current)
        duplicate={**current,'duplicate.md':current['opaque.MD']}
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_duplicate_identity'):n.inventory(duplicate)
    def test_full_historical_opaque_inventory_has_separate_bounded_capacity(self):
        current={str(i)+'.md':('---\nnote_id: "'+str(i)+'"\n---\n').encode() for i in range(1942)}
        current.update({'opaque'+str(i)+'.md':('---\nnote_id: "'+format(0xa0000000000000000000000000000000+i,'032x')+'"\n---\n').encode() for i in range(970)})
        self.assertEqual(len(n.inventory(current)),2912)
        self.assertFalse(n.owner_budget(str(i) for i in range(n.MAX_RECORDS+1)))
        self.assertFalse(n.owner_budget(format(0xa0000000000000000000000000000000+i,'032x') for i in range(n.MAX_EXISTING_OWNERS+1)))
        with self.assertRaisesRegex(raw.ParityFailure,'canonical_identity'):n.inventory({'bad.md':b'---\nnote_id: "opaque-invalid"\n---\n'})
    def test_git_candidate_preserves_opaque_existing_owner_bytes_and_state_on_repeat(self):
        parent=self.adopted();ident='ABCd'*8;path=self.f.repo/a.retention.GET/'opaque.MD';data=('---\nnote_id: "'+ident+'"\n---\nExact historical text').encode();path.write_bytes(data)
        parent=self.f.commit('opaque existing owner');self.f.reader=a.OfflineGit(self.f.repo)
        plan,states,counts=self.transaction(parent,{'200':self.note()},['200'])
        self.assertEqual(counts['new_created'],1);self.assertEqual(states['registry']['reserved'][ident],'existing_unmanaged')
        self.assertEqual(self.f.reader.read(plan['candidate_commit'],a.retention.GET+'/opaque.MD'),data)
        again,_,_=self.transaction(plan['candidate_commit'],{'200':self.note()},registry_sha=plan['registry_sha256'],details={});self.assertEqual(again['changes'],[])
