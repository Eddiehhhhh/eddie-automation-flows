"""One exact interrupted initial transaction, never a general old-rules bypass.

These hashes come from the reviewed successful public run and private readback.
New execution keeps its actual new code/policy identity. Only an already durable
copy of this unchanged plan may resume; a fresh plan never borrows old rules.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
import get_notion_sink_plan as p

OLD_RUN = _private_bindings.value('get_notion_plan_recovery', 'OLD_RUN')
OLD_ATTEMPT = _private_bindings.value('get_notion_plan_recovery', 'OLD_ATTEMPT')
OLD_JOB = _private_bindings.value('get_notion_plan_recovery', 'OLD_JOB')
OLD_CONTROLLER = _private_bindings.value('get_notion_plan_recovery', 'OLD_CONTROLLER')
OLD_PUBLIC_SHA = _private_bindings.value('get_notion_plan_recovery', 'OLD_PUBLIC_SHA')
OLD_NOTION_POLICY = _private_bindings.value('get_notion_plan_recovery', 'OLD_NOTION_POLICY')
OLD_PLAN = _private_bindings.value('get_notion_plan_recovery', 'OLD_PLAN')
OLD_ENGINE_RULES = _private_bindings.value('get_notion_plan_recovery', 'OLD_ENGINE_RULES')
OLD_CHECKPOINT = _private_bindings.value('get_notion_plan_recovery', 'OLD_CHECKPOINT')
OLD_CHECKPOINT_COUNT = _private_bindings.value('get_notion_plan_recovery', 'OLD_CHECKPOINT_COUNT')
OLD_ANCHOR = _private_bindings.value('get_notion_plan_recovery', 'OLD_ANCHOR')
OLD_GET = _private_bindings.value('get_notion_plan_recovery', 'OLD_GET')
OLD_PUBLIC_POLICY = _private_bindings.value('get_notion_plan_recovery', 'OLD_PUBLIC_POLICY')
HOLD_SHA256 = _private_bindings.value('get_notion_plan_recovery', 'HOLD_SHA256')
HELD_NOTE_SHA256 = _private_bindings.value('get_notion_plan_recovery', 'HELD_NOTE_SHA256')
HELD_PAGE_SHA256 = _private_bindings.value('get_notion_plan_recovery', 'HELD_PAGE_SHA256')
OPERATION_SHA256 = _private_bindings.value('get_notion_plan_recovery', 'OPERATION_SHA256')
REQUEST_FINGERPRINT = _private_bindings.value('get_notion_plan_recovery', 'REQUEST_FINGERPRINT')


def allows(plan, journal):
    """Caller must first authenticate/read back the persistent state externally."""
    try:
        return (isinstance(plan,dict) and isinstance(journal,dict)
            and plan.get('rules_sha256') == OLD_ENGINE_RULES
            and p.digest(plan) == OLD_PLAN and journal.get('plan_sha256') == OLD_PLAN
            and p.encoded(journal.get('plan')) == p.encoded(plan))
    except Exception:
        return False


def recover_checkpoint(client, run_id, run_attempt):
    """Called only after exact new-controller mismatch, never on denied reads."""
    import get_notion_journal_runtime as checkpoints
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(OLD_RUN,OLD_ATTEMPT):
        checkpoints.fail('journal_provenance_other_controller')
    result=checkpoints.recover_anchor(client,run_id=OLD_RUN,run_attempt=OLD_ATTEMPT,
        public_policy=deepcopy(OLD_PUBLIC_POLICY),approved_public_policy_sha256=OLD_PUBLIC_SHA,
        notion_policy_sha256=OLD_NOTION_POLICY)
    record=result.get('checkpoint',{});get=result.get('get_step_record',{})
    if (result.get('anchor')!=OLD_ANCHOR or result.get('job_id')!=OLD_JOB
            or result.get('checkpoint_count')!=OLD_CHECKPOINT_COUNT or record.get('record_sha256')!=OLD_CHECKPOINT
            or record.get('controller_sha')!=OLD_CONTROLLER
            or any(get.get(k)!=v for k,v in OLD_GET.items())
            or result.get('private_readback_required') is not True):
        checkpoints.fail('journal_provenance_record')
    return {**result,'recovery_anchor':'exact_uncertain_transaction_v1'}


def validate_initial_state(state, sha):
    """The first old checkpoint has one exact envelope, not a self-hashed claim."""
    try:
        journal=state['engine_state']['journal']
        return (sha==OLD_ANCHOR['state_sha256'] and p.digest(state)==sha
            and allows(journal['plan'],journal)
            and all(state[k] is None for k in ('origin','policy_sha256','last_enqueued_publication','observed_publication_tip'))
            and state['pending_by_id']=={} and state['rotation_order']==[])
    except Exception:
        return False


def validate_holds(holds):
    """Only externally pinned holds; references retain all original evidence.

    The complete original journal remains in the immutable anchor blob. No
    later source/rule change or retry_conflicts flag can release this hold.
    """
    import get_notion_historical_active_recovery as active
    import get_notion_authenticated_recovery as authenticated
    exact=active.inflight_hold()
    retained=_private_bindings.value('get_notion_plan_recovery','RETAINED_HOLDS')
    if not isinstance(retained,dict) or len(retained)>16:raise ValueError('state_invalid')
    for ident,row in retained.items():
        if (not isinstance(row,dict) or row.get('note_id')!=ident
                or row.get('contract')!='get-notion-exact-uncertain-hold-1'
                or row.get('effects')!='unresolved' or row.get('retry_allowed') is not False):
            raise ValueError('state_invalid')
    # Exact deny-only copies survive removal of historical execution grants.
    registered=([exact] if exact else [])+authenticated.inflight_holds()+list(retained.values())
    if not isinstance(holds,dict):raise ValueError('state_invalid')
    legacy=dict(holds)
    seen={}
    for exact in registered:
        ident=exact['note_id']
        if ident in seen and p.encoded(seen[ident])!=p.encoded(exact):raise ValueError('state_invalid')
        seen[ident]=exact
        if ident in legacy:
            if p.encoded(legacy.pop(ident))!=p.encoded(exact):raise ValueError('state_invalid')
    if len(legacy)>1:raise ValueError('state_invalid')
    for ident,row in legacy.items():
        if (p.digest(ident)!=HELD_NOTE_SHA256 or not isinstance(row,dict)
                or row.get('note_id')!=ident or p.digest(row)!=HOLD_SHA256):
            raise ValueError('state_invalid')
    return holds


def held(state, ident, page=None):
    holds=validate_holds(state.get('unresolved_by_id',{}))
    retained=_private_bindings.value('get_notion_plan_recovery','RETAINED_HOLDS')
    return ident in holds or ident in retained or any(page==row['page_id'] for row in [*holds.values(),*retained.values()])


def preserve_holds(before, after):
    old=validate_holds(before.get('unresolved_by_id',{}))
    new=validate_holds(after.get('unresolved_by_id',{}))
    if any(ident not in new or p.encoded(new[ident])!=p.encoded(row) for ident,row in old.items()):
        raise ValueError('state_invalid')


def isolate_outer(state, expected_sha):
    """Pure exact transition, no network and no interpretation of absence.

    The old operation is still unresolved. Only the active execution slot is
    released; the separately pinned note/page remains write-denied forever
    until a separately reviewed recovery contract is introduced.
    """
    if not validate_initial_state(state,expected_sha):raise ValueError('state_invalid')
    engine=state['engine_state'];journal=engine['journal']
    if (journal['index']!=1 or len(journal['plan']['records'])!=2
            or engine.get('unresolved_by_id') or len(engine['receipts'])!=1):
        raise ValueError('state_invalid')
    row=journal['plan']['records'][1];operation=journal['operation']
    if (p.digest(row['note_id'])!=HELD_NOTE_SHA256 or p.digest(row['page_id'])!=HELD_PAGE_SHA256
            or operation.get('kind')!='append' or operation.get('phase')!='in_flight'
            or operation.get('response') is not None or operation.get('archive_index')!=0
            or p.digest(operation)!=OPERATION_SHA256 or row['note_id'] in engine['receipts']):
        raise ValueError('state_invalid')
    hold={'contract':'get-notion-exact-uncertain-hold-1','note_id':row['note_id'],'page_id':row['page_id'],
        'anchor':deepcopy(OLD_ANCHOR),'run_id':OLD_RUN,'run_attempt':OLD_ATTEMPT,
        'checkpoint_sha256':OLD_CHECKPOINT,'plan_sha256':OLD_PLAN,'record_index':1,
        'operation_sha256':OPERATION_SHA256,'request_fingerprint':REQUEST_FINGERPRINT,
        'reason':'uncertain_append','effects':'unresolved','retry_allowed':False}
    validate_holds({row['note_id']:hold})
    result=deepcopy(state);result['engine_state']['journal']=None
    result['engine_state']['unresolved_by_id']={row['note_id']:hold}
    return result


# Exact acknowledged-create checkpoint from run10. These are separate from the
# immutable uncertain-append hold above; no hold permission or evidence changes.
RUN10_RUN = _private_bindings.value('get_notion_plan_recovery', 'RUN10_RUN')
RUN10_ATTEMPT = _private_bindings.value('get_notion_plan_recovery', 'RUN10_ATTEMPT')
RUN10_JOB = _private_bindings.value('get_notion_plan_recovery', 'RUN10_JOB')
RUN10_CONTROLLER = _private_bindings.value('get_notion_plan_recovery', 'RUN10_CONTROLLER')
RUN10_PUBLIC_SHA = _private_bindings.value('get_notion_plan_recovery', 'RUN10_PUBLIC_SHA')
RUN10_NOTION_POLICY = _private_bindings.value('get_notion_plan_recovery', 'RUN10_NOTION_POLICY')
RUN10_PLAN = _private_bindings.value('get_notion_plan_recovery', 'RUN10_PLAN')
RUN10_ENGINE_RULES = _private_bindings.value('get_notion_plan_recovery', 'RUN10_ENGINE_RULES')
RUN10_CHECKPOINT = _private_bindings.value('get_notion_plan_recovery', 'RUN10_CHECKPOINT')
RUN10_CHECKPOINT_COUNT = _private_bindings.value('get_notion_plan_recovery', 'RUN10_CHECKPOINT_COUNT')
RUN10_STATE_BYTES = _private_bindings.value('get_notion_plan_recovery', 'RUN10_STATE_BYTES')
RUN10_ANCHOR = _private_bindings.value('get_notion_plan_recovery', 'RUN10_ANCHOR')
RUN10_GET = _private_bindings.value('get_notion_plan_recovery', 'RUN10_GET')
RUN10_PUBLIC_POLICY = _private_bindings.value('get_notion_plan_recovery', 'RUN10_PUBLIC_POLICY')


def allows_run10(plan, journal):
    """Only an authenticated durable copy of this exact plan, never prepare."""
    try:
        return (isinstance(plan,dict) and isinstance(journal,dict)
            and plan.get('rules_sha256') == RUN10_ENGINE_RULES
            and p.digest(plan) == RUN10_PLAN and journal.get('plan_sha256') == RUN10_PLAN
            and p.encoded(journal.get('plan')) == p.encoded(plan))
    except Exception:
        return False


def recover_run10_checkpoint(client, run_id, run_attempt):
    """The caller retains its one shared discovery read/byte budget."""
    import get_notion_journal_runtime as checkpoints
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(RUN10_RUN,RUN10_ATTEMPT):
        checkpoints.fail('journal_provenance_other_controller')
    result=checkpoints.recover_anchor(client,run_id=RUN10_RUN,run_attempt=RUN10_ATTEMPT,
        public_policy=deepcopy(RUN10_PUBLIC_POLICY),approved_public_policy_sha256=RUN10_PUBLIC_SHA,
        notion_policy_sha256=RUN10_NOTION_POLICY)
    record=result.get('checkpoint',{});get=result.get('get_step_record',{})
    if (result.get('anchor')!=RUN10_ANCHOR or result.get('job_id')!=RUN10_JOB
            or result.get('checkpoint_count')!=RUN10_CHECKPOINT_COUNT or record.get('record_sha256')!=RUN10_CHECKPOINT
            or record.get('controller_sha')!=RUN10_CONTROLLER
            or any(get.get(k)!=v for k,v in RUN10_GET.items())
            or result.get('private_readback_required') is not True):
        checkpoints.fail('journal_provenance_record')
    return {**result,'recovery_anchor':'exact_run10_acknowledged_create_v1'}


def validate_run10_state(state, sha):
    """Authenticate the one original envelope, including saved response/hold."""
    try:
        journal=state['engine_state']['journal']
        return (sha==RUN10_ANCHOR['state_sha256'] and p.digest(state)==sha
            and len(p.encoded(state))==RUN10_STATE_BYTES
            and state['policy_sha256']==RUN10_PUBLIC_POLICY['get_policy_sha256']
            and allows_run10(journal['plan'],journal))
    except Exception:
        return False


def transition_run10_outer(state, sha, current_get_policy_sha256):
    """After authenticated remote readback, change only the queue policy field.

    Runtime supplies the already verified current policy and persists this by
    confirmed CAS before using QueuePublication. New checkpoints carry the new
    policy context. This transition cannot be reused on its resulting state;
    the exact old plan may continue under the newly authenticated checkpoint.
    Saved plan/response, holds and queue bytes stay intact.
    """
    if not validate_run10_state(state,sha):raise ValueError('state_invalid')
    p.hex_hash(current_get_policy_sha256,64)
    if current_get_policy_sha256==state['policy_sha256']:raise ValueError('state_invalid')
    result=deepcopy(state);result['policy_sha256']=current_get_policy_sha256
    return result
