import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
# Existing reviewed asset implementation used only with synthetic fetch.
import tempfile
import unittest
import json
from copy import deepcopy
from get_mirror_render import CompatibleWriter,render,destination
from get_mirror_incremental_replay import replay,snapshot,ParityFailure
from source_sync_assets import AssetStore
from source_sync_mirror_replay import safe_report

class RenderTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name)/'get';self.root.mkdir()
  self.note={'id':'123','note_type':'plain_text','title':'New title','content':'new content',
    'created_at':'2026-10-12T00:00:00Z','updated_at':'2026-10-13T00:00:00Z'}
  self.calls=[]
 def factory(self,root):
  def fetch(url,max_bytes):self.calls.append(url);return b'new-image','image/png'
  return CompatibleWriter(root,AssetStore(root,fetch=fetch))
 def run_replay(self):return replay(self.root,lambda:([deepcopy(self.note)],True),lambda _:deepcopy(self.note),self.factory)
 def test_created_naive_and_unknown_text_remain_original_metadata(self):
  for value in ('2026-04-23 18:29:55','unknown original value'):
   self.note['created_at']=value
   content,fields=render(self.note,[],Path('x.md'))
   self.assertEqual(fields['created_at'],value)
 def test_updated_naive_still_fails_before_renderer(self):
  self.note['updated_at']='2026-04-23 18:29:55';before=snapshot(self.root)
  with self.assertRaisesRegex(ParityFailure,'list_version_timezone_missing'):self.run_replay()
  self.assertEqual(snapshot(self.root),before);self.assertEqual(self.calls,[])
 def test_numeric_new_with_existing_iso_manifest_two_passes(self):
  path=self.root/'legacy.md';path.write_text('old legacy')
  state=self.root/'03 State';state.mkdir()
  (state/'manifest.json').write_text(json.dumps({'notes':[{'note_id':'legacy-x','path':'legacy.md','title':'old','category':'','note_type':'unknown','updated_at':'2026-10-01T00:00:00Z','created_at':'2026-10-01T00:00:00Z'}]}))
  self.note['updated_at']=1791849600000;self.note['created_at']='1791763200'
  result=self.run_replay()
  self.assertEqual(result['second']['fresh_details'],0)
  rows=json.loads((state/'manifest.json').read_text())['notes']
  self.assertTrue(all(isinstance(x['updated_at'],str) for x in rows))
  content=(self.root/'03 纯文本记录'/'New title.md').read_text()
  self.assertIn('2026-10-13T08:00:00.000+08:00',content)
  self.assertEqual(path.read_text(),'old legacy')
 def test_numeric_changed_keeps_old_path_two_passes(self):
  path=self.root/'old.md';path.write_text('---\nnote_id: "123"\nupdated_at: "2026-10-12T00:00:00Z"\n---\nold body\n')
  self.note['updated_at']=1791849600
  result=self.run_replay()
  self.assertEqual(result['first']['written_or_changed'],1)
  self.assertEqual(result['second']['fresh_details'],0)
  self.assertIn('new content',path.read_text())
 def test_new_note_second_skips_complete_manifest(self):
  result=self.run_replay()
  self.assertEqual(result['first']['written_or_changed'],1);self.assertEqual(result['second']['cached_skipped'],1)
  manifest=json.loads((self.root/'03 State/manifest.json').read_text())
  self.assertEqual(manifest['count'],1);self.assertEqual(len(manifest['notes']),1)
 def test_changed_keeps_original_path(self):
  path=self.root/'old category'/'old title.md';path.parent.mkdir();path.write_text('---\nnote_id: "123"\nupdated_at: "2026-10-12T00:00:00Z"\n---\nold body\n')
  self.run_replay();self.assertIn('new content',path.read_text());self.assertEqual(len(list(self.root.rglob('*.md'))),2)
 def test_matching_user_body_and_asset_never_downloaded(self):
  path=self.root/'old.md';path.write_text('---\nnote_id: "123"\nupdated_at: "2026-10-13T00:00:00Z"\n---\nhand edited ![[old.png]]\n')
  (self.root/'old.png').write_bytes(b'old');before=snapshot(self.root)
  self.note['attachments']=[{'type':'image','url':'https://example.test/image.png'}]
  result=self.run_replay();self.assertEqual(self.calls,[]);self.assertEqual(snapshot(self.root),before)
 def test_changed_image_url_wins_original_url_and_once(self):
  self.note['attachments']=[{'type':'image','url':'https://example.test/small.png','original_url':'https://example.test/huge.png'}]
  self.run_replay();self.assertEqual(self.calls,['https://example.test/small.png'])
 def test_audio_stays_remote(self):
  self.note['attachments']=[{'type':'audio','url':'https://example.test/a.mp3'}]
  self.run_replay();self.assertEqual(self.calls,[])
 def test_transcript_pending_preserves_old(self):
  self.note['note_type']='recorder_audio';before=snapshot(self.root)
  with self.assertRaisesRegex(ValueError,'transcript_pending'):self.run_replay()
  self.assertEqual(snapshot(self.root),before)
 def test_original_and_summary_distinct(self):
  self.note['note_type']='recorder_audio';self.note['audio']={'original':'actual original'}
  content,fields=render(self.note,[],Path('x.md'))
  self.assertIn('## AI 总结\n\nnew content',content);self.assertIn('## 原文\n\nactual original',content)
  self.assertEqual(len(fields),20)
 def test_partial_assets_fail_keeps_whole_seed(self):
  self.note['attachments']=[{'type':'image','url':'https://example.test/1.png'},{'type':'image','url':'https://example.test/2.png'}]
  before=snapshot(self.root);calls=[]
  def factory(root):
   def fetch(url,max_bytes):
    calls.append(url)
    if len(calls)>1:raise ValueError('quota')
    return b'first','image/png'
   return CompatibleWriter(root,AssetStore(root,fetch=fetch))
  with self.assertRaisesRegex(ValueError,'quota'):replay(self.root,lambda:([self.note],True),lambda _:self.note,factory)
  self.assertEqual(snapshot(self.root),before)
 def test_existing_manifest_nonapi_rows_kept(self):
  path=self.root/'legacy.md';path.write_text('old legacy')
  state=self.root/'03 State';state.mkdir();(state/'manifest.json').write_text(json.dumps({'notes':[{'note_id':'legacy-x','path':'legacy.md','title':'old','category':'','note_type':'unknown','updated_at':'','created_at':''}]}))
  self.run_replay();rows=json.loads((state/'manifest.json').read_text())['notes'];self.assertEqual(len(rows),2);self.assertEqual(path.read_text(),'old legacy')
 def test_collision_suffix(self):
  path=self.root/'03 纯文本记录'/'New title.md';path.parent.mkdir();path.write_text('unrelated')
  self.run_replay();self.assertTrue(path.with_name('New title-123.md').exists());self.assertEqual(path.read_text(),'unrelated')
 def test_corrupt_binary_readback_rolls_back(self):
  from unittest.mock import patch
  import source_sync_assets
  self.note['attachments']=[{'type':'image','url':'https://example.test/a.png'}]
  before=snapshot(self.root);real=source_sync_assets.write_binary
  def corrupt(root,relative,data):
   real(root,relative,data);(root/relative).write_bytes(b'corrupt')
  with patch('source_sync_assets.write_binary',side_effect=corrupt):
   with self.assertRaisesRegex(ValueError,'readback'):self.run_replay()
  self.assertEqual(snapshot(self.root),before)
 def test_old_corrupt_hash_path_is_not_silently_repaired(self):
  import hashlib
  self.note['attachments']=[{'type':'image','url':'https://example.test/a.png'}]
  target=self.root/'_assets'/'images'/(hashlib.sha256(b'new-image').hexdigest()+'.png')
  target.parent.mkdir(parents=True);target.write_bytes(b'historical-corruption')
  before=snapshot(self.root)
  with self.assertRaisesRegex(ParityFailure,'old_unselected_data_modified'):self.run_replay()
  self.assertEqual(snapshot(self.root),before)
 def test_unselected_file_modification_rejected(self):
  old=self.root/'nonapi.txt';old.write_text('keep');before=snapshot(self.root)
  def factory(root):
   writer=self.factory(root);original=writer.finish
   def finish():original();(root/'nonapi.txt').write_text('changed')
   writer.finish=finish;return writer
  with self.assertRaisesRegex(ParityFailure,'old_unselected_data_modified'):replay(self.root,lambda:([self.note],True),lambda _:self.note,factory)
  self.assertEqual(snapshot(self.root),before)
 def test_success_string_and_false_completeness_cannot_forge_report(self):
  result=safe_report({'status':'mirror_replay_ok','source_content_complete_claimed':True})
  self.assertEqual(result['status'],'failed');self.assertFalse(result['source_content_complete_claimed'])
 def test_safe_success_accepts_actual_two_pass_evidence(self):
  value=self.run_replay();value['status']='mirror_replay_ok'
  self.assertEqual(safe_report(value)['status'],'mirror_replay_ok')
 def test_half_swap_restores_old_seed(self):
  from unittest.mock import patch
  import os
  before=snapshot(self.root);real=os.replace;calls=[]
  def replacing(a,b):
   calls.append(1)
   if len(calls)==2:raise OSError('simulated')
   return real(a,b)
  with patch('get_mirror_incremental_replay.os.replace',side_effect=replacing):
   with self.assertRaises(OSError):self.run_replay()
  self.assertEqual(snapshot(self.root),before)
 def test_public_report_drops_private_strings(self):
  result=safe_report({'status':'failed','error':'https://private.test/token','title':'private','first':{'fresh_details':1,'id':'secret'}})
  self.assertNotIn('private',json.dumps(result));self.assertNotIn('secret',json.dumps(result))

if __name__=='__main__':unittest.main()
