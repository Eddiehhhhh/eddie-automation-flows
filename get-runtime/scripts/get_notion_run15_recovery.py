"""Exact run15 acknowledged PNG create recovery; pure bridge, no writes.

Only a caller-authenticated durable journal may use allows(). Preparation never
receives this exception. Saved page/upload handles and plan remain unchanged.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
import get_notion_sink_plan as p

TAG = 'exact_run15_acknowledged_png_create_v1'
RUN = _private_bindings.value('get_notion_run15_recovery', 'RUN')
ATTEMPT = _private_bindings.value('get_notion_run15_recovery', 'ATTEMPT')
JOB = _private_bindings.value('get_notion_run15_recovery', 'JOB')
PUBLIC_POLICY = _private_bindings.value('get_notion_run15_recovery', 'PUBLIC_POLICY')
PUBLIC_SHA = _private_bindings.value('get_notion_run15_recovery', 'PUBLIC_SHA')
NOTION_POLICY = _private_bindings.value('get_notion_run15_recovery', 'NOTION_POLICY')
CHECKPOINT = _private_bindings.value('get_notion_run15_recovery', 'CHECKPOINT')
GET_RECORD = _private_bindings.value('get_notion_run15_recovery', 'GET_RECORD')
ANCHOR = _private_bindings.value('get_notion_run15_recovery', 'ANCHOR')
STATE_BYTES = _private_bindings.value('get_notion_run15_recovery', 'STATE_BYTES')
PLAN_SHA = _private_bindings.value('get_notion_run15_recovery', 'PLAN_SHA')
OP_SHA = _private_bindings.value('get_notion_run15_recovery', 'OP_SHA')
ENGINE_RULES = _private_bindings.value('get_notion_run15_recovery', 'ENGINE_RULES')


def allows(plan, journal):
    """Exact original plan only, including later authenticated resume checkpoints."""
    try:
        return (isinstance(plan, dict) and isinstance(journal, dict)
            and plan.get('rules_sha256') == ENGINE_RULES
            and p.digest(plan) == PLAN_SHA and journal.get('plan_sha256') == PLAN_SHA
            and p.encoded(journal.get('plan')) == p.encoded(plan))
    except Exception: return False


def recover_checkpoint(client, run_id, run_attempt):
    """Reuse the caller's bounded provenance reader; no fallback target search."""
    import get_notion_outbox as o
    import get_notion_journal_runtime as c
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(RUN,ATTEMPT):
        c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(client,run_id=RUN,run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY),approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY)
    if (result.get('anchor')!=ANCHOR or result.get('job_id')!=JOB
            or result.get('checkpoint_count')!=15
            or not o.e.same(result.get('checkpoint'),CHECKPOINT)
            or not o.e.same(result.get('get_step_record'),GET_RECORD)
            or result.get('private_readback_required') is not True):
        c.fail('journal_provenance_record')
    return {**result,'recovery_anchor':TAG}


def validate_state(state, sha):
    import get_notion_outbox as o
    try:
        o.check_state(state)
        journal=state['engine_state']['journal'];assets=state['png_assets']
        return (sha==ANCHOR['state_sha256'] and o.digest(state)==sha
            and len(o.encoded(state))==STATE_BYTES
            and state['policy_sha256']==PUBLIC_POLICY['get_policy_sha256']
            and allows(journal['plan'],journal) and journal['index']==0
            and journal['operation']['kind']=='create'
            and journal['operation']['phase']=='acknowledged'
            and o.digest(journal['operation'])==OP_SHA
            and len(state['engine_state']['receipts'])==4
            and len(state['pending_by_id'])==7
            and len(state['engine_state']['unresolved_by_id'])==1
            and len(assets['uploads'])==2 and not assets['attachments']
            and all(x['state']['phase']=='uploaded_verified' for x in assets['uploads'].values()))
    except Exception: return False


def transition_outer(state, sha, new_get_policy_sha256):
    """Only the outer policy changes; caller confirms CAS and NEW checkpoint.

    The existing engine resumes its acknowledged saved page by readback. This
    function cannot create a page, upload a file, reset an operation or remove a
    hold. Its output cannot be passed through this old-anchor transition again.
    """
    import get_notion_outbox as o
    if not validate_state(state,sha): raise ValueError('state_invalid')
    o.p.hex_hash(new_get_policy_sha256,64)
    if new_get_policy_sha256==state['policy_sha256']: raise ValueError('state_invalid')
    result=deepcopy(state);result['policy_sha256']=new_get_policy_sha256
    o.check_state(result)
    return result
