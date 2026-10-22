"""Offline regression tests. All sources are synthetic; network/credentials fail closed."""
import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_my_podcast_to_raw as sync
import sync_notion_to_raw as helper

REAL_BLOCK_LOADER = sync.retrieve_block_children_checked
REAL_RENDER_BLOCKS = sync.render_blocks

START = dt.datetime(2026, 1, 22, 10, 0, 0, 555000, tzinfo=sync.TZ)
PRIOR = {'last_sync_at': '2026-01-21T10:00:00+08:00',
         'last_full_sync_at': '2025-12-01T00:00:00+08:00',
         'future_schema_key': {'keep': True}}


def page(number, **extra):
    return {'id': f'00000000-0000-4000-8000-{number:012d}',
            'created_time': '2026-01-01T00:00:00Z',
            'last_edited_time': '2026-01-21T00:00:00Z',
            'properties': {'Title': {'type': 'title', 'title': [{'plain_text': f'Synthetic episode {number}'}]}},
            **extra}


def response(pages=(), more=False, cursor=None):
    return {'results': list(pages), 'has_more': more, 'next_cursor': cursor}


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='podcast-offline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / '.github/state/xiaoyuzhou-sync-state.json'
        self.output = self.root / 'Raw/13 小宇宙'
        self.legacy = self.root / 'Raw/11 我的播客'
        self.clock = START
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for key, val in {'ROOT': self.root, 'STATE_PATH': self.state, 'OUTPUT_ROOT': self.output,
                         'LEGACY_OUTPUT_ROOTS': [self.legacy]}.items():
            self.stack.enter_context(mock.patch.object(sync, key, val))
        self.stack.enter_context(mock.patch.object(sync, 'now', lambda: self.clock))
        self.stack.enter_context(mock.patch.object(sync.urllib.request, 'urlopen', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(mock.patch.object(sync, 'notion_request', side_effect=AssertionError('Live API forbidden')))
        self.stack.enter_context(mock.patch.object(sync, 'load_notion_api_key', return_value='SYNTHETIC-NOT-A-CREDENTIAL'))
        self.db = self.stack.enter_context(mock.patch.object(sync, 'get_database', return_value={'id': 'synthetic-database'}))
        self.query = self.stack.enter_context(mock.patch.object(sync, 'query_database', return_value=response([page(1), page(2), page(3)])))
        self.stack.enter_context(mock.patch.object(sync, 'retrieve_block_children_checked', return_value=[]))
        self.stack.enter_context(mock.patch.object(sync, 'render_blocks', return_value=['Synthetic body.']))
        self.fetch = self.stack.enter_context(mock.patch.object(sync, 'fetch_page', return_value=page(1)))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.set_state(PRIOR)

    def set_state(self, value):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(value, indent=3) + '\n', encoding='utf-8')
        self.original_state_bytes = self.state.read_bytes()

    def run_main(self, *args):
        with mock.patch.object(sys, 'argv', ['sync', *args]):
            sync.main()

    def unchanged(self):
        self.assertEqual(self.state.read_bytes(), self.original_state_bytes)

    def bodies(self):
        return [p for p in self.output.rglob('*.md') if p.name != '索引.md']

    def bounds(self, full=False, hours=168):
        result = sync.build_episode_filter(full_sync=full, hours_back=hours, scan_started_at=START)
        return [f['last_edited_time'] for f in result['and'] if 'last_edited_time' in f]

    def test_existing_state_honors_default_lookback(self):
        self.assertEqual(self.bounds()[0]['on_or_after'], '2026-01-15T10:00:00+08:00')

    def test_explicit_hours_is_not_hardcoded_to_168(self):
        self.set_state({**PRIOR, 'last_sync_at': '2026-01-22T09:00:00+08:00'})
        self.assertEqual(self.bounds(hours=2)[0]['on_or_after'], '2026-01-22T08:00:00+08:00')

    def test_old_state_is_not_skipped_after_long_outage(self):
        self.set_state({**PRIOR, 'last_sync_at': '2025-11-01T00:00:00+08:00'})
        self.assertEqual(self.bounds()[0]['on_or_after'], '2025-11-01T00:00:00+08:00')

    def test_first_incremental_has_requested_window(self):
        self.state.unlink()
        self.assertEqual(self.bounds(hours=48)[0]['on_or_after'], '2026-01-20T10:00:00+08:00')

    def test_upper_bound_is_inclusive_start_not_completion(self):
        self.assertEqual(self.bounds()[-1], {'on_or_before': '2026-01-22T10:00:00+08:00'})

    def test_full_scan_has_only_eligibility_and_upper_bound(self):
        self.assertEqual(self.bounds(full=True), [{'on_or_before': '2026-01-22T10:00:00+08:00'}])

    def test_success_uses_start_and_preserves_unknown_state_fields(self):
        def later_query(*args, **kwargs):
            self.clock += dt.timedelta(hours=2)
            return response([page(1)])
        self.query.side_effect = later_query
        self.run_main()
        state = json.loads(self.state.read_text())
        self.assertEqual(state['last_sync_at'], '2026-01-22T10:00:00+08:00')
        self.assertEqual(state['last_full_sync_at'], PRIOR['last_full_sync_at'])
        self.assertEqual(state['future_schema_key'], PRIOR['future_schema_key'])
        self.assertEqual(sync.parse_frontmatter(self.bodies()[0].read_text())['synced_at'], '2026-01-22T12:00:00+08:00')

    def test_full_success_advances_both_original_fields(self):
        self.run_main('--all-current')
        state = json.loads(self.state.read_text())
        self.assertEqual(state['last_sync_at'], state['last_full_sync_at'])
        self.assertEqual(state['last_sync_at'], '2026-01-22T10:00:00+08:00')
        self.assertEqual(set(state), set(PRIOR))

    def test_empty_complete_scan_is_real_coverage(self):
        self.query.return_value = response()
        self.run_main()
        self.assertEqual(json.loads(self.state.read_text())['last_sync_at'], '2026-01-22T10:00:00+08:00')

    def test_complete_pagination_requests_each_cursor(self):
        self.query.side_effect = [response([page(1)], True, 'c1'), response([page(2)], True, 'c2'), response([page(3)])]
        self.run_main()
        self.assertEqual([c.kwargs['start_cursor'] for c in self.query.call_args_list], [None, 'c1', 'c2'])
        self.assertEqual(len(self.bodies()), 3)

    def test_partial_full_run_never_advances_either_watermark(self):
        self.run_main('--all-current', '--max-episodes', '2')
        self.assertEqual(len(self.bodies()), 2)
        self.unchanged()

    def test_partial_incremental_run_never_advances_watermark(self):
        self.run_main('--max-episodes', '2')
        self.assertEqual(len(self.bodies()), 2)
        self.unchanged()

    def test_exact_limit_is_conservative_not_full(self):
        self.run_main('--all-current', '--max-episodes', '3')
        self.unchanged()

    def test_limit_larger_than_exhausted_result_may_advance(self):
        self.run_main('--all-current', '--max-episodes', '4')
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_zero_limit_remains_unlimited(self):
        self.run_main('--all-current', '--max-episodes', '0')
        self.assertEqual(len(self.bodies()), 3)
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_missing_only_exhaustion_does_not_certify_freshness(self):
        self.run_main('--all-current', '--only-missing')
        self.assertEqual(len(self.bodies()), 3)
        self.unchanged()

    def test_missing_only_can_resume_by_existing_episode_ids_without_new_state_schema(self):
        self.run_main('--all-current', '--only-missing', '--max-episodes', '2')
        first = {p: p.read_bytes() for p in self.bodies()}
        self.assertEqual(len(first), 2)
        self.run_main('--all-current', '--only-missing', '--max-episodes', '2')
        self.assertEqual(len(self.bodies()), 3)
        self.assertTrue(all(p.read_bytes() == body for p, body in first.items()))
        self.unchanged()

    def test_index_only_never_loads_credentials_or_source_and_preserves_state_bytes(self):
        with mock.patch.object(sync, 'load_notion_api_key', side_effect=AssertionError('No credentials needed')):
            self.run_main('--rebuild-index')
        self.query.assert_not_called()
        self.db.assert_not_called()
        self.unchanged()

    def test_index_only_does_not_create_absent_state(self):
        self.state.unlink()
        self.run_main('--rebuild-index')
        self.assertFalse(self.state.exists())

    def test_targeted_repair_does_not_advance_database_watermark(self):
        self.run_main('--page-id', page(1)['id'])
        self.query.assert_not_called()
        self.assertEqual(len(self.bodies()), 1)
        self.unchanged()

    def test_repeated_target_ids_are_deduplicated(self):
        self.run_main('--page-id', page(1)['id'], '--page-id', page(1)['id'])
        self.fetch.assert_called_once()
        self.unchanged()

    def test_dry_run_writes_no_body_index_or_state(self):
        self.run_main('--all-current', '--dry-run')
        self.assertFalse(self.output.exists())
        self.unchanged()

    def test_dry_run_index_only_is_read_only(self):
        self.run_main('--rebuild-index', '--dry-run')
        self.assertFalse(self.output.exists())
        self.unchanged()

    def test_malformed_query_shapes_stop_without_state_change(self):
        for bad in [{}, {'error': 'unknown'}, response([], more=1), {'results': {}, 'has_more': False}, None]:
            with self.subTest(bad=bad):
                self.query.return_value = bad
                with self.assertRaises(RuntimeError): self.run_main()
                self.unchanged()

    def test_missing_nonempty_cursor_stops_instead_of_silent_exhaustion(self):
        for cursor in [None, '', 42]:
            with self.subTest(cursor=cursor):
                self.query.return_value = response([page(1)], True, cursor)
                with self.assertRaises(RuntimeError): self.run_main()
                self.unchanged()

    def test_repeated_cursor_stops(self):
        self.query.side_effect = [response([page(1)], True, 'c1'), response([page(2)], True, 'c1')]
        with self.assertRaises(RuntimeError): self.run_main()
        self.unchanged()

    def test_conflicting_terminal_cursor_stops(self):
        self.query.return_value = response([], False, 'unexpected')
        with self.assertRaises(RuntimeError): self.run_main()
        self.unchanged()

    def test_missing_page_identity_stops(self):
        for invalid in [{}, {'id': ''}, {'id': None}, {'id': 2}, None]:
            with self.subTest(page=invalid):
                self.query.return_value = response([invalid])
                with self.assertRaises(RuntimeError): self.run_main()
                self.unchanged()

    def test_duplicate_ids_stop_instead_of_false_unique_coverage(self):
        self.query.side_effect = [response([page(1)], True, 'c1'), response([page(1)])]
        with self.assertRaises(RuntimeError): self.run_main()
        self.unchanged()

    def test_mid_pagination_failure_does_not_advance_or_write_partial_page_list(self):
        self.query.side_effect = [response([page(1)], True, 'c1'), RuntimeError('Synthetic source failure')]
        with self.assertRaises(RuntimeError): self.run_main()
        self.assertFalse(self.bodies())
        self.unchanged()

    def test_body_failure_keeps_completed_files_but_not_watermark(self):
        with mock.patch.object(sync, 'build_body', side_effect=['first complete\n', RuntimeError('body failure')]):
            with self.assertRaises(RuntimeError): self.run_main()
        self.assertEqual(len(self.bodies()), 1)
        self.unchanged()

    def test_index_failure_does_not_advance_watermark(self):
        with mock.patch.object(sync, 'rebuild_index', side_effect=RuntimeError('index failure')):
            with self.assertRaises(RuntimeError): self.run_main()
        self.assertEqual(len(self.bodies()), 3)
        self.unchanged()

    def test_unknown_legacy_watermark_is_not_discarded(self):
        for invalid in ['', None, 'not-a-date']:
            with self.subTest(invalid=invalid):
                self.set_state({**PRIOR, 'last_sync_at': invalid})
                with self.assertRaises(RuntimeError): self.run_main()
                self.unchanged()

    def test_nonobject_state_stops(self):
        self.set_state([])
        with self.assertRaises(RuntimeError): self.run_main()
        self.unchanged()

    def test_invalid_state_json_stops_without_rewriting(self):
        self.state.write_text('{broken', encoding='utf-8')
        self.original_state_bytes = self.state.read_bytes()
        with self.assertRaises(json.JSONDecodeError): self.run_main()
        self.unchanged()

    def test_atomic_state_replace_failure_preserves_previous_bytes(self):
        with mock.patch.object(sync.os, 'replace', side_effect=OSError('Synthetic disk error')):
            with self.assertRaises(OSError): sync.write_state({'last_sync_at': 'new'})
        self.unchanged()
        self.assertEqual([p.name for p in self.state.parent.iterdir()], [self.state.name])

    def test_atomic_body_replace_failure_preserves_previous_file(self):
        p = self.output / 'old.md'
        p.parent.mkdir(parents=True)
        p.write_text('original', encoding='utf-8')
        with mock.patch.object(sync.os, 'replace', side_effect=OSError('Synthetic disk error')):
            with self.assertRaises(OSError): sync.write_verified_text(p, 'new')
        self.assertEqual(p.read_text(), 'original')
        self.assertEqual(list(p.parent.iterdir()), [p])

    def test_verified_write_noop_avoids_replace(self):
        p = self.root / 'unchanged.md'
        p.write_text('same', encoding='utf-8')
        with mock.patch.object(sync.os, 'replace') as replace:
            sync.write_verified_text(p, 'same')
        replace.assert_not_called()

    def test_post_replace_readback_failure_is_not_success(self):
        p = self.root / 'readback.md'
        real = sync.os.replace
        def wrong_after_replace(src, dst):
            real(src, dst)
            Path(dst).write_text('unexpected', encoding='utf-8')
        with mock.patch.object(sync.os, 'replace', side_effect=wrong_after_replace):
            with self.assertRaisesRegex(RuntimeError, 'readback'): sync.write_verified_text(p, 'expected')

    def test_legacy_body_is_not_removed_before_successful_target_write(self):
        old = self.legacy / 'old.md'
        old.parent.mkdir(parents=True)
        old.write_text(f'---\nepisode_page_id: "{page(1)["id"]}"\n---\nold body\n', encoding='utf-8')
        with mock.patch.object(sync, 'write_verified_text', side_effect=OSError('Synthetic disk error')) as write:
            with self.assertRaisesRegex(RuntimeError, 'Unmanaged'):
                sync.sync_one('synthetic', page(1)['id'], False, {}, page(1))
        write.assert_not_called()
        self.assertTrue(old.exists())

    def test_negative_parameters_are_rejected_before_any_query(self):
        for flag in ['--hours-back', '--max-episodes']:
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit): self.run_main(flag, '-1')
        self.query.assert_not_called()
        self.unchanged()

    def test_same_title_and_short_id_collision_stops_without_false_complete_watermark(self):
        pages = [page(n) for n in (1, 2, 3)]
        for item in pages:
            item['properties']['Title']['title'][0]['plain_text'] = 'Shared synthetic title'
        self.query.return_value = response(pages)
        with self.assertRaisesRegex(RuntimeError, 'identity conflict'): self.run_main('--all-current')
        self.assertEqual(len(self.bodies()), 2)
        ids = {sync.parse_frontmatter(p.read_text())['episode_page_id'] for p in self.bodies()}
        self.assertEqual(ids, {page(1)['id'], page(2)['id']})
        self.unchanged()

    def test_unknown_target_identity_is_not_overwritten(self):
        target = self.output / 'reserved.md'
        target.parent.mkdir(parents=True)
        target.write_text('existing unrecognized content', encoding='utf-8')
        with mock.patch.object(sync, 'unique_target_path', return_value=target):
            with self.assertRaisesRegex(RuntimeError, 'identity conflict|Unmanaged episode path'):
                sync.sync_one('synthetic', page(1)['id'], False, {}, page(1))
        self.assertEqual(target.read_text(), 'existing unrecognized content')


    def test_root_block_partial_response_does_not_advance_watermark(self):
        self.query.return_value = response([page(1)])
        with mock.patch.object(sync, 'retrieve_block_children_checked', REAL_BLOCK_LOADER), mock.patch.object(sync, 'render_blocks', REAL_RENDER_BLOCKS), mock.patch.object(sync, 'notion_request', return_value=response([self.block(10)], True, None)):
            with self.assertRaisesRegex(RuntimeError, 'block cursor'): self.run_main('--all-current')
        self.assertFalse(self.bodies())
        self.unchanged()

    @staticmethod
    def block(number, children=False, kind='paragraph'):
        return {'id': f'10000000-0000-4000-8000-{number:012d}', 'type': kind, 'has_children': children,
                kind: {'rich_text': [{'plain_text': f'Synthetic block {number}'}]}}

    def test_nested_block_partial_response_does_not_advance_watermark(self):
        self.query.return_value = response([page(1)])
        replies = [response([self.block(10, True)]), response([self.block(11)], True, None)]
        with mock.patch.object(sync, 'retrieve_block_children_checked', REAL_BLOCK_LOADER), mock.patch.object(sync, 'render_blocks', REAL_RENDER_BLOCKS), mock.patch.object(sync, 'notion_request', side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, 'block cursor'): self.run_main('--all-current')
        self.assertFalse(self.bodies())
        self.unchanged()

    def test_nested_complete_blocks_use_checked_loader_at_every_depth(self):
        self.query.return_value = response([page(1)])
        replies = [response([self.block(10, True)]), response([self.block(11, True)]), response([self.block(12)])]
        with mock.patch.object(sync, 'retrieve_block_children_checked', REAL_BLOCK_LOADER), mock.patch.object(sync, 'render_blocks', REAL_RENDER_BLOCKS), mock.patch.object(sync, 'notion_request', side_effect=replies) as request, mock.patch.object(helper, 'retrieve_block_children', side_effect=AssertionError('Unchecked loader forbidden')):
            self.run_main('--all-current')
        self.assertEqual(request.call_count, 3)
        text = self.bodies()[0].read_text()
        for number in (10, 11, 12): self.assertIn(f'Synthetic block {number}', text)
        self.assertIn('    Synthetic block 12', text)
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_block_multipage_collection_is_fully_rendered(self):
        self.query.return_value = response([page(1)])
        replies = [response([self.block(10)], True, 'body-c1'), response([self.block(11)])]
        with mock.patch.object(sync, 'retrieve_block_children_checked', REAL_BLOCK_LOADER), mock.patch.object(sync, 'render_blocks', REAL_RENDER_BLOCKS), mock.patch.object(sync, 'notion_request', side_effect=replies) as request:
            self.run_main('--all-current')
        self.assertIn('start_cursor=body-c1', request.call_args_list[1].args[2])
        self.assertIn('Synthetic block 10', self.bodies()[0].read_text())
        self.assertIn('Synthetic block 11', self.bodies()[0].read_text())

    def test_block_unknown_shapes_stop(self):
        for invalid in [{}, None, {'results': [], 'has_more': None}, {'results': {}, 'has_more': False}]:
            with self.subTest(invalid=invalid), mock.patch.object(sync, 'notion_request', return_value=invalid):
                with self.assertRaises(RuntimeError): REAL_BLOCK_LOADER('synthetic', page(1)['id'])

    def test_block_identity_is_required_and_unique(self):
        for items in [[{}], [None], [self.block(10), self.block(10)]]:
            with self.subTest(items=items), mock.patch.object(sync, 'notion_request', return_value=response(items)):
                with self.assertRaises(RuntimeError): REAL_BLOCK_LOADER('synthetic', page(1)['id'])

    def test_block_repeated_or_conflicting_cursor_stops(self):
        for replies in [[response([], True, 'c1'), response([], True, 'c1')], [response([], False, 'unexpected')]]:
            with self.subTest(replies=replies), mock.patch.object(sync, 'notion_request', side_effect=replies):
                with self.assertRaises(RuntimeError): REAL_BLOCK_LOADER('synthetic', page(1)['id'])

    def test_block_second_request_failure_does_not_write_or_checkpoint(self):
        self.query.return_value = response([page(1)])
        replies = [response([self.block(10)], True, 'body-c1'), RuntimeError('body request failed')]
        with mock.patch.object(sync, 'retrieve_block_children_checked', REAL_BLOCK_LOADER), mock.patch.object(sync, 'render_blocks', REAL_RENDER_BLOCKS), mock.patch.object(sync, 'notion_request', side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, 'body request failed'): self.run_main('--all-current')
        self.assertFalse(self.bodies())
        self.unchanged()

    def test_helper_default_positional_depth_preserves_existing_callers(self):
        with mock.patch.object(helper, 'retrieve_block_children', return_value=[self.block(11)]) as old_loader:
            rendered = REAL_RENDER_BLOCKS('synthetic', [self.block(10, True)], 1)
        old_loader.assert_called_once_with('synthetic', self.block(10)['id'])
        self.assertIn('  Synthetic block 10', rendered)
        self.assertIn('    Synthetic block 11', rendered)

    def test_helper_custom_loader_propagates_recursively(self):
        loader = mock.Mock(side_effect=[[self.block(11, True)], [self.block(12)]])
        with mock.patch.object(helper, 'retrieve_block_children', side_effect=AssertionError('Default loader forbidden')):
            rendered = REAL_RENDER_BLOCKS('synthetic', [self.block(10, True)], children_loader=loader)
        self.assertEqual(loader.call_count, 2)
        self.assertIn('    Synthetic block 12', rendered)

    def test_zero_hours_does_not_jump_past_older_watermark(self):
        self.assertEqual(self.bounds(hours=0)[0]['on_or_after'], PRIOR['last_sync_at'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
