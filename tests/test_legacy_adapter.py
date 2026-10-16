from copy import deepcopy
from pathlib import Path
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_legacy_adapter as a
import get_legacy_capture as c
from get_mirror_incremental_replay import freeze_legacy_census,ParityFailure

class LegacyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.repo=self.base/'repo';self.repo.mkdir();self.git('init','-q');self.git('config','user.name','Local Test');self.git('config','user.email','test@example.invalid')
        self.notes={};self.old={};paths={}
        for i in range(1,6):
            ident=str(i);note={'id':ident,'note_type':'plain_text','title':'Title','content':'Exact source '+ident+'\r\nline','created_at':1776968995,'updated_at':1776968995.0,'note_url':'https://www.biji.com/note/'+ident,'unknown':{'keep':True}}
            note.update(deepcopy(getattr(self,'extensions',{})))
            if i==3:note['attachments']=[{'url':'https://example.invalid/asset'}]
            if i==5:note['ref_content']='Unmapped separate text'
            text='---\nnote_id: "'+ident+'"\nnote_type: "plain_text"\ncreated_at: "Old date bytes"\nupdated_at: "Old updated bytes"\nunknown_local: {"keep":true}\n---\nUser prefix\n## 🧠 摘要\n'+note['content']+'\n\n## 📝 正文\n（正文与摘要一致，已省略重复内容）\n\n## 🔗 来源\nUser source text\n\n## 📎 附件\nUser tail\n'
            if i==4:text+='## 🧠 摘要\nDuplicate\n'
            data=text.encode();path=ident+'.md';self.notes[ident]=note;self.old[ident]=data;paths[ident]={'filePath':path,'gitBlob':a.retention.oid(data),'sha256':a.sha(data)}
            target=self.repo/a.retention.GET/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        inherited=self.repo/a.retention.GET/'assets/inherited.bin';inherited.parent.mkdir();inherited.write_bytes(b'Inherited capture asset')
        (self.repo/'unrelated.txt').write_text('Never touch');self.source=self.commit('source');get_tree=self.git('rev-parse',self.source+':'+a.retention.GET).strip().decode()
        self.root=self.base/'root';shutil.copytree(self.repo/a.retention.GET,self.root)
        pending=[str(i) for i in range(90,99)]
        self.summaries=list(deepcopy(self.notes).values())+[{'id':i,'note_type':'recorder_audio','title':'pending','content':'','updated_at':1776968995.0} for i in pending]+[{'id':'77','note_type':'plain_text','title':'New managed elsewhere','content':'new','updated_at':1776968995.0}]
        census,h=freeze_legacy_census(self.summaries,True,paths,pending,self.source,get_tree,'2026-10-16T07:00:00Z')
        ledger=c.initial_ledger(census,h);records={}
        class Source:
            def detail_raw(_,ident):return a.encoded({'data':{'note':self.notes[ident]}})
        def retain(files):
            for path,data in files.items():
                target=self.repo/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            self.commit('capture')
        ledger,result=c.capture_batches(census,ledger,records,Source(),self.old,retain)
        self.head=self.git('rev-parse','HEAD').strip().decode();self.anchor={'capture_commit':self.head,'census_sha256':h,'ledger_sha256':a.sha(a.encoded(ledger))}
        self.reader=a.OfflineGit(self.repo);self.evidence=a.CaptureEvidence(self.reader,self.anchor)
    def git(self,*args,data=None):
        result=subprocess.run(['git','-C',str(self.repo),*args],input=data,capture_output=True,check=True);return result.stdout
    def commit(self,message):
        self.git('add','--all');self.git('commit','-qm',message);return self.git('rev-parse','HEAD').decode().strip()
    def adopt(self):
        current,snap=a.load_current(self.root);state=a.initial_state(self.evidence,current);out=self.base/'adoption'
        plan=a.candidate(self.root,out,state,{},expected_tree_sha=a.root_digest(snap),operation='adoption')
        self.git('checkout','-q','-B','local-proposal',self.source)
        shutil.rmtree(self.repo/a.retention.GET);shutil.copytree(out/'candidate',self.repo/a.retention.GET)
        prior=self.commit('local adoption fixture');self.reader=a.OfflineGit(self.repo)
        return state,out/'candidate',prior,plan
    def update(self,state,root,prior,changes=None,details=None):
        current,_=a.load_current(root);summaries=deepcopy(self.summaries)
        for ident,change in (changes or {}).items():
            for summary in summaries:
                if summary['id']==ident:summary.update(change)
        if details is None:details={i:{**self.notes[i],**v} for i,v in (changes or {}).items()}
        return a.propose_updates(self.evidence,state,a.sha(a.encoded(state)),current,self.reader,prior,summaries,True,details)
    def test_capture_history_and_exact_first_adoption_state_only(self):
        state,root,prior,plan=self.adopt();self.assertEqual(set(state['legacyById']),{'1','2'});self.assertEqual(len(state['conflictsById']),3);self.assertEqual(len(state['pending_ids']),9)
        self.assertEqual([x['path'] for x in plan['changes']],[a.STATE]);self.assertFalse(plan['apply_ready']);self.assertFalse(plan['mirror_adopted'])
        for ident,data in self.old.items():self.assertEqual((root/(ident+'.md')).read_bytes(),data)
    def test_initial_user_edit_excludes_only_that_record_without_rewrite(self):
        path=self.root/'1.md';path.write_bytes(path.read_bytes()+b'User addition')
        current,_=a.load_current(self.root);state=a.initial_state(self.evidence,current)
        self.assertEqual(set(state['legacyById']),{'2'});self.assertEqual(state['conflictsById']['1'],'legacy_current_changed');self.assertTrue(path.read_bytes().endswith(b'User addition'))
    def test_changed_source_candidate_and_committed_repeat_noop(self):
        state,root,prior,_=self.adopt();new,changes,counts=self.update(state,root,prior,{'1':{'content':'New source','updated_at':1776968996}})
        self.assertEqual(counts['source_updated'],1);self.assertEqual(counts['unchanged'],1);self.assertEqual(counts['details_used'],1);self.assertEqual(counts['outside_legacy_cohort'],1)
        out=self.base/'update';snap=a.tree_snapshot(root);plan=a.candidate(root,out,new,changes,expected_tree_sha=a.root_digest(snap),operation='update')
        self.assertEqual({r['path'] for r in plan['changes']},{'1.md',a.STATE});self.assertIn(b'Old date bytes',(out/'candidate/1.md').read_bytes());self.assertIn(b'New source',(out/'candidate/1.md').read_bytes())
        shutil.rmtree(self.repo/a.retention.GET);shutil.copytree(out/'candidate',self.repo/a.retention.GET);next_commit=self.commit('local update fixture');self.reader=a.OfflineGit(self.repo)
        self.notes['1'].update(content='New source',updated_at=1776968996)
        for s in self.summaries:
            if s['id']=='1':s.update(self.notes['1'])
        again,changes,counts=self.update(new,out/'candidate',next_commit)
        self.assertEqual(a.encoded(again),a.encoded(new));self.assertEqual(changes,{});self.assertEqual(counts['details_used'],0)
        noop=a.candidate(out/'candidate',self.base/'noop',again,changes,expected_tree_sha=a.root_digest(a.tree_snapshot(out/'candidate')),operation='update');self.assertEqual(noop['changes'],[])
    def test_three_way_unowned_edits_and_raw_dates_survive(self):
        state,root,prior,_=self.adopt();p=root/'1.md';p.write_bytes(p.read_bytes().replace(b'User prefix',b'Edited prefix').replace(b'Old date bytes',b'User corrected date').replace(b'User tail',b'Edited tail'))
        new,changes,counts=self.update(state,root,prior,{'1':{'content':'Fresh'}})
        self.assertEqual(counts['source_updated'],1)
        for x in (b'Edited prefix',b'User corrected date',b'Edited tail',b'Fresh'):self.assertIn(x,changes['1.md'])
    def test_owned_user_conflict_does_not_advance_or_block_clean_record(self):
        state,root,prior,_=self.adopt();p=root/'1.md';p.write_bytes(p.read_bytes().replace(self.notes['1']['content'].encode(),b'User owned edit'))
        new,changes,counts=self.update(state,root,prior,{'1':{'content':'Source A','updated_at':1776968996},'2':{'content':'Source B','updated_at':1776968996}})
        self.assertEqual(new['legacyById']['1'],state['legacyById']['1']);self.assertEqual(new['conflictsById']['1'],'legacy_owned_text_edited');self.assertEqual(set(changes),{'2.md'});self.assertEqual(counts['conflicted'],1)
    def test_source_missing_empty_and_metadata_change_preserve_success_baseline(self):
        state,root,prior,_=self.adopt()
        for change,reason in [({'content':''},'source_text_missing'),({'title':'Changed'},'source_metadata_changed')]:
            new,changes,counts=self.update(state,root,prior,{'1':change});self.assertEqual(new['legacyById']['1'],state['legacyById']['1']);self.assertEqual(new['conflictsById']['1'],reason);self.assertEqual(changes,{})
    def test_changed_version_requires_exact_detail_cohort_and_token(self):
        state,root,prior,_=self.adopt()
        with self.assertRaisesRegex(ParityFailure,'adapter_detail_set'):self.update(state,root,prior,{'1':{'updated_at':1776968996}},details={})
        new,changes,_=self.update(state,root,prior,{'1':{'updated_at':1776968996}},details={'1':deepcopy(self.notes['1'])})
        self.assertEqual(new['conflictsById']['1'],'source_version_mismatch');self.assertEqual(new['legacyById']['1'],state['legacyById']['1'])
    def test_cached_records_still_validate_baseline_and_current_owned_text(self):
        state,root,prior,_=self.adopt();p=root/'1.md';p.write_bytes(p.read_bytes().replace(self.notes['1']['content'].encode(),b'User owned edit'))
        new,changes,counts=self.update(state,root,prior);self.assertEqual(new['conflictsById']['1'],'legacy_owned_text_edited');self.assertEqual(counts['details_used'],0);self.assertEqual(new['legacyById']['1'],state['legacyById']['1'])
    def test_wrong_external_anchor_and_tampered_state_rejected(self):
        with self.assertRaisesRegex(ParityFailure,'adapter_anchor'):a.CaptureEvidence(self.reader,{**self.anchor,'ledger_sha256':'f'*64})
        state,root,prior,_=self.adopt()
        with self.assertRaisesRegex(ParityFailure,'adapter_state_hash'):a.validate_state(state,'f'*64,self.evidence)
        bad=deepcopy(state);bad['legacyById']['1']['ownership']['filePath']='2.md'
        with self.assertRaisesRegex(ParityFailure,'adapter_ownership_changed'):a.validate_state(bad,a.sha(a.encoded(bad)),self.evidence)
        bad=deepcopy(state);bad['legacyById']['1']['ownership']['owned_spans'][0]['fields'].append('ref_content')
        with self.assertRaisesRegex(ParityFailure,'adapter_ownership_changed'):a.validate_state(bad,a.sha(a.encoded(bad)),self.evidence)
    def test_baseline_oid_must_belong_to_same_path_in_prior_history(self):
        foreign=b'Unrelated object';oid=self.git('hash-object','-w','--stdin',data=foreign).decode().strip()
        with self.assertRaisesRegex(ParityFailure,'baseline_missing'):self.reader.baseline(self.source,'1.md',oid,a.sha(foreign))
    def test_uncommitted_state_cannot_be_presented_as_pinned_state(self):
        state,root,prior,_=self.adopt();bad=deepcopy(state);bad['conflictsById']['1']='legacy_owned_text_edited'
        current,_=a.load_current(root);current[a.STATE]=a.encoded(bad)
        with self.assertRaisesRegex(ParityFailure,'adapter_state_commit'):a.propose_updates(self.evidence,bad,a.sha(a.encoded(bad)),current,self.reader,prior,self.summaries,True,{})
    def test_candidate_readback_failure_rolls_back_without_touching_input(self):
        current,snap=a.load_current(self.root);state=a.initial_state(self.evidence,current);output=self.base/'bad'
        def corrupt(dest):(dest/'1.md').write_bytes(b'Corrupt')
        with self.assertRaisesRegex(ParityFailure,'adapter_candidate_readback'):a.candidate(self.root,output,state,{},expected_tree_sha=a.root_digest(snap),operation='adoption',check=corrupt)
        self.assertFalse(output.exists());self.assertEqual(a.tree_snapshot(self.root),snap)
    def test_candidate_source_race_stops_publication(self):
        current,snap=a.load_current(self.root);state=a.initial_state(self.evidence,current);output=self.base/'bad'
        def race(_):(self.root/'1.md').write_bytes(b'Concurrent user change')
        with self.assertRaisesRegex(ParityFailure,'adapter_source_changed'):a.candidate(self.root,output,state,{},expected_tree_sha=a.root_digest(snap),operation='adoption',check=race)
        self.assertFalse(output.exists());self.assertEqual((self.root/'1.md').read_bytes(),b'Concurrent user change')
    def test_exact_retry_reuses_verified_output_and_corruption_stops(self):
        current,snap=a.load_current(self.root);state=a.initial_state(self.evidence,current);output=self.base/'retry';kwargs=dict(expected_tree_sha=a.root_digest(snap),operation='adoption')
        first=a.candidate(self.root,output,state,{},**kwargs);before=a.tree_snapshot(output/'candidate');again=a.candidate(self.root,output,state,{},**kwargs)
        self.assertEqual(first,again);self.assertEqual(before,a.tree_snapshot(output/'candidate'))
        (output/'candidate/1.md').write_bytes(b'Bad')
        with self.assertRaisesRegex(ParityFailure,'adapter_output_conflict'):a.candidate(self.root,output,state,{},**kwargs)
    def test_symlink_hardlink_and_output_aliases_fail_closed(self):
        (self.root/'link').symlink_to(self.root/'1.md')
        with self.assertRaisesRegex(ParityFailure,'adapter_alias'):a.tree_snapshot(self.root)
        (self.root/'link').unlink();os.link(self.root/'1.md',self.root/'hard')
        with self.assertRaisesRegex(ParityFailure,'adapter_alias'):a.tree_snapshot(self.root)
    def test_bad_current_document_and_identity_isolate_record(self):
        state,root,prior,_=self.adopt();p=root/'1.md'
        for value,reason in [(b'Invalid frontmatter','legacy_structure_ambiguous'),(self.old['1'].replace(b'note_id: "1"',b'note_id: "999"'),'legacy_identity_changed')]:
            p.write_bytes(value)
            new,changes,counts=self.update(state,root,prior,{'2':{'content':'Other clean update'}})
            self.assertEqual(new['legacyById']['1'],state['legacyById']['1']);self.assertEqual(new['conflictsById']['1'],reason);self.assertEqual(set(changes),{'2.md'})
    def test_invalid_detail_is_record_conflict_without_advancing(self):
        state,root,prior,_=self.adopt()
        for detail,reason in [(None,'detail_identity'),({'note_id':'999'},'detail_identity'),({**self.notes['1'],'updated_at':None},'source_version_mismatch')]:
            new,changes,counts=self.update(state,root,prior,{'1':{'updated_at':1776968996}},details={'1':detail})
            self.assertEqual(new['conflictsById']['1'],reason);self.assertEqual(new['legacyById']['1'],state['legacyById']['1']);self.assertEqual(changes,{})
    def test_absent_source_stays_pending_with_original_baseline(self):
        state,root,prior,_=self.adopt();current,_=a.load_current(root);summaries=[s for s in self.summaries if s['id']!='1']
        new,changes,counts=a.propose_updates(self.evidence,state,a.sha(a.encoded(state)),current,self.reader,prior,summaries,True,{})
        self.assertEqual(new['conflictsById']['1'],'source_missing');self.assertEqual(new['legacyById']['1'],state['legacyById']['1']);self.assertEqual(counts['unchanged'],1);self.assertEqual(changes,{})
    def test_malformed_state_containers_fail_with_fixed_errors(self):
        state,root,prior,_=self.adopt()
        for key,value in [('adoption_ids',[{}]),('legacyById',[]),('conflictsById',{'3':{}}),('pending_ids',[])]:
            bad={**state,key:value}
            with self.assertRaisesRegex(ParityFailure,'adapter_state_schema'):a.validate_state(bad,a.sha(a.encoded(bad)),self.evidence)
    def test_duplicate_source_and_incomplete_list_rejected(self):
        for summaries,terminal,error in [(self.summaries+[self.summaries[0]],True,'adapter_duplicate_source'),(self.summaries,False,'adapter_incomplete_list'),([{}],True,'adapter_source_identity')]:
            with self.assertRaisesRegex(ParityFailure,error):a.source_snapshot(summaries,terminal)
    def test_source_version_only_uses_exact_typed_token(self):
        state,root,prior,_=self.adopt();new,changes,counts=self.update(state,root,prior,{'1':{'updated_at':1776968995}})
        self.assertEqual(counts['source_version_only'],1);self.assertEqual(new['legacyById']['1']['ownership']['source_version'],{'type':'integer','value':1776968995});self.assertEqual(changes,{})
    def test_adoption_rejects_existing_state_and_output_alias(self):
        state,root,prior,_=self.adopt()
        with self.assertRaisesRegex(ParityFailure,'adapter_operation'):a.candidate(root,self.base/'again',state,{},expected_tree_sha=a.root_digest(a.tree_snapshot(root)),operation='adoption')
        current,snap=a.load_current(self.root)
        with self.assertRaisesRegex(ParityFailure,'adapter_output'):a.candidate(self.root,self.root/'nested',state,{},expected_tree_sha=a.root_digest(snap),operation='adoption')
    def test_offline_git_disables_lazy_transport(self):
        env=self.reader.local.env;self.assertEqual(env['GIT_NO_LAZY_FETCH'],'1')
        config={env['GIT_CONFIG_KEY_'+str(i)]:env['GIT_CONFIG_VALUE_'+str(i)] for i in range(int(env['GIT_CONFIG_COUNT']))}
        self.assertEqual(config['protocol.allow'],'never')
    def test_corrupt_unchanged_state_receipt_fails_before_any_candidate(self):
        state,root,prior,_=self.adopt();state['legacyById']['1']['ownership']['source_version']={'type':'string','value':'not a version'}
        (root/a.STATE).write_bytes(a.encoded(state));(self.repo/a.retention.GET/a.STATE).write_bytes(a.encoded(state));prior=self.commit('bad local fixture');self.reader=a.OfflineGit(self.repo)
        with self.assertRaises(ParityFailure):self.update(state,root,prior)
    def test_missing_baseline_blob_defers_only_record_and_does_not_mask_corruption(self):
        state,root,prior,_=self.adopt();blob=state['legacyById']['1']['baseline_blob'];obj=self.repo/'.git/objects'/blob[:2]/blob[2:];original=obj.read_bytes();obj.unlink()
        new,changes,counts=self.update(state,root,prior,{'2':{'content':'Still updates'}})
        self.assertEqual(new['conflictsById']['1'],'baseline_missing');self.assertEqual(new['legacyById']['1'],state['legacyById']['1']);self.assertEqual(set(changes),{'2.md'})
        obj.write_bytes(b'Corrupt object')
        with self.assertRaisesRegex(ParityFailure,'adapter_baseline_readback'):self.update(state,root,prior)
        obj.write_bytes(original)
    def test_foreign_output_created_at_publication_is_never_overwritten(self):
        current,snap=a.load_current(self.root);state=a.initial_state(self.evidence,current);output=self.base/'foreign'
        def race(_):output.mkdir()
        with self.assertRaisesRegex(ParityFailure,'adapter_output_conflict'):a.candidate(self.root,output,state,{},expected_tree_sha=a.root_digest(snap),operation='adoption',check=race)
        self.assertTrue(output.is_dir());self.assertEqual(list(output.iterdir()),[])
    def test_git_plan_publication_preserves_concurrent_foreign_directory(self):
        current=a.git_current(self.reader,self.source,self.evidence);state=a.initial_state(self.evidence,current);plan=a.git_candidate(self.reader,self.source,state,{},current,operation='adoption');output=self.base/'foreign-git';original=a.rename_noreplace
        def race(source,target):output.mkdir();return original(source,target)
        with patch.object(a,'rename_noreplace',side_effect=race),self.assertRaisesRegex(ParityFailure,'adapter_output_conflict'):a.save_git_plan(output,plan,state)
        self.assertEqual(list(output.iterdir()),[])
    def test_selected_blob_budget_preflights_before_materializing(self):
        with patch.object(self.reader.local,'blob',side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ParityFailure,'adapter_blob_budget'):self.reader.read(self.source,a.retention.GET+'/1.md',1)
    def test_filesystem_bounds_are_enforced_before_read(self):
        path=self.base/'oversized';path.write_bytes(b'Too large')
        with self.assertRaisesRegex(ParityFailure,'adapter_file_budget'):a.bounded_file(path,2)
        with patch.object(a,'MAX_TREE',1),patch.object(a,'bounded_file',side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ParityFailure,'adapter_tree_budget'):a.tree_snapshot(self.root)
    def test_capture_evidence_preserves_missing_inherited_asset_without_fetch(self):
        path=a.retention.GET+'/assets/inherited.bin';blob=self.git('rev-parse',self.source+':'+path).decode().strip();object_path=self.repo/'.git/objects'/blob[:2]/blob[2:];object_path.unlink()
        reader=a.OfflineGit(self.repo);evidence=a.CaptureEvidence(reader,self.anchor)
        current=a.git_current(reader,self.source,evidence);state=a.initial_state(evidence,current);plan=a.git_candidate(reader,self.source,state,{},current,operation='adoption')
        self.assertEqual(len(state['legacyById']),2);self.assertEqual(reader.tree(plan['candidate_commit'])[path],('100644','blob',blob));self.assertFalse(object_path.exists())
    def test_stale_unchanged_current_note_cannot_adopt_against_another_parent(self):
        current=a.git_current(self.reader,self.source,self.evidence);state=a.initial_state(self.evidence,current)
        path=self.repo/a.retention.GET/'1.md';path.write_bytes(path.read_bytes()+b'New user edit');parent=self.commit('later main fixture');reader=a.OfflineGit(self.repo)
        with self.assertRaisesRegex(ParityFailure,'adapter_source_changed'):a.git_candidate(reader,parent,state,{},current,operation='adoption')
    def test_empty_terminal_list_marks_managed_sources_missing(self):
        state,root,prior,_=self.adopt();current,_=a.load_current(root)
        new,changes,counts=a.propose_updates(self.evidence,state,a.sha(a.encoded(state)),current,self.reader,prior,[],True,{})
        self.assertEqual(counts['conflicted'],2);self.assertEqual(new['legacyById'],state['legacyById']);self.assertEqual(changes,{})
    def test_object_only_adoption_preserves_missing_assets_and_all_refs(self):
        asset=self.repo/a.retention.GET/'assets/binary.bin';asset.parent.mkdir(exist_ok=True);asset.write_bytes(b'Inherited asset bytes')
        parent=self.commit('asset fixture');blob=self.git('rev-parse',parent+':'+a.retention.GET+'/assets/binary.bin').decode().strip()
        (self.repo/'.git/objects'/blob[:2]/blob[2:]).unlink()
        reader=a.OfflineGit(self.repo);current=a.git_current(reader,parent,self.evidence);state=a.initial_state(self.evidence,current)
        before_refs=self.git('show-ref');before_index=(self.repo/'.git/index').read_bytes()
        first=a.git_candidate(reader,parent,state,{},current,operation='adoption');again=a.git_candidate(reader,parent,state,{},current,operation='adoption')
        self.assertEqual(first,again);self.assertEqual([r['path'] for r in first['changes']],[a.STATE]);self.assertEqual(self.git('show-ref'),before_refs);self.assertEqual((self.repo/'.git/index').read_bytes(),before_index)
        self.assertFalse((self.repo/'.git/objects'/blob[:2]/blob[2:]).exists())
        expected=reader.tree(parent).copy();expected[a.retention.GET+'/'+a.STATE]=('100644','blob',a.retention.oid(a.encoded(state)))
        self.assertEqual(reader.tree(first['candidate_commit']),expected)
    def test_object_only_update_and_noop_use_pinned_candidate_without_refs(self):
        current=a.git_current(self.reader,self.source,self.evidence);state=a.initial_state(self.evidence,current)
        initial=a.git_candidate(self.reader,self.source,state,{},current,operation='adoption');parent=initial['candidate_commit'];current=a.git_current(self.reader,parent,self.evidence)
        summaries=deepcopy(self.summaries)
        for summary in summaries:
            if summary['id']=='1':summary.update(content='Updated object text',updated_at=1776968996)
        new,changes,counts=a.propose_updates(self.evidence,state,a.sha(a.encoded(state)),current,self.reader,parent,summaries,True,{'1':{**self.notes['1'],'content':'Updated object text','updated_at':1776968996}})
        result=a.git_candidate(self.reader,parent,new,changes,current,operation='update');self.assertEqual({r['path'] for r in result['changes']},{'1.md',a.STATE})
        current=a.git_current(self.reader,result['candidate_commit'],self.evidence)
        repeated,no_changes,counts=a.propose_updates(self.evidence,new,a.sha(a.encoded(new)),current,self.reader,result['candidate_commit'],summaries,True,{})
        noop=a.git_candidate(self.reader,result['candidate_commit'],repeated,no_changes,current,operation='update');self.assertEqual(noop['changes'],[]);self.assertEqual(noop['candidate_commit'],result['candidate_commit'])
    def test_git_candidate_checks_current_blob_and_symbolic_entries(self):
        current=a.git_current(self.reader,self.source,self.evidence);state=a.initial_state(self.evidence,current)
        initial=a.git_candidate(self.reader,self.source,state,{},current,operation='adoption');parent=initial['candidate_commit'];current=a.git_current(self.reader,parent,self.evidence)
        current['1.md']=b'Stale input'
        with self.assertRaisesRegex(ParityFailure,'adapter_source_changed'):a.git_candidate(self.reader,parent,state,{'1.md':b'Unexpected output'},current,operation='update')
        (self.repo/a.retention.GET/'alias.md').symlink_to('1.md');alias_commit=self.commit('symlink fixture');reader=a.OfflineGit(self.repo)
        with self.assertRaisesRegex(ParityFailure,'adapter_alias'):a.git_candidate(reader,alias_commit,state,{},current,operation='adoption')
    def test_git_plan_exact_retry_and_corruption(self):
        current=a.git_current(self.reader,self.source,self.evidence);state=a.initial_state(self.evidence,current);plan=a.git_candidate(self.reader,self.source,state,{},current,operation='adoption');output=self.base/'git-plan'
        a.save_git_plan(output,plan,state);a.save_git_plan(output,plan,state);(output/'state.json').write_bytes(b'Corrupt')
        with self.assertRaisesRegex(ParityFailure,'adapter_output_conflict'):a.save_git_plan(output,plan,state)
    def test_git_cli_builds_state_only_object_proposal(self):
        args=['tool','--repo',str(self.repo),'--git-base',self.source,'--output',str(self.base/'git-cli'),'--capture-commit',self.head,'--census-sha256',self.anchor['census_sha256'],'--ledger-sha256',self.anchor['ledger_sha256']]
        output=io.StringIO();before=self.git('show-ref')
        with patch.object(sys,'argv',args),contextlib.redirect_stdout(output):status=a.main()
        self.assertEqual(status,0);self.assertEqual(json.loads(output.getvalue())['changed_paths'],1);self.assertEqual(before,self.git('show-ref'));self.assertNotIn(self.source,output.getvalue());self.assertNotIn('1.md',output.getvalue())
    def test_cli_builds_real_candidate_and_aggregate_output_only(self):
        args=['tool','--repo',str(self.repo),'--root',str(self.root),'--output',str(self.base/'cli'),'--capture-commit',self.head,'--census-sha256',self.anchor['census_sha256'],'--ledger-sha256',self.anchor['ledger_sha256'],'--expected-tree-sha256',a.root_digest(a.tree_snapshot(self.root))]
        output=io.StringIO()
        with patch.object(sys,'argv',args),contextlib.redirect_stdout(output):status=a.main()
        self.assertEqual(status,0);report=json.loads(output.getvalue());self.assertEqual(report['managed_proposals'],2);self.assertEqual(report['conflicts'],3);self.assertEqual(report['pending'],9);self.assertEqual(report['changed_paths'],1);self.assertNotIn('Exact source',output.getvalue());self.assertNotIn('1.md',output.getvalue())


class LegacyMetadataTests(LegacyAdapterTests):
    def setUp(self):
        self.extensions={'version':1,'display_title':'Title','timestamps':{'created_at':1776968995,'edited_at':1776968995.0,'record_updated_at':'2026-10-16T07:00:00Z'}}
        super().setUp()
    def updated_metadata(self,**extra):
        return {'version':2,'display_title':'Fresh display','timestamps':{'created_at':1776968995,'edited_at':1776968996.0,'record_updated_at':'2026-10-16T08:00:00Z'},**extra}
    def test_mutable_metadata_and_body_preserve_dates_and_repeat(self):
        state,root,prior,_=self.adopt();change=self.updated_metadata(content='Fresh source',updated_at=1776968996)
        new,changes,counts=self.update(state,root,prior,{'1':change})
        self.assertEqual(counts['source_updated'],1);self.assertEqual(counts['conflicted'],0)
        self.assertEqual(new['legacyById']['1']['ownership']['metadata_sha256'],state['legacyById']['1']['ownership']['metadata_sha256'])
        full={**self.notes['1'],**change};overlay=new['legacyById']['1']['source_overlay'];self.assertEqual(overlay['actual_source_sha256'],a.sha(a.encoded(full)))
        self.assertNotEqual(overlay['actual_source_sha256'],new['legacyById']['1']['ownership']['source_sha256'])
        for token in (b'Old date bytes',b'Old updated bytes',b'unknown_local',b'User prefix',b'User source text',b'User tail'):self.assertIn(token,changes['1.md'])
        current,_=a.load_current(root);plan=a.git_candidate(self.reader,prior,new,changes,current,operation='update')
        next_commit=plan['candidate_commit'];current['1.md']=changes['1.md'];current[a.STATE]=a.encoded(new)
        summaries=deepcopy(self.summaries)
        for summary in summaries:
            if a.note_identity(summary)=='1':summary.update(change)
        fresh=a.OfflineGit(self.repo)
        again,diff,counts=a.propose_updates(self.evidence,new,a.sha(a.encoded(new)),current,fresh,next_commit,summaries,True,{})
        self.assertEqual(a.encoded(again),a.encoded(new));self.assertEqual(diff,{});self.assertEqual(counts['details_used'],0)
    def test_metadata_only_preserves_whole_markdown(self):
        state,root,prior,_=self.adopt();new,changes,counts=self.update(state,root,prior,{'1':self.updated_metadata()})
        self.assertEqual(changes,{});self.assertEqual(counts['source_version_only'],1);self.assertEqual(counts['source_updated'],0)
        self.assertEqual(new['legacyById']['1']['baseline_blob'],state['legacyById']['1']['baseline_blob'])
    def test_unified_manual_metadata_update_single_cas_and_noop(self):
        import test_canonical_manual as manual
        import get_canonical_manual as g
        fixture=manual.CanonicalManualTests(methodName='test_first_preview_import_one_candidate_and_repeat_noop')
        with patch.object(LegacyAdapterTests,'extensions',self.extensions,create=True):fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first,_=fixture.prepare();change=self.updated_metadata(content='Fresh manual source',updated_at=1776968996)
        summaries=deepcopy(fixture.f.f.summaries)+list(fixture.f.notes.values())
        for summary in summaries:
            if a.note_identity(summary)=='1':summary.update(change)
        source=fixture.f.source(summaries=summaries);original=source.detail_raw
        source.detail_raw=lambda ident:a.encoded({'data':{'note':{**fixture.f.f.notes['1'],**change}}}) if ident=='1' else original(ident)
        plan,files=fixture.prepare(first['candidate_commit'],registry=first['registry_sha256'],source=source,first=False)
        self.assertEqual(plan['counts']['legacy_source_updated'],1);self.assertEqual(plan['counts']['legacy_conflicted'],0)
        self.assertEqual(g.public_report(fixture.report(plan,files))['status'],'canonical_candidate_verified')
        class Remote:
            def __init__(self):self.head=first['candidate_commit'];self.calls=[]
            def metadata(self):return {'full_name':g.r.REPOSITORY,'private':True}
            def ref(self,ref):return self.head if ref==g.r.MAIN_REF else g.m.PROTECTED[ref]
            def source_get_tree(self,commit):return fixture.reader.source_get_tree(commit)
            def cas_main(self,old,new):assert self.head==old;self.calls.append((old,new));self.head=new
            def commit(self,commit):return {'tree':fixture.reader.local.run('rev-parse',commit+'^{tree}').decode().strip(),'parents':fixture.reader.local.run('rev-list','--parents','-n','1',commit).decode().split()[1:]}
        remote=Remote()
        result=g.m.publish(fixture.reader,remote,plan,files,a.sha(a.encoded(plan)),lambda commit,values:dict(values),verifier=g.verify_plan,sleep=lambda _:None,writers_guard=lambda:None,checkpoint=lambda _:None)
        self.assertEqual(len(remote.calls),1);self.assertTrue(result['main_updated'])
        repeat,writes=fixture.prepare(plan['candidate_commit'],sha=plan['state_sha256'],registry=plan['registry_sha256'],source=fixture.f.source(summaries=summaries),first=False)
        self.assertEqual(writes,{});self.assertEqual(repeat['counts']['source_details'],0)

    def test_actual_mutable_metadata_references_remain_conflicts(self):
        state,root,prior,_=self.adopt()
        for title in ('https://example.invalid/new-reference','<IMG SRC="asset">','[label](asset)'):
            new,changes,counts=self.update(state,root,prior,{'1':self.updated_metadata(display_title=title,content='Fresh source')})
            self.assertEqual(counts['conflicted'],1);self.assertEqual(changes,{})
            self.assertEqual(new['conflictsById']['1'],'legacy_references_unresolved');self.assertEqual(new['legacyById']['1'],state['legacyById']['1'])
        bad=a.deepcopy(state);note=a.deepcopy(self.notes['1']);note['display_title']='https://example.invalid/cached-reference'
        bad['legacyById']['1']['source_overlay']=a.source_overlay(note,self.evidence.source['1'])
        current,_=a.load_current(root);current[a.STATE]=a.encoded(bad)
        # A rehashed/committed malformed overlay is still subject to the exact
        # actual-source reference gate before any cached fast path can advance.
        plan=a.git_candidate(self.reader,prior,bad,{},a.load_current(root)[0],operation='update')
        new,changes,counts=a.propose_updates(self.evidence,bad,a.sha(a.encoded(bad)),current,self.reader,plan['candidate_commit'],self.summaries,True,{})
        self.assertEqual(new['legacyById']['1'],bad['legacyById']['1']);self.assertEqual(new['conflictsById']['1'],'legacy_references_unresolved');self.assertEqual(changes,{})

    def test_actual_bad_shape_isolates_peer_before_reference_scan(self):
        state,root,prior,_=self.adopt()
        for field in ('audio','web_page'):
            new,changes,counts=self.update(state,root,prior,{'1':self.updated_metadata(**{field:'unsupported'},content='Source A'),'2':self.updated_metadata(content='Source B')})
            self.assertEqual(new['conflictsById']['1'],'source_shape_unsupported');self.assertEqual(new['legacyById']['1'],state['legacyById']['1']);self.assertEqual(set(changes),{'2.md'});self.assertEqual(counts['source_updated'],1)

    def test_overlay_schema_and_actual_hash_checked_on_cached_record(self):
        state,root,prior,_=self.adopt();new,changes,_=self.update(state,root,prior,{'1':self.updated_metadata()})
        for mutation in ('extra','type','hash','null'):
            bad=deepcopy(new);overlay=bad['legacyById']['1']['source_overlay']
            if mutation=='null':bad['legacyById']['1']['source_overlay']=None
            elif mutation=='extra':overlay['fields']['unknown']='x'
            elif mutation=='type':overlay['fields']['version']=True
            else:overlay['actual_source_sha256']='f'*64
            with self.assertRaises(ParityFailure):
                a.validate_state(bad,a.sha(a.encoded(bad)),self.evidence)
                row=bad['legacyById']['1'];core=a.reconstructed_source(self.evidence,'1',row['ownership'],self.old['1']);a.actual_source(row,core,self.evidence.source['1'])
    def test_creation_unknown_missing_empty_type_changes_conflict(self):
        state,root,prior,_=self.adopt()
        variants=[self.updated_metadata(timestamps={'created_at':1776968995.0,'edited_at':1776968996.0,'record_updated_at':'later'}),
                  self.updated_metadata(title='Changed title'),self.updated_metadata(display_title=''),self.updated_metadata(version=True),self.updated_metadata(new_unknown='unmapped')]
        for change in variants:
            new,changes,counts=self.update(state,root,prior,{'1':change})
            self.assertEqual(counts['conflicted'],1);self.assertEqual(changes,{});self.assertEqual(new['legacyById']['1'],state['legacyById']['1'])
        detail=deepcopy(self.notes['1']);detail.pop('version');summary=deepcopy(detail);summary['updated_at']=1776968996;detail['updated_at']=1776968996
        summaries=[summary if x['id']=='1' else x for x in self.summaries];current,_=a.load_current(root)
        new,changes,counts=a.propose_updates(self.evidence,state,a.sha(a.encoded(state)),current,self.reader,prior,summaries,True,{'1':detail})
        self.assertEqual(new['conflictsById']['1'],'source_metadata_changed');self.assertEqual(changes,{})
    def test_conflicting_peer_never_advances_overlay(self):
        state,root,prior,_=self.adopt();p=root/'1.md';p.write_bytes(p.read_bytes().replace(self.notes['1']['content'].encode(),b'User owned edit'))
        new,changes,counts=self.update(state,root,prior,{'1':self.updated_metadata(content='Source A'),'2':self.updated_metadata(content='Source B')})
        self.assertEqual(new['legacyById']['1'],state['legacyById']['1']);self.assertEqual(set(changes),{'2.md'});self.assertEqual(counts['conflicted'],1)

if __name__=='__main__':unittest.main()
