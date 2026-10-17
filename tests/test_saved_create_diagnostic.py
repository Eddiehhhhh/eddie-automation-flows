from copy import deepcopy
from unittest.mock import patch
import json
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"diagnostics"))

import get_notion_saved_create_diagnostic as d
import get_notion_executor as e
import get_notion_sink_plan as p
import test_notion_executor as f


class Tests(unittest.TestCase):
    def fixture(self, blocks=None):
        api=f.API(None);api.make(f.N, blocks or [f.paragraph('PRIVATE body'), f.paragraph('Second')])
        snap=api.pages[f.N]
        row={'action':'create','note_id':f.N,'rendered':blocks or [f.paragraph('PRIVATE body'), f.paragraph('Second')],
             'properties':{k:deepcopy(v) for k,v in snap['properties_raw'].items() if k in ('Title','笔记 ID')}}
        return row,snap
    def run_d(self,row,snap,page=None):return d.diagnose_created(row,snap,page or snap['page_id'],f.DS)
    def test_exact_match_no_mutation_or_private_output(self):
        row,snap=self.fixture();before=p.encoded([row,snap]);out=self.run_d(row,snap)
        self.assertEqual(out['status'],'matched');self.assertEqual(out['actual']['roots'],2)
        self.assertEqual(before,p.encoded([row,snap]))
        raw=json.dumps(out)
        for private in ('PRIVATE',f.N,f.DS,snap['page_id'],'Title','笔记 ID','User'):
            self.assertNotIn(private,raw)
    def test_saved_page_mismatch(self):
        row,snap=self.fixture();out=self.run_d(row,snap,'f'*32)
        self.assertFalse(out['page_matches_saved']);self.assertEqual(out['status'],'mismatch')
    def test_properties(self):
        row,snap=self.fixture();snap['properties_raw']['Title']['title'][0]['text']['content']='PRIVATE changed'
        out=self.run_d(row,snap);self.assertEqual(out['properties']['title_mismatched'],1)
        self.assertTrue(out['semantics_equal']);self.assertFalse(out['created_accepts'])
    def test_text_annotations_link_and_color(self):
        for category,mutate in [('text',lambda v:v['rich_text'][0]['text'].update(content='changed')),
               ('annotations',lambda v:v['rich_text'][0]['annotations'].update(bold=True)),
               ('link',lambda v:v['rich_text'][0]['text'].update(link={'url':'https://private.invalid'})),
               ('payload_other',lambda v:v.update(color='red'))]:
            with self.subTest(category=category):
                row,snap=self.fixture();mutate(snap['recursive_blocks'][0]['payload']);out=self.run_d(row,snap)
                self.assertEqual(out['status'],'mismatch');self.assertGreater(out['positional_differences'][category],0)
                self.assertNotIn('private.invalid',json.dumps(out))
    def test_whitespace_and_chunking(self):
        row,snap=self.fixture([f.paragraph('ab')]);snap['recursive_blocks'][0]['payload']['rich_text']=[p.text_run('a'),p.text_run('b')]
        self.assertEqual(self.run_d(row,snap)['status'],'matched')
        snap['recursive_blocks'][0]['payload']['rich_text']=[p.text_run('a b')]
        self.assertEqual(self.run_d(row,snap)['positional_differences']['text_whitespace_only'],1)
    def test_heading_children_table(self):
        row,snap=self.fixture([p.block('heading_4',{'rich_text':[p.text_run('heading')]})]);snap['recursive_blocks'][0]['type']='heading_3'
        self.assertEqual(self.run_d(row,snap)['positional_differences']['heading'],1)
        nested=f.paragraph('nested');nested['children']=[f.paragraph('child')]
        row,snap=self.fixture([nested]);snap['recursive_blocks']=snap['recursive_blocks'][:1];snap['recursive_blocks'][0].update(children_ids=[],has_children=False)
        self.assertEqual(self.run_d(row,snap)['positional_differences']['children'],1)
        table=p.block('table',{'table_width':1,'has_column_header':False,'has_row_header':False},[p.block('table_row',{'cells':[[p.text_run('cell')]]})])
        row,snap=self.fixture([table]);snap['recursive_blocks'][1]['payload']['cells'][0][0]['text']['content']='changed'
        self.assertEqual(self.run_d(row,snap)['positional_differences']['table'],1)
    def test_order_and_unique_alignment(self):
        row,snap=self.fixture();snap['recursive_blocks'].reverse();out=self.run_d(row,snap)
        self.assertTrue(out['root_order_only_difference']);self.assertEqual(out['positional_exact_roots'],0)
        row,snap=self.fixture();api=f.API(None);api.serial=200;rows,_=api.rows([f.paragraph('prefix')],snap['page_id']);snap['recursive_blocks']=rows+snap['recursive_blocks']
        out=self.run_d(row,snap);self.assertEqual(out['whole_root_alignment']['best_exact'],2);self.assertTrue(out['whole_root_alignment']['unique_best'])
    def test_image_identity_difference(self):
        img=p.block('image',{'kind':'notion_uploaded_png','upload_id':'a'*32,'sha256':'b'*64,'byte_count':100,'mime':'image/png','caption':[]})
        row,snap=self.fixture([img]);snap['recursive_blocks'][0]['payload']['sha256']='c'*64
        out=self.run_d(row,snap);self.assertEqual(out['positional_differences']['image'],1)
    def test_malformed_and_unknown_fields_are_sanitized(self):
        row,snap=self.fixture()
        for malformed in (None,[],{'PRIVATE':'secret'}):
            out=d.diagnose_created(row,malformed,'a'*32,f.DS);self.assertEqual(out['status'],'blocked');self.assertNotIn('PRIVATE',json.dumps(out))
        snap['recursive_blocks'][0]['payload']['PRIVATE']='secret';out=self.run_d(row,snap)
        self.assertEqual(out['stage'],'target');self.assertEqual(out['reason'],'target_invalid');self.assertNotIn('PRIVATE',json.dumps(out))
    def test_exception_text_is_never_forwarded(self):
        row,snap=self.fixture()
        with patch.object(e,'target',side_effect=RuntimeError('PRIVATE https://secret.invalid')):
            out=self.run_d(row,snap)
        self.assertEqual(out['stage'],'target');self.assertNotIn('PRIVATE',json.dumps(out))
    def test_list_default_start_and_mid_list_reset_stay_exact(self):
        one=p.block('numbered_list_item',{'rich_text':[p.text_run('one')],'list_start_index':1})
        two=p.block('numbered_list_item',{'rich_text':[p.text_run('two')],'list_start_index':1})
        row,snap=self.fixture([one,two]);snap['recursive_blocks'][0]['payload'].pop('list_start_index')
        self.assertEqual(self.run_d(row,snap)['status'],'matched')
        snap['recursive_blocks'][1]['payload'].pop('list_start_index')
        self.assertEqual(self.run_d(row,snap)['status'],'mismatch')
    def test_acceptance_crosscheck_fails_closed(self):
        row,snap=self.fixture()
        with patch.object(e,'created',side_effect=e.Blocked('readback_mismatch')):out=self.run_d(row,snap)
        self.assertEqual(out['status'],'blocked');self.assertEqual(out['stage'],'acceptance_crosscheck')

if __name__=='__main__':unittest.main()
