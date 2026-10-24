"""Exact #57 acknowledged-create continuation; no new plan or remote effects.

Only hash-only private state bindings appear here. All original receipts,
quarantine, immutable plan, source and acknowledged page remain untouched.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy

# Activate only after independent review of the exact authenticated evidence.
ACTIVATED = True
TAG = 'exact_run57_acknowledged_create_resume_v1'
RUN = _private_bindings.value('get_notion_run57_recovery', 'RUN')
ATTEMPT = _private_bindings.value('get_notion_run57_recovery', 'ATTEMPT')
JOB = _private_bindings.value('get_notion_run57_recovery', 'JOB')
JOB_SHA256 = _private_bindings.value('get_notion_run57_recovery', 'JOB_SHA256')
LOG_SHA256 = _private_bindings.value('get_notion_run57_recovery', 'LOG_SHA256')
LOG_BYTES = _private_bindings.value('get_notion_run57_recovery', 'LOG_BYTES')
PUBLIC_POLICY = _private_bindings.value('get_notion_run57_recovery', 'PUBLIC_POLICY')
PUBLIC_SHA = _private_bindings.value('get_notion_run57_recovery', 'PUBLIC_SHA')
NOTION_POLICY = _private_bindings.value('get_notion_run57_recovery', 'NOTION_POLICY')
CHECKPOINT = _private_bindings.value('get_notion_run57_recovery', 'CHECKPOINT')
GET_RECORD = _private_bindings.value('get_notion_run57_recovery', 'GET_RECORD')
ANCHOR = _private_bindings.value('get_notion_run57_recovery', 'ANCHOR')
RUN_BINDING = _private_bindings.value('get_notion_run57_recovery', 'RUN_BINDING')
STATE_BYTES = _private_bindings.value('get_notion_run57_recovery', 'STATE_BYTES')
PLAN_SHA = _private_bindings.value('get_notion_run57_recovery', 'PLAN_SHA')
PLAN_BYTES = _private_bindings.value('get_notion_run57_recovery', 'PLAN_BYTES')
OP_SHA = _private_bindings.value('get_notion_run57_recovery', 'OP_SHA')
ENGINE_RULES = _private_bindings.value('get_notion_run57_recovery', 'ENGINE_RULES')
TOTAL_BATCHES = _private_bindings.value('get_notion_run57_recovery', 'TOTAL_BATCHES')
ROW_SHA = _private_bindings.value('get_notion_run57_recovery', 'ROW_SHA')
PAGE_SHA = _private_bindings.value('get_notion_run57_recovery', 'PAGE_SHA')

PREFIX_ROW_SHAS = _private_bindings.value('get_notion_run57_recovery', 'PREFIX_ROW_SHAS')


def allows(plan, journal):
    """Exact acknowledged operation, then only its authenticated forward states.

    External checkpoint authentication is mandatory at callers. In particular,
    no other acknowledged first create is accepted merely because its page or
    plan matches. A verified first prefix must retain the original response;
    later batch phases remain under the normal guarded read/checkpoint engine.
    """
    import get_notion_executor as e
    try:
        if (not ACTIVATED or not isinstance(plan, dict) or not isinstance(journal, dict)
                or plan.get('rules_sha256') != ENGINE_RULES
                or e.digest(plan) != PLAN_SHA or len(e.encoded(plan)) != PLAN_BYTES
                or journal.get('plan_sha256') != PLAN_SHA
                or e.encoded(journal.get('plan')) != e.encoded(plan)
                or set(journal) != {'plan_sha256', 'plan', 'index', 'operation'}
                or len(plan['records']) != 1 or e.digest(plan['records'][0]) != ROW_SHA
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
        if e.digest(operation) == OP_SHA:
            return True
        # The only mutable fields of original batch zero are those populated by
        # a successful fresh strict prefix read. Its create response is fixed.
        first = operation['batches'][0]
        return (e.digest(operation['page_id']) == PAGE_SHA
                and first['phase'] == 'verified'
                and set(first) == {'phase', 'response', 'roots', 'blocks', 'prefix_sha256'}
                and first['response'] == operation['page_id']
                and operation['properties_sha256'] is not None)
    except Exception:
        return False


def creation_properties(row, page_id):
    """Readback-only projection for the two exact saved prefix rows/known page.

    Never use for a write request or fingerprint. The executor must already
    authorize the full exact plan/journal via allows before executing this row.
    """
    import get_notion_executor as e
    try:
        if not ACTIVATED or e.digest(row) not in PREFIX_ROW_SHAS or e.digest(page_id) != PAGE_SHA:
            return None
        props = e.properties
        return props.date_wire_properties(row['properties'], props.date_wire_binding(row['properties']))
    except Exception:
        return None


def verify_log(raw):
    import get_notion_journal_runtime as c
    if (not ACTIVATED or not isinstance(raw, bytes) or len(raw) != LOG_BYTES
            or c.codec.digest(raw) != LOG_SHA256):
        c.fail('journal_provenance_record')


def verify_publication(record, job_id, job, raw, run):
    import get_notion_journal_runtime as c
    verify_log(raw)
    if (job_id != JOB or c.codec.digest(job) != JOB_SHA256 or record != GET_RECORD
            or c.provenance.run_binding(run) != RUN_BINDING):
        c.provenance.fail('provenance_record')


def recover_checkpoint(client, run_id, run_attempt):
    import get_notion_journal_runtime as c
    if (not ACTIVATED or type(run_id) is not int or type(run_attempt) is not int
            or (run_id, run_attempt) != (RUN, ATTEMPT)):
        c.fail('journal_provenance_other_controller')
    class PinnedLogs:
        def json(self, path): return client.json(path)
        def logs(self, job):
            if job != JOB: c.fail('journal_provenance_record')
            raw = client.logs(job)
            verify_log(raw)
            return raw
    result = c.recover_anchor(PinnedLogs(), run_id=RUN, run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY), approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY, require_success=True)
    if (result.get('anchor') != ANCHOR or result.get('job_id') != JOB
            or result.get('job_sha256') != JOB_SHA256
            or result.get('checkpoint_count') != CHECKPOINT['sequence']
            or result.get('checkpoint') != CHECKPOINT
            or result.get('get_step_record') != GET_RECORD
            or result.get('private_readback_required') is not True
            or result.get('run_binding') != RUN_BINDING
            or result.get('latest_binding') != RUN_BINDING
            or result.get('clock_witness') is not None):
        c.fail('journal_provenance_record')
    return {**result, 'recovery_anchor': TAG}


def validate_state(state, sha):
    import get_notion_outbox as o
    import get_private_preview_retention as retention
    try:
        o.check_state(state)
        raw = o.encoded(state)
        journal = state['engine_state']['journal']
        return (sha == ANCHOR['state_sha256'] and o.digest(state) == sha
                and len(raw) == STATE_BYTES and retention.oid(raw) == ANCHOR['state_blob']
                and state['policy_sha256'] == PUBLIC_POLICY['get_policy_sha256']
                and allows(journal['plan'], journal) and journal['index'] == 0
                and o.digest(journal['operation']) == OP_SHA)
    except Exception:
        return False


def transition_outer(state, sha, new_get_policy_sha256):
    """Change ONLY outer policy; caller must confirm ordinary CAS/checkpoint."""
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
