"""Local bare/partial-clone loader and synthetic authenticated-log boundaries."""
from copy import deepcopy
import base64
import io
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_journal_runtime as rt
import get_notion_remote_journal as j
import get_notion_outbox as o
import get_notion_executor as e
import test_notion_remote_journal as git_fixture
import test_publication_provenance as public_fixture


def state(number=1):
    outer = o.initial_state()
    plan = {'records': [], 'synthetic_revision': number}
    outer['engine_state']['journal'] = {'plan': plan, 'plan_sha256': o.digest(plan),
        'index': 0, 'operation': {'phase': 'prepared'}}
    return outer


class Fixture(git_fixture.Fixture):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        fixture = self
        class Transport:
            @property
            def vault(self): return fixture.root
            def run(self, *args, **kwargs):
                if args == ('config', '--local', '--get-all', 'remote.origin.url'):
                    return (j.retention.REMOTE_URL + '\n').encode()
                fixture.fetch_args.append(args)
                return git_fixture.run(fixture.root, *args, **kwargs)
        self.local = Transport(); self.fetch_args = []; self.blob_reads = []

    def blob(self, oid, size):
        self.blob_reads.append(oid)
        return super().blob(oid, size)

    def tree_entries(self, tree):
        rows = []
        for raw in git_fixture.run(self.bare, 'ls-tree', '-z', tree).split(b'\0'):
            if not raw: continue
            meta, path = raw.split(b'\t'); mode, kind, oid = meta.decode().split()
            row = {'path': path.decode(), 'mode': mode, 'type': kind, 'sha': oid}
            if kind == 'blob': row['size'] = int(git_fixture.run(self.bare, 'cat-file', '-s', oid))
            rows.append(row)
        return rows

    def store(self, anchor=None, **kwargs):
        loader = rt.ExactJournalLoader(self.root, self)
        return j.RemoteJournal(self.root, self, anchor=anchor or {
            'commit': self.initial, 'state_blob': None, 'state_sha256': o.digest(o.initial_state())},
            load_commit=loader, guard=self.guard, confirm=self.confirm,
            sleep=lambda seconds: None, **kwargs)


def context(policy=None):
    policy = policy or public_fixture.policy()
    return {'run_id': 1001, 'run_attempt': 1, 'code_sha': policy['code_sha'],
        'controller_sha': 'c' * 40, 'rules_sha256': policy['rules_sha256'],
        'notion_policy_sha256': 'd' * 64, 'public_policy_sha256': rt.provenance.a.sha(rt.provenance.a.encoded(policy))}


def receipt(commit='1' * 40):
    return {'contract': j.RECEIPT_CONTRACT, 'repository': j.REPOSITORY,
        'ref': j.REF, 'path': j.STATE_PATH, 'parent_commit': '0' * 40,
        'commit': commit, 'tree': '2' * 40, 'state_blob': '3' * 40,
        'state_sha256': '4' * 64, 'state_bytes': 200, 'cas_attempts': 1,
        'readback_verified': True}


class LogsAPI(public_fixture.FakeAPI):
    def __init__(self, policy, checkpoints, conclusion='failure'):
        self.row = public_fixture.run(head_sha='c' * 40, conclusion=conclusion)
        super().__init__([self.row], {1001: public_fixture.record(self.row)})
        self.policy = policy
        self.job = {'id': 1501, 'run_id': 1001, 'run_attempt': 1, 'head_sha': 'c' * 40,
            'name': policy['job_name'], 'status': 'completed', 'conclusion': conclusion,
            'started_at': '2026-10-16T10:00:01Z', 'completed_at': '2026-10-16T10:04:59Z',
            'steps': [{'name': policy['publication_step_name'], 'number': 5, 'status': 'completed',
                'conclusion': 'success', 'started_at': '2026-10-16T10:00:02Z',
                'completed_at': '2026-10-16T10:01:00Z'},
                {'name': rt.NOTION_STEP, 'number': 6, 'status': 'completed',
                'conclusion': 'failure', 'started_at': '2026-10-16T10:01:01Z',
                'completed_at': '2026-10-16T10:04:58Z'}]}
        self.raw = ('2026-10-16T10:00:59Z ' + rt.provenance.publication_line(self.records[1001]) + '\n'
                    + ''.join('2026-10-16T10:02:00Z ' + line + '\n' for line in checkpoints.splitlines())).encode()

    def json(self, path):
        if '/jobs?' in path and path not in self.overrides:
            self.called.append(path); return {'total_count': 1, 'jobs': [deepcopy(self.job)]}
        return super().json(path)

    def logs(self, job_id):
        assert job_id == 1501; self.called.append('logs:' + str(job_id)); return self.raw


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name); self.store = self.f.store()

    def apply(self, value=None):
        return self.store.compare_and_set(self.store.read()[1], value or state())

    def test_fixed_outbox_codec_and_fresh_filtered_clone_resume(self):
        self.assertEqual(self.store.read()[0], o.initial_state())
        self.apply(); self.apply(state(2))
        anchor = deepcopy(self.store.anchor)
        self.f.root = self.f.clone('fresh-runtime')
        resumed = self.f.store(anchor)
        self.assertEqual(resumed.read(), (state(2), o.digest(state(2))))
        self.assertEqual(resumed.compare_and_set(o.digest(state(2)), state(3)), o.digest(state(3)))

    def test_raw_engine_state_is_not_an_allowed_schema(self):
        with self.assertRaises(j.JournalFailure):
            self.store.compare_and_set(o.digest(o.initial_state()), e.initial_state())
        self.assertEqual(self.f.calls, [])

    def test_real_blob_none_preserves_refs_index_and_missing_assets(self):
        blob = git_fixture.run(self.f.bare, 'rev-parse', 'main:asset.bin').decode().strip()
        refs = self.store.local.run('show-ref'); index = (self.f.root / '.git/index').read_bytes()
        missing = (blob + ' missing\n').encode()
        self.apply()
        self.assertEqual(self.store.local.run('cat-file', '--batch-check', data=(blob + '\n').encode()), missing)
        self.assertNotIn(blob, self.f.blob_reads)
        self.assertEqual(self.store.local.run('show-ref'), refs)
        self.assertEqual((self.f.root / '.git/index').read_bytes(), index)
        for args in self.f.fetch_args:
            self.assertIn('--filter=blob:none', args)
            self.assertIn('--no-write-fetch-head', args)
            self.assertRegex(args[-1], '^[0-9a-f]{40}$')

    def test_unrelated_main_changes_are_retained_with_runtime_loader(self):
        head = self.f.other_commit(); self.apply()
        self.assertEqual(self.f.calls[0][0], head)
        self.assertEqual(git_fixture.run(self.f.bare, 'show', 'main:unrelated/new.md'), b'Concurrent change\n')

    def test_loader_distinct_state_budget_stops_before_blob_download(self):
        self.apply(); self.f.root = self.f.clone('bounded-runtime')
        loader = rt.ExactJournalLoader(self.f.root, self.f, max_total_bytes=1)
        before = len(self.f.blob_reads)
        with self.assertRaisesRegex(rt.CheckpointFailure, 'journal_loader_budget'): loader(self.f.head())
        self.assertEqual(len(self.f.blob_reads), before)

    def test_loader_max_calls_and_wrong_origin_or_private_target(self):
        loader = rt.ExactJournalLoader(self.f.root, self.f, max_calls=1)
        loader(self.f.initial)
        with self.assertRaises(rt.CheckpointFailure): loader(self.f.initial)
        self.f.metadata = lambda: {'full_name': j.REPOSITORY, 'private': False}
        with self.assertRaises(rt.CheckpointFailure): rt.ExactJournalLoader(self.f.root, self.f)(self.f.initial)

    def test_loader_rejects_corrupt_blob(self):
        self.apply(); self.f.root = self.f.clone('bad-blob'); self.f.bad_blob = True
        with self.assertRaises(rt.CheckpointFailure): rt.ExactJournalLoader(self.f.root, self.f)(self.f.head())

    def test_detached_object_only_checkout_with_no_refs(self):
        self.store.local.run('update-ref', '--no-deref', 'HEAD', self.f.initial)
        for row in self.store.local.run('for-each-ref', '--format=%(refname)').decode().splitlines():
            self.store.local.run('update-ref', '-d', row)
        rt.ExactJournalLoader(self.f.root, self.f)(self.f.initial)
        self.assertEqual(self.store.local.run('for-each-ref'), b'')

    def test_fetch_ignoring_blob_none_is_rejected_before_state_or_cas(self):
        normal = self.f.local.run
        asset = git_fixture.run(self.f.bare, 'rev-parse', 'main:asset.bin').decode().strip()
        def widened(*args, **kwargs):
            result = normal(*args, **kwargs)
            if 'fetch' in args:
                data = git_fixture.run(self.f.bare, 'cat-file', 'blob', asset)
                git_fixture.run(self.f.root, 'hash-object', '-w', '--stdin', data=data)
            return result
        self.f.local.run = widened
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_loader_unrequested_blob$'):
            rt.ExactJournalLoader(self.f.root, self.f)(self.f.initial)
        self.assertEqual(self.f.calls, []); self.assertEqual(self.f.blob_reads, [])

    def test_engine_store_keeps_queue_and_engine_in_same_remote_cas(self):
        value = o.initial_state(); ident = '1234567890123456789'
        value.update(origin={'commit': 'a' * 40, 'receipt_sha256': None, 'sequence': 0},
            policy_sha256='f' * 64, last_enqueued_publication={'commit': 'b' * 40, 'receipt_sha256': 'c' * 64, 'sequence': 1},
            observed_publication_tip={'commit': 'b' * 40, 'receipt_sha256': 'c' * 64, 'sequence': 1},
            pending_by_id={ident: {'commit': 'b' * 40, 'parent': 'a' * 40, 'receipt_sha256': 'c' * 64,
                'sequence': 1, 'path': 'Raw/03 Get/a.md', 'blob': 'd' * 40, 'sha256': 'e' * 64}}, rotation_order=[ident])
        self.apply(value); engine_store = o.EngineStore(self.store)
        inner, version = engine_store.read()
        plan = {'records': [], 'skipped_conflicts': [], 'publication': {'initial_cohort': {'reviewed': True}}}
        inner['journal'] = {'plan': plan, 'plan_sha256': o.digest(plan), 'index': 0, 'operation': None}
        before = len(self.f.calls); engine_store.compare_and_set(version, inner)
        self.assertEqual(len(self.f.calls), before + 1)
        observed, _ = self.store.read()
        self.assertEqual(observed['pending_by_id'], value['pending_by_id'])
        self.assertEqual(observed['rotation_order'], value['rotation_order'])
        self.assertEqual(observed['engine_state'], inner)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        original = rt.controller.template_bytes()
        # Synthetic reviewed A includes the parent-specified fixed Notion step.
        template = original if rt.NOTION_STEP.encode() in original else original + b'\n      - name: Sync confirmed Get content to Notion\n        id: notion_sync\n        continue-on-error: true\n        run: python scripts/get_notion_runtime.py\n'
        self.patch = patch.object(rt.controller, 'template_bytes', return_value=template)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.policy = public_fixture.policy(); self.context = context(self.policy)
        self.stream = io.StringIO(); self.emitter = rt.CheckpointEmitter(self.stream, context=self.context)
        self.emitter(receipt())

    def api(self, **kwargs): return LogsAPI(self.policy, self.stream.getvalue(), **kwargs)

    def recover(self, api):
        return rt.recover_anchor(api, run_id=1001, run_attempt=1, public_policy=self.policy,
            approved_public_policy_sha256=rt.provenance.a.sha(rt.provenance.a.encoded(self.policy)), notion_policy_sha256='d' * 64)

    def test_hash_only_checkpoint_binds_A_C_and_failed_notion_run(self):
        self.emitter(receipt('2' * 40))
        result = self.recover(self.api())
        self.assertEqual(result['anchor']['commit'], '2' * 40)
        self.assertEqual(result['checkpoint_count'], 2)
        self.assertNotEqual(result['checkpoint']['code_sha'], result['checkpoint']['controller_sha'])
        self.assertTrue(result['private_readback_required'])
        self.assertFalse(result['get_run_success_claim'])
        self.assertNotIn('note_id', self.stream.getvalue()); self.assertNotIn('page_id', self.stream.getvalue())

    def test_cancelled_and_timed_out_terminal_runs_can_recover_checkpoint_only(self):
        for conclusion in ('cancelled', 'timed_out'):
            api = self.api(conclusion=conclusion)
            api.job['steps'][1]['conclusion'] = conclusion
            self.assertFalse(self.recover(api)['get_run_success_claim'])

    def test_ordinary_get_provenance_still_rejects_failed_run(self):
        with self.assertRaises(rt.provenance.ProvenanceFailure):
            rt.provenance.check_run(self.api().row, self.policy)

    def test_get_step_must_be_successful_and_real_marker_required(self):
        for mutate in ('failed_step', 'missing_marker', 'false_marker'):
            api = self.api()
            if mutate == 'failed_step': api.job['steps'][0]['conclusion'] = 'failure'
            elif mutate == 'missing_marker': api.raw = b'\n'.join(api.raw.splitlines()[1:])
            else: api.raw = api.raw.replace(b'canonical_publication_verified', b'local_candidate_verified')
            with self.assertRaises(rt.CheckpointFailure): self.recover(api)

    def test_wrong_controller_bytes_or_missing_fixed_notion_step_rejected(self):
        api = self.api()
        key = '/contents/' + self.policy['workflow_path'] + '?ref=' + 'c' * 40
        raw = rt.controller.expected_bytes(self.policy) + b'\n# foreign content'
        api.overrides[key] = {'type': 'file', 'path': self.policy['workflow_path'], 'encoding': 'base64',
            'size': len(raw), 'sha': j.retention.oid(raw), 'content': base64.b64encode(raw).decode()}
        with self.assertRaises(rt.CheckpointFailure): self.recover(api)
        original = rt.controller.template_bytes().replace(rt.NOTION_STEP.encode(), b'Different step').replace(b'id: notion_sync', b'id: other_step')
        with patch.object(rt.controller, 'template_bytes', return_value=original):
            policy = public_fixture.policy(); api = LogsAPI(policy, self.stream.getvalue())
            with self.assertRaises(rt.CheckpointFailure):
                rt.recover_anchor(api, run_id=1001, run_attempt=1, public_policy=policy,
                    approved_public_policy_sha256=rt.provenance.a.sha(rt.provenance.a.encoded(policy)), notion_policy_sha256='d' * 64)

    def test_wrong_run_attempt_branch_and_get_job_cannot_claim_checkpoint(self):
        for key, value in (('run_attempt', 2), ('head_branch', 'foreign'), ('event', 'pull_request')):
            api = self.api(); api.overrides['/actions/runs/1001/attempts/1'] = {**api.row, key: value}
            with self.assertRaises(rt.CheckpointFailure): self.recover(api)
        api = self.api(); api.job['head_sha'] = 'a' * 40
        with self.assertRaises(rt.CheckpointFailure): self.recover(api)

    def test_rerun_during_read_fails(self):
        api = self.api(); api.overrides['/actions/runs/1001'] = {**api.row, 'run_attempt': 2, 'head_sha': 'f' * 40}
        with self.assertRaises(rt.CheckpointFailure): self.recover(api)

    def test_record_wrong_context_extra_field_or_self_hash_does_not_grant_trust(self):
        line = self.stream.getvalue().strip()
        record = j.retention.strict_json(line[len(rt.MARKER):].encode())
        for key, value in (('code_sha', 'f' * 40), ('controller_sha', 'f' * 40),
                ('rules_sha256', 'e' * 64), ('notion_policy_sha256', 'e' * 64),
                ('public_policy_sha256', 'e' * 64), ('private_title', 'sensitive')):
            modified = {**record, key: value}; modified.pop('record_sha256')
            modified['record_sha256'] = o.digest(modified)
            api = LogsAPI(self.policy, rt.MARKER + o.encoded(modified).decode())
            with self.assertRaises(rt.CheckpointFailure): self.recover(api)

    def test_log_timestamp_and_fixed_step_interval_required(self):
        for replace in (b'2026-10-16T09:59:59Z ', b'2026-10-16T10:04:59Z ', b''):
            api = self.api(); api.raw = api.raw.replace(b'2026-10-16T10:02:00Z ', replace)
            with self.assertRaises(rt.CheckpointFailure): self.recover(api)
        api = self.api(); api.job['steps'][1]['name'] = 'Other arbitrary step'
        with self.assertRaises(rt.CheckpointFailure): self.recover(api)

    def test_checkpoint_timestamps_must_not_go_backwards(self):
        self.emitter(receipt('2' * 40)); api = self.api()
        lines = api.raw.splitlines(); lines[-1] = lines[-1].replace(b'10:02:00Z', b'10:01:59Z')
        api.raw = b'\n'.join(lines)
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_provenance_time$'): self.recover(api)

    def test_job_times_must_be_inside_original_run(self):
        api = self.api(); api.job['started_at'] = '2026-10-16T09:59:00Z'
        with self.assertRaises(rt.CheckpointFailure): self.recover(api)

    def test_missing_or_reordered_or_duplicate_checkpoint_chain_fails(self):
        self.emitter(receipt('2' * 40)); rows = self.stream.getvalue().splitlines()
        for lines in ([], rows[1:], rows[::-1], rows + rows[-1:]):
            with self.assertRaises(rt.CheckpointFailure):
                self.recover(LogsAPI(self.policy, '\n'.join(lines)))

    def test_flush_and_short_write_failure_lock_emitter(self):
        class Bad(io.StringIO):
            def flush(self): raise RuntimeError('PRIVATE ERROR')
        emitter = rt.CheckpointEmitter(Bad(), context=self.context)
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_checkpoint_failed$'): emitter(receipt())
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_checkpoint_locked$'): emitter(receipt())
        class Short(io.StringIO):
            def write(self, value): super().write(value[:10]); return 10
        emitter = rt.CheckpointEmitter(Short(), context=self.context)
        with self.assertRaises(rt.CheckpointFailure): emitter(receipt())

    def test_unverified_receipt_and_private_extra_fields_never_print(self):
        for value in ({**receipt(), 'readback_verified': False}, {**receipt(), 'page_id': 'secret'}):
            out = io.StringIO(); emitter = rt.CheckpointEmitter(out, context=self.context)
            with self.assertRaises(rt.CheckpointFailure): emitter(value)
            self.assertEqual(out.getvalue(), '')

    def test_real_fresh_clone_checkpoint_anchor_private_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(directory); store = f.store(); out = io.StringIO()
            store.confirm = rt.CheckpointEmitter(out, context=self.context)
            store.compare_and_set(o.digest(o.initial_state()), state())
            anchored = self.recover(LogsAPI(self.policy, out.getvalue()))['anchor']
            f.root = f.clone('fresh-recovery')
            resumed = f.store(anchored)
            self.assertEqual(resumed.read(), (state(), o.digest(state())))
            resumed.compare_and_set(o.digest(state()), state(2))
            self.assertEqual(len(f.calls), 2)


    def test_separate_python_process_authenticates_checkpoint_then_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(directory); store = f.store(); out = io.StringIO()
            store.confirm = rt.CheckpointEmitter(out, context=self.context)
            store.compare_and_set(o.digest(o.initial_state()), state())
            clone = f.clone('new-python-process')
            payload = {'root': str(clone), 'bare': str(f.bare), 'initial': f.initial,
                'policy': self.policy, 'policy_hash': self.context['public_policy_sha256'],
                'checkpoint': out.getvalue()}
            program = """
import json,sys
from pathlib import Path
from test_notion_journal_runtime import Fixture, LogsAPI, rt, j, o, state, git_fixture
v=json.load(sys.stdin)
anchor=rt.recover_anchor(LogsAPI(v['policy'],v['checkpoint']),run_id=1001,run_attempt=1,
    public_policy=v['policy'],approved_public_policy_sha256=v['policy_hash'],notion_policy_sha256='d'*64)['anchor']
f=Fixture.__new__(Fixture);f.root=Path(v['root']);f.bare=Path(v['bare']);f.initial=v['initial']
f.calls=[];f.guards=[];f.receipts=[];f.mode=None;f.stale=0;f.previous=None
f.bad_commit=None;f.bad_blob=False;f.confirm_value=True;f.guard_value=True;f.fetch_args=[];f.blob_reads=[]
class Transport:
    @property
    def vault(self):return f.root
    def run(self,*args,**kwargs):
        if args==('config','--local','--get-all','remote.origin.url'):return (j.retention.REMOTE_URL+'\\n').encode()
        return git_fixture.run(f.root,*args,**kwargs)
f.local=Transport();store=f.store(anchor)
assert store.read()==(state(),o.digest(state()))
assert store.compare_and_set(o.digest(state()),state(2))==o.digest(state(2))
assert len(f.calls)==1
print('separate_process_verified')
"""
            env = j.retention.safe_environment()
            env['PYTHONPATH'] = ':'.join(str(Path(path).absolute()) for path in sys.path if path)
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            result = subprocess.run([sys.executable, '-c', program], input=json.dumps(payload).encode(),
                env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, b'separate_process_verified\n')


class MultiAPI:
    def __init__(self, apis):
        self.apis = {api.row['id']: api for api in apis}; self.called = []
    def json(self, path):
        self.called.append(path)
        if '/runs?per_page=30' in path:
            return {'total_count': len(self.apis), 'workflow_runs': [deepcopy(a.row) for a in self.apis.values()]}
        if path.startswith('/actions/runs/'):
            return self.apis[int(path.split('/')[3])].json(path)
        return next(iter(self.apis.values())).json(path)
    def logs(self, job_id):
        self.called.append('logs:' + str(job_id))
        return self.apis[job_id - 500].logs(job_id)


class DiscoveryTests(unittest.TestCase):
    setUp = CheckpointTests.setUp
    def api(self, ident=1001, checkpoint=True):
        out = io.StringIO()
        rt.CheckpointEmitter(out, context={**self.context, 'run_id': ident})(receipt())
        api = LogsAPI(self.policy, out.getvalue() if checkpoint else '')
        api.row['id'] = ident; api.row['updated_at'] = '2026-10-16T10:' + str(ident - 995).zfill(2) + ':00Z'
        api.job.update(id=ident + 500, run_id=ident)
        old = rt.provenance.publication_line(public_fixture.record(public_fixture.run(head_sha='c' * 40)))
        new = rt.provenance.publication_line(public_fixture.record(api.row))
        api.raw = api.raw.replace(old.encode(), new.encode())
        api.logs = lambda job: api.raw
        return api
    def discover(self, api, **kwargs):
        return rt.discover_anchor(api, public_policy=self.policy,
            approved_public_policy_sha256=self.context['public_policy_sha256'],
            notion_policy_sha256='d' * 64, **kwargs)
    def test_latest_missing_checkpoint_skipped_for_older_verified(self):
        api = MultiAPI([self.api(), self.api(1002, False)])
        result = self.discover(api)
        self.assertEqual(result['run_id'], 1001); self.assertEqual(result['candidate_runs_checked'], 2)
        self.assertLessEqual(result['provenance_reads'], 40)
    def test_current_run_excluded_and_newest_valid_chosen(self):
        api = MultiAPI([self.api(), self.api(1002), self.api(1003)])
        result = self.discover(api, exclude_run_id=1003, exclude_run_attempt=1)
        self.assertEqual(result['run_id'], 1002)
        self.assertFalse(any('/runs/1003/' in p for p in api.called))
    def test_only_four_terminal_candidates_ever_checked(self):
        api = MultiAPI([self.api(1001), *[self.api(i, False) for i in range(1002, 1006)]])
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_provenance_missing$'):
            self.discover(api)
        self.assertFalse(any('/runs/1001/' in p for p in api.called))
        self.assertEqual(sum(p.startswith('logs:') for p in api.called), 4)
    def test_denied_log_read_stops_without_fallback_or_older_candidate(self):
        latest = self.api(1002)
        def denied(job): raise PermissionError('private provider message')
        latest.logs = denied
        api = MultiAPI([self.api(), latest])
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_provenance_failed$'):
            self.discover(api)
        self.assertFalse(any('/runs/1001/' in p for p in api.called))
    def test_list_attempt_drift_stops(self):
        one = self.api(); one.overrides['/actions/runs/1001/attempts/1'] = {**one.row, 'conclusion':'cancelled'}
        one.overrides['/actions/runs/1001'] = one.overrides['/actions/runs/1001/attempts/1']
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_provenance_changed$'):
            self.discover(MultiAPI([one]))
    def test_no_checkpoint_cannot_self_authorize_existing_private_journal(self):
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_provenance_missing$'):
            self.discover(MultiAPI([self.api(checkpoint=False)]))
    def test_public_policy_uses_existing_newline_canonical_codec(self):
        self.assertNotEqual(self.context['public_policy_sha256'], o.digest(self.policy))
        with self.assertRaises(rt.CheckpointFailure):
            rt.discover_anchor(MultiAPI([self.api()]), public_policy=self.policy,
                approved_public_policy_sha256=o.digest(self.policy), notion_policy_sha256='d' * 64)
    def test_injected_read_budget_stops_after_40_without_retry(self):
        api = MultiAPI([self.api()]); bounded = rt._ReadBudget(api)
        for _ in range(40): bounded.json('/actions/runs/1001')
        with self.assertRaisesRegex(rt.CheckpointFailure, '^journal_provenance_budget$'): bounded.json('/actions/runs/1001')
        self.assertEqual(len(api.called), 40)


if __name__ == '__main__': unittest.main()
