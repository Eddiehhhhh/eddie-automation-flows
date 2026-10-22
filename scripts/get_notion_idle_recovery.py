"""One code-registered idle predecessor, disabled until real evidence is reviewed.

This is policy continuity, not transaction recovery or publication authority.
The sole capsule must be populated from the actual terminal attempt and private
readback after the engine is idle. Nothing is learned from the current state.
No clock exception, transport, writer, permission or budget is introduced here.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy

TAG = 'exact_idle_predecessor_checkpoint_v1'
# Deliberately empty. Never predict a future run, checkpoint or state pin.
PREDECESSOR = _private_bindings.value('get_notion_idle_recovery', 'PREDECESSOR')
MAX_EXTRA_RECEIPTS = 16
CAPSULE_KEYS = {'public_policy', 'public_policy_sha256', 'notion_policy_sha256',
    'run_binding', 'job_id', 'job_sha256', 'get_record', 'checkpoint_count',
    'checkpoint', 'anchor', 'receipts'}
RECEIPT_KEYS = {'commit', 'receipt_sha256', 'sequence', 'policy_sha256'}


def _capsule():
    """Validate and copy immutable code registration, never runtime input."""
    import get_notion_journal_runtime as c
    import get_notion_outbox as o
    import get_notion_run53_recovery as run53
    import get_notion_run57_recovery as run57
    p = c.provenance
    if PREDECESSOR is None:
        return None
    try:
        value = deepcopy(PREDECESSOR)
        if not isinstance(value, dict) or set(value) != CAPSULE_KEYS:
            raise ValueError
        policy = p.validate_policy(value['public_policy'], value['public_policy_sha256'])
        c.journal.checked(value['notion_policy_sha256'], 64)
        c.journal.checked(value['job_sha256'], 64)
        p.positive(value['job_id'])
        run = value['run_binding']
        if (not isinstance(run, dict) or p.run_binding(run) != run
                or run['status'] != 'completed' or run['conclusion'] != 'success'
                or run['id'] in (run53.RUN, run57.RUN)
                or run['event'] not in ('schedule', 'workflow_dispatch')
                or run['workflow_id'] != policy['workflow_id']
                or run['path'] != policy['workflow_path']
                or run['head_branch'] != policy['head_branch']):
            raise ValueError
        p.positive(run['id']); p.positive(run['run_attempt'])
        c.journal.checked(run['head_sha'], 40)
        if p.timestamp(run['created_at']) > p.timestamp(run['updated_at']):
            raise ValueError
        record = value['get_record']
        p.parse_record(p.publication_line(record).encode(), policy, run)
        count = value['checkpoint_count']
        final = value['checkpoint']
        if (type(count) is not int or not 1 <= count <= c.MAX_CHECKPOINTS
                or not isinstance(final, dict) or set(final) != c.RECORD_KEYS
                or final['contract'] != c.CONTRACT
                or final['status'] != 'journal_readback_verified'
                or type(final['sequence']) is not int or final['sequence'] != count
                or final['readback_verified'] is not True
                or type(final['cas_attempts']) is not int or final['cas_attempts'] not in (0, 1)):
            raise ValueError
        expected = {'run_id': run['id'], 'run_attempt': run['run_attempt'],
            'code_sha': policy['code_sha'], 'controller_sha': run['head_sha'],
            'rules_sha256': policy['rules_sha256'],
            'notion_policy_sha256': value['notion_policy_sha256'],
            'public_policy_sha256': value['public_policy_sha256']}
        if any(final[key] != expected[key] for key in c.CONTEXT_KEYS):
            raise ValueError
        if count == 1:
            if final['previous_record_sha256'] is not None: raise ValueError
        else:
            c.journal.checked(final['previous_record_sha256'], 64)
        if final['cas_attempts']:
            c.journal.checked(final['parent_commit'], 40)
        elif final['parent_commit'] is not None:
            raise ValueError
        for key in ('commit', 'tree', 'state_blob'):
            c.journal.checked(final[key], 40)
        for key in ('state_sha256', 'record_sha256'):
            c.journal.checked(final[key], 64)
        if (type(final['state_bytes']) is not int
                or not 0 < final['state_bytes'] <= c.journal.MAX_STATE
                or o.digest({k: v for k, v in final.items() if k != 'record_sha256'}) != final['record_sha256']
                or value['anchor'] != {k: final[k] for k in ('commit', 'state_blob', 'state_sha256')}):
            raise ValueError
        receipts = value['receipts']
        if not isinstance(receipts, tuple) or len(receipts) > MAX_EXTRA_RECEIPTS:
            raise ValueError
        seen = set(); previous = 2
        for receipt in receipts:
            if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
                raise ValueError
            c.journal.checked(receipt['commit'], 40)
            c.journal.checked(receipt['receipt_sha256'], 64)
            c.journal.checked(receipt['policy_sha256'], 64)
            sequence = receipt['sequence']
            if (type(sequence) is not int or not previous < sequence <= record['receipt_sequence']
                    or receipt['commit'] in seen):
                raise ValueError
            seen.add(receipt['commit']); previous = sequence
        if record['receipt_sequence'] > 2 and not any(
                receipt['commit'] == record['receipt_commit']
                and receipt['receipt_sha256'] == record['receipt_sha256']
                and receipt['sequence'] == record['receipt_sequence'] for receipt in receipts):
            raise ValueError
        return value
    except Exception:
        c.fail('journal_provenance_record')


def _forward_policy(policy, capsule):
    """Executing rules/Get policy; unchanged or one exact fixed-source template."""
    import get_recurring_entry as entry
    import get_recurring_controller as controller
    import get_recurring_policy_recovery as recovery
    old = capsule['public_policy']
    template_transition = controller.fixed_idle_template_transition(old, policy)
    stable = set(old) - {'code_sha', 'rules_sha256', 'get_policy_sha256'}
    if template_transition: stable.remove('controller_template_sha256')
    return (isinstance(policy, dict)
        and all(policy.get(key) == old[key] for key in stable)
        and (template_transition or old['controller_template_sha256'] == controller.template_sha256())
        and policy.get('code_sha') != old['code_sha']
        and policy.get('rules_sha256') == entry.rules_sha256()
        and policy.get('get_policy_sha256') == recovery.current_get_policy()
        and policy.get('get_policy_sha256') != old['get_policy_sha256'])


def candidate_policy(policy, run):
    """Called only inside the ordinary fresh-original-attempt verifier."""
    import get_publication_provenance as p
    capsule = _capsule()
    if (capsule is not None and _forward_policy(policy, capsule)
            and p.run_binding(run) == capsule['run_binding']):
        return deepcopy(capsule['public_policy']), True
    return policy, False


def registered_run(run_id):
    capsule = _capsule()
    return capsule is not None and type(run_id) is int and run_id == capsule['run_binding']['id']


def verify_original_record(record, job_id, *, job=None):
    import get_publication_provenance as p
    import get_notion_outbox as o
    capsule = _capsule()
    if (capsule is None or record != capsule['get_record']
            or type(job_id) is not int or job_id != capsule['job_id']
            or not isinstance(job, dict) or job.get('id') != job_id
            or o.digest(job) != capsule['job_sha256']):
        p.fail('provenance_record')


def accepts_receipt(commit, receipt_sha256, value, requested_policy):
    """Exact additional old receipts only; normal receipt/chain checks remain."""
    import get_recurring_policy_recovery as recovery
    capsule = _capsule()
    return (capsule is not None and isinstance(value, dict)
        and requested_policy == recovery.current_get_policy()
        and any(commit == row['commit'] and receipt_sha256 == row['receipt_sha256']
            and type(value.get('sequence')) is int and value['sequence'] == row['sequence']
            and value.get('policy_sha256') == row['policy_sha256']
            and requested_policy != row['policy_sha256'] for row in capsule['receipts']))


def can_recover(policy, run_id, run_attempt):
    capsule = _capsule()
    return (capsule is not None and type(run_id) is int and type(run_attempt) is int
        and (run_id, run_attempt) == (capsule['run_binding']['id'], capsule['run_binding']['run_attempt'])
        and _forward_policy(policy, capsule))


def _matches_proof(result, capsule):
    return (isinstance(result, dict)
        and result.get('status') == 'journal_provenance_verified'
        and result.get('anchor') == capsule['anchor']
        and result.get('run_id') == capsule['run_binding']['id']
        and result.get('run_attempt') == capsule['run_binding']['run_attempt']
        and result.get('run_binding') == capsule['run_binding']
        and result.get('job_id') == capsule['job_id']
        and result.get('job_sha256') == capsule['job_sha256']
        and result.get('checkpoint_count') == capsule['checkpoint_count']
        and result.get('checkpoint') == capsule['checkpoint']
        and result.get('get_step_record') == capsule['get_record']
        and result.get('private_readback_required') is True
        and result.get('clock_witness') is None)


def recover_checkpoint(client, run_id, run_attempt, *, current_public_policy):
    """Same bounded client; full original chain, exact controller and fresh reread."""
    import get_notion_journal_runtime as c
    capsule = _capsule()
    if not can_recover(current_public_policy, run_id, run_attempt):
        c.fail('journal_provenance_other_controller')
    result = c.recover_anchor(client, run_id=run_id, run_attempt=run_attempt,
        public_policy=capsule['public_policy'],
        approved_public_policy_sha256=capsule['public_policy_sha256'],
        notion_policy_sha256=capsule['notion_policy_sha256'], require_success=True)
    if not _matches_proof(result, capsule):
        c.fail('journal_provenance_record')
    return {**result, 'recovery_anchor': TAG}


def transition_outer(state, sha, new_get_policy_sha256, *, proof):
    """Pure single-field change; caller MUST use ordinary confirmed CAS/readback."""
    import get_notion_outbox as o
    import get_private_preview_retention as retention
    import get_recurring_policy_recovery as recovery
    capsule = _capsule()
    o.check_state(state)
    raw = o.encoded(state)
    if (capsule is None or not _matches_proof(proof, capsule)
            or proof.get('recovery_anchor') != TAG
            or sha != capsule['anchor']['state_sha256'] or o.digest(raw) != sha
            or len(raw) != capsule['checkpoint']['state_bytes']
            or retention.oid(raw) != capsule['anchor']['state_blob']
            or state['engine_state']['journal'] is not None
            or state['policy_sha256'] != capsule['public_policy']['get_policy_sha256']
            or new_get_policy_sha256 != recovery.current_get_policy()
            or new_get_policy_sha256 == state['policy_sha256']):
        o.fail('outbox_state_invalid')
    result = deepcopy(state)
    result['policy_sha256'] = new_get_policy_sha256
    o.check_state(result)
    return result
