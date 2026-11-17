"""Purely synthetic media contracts. No transport opens a network connection."""
import base64
import copy
import hashlib
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from core import Hold, snapshot
from media_delivery import GitHubMedia, NotionMedia, PART_BYTES, manifest_assets
from notion_sink import NotionSink
from retry import HTTPFailure
from test_core import GitFake, note
from test_notion_sink import Fake, Journal


class MediaFake(Fake):
    def __init__(self, journal):
        super().__init__()
        self.journal = journal
        self.uploads = {}
        self.upload_data = {}
        self.upload_writes = 0
        self.upload_bodies = []
        self.send_calls = 0
        self.lose = None
        self.file_bytes = {}

    def _intent(self, phase):
        assert any(value.get('phase') == phase for value in self.journal.data.values())

    def request(self, method, path, body=None):
        if path == '/v1/file_uploads':
            self._intent('upload_create_intent')
            self.upload_writes += 1
            self.upload_bodies.append(copy.deepcopy(body))
            uid = f'{self.upload_writes:032x}'
            value = {'id': uid, 'status': 'pending', 'parts': body.get('number_of_parts', 1)}
            self.uploads[uid] = value
            self.upload_data[uid] = []
            if self.lose == 'create':
                self.lose = None
                raise TimeoutError('synthetic_response_loss')
            return copy.deepcopy(value)
        if path.startswith('/v1/file_uploads/'):
            uid = path.split('/')[3]
            if method == 'POST':
                self._intent('upload_complete_intent')
                self.uploads[uid]['status'] = 'uploaded'
                if self.lose == 'complete':
                    self.lose = None
                    raise TimeoutError('synthetic_response_loss')
            return copy.deepcopy(self.uploads[uid])
        if method == 'PATCH' and body and body.get('children', [{}])[0].get('type') == 'toggle':
            first = body['children'][0]
            children = first['toggle'].get('children')
            if children:
                self._intent('attach_intent')
                parent = path.split('/')[3]
                self.counter += 1
                block = copy.deepcopy(first)
                block['id'] = f'b{self.counter}'
                nested = block['toggle'].pop('children')
                for child in nested:
                    self.counter += 1
                    child['id'] = f'b{self.counter}'
                    media = child['file']
                    uid = media.pop('file_upload')['id']
                    url = 'https://notion-media.example.test/' + uid
                    self.file_bytes[url] = b''.join(self.upload_data[uid])
                    media.update(type='file', file={'url': url})
                self.blocks[block['id']] = nested
                self.blocks[parent].append(block)
                if self.lose == 'attach':
                    self.lose = None
                    raise TimeoutError('synthetic_response_loss')
                return {'results': [copy.deepcopy(block)]}
        return super().request(method, path, body)

    def send_file(self, uid, filename, mime, data, part_number=None):
        self._intent('part_send_intent')
        assert '/' not in filename and '\n' not in filename
        self.send_calls += 1
        self.upload_data[uid].append(data)
        if part_number is None:
            self.uploads[uid]['status'] = 'uploaded'
        if self.lose == 'send':
            self.lose = None
            raise TimeoutError('synthetic_response_loss')

    def download(self, url, sha, size):
        return self.file_bytes[url]


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.data = b'synthetic plain text\n'
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.asset = {'sha256': self.sha, 'bytes': len(self.data), 'local_name': self.sha + '.bin',
                      'source_field': 'attachments/0', 'content_type': 'text/plain',
                      'source_url_sha256': hashlib.sha256(b'https://source.example.test/a').hexdigest()}
        p = self.root / self.asset['local_name']
        p.write_bytes(self.data)
        p.chmod(0o600)
        n = note()
        n['attachments'] = [{'url': 'https://source.example.test/a', 'type': 'file'}]
        self.snap = snapshot(n)
        self.manifest = {'source_id': self.snap['id'], 'source_version': self.snap['version'],
                         'coverage': 'declared_attachments_and_audio_only', 'assets': [self.asset]}
        self.journal = Journal()
        self.http = MediaFake(self.journal)
        self.text = NotionSink(self.http, 'db', self.journal, namespace='test')
        self.receipt = self.text.sync(self.snap)
        self.sink = NotionMedia(self.http, 'db', self.journal, namespace='test', download=self.http.download)

    def sync(self):
        return self.sink.sync(self.snap, self.receipt, self.manifest, self.root)

    def test_github_binary_and_repeat_and_private_gate(self):
        gh = GitFake()
        sink = GitHubMedia(gh, 'owner/private', 'proof', 'test')
        result = sink.sync(self.asset, self.data)
        self.assertTrue(result['binary_assets_verified'])
        self.assertEqual(sink.sync(self.asset, self.data), result)
        self.assertEqual(gh.writes, 1)
        gh.private = False
        with self.assertRaisesRegex(Hold, 'private_repository'):
            sink.sync(self.asset, self.data)

    def test_github_uncertain_write_reconciles_without_repeat(self):
        gh = GitFake()
        gh.uncertain = True
        sink = GitHubMedia(gh, 'owner/private', 'proof', 'test')
        with self.assertRaises(Hold):
            sink.sync(self.asset, self.data)
        self.assertTrue(sink.sync(self.asset, self.data)['binary_assets_verified'])
        self.assertEqual(gh.writes, 1)

    def test_github_conflict_and_digest_mismatch(self):
        gh = GitFake()
        sink = GitHubMedia(gh, 'owner/private', 'proof', 'test')
        sink.sync(self.asset, self.data)
        gh.file['content'] = base64.b64encode(b'edited').decode()
        with self.assertRaisesRegex(Hold, 'conflict'):
            sink.sync(self.asset, self.data)
        with self.assertRaisesRegex(Hold, 'local_bytes_mismatch'):
            sink.sync(self.asset, b'bad')
        self.assertEqual(gh.writes, 1)

    def test_github_restore_reads_only_and_does_not_overwrite_local_corruption(self):
        gh = GitFake()
        sink = GitHubMedia(gh, 'owner/private', 'proof', 'test')
        sink.sync(self.asset, self.data)
        calls = []
        original = gh.request
        def request(method, *args):
            calls.append(method)
            return original(method, *args)
        gh.request = request
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            self.assertTrue(sink.restore(self.asset, root))
            path = root / self.asset['local_name']
            self.assertEqual(path.read_bytes(), self.data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(all(method == 'GET' for method in calls))
            path.write_bytes(b'corrupt')
            with self.assertRaisesRegex(Hold, 'local_bytes_mismatch'):
                sink.restore(self.asset, root)
            self.assertEqual(path.read_bytes(), b'corrupt')
        self.assertEqual(gh.writes, 1)

    def test_github_restore_missing_or_corrupt_remote_bytes_never_publishes_cache(self):
        gh = GitFake()
        sink = GitHubMedia(gh, 'owner/private', 'proof', 'test')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            self.assertFalse(sink.restore(self.asset, root))
            self.assertEqual(list(root.iterdir()), [])
            sink.sync(self.asset, self.data)
            gh.file['content'] = base64.b64encode(b'corrupt').decode()
            with self.assertRaisesRegex(Hold, 'local_bytes_mismatch'):
                sink.restore(self.asset, root)
            self.assertEqual(list(root.iterdir()), [])

    def test_notion_complete_and_repeat_verifies_bytes_without_target_write(self):
        result = self.sync()
        self.assertTrue(result['binary_assets_verified'])
        target_writes = len([x for x in self.http.calls if x[0] == 'PATCH'])
        self.assertEqual(self.sync(), result)
        self.assertEqual(self.http.upload_writes, 1)
        self.assertEqual(self.http.send_calls, 1)
        self.assertEqual(target_writes, len([x for x in self.http.calls if x[0] == 'PATCH']))
        self.assertEqual(len(self.text.block_text(self.receipt['block_id'])), 1)

    def test_notion_send_response_loss_retrieves_status_and_never_resends(self):
        self.http.lose = 'send'
        with self.assertRaisesRegex(Hold, 'transport_uncertain'):
            self.sync()
        self.assertTrue(self.sync()['binary_assets_verified'])
        self.assertEqual(self.http.send_calls, 1)
        self.assertEqual(self.http.upload_writes, 1)

    def test_notion_create_response_loss_holds_without_duplicate_upload(self):
        self.http.lose = 'create'
        with self.assertRaisesRegex(Hold, 'transport_uncertain'):
            self.sync()
        with self.assertRaisesRegex(Hold, 'create_requires_reconcile'):
            self.sync()
        self.assertEqual(self.http.upload_writes, 1)
        self.assertEqual(self.http.send_calls, 0)

    def test_notion_attach_response_loss_reconciles_one_toggle(self):
        self.http.lose = 'attach'
        with self.assertRaisesRegex(Hold, 'transport_uncertain'):
            self.sync()
        self.assertTrue(self.sync()['binary_assets_verified'])
        self.assertEqual(len(self.http.blocks[self.receipt['page_id']]), 2)
        self.assertEqual(self.http.upload_writes, 1)

    def test_notion_missing_uncertain_attach_holds(self):
        self.http.lose = 'attach'
        with self.assertRaisesRegex(Hold, 'transport_uncertain'):
            self.sync()
        self.http.blocks[self.receipt['page_id']].pop()
        with self.assertRaisesRegex(Hold, 'attach_requires_reconcile'):
            self.sync()
        self.assertEqual(self.http.upload_writes, 1)

    def test_notion_changed_media_and_text_are_held(self):
        result = self.sync()
        toggle = result['assets'][0]['toggle_id']
        self.http.blocks[toggle][0]['file']['caption'][0]['text']['content'] = 'human change'
        with self.assertRaisesRegex(Hold, 'caption_modified'):
            self.sync()
        self.http.blocks[self.receipt['block_id']][0]['paragraph']['rich_text'][0]['text']['content'] = 'human text'
        with self.assertRaisesRegex(Hold, 'managed_text_modified'):
            self.sync()

    def test_transport_and_download_errors_do_not_expose_private_url(self):
        self.http.download = lambda *args: None
        def failed_download(*args):
            raise RuntimeError('https://private.example.test/file?signature=secret')
        self.sink.download = failed_download
        with self.assertRaisesRegex(Hold, '^media_download_uncertain$'):
            self.sync()
        def failed_request(*args):
            raise RuntimeError('Bearer private-token')
        self.http.request = failed_request
        with self.assertRaisesRegex(Hold, '^media_transport_uncertain$'):
            self.sync()

    def test_notion_unsupported_mime_holds_before_upload_or_intent(self):
        before = copy.deepcopy(self.journal.data)
        for mime in ('application/octet-stream', 'not-a-mime', 'application/x-unknown'):
            self.asset['content_type'] = mime
            with self.assertRaisesRegex(Hold, '^media_notion_unsupported_mime$'):
                self.sync()
        self.assertEqual(self.http.upload_writes, 0)
        self.assertEqual(self.journal.data, before)

    def test_http_failure_status_is_retained_without_response_or_headers(self):
        original = self.http.request
        def rejected(method, path, body=None):
            if path == '/v1/file_uploads':
                raise HTTPFailure(400, 'https://private.example.test/?signature=secret')
            return original(method, path, body)
        self.http.request = rejected
        with self.assertRaisesRegex(Hold, '^media_http_400$'):
            self.sync()
        with self.assertRaisesRegex(Hold, '^media_upload_create_requires_reconcile$'):
            self.sync()
        self.assertEqual(self.http.upload_writes, 0)

    def test_send_http_failure_is_retained_and_not_repeated(self):
        def rejected(*args, **kwargs):
            self.http.send_calls += 1
            raise HTTPFailure(413, 'private-response-header')
        self.http.send_file = rejected
        with self.assertRaisesRegex(Hold, '^media_http_413$'):
            self.sync()
        with self.assertRaisesRegex(Hold, '^media_upload_send_requires_reconcile$'):
            self.sync()
        self.assertEqual(self.http.send_calls, 1)

    def test_wav_audio_has_supported_filename_on_all_python_versions(self):
        self.asset['content_type']='audio/wav'
        self.assertTrue(self.sync()['binary_assets_verified'])
        self.assertTrue(self.http.upload_bodies[0]['filename'].endswith('.wav'))

    def test_audio_ogg_is_not_downgraded_to_octet_stream(self):
        self.asset['content_type'] = 'audio/ogg'
        self.assertTrue(self.sync()['binary_assets_verified'])
        self.assertEqual(self.http.upload_bodies[0]['content_type'], 'audio/ogg')
        self.assertTrue(self.http.upload_bodies[0]['filename'].endswith('.oga')
                        or self.http.upload_bodies[0]['filename'].endswith('.ogg'))

    def test_multipart_complete_response_loss_uses_uploaded_status(self):
        self.data = b'x' * (20 * 1024 * 1024 + 1)
        self.asset.update(sha256=hashlib.sha256(self.data).hexdigest(), bytes=len(self.data))
        self.asset['local_name'] = self.asset['sha256'] + '.bin'
        path = self.root / self.asset['local_name']
        path.write_bytes(self.data)
        path.chmod(0o600)
        self.http.lose = 'complete'
        with self.assertRaisesRegex(Hold, '^media_transport_uncertain$'):
            self.sync()
        self.assertTrue(self.sync()['binary_assets_verified'])
        self.assertEqual(self.http.upload_writes, 1)
        self.assertEqual(self.http.send_calls, 3)

    def test_notion_download_digest_mismatch_never_verified(self):
        self.sink.download = lambda *args: b'corrupt'
        with self.assertRaisesRegex(Hold, 'local_bytes_mismatch'):
            self.sync()
        self.assertFalse(any(v.get('phase') == 'verified' for k, v in self.journal.data.items() if k.startswith('media:')))

    def test_notion_corrupt_local_asset_rejected_before_upload(self):
        (self.root / self.asset['local_name']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(Hold, 'local_bytes_mismatch'):
            self.sync()
        self.assertEqual(self.http.upload_writes, 0)

    def test_manifest_cannot_claim_missing_or_embedded_assets(self):
        bad = copy.deepcopy(self.manifest)
        bad['assets'] = []
        with self.assertRaisesRegex(Hold, 'incomplete'):
            manifest_assets(self.snap, bad)
        self.snap['note']['content'] += '![](https://other.example.test/image.png)'
        with self.assertRaisesRegex(Hold, 'embedded_coverage'):
            manifest_assets(self.snap, self.manifest)

    def test_multipart_sends_ordered_parts_and_completes(self):
        self.data = b'x' * (20 * 1024 * 1024 + 1)
        self.asset.update(sha256=hashlib.sha256(self.data).hexdigest(), bytes=len(self.data))
        self.asset['local_name'] = self.asset['sha256'] + '.bin'
        p = self.root / self.asset['local_name']
        p.write_bytes(self.data)
        p.chmod(0o600)
        result = self.sync()
        self.assertTrue(result['binary_assets_verified'])
        self.assertEqual(self.http.send_calls, 3)
        self.assertEqual([len(x) for x in next(iter(self.http.upload_data.values()))], [PART_BYTES, PART_BYTES, 1])

    def test_multipart_response_loss_holds_pending_parts(self):
        self.data = b'x' * (20 * 1024 * 1024 + 1)
        self.asset.update(sha256=hashlib.sha256(self.data).hexdigest(), bytes=len(self.data))
        self.asset['local_name'] = self.asset['sha256'] + '.bin'
        p = self.root / self.asset['local_name']
        p.write_bytes(self.data)
        p.chmod(0o600)
        self.http.lose = 'send'
        with self.assertRaisesRegex(Hold, 'transport_uncertain'):
            self.sync()
        with self.assertRaisesRegex(Hold, 'send_requires_reconcile'):
            self.sync()
        self.assertEqual(self.http.send_calls, 1)


if __name__ == '__main__':
    unittest.main()
