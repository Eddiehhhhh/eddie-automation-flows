from copy import deepcopy
import unittest
import get_notion_list_semantics as m
import get_notion_sink_plan as p
import get_notion_live_transport as t
import get_notion_executor as e
from test_notion_executor import API, N, DS, paragraph

def item(text='one',start=True):
    payload={'rich_text':[p.text_run(text)]}
    if start is not None:payload['list_start_index']=start
    return p.block('numbered_list_item',payload)

class NumberingTests(unittest.TestCase):
    def test_only_segment_start_equivalent(self):
        source=[paragraph('first'),item(start=1),item('two',None),paragraph('second'),item('three',1)]
        absent=deepcopy(source)
        for row in absent:row['payload'].pop('list_start_index',None)
        self.assertEqual(m.defaults(source),m.defaults(absent))
        self.assertNotEqual(source,absent)
        self.assertEqual(t.wire_blocks(source),t.wire_blocks(absent))
        self.assertEqual(source[1]['payload']['list_start_index'],1)
    def test_unknown_or_numbered_predecessor_is_not_equivalent(self):
        for previous in ('unknown','numbered_list_item'):
            self.assertNotEqual(m.defaults([item(start=1)],previous),m.defaults([item(start=None)],previous))
            with self.assertRaisesRegex(t.Failure,'unsupported_source_format'):t.wire_blocks([item(start=1)],previous)
        self.assertEqual(t.wire_blocks([item(start=1)],None),t.wire_blocks([item(start=None)],None))
    def test_mid_list_reset_and_invalid_values_block(self):
        for value in (None,True,2):
            row=item(start=1);row['payload']['list_start_index']=value
            with self.assertRaises(t.Failure):t.wire_blocks([row],None)
        with self.assertRaises(t.Failure):t.wire_blocks([item(start=1),item('reset',1)],None)
        row=item(start=1);row['payload']['list_format']='letters'
        with self.assertRaises(t.Failure):t.wire_blocks([row],None)
    def test_nested_own_sibling_context(self):
        parent=paragraph('parent');parent['children']=[item(start=1),item('two',None)]
        wire=t.wire_blocks([parent]);self.assertNotIn('list_start_index',wire[0]['paragraph']['children'][0]['numbered_list_item'])
        parent['children'][1]['payload']['list_start_index']=1
        with self.assertRaises(t.Failure):t.wire_blocks([parent])
    def test_created_default_readback(self):
        api=API(None);rendered=[paragraph('intro'),item(start=1),item('two',None)]
        actual=deepcopy(rendered);actual[1]['payload'].pop('list_start_index')
        api.make(N,actual);snapshot=api.pages[N]
        row={'note_id':N,'rendered':rendered,'properties':{}}
        self.assertEqual(len(e.created(snapshot,row,DS,snapshot['page_id'])),3)
    def test_appended_default_and_reset_readback(self):
        api=API(None);roots=api.make(N,[paragraph('old')]);before=deepcopy(api.pages[N]);page=before['page_id']
        rendered=[item(start=1),item('two',None)];actual=deepcopy(rendered);actual[0]['payload'].pop('list_start_index')
        rows,new=api.rows(actual,page);after=deepcopy(before);after['recursive_blocks']+=rows
        row={'note_id':N,'rendered':rendered,'after_anchor':roots[-1]}
        self.assertEqual(e.appended(before,after,row,DS)[0],new)
        # Same absent readback cannot stand for a reset after a numbered anchor.
        before['recursive_blocks'][0]['type']='numbered_list_item';after['recursive_blocks'][0]['type']='numbered_list_item'
        with self.assertRaisesRegex(e.Blocked,'readback_mismatch'):e.appended(before,after,row,DS)
    def test_request_schema_does_not_emit_response_only_fields(self):
        source=[paragraph('a')]+[item(str(i),1 if i==1 else None) for i in range(1,5)]+[paragraph('b')]+[item(str(i),1 if i==1 else None) for i in range(1,4)]
        source[1]['payload']['list_format']='numbers'
        wire=t.wire_blocks(source)
        self.assertEqual(len(wire),9)
        for row in wire:
            self.assertNotIn('list_start_index',row[row['type']]);self.assertNotIn('list_format',row[row['type']])
        self.assertEqual([x['type'] for x in source],[x['type'] for x in wire])
