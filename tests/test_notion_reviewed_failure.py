"""Synthetic retry eligibility for exact new content reviews, not broad retries."""
import unittest
from unittest.mock import patch
import get_notion_content_scope as scope


class ReviewedFailureTests(unittest.TestCase):
    def setUp(self):
        self.ident = '12345'
        self.source = {'blob': 'a' * 40, 'sha256': 'b' * 64}
        self.failed = {'md_blob': 'a' * 40, 'reason': 'content_review_required'}
        p = patch.object(scope, 'NEWLY_REVIEWED', {scope.p.digest(self.ident): 'b' * 64})
        p.start()
        self.addCleanup(p.stop)

    def test_exact_previously_review_blocked_version_can_be_reconsidered(self):
        self.assertTrue(scope.retry_reviewed_failure(self.ident, self.source, self.failed))

    def test_other_failures_stay_blocked(self):
        for reason in ('privacy_scope_required', 'empty_source', 'prior_denial',
                       'missing_block_ownership', 'unresolved_operation', 'missing_asset'):
            with self.subTest(reason=reason):
                self.assertFalse(scope.retry_reviewed_failure(self.ident, self.source,
                    {**self.failed, 'reason': reason}))

    def test_changed_or_unreviewed_sources_stay_blocked(self):
        self.assertFalse(scope.retry_reviewed_failure('99999', self.source, self.failed))
        self.assertFalse(scope.retry_reviewed_failure(self.ident,
            {**self.source, 'sha256': 'c' * 64}, self.failed))
        self.assertFalse(scope.retry_reviewed_failure(self.ident,
            {**self.source, 'blob': 'c' * 40}, self.failed))

    def test_missing_source_fields_do_not_match(self):
        self.assertFalse(scope.retry_reviewed_failure(self.ident, {}, {'reason': 'content_review_required'}))


if __name__ == '__main__':
    unittest.main()
