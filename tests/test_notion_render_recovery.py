"""Synthetic faithful render repairs; no private fixtures, credentials or I/O."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_sink_plan as p
import get_notion_executor as e
import get_notion_live_transport as t

NOTE = '1234567890123456789'
PATH = 'Raw/03 Get/render-recovery.md'


def render(body):
    data = ('---\nnote_id: "' + NOTE + '"\n---\n' + body).encode()
    return p.render_markdown(data, NOTE, {}, PATH, {PATH: p.git_blob(data)})


def paragraph(text, **flags):
    return p.block('paragraph', {'rich_text': [p.text_run(text, **flags)]})


class InlineTests(unittest.TestCase):
    def test_single_tilde_stays_exact_literal_with_annotations_and_links(self):
        runs = p.rich_text('a~b **c~d** *e~f* [g~h](https://example.invalid/a~b)')
        self.assertEqual(''.join(r['text']['content'] for r in runs), 'a~b c~d e~f g~h')
        self.assertTrue(next(r for r in runs if r['text']['content'] == 'c~d')['annotations']['bold'])
        self.assertTrue(next(r for r in runs if r['text']['content'] == 'e~f')['annotations']['italic'])
        self.assertEqual(runs[-1]['text']['link']['url'], 'https://example.invalid/a~b')

    def test_escaped_tilde_removes_only_its_escape(self):
        self.assertEqual(''.join(r['text']['content'] for r in p.rich_text(r'a\~b')), 'a~b')
        self.assertEqual(''.join(r['text']['content'] for r in p.rich_text(r'\~\~literal\~\~')), '~~literal~~')

    def test_unescaped_strikethrough_and_unknown_markup_still_block(self):
        for source in ('~~text~~', 'a~~b', '~~~text~~~', '**~~text~~**', '<span>text</span>', '[reference]', '_emphasis_'):
            with self.subTest(source=source), self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                p.rich_text(source)

    def test_tilde_fence_and_unknown_asset_still_block(self):
        with self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'): render('~~~\ncode\n~~~')
        with self.assertRaisesRegex(p.Blocked, '^missing_asset$'): render('literal~value\n\n![image](https://example.invalid/image.png)')


class OrderedListTests(unittest.TestCase):
    def test_exact_continuation_across_blank_lines_stays_same_list(self):
        blocks, _, _ = render('1. first\n\n2. second\n\n\n3. third')
        self.assertEqual([row['type'] for row in blocks], ['numbered_list_item'] * 3)
        self.assertEqual([row['payload'].get('list_start_index') for row in blocks], [1, None, None])
        self.assertEqual([row['payload']['rich_text'][0]['text']['content'] for row in blocks], ['first', 'second', 'third'])

    def test_explicit_reset_across_blank_is_preserved(self):
        blocks, _, _ = render('1. first\n2. second\n\n1. reset\n2. next')
        self.assertEqual([row['payload'].get('list_start_index') for row in blocks], [1, None, 1, None])

    def test_existing_tight_list_behavior_is_unchanged(self):
        blocks, _, _ = render('1. first\n1. next\n9. next')
        self.assertEqual([row['payload'].get('list_start_index') for row in blocks], [1, None, None])

    def test_real_start_skipped_number_and_intervening_blocks_still_block(self):
        for source in ('2. start', '1. first\n\n3. skipped', '1. first\n\n## heading\n\n2. second',
                       '1. first\n\nparagraph\n\n2. second', '1. [x] task\n\n2. second',
                       '1. first\n\n- bullet\n\n2. second'):
            with self.subTest(source=source), self.assertRaisesRegex(p.Blocked, '^unsupported_source_format$'):
                render(source)


class CompactionTests(unittest.TestCase):
    def test_eight_disjoint_pairs_fit_108_roots_and_preserve_all_runs(self):
        rows = [p.block('heading_2', {'rich_text': [p.text_run('heading')]})]
        for index in range(106): rows.append(paragraph('paragraph-' + str(index), bold=index % 2 == 0))
        rows.append(p.block('heading_2', {'rich_text': [p.text_run('last heading')]}))
        before = deepcopy(rows); compacted, count = p.compact_paragraph_pairs(rows)
        self.assertEqual(count, 8); self.assertEqual(len(compacted), 100); self.assertEqual(rows, before)
        self.assertEqual(compacted[0], before[0]); self.assertEqual(compacted[-1], before[-1])
        for pair in range(8):
            start = 1 + 2 * pair
            self.assertEqual(compacted[pair + 1]['payload']['rich_text'],
                before[start]['payload']['rich_text'] + [p.text_run('\n\n')] + before[start + 1]['payload']['rich_text'])
        self.assertEqual(compacted[9:-1], before[17:-1])
        self.assertEqual(len(t.wire_blocks(e.request_blocks(compacted))), 100)
        compacted[1]['payload']['rich_text'][0]['text']['content'] = 'mutated result'
        self.assertEqual(rows, before)

    def test_renderer_records_explicit_warning_and_is_deterministic(self):
        body = '\n\n'.join('paragraph-' + str(index) for index in range(108))
        blocks, used, warnings = render(body)
        self.assertEqual(len(blocks), 100); self.assertEqual(used, {})
        self.assertEqual(warnings, ['paragraph_boundaries_compacted'])
        self.assertEqual(render(body), (blocks, used, warnings))

    def test_no_compaction_for_already_bounded_or_unfit_cohorts(self):
        for count in (0, 1, 100, 201):
            rows = [paragraph(str(index)) for index in range(count)]
            compacted, merged = p.compact_paragraph_pairs(rows)
            self.assertEqual(compacted, rows); self.assertEqual(merged, 0)
        with self.assertRaises(e.Blocked): e.request_blocks([paragraph('x') for _ in range(101)])

    def test_structures_children_colors_and_extra_payload_never_cross(self):
        alternatives = [
            p.block('heading_2', {'rich_text': [p.text_run('heading')]}),
            p.block('bulleted_list_item', {'rich_text': [p.text_run('list')]}),
            p.block('quote', {'rich_text': [p.text_run('quote')]}),
            p.block('callout', {'rich_text': [p.text_run('callout')], 'icon': {'type': 'emoji', 'emoji': 'x'}}),
            p.block('paragraph', {'rich_text': [p.text_run('parent')]}, [paragraph('child')]),
            p.block('paragraph', {'rich_text': [p.text_run('colored')], 'color': 'red'}),
        ]
        special = paragraph('extended'); special['payload']['icon'] = None; alternatives.append(special)
        for barrier in alternatives:
            rows = [paragraph('plain'), barrier] * 51
            before = deepcopy(rows); compacted, count = p.compact_paragraph_pairs(rows)
            self.assertEqual(count, 0); self.assertEqual(compacted, before); self.assertEqual(rows, before)

    def test_partial_pairing_that_cannot_fit_is_not_applied(self):
        rows = [paragraph('one'), paragraph('two')] + [p.block('divider', {}) for _ in range(106)]
        compacted, count = p.compact_paragraph_pairs(rows)
        self.assertEqual(compacted, rows); self.assertEqual(count, 0)

    def test_rich_text_links_annotations_and_two_blank_line_characters_are_exact(self):
        left = paragraph('bold', bold=True); right = paragraph('linked', italic=True)
        right['payload']['rich_text'][0]['text']['link'] = {'url': 'https://example.invalid/path'}
        rows = [left, right] + [p.block('divider', {}) for _ in range(99)]
        compacted, count = p.compact_paragraph_pairs(rows)
        self.assertEqual(count, 1)
        self.assertEqual(compacted[0]['payload']['rich_text'], left['payload']['rich_text'] + [p.text_run('\n\n')] + right['payload']['rich_text'])
        self.assertFalse(compacted[0]['payload']['rich_text'][1]['annotations']['bold'])
        self.assertIsNone(compacted[0]['payload']['rich_text'][1]['text']['link'])

    def test_pair_that_exceeds_100_text_chunks_is_skipped(self):
        left = paragraph('x' * (2000 * 50)); right = paragraph('y' * (2000 * 50))
        rows = [left, right] + [p.block('divider', {}) for _ in range(99)]
        compacted, count = p.compact_paragraph_pairs(rows)
        self.assertEqual(compacted, rows); self.assertEqual(count, 0)


if __name__ == '__main__': unittest.main()
