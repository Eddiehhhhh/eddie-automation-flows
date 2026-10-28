"""Code-pinned canonical Get predecessors, independent of journal recovery.

No failed/skipped Get or fixed-archive record can become a publication. Records
are authenticated afresh by the unchanged original Actions/controller verifier.
No transport, budget, receipt or private state authority is added here.
"""
from copy import deepcopy
import get_private_runtime_bindings as bindings

KEYS = {'public_policy', 'public_policy_sha256', 'run_binding', 'job_id', 'job_sha256', 'get_record'}


def registry():
    import get_publication_provenance as p
    rows = bindings.value('get_publication_registered_predecessor', 'REGISTRY')
    if not isinstance(rows, list) or len(rows) > p.MAX_CANDIDATES:
        p.fail('provenance_record')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != KEYS: p.fail('provenance_record')
        old = p.validate_policy(row['public_policy'], row['public_policy_sha256'])
        run = row['run_binding']
        if (not isinstance(run, dict) or p.run_binding(run) != run
                or run.get('status') != 'completed' or run.get('conclusion') != 'success'
                or run.get('workflow_id') != old['workflow_id']
                or run.get('path') != old['workflow_path']
                or run.get('head_branch') != old['head_branch']
                or run.get('event') not in ('schedule', 'workflow_dispatch')):
            p.fail('provenance_record')
        key = (p.positive(run['id']), p.positive(run['run_attempt']))
        if key in seen: p.fail('provenance_record')
        seen.add(key); p.positive(row['job_id']); p.a.checked(row['job_sha256'], 64)
        # Full record schema/digest/context, not just an opaque receipt marker.
        record = p.parse_record(p.publication_line(row['get_record']).encode(), old, run)
        if record != row['get_record']: p.fail('provenance_record')
    return deepcopy(rows)


def candidate_policy(policy, run):
    import get_notion_checkpoint_resume as exact
    selected, matched = exact.source_candidate_policy(policy, run)
    if matched: return selected, True
    import get_publication_provenance as p
    import get_notion_idle_recovery as idle
    matches = [r for r in registry() if p.run_binding(run) == r['run_binding']]
    if len(matches) == 1 and idle._forward_policy(policy, matches[0]):
        return deepcopy(matches[0]['public_policy']), True
    return policy, False


def verify_original_record(record, job_id, *, job, run):
    import get_notion_checkpoint_resume as exact
    if exact.verify_source_record(record,job_id,job=job,run=run):return
    import get_publication_provenance as p
    import get_notion_outbox as o
    rows = [r for r in registry() if p.run_binding(run) == r['run_binding']]
    if (len(rows) != 1 or record != rows[0]['get_record']
            or type(job_id) is not int or job_id != rows[0]['job_id']
            or not isinstance(job, dict) or job.get('id') != job_id
            or o.digest(job) != rows[0]['job_sha256']):
        p.fail('provenance_record')


def discovery_locator(policy):
    """One already registered producer coordinate, never publication evidence.

    Discovery must fetch its current alias and reauthenticate the exact original
    attempt/controller/job/log/record. It consumes an ORIGINAL candidate slot and
    the SAME transport budget. No arbitrary caller/state/CLI locator is accepted.
    Old incompatible policies retain their original discovery behavior.
    """
    import get_publication_provenance as p
    import get_notion_idle_recovery as idle
    import get_notion_checkpoint_resume as exact
    coordinate=exact.source_discovery_locator(policy)
    if coordinate is not None:return coordinate
    eligible = [row for row in registry() if idle._forward_policy(policy, row)]
    if not eligible:
        return None
    row = max(eligible, key=lambda item: (
        item['get_record']['receipt_sequence'],
        p.timestamp(item['run_binding']['updated_at']),
        item['run_binding']['id'], item['run_binding']['run_attempt']))
    return (row['run_binding']['id'], row['run_binding']['run_attempt'])
