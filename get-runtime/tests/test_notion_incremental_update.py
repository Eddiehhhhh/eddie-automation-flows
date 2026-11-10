"""An owned update has a durable intent before each Notion write."""
import hashlib
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_incremental_update as update


ID = '1926106578851738352'
PAGE = '1' * 32
SOURCE = '2' * 32
OLD = ['3' * 32, '4' * 32]
NEW = ['5' * 32, '6' * 32]
DATA = b'changed recording text'
PROPS = {'笔记 ID': {'type': 'rich_text', 'rich_text': [{'type': 'text', 'text': {'content': ID}}]},
         '标题': {'type': 'title', 'title': [{'type': 'text', 'text': {'content': '新标题'}}]}}
BLOCKS = [{'type': 'heading_1', 'payload': {'rich_text': [], 'is_toggleable': True}, 'children': []},
          {'type': 'heading_1', 'payload': {'rich_text': [], 'is_toggleable': True}, 'children': []}]


def snapshot(tag, title='旧标题'):
    props = {'笔记 ID': PROPS['笔记 ID'],
             '标题': {'type': 'title', 'title': [{'type': 'text', 'text': {'content': title}}]},
             '人工字段': {'type': 'rich_text', 'rich_text': []}}
    return {'page_id': PAGE, 'tag': tag, 'properties_raw': props}


class FakeAPI:
    def __init__(self):
        self.state = snapshot('before')
        self.writes = []

    def read_note(self, ident):
        return self.state

    def matches(self, ident):
        return [PAGE]

    def preflight_append(self, page, blocks, after):
        return {'method': 'PATCH', 'path': '/blocks/' + page + '/children', 'body': {}}

    def append_blocks(self, page, blocks, after):
        self.writes.append('append')
        self.state = snapshot('appended')

    def archive_block(self, block):
        self.writes.append('archive:' + block)
        self.state = snapshot('one_archived' if block == OLD[0] else 'body_done')

    def update_incremental_properties(self, page, props, names):
        self.writes.append('properties')
        self.state = snapshot('body_done', '新标题')


class OwnedUpdate(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.saved = []
        self.owned = {'note_id': ID, 'page_id': PAGE,
                      'source_sha256': 'a' * 64, 'target_sha256': 'b' * 64,
                      'origin': 'created'}
        self.patches = [
            patch.object(update.worker, 'target_sha', side_effect=lambda snap, _: 'b' * 64 if snap['tag'] == 'before' else 'c' * 64),
            patch.object(update.worker, 'verify_note', return_value='c' * 64),
            patch.object(update.sink, 'snapshot_tree', side_effect=lambda snap: (None, OLD if snap['tag'] == 'before' else NEW, None,
                ['old'] if snap['tag'] == 'before' else ['old', 'new'] if snap['tag'] == 'appended' else ['new'])),
            patch.object(update.sink, 'comparison_semantics', return_value=['new']),
            patch.object(update.executor, 'target', side_effect=lambda snap, *_: {'page_id': PAGE}),
            patch.object(update.executor, 'appended', side_effect=lambda before, after, *_: (NEW, NEW) if after['tag'] == 'appended' else (_ for _ in ()).throw(update.executor.Blocked('readback_mismatch'))),
            patch.object(update.executor, 'archived', side_effect=lambda before, after, *_: None if after['tag'] in ('one_archived', 'body_done') else (_ for _ in ()).throw(update.executor.Blocked('readback_mismatch'))),
            patch.object(update, 'same_page', side_effect=lambda x, y, *_: x['tag'] == y['tag'] and x['properties_raw'] == y['properties_raw']),
            patch.object(update, 'write_api', side_effect=lambda *args: self.api),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def call(self, journal=None, persist=True):
        return update.update(Path('/unused'), ID, PAGE, DATA, PROPS, BLOCKS,
                             SOURCE, 'token', self.api, self.owned, journal,
                             (lambda value: self.saved.append(copy.deepcopy(value))) if persist else None)

    def test_recording_two_roots_replaced_once_and_receipt_returned(self):
        result = self.call()
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(self.api.writes, ['append', 'archive:' + OLD[0],
                                           'archive:' + OLD[1], 'properties'])
        self.assertEqual(result['owned']['source_sha256'], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(result['journal_complete']['phase'], 'complete')
        self.assertEqual(self.saved[0]['phase'], 'append_in_flight')

    def test_resumed_in_flight_append_never_dispatches_again(self):
        self.assertEqual(self.call(persist=False)['status'], 'update_ready')
        with self.assertRaisesRegex(ValueError, 'write_uncertain'):
            self.call({'contract': update.CONTRACT, 'note_id': ID, 'page_id': PAGE,
                       'source_sha256': hashlib.sha256(DATA).hexdigest(),
                       'prior_target_sha256': 'b' * 64, 'phase': 'append_in_flight',
                       'before': snapshot('before'), 'working': snapshot('before'),
                       'old_roots': OLD, 'archive_index': 0})
        self.assertEqual(self.api.writes, [])


    def test_observed_append_resumes_at_archive_without_duplicate_append(self):
        self.call()
        intent = self.saved[0]
        self.api = FakeAPI()
        self.api.state = snapshot('appended')
        result = self.call(intent)
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(self.api.writes, ['archive:' + OLD[0], 'archive:' + OLD[1], 'properties'])

    def test_uncertain_property_write_is_not_repeated(self):
        original = self.api.update_incremental_properties
        def no_effect(*args):
            self.api.writes.append('properties')
        self.api.update_incremental_properties = no_effect
        with self.assertRaisesRegex(ValueError, 'write_uncertain'):
            self.call()
        self.assertEqual(self.api.writes.count('properties'), 1)
        intent = self.saved[-1]
        self.assertEqual(intent['phase'], 'properties_in_flight')
        self.api.update_incremental_properties = original
        with self.assertRaisesRegex(ValueError, 'write_uncertain'):
            self.call(intent)
        self.assertEqual(self.api.writes.count('properties'), 1)

    def test_manual_change_before_reservation_stops_without_write(self):
        self.api.state = snapshot('before', '手工改过')
        with patch.object(update.worker, 'target_sha', return_value='x' * 64):
            with self.assertRaisesRegex(ValueError, 'manual_edit_or_unknown_write'):
                self.call()
        self.assertEqual(self.api.writes, [])

    def test_adopted_page_manual_change_stops_before_journal_or_write(self):
        self.owned['origin'] = 'adopted'
        self.owned['adoption'] = {'contract': 'get-notion-exact-adoption-1',
            'source_commit': 'a' * 40, 'source_blob': 'b' * 40,
            'initial_target_sha256': 'b' * 64,
            'target_snapshot_sha256': 'c' * 64}
        with patch.object(update.executor, 'target_sha', return_value='d' * 64):
            with self.assertRaisesRegex(ValueError, 'manual_edit_or_unknown_write'):
                self.call()
        self.assertEqual(self.saved, [])
        self.assertEqual(self.api.writes, [])

    def test_adopted_origin_and_baseline_survive_successful_update(self):
        self.owned['origin'] = 'adopted'
        self.owned['adoption'] = {'contract': 'get-notion-exact-adoption-1',
            'source_commit': 'a' * 40, 'source_blob': 'b' * 40,
            'initial_target_sha256': 'b' * 64,
            'target_snapshot_sha256': 'c' * 64}
        with patch.object(update.executor, 'target_sha', side_effect=['c' * 64, 'd' * 64]):
            result = self.call()
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(result['owned']['origin'], 'adopted')
        self.assertEqual(result['owned']['adoption']['initial_target_sha256'], 'b' * 64)
        self.assertEqual(result['owned']['adoption']['target_snapshot_sha256'], 'd' * 64)


class NativeRecordingReadback(unittest.TestCase):
    def test_two_foldable_roots_with_children_are_verified_and_archived(self):
        sink = update.sink
        def root(title, child):
            return sink.block('heading_1',
                {'rich_text': [sink.text_run(title)], 'is_toggleable': True},
                [sink.block('heading_2', {'rich_text': [sink.text_run(child)]})])
        old = [root('旧总结', '旧章节'), root('旧原文', '旧内容')]
        new = [root('录音智能总结', '章节概要'), root('原文', '金句精选')]
        ids = ['3' * 32, '7' * 32, '4' * 32, '8' * 32,
               '5' * 32, '9' * 32, '6' * 32, 'a' * 32]
        def block_rows(blocks, selected):
            rows = []
            for index in selected:
                item, child = blocks[index // 2], blocks[index // 2]['children'][0]
                rows.append({'id': ids[index], 'parent_id': PAGE, 'children_ids': [ids[index + 1]],
                             'has_children': True, 'type': item['type'], 'payload': item['payload']})
                rows.append({'id': ids[index + 1], 'parent_id': ids[index], 'children_ids': [],
                             'has_children': False, 'type': child['type'], 'payload': child['payload']})
            return rows
        def native(rows):
            return {'complete': True, 'native_blocks_available': True,
                    'identity_query_complete': True, 'exact_id_matches': [PAGE],
                    'page_id': PAGE, 'parent_data_source': SOURCE, 'id_property': ID,
                    'properties_raw': PROPS, 'last_edited_time': '2026-11-07T00:00:00Z',
                    'recursive_blocks': rows}
        before = native(block_rows(old + new, [0, 2]))
        appended = native(block_rows(old + new, [0, 2, 4, 6]))
        after_one = native(block_rows(old + new, [2, 4, 6]))
        after_two = native(block_rows(old + new, [4, 6]))
        row = {'note_id': ID, 'rendered': new, 'after_anchor': ids[2]}
        self.assertEqual(update.executor.appended(before, appended, row, SOURCE)[0], [ids[4], ids[6]])
        update.executor.archived(appended, after_one, ID, SOURCE, ids[0])
        update.executor.archived(after_one, after_two, ID, SOURCE, ids[2])
        self.assertEqual(sink.snapshot_tree(after_two)[3], sink.comparison_semantics(new))
