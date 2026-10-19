"""Synthetic fidelity coverage; no recordings, identities or source excerpts."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_sink_plan as p


def render(body):
    raw = ('---\nnote_id: "1234567890123456789"\n---\n' + body).encode()
    return p.render_markdown(raw, '1234567890123456789', {},
                             'Raw/03 Get/synthetic.md', {})[0]


def visible(block):
    return ''.join(run['text']['content'] for run in block['payload']['rich_text'])


class RenderFidelityTests(unittest.TestCase):
    def test_bounded_category_badges_remain_visible_and_unlinked(self):
        for value in ('[🧪 测试]', 'Label [🌱 示例].', '[📄 工作]', '[00:01:02]'):
            with self.subTest(value=value):
                runs = p.rich_text(value)
                self.assertEqual(''.join(r['text']['content'] for r in runs), value)
                self.assertTrue(all(r['text']['link'] is None for r in runs))

    def test_real_badge_links_keep_link_semantics(self):
        runs = p.rich_text('[🧪 测试](https://example.invalid/category)')
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['text']['content'], '🧪 测试')
        self.assertEqual(runs[0]['text']['link'], {'url': 'https://example.invalid/category'})
        self.assertEqual(p.rich_text('**[🧪 测试]**'), [p.text_run('[🧪 测试]', bold=True)])

    def test_unrecognized_and_malformed_brackets_stay_blocked(self):
        for value in ('[unknown]', '[🧪 example]', '[🧪测试]', '[🧪 ]',
                      '[🧪 ' + '例' * 17 + ']', '[[🧪 测试]]', '[🧪 测试',
                      '[🧪 测试]]', '[🧪 **测试**]'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                    p.rich_text(value)

    def test_badge_reference_forms_never_become_literal_text(self):
        for gap in ('', ' ', '\t', '\n', '\r\n'):
            for suffix in ('[ref]', '[]', ': /definition', '(broken target)'):
                with self.subTest(gap=gap, suffix=suffix):
                    with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                        p.rich_text('[🧪 测试]' + gap + suffix)
        for body in ('[🧪 测试]\n\n[🧪 测试]: /definition\n',
                     '*   Parent\n    [🧪 测试]: /definition\n'):
            with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                render(body)

    def test_badges_never_hide_image_syntax(self):
        for value in ('See ![🧪 测试]', 'See ![🧪 测试][]',
                      'See ![🧪 测试](https://example.invalid/image.png)'):
            with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                p.rich_text(value)
        self.assertEqual(p.rich_text('`![🧪 测试]`'),
                         [p.text_run('![🧪 测试]', code=True)])

    def test_badges_in_aligned_list_paragraphs_preserve_hierarchy(self):
        rows = render('*   **Parent**\n    [00:01:02](https://example.invalid/1) [🧪 测试]\n'
                      '    Body.\n\n    [🌱 示例]\n    Child.\n\n    Final child.\n')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['type'], 'bulleted_list_item')
        self.assertEqual(visible(rows[0]), 'Parent\n00:01:02 [🧪 测试]\nBody.')
        self.assertEqual([visible(b) for b in rows[0]['children']],
                         ['[🌱 示例]\nChild.', 'Final child.'])
        self.assertTrue(rows[0]['payload']['rich_text'][0]['annotations']['bold'])

    def test_one_to_three_leading_prose_spaces_are_retained_exactly(self):
        for count in (1, 2, 3):
            prefix = ' ' * count
            body = prefix + 'First.\n' + prefix + 'Second.\n\nLast.\n'
            rows = render(body)
            self.assertEqual([b['type'] for b in rows], ['paragraph', 'paragraph'])
            self.assertEqual([visible(b) for b in rows],
                             [prefix + 'First.\n' + prefix + 'Second.', 'Last.'])

    def test_indented_prose_continues_open_paragraph_without_splitting(self):
        rows = render('First.\n Second.\n  Third.\n   Fourth.\n\nSeparate.\n')
        self.assertEqual([visible(b) for b in rows],
                         ['First.\n Second.\n  Third.\n   Fourth.', 'Separate.'])

    def test_indented_structures_and_code_are_not_plain_prose(self):
        for value in ('# Heading', '> Quote', '- Nested', '* Nested', '1. Nested',
                      '1) Nested', '| Table |', '```', '~~~', '![](image.png)',
                      '<div>HTML</div>', '---', '___', '==='):
            for count in (1, 2, 3):
                with self.subTest(value=value, count=count):
                    with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                        render(' ' * count + value + '\n')
        for value in ('    Code.\n', '\tCode.\n', 'First.\n    Code.\n'):
            with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                render(value)

    def test_unhandled_container_continuations_never_become_roots(self):
        for prefix in ('- Parent', '1. Parent', '- [x] Parent', '> Parent'):
            for gap in ('\n', '\n\n'):
                for count in (1, 2, 3):
                    with self.subTest(prefix=prefix, gap=gap, count=count):
                        with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                            render(prefix + gap + ' ' * count + 'Continuation.\n')

    def test_numeric_dot_text_cannot_interrupt_open_paragraph(self):
        for number in ('0', '2', '27', '002', '1234567890', '0000000001'):
            body = 'Lead.\n' + number + '. Continuation.\n\nTail.\n'
            rows = render(body)
            self.assertEqual([b['type'] for b in rows], ['paragraph', 'paragraph'])
            self.assertEqual(visible(rows[0]), 'Lead.\n' + number + '. Continuation.')

    def test_start_at_one_still_interrupts_paragraph_as_a_list(self):
        for number in ('1', '01', '001'):
            rows = render('Lead.\n' + number + '. First.\n2. Second.\n')
            self.assertEqual([b['type'] for b in rows],
                             ['paragraph', 'numbered_list_item', 'numbered_list_item'])
            self.assertEqual(rows[1]['payload']['list_start_index'], 1)
            self.assertEqual([visible(b) for b in rows], ['Lead.', 'First.', 'Second.'])

    def test_standalone_nonone_list_starts_remain_blocked(self):
        for body in ('2. First.\n', 'Lead.\n\n2. First.\n', '## Heading\n2. First.\n'):
            with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                render(body)
        rows = render('1. First.\n\n2. Second.\n')
        self.assertEqual([b['type'] for b in rows], ['numbered_list_item'] * 2)

    def test_large_paragraph_cohort_has_no_new_compaction(self):
        parts = ['Paragraph ' + str(index) + '.' for index in range(205)]
        rows = render('\n\n'.join(parts) + '\n')
        self.assertEqual([visible(b) for b in rows], parts)

    def test_input_bytes_never_mutate(self):
        raw = ('---\nnote_id: "1234567890123456789"\n---\n'
               'Lead.\n  Continuation.\n2. Numeric text.\n\n[🧪 测试]\n').encode()
        before = bytes(raw)
        p.render_markdown(raw, '1234567890123456789', {}, 'Raw/03 Get/synthetic.md', {})
        self.assertEqual(raw, before)


if __name__ == '__main__':
    unittest.main()
