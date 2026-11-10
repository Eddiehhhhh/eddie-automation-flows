"""Read-only, exact Get publication evidence for a possible Notion-only route.

This module grants NO write, journal, policy-migration or receipt-chain authority.
The caller must supply an independently reviewed source policy, not derive its
approval from user input. Ordinary successful runs use the existing verifier.
The additional case requires a terminal failed Notion step AFTER a successful
Get publication, with the original controller, attempt, logs and readback intact.
There is deliberately no CLI, workflow registration, source client or write API.
"""
from copy import deepcopy
import re

import get_publication_provenance as p

CONTRACT = 'get-notion-replay-source-observation-1'
NOTION_STEP = 'Sync confirmed Get content to Notion'
SELECTION_KEYS = frozenset(('run_id', 'run_attempt', 'record_sha256',
                            'receipt_commit', 'receipt_sha256'))
FAILURES = frozenset(('failure', 'cancelled', 'timed_out'))


def selection(value):
    if not isinstance(value, dict) or set(value) != SELECTION_KEYS:
        p.fail('provenance_record')
    p.positive(value['run_id']); p.positive(value['run_attempt'])
    p.a.checked(value['receipt_commit'], 40)
    for key in ('record_sha256', 'receipt_sha256'): p.a.checked(value[key], 64)
    return deepcopy(value)


def _selected_record(record, selected):
    if any(record.get(key) != selected[key] for key in SELECTION_KEYS):
        p.fail('provenance_record')
    return record


def _interval(value, outer):
    start, end = p.timestamp(value.get('started_at')), p.timestamp(value.get('completed_at'))
    if not outer[0] <= start <= end <= outer[1]: p.fail('provenance_timestamp')
    return start, end


def _job(result, run, policy):
    jobs = result.get('jobs') if isinstance(result, dict) else None
    if (not isinstance(jobs, list) or type(result.get('total_count')) is not int
            or result['total_count'] != len(jobs) or len(jobs) > 100):
        p.fail('provenance_job_bound')
    matches = [row for row in jobs if isinstance(row, dict) and row.get('name') == policy['job_name']]
    if len(matches) != 1: p.fail('provenance_job')
    job = matches[0]; p.positive(job.get('id'))
    if (job.get('run_id') != run['id'] or job.get('run_attempt', run['run_attempt']) != run['run_attempt']
            or job.get('head_sha') != run['head_sha'] or job.get('status') != 'completed'
            or job.get('conclusion') not in FAILURES): p.fail('provenance_job')
    times = _interval(job, (p.timestamp(run['created_at']), p.timestamp(run['updated_at'])))
    steps = job.get('steps')
    if not isinstance(steps, list) or len(steps) > 100: p.fail('provenance_job')
    selected_steps = []
    for name, outcomes in ((policy['publication_step_name'], {'success'}), (NOTION_STEP, FAILURES)):
        matches = [row for row in steps if isinstance(row, dict) and row.get('name') == name]
        if len(matches) != 1: p.fail('provenance_job')
        row = matches[0]; p.positive(row.get('number'))
        if row.get('status') != 'completed' or row.get('conclusion') not in outcomes:
            p.fail('provenance_job')
        selected_steps.append((row, _interval(row, times)))
    (get_step, get_times), (notion_step, notion_times) = selected_steps
    if get_step['number'] >= notion_step['number'] or get_times[1] > notion_times[0]:
        p.fail('provenance_timestamp')
    return job, get_times, notion_step['conclusion']


def _failed_notion_source(client, run, policy, selected):
    # No boolean relaxes the ordinary verifier. This separate observation is
    # limited to an exact, terminal Get-success/Notion-failure pair.
    if run['status'] != 'completed' or run['conclusion'] not in FAILURES:
        p.fail('provenance_run')
    p.controller.verify_remote(client, run['head_sha'], policy, event=run['event'])
    route = '/actions/runs/' + str(run['id']) + '/attempts/' + str(run['run_attempt']) + '/jobs?per_page=100&page=1'
    job, times, notion_outcome = _job(client.json(route), run, policy)
    raw = client.logs(job['id'])
    if not isinstance(raw, bytes) or len(raw) > p.MAX_LOG: p.fail('provenance_budget')
    raw = p.private_proof.expand_for(client, raw, policy=policy, run=run, kinds=('publication',))
    try: lines = raw.decode('utf-8', 'strict').splitlines()
    except UnicodeError: p.fail('provenance_logs_shape')
    found = []
    for line in lines:
        match = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z) (.*)$', line)
        content = match[2] if match else line
        if not content.startswith(p.MARKER): continue
        if match is None or not times[0] <= p.timestamp(match[1]) <= times[1]:
            p.fail('provenance_timestamp')
        found.append(line)
    record = _selected_record(p.parse_record(('\n'.join(found) + '\n').encode(), policy, run), selected)
    # Immutable original job details must also survive an independent readback.
    final_job, _, final_outcome = _job(client.json(route), run, policy)
    if final_job != job or final_outcome != notion_outcome: p.fail('provenance_run_changed')
    return record, job['id'], notion_outcome


def verify(client, source_policy, approved_source_policy_sha256, selected):
    """Return original evidence unchanged; consumers still need Git readback.

    Selecting a different commit, receipt, attempt or record hash fails closed.
    A current/rerun writer is never selected, and no source discovery occurs.
    """
    policy = p.validate_policy(source_policy, approved_source_policy_sha256)
    selected = selection(selected)
    root = '/actions/runs/' + str(selected['run_id'])
    latest = p.check_run_identity(client.json(root), policy)
    if (latest['id'] != selected['run_id'] or latest['run_attempt'] != selected['run_attempt']
            or latest.get('status') != 'completed'):
        p.fail('provenance_run_changed')
    attempt_path = root + '/attempts/' + str(selected['run_attempt'])
    run = p.check_run_identity(client.json(attempt_path), policy)
    if (run['id'] != selected['run_id'] or run['run_attempt'] != selected['run_attempt']
            or p.run_identity(run) != p.run_identity(latest)
            or p.run_semantic_binding(run) != p.run_semantic_binding(latest)
            or not p._attempt_clock_matches(run, latest, selected['run_attempt'])):
        p.fail('provenance_run_changed')
    if run.get('status') == 'completed' and run.get('conclusion') == 'success':
        proof = p.verified_candidate(client, latest, policy,
            run_attempt=selected['run_attempt'], include_verified_details=True)
        if proof is None: p.fail('provenance_unconfirmed')
        record = _selected_record(proof['verified_details']['record'], selected)
        job_id = proof['job_id']; outcome = 'not_checked_by_publication_verifier'
    else:
        record, job_id, outcome = _failed_notion_source(client, run, policy, selected)
    if (p.run_binding(p.check_run_identity(client.json(attempt_path), policy)) != p.run_binding(run)
            or p.run_binding(p.check_run_identity(client.json(root), policy)) != p.run_binding(latest)):
        p.fail('provenance_run_changed')
    return {'contract': CONTRACT, 'write_ready': False, 'record': deepcopy(record),
        'source_run_id': selected['run_id'], 'source_run_attempt': selected['run_attempt'],
        'source_job_id': job_id, 'source_policy_sha256': approved_source_policy_sha256,
        'notion_outcome': outcome}
