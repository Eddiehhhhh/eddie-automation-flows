"""Offline checks for source body fidelity and safe bounded previews."""
import argparse
import copy
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_notion_to_raw as sync

DB = {'id': '00000000-0000-4000-8000-000000000001',
      'title': [{'plain_text': 'Synthetic'}],
      'properties': {'Title': {'type': 'title'}}}
PAGE = {'id': '00000000-0000-4000-8000-000000000002',
        'last_edited_time': '2026-10-30T00:00:00Z',
        'properties': {'Title': {'type': 'title', 'title': [{'plain_text': 'Example'}]}}}
NOW = dt.datetime(2026, 10, 31, tzinfo=sync.TZ)


def paragraph(text, ident, children=False):
    return {'id': ident, 'type': 'paragraph', 'has_children': children,
            'paragraph': {'rich_text': [{'plain_text': text}]}}


class NotionExportTests(unittest.TestCase):
    def test_page_body_includes_own_and_nested_blocks(self):
        with mock.patch.object(sync, 'retrieve_block_children', side_effect=[
                [paragraph('Own source text', 'parent', True)],
                [paragraph('Nested source text', 'child')]]) as read:
            text, _, _ = sync.render_page_markdown('fixture', DB, PAGE, 'Title', None, True)
        self.assertIn('Own source text', text)
        self.assertIn('Nested source text', text)
        self.assertIn('page_id:', text)
        self.assertEqual(read.call_count, 2)

    def test_metadata_mode_does_not_fetch_own_blocks(self):
        with mock.patch.object(sync, 'retrieve_block_children', side_effect=AssertionError('body read')):
            sync.render_page_markdown('fixture', DB, PAGE, 'Title', None, False)

    def test_capped_sample_preserves_both_watermarks(self):
        prior = {'last_sync_at': '2026-10-01T00:00:00+08:00',
                 'last_full_sync_at': '2026-09-01T00:00:00+08:00'}
        local = {'databases': {DB['id']: copy.deepcopy(prior)}}
        public = {'databases': {'Synthetic': copy.deepcopy(prior)}}
        args = argparse.Namespace(full_sync=True, window_hours=24, max_pages=1,
                                  export_body=False, dry_run=False)
        with mock.patch.object(sync, 'resolve_database', return_value=DB), \
             mock.patch.object(sync, 'paginate_database', return_value=iter([PAGE, PAGE])):
            result = sync.sync_database('fixture', {'name': 'Synthetic'}, args,
                                        public, local, {}, {}, NOW)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['updated_at'], '')
        for key, val in prior.items():
            self.assertEqual(public['databases']['Synthetic'][key], val)
            self.assertEqual(local['databases'][DB['id']][key], val)

    def test_complete_incremental_advances_watermark(self):
        public = {}
        args = argparse.Namespace(full_sync=False, window_hours=24, max_pages=0,
                                  export_body=False, dry_run=False)
        with mock.patch.object(sync, 'resolve_database', return_value=DB), \
             mock.patch.object(sync, 'paginate_database', return_value=iter([PAGE])):
            result = sync.sync_database('fixture', {'name': 'Synthetic'}, args,
                                        public, {}, {}, {}, NOW)
        self.assertEqual(result['status'], 'synced')
        self.assertEqual(public['databases']['Synthetic']['last_sync_at'], sync.format_time(NOW))

    def test_database_pagination_rejects_partial_shapes_without_watermark(self):
        for reply in ({}, {'results': [], 'has_more': True},
                      {'results': [], 'has_more': False, 'next_cursor': 'extra'},
                      {'results': ['bad'], 'has_more': False}):
            with self.subTest(reply=reply), mock.patch.object(sync, 'resolve_database', return_value=DB), \
                 mock.patch.object(sync, 'query_database', return_value=reply):
                prior = {'databases': {'Synthetic': {'last_sync_at': '2026-10-01T00:00:00+08:00'}}}
                public = copy.deepcopy(prior)
                args = argparse.Namespace(full_sync=False, window_hours=24, max_pages=0,
                                          export_body=False, dry_run=False)
                with self.assertRaises(RuntimeError):
                    sync.sync_database('fixture', {'name': 'Synthetic'}, args, public, {}, {}, {}, NOW)
                self.assertEqual(public, prior)

    def test_block_pagination_rejects_missing_and_repeated_cursor(self):
        for replies in ([{'results': [], 'has_more': True}],
                        [{'results': [], 'has_more': True, 'next_cursor': 'again'}] * 2):
            with self.subTest(replies=replies), mock.patch.object(sync, 'notion_request', side_effect=replies):
                with self.assertRaises(RuntimeError):
                    sync.retrieve_block_children('fixture', PAGE['id'])

    def test_complete_database_and_body_pagination(self):
        replies = [{'results': [PAGE], 'has_more': True, 'next_cursor': 'next'},
                   {'results': [], 'has_more': False, 'next_cursor': None}]
        with mock.patch.object(sync, 'query_database', side_effect=replies) as read:
            self.assertEqual(list(sync.paginate_database('fixture', DB['id'])), [PAGE])
            self.assertEqual(read.call_args.kwargs['start_cursor'], 'next')
        replies = [{'results': [paragraph('First', 'one')], 'has_more': True, 'next_cursor': 'next'},
                   {'results': [paragraph('Second', 'two')], 'has_more': False, 'next_cursor': None}]
        with mock.patch.object(sync, 'notion_request', side_effect=replies):
            self.assertEqual(len(sync.retrieve_block_children('fixture', PAGE['id'])), 2)

    def test_dry_run_main_writes_nothing(self):
        args = argparse.Namespace(export_body=False, dry_run=True)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(sync, 'PUBLIC_LOG_DIR', Path(tmp) / 'logs'), \
             mock.patch.object(sync, 'parse_args', return_value=args), \
             mock.patch.object(sync, 'load_notion_api_key', return_value='fixture'), \
             mock.patch.object(sync, 'target_specs', return_value=[]), \
             mock.patch.object(sync, 'load_public_manifest', return_value={}), \
             mock.patch.object(sync, 'load_local_state', return_value={}), \
             mock.patch.object(sync, 'build_page_index', return_value={}):
            self.assertEqual(sync.main(), 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
