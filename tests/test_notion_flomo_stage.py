"""Synthetic sources; never use real credentials, files, network or Notion writes."""
import copy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import re
import unittest
from unittest import mock
import os
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_notion_flomo_to_raw as bridge
REAL_LOCK = bridge.writer_lock
VERIFY_PRIVATE = getattr(bridge.private_checkout, "verify_private_checkout", None)


def page(mid='YWJj', ident='page-one', title='Same title'):
    return {'id': ident, 'url': 'https://notion.invalid/' + ident,
            'created_time': '2026-01-01T00:00:00Z', 'last_edited_time': '2026-10-31T00:00:00Z',
            'properties': {'Name': {'type': 'title', 'title': [{'plain_text': title}]},
                           'Link': {'type': 'url', 'url': 'https://v.flomoapp.com/mine/?memo_id=' + mid},
                           'Tags': {'type': 'multi_select', 'multi_select': [{'name': 'sample/tag'}]},
                           'Created At': {'type': 'date', 'date': {'start': '2026-01-01T10:15:00+08:00', 'end': None, 'time_zone': None}},
                           '日记': {'type': 'relation', 'id': 'relation', 'has_more': False,
                                  'relation': [{'id': 'synthetic-diary'}]}}}


def block(text='Source text', ident='block-one', children=False):
    return {'id': ident, 'type': 'paragraph', 'has_children': children,
            'paragraph': {'rich_text': [{'plain_text': text, 'annotations': {'bold': True}}]}}


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / 'Raw/01 Flomo/2026/2026-01/Original.md'
        self.old.parent.mkdir(parents=True)
        self.old.write_text('---\nmemo_id: "YWJj"\n---\nOriginal local body\n')
        self.original = self.old.read_bytes()
        self.identity = mock.patch.object(bridge.private_checkout, 'verify_private_checkout', create=True)
        self.identity.start(); self.addCleanup(self.identity.stop)
        lock = mock.patch.object(bridge, 'writer_lock', return_value=nullcontext())
        lock.start(); self.addCleanup(lock.stop)
        self.rows = [page()]
        self.stack = mock.patch.multiple(bridge.notion,
            paginate_database=mock.DEFAULT, retrieve_block_children=mock.DEFAULT,
            notion_request=mock.DEFAULT)
        mocks = self.stack.start(); self.addCleanup(self.stack.stop)
        mocks['paginate_database'].side_effect = lambda *args: iter(copy.deepcopy(self.rows))
        mocks['retrieve_block_children'].return_value = [block()]
        mocks['notion_request'].side_effect = lambda key, method, path, *args: copy.deepcopy(
            next(row for row in self.rows if path == '/pages/' + row['id']))
        self.mocks = mocks
        self.addCleanup(lambda: self.assertEqual(self.old.read_bytes(), self.original))

    def record_path(self, mid='YWJj', suffix='.md', page_id=None):
        state = json.loads((self.root / bridge.STATE).read_text())
        record = state['pages'][mid]
        if page_id is not None: record = record['source_pages'][page_id]
        files = record['files']
        return self.root / bridge.STAGE / next(p for p in files if p.endswith('/' + mid + suffix))

    def run_sync(self, **kwargs):
        return bridge.run(self.root, 'synthetic-db', 'synthetic-credential', **kwargs)

    def test_private_identity_failure_never_writes(self):
        self.identity.stop()
        self.assertIsNotNone(VERIFY_PRIVATE, 'shared private-checkout helper must land first')
        for origin, metadata, token in [
                ('https://github.com/Eddiehhhhh/eddie-llm-wiki.git', {'private': False, 'full_name': 'Eddiehhhhh/eddie-llm-wiki'}, 'fixture'),
                ('https://github.com/wrong/repository.git', {'private': True, 'full_name': 'wrong/repository'}, 'fixture'),
                ('https://github.com/Eddiehhhhh/eddie-llm-wiki.git', {'private': True, 'full_name': 'wrong/repository'}, 'fixture'),
                ('https://github.com/Eddiehhhhh/eddie-llm-wiki.git', {}, '')]:
            with self.subTest(origin=origin, metadata=metadata), \
                 mock.patch.object(bridge.private_checkout, 'verify_private_checkout', VERIFY_PRIVATE, create=True), \
                 mock.patch.object(bridge.private_checkout, 'verify_private_repository', return_value=metadata), \
                 mock.patch.object(bridge.private_checkout.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=origin)), \
                 mock.patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': token}):
                with self.assertRaises(ValueError): self.run_sync(apply=True)
                self.assertFalse((self.root / bridge.STAGE).exists())
                self.assertFalse((self.root / bridge.STATE).exists())

    def test_immutable_publish_does_not_replace_a_racing_editor(self):
        path = self.root / 'immutable.md'
        real_link = bridge.os.link
        def race(source, target):
            target.write_bytes(b'user wrote between check and publish')
            return real_link(source, target)
        with mock.patch.object(bridge.os, 'link', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'stage_edited'):
                bridge.immutable_write(path, b'candidate')
        self.assertEqual(path.read_bytes(), b'user wrote between check and publish')

    def test_budget_holds_are_specific_and_persisted(self):
        for code in ('asset_file_budget', 'asset_batch_bound'):
            with self.subTest(code=code):
                self.mocks['retrieve_block_children'].return_value = [
                    {'id': 'asset', 'type': 'image', 'image': {'type': 'file', 'file': {'url': 'https://files.invalid/a'}}}]
                def blocked(url): raise ValueError(code)
                result = self.run_sync(apply=True, downloader=blocked)
                self.assertEqual(result['held'], {'YWJj': code})
                saved = json.loads((self.root / bridge.STAGE / 'coverage.json').read_text())
                self.assertEqual(saved['held']['YWJj'], code)

    def test_old_version_edit_is_held_after_new_version_exists(self):
        self.run_sync(apply=True)
        old = self.record_path()
        self.rows[0]['last_edited_time'] = '2026-11-01T00:00:00Z'
        self.run_sync(apply=True)
        old.write_text('Edited older version')
        result = self.run_sync(apply=True)
        self.assertEqual(result['held'], {'YWJj': 'stage_edited'})
        self.assertEqual(old.read_text(), 'Edited older version')

    def test_state_writer_lock_rejects_second_owner(self):
        bridge.subprocess.run(['git', '-C', str(self.root), 'init', '-q'], check=True)
        from sync_notion_flomo_to_raw import writer_lock
        # setUp patches the run adapter, so use the original function saved below.
        with REAL_LOCK(self.root):
            with self.assertRaisesRegex(ValueError, 'stage_writer_busy'):
                with REAL_LOCK(self.root): self.fail('second writer entered')

    def test_remote_readback_verifies_state_blob_and_detects_mismatch(self):
        self.run_sync(apply=True)
        raw = (self.root / bridge.STATE).read_bytes()
        expected = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        for remote, succeeds in [(expected, True), ('0' * 40, False)]:
            def git(args, **kwargs):
                if 'ls-tree' in args:
                    state = json.loads(raw)
                    values = []
                    for record in state['pages'].values():
                        for rel in bridge.prior_files(record):
                            data = (self.root / bridge.STAGE / rel).read_bytes()
                            sha = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                            values.append('100644 blob ' + sha + '\t' + str(bridge.STAGE / rel) + '\0')
                    value = ''.join(values)
                else: value = remote if 'origin/main:' + str(bridge.STATE) in args else expected
                return SimpleNamespace(returncode=0, stdout=value)
            with self.subTest(remote_matches=succeeds), mock.patch.object(bridge.subprocess, 'run', side_effect=git) as command:
                if succeeds: bridge.verify_remote_state(self.root)
                else:
                    with self.assertRaisesRegex(ValueError, 'private_state_readback_failed'):
                        bridge.verify_remote_state(self.root)
                self.assertTrue(any('fetch' in call.args[0] for call in command.call_args_list))

    def test_inventory_is_read_only(self):
        result = self.run_sync()
        self.assertEqual(result['pending'], 1)
        self.assertFalse((self.root / bridge.STAGE).exists())
        self.assertFalse((self.root / bridge.STATE).exists())
        self.mocks['retrieve_block_children'].assert_not_called()

    def test_apply_retains_body_properties_tags_and_local_mapping(self):
        result = self.run_sync(apply=True)
        self.assertTrue(result['complete'])
        text = self.record_path().read_text()
        self.assertIn('Source text', text)
        self.assertIn('sample/tag', text)
        self.assertIn('synthetic-diary', text)
        self.assertIn('Original.md', text)
        raw = json.loads(self.record_path(suffix='.json').read_text())
        self.assertTrue(raw['blocks'][0]['paragraph']['rich_text'][0]['annotations']['bold'])
        self.assertEqual(raw['page']['properties']['日记']['relation'], [{'id': 'synthetic-diary'}])

    def test_real_notion_date_shape_and_string_summary_preserve_source_date(self):
        self.assertIsInstance(bridge.notion.summarize_properties(self.rows[0])['Created At'], str)
        self.run_sync(apply=True)
        text = self.record_path().read_text()
        self.assertIn('created_at: "2026-01-01T10:15:00+08:00"', text)
        self.assertIn('notion_created_time: "2026-01-01T00:00:00Z"', text)

    def test_missing_flomo_source_date_does_not_use_notion_creation_time(self):
        del self.rows[0]['properties']['Created At']
        self.run_sync(apply=True)
        text = self.record_path().read_text()
        self.assertNotIn('created_at: "2026-01-01T00:00:00Z"', text)
        self.assertIn('notion_created_time: "2026-01-01T00:00:00Z"', text)
        raw = json.loads(self.record_path(suffix='.json').read_text())
        self.assertNotIn('Created At', raw['page']['properties'])

    def test_high_confidence_credential_shapes_hold_without_object_or_progress(self):
        # Deliberately invalid synthetic values; never call an account API.
        examples = ['ghp_' + 'A' * 36, 'github_pat_' + 'B' * 50,
                    'sk-proj-' + 'C' * 35, '-----BEGIN OPENSSH PRIVATE KEY-----']
        for sample in examples:
            with self.subTest(shape=sample.split('_')[0][:7]):
                self.mocks['retrieve_block_children'].return_value = [block(sample)]
                result = self.run_sync(apply=True)
                self.assertEqual(result['held'], {'YWJj': 'potential_credential_in_source'})
                self.assertFalse((self.root / bridge.STATE).exists())
                self.assertFalse((self.root / bridge.STAGE / 'versions').exists())
                coverage = (self.root / bridge.STAGE / 'coverage.json').read_text()
                self.assertNotIn(sample, coverage)
                self.assertNotIn(hashlib.sha256(sample.encode()).hexdigest(), coverage)

    def test_rendered_joined_credential_is_held_and_other_object_still_stages(self):
        self.rows.append(page('ZGVm', ident='page-two'))
        sensitive = block('ghp_')
        sensitive['paragraph']['rich_text'].append({'plain_text': 'A' * 36})
        self.mocks['retrieve_block_children'].side_effect = [[sensitive], [block('Ordinary text')]]
        result = self.run_sync(apply=True)
        self.assertEqual(result['held'], {'YWJj': 'potential_credential_in_source'})
        self.assertEqual(result['written'], 1)
        state = json.loads((self.root / bridge.STATE).read_text())
        self.assertEqual(set(state['pages']), {'ZGVm'})
        self.assertFalse((self.root / bridge.STAGE / 'versions/YWJj').exists())
        self.assertTrue(self.record_path('ZGVm').exists())

    def test_share_and_notion_resource_parameters_are_not_account_credentials(self):
        source = 'https://share.invalid/a?xsec_token=' + 'A' * 64
        source += '\nhttps://files.invalid/a?X-Amz-Credential=example&X-Amz-Signature=' + 'B' * 64
        self.mocks['retrieve_block_children'].return_value = [block(source)]
        result = self.run_sync(apply=True)
        self.assertTrue(result['complete'])
        self.assertEqual(result['written'], 1)

    def test_nested_body_is_fully_rendered(self):
        self.mocks['retrieve_block_children'].side_effect = [[block('Parent', children=True)], [block('Nested', 'child')]]
        self.run_sync(apply=True)
        self.assertIn('Nested', self.record_path().read_text())

    def test_identical_duplicate_sources_keep_both_receipts_without_a_winner(self):
        self.rows.append(page(ident='page-two'))
        result = self.run_sync(apply=True)
        self.assertEqual(result['held'], {})
        self.assertEqual(result['written'], 2)
        self.assertTrue(result['capture_complete'])
        self.assertEqual(result['unmerged_variant_group_count'], 0)
        record = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']
        self.assertNotIn('notion_page_id', record)
        self.assertEqual(set(record['source_pages']), {'page-one', 'page-two'})
        for ident in record['source_pages']:
            value = json.loads(self.record_path(suffix='.json', page_id=ident).read_text())
            self.assertEqual(value['page']['id'], ident)
        index = self.root / bridge.STAGE / record['current_variant_index']
        self.assertIn('stage_only: true', index.read_text())
        self.assertEqual(index.read_text().count('[来源 '), 2)

    def test_distinct_duplicate_bodies_and_properties_remain_separate_variants(self):
        second = page(ident='page-two', title='Other original title')
        second['properties']['Tags']['multi_select'] = [{'name': 'other/tag'}]
        self.rows.append(second)
        self.mocks['retrieve_block_children'].side_effect = [[block('Original A')], [block('Original B')]]
        result = self.run_sync(apply=True)
        self.assertTrue(result['capture_complete'])
        self.assertFalse(result['semantic_merge_complete'])
        self.assertFalse(result['fact_promotion_allowed'])
        self.assertEqual(result['unmerged_variant_group_count'], 1)
        self.assertIn('Original A', self.record_path(page_id='page-one').read_text())
        self.assertIn('Original B', self.record_path(page_id='page-two').read_text())
        self.assertNotEqual(self.record_path(page_id='page-one').parent, self.record_path(page_id='page-two').parent)

    def test_duplicate_batch_limit_resumes_each_page_without_repeating_prior_source(self):
        self.rows.append(page(ident='page-two'))
        result = self.run_sync(apply=True, max_pages=1)
        self.assertEqual((result['written'], result['pending']), (1, 1))
        first = self.record_path(page_id='page-one')
        original = first.read_bytes()
        self.mocks['retrieve_block_children'].reset_mock()
        result = self.run_sync(apply=True, max_pages=1)
        self.assertEqual((result['written'], result['unchanged'], result['pending']), (1, 1, 0))
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(self.mocks['retrieve_block_children'].call_count, 1)

    def test_ordinary_memo_becoming_duplicate_preserves_old_receipt_and_history(self):
        self.run_sync(apply=True)
        before = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']
        before_path = self.record_path()
        before_bytes = before_path.read_bytes()
        self.rows.append(page(ident='page-two'))
        result = self.run_sync(apply=True)
        after = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(result['written'], 1)
        self.assertEqual(after['source_pages']['page-one'], before)
        self.assertTrue(all(after[key] == value for key, value in before.items()))
        self.assertEqual(before_path.read_bytes(), before_bytes)
        # A former source disappearing from the active set does not erase it.
        self.rows = [self.rows[1]]
        self.run_sync(apply=True)
        after = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']
        self.assertEqual(set(after['source_pages']), {'page-one', 'page-two'})
        self.assertIn('保留历史', (self.root / bridge.STAGE / after['current_variant_index']).read_text())

    def test_duplicate_source_edit_preserves_both_previous_and_other_page_versions(self):
        self.rows.append(page(ident='page-two'))
        self.run_sync(apply=True)
        before = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']['source_pages']
        old_path = self.record_path(page_id='page-one')
        old_bytes = old_path.read_bytes()
        self.rows[0]['last_edited_time'] = '2026-11-02T00:00:00Z'
        self.mocks['retrieve_block_children'].return_value = [block('Revised source A')]
        self.run_sync(apply=True)
        after = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']['source_pages']
        self.assertEqual(after['page-two'], before['page-two'])
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(len(after['page-one']['versions']), 2)

    def test_credential_in_one_duplicate_does_not_block_the_other_source(self):
        self.rows.append(page(ident='page-two'))
        self.mocks['retrieve_block_children'].side_effect = [[block('sk-' + 'A' * 36)], [block('Safe source')]]
        result = self.run_sync(apply=True)
        self.assertEqual(result['held'], {'YWJj::page-one': 'potential_credential_in_source'})
        self.assertEqual(result['written'], 1)
        record = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']
        self.assertEqual(set(record['source_pages']), {'page-two'})
        self.assertEqual(len(list((self.root / bridge.STAGE).rglob('YWJj.json'))), 1)

    def test_duplicate_asset_bytes_and_links_remain_owned_by_each_source(self):
        self.rows.append(page(ident='page-two'))
        def image(name):
            return {'id': name, 'type': 'image', 'has_children': False,
                    'image': {'type': 'external', 'external': {'url': 'https://files.invalid/' + name + '.png'}}}
        self.mocks['retrieve_block_children'].side_effect = [[image('asset-a')], [image('asset-b')]]
        result = self.run_sync(apply=True, downloader=lambda url: url.rsplit('/', 1)[-1].encode())
        self.assertTrue(result['capture_complete'])
        record = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']
        self.assertEqual(sum('/assets/' in rel for rel in bridge.prior_files(record)), 2)
        for ident in record['source_pages']:
            note = self.record_path(page_id=ident)
            self.assertIn('assets/', note.read_text())
            self.assertEqual(len(list((note.parent / 'assets').iterdir())), 1)

    def test_failed_source_state_write_does_not_pollute_the_other_receipt_and_can_resume(self):
        self.rows.append(page(ident='page-two'))
        real_write = bridge.atomic_write
        failed = False
        def write(path, raw):
            nonlocal failed
            if path == self.root / bridge.STATE and not failed:
                failed = True
                raise OSError('synthetic atomic state failure')
            return real_write(path, raw)
        with mock.patch.object(bridge, 'atomic_write', side_effect=write):
            result = self.run_sync(apply=True)
        self.assertEqual(result['written'], 1)
        self.assertEqual(set(result['held']), {'YWJj::page-one'})
        record = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']
        self.assertEqual(set(record['source_pages']), {'page-two'})
        result = self.run_sync(apply=True)
        self.assertEqual((result['written'], result['unchanged']), (1, 1))
        self.assertTrue(result['capture_complete'])

    def test_legacy_550_ordinary_receipts_remain_value_identical(self):
        legacy = {}
        old_rows = []
        for number in range(550):
            mid = 'legacy' + str(number)
            current = page(mid, 'old-page-' + str(number))
            old_rows.append(current)
            files = {mid + '.md': b'---\nstage_only: true\n---\nLegacy source\n',
                     mid + '.json': bridge.encoded({'page': current, 'blocks': [block('Legacy source')]})}
            version, hashes = bridge.apply_record(self.root, mid, files, {})
            legacy[mid] = {'notion_page_id': current['id'], 'last_edited_time': current['last_edited_time'],
                           'current_version': version, 'files': hashes, 'versions': {version: hashes}}
        bridge.atomic_write(self.root / bridge.STATE, bridge.encoded({'version': 1, 'pages': legacy}))
        self.rows = [*old_rows, page(), page(ident='page-two')]
        result = self.run_sync(apply=True)
        self.assertEqual((result['unchanged'], result['written']), (550, 2))
        after = json.loads((self.root / bridge.STATE).read_text())['pages']
        self.assertTrue(all(after[mid] == record for mid, record in legacy.items()))

    def test_variant_index_is_excluded_from_fact_candidates(self):
        import datetime
        import iteration_incremental_audit as audit
        self.rows.append(page(ident='page-two'))
        self.run_sync(apply=True)
        record = json.loads((self.root / bridge.STATE).read_text())['pages']['YWJj']
        index = self.root / bridge.STAGE / record['current_variant_index']
        self.assertIsNone(audit.candidate_from_file(index, 'Raw/02 Notion/', datetime.date(2020, 1, 1), datetime.date(2030, 1, 1)))

    def test_variant_remote_readback_requires_every_source_and_index(self):
        self.rows.append(page(ident='page-two'))
        self.run_sync(apply=True)
        raw = (self.root / bridge.STATE).read_bytes()
        expected = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        record = json.loads(raw)['pages']['YWJj']
        paths = bridge.prior_files(record)
        self.assertEqual(len(paths), 5)
        for omitted in [None, record['current_variant_index'], next(rel for rel in paths if rel.endswith('.json'))]:
            def git(args, **kwargs):
                if 'ls-tree' not in args: return SimpleNamespace(returncode=0, stdout=expected)
                rows = []
                for rel in paths:
                    if rel == omitted: continue
                    data = (self.root / bridge.STAGE / rel).read_bytes()
                    sha = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                    rows.append('100644 blob ' + sha + '\t' + str(bridge.STAGE / rel) + '\0')
                return SimpleNamespace(returncode=0, stdout=''.join(rows))
            with self.subTest(omitted=omitted), mock.patch.object(bridge.subprocess, 'run', side_effect=git):
                if omitted is None: self.assertTrue(bridge.verify_remote_state(self.root))
                else:
                    with self.assertRaisesRegex(ValueError, 'private_state_readback_failed'):
                        bridge.verify_remote_state(self.root)

    def test_account_key_in_claimed_memo_identity_never_leaks_to_report(self):
        sample = 'ghp_' + 'A' * 36
        self.rows = [page(sample, 'bad-page'), page('ZGVm', 'good-page')]
        result = self.run_sync(apply=True)
        self.assertEqual(result['written'], 1)
        self.assertEqual(result['held'], {'page::bad-page': 'potential_credential_in_source'})
        self.assertNotIn(sample, bridge.encoded(result).decode())
        self.assertNotIn(sample, (self.root / bridge.STATE).read_text())

    def test_full_source_pagination_failure_prevents_any_object_capture(self):
        def incomplete(*args):
            yield page()
            raise RuntimeError('incomplete source listing')
        self.mocks['paginate_database'].side_effect = incomplete
        with self.assertRaises(RuntimeError): self.run_sync(apply=True)
        self.assertFalse((self.root / bridge.STAGE).exists())
        self.assertFalse((self.root / bridge.STATE).exists())

    def test_unchanged_run_uses_existing_content_identity(self):
        self.run_sync(apply=True)
        self.mocks['retrieve_block_children'].reset_mock()
        result = self.run_sync(apply=True)
        self.assertEqual(result['unchanged'], 1)
        self.mocks['retrieve_block_children'].assert_not_called()

    def test_manual_stage_edit_is_never_overwritten(self):
        self.run_sync(apply=True)
        path = self.record_path()
        path.write_text('User changed this stage')
        result = self.run_sync(apply=True)
        self.assertEqual(result['held'], {'YWJj': 'stage_edited'})
        self.assertEqual(path.read_text(), 'User changed this stage')

    def test_source_edit_reuses_memo_id_path(self):
        self.run_sync(apply=True)
        previous = self.record_path()
        previous_bytes = previous.read_bytes()
        self.rows[0]['properties']['Name']['title'][0]['plain_text'] = 'Changed title'
        self.rows[0]['last_edited_time'] = '2026-11-01T00:00:00Z'
        self.run_sync(apply=True)
        notes = list((self.root / bridge.STAGE).rglob('*.md'))
        self.assertEqual(len(notes), 2)
        self.assertEqual(previous.read_bytes(), previous_bytes)
        self.assertIn('Changed title', self.record_path().read_text())

    def test_limit_resumes_per_memo_without_a_skipping_watermark(self):
        self.rows.append(page('ZGVm', ident='page-two'))
        first = self.run_sync(apply=True, max_pages=1)
        self.assertEqual((first['written'], first['pending']), (1, 1))
        second = self.run_sync(apply=True, max_pages=1)
        self.assertEqual((second['written'], second['unchanged'], second['pending']), (1, 1, 0))

    def test_source_version_race_prevents_stage_write(self):
        self.mocks['notion_request'].return_value = {'last_edited_time': 'changed'}
        self.mocks['notion_request'].side_effect = None
        result = self.run_sync(apply=True)
        self.assertEqual(result['held'], {'YWJj': 'source_changed_during_read'})
        self.assertFalse(bool(list((self.root / bridge.STAGE).rglob('YWJj.md'))))

    def test_attachments_are_materialized_with_hash_and_local_link(self):
        asset = {'id': 'image', 'type': 'image', 'has_children': False,
                 'image': {'type': 'file', 'file': {'url': 'https://files.invalid/test.png?token=private'}}}
        self.mocks['retrieve_block_children'].return_value = [asset]
        result = self.run_sync(apply=True, downloader=lambda url: b'synthetic-image')
        self.assertTrue(result['complete'])
        asset_path = Path('assets') / (hashlib.sha256(b'synthetic-image').hexdigest() + '.png')
        self.assertEqual((self.record_path().parent / asset_path).read_bytes(), b'synthetic-image')
        text = self.record_path().read_text()
        self.assertIn(asset_path.as_posix(), text)
        self.assertNotIn('token=private', text)

    def test_relation_pagination_retains_all_ids(self):
        current = page()
        current['properties']['日记'].update(has_more=True, id='encoded%3Aid')
        replies = [{'results': [{'type': 'relation', 'relation': {'id': 'one'}}], 'has_more': True, 'next_cursor': 'next'},
                   {'results': [{'type': 'relation', 'relation': {'id': 'two'}}], 'has_more': False}]
        with mock.patch.object(bridge.notion, 'notion_request', side_effect=replies) as request:
            value = bridge.checked_page('fixture', current)
        self.assertEqual(value['properties']['日记']['relation'], [{'id': 'one'}, {'id': 'two'}])
        self.assertIn('encoded%3Aid', request.call_args_list[0].args[2])
        self.assertNotIn('%253A', request.call_args_list[0].args[2])

    def test_failed_object_does_not_discard_other_object_receipt(self):
        self.rows.append(page('ZGVm', ident='page-two'))
        self.mocks['retrieve_block_children'].side_effect = [[block()], RuntimeError('fixture-failure')]
        result = self.run_sync(apply=True)
        self.assertEqual(result['written'], 1)
        self.assertEqual(set(result['held']), {'ZGVm'})
        state = json.loads((self.root / bridge.STATE).read_text())
        self.assertEqual(set(state['pages']), {'YWJj'})
        self.assertTrue(self.record_path().is_file())

    def test_downloader_never_sends_notion_credential_and_rejects_short_read(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'ab'
        response.headers = {'Content-Length': '3'}
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(bridge.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('93.184.216.34', 443))]), \
             mock.patch.object(bridge.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, 'incomplete_asset'):
                bridge.download_asset('https://files.invalid/a')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertNotIn('Authorization', dict(request.header_items()))

    def test_workflow_keeps_existing_schedule_and_persists_before_partial_failure(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/sync-notion-personal-data.yml'
        text = path.read_text()
        self.assertEqual(re.findall(r'    - cron: "([^"]+)"', text), ['20 19 * * *', '20 7 * * *'])
        self.assertIn('  group: eddie-private-vault-writers', text)
        # Inspect these concrete steps; this is not a general YAML parser.
        snippets = text.split('      - name: ')[1:]
        steps = {part.splitlines()[0]: part for part in snippets}
        stage = steps['Stage Notion flomo content privately']
        commit = steps['Persist flomo staging and object progress']
        readback = steps['Verify remote flomo object state']
        final = steps['Report incomplete flomo staging']
        self.assertIn('        continue-on-error: true', stage)
        self.assertIn("!cancelled() && (steps.flomo_sync.outcome == 'success' || steps.flomo_sync.outcome == 'failure')", commit)
        self.assertLess(text.index('Commit Raw updates'), text.index('Read private flomo stage configuration'))
        self.assertLess(text.index('Commit Raw updates'), text.index('Stage Notion flomo content privately'))
        self.assertLess(text.index('Persist flomo staging and object progress'), text.index('Report incomplete flomo staging'))
        self.assertIn('notion-flomo-sync-state.json', commit)
        self.assertNotIn('Raw/01', commit)
        self.assertIn("steps.flomo_commit.outcome == 'success'", readback)
        self.assertIn('--verify-remote-state', readback)
        self.assertIn("steps.flomo_sync.outcome == 'failure'", final)
        self.assertIn('exit 2', final)

    def test_flomo_conditions_preserve_partial_results_without_starting_cancelled_writes(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/sync-notion-personal-data.yml'
        steps = {part.splitlines()[0]: part for part in path.read_text().split('      - name: ')[1:]}
        names = ['Read private flomo stage configuration', 'Stage Notion flomo content privately',
                 'Persist flomo staging and object progress', 'Verify remote flomo object state',
                 'Report incomplete flomo staging']
        self.assertIn('continue-on-error: true', steps[names[0]])
        def allowed(name, outcomes=None, cancelled=False):
            state = {'private_checkout': 'success', 'source_selection': 'skipped',
                     'flomo_config': 'success', 'flomo_requested': 'skipped',
                     'flomo_sync': 'success', 'flomo_commit': 'success', 'flomo_readback': 'success'}
            state.update(outcomes or {})
            model = SimpleNamespace(**{key: SimpleNamespace(outcome=value) for key, value in state.items()})
            model.flomo_config.outputs = SimpleNamespace(enabled='true' if state['flomo_config'] == 'success' else '')
            expression = steps[name].split('        if: ${{ ', 1)[1].split(' }}', 1)[0]
            expression = expression.replace('!cancelled()', 'not cancelled()').replace('!inputs.full_sync', 'not inputs.full_sync')
            expression = expression.replace('&&', 'and').replace('||', 'or')
            return eval(expression, {'__builtins__': {}}, {'steps': model, 'cancelled': lambda: cancelled,
                'inputs': SimpleNamespace(full_sync=False, flomo_stage_only=False),
                'github': SimpleNamespace(event_name='schedule')})
        # No success() dependency: earlier ordinary source failure can still stage.
        self.assertTrue(allowed(names[0], {'ordinary_source': 'failure'}))
        self.assertFalse(allowed(names[1], {'flomo_config': 'failure'}))
        self.assertTrue(allowed(names[4], {'flomo_config': 'failure', 'flomo_sync': 'skipped', 'flomo_commit': 'skipped'}))
        self.assertTrue(allowed(names[2], {'flomo_sync': 'failure'}))
        self.assertTrue(allowed(names[4], {'flomo_sync': 'failure'}))
        self.assertFalse(allowed(names[3], {'flomo_commit': 'failure'}))
        self.assertTrue(allowed(names[4], {'flomo_commit': 'failure'}))
        self.assertFalse(allowed(names[0], {'source_selection': 'failure'}))
        for name in names:
            self.assertFalse(allowed(name, cancelled=True), name)

    def test_remote_readback_rejects_state_whose_content_was_not_committed(self):
        self.run_sync(apply=True)
        raw = (self.root / bridge.STATE).read_bytes()
        expected = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        def git(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout='' if 'ls-tree' in args else expected)
        with mock.patch.object(bridge.subprocess, 'run', side_effect=git):
            with self.assertRaisesRegex(ValueError, 'private_state_readback_failed'):
                bridge.verify_remote_state(self.root)

    def test_actual_workflow_configuration_script_defaults_off_and_validates(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/sync-notion-personal-data.yml'
        snippet = path.read_text().split('      - name: Read private flomo stage configuration', 1)[1]
        script = textwrap.dedent(snippet.split("python - <<'PYTHON'\n", 1)[1].split('          PYTHON', 1)[0])
        config = self.root / '.github/state/notion-flomo-config.json'
        output = self.root / 'github-output'
        for value, expected in [(None, 'enabled=false'),
                                ({'version': 1, 'stage_enabled': False}, 'enabled=false'),
                                ({'version': 1, 'stage_enabled': True}, 'enabled=true')]:
            if value is not None:
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(json.dumps(value))
            output.unlink(missing_ok=True)
            with mock.patch.dict(os.environ, {'VAULT_ROOT': str(self.root), 'GITHUB_OUTPUT': str(output)}):
                exec(compile(script, 'workflow-flomo-config', 'exec'), {})
            self.assertEqual(output.read_text().strip(), expected)
        config.write_text(json.dumps({'version': 1, 'stage_enabled': 'true'}))
        with mock.patch.dict(os.environ, {'VAULT_ROOT': str(self.root), 'GITHUB_OUTPUT': str(output)}):
            with self.assertRaises(ValueError): exec(compile(script, 'workflow-flomo-config', 'exec'), {})

    def test_asset_fetch_rejects_credentials_and_private_addresses(self):
        with self.assertRaisesRegex(ValueError, 'unsafe_asset_url'):
            bridge.download_asset('https://user:pass@example.invalid/a')
        with mock.patch.object(bridge.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('127.0.0.1', 443))]):
            with self.assertRaisesRegex(ValueError, 'unsafe_asset_address'):
                bridge.download_asset('https://example.invalid/a')


if __name__ == '__main__': unittest.main()
