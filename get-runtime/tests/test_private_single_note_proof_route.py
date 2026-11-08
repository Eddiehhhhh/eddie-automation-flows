import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, 'get-runtime/scripts')
import get_notion_route_selection as selection
import get_notion_controller_route as controller


class PrivateProofRouteTest(unittest.TestCase):
    KEY = 'recording-card-20261108'

    def test_only_dispatch_apply_with_registered_key_is_accepted(self):
        self.assertTrue(selection.preflight('workflow_dispatch', True, selection.PROOF_MODE, self.KEY))
        for event, apply, key in (('schedule', True, self.KEY), ('workflow_dispatch', False, self.KEY),
                                  ('workflow_dispatch', True, 'unregistered-proof-key')):
            with self.subTest(event=event, apply=apply, key=key), self.assertRaises(selection.SelectionError):
                selection.preflight(event, apply, selection.PROOF_MODE, key)

    def test_private_route_resolves_mapping_after_private_bindings_are_loaded(self):
        row = {'note_id':'1234567890','source_path':'Raw/03 Get/99 录音卡笔记/sample-1234567890.md',
               'source_blob_sha':'a'*40,'nonce':'b'*64}
        with patch.dict(os.environ, {'APPLY':'true','GITHUB_EVENT_NAME':'workflow_dispatch',
              'NOTION_MODE':selection.PROOF_MODE,'NOTION_INPUT_SHA256':self.KEY}), \
             patch('get_private_single_note_proof.bindings.value', return_value={self.KEY:row}):
            self.assertEqual(controller.selection(), (selection.PROOF_MODE, self.KEY, 'get-private-single-note-proof-1'))


if __name__ == '__main__':
    unittest.main()
