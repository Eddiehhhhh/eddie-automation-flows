import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_inline_image_assets as images


class InlineImageTests(unittest.TestCase):
    def test_extract_rewrite_and_manifest_excludes_source_url(self):
        body = 'Text\n![diagram](https://cdn.example/image.png?sig=private)\nTail'
        refs = images.extract(body)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['url'], 'https://cdn.example/image.png?sig=private')
        rewritten = images.rewrite(body, refs, ['../_assets/images/' + 'a' * 64 + '.png'])
        self.assertEqual(rewritten, 'Text\n![diagram](../_assets/images/' + 'a' * 64 + '.png)\nTail')
        manifest = images.public_manifest(refs, [{'path': '_assets/images/' + 'a' * 64 + '.png',
            'sha256': 'a' * 64, 'bytes': 12, 'mime': 'image/png'}])
        self.assertNotIn('cdn.example', repr(manifest))
        self.assertNotIn('private', repr(manifest))

    def test_rejects_non_https_and_unparsed_image_syntax(self):
        for body in ('![x](http://cdn.example/a.png)', '![x](https://cdn.example/a(b).png)',
                     '<img src="https://cdn.example/a.png">', '![broken](not-a-url)'):
            with self.subTest(body=body), self.assertRaises(images.ImageReferenceError):
                images.extract(body)

    def test_rewrite_detects_changed_source_offsets(self):
        body = '![x](https://cdn.example/a.png)'
        refs = images.extract(body)
        with self.assertRaises(images.ImageReferenceError):
            images.rewrite(body.replace('cdn.example', 'evil.example'), refs, ['asset.png'])

    def test_manifest_rejects_unsafe_path_or_non_image(self):
        refs = images.extract('![x](https://cdn.example/a.png)')
        with self.assertRaises(images.ImageReferenceError):
            images.public_manifest(refs, [{'path': '../secret.png', 'sha256': 'a' * 64,
                'bytes': 12, 'mime': 'image/png'}])

    def test_materialize_uses_existing_store_and_returns_url_free_manifest(self):
        class Store:
            def materialize(self, note):
                self.note = note
                return [{'status': 'downloaded', 'path': '_assets/images/' + 'a' * 64 + '.png',
                         'sha256': 'a' * 64, 'bytes': 12, 'mime': 'image/png'}]
        store = Store()
        body, assets, manifest = images.materialize_body(
            '![x](https://cdn.example/a.png?sig=secret)',
            '05 图片笔记/example.md', store)
        self.assertEqual(store.note['attachments'], [{'type': 'image', 'url': 'https://cdn.example/a.png?sig=secret'}])
        self.assertEqual(body, '![x](../_assets/images/' + 'a' * 64 + '.png)')
        self.assertEqual(len(assets), 1)
        self.assertNotIn('secret', repr(manifest))


if __name__ == '__main__':
    unittest.main()
