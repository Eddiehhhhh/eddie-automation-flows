"""Synthetic read-only source fixtures; no network, credentials or real content."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_notion_flomo_to_raw as m
from test_notion_flomo_stage import page, block


def source(mid='memo-a', ident='page-a'):
    value = page(mid, ident)
    value['parent'] = {'type': 'database_id', 'database_id': 'synthetic-db'}
    for n, prop in value['properties'].items(): prop.setdefault('id', n)
    value['properties']['Name']['title'][0].update(type='text', text={'content': 'Source title'})
    value['properties']['Full text'] = {'id': 'text-prop', 'type': 'rich_text', 'rich_text': []}
    return value


class ProofTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rows = [source()]
        self.calls = []
        self.responses = {}
        self.block_rows = [block()]
        for target, value in ((m.private_checkout, 'verify_private_checkout'), (m, 'writer_lock')):
            patch = mock.patch.object(target, value, return_value=nullcontext()); patch.start(); self.addCleanup(patch.stop)
        for name, effect in [('paginate_database', self.inventory), ('notion_request', self.request),
                             ('retrieve_block_children', self.blocks)]:
            patch = mock.patch.object(m.notion, name, side_effect=effect); patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(m.time, 'sleep'); patch.start(); self.addCleanup(patch.stop)

    def observe(self, method, path):
        observer = m.notion.REQUEST_OBSERVER.get()
        if observer: observer(method, path)
        self.calls.append((method, path))

    def inventory(self, key, database):
        self.observe('POST', '/databases/' + m.notion.normalize_notion_id(database) + '/query')
        return iter(copy.deepcopy(self.rows))

    def blocks(self, key, ident):
        self.observe('GET', '/blocks/' + ident + '/children')
        return copy.deepcopy(self.block_rows)

    def request(self, key, method, path, payload=None):
        self.observe(method, path)
        if path in self.responses:
            value = self.responses[path]
            if isinstance(value, Exception): raise value
            return copy.deepcopy(value)
        parts = urlsplit(path).path.split('/')
        row = next(v for v in self.rows if v['id'] == parts[2])
        if len(parts) == 3: return copy.deepcopy(row)
        prop = next(p for p in row['properties'].values() if p['id'] == unquote(parts[4]))
        kind = prop['type']; values = copy.deepcopy(prop[kind])
        if kind == 'rich_text':
            values = [{'type': 'text', 'text': {'content': 'Full source body'},
                       'plain_text': 'Full source body', 'annotations': {'italic': True}}]
        return {'object': 'list', 'type': 'property_item', 'has_more': False, 'next_cursor': None,
                'property_item': {'id': prop['id'], 'type': kind, 'next_url': None},
                'results': [{'object': 'property_item', 'id': prop['id'], 'type': kind, kind: v} for v in values]}

    def run_sync(self, **kwargs):
        return m.run(self.root, 'synthetic-db', 'invalid-fixture', apply=True, **kwargs)

    def record(self, mid='memo-a', ident=None):
        value = json.loads((self.root / m.STATE).read_bytes())['pages'][mid]
        return value['source_pages'][ident] if ident else value

    def snapshot(self, record, mid='memo-a'):
        return json.loads((self.root / m.STAGE / next(p for p in record['files'] if p.endswith('/'+mid+'.json'))).read_bytes())

    def test_old_same_version_backfilled_once_and_full_property_evidence_preserved(self):
        self.run_sync()
        old = copy.deepcopy(self.record())
        result = self.run_sync(property_proof_enabled=True)
        self.assertTrue(result['property_proof_complete']); self.assertEqual(result['written'], 1)
        record = self.record(); self.assertEqual(record['property_proof_version'], 1)
        self.assertTrue(set(old['versions']) < set(record['versions']))
        proof = self.snapshot(record)['property_capture']
        self.assertEqual(proof['original_page'], self.rows[0])
        self.assertEqual(proof['original_page']['properties']['Full text']['rich_text'], [])
        self.assertEqual(proof['hydrated_page']['properties']['Full text']['rich_text'][0]['plain_text'], 'Full source body')
        self.assertTrue(proof['hydrated_page']['properties']['Full text']['rich_text'][0]['annotations']['italic'])
        self.assertIn('text-prop', proof['property_responses'])
        note = next(p for p in record['files'] if p.endswith('/memo-a.md'))
        readable = (self.root/m.STAGE/note).read_text().split('---', 2)[2]
        self.assertIn('Full source body', readable)
        import flomo_cloud_current as current
        rendered = current.notion_body(self.root, 'memo-a', record, {}, {})[2]
        self.assertIn('Full source body', rendered)
        self.calls.clear(); again = self.run_sync(property_proof_enabled=True)
        self.assertEqual(again['unchanged'], 1); self.assertEqual(again['written'], 0)
        self.assertEqual(self.calls, [('POST', '/databases/synthetic-db/query')])

    def test_page_budget_retains_progress_then_resumes_without_legacy_loss(self):
        self.rows.append(source('memo-b', 'page-b'))
        self.run_sync()
        old = {mid: self.record(mid) for mid in ('memo-a', 'memo-b')}
        kwargs = {'property_proof_enabled': True, 'property_proof_batch': {'max_pages': 1}}
        result = self.run_sync(**kwargs)
        self.assertFalse(result['complete']); self.assertEqual(result['written'], 1)
        self.assertEqual(result['pending'], 1); self.assertEqual(result['budget_reason'], 'page_budget')
        self.assertEqual(self.record('memo-b'), old['memo-b'])
        self.assertTrue(self.run_sync(**kwargs)['complete'])
        self.assertEqual(self.record('memo-b')['property_proof_version'], 1)

    def test_new_input_prioritized_over_old_property_backfill(self):
        self.run_sync()
        self.rows.append(source('memo-b', 'page-b'))
        result = self.run_sync(property_proof_enabled=True, max_pages=1)
        self.assertEqual(result['written'], 1)
        self.assertNotIn('property_proof_version', self.record('memo-a'))
        self.assertEqual(self.record('memo-b')['property_proof_version'], 1)

    def test_new_variant_prioritized_inside_existing_memo_group(self):
        self.run_sync()
        original = copy.deepcopy(self.record())
        self.rows.append(source('memo-a', 'page-b'))
        result = self.run_sync(property_proof_enabled=True, max_pages=1)
        self.assertEqual(result['written'], 1); self.assertFalse(result['complete'])
        self.assertEqual(self.record(ident='page-a'), original)
        self.assertEqual(self.record(ident='page-b')['property_proof_version'], 1)

    def test_variants_each_have_full_proof_and_old_versions(self):
        self.rows.append(source('memo-a', 'page-b'))
        self.run_sync()
        result = self.run_sync(property_proof_enabled=True)
        self.assertTrue(result['complete']); self.assertEqual(result['written'], 2)
        for ident in ('page-a','page-b'):
            record = self.record(ident=ident)
            self.assertEqual(record['property_proof_version'], 1)
            self.assertEqual(len(record['versions']), 2)
            self.assertEqual(self.snapshot(record)['property_capture']['original_page']['id'], ident)

    def test_credential_in_property_envelope_prevents_all_object_files_and_state(self):
        path = '/pages/page-a/properties/text-prop?page_size=100'
        envelope = self.request('fixture', 'GET', path)
        envelope['unrelated_source_field'] = 'secret_' + 'a'*40
        self.responses[path] = envelope
        result = self.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held'], {'memo-a': 'potential_credential_in_source'})
        self.assertFalse((self.root / m.STATE).exists())
        self.assertFalse((self.root / m.STAGE / 'versions').exists())
        self.assertNotIn('a'*40, (self.root / m.STAGE / 'coverage.json').read_text())

    def test_malformed_property_envelope_held_not_marked_complete(self):
        self.responses['/pages/page-a/properties/text-prop?page_size=100'] = {}
        result = self.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held'], {'memo-a': 'incomplete_property_proof'})
        self.assertFalse(result['property_proof_complete']); self.assertFalse((self.root/m.STATE).exists())

    def test_partial_inventory_budget_does_not_erase_state_or_claim_counts(self):
        self.run_sync(); before = (self.root/m.STATE).read_bytes()
        from notion_source_budget import BudgetExceeded
        with mock.patch.object(m.notion, 'paginate_database', side_effect=BudgetExceeded('request_budget')):
            result = self.run_sync(property_proof_enabled=True)
        self.assertEqual((self.root/m.STATE).read_bytes(), before)
        self.assertFalse(result['inventory_complete']); self.assertIsNone(result['notion_pages'])
        self.assertFalse(result['complete'])

    def test_identity_move_and_missing_known_page_cannot_claim_complete(self):
        self.run_sync()
        self.rows[0]['parent']['database_id'] = 'other-db'
        result = self.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held'], {'memo-a': 'source_identity_changed'})
        self.rows.clear()
        result = self.run_sync(property_proof_enabled=True)
        self.assertIn('source_identity_not_observed', result['held'].values())
        self.assertFalse(result['complete'])

    def test_context_reset_after_error_and_default_mode_has_no_observer(self):
        sentinel = lambda method, path: None
        token = m.notion.REQUEST_OBSERVER.set(sentinel)
        retry = m.notion.READ_RETRIES.set(3)
        try:
            self.run_sync(property_proof_enabled=True)
            self.assertIs(m.notion.REQUEST_OBSERVER.get(), sentinel)
            self.assertEqual(m.notion.READ_RETRIES.get(), 3)
        finally:
            m.notion.REQUEST_OBSERVER.reset(token); m.notion.READ_RETRIES.reset(retry)

    def test_same_edit_timestamp_visible_property_change_requires_new_proof(self):
        self.run_sync(property_proof_enabled=True)
        old = self.record()['current_version']
        self.rows[0]['properties']['Tags']['multi_select'].append({'name': 'changed'})
        result = self.run_sync(property_proof_enabled=True)
        self.assertEqual(result['written'], 1)
        self.assertNotEqual(old, self.record()['current_version'])

    def test_source_changed_at_final_read_keeps_prior_receipt(self):
        self.run_sync(); before = (self.root/m.STATE).read_bytes()
        original = self.request
        reads = []
        def changing(key, method, path, payload=None):
            value = original(key, method, path, payload)
            if path == '/pages/page-a':
                reads.append(path)
                if len(reads) == 2: value['properties']['Tags']['multi_select'] = []
            return value
        with mock.patch.object(m.notion, 'notion_request', side_effect=changing):
            result = self.run_sync(property_proof_enabled=True)
        self.assertEqual(result['held'], {'memo-a': 'source_changed_during_read'})
        self.assertEqual((self.root/m.STATE).read_bytes(), before)

    def test_request_budget_exhaustion_is_pending_not_false_success(self):
        for number in range(25):
            self.rows[0]['properties']['Text'+str(number)] = {'id':'text'+str(number), 'type':'rich_text', 'rich_text':[]}
        result = self.run_sync(property_proof_enabled=True, property_proof_batch={'max_requests':20})
        self.assertEqual(result['requests'], 20)
        self.assertEqual(result['budget_reason'], 'request_budget')
        self.assertEqual(result['pending'], 1); self.assertEqual(result['held'], {})
        self.assertFalse((self.root/m.STATE).exists())

    def test_asset_budget_is_accounted_before_any_object_is_published(self):
        from notion_source_budget import BudgetExceeded
        self.block_rows = [{'id':'asset-block','type':'image','image':{'type':'external','external':{'url':'https://assets.invalid/a.png'}}}]
        with mock.patch('notion_source_budget.Budget.asset', side_effect=BudgetExceeded('asset_batch_budget')):
            result = self.run_sync(property_proof_enabled=True, downloader=lambda url:b'synthetic-image')
        self.assertEqual(result['budget_reason'], 'asset_batch_budget')
        self.assertFalse((self.root/m.STATE).exists())
        self.assertFalse((self.root/m.STAGE/'versions').exists())

    def test_job_reserve_skips_expensive_followups_and_preserves_state(self):
        with mock.patch.dict(m.os.environ, {'NOTION_SOURCE_JOB_DEADLINE':'1'}), \
             mock.patch('flomo_original_source.enrich') as original, \
             mock.patch('flomo_cloud_current.export') as current:
            result = self.run_sync(property_proof_enabled=True, original_source_enabled=True, current_export_enabled=True)
        self.assertFalse(result['complete'])
        self.assertEqual(result['budget_reason'], 'job_budget_reserved_for_persistence')
        original.assert_not_called(); current.assert_not_called()
        self.assertFalse((self.root/m.STATE).exists())

    def test_proof_inventory_only_does_not_hydrate_or_write(self):
        result = m.run(self.root, 'synthetic-db', 'invalid-fixture', property_proof_enabled=True)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(self.calls, [('POST','/databases/synthetic-db/query')])
        self.assertFalse((self.root/m.STAGE).exists()); self.assertFalse((self.root/m.STATE).exists())

    def test_generic_delegates_only_completed_flomo_proof_without_writing_owner_state(self):
        import sync_notion_sources_to_raw as generic
        row = {'id': 'page-a', 'flomo_memo_id': 'memo-a',
               'last_edited_time': self.rows[0]['last_edited_time'],
               'metadata_fingerprint': m.metadata_fingerprint(self.rows[0])}
        self.run_sync()
        self.assertFalse(generic.flomo_delegation(self.root, row, 'synthetic-db')['verified'])
        self.run_sync(property_proof_enabled=True)
        before = (self.root/m.STATE).read_bytes()
        self.assertTrue(generic.flomo_delegation(self.root, row, 'synthetic-db')['verified'])
        self.assertEqual((self.root/m.STATE).read_bytes(), before)
        self.assertEqual(generic.metadata_fingerprint(self.rows[0]), row['metadata_fingerprint'])

    def test_generic_never_delegates_a_same_timestamp_but_stale_property_proof(self):
        import sync_notion_sources_to_raw as generic
        self.run_sync(property_proof_enabled=True)
        row = {'id': 'page-a', 'flomo_memo_id': 'memo-a',
               'last_edited_time': self.rows[0]['last_edited_time'],
               'metadata_fingerprint': '0'*64}
        self.assertFalse(generic.flomo_delegation(self.root, row, 'synthetic-db')['verified'])
        row.pop('metadata_fingerprint')
        self.assertFalse(generic.flomo_delegation(self.root, row, 'synthetic-db')['verified'])

    def test_missing_old_receipt_metadata_proof_stays_pending_without_silent_upgrade(self):
        import sync_notion_sources_to_raw as generic
        self.run_sync(property_proof_enabled=True)
        path = self.root/m.STATE
        state = json.loads(path.read_bytes());state['pages']['memo-a'].pop('metadata_fingerprint')
        path.write_bytes(m.encoded(state)); before = path.read_bytes()
        row = {'id': 'page-a', 'flomo_memo_id': 'memo-a',
               'last_edited_time': self.rows[0]['last_edited_time']}
        result = generic.flomo_delegation(self.root, row, 'synthetic-db')
        self.assertFalse(result['verified']); self.assertEqual(result['reason'], 'delegated_property_proof_pending')
        self.assertEqual(path.read_bytes(), before)

    def test_compact_database_id_allows_the_sdk_normalized_readonly_query(self):
        database = '123456781234123412341234567890ab'
        self.rows[0]['parent']['database_id'] = m.notion.normalize_notion_id(database)
        result = m.run(self.root, database, 'invalid-fixture', apply=True, property_proof_enabled=True)
        self.assertTrue(result['complete'])
        self.assertEqual(self.calls[0], ('POST', '/databases/'+m.notion.normalize_notion_id(database)+'/query'))

    def test_source_and_flomo_fingerprints_share_signed_url_normalization(self):
        import sync_notion_sources_to_raw as generic
        first = copy.deepcopy(self.rows[0])
        first['properties']['Attachment'] = {'id':'asset-prop', 'type':'files', 'files':[
            {'name':'source.bin', 'type':'file', 'file':{'url':'https://assets.invalid/source.bin?token=first', 'expiry_time':'first'}}]}
        later = copy.deepcopy(first)
        later['properties']['Attachment']['files'][0]['file'].update(
            url='https://assets.invalid/source.bin?token=second', expiry_time='second')
        self.assertEqual(m.metadata_fingerprint(first), m.metadata_fingerprint(later))
        self.assertEqual(generic.metadata_fingerprint(first), m.metadata_fingerprint(first))
        self.assertEqual(generic.metadata_fingerprint(later), m.metadata_fingerprint(later))
        later['properties']['Attachment']['files'][0]['name'] = 'changed.bin'
        self.assertNotEqual(m.metadata_fingerprint(first), m.metadata_fingerprint(later))

    def test_bounds_are_finite_even_with_legacy_unlimited(self):
        self.assertEqual(m.proof_limits(max_pages=0)['max_pages'], 50)
        for value in ({'max_pages':0}, {'max_requests': True}, {'max_seconds':99999}, {'unknown':1}):
            with self.assertRaisesRegex(ValueError, 'invalid_property_proof_batch'): m.proof_limits(value)


if __name__ == '__main__': unittest.main()
