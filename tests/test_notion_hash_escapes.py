"""Synthetic CommonMark hash-escape regression; no private capsule or network.

Expected results follow CommonMark 0.31.2 section 2.4:
https://spec.commonmark.org/0.31.2/#backslash-escapes
The independent parser comparison is release evidence, not a CI dependency.
"""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import get_private_runtime_bindings as bindings


def synthetic_module(name, keys):
    """Load real code in isolation; substitute only unrelated scope hashes."""
    values = {(name, key): '0' * 64 for key in keys}
    spec = importlib.util.spec_from_file_location('_hash_escape_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.object(bindings, 'value', side_effect=lambda owner, key: values[(owner, key)]):
        spec.loader.exec_module(module)
    return module


class HashEscapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = synthetic_module('get_notion_sink_plan', ('DATA_SOURCE_SHA256', 'COHORT_SHA256'))
        cls.projection = synthetic_module('get_notion_credential_projection',
            ('NOTE_SHA256', 'SOURCE_SHA256', 'BODY_SHA256', 'PROJECTED_BODY_SHA256'))

    @staticmethod
    def text(runs):
        return ''.join(run['text']['content'] for run in runs)

    def render(self, body):
        ident = '1234567890123456789'
        path = 'Raw/03 Get/03 纯文本记录/synthetic.md'
        data = ('---\nnote_id: "' + ident + '"\n---\n' + body).encode()
        with patch.dict(sys.modules, {'get_notion_credential_projection': self.projection}):
            blocks, assets, warnings = self.renderer.render_markdown(data, ident, {}, path, {})
        self.assertEqual(assets, {})
        self.assertEqual(warnings, [])
        return blocks

    def test_plain_hash_escape_and_literal_hash(self):
        for source, expected in [(r'\#sample', '#sample'), ('#sample', '#sample'),
                                 (r'before \#sample after', 'before #sample after')]:
            with self.subTest(source=source):
                self.assertEqual(self.text(self.renderer.rich_text(source)), expected)

    def test_backslash_parity_in_plain_emphasis_and_link_labels(self):
        for count in range(1, 7):
            source = '\\' * count + '#sample'
            expected = '\\' * (count // 2) + '#sample'
            for before, after in [('', ''), ('**', '**'), ('*', '*'),
                                  ('[', '](https://example.invalid/path#fragment)')]:
                with self.subTest(count=count, context=before):
                    runs = self.renderer.rich_text(before + source + after)
                    self.assertEqual(self.text(runs), expected)

    def test_emphasis_annotations_are_preserved(self):
        for source, flag in [(r'**\#sample**', 'bold'), (r'*\#sample*', 'italic')]:
            with self.subTest(flag=flag):
                runs = self.renderer.rich_text(source)
                self.assertEqual(self.text(runs), '#sample')
                self.assertTrue(all(run['annotations'][flag] for run in runs))
                self.assertTrue(all(not run['annotations']['code'] for run in runs))

    def test_inline_code_preserves_backslashes(self):
        for count in (1, 2, 3):
            content = '\\' * count + '#sample'
            with self.subTest(count=count):
                runs = self.renderer.rich_text('`' + content + '`')
                self.assertEqual(self.text(runs), content)
                self.assertTrue(all(run['annotations']['code'] for run in runs))

    def test_fenced_code_preserves_backslashes(self):
        content = r'\#sample' + '\n' + r'\\#sample'
        blocks = self.render('```text\n' + content + '\n```\n')
        self.assertEqual([block['type'] for block in blocks], ['code'])
        self.assertEqual(self.text(blocks[0]['payload']['rich_text']), content)

    def test_links_decode_label_but_do_not_rewrite_destination(self):
        url = r'https://example.invalid/a\#fragment'
        runs = self.renderer.rich_text(r'[\#sample](' + url + ')')
        self.assertEqual(self.text(runs), '#sample')
        self.assertEqual(runs[0]['text']['link'], {'url': url})

    def test_block_types_do_not_change_after_unescaping(self):
        for source, kind in [(r'\# sample', 'paragraph'), (r'## \# sample', 'heading_2'),
                             (r'- \# sample', 'bulleted_list_item'),
                             (r'> \# sample', 'quote'), (r'1. \# sample', 'numbered_list_item')]:
            with self.subTest(kind=kind):
                blocks = self.render(source + '\n')
                self.assertEqual([block['type'] for block in blocks], [kind])
                self.assertEqual(self.text(blocks[0]['payload']['rich_text']), '# sample')

    def test_unrelated_literal_backslashes_and_supported_escapes(self):
        for source, expected in [(r'\A', r'\A'), (r'\3', r'\3'),
                                 (r'\*sample\*', '*sample*'), ('word_name', 'word_name')]:
            with self.subTest(source=source):
                self.assertEqual(self.text(self.renderer.rich_text(source)), expected)

    def test_unsupported_grammar_remains_blocked(self):
        for source in ['_unsupported_', '![sample](https://example.invalid/a.png)',
                       '<script>sample</script>', r'[a\_b\#sample](https://example.invalid)']:
            with self.subTest(source=source), self.assertRaises(self.renderer.Blocked):
                self.renderer.rich_text(source)


if __name__ == '__main__':
    unittest.main()
