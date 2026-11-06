import sys
import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import stage_getnote_asset_probe as probe


class PrivateGetAssetProbeTests(unittest.TestCase):
    def test_run_ref_is_limited_to_probe_namespace(self):
        self.assertEqual(probe.safe_ref('codex/get-asset-proof-12345-1'),
                         'codex/get-asset-proof-12345-1')
        for value in ('main', 'codex/get-asset-proof-', 'codex/other-123', '../main'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                probe.safe_ref(value)

    def test_only_exact_source_url_is_rewritten(self):
        source_url = 'https://get.example/private-image?signature=do-not-print'
        markdown = f'![diagram]({source_url})\n\n![other](https://elsewhere.invalid/x.png)\n'
        assets = [{'url': source_url, 'status': 'downloaded', 'path': '_assets/images/a.png',
                   'sha256': 'a' * 64}]
        rewritten, unmatched, matched = probe.rewrite_remote_image_links(
            markdown, assets, Path('/vault/Raw/03 Get/01 Notes/note.md'))
        self.assertIn('![diagram](../_assets/images/a.png)', rewritten)
        self.assertIn('![other](https://elsewhere.invalid/x.png)', rewritten)
        self.assertEqual(unmatched, 1)
        self.assertEqual(matched, {__import__('hashlib').sha256(source_url.encode()).hexdigest()})
        self.assertNotIn(source_url, rewritten)

    def test_non_downloaded_asset_fails_closed(self):
        with self.assertRaises(SystemExit):
            probe.rewrite_remote_image_links(
                '![x](https://example.invalid/a.png)',
                [{'url': 'https://example.invalid/a.png', 'status': 'not-downloaded',
                  'path': '_assets/images/a.png', 'sha256': 'a' * 64}],
                Path('/vault/Raw/03 Get/01 Notes/note.md'))

    def test_image_alias_is_normalized_and_exact_url_alias_rewritten(self):
        original = 'https://get.invalid/original.png?secret=private'
        markdown_url = 'https://get.invalid/display.png?other=private'
        attachments, urls, status = probe.normalize_image_attachments(
            {'images': [{'original_url': original, 'url': markdown_url}]})
        self.assertEqual(status, 'ok')
        self.assertEqual(urls, {original, markdown_url})
        self.assertEqual(attachments[0]['type'], 'image')
        rewritten, unmatched, matched = probe.rewrite_remote_image_links(
            f'![image]({markdown_url})\n',
            [{'url': original, 'status': 'downloaded', 'path': '_assets/images/a.png',
              'sha256': 'a' * 64}],
            Path('/vault/Raw/03 Get/01 Notes/note.md'), attachments)
        self.assertEqual(unmatched, 0)
        self.assertIn('../_assets/images/a.png', rewritten)
        self.assertNotIn(markdown_url, rewritten)
        self.assertIn(__import__('hashlib').sha256(original.encode()).hexdigest(), matched)

    def test_no_image_descriptor_is_distinguished(self):
        _, urls, status = probe.normalize_image_attachments({'attachments': []})
        self.assertEqual((urls, status), (set(), 'no_image_descriptors'))

    def test_candidate_diagnostics_keep_counts_only(self):
        note = {'note_id': '1234567890123456789',
                'images': [{'original_url': 'https://private.invalid/image?token=secret'}]}
        descriptors, urls, status = probe.normalize_image_attachments(note)
        previous = dict(probe.CANDIDATE_DIAGNOSTICS)
        previous_stage = probe.CURRENT_STAGE
        previous_category = probe.CURRENT_FAILURE_CATEGORY
        previous_reported = probe.FAILURE_REPORTED
        try:
            probe.CANDIDATE_DIAGNOSTICS.update(dict.fromkeys(probe.CANDIDATE_DIAGNOSTIC_NAMES, 0))
            probe.record_candidate_diagnostics(note, descriptors, urls, status,
                                               {'https://private.invalid/image?token=secret'})
            self.assertEqual(probe.CANDIDATE_DIAGNOSTICS['checked'], 1)
            self.assertEqual(probe.CANDIDATE_DIAGNOSTICS['images_field'], 1)
            self.assertEqual(probe.CANDIDATE_DIAGNOSTICS['image_descriptors'], 1)
            self.assertEqual(probe.CANDIDATE_DIAGNOSTICS['source_url_matches'], 1)
            with TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_OUTPUT': str(Path(temp) / 'output')}):
                probe.CURRENT_STAGE = 'match_image_source'
                probe.CURRENT_FAILURE_CATEGORY = 'no_image_descriptors'
                probe.FAILURE_REPORTED = False
                probe.report_failure_stage()
                output = (Path(temp) / 'output').read_text()
                self.assertIn('candidate_stats=1,1,0,0,1,0,0,1,1,0,0,1,0', output)
                self.assertNotIn('1234567890123456789', output)
                self.assertNotIn('private.invalid', output)
                self.assertNotIn('secret', output)
        finally:
            probe.CANDIDATE_DIAGNOSTICS.clear()
            probe.CANDIDATE_DIAGNOSTICS.update(previous)
            probe.CURRENT_STAGE = previous_stage
            probe.CURRENT_FAILURE_CATEGORY = previous_category
            probe.FAILURE_REPORTED = previous_reported

    def test_failure_stage_output_is_fixed_and_private(self):
        with TemporaryDirectory() as temp:
            output_path = Path(temp) / 'github-output'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output_path)}):
                old_stage = probe.CURRENT_STAGE
                old_category = probe.CURRENT_FAILURE_CATEGORY
                old_reported = probe.FAILURE_REPORTED
                old_diagnostics = dict(probe.CANDIDATE_DIAGNOSTICS)
                try:
                    probe.CANDIDATE_DIAGNOSTICS.update(dict.fromkeys(probe.CANDIDATE_DIAGNOSTIC_NAMES, 0))
                    probe.CURRENT_STAGE = 'publish_private_ref'
                    probe.CURRENT_FAILURE_CATEGORY = 'not_found'
                    probe.FAILURE_REPORTED = False
                    probe.report_failure_stage()
                    probe.report_failure_stage()
                    self.assertEqual(output_path.read_text(),
                                     'failure_stage=publish_private_ref\nfailure_category=not_found\n'
                                     'candidate_stats=0,0,0,0,0,0,0,0,0,0,0,0,0\n')
                finally:
                    probe.CURRENT_STAGE = old_stage
                    probe.CURRENT_FAILURE_CATEGORY = old_category
                    probe.FAILURE_REPORTED = old_reported
                    probe.CANDIDATE_DIAGNOSTICS.clear()
                    probe.CANDIDATE_DIAGNOSTICS.update(old_diagnostics)

    def test_detail_failures_are_reduced_to_static_categories(self):
        cases = {'GetNote HTTP 401': 'auth_denied', 'GetNote HTTP 404': 'not_found',
                 'GetNote HTTP 429': 'rate_limited', 'GetNote HTTP 503': 'server_error',
                 'GetNote API error 10202': 'api_rejected',
                 'GetNote transport failed': 'transport_error',
                 'sensitive response body': 'detail_error'}
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(probe.detail_failure_category(RuntimeError(message)), expected)

    def test_candidate_ids_come_only_from_private_queue_and_are_bounded(self):
        ids = ['1234567890123456789', '2234567890123456789',
               '3234567890123456789', '4234567890123456789']
        queue = {'pending': ids}
        registry = {'newById': {ident: {'filePath': '01 Notes/note-' + ident + '.md'} for ident in ids}}
        payloads = {
            probe.QUEUE_PATH: __import__('json').dumps(queue).encode(),
            probe.REGISTRY_PATH: __import__('json').dumps(registry).encode(),
        }
        for ident in ids:
            payloads['Raw/03 Get/01 Notes/note-' + ident + '.md'] = (
                '---\nnote_id: "' + ident + '"\n---\n![img](https://get.invalid/' + ident + '.png)\n').encode()
        def fake_git(_root, *args, **_kwargs):
            if args[0:2] == ('cat-file', '-s'):
                return str(len(payloads[args[-1].removeprefix('HEAD:')])).encode()
            return payloads[args[-1].removeprefix('HEAD:')]
        with patch.object(probe, 'git', side_effect=fake_git):
            found = probe.pending_image_candidates(Path('/private/vault'))
        expected_count = min(len(ids), probe.MAX_CANDIDATES)
        self.assertEqual(len(found), expected_count)
        self.assertEqual([ident for ident, _ in found], ids[:expected_count])


if __name__ == '__main__':
    unittest.main()
