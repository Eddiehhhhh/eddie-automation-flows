import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_sync_urls as urls
from source_sync_store import attachment_manifest
from source_sync_assets import AssetStore
import sync_getnote_to_raw as sync
from verify_shadow_repeat import snapshot

NOW = 2000000000


def signed(expiry=NOW + 172800, signature='OLD', path='object.m4a'):
    return f'https://example.invalid/{path}?OSSAccessKeyId=SYNTHETIC&Expires={expiry}&Signature={signature}'


def note(url):
    return dict(note_id='1001', note_type='plain_text', title='source title', content='source body',
                created_at='2020-01-01T00:00:00Z', updated_at='2020-01-01T00:00:00Z',
                attachments=[{'url': url, 'type': 'audio', 'id': 'asset1', 'name': 'source name'}])


def link_assets(value):
    return [{'url': value['attachments'][0]['url'], 'kind': 'audio', 'status': 'not-downloaded'}]


class SignedURLTests(unittest.TestCase):
    def test_only_strict_observed_scheme_and_useful_validity_are_reused(self):
        old, new = signed(), signed(NOW + 172810, 'NEW')
        self.assertTrue(urls.reusable_url(old, new, NOW))
        for bad in (new + '&extra=1', new + '&Signature=other', new.replace('Expires=', 'expires='),
                    new.replace('Expires=2000172810', 'Expires=PRIVATE'),
                    new.replace('SYNTHETIC', 'DIFFERENT'), new.replace('object.m4a', 'other.m4a'),
                    new.replace('example.invalid', 'different.invalid'), new + '#changed',
                    new.replace('https:', 'http:'), new.replace('https://', 'https://user:pass@'),
                    new.replace('example.invalid', 'example.invalid:8443'), new.replace('Signature=NEW', 'Signature='),
                    new.replace('2000172810', '2000172799')):
            with self.subTest(bad=bad): self.assertFalse(urls.reusable_url(old, bad, NOW))
        for expiry in (NOW - 1, NOW, NOW + urls.REFRESH_BEFORE_SECONDS):
            self.assertFalse(urls.reusable_url(signed(expiry), new, NOW))
        self.assertIsNone(urls.oss_v1_url('https://[broken'))

    def test_retains_whole_snapshot_and_asset_descriptor_without_mutating_input(self):
        old, new = note(signed()), note(signed(NOW + 172810, 'NEW'))
        assets = link_assets(new)
        manifest = dict(attachment_manifest(old), assets=link_assets(old))
        result, retained_assets, count = urls.stabilize_attachment_urls(new, assets, old, manifest, NOW)
        self.assertEqual(result, old); self.assertEqual(retained_assets, link_assets(old)); self.assertEqual(count, 1)
        self.assertEqual(new['attachments'][0]['url'], signed(NOW + 172810, 'NEW'))
        self.assertEqual(assets, link_assets(new))

    def test_source_descriptor_query_and_binary_edits_are_not_suppressed(self):
        old, new = note(signed()), note(signed(NOW + 172810, 'NEW'))
        manifest = dict(attachment_manifest(old), assets=link_assets(old))
        variants = []
        for key, value in [('content', 'edited'), ('title', 'edited'), ('updated_at', 'new'),
                           ('note_id', '1002'), ('extra', 1), ('content', signed(NOW + 172810, 'NEW')),
                           ('audio', {'original': signed(NOW + 172810, 'NEW')})]:
            changed = copy.deepcopy(new); changed[key] = value; variants.append(changed)
        for key, value in [('id', 'asset2'), ('type', 'video'), ('size', 2), ('name', 'renamed'),
                           ('updated_at', 'new'), ('unknown', 'changed')]:
            changed = copy.deepcopy(new); changed['attachments'][0][key] = value; variants.append(changed)
        changed = copy.deepcopy(new); changed['attachments'].append({}); variants.append(changed)
        changed = copy.deepcopy(new); changed['attachments'] = []; variants.append(changed)
        changed = copy.deepcopy(new); changed['attachments'][0]['url'] += '&versionId=2'; variants.append(changed)
        for changed in variants:
            self.assertEqual(urls.stabilize_attachment_urls(changed, link_assets(new), old, manifest, NOW)[2], 0)
        old['typed'] = 1; new['typed'] = True
        self.assertEqual(urls.stabilize_attachment_urls(new, link_assets(new), old, manifest, NOW)[2], 0)
        del old['typed']; del new['typed']
        new_assets = link_assets(new); new_assets[0]['sha256'] = 'DIFFERENT_BYTES'
        self.assertEqual(urls.stabilize_attachment_urls(new, new_assets, old, manifest, NOW)[2], 0)
        self.assertEqual(urls.stabilize_attachment_urls(new, link_assets(new), old, {}, NOW)[2], 0)

    def test_reordering_and_multiple_url_fields_are_exact(self):
        old = note(signed()); old['attachments'].append({'id': 'asset2', 'type': 'audio', 'url': signed(path='other.m4a')})
        new = copy.deepcopy(old)
        for item in new['attachments']: item['url'] = item['url'].replace('OLD', 'NEW')
        assets_old = [{'url': a['url']} for a in old['attachments']]
        assets_new = [{'url': a['url']} for a in new['attachments']]
        manifest = dict(attachment_manifest(old), assets=assets_old)
        self.assertEqual(urls.stabilize_attachment_urls(new, assets_new, old, manifest, NOW)[2], 2)
        new['attachments'].reverse(); assets_new.reverse()
        self.assertEqual(urls.stabilize_attachment_urls(new, assets_new, old, manifest, NOW)[2], 0)
        for field in ('original_url', 'download_url'):
            old = note(signed()); old['attachments'][0][field] = old['attachments'][0].pop('url')
            new = copy.deepcopy(old); new['attachments'][0][field] = signed(NOW + 172810, 'NEW')
            self.assertEqual(urls.stabilize_attachment_urls(new, [], old, dict(attachment_manifest(old), assets=[]), NOW)[2], 1)

    def test_cache_read_bounds_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = root/'cache.json'
            self.assertIsNone(urls.read_previous(p, root))
            p.write_text('{}'); self.assertEqual(urls.read_previous(p, root), {})
            p.write_bytes(b'x'*(urls.MAX_CACHE_BYTES+1)); self.assertIsNone(urls.read_previous(p, root))
            p.write_text('PRIVATE invalid json'); self.assertIsNone(urls.read_previous(p, root))
            p.unlink(); p.symlink_to(root/'target')
            with self.assertRaises(ValueError): urls.read_previous(p, root)

    def run_sync(self, root, value, fetch=None):
        raw = root/'Raw'/'03 Get'
        with patch.multiple(sync, ROOT=root, RAW_DIR=raw, NOTES_DIR=raw/'01 Notes', STATE_DIR=raw/'03 State'), \
             patch.dict(os.environ, {'VAULT_ROOT': str(root)}), patch.object(sys, 'argv', ['sync']), \
             patch.object(sync, 'load_credentials', return_value=('synthetic', 'synthetic')), \
             patch.object(sync, 'fetch_note_list', return_value=[value]), \
             patch.object(sync, 'fetch_note_detail', return_value=copy.deepcopy(value)), \
             patch.object(urls.time, 'time', return_value=NOW), \
             patch.object(sync, 'AssetStore', side_effect=lambda path, **kw: AssetStore(path, fetch=fetch, **kw)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(sync.main(), 0)
            return json.loads(out.getvalue())

    def test_end_to_end_repeat_expiry_refresh_and_real_edit_preserve_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = note(signed()); fresh = note(signed(NOW+172810, 'NEW'))
            self.assertEqual(self.run_sync(root, old)['written_or_changed'], 1)
            before = snapshot(root); paths = list(root.rglob('*.md'))
            result = self.run_sync(root, fresh)
            self.assertEqual((result['written_or_changed'], result['attachment_urls_reused']), (0, 1))
            self.assertEqual(before, snapshot(root))
            # Near-expiry cache must adopt the current URL in all three outputs.
            cache = next(root.rglob('raw-cache/*.json'))
            manifest = next(root.rglob('attachments/*.json'))
            expired_url = signed(NOW+3600)
            for path in (cache, manifest, paths[0]):
                path.write_text(path.read_text().replace(signed(), expired_url))
            result = self.run_sync(root, fresh)
            self.assertEqual((result['written_or_changed'], result['attachment_urls_reused']), (1, 0))
            for path in (cache, manifest, paths[0]):
                self.assertIn(fresh['attachments'][0]['url'], path.read_text())
                self.assertNotIn(expired_url, path.read_text())
            changed = copy.deepcopy(fresh); changed['title'] = 'renamed'; changed['content'] = 'real old-note edit'
            changed['attachments'][0]['url'] = signed(NOW+172820, 'LATEST')
            result = self.run_sync(root, changed)
            self.assertEqual((result['written_or_changed'], result['attachment_urls_reused']), (1, 0))
            self.assertEqual(paths, list(root.rglob('*.md')))
            self.assertIn('real old-note edit', paths[0].read_text())
            self.assertIn('Signature=LATEST', paths[0].read_text())

    def test_fresh_downloads_detect_in_place_binary_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); calls = []
            old = note(signed(path='image.png')); old['attachments'][0]['type'] = 'image'
            fresh = copy.deepcopy(old); fresh['attachments'][0]['url'] = signed(NOW+172810, 'NEW', 'image.png')
            def fetch(url, **kw): calls.append(url); return b'synthetic-image', 'image/png'
            self.run_sync(root, old, fetch); before = snapshot(root)
            result = self.run_sync(root, fresh, fetch)
            self.assertEqual(calls[-1], fresh['attachments'][0]['url'])
            self.assertEqual(result['attachment_urls_reused'], 1)
            self.assertEqual(snapshot(root), before)
            result = self.run_sync(root, fresh, lambda url, **kw: (b'changed-image', 'image/png'))
            self.assertEqual(result['attachment_urls_reused'], 0)
            self.assertEqual(result['written_or_changed'], 1)
            manifest = json.loads(next(root.rglob('attachments/*.json')).read_text())
            self.assertEqual(manifest['assets'][0]['sha256'], hashlib.sha256(b'changed-image').hexdigest())
            self.assertEqual(manifest['assets'][0]['url'], fresh['attachments'][0]['url'])


if __name__ == '__main__': unittest.main()
