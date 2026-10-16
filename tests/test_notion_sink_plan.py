from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_sink_plan as n


class SinkTests(unittest.TestCase):
    def setUp(self):
        self.ident = '1234567890123456789'; self.page = '1' * 32
        self.data_source = 'a' * 32; self.allowlist = ['1234567890123456789', '2234567890123456789']
        for name, value in [('DATA_SOURCE_SHA256', n.digest(self.data_source)), ('COHORT_SHA256', n.digest(sorted(self.allowlist)))]:
            guard = patch.object(n, name, value); guard.start(); self.addCleanup(guard.stop)
        self.path = 'Raw/03 Get/03 纯文本记录/example.md'
        self.md = self.markdown('## Header\n\nFull **bold** text, [link](https://example.invalid).\n\nLast paragraph.\n')
        self.blob = n.git_blob(self.md)
        self.mapping = {self.ident: {'path': self.path, 'md_blob': self.blob,
            'page_id': self.page, 'data_source_id': self.data_source}}
        self.blobs = {self.blob: self.md}; self.assets = {}
        self.batch = {'repository': n.REPOSITORY, 'commit': 'a' * 40, 'parent': 'b' * 40,
            'publication_status': 'canonical_published', 'remote_readback_verified': True,
            'changed_content_ids': [self.ident], 'content_tree': {self.path: self.blob},
            'content_sha256': {self.path: n.digest(self.md)}, 'content_complete': {self.ident: True}}
        self.policy = {'repository': n.REPOSITORY, 'data_source_id': self.data_source,
            'allowlist': sorted(self.allowlist), 'read_only': True, 'format_accepted': False,
            'allow_create': False, 'allow_property_updates': False, 'allow_deletes': False,
            'expected_source_commit': self.batch['commit'], 'ownership_sha256': {}}
        rendered, _, _ = n.render_markdown(self.md, self.ident, {}, self.path, self.batch['content_tree'])
        self.snapshot, self.receipt = self.target(rendered)
        self.pin()

    def markdown(self, body, ident=None):
        return ('---\nnote_id: "' + (ident or self.ident) + '"\ncreated_at: "Original raw date"\n---\n' + body).encode()

    def target(self, rendered):
        protected = {'id': '2' * 32, 'parent_id': self.page, 'type': 'callout',
            'payload': {'rich_text': [n.text_run('Preview only; never silently remove.')],
                        'icon': {'type': 'emoji', 'emoji': '📝'}}, 'children_ids': [], 'has_children': False}
        rows = [protected]; serial = [3]
        def append(block, parent):
            ident = ('%032x' % serial[0]); serial[0] += 1
            row = {'id': ident, 'parent_id': parent, 'type': block['type'], 'payload': deepcopy(block['payload']), 'children_ids': [], 'has_children': bool(block['children'])}
            rows.append(row)
            for child in block['children']: row['children_ids'].append(append(child, ident))
            return ident
        for block in rendered: append(block, self.page)
        snapshot = {'complete': True, 'native_blocks_available': True, 'page_id': self.page,
            'parent_data_source': self.data_source, 'id_property': self.ident,
            'identity_query_complete': True, 'exact_id_matches': [self.page],
            'last_edited_time': '2026-10-16T10:00:00Z', 'fetched_at': '2026-10-16T10:00:01Z',
            'properties_raw': {'笔记 ID': {'type': 'rich_text', 'rich_text': [n.text_run(self.ident)]}, 'Relations': {'relation': [{'id': 'f' * 32}]}, 'Title': {'title': [{'text': {'content': 'Human title'}}]}},
            'recursive_blocks': rows}
        nodes, _, order, _ = n.snapshot_tree(snapshot)
        receipt = {'note_id': self.ident, 'page_id': self.page, 'data_source_id': self.data_source,
            'source_commit': self.batch['commit'], 'md_blob': self.blob, 'renderer_version': n.RENDERER,
            'managed_blocks': [{'id': i, 'normalized_hash': n.block_hash(nodes[i])} for i in order if i != protected['id']],
            'protected_blocks': [{'id': protected['id'], 'normalized_hash': n.block_hash(nodes[protected['id']])}],
            'properties_baseline': deepcopy(snapshot['properties_raw']), 'asset_bindings': {}, 'readback_verified': True}
        return snapshot, receipt

    def pin(self):
        self.policy['ownership_sha256'][self.ident] = n.digest(self.receipt)

    def plan(self):
        return n.plan_sink(self.batch, self.mapping, self.blobs, self.assets,
            {self.ident: self.snapshot}, {self.ident: self.receipt}, self.policy)

    def record(self): return self.plan()['records'][0]

    def change_source(self, body):
        self.md = self.markdown(body); self.blob = n.git_blob(self.md)
        self.blobs[self.blob] = self.md; self.mapping[self.ident]['md_blob'] = self.blob
        self.batch['content_tree'][self.path] = self.blob; self.batch['content_sha256'][self.path] = n.digest(self.md)

    def test_matching_native_target_noop_preserves_properties_and_preview_callout(self):
        before = deepcopy((self.snapshot, self.receipt, self.policy)); result = self.plan()
        self.assertEqual(result['decision'], 'noop_verified'); self.assertEqual(result['operations'], [])
        self.assertEqual(before, (self.snapshot, self.receipt, self.policy))
        self.assertFalse(result['receipt_advance']); self.assertEqual(result['records'][0]['protected_block_ids'], ['2' * 32])

    def test_changed_source_preview_only_never_whole_page_or_property_replace(self):
        self.change_source('## New header\n\nChanged source.\n'); record = self.record()
        self.assertEqual(record['decision'], 'preview_diff')
        self.assertEqual(record['reason_codes'], ['writer_gate_unknown', 'format_not_accepted', 'authorization_missing'])
        self.assertEqual(record['operations'][0]['operation'], 'preview_owned_region')
        self.assertFalse(record['operations'][0]['executable']); self.assertFalse(record['apply_ready'])

    def test_repeated_plan_exact_and_no_receipt_advance(self):
        self.change_source('A source update.\n'); self.assertEqual(self.plan(), self.plan())
        self.assertFalse(self.record()['receipt_advance'])

    def test_managed_human_edit_is_conflict(self):
        self.snapshot['recursive_blocks'][1]['payload']['rich_text'] = [n.text_run('Human edit')]
        self.assertEqual(self.record()['reason_codes'], ['human_edit_conflict'])
        self.assertEqual(self.plan()['operations'], [])

    def test_unowned_new_paragraph_and_changed_properties_are_preserved(self):
        self.snapshot['recursive_blocks'].append({'id': 'e' * 32, 'parent_id': self.page, 'type': 'paragraph', 'payload': {'rich_text': [n.text_run('User addition')]}, 'children_ids': [], 'has_children': False})
        self.snapshot['properties_raw']['New relation'] = {'relation': [{'id': 'd' * 32}]}
        before = deepcopy(self.snapshot); record = self.record()
        self.assertEqual(record['decision'], 'noop_verified'); self.assertIn('e' * 32, record['protected_block_ids'])
        self.assertEqual(self.snapshot, before)

    def test_duplicate_identity_rejected_without_title_or_time_selection(self):
        self.snapshot['exact_id_matches'].append('f' * 32)
        self.assertEqual(self.record()['reason_codes'], ['duplicate_id'])

    def test_nineteen_digit_string_roundtrips_numeric_id_rejected(self):
        self.assertEqual(json.loads(json.dumps(self.plan()))['records'][0]['note_id'], self.ident)
        self.batch['changed_content_ids'] = [int(self.ident)]
        self.assertEqual(self.plan()['reason_codes'], ['id_not_string'])

    def test_state_only_commit_never_enqueues(self):
        self.batch['changed_content_ids'] = []
        out = self.plan(); self.assertEqual(out['decision'], 'noop_verified')
        self.assertEqual(out['reason_codes'], ['state_only_commit']); self.assertEqual(out['enqueue_count'], 0)

    def test_explicit_initial_preview_is_separate_from_changed_content(self):
        self.batch['changed_content_ids'] = []
        self.batch['publication_status'] = 'published'
        self.policy['initial_two_preview'] = {'source_commit': self.batch['commit'], 'ids': sorted(self.allowlist), 'authorized_read_only': True}
        out = self.plan()
        self.assertEqual(out['source_mode'], 'initial_two_preview')
        self.assertEqual(len(out['records']), 2)
        self.assertEqual(out['enqueue_count'], 0)
        self.assertFalse(out['apply_ready'])
        self.assertEqual(out['records'][0]['decision'], 'noop_verified')
        self.assertEqual(out['records'][1]['reason_codes'], ['incomplete_source'])

    def test_initial_preview_requires_exact_source_cohort_and_explicit_gate(self):
        self.batch['changed_content_ids'] = []; self.batch['publication_status'] = 'published'
        correct = {'source_commit': self.batch['commit'], 'ids': sorted(self.allowlist), 'authorized_read_only': True}
        for bad in ({**correct, 'source_commit': 'f'*40}, {**correct, 'ids': [self.ident]}, {**correct, 'authorized_read_only': False}, {**correct, 'extra': True}):
            self.policy['initial_two_preview'] = bad
            self.assertEqual(self.plan()['reason_codes'], ['authorization_missing'])
        self.policy['initial_two_preview'] = correct; self.batch['changed_content_ids'] = [self.ident]
        self.assertEqual(self.plan()['reason_codes'], ['authorization_missing'])

    def test_published_noncanonical_state_only_without_preview_is_not_adopted(self):
        self.batch['changed_content_ids'] = []; self.batch['publication_status'] = 'published'
        self.assertEqual(self.plan()['reason_codes'], ['uncommitted_source'])

    def test_running_prepare_or_unconfirmed_publication_never_enqueues(self):
        for status in ('prepared', 'dry_run', 'canonical_preview_candidate_verified'):
            self.batch['publication_status'] = status
            self.assertEqual(self.plan()['reason_codes'], ['dry_run_source'])
        self.batch['publication_status'] = 'canonical_published'; self.batch['remote_readback_verified'] = False
        self.assertEqual(self.plan()['reason_codes'], ['uncommitted_source'])

    def test_incomplete_source_and_wrong_blob_retain_target(self):
        self.batch['content_complete'][self.ident] = False
        self.assertEqual(self.record()['reason_codes'], ['incomplete_source'])
        self.batch['content_complete'][self.ident] = True; self.blobs[self.blob] += b'changed'
        self.assertEqual(self.record()['reason_codes'], ['incomplete_source'])

    def test_new_target_missing_is_explicit_block_no_create_fallback(self):
        self.snapshot['exact_id_matches'] = []
        self.assertEqual(self.record()['reason_codes'], ['missing_target']); self.assertEqual(self.plan()['operations'], [])

    def test_non_native_enhanced_markdown_cannot_become_ownership(self):
        self.snapshot.update(complete=False, native_blocks_available=False, recursive_blocks=[])
        self.assertEqual(self.record()['reason_codes'], ['missing_block_ownership'])

    def test_missing_or_forged_ownership_hash_is_rejected(self):
        self.policy['ownership_sha256'] = {}
        self.assertEqual(self.record()['reason_codes'], ['missing_block_ownership'])
        self.pin(); self.receipt['md_blob'] = 'e' * 40
        self.assertEqual(self.record()['reason_codes'], ['missing_block_ownership'])

    def test_missing_child_unknown_type_move_and_reorder_fail_closed(self):
        original = deepcopy(self.snapshot)
        self.snapshot['recursive_blocks'][1]['has_children'] = True
        self.assertEqual(self.record()['reason_codes'], ['incomplete_target_read'])
        self.snapshot = deepcopy(original); self.snapshot['recursive_blocks'][1]['type'] = 'unknown'
        self.assertEqual(self.record()['reason_codes'], ['incomplete_target_read'])
        self.snapshot = deepcopy(original); self.snapshot['recursive_blocks'][1:3] = self.snapshot['recursive_blocks'][1:3][::-1]
        self.assertEqual(self.record()['reason_codes'], ['human_edit_conflict'])

    def test_wrong_parent_and_incomplete_exact_id_query_block(self):
        self.snapshot['parent_data_source'] = 'f' * 32
        self.assertEqual(self.record()['reason_codes'], ['wrong_parent'])
        self.snapshot['parent_data_source'] = self.data_source; self.snapshot['identity_query_complete'] = False
        self.assertEqual(self.record()['reason_codes'], ['incomplete_target_read'])

    def test_privacy_held_audio_outside_exact_two_allowlist(self):
        self.batch['changed_content_ids'] = ['3234567890123456789']
        self.assertEqual(self.plan()['records'][0]['reason_codes'], ['privacy_hold'])

    def test_image_requires_commit_bytes_and_confirmed_persistent_file_identity(self):
        self.change_source('![Caption](../assets/image.png)\n')
        self.assertEqual(self.record()['reason_codes'], ['missing_asset'])
        image = b'exact synthetic image bytes'; path = 'Raw/03 Get/assets/image.png'; oid = n.git_blob(image)
        self.assets['../assets/image.png'] = {'bytes': image, 'path': path, 'git_blob': oid, 'sha256': n.digest(image), 'notion_file_id': 'f' * 32, 'readback_verified': True}
        self.batch['content_tree'][path] = oid
        out = self.record(); self.assertEqual(out['decision'], 'preview_diff')
        payload = out['operations'][0]['proposed_blocks'][0]['payload']; self.assertEqual(payload['file_identity'], 'f' * 32)
        self.assertNotIn('url', payload)
        self.assets['../assets/image.png']['bytes'] += b'corrupt'
        self.assertEqual(self.record()['reason_codes'], ['missing_asset'])

    def test_table_heading_fallback_code_task_and_trailing_text_render(self):
        self.change_source('#### Deep heading\n\n| A | B |\n| --- | --- |\n| value | **bold** |\n\n```python\nprint(1)\n```\n\n- [x] Complete\n\nTrailing final paragraph.\n')
        out = self.record(); self.assertEqual(out['decision'], 'preview_diff')
        blocks = out['operations'][0]['proposed_blocks']
        self.assertEqual([b['type'] for b in blocks], ['paragraph', 'table', 'code', 'to_do', 'paragraph'])
        self.assertIn('heading_level_fallback', out['render_warnings'])
        self.assertEqual(blocks[-1]['payload']['rich_text'][0]['text']['content'], 'Trailing final paragraph.')

    def test_unsupported_nested_syntax_is_explicit_not_flattened(self):
        self.change_source('- Parent\n  - Child\n')
        self.assertEqual(self.record()['reason_codes'], ['unsupported_source_format'])

    def test_property_or_create_permission_does_not_open_mutation_mode(self):
        for key in ('allow_create', 'allow_property_updates', 'allow_deletes'):
            self.policy[key] = True
            self.assertEqual(self.plan()['reason_codes'], ['authorization_missing'])
            self.policy[key] = False

    def test_writer_and_format_gates_do_not_create_apply_permission(self):
        self.change_source('New body.\n'); self.policy['format_accepted'] = True
        self.policy['old_writer_evidence'] = {'fresh_readback_verified': True, 'writers': {name: {'disabled': True, 'active_runs': 0} for name in ('getnote-to-notion', 'notion-to-getnote')}}
        out = self.record(); self.assertEqual(out['reason_codes'], ['authorization_missing']); self.assertFalse(out['apply_ready'])

    def test_uncertain_append_create_never_blindly_retries(self):
        journal = {'status': 'uncertain', 'note_id': self.ident, 'page_id': self.page, 'data_source_id': self.data_source, 'expected_after_hash': 'f' * 64}
        for status in ('uncertain', 'prepared'):
            journal['status'] = status; out = n.inspect_uncertain_operation(journal, self.snapshot)
            self.assertFalse(out['retry_allowed']); self.assertEqual(out['operations'], []); self.assertEqual(out['decision'], 'blocked')
        journal['expected_after_hash'] = self.record()['before_hash']
        out = n.inspect_uncertain_operation(journal, self.snapshot)
        self.assertEqual(out['decision'], 'noop_verified'); self.assertFalse(out['retry_allowed']); self.assertFalse(out['receipt_advance'])

    def test_public_summary_contains_no_identity_or_private_text(self):
        self.change_source('PRIVATE BODY https://private.invalid\n')
        text = json.dumps(n.public_summary(self.plan()))
        for private in (self.ident, self.page, 'PRIVATE', 'https', 'example.md'):
            self.assertNotIn(private, text)
        self.assertEqual(n.public_summary({'records': ['bad']})['records'], 0)

    def test_two_samples_independent_noops_and_record_conflict_isolation(self):
        ident2 = '2234567890123456789'; old_ident = self.ident
        self.ident = ident2; md2 = self.markdown('Second sample.\n'); blob2 = n.git_blob(md2)
        rendered, _, _ = n.render_markdown(md2, ident2, {}, self.path, {})
        self.page = '9' * 32; snap2, receipt2 = self.target(rendered); self.page = '1' * 32; self.ident = old_ident
        path2 = 'Raw/03 Get/03 纯文本记录/second.md'; self.batch['changed_content_ids'].append(ident2)
        self.batch['content_complete'][ident2] = True; self.batch['content_tree'][path2] = blob2; self.batch['content_sha256'][path2] = n.digest(md2)
        self.blobs[blob2] = md2; self.mapping[ident2] = {**self.mapping[old_ident], 'path': path2, 'md_blob': blob2, 'page_id': '9' * 32}
        self.policy['ownership_sha256'][ident2] = n.digest(receipt2)
        targets = {old_ident: self.snapshot, ident2: snap2}; receipts = {old_ident: self.receipt, ident2: receipt2}
        out = n.plan_sink(self.batch, self.mapping, self.blobs, {}, targets, receipts, self.policy)
        self.assertEqual([r['decision'] for r in out['records']], ['noop_verified', 'noop_verified'])
        targets[old_ident]['exact_id_matches'].append('f' * 32)
        out = n.plan_sink(self.batch, self.mapping, self.blobs, {}, targets, receipts, self.policy)
        self.assertEqual([r['decision'] for r in out['records']], ['blocked', 'noop_verified'])

    def test_two_ids_cannot_map_to_one_page(self):
        self.mapping['2234567890123456789'] = deepcopy(self.mapping[self.ident])
        self.assertEqual(self.plan()['reason_codes'], ['duplicate_id'])

    def test_raw_id_property_must_match_extracted_id(self):
        self.snapshot['properties_raw']['笔记 ID']['rich_text'] = [n.text_run('2234567890123456789')]
        self.assertEqual(self.record()['reason_codes'], ['unmapped_id'])

    def test_unsupported_inline_markup_never_becomes_plain_semantic_noop(self):
        for body in ('_italic_', '~~strike~~', 'Text <b>bold</b>', '**nested _italic_**', '[<b>label</b>](https://example.invalid)'):
            self.change_source(body + '\n')
            self.assertEqual(self.record()['reason_codes'], ['unsupported_source_format'])

    def test_divider_after_paragraph_keeps_native_boundary_and_setext_blocks(self):
        self.change_source('Before divider\n___\nAfter divider\n')
        blocks = self.record()['operations'][0]['proposed_blocks']
        self.assertEqual([b['type'] for b in blocks], ['paragraph', 'divider', 'paragraph'])
        self.change_source('Setext heading\n---\n')
        self.assertEqual(self.record()['reason_codes'], ['unsupported_source_format'])

    def test_uncertain_recovery_binds_exact_page_even_with_empty_body(self):
        snap = deepcopy(self.snapshot); snap['recursive_blocks'] = []
        nodes, _, order, _ = n.snapshot_tree(snap)
        after = n.digest({'nodes': [nodes[i] for i in order], 'properties': snap['properties_raw']})
        journal = {'status': 'uncertain', 'note_id': self.ident, 'page_id': self.page, 'data_source_id': self.data_source, 'expected_after_hash': after}
        self.assertEqual(n.inspect_uncertain_operation(journal, snap)['decision'], 'noop_verified')
        for field in ('page_id', 'exact_id_matches'):
            bad = deepcopy(snap); bad[field] = '9' * 32 if field == 'page_id' else ['9' * 32]
            self.assertEqual(n.inspect_uncertain_operation(journal, bad)['decision'], 'blocked')

    def test_malformed_optional_evidence_and_recovery_snapshot_fail_closed(self):
        journal = {'status': 'uncertain'}
        for malformed in (None, [], 'not a snapshot'):
            self.assertEqual(n.inspect_uncertain_operation(journal, malformed)['decision'], 'blocked')
        self.batch['content_complete'] = []
        self.assertEqual(self.record()['reason_codes'], ['incomplete_source'])
        self.batch['content_complete'] = {self.ident: True}; self.policy['ownership_sha256'] = []
        self.assertEqual(self.record()['reason_codes'], ['missing_block_ownership'])


if __name__ == '__main__': unittest.main()
