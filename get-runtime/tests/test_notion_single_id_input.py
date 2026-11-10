from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
import types
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# This test suite supplies synthetic bindings instead of personal data.
bindings = types.ModuleType('get_private_runtime_bindings')
bindings.value = lambda module, name: set() if module == 'get_notion_content_scope' else '0' * 64
with patch.dict(sys.modules):
    # Other tests register projection-only stub modules at import time. This
    # CLI test needs the real runtime closure, independent of discovery order.
    for module_name in list(sys.modules):
        if module_name.startswith('get_'):
            del sys.modules[module_name]
    sys.modules['get_private_runtime_bindings'] = bindings
    import get_notion_runtime as runtime


class SingleNoteNotionInput(unittest.TestCase):
    def test_stale_idle_baseline_is_not_accepted(self):
        binding = {
            'contract': runtime.idle_recovery.REVIEWED_BASELINE_TAG,
            'anchor': {'commit': 'c' * 40, 'state_blob': 'b' * 40, 'state_sha256': 'a' * 64},
            'state_bytes': 1, 'old_policy_sha256': 'd' * 64,
            'body_receipts': 1, 'property_receipts': 1, 'unresolved': 1,
            'body_receipts_sha256': 'e' * 64, 'property_receipts_sha256': 'f' * 64,
            'unresolved_sha256': '1' * 64, 'target_material_sha256': '2' * 64,
        }
        modules = {'get_notion_checkpoint_resume': runtime.checkpoint_resume,
                   'get_notion_outbox': runtime.outbox,
                   'get_private_runtime_bindings': runtime.idle_recovery._private_bindings}
        with patch.dict(sys.modules, modules), \
             patch.object(runtime.idle_recovery._private_bindings, 'value', return_value=binding), \
             patch.object(runtime.checkpoint_resume, '_capsule_bytes', return_value=b'{}'), \
             patch.object(runtime.idle_recovery, 'reviewed_material_sha256', return_value='3' * 64):
            self.assertIsNone(runtime.idle_recovery.reviewed_baseline())
            self.assertIsNone(runtime.idle_recovery.reviewed_baseline_anchor(b'old', {}, (None, None)))

    def test_single_note_uses_only_the_reviewed_idle_baseline_anchor(self):
        tag = runtime.idle_recovery.REVIEWED_BASELINE_TAG
        self.assertTrue(runtime.single_note_anchor_allowed('123', tag))
        self.assertTrue(runtime.single_note_anchor_allowed('123', None))
        self.assertFalse(runtime.single_note_anchor_allowed('123', 'another_recovery_anchor'))
        self.assertTrue(runtime.single_note_anchor_allowed(None, 'another_recovery_anchor'))

    def test_valid_cli_id_reaches_runtime_and_bad_id_does_not(self):
        policy = {'workflow_path': '.github/workflows/check.yml', 'head_branch': 'main'}
        env = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GET_SINGLE_NOTE_MODE': 'true',
               'GET_SINGLE_NOTE_ID': '123', 'NOTION_MODE': 'canonical',
               'NOTION_API_KEY': 'synthetic', 'GITHUB_TOKEN': 'synthetic',
               'GITHUB_REPOSITORY': runtime.provenance.REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_WORKFLOW_REF': runtime.provenance.REPOSITORY + '/' + policy['workflow_path'] + '@refs/heads/main'}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, env), \
             patch.object(runtime.recurring, 'executed_code_sha', return_value='a'*40), \
             patch.object(runtime, 'run', return_value={}) as dispatch, redirect_stdout(io.StringIO()):
            root = Path(temp)
            (root/'policy').write_bytes(runtime.a.encoded(policy))
            (root/'publication').write_bytes(b'test-placeholder')
            args = ['--apply', '--scope-from-policy', '--policy', str(root/'policy'),
                    '--publication-file', str(root/'publication')]
            runtime.main(args)
            self.assertEqual(dispatch.call_args.kwargs['single_note_id'], '123')
            dispatch.reset_mock()
            os.environ['GET_SINGLE_NOTE_ID'] = 'invalid'
            self.assertNotEqual(runtime.main(args), 0)
            dispatch.assert_not_called()

    def test_private_proof_cli_uses_controller_selection_and_rejects_bad_binding(self):
        key = 'recording-card-20261108'
        row = {'note_id': '123', 'source_path': 'Raw/03 Get/99 录音卡笔记/sample-123.md',
               'source_blob_sha': 'a' * 40, 'nonce': 'b' * 64}
        policy = {'workflow_path': '.github/workflows/check.yml', 'head_branch': 'main'}
        env = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GET_SINGLE_NOTE_MODE': 'true',
               'GET_SINGLE_NOTE_ID': '123', 'NOTION_MODE': 'private-single-note-proof',
               'NOTION_INPUT_SHA256': key, 'APPLY': 'true', 'NOTION_API_KEY': 'synthetic',
               'GITHUB_TOKEN': 'synthetic', 'GITHUB_REPOSITORY': runtime.provenance.REPOSITORY,
               'GITHUB_REF': 'refs/heads/main',
               'GITHUB_WORKFLOW_REF': runtime.provenance.REPOSITORY + '/.github/workflows/check.yml@refs/heads/main'}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, env), \
             patch.object(runtime.recurring, 'executed_code_sha', return_value='a' * 40), \
             patch('get_private_single_note_proof.bindings.value', return_value={key: row}), \
             patch.object(runtime, 'run', return_value={}) as dispatch, redirect_stdout(io.StringIO()):
            root = Path(temp)
            (root / 'policy').write_bytes(runtime.a.encoded(policy))
            (root / 'publication').write_bytes(b'test-placeholder')
            args = ['--apply', '--scope-from-policy', '--policy', str(root / 'policy'),
                    '--publication-file', str(root / 'publication')]
            runtime.main(args)
            self.assertEqual(dispatch.call_args.kwargs['single_note_id'], '123')
            dispatch.reset_mock()
            os.environ['NOTION_INPUT_SHA256'] = 'unregistered-proof-key'
            self.assertNotEqual(runtime.main(args), 0)
            dispatch.assert_not_called()
            os.environ['NOTION_INPUT_SHA256'] = key
            os.environ['GET_SINGLE_NOTE_ID'] = '456'
            self.assertNotEqual(runtime.main(args), 0)
            dispatch.assert_not_called()
    def test_runtime_validates_id_before_authority_or_transport(self):
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}), \
             patch.object(runtime.fixed_auth, 'admit', side_effect=ValueError('notion_context')) as guard:
            valid = runtime.run(None, None, None, apply=True, single_note_id='123')
            self.assertEqual(valid['reason'], 'notion_context')
            guard.assert_called_once()
            guard.reset_mock()
            invalid = runtime.run(None, None, None, apply=True, single_note_id='invalid')
            self.assertEqual(invalid['stage'], 'input')
            guard.assert_not_called()
