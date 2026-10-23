"""Bounded public-Actions provenance for canonical Get receipts.

Only public metadata/log GETs. Anonymous reads remain explicit; production may
inject its existing runtime token for this one fixed public API repository.
No credential discovery/store, new permissions, private requests or writes.
The caller pins one reviewed repository/workflow/code policy independently.
"""
import get_private_proof as private_proof
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import get_recurring_entry as entry
import get_recurring_controller as controller
import get_recurring_policy_recovery as recovery
from get_recurring_entry import a, m

REPOSITORY = 'Eddiehhhhh/eddie-automation-flows'
API = 'https://api.github.com/repos/' + REPOSITORY
HISTORY_WITNESS = _private_bindings.value('get_publication_provenance', 'HISTORY_WITNESS')
CONTRACT = 'get-canonical-publication-anchor-1'
POLICY_CONTRACT = 'get-public-actions-provenance-policy-1'
MARKER = 'GET_CANONICAL_PUBLICATION_V1 '
MAX_JSON = 2 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_REQUESTS = 40
MAX_RUN_PAGES = 3
RUN_PAGE_SIZE = 30
MAX_CANDIDATES = 4
MAX_SECONDS = 120
POLICY_KEYS = {'contract', 'repository', 'repository_id', 'workflow_id', 'workflow_path',
               'head_branch', 'code_sha', 'job_name', 'publication_step_name',
               'rules_sha256', 'get_policy_sha256', 'controller_template_sha256'}
RECORD_KEYS = {'contract', 'status', 'run_id', 'run_attempt', 'code_sha', 'rules_sha256',
               'policy_sha256', 'observed_main_commit', 'receipt_commit', 'receipt_sha256',
               'receipt_sequence', 'main_updated', 'cas_attempts', 'readback_verified', 'record_sha256', 'controller_sha'}


class ProvenanceFailure(a.ParityFailure):
    pass


def fail(code):
    raise ProvenanceFailure(code)


def positive(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        fail('provenance_schema')
    return value


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z', value):
        fail('provenance_timestamp')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        fail('provenance_timestamp')


def validate_policy(value, expected_sha256):
    """The expected policy hash is externally reviewed, never learned here."""
    if not isinstance(value, dict) or set(value) != POLICY_KEYS or a.sha(a.encoded(value)) != a.checked(expected_sha256, 64):
        fail('provenance_policy')
    if value['contract'] != POLICY_CONTRACT or value['repository'] != REPOSITORY:
        fail('provenance_policy')
    positive(value['repository_id']); positive(value['workflow_id'])
    for key, width in [('code_sha', 40), ('rules_sha256', 64), ('get_policy_sha256', 64), ('controller_template_sha256', 64)]:
        a.checked(value[key], width)
    if not isinstance(value['workflow_path'], str) or not re.fullmatch(r'\.github/workflows/[a-z0-9][a-z0-9_-]{0,100}\.ya?ml', value['workflow_path']):
        fail('provenance_policy')
    if not isinstance(value['head_branch'], str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,128}', value['head_branch']) or '..' in value['head_branch']:
        fail('provenance_policy')
    for key in ('job_name', 'publication_step_name'):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 160 or any(ord(c) < 32 for c in value[key]):
            fail('provenance_policy')
    controller.policy_template_bytes(value)
    if value['job_name'] != controller.JOB_NAME or value['publication_step_name'] != controller.STEP_NAME:
        fail('provenance_controller_policy')
    return deepcopy(value)


class InspectRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow automatically; expose HTTP status to the exact log gate.

    urllib converts this returned None into HTTPError, which once() consumes as
    a bounded response. The log gate alone validates one HTTPS CDN destination;
    it then makes a separate credential-free GET. Other redirects stay failures.
    """
    def redirect_request(self, *args, **kwargs):
        return None


class _PublicGithub:
    """Shared bounded transport; no ambient auth or reusable download URLs."""
    def __init__(self, *, token=None, opener=None, clock=time.monotonic, sleep=time.sleep):
        self._token = token
        self.credentials_used = int(token is not None)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), InspectRedirect())
        self.clock = clock; self.sleep = sleep; self.elapsed = 0; self.requests = 0; self.bytes = 0

    def once(self, url, maximum):
        if self.requests >= MAX_REQUESTS or self.elapsed >= MAX_SECONDS:
            fail('provenance_budget')
        self.requests += 1
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'canonical-get-public-provenance',
                   'X-GitHub-Api-Version': '2022-11-28', 'Accept-Encoding': 'identity'}
        # Never forward the runtime token to a signed log-download host, even
        # one inside GitHub's own allowed CDN domains. Exact API repo only.
        if self._token is not None and (url == API or url.startswith(API + '/')):
            headers['Authorization'] = 'Bearer ' + self._token
        request = urllib.request.Request(url, headers=headers, method='GET')
        # Private object loading and other caller work can separate public
        # phases by minutes. Charge only actual public transport time, including
        # failed open/read operations; never reset the cumulative request budget.
        started = self.clock()
        try:
            try:
                response = self.opener.open(request, timeout=min(15, MAX_SECONDS - self.elapsed))
            except urllib.error.HTTPError as exc:
                response = exc
            except Exception:
                fail('provenance_transport')
            try:
                with response:
                    status = response.status
                    headers = dict(response.headers.items())
                    raw = response.read(maximum + 1)
            except Exception:
                fail('provenance_transport')
        finally:
            self.elapsed += max(0, self.clock() - started)
        if self.elapsed >= MAX_SECONDS:
            fail('provenance_budget')
        self.bytes += len(raw)
        if len(raw) > maximum or self.bytes > MAX_TOTAL:
            fail('provenance_budget')
        return status, {k.lower(): v for k, v in headers.items()}, raw

    def request(self, path, maximum, *, log=False):
        if not isinstance(path, str) or path != '' and not path.startswith('/') or any(c in path for c in ('\\', '\r', '\n', '#')) or '..' in path:
            fail('provenance_target')
        url = API + path
        for attempt in range(2):
            status, headers, raw = self.once(url, maximum)
            if status in (500, 502, 503, 504) and attempt == 0:
                started = self.clock()
                try:
                    self.sleep(1)
                finally:
                    self.elapsed += max(0, self.clock() - started)
                continue
            break
        # No retry of rate limits, authentication denials, expiry or ambiguity.
        if log:
            if status != 302:
                fail('provenance_logs_unavailable')
            location = headers.get('location', '')
            try:
                parsed = urllib.parse.urlsplit(location)
                port = parsed.port
                host = parsed.hostname or ''
            except ValueError:
                fail('provenance_log_redirect')
            allowed = host.endswith('.blob.core.windows.net') or host.endswith('.actions.githubusercontent.com') or host.endswith('.githubusercontent.com')
            if len(location) > 8192 or parsed.scheme != 'https' or parsed.username or parsed.password or port not in (None, 443) or parsed.fragment or not allowed:
                fail('provenance_log_redirect')
            status, _, raw = self.once(location, maximum)
            if status != 200:
                fail('provenance_logs_unavailable')
            return raw
        if status != 200:
            fail('provenance_unavailable')
        return raw

    def json(self, path):
        return m.strict_capture_json(self.request(path, MAX_JSON), MAX_JSON)

    def logs(self, job_id):
        return self.request('/actions/jobs/' + str(positive(job_id)) + '/logs', MAX_LOG, log=True)


class AnonymousGithub(_PublicGithub):
    """Deliberate public read mode. Never inspect environment credentials."""
    def __init__(self, *, opener=None, clock=time.monotonic, sleep=time.sleep):
        super().__init__(opener=opener, clock=clock, sleep=sleep)


class PublicGithub(_PublicGithub):
    """Existing runtime token, fixed public API GETs, no anonymous fallback."""
    def __init__(self, token, *, opener=None, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token):
            fail('provenance_credentials')
        super().__init__(token=token, opener=opener, clock=clock, sleep=sleep)


def publication_record(reader, evidence, plan, files, publication, approved_rules_sha256, *, run_id, run_attempt, code_sha, controller_sha=None, bootstrap=entry.BOOTSTRAP, history_witness=None):
    """Call only with the actual shared publisher result after exact readback."""
    positive(run_id); positive(run_attempt); a.checked(code_sha, 40)
    controller_sha = a.checked(controller_sha if controller_sha is not None else code_sha, 40)
    handoff = entry.confirmed_private_handoff(reader, evidence, plan, files, publication, approved_rules_sha256, bootstrap=bootstrap, history_witness=history_witness)
    if not handoff['receipt_commit'] or not handoff['receipt_sha256']:
        fail('provenance_no_receipt')
    receipt = entry.read_receipt(reader, handoff['receipt_commit'], handoff['receipt_sha256'], plan['policy_sha256'])
    record = {'contract': CONTRACT, 'status': 'canonical_publication_verified',
              'run_id': run_id, 'run_attempt': run_attempt, 'code_sha': code_sha, 'controller_sha': controller_sha,
              'rules_sha256': approved_rules_sha256, 'policy_sha256': plan['policy_sha256'],
              'observed_main_commit': plan['candidate_commit'], 'receipt_commit': handoff['receipt_commit'],
              'receipt_sha256': handoff['receipt_sha256'], 'receipt_sequence': receipt['sequence'],
              **publication}
    record['record_sha256'] = a.sha(a.encoded(record))
    return record


def publication_line(record):
    """Only this exact line belongs in the final successful publication step."""
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        fail('provenance_record')
    parse_record((MARKER + a.encoded(record).decode()).encode(),
                 {'code_sha': record['code_sha'], 'rules_sha256': record['rules_sha256'], 'get_policy_sha256': record['policy_sha256']},
                 {'id': record['run_id'], 'run_attempt': record['run_attempt'], 'head_sha': record['controller_sha']})
    return MARKER + a.encoded(record).decode().rstrip('\n')


def parse_record(raw, policy, run):
    if not isinstance(raw, bytes) or len(raw) > MAX_LOG:
        fail('provenance_budget')
    try:
        lines = raw.decode('utf-8', 'strict').splitlines()
    except UnicodeError:
        fail('provenance_logs_shape')
    records = []
    for line in lines:
        # GitHub's authenticated-origin plain-text log adds an ISO timestamp.
        line = re.sub(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z ', '', line, count=1)
        if line.startswith(MARKER):
            if len(line) > 8192:
                fail('provenance_record')
            records.append(m.strict_capture_json(line[len(MARKER):].encode(), 8192))
    if len(records) != 1:
        fail('provenance_record_count')
    record = records[0]
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        fail('provenance_record')
    positive(record['run_id']); positive(record['run_attempt'])
    if a.checked(record['controller_sha'], 40) != run['head_sha']:
        fail('provenance_record')
    for key, width in [('code_sha', 40), ('rules_sha256', 64), ('policy_sha256', 64)]:
        a.checked(record[key], width)
    unsigned = {k: v for k, v in record.items() if k != 'record_sha256'}
    if a.sha(a.encoded(unsigned)) != a.checked(record['record_sha256'], 64):
        fail('provenance_record')
    if record['contract'] != CONTRACT or record['status'] != 'canonical_publication_verified' or record['run_id'] != run['id'] or record['run_attempt'] != run['run_attempt']:
        fail('provenance_record')
    if any(record[k] != policy[p] for k, p in [('code_sha', 'code_sha'), ('rules_sha256', 'rules_sha256'), ('policy_sha256', 'get_policy_sha256')]):
        fail('provenance_record')
    if type(record['main_updated']) is not bool or type(record['cas_attempts']) is not int or record['cas_attempts'] != int(record['main_updated']) or record['readback_verified'] is not True:
        fail('provenance_unconfirmed')
    for k, width in [('observed_main_commit', 40), ('receipt_commit', 40), ('receipt_sha256', 64)]:
        a.checked(record[k], width)
    if type(record['receipt_sequence']) is not int or not 1 <= record['receipt_sequence'] <= 1000000:
        fail('provenance_record')
    if record['main_updated'] and record['observed_main_commit'] != record['receipt_commit']:
        fail('provenance_record')
    return record


def verified_prepare_only(raw, policy, run, job, emission):
    """A missing apply receipt is fatal; only the fixed runtime's full safe
    manual report, inside its successful step, proves a prepare-only execution.
    This is a negative classification, never a publication trust anchor.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_LOG:
        fail('provenance_budget')
    try:
        text = raw.decode('utf-8', 'strict')
    except UnicodeError:
        fail('provenance_logs_shape')
    if MARKER.strip() in text or 'recurring_candidate_verified' not in text:
        return False
    if run['event'] != 'workflow_dispatch':
        fail('provenance_prepare_only')
    notion = [s for s in job['steps'] if isinstance(s, dict)
              and s.get('name') == 'Sync confirmed Get content to Notion']
    if len(notion) != 1 or notion[0].get('status') != 'completed' or notion[0].get('conclusion') != 'skipped':
        fail('provenance_prepare_only')
    start = timestamp(emission.get('started_at')); end = timestamp(emission.get('completed_at'))
    if not (timestamp(run['created_at']) <= timestamp(job.get('started_at')) <= start <= end
            <= timestamp(job['completed_at']) <= timestamp(run['updated_at'])):
        fail('provenance_timestamp')
    # GitHub may declare completed_at only to whole-second precision while
    # runner logs retain fractions. That declared second is [end, end + 1s);
    # a fractional API end keeps its exact inclusive bound. Metadata ordering
    # and the start bound above remain unchanged. This affects only the
    # prepare-only negative classification, never publication/checkpoint proof.
    def exact_stamp(value):
        # datetime truncates nanoseconds; preserve all declared log precision.
        stamp = timestamp(value)
        fraction = value.split('.', 1)[1][:-1] if '.' in value else ''
        return stamp.replace(microsecond=0), int(fraction.ljust(9, '0'))
    coarse_end = '.' not in emission['completed_at']
    lower = exact_stamp(emission['started_at'])
    upper = (end + timedelta(seconds=1), 0) if coarse_end else exact_stamp(emission['completed_at'])
    outer_end = min(exact_stamp(job['completed_at']), exact_stamp(run['updated_at']))
    reports = []
    for line in text.splitlines():
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z) (.*)', line)
        if match:
            stamp = exact_stamp(match[1])
            in_window = lower <= stamp <= outer_end and (stamp < upper if coarse_end else stamp <= upper)
            if in_window and match[2].startswith('{'):
                reports.append(m.strict_capture_json(match[2].encode(), 16384))
    if len(reports) != 1:
        fail('provenance_prepare_only')
    value = reports[0]
    import get_private_runtime_report as private_report
    if isinstance(value, dict) and value.get('contract') == private_report.CONTRACT:
        if value != private_report.PREPARE or policy['controller_template_sha256'] != controller.template_sha256():
            fail('provenance_prepare_only')
        return True
    fixed = {'status': 'recurring_candidate_verified', 'main_updated': False,
             'cas_attempts': 0, 'local_proposal_only': True, 'apply_ready': False,
             'schedule_enabled': False, 'asset_downloads': 0, 'notion_writes': 0,
             'confirmed_content_ids': 0, 'opaque_source_fields_rendered': False,
             'complete_source_rendering_claimed': False, 'readback_verified': True,
             'sink_status': 'not_requested', 'policy_sha256': policy['get_policy_sha256']}
    keys = set(fixed) | set(entry.engine.COUNTS) | {k for k, _ in entry.engine.HASHES}
    keys |= {'receipt_commit', 'receipt_sha256', 'production_activated'}
    if (not isinstance(value, dict) or set(value) != keys
            or any(type(value[k]) is not type(v) or value[k] != v for k, v in fixed.items())
            or type(value['production_activated']) is not bool):
        fail('provenance_prepare_only')
    for key, width in entry.engine.HASHES:
        a.checked(value[key], width)
    for key in entry.engine.COUNTS:
        if type(value[key]) is not int or not 0 <= value[key] <= m.MAX_CHANGED_BYTES:
            fail('provenance_prepare_only')
    if value['new_selected'] > 50 or value['notes_changed'] != sum(value[k] for k in (
            'new_created', 'new_preview_imported', 'new_updated', 'legacy_source_updated')):
        fail('provenance_prepare_only')
    if value['receipt_sha256'] is None:
        if value['receipt_commit'] is not None:
            fail('provenance_prepare_only')
    else:
        a.checked(value['receipt_sha256'], 64); a.checked(value['receipt_commit'], 40)
    return True


def check_run_origin(run, policy):
    if not isinstance(run, dict):
        fail('provenance_run')
    positive(run.get('id')); positive(run.get('run_attempt'))
    repo = run.get('repository'); head_repo = run.get('head_repository')
    if any(not isinstance(x, dict) or x.get('id') != policy['repository_id'] or x.get('full_name') != REPOSITORY or x.get('private') is not False for x in (repo, head_repo)):
        fail('provenance_run')
    if run.get('workflow_id') != policy['workflow_id'] or run.get('path') != policy['workflow_path'] or run.get('head_branch') != policy['head_branch']:
        fail('provenance_run')
    a.checked(run.get('head_sha'), 40)
    timestamp(run.get('created_at')); timestamp(run.get('updated_at'))
    return run


def check_run_identity(run, policy):
    check_run_origin(run, policy)
    if run.get('event') not in ('workflow_dispatch', 'schedule'):
        fail('provenance_run')
    return run

def check_run(run, policy):
    check_run_identity(run, policy)
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        fail('provenance_run')
    return run


def run_identity(run):
    # Attempt status/times may advance, but a rerun cannot change its origin C.
    return {k: run.get(k) for k in ('id', 'workflow_id', 'path', 'head_sha',
                                   'head_branch', 'event')}


def run_binding(run):
    result = {k: run.get(k) for k in ('id', 'run_attempt', 'workflow_id', 'path', 'head_sha', 'head_branch',
                                   'event', 'status', 'conclusion', 'created_at', 'updated_at')}
    # Preserve the actual raw presence. Historical reduced bindings cannot
    # infer a missing started clock and cannot qualify for an exact witness.
    if 'run_started_at' in run:
        result['run_started_at'] = run['run_started_at']
    return result


def run_semantic_binding(run):
    # List/latest and exact-attempt endpoints have distinct timestamp clocks.
    # created_at is separately bounded to the observed one-second endpoint skew.
    # Compare all other binding fields across endpoints; same-endpoint rereads
    # must still use run_binding, including updated_at, without any tolerance.
    return {k: v for k, v in run_binding(run).items() if k not in ('created_at', 'updated_at')}


def verified_candidate(client, listed, policy, *, run_attempt=None, verified_controllers=None, rejected_controllers=None, include_verified_details=False):
    """Prove one original successful attempt, never a later attempt's result."""
    if type(include_verified_details) is not bool:
        fail('provenance_record')
    check_run_identity(listed, policy)
    run_id = positive(listed['id'])
    attempt = positive(listed['run_attempt'] if run_attempt is None else run_attempt)
    if attempt > listed['run_attempt']:
        fail('provenance_run_changed')
    root = '/actions/runs/' + str(run_id)
    attempt_path = root + '/attempts/' + str(attempt)
    run = check_run_identity(client.json(attempt_path), policy)
    clock_witness = recovery.run53_recovery.clock_witness(run, listed)
    if (run['run_attempt'] != attempt or run_identity(run) != run_identity(listed)
            or (not timedelta(0) <= timestamp(run['created_at']) - timestamp(listed['created_at']) <= timedelta(seconds=1)
                and clock_witness is None)
            or attempt == listed['run_attempt'] and run_semantic_binding(run) != run_semantic_binding(listed)):
        fail('provenance_run_changed')
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        return None
    candidate_policy, original_recovery = recovery.candidate_policy(policy, run)
    try:
        # One discovery may encounter the same immutable controller repeatedly.
        # Deduplicate only after exact deterministic-byte verification. The
        # client's immutable-byte cache may span calls; the policy/event key
        # below also supports injected fixture clients without that cache.
        controller_key = (run['head_sha'], run['event'], a.sha(a.encoded(candidate_policy)))
        if verified_controllers is None or controller_key not in verified_controllers:
            controller.verify_remote(client, run['head_sha'], candidate_policy, event=run['event'])
            if verified_controllers is not None:
                verified_controllers.add(controller_key)
    except ProvenanceFailure as exc:
        if str(exc) == 'provenance_controller_mismatch':
            if rejected_controllers is not None:
                rejected_controllers.add(controller_key)
            return None  # Historical other code is never an anchor.
        raise  # Missing/denied/malformed provenance remains fail-closed.
    result = client.json(root + '/attempts/' + str(attempt) + '/jobs?per_page=100&page=1')
    jobs = result.get('jobs') if isinstance(result, dict) else None
    if not isinstance(jobs, list) or type(result.get('total_count')) is not int or result['total_count'] != len(jobs) or len(jobs) > 100:
        fail('provenance_job_bound')
    matches = [j for j in jobs if isinstance(j, dict) and j.get('name') == policy['job_name']]
    if not matches:
        return None  # Same fixed workflow may have a non-publication mode.
    if len(matches) != 1:
        fail('provenance_job')
    job = matches[0]; positive(job.get('id'))
    if job.get('run_id') != run_id or job.get('head_sha') != run['head_sha'] or job.get('status') != 'completed' or job.get('conclusion') != 'success':
        fail('provenance_job')
    if job.get('run_attempt', attempt) != attempt:
        fail('provenance_job')
    steps = job.get('steps')
    if not isinstance(steps, list) or len(steps) > 100:
        fail('provenance_job')
    emission = [s for s in steps if isinstance(s, dict) and s.get('name') == policy['publication_step_name']]
    if not emission or len(emission) == 1 and emission[0].get('conclusion') == 'skipped':
        return None
    if len(emission) != 1 or emission[0].get('status') != 'completed' or emission[0].get('conclusion') != 'success':
        fail('provenance_job')
    completed = timestamp(job.get('completed_at'))
    if completed > timestamp(run['updated_at']):
        fail('provenance_timestamp')
    raw = client.logs(job['id'])
    raw = private_proof.expand_for(client, raw, policy=candidate_policy, run=run, kinds=('publication',))
    if verified_prepare_only(raw, candidate_policy, run, job, emission[0]):
        record = None
    else:
        record = parse_record(raw, candidate_policy, run)
    if original_recovery:
        if record is None:
            fail('provenance_record')
        recovery.verify_original_record(record, job['id'], job=job, raw=raw, run=run)
    # Re-read the *original* attempt, then ensure latest is still the same run.
    # A failed/in-progress later rerun cannot erase this successful publication,
    # or inherit it: the record remains bound to the original attempt and job.
    original = check_run(client.json(attempt_path), policy)
    if run_binding(original) != run_binding(run):
        fail('provenance_run_changed')
    current = check_run_identity(client.json(root), policy)
    if (original_recovery and run_id == recovery.run57_recovery.RUN
            and (run_binding(listed) != recovery.run57_recovery.RUN_BINDING
                or run_binding(current) != recovery.run57_recovery.RUN_BINDING)):
        fail('provenance_run_changed')
    if clock_witness is not None and recovery.run53_recovery.clock_witness(run, current) != clock_witness:
        fail('provenance_run_changed')
    if (run_identity(current) != run_identity(run)
            or current['created_at'] != listed['created_at']
            or current['run_attempt'] < listed['run_attempt']
            or timestamp(current['updated_at']) < timestamp(listed['updated_at'])
            or current['run_attempt'] == attempt and run_semantic_binding(current) != run_semantic_binding(run)):
        fail('provenance_run_changed')
    if record is None:
        return None  # Strict prepare proof was rechecked against the original attempt.
    return {'anchor': {'commit': record['receipt_commit'], 'receipt_sha256': record['receipt_sha256']},
            'receipt_sequence': record['receipt_sequence'], 'record_sha256': record['record_sha256'],
            'run_id': run_id, 'run_attempt': attempt, 'job_id': job['id'],
            'completed_at': job['completed_at'],
            **({'verified_details': {'run_binding': run_binding(run),
                 'job_sha256': _history_job_sha256(job), 'record': deepcopy(record)}}
               if include_verified_details else {}),
            **({'clock_witness': clock_witness} if clock_witness is not None else {})}


def _history_job_sha256(job):
    # Preserve the ORIGINAL idle registration codec, which has no trailing LF.
    import get_notion_outbox as codec
    return codec.digest(job)


def history_witness_required(anchor, receipt):
    """Selection only, never trust: only edges incident to the pinned receipt."""
    value = HISTORY_WITNESS
    if value is None:
        return False
    if not isinstance(value, dict) or not isinstance(value.get('get_record'), dict):
        fail('provenance_history_witness')
    commit = value['get_record'].get('receipt_commit')
    return anchor['commit'] == commit or receipt.get('previous_receipt_commit') == commit


def _history_witness_registration(policy):
    import get_notion_idle_recovery as idle
    value = deepcopy(HISTORY_WITNESS)
    keys = {'public_policy', 'public_policy_sha256', 'run_binding',
            'job_id', 'job_sha256', 'get_record'}
    if not isinstance(value, dict) or set(value) != keys:
        fail('provenance_history_witness')
    old = validate_policy(value['public_policy'], value['public_policy_sha256'])
    run = value['run_binding']; record = value['get_record']
    if (not isinstance(run, dict) or run_binding(run) != run
            or type(run.get('id')) is not int or run['id'] != 39803282386
            or type(run.get('run_attempt')) is not int or run['run_attempt'] != 1
            or run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or not idle._forward_policy(policy, {'public_policy': old})):
        fail('provenance_history_witness')
    positive(value['job_id']); a.checked(value['job_sha256'], 64)
    parse_record(publication_line(record).encode(), old, run)
    if (record['receipt_sequence'] != 2 or record['main_updated'] is not False
            or record['cas_attempts'] != 0 or record['readback_verified'] is not True
            or record['observed_main_commit'] == record['receipt_commit']):
        fail('provenance_history_witness')
    return value, old, run


def _history_witness_from_verified(value, verified):
    run = value['run_binding']; record = value['get_record']
    if (verified is None or verified['job_id'] != value['job_id']
            or a.encoded(verified['verified_details']) != a.encoded({'run_binding': run,
                'job_sha256': value['job_sha256'], 'record': record})):
        fail('provenance_history_witness')
    return entry.VerifiedHistoryWitness(record['receipt_commit'],
        record['receipt_sha256'], record['receipt_sequence'],
        record['observed_main_commit'], record['record_sha256'])



def verify_history_witness(client, policy, *, candidate_runs_checked):
    """Fresh authentication within the SAME four-candidate and transport budget."""
    if type(candidate_runs_checked) is not int or not 0 <= candidate_runs_checked < MAX_CANDIDATES:
        fail('provenance_candidate_bound')
    value, old, run = _history_witness_registration(policy)
    listed = check_run_identity(client.json('/actions/runs/' + str(run['id'])), old)
    verified = verified_candidate(client, listed, old,
        run_attempt=run['run_attempt'], include_verified_details=True)
    return _history_witness_from_verified(value, verified)


def discover(client, policy, *, exclude_run_id=None, exclude_run_attempt=None, reserve_next_run=False, capture_history_witness=False):
    """Original four-candidate discovery; optional admission reserves one slot."""
    if type(reserve_next_run) is not bool or type(capture_history_witness) is not bool: fail('provenance_exclusion')
    captured_witness = None
    candidate_limit = MAX_CANDIDATES - int(reserve_next_run)
    if (exclude_run_id is None) != (exclude_run_attempt is None):
        fail('provenance_exclusion')
    if exclude_run_id is not None:
        positive(exclude_run_id); positive(exclude_run_attempt)
    repo = client.json('')
    if not isinstance(repo, dict) or repo.get('full_name') != REPOSITORY or repo.get('id') != policy['repository_id'] or repo.get('private') is not False:
        fail('provenance_repository')
    workflow = client.json('/actions/workflows/' + str(policy['workflow_id']))
    if not isinstance(workflow, dict) or workflow.get('id') != policy['workflow_id'] or workflow.get('path') != policy['workflow_path']:
        fail('provenance_workflow')
    listed = []; seen = set()
    for page in range(1, MAX_RUN_PAGES + 1):
        value = client.json('/actions/workflows/' + str(policy['workflow_id']) + '/runs?per_page=' + str(RUN_PAGE_SIZE) + '&page=' + str(page))
        rows = value.get('workflow_runs') if isinstance(value, dict) else None
        if not isinstance(rows, list) or len(rows) > RUN_PAGE_SIZE or type(value.get('total_count')) is not int or value['total_count'] < len(rows):
            fail('provenance_run_bound')
        for row in rows:
            if not isinstance(row, dict):
                fail('provenance_run')
            ident = positive(row.get('id'))
            if ident in seen:
                fail('provenance_run_changed')
            seen.add(ident)
            if row.get('head_branch') != policy['head_branch']:
                continue
            # A failed first push attempt is not an eligible publication event.
            # GitHub records workflow-parse failures this way even for a workflow
            # whose only permitted triggers are dispatch/schedule. Authenticate
            # its full origin/shape before negative selection; never use it as
            # anchor evidence, and never hide reruns, unknown events or statuses.
            if (row.get('event') == 'push' and type(row.get('run_attempt')) is int
                    and row['run_attempt'] == 1 and row.get('status') == 'completed'
                    and row.get('conclusion') == 'failure'):
                check_run_origin(row, policy)
                continue
            listed.append(check_run_identity(row, policy))
        if len(rows) < RUN_PAGE_SIZE:
            break
    listed.sort(key=lambda x: (timestamp(x['updated_at']), x['id']), reverse=True)
    found = []; checked = 0; verified_controllers = set(); rejected_controllers = set()
    for run in listed:
        # A first attempt listed as terminal failure cannot contain a successful
        # publication. This is negative selection only, never anchor evidence.
        # Reruns remain eligible: an earlier successful attempt must not vanish.
        if (run['run_attempt'] == 1 and run.get('status') == 'completed'
                and run.get('conclusion') in ('failure', 'cancelled', 'timed_out', 'skipped', 'action_required', 'neutral', 'stale')):
            continue
        # Read attempts in descending order, never fabricate an older success
        # from latest metadata. Failed/nonpublication attempts consume the same
        # four-candidate bound. A verified publication ends this run's search.
        for attempt in range(run['run_attempt'], 0, -1):
            if run['id'] == exclude_run_id and attempt == exclude_run_attempt:
                continue
            capture = (capture_history_witness and HISTORY_WITNESS is not None
                       and run['id'] == 39803282386 and attempt == 1)
            # Lazy policy validation: ordinary non-witness runs remain original.
            witness_info = _history_witness_registration(policy) if capture else None
            verification_policy = witness_info[1] if capture else policy
            # Deduplicate only a controller mismatch actually verified earlier
            # in this discovery. The immutable head/event/policy key proves the
            # same first successful attempt cannot become an eligible anchor.
            # Registered old policy keys differ; reruns/unknown states never skip.
            if (type(run.get('run_attempt')) is int and run['run_attempt'] == 1
                    and attempt == 1 and run.get('status') == 'completed'
                    and run.get('conclusion') == 'success'):
                selected_policy, _ = recovery.candidate_policy(verification_policy, run)
                negative_key = (run['head_sha'], run['event'], a.sha(a.encoded(selected_policy)))
                if negative_key in rejected_controllers:
                    continue
            if checked >= candidate_limit:
                break
            checked += 1
            value = verified_candidate(client, run, verification_policy, run_attempt=attempt,
                                       verified_controllers=verified_controllers,
                                       rejected_controllers=rejected_controllers,
                                       include_verified_details=capture)
            if value is not None:
                if capture:
                    registration, _, _ = _history_witness_registration(policy)
                    captured_witness = _history_witness_from_verified(registration, value)
                    value = {key: item for key, item in value.items() if key != 'verified_details'}
                if reserve_next_run:
                    # Existence admission only, never publication authority.
                    # Full original attempt/controller/log/alias checks above
                    # have completed. Canonical selection below is unchanged.
                    return {'status': 'public_provenance_capacity_verified',
                            'candidate_runs_checked': checked}
                found.append(value)
                break
        if checked >= candidate_limit:
            break
    if not found:
        fail('provenance_no_verified_publication')
    by_sequence = {}
    for value in found:
        sequence = value['receipt_sequence']
        if sequence in by_sequence and by_sequence[sequence] != value['anchor']:
            fail('provenance_receipt_fork')
        by_sequence[sequence] = value['anchor']
    chosen = max(found, key=lambda x: (x['receipt_sequence'], timestamp(x['completed_at']), x['run_id']))
    return {**chosen, **({'history_witness': captured_witness} if capture_history_witness else {}), 'status': 'public_provenance_verified', 'candidate_runs_checked': checked,
            'publication_records_verified': len(found), 'new_permissions': 0, 'credentials_used': getattr(client, 'credentials_used', 0)}


def verify_execution(client, policy, execution, trigger):
    """Bind the executing pinned A to the exact reviewed controller at C."""
    def reject(fields, row=None, reason='provenance_execution'):
        # Fixed field names and bounded public enums only; never dump a body,
        # identity, URL, token, exception text or arbitrary remote string.
        status = row.get('status') if isinstance(row, dict) else None
        conclusion = row.get('conclusion') if isinstance(row, dict) else None
        known_status = ('queued', 'in_progress', 'completed', 'waiting', 'pending', 'requested')
        known_conclusion = ('success', 'failure', 'cancelled', 'timed_out', 'skipped', 'neutral', 'action_required', 'stale')
        diagnostic = {'contract': 'get-execution-rejection-1', 'fields': fields,
                      'status': status if type(status) is str and status in known_status else 'other',
                      'conclusion': conclusion if type(conclusion) is str and conclusion in known_conclusion else 'none' if conclusion is None else 'other'}
        try:
            print(json.dumps(diagnostic, sort_keys=True), file=sys.stderr)
        except Exception:
            pass  # Observability must never authorize or change the failure.
        fail(reason)
    run_id = positive(execution['run_id']); attempt = positive(execution['run_attempt'])
    if execution['code_sha'] != policy['code_sha']:
        reject(['code_sha'])
    expected = {'id': run_id, 'run_attempt': attempt, 'workflow_id': policy['workflow_id'],
                'path': policy['workflow_path'], 'head_branch': policy['head_branch'],
                'head_sha': execution['controller_sha'],
                'event': 'schedule' if trigger == 'schedule' else 'workflow_dispatch'}
    for read_number in range(3):
        row = client.json('/actions/runs/' + str(run_id) + '/attempts/' + str(attempt))
        repo = row.get('repository') if isinstance(row, dict) else None
        head_repo = row.get('head_repository') if isinstance(row, dict) else None
        if any(not isinstance(value, dict) or value.get('id') != policy['repository_id'] or value.get('full_name') != REPOSITORY or value.get('private') is not False for value in (repo, head_repo)):
            reject(['repository_identity'], row)
        mismatches = [key for key, value in expected.items() if row.get(key) != value]
        if mismatches:
            reject(mismatches, row)
        if row.get('status') == 'in_progress' and row.get('conclusion') is None:
            break
        # A stale queued observation never authorizes work. Re-read only the
        # same fully matched attempt, within the original shared transport caps.
        if row.get('status') != 'queued' or row.get('conclusion') is not None or read_number == 2:
            reject(['status', 'conclusion'], row)
        delay = (5, 10)[read_number]
        if client.elapsed + delay >= MAX_SECONDS:
            reject(['wait_budget'], row, 'provenance_budget')
        started = client.clock()
        try:
            client.sleep(delay)
        finally:
            client.elapsed += max(0, client.clock() - started)
        if client.elapsed >= MAX_SECONDS:
            reject(['wait_budget'], row, 'provenance_budget')
    controller.verify_remote(client, execution['controller_sha'], policy, event=row['event'])


def prepare_with_public_provenance(reader, evidence, parent, source, client, policy, policy_sha256, *, bootstrap=entry.BOOTSTRAP, trigger='schedule', preview=None, exclude_run_id=None, exclude_run_attempt=None):
    """Automatic subsequent-run anchor acquisition; first bootstrap stays exact."""
    policy = validate_policy(policy, policy_sha256)
    if policy['rules_sha256'] != entry.rules_sha256() or policy['get_policy_sha256'] != entry.policy_sha256(policy['rules_sha256'], bootstrap):
        fail('provenance_policy')
    if entry.FULL_RECEIPT not in reader.tree(parent):
        # This does not infer adoption from absence: entry.prepare proves the
        # exact immutable bootstrap and rejects any intervening Get mutation.
        return entry.prepare(reader, evidence, parent, source, approved_rules_sha256=policy['rules_sha256'],
                             preview=preview, bootstrap=bootstrap, trigger=trigger)
    verified = discover(client, policy, exclude_run_id=exclude_run_id, exclude_run_attempt=exclude_run_attempt, capture_history_witness=True)
    receipt = entry.read_receipt(reader, verified['anchor']['commit'], verified['anchor']['receipt_sha256'], policy['get_policy_sha256'])
    if receipt['sequence'] != verified['receipt_sequence']:
        fail('provenance_receipt_sequence')
    history_witness = (verified.get('history_witness') or verify_history_witness(client, policy, candidate_runs_checked=verified['candidate_runs_checked'])
        if history_witness_required(verified['anchor'], receipt) else None)
    return entry.prepare(reader, evidence, parent, source, approved_rules_sha256=policy['rules_sha256'],
                         anchor=verified['anchor'], bootstrap=bootstrap, trigger=trigger, history_witness=history_witness)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--approved-policy-sha256', required=True)
    parser.add_argument('--exclude-run-id', type=int)
    parser.add_argument('--exclude-run-attempt', type=int)
    args = parser.parse_args(argv)
    try:
        policy = validate_policy(m.strict_capture_json(a.bounded_file(args.policy, 16384), 16384), args.approved_policy_sha256)
        value = discover(AnonymousGithub(), policy, exclude_run_id=args.exclude_run_id, exclude_run_attempt=args.exclude_run_attempt)
        # Only numeric run metadata and hashes. No job log content/URLs or IDs
        # of Get notes are retained or emitted.
        print(json.dumps(value)); return 0
    except Exception:
        print(json.dumps({'status': 'public_provenance_failed', 'new_permissions': 0, 'credentials_used': 0})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
