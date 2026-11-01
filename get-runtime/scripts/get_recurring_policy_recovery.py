"""Two exact publication proofs for the production recovery code repairs.

These are external publication evidence, not values learned from a receipt or
repository state. This exception admits only the original sequence-one receipt
unchanged; all normal parent, complete-delta and ownership checks still run.
New receipts and public records always carry the executing runtime's policy.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
import get_notion_run32_recovery as run32_recovery
import get_notion_run53_recovery as run53_recovery
import get_notion_run57_recovery as run57_recovery
import get_notion_historical_active_recovery as historical_active
import get_notion_idle_recovery as idle_recovery

OLD_PUBLIC_POLICY_SHA256 = _private_bindings.value('get_recurring_policy_recovery', 'OLD_PUBLIC_POLICY_SHA256')
_OLD_PUBLIC_POLICY = _private_bindings.value('get_recurring_policy_recovery', '_OLD_PUBLIC_POLICY')
ORIGINAL_RUN_ID = _private_bindings.value('get_recurring_policy_recovery', 'ORIGINAL_RUN_ID')
ORIGINAL_RUN_ATTEMPT = _private_bindings.value('get_recurring_policy_recovery', 'ORIGINAL_RUN_ATTEMPT')
ORIGINAL_JOB_ID = _private_bindings.value('get_recurring_policy_recovery', 'ORIGINAL_JOB_ID')
ORIGINAL_CONTROLLER_SHA = _private_bindings.value('get_recurring_policy_recovery', 'ORIGINAL_CONTROLLER_SHA')
ORIGINAL_RECEIPT_COMMIT = _private_bindings.value('get_recurring_policy_recovery', 'ORIGINAL_RECEIPT_COMMIT')
ORIGINAL_RECEIPT_SHA256 = _private_bindings.value('get_recurring_policy_recovery', 'ORIGINAL_RECEIPT_SHA256')
ORIGINAL_RECORD_SHA256 = _private_bindings.value('get_recurring_policy_recovery', 'ORIGINAL_RECORD_SHA256')

# The later confirmed no-op refreshes provenance, not the original receipt.
# This policy/proof was independently verified before this recovery was built.
NOOP_PUBLIC_POLICY_SHA256 = _private_bindings.value('get_recurring_policy_recovery', 'NOOP_PUBLIC_POLICY_SHA256')
_NOOP_PUBLIC_POLICY = _private_bindings.value('get_recurring_policy_recovery', '_NOOP_PUBLIC_POLICY')
NOOP_RUN_ID = _private_bindings.value('get_recurring_policy_recovery', 'NOOP_RUN_ID')
NOOP_RUN_ATTEMPT = _private_bindings.value('get_recurring_policy_recovery', 'NOOP_RUN_ATTEMPT')
NOOP_JOB_ID = _private_bindings.value('get_recurring_policy_recovery', 'NOOP_JOB_ID')
NOOP_CONTROLLER_SHA = _private_bindings.value('get_recurring_policy_recovery', 'NOOP_CONTROLLER_SHA')
NOOP_RECORD_SHA256 = _private_bindings.value('get_recurring_policy_recovery', 'NOOP_RECORD_SHA256')


# Exact successful canonical publication from the first scheduled-anchor repair.
SEQUENCE_TWO_RECORD = _private_bindings.value('get_recurring_policy_recovery', 'SEQUENCE_TWO_RECORD')
SEQUENCE_TWO_JOB = _private_bindings.value('get_recurring_policy_recovery', 'SEQUENCE_TWO_JOB')
_SEQUENCE_TWO_POLICY = _private_bindings.value('get_recurring_policy_recovery', '_SEQUENCE_TWO_POLICY')
SEQUENCE_TWO_PUBLIC_SHA256 = _private_bindings.value('get_recurring_policy_recovery', 'SEQUENCE_TWO_PUBLIC_SHA256')

def sequence_two_policy():
    from get_recurring_entry import a
    if a.sha(a.encoded(_SEQUENCE_TWO_POLICY)) != SEQUENCE_TWO_PUBLIC_SHA256:
        raise ValueError('recovery_policy_changed')
    return deepcopy(_SEQUENCE_TWO_POLICY)


def old_public_policy():
    """Immutable externally approved old P, also used by the exact Notion repair."""
    from get_recurring_entry import a
    if a.sha(a.encoded(_OLD_PUBLIC_POLICY)) != OLD_PUBLIC_POLICY_SHA256:
        raise ValueError('recovery_policy_changed')
    return deepcopy(_OLD_PUBLIC_POLICY)



def noop_public_policy():
    """Exact independently approved policy of the successful run-sixteen manual no-op."""
    from get_recurring_entry import a
    if a.sha(a.encoded(_NOOP_PUBLIC_POLICY)) != NOOP_PUBLIC_POLICY_SHA256:
        raise ValueError('recovery_policy_changed')
    return deepcopy(_NOOP_PUBLIC_POLICY)


def run32_public_policy():
    """Exact completed #32 no-op; the original #26 receipt stays unchanged."""
    from get_recurring_entry import a
    if a.sha(a.encoded(run32_recovery.PUBLIC_POLICY)) != run32_recovery.PUBLIC_SHA:
        raise ValueError('recovery_policy_changed')
    return deepcopy(run32_recovery.PUBLIC_POLICY)


def current_get_policy():
    import get_recurring_entry as entry
    return entry.policy_sha256(entry.rules_sha256(), entry.BOOTSTRAP)


def accepts_receipt(commit, receipt_sha256, value, requested_policy):
    """No arbitrary old receipt, policy choice, reverse migration or relabeling."""
    if idle_recovery.accepts_receipt(commit, receipt_sha256, value, requested_policy):
        return True
    if (commit == SEQUENCE_TWO_RECORD['receipt_commit']
            and receipt_sha256 == SEQUENCE_TWO_RECORD['receipt_sha256']
            and type(value.get('sequence')) is int and value['sequence'] == 2
            and value.get('policy_sha256') == sequence_two_policy()['get_policy_sha256']
            and requested_policy == current_get_policy()):
        return True
    return (commit == ORIGINAL_RECEIPT_COMMIT
            and receipt_sha256 == ORIGINAL_RECEIPT_SHA256
            and type(value.get('sequence')) is int and value['sequence'] == 1
            and value.get('policy_sha256') == old_public_policy()['get_policy_sha256']
            and requested_policy == current_get_policy())


def candidate_policy(policy, run):
    """Only the fixed reviewed predecessors can use their exact old policy proof."""
    selected, admitted = historical_active.candidate_policy(policy, run)
    if admitted: return selected, admitted
    selected, admitted = idle_recovery.candidate_policy(policy, run)
    if admitted:
        return selected, admitted
    import get_recurring_entry as entry
    old = old_public_policy()
    run_id, attempt, controller = ORIGINAL_RUN_ID, ORIGINAL_RUN_ATTEMPT, ORIGINAL_CONTROLLER_SHA
    event = 'workflow_dispatch'
    if run.get('id') == NOOP_RUN_ID:
        old = noop_public_policy()
        run_id, attempt, controller = NOOP_RUN_ID, NOOP_RUN_ATTEMPT, NOOP_CONTROLLER_SHA
        event = 'workflow_dispatch'
    if run.get('id') == SEQUENCE_TWO_RECORD['run_id']:
        old = sequence_two_policy()
        run_id, attempt, controller = (SEQUENCE_TWO_RECORD[k] for k in ('run_id','run_attempt','controller_sha'))
    if run.get('id') == run32_recovery.RUN:
        old = run32_public_policy()
        run_id, attempt, controller = (run32_recovery.GET_RECORD[k] for k in ('run_id','run_attempt','controller_sha'))
    if run53_recovery.ACTIVATED and run.get('id') == run53_recovery.RUN:
        old = deepcopy(run53_recovery.PUBLIC_POLICY)
        run_id, attempt, controller = (run53_recovery.GET_RECORD[k] for k in ('run_id','run_attempt','controller_sha'))
        event = run53_recovery.ATTEMPT_CLOCK['event']
    if run57_recovery.ACTIVATED and run.get('id') == run57_recovery.RUN:
        old = deepcopy(run57_recovery.PUBLIC_POLICY)
        run_id, attempt, controller = (run57_recovery.GET_RECORD[k] for k in ('run_id','run_attempt','controller_sha'))
        event = run57_recovery.RUN_BINDING['event']
    from get_recurring_controller import template_sha256, historical_template_digests
    stable = set(old) - {'code_sha', 'rules_sha256', 'get_policy_sha256', 'controller_template_sha256'}
    template_ok = (policy.get('controller_template_sha256') == old['controller_template_sha256']
        or (policy.get('controller_template_sha256') == template_sha256()
            and old['controller_template_sha256'] in historical_template_digests()))
    if (template_ok and all(policy.get(k) == old[k] for k in stable)
            and policy.get('code_sha') != old['code_sha']
            and policy.get('rules_sha256') == entry.rules_sha256()
            and policy.get('get_policy_sha256') == current_get_policy()
            and run.get('id') == run_id
            and run.get('run_attempt') == attempt
            and run.get('head_sha') == controller
            and run.get('event') == event):
        return old, True
    return policy, False


def verify_original_record(record, job_id, *, job=None, raw=None, run=None):
    from get_publication_provenance import fail
    if historical_active.registered_run(record['run_id']):
        historical_active.verify_original_record(record, job_id, job=job, raw=raw, run=run)
        return
    if idle_recovery.registered_run(record['run_id']):
        idle_recovery.verify_original_record(record, job_id, job=job)
        return
    if record['run_id'] == run57_recovery.RUN:
        run57_recovery.verify_publication(record, job_id, job, raw, run)
        return
    if record['run_id'] == run53_recovery.RUN:
        if not run53_recovery.ACTIVATED or job_id != run53_recovery.JOB or record != run53_recovery.GET_RECORD:
            fail('provenance_record')
        return
    if record['run_id'] == run32_recovery.RUN:
        if job_id != run32_recovery.JOB or record != run32_recovery.GET_RECORD:
            fail('provenance_record')
        return
    if record['run_id'] == SEQUENCE_TWO_RECORD['run_id']:
        if job_id != SEQUENCE_TWO_JOB or record != SEQUENCE_TWO_RECORD:
            fail('provenance_record')
        return
    if record['run_id'] == NOOP_RUN_ID:
        expected_job, expected_record, updated = NOOP_JOB_ID, NOOP_RECORD_SHA256, False
        expected_attempt = NOOP_RUN_ATTEMPT
    elif record['run_id'] == ORIGINAL_RUN_ID:
        expected_job, expected_record, updated = ORIGINAL_JOB_ID, ORIGINAL_RECORD_SHA256, True
        expected_attempt = ORIGINAL_RUN_ATTEMPT
    else:
        fail('provenance_record')
    if (job_id != expected_job or record['record_sha256'] != expected_record
            or record['run_attempt'] != expected_attempt
            or record['receipt_commit'] != ORIGINAL_RECEIPT_COMMIT
            or record['receipt_sha256'] != ORIGINAL_RECEIPT_SHA256
            or record['receipt_sequence'] != 1 or record['main_updated'] is not updated):
        fail('provenance_record')
