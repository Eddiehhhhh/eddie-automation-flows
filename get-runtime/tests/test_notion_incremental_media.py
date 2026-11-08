import hashlib
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_incremental_media as media


class IncrementalMediaSource(unittest.TestCase):
    def setUp(self):
        self.ident = '1926106578851738352'
        self.asset_data = b'\xff\xd8\xfffixture\xff\xd9'
        self.blob = 'b' * 40
        self.sha = hashlib.sha256(self.asset_data).hexdigest()
        self.content = ('---\nnote_id: "' + self.ident + '"\n---\n'
                        '图片：\n![封面](../_assets/images/' + self.sha + '.jpg)\n').encode()
        self.path = 'Raw/03 Get/_assets/images/' + self.sha + '.jpg'
        self.asset = SimpleNamespace(commit='c' * 40, path=self.path, blob=self.blob,
                                     sha256=self.sha, data=self.asset_data,
                                     content_type='image/jpeg', block_type='image')
        self.row = {'source_metadata': {'inline_images': {
            'contract': 'get-inline-image-manifest-1',
            'images': [{'reference_sha256': 'd' * 64, 'source_sha256': 'e' * 64,
                        'asset_path': self.path, 'asset_blob': self.blob,
                        'asset_sha256': self.sha, 'asset_bytes': len(self.asset_data),
                        'asset_mime': 'image/jpeg'}]}}}

    def test_registry_manifest_binds_rewritten_markdown_to_exact_private_blob(self):
        with patch.object(media.private_assets, 'prepare', return_value=self.asset) as prepare:
            rows = media.source_assets(None, 'c' * 40, self.ident, self.content,
                                       'Raw/03 Get/03 纯文本记录/note.md', self.row)
        prepare.assert_called_once_with(None, 'c' * 40, self.path)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]['asset'], self.asset)
        self.assertEqual(rows[0]['reference'], '../_assets/images/' + self.sha + '.jpg')

    def test_manifest_path_mismatch_is_held_before_any_upload(self):
        row = {'source_metadata': {'inline_images': {
            **self.row['source_metadata']['inline_images'],
            'images': [dict(self.row['source_metadata']['inline_images']['images'][0],
                            asset_path='Raw/03 Get/_assets/images/other.jpg')]}}}
        with patch.object(media.private_assets, 'prepare') as prepare:
            with self.assertRaisesRegex(ValueError, 'source_content_held'):
                media.source_assets(None, 'c' * 40, self.ident, self.content,
                                    'Raw/03 Get/03 纯文本记录/note.md', row)
        prepare.assert_not_called()

    def test_private_blob_mismatch_is_held(self):
        wrong = SimpleNamespace(**{**vars(self.asset), 'blob': 'f' * 40})
        with patch.object(media.private_assets, 'prepare', return_value=wrong):
            with self.assertRaisesRegex(ValueError, 'source_content_held'):
                media.source_assets(None, 'c' * 40, self.ident, self.content,
                                    'Raw/03 Get/03 纯文本记录/note.md', self.row)

    def test_source_image_url_is_never_accepted_as_manifest_input(self):
        data = self.content.replace(b'../_assets/images/', b'https://signed.example/image/')
        with patch.object(media.private_assets, 'prepare') as prepare:
            with self.assertRaisesRegex(ValueError, 'source_content_held'):
                media.source_assets(None, 'c' * 40, self.ident, data,
                                    'Raw/03 Get/03 纯文本记录/note.md', self.row)
        prepare.assert_not_called()

    def test_existing_exact_hash_reuses_native_block_without_upload(self):
        snapshot = {'complete': True, 'native_blocks_available': True,
                    'recursive_blocks': [{'id': '1' * 32, 'type': 'image',
                                          'payload': {'fixture': True}}]}
        with patch.object(media.private_assets, 'canonical_native_file', return_value={
                'type': 'image', 'sha256': self.sha, 'size': len(self.asset_data)}) as readback:
            rows = media.match_existing_images(snapshot, [{'asset': self.asset}])
        self.assertEqual(rows[0]['block_id'], '1' * 32)
        readback.assert_called_once()

    def test_existing_different_hash_holds_instead_of_duplicating_or_overwriting(self):
        snapshot = {'complete': True, 'native_blocks_available': True,
                    'recursive_blocks': [{'id': '1' * 32, 'type': 'image',
                                          'payload': {'fixture': True}}]}
        with patch.object(media.private_assets, 'canonical_native_file', return_value={
                'type': 'image', 'sha256': 'f' * 64, 'size': len(self.asset_data)}), \
                patch.object(media.private_assets, 'publish') as upload:
            with self.assertRaisesRegex(ValueError, 'existing_content_differs'):
                media.match_existing_images(snapshot, [{'asset': self.asset}])
        upload.assert_not_called()

    def test_missing_receipt_is_empty_only_when_git_tree_confirms_absence(self):
        with patch('subprocess.check_output', return_value=b''):
            self.assertEqual(media.receipts_from_main(None, self.ident, self.content), {})

    def test_receipt_blob_read_failure_never_becomes_first_upload(self):
        state_path = media.STATE_DIR + '/' + self.ident + '.json'
        tree_row = ('100644 blob ' + 'a' * 40 + '\t' + state_path + '\0').encode()
        with patch('subprocess.check_output', side_effect=[tree_row,
                subprocess.CalledProcessError(128, 'git show')]):
            with self.assertRaisesRegex(ValueError, 'source_content_held'):
                media.receipts_from_main(None, self.ident, self.content)

    def test_checkpoint_failure_after_dispatch_never_sends_twice(self):
        data = b'\x89PNG\r\n\x1a\nfixture'
        sha = hashlib.sha256(data).hexdigest()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        asset = SimpleNamespace(data=data, sha256=sha, blob=blob,
            commit='c' * 40, path='Raw/03 Get/_assets/images/' + sha + '.png',
            content_type='image/png', filename=sha + '.png', block_type='image', fallback_url=None)
        key = media.media_state_key(asset)
        durable = {}
        sends = []
        uploaded = {'object': 'file_upload', 'id': '00000000-0000-0000-0000-000000000001',
                    'status': 'uploaded', 'content_length': len(data), 'filename': asset.filename,
                    'content_type': asset.content_type}

        def request(_token, method, path, body=None, content_type='application/json'):
            if method == 'GET' and path == '/users/me':
                return {'bot': {'workspace_limits': {'max_file_upload_size_in_bytes': 1024 * 1024}}}
            if method == 'POST' and path == '/file_uploads':
                return {'object': 'file_upload', 'id': uploaded['id'], 'status': 'pending'}
            if method == 'POST' and path.endswith('/send'):
                sends.append(path)
                return {'ok': True}
            if method == 'GET' and path == '/file_uploads/' + uploaded['id']:
                return uploaded
            self.fail('unexpected upload request')

        def checkpoint(state):
            receipt = state[key]
            if receipt.get('phase') == 'sent':
                raise OSError('simulated checkpoint failure')
            durable.update(state)

        with patch.object(media.private_assets, '_request', side_effect=request):
            with self.assertRaisesRegex(ValueError, 'missing_asset'):
                media.publish_missing([{'asset': asset}], 'token', {}, checkpoint)
            self.assertEqual(len(sends), 1)
            self.assertEqual(durable[key]['phase'], 'dispatched')
            self.assertTrue(durable[key]['no_retry'])
            result = media.publish_missing([{'asset': asset}], 'token', durable,
                                           lambda state: durable.update(state))
        self.assertEqual(len(sends), 1)
        self.assertEqual(result[key]['phase'], 'verified')

    def test_uploaded_block_readback_checks_real_bytes(self):
        assets = [{'asset': self.asset}]
        receipts = {media.media_state_key(self.asset): {'phase': 'verified',
                    'upload_id': '00000000-0000-0000-0000-000000000001'}}
        snapshot = {'recursive_blocks': [{'id': '1' * 32, 'type': 'image',
                                          'payload': {'url': 'https://files.example/temporary'}}]}
        with patch.object(media.private_assets, '_native_bytes', return_value=self.asset_data):
            result = media.bind_uploaded_snapshot(snapshot, assets, receipts)
        self.assertTrue(result['media_readback_verified'])
        self.assertEqual(result['recursive_blocks'][0]['payload']['sha256'], self.sha)
        with patch.object(media.private_assets, '_native_bytes', return_value=b'not the source bytes'):
            with self.assertRaisesRegex(ValueError, 'missing_asset'):
                media.bind_uploaded_snapshot(snapshot, assets, receipts)

if __name__ == '__main__':
    unittest.main()
