import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import tempfile
import unittest
from copy import deepcopy
import get_mirror_incremental_replay as r

class ReplayTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.note=self.root/'old title.md'
  self.note.write_text('---\nnote_id: "123"\nupdated_at: "2026-10-12T00:00:00Z"\n---\nUser edited text ![[asset.png]]\n')
  (self.root/'asset.png').write_bytes(b'old-small-image')
  (self.root/'non-get.txt').write_text('untouched')
  self.summary={'id':'123','updated_at':'2026-10-12T00:00:00Z'}
  self.list=lambda:([deepcopy(self.summary)],True)
  self.calls=[]
 def detail(self,ident):
  self.calls.append(ident);return deepcopy(self.summary)
 def test_legacy_naive_cold_adopts_aware_mirror_then_zero_details(self):
  self.summary['updated_at']='2026-10-12 08:00:00';before=r.snapshot(self.root)
  result=r.replay(self.root,self.list,self.detail)
  self.assertEqual(result['first']['adopted_after_detail'],1)
  self.assertEqual(result['first']['list_time_naive'],1)
  self.assertEqual(result['second']['cached_skipped'],1)
  self.assertEqual(result['second']['fresh_details'],0)
  self.assertEqual(r.snapshot(self.root),before)
 def test_legacy_naive_wrong_detail_version_still_fails(self):
  self.summary['updated_at']='2026-10-12 08:00:00';before=r.snapshot(self.root)
  with self.assertRaisesRegex(r.ParityFailure,'source_changed_during_pass'):
   r.replay(self.root,self.list,lambda _:dict(self.summary,updated_at='2026-10-12 08:00:02'))
  self.assertEqual(r.snapshot(self.root),before)
 def test_numeric_list_and_detail_adopt_then_skip_aware_mirror(self):
  self.summary['updated_at']=1791763200
  result=r.replay(self.root,self.list,self.detail)
  self.assertEqual(result['first']['adopted_after_detail'],1)
  self.assertEqual(result['second']['fresh_details'],0)
 def test_format_distribution_survives_list_version_failure(self):
  from source_sync_mirror_replay import safe_report
  values=['2026-04-23T18:29:55',1791763200,'2026-10-12T00:00:00Z',None,True,'bad']
  summaries=[{'id':str(i+1),'updated_at':v} for i,v in enumerate(values)]
  progress=[]
  with self.assertRaisesRegex(r.ParityFailure,'list_version_timezone_missing'):
   r.replay(self.root,lambda:(summaries,True),self.detail,progress=progress)
  public=safe_report({'status':'failed','error':'list_version_timezone_missing','first':progress[0]})
  for key in ('epoch','aware','naive','missing','type','invalid'):self.assertEqual(public['first']['list_time_'+key],1)
  self.assertEqual(self.calls,[])
  self.assertEqual(public['error'],'list_version_timezone_missing')
 def test_cold_adopt_then_skip_preserves_every_byte_path_mtime(self):
  before=r.snapshot(self.root);result=r.replay(self.root,self.list,self.detail)
  self.assertEqual(result['first']['fresh_details'],1)
  self.assertEqual(result['first']['adopted_after_detail'],1)
  self.assertEqual(result['second']['cached_skipped'],1)
  self.assertEqual(result['second']['fresh_details'],0)
  self.assertEqual(self.calls,['123']);self.assertEqual(r.snapshot(self.root),before)
  self.assertTrue(result['state_lastSyncedAt_changed'])
 def test_changed_requires_original_renderer_preserves_old_data(self):
  before=r.snapshot(self.root);self.summary['updated_at']='2026-10-13T00:00:00Z'
  with self.assertRaisesRegex(r.ParityFailure,'original_renderer_required'):r.replay(self.root,self.list,self.detail)
  self.assertEqual(r.snapshot(self.root),before)
 def test_new_requires_original_renderer(self):
  self.summary['id']='456'
  with self.assertRaisesRegex(r.ParityFailure,'original_renderer_required'):r.replay(self.root,self.list,self.detail)
 def test_duplicate_real_mirror_fails_before_api(self):
  (self.root/'duplicate.md').write_bytes(self.note.read_bytes())
  with self.assertRaisesRegex(r.ParityFailure,'duplicate_mirror_identity'):r.replay(self.root,self.list,self.detail)
  self.assertEqual(self.calls,[])
 def test_incomplete_pagination(self):
  with self.assertRaisesRegex(r.ParityFailure,'incomplete_list'):r.replay(self.root,lambda:([self.summary],False),self.detail)
 def test_duplicate_list(self):
  with self.assertRaisesRegex(r.ParityFailure,'invalid_list_identity_version'):r.replay(self.root,lambda:([self.summary,self.summary],True),self.detail)
 def test_detail_wrong_id(self):
  with self.assertRaisesRegex(r.ParityFailure,'detail_identity'):r.replay(self.root,self.list,lambda _:dict(self.summary,id='bad'))
 def test_detail_version_changes(self):
  with self.assertRaisesRegex(r.ParityFailure,'source_changed_during_pass'):r.replay(self.root,self.list,lambda _:dict(self.summary,updated_at='2026-10-13T00:00:00Z'))
 def test_detail_failure_preserves_old(self):
  before=r.snapshot(self.root)
  def fail(_):raise RuntimeError('quota')
  with self.assertRaises(RuntimeError):r.replay(self.root,self.list,fail)
  self.assertEqual(r.snapshot(self.root),before)
 def test_source_set_change_between_passes(self):
  calls=[]
  def listing():
   calls.append(1);return ([self.summary] if len(calls)==1 else [],True)
  with self.assertRaisesRegex(r.ParityFailure,'source_changed_between_passes'):r.replay(self.root,listing,self.detail)
 def test_manifest_identity_conflict(self):
  import json
  path=self.root/'03 State';path.mkdir()
  (path/'manifest.json').write_text(json.dumps({'notes':[{'path':'old title.md','note_id':'bad'}]}))
  with self.assertRaisesRegex(r.ParityFailure,'manifest_identity_conflict'):r.replay(self.root,self.list,self.detail)
 def test_manifest_path_escape(self):
  import json
  path=self.root/'03 State';path.mkdir()
  (path/'manifest.json').write_text(json.dumps({'notes':[{'path':'../escape'}]}))
  with self.assertRaisesRegex(r.ParityFailure,'manifest_path_escape'):r.replay(self.root,self.list,self.detail)
 def test_conflicting_frontmatter_identities(self):
  self.note.write_text(self.note.read_text().replace('note_id: "123"','note_id: "123"\ncanonicalId: api:456'))
  with self.assertRaisesRegex(r.ParityFailure,'conflicting_identity_fields'):r.replay(self.root,self.list,self.detail)
 def test_public_output_contains_only_counts_and_flags(self):
  import json
  result=json.dumps(r.replay(self.root,self.list,self.detail))
  for private in ['old title','User edited','123','asset.png']:self.assertNotIn(private,result)

if __name__=='__main__':unittest.main()
