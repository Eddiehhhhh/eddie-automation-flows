"""Rebuilt synthetic coverage for the byte-restored comparison-only HTTP rule.

The original test file was unavailable after workspace rollback. This replacement
is independently reviewed; it does not claim the original test file's hash.
No private source text, page identifiers, credentials or network calls are used.
"""
from copy import deepcopy
from unittest.mock import patch
import unittest

import get_notion_executor as e
import get_notion_sink_plan as p
import get_notion_live_transport as t
import test_notion_executor as f
from test_notion_compare_normalization import unwire

PATH = 'Raw/03 Get/notes/http-comparison.md'


def linked(label='link label', url='https://example.invalid'):
    run = p.text_run(label)
    run['text']['link'] = {'url': url}
    return run


def paragraph(url='https://example.invalid', label='link label'):
    return p.block('paragraph', {'rich_text': [linked(label, url)]})


def slash_links(rows):
    """Synthetic API model inserts only the slash, independently of the SUT."""
    result = deepcopy(rows)
    for row in result:
        payload = row['payload']
        groups = [payload[k] for k in ('rich_text', 'caption') if k in payload]
        groups += payload.get('cells', [])
        for runs in groups:
            for run in runs:
                link = run['text']['link']
                if link is not None:
                    url = link['url']
                    start = url.index('://') + 3
                    ends = [i for mark in ('?', '#') if (i := url.find(mark, start)) >= 0]
                    end = min(ends) if ends else len(url)
                    if '/' not in url[start:end]:
                        link['url'] = url[:end] + '/' + url[end:]
        row['children'] = slash_links(row['children'])
    return result


class Tests(unittest.TestCase):
    def test_legal_ascii_empty_paths_preserve_all_other_bytes(self):
        cases = [
            ('http://example.invalid', 'http://example.invalid/'),
            ('HTTPS://EXAMPLE.invalid', 'HTTPS://EXAMPLE.invalid/'),
            ('https://example.invalid:443', 'https://example.invalid:443/'),
            ('https://example.invalid:00080?q=%2f#x%2F', 'https://example.invalid:00080/?q=%2f#x%2F'),
            ('http://127.0.0.1:65535?#', 'http://127.0.0.1:65535/?#'),
            ('https://[2001:db8::1]:8443?q=one/two#part', 'https://[2001:db8::1]:8443/?q=one/two#part'),
            ('https://example.invalid?', 'https://example.invalid/?'),
            ('https://example.invalid#', 'https://example.invalid/#'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(p.comparison_http_empty_path(source), expected)
                self.assertEqual(p.comparison_http_empty_path(expected), expected)
                self.assertEqual(p.comparison_semantics([paragraph(source)]), p.comparison_semantics([paragraph(expected)]))

    def test_malformed_unsupported_userinfo_and_nonempty_paths_stay_exact(self):
        cases = [None, 42, '', 'example.invalid', '//example.invalid', 'ftp://example.invalid',
            'https://user@example.invalid', 'https://user:pass@example.invalid', 'https://',
            'https://example.invalid:', 'https://example.invalid:-1', 'https://example.invalid:65536',
            'https://example.invalid:000080', 'https://example.invalid:abc', 'https://[invalid]',
            'https://[::1', 'https://[::1]suffix', 'https://::1', 'https://例.invalid',
            'https://example.invalid/中文', 'https://example.invalid?q=é',
            'https://example.invalid%xx', 'https://example.invalid?q=%zz',
            'https://exa mple.invalid', 'https://example.invalid\n', 'https://example.invalid\t',
            r'https://example.invalid\path', 'https://example.invalid/?q=1',
            'https://example.invalid/a', 'https://example.invalid//', 'https://example.invalid/./',
            'https://example.invalid?q=x#one#two']
        for value in cases:
            with self.subTest(value=value): self.assertEqual(p.comparison_http_empty_path(value), value)

    def test_other_url_differences_remain_readback_mismatches(self):
        pairs = [
            ('https://example.invalid', 'http://example.invalid/'),
            ('HTTPS://example.invalid', 'https://example.invalid/'),
            ('https://EXAMPLE.invalid', 'https://example.invalid/'),
            ('https://example.invalid', 'https://other.invalid/'),
            ('https://example.invalid', 'https://example.invalid:443/'),
            ('http://example.invalid:80', 'http://example.invalid/'),
            ('https://example.invalid?q=1', 'https://example.invalid/?q=2'),
            ('https://example.invalid#one', 'https://example.invalid/#two'),
            ('https://example.invalid?', 'https://example.invalid/'),
            ('https://example.invalid#', 'https://example.invalid/'),
            ('https://example.invalid?q=%2f', 'https://example.invalid/?q=%2F'),
            ('https://example.invalid/a', 'https://example.invalid/a/'),
            ('https://user@example.invalid', 'https://user@example.invalid/'),
            ('https://例.invalid', 'https://例.invalid/'),
            ('https://example.invalid:65536', 'https://example.invalid:65536/'),
            ('mailto:user@example.invalid', 'mailto:user@example.invalid/'),
        ]
        for expected, actual in pairs:
            with self.subTest(pair=(expected, actual)):
                rows = [paragraph(expected)]; api = f.API(None); api.make(f.N, [paragraph(actual)])
                snap = api.pages[f.N]
                self.assertNotEqual(p.comparison_semantics(rows), p.comparison_semantics([paragraph(actual)]))
                with self.assertRaisesRegex(e.Blocked, '^readback_mismatch$'):
                    e.created(snap, {'note_id': f.N, 'rendered': rows, 'properties': {}}, f.DS, snap['page_id'])

    def test_recursive_caption_table_and_chunk_comparison_is_copy_only(self):
        nested = paragraph(); nested['children'] = [paragraph('http://example.invalid:81')]
        code = p.block('code', {'rich_text': [linked('print()')], 'caption': [linked('caption')], 'language': 'plain text'})
        table = p.block('table', {'table_width': 2, 'has_column_header': False, 'has_row_header': False},
            [p.block('table_row', {'cells': [[linked('a')], [linked('b', 'https://example.invalid?q=1')]]})])
        rows = [nested, code, table]; raw = p.encoded(rows)
        self.assertEqual(p.comparison_semantics(rows), p.comparison_semantics(slash_links(rows)))
        self.assertEqual(p.encoded(rows), raw)
        split = paragraph(); split['payload']['rich_text'] = [linked('a'), linked('b', 'https://example.invalid/')]
        self.assertEqual(p.comparison_semantics([split]), p.comparison_semantics([paragraph('https://example.invalid/', 'ab')]))
        changed = slash_links(rows); changed[2]['children'][0]['payload']['cells'].reverse()
        self.assertNotEqual(p.comparison_semantics(rows), p.comparison_semantics(changed))

    def test_render_wire_plan_and_node_hashes_are_not_rewritten(self):
        data = f.md(f.N, '[link label](https://example.invalid)')
        rows = p.render_markdown(data, f.N, {}, PATH, {PATH: p.git_blob(data)})[0]
        wire = t.wire_blocks(e.request_blocks(rows), None)
        plan = {'rendered': rows, 'request_blocks': wire}
        before = p.encoded(plan); sha = p.digest(plan)
        expected = deepcopy(rows)
        self.assertEqual(p.comparison_semantics(rows), p.comparison_semantics(slash_links(rows)))
        self.assertEqual(p.encoded(plan), before); self.assertEqual(p.digest(plan), sha)
        self.assertEqual(rows, expected)
        self.assertIn(b'https://example.invalid"', p.encoded(wire))
        api = f.API(None); api.make(f.N, rows); native = api.pages[f.N]['recursive_blocks'][0]
        changed = deepcopy(native); changed['payload']['rich_text'][0]['text']['link']['url'] += '/'
        self.assertNotEqual(p.block_hash(native), p.block_hash(changed))

    def test_created_accepts_slash_only_and_keeps_page_property_guards(self):
        rows = [paragraph(), paragraph('https://example.invalid:8080?q=1#x')]
        api = f.API(None); api.make(f.N, slash_links(rows)); snapshot = api.pages[f.N]
        row = {'note_id': f.N, 'rendered': rows, 'properties': deepcopy(snapshot['properties_raw'])}
        raw = p.encoded([row, snapshot])
        self.assertEqual(len(e.created(snapshot, row, f.DS, snapshot['page_id'])), 2)
        self.assertEqual(p.encoded([row, snapshot]), raw)
        with self.assertRaises(e.Blocked): e.created(snapshot, row, f.DS, 'f' * 32)
        changed = deepcopy(snapshot); changed['properties_raw']['Title']['title'][0]['text']['content'] += ' edited'
        with self.assertRaisesRegex(e.Blocked, '^readback_mismatch$'): e.created(changed, row, f.DS, snapshot['page_id'])
        for key in ('text', 'annotations'):
            changed = deepcopy(snapshot); run = changed['recursive_blocks'][0]['payload']['rich_text'][0]
            if key == 'text': run['text']['content'] += ' edited'
            else: run['annotations']['bold'] = True
            with self.assertRaisesRegex(e.Blocked, '^readback_mismatch$'): e.created(changed, row, f.DS, snapshot['page_id'])

    def test_appended_only_normalizes_new_payload_preserving_existing_nodes(self):
        api = f.API(None); roots = api.make(f.N, [paragraph(), f.paragraph('User tail')])
        before = deepcopy(api.pages[f.N]); after = deepcopy(before); rows = [paragraph('https://new.invalid')]
        new, ids = api.rows(slash_links(rows), before['page_id']); after['recursive_blocks'] += new
        row = {'note_id': f.N, 'rendered': rows, 'after_anchor': roots[-1]}
        raw = p.encoded(row)
        self.assertEqual(e.appended(before, after, row, f.DS, ids)[0], ids)
        self.assertEqual(p.encoded(row), raw)
        after['recursive_blocks'][0]['payload']['rich_text'][0]['text']['link']['url'] += '/'
        with self.assertRaisesRegex(e.Blocked, '^readback_mismatch$'): e.appended(before, after, row, f.DS, ids)

    def test_owned_noop_still_requires_exact_prior_receipt_node_hashes(self):
        data = f.md(f.N, '[link label](https://example.invalid)'); blob = p.git_blob(data)
        rows = p.render_markdown(data, f.N, {}, PATH, {PATH: blob})[0]
        api = f.API(None); api.make(f.N, slash_links(rows)); snap = api.pages[f.N]; view = e.target(snap, f.N, f.DS)
        source = {'path': PATH, 'blob': blob, 'sha256': p.digest(data)}
        receipt = e.receipt(snap, f.N, f.DS, view['order'], {'commit': 'a' * 40}, source)
        batch = {'commit': 'b' * 40, 'content_complete': {f.N: True}, 'content_tree': {PATH: blob}, 'content_sha256': {PATH: p.digest(data)}}
        mapping = {'path': PATH, 'md_blob': blob, 'page_id': snap['page_id'], 'data_source_id': f.DS}
        policy = {'allowlist': [f.N], 'data_source_id': f.DS, 'ownership_sha256': {f.N: p.digest(receipt)}}
        check = lambda target: p.record_plan(f.N, batch, mapping, {blob: data}, {}, target, receipt, policy)
        self.assertEqual(check(snap)['decision'], 'noop_verified')
        changed = deepcopy(snap); changed['recursive_blocks'][0]['payload']['rich_text'][0]['text']['link']['url'] = 'https://example.invalid'
        self.assertEqual(check(changed)['reason_codes'], ['human_edit_conflict'])

    def test_synthetic_99_roots_100_nodes_and_13_explicit_links(self):
        rows = [paragraph('https://example.invalid:' + str(8000 + i), '00:00:01') for i in range(12)]
        rows.append(paragraph('https://example.invalid', '00:00:13'))
        rows += [f.paragraph('Synthetic paragraph ' + str(i)) for i in range(86)]
        rows[-1]['children'] = [f.paragraph('Synthetic child')]
        api = f.API(None); api.make(f.N, slash_links(rows)); snap = api.pages[f.N]
        self.assertEqual(len(rows), 99); self.assertEqual(len(snap['recursive_blocks']), 100)
        self.assertNotEqual(p.digest(rows), p.digest(slash_links(rows)))
        self.assertEqual(len(e.created(snap, {'note_id': f.N, 'rendered': rows, 'properties': {}}, f.DS, snap['page_id'])), 100)

    def test_shape_boundaries_and_list_resets_are_not_normalized_away(self):
        bad = paragraph(); bad['unknown'] = 1
        with self.assertRaises(p.Blocked): p.comparison_semantics([bad])
        bad = paragraph(); bad['payload']['rich_text'][0]['text']['link']['unknown'] = 1
        with self.assertRaises(p.Blocked): p.comparison_semantics([bad])
        a = p.block('numbered_list_item', {'rich_text': [linked('a')], 'list_start_index': 1})
        b = p.block('numbered_list_item', {'rich_text': [linked('b')], 'list_start_index': 1})
        no_reset = deepcopy(b); no_reset['payload'].pop('list_start_index')
        self.assertNotEqual(p.comparison_semantics([a, b], None), p.comparison_semantics([a, no_reset], None))
        nested = paragraph(); nested['children'] = [paragraph()]
        self.assertNotEqual(p.comparison_semantics([nested]), p.comparison_semantics([paragraph(), paragraph()]))

    def test_real_git_saved_create_resume_does_not_recreate_or_change_plan(self):
        fixture = f.Tests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.api.pages.clear(); state, sha = fixture.store.read(); state['receipts'] = {}; fixture.store.compare_and_set(sha, state)
        fixture.publish('[link label](https://example.invalid:8080)'); plan = fixture.plan()
        raw = p.encoded(plan); digest = e.digest(plan); original = fixture.api.create_page
        def create(ds, note, props, blocks):
            return original(ds, note, props, slash_links(unwire(t.wire_blocks(blocks, None))))
        fixture.api.create_page = create
        with patch.object(e, 'created', side_effect=e.Blocked('readback_mismatch')):
            self.assertEqual(fixture.run_plan(plan)['reason'], 'readback_mismatch')
        saved, _ = fixture.store.read(); self.assertEqual(saved['journal']['operation']['phase'], 'acknowledged')
        page = saved['journal']['operation']['response']; self.assertEqual(fixture.writes(), [('create',)])
        result = fixture.run_plan(plan); self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['external_write_attempts'], 0); self.assertEqual(fixture.writes(), [('create',)])
        self.assertEqual(fixture.api.pages[f.N]['page_id'], page)
        self.assertEqual(p.encoded(plan), raw); self.assertEqual(e.digest(plan), digest)
        self.assertIsNone(fixture.store.read()[0]['journal'])
        repeat = fixture.plan(); self.assertEqual(repeat['records'], [])
        self.assertEqual(fixture.run_plan(repeat)['external_write_attempts'], 0)
        self.assertEqual(fixture.writes(), [('create',)])


if __name__ == '__main__': unittest.main()
