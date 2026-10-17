"""Synthetic comparison-only normalization; no live/private payload fixtures."""
from copy import deepcopy
from unittest.mock import patch
import unittest

import get_notion_executor as e
import get_notion_sink_plan as p
import get_notion_live_transport as t
import test_notion_executor as fixtures

N, DS = fixtures.N, fixtures.DS
PATH = 'Raw/03 Get/notes/normalization.md'


def body():
    return '\n\n'.join('paragraph-' + str(i) for i in range(107)) + '\n\n' + 'x' * 4200


def rendered():
    data = fixtures.md(N, body())
    return data, p.render_markdown(data, N, {}, PATH, {PATH: p.git_blob(data)})[0]


def unwire(rows):
    result = []
    for row in rows:
        payload = deepcopy(row[row['type']]); children = payload.pop('children', [])
        result.append({'type': row['type'], 'payload': payload, 'children': unwire(children)})
    return result


class ComparisonTests(unittest.TestCase):
    def test_compacted_and_wire_chunked_runs_compare_without_mutating_input(self):
        _, rows = rendered(); before = p.encoded(rows)
        wire_rows = unwire(t.wire_blocks(e.request_blocks(rows), None))
        self.assertEqual(len(rows), 100)
        self.assertNotEqual(rows, wire_rows)
        self.assertEqual(p.comparison_semantics(rows, None), p.comparison_semantics(wire_rows, None))
        self.assertEqual(p.encoded(rows), before)
        self.assertEqual(len(rows[0]['payload']['rich_text']), 3)
        self.assertEqual(len(wire_rows[-1]['payload']['rich_text']), 3)

    def test_created_compacted_100_roots_and_properties(self):
        _, rows = rendered(); api = fixtures.API(None)
        api.make(N, unwire(t.wire_blocks(e.request_blocks(rows), None)))
        snap = api.pages[N]; row = {'note_id': N, 'rendered': rows, 'properties': deepcopy(snap['properties_raw'])}
        before = p.encoded(row)
        self.assertEqual(len(e.created(snap, row, DS, snap['page_id'])), 100)
        self.assertEqual(p.encoded(row), before)
        with self.assertRaises(e.Blocked): e.created(snap, row, DS, 'f' * 32)
        changed = deepcopy(snap); changed['properties_raw']['Title']['title'][0]['text']['content'] += ' changed'
        with self.assertRaisesRegex(e.Blocked, '^readback_mismatch$'): e.created(changed, row, DS, snap['page_id'])

    def test_appended_compacted_roots_preserve_every_old_node(self):
        _, rows = rendered(); api = fixtures.API(None)
        old = api.make(N, [fixtures.paragraph('User prefix'), fixtures.paragraph('Old owner')])
        before = deepcopy(api.pages[N]); after = deepcopy(before)
        added, ids = api.rows(unwire(t.wire_blocks(e.request_blocks(rows), 'paragraph')), before['page_id'])
        after['recursive_blocks'] += added
        row = {'note_id': N, 'rendered': rows, 'after_anchor': old[-1]}
        self.assertEqual(e.appended(before, after, row, DS, ids)[0], ids)
        after['recursive_blocks'][0]['payload']['rich_text'][0]['text']['content'] += ' changed'
        with self.assertRaisesRegex(e.Blocked, '^readback_mismatch$'): e.appended(before, after, row, DS, ids)

    def test_owned_compacted_readback_is_noop_and_retains_node_hash_guards(self):
        data, rows = rendered(); blob = p.git_blob(data); api = fixtures.API(None)
        api.make(N, unwire(t.wire_blocks(e.request_blocks(rows), None)))
        snap = api.pages[N]; view = e.target(snap, N, DS)
        source = {'path': PATH, 'blob': blob, 'sha256': p.digest(data)}
        receipt = e.receipt(snap, N, DS, view['order'], {'commit': 'a' * 40}, source)
        batch = {'commit': 'b' * 40, 'content_complete': {N: True}, 'content_tree': {PATH: blob}, 'content_sha256': {PATH: p.digest(data)}}
        mapping = {'path': PATH, 'md_blob': blob, 'page_id': snap['page_id'], 'data_source_id': DS}
        policy = {'allowlist': [N], 'data_source_id': DS, 'ownership_sha256': {N: p.digest(receipt)}}
        check = lambda target: p.record_plan(N, batch, mapping, {blob: data}, {}, target, receipt, policy)
        self.assertEqual(check(snap)['decision'], 'noop_verified')
        changed = deepcopy(snap); changed['recursive_blocks'][0]['payload']['rich_text'][0]['annotations']['bold'] = True
        self.assertEqual(check(changed)['reason_codes'], ['human_edit_conflict'])

    def test_actual_text_annotations_link_color_structure_changes_still_reject(self):
        _, rows = rendered(); api = fixtures.API(None)
        api.make(N, unwire(t.wire_blocks(e.request_blocks(rows), None)))
        snapshot = api.pages[N]; row = {'note_id': N, 'rendered': rows, 'properties': {}}
        def text(s): s['recursive_blocks'][0]['payload']['rich_text'][0]['text']['content'] += 'changed'
        def annotation(s): s['recursive_blocks'][0]['payload']['rich_text'][0]['annotations']['italic'] = True
        def link(s): s['recursive_blocks'][0]['payload']['rich_text'][0]['text']['link'] = {'url': 'https://example.invalid/changed'}
        def color(s): s['recursive_blocks'][0]['payload']['color'] = 'red'
        def kind(s): s['recursive_blocks'][0]['type'] = 'quote'
        def order(s): s['recursive_blocks'][0], s['recursive_blocks'][1] = s['recursive_blocks'][1], s['recursive_blocks'][0]
        def missing(s): s['recursive_blocks'].pop()
        for change in (text, annotation, link, color, kind, order, missing):
            with self.subTest(change=change.__name__):
                changed = deepcopy(snapshot); change(changed)
                with self.assertRaisesRegex(e.Blocked, '^readback_mismatch$'): e.created(changed, row, DS, snapshot['page_id'])

    def test_children_and_table_cell_boundaries_still_exact(self):
        text = fixtures.paragraph('a')
        nested = deepcopy(text); nested['children'] = [fixtures.paragraph('b')]
        flat = fixtures.paragraph('a\nb')
        self.assertNotEqual(p.comparison_semantics([nested]), p.comparison_semantics([flat]))
        table = p.block('table', {'table_width': 2, 'has_column_header': False, 'has_row_header': False},
            [p.block('table_row', {'cells': [[p.text_run('a'), p.text_run('b')], [p.text_run('c')]]})])
        normalized = deepcopy(table); normalized['children'][0]['payload']['cells'][0] = [p.text_run('ab')]
        self.assertEqual(p.comparison_semantics([table]), p.comparison_semantics([normalized]))
        changed = deepcopy(normalized); changed['children'][0]['payload']['cells'] = [[p.text_run('a')], [p.text_run('bc')]]
        self.assertNotEqual(p.comparison_semantics([table]), p.comparison_semantics([changed]))

    def test_unknown_shape_and_mid_list_reset_are_not_normalized_away(self):
        row = fixtures.paragraph('text'); row['extra'] = True
        with self.assertRaises(p.Blocked): p.comparison_semantics([row])
        row = fixtures.paragraph('text'); row['payload']['unknown'] = None
        with self.assertRaises(p.Blocked): p.comparison_semantics([row])
        one = p.block('numbered_list_item', {'rich_text': [p.text_run('first')], 'list_start_index': 1})
        reset = p.block('numbered_list_item', {'rich_text': [p.text_run('reset')], 'list_start_index': 1})
        absent = deepcopy(reset); absent['payload'].pop('list_start_index')
        self.assertNotEqual(p.comparison_semantics([one, reset], None), p.comparison_semantics([one, absent], None))

    def test_real_git_acknowledged_create_resume_is_read_only_and_plan_exact(self):
        fixture = fixtures.Tests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.api.pages.clear(); state, sha = fixture.store.read(); state['receipts'] = {}
        fixture.store.compare_and_set(sha, state); fixture.publish(body()); plan = fixture.plan()
        before = p.encoded(plan); plan_sha = e.digest(plan)
        original = fixture.api.create_page
        def wire_create(ds, note, props, rows):
            return original(ds, note, props, unwire(t.wire_blocks(rows, None)))
        fixture.api.create_page = wire_create
        with patch.object(e, 'created', side_effect=e.Blocked('readback_mismatch')):
            self.assertEqual(fixture.run_plan(plan)['reason'], 'readback_mismatch')
        saved, _ = fixture.store.read()
        self.assertEqual(saved['journal']['operation']['phase'], 'acknowledged')
        self.assertIsNotNone(saved['journal']['operation']['response'])
        self.assertEqual(fixture.writes(), [('create',)])
        result = fixture.run_plan(plan)
        self.assertEqual(result['status'], 'verified'); self.assertEqual(result['external_write_attempts'], 0)
        self.assertEqual(fixture.writes(), [('create',)])
        self.assertEqual(p.encoded(plan), before); self.assertEqual(e.digest(plan), plan_sha)
        self.assertIsNone(fixture.store.read()[0]['journal'])
        repeat = fixture.plan(); self.assertEqual(repeat['records'], [])
        self.assertEqual(fixture.run_plan(repeat)['external_write_attempts'], 0)
        self.assertEqual(fixture.writes(), [('create',)])


if __name__ == '__main__': unittest.main()
