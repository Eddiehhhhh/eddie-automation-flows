"""Exact-object journal loading and safe hash-only Actions checkpoints.

No credentials, HTTP implementation, remote writes or command-line entry.
Transports are injected from the existing approved runtime. Flushing stdout is
not proof of platform log retention: absent/mismatched provenance fails closed.
"""
from copy import deepcopy
from pathlib import Path
import re

import get_notion_outbox as codec
import get_notion_remote_journal as journal
import get_publication_provenance as provenance
import get_recurring_controller as controller
import get_notion_plan_recovery as plan_recovery
import get_notion_run15_recovery as run15_recovery
import get_notion_png_recovery as png_recovery

MARKER = 'GET_NOTION_JOURNAL_V1 '
CONTRACT = 'get-notion-journal-checkpoint-1'
NOTION_STEP = 'Sync confirmed Get content to Notion'
MAX_CHECKPOINTS = 1024
CONTEXT_KEYS = {'run_id', 'run_attempt', 'code_sha', 'controller_sha', 'rules_sha256',
                'notion_policy_sha256', 'public_policy_sha256'}
RECEIPT_KEYS = {'contract', 'repository', 'ref', 'path', 'parent_commit', 'commit',
                'tree', 'state_blob', 'state_sha256', 'state_bytes', 'cas_attempts',
                'readback_verified'}
RECORD_KEYS = CONTEXT_KEYS | {'contract', 'status', 'sequence', 'previous_record_sha256',
                'parent_commit', 'commit', 'tree', 'state_blob', 'state_sha256',
                'state_bytes', 'cas_attempts', 'readback_verified', 'record_sha256'}


class CheckpointFailure(ValueError):
    pass


def fail(code):
    raise CheckpointFailure(code) from None


def context(value):
    if not isinstance(value, dict) or set(value) != CONTEXT_KEYS:
        fail('journal_checkpoint_context')
    for key in ('run_id', 'run_attempt'): provenance.positive(value[key])
    for key in ('code_sha', 'controller_sha'): journal.checked(value[key], 40)
    for key in ('rules_sha256', 'notion_policy_sha256', 'public_policy_sha256'):
        journal.checked(value[key], 64)
    return deepcopy(value)


class ExactJournalLoader:
    """Only commit/tree and fixed-path state; never fetch inherited asset blobs.

    remote.local must be the existing validated transport for this exact clone.
    No clone, identity, token or persistent Git configuration is created here.
    """
    def __init__(self, root, remote, *, max_calls=2048, max_state_bytes=journal.MAX_STATE,
                 max_total_bytes=journal.MAX_BATCH_BYTES):
        self.root = Path(root).resolve(strict=True)
        self.remote = remote
        self.local = journal.retention.LocalGit(self.root)
        self.local.env['GIT_NO_LAZY_FETCH'] = '1'
        n = int(self.local.env['GIT_CONFIG_COUNT'])
        self.local.env.update(GIT_CONFIG_COUNT=str(n + 1))
        self.local.env['GIT_CONFIG_KEY_' + str(n)] = 'protocol.allow'
        self.local.env['GIT_CONFIG_VALUE_' + str(n)] = 'never'
        if Path(remote.local.vault).resolve(strict=True) != self.root:
            fail('journal_loader_target')
        for value, limit in ((max_calls, 2048), (max_state_bytes, journal.MAX_STATE),
                             (max_total_bytes, journal.MAX_BATCH_BYTES)):
            if type(value) is not int or not 1 <= value <= limit: fail('journal_loader_budget')
        self.max_calls = max_calls; self.max_state_bytes = max_state_bytes
        self.max_total_bytes = max_total_bytes; self.calls = 0; self.bytes = 0
        self.loaded = set(); self.verified_commits = {}

    def _identity(self):
        meta = self.remote.metadata()
        if meta.get('full_name') != journal.REPOSITORY or meta.get('private') is not True:
            fail('journal_loader_target')
        origins = self.remote.local.run('config', '--local', '--get-all', 'remote.origin.url').decode().splitlines()
        if origins != [journal.retention.REMOTE_URL]: fail('journal_loader_target')

    def _state_entry(self, tree):
        for index, part in enumerate(journal.STATE_PATH.split('/')):
            rows = self.remote.tree_entries(tree)
            if not isinstance(rows, list) or len(rows) > 100000: fail('journal_loader_tree')
            matches = [r for r in rows if isinstance(r, dict) and r.get('path') == part]
            if not matches: return None
            if len(matches) != 1: fail('journal_loader_tree')
            row = matches[0]; journal.checked(row.get('sha'), 40)
            if index < 2:
                if row.get('mode') != '040000' or row.get('type') != 'tree': fail('journal_loader_tree')
                tree = row['sha']
            else:
                if row.get('mode') != '100644' or row.get('type') != 'blob': fail('journal_loader_tree')
                if type(row.get('size')) is not int or not 0 <= row['size'] <= self.max_state_bytes:
                    fail('journal_loader_budget')
                return row

    def _blobs(self):
        rows = self.local.run('cat-file', '--batch-all-objects',
                              '--batch-check=%(objectname) %(objecttype)')
        if len(rows) > 64 * 1024 * 1024: fail('journal_loader_budget')
        return {row.split()[0] for row in rows.splitlines() if row.endswith(b' blob')}

    def _cached(self, commit):
        cached = self.verified_commits[commit]
        # Immutable OIDs only: local corruption/missing objects stop. The caller
        # still performs fresh head/source guards and complete candidate
        # readback. Only exact immutable remote objects may be reused.
        raw = self.local.run('cat-file', 'commit', commit)
        if self.local.run('hash-object', '-t', 'commit', '--stdin', data=raw).decode().strip() != commit:
            fail('journal_loader_cache')
        tree = self.local.run('rev-parse', commit + '^{tree}').decode().strip()
        if tree != cached['tree']: fail('journal_loader_cache')
        raw = self.local.run('cat-file', 'tree', tree)
        if self.local.run('hash-object', '-t', 'tree', '--stdin', data=raw).decode().strip() != tree:
            fail('journal_loader_cache')
        row = self.local.tree(commit).get(journal.STATE_PATH)
        expected = cached['state']
        if expected is None:
            if row is not None: fail('journal_loader_cache')
            return
        if row != ('100644', 'blob', expected['sha']): fail('journal_loader_cache')
        if self.local.run('cat-file', '-s', expected['sha']).strip() != str(expected['size']).encode():
            fail('journal_loader_cache')
        data = self.local.blob(expected['sha'])
        if (len(data) != expected['size'] or journal.retention.oid(data) != expected['sha']
                or codec.digest(data) != expected['sha256']): fail('journal_loader_cache')
        value = journal.retention.strict_json(data); codec.check_state(value)
        if codec.encoded(value) != data: fail('journal_loader_cache')

    def __call__(self, commit):
        try:
            journal.checked(commit, 40); self._identity()
            if self.calls >= self.max_calls: fail('journal_loader_budget')
            self.calls += 1
            if commit in self.verified_commits:
                self._cached(commit)
                return None
            refs = self.local.run('for-each-ref', '--format=%(refname) %(objectname)')
            head_bytes = (self.root / '.git/HEAD').read_bytes()
            existing_blobs = self._blobs()
            index = self.root / '.git/index'
            old_index = index.read_bytes() if index.exists() else None
            self.remote.local.run('-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
                'fetch', '--depth=512', '--filter=blob:none', '--no-tags',
                '--no-recurse-submodules', '--no-write-fetch-head', 'origin', commit)
            if self._blobs() - existing_blobs: fail('journal_loader_unrequested_blob')
            if self.local.run('cat-file', '-t', commit).strip() != b'commit': fail('journal_loader_commit')
            actual = self.remote.commit(commit)
            tree = self.local.run('rev-parse', commit + '^{tree}').decode().strip()
            if tree != actual.get('tree'): fail('journal_loader_commit')
            row = self._state_entry(tree)
            local_row = self.local.tree(commit).get(journal.STATE_PATH)
            if row is None:
                if local_row is not None: fail('journal_loader_tree')
            else:
                if local_row != ('100644', 'blob', row['sha']): fail('journal_loader_tree')
                if row['sha'] not in self.loaded:
                    if self.bytes + row['size'] > self.max_total_bytes: fail('journal_loader_budget')
                    data = self.remote.blob(row['sha'], row['size'])
                    if (not isinstance(data, bytes) or len(data) != row['size']
                            or journal.retention.oid(data) != row['sha']): fail('journal_loader_blob')
                    value = journal.retention.strict_json(data); codec.check_state(value)
                    if codec.encoded(value) != data: fail('journal_loader_blob')
                    if self.local.run('hash-object', '-w', '--stdin', data=data).decode().strip() != row['sha']:
                        fail('journal_loader_blob')
                    self.loaded.add(row['sha']); self.bytes += len(data)
                if self.local.run('cat-file', '-s', row['sha']).strip() != str(row['size']).encode():
                    fail('journal_loader_blob')
            if (self.local.run('for-each-ref', '--format=%(refname) %(objectname)') != refs
                    or (self.root / '.git/HEAD').read_bytes() != head_bytes
                    or (index.read_bytes() if index.exists() else None) != old_index):
                fail('journal_loader_local_mutation')
            self.verified_commits[commit] = {'tree': tree, 'state': None if row is None else {
                'sha': row['sha'], 'size': row['size'], 'sha256': codec.digest(self.local.blob(row['sha']))}}
            return None
        except CheckpointFailure: raise
        except Exception: fail('journal_loader_failed')


class CheckpointEmitter:
    """Hash/OID/count-only callback after RemoteJournal's complete readback.

    Context is already authenticated by the reviewed runtime. This callback
    verifies shape, writes one complete marker and flushes. It cannot promise
    that Actions will retain the log; future recovery must authenticate it.
    """
    def __init__(self, stream, *, context):
        self.context = globals()['context'](context)
        self.stream = stream; self.sequence = 0; self.previous = None; self.failed = False

    def __call__(self, receipt):
        if self.failed: fail('journal_checkpoint_locked')
        try:
            if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS
                    or receipt['contract'] != journal.RECEIPT_CONTRACT
                    or receipt['repository'] != journal.REPOSITORY or receipt['ref'] != journal.REF
                    or receipt['path'] != journal.STATE_PATH or receipt['readback_verified'] is not True):
                fail('journal_checkpoint_receipt')
            if type(receipt['cas_attempts']) is not int or receipt['cas_attempts'] not in (0, 1):
                fail('journal_checkpoint_receipt')
            if receipt['cas_attempts'] == 1: journal.checked(receipt['parent_commit'], 40)
            elif receipt['parent_commit'] is not None: fail('journal_checkpoint_receipt')
            for key in ('commit', 'tree', 'state_blob'): journal.checked(receipt[key], 40)
            journal.checked(receipt['state_sha256'], 64)
            if type(receipt['state_bytes']) is not int or not 0 < receipt['state_bytes'] <= journal.MAX_STATE:
                fail('journal_checkpoint_receipt')
            if self.sequence >= MAX_CHECKPOINTS: fail('journal_checkpoint_budget')
            record = {**self.context, 'contract': CONTRACT, 'status': 'journal_readback_verified',
                'sequence': self.sequence + 1, 'previous_record_sha256': self.previous,
                **{key: receipt[key] for key in ('parent_commit', 'commit', 'tree', 'state_blob',
                    'state_sha256', 'state_bytes', 'cas_attempts', 'readback_verified')}}
            record['record_sha256'] = codec.digest(record)
            line = MARKER + codec.encoded(record).decode().rstrip('\n') + '\n'
            if self.stream.write(line) != len(line): fail('journal_checkpoint_write')
            self.stream.flush()
            self.sequence = record['sequence']; self.previous = record['record_sha256']
            return True
        except Exception:
            self.failed = True
            fail('journal_checkpoint_failed')


def _run_identity(run, policy):
    if not isinstance(run, dict): fail('journal_provenance_run')
    provenance.positive(run.get('id')); provenance.positive(run.get('run_attempt'))
    for key in ('repository', 'head_repository'):
        repo = run.get(key)
        if (not isinstance(repo, dict) or repo.get('id') != policy['repository_id']
                or repo.get('full_name') != provenance.REPOSITORY or repo.get('private') is not False):
            fail('journal_provenance_run')
    if (run.get('workflow_id') != policy['workflow_id'] or run.get('path') != policy['workflow_path']
            or run.get('head_branch') != policy['head_branch']
            or run.get('event') not in ('schedule', 'workflow_dispatch')):
        fail('journal_provenance_run')
    journal.checked(run.get('head_sha'), 40)
    if provenance.timestamp(run['created_at']) > provenance.timestamp(run['updated_at']):
        fail('journal_provenance_time')
    return run


def _terminal_run(run, policy):
    run = _run_identity(run, policy)
    if run.get('status') != 'completed' or run.get('conclusion') not in ('success', 'failure', 'cancelled', 'timed_out'):
        fail('journal_provenance_run')
    return run


def _latest_run(run, policy):
    run = _run_identity(run, policy)
    if run.get('status') == 'completed': return _terminal_run(run, policy)
    if run.get('status') not in ('queued', 'in_progress', 'waiting', 'requested', 'pending') or run.get('conclusion') is not None:
        fail('journal_provenance_run')
    return run


def _same_run(older, newer, *, cross_endpoint=False):
    keys = ('id', 'workflow_id', 'path', 'head_sha', 'head_branch', 'event', 'created_at')
    if (any(older.get(key) != newer.get(key) for key in keys)
            or newer['run_attempt'] < older['run_attempt']
            or (not cross_endpoint and provenance.timestamp(newer['updated_at']) < provenance.timestamp(older['updated_at']))):
        fail('journal_provenance_changed')



def _interval(step, job):
    start = provenance.timestamp(step.get('started_at'))
    end = provenance.timestamp(step.get('completed_at'))
    if not provenance.timestamp(job.get('started_at')) <= start <= end <= provenance.timestamp(job.get('completed_at')):
        fail('journal_provenance_time')
    return start, end


def _records(raw, expected, notion_interval, get_interval, policy, run):
    if not isinstance(raw, bytes) or len(raw) > provenance.MAX_LOG: fail('journal_provenance_budget')
    records = []; get_lines = []; previous = None; previous_stamp = None
    try: lines = raw.decode('utf-8', 'strict').splitlines()
    except Exception: fail('journal_provenance_log')
    for line in lines:
        match = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z) (.*)$', line)
        content = match[2] if match else line
        if not content.startswith((MARKER, provenance.MARKER)): continue
        if match is None: fail('journal_provenance_time')
        stamp = provenance.timestamp(match[1])
        if content.startswith(provenance.MARKER):
            if not get_interval[0] <= stamp <= get_interval[1]: fail('journal_provenance_time')
            get_lines.append(line); continue
        if not notion_interval[0] <= stamp <= notion_interval[1]: fail('journal_provenance_time')
        if previous_stamp is not None and stamp < previous_stamp: fail('journal_provenance_time')
        previous_stamp = stamp
        if len(content) > 8192 or len(records) >= MAX_CHECKPOINTS: fail('journal_provenance_budget')
        value = journal.retention.strict_json(content[len(MARKER):].encode())
        if not isinstance(value, dict) or set(value) != RECORD_KEYS: fail('journal_provenance_record')
        if (value['contract'] != CONTRACT or value['status'] != 'journal_readback_verified'
                or any(value[k] != expected[k] for k in CONTEXT_KEYS)
                or value['readback_verified'] is not True
                or type(value['sequence']) is not int or value['sequence'] != len(records) + 1
                or value['previous_record_sha256'] != previous): fail('journal_provenance_record')
        unsigned = {k: v for k, v in value.items() if k != 'record_sha256'}
        if codec.digest(unsigned) != journal.checked(value['record_sha256'], 64): fail('journal_provenance_record')
        for key in ('commit', 'tree', 'state_blob'): journal.checked(value[key], 40)
        journal.checked(value['state_sha256'], 64)
        if type(value['cas_attempts']) is not int or value['cas_attempts'] not in (0, 1): fail('journal_provenance_record')
        if value['cas_attempts'] == 1: journal.checked(value['parent_commit'], 40)
        elif value['parent_commit'] is not None: fail('journal_provenance_record')
        if type(value['state_bytes']) is not int or not 0 < value['state_bytes'] <= journal.MAX_STATE:
            fail('journal_provenance_record')
        records.append(value); previous = value['record_sha256']
    if not records: fail('journal_provenance_missing')
    get_record = provenance.parse_record(('\n'.join(get_lines) + '\n').encode(), policy, run)
    return records, get_record


def recover_anchor(client, *, run_id, run_attempt, public_policy,
                   approved_public_policy_sha256, notion_policy_sha256):
    """Authenticate one original terminal Actions attempt, even if Notion failed.

    This never qualifies a failed run as Get success. It separately requires a
    successful Get step/record and exact controller bytes. Returned anchor still
    requires RemoteJournal's private commit/tree/blob readback before use.
    """
    try:
        policy = provenance.validate_policy(public_policy, approved_public_policy_sha256)
        provenance.positive(run_id); provenance.positive(run_attempt)
        journal.checked(notion_policy_sha256, 64)
        repo = client.json('')
        if repo.get('id') != policy['repository_id'] or repo.get('full_name') != provenance.REPOSITORY or repo.get('private') is not False:
            fail('journal_provenance_repository')
        root = '/actions/runs/' + str(run_id)
        run = _terminal_run(client.json(root + '/attempts/' + str(run_attempt)), policy)
        if run['id'] != run_id or run['run_attempt'] != run_attempt: fail('journal_provenance_run')
        controller.verify_remote(client, run['head_sha'], policy, event=run['event'])
        reviewed = controller.expected_bytes(policy)
        if (reviewed.count(('      - name: ' + NOTION_STEP).encode()) != 1
                or reviewed.count(b'        id: notion_sync') != 1):
            fail('journal_provenance_controller_step')
        result = client.json(root + '/attempts/' + str(run_attempt) + '/jobs?per_page=100&page=1')
        jobs = result.get('jobs')
        if not isinstance(jobs, list) or type(result.get('total_count')) is not int or result['total_count'] != len(jobs) or len(jobs) > 100:
            fail('journal_provenance_jobs')
        matches = [job for job in jobs if isinstance(job, dict) and job.get('name') == policy['job_name']]
        if len(matches) != 1: fail('journal_provenance_job')
        job = matches[0]; provenance.positive(job.get('id'))
        if (job.get('run_id') != run_id or job.get('run_attempt', run_attempt) != run_attempt
                or job.get('head_sha') != run['head_sha'] or job.get('status') != 'completed'
                or job.get('conclusion') not in ('success', 'failure', 'cancelled', 'timed_out')
                or not provenance.timestamp(run['created_at']) <= provenance.timestamp(job.get('started_at'))
                    <= provenance.timestamp(job.get('completed_at')) <= provenance.timestamp(run['updated_at'])):
            fail('journal_provenance_job')
        steps = job.get('steps')
        if not isinstance(steps, list) or len(steps) > 100: fail('journal_provenance_steps')
        def selected(name):
            rows = [step for step in steps if isinstance(step, dict) and step.get('name') == name]
            if len(rows) != 1 or rows[0].get('status') != 'completed': fail('journal_provenance_steps')
            provenance.positive(rows[0].get('number')); return rows[0]
        get_step = selected(policy['publication_step_name']); notion_step = selected(NOTION_STEP)
        if notion_step.get('conclusion') == 'skipped':
            fail('journal_provenance_no_checkpoint')
        if (get_step.get('conclusion') != 'success'
                or notion_step.get('conclusion') not in ('success', 'failure', 'cancelled', 'timed_out')
                or get_step['number'] >= notion_step['number']): fail('journal_provenance_steps')
        get_times = _interval(get_step, job); notion_times = _interval(notion_step, job)
        if get_times[1] > notion_times[0]: fail('journal_provenance_time')
        expected = context({'run_id': run_id, 'run_attempt': run_attempt,
            'code_sha': policy['code_sha'], 'controller_sha': run['head_sha'],
            'rules_sha256': policy['rules_sha256'], 'notion_policy_sha256': notion_policy_sha256,
            'public_policy_sha256': approved_public_policy_sha256})
        records, get_record = _records(client.logs(job['id']), expected, notion_times, get_times, policy, run)
        # Re-read the exact original attempt, not just the mutable run alias.
        original = _terminal_run(client.json(root + '/attempts/' + str(run_attempt)), policy)
        if provenance.run_binding(original) != provenance.run_binding(run): fail('journal_provenance_changed')
        current = _latest_run(client.json(root), policy)
        _same_run(run, current, cross_endpoint=True)
        if current['run_attempt'] == run_attempt and provenance.run_semantic_binding(current) != provenance.run_semantic_binding(run):
            fail('journal_provenance_changed')
        final = records[-1]
        return {'status': 'journal_provenance_verified',
            'anchor': {key: final[key] for key in ('commit', 'state_blob', 'state_sha256')},
            'checkpoint': final, 'checkpoint_count': len(records), 'run_id': run_id,
            'run_attempt': run_attempt, 'job_id': job['id'],
            'get_step_record': get_record, 'get_run_success_claim': False,
            'run_binding': provenance.run_binding(run), 'latest_binding':provenance.run_binding(current), 'latest_attempt': current['run_attempt'],
            'completed_at': job['completed_at'],
            'private_readback_required': True, 'credentials_created': 0,
            'transport': 'caller_injected', 'new_permissions': 0}
    except CheckpointFailure: raise
    except provenance.ProvenanceFailure as exc:
        if str(exc) == 'provenance_controller_mismatch':
            fail('journal_provenance_other_controller')
        fail('journal_provenance_failed')
    except Exception: fail('journal_provenance_failed')


class _ReadBudget:
    """One injected identity/transport; never retry or substitute another client."""
    def __init__(self, client):
        self.client = client; self.requests = 0; self.bytes = 0; self.controllers = {}

    def call(self, kind, value):
        if self.requests >= 40: fail('journal_provenance_budget')
        self.requests += 1
        answer = getattr(self.client, kind)(value)
        raw = answer if kind == 'logs' else provenance.a.encoded(answer)
        limit = provenance.MAX_LOG if kind == 'logs' else provenance.MAX_JSON
        if not isinstance(raw, bytes) or len(raw) > limit:
            fail('journal_provenance_budget')
        self.bytes += len(raw)
        if self.bytes > provenance.MAX_TOTAL: fail('journal_provenance_budget')
        return answer

    def json(self, value):
        # A controller's contents at an exact immutable commit may be reused
        # within this discovery only. Mutable runs/jobs/repo reads stay fresh.
        immutable = isinstance(value, str) and re.fullmatch(
            r'/contents/\.github/workflows/[a-z0-9][a-z0-9_-]{0,100}\.ya?ml\?ref=[a-f0-9]{40}', value)
        if immutable and value in self.controllers: return deepcopy(self.controllers[value])
        answer = self.call('json', value)
        if immutable: self.controllers[value] = deepcopy(answer)
        return answer
    def logs(self, value): return self.call('logs', value)


def discover_anchor(client, *, public_policy, approved_public_policy_sha256,
                    notion_policy_sha256, exclude_run_id=None, exclude_run_attempt=None):
    """Find the latest verified journal in at most four terminal attempts.

    Historical attempts remain eligible after a failed or ongoing rerun.

    Missing/old-code/no-checkpoint runs may be skipped. Denied, malformed or
    uncertain control-plane reads fail closed, with no alternate credentials.
    An existing private journal without a result cannot bootstrap itself.
    """
    try:
        policy = provenance.validate_policy(public_policy, approved_public_policy_sha256)
        journal.checked(notion_policy_sha256, 64)
        if (exclude_run_id is None) != (exclude_run_attempt is None): fail('journal_provenance_context')
        if exclude_run_id is not None:
            provenance.positive(exclude_run_id); provenance.positive(exclude_run_attempt)
        bounded = _ReadBudget(client)
        repo = bounded.json('')
        if (not isinstance(repo, dict) or repo.get('id') != policy['repository_id']
                or repo.get('full_name') != provenance.REPOSITORY or repo.get('private') is not False):
            fail('journal_provenance_repository')
        workflow = bounded.json('/actions/workflows/' + str(policy['workflow_id']))
        if (not isinstance(workflow, dict) or workflow.get('id') != policy['workflow_id']
                or workflow.get('path') != policy['workflow_path']): fail('journal_provenance_workflow')
        # Five listed runs plus the explicit current run; only its current attempt is excluded.
        listing = bounded.json('/actions/workflows/' + str(policy['workflow_id'])
            + '/runs?per_page=5&page=1')
        rows = listing.get('workflow_runs') if isinstance(listing, dict) else None
        if (not isinstance(rows, list) or len(rows) > 5 or type(listing.get('total_count')) is not int
                or listing['total_count'] < len(rows)): fail('journal_provenance_runs')
        candidates = []; seen = set(); latest = {}; attempt_candidates=set()
        for row in rows:
            row = _latest_run(row, policy)
            if row['id'] in latest: fail('journal_provenance_changed')
            latest[row['id']] = row
        if exclude_run_id is not None:
            current = _latest_run(bounded.json('/actions/runs/' + str(exclude_run_id)), policy)
            if current['id'] != exclude_run_id or current['run_attempt'] != exclude_run_attempt:
                fail('journal_provenance_changed')
            if exclude_run_id in latest: _same_run(latest[exclude_run_id], current)
            latest[exclude_run_id] = current
        def enqueue(row, *, from_attempt=False):
            key = (row['id'], row['run_attempt'])
            if key in seen: fail('journal_provenance_changed')
            seen.add(key); candidates.append(row)
            if from_attempt:attempt_candidates.add(key)
        def previous(row):
            if row['run_attempt'] <= 1: return
            number = row['run_attempt'] - 1
            prior = _terminal_run(bounded.json('/actions/runs/' + str(row['id'])
                + '/attempts/' + str(number)), policy)
            if prior['run_attempt'] != number: fail('journal_provenance_changed')
            _same_run(prior, row, cross_endpoint=(row['id'],row['run_attempt']) not in attempt_candidates); enqueue(prior,from_attempt=True)
        for row in latest.values():
            if ((row['id'], row['run_attempt']) == (exclude_run_id, exclude_run_attempt)
                    or row['status'] != 'completed'):
                previous(row)
            else: enqueue(row)
        def match_candidate(result,row):
            binding=provenance.run_binding(row)
            if (row['id'],row['run_attempt']) in attempt_candidates:
                if result['run_binding']!=binding:fail('journal_provenance_changed')
            elif provenance.run_semantic_binding(result['run_binding'])!=provenance.run_semantic_binding(row):
                fail('journal_provenance_changed')
            # Only listing/alias observations bound the mutable latest clock.
            now=result.get('latest_binding')
            if not isinstance(now,dict):fail('journal_provenance_changed')
            before=latest[row['id']]
            _same_run(before,now)
            if now['run_attempt']==before['run_attempt'] and provenance.run_semantic_binding(now)!=provenance.run_semantic_binding(before):fail('journal_provenance_changed')
        checked = 0
        while candidates and checked < 4:
            candidates.sort(key=lambda row: (provenance.timestamp(row['updated_at']),
                row['id'], row['run_attempt']), reverse=True)
            row = candidates.pop(0); checked += 1
            try:
                result = recover_anchor(bounded, run_id=row['id'], run_attempt=row['run_attempt'],
                    public_policy=policy, approved_public_policy_sha256=approved_public_policy_sha256,
                    notion_policy_sha256=notion_policy_sha256)
            except CheckpointFailure as exc:
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(plan_recovery.OLD_RUN,plan_recovery.OLD_ATTEMPT)):
                    # One reviewed old transaction only; same bounded client,
                    # exact original controller/log proof and fixed state anchor.
                    result=plan_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(plan_recovery.RUN10_RUN,plan_recovery.RUN10_ATTEMPT)):
                    result=plan_recovery.recover_run10_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(png_recovery.RUN,png_recovery.ATTEMPT)):
                    result=png_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(run15_recovery.RUN,run15_recovery.ATTEMPT)):
                    result=run15_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if str(exc) not in ('journal_provenance_missing', 'journal_provenance_no_checkpoint',
                                   'journal_provenance_other_controller'):
                    raise
            else:
                match_candidate(result,row)
                # All known candidates are ordered by original attempt time.
                # An earlier attempt can never outrank the attempt above it.
                # Stop as soon as the newest eligible checkpoint is verified;
                # do not consume the caller's physical budget after success.
                return {**result, 'candidate_runs_checked': checked, 'candidate_attempts_checked': checked,
                        'provenance_reads': bounded.requests, 'provenance_bytes': bounded.bytes}
            if checked < 4: previous(row)
        fail('journal_provenance_missing')
    except CheckpointFailure: raise
    except Exception: fail('journal_provenance_failed')
