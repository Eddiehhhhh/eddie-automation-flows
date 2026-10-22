"""Synthetic attachment-completeness and direct-CLI privacy regression tests."""
import contextlib
import io
import sys
import traceback
import unittest
import urllib.error
from unittest import mock

import test_sync_my_podcast_cursor as fixtures

sync = fixtures.sync
URL1 = 'https://example.invalid/first.txt?signature=SYNTHETIC_SECRET_FIRST'
URL2 = 'https://example.invalid/second.txt?signature=SYNTHETIC_SECRET_SECOND'
TITLE = 'SYNTHETIC_PRIVATE_EPISODE_CANARY'
NAME1 = 'SYNTHETIC_PRIVATE_ATTACHMENT_ONE.txt'
NAME2 = 'SYNTHETIC_PRIVATE_ATTACHMENT_TWO.txt'
MISSING = object()


def attachment(url=URL1, kind='file', name=NAME1):
    return {'name': name, 'type': kind, kind: {'url': url}}


class AttachmentTests(unittest.TestCase):
    setUp = fixtures.CursorTests.setUp
    set_state = fixtures.CursorTests.set_state
    run_main = fixtures.CursorTests.run_main
    unchanged = fixtures.CursorTests.unchanged
    bodies = fixtures.CursorTests.bodies

    def source(self, files=MISSING, prop=MISSING):
        item = fixtures.page(1)
        item['properties']['Title']['title'][0]['plain_text'] = TITLE
        item['properties']['Description'] = {'type': 'rich_text', 'rich_text': [{'plain_text': 'Synthetic description.'}]}
        if prop is not MISSING:
            item['properties']['逐字稿'] = prop
        elif files is not MISSING:
            item['properties']['逐字稿'] = {'type': 'files', 'files': files}
        self.query.return_value = fixtures.response([item])
        self.fetch.return_value = item
        return item

    def seed(self):
        # Seed an actual managed note, not an ID-only unregistered path.
        note = sync.sync_one('synthetic', fixtures.page(1)['id'], False, {}, fixtures.page(1))
        index = self.output / '索引.md'
        index.write_text('ORIGINAL COMPLETE INDEX\n', encoding='utf-8')
        return {note: note.read_bytes(), index: index.read_bytes(), self.state: self.state.read_bytes()}

    def preserve_failure(self, files=MISSING, prop=MISSING, replies=None):
        before = self.seed()
        self.source(files, prop)
        replies = replies or {}
        visited = []
        def download(url, **kwargs):
            visited.append(url)
            result = replies[url]
            if isinstance(result, Exception): raise result
            return io.BytesIO(result)
        with mock.patch.object(sync.urllib.request, 'urlopen', side_effect=download), mock.patch.object(sync.time, 'sleep'):
            with self.assertRaises((RuntimeError, TypeError)): self.run_main('--all-current')
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        return visited

    def test_file_attachment_is_downloaded_into_body(self):
        self.source([attachment()])
        with mock.patch.object(sync.urllib.request, 'urlopen', return_value=io.BytesIO(b'FIRST_TRANSCRIPT')) as request:
            self.run_main('--all-current')
        request.assert_called_once_with(URL1, timeout=60)
        text = self.bodies()[0].read_text()
        self.assertIn('## 逐字稿\n\nFIRST_TRANSCRIPT', text)
        self.assertIn('transcript_file_count: 1', text)
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_external_attachment_is_downloaded_into_body(self):
        self.source([attachment(kind='external')])
        with mock.patch.object(sync.urllib.request, 'urlopen', return_value=io.BytesIO(b'EXTERNAL_TRANSCRIPT')):
            self.run_main('--all-current')
        self.assertIn('EXTERNAL_TRANSCRIPT', self.bodies()[0].read_text())

    def test_all_attachments_are_preserved_in_source_order_and_distinguished(self):
        self.source([attachment(), attachment(URL2, 'external', NAME2)])
        with mock.patch.object(sync.urllib.request, 'urlopen', side_effect=[io.BytesIO(b'FIRST_TRANSCRIPT'), io.BytesIO(b'SECOND_TRANSCRIPT')]) as request:
            self.run_main('--all-current')
        self.assertEqual([c.args[0] for c in request.call_args_list], [URL1, URL2])
        text = self.bodies()[0].read_text()
        expected = '### 逐字稿附件 1\n\nFIRST_TRANSCRIPT\n\n### 逐字稿附件 2\n\nSECOND_TRANSCRIPT'
        self.assertIn(expected, text)
        self.assertIn(NAME1, text)
        self.assertIn(NAME2, text)
        self.assertIn('transcript_file_count: 2', text)
        self.assertNotIn(URL1, text)
        self.assertNotIn(URL2, text)

    def test_three_attachments_with_same_name_remain_separate(self):
        third = 'https://example.invalid/third.txt'
        self.source([attachment(), attachment(URL2), attachment(third)])
        with mock.patch.object(sync.urllib.request, 'urlopen', side_effect=[io.BytesIO(b'one'), io.BytesIO(b'two'), io.BytesIO(b'three')]):
            self.run_main('--all-current')
        text = self.bodies()[0].read_text()
        for ordinal, body in [(1, 'one'), (2, 'two'), (3, 'three')]:
            self.assertIn(f'### 逐字稿附件 {ordinal}\n\n{body}', text)

    def test_attachment_whitespace_and_unicode_are_not_stripped(self):
        transcript = '\n  保留完整原文。\r\n\t尾部  \n\n'
        self.source([attachment()])
        with mock.patch.object(sync.urllib.request, 'urlopen', return_value=io.BytesIO(transcript.encode('utf-8'))):
            self.run_main('--all-current')
        # read_text normalizes CRLF, so inspect bytes for exact embedded UTF-8 content.
        self.assertIn(transcript.encode('utf-8'), self.bodies()[0].read_bytes())

    def test_nonempty_files_missing_url_fails_without_writes(self):
        self.preserve_failure([{'name': NAME1, 'type': 'file', 'file': {}}])

    def test_malformed_files_entry_fails_without_writes(self):
        self.preserve_failure([None])

    def test_malformed_files_container_fails_without_writes(self):
        self.preserve_failure({'unexpected': 'mapping'})

    def test_null_files_container_is_unknown_not_empty(self):
        self.preserve_failure(None)

    def test_present_null_property_is_unknown_not_absent(self):
        self.preserve_failure(prop=None)

    def test_present_wrong_property_type_is_unknown_not_absent(self):
        self.preserve_failure(prop={'type': 'rich_text', 'rich_text': []})

    def test_missing_files_member_is_unknown_not_empty(self):
        self.preserve_failure(prop={'type': 'files'})

    def test_unknown_attachment_type_fails_without_writes(self):
        self.preserve_failure([attachment(kind='unknown')])

    def test_nonmapping_file_payload_fails_without_writes(self):
        self.preserve_failure([{'name': NAME1, 'type': 'file', 'file': 'malformed'}])

    def test_invalid_attachment_name_fails_without_writes(self):
        self.preserve_failure([attachment(name=None)])

    def test_invalid_url_types_or_nonhttp_schemes_are_rejected(self):
        for url in [None, '', '  ', 'file:///tmp/private', 'data:text/plain,secret', 'https:///missing-host', 'https://user:password@example.invalid/a', 'https://example.invalid/has space', ' https://example.invalid/a', 'https://[bad/a']:
            with self.subTest(url=url):
                with self.assertRaises(RuntimeError): sync.prop_files({'type': 'files', 'files': [attachment(url)]})

    def test_second_entry_validation_happens_before_any_download(self):
        visited = self.preserve_failure([attachment(), {'type': 'file', 'file': {}}], replies={URL1: b'FIRST_TRANSCRIPT'})
        self.assertEqual(visited, [])

    def test_blank_first_attachment_fails_without_writes(self):
        self.preserve_failure([attachment()], replies={URL1: b' \n\t'})

    def test_bom_only_attachment_is_not_treated_as_content(self):
        self.preserve_failure([attachment()], replies={URL1: b'\xef\xbb\xbf \n'})

    def test_blank_second_attachment_fails_without_writes(self):
        visited = self.preserve_failure([attachment(), attachment(URL2)], replies={URL1: b'FIRST_TRANSCRIPT', URL2: b''})
        self.assertEqual(visited, [URL1, URL2])

    def test_invalid_utf8_first_attachment_fails_without_replacement(self):
        visited = self.preserve_failure([attachment()], replies={URL1: b'FIRST_TRANSCRIPT\xff'})
        self.assertEqual(visited, [URL1])

    def test_invalid_utf8_second_attachment_fails_without_writes(self):
        self.preserve_failure([attachment(), attachment(URL2)], replies={URL1: b'FIRST_TRANSCRIPT', URL2: b'SECOND_TRANSCRIPT\xff'})

    def test_first_http_failure_fails_without_writes_or_blind_403_retry(self):
        error = urllib.error.HTTPError(URL1, 403, 'Synthetic rejection', {}, None)
        visited = self.preserve_failure([attachment()], replies={URL1: error})
        self.assertEqual(visited, [URL1])

    def test_second_http_failure_fails_without_writes(self):
        error = urllib.error.HTTPError(URL2, 403, 'Synthetic rejection', {}, None)
        visited = self.preserve_failure([attachment(), attachment(URL2)], replies={URL1: b'FIRST_TRANSCRIPT', URL2: error})
        self.assertEqual(visited, [URL1, URL2])

    def test_network_timeout_fails_closed_after_bounded_retries(self):
        visited = self.preserve_failure([attachment()], replies={URL1: TimeoutError(URL1)})
        self.assertEqual(visited, [URL1, URL1, URL1])

    def test_transient_503_can_retry_without_exposing_error(self):
        self.source([attachment()])
        replies = [urllib.error.HTTPError(URL1, 503, TITLE, {}, None), io.BytesIO(b'RECOVERED_TRANSCRIPT')]
        with mock.patch.object(sync.urllib.request, 'urlopen', side_effect=replies), mock.patch.object(sync.time, 'sleep'):
            self.run_main('--all-current')
        self.assertIn('RECOVERED_TRANSCRIPT', self.bodies()[0].read_text())

    def test_absent_transcript_property_is_explicit_no_attachment_boundary(self):
        self.source()
        self.run_main('--all-current')
        text = self.bodies()[0].read_text()
        self.assertNotIn('## 逐字稿', text)
        self.assertIn('transcript_file_count: 0', text)

    def test_empty_declared_files_are_valid_no_attachment_boundary(self):
        self.source([])
        self.run_main('--all-current')
        self.assertNotIn('## 逐字稿', self.bodies()[0].read_text())
        self.assertNotEqual(self.state.read_bytes(), self.original_state_bytes)

    def test_dry_run_validates_structure_but_downloads_nothing(self):
        self.source([attachment(), attachment(URL2)])
        with mock.patch.object(sync.urllib.request, 'urlopen', side_effect=AssertionError('Dry run must not download')):
            self.run_main('--dry-run', '--all-current')
        self.assertFalse(self.output.exists())
        self.unchanged()

    def test_download_errors_suppress_signed_url_and_exception_chain(self):
        error = urllib.error.URLError(URL1 + TITLE)
        with mock.patch.object(sync.urllib.request, 'urlopen', side_effect=error), mock.patch.object(sync.time, 'sleep'):
            try: sync.download_text(URL1)
            except RuntimeError:
                rendered = traceback.format_exc()
            else: self.fail('Expected safe error')
        self.assertNotIn(URL1, rendered)
        self.assertNotIn(TITLE, rendered)
        self.assertIn('Transcript attachment download failed', rendered)

    def test_direct_success_stdout_contains_counts_not_identifiers_or_paths(self):
        self.source([attachment()])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sync.urllib.request, 'urlopen', return_value=io.BytesIO(b'PRIVATE_BODY_CANARY')), mock.patch.object(sys, 'argv', ['sync', '--all-current']), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(sync.run_cli(), 0)
        output = out.getvalue() + err.getvalue()
        for secret in [TITLE, NAME1, URL1, fixtures.page(1)['id'], 'PRIVATE_BODY_CANARY', str(self.output)]:
            self.assertNotIn(secret, output)
        self.assertIn('SYNC 1/1', output)
        self.assertIn('WROTE episode', output)
        self.assertIn('WROTE index', output)

    def test_direct_dry_run_stdout_contains_no_private_target(self):
        self.source([attachment()])
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['sync', '--all-current', '--dry-run']), contextlib.redirect_stdout(out):
            self.assertEqual(sync.run_cli(), 0)
        for secret in [TITLE, NAME1, URL1, fixtures.page(1)['id'], str(self.output)]:
            self.assertNotIn(secret, out.getvalue())
        self.assertIn('DRY-RUN episode target', out.getvalue())

    def test_cli_api_exception_payload_is_not_logged(self):
        self.query.side_effect = RuntimeError('SYNTHETIC_PRIVATE_PAYLOAD ' + URL1 + TITLE)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['sync', '--all-current']), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(sync.run_cli(), 1)
        self.assertEqual(err.getvalue(), 'ERROR PODCAST_SYNC_FAILED RuntimeError\n')
        for secret in [URL1, TITLE, 'SYNTHETIC_PRIVATE_PAYLOAD', 'Traceback']:
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
        self.unchanged()

    def test_cli_opaque_system_exit_payload_is_not_logged(self):
        with mock.patch.object(sync, 'main', side_effect=SystemExit(URL1 + TITLE)), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(sync.run_cli(), 1)
        self.assertEqual(err.getvalue(), 'ERROR PODCAST_SYNC_FAILED SystemExit\n')

    def test_cli_numeric_exit_code_is_preserved(self):
        with mock.patch.object(sync, 'main', side_effect=SystemExit(2)):
            self.assertEqual(sync.run_cli(), 2)

    def test_own_path_conflict_error_contains_no_private_filename(self):
        target = self.output / f'{TITLE}.md'
        target.parent.mkdir(parents=True)
        target.write_text('unmanaged note', encoding='utf-8')
        item = self.source([])
        with mock.patch.object(sync, 'unique_target_path', return_value=target):
            with self.assertRaises(RuntimeError) as error:
                sync.sync_one('synthetic', item['id'], False, {}, item)
        self.assertNotIn(TITLE, str(error.exception))
        self.assertNotIn(str(target), str(error.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
