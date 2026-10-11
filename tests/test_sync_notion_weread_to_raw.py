import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_notion_weread_to_raw as sync

A = '11111111-1111-1111-1111-111111111111'
B = '22222222-2222-2222-2222-222222222222'


def page(identity=A, title='Fixture book', start=None):
    return {'id': identity, 'properties': {
        'Name': {'type': 'title', 'title': [{'plain_text': title}]},
        '开始阅读时间': {'type': 'date', 'date': {'start': start} if start else None},
        '阅读状态': {'type': 'status', 'status': {'name': 'Reading'}},
    }}


class CanonicalPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'Raw' / '15 微信读书'
        self.patches = [patch.object(sync, 'ROOT', self.root), patch.object(sync, 'OUTPUT_ROOT', self.output)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def store(self, item, month=9):
        with patch.object(sync, 'now', return_value=dt.datetime(2026, month, 1, tzinfo=sync.TZ)):
            content, proposed = sync.render_book_note(item, {'id': A})
            paths = sync.BookPaths(self.output)
            target = paths.target(item, proposed)
            sync.atomic_write(target, content)
            paths.save()
            return target

    def test_cross_month_rename_status_and_new_start_reuse_path(self):
        original = self.store(page())
        changed = page(title='Renamed', start='2026-10-02')
        changed['properties']['阅读状态']['status']['name'] = 'Finished'
        self.assertEqual(original, self.store(changed, month=10))
        self.assertIn('Finished', original.read_text())
        self.assertEqual(1, len(list(self.output.rglob('*.md'))))

    def test_start_date_initial_layout_and_later_date_removal(self):
        original = self.store(page(start='2025-06-02'))
        self.assertEqual('2025/2025-06/Fixture book.md', str(original.relative_to(self.output)))
        self.assertEqual(original, self.store(page(), month=10))

    def test_same_title_different_ids_do_not_overwrite(self):
        first = self.store(page())
        second = self.store(page(B))
        self.assertNotEqual(first, second)
        self.assertEqual(A, sync.stored_page_id(first))
        self.assertEqual(B, sync.stored_page_id(second))

    def test_legacy_duplicates_with_unique_body_are_isolated(self):
        content, _ = sync.render_book_note(page(), {'id': A})
        old = self.output / '2026/2026-08/Old.md'
        new = self.output / '2026/2026-09/New.md'
        sync.atomic_write(old, content + '\nunique old body\n')
        sync.atomic_write(new, content + '\nunique new body\n')
        before = {p: p.read_bytes() for p in (old, new)}
        with self.assertRaisesRegex(sync.BookConflict, 'Multiple WeRead paths'):
            self.store(page(title='Renamed'), month=10)
        self.assertEqual(before, {p: p.read_bytes() for p in (old, new)})
        group = sync.BookPaths(self.output).duplicate_report()['groups'][0]
        self.assertTrue(group['conflict'])
        self.assertIsNone(group['canonical_path'])

    def test_legacy_source_id_single_file_is_reused(self):
        content, proposed = sync.render_book_note(page(), {'id': A})
        sync.atomic_write(proposed, content.replace('page_id:', 'source_id:'))
        self.assertEqual(proposed, self.store(page(title='Renamed'), month=10))

    def test_conflict_does_not_block_other_id_or_replace_full_index(self):
        content, _ = sync.render_book_note(page(), {'id': A})
        old, new = self.output / 'old.md', self.output / 'new.md'
        sync.atomic_write(old, content + 'unique old body')
        sync.atomic_write(new, content + 'unique new body')
        index = self.output / '索引.md'
        sync.atomic_write(index, 'existing complete index')
        args = type('Args', (), {'audit_report': None, 'dry_run': False, 'max_pages': 0})()
        with patch.object(sync, 'parse_args', return_value=args), patch.object(sync, 'load_notion_api_key', return_value='fixture'), patch.object(sync, 'resolve_database', return_value={'id': A}), patch.object(sync, 'paginate_database', return_value=[page(), page(B, 'Healthy')]):
            sync.main()
            sync.main()
        self.assertIn('unique old body', old.read_text())
        self.assertIn('unique new body', new.read_text())
        self.assertEqual('existing complete index', index.read_text())
        self.assertIn(B, sync.BookPaths(self.output).paths)
        health = json.loads((self.output / '.notion-path-conflicts.json').read_text())
        self.assertEqual('partial', health['status'])
        self.assertEqual(A, health['isolated'][0]['page_id'])
        self.assertEqual(4, len(list(self.output.rglob('*.md'))))

    def test_identical_copies_are_isolated_before_first_write_and_retry(self):
        content, _ = sync.render_book_note(page(), {'id': A})
        first, second = self.output / 'first.md', self.output / 'second.md'
        for path in (first, second):
            sync.atomic_write(path, content)
        before = {p: p.read_bytes() for p in (first, second)}
        for month in (10, 11):
            with self.assertRaises(sync.BookConflict):
                self.store(page(), month=month)
            self.assertEqual(before, {p: p.read_bytes() for p in (first, second)})
        self.assertIsNone(sync.BookPaths(self.output).duplicate_report()['groups'][0]['canonical_path'])

    def test_unfetched_conflict_keeps_index_and_partial_status(self):
        content, _ = sync.render_book_note(page(), {'id': A})
        first, second = self.output / 'first.md', self.output / 'second.md'
        sync.atomic_write(first, content + 'first unique body')
        sync.atomic_write(second, content + 'second unique body')
        before = {p: p.read_bytes() for p in (first, second)}
        index = self.output / '索引.md'
        sync.atomic_write(index, 'existing full index contains A')
        args = type('Args', (), {'audit_report': None, 'dry_run': False, 'max_pages': 0})()
        for pages in ([page(B, 'Healthy')], [page(A), page(B, 'Healthy')]):
            with patch.object(sync, 'parse_args', return_value=args), patch.object(sync, 'load_notion_api_key', return_value='fixture'), patch.object(sync, 'resolve_database', return_value={'id': A}), patch.object(sync, 'paginate_database', return_value=pages):
                sync.main()
            self.assertEqual(before, {p: p.read_bytes() for p in (first, second)})
            self.assertEqual('existing full index contains A', index.read_text())
            self.assertIn(B, sync.BookPaths(self.output).paths)
            health = json.loads((self.output / '.notion-path-conflicts.json').read_text())
            self.assertEqual('partial', health['status'])
            self.assertEqual(1, health['existing_conflict_count'])
            self.assertEqual(A, health['isolated'][0]['page_id'])
            self.assertEqual(len(pages) == 2, health['isolated'][0]['seen_in_scan'])

    def test_in_root_symlink_alias_cannot_reassign_another_identity(self):
        original = self.store(page())
        alias = self.output / 'alias'
        alias.symlink_to(original.parent, target_is_directory=True)
        alias_path = alias / original.name
        state = self.output / '.notion-page-paths.json'
        state.write_text(json.dumps({'version': 1, 'pages': {B: str(alias_path.relative_to(self.output))}}))
        before = original.read_bytes()
        with self.assertRaisesRegex(ValueError, 'escapes output root'):
            sync.BookPaths(self.output)
        self.assertEqual(before, original.read_bytes())
        self.assertEqual(A, sync.stored_page_id(original))

    def test_missing_canonical_is_rebuilt_at_reserved_path(self):
        original = self.store(page())
        original.unlink()
        self.assertEqual(original, self.store(page(title='New'), month=10))
        self.assertTrue(original.exists())

    def test_content_failure_keeps_old_note_and_state(self):
        original = self.store(page())
        before = original.read_bytes()
        state = (self.output / '.notion-page-paths.json').read_bytes()
        with patch.object(sync.os, 'replace', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                self.store(page(title='Changed'), month=10)
        self.assertEqual(before, original.read_bytes())
        self.assertEqual(state, (self.output / '.notion-page-paths.json').read_bytes())
        self.assertEqual(original, self.store(page(title='Changed'), month=10))
        self.assertEqual([], list(original.parent.glob('.weread-*')))

    def test_index_failure_after_note_write_recovered_by_scan(self):
        with patch.object(sync.BookPaths, 'save', side_effect=OSError('fixture state failure')):
            with self.assertRaises(OSError):
                self.store(page())
        original = next(self.output.rglob('*.md'))
        self.assertEqual(original, self.store(page(title='Renamed'), month=10))
        self.assertEqual(1, len(list(self.output.rglob('*.md'))))

    def test_unknown_note_is_not_overwritten(self):
        unknown = self.output / '2026/2026-09/Fixture book.md'
        sync.atomic_write(unknown, 'handwritten note')
        result = self.store(page())
        self.assertNotEqual(unknown, result)
        self.assertEqual('handwritten note', unknown.read_text())

    def test_bad_state_fails_closed(self):
        self.store(page())
        state_path = self.output / '.notion-page-paths.json'
        for relative in ('../outside.md', '/tmp/outside.md', '2026/../../outside.md'):
            state_path.write_text(json.dumps({'version': 1, 'pages': {A: relative}}))
            with self.assertRaises(ValueError):
                sync.BookPaths(self.output)
        state_path.write_text('{broken')
        with self.assertRaises(ValueError):
            sync.BookPaths(self.output)

    def test_identity_collision_and_invalid_id_fail_closed(self):
        first = self.store(page())
        (self.output / '.notion-page-paths.json').write_text(json.dumps({
            'version': 1, 'pages': {B: str(first.relative_to(self.output))}}))
        with self.assertRaises(ValueError):
            sync.BookPaths(self.output)
        with self.assertRaises(ValueError):
            sync.notion_page_id('')

    def test_main_dry_run_and_limited_run_preserve_index(self):
        index = self.output / '索引.md'
        sync.atomic_write(index, 'complete index')
        args = type('Args', (), {'audit_report': None, 'dry_run': True, 'max_pages': 1})()
        with patch.object(sync, 'parse_args', return_value=args), patch.object(sync, 'load_notion_api_key', return_value='fixture'), patch.object(sync, 'resolve_database', return_value={'id': A}), patch.object(sync, 'paginate_database', return_value=[page()]):
            sync.main()
            self.assertEqual([index], list(self.output.rglob('*.md')))
            self.assertFalse((self.output / '.notion-page-paths.json').exists())
            args.dry_run = False
            sync.main()
        self.assertEqual('complete index', index.read_text())

    def test_symlink_and_conflicting_legacy_identity_fail_closed(self):
        outside = self.root / 'outside.md'
        sync.atomic_write(outside, 'fixture')
        self.output.mkdir(parents=True)
        link = self.output / 'link.md'
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            sync.BookPaths(self.output)
        link.unlink()
        content, _ = sync.render_book_note(page(), {'id': A})
        sync.atomic_write(link, content.replace('page_id:', f'source_id: "{B}"\npage_id:'))
        with self.assertRaises(ValueError):
            sync.BookPaths(self.output)

    def test_full_index_points_to_canonical_path_after_rename(self):
        original = self.store(page())
        args = type('Args', (), {'audit_report': None, 'dry_run': False, 'max_pages': 0})()
        with patch.object(sync, 'parse_args', return_value=args), patch.object(sync, 'load_notion_api_key', return_value='fixture'), patch.object(sync, 'resolve_database', return_value={'id': A}), patch.object(sync, 'paginate_database', return_value=[page(title='New')]):
            sync.main()
        index = (self.output / '索引.md').read_text()
        self.assertIn(str(original.relative_to(self.output)), index)
        self.assertIn('| New |', index)

    def test_audit_cannot_overwrite_source_file(self):
        original = self.store(page())
        before = original.read_bytes()
        args = type('Args', (), {'audit_report': original})()
        with patch.object(sync, 'parse_args', return_value=args):
            with self.assertRaises(ValueError):
                sync.main()
        self.assertEqual(before, original.read_bytes())

    def test_triple_dash_fields_round_trip_and_retry(self):
        item = page(title='Title --- "quoted"')
        item['properties']['作者'] = {'type': 'rich_text', 'rich_text': [{'plain_text': 'Author --- test'}]}
        item['properties']['阅读状态']['status']['name'] = 'Status --- test'
        database = {'id': A, 'title': [{'plain_text': 'Database --- test'}]}
        content, proposed = sync.render_book_note(item, database)
        sync.atomic_write(proposed, content)
        self.assertEqual(A, sync.stored_page_id(proposed))
        self.assertEqual(proposed, self.store(item, month=10))
        self.assertEqual(proposed, self.store(item, month=11))

    def test_bom_crlf_and_single_quoted_identity(self):
        content, path = sync.render_book_note(page(), {'id': A})
        content = content.replace(f'page_id: "{A}"', f"page_id: '{A}'")
        path.parent.mkdir(parents=True)
        path.write_bytes(('\ufeff' + content.replace('\n', '\r\n')).encode('utf-8'))
        self.assertEqual(A, sync.stored_page_id(path))
        self.assertEqual(path, self.store(page(title='Changed')))

    def test_incomplete_header_and_body_cannot_supply_identity(self):
        content, path = sync.render_book_note(page(), {'id': A})
        header = content.split('\n---\n', 1)[0]
        for text in (header, 'body\n' + content, '---\ntitle: fixture\n---\n' + content):
            sync.atomic_write(path, text)
            self.assertEqual('', sync.stored_page_id(path))
        sync.atomic_write(path, f'---\nsource_system: notion\nsource_type: wechat_read\ndescription: |\n  page_id: {A}\n---\npage_id: {A}\n')
        self.assertEqual('', sync.stored_page_id(path))

    def test_duplicate_or_embedded_identity_cannot_authorize_overwrite(self):
        content, path = sync.render_book_note(page(), {'id': A})
        sync.atomic_write(path, content.replace('page_id:', f'page_id: "{A}"\npage_id:'))
        with self.assertRaises(ValueError):
            sync.stored_page_id(path)
        for value in ('prefix-' + A, 'https://example.com/' + A, '|', A + ' trailing'):
            with self.assertRaises(ValueError):
                sync.notion_page_id(value)

    def test_audit_mode_never_calls_api_or_changes_notes(self):
        original = self.store(page())
        before = original.read_bytes()
        report = self.root / 'audit.json'
        args = type('Args', (), {'audit_report': report})()
        with patch.object(sync, 'parse_args', return_value=args), patch.object(sync, 'load_notion_api_key', side_effect=AssertionError('API called')):
            sync.main()
        self.assertEqual(before, original.read_bytes())
        self.assertTrue(report.exists())


if __name__ == '__main__':
    unittest.main()
