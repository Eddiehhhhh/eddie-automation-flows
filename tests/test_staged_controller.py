"""Manual acceptance then minute-17 activation under one immutable A/policy."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import get_recurring_controller as c
import get_publication_provenance as p
import get_recurring_runtime as runtime
import get_notion_runtime as notion
import get_notion_journal_runtime as journal
from get_recurring_entry import a, r
import test_publication_provenance as fixture
import test_notion_journal_runtime as journal_fixture


class StagedControllerTests(unittest.TestCase):
    def setUp(self):
        self.policy = fixture.policy()
        self.digest = a.sha(a.encoded(self.policy))
        self.scope = c.scope_for(self.policy, self.digest)
        self.scope_sha = a.sha(a.encoded(self.scope))

    def response(self, raw):
        return {'type': 'file', 'path': self.policy['workflow_path'], 'encoding': 'base64',
                'size': len(raw), 'sha': r.oid(raw), 'content': base64.b64encode(raw).decode()}

    def set_variant(self, api, sha, scheduled):
        api.overrides['/contents/' + self.policy['workflow_path'] + '?ref=' + sha] = self.response(
            c.expected_bytes(self.policy, scheduled=scheduled))

    def test_two_variants_differ_only_by_fixed_cron_block_and_keep_policy_scope(self):
        policy = deepcopy(self.policy); template_hash = c.template_sha256()
        activation = (runtime.PRODUCTION_ACTIVATED, notion.PRODUCTION_ACTIVATED)
        full = c.render(policy, self.digest, self.scope_sha, scheduled=True)
        manual = c.render(policy, self.digest, self.scope_sha, scheduled=False)
        self.assertEqual(full.replace(c.SCHEDULE_BLOCK.encode(), b'', 1), manual)
        self.assertEqual(full.count(c.SCHEDULE_BLOCK.encode()), 1)
        self.assertNotIn(b'  schedule:\n', manual)
        self.assertIn(b'  workflow_dispatch:', manual)
        for raw in (full, manual):
            self.assertIn(self.digest.encode(), raw); self.assertIn(self.scope_sha.encode(), raw)
            self.assertIn(("ref: '" + policy['code_sha'] + "'").encode(), raw)
            self.assertIn(b'continue-on-error: true', raw)
            self.assertIn(b'contents: read', raw)
        self.assertEqual(policy, self.policy); self.assertEqual(c.template_sha256(), template_hash)
        self.assertEqual(c.scope_for(policy, self.digest), self.scope)
        self.assertEqual((runtime.PRODUCTION_ACTIVATED, notion.PRODUCTION_ACTIVATED), activation)

    def test_renderer_cli_defaults_manual_then_explicit_flag_renders_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); policy_path = root / 'policy.json'; policy_path.write_bytes(a.encoded(self.policy))
            args = ['--policy', str(policy_path), '--approved-policy-sha256', self.digest,
                    '--approved-scope-sha256', self.scope_sha]
            for flag, scheduled in ([], False), (['--enable-schedule'], True):
                target = root / ('scheduled.yml.disabled' if scheduled else 'manual.yml.disabled')
                with patch('builtins.print') as output:
                    self.assertEqual(c.main(args + ['--output', str(target)] + flag), 0)
                self.assertEqual(target.read_bytes(), c.expected_bytes(self.policy, scheduled=scheduled))
                report = json.loads(output.call_args.args[0])
                self.assertEqual(report['schedule_enabled'], scheduled); self.assertFalse(report['published'])
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_variant_selector_is_boolean_and_template_requires_exact_unique_cron(self):
        for bad in (1, None, 'false', {}):
            with self.assertRaises(p.ProvenanceFailure):
                c.render(self.policy, self.digest, self.scope_sha, scheduled=bad)
        template = c.template_bytes()
        for bad in (template.replace(c.SCHEDULE_BLOCK.encode(), b''), template + c.SCHEDULE_BLOCK.encode()):
            with patch.object(c, 'template_bytes', return_value=bad):
                policy = fixture.policy(); digest = a.sha(a.encoded(policy))
                with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_template'):
                    c.render(policy, digest, a.sha(a.encoded(c.scope_for(policy, digest))), scheduled=False)

    def test_dispatch_accepts_exact_two_versions_schedule_accepts_only_enabled(self):
        for scheduled in (False, True):
            api = fixture.FakeAPI(); self.set_variant(api, 'c' * 40, scheduled)
            self.assertEqual(c.verify_remote(api, 'c' * 40, self.policy, event='workflow_dispatch'), 'c' * 40)
            if scheduled:
                self.assertEqual(c.verify_remote(api, 'c' * 40, self.policy, event='schedule'), 'c' * 40)
            else:
                with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_mismatch'):
                    c.verify_remote(api, 'c' * 40, self.policy, event='schedule')
        api = fixture.FakeAPI()
        with self.assertRaises(p.ProvenanceFailure): c.verify_remote(api, 'c' * 40, self.policy, event='pull_request')
        self.assertEqual(api.called, [])

    def test_no_arbitrary_cron_script_permissions_or_registration_stub_is_accepted(self):
        full = c.expected_bytes(self.policy); manual = c.expected_bytes(self.policy, scheduled=False)
        bads = [full.replace(b'17 * * * *', b'18 * * * *'),
                manual + b'\n# unexpected addition\n',
                manual.replace(b'contents: read', b'contents: write'),
                manual.replace(b'python scripts/get_recurring_runtime.py', b'python foreign.py'),
                b'on: workflow_dispatch\npermissions: {}\njobs:\n  register:\n    if: false\n']
        for raw in bads:
            for event in ('workflow_dispatch', 'schedule'):
                api = fixture.FakeAPI()
                api.overrides['/contents/' + self.policy['workflow_path'] + '?ref=' + 'c' * 40] = self.response(raw)
                with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_mismatch'):
                    c.verify_remote(api, 'c' * 40, self.policy, event=event)

    def test_current_schedule_rejects_manual_controller_before_private_io(self):
        execution = {'run_id': 1002, 'run_attempt': 1, 'code_sha': 'a' * 40, 'controller_sha': 'c' * 40}
        api = fixture.FakeAPI(); self.set_variant(api, 'c' * 40, False)
        row = fixture.run(1002, head_sha='c' * 40, status='in_progress', conclusion=None)
        api.overrides['/actions/runs/1002/attempts/1'] = row
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_mismatch'):
            p.verify_execution(api, self.policy, execution, 'schedule')
        row['event'] = 'workflow_dispatch'
        p.verify_execution(api, self.policy, execution, 'manual')

    def test_first_scheduled_execution_recovers_manual_acceptance_same_policy(self):
        prior = fixture.run(1001, head_sha='c' * 40, event='workflow_dispatch')
        current = fixture.run(1002, head_sha='d' * 40, status='in_progress', conclusion=None, minute=9)
        api = fixture.FakeAPI([prior, current]); self.set_variant(api, 'c' * 40, False); self.set_variant(api, 'd' * 40, True)
        p.verify_execution(api, self.policy, {'run_id': 1002, 'run_attempt': 1, 'code_sha': 'a' * 40, 'controller_sha': 'd' * 40}, 'schedule')
        result = p.discover(api, self.policy, exclude_run_id=1002, exclude_run_attempt=1)
        self.assertEqual(result['run_id'], 1001); self.assertEqual(result['anchor']['commit'], 'd' * 40)
        self.assertEqual(result['publication_records_verified'], 1)
        self.assertEqual(a.sha(a.encoded(self.policy)), self.digest)

    def test_manual_cache_cannot_authorize_same_sha_claimed_as_schedule(self):
        manual = fixture.run(1002, head_sha='c' * 40, event='workflow_dispatch', minute=9)
        forged_schedule = fixture.run(1001, head_sha='c' * 40, minute=5)
        api = fixture.FakeAPI([manual, forged_schedule]); self.set_variant(api, 'c' * 40, False)
        result = p.discover(api, self.policy)
        self.assertEqual(result['run_id'], 1002); self.assertEqual(result['publication_records_verified'], 1)
        self.assertNotIn('logs:1501', api.called)
        self.assertEqual(len([path for path in api.called if path.startswith('/contents/')]), 2)

    def test_notion_manual_checkpoint_survives_switch_but_schedule_cannot_borrow_it(self):
        stream = io.StringIO()
        journal.CheckpointEmitter(stream, context=journal_fixture.context(self.policy))(journal_fixture.receipt())
        api = journal_fixture.LogsAPI(self.policy, stream.getvalue())
        api.row['event'] = 'workflow_dispatch'; self.set_variant(api, 'c' * 40, False)
        result = journal.recover_anchor(api, run_id=1001, run_attempt=1, public_policy=self.policy,
            approved_public_policy_sha256=self.digest, notion_policy_sha256='d' * 64)
        self.assertEqual(result['anchor']['commit'], journal_fixture.receipt()['commit'])
        self.assertFalse(result['get_run_success_claim'])
        api.row['event'] = 'schedule'
        with self.assertRaises(journal.CheckpointFailure):
            journal.recover_anchor(api, run_id=1001, run_attempt=1, public_policy=self.policy,
                approved_public_policy_sha256=self.digest, notion_policy_sha256='d' * 64)

    def test_real_git_manual_receipt_remains_valid_for_scheduled_noop(self):
        f = fixture.RealGitIntegrationTests(methodName='test_verified_public_record_drives_next_entry_without_manual_receipt_hash')
        f.setUp(); self.addCleanup(f.doCleanups)
        plan, files = f.f.prepare(); record, policy, api = f.data(plan, files)
        self.policy = policy
        api.runs[0]['event'] = 'workflow_dispatch'
        self.set_variant(api, 'a' * 40, False)
        source = f.f.f.source()
        again, changes = p.prepare_with_public_provenance(f.f.reader, f.f.evidence, plan['candidate_commit'], source,
            api, policy, a.sha(a.encoded(policy)), bootstrap=f.f.bootstrap, trigger='schedule',
            exclude_run_id=1002, exclude_run_attempt=1)
        self.assertEqual(changes, {}); self.assertEqual(again['receipt_commit'], plan['candidate_commit'])
        self.assertEqual(again['policy_sha256'], plan['policy_sha256']); self.assertEqual(source.ids, [])


if __name__ == '__main__': unittest.main()
