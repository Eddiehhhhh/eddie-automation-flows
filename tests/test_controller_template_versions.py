"""Historical policies render the exact archived bytes, never a new template."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import get_recurring_controller as c
import get_recurring_controller_legacy as legacy
import get_recurring_policy_recovery as recovery
import get_publication_provenance as p
from get_recurring_entry import a, r
import test_publication_provenance as fixture


class TemplateVersionTests(unittest.TestCase):
    def test_archived_data_is_exact_immutable_deployed_template(self):
        self.assertEqual(legacy.SOURCE_RUNTIME_SHA, '6ad627793c23e80ad2ca9568048eb4e595c881a0')
        self.assertEqual(legacy.TEMPLATE_SHA256,
                         '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2')
        self.assertEqual(hashlib.sha256(legacy.TEMPLATE_BYTES).hexdigest(), legacy.TEMPLATE_SHA256)
        self.assertNotEqual(c.template_sha256(), legacy.TEMPLATE_SHA256)
        self.assertNotIn(b'Install verified metadata parser', legacy.TEMPLATE_BYTES)
        self.assertIn(b'Install verified metadata parser', c.template_bytes())

    def test_selector_accepts_only_exact_current_or_archived_policy_template(self):
        current = fixture.policy()
        historical = {**current, 'controller_template_sha256': legacy.TEMPLATE_SHA256}
        self.assertEqual(c.policy_template_bytes(current), c.template_bytes())
        self.assertEqual(c.policy_template_bytes(historical), legacy.TEMPLATE_BYTES)
        for value in (None, {}, {**current, 'controller_template_sha256': 'f' * 64}):
            with self.subTest(value=value), self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_policy'):
                c.policy_template_bytes(value)
        for policy in (current, historical):
            self.assertEqual(p.validate_policy(policy, a.sha(a.encoded(policy))), policy)

    def test_hash_checked_archive_cannot_be_modified_or_substituted(self):
        policy = fixture.policy(controller_template_sha256=legacy.TEMPLATE_SHA256)
        for raw in (legacy.TEMPLATE_BYTES + b'\n', c.template_bytes(), bytearray(legacy.TEMPLATE_BYTES)):
            with patch.object(legacy, 'TEMPLATE_BYTES', raw):
                with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_template'):
                    c.expected_bytes(policy)

    def test_legacy_render_matches_recorded_remote_controller_bytes(self):
        # Independently captured Contents API response, not a new rendering.
        evidence = json.loads((ROOT / 'tests/fixtures/canonical-run26-public-proof.json').read_bytes())
        policy = recovery.sequence_two_policy()
        sha = recovery.SEQUENCE_TWO_RECORD['controller_sha']
        saved = evidence['controllers'][sha]
        raw = base64.b64decode(saved['content'].replace('\n', ''), validate=True)
        self.assertEqual(saved['size'], len(raw))
        self.assertEqual(saved['sha'], r.oid(raw))
        self.assertEqual(c.expected_bytes(policy), raw)
        self.assertNotIn(b'Install verified metadata parser', raw)

    def test_legacy_and_current_manual_variants_remove_only_fixed_schedule(self):
        for digest in (c.template_sha256(), legacy.TEMPLATE_SHA256):
            policy = fixture.policy(controller_template_sha256=digest)
            full = c.expected_bytes(policy)
            manual = c.expected_bytes(policy, scheduled=False)
            self.assertEqual(full.replace(c.SCHEDULE_BLOCK.encode(), b'', 1), manual)
            self.assertEqual(full.count(c.SCHEDULE_BLOCK.encode()), 1)
            self.assertNotIn(c.SCHEDULE_BLOCK.encode(), manual)

    def test_historical_policy_is_not_rewritten_or_rendered_from_current_bytes(self):
        policy = recovery.sequence_two_policy()
        before = deepcopy(policy)
        old = c.expected_bytes(policy)
        # Even a policy differing only by the approved template digest must
        # have its own exact substitution bytes and public policy hash.
        current = {**policy, 'controller_template_sha256': c.template_sha256()}
        new = c.expected_bytes(current)
        self.assertEqual(policy, before)
        self.assertNotEqual(old, new)
        self.assertIn(b'Install verified metadata parser', new)
        api = fixture.FakeAPI()
        api.policy = current
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_mismatch'):
            c.verify_remote(api, 'c' * 40, policy)

    def test_legacy_support_does_not_relax_external_policy_hash_or_step_names(self):
        policy = fixture.policy(controller_template_sha256=legacy.TEMPLATE_SHA256)
        for changes in ({'job_name': 'other'}, {'publication_step_name': 'other'}):
            bad = {**policy, **changes}
            with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_policy'):
                p.validate_policy(bad, a.sha(a.encoded(bad)))
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_policy'):
            p.validate_policy(policy, 'f' * 64)


if __name__ == '__main__':
    unittest.main()
