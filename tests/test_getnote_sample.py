import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_getnote_to_raw as g
import verify_shadow_repeat as shadow


def note(ident, kind='plain_text'):
    result = {'id': ident, 'title': 'private title', 'content': 'BEGIN private body END',
              'note_type': kind, 'created_at': '2020-01-01T00:00:00Z',
              'updated_at': '2020-01-01T00:00:00Z'}
    if kind == 'recorder_audio':
        result['audio'] = {'original': 'BEGIN private transcript END'}
    return result


class GetSampleTests(unittest.TestCase):
    def test_sample_api_response_byte_cap_fails_before_json_parse(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'x' * 1025
        with patch.object(g.urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'response byte limit'):
                g.api_get(g.LIST_PATH, {}, 'key', 'client', max_response_bytes=1024)

    def test_bounded_selection_stops_after_two_pages_and_requires_both_types(self):
        pages = [(1, [note('ordinary')], True),
                 (2, [note('recording', 'recorder_audio')], True),
                 (3, [note('extra')], True)]
        with patch.object(g, 'fetch_note_pages', return_value=iter(pages)) as fetch:
            selected, read_pages = g.fetch_bounded_sample('key', 'client', 2, 'cursor')
        self.assertEqual([g.note_id_from(x) for x in selected], ['ordinary', 'recording'])
        self.assertEqual(read_pages, 2)
        self.assertEqual(fetch.call_args.args[2:], (None, 'cursor'))
        with patch.object(g, 'fetch_note_pages', return_value=iter(pages)):
            with self.assertRaisesRegex(RuntimeError, 'bounded sample'):
                g.fetch_bounded_sample('key', 'client', 1, 'cursor')

    def test_sample_marks_source_incomplete_enforces_asset_budgets_and_repeats_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / 'Raw' / '03 Get'
            details = {'ordinary': note('ordinary'), 'recording': note('recording', 'recorder_audio')}
            with patch.multiple(g, ROOT=root, RAW_DIR=raw, NOTES_DIR=raw/'01 Notes',
                                STATE_DIR=raw/'03 State'), \
                 patch.dict(os.environ, {'VAULT_ROOT': str(root)}), \
                 patch.object(sys, 'argv', ['sync', '--sample', '--sample-pages', '2']), \
                 patch.object(g, 'load_credentials', return_value=('key', 'client')), \
                 patch.object(g, 'fetch_bounded_sample', return_value=(list(details.values()), 2)), \
                 patch.object(g, 'fetch_note_detail', side_effect=lambda ident, *_: details[ident]), \
                 patch.object(g, 'AssetStore') as asset_store:
                asset_store.return_value.materialize.return_value = []
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(g.main(), 0)
                first = json.loads(output.getvalue())
                self.assertEqual((first['scope'], first['source_complete'], first['sample_pages']), ('sample', False, 2))
                self.assertEqual(first['fetched'], 2)
                self.assertEqual(asset_store.call_args.kwargs,
                                 {'max_file': 2*1024*1024, 'max_run': 4*1024*1024, 'max_assets': 2})
                receipt = json.loads((raw/'03 State'/'receipt.json').read_text())
                self.assertEqual((receipt['scope'], receipt['complete']), ('sample', False))
                text = '\n'.join(p.read_text() for p in raw.rglob('*.md'))
                self.assertIn('BEGIN private body END', text)
                self.assertIn('BEGIN private transcript END', text)
                before = {p.relative_to(raw): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in raw.rglob('*') if p.is_file()}
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(g.main(), 0)
                self.assertEqual(before, {p.relative_to(raw): (p.read_bytes(), p.stat().st_mtime_ns)
                                          for p in raw.rglob('*') if p.is_file()})

    def test_sample_rejects_more_than_two_attachment_descriptors_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root/'Raw'/'03 Get'
            ordinary = note('ordinary')
            ordinary['attachments'] = [{'type': 'audio', 'url': 'https://example.invalid/a'}] * 3
            recording = note('recording', 'recorder_audio')
            details = {'ordinary': ordinary, 'recording': recording}
            with patch.multiple(g, ROOT=root, RAW_DIR=raw, NOTES_DIR=raw/'01 Notes',
                                STATE_DIR=raw/'03 State'), \
                 patch.dict(os.environ, {'VAULT_ROOT': str(root)}), \
                 patch.object(sys, 'argv', ['sync', '--sample']), \
                 patch.object(g, 'load_credentials', return_value=('key', 'client')), \
                 patch.object(g, 'fetch_bounded_sample', return_value=(list(details.values()), 1)), \
                 patch.object(g, 'fetch_note_detail', side_effect=lambda ident, *_: details[ident]):
                with self.assertRaisesRegex(RuntimeError, 'attachment count'):
                    g.main()
            self.assertFalse(raw.exists())

    def test_shadow_defaults_to_sample_and_never_prints_private_content(self):
        commands = []
        def run(command, env, capture_output, text):
            commands.append(command)
            root = Path(env['VAULT_ROOT'])
            target = root/'private-title.md'
            if not target.exists():
                target.write_text('BEGIN private transcript END')
            return type('Result', (), {'returncode': 0, 'stdout': json.dumps({
                'scope': 'sample', 'source_complete': False, 'fetched': 2,
                'written_or_changed': 0}), 'stderr': 'private error'})()
        with patch.object(sys, 'argv', ['shadow']), patch.object(shadow.subprocess, 'run', side_effect=run):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(shadow.main(), 0)
        self.assertEqual(len(commands), 2)
        self.assertTrue(all('--sample' in command for command in commands))
        report = json.loads(output.getvalue())
        self.assertEqual((report['scope'], report['source_complete'], report['status']), ('sample', False, 'sample_ok'))
        self.assertNotIn('private', output.getvalue())

    def test_full_shadow_requires_explicit_flag_and_rejects_sample_report(self):
        commands = []
        def run(command, env, capture_output, text):
            commands.append(command)
            return type('Result', (), {'returncode': 0, 'stdout': json.dumps({
                'scope': 'sample', 'source_complete': False, 'fetched': 2,
                'written_or_changed': 0}), 'stderr': 'private error'})()
        with patch.object(sys, 'argv', ['shadow', '--full']), \
             patch.object(shadow.subprocess, 'run', side_effect=run):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(shadow.main(), 1)
        self.assertEqual(len(commands), 1)
        self.assertNotIn('--sample', commands[0])
        report = json.loads(output.getvalue())
        self.assertEqual(report['status'], 'invalid-source-report')
        self.assertFalse(report['source_complete'])
        self.assertNotIn('private', output.getvalue())


if __name__ == '__main__':
    unittest.main()
