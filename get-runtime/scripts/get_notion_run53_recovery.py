"""Exact #53/attempt 2 verified-prefix bridge; no new plan or remote effects.

All pins are independently observed public hashes or hash-only private state
bindings. The private journal and page/content IDs are deliberately not included.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy

# A separate independent review must activate this one-way deployment bridge.
ACTIVATED = True
TAG = 'exact_run53_verified_batch_resume_v1'
RUN = _private_bindings.value('get_notion_run53_recovery', 'RUN')
ATTEMPT = _private_bindings.value('get_notion_run53_recovery', 'ATTEMPT')
JOB = _private_bindings.value('get_notion_run53_recovery', 'JOB')
JOB_SHA256 = _private_bindings.value('get_notion_run53_recovery', 'JOB_SHA256')
PUBLIC_POLICY = _private_bindings.value('get_notion_run53_recovery', 'PUBLIC_POLICY')
PUBLIC_SHA = _private_bindings.value('get_notion_run53_recovery', 'PUBLIC_SHA')
NOTION_POLICY = _private_bindings.value('get_notion_run53_recovery', 'NOTION_POLICY')
CHECKPOINT = _private_bindings.value('get_notion_run53_recovery', 'CHECKPOINT')
GET_RECORD = _private_bindings.value('get_notion_run53_recovery', 'GET_RECORD')
ANCHOR = _private_bindings.value('get_notion_run53_recovery', 'ANCHOR')
STATE_BYTES = _private_bindings.value('get_notion_run53_recovery', 'STATE_BYTES')
PLAN_SHA = _private_bindings.value('get_notion_run53_recovery', 'PLAN_SHA')
PLAN_BYTES = _private_bindings.value('get_notion_run53_recovery', 'PLAN_BYTES')
OP_SHA = _private_bindings.value('get_notion_run53_recovery', 'OP_SHA')
ENGINE_RULES = _private_bindings.value('get_notion_run53_recovery', 'ENGINE_RULES')
VERIFIED_BATCHES = _private_bindings.value('get_notion_run53_recovery', 'VERIFIED_BATCHES')
TOTAL_BATCHES = _private_bindings.value('get_notion_run53_recovery', 'TOTAL_BATCHES')
PREFIX_SHA = _private_bindings.value('get_notion_run53_recovery', 'PREFIX_SHA')
PAGE_SHA = _private_bindings.value('get_notion_run53_recovery', 'PAGE_SHA')
PROPERTIES_SHA = _private_bindings.value('get_notion_run53_recovery', 'PROPERTIES_SHA')
ATTEMPT_CLOCK = _private_bindings.value('get_notion_run53_recovery', 'ATTEMPT_CLOCK')
ALIAS_CLOCK = _private_bindings.value('get_notion_run53_recovery', 'ALIAS_CLOCK')


def clock_witness(attempt, alias):
    """One exact directional endpoint discrepancy, never a wider clock limit.

    Require every checked raw field, including run_started_at. Missing values
    in older reduced bindings are not inferred from the other endpoint.
    """
    if not ACTIVATED or not isinstance(attempt, dict) or not isinstance(alias, dict):
        return None
    if any(k not in attempt or k not in alias for k in ATTEMPT_CLOCK):
        return None
    before = {k: attempt[k] for k in ATTEMPT_CLOCK}
    after = {k: alias[k] for k in ALIAS_CLOCK}
    if before != ATTEMPT_CLOCK or after != ALIAS_CLOCK:
        return None
    return {'attempt': before, 'alias': after}


def allows(plan, journal):
    """Only this unchanged plan and authenticated forward-prefix checkpoints.

    Callers must first authenticate the journal externally. This is never a
    preparation exception or authority to adopt unknown remote effects.
    """
    import get_notion_executor as e
    try:
        if (not ACTIVATED or not isinstance(plan, dict) or not isinstance(journal, dict)
                or plan.get('rules_sha256') != ENGINE_RULES
                or e.digest(plan) != PLAN_SHA or len(e.encoded(plan)) != PLAN_BYTES
                or journal.get('plan_sha256') != PLAN_SHA
                or e.encoded(journal.get('plan')) != e.encoded(plan)
                or len(plan['records']) != 1
                or plan['records'][0].get('action') != 'create'
                or len(plan['records'][0]['create_batches']['batches']) != TOTAL_BATCHES
                or type(journal.get('index')) is not int):
            return False
        if journal['index'] == 1:
            return journal.get('operation') is None
        if journal['index'] != 0:
            return False
        operation = journal['operation']
        e.create_batches.validate_operation(operation, plan['records'][0])
        return (operation['batch_index'] >= VERIFIED_BATCHES - 1
                and e.digest(operation['page_id']) == PAGE_SHA
                and operation['properties_sha256'] == PROPERTIES_SHA
                and len(operation['batches']) >= VERIFIED_BATCHES
                and all(b['phase'] == 'verified' for b in operation['batches'][:VERIFIED_BATCHES])
                and e.digest(operation['batches'][:VERIFIED_BATCHES]) == PREFIX_SHA)
    except Exception:
        return False


def recover_checkpoint(client, run_id, run_attempt):
    import get_notion_journal_runtime as c
    if (not ACTIVATED or type(run_id) is not int or type(run_attempt) is not int
            or (run_id, run_attempt) != (RUN, ATTEMPT)):
        c.fail('journal_provenance_other_controller')
    result = c.recover_anchor(client, run_id=RUN, run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY), approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY, require_success=True)
    if (result.get('anchor') != ANCHOR or result.get('job_id') != JOB
            or result.get('job_sha256') != JOB_SHA256
            or result.get('checkpoint_count') != 9
            or result.get('checkpoint') != CHECKPOINT
            or result.get('get_step_record') != GET_RECORD
            or result.get('private_readback_required') is not True
            or result.get('clock_witness') != {'attempt': ATTEMPT_CLOCK, 'alias': ALIAS_CLOCK}):
        c.fail('journal_provenance_record')
    return {**result, 'recovery_anchor': TAG}


def validate_state(state, sha):
    import get_notion_outbox as o
    import get_private_preview_retention as retention
    try:
        o.check_state(state)
        raw = o.encoded(state)
        journal = state['engine_state']['journal']
        operation = journal['operation']
        return (sha == ANCHOR['state_sha256'] and o.digest(state) == sha
                and len(raw) == STATE_BYTES and retention.oid(raw) == ANCHOR['state_blob']
                and state['policy_sha256'] == PUBLIC_POLICY['get_policy_sha256']
                and allows(journal['plan'], journal) and journal['index'] == 0
                and operation['phase'] == 'verified'
                and operation['batch_index'] == VERIFIED_BATCHES - 1
                and len(operation['batches']) == VERIFIED_BATCHES
                and o.digest(operation) == OP_SHA)
    except Exception:
        return False


def transition_outer(state, sha, new_get_policy_sha256):
    """Change ONLY the outer policy, before ordinary confirmed CAS/checkpoint."""
    import get_notion_outbox as o
    import get_recurring_policy_recovery as recovery
    if not validate_state(state, sha) or new_get_policy_sha256 != recovery.current_get_policy():
        o.fail('outbox_state_invalid')
    if new_get_policy_sha256 == state['policy_sha256']:
        o.fail('outbox_state_invalid')
    result = deepcopy(state)
    result['policy_sha256'] = new_get_policy_sha256
    o.check_state(result)
    return result
