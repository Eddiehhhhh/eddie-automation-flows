from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_canonical_writer as n
import get_notion_sink_plan as notion
import get_notion_executor as executor


def source():
 return {'id':'1','title':'Fixture','note_type':'recorder_audio','content':'source summary',
         'audio':{'original':'original transcript with exact spaces  '},
         'created_at':'2026-10-13T00:00:00Z','updated_at':'2026-10-13T00:00:00Z',
         'timeline':{'moments':[{'text':'raw ``` marker\n原文','position':123,'nested':{'flag':True,'value':None}}],
                     'resources':[],'schema_version':1,'version':1}}


class TimelineFidelity(unittest.TestCase):
 def test_exact_typed_json_and_transcript_preserved(self):
  note=source();original=deepcopy(note);validated=n.source_note(note,note)
  data=n.render_exact(validated,n.new_path(validated,'1'));text=data.decode()
  payload=re.search(r'## 时间线标记（原始结构）\n\n```json\n(.*?)\n```',text,re.S).group(1)
  self.assertEqual(json.loads(payload),note['timeline']['moments']);self.assertNotIn('```',payload)
  self.assertIn(note['audio']['original'],text);self.assertEqual(note,original)
  self.assertEqual(n.source_metadata(validated)['timeline'],note['timeline'])
 def test_empty_timeline_and_shared_legacy_renderer_are_unchanged(self):
  note=source();note['timeline']['moments']=[];path=n.new_path(note,'1')
  legacy=n.render(note,[],Path(path),faithful=True)[0].encode()
  self.assertEqual(n.render_exact(note,path),legacy)
  self.assertNotIn('时间线标记',n.render(source(),[],Path(path),faithful=True)[0])
 def test_other_structures_and_missing_original_stay_held(self):
  note=source();note['timeline']['resources']=[{'url':'https://example.invalid/'}]
  with self.assertRaisesRegex(RuntimeError,'source_structured_text_unmapped'):n.source_note(note,note)
  note=source();note['meeting_todos']={'source':'fixture','items':[{'text':'todo'}]}
  with self.assertRaisesRegex(RuntimeError,'source_structured_text_unmapped'):n.source_note(note,note)
  note=source();note['audio']['original']=''
  with self.assertRaisesRegex(RuntimeError,'source_transcript_pending'):n.source_note(note,note)
 def test_manual_modified_note_is_not_overwritten(self):
  evidence=SimpleNamespace(anchor={'capture_commit':'1'*40,'census_sha256':'2'*64,'ledger_sha256':'3'*64},initial={},old={},conflicts={},source={},census={'eligible':{},'pending_ids':[],'summaries':{}})
  legacy=n.legacy.initial_state(evidence,{});digest=n.legacy.sha(n.legacy.encoded(legacy));registry=n.initial_registry(evidence,legacy,digest,{})
  note=source();state,changes,_=n.propose_new(evidence,legacy,digest,registry,n.legacy.sha(n.legacy.encoded(registry)),{},[note],True,{'1':note},selected_new=['1'])
  path=state['newById']['1']['filePath'];manual=changes[path]+b'\nmanual addition\n';current={path:manual}
  newer=deepcopy(note);newer['updated_at']='2026-10-14T00:00:00Z';newer['timeline']['moments'].append({'text':'new'})
  result,outputs,counts=n.propose_new(evidence,legacy,digest,state,n.legacy.sha(n.legacy.encoded(state)),current,[newer],True,{'1':newer})
  self.assertEqual(result['conflictsById']['1'],'local_note_edited');self.assertEqual(outputs,{})
  self.assertEqual(current[path],manual);self.assertEqual(result['newById']['1'],state['newById']['1'])
 def test_notion_preview_is_json_code_and_has_no_transport(self):
  note=source();path=n.new_path(note,'1');data=n.render_exact(n.source_note(note,note),path)
  with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('no Notion transport')):
   blocks,used,warnings=notion.render_markdown(data,'1',{},'Raw/03 Get/'+path,{})
  codes=[block['payload'] for block in blocks if block['type']=='code']
  self.assertEqual(len(codes),1);self.assertEqual(codes[0]['language'],'json')
  self.assertEqual(json.loads(codes[0]['rich_text'][0]['text']['content']),note['timeline']['moments']);self.assertEqual(used,{})

 def test_long_timeline_request_payload_is_lossless(self):
  note=source();note['timeline']['moments']=[{'text':'原文`'*6000,'position':123}]
  path=n.new_path(note,'1');data=n.render_exact(n.source_note(note,note),path)
  with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('no Notion transport')):
   blocks,_,_=notion.render_markdown(data,'1',{},'Raw/03 Get/'+path,{})
   request=executor.request_blocks(blocks)
  code=next(block for block in request if block['type']=='code')
  runs=code['payload']['rich_text']
  self.assertGreater(len(runs),1);self.assertTrue(all(len(run['text']['content'])<=2000 for run in runs))
  self.assertEqual(json.loads(''.join(run['text']['content'] for run in runs)),note['timeline']['moments'])
 def test_oversized_single_code_block_is_rejected_not_truncated(self):
  note=source();note['timeline']['moments']=[{'text':'x'*200001}]
  path=n.new_path(note,'1');data=n.render_exact(n.source_note(note,note),path)
  blocks,_,_=notion.render_markdown(data,'1',{},'Raw/03 Get/'+path,{})
  original=deepcopy(blocks)
  with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('no Notion transport')):
   with self.assertRaisesRegex(executor.Blocked,'request_budget'):executor.request_blocks(blocks)
  self.assertEqual(blocks,original)
  code=next(block for block in blocks if block['type']=='code')
  self.assertEqual(json.loads(code['payload']['rich_text'][0]['text']['content']),note['timeline']['moments'])

if __name__=='__main__':unittest.main()
