import copy
import hashlib
import unittest

from core import snapshot
from notion_sink import Hold, NotionSink, plain, rich
from notion_native import FORMAT, read_rich, text_items
from render import render_text
from test_notion_sink import Fake, Journal


BASE = 'https://github.com/owner/private/blob/main'


class NativeFake(Fake):
    def request(self, method, path, body=None):
        if method == 'PATCH' and path.startswith('/v1/blocks/') and not path.endswith('/children'):
            self.calls.append((method, path, copy.deepcopy(body)))
            for group in self.blocks.values():
                for block in group:
                    if block['id'] == path.rsplit('/', 1)[1]:
                        block['toggle'].update(copy.deepcopy(body['toggle']))
                        if self.timeout == 'rename':
                            self.timeout = None
                            raise TimeoutError()
                        return copy.deepcopy(block)
            raise AssertionError('missing synthetic block')
        if (self.timeout == 'original-heading' and method == 'PATCH' and path.endswith('/children')
                and body['children'][0]['type'] == 'heading_1'
                and plain(body['children'][0]['heading_1']['rich_text']) == '原文'):
            self.timeout = None
            super().request(method, path, body)
            raise TimeoutError()
        return super().request(method, path, body)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.api = NativeFake()
        self.journal = Journal()
        self.sink = NotionSink(self.api, 'db', self.journal, source_base=BASE)
        self.note = {'id': '1', 'title':'Synthetic title',
                     'updated_at': '2026-11-19T10:23:45+08:00',
                     'note_type': 'plain_text', 'attachments': [],
                     'content': '## 小节\n- **重点**\n1. 步骤\n> 引用\n```\n# 原样\n```'}

    def writes(self):
        return len([call for call in self.api.calls if call[0] == 'PATCH'])

    def test_native_markup_receipt_and_human_neighbor(self):
        snap = snapshot(self.note)
        receipt = self.sink.sync(snap)
        self.assertEqual(receipt['format'], FORMAT)
        self.assertEqual(receipt['content_sha256'], hashlib.sha256(render_text(snap).encode()).hexdigest())
        blocks = self.api.blocks[receipt['block_id']]
        self.assertEqual([block['type'] for block in blocks],
                         ['heading_2', 'bulleted_list_item', 'numbered_list_item', 'quote', 'code'])
        self.assertEqual(read_rich(blocks[1]['bulleted_list_item']['rich_text']), [('重点', None, ('bold',))])
        self.assertEqual(plain(blocks[-1]['code']['rich_text']), '# 原样')
        top = self.api.blocks[receipt['page_id']]
        self.assertEqual(plain(top[0]['toggle']['rich_text']), 'Get 正文 · 源文件')
        self.assertIn(snap['version'], top[0]['toggle']['rich_text'][1]['text']['link']['url'])
        top.append({'id': 'unrelated', 'type': 'toggle', 'toggle': {'rich_text': [
            {'type': 'mention', 'plain_text': '人工备注'}]}})
        writes = self.writes()
        self.assertEqual(self.sink.sync(snap, receipt), receipt)
        self.assertEqual(self.writes(), writes)

    def test_native_render_preserves_original_and_hides_signatures(self):
        url = 'https://mediacdn.umiwi.com/synthetic.png?Expires=123&OSSAccessKeyId=X&Signature=Y'
        self.note['content'] = '![](' + url + ')\n正文'
        self.note['note_type'] = 'recorder_audio'
        self.note['audio'] = {'original': '转录与附件 ' + url, 'play_url': 'https://mediacdn.umiwi.com/synthetic.ogg'}
        snap = snapshot(self.note)
        receipt = self.sink.sync(snap)
        shown = str([self.api.blocks[s['block_id']] for s in receipt['sections']])
        self.assertNotIn('Signature=', shown)
        self.assertIn('转录与附件', shown)
        self.assertIn('图片见附件', shown)
        self.assertIn('Signature=', render_text(snap))

    def test_ambiguous_native_create_and_append_keep_one_container(self):
        snap = snapshot(dict(self.note, content='plain text'))
        for phase in ('toggle', 'append'):
            self.api.timeout = phase
            with self.assertRaises(TimeoutError):
                self.sink.sync(snap)
        receipt = self.sink.sync(snap)
        self.assertEqual(len(self.api.blocks[receipt['page_id']]), 1)
        self.assertEqual(len(self.api.blocks[receipt['block_id']]), 1)

    def test_legacy_receipt_readback_and_history_rename_are_recoverable(self):
        snap = snapshot(self.note)
        old = NotionSink(self.api, 'db', self.journal).sync(snap)
        writes = self.writes()
        self.assertEqual(self.sink.sync(snap, old), old)
        self.assertEqual(self.writes(), writes)
        next_snap = snapshot(dict(self.note, content='## 新的正文\n第一段'))
        self.api.timeout = 'rename'
        with self.assertRaises(TimeoutError):
            self.sink.sync(next_snap, old)
        recovered = self.sink.sync(next_snap, old)
        self.assertEqual(recovered['page_id'], old['page_id'])
        self.assertNotEqual(recovered['block_id'], old['block_id'])
        top = self.api.blocks[old['page_id']]
        self.assertEqual(len(top), 2)
        self.assertTrue(plain(top[0]['toggle']['rich_text']).startswith('历史正文'))
        self.assertEqual(''.join(self.sink.block_text(old['block_id'])), render_text(snap))
        self.assertEqual(self.sink.sync(next_snap, recovered), recovered)

    def test_exactly_renamed_legacy_label_does_not_add_another_container(self):
        snap = snapshot(self.note)
        old = NotionSink(self.api, 'db', self.journal).sync(snap)
        top = self.api.blocks[old['page_id']]
        top[0]['toggle']['rich_text'] = self.sink.legacy_header(snap['id'], snap['version'])
        writes = self.writes()
        self.assertEqual(self.sink.sync(snap, old), old)
        self.assertEqual((len(top), self.writes()), (1, writes))

    def test_recording_matches_reference_two_toggle_headings_and_levels(self):
        self.note.update(note_type='recorder_audio',
                         content='### 章节概要\n#### 子章节\n- **重点**',
                         audio={'original': '录音逐字稿', 'play_url': 'https://mediacdn.umiwi.com/synthetic.ogg'})
        snap = snapshot(self.note)
        receipt = self.sink.sync(snap)
        top = self.api.blocks[receipt['page_id']]
        self.assertEqual([plain(b['heading_1']['rich_text']) for b in top], ['录音智能总结', '原文'])
        self.assertTrue(all(b['heading_1']['is_toggleable'] for b in top))
        self.assertEqual([b['type'] for b in self.api.blocks[top[0]['id']]],
                         ['heading_3', 'heading_4', 'bulleted_list_item'])
        self.assertEqual(plain(self.api.blocks[top[1]['id']][0]['paragraph']['rich_text']), '录音逐字稿')
        writes = self.writes()
        self.assertEqual(self.sink.sync(snap, receipt), receipt)
        self.assertEqual(self.writes(), writes)

    def test_notion_bare_host_slash_is_only_tolerated_link_rewrite(self):
        def link(url):
            return read_rich(text_items('source', lambda s: [s], url=url))
        self.assertEqual(link('https://example.test'), link('https://example.test/'))
        self.assertEqual(link('https://example.test?x=a%2Fb'), link('https://example.test/?x=a%2Fb'))
        self.assertNotEqual(link('https://example.test/folder'), link('https://example.test/folder/'))
        self.assertNotEqual(link('https://example.test/?x=a%2Fb'), link('https://example.test/?x=a/b'))

    def test_huge_single_line_stays_below_serialized_request_limit(self):
        original = '中文🧪' * 65000
        self.note['content'] = original
        receipt = self.sink.sync(snapshot(self.note))
        blocks = self.api.blocks[receipt['block_id']]
        self.assertGreater(len(blocks), 1)
        self.assertEqual(''.join(plain(b['paragraph']['rich_text']) for b in blocks), original)

    def test_long_recording_transcript_keeps_all_line_breaks_in_bounded_blocks(self):
        original = '\n'.join(str(i) + '. 原始录音🧪' for i in range(1200))
        self.note.update(note_type='recorder_audio', content='### 智能总结',
                         audio={'original': original, 'play_url': 'https://mediacdn.umiwi.com/synthetic.ogg'})
        result = self.sink.sync(snapshot(self.note))
        children = self.api.blocks[result['sections'][1]['block_id']]
        self.assertTrue(all(block['type'] == 'paragraph' for block in children))
        self.assertLess(len(children), 20)
        self.assertEqual(''.join(plain(block['paragraph']['rich_text']) for block in children), original)

    def test_recording_second_section_timeout_reuses_both_known_headings(self):
        self.note.update(note_type='recorder_audio',
                         content='### 章节概要\n#### 子章节\n- **重点**',
                         audio={'original': '录音逐字稿', 'play_url': 'https://mediacdn.umiwi.com/synthetic.ogg'})
        snap = snapshot(self.note)
        self.api.timeout = 'original-heading'
        with self.assertRaises(TimeoutError):
            self.sink.sync(snap)
        native = self.sink.sync(snap)
        top = self.api.blocks[native['page_id']]
        self.assertEqual(len(top), 2)
        self.assertEqual([section['block_id'] for section in native['sections']], [block['id'] for block in top])

    def test_native_receipt_stops_on_human_format_edit(self):
        snap = snapshot(self.note)
        receipt = self.sink.sync(snap)
        heading = self.api.blocks[receipt['block_id']][0]['heading_2']
        heading['rich_text'][0]['annotations'] = {'bold': True}
        writes = self.writes()
        with self.assertRaisesRegex(Hold, 'managed_content_modified'):
            self.sink.sync(snapshot(dict(self.note, content='new')), receipt)
        self.assertEqual(self.writes(), writes)
