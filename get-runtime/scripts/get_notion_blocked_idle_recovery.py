"""Exact failed fixed-source idle predecessor; no canonical success authority.

Disabled until an actual terminal/private readback registration is reviewed.
Never learns registration from current state and never releases any hold.
"""
from copy import deepcopy
import get_private_runtime_bindings as bindings

TAG = 'exact_blocked_idle_predecessor_v1'
BLOCKED_PREDECESSOR = bindings.value('get_notion_blocked_idle_recovery', 'BLOCKED_PREDECESSOR')
KEYS = {'public_policy', 'public_policy_sha256', 'notion_policy_sha256',
    'run_binding', 'latest_binding', 'job_id', 'job_sha256', 'checkpoint_count',
    'checkpoint', 'anchor', 'fixed_source_record', 'terminal_report',
    'selection', 'selection_sha256', 'holds', 'selected_hold', 'receipts'}


def _capsule():
    import get_notion_journal_runtime as c
    import get_notion_outbox as o
    if BLOCKED_PREDECESSOR is None: return None
    try:
        v = deepcopy(BLOCKED_PREDECESSOR); p = c.provenance
        if not isinstance(v, dict) or set(v) != KEYS: raise ValueError
        policy = p.validate_policy(v['public_policy'], v['public_policy_sha256'])
        for key in ('notion_policy_sha256', 'job_sha256', 'selection_sha256'): c.journal.checked(v[key], 64)
        p.positive(v['job_id'])
        run = v['run_binding']; latest = v['latest_binding']
        if (p.run_binding(run) != run or p.run_binding(latest) != latest
                or run['status'] != 'completed' or run['conclusion'] != 'failure'
                or latest['status'] != 'completed' or latest['conclusion'] != 'failure'
                or latest['run_attempt'] != run['run_attempt']
                or run['event'] != 'workflow_dispatch'
                or run['workflow_id'] != policy['workflow_id']
                or run['path'] != policy['workflow_path'] or run['head_branch'] != policy['head_branch']): raise ValueError
        c._same_run(run, latest, cross_endpoint=True)
        if p.run_semantic_binding(run) != p.run_semantic_binding(latest): raise ValueError
        count = v['checkpoint_count']; final = v['checkpoint']
        if (type(count) is not int or not 1 <= count <= c.MAX_CHECKPOINTS
                or not isinstance(final, dict) or set(final) != c.RECORD_KEYS
                or final['contract'] != c.CONTRACT or final['status'] != 'journal_readback_verified'
                or type(final['sequence']) is not int or final['sequence'] != count
                or final['readback_verified'] is not True
                or type(final['cas_attempts']) is not int or final['cas_attempts'] not in (0, 1)):
            raise ValueError
        context = {'run_id':run['id'], 'run_attempt':run['run_attempt'], 'code_sha':policy['code_sha'],
            'controller_sha':run['head_sha'], 'rules_sha256':policy['rules_sha256'],
            'notion_policy_sha256':v['notion_policy_sha256'], 'public_policy_sha256':v['public_policy_sha256']}
        c.context(context)
        if any(final[k] != context[k] for k in c.CONTEXT_KEYS): raise ValueError
        if count == 1:
            if final['previous_record_sha256'] is not None: raise ValueError
        else: c.journal.checked(final['previous_record_sha256'],64)
        if final['cas_attempts']: c.journal.checked(final['parent_commit'],40)
        elif final['parent_commit'] is not None: raise ValueError
        for k in ('commit','tree','state_blob'): c.journal.checked(final[k],40)
        for k in ('state_sha256','record_sha256'): c.journal.checked(final[k],64)
        if (type(final['state_bytes']) is not int or not 0 < final['state_bytes'] <= c.journal.MAX_STATE
                or o.digest({k:x for k,x in final.items() if k!='record_sha256'}) != final['record_sha256']
                or v['anchor'] != {k:final[k] for k in ('commit','state_blob','state_sha256')}): raise ValueError
        fixed = c.fixed_auth.parse(o.encoded(v['fixed_source_record']), context, run)
        if fixed['mode'] != 'historical-apply' or fixed['selection_sha256'] != v['selection_sha256']: raise ValueError
        report = v['terminal_report']
        if (not isinstance(report,dict) or set(report) != {'contract','status','reason','write_state',
                'journal_write_state','business_health_asserted','complete_library_claimed'}
                or report.get('contract') != 'get-private-runtime-report-2'
                or report.get('status') != 'blocked' or report.get('reason') != 'unresolved_operation'
                or report.get('journal_write_state') != 'confirmed'
                or report.get('write_state') not in ('confirmed','not_attempted')
                or report.get('business_health_asserted') is not False
                or report.get('complete_library_claimed') is not False
                or any(report.get(k) in ('unknown','uncertain') for k in ('write_state','journal_write_state'))): raise ValueError
        if not isinstance(v['holds'],dict) or not isinstance(v['selected_hold'],str) or not isinstance(v['receipts'],dict): raise ValueError
        return v
    except Exception: c.fail('journal_provenance_record')


def can_recover(policy, run_id, run_attempt):
    import get_notion_idle_recovery as idle
    v = _capsule()
    return (v is not None and type(run_id) is int and type(run_attempt) is int
        and (run_id,run_attempt)==(v['run_binding']['id'],v['run_binding']['run_attempt'])
        and idle._forward_policy(policy,v))


def _matches(proof,v):
    return (isinstance(proof,dict) and proof.get('status')=='journal_provenance_verified'
        and all(proof.get(k)==v[k] for k in ('anchor','run_binding','latest_binding','job_id','job_sha256',
            'checkpoint_count','checkpoint','fixed_source_record','terminal_report'))
        and proof.get('run_id')==v['run_binding']['id'] and proof.get('run_attempt')==v['run_binding']['run_attempt']
        and proof.get('terminal_conclusions') == {'job':'failure','notion':'failure','get':'skipped'}
        and proof.get('get_step_record') is None and proof.get('get_run_success_claim') is False
        and proof.get('private_readback_required') is True and proof.get('clock_witness') is None)


def recover_checkpoint(client, run_id, run_attempt, *, current_public_policy):
    import get_notion_journal_runtime as c
    v = _capsule()
    if not can_recover(current_public_policy,run_id,run_attempt): c.fail('journal_provenance_other_controller')
    proof=c.recover_anchor(client,run_id=run_id,run_attempt=run_attempt,public_policy=v['public_policy'],
        approved_public_policy_sha256=v['public_policy_sha256'],notion_policy_sha256=v['notion_policy_sha256'],
        require_success=False,include_job_digest=True,include_terminal_report=True)
    if not _matches(proof,v): c.fail('journal_provenance_record')
    return {**proof,'recovery_anchor':TAG}


def _verify_business(state,v):
    """Re-derive the registered selection minus one exact unresolved hold.

    Other globally registered old holds are preserved, not counted as this
    selection's failures. Their entire map is still byte-locked to registration.
    """
    import get_notion_outbox as o
    import get_notion_historical_input as inputs
    import get_notion_historical_runtime as historical
    engine=state['engine_state']
    if engine['journal'] is not None or o.encoded(engine.get('unresolved_by_id',{})) != o.encoded(v['holds']): o.fail('outbox_state_invalid')
    members=inputs.validate_apply_selection(v['selection'],v['selection_sha256'],apply=True)
    expected_rows=sum(len(manifest['records']) for manifest, _ in members)
    rows={}; confirmed={}; selected_holds=[]
    for manifest,digest in members:
        for ident,row in manifest['records'].items():
            if ident in rows: o.fail('outbox_state_invalid')
            rows[ident]=row
            hold=engine.get('unresolved_by_id',{}).get(ident)
            if hold is not None:
                if (ident!=v['selected_hold'] or hold.get('note_id')!=ident
                        or hold.get('page_id')!=row['target']['page_id']
                        or hold.get('reason')!='uncertain_append' or hold.get('effects')!='unresolved'
                        or hold.get('retry_allowed') is not False or ident in engine['receipts']
                        or ident in engine['failures']): o.fail('outbox_state_invalid')
                selected_holds.append(ident)
            else:
                if not historical.body_receipts_match(state,{**manifest,'records':{ident:row}}): o.fail('outbox_state_invalid')
                confirmed[ident]=engine['receipts'][ident]
    if (expected_rows <= 1 or len(rows)!=expected_rows or len(confirmed)!=expected_rows-1 or selected_holds!=[v['selected_hold']]
            or o.encoded(confirmed)!=o.encoded(v['receipts'])): o.fail('outbox_state_invalid')


def transition_outer(state, sha, new_get_policy_sha256, *, proof):
    import get_notion_outbox as o
    import get_private_preview_retention as retention
    import get_recurring_policy_recovery as recovery
    v=_capsule();o.check_state(state);raw=o.encoded(state)
    if (v is None or not _matches(proof,v) or proof.get('recovery_anchor')!=TAG
            or sha!=v['anchor']['state_sha256'] or o.digest(raw)!=sha
            or len(raw)!=v['checkpoint']['state_bytes'] or retention.oid(raw)!=v['anchor']['state_blob']
            or state['policy_sha256']!=v['public_policy']['get_policy_sha256']
            or new_get_policy_sha256!=recovery.current_get_policy() or new_get_policy_sha256==state['policy_sha256']): o.fail('outbox_state_invalid')
    _verify_business(state,v)
    result=deepcopy(state);result['policy_sha256']=new_get_policy_sha256;o.check_state(result)
    return result
