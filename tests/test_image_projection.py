from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_image_projection as m
from get_notion_image_projection import p, h

N = '1234567890123456789'; PAGE = '1' * 32; DS = '2' * 32
PATH = 'Raw/03 Get/notes/synthetic.md'; ASSET_PATH = 'Raw/03 Get/_assets/synthetic.jpeg'
REFERENCE = '../_assets/synthetic.jpeg'; ASSET = b'\xff\xd8\xffSynthetic fixture\xff\xd9'
IMAGE_LINE = '- Picture: ![[' + REFERENCE + ']]'


def snapshot(blocks):
    rows = []; serial = [100]
    def add(block, parent):
        serial[0] += 1; bid = '%032x' % serial[0]; children = []
        rows.append({'id': bid, 'parent_id': parent, 'type': block['type'],
            'payload': deepcopy(block['payload']), 'children_ids': children, 'has_children': bool(block['children'])})
        for child in block['children']: children.append(add(child, bid))
        return bid
    for block in blocks: add(block, PAGE)
    return {'complete': True, 'native_blocks_available': True, 'identity_query_complete': True,
        'exact_id_matches': [PAGE], 'page_id': PAGE, 'parent_data_source': DS, 'id_property': N,
        'properties_raw': {'笔记 ID': {'type': 'rich_text', 'rich_text': [p.text_run(N)]}},
        'last_edited_time': '2026-10-16T00:00:00Z', 'recursive_blocks': rows}


class Tests(unittest.TestCase):
    def setUp(self):
        self.data = ('---\nnote_id: ' + json.dumps(N) + '\ntitle: "Synthetic"\n---\n'
            '## Heading\n\nBody > comparison.\n\n' + IMAGE_LINE + '\n\nTail text.\n\n'
            '---\nsource: api\nupdated: 2026-10-16T00:00:00Z\ncanonicalId: api:' + N + '\n').encode()
        self.tree = {PATH: p.git_blob(self.data), ASSET_PATH: p.git_blob(ASSET)}
        self.binding = m.build_source_binding(self.data, N, PATH, self.tree, ASSET)
        self.binding_sha = p.digest(self.binding)
        self.history = ('<callout icon="📝" color="gray_bg">\n\tOriginal preview\n</callout>\n\n'
            '<image src="file-upload://11111111-2222-3333-4444-555555555555">Caption</image>\n\n'
            '## Heading\n\nBody > comparison.\n\n' + m.REPLACEMENT + '\n\nTail text.')
        self.ev = {'note_id': N, 'page_id': PAGE, 'source': {'commit': 'a' * 40, 'path': PATH,
            'blob': p.git_blob(self.data), 'sha256': p.digest(self.data)}, 'prepared_markdown': self.history,
            'readback': {'id': PAGE, 'text': '<content>\n' + self.history + '\n</content>'}, 'h4_mapping': 'heading_4'}
        self.proof = m.build_hash_proof(self.ev, p.digest(self.ev), self.data, self.tree, self.binding, self.binding_sha)
        self.proof_sha = p.digest(self.proof)

    def project(self, data=None, binding=None, tree=None):
        return m.project_image_source(self.data if data is None else data, N, PATH,
            self.tree if tree is None else tree, self.binding if binding is None else binding, self.binding_sha)

    def target(self):
        blocks = [p.block('paragraph', {'rich_text': [p.text_run('Original metadata')]}), p.block('divider', {})]
        for entry in h.historical_roots(self.history, N, h4_mapping='heading_4'):
            if entry.get('kind') == 'image':
                blocks.append({'type': 'image', 'payload': {'type': 'file', 'file': {'url': 'https://private.invalid/temporary', 'expiry_time': 'later'}, 'caption': []}, 'children': []})
            else: blocks.append(entry['block'])
        blocks.append(p.block('paragraph', {'rich_text': [p.text_run('User addition')]}))
        return snapshot(blocks)

    def test_exact_single_byte_region_only_and_original_identity_retained(self):
        result = self.project()
        self.assertEqual(result['markdown_bytes'], self.data.replace(IMAGE_LINE.encode(), m.REPLACEMENT.encode(), 1))
        self.assertEqual(result['source_blob'], p.git_blob(self.data)); self.assertEqual(result['source_sha256'], p.digest(self.data))
        self.assertNotEqual(result['source_sha256'], result['projection_sha256'])
        self.assertTrue(result['native_image_binding_required']); self.assertFalse(result['image_owned']); self.assertFalse(result['image_write_allowed'])

    def test_crlf_image_line_and_other_bytes_survive(self):
        data = self.data.replace((IMAGE_LINE + '\n').encode(), (IMAGE_LINE + '\r\n').encode())
        tree = {**self.tree, PATH: p.git_blob(data)}
        out = self.project(data, tree=tree)
        self.assertEqual(out['markdown_bytes'], data.replace(IMAGE_LINE.encode(), m.REPLACEMENT.encode(), 1))

    def test_text_changes_may_project_but_initial_history_proof_stays_original(self):
        data = self.data.replace(b'Tail text.', b'New user source text.'); tree = {**self.tree, PATH: p.git_blob(data)}
        self.assertIn(b'New user source text.', self.project(data, tree=tree)['markdown_bytes'])
        self.assertEqual(m.propose_from_git(self.proof, self.proof_sha, data, tree, self.target(), DS)['reason'], 'source_changed')

    def test_changed_deleted_duplicated_or_additional_image_references_block(self):
        for data in (self.data.replace(b'synthetic.jpeg', b'other.jpeg'), self.data.replace(IMAGE_LINE.encode(), b''),
                     self.data + ('\n' + IMAGE_LINE).encode(), self.data + b'\n![extra](x.jpeg)',
                     self.data + b'\n<IMG src="x">', self.data.replace(b'Picture:', b'Changed label:')):
            with self.subTest(data_sha=p.digest(data)), self.assertRaises(m.Blocked):
                self.project(data, tree={**self.tree, PATH: p.git_blob(data)})

    def test_code_wrapping_or_escaped_reference_never_silently_projects(self):
        for line in ('```\n' + IMAGE_LINE + '\n```', '~~~\n' + IMAGE_LINE + '\n~~~', IMAGE_LINE.replace('![[', '\\![[')):
            data = self.data.replace(IMAGE_LINE.encode(), line.encode())
            with self.assertRaises(m.Blocked): self.project(data, tree={**self.tree, PATH: p.git_blob(data)})

    def test_unsafe_reference_or_unapproved_grammar_blocks_at_binding_creation(self):
        for line in ('![[../_assets/synthetic.jpeg]]', '- ![[../../outside.jpeg]]', '- ![[https://evil.invalid/x.jpeg]]',
                     '- ![[../_assets/synthetic.jpeg|alias]]', '- ![[../_assets/x%20.jpeg]]', '- ![x](../_assets/synthetic.jpeg)'):
            data = self.data.replace(IMAGE_LINE.encode(), line.encode())
            with self.assertRaises(m.Blocked): m.build_source_binding(data, N, PATH, {**self.tree, PATH: p.git_blob(data)}, ASSET)

    def test_asset_tree_change_absence_or_uncommitted_source_is_conflict(self):
        for tree in ({PATH: p.git_blob(self.data)}, {**self.tree, ASSET_PATH: 'b' * 40}, {**self.tree, PATH: 'b' * 40}):
            with self.assertRaisesRegex(m.Blocked, 'asset_changed'): self.project(tree=tree)

    def test_untrusted_or_malformed_binding_cannot_be_self_adopted(self):
        for key, value in (('asset_sha256', 'f' * 64), ('asset_bytes', True), ('note_id', '999'), ('reference', 'other')):
            altered = {**self.binding, key: value}
            with self.assertRaises(m.Blocked): self.project(binding=altered)
        for value in (None, [], {}, b''):
            with self.assertRaises(m.Blocked): m.check_binding(value, self.binding_sha)

    def test_source_binding_requires_exact_jpeg_bytes_and_size(self):
        for raw in (b'', b'not JPEG', b'\xff\xd8\xffmissing end'):
            with self.assertRaises(m.Blocked): m.build_source_binding(self.data, N, PATH, self.tree, raw)
        self.assertEqual(self.binding['asset_sha256'], p.digest(ASSET)); self.assertEqual(self.binding['asset_bytes'], len(ASSET))

    def test_three_way_proof_and_native_unique_range_protect_image_and_user_blocks(self):
        target = self.target(); before = deepcopy(target)
        result = m.propose_from_git(self.proof, self.proof_sha, self.data, self.tree, target, DS)
        self.assertEqual(result['decision'], 'candidate'); self.assertEqual(len(result['managed_block_ids']), 4)
        image = next(row['id'] for row in target['recursive_blocks'] if row['type'] == 'image')
        self.assertIn(image, result['protected_block_ids']); self.assertNotIn(image, result['managed_block_ids'])
        self.assertTrue(result['native_image_binding_required']); self.assertFalse(result['execution_ready'])
        self.assertIsNone(result['executor_scope']); self.assertFalse(result['ownership_adopted']); self.assertEqual(target, before)

    def test_unrelated_readback_or_changed_prepared_cannot_produce_proof(self):
        for ev in ({**self.ev, 'readback': {'id': PAGE, 'text': '<content>Unrelated</content>'}},
                   {**self.ev, 'prepared_markdown': self.history.replace('Tail text.', 'Unrelated')}):
            with self.assertRaises((m.Blocked, h.Blocked)):
                m.build_hash_proof(ev, p.digest(ev), self.data, self.tree, self.binding, self.binding_sha)

    def test_missing_or_extra_native_image_is_not_a_text_only_success(self):
        target = self.target(); image = next(row for row in target['recursive_blocks'] if row['type'] == 'image')
        for rows in ([row for row in target['recursive_blocks'] if row['type'] != 'image'],
                     target['recursive_blocks'] + [{**image, 'id': 'f' * 32}],
                     [row for row in target['recursive_blocks'] if row['type'] != 'image'] + [image]):
            value = {**target, 'recursive_blocks': rows}
            out = m.propose_from_git(self.proof, self.proof_sha, self.data, self.tree, value, DS)
            self.assertEqual(out['decision'], 'blocked'); self.assertEqual(out['reason'], 'missing_asset')
            self.assertIsNone(out['executor_scope']); self.assertEqual(out['managed_block_ids'], [])

    def test_user_edit_and_duplicate_native_text_ranges_block(self):
        target = self.target(); changed = deepcopy(target)
        next(row for row in changed['recursive_blocks'] if row['type'] == 'heading_2')['payload']['rich_text'] = [p.text_run('User edit')]
        self.assertEqual(m.propose_from_git(self.proof, self.proof_sha, self.data, self.tree, changed, DS)['reason'], 'no_unique_range')
        expected = h.owned_sequence(h.historical_roots(self.history, N, h4_mapping='heading_4'))
        self.assertEqual(m.propose_from_git(self.proof, self.proof_sha, self.data, self.tree, snapshot(expected + expected), DS)['reason'], 'no_unique_range')

    def test_hash_only_template_exact_reconstruction_no_private_paths_or_ids(self):
        template = m.public_proof_template(self.proof, self.proof_sha); raw = json.dumps(template)
        for private in (N, PAGE, PATH, ASSET_PATH, REFERENCE, 'Tail text.', 'file-upload://'):
            self.assertNotIn(private, raw)
        args = dict(note_id=N, page_id=PAGE, source_path=PATH, source_data=self.data, content_tree=self.tree)
        self.assertEqual(m.reconstruct_proof(template, p.digest(template), **args), self.proof)
        for key, value in (('note_id', '999'), ('page_id', '3' * 32), ('source_path', 'Raw/03 Get/other.md')):
            with self.assertRaises(m.Blocked): m.reconstruct_proof(template, p.digest(template), **{**args, key: value})
        with self.assertRaises(m.Blocked): m.reconstruct_proof({**template, 'extra': True}, p.digest({**template, 'extra': True}), **args)

    def test_proof_mutation_requires_external_hash_and_recomputed_semantics(self):
        altered = {**self.proof, 'candidate_semantic_sha256': 'f' * 64}
        self.assertEqual(m.propose_from_git(altered, self.proof_sha, self.data, self.tree, self.target(), DS)['reason'], 'evidence_invalid')
        self.assertEqual(m.propose_from_git(altered, p.digest(altered), self.data, self.tree, self.target(), DS)['reason'], 'source_changed')
        for value in (None, [], {}):
            self.assertEqual(m.propose_from_git(value, self.proof_sha, self.data, self.tree, self.target(), DS)['decision'], 'blocked')

    def test_real_git_membership_source_and_asset_remain_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for path, data in ((PATH, self.data), (ASSET_PATH, ASSET)):
                target = root / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            def git(*args): return subprocess.run(['git', '-C', folder, *args], check=True, capture_output=True).stdout
            git('init', '-q'); git('config', 'user.name', 'Synthetic'); git('config', 'user.email', 'synthetic@example.invalid')
            git('add', '.'); git('commit', '-qm', 'Source'); commit = git('rev-parse', 'HEAD').decode().strip()
            tree = {path: git('rev-parse', commit + ':' + path).decode().strip() for path in (PATH, ASSET_PATH)}
            before = git('show-ref'); raw = git('show', commit + ':' + PATH)
            out = self.project(raw, tree=tree)
            self.assertEqual(out['source_blob'], tree[PATH]); self.assertEqual(out['asset_blob'], tree[ASSET_PATH])
            self.assertEqual(git('show-ref'), before); self.assertEqual((root / PATH).read_bytes(), self.data)


if __name__ == '__main__': unittest.main()
