from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_legacy_owned_spans as owned
from get_mirror_incremental_replay import freeze_legacy_census,make_legacy_observation,capture_json_bytes,ParityFailure
from get_private_preview_retention import oid

class OwnedSpanTests(unittest.TestCase):
    def fixture(self,changes=None,extra_header='',body=None,prefix='User prefix\n',source_tail='User source note\n',asset_tail='User tail\n'):
        note={'id':'1','note_type':'plain_text','title':'Title','content':'Exact source\r\nline','created_at':1776968995,'updated_at':1776968995.0,'unknown':{'keep':True}}
        note.update(changes or {})
        body='（正文与摘要一致，已省略重复内容）' if body is None else body
        old=('---\nnote_id: "1"\nnote_type: "'+note['note_type']+'"\ncreated_at: "Legacy bytes"\nupdated_at: "Legacy updated bytes"\nunknown_local: {"retain":true}\n'+extra_header+'---\n'+prefix+'## 🧠 摘要\n'+note['content']+'\n\n## 📝 正文\n'+body+'\n\n## 🔗 来源\n'+source_tail+'\n## 📎 附件\n'+asset_tail).encode()
        paths={'1':{'filePath':'note.md','gitBlob':oid(old),'sha256':owned.digest(old)}}
        census,ch=freeze_legacy_census([note],True,paths,[],'a'*40,'b'*40,'2026-10-16T07:00:00Z')
        record=make_legacy_observation(census,ch,'1',capture_json_bytes({'data':{'note':note}}),old,'2026-10-16T08:00:00Z')
        state=owned.propose_ownership(census,record,old,observation_sha256=owned.digest(capture_json_bytes(record)))
        return note,state,old
    def update(self,state,baseline,current,note):
        return owned.propose_update(state,baseline,current,note,state_sha256=owned.digest(capture_json_bytes(state)))
    def test_unchanged_is_byte_exact_and_no_snapshot_or_adoption(self):
        note,state,old=self.fixture();result=self.update(state,old,old,note)
        self.assertEqual(result['bytes'],old);self.assertEqual(result['classification'],'unchanged')
        self.assertFalse(result['mirror_adopted']);self.assertFalse(result['history_appended']);self.assertTrue(result['blocked_from_apply'])
    def test_changed_source_only_replaces_owned_whole_field(self):
        note,state,old=self.fixture();new={**note,'content':'Fresh exact\nsource','updated_at':1776968996}
        result=self.update(state,old,old,new)
        self.assertEqual(result['bytes'],old.replace(note['content'].encode(),new['content'].encode(),1))
        self.assertEqual(result['classification'],'source_updated')
        for text in (b'Legacy bytes',b'Legacy updated bytes',b'unknown_local',b'User prefix',b'User source note',b'User tail'):self.assertIn(text,result['bytes'])
    def test_three_way_unowned_edits_survive_in_prefix_metadata_source_and_tail(self):
        note,state,old=self.fixture();current=old.replace(b'User prefix',b'My edited prefix').replace(b'Legacy bytes',b'My corrected date').replace(b'User source note',b'New handwritten source note').replace(b'User tail',b'Edited tail')
        result=self.update(state,old,current,{**note,'content':'Source advanced'})
        self.assertEqual(result['bytes'],current.replace(note['content'].encode(),b'Source advanced',1))
        self.assertTrue(result['unowned_local_edits_preserved'])
    def test_owned_local_edit_conflicts_even_when_source_unchanged(self):
        note,state,old=self.fixture();current=old.replace(note['content'].encode(),b'User changed owned field')
        with self.assertRaisesRegex(ParityFailure,'legacy_owned_text_edited'):self.update(state,old,current,note)
    def test_repeated_updates_are_idempotent_without_appended_snapshots(self):
        note,state,old=self.fixture();new={**note,'content':'Second source'}
        first=self.update(state,old,old,new);again=self.update(first['proposed_state'],first['bytes'],first['bytes'],new)
        self.assertEqual(again['bytes'],first['bytes']);self.assertEqual(again['classification'],'unchanged')
        third=self.update(again['proposed_state'],again['bytes'],again['bytes'],{**new,'content':'Third source'})
        self.assertNotIn(b'Second source',third['bytes']);self.assertEqual(third['bytes'].count(b'Third source'),1)
    def test_raw_version_only_update_preserves_all_old_bytes_and_types(self):
        note,state,old=self.fixture();result=self.update(state,old,old,{**note,'updated_at':1776968996})
        self.assertEqual(result['bytes'],old);self.assertEqual(result['classification'],'source_version_only');self.assertEqual(result['proposed_state']['source_version']['type'],'integer')
    def test_partial_source_missing_empty_new_text_and_metadata_changes_conflict(self):
        note,state,old=self.fixture()
        for change in ({'content':''},{'content':None},{'ref_content':'Unmapped source'},{'title':'Changed title'},{'unknown':{'keep':False}},{'created_at':1776968995.0}):
            with self.subTest(change=change),self.assertRaises(ParityFailure):self.update(state,old,old,{**note,**change})
        missing=dict(note);missing.pop('content')
        with self.assertRaises(ParityFailure):self.update(state,old,old,missing)
    def test_alias_divergence_conflicts_and_equal_aliases_update_together(self):
        note,state,old=self.fixture({'ref_content':'Exact source\r\nline'})
        with self.assertRaisesRegex(ParityFailure,'source_alias_diverged'):self.update(state,old,old,{**note,'content':'Only content changed'})
        result=self.update(state,old,old,{**note,'content':'Both advanced','ref_content':'Both advanced'})
        self.assertEqual(result['bytes'].count(b'Both advanced'),1)
    def test_body_and_named_transcript_update_together_without_stale_metadata(self):
        original='Full original transcript'
        note,state,old=self.fixture({'note_type':'recorder_audio','audio':{'original':original}},extra_header='audio_transcript: '+json.dumps(original)+'\n',body=original)
        result=self.update(state,old,old,{**note,'audio':{'original':'New original'}})
        self.assertNotIn(original.encode(),result['bytes']);self.assertEqual(result['bytes'].count(b'New original'),2)
    def test_nonmatching_named_legacy_source_text_cannot_be_silently_kept_current(self):
        with self.assertRaisesRegex(ParityFailure,'legacy_named_text_unmapped'):self.fixture(extra_header='web_excerpt: "Important older excerpt"\n')
    def test_source_text_substrings_never_admit_and_unmapped_field_defers_only_record(self):
        with self.assertRaisesRegex(ParityFailure,'source_text_unmapped'):self.fixture({'web_page':{'content':'Different full article'}})
        note,state,old=self.fixture();self.assertEqual(state['identity'],'1')
    def test_source_and_legacy_asset_references_are_unresolved(self):
        for change in ({'attachments':[{'url':'https://example.invalid/a'}]},{'audio':{'play_url':'https://example.invalid/a'}},{'web_page':{'url':'https://example.invalid/a'}}):
            with self.subTest(change=change),self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):self.fixture(change)
        for header in ('audio_url: "https://example.invalid/a"\n','attachments: ["old.png"]\n'):
            with self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):self.fixture(extra_header=header)
        with self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):self.fixture(asset_tail='![old](asset.png)')
        for tail in ('<IMG SRC="asset.png">','<VIDEO SRC="clip.mp4">','https://example.invalid/plain-link'):
            with self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):self.fixture(asset_tail=tail)
        note,state,old=self.fixture()
        with self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):self.update(state,old,old,{**note,'content':'New source image https://example.invalid/new.png'})
    def test_repeated_markers_and_edited_headers_fail_closed(self):
        note,state,old=self.fixture()
        for current in (old+'## 🧠 摘要\n'.encode(),old.replace('## 🧠 摘要'.encode(),b'Changed heading')):
            with self.assertRaisesRegex(ParityFailure,'legacy_structure_ambiguous'):self.update(state,old,current,note)
    def test_exact_top_level_identity_bound_backlink_is_metadata_not_asset(self):
        note,state,old=self.fixture({'note_url':'https://www.biji.com/note/1'})
        before=deepcopy(note);result=self.update(state,old,old,{**note,'content':'Updated source'})
        self.assertEqual(result['classification'],'source_updated');self.assertEqual(note,before)
        self.assertEqual(state['metadata_sha256'],owned.metadata_hash(note))
        self.assertNotEqual(state['metadata_sha256'],owned.metadata_hash({k:v for k,v in note.items() if k!='note_url'}))
        with self.assertRaisesRegex(ParityFailure,'source_metadata_changed'):
            self.update(state,old,old,{k:v for k,v in note.items() if k!='note_url'})
        again=self.update(result['proposed_state'],result['bytes'],result['bytes'],{**note,'content':'Updated source'})
        self.assertEqual(again['classification'],'unchanged');self.assertEqual(again['bytes'],result['bytes'])
    def test_backlink_uses_canonical_note_identity_not_storage_id(self):
        note,state,old=self.fixture({'note_id':'1','id':987,'note_url':'https://www.biji.com/note/1'})
        self.assertEqual(self.update(state,old,old,note)['classification'],'unchanged')
        with self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):
            self.update(state,old,old,{**note,'note_url':'https://www.biji.com/note/987'})
    def test_backlink_lookalikes_wrong_identity_and_nonstring_fail_closed(self):
        bad=['http://www.biji.com/note/1','https://biji.com/note/1','https://www.biji.com/note/2','https://www.biji.com/note/1/','https://www.biji.com/note/1?x=1','https://www.biji.com/note/1#x','https://www.biji.com/note/%31','https://www.biji.com/note/01','https://www.biji.com@evil.invalid/note/1','https://www.biji.com.evil.invalid/note/1','HTTPS://www.biji.com/note/1','https://www.biji.com:443/note/1',' https://www.biji.com/note/1','https://www.biji.com/note/1\n',False,0,[],{}]
        for value in bad:
            with self.subTest(value=value),self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):
                self.fixture({'note_url':value})
    def test_backlink_exception_does_not_hide_other_source_or_local_references(self):
        canonical='https://www.biji.com/note/1'
        for changes in ({'unknown':{'note_url':canonical}},{'ref_content':canonical},{'attachments':[{'url':canonical}]},{'web_page':{'url':canonical}}):
            with self.subTest(changes=changes),self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):
                self.fixture({'note_url':canonical,**changes})
        for kwargs in ({'source_tail':canonical},{'asset_tail':canonical},{'extra_header':'custom_url: "'+canonical+'"\n'}):
            with self.subTest(kwargs=kwargs),self.assertRaisesRegex(ParityFailure,'legacy_references_unresolved'):
                self.fixture({'note_url':canonical},**kwargs)
    def test_named_transcript_cannot_be_owned_by_unrelated_equal_summary(self):
        with self.assertRaisesRegex(ParityFailure,'legacy_named_text_unmapped'):
            self.fixture(extra_header='audio_transcript: '+json.dumps('Exact source\r\nline')+'\n')
    def test_source_alias_and_header_bytes_do_not_reformat_on_unchanged(self):
        text='中文 original'
        note,state,old=self.fixture({'note_type':'recorder_audio','audio':{'original':text}},extra_header='audio_transcript: '+json.dumps(text,ensure_ascii=True)+'\n',body=text)
        self.assertEqual(self.update(state,old,old,note)['bytes'],old)
    def test_rehashed_malformed_state_is_rejected(self):
        note,state,old=self.fixture()
        for change in ({'extra':'unapproved'},{'history_appended':True},{'owned_spans':[]},{'source_text':{}}):
            with self.subTest(change=change),self.assertRaisesRegex(ParityFailure,'legacy_state_untrusted'):self.update({**state,**change},old,old,note)
    def test_current_new_named_source_fields_are_explicit_conflicts(self):
        note,state,old=self.fixture({'note_type':'recorder_audio','audio':{'original':'Original'}},body='Original')
        for key in ('audio_transcript','web_excerpt'):
            current=old.replace(b'---\n',b'---\n'+key.encode()+b': "Added local text"\n',1)
            with self.assertRaisesRegex(ParityFailure,'legacy_named_text_unmapped'):self.update(state,old,current,{**note,'audio':{'original':'New original'}})
    def test_canonical_note_id_precedes_numeric_storage_id(self):
        note,state,old=self.fixture({'note_id':'1','id':987})
        self.assertEqual(self.update(state,old,old,note)['bytes'],old)
    def test_omitted_duplicate_slot_cannot_leave_stale_current_text(self):
        note,state,old=self.fixture(body='Exact source\r\nline')
        self.assertEqual(len(state['owned_spans']),2)
        bad=deepcopy(state);bad['owned_spans'].pop()
        with self.assertRaisesRegex(ParityFailure,'legacy_state_untrusted'):self.update(bad,old,old,{**note,'content':'Updated'})
    def test_rehashed_path_evidence_version_and_coverage_are_validated(self):
        note,state,old=self.fixture()
        for change in ({'filePath':'../../other.md'},{'capture_evidence':{}},{'source_version':{'type':'fake','value':1776968995}}):
            with self.subTest(change=change),self.assertRaisesRegex(ParityFailure,'legacy_state_untrusted'):self.update({**state,**change},old,old,note)
    def test_source_matching_unowned_text_does_not_acquire_new_ownership(self):
        note,state,old=self.fixture(body='Future text')
        first=self.update(state,old,old,{**note,'content':'Future text'})
        again=self.update(first['proposed_state'],first['bytes'],first['bytes'],{**note,'content':'Future text'})
        self.assertEqual(again['bytes'],first['bytes']);self.assertEqual(len(again['proposed_state']['owned_spans']),1)
        next_result=self.update(again['proposed_state'],again['bytes'],again['bytes'],{**note,'content':'After future'})
        self.assertIn(b'Future text',next_result['bytes']);self.assertEqual(next_result['bytes'].count(b'After future'),1)
    def test_independent_source_fields_can_converge_then_diverge_without_alias_reassignment(self):
        note,state,old=self.fixture({'note_type':'recorder_audio','audio':{'original':'Original'}},body='Original')
        equal={**note,'content':'Equal now','audio':{'original':'Equal now'}}
        first=self.update(state,old,old,equal);again=self.update(first['proposed_state'],first['bytes'],first['bytes'],equal)
        diverged=self.update(again['proposed_state'],again['bytes'],again['bytes'],{**note,'content':'Summary next','audio':{'original':'Original next'}})
        self.assertIn(b'Summary next',diverged['bytes']);self.assertIn(b'Original next',diverged['bytes']);self.assertNotIn(b'Equal now',diverged['bytes'])
    def test_state_hash_and_baseline_identity_are_required(self):
        note,state,old=self.fixture()
        with self.assertRaisesRegex(ParityFailure,'legacy_state_changed'):owned.propose_update(state,old,old,note,state_sha256='f'*64)
        with self.assertRaisesRegex(ParityFailure,'legacy_baseline_changed'):self.update(state,old+b'changed',old,note)
        with self.assertRaisesRegex(ParityFailure,'legacy_identity_changed'):self.update(state,old,old,{**note,'id':'2'})


class OwnershipAggregateTests(unittest.TestCase):
    def test_aggregate_identity_backlink_does_not_mask_real_reference_conflicts(self):
        import get_legacy_capture as capture
        from test_legacy_capture import ReconciliationAggregateTests
        fixture=ReconciliationAggregateTests().fixture
        counts=capture.ownership_proposal_counts(*fixture(note_changes={'note_url':'https://www.biji.com/note/1'}))
        self.assertEqual(counts['owned_span_proposals_ready'],1);self.assertEqual(sum(counts.values()),1)
        counts=capture.ownership_proposal_counts(*fixture(note_changes={'note_url':'https://www.biji.com/note/1','unknown_url':'https://example.invalid/a'}))
        self.assertEqual(counts['owned_span_reference_conflicts'],1);self.assertEqual(sum(counts.values()),1)
    def test_verified_capture_counts_are_private_pure_proposals(self):
        import get_legacy_capture as capture
        from test_legacy_capture import ReconciliationAggregateTests
        args=ReconciliationAggregateTests().fixture();before=deepcopy(args)
        counts=capture.ownership_proposal_counts(*args)
        self.assertEqual(counts['owned_span_proposals_ready'],1);self.assertEqual(sum(counts.values()),1);self.assertEqual(args,before)
    def test_one_conflict_does_not_block_other_records_and_reasons_are_fixed(self):
        import get_legacy_capture as capture
        from test_legacy_capture import ReconciliationAggregateTests
        cases=[({'note_changes':{'web_page':{'content':'Not represented'}}},'owned_span_unmapped_source_text'),({'extra_fields':{'web_excerpt':'Older excerpt'}},'owned_span_unmapped_legacy_text'),({'extra_fields':{'audio_url':'https://example.invalid/a'}},'owned_span_reference_conflicts'),({'duplicate_header':True},'owned_span_structure_conflicts')]
        for kwargs,key in cases:
            with self.subTest(key=key):
                counts=capture.ownership_proposal_counts(*ReconciliationAggregateTests().fixture(**kwargs));self.assertEqual(counts[key],1);self.assertEqual(sum(counts.values()),1)
    def test_owned_proposal_public_counts_are_bounded_and_no_adoption(self):
        import get_legacy_capture as capture
        counts={key:0 for key in capture.OWNERSHIP_KEYS};counts['owned_span_proposals_ready']=1
        value={'status':'retained_capture_verified','verification_only':True,'eligible':1,'captured':1,'remaining':0,'source_missing':0,'capture_batches':1,'raw_bytes':1,'retained_bytes':1,'refs_verified':True,'readback_verified':True,**counts,'private_proposal':'secret'}
        clean=capture.public_report(value);self.assertEqual(clean['owned_span_proposals_ready'],1);self.assertNotIn('private_proposal',clean);self.assertEqual(clean['mirror_adopted'],0);self.assertFalse(clean['apply_ready']);self.assertEqual(capture.public_report(clean),clean)
        for change in ({'owned_span_proposals_ready':2},{'owned_span_reference_conflicts':True}):self.assertEqual(capture.public_report({**value,**change})['status'],'failed')

if __name__=='__main__':unittest.main()
