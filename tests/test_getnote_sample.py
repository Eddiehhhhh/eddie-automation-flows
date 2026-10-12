import contextlib
import email.message
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock
from pathlib import Path
from unittest.mock import patch
import urllib.request
import urllib.response
import urllib.error

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
    def test_authenticated_get_rejects_unlisted_path_before_opening(self):
        with patch.object(g.urllib.request, 'build_opener') as build:
            with self.assertRaisesRegex(ValueError, 'unsupported Get API path'):
                g.api_get('/elsewhere', {}, 'key', 'client')
        build.assert_not_called()

    def test_authenticated_get_rejects_redirects_and_proxy_without_leaking(self):
        secret = 'SYNTHETIC-GET-SECRET-DO-NOT-PRINT'
        private = 'SYNTHETIC-PRIVATE-BODY-DO-NOT-PRINT'
        class FakeHTTPS(urllib.request.HTTPSHandler):
            def __init__(self, target, status):
                super().__init__()
                self.target, self.status, self.calls = target, status, []
            def https_open(self, request):
                self.calls.append((request.full_url, dict(request.header_items())))
                headers = email.message.Message()
                headers['Location'] = self.target
                response = urllib.response.addinfourl(io.BytesIO(private.encode()), headers,
                                                       request.full_url, self.status)
                response.msg = private
                return response
        for status in (301, 302, 307, 308):
            handler = FakeHTTPS('https://other.invalid/?secret='+secret, status)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 g.NoRedirect(), handler)
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy.invalid:8080'}), \
                 patch.object(g.urllib.request, 'urlopen', side_effect=AssertionError('default opener used')), \
                 patch.object(g.urllib.request, 'build_opener', return_value=opener) as build, \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaisesRegex(RuntimeError, 'redirect blocked') as failure:
                    g.api_get(g.LIST_PATH, {}, secret, secret)
            self.assertEqual(len(handler.calls), 1)
            self.assertEqual(handler.calls[0][0], g.BASE_URL + g.LIST_PATH)
            self.assertEqual(handler.calls[0][1]['Authorization'], secret)
            self.assertEqual(handler.calls[0][1]['X-client-id'], secret)
            self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {}
                                for h in build.call_args.args))
            self.assertNotIn(secret, str(failure.exception)+out.getvalue()+err.getvalue())
            self.assertNotIn(private, str(failure.exception)+out.getvalue()+err.getvalue())

    def test_get_api_error_does_not_echo_upstream_message_or_secret(self):
        secret = 'SYNTHETIC-GET-SECRET-DO-NOT-PRINT'
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'success': False, 'error': {'code': 12345, 'message': 'private '+secret}}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(g.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(RuntimeError) as failure:
                g.api_get(g.LIST_PATH, {}, secret, secret)
        self.assertNotIn(secret, str(failure.exception))
        self.assertNotIn('private', str(failure.exception))

    def test_get_http_retry_boundaries_are_preserved_without_leaking_body(self):
        secret = 'SYNTHETIC-GET-SECRET-DO-NOT-PRINT'
        for status, expected_calls in ((401, 1), (429, 5), (500, 5)):
            def failure():
                return urllib.error.HTTPError(g.BASE_URL+g.LIST_PATH+'?secret='+secret,
                                              status, 'private '+secret, {}, io.BytesIO(secret.encode()))
            opener = MagicMock()
            opener.open.side_effect = [failure() for _ in range(expected_calls)]
            with patch.object(g.urllib.request, 'build_opener', return_value=opener), \
                 patch.object(g.time, 'sleep'):
                with self.assertRaises(RuntimeError) as raised:
                    g.api_get(g.LIST_PATH, {}, secret, secret)
            self.assertEqual(opener.open.call_count, expected_calls)
            self.assertNotIn(secret, str(raised.exception))

    def test_sample_api_response_byte_cap_fails_before_json_parse(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'x' * 1025
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(g.urllib.request, 'build_opener', return_value=opener):
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
        progress = {}
        with patch.object(g, 'fetch_note_pages', return_value=iter(pages)):
            with self.assertRaisesRegex(RuntimeError, 'bounded sample'):
                g.fetch_bounded_sample('key', 'client', 1, 'cursor', progress=progress)
        self.assertEqual(progress['sample_pages_read'], 1)

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

    def test_sync_cli_failure_emits_only_fixed_diagnostic(self):
        private = 'SYNTHETIC-PRIVATE-URL-AND-KEY'
        with patch.object(sys, 'argv', ['sync', '--sample']), \
             patch.dict(os.environ, {'VAULT_ROOT': '/tmp/private-shadow',
                                      'GETNOTE_API_KEY': 'key', 'GETNOTE_CLIENT_ID': 'client'}), \
             patch.object(g, 'fetch_bounded_sample', side_effect=RuntimeError(private)):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(g.run_cli(), 1)
        report = json.loads(out.getvalue())
        self.assertEqual((report['stage'], report['error_code']), ('list', 'unexpected'))
        self.assertNotIn(private, out.getvalue() + err.getvalue())
        self.assertEqual(g.safe_error_code('list', RuntimeError('bounded sample lacks ordinary or recording note')),
                         'sample_types_missing')
        self.assertEqual(g.safe_error_code('detail', RuntimeError('GetNote HTTP 401')), 'api_auth')

    def test_shadow_failure_classifies_pending_and_rejects_private_child_text(self):
        cases = [
            (1, json.dumps({'status': 'failed', 'stage': 'list',
                            'error_code': 'sample_types_missing', 'sample_pages_read': 2}),
             ('list', 'sample_types_missing')),
            (2, json.dumps({'scope': 'sample', 'pending': 1, 'fetched': 2,
                            'sample_pages': 2, 'private': 'SECRET'}),
             ('transcript', 'transcript_pending')),
            (1, 'SECRET private URL', ('launch', 'unknown_failure')),
        ]
        for exit_code, child_stdout, expected in cases:
            with self.subTest(exit_code=exit_code, expected=expected), \
                 patch.object(sys, 'argv', ['shadow', '--sample-pages', '2']), \
                 patch.object(shadow.subprocess, 'run', return_value=type('Result', (), {
                     'returncode': exit_code, 'stdout': child_stdout, 'stderr': 'SECRET key'})()):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(shadow.main(), 1)
                report = json.loads(out.getvalue())
                self.assertEqual((report['stage'], report['error_code']), expected)
                self.assertEqual(report['run'], 1)
                self.assertNotIn('SECRET', out.getvalue() + err.getvalue())

    def test_public_shadow_report_rejects_arbitrary_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.json'
            path.write_text(json.dumps({'status': 'failed', 'stage': 'private title',
                                        'error_code': 'SECRET', 'run': 1,
                                        'fetched': [2, 'SECRET']}))
            out = io.StringIO()
            shadow.publish_safe_report(path, out)
            self.assertNotIn('SECRET', out.getvalue())
            self.assertNotIn('private title', out.getvalue())
            self.assertIn('invalid_report', out.getvalue())
            path.write_text(json.dumps({'scope': 'sample', 'status': 'failed',
                                        'source_complete': True, 'stage': ['SECRET'],
                                        'error_code': ['SECRET'], 'run': 1}))
            out = io.StringIO()
            shadow.publish_safe_report(path, out)
            self.assertNotIn('SECRET', out.getvalue())
            self.assertFalse(json.loads(out.getvalue())['source_complete'])
            path.write_text(json.dumps({'scope': 'sample', 'status': 'failed',
                                        'stage': 'list', 'error_code': 'sample_types_missing',
                                        'sample_pages_limit': 2, 'run': 1,
                                        'private': 'SECRET'}))
            out = io.StringIO()
            shadow.publish_safe_report(path, out)
            public = json.loads(out.getvalue())
            self.assertEqual((public['stage'], public['error_code'], public['sample_pages_limit']),
                             ('list', 'sample_types_missing', 2))
            self.assertNotIn('SECRET', out.getvalue())


if __name__ == '__main__':
    unittest.main()
