import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from get_cloud_incremental_proof import sync_page
from source_sync_assets import AssetStore, AssetError


class Incremental(unittest.TestCase):
    def test_replay_update_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            folder = root / 'Raw/03 Get/01 Notes/Cloud Proof'
            listing = lambda cursor: {'notes': [{'id': '1'}], 'has_more': False}
            detail = lambda identity: {'id': identity, 'content': 'first'}
            sync_page(root, listing, detail)
            first = {p.name: p.read_bytes() for p in folder.iterdir()}
            sync_page(root, listing, detail)
            self.assertEqual(first, {p.name: p.read_bytes() for p in folder.iterdir()})
            sync_page(root, listing, lambda identity: {'id': identity, 'content': 'changed'})
            self.assertIn('changed', (folder / '1.md').read_text())
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            def failure(identity):
                raise RuntimeError('network failure')
            with self.assertRaises(RuntimeError):
                sync_page(root, listing, failure)
            self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_cursor_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cursors = []
            def listing(cursor):
                cursors.append(cursor)
                return {'notes': [{'id': '1' if cursor is None else '2'}],
                        'has_more': cursor is None, 'cursor': 'next'}
            for _ in range(2):
                sync_page(root, listing, lambda identity: {'id': identity, 'content': identity})
            self.assertEqual(cursors, [None, 'next'])
            self.assertEqual(len(list(root.rglob('*.md'))), 2)

    def test_asset_link_and_failed_download_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            raw = root / 'Raw/03 Get'
            listing = lambda cursor: {'notes': [{'id': '1'}], 'has_more': False}
            detail = lambda identity: {'id': identity, 'content': 'body', 'attachments': [
                {'type': 'image', 'url': 'https://example.com/p.png'}]}
            store = AssetStore(raw, fetch=lambda url, max_bytes: (b'fixture-png', 'image/png'))
            with patch('sync_getnote_to_raw.RAW_DIR', raw):
                sync_page(root, listing, detail, store)
            folder = raw / '01 Notes/Cloud Proof'
            self.assertIn('../../_assets/images/', (folder / '1.md').read_text())
            before = (folder / 'state.json').read_bytes()
            def failure(url, max_bytes):
                raise AssetError('download failed')
            with self.assertRaises(AssetError):
                sync_page(root, listing, detail, AssetStore(raw, fetch=failure))
            self.assertEqual(before, (folder / 'state.json').read_bytes())
