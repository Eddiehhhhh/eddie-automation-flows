"""Synthetic routing tests: exact cohorts never borrow one another's proof."""
import unittest
from unittest.mock import patch

import get_notion_runtime as runtime


class Seq2RuntimeTests(unittest.TestCase):
    def test_unconfigured_new_anchor_is_fail_closed(self):
        with patch.object(runtime, 'PNG_SEQ2_ASSET_ANCHOR', None):
            with self.assertRaisesRegex(ValueError, 'png_anchor_pending'):
                runtime.png_anchor({'commit': runtime.PNG_SEQ2_PUBLICATION})

    def test_unknown_publication_cannot_borrow_old_anchor(self):
        with self.assertRaisesRegex(ValueError, 'png_anchor_pending'):
            runtime.png_anchor({'commit': '9' * 40})

    def test_legacy_anchor_is_unchanged(self):
        self.assertEqual(runtime.png_anchor({'commit': runtime.PNG_ASSET_ANCHOR['publication_commit']}),
            (runtime.PNG_ASSET_ANCHOR, runtime.PNG_ASSET_ANCHOR_SHA256))

    def test_new_anchor_must_bind_exact_publication(self):
        with patch.object(runtime, 'PNG_SEQ2_ASSET_ANCHOR', {'publication_commit': '9' * 40}), \
                patch.object(runtime, 'PNG_SEQ2_ASSET_ANCHOR_SHA256', 'a' * 64):
            with self.assertRaisesRegex(ValueError, 'png_anchor_pending'):
                runtime.png_anchor({'commit': runtime.PNG_SEQ2_PUBLICATION})

    def test_pin_cannot_cross_source_cohort(self):
        ident = '12345'
        note_sha = runtime.png_upload.sha(ident.encode())
        with patch.object(runtime.png_upload, 'SEQ2_PINS', {note_sha: {'md_sha256': 'b' * 64}}):
            records = {ident: {'sha256': 'b' * 64}}
            self.assertTrue(runtime.png_selected({'commit': runtime.PNG_SEQ2_PUBLICATION, 'records': records}))
            self.assertFalse(runtime.png_selected({'commit': '9' * 40, 'records': records}))
            self.assertFalse(runtime.png_selected({'commit': runtime.PNG_SEQ2_PUBLICATION,
                'records': {ident: {'sha256': 'c' * 64}}}))

    def test_new_queue_uploads_only_one_page_at_a_time(self):
        state = {'rotation_order': ['12345'], 'pending_by_id': {
            '12345': {'commit': runtime.PNG_SEQ2_PUBLICATION}}, 'engine_state': {'journal': None}}
        with patch.object(runtime.expired_recovery, 'only_note_id', return_value=None):
            self.assertEqual(runtime.queue_limit(state), 1)
            state['engine_state']['journal'] = {'immutable': True}
            self.assertEqual(runtime.queue_limit(state), runtime.MAX_RUN_RECORDS)

    def test_legacy_recovery_priority_is_not_overridden(self):
        state = {'rotation_order': ['12345'], 'pending_by_id': {
            '12345': {'commit': runtime.PNG_SEQ2_PUBLICATION}}, 'engine_state': {'journal': None}}
        with patch.object(runtime.expired_recovery, 'only_note_id', return_value='67890'):
            self.assertEqual(runtime.queue_limit(state), runtime.MAX_RUN_RECORDS)


if __name__ == '__main__':
    unittest.main()
