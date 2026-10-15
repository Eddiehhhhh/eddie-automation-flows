import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from get_v7_compat import *

class V7(unittest.TestCase):
    def setUp(self):
        self.iso='2026-10-14T19:10:33+08:00';self.ms=timestamp_ms(self.iso)
        self.summary={'id':'11','updated_at':self.iso}
        self.state={'notesByCanonicalId':{'api:11':{'updatedAt':self.ms,'filePath':'old/location.md','deleted':True,'fingerprint':'old'}}}
        self.mirror={'source_id':'11','path':'Raw/03 Get/03 纯文本记录/existing.md','exists':True,'updated_at':self.iso}
    def run_case(self):return recover_unchanged(self.summary,self.state,[self.mirror],1234)
    def test_matches_original_v7_skip_and_rebind(self):
        result=self.run_case();self.assertEqual(result['decision'],'skip')
        entry=result['state']['notesByCanonicalId']['api:11']
        self.assertEqual(entry['filePath'],self.mirror['path']);self.assertFalse(entry['deleted'])
        self.assertEqual(entry['lastSyncedAt'],1234);self.assertEqual(entry['fingerprint'],'old')
        self.assertTrue(self.state['notesByCanonicalId']['api:11']['deleted'])
    def test_non_deleted_state_also_skips_v7(self):
        self.state['notesByCanonicalId']['api:11']['deleted']=False
        self.assertEqual(self.run_case()['decision'],'skip')
    def test_strict_less_than_one_second(self):
        self.state['notesByCanonicalId']['api:11']['updatedAt']=self.ms+999
        self.assertEqual(self.run_case()['decision'],'skip')
        self.state['notesByCanonicalId']['api:11']['updatedAt']=self.ms+1000
        self.assertEqual(self.run_case()['decision'],'detail')
    def test_state_missing_requires_detail_not_new_cache(self):
        self.state={};self.assertEqual(self.run_case()['reason'],'missing_state')
    def test_missing_actual_body_requires_detail(self):
        self.mirror['exists']=False;self.assertEqual(self.run_case()['decision'],'detail')
    def test_id_mismatch_requires_detail(self):
        self.mirror['source_id']='12';self.assertEqual(self.run_case()['decision'],'detail')
    def test_unknown_version_never_skips(self):
        for value in ('bad','2026-10-15T10:00:00','',None):
            self.summary['updated_at']=value;self.assertEqual(self.run_case()['decision'],'detail')
    def test_duplicate_identity_explicit_migration_guard(self):
        other=dict(self.mirror,path='Raw/03 Get/another.md')
        self.assertEqual(recover_unchanged(self.summary,self.state,[self.mirror,other],0)['decision'],'conflict')
    def test_v7_does_not_claim_body_or_pending_check(self):
        self.mirror.update(updated_at='different',body='user edited or pending transcript')
        self.summary['note_type']='recorder_audio'
        self.assertEqual(self.run_case()['decision'],'skip')
    def test_post_detail_adoption_can_keep_existing_file_without_state(self):
        note={'sourceIds':{'api':'11'},'updatedAt':self.ms}
        self.assertTrue(local_mirror_matches_note(self.mirror,note))
    def test_post_detail_trusted_tombstone_timestamp(self):
        note={'sourceIds':{'api':'11'},'updatedAt':self.ms}
        self.mirror['updated_at']='bad'
        self.assertFalse(local_mirror_matches_note(self.mirror,note))
        self.assertTrue(local_mirror_matches_note(self.mirror,note,self.ms))
        self.mirror['source_id']='wrong'
        self.assertFalse(local_mirror_matches_note(self.mirror,note,self.ms))
    def test_inventory_counts_separate_cached_and_fresh(self):
        counts,decisions,state=classify_list([self.summary,{'id':'12','updated_at':self.iso}],self.state,[self.mirror],0)
        self.assertEqual(counts,{'enumerated':2,'cached_skipped':1,'needs_detail':1,'conflicts':0})
    def test_changed_title_keeps_original_path_if_same_source_version(self):
        self.summary['title']='new title'
        self.assertEqual(self.run_case()['path'],self.mirror['path'])

if __name__=='__main__':unittest.main()
