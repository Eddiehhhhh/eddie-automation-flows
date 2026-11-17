"""Synthetic cases prompted by a legacy-page anomaly; no private source content.
These test conservative behavior, not a conclusion about the real note's origin.
"""
import json,unittest
from core import Hold,snapshot,requires_binary_assets
from render import render_text
from assets import asset_references
class Fidelity(unittest.TestCase):
    def test_historical_shape_retains_summary_transcript_and_audio_attachment(self):
        n={'note_id':'9007199254740993','title':'synthetic recording','note_type':'recorder_audio','tags':['录音卡笔记'],'content':'摘'*4528,'updated_at':'2026-11-17','audio':{'original':'录'*12717,'play_url':'https://example.invalid/synthetic-audio'},'attachments':[{'type':'audio','url':'https://example.invalid/synthetic-audio'}]}
        s=snapshot(n);rendered=render_text(s)
        self.assertIn(n['content'],rendered);self.assertIn(n['audio']['original'],rendered)
        self.assertEqual(s['note']['note_type'],'recorder_audio');self.assertEqual(len(s['note']['attachments']),1)
        self.assertTrue(requires_binary_assets(s))
    def test_recording_tag_is_not_authoritative_type(self):
        n={'note_id':'9007199254740993','title':'synthetic','note_type':'plain_text','tags':['录音卡笔记'],'content':'','updated_at':'2026-11-17','attachments':[]}
        s=snapshot(n)
        self.assertEqual(s['note']['note_type'],'plain_text')
        self.assertNotIn('audio',s['note'])
        self.assertEqual(s['id'],'9007199254740993')
        self.assertEqual(asset_references(s['note']),[])
    def test_known_recording_missing_transcript_holds(self):
        n={'note_id':'9007199254740993','note_type':'recorder_audio','content':'','updated_at':'2026-11-17','tags':['synthetic']}
        with self.assertRaisesRegex(Hold,'transcript'):snapshot(n)
    def test_recording_with_empty_summary_keeps_full_transcript(self):
        transcript='合成转录🧪'*20000
        n={'note_id':'9007199254740993','note_type':'recorder_audio','content':'','updated_at':'2026-11-17','audio':{'original':transcript,'play_url':'https://example.invalid/synthetic'},'attachments':[]}
        s=snapshot(n)
        self.assertIn(transcript,render_text(s));self.assertTrue(requires_binary_assets(s))
        self.assertEqual(asset_references(n)[0]['field'],'audio/play_url')
    def test_plain_text_type_does_not_hide_present_audio(self):
        n={'note_id':'9007199254740993','note_type':'plain_text','content':'synthetic','updated_at':'2026-11-17','audio':{'original':'synthetic transcript','play_url':'https://example.invalid/synthetic'}}
        s=snapshot(n)
        self.assertTrue(requires_binary_assets(s));self.assertIn('synthetic transcript',render_text(s))
if __name__=='__main__':unittest.main()
