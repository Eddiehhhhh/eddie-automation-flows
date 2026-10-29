import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
sys.path.append('/workspace/shared/get-preview-wiring-draft/candidate/scripts')
from get_mirror_render import render
class AudioField(unittest.TestCase):
 def note(self,kind='audio',**kw):return dict(note_id='123',note_type=kind,title='Synthetic note',created_at='2026-01-01T00:00:00+08:00',updated_at='2026-01-01T00:00:00+08:00',content='Synthetic AI summary',**kw)
 def test_original_supplies_transcript_for_audio_types(self):
  for kind in ['audio','local_audio','recorder_audio','recorder_flash_audio']:
   body,fm=render(self.note(kind,audio={'original':'Synthetic full original'}),[],Path('x.md'))
   self.assertEqual(fm['audio_transcript'],'Synthetic full original');self.assertIn('## AI 总结',body);self.assertIn('## 原文',body)
 def test_existing_explicit_fields_preserved(self):
  for fields,expected in [({'audio':{'original':'original','transcript':'explicit'}},'explicit'),({'audio':{'original':'original'},'audio_transcript':'legacy'},'legacy')]:
   self.assertEqual(render(self.note(**fields),[],Path('x.md'))[1]['audio_transcript'],expected)
 def test_summary_never_becomes_transcript(self):
  self.assertEqual(render(self.note(),[],Path('x.md'))[1]['audio_transcript'],'')
 def test_non_audio_not_inferred(self):
  self.assertEqual(render(self.note('plain_text',audio={'original':'other'}),[],Path('x.md'))[1]['audio_transcript'],'')
if __name__=='__main__':unittest.main()
