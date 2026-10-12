import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_sync_migration_check as check
import source_sync_inventory as inventory
from source_sync_assets import AssetError
from source_sync_store import write_if_changed
from verify_shadow_repeat import snapshot


def fixture():
    manifest = {'source_system': 'getnote', 'count': 3, 'notes': [
        {'note_id': '1001', 'path': 'images/PRIVATE.md', 'note_type': 'img_text'},
        {'note_id': 'opaque', 'path': 'legacy/PRIVATE.md'},
        {'note_id': '', 'path': 'unknown/PRIVATE.md'}]}
    tree = {'truncated': False, 'tree': [dict(path=p, type='blob', mode='100644', size=100, sha='a'*40)
            for p in ('images/PRIVATE.md', 'legacy/PRIVATE.md', 'unknown/PRIVATE.md', 'unindexed.md')]}
    note = {'note_id': '1001', 'note_type': 'img_text', 'title': 'PRIVATE', 'content': 'PRIVATE',
            'attachments': [{'type': 'image', 'url': 'https://example.com/image.png'}]}
    return manifest, tree, note


class MigrationCheckTests(unittest.TestCase):
    def test_tree_validates_bounds_aliases_and_no_symlinks(self):
        _, tree, _ = fixture()
        self.assertEqual(len(check.validate_tree(tree)), 4)
        for change in ('truncated', 'traversal', 'symlink', 'alias', 'size', 'sha', 'entries'):
            value = copy.deepcopy(tree)
            if change == 'truncated': value['truncated'] = True
            if change == 'traversal': value['tree'][0]['path'] = '../PRIVATE'
            if change == 'symlink': value['tree'][0]['mode'] = '120000'
            if change == 'alias': value['tree'][1]['path'] = value['tree'][0]['path'].upper()
            if change == 'size': value['tree'][0]['size'] = check.MAX_PRIOR_BYTES+1
            if change == 'sha': value['tree'][0]['sha'] = 'SECRET'
            if change == 'entries': value['tree'] *= 2000
            with self.assertRaises(ValueError): check.validate_tree(value)

    def test_all_manifest_paths_mapped_unknown_paths_preserved_without_body_claim(self):
        manifest, tree, _ = fixture()
        mapping, _ = inventory.validate_manifest(manifest)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = check.audit_map(root, check.validate_tree(tree), manifest, mapping)
            self.assertEqual(result, {'tree_markdown': 4, 'unindexed_markdown': 1,
                                      'mapped_api_paths': 1, 'path_map_ok': True})
            self.assertEqual(len(snapshot(root)), 4)
            self.assertFalse(check.safe_report(result)['full_existing_body_replay'])
            missing = check.validate_tree(tree); missing.pop('images/PRIVATE.md')
            with self.assertRaises(ValueError): check.audit_map(root, missing, manifest, mapping)

    def test_real_binary_repeat_checks_hash_mtime_local_link_and_unselected_files(self):
        manifest, tree, note = fixture()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            check.audit_map(root, check.validate_tree(tree), manifest, {'1001': 'images/PRIVATE.md'})
            result = check.roundtrip(root, 'images/PRIVATE.md', note, lambda: copy.deepcopy(note),
                                     fetch=lambda *a, **kw: (b'PNGfixture', 'image/png'))
            self.assertTrue(result['sample_hashes_and_mtimes_equal'])
            self.assertEqual(result['downloaded_assets'], 2)
            self.assertEqual(result['downloaded_bytes'], 20)
            self.assertTrue(result['sample_binary_readback_ok'])
            self.assertFalse((root/'03 State'/'receipt.json').exists())

    def test_asset_failure_does_not_write_note_cache_or_receipt(self):
        manifest, tree, note = fixture()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            check.audit_map(root, check.validate_tree(tree), manifest, {'1001': 'images/PRIVATE.md'})
            before = snapshot(root)
            def fail(*a, **kw): raise AssetError('PRIVATE')
            with self.assertRaises(AssetError): check.roundtrip(root, 'images/PRIVATE.md', note, lambda: note, fetch=fail)
            self.assertEqual(snapshot(root), before)

    def test_changed_binary_and_real_header_mismatch_fail_exact_acceptance(self):
        manifest, tree, note = fixture()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            check.audit_map(root, check.validate_tree(tree), manifest, {'1001': 'images/PRIVATE.md'})
            contents = iter((b'FIRST', b'SECOND'))
            with self.assertRaisesRegex(ValueError, 'repeat mismatch'):
                check.roundtrip(root, 'images/PRIVATE.md', note, lambda: note,
                                fetch=lambda *a, **kw: (next(contents), 'image/png'))
            write_if_changed(root/'images/PRIVATE.md', '---\nnote_id: "9999"\n---\nPRIVATE')
            with self.assertRaisesRegex(ValueError, 'real header mismatch'):
                check.roundtrip(root, 'images/PRIVATE.md', note, lambda: note)

    def test_detail_count_and_budget_are_bounded(self):
        _, _, note = fixture()
        progress = {'detail_calls': 0}
        budget = inventory.ResponseBudget(12*check.MIB)
        with patch.object(check.sync, 'api_get', return_value={'data': {'note': note}}) as api:
            for _ in range(4): check.detail('1001', 'KEY', 'CLIENT', budget, progress)
            with self.assertRaises(ValueError): check.detail('1001', 'KEY', 'CLIENT', budget, progress)
            self.assertEqual(api.call_count, 4)
            self.assertTrue(all(c.kwargs['max_response_bytes'] == 2*check.MIB for c in api.call_args_list))

    def test_only_bounded_image_file_descriptors_and_allowlisted_output(self):
        _, _, note = fixture()
        self.assertTrue(check.supported_binary_note(note))
        for key, value in [('images', [{'url':'PRIVATE'}]), ('attachments', note['attachments']*3),
                           ('attachments', [{'type':'audio','url':'https://example.com/audio.mp3'}])]:
            bad = copy.deepcopy(note); bad[key] = value
            self.assertFalse(check.supported_binary_note(bad))
        value = dict.fromkeys(check.FLAGS, True)
        value.update(dict.fromkeys(check.COUNTS, 0))
        value.update(status='mapping_and_binary_ok', PRIVATE='SECRET', source_complete=True, published=True,
                     full_existing_body_replay=True, detail_calls=2, downloaded_assets=2, downloaded_bytes=20)
        report = check.safe_report(value)
        self.assertEqual(report['status'], 'mapping_and_binary_ok')
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertFalse(report['source_complete']); self.assertFalse(report['published'])
        self.assertFalse(report['full_existing_body_replay'])

    def test_private_snapshot_pins_exact_commit_and_seven_bounded_metadata_reads(self):
        manifest, tree, _ = fixture()
        responses = [b'{"private":true}', json.dumps({'object': {'type':'commit','sha':'a'*40}}).encode(),
                     json.dumps({'tree':{'sha':'b'*40}}).encode()]
        for path in ('Raw', '03 Get'):
            responses.append(json.dumps({'truncated':False, 'tree':[
                {'path':path,'type':'tree','mode':'040000','sha':'b'*40}]}).encode())
        responses += [json.dumps(tree).encode(), json.dumps(manifest).encode()]
        with patch.dict(check.os.environ, {'PRIVATE_VAULT_TOKEN':'SECRET'}), \
             patch.object(check.sample, 'github_read', side_effect=responses) as read:
            token, commit, blobs, result, mapping, meta = check.private_snapshot()
            self.assertEqual(read.call_count, 7)
            self.assertTrue(read.call_args_list[-1].args[0].endswith('?ref='+'a'*40))
            self.assertTrue(all(c.args[2] <= 2*check.MIB for c in read.call_args_list))
            self.assertEqual(mapping, {'1001':'images/PRIVATE.md'})


if __name__ == '__main__': unittest.main()
