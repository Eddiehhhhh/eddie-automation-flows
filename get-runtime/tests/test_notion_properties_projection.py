import json
import hashlib
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
NOTE_ID = '1234567890'
PAGE_ID = '11111111111141118111111111111111'
SOURCE_ID = '22222222222242228222222222222222'
_PREEXISTING_MODULES = set(sys.modules)

# These pure projections do not need private production bindings. The runtime
# imports the pinned bundle in production; tests supply inert structural IDs.
bindings = types.ModuleType('get_private_runtime_bindings')
def binding_value(module, name):
    if module == 'get_notion_sink_plan': return '0' * 64
    if module == 'get_notion_content_scope':
        return {'REVIEWED': [], 'NEWLY_REVIEWED': [], 'HELD': [],
                'EMPTY_SHA256': '0' * 64}.get(name, '0' * 64)
    return '0' * 64
bindings.value = binding_value

# Keep transport tests focused on the request method without importing the
# production executor's private runtime dependency closure.
native_stub = types.ModuleType('get_notion_native')
native_stub.uuid = lambda value: value
native_stub.sha = lambda value: hashlib.sha256(json.dumps(
    value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
native_stub.encoded = lambda value: json.dumps(value).encode()
native_stub.note_id = lambda value: value
native_stub.DATA_SOURCE_PIN = SOURCE_ID
native_stub.DATABASE_PIN = SOURCE_ID
readonly_stub = types.ModuleType('get_notion_readonly')
readonly_stub.read_route = lambda method, path: True
readonly_stub.MAX_RESPONSE = 1_000_000
readonly_stub.MAX_HTTP_BYTES = 10_000_000
readonly_stub.NoRedirect = object
mirror_stub = types.ModuleType('get_mirror_incremental_replay')
mirror_stub.strict_capture_json = lambda *args, **kwargs: {}
_STUBS = {'get_private_runtime_bindings': bindings, 'get_notion_native': native_stub,
          'get_notion_readonly': readonly_stub, 'get_mirror_incremental_replay': mirror_stub}
_ORIGINALS = {name: sys.modules.get(name) for name in _STUBS}
sys.modules.update(_STUBS)
try:
    import get_notion_properties as props
    import get_notion_live_transport as transport
    import get_notion_incremental_properties as incremental
    import get_notion_metadata_pending as metadata_pending
    import get_notion_property_transaction as property_transaction
finally:
    for name, original in _ORIGINALS.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


def tearDownModule():
    for name in ('get_notion_properties', 'get_notion_live_transport',
                 'get_notion_incremental_properties', 'get_notion_metadata_pending',
                 'get_notion_sink_plan'):
        if name not in _PREEXISTING_MODULES:
            sys.modules.pop(name, None)


def schema(options=None):
    options = options or {}
    result = {}
    for index, (name, (_, kind)) in enumerate(props.FIELDS.items(), 1):
        item = {'id': 'prop-' + str(index), 'type': kind}
        if kind in ('select', 'multi_select'):
            item[kind] = {'options': [
                {'id': 'opt-' + str(i), 'name': value}
                for i, value in enumerate(options.get(name, []), 1)]}
        result[name] = item
    return result


def canonical_note(**updates):
    fields = {
        'source_system': 'getnote', 'note_id': NOTE_ID, 'title': '录音卡',
        'note_type': 'recorder_audio', 'source': '录音卡',
        'created_at': '2026-11-05T13:14:15+08:00',
        'updated_at': '2026-11-05T13:16:17+08:00',
        'tags': ['新标签'], 'topics': ['新主题'], 'is_child_note': False,
        'parent_id': '1234567891', 'children_ids': ['1234567892'],
        'share_id': 'share-id',
        'web_url': 'https://example.invalid/source', 'web_domain': 'example.invalid',
        'web_excerpt': '网页摘录', 'audio_url': 'https://example.invalid/audio.mp3',
        'audio_duration': 12.5, 'audio_transcript': '逐字稿',
        'attachments': ['https://example.invalid/attachment.pdf'],
    }
    fields.update(updates)
    return ('---\n' + ''.join(
        key + ': ' + json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'
        for key, value in fields.items()) + '---\n正文保留可读').encode()


def project(data, ident, projection, timestamps=None, source_metadata=None):
    if source_metadata is None:
        source_metadata = {'attachments': [{
            'type': 'audio', 'url': 'https://example.invalid/audio.mp3',
            'title': 'sample audio', 'duration': 8151120}]}
    return props.project(data, ident, projection, timestamps, source_metadata)


class NotionPropertyProjection(unittest.TestCase):
    def test_registry_attachment_metadata_uses_exact_verified_source_row(self):
        raw = json.dumps({'newById': {NOTE_ID: {
            'baseline_blob': 'b' * 40, 'mirror_sha256': 'c' * 64,
            'filePath': '01 语音记录/录音卡.md',
            'source_metadata': {'attachments': [{
                'type': 'audio', 'url': 'https://example.invalid/audio.mp3',
                'title': 'sample audio', 'duration': 8151120}]},
        }}}, separators=(',', ':')).encode()
        registry_blob = 'a' * 40

        class Engine:
            class P:
                git_blob = staticmethod(lambda data: registry_blob)
            p = P()
            digest = staticmethod(lambda data: __import__('hashlib').sha256(data).hexdigest())
            def fail(self, reason): raise ValueError(reason)

        class Repo:
            read = staticmethod(lambda commit, path, limit: raw)

        class Publication:
            descriptor = {'commit': 'd' * 40,
                          'content_tree': {props.REGISTRY: registry_blob}}
            repo = Repo()
            check_assets = staticmethod(lambda assets: None)

        selected = {NOTE_ID: {'blob': 'b' * 40, 'sha256': 'c' * 64,
                              'path': 'Raw/03 Get/01 语音记录/录音卡.md'}}
        binding = {'path': props.REGISTRY, 'blob': registry_blob,
                   'sha256': Engine.digest(raw)}
        with patch.object(property_transaction, 'engine', return_value=Engine()):
            _, source_metadata = property_transaction.registry_source_values(
                Publication(), binding, selected)
        self.assertEqual(source_metadata[NOTE_ID]['attachments'][0]['duration'], 8151120)

    def test_all_canonical_source_attributes_have_typed_projection(self):
        actual_schema = schema()
        projection = props.schema_projection(actual_schema)
        self.assertEqual(set(projection), set(props.FIELDS))
        desired, held = project(canonical_note(), NOTE_ID, projection)
        self.assertEqual(held, {})
        self.assertEqual(set(desired), set(props.FIELDS))
        self.assertEqual(desired['笔记类型'], {'type': 'select', 'select': {'name': 'recorder_audio'}})
        self.assertEqual(desired['标签']['multi_select'], [{'name': '新标签'}])
        self.assertEqual(desired['主题']['multi_select'], [{'name': '新主题'}])
        self.assertFalse(desired['是否子笔记']['checkbox'])
        self.assertEqual(desired['音频时长']['number'], 12.5)
        self.assertEqual(desired['附件链接']['rich_text'][0]['text']['content'],
                         '["https://example.invalid/attachment.pdf"]')
        self.assertEqual(desired['附件原始属性']['rich_text'][0]['text']['content'],
                         '[{"type":"audio","url":"https://example.invalid/audio.mp3","title":"sample audio","duration":8151120}]')
        self.assertEqual(props.value(desired['创建时间原值'])['rich_text'][0]['text']['content'],
                         '2026-11-05T13:14:15+08:00')
        self.assertEqual(props.value(desired['创建时间'])['date']['start'],
                         '2026-11-05T05:14:15+00:00')

    def test_select_option_growth_does_not_change_schema_identity(self):
        before = props.schema_projection(schema({'标签': ['existing'], '主题': ['topic']}))
        after = props.schema_projection(schema({'标签': ['existing', 'new'], '主题': ['topic', 'next']}))
        self.assertEqual(before, after)
        desired, held = project(canonical_note(), NOTE_ID, before)
        self.assertEqual(held, {})
        self.assertEqual(desired['标签']['multi_select'], [{'name': '新标签'}])
        self.assertEqual(desired['主题']['multi_select'], [{'name': '新主题'}])

    def test_multiselect_comma_and_unknown_note_type_are_held(self):
        desired, held = project(canonical_note(tags=['bad,tag']), NOTE_ID,
                                props.schema_projection(schema()))
        self.assertNotIn('标签', desired)
        self.assertEqual(held['标签'], 'property_source_invalid')
        desired, held = project(canonical_note(note_type='unexpected'), NOTE_ID,
                                props.schema_projection(schema()))
        self.assertNotIn('笔记类型', desired)
        self.assertEqual(held['笔记类型'], 'property_source_invalid')

    def test_explicit_empty_values_project_and_only_managed_values_clear(self):
        projection = props.schema_projection(schema())
        desired, held = project(canonical_note(tags=[], source='', note_type='',
            web_url='', audio_duration=None, children_ids=[]), NOTE_ID, projection)
        self.assertEqual(held, {})
        self.assertEqual(desired['标签']['multi_select'], [])
        self.assertEqual(desired['来源']['rich_text'], [])
        self.assertIsNone(desired['笔记类型']['select'])
        self.assertIsNone(desired['网页链接']['url'])
        self.assertIsNone(desired['音频时长']['number'])
        self.assertEqual(desired['子笔记ID']['rich_text'], [])
        current = {'来源': {'type': 'rich_text', 'rich_text': [
            {'type': 'text', 'text': {'content': 'old'}}]}}
        managed = {'来源': props.value(current['来源'])}
        patch, _, held = props.three_way({'来源': desired['来源']}, current, managed,
            fill_empty=True)
        self.assertEqual(patch, {'来源': desired['来源']})
        self.assertEqual(held, {})
        patch, _, held = props.three_way({'来源': desired['来源']}, current, {}, fill_empty=True)
        self.assertEqual(patch, {})
        self.assertEqual(held['来源'], 'property_unowned_value')

    def test_33kb_transcript_maps_and_facet_completes(self):
        import hashlib
        data = canonical_note(audio_transcript='转写' * 17000)
        projection = props.schema_projection(schema())
        desired, held = project(data, NOTE_ID, projection)
        self.assertIn('音频转写', desired)
        self.assertNotIn('音频转写', held)
        self.assertIn('标签', desired)
        self.assertIn('来源系统', desired)

        class Engine:
            MAX_REQUEST_BYTES = 400 * 1024
            class P:
                text_run = staticmethod(lambda text: {'type': 'text', 'text': {'content': text}})
            p = P()
            digest = staticmethod(lambda value: hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
            def fail(self, reason): raise ValueError(reason)
        class Publication:
            descriptor = {'records': {NOTE_ID: {'blob': 'b' * 40}}, 'commit': 'a' * 40}
        metadata = (projection, {}, {NOTE_ID: {'attachments': []}}, {NOTE_ID: {}}, {'schema_sha256': 'c' * 64,
            'registry': None})
        with patch.object(metadata_pending, 'engine', return_value=Engine()):
            facet = metadata_pending.facet(Publication(), NOTE_ID, data,
                {'title_property': '标题'}, metadata, {'property_receipts': {},
                    'metadata_results': {}})
        self.assertIn('音频转写', facet['desired'])
        self.assertEqual(facet['desired']['附件原始属性']['rich_text'][0]['text']['content'], '[]')
        self.assertNotIn('音频转写', facet['held'])
        self.assertIn('标签', facet['desired'])

    def test_creation_fields_receives_registry_attachment_values(self):
        metadata = (props.schema_projection(schema()), {},
            {NOTE_ID: {'attachments': [{'type': 'audio', 'duration': 8151120}]}},
            {NOTE_ID: {}}, {'schema_sha256': 'c' * 64, 'registry': None})
        desired, held, schema_sha, assets = property_transaction.creation_fields(
            None, NOTE_ID, canonical_note(), metadata)
        self.assertIn('附件原始属性', desired)
        self.assertEqual(desired['附件原始属性']['rich_text'][0]['text']['content'],
                         '[{"type":"audio","duration":8151120}]')
        self.assertEqual(held, {})
        self.assertEqual(schema_sha, 'c' * 64)
        self.assertEqual(assets, {})

    def test_total_property_budget_holds_only_overflow_field(self):
        data = canonical_note(source='a' * 150_000, web_excerpt='b' * 150_000,
            audio_transcript='c' * 150_000)
        desired, held = project(data, NOTE_ID, props.schema_projection(schema()))
        self.assertLessEqual(len(props.p.encoded(desired)), props.MAX_PROPERTIES_BYTES)
        self.assertIn('来源', desired)
        self.assertIn('网页摘要', desired)
        self.assertNotIn('音频转写', desired)
        self.assertEqual(held['音频转写'], 'property_budget')

    def test_missing_empty_source_stays_held(self):
        data = canonical_note().replace('tags: ["新标签"]\n'.encode(), b'')
        desired, held = project(data, NOTE_ID,
            props.schema_projection(schema()))
        self.assertNotIn('标签', desired)
        self.assertEqual(held['标签'], 'property_source_missing')

    def test_attachment_metadata_requires_verified_registry_value(self):
        desired, held = props.project(canonical_note(), NOTE_ID,
            props.schema_projection(schema()))
        self.assertNotIn('附件原始属性', desired)
        self.assertEqual(held['附件原始属性'], 'property_source_missing')

    def test_option_refusal_requires_exact_403_and_unchanged_readback(self):
        marker = props.confirmed_option_refusal('property_option_permission_denied', 403,
            'f' * 64, 'f' * 64, 'a' * 64, 'a' * 64)
        self.assertEqual(marker, {'reason': 'property_option_permission_denied',
            'status': 403, 'request_fingerprint': 'f' * 64})
        self.assertIsNone(props.confirmed_option_refusal('property_option_permission_denied',
            403, 'f' * 64, 'f' * 64, 'a' * 64, 'b' * 64))
        self.assertIsNone(props.confirmed_option_refusal('permission_denied',
            403, 'f' * 64, 'f' * 64, 'a' * 64, 'a' * 64))

    def test_incremental_schema_guard_allows_option_growth_only(self):
        base = schema({'标签': ['旧标签'], '主题': ['旧主题']})
        bound = {'schema_sha256': props.p.digest(props.schema_projection(base)),
            'native_schema_sha256': props.p.digest(props.native_schema_projection(base))}
        current = schema({'标签': ['旧标签', '新标签'], '主题': ['旧主题', '新主题']})
        class Engine:
            digest = staticmethod(lambda value: props.p.digest(value))
        class API:
            def schema(self): return current
        with patch.object(incremental, 'engine', return_value=Engine()):
            self.assertTrue(incremental.same_schema(API(), bound))
            current['标签']['id'] = 'different-column-id'
            self.assertFalse(incremental.same_schema(API(), bound))
            current['标签']['id'] = base['标签']['id']
            current['未跟踪列'] = {'id': 'extra', 'type': 'rich_text'}
            self.assertFalse(incremental.same_schema(API(), bound))
            current.pop('未跟踪列')
            current['标签']['multi_select']['sort_order'] = 'changed'
            self.assertFalse(incremental.same_schema(API(), bound))

    def test_option_refusal_becomes_held_without_claiming_ownership(self):
        attempted = {'patch': {'标签': {'type': 'multi_select',
                'multi_select': [{'name': '新标签'}]}},
            'held': {'主题': 'property_source_missing'},
            'request_fingerprint': 'f' * 64, 'managed': {'来源': 'ignored'}}
        prior = {'managed': {'来源': {'type': 'rich_text', 'rich_text': []}}}
        held = incremental.option_refusal_intent(attempted, prior)
        self.assertEqual(held['patch'], {})
        self.assertIsNone(held['date_wire'])
        self.assertEqual(held['managed'], prior['managed'])
        self.assertEqual(held['held'], {'主题': 'property_source_missing',
            '标签': 'property_option_permission_denied'})
        self.assertEqual(held['request_fingerprint'], 'f' * 64)

    def test_same_source_and_unchanged_target_keep_option_refusal_across_plans(self):
        prior = {'md_blob': 'b' * 40, 'managed': {},
            'held': {'标签': 'property_option_permission_denied'}}
        desired = {'标签': {'type': 'multi_select', 'multi_select': [{'name': '新标签'}]}}
        row = {'note_id': NOTE_ID, 'metadata_completion': {
            'md_blob': 'b' * 40, 'desired': desired, 'held': {},
            'title_property': '标题', 'date_wire': None}}
        plan = {'policy': {'data_source_id': SOURCE_ID}}
        target = {'page_id': PAGE_ID, 'properties': {
            '标签': {'type': 'multi_select', 'multi_select': []}}}
        class Engine:
            transport = transport
            target = staticmethod(lambda snapshot, ident, source: snapshot)
        with patch.object(incremental, 'engine', return_value=Engine()):
            first = incremental.outcome(row, plan, target, prior, True,
                {'标签': target['properties']['标签']})
            second = incremental.outcome(row, plan, target, prior, True,
                {'标签': target['properties']['标签']})
            self.assertEqual(first['patch'], {})
            self.assertEqual(second['patch'], {})
            self.assertEqual(second['held']['标签'], 'property_option_permission_denied')
            row['metadata_completion']['md_blob'] = 'd' * 40
            changed_source = incremental.outcome(row, plan, target, prior, True,
                {'标签': target['properties']['标签']})
            self.assertIn('标签', changed_source['patch'])
            self.assertNotIn('标签', changed_source['held'])
            row['metadata_completion']['md_blob'] = 'b' * 40
            edited_target = {'page_id': PAGE_ID, 'properties': {'标签': desired['标签']}}
            changed_target = incremental.outcome(row, plan, edited_target, prior, True,
                {'标签': target['properties']['标签']})
            self.assertEqual(changed_target['patch'], {})
            self.assertNotIn('标签', changed_target['held'])
            refreshed_baseline = incremental.outcome(row, plan, edited_target, prior, True,
                {'标签': desired['标签']})
            self.assertEqual(refreshed_baseline['patch'], {})
            self.assertNotIn('标签', refreshed_baseline['held'])

    def test_legacy_metadata_result_is_readable_but_not_enabled_as_v2(self):
        class Engine:
            class P:
                note_id = staticmethod(lambda value: value)
                hex_hash = staticmethod(lambda value, width: value)
                notion_id = staticmethod(lambda value: value)
            p = P()
            def fail(self, reason): raise ValueError(reason)
        legacy = {'contract': metadata_pending.LEGACY_CONTRACT,
            'source_commit': 'a' * 40, 'md_blob': 'b' * 40,
            'page_id': PAGE_ID, 'data_source_id': SOURCE_ID,
            'plan_sha256': 'c' * 64, 'facet_sha256': 'd' * 64,
            'properties_sha256': 'e' * 64,
            'held': {'创建时间': 'property_source_empty', '更新时间': 'metadata_sync_pending',
                '标签': 'property_source_invalid', '笔记类型': 'property_manual_edit',
                '旧标题字段': 'title_ownership_required'},
            'readback_verified': True}
        with patch.object(metadata_pending, 'engine', return_value=Engine()):
            metadata_pending.check_results({NOTE_ID: legacy})
            with self.assertRaises(ValueError):
                metadata_pending.check_results({NOTE_ID: {**legacy,
                    'held': {**legacy['held'], '额外字段': 'property_source_empty'}}})
        self.assertFalse(metadata_pending.enabled({'metadata_completion_contract':
            metadata_pending.LEGACY_CONTRACT}))
        self.assertTrue(metadata_pending.enabled({'metadata_completion_contract':
            metadata_pending.CONTRACT}))

    def test_date_wire_truncates_only_native_date_and_keeps_exact_shadow(self):
        desired, held = project(canonical_note(), NOTE_ID,
                                      props.schema_projection(schema()))
        self.assertEqual(held, {})
        date_properties = {'创建时间': desired['创建时间']}
        wire = props.date_wire_properties(date_properties, props.date_wire_binding(date_properties))
        self.assertEqual(wire['创建时间']['date']['start'], '2026-11-05T05:14:00.000Z')
        self.assertEqual(desired['创建时间原值']['rich_text'][0]['text']['content'],
                         '2026-11-05T13:14:15+08:00')

    def test_legacy_four_field_receipts_remain_readable(self):
        receipt = {'contract': props.LEGACY_CONTRACT, 'page_id': PAGE_ID,
            'data_source_id': SOURCE_ID, 'source_commit': 'a' * 40,
            'md_blob': 'b' * 40, 'manifest_sha256': 'c' * 64,
            'managed': {}, 'held': {'标签': 'property_option_missing'},
            'readback_verified': True}
        self.assertEqual(props.check_receipts({NOTE_ID: receipt}), {NOTE_ID: receipt})


class MultiSelectOptionRefusal(unittest.TestCase):
    def test_database_write_permission_refusal_has_exact_reason_and_fingerprint(self):
        request = transport.API.__new__(transport.API)
        request.known_pages = {PAGE_ID}
        request.source = SOURCE_ID
        request.schema = lambda: schema()

        def refuse(method, path, body):
            key = transport.fingerprint(method, path, body)
            raise transport.WriteFailure('permission_denied', 403, key)

        request._write = refuse
        with self.assertRaises(transport.WriteFailure) as caught:
            request.update_properties(PAGE_ID, {'标签': {'type': 'multi_select',
                'multi_select': [{'name': '新标签'}]}})
        self.assertEqual(str(caught.exception), 'property_option_permission_denied')
        self.assertEqual(caught.exception.status, 403)
        expected = transport.fingerprint('PATCH', '/pages/' + PAGE_ID,
            {'properties': {'标签': {'type': 'multi_select',
                'multi_select': [{'name': '新标签'}]}}})
        self.assertEqual(caught.exception.request_fingerprint, expected)

    def test_general_403_without_new_option_stays_permission_denied(self):
        request = transport.API.__new__(transport.API)
        request.known_pages = {PAGE_ID}
        request.source = SOURCE_ID
        request.schema = lambda: schema({'标签': ['已有']})

        def refuse(method, path, body):
            raise transport.WriteFailure('permission_denied', 403,
                transport.fingerprint(method, path, body))

        request._write = refuse
        with self.assertRaises(transport.WriteFailure) as caught:
            request.update_properties(PAGE_ID, {'标签': {'type': 'multi_select',
                'multi_select': [{'name': '已有'}]}})
        self.assertEqual(str(caught.exception), 'permission_denied')

    def test_live_transport_accepts_explicit_empty_select_and_multiselect(self):
        request = transport.API.__new__(transport.API)
        request.known_pages = {PAGE_ID}
        request.source = SOURCE_ID
        request.schema = lambda: schema()
        calls = []
        request._write = lambda method, path, body: (calls.append((method, path, body)) or {
            'object': 'page', 'id': PAGE_ID,
            'parent': {'data_source_id': SOURCE_ID}})
        request.update_properties(PAGE_ID, {
            '标签': {'type': 'multi_select', 'multi_select': []},
            '笔记类型': {'type': 'select', 'select': None}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]['properties']['标签']['multi_select'], [])
        self.assertIsNone(calls[0][2]['properties']['笔记类型']['select'])

    def test_property_wire_accepts_explicit_empty_values(self):
        import get_notion_property_transaction as transaction
        class Engine:
            MAX_REQUEST_BYTES = 400 * 1024
            class P:
                notion_id = staticmethod(lambda value: value)
            p = P()
            encoded = staticmethod(lambda value: json.dumps(value,
                sort_keys=True, separators=(',', ':')).encode())
            def fail(self, reason): raise ValueError(reason)
        projected, _ = project(canonical_note(audio_transcript='x' * 33000),
            NOTE_ID, props.schema_projection(schema()))
        row = {'property_only': True, 'note_id': NOTE_ID, 'page_id': PAGE_ID,
            'property_patch': {'标签': {'type': 'multi_select', 'multi_select': []},
                '笔记类型': {'type': 'select', 'select': None},
                '音频转写': projected['音频转写']}}
        with patch.object(transaction, 'engine', return_value=Engine()):
            spec = transaction.wire(row)
        self.assertEqual(spec['body']['properties']['标签']['multi_select'], [])
        self.assertIsNone(spec['body']['properties']['笔记类型']['select'])
        self.assertLess(len(json.dumps(spec['body'], ensure_ascii=False).encode()), 400_000)

    def test_property_transaction_persists_refusal_without_ack_or_ownership_claim(self):
        import get_notion_property_transaction as transaction
        import hashlib

        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True,
                separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

        class Engine:
            transport = transport
            MAX_REQUEST_BYTES = 400 * 1024
            class P:
                notion_id = staticmethod(lambda value: value)
                writer_gate = staticmethod(lambda value: True)
            p = P()
            class Guard:
                held = staticmethod(lambda *args: False)
            plan_recovery = Guard()
            class Scope:
                reason = staticmethod(lambda *args: None)
            content_scope = Scope()
            def digest(self, value): return digest(value)
            def encoded(self, value): return json.dumps(value, sort_keys=True,
                separators=(',', ':'), ensure_ascii=False).encode()
            def target_sha(self, snapshot, ident, source): return digest(snapshot)
            def read_target(self, api, ident, source): return api.read_note(ident)
            def fail(self, reason): raise ValueError(reason)

        actual_schema = schema()
        tag_value = {'type': 'multi_select', 'multi_select': [{'name': '新标签'}]}
        source = {'blob': 'b' * 40, 'path': 'Raw/03 Get/01 Notes/test.md', 'sha256': 'c' * 64}
        snapshot = {'stable': 'pre-write'}
        prior_managed = {'标签': {'type': 'multi_select', 'multi_select': [{'name': '旧标签'}]}}
        attempted_managed = {'标签': props.value(tag_value)}
        state = {'journal': {'index': 0, 'operation': None}, 'receipts': {},
            'property_receipts': {NOTE_ID: {'managed': prior_managed}}}

        class Store:
            def __init__(self): self.version = digest(state)
            def read(self): return state, self.version
            def compare_and_set(self, expected, value):
                if expected != self.version: raise AssertionError('bad CAS')
                self.version = digest(value)
                return self.version

        class Publication:
            descriptor = {'commit': 'a' * 40}
            def check(self, descriptor): pass
            def check_assets(self, assets): pass
            def read(self, ident): return b'source'

        class API:
            def schema(self): return actual_schema
            def read_note(self, ident): return snapshot
            def update_properties(self, page, properties):
                key = transport.fingerprint('PATCH', '/pages/' + page,
                    {'properties': properties})
                raise transport.WriteFailure('property_option_permission_denied', 403, key)

        schema_sha = props.p.digest(props.schema_projection(actual_schema))
        manifest = {'schema_sha256': schema_sha}
        plan = {'property_only': True, 'property_manifest': manifest,
            'property_manifest_sha256': digest(manifest),
            'policy': {'data_source_id': SOURCE_ID},
            'publication': Publication.descriptor}
        row = {'property_only': True, 'note_id': NOTE_ID, 'page_id': PAGE_ID,
            'source': source, 'source_assets': {}, 'property_patch': {'标签': tag_value},
            'property_managed': attempted_managed, 'held_fields': {},
            'property_schema_sha256': schema_sha,
            'before_sha256': digest(snapshot)}
        counts = {'write_calls': 0, 'retained_conflicts': 0, 'updated': 0, 'noop': 0}
        with patch.object(transaction, 'engine', return_value=Engine()):
            transaction.execute_row(row, plan, Publication(), API(), Store(), state,
                digest(state), counts, lambda: {})
        receipt = state['property_receipts'][NOTE_ID]
        self.assertEqual(receipt['managed'], prior_managed)
        self.assertEqual(receipt['held'], {'标签': 'property_option_permission_denied'})
        self.assertIsNone(state['journal']['operation'])
        self.assertEqual(state['journal']['index'], 1)
        self.assertEqual(counts['write_calls'], 1)
        self.assertEqual(counts['retained_conflicts'], 1)

    def test_incremental_finish_persists_403_refusal_without_unbound_response(self):
        import get_notion_property_transaction as transaction
        import hashlib

        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True,
                separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

        before = {'page_id': PAGE_ID, 'properties': {'标签': {'type': 'multi_select', 'multi_select': []}}}
        patch_value = {'标签': {'type': 'multi_select', 'multi_select': [{'name': '新标签'}]}}
        attempted = {'patch': patch_value, 'date_wire': None,
            'request_fingerprint': 'f' * 64, 'managed': {}, 'held': {}, 'schema_ok': True}
        body = {'note_id': NOTE_ID, 'page_id': PAGE_ID, 'data_source_id': SOURCE_ID,
            'source_commit': 'a' * 40, 'md_blob': 'b' * 40, 'managed_blocks': [],
            'properties_baseline': before['properties']}
        bound = {'contract': metadata_pending.CONTRACT, 'source_commit': 'a' * 40,
            'md_blob': 'b' * 40, 'title_property': '标题', 'desired': patch_value,
            'held': {}, 'required_fields': [], 'schema_sha256': 'c' * 64,
            'native_schema_sha256': None, 'source_assets': {}}
        operation = {'kind': incremental.KIND, 'phase': 'prepared', 'body_receipt': body,
            'before': before, 'prior_properties': None, 'plan_sha256': 'd' * 64,
            'facet_sha256': 'e' * 64, 'intent': attempted, 'response': None}
        value = {'journal': {'operation': operation}, 'receipts': {NOTE_ID: body},
            'property_receipts': {}}
        version = digest(value)
        source = {'blob': 'b' * 40, 'path': 'Raw/03 Get/01 Notes/test.md', 'sha256': 'c' * 64}
        row = {'note_id': NOTE_ID, 'action': 'update', 'source': source,
            'metadata_completion': bound, 'source_assets': {}, 'rendered': []}
        plan = {'publication': {'commit': 'a' * 40},
            'policy': {'data_source_id': SOURCE_ID}, 'property_incremental_scope_sha256': 'f' * 64}

        class Engine:
            transport = transport
            REASONS = frozenset()
            class P:
                writer_gate = staticmethod(lambda value: True)
            p = P()
            class Guard:
                held = staticmethod(lambda *args: False)
            plan_recovery = Guard()
            class Scope:
                reason = staticmethod(lambda *args: None)
            content_scope = Scope()
            def digest(self, obj): return digest(obj)
            def target_sha(self, snapshot, ident, data_source): return digest(snapshot)
            def read_target(self, api, ident, data_source): return api.read_note(ident)
            def target(self, snapshot, ident, data_source): return snapshot
            def fail(self, reason): raise ValueError(reason)

        class Store:
            current_version = version
            def read(self): return value, self.current_version
            def compare_and_set(self, expected, obj):
                if expected != self.current_version: raise AssertionError('bad CAS')
                self.current_version = digest(obj)
                return self.current_version

        class Publication:
            def check(self, descriptor): pass
            def check_assets(self, assets): pass
            def read(self, ident): return b'source'

        class API:
            def read_note(self, ident): return before
            def update_properties(self, page, properties):
                key = transport.fingerprint('PATCH', '/pages/' + page,
                    {'properties': properties})
                raise transport.WriteFailure('property_option_permission_denied', 403, key)

        spec = {'method': 'PATCH', 'path': '/pages/' + PAGE_ID,
            'body': {'properties': patch_value}}
        counts = {'body_verified': 0, 'metadata_pending': 0, 'write_calls': 0,
            'properties_updated': 0, 'retained_conflicts': 0}
        with (patch.object(incremental, 'engine', return_value=Engine()),
              patch.object(incremental, 'check_plan'),
              patch.object(incremental, 'validate_operation'),
              patch.object(incremental, 'outcome', return_value=attempted),
              patch.object(incremental, 'same_schema', return_value=True),
              patch.object(incremental.tx, 'wire', return_value=spec),
              patch.object(incremental.tx, 'verify_after', return_value=before)):
            incremental.finish(row, plan, Publication(), API(), Store(), value,
                version, counts, lambda: {})
        self.assertEqual(value['journal']['operation']['phase'], 'verified')
        self.assertEqual(value['journal']['operation']['write_refusal']['status'], 403)
        self.assertEqual(value['journal']['operation']['intent']['patch'], {})
        self.assertEqual(value['property_receipts'][NOTE_ID]['held'],
            {'标签': 'property_option_permission_denied'})
        self.assertEqual(counts['write_calls'], 1)


if __name__ == '__main__':
    unittest.main()
