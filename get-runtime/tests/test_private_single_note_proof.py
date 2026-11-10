import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, 'get-runtime/scripts')
import get_private_single_note_proof as proof


class ProofSelectionTest(unittest.TestCase):
    def row(self, note_id='12345678901234567890'):
        return {'note_id': note_id,
                'source_path': f'Raw/03 Get/99 录音卡笔记/sample-{note_id}.md',
                'source_blob_sha': 'a' * 40,
                'nonce': 'b' * 64}

    def test_resolves_only_the_hash_bound_allowlisted_key(self):
        mapping = {next(iter(proof.KEYS)): self.row()}
        with patch.object(proof.bindings, 'value', return_value=mapping) as read:
            self.assertEqual(proof.resolve(next(iter(proof.KEYS))), self.row()['note_id'])
        read.assert_called_once_with('get_private_single_note_proof', 'PROOF_KEYS')

    def test_unknown_key_rejected_before_private_read(self):
        with patch.object(proof.bindings, 'value') as read, self.assertRaises(ValueError):
            proof.resolve('unregistered-proof-key')
        read.assert_not_called()

    def test_duplicate_mapping_key_rejected(self):
        with patch.object(proof.bindings, 'value', return_value={
            next(iter(proof.KEYS)): self.row(), 'duplicate-alias': self.row()
        }), self.assertRaises(ValueError):
            proof.resolve(next(iter(proof.KEYS)))

    def test_invalid_id_or_path_rejected(self):
        key = next(iter(proof.KEYS))
        for row in (self.row('not-an-id'), {**self.row(), 'source_path': 'Raw/public.md'}):
            with self.subTest(row=row), patch.object(proof.bindings, 'value', return_value={key: row}), self.assertRaises(ValueError):
                proof.resolve(key)

    def test_github_env_contains_only_resolved_private_id(self):
        import tempfile
        from pathlib import Path
        key = next(iter(proof.KEYS)); note_id = self.row()['note_id']
        mapping = {key: self.row()}
        with tempfile.TemporaryDirectory() as directory:
            envfile = Path(directory) / 'env'; envfile.write_text('')
            with patch.dict('os.environ', {'RUNNER_TEMP':directory,'GITHUB_RUN_ID':'100',
                    'GITHUB_RUN_ATTEMPT':'1'}), \
                 patch.object(proof.bindings, 'value', return_value=mapping), patch('builtins.print') as output:
                result = proof.export_to_github_env(key, envfile)
            self.assertEqual(result, {'status': 'private_single_note_proof_resolved'})
            self.assertEqual(envfile.read_text(), f'GET_SINGLE_NOTE_ID={note_id}\n')
            output.assert_called_once_with(f'::add-mask::{note_id}', flush=True)

    def test_runtime_reads_private_id_from_stable_file_across_step_env_files(self):
        import os
        import tempfile
        from pathlib import Path
        note_id = self.row()['note_id']
        with tempfile.TemporaryDirectory() as directory:
            source_env = Path(directory) / 'resolve-env'; source_env.write_text('')
            runtime_env = Path(directory) / 'runtime-env'; runtime_env.write_text('')
            key = next(iter(proof.KEYS)); mapping = {key: self.row()}
            context = {'GET_SINGLE_NOTE_ID':'', 'GET_SINGLE_NOTE_MODE':'true',
                       'RUNNER_TEMP':directory, 'GITHUB_RUN_ID':'101', 'GITHUB_RUN_ATTEMPT':'2'}
            with patch.dict(os.environ, {**context,'GITHUB_ENV':str(source_env)}), \
                 patch.object(proof.bindings, 'value', return_value=mapping), \
                 patch('builtins.print'):
                proof.export_to_github_env(key, source_env)
            with patch.dict(os.environ, {**context,'GITHUB_ENV':str(runtime_env)}):
                self.assertEqual(proof.runtime_note_id(), note_id)
            proof_file = Path(directory) / 'get-single-note-proof-101-2.txt'
            proof_file.write_text(f'{note_id}\n456\n')
            with patch.dict(os.environ, {**context,'GITHUB_ENV':str(runtime_env)}):
                self.assertIsNone(proof.runtime_note_id())


if __name__ == '__main__':
    unittest.main()
