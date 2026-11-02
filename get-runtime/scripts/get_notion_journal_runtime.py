"""Exact-object journal loading and safe hash-only Actions checkpoints.

No credentials, HTTP implementation, remote writes or command-line entry.
Transports are injected from the existing approved runtime. Flushing stdout is
not proof of platform log retention: absent/mismatched provenance fails closed.
"""
import get_notion_fixed_source as fixed_auth
import get_notion_authenticated_recovery as authenticated_recovery
import get_private_proof as private_proof
from copy import deepcopy
from pathlib import Path
import re
import hashlib
from datetime import timedelta

import get_notion_outbox as codec
import get_notion_remote_journal as journal
import get_publication_provenance as provenance
import get_recurring_controller as controller
import get_notion_plan_recovery as plan_recovery
import get_notion_run15_recovery as run15_recovery
import get_notion_run53_recovery as run53_recovery
import get_notion_run57_recovery as run57_recovery
import get_notion_historical_active_recovery as historical_active
import get_notion_run16_recovery as run16_recovery
import get_notion_run18_recovery as run18_recovery
import get_notion_run28_recovery as run28_recovery
import get_notion_run32_recovery as run32_recovery
import get_notion_png_recovery as png_recovery
import get_notion_idle_recovery as idle_recovery
import get_notion_blocked_idle_recovery as blocked_idle

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

    def _local_complete_successor(self, commit):
        """Avoid re-shallowing a locally built CAS object, never remote readback.

        Only a complete single-parent successor of this loader's authenticated
        parent qualifies. Missing metadata uses the original bounded fetch;
        malformed/corrupt identities fail closed. No blobs or refs are fetched.
        """
        info = self.local.run('cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)',
                              data=(commit + '\n').encode())
        if info == (commit + ' missing\n').encode(): return False
        parts = info.decode().split()
        if len(parts) != 3 or parts[:2] != [commit, 'commit'] or not parts[2].isdigit(): fail('journal_loader_commit')
        if int(parts[2]) > 64 * 1024 * 1024: fail('journal_loader_budget')
        raw = self.local.run('cat-file', 'commit', commit)
        if hashlib.sha1(b'commit ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != commit:
            fail('journal_loader_commit')
        headers = raw.split(b'\n\n', 1)[0].splitlines()
        parents = [line[7:].decode() for line in headers if line.startswith(b'parent ')]
        trees = [line[5:].decode() for line in headers if line.startswith(b'tree ')]
        if len(trees) != 1: fail('journal_loader_tree')
        tree = journal.checked(trees[0], 40)
        if len(parents) != 1 or parents[0] not in self.verified_commits: return False
        shallow = self.root / '.git/shallow'
        if shallow.exists():
            if shallow.stat().st_size > 2 * 1024 * 1024: fail('journal_loader_budget')
            boundaries = shallow.read_text().splitlines()
            if commit in boundaries or parents[0] in boundaries: return False
        self._cached(parents[0])
        pending = {tree}; seen = set(); total = 0
        while pending:
            batch = sorted(pending - seen)
            if not batch: break
            if len(seen) + len(batch) > 100000: fail('journal_loader_budget')
            request = ''.join(x+'\n' for x in batch).encode()
            sizes = self.local.run('cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)', data=request).decode().splitlines()
            if len(sizes) != len(batch): fail('journal_loader_tree')
            needed = 0
            for oid, line in zip(batch, sizes):
                parts = line.split()
                if parts == [oid, 'missing']: return False
                if len(parts) != 3 or parts[:2] != [oid, 'tree'] or not parts[2].isdigit(): fail('journal_loader_tree')
                needed += int(parts[2]) + len(line.encode()) + 2
            if total + needed > 64 * 1024 * 1024: fail('journal_loader_budget')
            data = self.local.run('cat-file', '--batch', data=request)
            total += len(data)
            if total > 64 * 1024 * 1024: fail('journal_loader_budget')
            offset = 0; pending = set()
            for oid in batch:
                end = data.find(b'\n', offset)
                if end < 0: fail('journal_loader_tree')
                header = data[offset:end].decode().split(); offset = end + 1
                if header == [oid, 'missing']: return False
                if len(header) != 3 or header[:2] != [oid, 'tree'] or not header[2].isdigit(): fail('journal_loader_tree')
                size = int(header[2]); raw = data[offset:offset+size]; offset += size
                if len(raw) != size or data[offset:offset+1] != b'\n': fail('journal_loader_tree')
                offset += 1
                if hashlib.sha1(b'tree ' + str(size).encode() + b'\0' + raw).hexdigest() != oid:
                    fail('journal_loader_tree')
                cursor = 0
                while cursor < len(raw):
                    space = raw.find(b' ', cursor); nul = raw.find(b'\0', space + 1)
                    if space <= cursor or nul <= space + 1 or nul + 21 > len(raw): fail('journal_loader_tree')
                    mode = raw[cursor:space]; child = raw[nul+1:nul+21].hex()
                    if mode in (b'40000', b'040000'): pending.add(child)
                    cursor = nul + 21
                seen.add(oid)
            if offset != len(data): fail('journal_loader_tree')
        return True

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
            if not self._local_complete_successor(commit):
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
    def __init__(self, stream, *, context, compact=False):
        if type(compact) is not bool: fail('journal_checkpoint_receipt')
        self.compact = compact
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
            if self.compact: line = private_proof.line('journal', record) + '\n'
            if self.stream.write(line) != len(line): fail('journal_checkpoint_write')
            self.stream.flush()
            self.sequence = record['sequence']; self.previous = record['record_sha256']
            return True
        except Exception:
            self.failed = True
            fail('journal_checkpoint_failed')


def _run_origin(run, policy):
    if not isinstance(run, dict): fail('journal_provenance_run')
    provenance.positive(run.get('id')); provenance.positive(run.get('run_attempt'))
    for key in ('repository', 'head_repository'):
        repo = run.get(key)
        if (not isinstance(repo, dict) or repo.get('id') != policy['repository_id']
                or repo.get('full_name') != provenance.REPOSITORY or repo.get('private') is not False):
            fail('journal_provenance_run')
    if (run.get('workflow_id') != policy['workflow_id'] or run.get('path') != policy['workflow_path']
            or run.get('head_branch') != policy['head_branch']):
        fail('journal_provenance_run')
    journal.checked(run.get('head_sha'), 40)
    if provenance.timestamp(run['created_at']) > provenance.timestamp(run['updated_at']):
        fail('journal_provenance_time')
    return run


def _run_identity(run, policy):
    _run_origin(run, policy)
    if run.get('event') not in ('schedule', 'workflow_dispatch'):
        fail('journal_provenance_run')
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
    keys = ('id', 'workflow_id', 'path', 'head_sha', 'head_branch', 'event')
    # Exact-attempt and latest/list endpoints differ by at most one second.
    # Same-endpoint observations retain exact created_at comparison.
    skew = provenance.timestamp(newer['created_at']) - provenance.timestamp(older['created_at'])
    witness = run53_recovery.clock_witness(older, newer) if cross_endpoint else None
    if ((not cross_endpoint and skew != timedelta(0))
            or (cross_endpoint and not -timedelta(seconds=1) <= skew <= timedelta(0) and witness is None)):
        fail('journal_provenance_changed')
    if (any(older.get(key) != newer.get(key) for key in keys)
            or newer['run_attempt'] < older['run_attempt']
            or (not cross_endpoint and provenance.timestamp(newer['updated_at']) < provenance.timestamp(older['updated_at']))):
        fail('journal_provenance_changed')
    return witness


def _interval(step, job):
    start = provenance.timestamp(step.get('started_at'))
    end = provenance.timestamp(step.get('completed_at'))
    if not provenance.timestamp(job.get('started_at')) <= start <= end <= provenance.timestamp(job.get('completed_at')):
        fail('journal_provenance_time')
    return start, end


def _records(raw, expected, notion_interval, get_interval, policy, run):
    if not isinstance(raw, bytes) or len(raw) > provenance.MAX_LOG: fail('journal_provenance_budget')
    records = []; get_lines = []; fixed_records = []; previous = None; previous_stamp = None
    try: lines = raw.decode('utf-8', 'strict').splitlines()
    except Exception: fail('journal_provenance_log')
    for line in lines:
        match = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z) (.*)$', line)
        content = match[2] if match else line
        if not content.startswith((MARKER, provenance.MARKER, fixed_auth.MARKER)): continue
        if match is None: fail('journal_provenance_time')
        stamp = provenance.timestamp(match[1])
        if content.startswith(fixed_auth.MARKER):
            if (get_interval is not None or records or fixed_records
                    or not notion_interval[0] <= stamp <= notion_interval[1]):
                fail('journal_provenance_record')
            fixed_records.append(fixed_auth.parse(content[len(fixed_auth.MARKER):].encode(), expected, run))
            previous_stamp = stamp
            continue
        if content.startswith(provenance.MARKER):
            if get_interval is None: fail('journal_provenance_record')
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
    if get_interval is None:
        if get_lines or len(fixed_records) != 1: fail('journal_provenance_record')
        return records, fixed_records[0]
    if fixed_records: fail('journal_provenance_record')
    get_record = provenance.parse_record(('\n'.join(get_lines) + '\n').encode(), policy, run)
    return records, get_record


def _terminal_report(raw, notion_interval, *, completed_at=None):
    """Opt-in exact final public result from the authenticated original job log.

    This is not a new log claim: the ordinary original attempt, controller,
    checkpoint chain, job digest and fresh rereads remain mandatory.
    """
    found=[]; last_checkpoint=None
    for line in raw.decode('utf-8','strict').splitlines():
        match=re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z) (.*)$',line)
        content=match[2] if match else line
        if content.startswith(MARKER):
            if found or match is None: fail('journal_provenance_record')
            last_checkpoint=provenance.timestamp(match[1])
        if 'get-private-runtime-report-2' not in content: continue
        # Ignore the displayed shell source, not an emitted report. A JSON
        # report with this contract must have a timestamp and exact object.
        if not content.startswith('{'): continue
        value=journal.retention.strict_json(content.encode())
        if not isinstance(value,dict) or value.get('contract')!='get-private-runtime-report-2': continue
        if match is None or found or last_checkpoint is None: fail('journal_provenance_record')
        stamp=provenance.timestamp(match[1])
        # API timestamps without a fractional component identify an entire second.
        # Only this opt-in terminal report uses that explicit representation;
        # original checkpoints and fractional API endpoints stay exact.
        whole_second = (isinstance(completed_at, str) and re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', completed_at) is not None
            and provenance.timestamp(completed_at) == notion_interval[1])
        within_end = (stamp < notion_interval[1] + timedelta(seconds=1)
            if whole_second else stamp <= notion_interval[1])
        if not notion_interval[0] <= last_checkpoint <= stamp or not within_end: fail('journal_provenance_time')
        found.append(value)
    if len(found)!=1: fail('journal_provenance_record')
    return found[0]


def recover_anchor(client, *, run_id, run_attempt, public_policy,
                   approved_public_policy_sha256, notion_policy_sha256, require_success=False, include_job_digest=False, include_terminal_report=False, exact_missing_binding=None):
    """Authenticate one original terminal Actions attempt, even if Notion failed.

    This never qualifies a failed run as Get success. Exact controller bytes
    bind mutually exclusive canonical-success/publication or manual fixed-
    archive/skipped-Get evidence. Returned anchor still requires RemoteJournal's
    private commit/tree/blob readback before use.
    """
    try:
        if type(require_success) is not bool or type(include_job_digest) is not bool or type(include_terminal_report) is not bool: fail('journal_provenance_run')
        policy = provenance.validate_policy(public_policy, approved_public_policy_sha256)
        provenance.positive(run_id); provenance.positive(run_attempt)
        journal.checked(notion_policy_sha256, 64)
        repo = client.json('')
        if repo.get('id') != policy['repository_id'] or repo.get('full_name') != provenance.REPOSITORY or repo.get('private') is not False:
            fail('journal_provenance_repository')
        root = '/actions/runs/' + str(run_id)
        run = _terminal_run(client.json(root + '/attempts/' + str(run_attempt)), policy)
        if run['id'] != run_id or run['run_attempt'] != run_attempt: fail('journal_provenance_run')
        if require_success and run['conclusion'] != 'success': fail('journal_provenance_run')
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
        if require_success and job.get('conclusion') != 'success': fail('journal_provenance_job')
        steps = job.get('steps')
        if not isinstance(steps, list) or len(steps) > 100: fail('journal_provenance_steps')
        def selected(name):
            rows = [step for step in steps if isinstance(step, dict) and step.get('name') == name]
            if len(rows) != 1 or rows[0].get('status') != 'completed': fail('journal_provenance_steps')
            provenance.positive(rows[0].get('number')); return rows[0]
        get_step = selected(policy['publication_step_name']); notion_step = selected(NOTION_STEP)
        if notion_step.get('conclusion') == 'skipped':
            fail('journal_provenance_no_checkpoint')
        fixed = get_step.get('conclusion') == 'skipped'
        # Only the newly reviewed exact controller supports this second source
        # identity. An old skipped Get step can never become archive authority.
        if fixed and (policy['controller_template_sha256'] != controller.template_sha256()
                or run['event'] != 'workflow_dispatch'):
            fail('journal_provenance_steps')
        if (get_step.get('conclusion') not in ('success', 'skipped')
                or notion_step.get('conclusion') not in ('success', 'failure', 'cancelled', 'timed_out')
                or get_step['number'] >= notion_step['number']): fail('journal_provenance_steps')
        if require_success and notion_step.get('conclusion') != 'success': fail('journal_provenance_steps')
        get_times = None if fixed else _interval(get_step, job)
        notion_times = _interval(notion_step, job)
        if get_times is not None and get_times[1] > notion_times[0]: fail('journal_provenance_time')
        expected = context({'run_id': run_id, 'run_attempt': run_attempt,
            'code_sha': policy['code_sha'], 'controller_sha': run['head_sha'],
            'rules_sha256': policy['rules_sha256'], 'notion_policy_sha256': notion_policy_sha256,
            'public_policy_sha256': approved_public_policy_sha256})
        raw_log = client.logs(job['id'])
        if exact_missing_binding is not None:
            authenticated_recovery.checkpoint_resume.check_negative_observation(
                exact_missing_binding,policy,run,job,raw_log)
        raw = private_proof.expand_for(client, raw_log, policy=policy, run=run, notion_policy_sha256=notion_policy_sha256)
        records, get_record = _records(raw, expected, notion_times, get_times, policy, run)
        terminal_report = _terminal_report(raw, notion_times, completed_at=notion_step.get('completed_at')) if include_terminal_report else None
        # Re-read the exact original attempt, not just the mutable run alias.
        original = _terminal_run(client.json(root + '/attempts/' + str(run_attempt)), policy)
        if provenance.run_binding(original) != provenance.run_binding(run): fail('journal_provenance_changed')
        current = _latest_run(client.json(root), policy)
        clock_witness = _same_run(run, current, cross_endpoint=True)
        if current['run_attempt'] == run_attempt and provenance.run_semantic_binding(current) != provenance.run_semantic_binding(run):
            fail('journal_provenance_changed')
        final = records[-1]
        return {'status': 'journal_provenance_verified',
            'anchor': {key: final[key] for key in ('commit', 'state_blob', 'state_sha256')},
            'checkpoint': final, 'checkpoint_count': len(records), 'run_id': run_id,
            'run_attempt': run_attempt, 'job_id': job['id'],
            'get_step_record': None if fixed else get_record, 'get_run_success_claim': False,
            **({'fixed_source_record': get_record} if fixed else {}),
            **({'terminal_report': terminal_report, 'terminal_conclusions': {
                'job': job['conclusion'], 'notion': notion_step['conclusion'],
                'get': get_step['conclusion']}} if include_terminal_report else {}),
            'run_binding': provenance.run_binding(run), 'latest_binding':provenance.run_binding(current), 'latest_attempt': current['run_attempt'],
            'completed_at': job['completed_at'],
            **({'clock_witness': clock_witness} if clock_witness is not None else {}),
            **({'job_sha256': codec.digest(job)} if require_success or include_job_digest else {}),
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
        self.client = client; self.requests = 0; self.bytes = 0; self.controllers = {}; self.repository = None

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
        # One repository identity observation per discovery, validated by the
        # caller before any candidate. Every run/attempt also independently
        # carries the same repository IDs/publicness. No cache survives a run.
        if value == '' and self.repository is not None:
            return deepcopy(self.repository)
        # Immutable controller bytes may be reused; runs/jobs remain fresh.
        immutable = isinstance(value, str) and re.fullmatch(
            r'/contents/\.github/workflows/[a-z0-9][a-z0-9_-]{0,100}\.ya?ml\?ref=[a-f0-9]{40}', value)
        if immutable and value in self.controllers: return deepcopy(self.controllers[value])
        answer = self.call('json', value)
        if immutable: self.controllers[value] = deepcopy(answer)
        if value == '': self.repository = deepcopy(answer)
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
        # Bounded listing; failed runs remain candidates because a failed
        # Notion attempt can contain acknowledged durable writes.
        candidates = []; seen = set(); latest = {}; attempt_candidates=set(); listed_ids=set()
        for page in range(1, 4):
            listing = bounded.json('/actions/workflows/' + str(policy['workflow_id'])
                + '/runs?per_page=30&page=' + str(page))
            rows = listing.get('workflow_runs') if isinstance(listing, dict) else None
            if (not isinstance(rows, list) or len(rows) > 30 or type(listing.get('total_count')) is not int
                    or listing['total_count'] < len(rows)): fail('journal_provenance_runs')
            for row in rows:
                # A workflow-parse failure may be listed as a push. Only its
                # first terminal failed attempt with authenticated zero jobs
                # is negative evidence; failed Notion jobs remain candidates.
                if not isinstance(row, dict): fail('journal_provenance_run')
                ident=provenance.positive(row.get('id'))
                if ident in listed_ids: fail('journal_provenance_changed')
                listed_ids.add(ident)
                if (row.get('event') == 'push' and type(row.get('run_attempt')) is int
                        and row['run_attempt'] == 1 and row.get('status') == 'completed'
                        and row.get('conclusion') == 'failure'):
                    _run_origin(row, policy)
                    empty=bounded.json('/actions/runs/'+str(ident)+'/attempts/1/jobs?per_page=100&page=1')
                    if (not isinstance(empty,dict) or type(empty.get('total_count')) is not int
                            or empty['total_count'] != 0 or empty.get('jobs') != []):
                        fail('journal_provenance_jobs')
                    continue
                row = _latest_run(row, policy)
                latest[row['id']] = row
            if len(rows) < 30: break
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
            elif (provenance.run_semantic_binding(result['run_binding'])!=provenance.run_semantic_binding(row)
                    or (not timedelta(0) <= provenance.timestamp(result['run_binding']['created_at']) - provenance.timestamp(row['created_at']) <= timedelta(seconds=1)
                        and not (result.get('clock_witness') is not None
                            and result['clock_witness'] == run53_recovery.clock_witness(result['run_binding'], row)))):
                fail('journal_provenance_changed')
            # Only listing/alias observations bound the mutable latest clock.
            now=result.get('latest_binding')
            if not isinstance(now,dict):fail('journal_provenance_changed')
            before=latest[row['id']]
            _same_run(before,now)
            if now['run_attempt']==before['run_attempt'] and provenance.run_semantic_binding(now)!=provenance.run_semantic_binding(before):fail('journal_provenance_changed')
            # A dynamic current hint cannot be bypassed by any older exact
            # recovery branch. Every returned checkpoint must explain the same
            # current state under this policy; original current ancestry remains
            # mandatory after discovery.
            if locator_envelope is not None:
                result.update(rotation.validate_found(locator_envelope, result))
        # This single exact registration reserves one of the original four
        # candidate slots. Listing retention is not full log authentication.
        import get_notion_idle_rotation as rotation
        locator_envelope = rotation.current_hint(client, policy)
        locator = (tuple(locator_envelope['hint']['execution'][k]
                         for k in ('run_id', 'run_attempt'))
                   if locator_envelope is not None else
                   authenticated_recovery.checkpoint_resume.discovery_locator(policy))
        locator_module = authenticated_recovery.checkpoint_resume
        if locator is None:
            locator = authenticated_recovery.clean_active.discovery_locator(policy)
            locator_module = authenticated_recovery.clean_active
        if locator == (exclude_run_id, exclude_run_attempt): fail('journal_provenance_context')
        if locator is not None and locator_envelope is None:
            candidates = [r for r in candidates if (r['id'],r['run_attempt']) != locator]
        checked = 0; locator_used = False
        while (candidates or (locator is not None and not locator_used)) and checked < 4:
            candidates.sort(key=lambda row: (provenance.timestamp(row['updated_at']),
                row['id'], row['run_attempt']), reverse=True)
            if locator is not None and not locator_used and (checked == 3 or not candidates):
                ident,attempt=locator
                row=_latest_run(bounded.json('/actions/runs/'+str(ident)),policy)
                if row['id']!=ident or row['run_attempt']!=attempt or row['status']!='completed':fail('journal_provenance_changed')
                if ident in latest:_same_run(latest[ident],row)
                if locator_envelope is None and not locator_module.candidate_policy(policy,row)[1]:fail('journal_provenance_changed')
                latest[ident]=row;locator_used=True
            else:
                row = candidates.pop(0)
                if locator_envelope is not None and (row['id'], row['run_attempt']) == locator:
                    locator_used = True
            checked += 1
            # Exact target-scoped evidence is selected BEFORE an otherwise
            # deterministic mismatching-controller read. Each original
            # attempt/job/log/alias verifier remains intact; no dynamic cache.
            exact = authenticated_recovery.checkpoint_resume
            if exact.negative_binding(policy,row,latest=latest[row['id']]) is not None:
                exact.require_missing_negative(bounded,policy,row)
                continue
            if exact.candidate_policy(policy,row)[1]:
                result=exact.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                match_candidate(result,row)
                return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                    'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
            try:
                result = recover_anchor(bounded, run_id=row['id'], run_attempt=row['run_attempt'],
                    public_policy=policy, approved_public_policy_sha256=approved_public_policy_sha256,
                    notion_policy_sha256=notion_policy_sha256)
            except CheckpointFailure as exc:
                if locator_envelope is not None and (row['id'], row['run_attempt']) == locator:
                    raise
                if (str(exc) == 'journal_provenance_other_controller'
                        and authenticated_recovery.registered_run(row['id'], row['run_attempt'])
                        and authenticated_recovery.candidate_policy(policy, row)[1]):
                    result = authenticated_recovery.recover_checkpoint(bounded, row['id'], row['run_attempt'])
                    match_candidate(result, row)
                    return {**result, 'candidate_runs_checked': checked, 'candidate_attempts_checked': checked,
                        'provenance_reads': bounded.requests, 'provenance_bytes': bounded.bytes}
                if (str(exc) == 'journal_provenance_other_controller'
                        and historical_active.registered_run(row['id'], row['run_attempt'])
                        and historical_active.candidate_policy(policy, row)[1]):
                    result = historical_active.recover_checkpoint(bounded, row['id'], row['run_attempt'])
                    match_candidate(result, row)
                    return {**result, 'candidate_runs_checked': checked, 'candidate_attempts_checked': checked,
                        'provenance_reads': bounded.requests, 'provenance_bytes': bounded.bytes}
                if (str(exc) == 'journal_provenance_other_controller'
                        and blocked_idle.can_recover(policy, row['id'], row['run_attempt'])):
                    result = blocked_idle.recover_checkpoint(bounded, row['id'], row['run_attempt'],
                        current_public_policy=policy)
                    match_candidate(result, row)
                    return {**result, 'candidate_runs_checked': checked, 'candidate_attempts_checked': checked,
                        'provenance_reads': bounded.requests, 'provenance_bytes': bounded.bytes}
                if (str(exc) == 'journal_provenance_other_controller'
                        and idle_recovery.can_recover(policy, row['id'], row['run_attempt'])):
                    result = idle_recovery.recover_checkpoint(bounded, row['id'], row['run_attempt'],
                        current_public_policy=policy)
                    match_candidate(result, row)
                    return {**result, 'candidate_runs_checked': checked, 'candidate_attempts_checked': checked,
                        'provenance_reads': bounded.requests, 'provenance_bytes': bounded.bytes}
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
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(run16_recovery.RUN,run16_recovery.ATTEMPT)):
                    result=run16_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(run18_recovery.RUN,run18_recovery.ATTEMPT)):
                    result=run18_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(run28_recovery.RUN,run28_recovery.ATTEMPT)):
                    result=run28_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(run32_recovery.RUN,run32_recovery.ATTEMPT)):
                    result=run32_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(run53_recovery.RUN,run53_recovery.ATTEMPT)):
                    result=run53_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                if (str(exc)=='journal_provenance_other_controller'
                        and (row['id'],row['run_attempt'])==(run57_recovery.RUN,run57_recovery.ATTEMPT)):
                    result=run57_recovery.recover_checkpoint(bounded,row['id'],row['run_attempt'])
                    match_candidate(result,row)
                    return {**result,'candidate_runs_checked':checked,'candidate_attempts_checked':checked,
                        'provenance_reads':bounded.requests,'provenance_bytes':bounded.bytes}
                # A mismatched controller or an exactly validated skipped
                # Notion step cannot provide this policy's checkpoint. Do not
                # spend the four log-proof slots on these negative candidates.
                # The shared 40 physical-read / byte budgets still bound cost.
                if locator is None and str(exc) in ('journal_provenance_other_controller', 'journal_provenance_no_checkpoint'):
                    checked -= 1
                if str(exc) not in ('journal_provenance_missing', 'journal_provenance_no_checkpoint',
                                   'journal_provenance_other_controller'):
                    raise
            else:
                match_candidate(result,row)
                result = rotation.validate_found(locator_envelope, result)
                # All known candidates are ordered by original attempt time.
                # An earlier attempt can never outrank the attempt above it.
                # Stop as soon as the newest eligible checkpoint is verified;
                # do not consume the caller's physical budget after success.
                return {**result, 'candidate_runs_checked': checked, 'candidate_attempts_checked': checked,
                        'provenance_reads': bounded.requests, 'provenance_bytes': bounded.bytes}
            if checked < (3 if locator is not None else 4): previous(row)
        fail('journal_provenance_missing')
    except CheckpointFailure: raise
    except Exception: fail('journal_provenance_failed')
