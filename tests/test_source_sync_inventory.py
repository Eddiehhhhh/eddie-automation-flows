import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_sync_inventory as inventory
import source_sync_sample as sample
import sync_getnote_to_raw as sync


def manifest():
    return {'source_system': 'getnote', 'count': 4, 'notes': [
        {'note_id': '1001', 'path': 'plain/PRIVATE.md'},
        {'note_id': '1002', 'path': 'recording/PRIVATE.md'},
        {'note_id': 'opaque-private-id', 'path': 'legacy/PRIVATE.md'},
        {'note_id': '', 'path': 'unknown/PRIVATE.md'}]}


class InventoryTests(unittest.TestCase):
    def test_manifest_keeps_opaque_and_missing_ids_separate(self):
        mapping, meta = inventory.validate_manifest(manifest())
        self.assertEqual(set(mapping), {'1001', '1002'})
        self.assertEqual(meta, {'prior_manifest_notes': 4, 'prior_api_notes': 2,
                              'prior_non_api_notes_preserved': 1, 'prior_missing_id_notes_preserved': 1})
        for change in ('duplicate_id', 'duplicate_path', 'invalid_path', 'numeric_id', 'count', 'too_many'):
            bad = manifest()
            if change == 'duplicate_id': bad['notes'][1]['note_id'] = '1001'
            if change == 'duplicate_path': bad['notes'][1]['path'] = bad['notes'][0]['path']
            if change == 'invalid_path': bad['notes'][1]['path'] = '../PRIVATE.md'
            if change == 'numeric_id': bad['notes'][0]['note_id'] = 1001
            if change == 'count': bad['count'] = 5
            if change == 'too_many': bad['notes'] *= 2000; bad['count'] = len(bad['notes'])
            with self.assertRaises(sample.FixedSampleError): inventory.validate_manifest(bad)

    def test_census_reads_lists_only_and_counts_difference_without_values(self):
        mapping, meta = inventory.validate_manifest(manifest())
        pages = [{'data': {'notes': [{'note_id': '1001', 'title': 'PRIVATE'}], 'has_more': True, 'cursor': 'PRIVATE'}},
                 {'data': {'notes': [{'note_id': '1003', 'content': 'PRIVATE'}], 'has_more': False}}]
        with patch.object(sync, 'api_get', side_effect=pages) as api, \
             patch.object(sync, 'fetch_note_detail', side_effect=AssertionError('details forbidden')) as detail:
            report = inventory.census(mapping, meta, 'KEY', 'CLIENT')
        self.assertTrue(report['enumeration_complete']); self.assertFalse(report['source_complete'])
        self.assertEqual((report['pages'], report['source_notes'], report['matched_prior_api_notes'],
                          report['new_api_notes'], report['prior_api_not_enumerated']), (2, 2, 1, 1, 1))
        self.assertTrue(all(c.args[0] == sync.LIST_PATH for c in api.call_args_list))
        self.assertTrue(all(c.kwargs['max_response_bytes'] == 2*1024*1024 for c in api.call_args_list))
        detail.assert_not_called()
        self.assertNotIn('PRIVATE', json.dumps(report)); self.assertNotIn('1001', json.dumps(report))

    def test_page_cycle_duplicate_missing_terminal_and_count_limits_fail_closed(self):
        mapping, meta = inventory.validate_manifest(manifest())
        invalid = [
            [{'data': {'notes': [{'note_id': '1001'}], 'has_more': True, 'cursor': 'x'}}]*2,
            [{'data': {'notes': [{'note_id': '1001'}, {'note_id': '1001'}], 'has_more': False}}],
            [{'data': {'notes': [{'note_id': '1001'}]}}],
        ]
        for pages in invalid:
            with patch.object(sync, 'api_get', side_effect=pages), self.assertRaises(RuntimeError):
                inventory.census(mapping, meta, 'KEY', 'CLIENT')
        with patch.object(inventory, 'MAX_PAGES', 1), patch.object(sync, 'api_get', return_value={
                'data': {'notes': [{'note_id': '1001'}], 'has_more': True, 'cursor': 'x'}}), self.assertRaises(RuntimeError):
            inventory.census(mapping, meta, 'KEY', 'CLIENT')
        with patch.object(inventory, 'MAX_NOTES', 1), patch.object(sync, 'api_get', return_value={
                'data': {'notes': [{'note_id': '1001'}, {'note_id': '1002'}], 'has_more': False}}), self.assertRaises(inventory.SourceLimit):
            inventory.census(mapping, meta, 'KEY', 'CLIENT')

    def test_transport_budget_counts_actual_bytes_and_stops_before_next_request(self):
        class Response(io.BytesIO):
            pass
        opener = Mock()
        opener.open.return_value = Response(b'{}')
        budget = inventory.ResponseBudget(2)
        with patch.object(sync.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(sync.api_get(sync.LIST_PATH, {}, 'KEY', 'CLIENT', 20, budget), {})
            self.assertEqual(budget.used, 2)
            with self.assertRaises(inventory.SourceLimit): sync.api_get(sync.LIST_PATH, {}, 'KEY', 'CLIENT', 20, budget)
            self.assertEqual(opener.open.call_count, 1)
        for total, per, error in ((5, 20, inventory.SourceLimit), (20, 5, RuntimeError)):
            opener.open.return_value = Response(b'x'*21)
            with patch.object(sync.urllib.request, 'build_opener', return_value=opener), self.assertRaises(error):
                sync.api_get(sync.LIST_PATH, {}, 'KEY', 'CLIENT', per, inventory.ResponseBudget(total))

    def test_private_index_pins_three_bounded_reads_and_public_repo_fails(self):
        responses = [b'{"private":true}', json.dumps({'object': {'type': 'commit', 'sha': 'a'*40}}).encode(),
                     json.dumps(manifest()).encode()]
        with patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'SECRET'}), \
             patch.object(sample, 'github_read', side_effect=responses) as read:
            mapping, meta = inventory.private_index()
            self.assertEqual(read.call_count, 3)
            self.assertEqual(read.call_args_list[-1].args[2], sample.MANIFEST_MAX_BYTES)
            self.assertTrue(read.call_args_list[-1].args[0].endswith('?ref='+'a'*40))
            self.assertTrue(read.call_args_list[-1].kwargs['raw'])
        with patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'SECRET'}), \
             patch.object(sample, 'github_read', return_value=b'{"private":false}'), \
             self.assertRaises(sample.FixedSampleError): inventory.private_index()

    def test_safe_report_drops_injected_values_and_never_claims_full_sync(self):
        report = dict.fromkeys(inventory.COUNTS, 0)
        report.update(status='inventory_ok', enumeration_complete=True, source_complete=True, published=True,
                      PRIVATE='SECRET', note_ids=['SECRET'])
        safe = inventory.safe_report(report)
        self.assertEqual(safe['status'], 'inventory_ok')
        self.assertFalse(safe['published']); self.assertFalse(safe['source_complete'])
        self.assertNotIn('SECRET', json.dumps(safe))
        for value in (-1, True, 'SECRET', inventory.MAX_NOTES+1):
            bad = dict(report, source_notes=value)
            self.assertEqual(inventory.safe_report(bad)['status'], 'failed')

    def test_cli_failures_are_fixed_reports_with_no_source_or_credential_output(self):
        with patch.object(sys, 'argv', ['inventory']), \
             patch.object(inventory, 'private_index', side_effect=RuntimeError('SECRET PRIVATE')), \
             patch.object(sync, 'load_credentials') as credentials, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(inventory.main(), 1)
            credentials.assert_not_called()
            self.assertEqual(json.loads(out.getvalue())['error_code'], 'private_index_invalid')
            self.assertNotIn('SECRET', out.getvalue())
        for error in (inventory.SourceLimit('SECRET'), RuntimeError('PRIVATE')):
            with patch.object(sys, 'argv', ['inventory']), \
                 patch.object(inventory, 'private_index', return_value=inventory.validate_manifest(manifest())), \
                 patch.object(sync, 'load_credentials', return_value=('SECRET', 'SECRET')), \
                 patch.object(inventory, 'census', side_effect=error), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(inventory.main(), 1)
                self.assertFalse(json.loads(out.getvalue())['enumeration_complete'])
                self.assertNotIn('SECRET', out.getvalue()); self.assertNotIn('PRIVATE', out.getvalue())


if __name__ == '__main__': unittest.main()
