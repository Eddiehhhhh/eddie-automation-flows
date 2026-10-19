"""Synthetic exact-version projection tests; no private source or credentials."""
import hashlib
import unittest
from unittest.mock import patch

import get_notion_credential_projection as projection


def sha(value):
    return hashlib.sha256(value).hexdigest()


class CredentialProjectionTests(unittest.TestCase):
    def setUp(self):
        self.ident = '12345'
        self.body = 'price 20; time 12:30; synthetic 000111; end'
        self.source = b'fixture source, never exported'
        start = self.body.index('000111')
        self.spans = ((start, start + 6),)
        self.expected = self.body[:start] + projection.REPLACEMENT + self.body[start + 6:]
        self.patches = patch.multiple(projection, NOTE_SHA256=sha(self.ident.encode()),
            SOURCE_SHA256=sha(self.source), BODY_SHA256=sha(self.body.encode()),
            PROJECTED_BODY_SHA256=sha(self.expected.encode()), SPANS=self.spans)
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def test_exact_version_only_changes_explicit_span(self):
        self.assertEqual(projection.render_body(self.source, self.ident, self.body), self.expected)
        self.assertIn('price 20; time 12:30;', self.expected)
        self.assertEqual(self.source, b'fixture source, never exported')

    def test_unrelated_note_is_unchanged(self):
        self.assertIs(projection.render_body(self.source, '99999', self.body), self.body)

    def test_changed_original_source_is_blocked(self):
        with self.assertRaisesRegex(ValueError, 'content_review_required'):
            projection.render_body(self.source + b'x', self.ident, self.body)

    def test_changed_body_is_blocked(self):
        with self.assertRaisesRegex(ValueError, 'content_review_required'):
            projection.render_body(self.source, self.ident, self.body + 'x')

    def test_incorrect_projection_digest_is_blocked(self):
        with patch.object(projection, 'PROJECTED_BODY_SHA256', '0' * 64):
            with self.assertRaisesRegex(ValueError, 'content_review_required'):
                projection.render_body(self.source, self.ident, self.body)

    def test_overlapping_or_out_of_range_spans_are_blocked(self):
        for spans in ((self.spans[0], self.spans[0]), ((0, 999),)):
            with self.subTest(spans=spans), patch.object(projection, 'SPANS', spans):
                with self.assertRaisesRegex(ValueError, 'content_review_required'):
                    projection.render_body(self.source, self.ident, self.body)

    def test_non_numeric_span_is_blocked(self):
        with patch.object(projection, 'SPANS', ((0, 5),)):
            with self.assertRaisesRegex(ValueError, 'content_review_required'):
                projection.render_body(self.source, self.ident, self.body)


if __name__ == '__main__':
    unittest.main()
