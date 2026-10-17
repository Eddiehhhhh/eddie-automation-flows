"""Exact first-publication repair, strict prepare exclusion and retained MD queue."""
from contextlib import ExitStack
from copy import deepcopy
import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_recurring_policy_recovery as recovery
import get_publication_provenance as p
from get_recurring_entry import a
import test_publication_provenance as public
import test_recurring_entry as real_git
import test_confirmed_source as source_fixture


def prepare_report(policy):
    value = {k: 0 for k in p.entry.engine.COUNTS}
    value.update({k: 'a' * width for k, width in p.entry.engine.HASHES})
    value.update(status='recurring_candidate_verified', main_updated=False, cas_attempts=0,
        local_proposal_only=True, apply_ready=False, schedule_enabled=False,
        receipt_commit=None, receipt_sha256=None, policy_sha256=policy['get_policy_sha256'],
        asset_downloads=0, notion_writes=0, confirmed_content_ids=0,
        opaque_source_fields_rendered=False, complete_source_rendering_claimed=False,
        readback_verified=True, production_activated=True, sink_status='not_requested')
    return value


def prepare_api(value=None):
    rows = [public.run(1002, minute=9), public.run(1001, event='workflow_dispatch')]
    api = public.FakeAPI(rows)
    path = '/actions/runs/1001/attempts/1/jobs?per_page=100&page=1'
    jobs = api.json(path); job = jobs['jobs'][0]
    job['started_at'] = '2026-10-16T10:00:01Z'
    job['steps'][0].update(started_at='2026-10-16T10:01:00Z', completed_at='2026-10-16T10:04:59Z')
    job['steps'].append({'name': 'Sync confirmed Get content to Notion', 'number': 6,
                         'status': 'completed', 'conclusion': 'skipped'})
    api.overrides[path] = jobs
    api.log_overrides[1501] = b'2026-10-16T10:04:58Z ' + a.encoded(value or prepare_report(public.policy()))
    api.called.clear()
    return api


class PrepareOnlyTests(unittest.TestCase):
    def test_verified_publication_survives_older_strict_manual_prepare(self):
        api = prepare_api(); value = p.discover(api, public.policy())
        self.assertEqual(value['run_id'], 1002)
        self.assertEqual(value['publication_records_verified'], 1)
        self.assertEqual(value['candidate_runs_checked'], 2)
        self.assertEqual(api.called.count('/actions/runs/1001/attempts/1'), 2)
        self.assertEqual(api.called[-1], '/actions/runs/1001')

    def test_prepare_alone_never_supplies_an_anchor(self):
        api = prepare_api(); api.runs = api.runs[1:]
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_no_verified_publication'):
            p.discover(api, public.policy())

    def test_missing_actual_apply_marker_and_ambiguous_reports_fail(self):
        safe = prepare_report(public.policy())
        cases = [{**safe, 'status': 'recurring_transaction_verified'},
                 {**safe, 'cas_attempts': 1}, {**safe, 'cas_attempts': False},
                 {**safe, 'main_updated': True}, {**safe, 'readback_verified': False},
                 {**safe, 'notion_writes': 1}, {**safe, 'sink_status': 'partial'},
                 {**safe, 'policy_sha256': 'f' * 64}, {**safe, 'extra': 'unsafe'},
                 {**safe, 'new_selected': 51}, {**safe, 'notes_changed': 1},
                 {**safe, 'receipt_commit': 'f' * 40}]
        for report in cases:
            with self.subTest(report=report):
                with self.assertRaises(a.ParityFailure): p.discover(prepare_api(report), public.policy())
        for change in ('duplicate', 'marker', 'malformed', 'untimed', 'outside'):
            api = prepare_api(); raw = api.log_overrides[1501]
            api.log_overrides[1501] = {'duplicate': raw + raw,
                'marker': raw + p.MARKER.encode() + b'{}\n',
                'malformed': raw + b'2026-10-16T10:04:57Z {broken\n',
                'untimed': raw.split(b' ', 1)[1],
                'outside': raw.replace(b'10:04:58Z', b'10:00:00Z')}[change]
            with self.subTest(change=change):
                with self.assertRaises(a.ParityFailure): p.discover(api, public.policy())

    def test_schedule_or_running_notion_or_missing_step_times_fail(self):
        for mode in ('schedule', 'notion_success', 'notion_missing', 'time_missing', 'time_order'):
            api = prepare_api(); job = api.overrides['/actions/runs/1001/attempts/1/jobs?per_page=100&page=1']['jobs'][0]
            if mode == 'schedule': api.runs[1]['event'] = 'schedule'
            if mode == 'notion_success': job['steps'][1]['conclusion'] = 'success'
            if mode == 'notion_missing': job['steps'].pop()
            if mode == 'time_missing': job['steps'][0].pop('started_at')
            if mode == 'time_order': job['steps'][0]['started_at'] = '2026-10-16T10:05:00Z'
            with self.subTest(mode=mode):
                with self.assertRaises(a.ParityFailure): p.discover(api, public.policy())

    def test_prepare_rejection_has_safe_fixed_runtime_diagnostic(self):
        import get_recurring_runtime as runtime
        error = p.ProvenanceFailure('provenance_prepare_only')
        error.runtime_stage = 'receipt_provenance'
        value = runtime.failure(error)
        self.assertEqual(value['error'], 'provenance_prepare_only')
        self.assertEqual(value['stage'], 'receipt_provenance')

    def test_actual_prepare_report_in_declared_whole_second_end_is_accepted(self):
        api = prepare_api(); row = api.runs[1]
        row.update(created_at='2026-10-17T08:20:00Z', updated_at='2026-10-17T08:28:18Z')
        job = api.overrides['/actions/runs/1001/attempts/1/jobs?per_page=100&page=1']['jobs'][0]
        job.update(started_at='2026-10-17T08:20:01Z', completed_at='2026-10-17T08:28:17Z')
        step = job['steps'][0]
        step.update(started_at='2026-10-17T08:20:02Z', completed_at='2026-10-17T08:28:16Z')
        raw = b'2026-10-17T08:28:16.6535175Z ' + a.encoded(prepare_report(public.policy()))
        self.assertTrue(p.verified_prepare_only(raw, public.policy(), row, job, step))
        # The end of the declared second is excluded, not rounded back in.
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_prepare_only'):
            p.verified_prepare_only(raw.replace(b'16.6535175Z', b'17Z'), public.policy(), row, job, step)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_prepare_only'):
            p.verified_prepare_only(raw.replace(b'16.6535175Z', b'17.0000001Z'), public.policy(), row, job, step)

    def test_fractional_end_remains_exact_and_start_bound_is_not_relaxed(self):
        api = prepare_api(); row = api.runs[1]
        job = api.overrides['/actions/runs/1001/attempts/1/jobs?per_page=100&page=1']['jobs'][0]
        step = job['steps'][0]; step['completed_at'] = '2026-10-16T10:04:59.500000Z'
        body = a.encoded(prepare_report(public.policy()))
        exact = b'2026-10-16T10:04:59.500000Z ' + body
        self.assertTrue(p.verified_prepare_only(exact, public.policy(), row, job, step))
        for stamp in (b'2026-10-16T10:04:59.5000001Z', b'2026-10-16T10:04:59.500001Z',
                      b'2026-10-16T10:00:59.999999Z'):
            with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_prepare_only'):
                p.verified_prepare_only(stamp + b' ' + body, public.policy(), row, job, step)

    def test_coarse_step_end_cannot_exceed_precise_job_or_run_end(self):
        for target in ('job', 'run'):
            api = prepare_api(); row = api.runs[1]
            job = api.overrides['/actions/runs/1001/attempts/1/jobs?per_page=100&page=1']['jobs'][0]
            step = job['steps'][0]
            job['completed_at'] = '2026-10-16T10:04:59Z'
            (job if target == 'job' else row)['completed_at' if target == 'job' else 'updated_at'] = '2026-10-16T10:04:59.500000Z'
            raw = b'2026-10-16T10:04:59.5000001Z ' + a.encoded(prepare_report(public.policy()))
            with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_prepare_only'):
                p.verified_prepare_only(raw, public.policy(), row, job, step)

    def test_original_attempt_mutation_is_checked_even_for_prepare(self):
        api = prepare_api(); path = '/actions/runs/1001/attempts/1'; calls = []
        def read():
            calls.append(1)
            return {**api.runs[1], **({'conclusion': 'failure'} if len(calls) > 1 else {})}
        api.overrides[path] = read
        with self.assertRaises(p.ProvenanceFailure): p.discover(api, public.policy())
        self.assertEqual(len(calls), 2)


class RecoveryAPI(public.FakeAPI):
    def __init__(self, current, **changes):
        old = recovery.old_public_policy()
        repo = {'id': old['repository_id'], 'full_name': old['repository'], 'private': False}
        row = public.run(recovery.ORIGINAL_RUN_ID, head_sha=recovery.ORIGINAL_CONTROLLER_SHA,
            event='workflow_dispatch', workflow_id=old['workflow_id'], repository=repo, head_repository=repo)
        row.update(changes)
        record = public.record(row, code_sha=old['code_sha'], rules_sha256=old['rules_sha256'],
            policy_sha256=old['get_policy_sha256'], observed_main_commit=recovery.ORIGINAL_RECEIPT_COMMIT,
            receipt_commit=recovery.ORIGINAL_RECEIPT_COMMIT, receipt_sha256=recovery.ORIGINAL_RECEIPT_SHA256)
        super().__init__([row], {row['id']: record}); self.policy = current; self.old = old

    def json(self, path):
        if path == '': return {'id': self.old['repository_id'], 'full_name': p.REPOSITORY, 'private': False}
        if path == '/actions/workflows/' + str(self.old['workflow_id']):
            return {'id': self.old['workflow_id'], 'path': self.old['workflow_path']}
        if path.startswith('/contents/'):
            self.called.append(path)
            raw = p.controller.expected_bytes(self.old, scheduled=False)
            return {'type': 'file', 'path': self.old['workflow_path'], 'encoding': 'base64',
                'size': len(raw), 'sha': p.entry.r.oid(raw), 'content': base64.b64encode(raw).decode()}
        path = path.replace('/actions/workflows/' + str(self.old['workflow_id']) + '/', '/actions/workflows/21/')
        value = super().json(path)
        if '/jobs?' in path and path not in self.overrides:
            row = value['jobs'][0]; old_id = row['id']; row['id'] = recovery.ORIGINAL_JOB_ID
            self.job_attempts[row['id']] = self.job_attempts[old_id]
        return value



class NoopRecoveryAPI(RecoveryAPI):
    def __init__(self, current, **changes):
        super().__init__(current)
        self.old = recovery.noop_public_policy()
        row = {**self.runs[0], 'id': recovery.NOOP_RUN_ID,
               'head_sha': recovery.NOOP_CONTROLLER_SHA, **changes}
        self.runs = [row]
        self.records = {row['id']: public.record(row, code_sha=self.old['code_sha'],
            rules_sha256=self.old['rules_sha256'], policy_sha256=self.old['get_policy_sha256'],
            observed_main_commit='df1bf90967ce4a707ec8fe781ccc41e254fb1006',
            receipt_commit=recovery.ORIGINAL_RECEIPT_COMMIT,
            receipt_sha256=recovery.ORIGINAL_RECEIPT_SHA256,
            main_updated=False, cas_attempts=0)}

    def json(self, path):
        value = super().json(path)
        if '/jobs?' in path and path not in self.overrides:
            row = value['jobs'][0]; old_id = row['id']; row['id'] = recovery.NOOP_JOB_ID
            self.job_attempts[row['id']] = self.job_attempts[old_id]
        return value


class ConfirmedNoopBridgeTests(unittest.TestCase):
    def test_fixed_noop_policy_and_record_hash_match_external_evidence(self):
        old = recovery.noop_public_policy()
        self.assertEqual(a.sha(a.encoded(old)), recovery.NOOP_PUBLIC_POLICY_SHA256)
        old['code_sha'] = 'b' * 40
        self.assertNotEqual(old, recovery.noop_public_policy())
        api = NoopRecoveryAPI(current_policy())
        self.assertEqual(api.records[recovery.NOOP_RUN_ID]['record_sha256'], recovery.NOOP_RECORD_SHA256)

    def test_noop_is_discoverable_when_original_run_is_out_of_window(self):
        policy = current_policy(); api = NoopRecoveryAPI(policy)
        value = p.discover(api, policy)
        self.assertEqual(value['run_id'], recovery.NOOP_RUN_ID)
        self.assertEqual(value['job_id'], recovery.NOOP_JOB_ID)
        self.assertEqual(value['record_sha256'], recovery.NOOP_RECORD_SHA256)
        self.assertEqual(value['anchor'], {'commit': recovery.ORIGINAL_RECEIPT_COMMIT,
                                          'receipt_sha256': recovery.ORIGINAL_RECEIPT_SHA256})
        self.assertEqual(value['receipt_sequence'], 1)
        self.assertEqual(value['candidate_runs_checked'], 1)
        self.assertEqual(api.called.count('/actions/runs/' + str(recovery.NOOP_RUN_ID) + '/attempts/1'), 2)
        self.assertFalse(any(str(recovery.ORIGINAL_RUN_ID) in path for path in api.called))
        self.assertEqual((p.MAX_REQUESTS, p.MAX_CANDIDATES, p.MAX_RUN_PAGES), (32, 4, 3))

    def test_other_run_attempt_controller_and_current_policy_are_rejected(self):
        for changes in ({'id': recovery.NOOP_RUN_ID + 1}, {'run_attempt': 2},
                        {'head_sha': 'b' * 40}, {'event': 'schedule'}):
            policy = current_policy(); api = NoopRecoveryAPI(policy, **changes)
            if changes.get('run_attempt') == 2:
                api.attempts[(recovery.NOOP_RUN_ID, 1)] = {**api.runs[0], 'run_attempt': 1, 'conclusion': 'failure'}
            with self.subTest(changes=changes):
                with self.assertRaises(p.ProvenanceFailure): p.discover(api, policy)
        for key in ('rules_sha256', 'get_policy_sha256'):
            policy = current_policy(); policy[key] = 'e' * 64
            with self.assertRaises(p.ProvenanceFailure): p.discover(NoopRecoveryAPI(policy), policy)

    def test_exact_job_record_noop_and_original_receipt_are_mandatory(self):
        policy = current_policy()
        for mode in ('job', 'observed', 'updated', 'receipt', 'sequence'):
            api = NoopRecoveryAPI(policy)
            if mode == 'job':
                path = '/actions/runs/' + str(recovery.NOOP_RUN_ID) + '/attempts/1/jobs?per_page=100&page=1'
                jobs = api.json(path); jobs['jobs'][0]['id'] += 1
                api.overrides[path] = jobs; api.job_attempts[jobs['jobs'][0]['id']] = (recovery.NOOP_RUN_ID, 1)
            else:
                row = api.runs[0]
                changes = {'observed': {'observed_main_commit': 'a' * 40},
                    'updated': {'main_updated': True, 'cas_attempts': 1, 'observed_main_commit': recovery.ORIGINAL_RECEIPT_COMMIT},
                    'receipt': {'receipt_sha256': 'a' * 64}, 'sequence': {'receipt_sequence': 2}}[mode]
                api.records[row['id']] = public.record(row, **{**api.records[row['id']], **changes})
            with self.subTest(mode=mode):
                with self.assertRaises(p.ProvenanceFailure): p.discover(api, policy)

    def test_denied_controller_read_fails_without_policy_or_transport_fallback(self):
        policy = current_policy(); api = NoopRecoveryAPI(policy); calls = []
        read = api.json
        def denied(path):
            calls.append(path)
            if path.startswith('/contents/'):
                p.fail('provenance_unavailable')
            return read(path)
        api.json = denied
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_unavailable'):
            p.discover(api, policy)
        self.assertEqual(sum(path.startswith('/contents/') for path in calls), 1)
        self.assertFalse(any(path.startswith('logs:') for path in api.called))

    def test_noop_policy_does_not_expand_old_receipt_policy_exception(self):
        value = {'sequence': 1, 'policy_sha256': recovery.noop_public_policy()['get_policy_sha256']}
        self.assertFalse(recovery.accepts_receipt(recovery.ORIGINAL_RECEIPT_COMMIT,
            recovery.ORIGINAL_RECEIPT_SHA256, value, recovery.current_get_policy()))
        value['policy_sha256'] = recovery.old_public_policy()['get_policy_sha256']
        self.assertTrue(recovery.accepts_receipt(recovery.ORIGINAL_RECEIPT_COMMIT,
            recovery.ORIGINAL_RECEIPT_SHA256, value, recovery.current_get_policy()))
        self.assertFalse(recovery.accepts_receipt('a' * 40, recovery.ORIGINAL_RECEIPT_SHA256,
            value, recovery.current_get_policy()))


def current_policy():
    return {**recovery.old_public_policy(), 'code_sha': 'f' * 40,
            'rules_sha256': p.entry.rules_sha256(), 'get_policy_sha256': recovery.current_get_policy()}


class ExactProvenanceBridgeTests(unittest.TestCase):
    def test_literal_old_policy_and_original_record_match_external_pins(self):
        old = recovery.old_public_policy()
        self.assertEqual(a.sha(a.encoded(old)), recovery.OLD_PUBLIC_POLICY_SHA256)
        old['code_sha'] = 'b' * 40
        self.assertNotEqual(old, recovery.old_public_policy())
        api = RecoveryAPI(current_policy())
        self.assertEqual(api.records[recovery.ORIGINAL_RUN_ID]['record_sha256'], recovery.ORIGINAL_RECORD_SHA256)

    def test_current_policy_discovers_exact_old_publication_without_relabeling(self):
        policy = current_policy(); api = RecoveryAPI(policy)
        value = p.discover(api, policy)
        self.assertEqual(value['anchor'], {'commit': recovery.ORIGINAL_RECEIPT_COMMIT,
                                         'receipt_sha256': recovery.ORIGINAL_RECEIPT_SHA256})
        self.assertEqual(value['record_sha256'], recovery.ORIGINAL_RECORD_SHA256)
        self.assertEqual(api.records[recovery.ORIGINAL_RUN_ID]['policy_sha256'], recovery.old_public_policy()['get_policy_sha256'])
        self.assertEqual(api.called.count('/actions/runs/' + str(recovery.ORIGINAL_RUN_ID) + '/attempts/1'), 2)

    def test_only_original_run_attempt_controller_and_current_policy_are_eligible(self):
        for changes in ({'id': recovery.ORIGINAL_RUN_ID + 1}, {'run_attempt': 2},
                        {'head_sha': 'b' * 40}, {'event': 'schedule'}):
            policy = current_policy(); api = RecoveryAPI(policy, **changes)
            if changes.get('run_attempt') == 2:
                api.attempts[(recovery.ORIGINAL_RUN_ID, 1)] = {**api.runs[0], 'run_attempt': 1, 'conclusion': 'failure'}
            with self.subTest(changes=changes):
                with self.assertRaises(p.ProvenanceFailure): p.discover(api, policy)
        for key in ('rules_sha256', 'get_policy_sha256'):
            policy = current_policy(); policy[key] = 'e' * 64
            with self.assertRaises(p.ProvenanceFailure): p.discover(RecoveryAPI(policy), policy)

    def test_original_job_and_exact_record_are_mandatory(self):
        policy = current_policy()
        for mode in ('job', 'record', 'receipt', 'sequence'):
            api = RecoveryAPI(policy)
            if mode == 'job':
                path = '/actions/runs/' + str(recovery.ORIGINAL_RUN_ID) + '/attempts/1/jobs?per_page=100&page=1'
                jobs = api.json(path); jobs['jobs'][0]['id'] += 1
                api.overrides[path] = jobs; api.job_attempts[jobs['jobs'][0]['id']] = (recovery.ORIGINAL_RUN_ID, 1)
            else:
                row = api.runs[0]; changes = {'record': {'main_updated': False, 'cas_attempts': 0},
                    'receipt': {'receipt_sha256': 'a' * 64}, 'sequence': {'receipt_sequence': 2}}[mode]
                old = api.records[row['id']]; api.records[row['id']] = public.record(row, **{**old, **changes})
            with self.subTest(mode=mode):
                with self.assertRaises(p.ProvenanceFailure): p.discover(api, policy)


class RealReceiptBridgeTests(unittest.TestCase):
    def setUp(self):
        self.f = real_git.RecurringEntryTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.new_rules = self.f.rules; self.f.rules = 'b' * 64
        with patch.object(p.entry, 'rules_sha256', return_value=self.f.rules):
            self.first, self.first_files = self.f.prepare()
        self.f.rules = self.new_rules
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(p.entry, 'BOOTSTRAP', self.f.bootstrap))
        self.stack.enter_context(patch.object(recovery, 'ORIGINAL_RECEIPT_COMMIT', self.first['candidate_commit']))
        self.stack.enter_context(patch.object(recovery, 'ORIGINAL_RECEIPT_SHA256', self.first['receipt_sha256']))
        self.stack.enter_context(patch.object(recovery, 'old_public_policy', return_value={**recovery.old_public_policy(),
            'get_policy_sha256': self.first['policy_sha256']}))

    def test_noop_keeps_original_receipt_bytes_and_current_execution_marker(self):
        original = self.first_files[p.entry.FULL_RECEIPT]
        plan, files = self.f.prepare(self.first['candidate_commit'], self.f.anchor(self.first))
        self.assertEqual(files, {}); self.assertNotEqual(plan['policy_sha256'], self.first['policy_sha256'])
        value = p.publication_record(self.f.reader, self.f.evidence, plan, files,
            {'main_updated': False, 'cas_attempts': 0, 'readback_verified': True}, self.new_rules,
            run_id=1001, run_attempt=1, code_sha='f' * 40, bootstrap=self.f.bootstrap)
        self.assertEqual(value['policy_sha256'], plan['policy_sha256']); self.assertEqual(value['receipt_sequence'], 1)
        self.assertEqual(value['receipt_commit'], self.first['candidate_commit'])
        self.assertEqual(self.f.reader.read(value['receipt_commit'], p.entry.FULL_RECEIPT), original)

    def test_new_sequence_two_has_new_policy_and_exact_old_predecessor(self):
        notes = deepcopy(self.f.f.notes); notes['200']['content'] = 'Changed after the repair'
        plan, files = self.f.prepare(self.first['candidate_commit'], self.f.anchor(self.first), self.f.f.source(notes))
        value = json.loads(files[p.entry.FULL_RECEIPT]); old = json.loads(self.first_files[p.entry.FULL_RECEIPT])
        self.assertEqual(value['sequence'], 2); self.assertNotEqual(value['policy_sha256'], old['policy_sha256'])
        self.assertEqual(value['previous_receipt_commit'], self.first['candidate_commit'])
        self.assertEqual(value['previous_receipt_sha256'], self.first['receipt_sha256'])
        self.assertEqual(value['changed_content_ids'], ['200']); self.f.check(plan, files)
        again, writes = self.f.prepare(plan['candidate_commit'], self.f.anchor(plan), self.f.f.source(notes))
        self.assertEqual(writes, {})

    def test_other_receipt_policy_commit_and_foreign_delta_are_rejected(self):
        current = recovery.current_get_policy()
        with self.assertRaisesRegex(p.entry.EntryFailure, 'entry_policy_changed'):
            p.entry.read_receipt(self.f.reader, self.first['candidate_commit'], self.first['receipt_sha256'], 'e' * 64)
        with patch.object(recovery, 'ORIGINAL_RECEIPT_COMMIT', 'a' * 40):
            with self.assertRaisesRegex(p.entry.EntryFailure, 'entry_policy_changed'):
                p.entry.read_receipt(self.f.reader, self.first['candidate_commit'], self.first['receipt_sha256'], current)
        with patch.object(recovery, 'old_public_policy', return_value={'get_policy_sha256': 'e' * 64}):
            with self.assertRaisesRegex(p.entry.EntryFailure, 'entry_policy_changed'):
                p.entry.read_receipt(self.f.reader, self.first['candidate_commit'], self.first['receipt_sha256'], current)
        parent = self.f.commit_change(self.first['candidate_commit'], {p.entry.r.GET + '/foreign.md': b'foreign'})
        with self.assertRaisesRegex(p.entry.EntryFailure, 'entry_foreign_get_change'):
            self.f.prepare(parent, self.f.anchor(self.first))


class RetainedQueueTests(unittest.TestCase):
    def test_old_ten_markdown_events_survive_noop_and_new_state_only_receipt(self):
        for state_only in (False, True):
            with self.subTest(state_only=state_only):
                f = source_fixture.Tests(); f.setUp()
                try:
                    f.publication({source_fixture.N(i): 'retained exact body ' + str(i) for i in range(10)})
                    first = deepcopy(f.tip); raw = f.remote.repo.read(first['commit'], p.entry.FULL_RECEIPT)
                    old = recovery.old_public_policy(); old['get_policy_sha256'] = source_fixture.POLICY
                    new_policy = recovery.current_get_policy()
                    with patch.object(recovery, 'old_public_policy', return_value=old), \
                         patch.object(recovery, 'ORIGINAL_RECEIPT_COMMIT', first['commit']), \
                         patch.object(recovery, 'ORIGINAL_RECEIPT_SHA256', first['receipt_sha256']):
                        if state_only:
                            f.publication({}, mutate=lambda r: r.update(policy_sha256=new_policy))
                        row = public.run(head_sha='f' * 40)
                        record = public.record(row, code_sha='f' * 40, rules_sha256=p.entry.rules_sha256(),
                            policy_sha256=new_policy, main_updated=False, cas_attempts=0,
                            observed_main_commit=f.tip['commit'], receipt_commit=f.tip['commit'],
                            receipt_sha256=f.tip['receipt_sha256'], receipt_sequence=f.tip['sequence'])
                        parsed = p.parse_record(p.publication_line(record).encode(),
                            {'code_sha': 'f' * 40, 'rules_sha256': p.entry.rules_sha256(), 'get_policy_sha256': new_policy}, row)
                        confirmed_tip = {'commit': parsed['receipt_commit'], 'receipt_sha256': parsed['receipt_sha256'],
                                         'sequence': parsed['receipt_sequence']}
                        local, chain = f.load(policy=new_policy, tip=confirmed_tip)
                        self.assertEqual(chain['origin'], f.origin); self.assertEqual(chain['origin']['sequence'], 0)
                        self.assertEqual(chain['publications'][0]['commit'], first['commit'])
                        value = f.ingest(local, chain)
                        self.assertEqual(len(value['pending_by_id']), 10)
                        self.assertEqual({r['commit'] for r in value['pending_by_id'].values()}, {first['commit']})
                        pub = source_fixture.outbox.QueuePublication(local.vault, value, source_fixture.outbox.digest(value))
                        self.assertEqual(len(pub.descriptor['records']), 10)
                        _, again = f.load(local, after=f.tip, policy=new_policy)
                        self.assertEqual(again['publications'], []); self.assertEqual(f.ingest(local, again, value), value)
                        self.assertEqual(f.remote.repo.read(first['commit'], p.entry.FULL_RECEIPT), raw)
                finally: f.doCleanups()


if __name__ == '__main__': unittest.main()
