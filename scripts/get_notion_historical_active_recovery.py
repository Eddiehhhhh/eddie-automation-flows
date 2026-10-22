"""One exact historical append continuation; local candidate pending review.

No generic old-rules compatibility. New rules retain their real identity, and
only the unchanged durable plan and legal forward checkpoints may continue.
No remote effects, authentication shortcuts or runtime-provided trust pins.
"""
from copy import deepcopy

TAG = 'exact_historical_active_append_resume_v1'
# This local candidate binds the original full verifier result privately.
# Never populate from the current state, an input file or a command-line hash.
import get_private_runtime_bindings as _private_bindings
BINDING = _private_bindings.value('get_notion_historical_active_recovery', 'BINDING')
SUCCESSOR_BINDING = _private_bindings.value('get_notion_historical_active_recovery', 'SUCCESSOR_BINDING')


def _validated_binding(value):
    """Strict schema, with authority coming only from code-pinned private bytes."""
    import get_notion_outbox as o
    import get_publication_provenance as p
    from get_private_preview_retention import oid
    keys = {'public_policy','public_policy_sha256','notion_policy_sha256','run_binding',
        'job_id','job_sha256','get_record','checkpoint_count','checkpoint','anchor',
        'state_bytes','plan_sha256','plan_bytes','engine_rules_sha256','manifest_sha256',
        'initial_index','initial_operation_sha256'}
    try:
        b = deepcopy(value)
        if not isinstance(b,dict) or set(b) != keys: return None
        p.validate_policy(b['public_policy'], b['public_policy_sha256'])
        for k in ('notion_policy_sha256','job_sha256','plan_sha256','engine_rules_sha256',
                  'manifest_sha256','initial_operation_sha256'):
            p.a.checked(b[k],64)
        if (p.run_binding(b['run_binding']) != b['run_binding']
                or b['run_binding']['status'] != 'completed' or b['run_binding']['conclusion'] != 'success'
                or type(b['job_id']) is not int or b['job_id'] <= 0
                or type(b['checkpoint_count']) is not int or not 1 <= b['checkpoint_count'] <= 1024
                or type(b['state_bytes']) is not int or not 0 < b['state_bytes'] <= o.e.MAX_STATE
                or type(b['plan_bytes']) is not int or not 0 < b['plan_bytes'] <= o.e.MAX_STATE
                or type(b['initial_index']) is not int or not 0 <= b['initial_index'] < o.e.MAX_RECORDS): return None
        c = b['checkpoint']; policy = b['public_policy']; run = b['run_binding']
        if (c['sequence'] != b['checkpoint_count'] or c['state_bytes'] != b['state_bytes']
                or c['readback_verified'] is not True or c['status'] != 'journal_readback_verified'
                or c['record_sha256'] != o.digest({k:v for k,v in c.items() if k != 'record_sha256'})
                or b['anchor'] != {k:c[k] for k in ('commit','state_blob','state_sha256')}
                or c['run_id'] != run['id'] or c['run_attempt'] != run['run_attempt']
                or c['controller_sha'] != run['head_sha'] or c['code_sha'] != policy['code_sha']
                or c['rules_sha256'] != policy['rules_sha256']
                or c['public_policy_sha256'] != b['public_policy_sha256']
                or c['notion_policy_sha256'] != b['notion_policy_sha256']): return None
        record = b['get_record']
        if (record.get('run_id') != run['id'] or record.get('run_attempt') != run['run_attempt']
                or record.get('controller_sha') != run['head_sha']
                or record.get('code_sha') != policy['code_sha']): return None
        # Full original publication parsing belongs to recover_anchor, not
        # repeated pure admission/schema checks on every journal checkpoint.
        return b
    except Exception:
        return None



def binding():
    return _validated_binding(BINDING)


def successor_binding():
    base = binding(); successor = _validated_binding(SUCCESSOR_BINDING)
    # One authenticated successor checkpoint of the SAME immutable operation.
    # Never allow this registry to authorize a different plan or broader scope.
    same = ('plan_sha256','plan_bytes','engine_rules_sha256','manifest_sha256',
            'initial_index','initial_operation_sha256','state_bytes')
    if (not base or not successor or any(base[k] != successor[k] for k in same)
            or base['run_binding']['id'] == successor['run_binding']['id']
            or base['public_policy'] == successor['public_policy']):
        return None
    return successor


def _run_binding(run_id, attempt=None):
    for b in (binding(), successor_binding()):
        if (b and type(run_id) is int and run_id == b['run_binding']['id']
                and (attempt is None or type(attempt) is int and attempt == b['run_binding']['run_attempt'])):
            return b
    return None


def _state_binding(sha):
    for b in (binding(), successor_binding()):
        if b and sha == b['anchor']['state_sha256']: return b
    return None


def registered_run(run_id, attempt=None):
    return _run_binding(run_id, attempt) is not None


def candidate_policy(policy, run):
    import get_recurring_entry as entry
    import get_recurring_controller as controller
    import get_recurring_policy_recovery as recovery
    import get_publication_provenance as p
    b = _run_binding(run.get('id'), run.get('run_attempt'))
    if not b: return policy, False
    old = b['public_policy']
    stable = set(old) - {'code_sha', 'rules_sha256', 'get_policy_sha256'}
    if (p.run_binding(run) == b['run_binding']
            and all(policy.get(k) == old[k] for k in stable)
            and policy.get('code_sha') != old['code_sha']
            and policy.get('rules_sha256') == entry.rules_sha256()
            and policy.get('get_policy_sha256') == recovery.current_get_policy()
            and policy.get('get_policy_sha256') != old['get_policy_sha256']
            and policy.get('controller_template_sha256') == controller.template_sha256()):
        return deepcopy(old), True
    return policy, False


def verify_original_record(record, job_id, *, job=None, raw=None, run=None):
    import get_publication_provenance as p
    import get_notion_outbox as o
    b = _run_binding(record.get('run_id'), record.get('run_attempt'))
    if (not b or record != b['get_record'] or job_id != b['job_id']
            or o.digest(job) != b['job_sha256'] or p.run_binding(run) != b['run_binding']):
        p.fail('provenance_record')


def recover_checkpoint(client, run_id, run_attempt):
    import get_notion_journal_runtime as c
    b = _run_binding(run_id, run_attempt)
    if not registered_run(run_id, run_attempt): c.fail('journal_provenance_other_controller')
    # The full original verifier reconstructs compact proofs using the private
    # persisted records. Saved expanded logs never replace production retrieval.
    result = c.recover_anchor(client, run_id=run_id, run_attempt=run_attempt,
        public_policy=deepcopy(b['public_policy']),
        approved_public_policy_sha256=b['public_policy_sha256'],
        notion_policy_sha256=b['notion_policy_sha256'], require_success=True)
    expected = {'anchor': b['anchor'], 'job_id': b['job_id'],
        'job_sha256': b['job_sha256'], 'checkpoint_count': b['checkpoint_count'],
        'checkpoint': b['checkpoint'], 'get_step_record': b['get_record'],
        'run_binding': b['run_binding'], 'latest_binding': b['run_binding'],
        'private_readback_required': True, 'clock_witness': None}
    if any(result.get(k) != v for k, v in expected.items()): c.fail('journal_provenance_record')
    return {**result, 'recovery_anchor': TAG}


def _operation_valid(plan, journal, b):
    import get_notion_executor as e
    index = journal['index']; operation = journal['operation']
    if index == b['initial_index']:
        return operation is not None and e.digest(operation) == b['initial_operation_sha256']
    if index == len(plan['records']): return operation is None
    row = plan['records'][index]
    # This repair is append-only and must never broaden archive/create rights.
    if row['action'] != 'update' or row['old_roots']: return False
    if operation is None: return True
    base = {'kind', 'phase', 'before', 'response', 'archive_index'}
    kind = operation.get('kind'); phase = operation.get('phase')
    if type(operation.get('archive_index')) is not int or operation['archive_index'] != 0: return False
    if e.target_sha(operation.get('before'), row['note_id'], plan['policy']['data_source_id']) != row['before_sha256']: return False
    if 'write_failure' in operation:
        f = operation['write_failure']
        blocks = e.transport.wire_blocks(row['request_blocks'], e.append_previous_type(row), uploaded_pngs=row.get('uploaded_pngs'))
        body = {'children': blocks}
        if row['after_anchor'] is not None: body['after'] = row['after_anchor']
        fp = e.transport.fingerprint('PATCH', '/blocks/' + row['page_id'] + '/children', body)
        if ((kind,phase) not in (('append','in_flight'),('cleanup','verified')) or set(f) != {'http_status','request_fingerprint'}
                or type(f['http_status']) is not int or not 400 <= f['http_status'] <= 599
                or f['request_fingerprint'] != fp): return False
    if kind == 'append':
        if not base <= set(operation) <= base | {'write_failure'}: return False
        if phase not in ('prepared', 'in_flight', 'acknowledged'): return False
        if phase in ('prepared', 'in_flight') and operation['response'] is not None: return False
        if phase == 'acknowledged':
            ids = operation['response']
            if not isinstance(ids,list) or len(ids) != len(row['rendered']) or len(set(ids)) != len(ids): return False
            for ident in ids: e.p.notion_id(ident)
            if set(ids) & {x['id'] for x in operation['before']['recursive_blocks']}: return False
        return True
    if (kind != 'cleanup' or phase != 'verified'
            or not base | {'working','new_roots','managed'} <= set(operation) <= base | {'working','new_roots','managed','write_failure'}): return False
    roots, managed = e.appended(operation['before'], operation['working'], row,
                               plan['policy']['data_source_id'], operation['response'])
    return roots == operation['new_roots'] and managed == operation['managed']


def allows(plan, journal):
    import get_notion_executor as e
    b = binding()
    try:
        if (not b or not isinstance(plan, dict) or not isinstance(journal, dict)
                or set(journal) != {'plan_sha256','plan','index','operation'}
                or plan.get('rules_sha256') != b['engine_rules_sha256']
                or e.digest(plan) != b['plan_sha256'] or len(e.encoded(plan)) != b['plan_bytes']
                or journal['plan_sha256'] != b['plan_sha256'] or not e.same(journal['plan'], plan)
                or plan.get('historical_only') is not True or plan.get('property_only')
                or plan.get('historical_manifest_sha256') != b['manifest_sha256']
                or type(journal['index']) is not int
                or not b['initial_index'] <= journal['index'] <= len(plan['records'])
                or any(r['action'] != 'update' or r['old_roots'] for r in plan['records'][b['initial_index']:])):
            return False
        return _operation_valid(plan, journal, b)
    except Exception:
        return False


def initial_state_valid(state, sha):
    import get_notion_outbox as o
    import get_private_preview_retention as retention
    b = _state_binding(sha)
    try:
        o.check_state(state); raw = o.encoded(state); j = state['engine_state']['journal']
        return bool(b and sha == b['anchor']['state_sha256'] == o.digest(raw)
            and len(raw) == b['state_bytes'] and retention.oid(raw) == b['anchor']['state_blob']
            and state['policy_sha256'] == b['public_policy']['get_policy_sha256']
            and allows(j['plan'], j) and j['index'] == b['initial_index'])
    except Exception:
        return False


def check_mode(state, found, manifest, expected, other_modes):
    import get_notion_outbox as o
    b = binding(); j = state['engine_state']['journal']
    if found.get('recovery_anchor') == TAG or b and j and j.get('plan_sha256') == b['plan_sha256']:
        if (not b or any(x is not None for x in other_modes) or manifest is None
                or expected != b['manifest_sha256'] or o.digest(manifest) != expected
                or j is None or not allows(j['plan'], j)
                or manifest != j['plan']['historical_manifest']): o.fail('outbox_state_invalid')


def transition_outer(state, sha, new_policy, *, proof):
    import get_notion_outbox as o
    import get_recurring_policy_recovery as recovery
    b = _state_binding(sha)
    if (not initial_state_valid(state, sha) or not b
            or proof.get('recovery_anchor') != TAG
            or proof.get('status') != 'journal_provenance_verified'
            or proof.get('private_readback_required') is not True
            or proof.get('latest_binding') != b['run_binding']
            or proof.get('run_id') != b['run_binding']['id']
            or proof.get('run_attempt') != b['run_binding']['run_attempt']
            or any(proof.get(k) != b[v] for k,v in (
                ('anchor','anchor'),('checkpoint','checkpoint'),('checkpoint_count','checkpoint_count'),
                ('job_id','job_id'),('job_sha256','job_sha256'),('run_binding','run_binding'),('get_step_record','get_record')))
            or new_policy != recovery.current_get_policy() or new_policy == state['policy_sha256']):
        o.fail('outbox_state_invalid')
    result = deepcopy(state); result['policy_sha256'] = new_policy
    o.check_state(result)
    return result


def preserve(before, after):
    """CAS-level monotonicity in addition to external checkpoint authentication."""
    import get_notion_outbox as o
    b = binding()
    if not b: return
    old = before['engine_state']['journal']
    if old is None or old.get('plan_sha256') != b['plan_sha256']: return
    if not allows(old['plan'], old): o.fail('outbox_state_invalid')
    new = after['engine_state']['journal']
    for k in before:
        if k not in ('policy_sha256','engine_state') and before[k] != after[k]: o.fail('outbox_state_invalid')
    if before['policy_sha256'] != after['policy_sha256']:
        import get_recurring_policy_recovery as recovery
        if (not initial_state_valid(before, o.digest(before)) or after['policy_sha256'] != recovery.current_get_policy()
                or before['engine_state'] != after['engine_state']): o.fail('outbox_state_invalid')
        return
    import get_recurring_policy_recovery as recovery
    if before['policy_sha256'] != recovery.current_get_policy(): o.fail('outbox_state_invalid')
    old_engine = before['engine_state']; new_engine = after['engine_state']
    for k in old_engine:
        if k not in ('journal','receipts') and old_engine[k] != new_engine[k]: o.fail('outbox_state_invalid')
    if new is None:
        if old['index'] != len(old['plan']['records']) or old['operation'] is not None or old_engine['receipts'] != new_engine['receipts']: o.fail('outbox_state_invalid')
        return
    if not allows(new['plan'], new) or not old['index'] <= new['index'] <= old['index'] + 1: o.fail('outbox_state_invalid')
    import get_notion_executor as e
    if new['index'] > old['index']:
        op = old['operation']
        if new['operation'] is not None or op is None or op.get('kind') != 'cleanup' or op.get('phase') != 'verified': o.fail('outbox_state_invalid')
        row = old['plan']['records'][old['index']]; ident = row['note_id']
        wanted = deepcopy(old_engine['receipts'])
        if ident in wanted: o.fail('outbox_state_invalid')
        wanted[ident] = e.receipt(op['working'], ident, old['plan']['policy']['data_source_id'],
            op['managed'], old['plan']['publication'], row['source'], row.get('preserved_asset_bindings'))
        if row.get('creation_plan_sha256'): wanted[ident]['creation_plan_sha256'] = row['creation_plan_sha256']
        if new_engine['receipts'] != wanted: o.fail('outbox_state_invalid')
        return
    if old_engine['receipts'] != new_engine['receipts']: o.fail('outbox_state_invalid')
    a = old['operation']; z = new['operation']
    if a is None:
        if z is not None and (z['kind'],z['phase']) != ('append','prepared'): o.fail('outbox_state_invalid')
        return
    if z is None or a['before'] != z['before']: o.fail('outbox_state_invalid')
    if a['kind'] == 'cleanup':
        if a != z: o.fail('outbox_state_invalid')
        return
    if a['response'] is not None and a['response'] != z['response']: o.fail('outbox_state_invalid')
    phases = {('prepared','in_flight'),('in_flight','prepared'),
              ('in_flight','acknowledged'),('in_flight','verified'),('acknowledged','verified')}
    if a['phase'] == z['phase']:
        if a == z: return
        if a['phase'] != 'in_flight' or 'write_failure' in a or set(z) != set(a) | {'write_failure'} or any(z[k] != v for k,v in a.items()): o.fail('outbox_state_invalid')
        return
    if (a['phase'],z['phase']) not in phases: o.fail('outbox_state_invalid')
    if a['phase'] == 'in_flight' and z['phase'] == 'prepared' and 'write_failure' in a: o.fail('outbox_state_invalid')
    if z['kind'] == 'append':
        permitted = {'phase','response'} if z['phase'] == 'acknowledged' else {'phase'}
        if set(a) != set(z) or any(z[k] != v for k,v in a.items() if k not in permitted): o.fail('outbox_state_invalid')
    elif z['kind'] == 'cleanup':
        # appended() above validates the new native snapshot, roots and body.
        if any(z.get(k) != v for k,v in a.items() if k not in ('kind','phase')): o.fail('outbox_state_invalid')
    else: o.fail('outbox_state_invalid')
