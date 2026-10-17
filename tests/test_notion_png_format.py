"""Narrow literal time/category and aligned bullet-paragraph fidelity tests."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_sink_plan as p


def markdown(body):
    return ('---\nnote_id: "1234567890123456789"\n---\n' + body).encode()


def render(body):
    raw = markdown(body); path = 'Raw/03 Get/notes/synthetic.md'
    return p.render_markdown(raw, '1234567890123456789', {}, path, {path: p.git_blob(raw)})[0]


def link(label, url):
    value = p.text_run(label); value['text']['link'] = {'url': url}; return value


class PngFormatTests(unittest.TestCase):
    def test_adjacent_bracket_labels_remain_reference_ambiguous(self):
        with self.assertRaisesRegex(p.Blocked, 'unsupported_source_format'):
            p.rich_text('Speaker [00:15:50] [📄 工作].')
        # Separate directly adjacent bracket labels remain reference-ambiguous;
        # the two unambiguous source forms are exercised independently below.

    def test_literal_source_forms_keep_every_character(self):
        for text in ('Speaker [00:15:50]', 'From 00:00 to [04:50:43].', 'Category [📄 工作]'):
            with self.subTest(text=text):
                values = p.rich_text(text)
                self.assertEqual(''.join(v['text']['content'] for v in values), text)
                self.assertTrue(all(v['text']['link'] is None and v['annotations'] == p.ANNOTATIONS for v in values))

    def test_real_inline_time_links_keep_url_while_literal_time_does_not_gain_link(self):
        self.assertEqual(p.rich_text('[00:15:50](https://example.invalid/950)'),
                         [link('00:15:50', 'https://example.invalid/950')])
        self.assertEqual(p.rich_text('[00:15:50]'), [p.text_run('[00:15:50]')])
        self.assertEqual(p.rich_text('**[00:15:50]**'), [p.text_run('[00:15:50]', bold=True)])

    def test_reference_definitions_links_unknown_brackets_and_html_stay_blocked(self):
        for text in ('[00:15:50]: https://example.invalid', '[📄 工作]: /reference',
                     '[00:15:50][reference]', '[📄 工作] [ref]', '[unknown]',
                     '[00:15:50][]', '[00:15:50] (broken)', '[00:99:50]', '[00:15:99]',
                     '<span>value</span>', '<br onload="x">', 'unfinished [00:15:50'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(p.Blocked, 'unsupported_source_format'): p.rich_text(text)
        with self.assertRaisesRegex(p.Blocked, 'unsupported_source_format'):
            render('Speaker [00:15:50]\n\n[00:15:50]: https://example.invalid\n')

    def test_aligned_continuations_preserve_parent_paragraph_children_links_and_bold(self):
        body = ('*   **Title**\n'
                '    [00:01:42](https://example.invalid/102)-[00:16:57](https://example.invalid/1017) [📄 工作]\n'
                '    First body **bold**.\n\n'
                '    [00:00:00](https://example.invalid/0) [📄 工作]\n'
                '    Second body.\n\n'
                '## After\n\nOutside.\n')
        rows = render(body)
        self.assertEqual([r['type'] for r in rows], ['bulleted_list_item', 'heading_2', 'paragraph'])
        self.assertEqual(rows[0]['payload']['rich_text'], [p.text_run('Title', bold=True), p.text_run('\n'),
            link('00:01:42', 'https://example.invalid/102'), p.text_run('-'),
            link('00:16:57', 'https://example.invalid/1017'), p.text_run(' [📄 工作]\nFirst body '), p.text_run('bold', bold=True), p.text_run('.')])
        self.assertEqual(rows[0]['children'], [p.block('paragraph', {'rich_text': [
            link('00:00:00', 'https://example.invalid/0'), p.text_run(' [📄 工作]\nSecond body.')]})])
        self.assertEqual(rows[1]['payload']['rich_text'], [p.text_run('After')])
        self.assertEqual(rows[2]['payload']['rich_text'], [p.text_run('Outside.')])

    def test_multiple_continuation_paragraphs_stay_in_one_item_and_order(self):
        for marker in ('*', '-', '+'):
            rows = render(marker + '   First\n    continuation\n\n    Second\n\n    Third\n\n* Next\n')
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['payload']['rich_text'], [p.text_run('First\ncontinuation')])
            self.assertEqual([r['payload']['rich_text'] for r in rows[0]['children']],
                             [[p.text_run('Second')], [p.text_run('Third')]])
            self.assertEqual(rows[1]['payload']['rich_text'], [p.text_run('Next')])

    def test_no_code_nested_block_reference_or_html_is_flattened_as_continuation(self):
        for content in ('    code', '\tcode', '- nested', '-\tnested', '-', '1. nested', '1)\tnested',
                        '# heading', '> quote', '```', '~~~', '| table |', '---', '--', '===',
                        '<div>html</div>', '![](image.png)', '[00:15:50]: /definition', '[other]'):
            with self.subTest(content=content):
                with self.assertRaisesRegex(p.Blocked, 'unsupported_source_format'):
                    render('*   First\n    ' + content + '\n')
        for body in ('    code outside a list\n', '* First\n    code\n', '*   First\n\tcode\n'):
            with self.assertRaisesRegex(p.Blocked, 'unsupported_source_format'): render(body)

    def test_inline_image_in_continuation_never_becomes_a_caption_link(self):
        for gap in ('\n', '\n\n'):
            with self.subTest(gap=gap):
                with self.assertRaisesRegex(p.Blocked, 'unsupported_source_format'):
                    render('*   First' + gap + '    See ![diagram](https://example.invalid/x.png)\n')

    def test_ordinary_bold_and_italic_inline_images_are_not_flattened(self):
        for text in ('See ![diagram](https://example.invalid/x.png)',
                     '**See ![diagram](https://example.invalid/x.png)**',
                     '*See ![diagram](https://example.invalid/x.png)*',
                     '![diagram](https://example.invalid/x.png) trailing text'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(p.Blocked, 'unsupported_source_format'):
                    render(text + '\n')

    def test_inline_code_image_examples_remain_literal_in_paragraph_and_continuation(self):
        example = '![diagram](https://example.invalid/x.png)'
        self.assertEqual(p.rich_text('`' + example + '`'), [p.text_run(example, code=True)])
        rows = render('Example `' + example + '`\n')
        self.assertEqual(rows[0]['payload']['rich_text'], [p.text_run('Example '), p.text_run(example, code=True)])
        rows = render('*   First\n    Example `' + example + '`\n')
        self.assertEqual(rows[0]['payload']['rich_text'], [p.text_run('First\nExample '), p.text_run(example, code=True)])

    def test_regular_existing_lists_and_inline_code_are_unchanged(self):
        rows = render('* one\n* two\n\n1. ordered\n2. next\n')
        self.assertEqual([r['type'] for r in rows], ['bulleted_list_item'] * 2 + ['numbered_list_item'] * 2)
        rows = render('*   Label\n    inline `code` retained\n')
        self.assertEqual(rows[0]['payload']['rich_text'], [p.text_run('Label\ninline '),
            p.text_run('code', code=True), p.text_run(' retained')])

    def test_missing_image_binding_is_still_blocked(self):
        with self.assertRaisesRegex(p.Blocked, 'missing_asset'):
            render('![diagram](image.png)\n\nSpeaker [00:15:50]\n')


if __name__ == '__main__': unittest.main()
