"""Synthetic metadata contracts; no real source data, tokens, or network."""
from copy import deepcopy
import unittest

from core import Hold, snapshot
from notion_projection import FIELDS, ProjectedNotionSink, _desired
from notion_sink import NotionSink, plain
from test_core import note
from test_notion_sink import Fake, Journal


class PropertyFake(Fake):
    def __init__(self, journal, business=True):
        super().__init__()
        self.journal = journal
        self.property_patches = []
        self.lose_patch = None
        self.change_after_intent = False
        self.extra_reads = 0
        if business:
            for index, (name, (_, kind)) in enumerate(FIELDS.items()):
                schema = {'id': 'prop-' + str(index), 'type': kind}
                if kind in ('select', 'multi_select'):
                    schema[kind] = {'options': [{'id': 'synthetic-option', 'name': '文字笔记'}] if kind == 'select' else []}
                self.database['properties'][name] = schema

    def request(self, method, path, body=None):
        if method == 'POST' and path == '/v1/pages':
            page = super().request(method, path, body)
            for name, definition in self.database['properties'].items():
                if name in FIELDS:
                    kind = definition['type']
                    self.pages[page['id']]['properties'][name] = {
                        'id': definition['id'], 'type': kind, kind: None if kind == 'select' else []}
            return deepcopy(self.pages[page['id']])
        if method == 'GET' and path.startswith('/v1/pages/'):
            pending = [value.get('pending') for value in self.journal.data.values()
                       if isinstance(value, dict) and value.get('pending') and 'patch' in value['pending']]
            if self.change_after_intent and pending:
                self.change_after_intent = False
                self.pages[path.split('/')[-1]]['properties']['来源']['rich_text'] = [
                    {'type': 'text', 'text': {'content': 'synthetic human value'}}]
        business_patch = (method == 'PATCH' and path.startswith('/v1/pages/') and body
                          and any(name in FIELDS for name in body.get('properties', {})))
        if not business_patch:
            return super().request(method, path, body)
        pending = [value.get('pending') for value in self.journal.data.values()
                   if isinstance(value, dict) and value.get('pending') and 'patch' in value['pending']]
        assert len(pending) == 1 and pending[0]['patch'] == body['properties']
        self.property_patches.append(deepcopy(body['properties']))
        if self.lose_patch == 'before':
            self.lose_patch = None
            raise TimeoutError('synthetic undisclosed result')
        result = super().request(method, path, body)
        if self.lose_patch == 'after':
            self.lose_patch = None
            raise TimeoutError('synthetic response loss')
        return result


class Tests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.http = PropertyFake(self.journal)
        self.text = NotionSink(self.http, 'db', self.journal, namespace='test')
        self.sink = ProjectedNotionSink(self.text, self.http, 'db', self.journal, 'test')
        n = note()
        n.update(tags=[{'id': 'synthetic-id', 'name': 'synthetic tag', 'type': 'user'}],
                 topics=[{'id': 'synthetic-topic', 'name': 'synthetic topic'}],
                 created_at='synthetic original creation', updated_at='synthetic original update',
                 note_type='plain_text', source='synthetic')
        self.snap = snapshot(n)
        self.key = 'projection:notion:db:test:' + self.snap['id']

    def state(self):
        return self.journal.get(self.key)

    def props(self, receipt):
        return self.http.pages[receipt['page_id']]['properties']

    def update(self, **fields):
        n = deepcopy(self.snap['note'])
        n.update(fields)
        self.snap = snapshot(n)

    def test_named_object_tags_and_original_times_project_without_date_inference(self):
        result = self.sink.sync(self.snap)
        props = self.props(result)
        self.assertEqual(props['标签']['multi_select'], [{'name': 'synthetic tag'}])
        self.assertEqual(props['主题']['multi_select'], [{'name': 'synthetic topic'}])
        self.assertEqual(plain(props['创建时间原值']['rich_text']), self.snap['note']['created_at'])
        self.assertEqual(plain(props['更新时间原值']['rich_text']), self.snap['note']['updated_at'])
        self.assertEqual(plain(props['内容类型']['rich_text']), 'plain_text')
        self.assertEqual(plain(props['来源系统']['rich_text']), 'getnote')
        self.assertEqual(props['笔记类型']['select'], {'name': '文字笔记'})
        self.assertEqual(result['projection']['status'], 'verified')
        self.assertNotIn('pending', self.state())
        self.assertEqual(self.snap['note']['tags'][0]['id'], 'synthetic-id')

    def test_no_business_schema_preserves_exact_text_receipt(self):
        journal = Journal()
        http = PropertyFake(journal, business=False)
        text = NotionSink(http, 'db', journal, namespace='test')
        result = ProjectedNotionSink(text, http, 'db', journal, 'test').sync(self.snap)
        self.assertEqual(result, journal.get('notion:db:test:' + self.snap['id'])['receipt'])
        self.assertEqual(http.property_patches, [])
        self.assertFalse(any(method == 'PATCH' and '/databases/' in path for method, path, body in http.calls))

    def test_same_version_repeat_does_not_repeat_property_patch(self):
        result = self.sink.sync(self.snap)
        count = len(self.http.property_patches)
        self.sink.sync(self.snap, result)
        self.assertEqual(len(self.http.property_patches), count)

    def test_owned_fields_follow_source_updates_and_can_clear_explicit_empty_tags(self):
        result = self.sink.sync(self.snap)
        self.update(tags=[], source='updated source')
        changed = self.sink.sync(self.snap, result)
        self.assertEqual(self.props(changed)['标签']['multi_select'], [])
        self.assertEqual(plain(self.props(changed)['来源']['rich_text']), 'updated source')
        self.assertEqual(self.state()['managed']['标签']['value'], [])

    def test_manual_edit_is_preserved_and_safe_sibling_can_update(self):
        result = self.sink.sync(self.snap)
        self.props(result)['来源']['rich_text'] = [{'text': {'content': 'human source'}}]
        self.update(source='source change', tags=[{'name': 'next tag'}])
        with self.assertRaisesRegex(Hold, '^metadata_property_conflict$'):
            self.sink.sync(self.snap, result)
        self.assertEqual(plain(self.props(result)['来源']['rich_text']), 'human source')
        self.assertEqual(self.props(result)['标签']['multi_select'], [{'name': 'next tag'}])
        self.assertEqual(self.state()['projection']['conflicts']['来源'], 'property_manual_edit')

    def test_matching_unowned_value_is_observed_without_ownership_adoption(self):
        text = self.text.sync(self.snap)
        self.props(text)['来源'] = {'rich_text': [{'text': {'content': 'synthetic'}}]}
        result = self.sink.sync(self.snap, text)
        self.assertIn('来源', result['projection']['observed_fields'])
        self.assertNotIn('来源', self.state()['managed'])
        self.update(source='changed')
        with self.assertRaisesRegex(Hold, 'metadata_property_conflict'):
            self.sink.sync(self.snap, result)
        self.assertEqual(plain(self.props(result)['来源']['rich_text']), 'synthetic')

    def test_unknown_nonempty_field_is_never_overwritten_or_cleared(self):
        text = self.text.sync(self.snap)
        self.props(text)['标签'] = {'multi_select': [{'name': 'human tag'}]}
        self.update(tags=[])
        with self.assertRaisesRegex(Hold, 'metadata_property_conflict'):
            self.sink.sync(self.snap, text)
        self.assertEqual(self.props(text)['标签']['multi_select'], [{'name': 'human tag'}])

    def test_missing_source_field_preserves_owned_value(self):
        result = self.sink.sync(self.snap)
        n = deepcopy(self.snap['note'])
        n.pop('tags')
        self.snap = snapshot(n)
        changed = self.sink.sync(self.snap, result)
        self.assertEqual(self.props(changed)['标签']['multi_select'], [{'name': 'synthetic tag'}])
        self.assertEqual(changed['projection']['unprojected']['标签'], 'source_property_missing')

    def test_response_loss_reads_full_patch_and_does_not_repeat_write(self):
        self.http.lose_patch = 'after'
        result = self.sink.sync(self.snap)
        self.assertEqual(result['projection']['status'], 'verified')
        self.sink.sync(self.snap, result)
        self.assertEqual(len(self.http.property_patches), 1)

    def test_unknown_patch_without_visible_result_is_not_retried(self):
        self.http.lose_patch = 'before'
        with self.assertRaisesRegex(Hold, '^metadata_write_requires_reconcile$'):
            self.sink.sync(self.snap)
        self.assertIn('pending', self.state())
        with self.assertRaisesRegex(Hold, '^metadata_write_requires_reconcile$'):
            self.sink.sync(self.snap)
        self.assertEqual(len(self.http.property_patches), 1)
        self.update(source='new revision')
        with self.assertRaisesRegex(Hold, '^metadata_unresolved_prior_revision$'):
            self.sink.sync(self.snap)

    def test_pending_patch_can_reconcile_later_visible_result_without_new_write(self):
        self.http.lose_patch = 'before'
        with self.assertRaises(Hold):
            self.sink.sync(self.snap)
        pending = self.state()['pending']
        self.http.pages[pending['page_id']]['properties'].update(deepcopy(pending['patch']))
        result = self.sink.sync(self.snap)
        self.assertEqual(result['projection']['status'], 'verified')
        self.assertEqual(len(self.http.property_patches), 1)

    def test_change_between_intent_and_dispatch_is_not_overwritten(self):
        self.http.change_after_intent = True
        with self.assertRaisesRegex(Hold, '^metadata_property_conflict$'):
            self.sink.sync(self.snap)
        self.assertEqual(self.http.property_patches, [])
        self.assertNotIn('pending', self.state())
        self.assertEqual(self.state()['conflicts']['来源'], 'property_changed_before_write')

    def test_property_type_or_options_error_only_marks_affected_field(self):
        self.http.database['properties']['主题']['type'] = 'rich_text'
        self.http.database['properties']['笔记类型']['select']['options'] = [{'name': 'other option'}]
        result = self.sink.sync(self.snap)
        self.assertEqual(result['projection']['status'], 'partial')
        self.assertEqual(result['projection']['unprojected']['主题'], 'schema_property_invalid')
        self.assertEqual(result['projection']['unprojected']['笔记类型'], 'note_type_option_unavailable')
        self.assertIn('标签', result['projection']['managed_fields'])
        self.assertNotIn('笔记类型', self.http.property_patches[0])

    def test_unknown_note_type_is_kept_in_rich_text_without_new_select_option(self):
        self.update(note_type='synthetic_unknown_type')
        result = self.sink.sync(self.snap)
        self.assertEqual(plain(self.props(result)['内容类型']['rich_text']), 'synthetic_unknown_type')
        self.assertEqual(self.props(result)['笔记类型']['select'], None)
        self.assertEqual(len(self.http.database['properties']['笔记类型']['select']['options']), 1)

    def test_utf16_chunks_are_bounded_and_overlong_field_is_not_truncated(self):
        self.update(source='🧪' * 3000)
        result = self.sink.sync(self.snap)
        runs = self.props(result)['来源']['rich_text']
        self.assertEqual(len(runs), 3)
        self.assertEqual(plain(runs), self.snap['note']['source'])
        self.assertTrue(all(len(run['text']['content'].encode('utf-16-le')) // 2 <= 2000 for run in runs))
        self.update(source='🧪' * 100001)
        too_long = self.sink.sync(self.snap, result)
        self.assertEqual(too_long['projection']['unprojected']['来源'], 'property_budget')
        self.assertEqual(plain(self.props(result)['来源']['rich_text']), '🧪' * 3000)

    def test_budget_and_invalid_tag_objects_do_not_block_safe_fields(self):
        available = deepcopy(self.http.database['properties'])
        desired, held = _desired({'created_at': 'x' * 200000, 'updated_at': 'x' * 200000,
                                  'tags': [{'missing_name': 'bad'}], 'source': 'ok'},
                                 {name: available[name] for name in FIELDS})
        self.assertEqual(held['标签'], 'source_property_invalid')
        self.assertEqual(held['更新时间原值'], 'property_budget')
        self.assertEqual(plain(desired['来源']['rich_text']), 'ok')

    def test_dates_transcripts_durations_and_relations_are_not_projected(self):
        for name, kind in [('创建时间', 'date'), ('更新时间', 'date'), ('音频转写', 'rich_text'),
                           ('录音总结', 'rich_text'), ('音频时长', 'number'), ('父笔记ID', 'rich_text')]:
            self.http.database['properties'][name] = {'id': 'ignored', 'type': kind}
        self.update(audio={'original': 'PRIVATE_SYNTHETIC_TRANSCRIPT'}, parent_id='synthetic-parent')
        result = self.sink.sync(self.snap)
        self.assertFalse(any(name in patch for patch in self.http.property_patches
                             for name in ('创建时间', '更新时间', '音频转写', '录音总结', '音频时长', '父笔记ID')))
        self.assertEqual(self.snap['note']['audio']['original'], 'PRIVATE_SYNTHETIC_TRANSCRIPT')

    def test_malformed_option_config_only_marks_its_field(self):
        self.http.database['properties']['标签']['multi_select'] = []
        result = self.sink.sync(self.snap)
        self.assertEqual(result['projection']['unprojected']['标签'], 'schema_options_invalid')
        self.assertIn('来源', result['projection']['managed_fields'])

    def test_pending_readback_with_incomplete_property_is_not_accepted(self):
        self.http.lose_patch = 'before'
        with self.assertRaises(Hold):
            self.sink.sync(self.snap)
        pending = self.state()['pending']
        props = self.http.pages[pending['page_id']]['properties']
        props.update(deepcopy(pending['patch']))
        props['来源']['has_more'] = True
        with self.assertRaisesRegex(Hold, 'metadata_write_requires_reconcile'):
            self.sink.sync(self.snap)
        self.assertEqual(len(self.http.property_patches), 1)
        self.assertIn('pending', self.state())

    def test_failed_intent_checkpoint_prevents_property_dispatch(self):
        from core import CheckpointFailure
        original = self.journal.set
        def failed(key, value):
            if key == self.key and value.get('pending'):
                raise CheckpointFailure('synthetic checkpoint failure')
            original(key, value)
        self.journal.set = failed
        with self.assertRaises(CheckpointFailure):
            self.sink.sync(self.snap)
        self.assertEqual(self.http.property_patches, [])

    def test_formatted_human_property_is_preserved(self):
        result = self.sink.sync(self.snap)
        self.props(result)['来源']['rich_text'][0]['annotations'] = {'bold': True}
        self.update(source='changed')
        with self.assertRaisesRegex(Hold, 'metadata_property_conflict'):
            self.sink.sync(self.snap, result)
        self.assertTrue(self.props(result)['来源']['rich_text'][0]['annotations']['bold'])


if __name__ == '__main__':
    unittest.main()
