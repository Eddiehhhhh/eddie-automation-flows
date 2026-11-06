"""The new lane must never create without a durable, run-bound attempt."""
import os
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_incremental_worker as worker


class FakeAPI:
    created = False
    def __init__(self, request, source, ids, **kwargs):
        self.source = source
        self.ids = ids

    def schema(self):
        return {'笔记 ID': {'type': 'rich_text'}, '标题': {'type': 'title'}}

    def matches(self, ident):
        return ['1' * 32] if self.created else []

    def create_page(self, *args):
        type(self).created = True
        return '1' * 32

    def read_note(self, ident):
        return {'page_id': '1' * 32}


class IncrementalWorker(unittest.TestCase):
    def setUp(self):
        FakeAPI.created = False
        self.ident = '1926106578851738352'
        self.commit = 'a' * 40
        self.data = b'fixture'
        self.props = {'笔记 ID': {'type': 'rich_text', 'rich_text': []}}
        self.blocks = [{'type': 'paragraph', 'payload': {'rich_text': []}, 'children': []}]
        self.patches = [patch.object(worker, 'source', return_value=('Raw/03 Get/note.md', self.data, {})),
                        patch.object(worker, 'desired', return_value=(self.props, self.blocks)),
                        patch.object(worker.transport, 'Transport', return_value=object()),
                        patch.object(worker.transport, 'API', FakeAPI)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_read_only_preview_cannot_create(self):
        result = worker.run(Path('/unused'), self.ident, self.commit, 'b' * 32, 'token')
        self.assertEqual(result['status'], 'ready')

    def test_attempt_from_another_run_cannot_create(self):
        reserved = {'note_id': self.ident,
                    'source_sha256': worker.hashlib.sha256(self.data).hexdigest(),
                    'run_id': 'another-run', 'run_attempt': '1', 'status': 'attempt_reserved'}
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}), \
                patch.object(worker, 'attempt_from_main', return_value=reserved):
            with self.assertRaisesRegex(ValueError, 'attempt_not_reserved'):
                worker.run(Path('/unused'), self.ident, self.commit, 'b' * 32, 'token', True)

    def test_exact_reserved_attempt_creates_once_and_reads_back(self):
        reserved = {'note_id': self.ident,
                    'source_sha256': hashlib.sha256(self.data).hexdigest(),
                    'run_id': '123', 'run_attempt': '1', 'status': 'attempt_reserved'}
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}), \
                patch.object(worker, 'attempt_from_main', return_value=reserved), \
                patch.object(worker, 'verify_note', return_value='c' * 64) as verified, \
                patch.object(worker.transport, 'wire_blocks', return_value=[]), \
                patch.object(worker.transport, 'fingerprint', return_value='a' * 64):
            result = worker.run(Path('/unused'), self.ident, self.commit, 'b' * 32, 'token', True)
        self.assertEqual(result['status'], 'created')
        self.assertEqual(result['page_id'], '1' * 32)
        self.assertEqual(result['owned']['target_sha256'], 'c' * 64)
        verified.assert_called_once()

    def test_changed_existing_page_holds_without_any_write(self):
        FakeAPI.created = True
        with patch.object(worker, 'verify_note', side_effect=ValueError('existing_content_differs')):
            with self.assertRaisesRegex(ValueError, 'existing_note_requires_review'):
                worker.run(Path('/unused'), self.ident, self.commit, 'b' * 32, 'token', True)

    def test_source_requires_registered_blob_and_no_unresolved_write(self):
        self.patches[0].stop()
        data = ('---\nnote_id: "' + self.ident + '"\n---\nbody').encode()
        blob = hashlib.sha1(('blob ' + str(len(data)) + '\0').encode() + data).hexdigest()
        path = 'Raw/03 Get/03 纯文本记录/note-' + self.ident + '.md'
        row = {'filePath': path.removeprefix('Raw/03 Get/'), 'baseline_blob': blob}
        results = {
            ('rev-parse', 'HEAD'): (self.commit + '\n').encode(),
            ('remote', 'get-url', 'origin'): ('https://github.com/' + worker.REPO + '.git\n').encode(),
            ('ls-remote', 'origin', 'refs/heads/main'): (self.commit + '\trefs/heads/main\n').encode(),
            ('ls-tree', '-z', 'HEAD', '--', path): ('100644 blob ' + blob + '\t' + path + '\0').encode(),
            ('show', 'HEAD:' + path): data,
            ('show', 'HEAD:' + worker.REGISTRY): json.dumps({'newById': {self.ident: row}}).encode(),
            ('show', 'HEAD:' + worker.STATE): json.dumps({'engine_state': {'unresolved_by_id': {}}}).encode(),
            ('hash-object', '--stdin'): (blob + '\n').encode(),
        }
        with patch.object(worker, 'git', side_effect=lambda root, *args, **kw: results[args]):
            found_path, found_data, found_row = worker.source(Path('/unused'), self.ident, self.commit)
            self.assertEqual((found_path, found_data), (path, data))
            self.assertEqual(found_row['source_metadata'], {'attachments': [], 'timestamps': {}})
            changed = dict(row, baseline_blob='f' * 40)
            results[('show', 'HEAD:' + worker.REGISTRY)] = json.dumps({'newById': {self.ident: changed}}).encode()
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                worker.source(Path('/unused'), self.ident, self.commit)
            results[('show', 'HEAD:' + worker.REGISTRY)] = json.dumps({'newById': {self.ident: row}}).encode()
            results[('show', 'HEAD:' + worker.STATE)] = json.dumps({'engine_state': {'unresolved_by_id': {self.ident: {}}}}).encode()
            with self.assertRaisesRegex(ValueError, 'historical_write_held'):
                worker.source(Path('/unused'), self.ident, self.commit)

    def test_attempt_is_read_from_private_main_not_caller_file(self):
        self.assertEqual(worker.ATTEMPTS, '.sync/get-notion-incremental/attempts')
        reserved = {'note_id': self.ident, 'source_sha256': 'a' * 64,
                    'run_id': '123', 'run_attempt': '1', 'status': 'attempt_reserved'}
        with patch.object(worker, 'git', return_value=json.dumps(reserved).encode()) as read:
            self.assertEqual(worker.attempt_from_main(Path('/private'), self.ident), reserved)
        read.assert_called_once_with(Path('/private'), 'show',
                                     'HEAD:' + worker.ATTEMPTS + '/' + self.ident + '.json')
        with patch.object(worker, 'git', return_value=b'{"note_id":"unbound"}'):
            with self.assertRaisesRegex(ValueError, 'attempt_not_reserved'):
                worker.attempt_from_main(Path('/private'), self.ident)

    def test_recording_projects_five_fields_and_foldable_hierarchy(self):
        self.patches[1].stop()
        body = ('## AI 总结\n### 📑 智能总结\n#### 录音信息\n'
                '- **录音时间**：2026-11-04 08:19:11 ~ 2026-11-04 10:35:02\n'
                '- **时长**：约 2 小时 15 分钟\n- **参与人数**：约 2 人\n'
                '- **内容类型**：个人日常\n#### 录音总结\n\n这是摘要。\n'
                '### 📅 章节概要\n章节。\n### ✨ 金句精选\n金句。\n## 原文\n逐字稿。')
        data = ('---\nnote_id: "' + self.ident + '"\nnote_type: "recorder_audio"\n---\n' + body).encode()
        schema = {name: {'type': 'rich_text'} for name in ('录音时间', '时长', '参与人数', '内容类型', '录音总结')}
        with patch('get_notion_credential_projection.render_body', return_value=body):
            fields = worker.recording_properties(data, self.ident, schema)
        self.assertEqual(set(fields), set(schema))
        self.assertEqual(fields['录音总结']['rich_text'][0]['text']['content'], '这是摘要。')
        blocks = [worker.sink.block('heading_2', {'rich_text': [worker.sink.text_run('AI 总结')]}),
                  worker.sink.block('heading_3', {'rich_text': [worker.sink.text_run('📑 智能总结')]}),
                  worker.sink.block('heading_4', {'rich_text': [worker.sink.text_run('录音信息')]}),
                  worker.sink.block('paragraph', {'rich_text': [worker.sink.text_run('正文')]}),
                  worker.sink.block('heading_2', {'rich_text': [worker.sink.text_run('原文')]}),
                  worker.sink.block('paragraph', {'rich_text': [worker.sink.text_run('逐字稿')]}),]
        folded = worker.recording_blocks(blocks)
        self.assertEqual([b['payload']['rich_text'][0]['text']['content'] for b in folded], ['录音智能总结', '原文'])
        self.assertEqual([len(b['children']) for b in folded], [2, 1])
        self.assertTrue(all(b['payload']['is_toggleable'] for b in folded))
        self.assertEqual(len(worker.transport.wire_blocks(folded)), 2)

    def test_recording_readback_compares_five_text_fields_not_titles(self):
        page = '1' * 32
        five = {name: {'type': 'rich_text', 'rich_text': [worker.sink.text_run(name)]}
                for name in worker.RECORDING_FIELDS}
        props = {'笔记 ID': {'type': 'rich_text', 'rich_text': [worker.sink.text_run(self.ident)]},
                 '标题': {'type': 'title', 'title': [worker.sink.text_run('录音卡')]}, **five}
        actual = {'笔记 ID': props['笔记 ID'],
                  '标题': {'type': 'title', 'title': [{'plain_text': '录音卡'}]}, **five}
        api = FakeAPI(None, 'b' * 32, [self.ident])
        api.read_note = lambda ident: {'page_id': page, 'properties_raw': actual}
        with patch.object(worker.sink, 'snapshot_tree', return_value=({}, [], [], [])):
            fingerprint = worker.verify_note(api, self.ident, page, props, [])
        self.assertRegex(fingerprint, r'^[a-f0-9]{64}$')


if __name__ == '__main__':
    unittest.main()
