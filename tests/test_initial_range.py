from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_initial_range as m
import get_notion_sink_plan as p

N='1234567890123456789';P='1'*32;DS='2'*32
HISTORY='<callout icon="📝" color="gray_bg">\n\tHistorical preview notice\n</callout>\n\n#### **Nested title**\n\nParagraph > comparison and [link](https://example.invalid/a_b)\n\n<table fit-page-width="true" header-row="true">\n\t<tr>\n\t\t<td>**Name**</td>\n\t\t<td>Value</td>\n\t</tr>\n\t<tr>\n\t\t<td>Example</td>\n\t\t<td>First<br/>Second</td>\n\t</tr>\n</table>\n\nFinal paragraph.'

def paragraph(text):return p.block('paragraph',{'rich_text':[p.text_run(text)]})
def evidence(history=HISTORY):
 data=('---\nnote_id: '+json.dumps(N)+'\n---\nSource body\n').encode()
 value={'note_id':N,'page_id':P,'source':{'commit':'a'*40,'path':'Raw/03 Get/notes/sample.md','blob':p.git_blob(data),'sha256':p.digest(data)},'prepared_markdown':history,'readback':{'id':P,'text':'<page>\n<content>\n'+history+'\n</content>\n</page>'},'h4_mapping':'heading_3'}
 return value,data

def snapshot(blocks):
 rows=[];serial=[10]
 def add(block,parent):
  serial[0]+=1;ident='%032x'%serial[0];children=[]
  rows.append({'id':ident,'parent_id':parent,'type':block['type'],'payload':deepcopy(block['payload']),'children_ids':children,'has_children':bool(block['children'])})
  for child in block['children']:children.append(add(child,ident))
  return ident
 for block in blocks:add(block,P)
 return {'complete':True,'native_blocks_available':True,'identity_query_complete':True,'exact_id_matches':[P],'page_id':P,'parent_data_source':DS,'id_property':N,'last_edited_time':'2026-10-16T00:00:00Z','properties_raw':{'笔记 ID':{'type':'rich_text','rich_text':[p.text_run(N)]},'User property':{'type':'number','number':5}},'recursive_blocks':rows}

class Tests(unittest.TestCase):
 def fixture(self,history=HISTORY):
  ev,data=evidence(history);entries=m.historical_roots(history,N,h4_mapping='heading_3')
  blocks=[paragraph('Original metadata'),p.block('divider',{})]
  for entry in entries:
   if entry.get('kind')=='image':blocks.append({'type':'image','payload':{'type':'file','file':{'url':'https://private.invalid/temporary','expiry_time':'later'},'caption':[]},'children':[]})
   else:blocks.append(entry['block'])
  blocks.append(paragraph('Human extra'))
  return ev,data,snapshot(blocks),entries
 def test_explicit_h4_table_and_callout_parser(self):
  entries=m.historical_roots(HISTORY,N,h4_mapping='heading_3')
  self.assertEqual(entries[0]['role'],'protected');self.assertEqual(entries[0]['block']['type'],'callout')
  self.assertEqual(entries[1]['block']['type'],'heading_3');self.assertTrue(entries[1]['block']['payload']['rich_text'][0]['annotations']['bold'])
  table=next(x['block'] for x in entries if x['block']['type']=='table')
  self.assertEqual(table['payload'],{'table_width':2,'has_column_header':True,'has_row_header':False});self.assertEqual(len(table['children']),2)
  self.assertEqual(table['children'][1]['payload']['cells'][1][0]['text']['content'],'First\nSecond')
 def test_candidate_keeps_metadata_callout_divider_and_human_extras(self):
  ev,data,target,entries=self.fixture();before=deepcopy(target);out=m.propose(ev,p.digest(ev),data,target,DS)
  self.assertEqual(out['decision'],'candidate');self.assertFalse(out['ownership_adopted']);self.assertTrue(out['requires_explicit_scope_approval']);self.assertIsNotNone(out['executor_scope'])
  self.assertEqual(len(out['protected_block_ids']),4);self.assertEqual(target,before)
  managed=set(out['managed_block_ids']);self.assertTrue(all(row['type'] not in ('divider','callout') for row in target['recursive_blocks'] if row['id'] in managed))
 def test_unique_range_requires_full_order_and_no_human_owned_edit(self):
  ev,data,target,_=self.fixture();target['recursive_blocks'][3]['payload']['rich_text']=[p.text_run('Human edited title')]
  self.assertEqual(m.propose(ev,p.digest(ev),data,target,DS)['reason'],'no_unique_range')
 def test_duplicate_expected_sequence_blocks(self):
  ev,data,target,entries=self.fixture();wanted=[x['block'] for x in entries if x['role']=='candidate'];target=snapshot(wanted+wanted)
  self.assertEqual(m.propose(ev,p.digest(ev),data,target,DS)['reason'],'no_unique_range')
 def test_source_and_historical_input_anchors_fail_closed(self):
  ev,data,target,_=self.fixture()
  self.assertEqual(m.propose(ev,'f'*64,data,target,DS)['reason'],'evidence_invalid')
  self.assertEqual(m.propose(ev,p.digest(ev),data+b'changed',target,DS)['reason'],'source_changed')
  edited=deepcopy(ev);edited['prepared_markdown']+='Changed';self.assertEqual(m.propose(edited,p.digest(ev),data,target,DS)['reason'],'evidence_invalid')
  edited=deepcopy(ev);edited['readback']['text']+='Changed';self.assertEqual(m.propose(edited,p.digest(ev),data,target,DS)['reason'],'evidence_invalid')
 def test_image_upload_reference_never_becomes_owned_native_identity(self):
  history='<image src="file-upload://11111111-2222-3333-4444-555555555555">Caption</image>\n\n## Heading\n\nBody'
  ev,data,target,_=self.fixture(history);out=m.propose(ev,p.digest(ev),data,target,DS)
  self.assertEqual(out['decision'],'candidate');self.assertEqual(out['pending_assets'],['native_image_binding_required']);self.assertIsNone(out['executor_scope'])
  image=next(x for x in target['recursive_blocks'] if x['type']=='image');self.assertIn(image['id'],out['protected_block_ids']);self.assertNotIn(image['id'],out['managed_block_ids'])
  self.assertNotIn('file-upload',str(out));self.assertNotIn('private.invalid',str(out))
 def test_h4_different_native_kind_never_guessed(self):
  ev,data,target,_=self.fixture();target['recursive_blocks'][3]['type']='heading_2'
  self.assertEqual(m.propose(ev,p.digest(ev),data,target,DS)['reason'],'no_unique_range')
  ev['h4_mapping']='guess';self.assertEqual(m.propose(ev,p.digest(ev),data,target,DS)['reason'],'unsupported_history')
 def test_unknown_xml_attributes_entities_and_table_shape_block(self):
  for value in ('<callout icon="x" color="red_bg">\n\tx\n</callout>', '<table evil="true"><tr><td>x</td></tr></table>', '<!DOCTYPE table><table fit-page-width="true" header-row="true"><tr><td>x</td></tr></table>', HISTORY.replace('<td>Value</td>',''),'<image src="https://example.invalid/x">Caption</image>'):
   with self.assertRaises(m.Blocked):m.historical_roots(value,N,h4_mapping='heading_3')
 def test_divider_within_historical_owned_range_is_not_silently_owned(self):
  ev,data,target,_=self.fixture('Before\n\n---\n\nAfter')
  self.assertEqual(m.propose(ev,p.digest(ev),data,target,DS)['reason'],'noncontiguous_history')
 def test_identity_duplicate_missing_topology_and_parent_guards(self):
  ev,data,target,_=self.fixture()
  duplicate=deepcopy(target);duplicate['exact_id_matches'].append('3'*32);self.assertEqual(m.propose(ev,p.digest(ev),data,duplicate,DS)['reason'],'duplicate_id')
  self.assertEqual(m.propose(ev,p.digest(ev),data,target,'4'*32)['reason'],'wrong_parent')
  broken=deepcopy(target);broken['recursive_blocks'][4]['parent_id']='f'*32;self.assertEqual(m.propose(ev,p.digest(ev),data,broken,DS)['reason'],'target_invalid')
  for value in (None,[],{}):self.assertEqual(m.propose(ev,p.digest(ev),data,value,DS)['decision'],'blocked')
 def test_public_summary_never_contains_ids_properties_text_or_urls(self):
  ev,data,target,_=self.fixture();out=m.public_summary(m.propose(ev,p.digest(ev),data,target,DS))
  for value in (N,P,'Source body','User property','http'):self.assertNotIn(value,str(out))
  self.assertEqual(out['writes'],0);self.assertFalse(out['ownership_adopted']);self.assertEqual(m.public_summary({'reason':{}})['decision'],'blocked')
 def test_real_git_blob_source_receipt_roundtrip(self):
  ev,data,target,_=self.fixture()
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);path=root/ev['source']['path'];path.parent.mkdir(parents=True);path.write_bytes(data)
   def git(*args):return subprocess.run(['git','-C',td,*args],check=True,capture_output=True).stdout
   git('init','-q');git('config','user.name','Synthetic');git('config','user.email','synthetic@example.invalid');git('add','.');git('commit','-qm','source')
   ev['source']['commit']=git('rev-parse','HEAD').decode().strip();self.assertEqual(git('rev-parse','HEAD:'+ev['source']['path']).decode().strip(),ev['source']['blob'])
   raw=git('show',ev['source']['commit']+':'+ev['source']['path']);self.assertEqual(m.propose(ev,p.digest(ev),raw,target,DS)['decision'],'candidate')
 def test_html_void_br_in_cells_is_exact_newline_but_escaped_br_blocks(self):
  entries=m.historical_roots(HISTORY.replace('<br/>','<br>'),N,h4_mapping='heading_3')
  table=next(x['block'] for x in entries if x['block']['type']=='table');self.assertEqual(table['children'][1]['payload']['cells'][1][0]['text']['content'],'First\nSecond')
  for old in ('First\\<br>Second','`<br>`'):
   with self.assertRaises(m.Blocked):m.historical_roots(HISTORY.replace('First<br/>Second',old),N,h4_mapping='heading_3')
 def hash_fixture(self):
  history='<callout icon="📝" color="gray_bg">\n\tHistorical preview notice\n</callout>\n\n#### **Nested title**\n\nBody'
  ev,_,target,_=self.fixture(history)
  data=('---\nnote_id: '+json.dumps(N)+'\n---\n#### **Nested title**\n\nBody\n\n---\nsource: api\nupdated: 2026-10-16T00:00:00.000Z\ncanonicalId: api:'+N+'\n').encode()
  ev['source'].update(blob=p.git_blob(data),sha256=p.digest(data));proof=m.build_hash_proof(ev,p.digest(ev),data)
  return ev,data,target,proof
 def test_hash_only_proof_reconstructs_git_without_private_historical_text(self):
  ev,data,target,proof=self.hash_fixture();out=m.propose_from_git(proof,p.digest(proof),data,target,DS)
  self.assertEqual(out['decision'],'candidate');self.assertIsNotNone(out['executor_scope']);self.assertEqual(out['managed_block_ids'],m.propose(ev,p.digest(ev),data,target,DS)['managed_block_ids'])
  for private in ('Nested title','Historical preview notice','prepared_markdown','readback'):
   self.assertNotIn(private+'"',json.dumps(proof))
  self.assertNotIn('Body',json.dumps(proof));self.assertFalse(out['ownership_adopted'])
 def test_hash_proof_candidate_mismatch_or_unapproved_change_blocks(self):
  ev,data,target,proof=self.hash_fixture();bad=deepcopy(proof);bad['candidate_semantic_sha256']='f'*64
  self.assertEqual(m.propose_from_git(bad,p.digest(proof),data,target,DS)['reason'],'evidence_invalid')
  self.assertEqual(m.propose_from_git(bad,p.digest(bad),data,target,DS)['reason'],'source_changed')
  ev['prepared_markdown']=ev['prepared_markdown'].replace('Body','Different historical body')
  with self.assertRaises(m.Blocked):m.build_hash_proof(ev,p.digest(ev),data)
 def test_reconstruction_footer_id_shape_and_image_guards(self):
  ev,data,target,proof=self.hash_fixture()
  for raw in (data.replace(('canonicalId: api:'+N).encode(),b'canonicalId: api:9999999999999999999'),data.replace(b'source: api',b'source: other'),data.replace(b'updated: 2026',b'updated: uncertain2026')):
   with self.assertRaisesRegex(m.Blocked,'unsupported_history'):m.reconstructed_roots(raw,N,ev['source']['path'])
  with self.assertRaisesRegex(m.Blocked,'missing_asset'):m.reconstructed_roots(data.replace(b'Body',b'![[asset.jpeg]]'),N,ev['source']['path'])
 def test_hash_proof_wrong_source_and_bad_types_fail_closed(self):
  ev,data,target,proof=self.hash_fixture()
  self.assertEqual(m.propose_from_git(proof,p.digest(proof),data+b'changed',target,DS)['reason'],'source_changed')
  bad=deepcopy(proof);bad['candidate_roots']=True;self.assertEqual(m.propose_from_git(bad,p.digest(bad),data,target,DS)['reason'],'source_changed')
  for value in (None,[],{}):self.assertEqual(m.propose_from_git(value,'f'*64,data,target,DS)['decision'],'blocked')
 def test_unrelated_historical_readback_never_proves_prepared_input(self):
  ev,data,target,proof=self.hash_fixture();ev['readback']['text']='<content>Unrelated content</content>'
  with self.assertRaisesRegex(m.Blocked,'evidence_invalid'):m.build_hash_proof(ev,p.digest(ev),data)
 def test_readback_auto_link_same_address_and_comparison_escape_only(self):
  history='Literal > comparison\n\n- https://example.invalid/a_b'
  ev,data=evidence(history);ev['readback']['text']='<content>Literal '+chr(92)+'> comparison\n- [https://example.invalid/a_b](https://example.invalid/a_b)</content>'
  self.assertTrue(m.evidence_roots(ev,p.digest(ev),data))
  ev['readback']['text']=ev['readback']['text'].replace('](https://example.invalid/a_b)','](https://other.invalid/)')
  with self.assertRaisesRegex(m.Blocked,'evidence_invalid'):m.evidence_roots(ev,p.digest(ev),data)
 def test_readback_comparison_escape_preserves_literal_slash_and_code(self):
  for history in ('Literal '+chr(92)+'> comparison','`literal '+chr(92)+'> code`'):
   ev,data=evidence(history)
   if history.startswith('`'):self.assertTrue(m.evidence_roots(ev,p.digest(ev),data))
   else:
    with self.assertRaisesRegex(m.Blocked,'evidence_invalid'):m.evidence_roots(ev,p.digest(ev),data)
  ev,data=evidence('Literal > comparison');ev['readback']['text']='<content>Literal '+chr(92)*2+'> comparison</content>'
  with self.assertRaisesRegex(m.Blocked,'evidence_invalid'):m.evidence_roots(ev,p.digest(ev),data)
 def test_public_template_rebuild_requires_exact_identity_path_and_full_hash(self):
  ev,data,target,proof=self.hash_fixture();template=m.public_proof_template(proof,p.digest(proof))
  for private in (N,P,proof['source']['path'],'Nested title'):self.assertNotIn(private,json.dumps(template))
  rebuilt=m.reconstruct_proof(template,p.digest(template),note_id=N,page_id=P,source_path=proof['source']['path']);self.assertEqual(rebuilt,proof)
  with self.assertRaises(m.Blocked):m.reconstruct_proof(template,p.digest(template),note_id='9'*19,page_id=P,source_path=proof['source']['path'])
  bad=deepcopy(template);bad['proof_sha256']='f'*64
  with self.assertRaises(m.Blocked):m.reconstruct_proof(bad,p.digest(bad),note_id=N,page_id=P,source_path=proof['source']['path'])
if __name__=='__main__':unittest.main()
