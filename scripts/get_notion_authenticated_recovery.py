"""Code-pinned registrations for authenticated unknown append isolation.

No network writes or inferred absence. Each registration identifies an original
failed run, exact immutable state and plan. Isolation preserves its evidence and
only releases that execution slot while permanently denying the note/page.
"""
from copy import deepcopy
import get_private_runtime_bindings as bindings
import get_notion_create_resume as create_resume

TAG = 'authenticated_unknown_append_v1'


def _codec():
    import get_notion_outbox as o
    return o


def registry():
    # Empty is an explicit supported registry value, not an exception fallback.
    entries = bindings.value('get_notion_authenticated_recovery', 'REGISTRY')
    if not isinstance(entries, list) or len(entries) > 16:
        raise ValueError('authenticated_recovery_invalid')
    seen = set()
    for b in entries:
        if not isinstance(b, dict) or set(b) != {'proof', 'public_policy', 'public_policy_sha256', 'notion_policy_sha256', 'plan_sha256', 'operation_sha256', 'initial_index', 'record_count', 'selection_sha256', 'manifest_sha256', 'hold'}:
            raise ValueError('authenticated_recovery_invalid')
        p = b['proof']; h = b['hold']; key = (p['run_id'], p['run_attempt'])
        if key in seen or type(b['initial_index']) is not int or type(b['record_count']) is not int or not 0 <= b['initial_index'] < b['record_count']:
            raise ValueError('authenticated_recovery_invalid')
        seen.add(key)
        if (p['status'] != 'journal_provenance_verified' or p['get_run_success_claim'] is not False or p['private_readback_required'] is not True
                or p['terminal_report']['write_state'] != 'unknown'
                or h != _make_hold(b, h['note_id'], h['page_id'])):
            raise ValueError('authenticated_recovery_invalid')
        o = _codec(); o.e.p.note_id(h['note_id']); o.e.p.notion_id(h['page_id'])
    return deepcopy(entries)


def _make_hold(b, note, page):
    p = b['proof']
    return {'contract': 'get-notion-exact-uncertain-hold-1', 'note_id': note, 'page_id': page,
        'anchor': deepcopy(p['anchor']), 'run_id': p['run_id'], 'run_attempt': p['run_attempt'],
        'checkpoint_sha256': p['checkpoint']['record_sha256'], 'plan_sha256': b['plan_sha256'],
        'record_index': b['initial_index'], 'operation_sha256': b['operation_sha256'],
        'reason': 'uncertain_append', 'effects': 'unresolved', 'retry_allowed': False}


def _append_run_binding(run_id, attempt=None):
    found = [b for b in registry() if type(run_id) is int and b['proof']['run_id'] == run_id
             and (attempt is None or type(attempt) is int and b['proof']['run_attempt'] == attempt)]
    return found[0] if len(found) == 1 else None


def run_binding(run_id, attempt=None):
    old = _append_run_binding(run_id, attempt)
    new = create_resume.run_binding(run_id, attempt)
    if old is not None and new is not None: raise ValueError('authenticated_recovery_invalid')
    return new if new is not None else old


def plan_binding(plan):
    old = _append_plan_binding(plan)
    new = create_resume.plan_binding(plan)
    if old is not None and new is not None: raise ValueError('authenticated_recovery_invalid')
    return new if new is not None else old


def registered_run(run_id, attempt=None):
    return run_binding(run_id, attempt) is not None


def candidate_policy(policy, run):
    if create_resume.run_binding(run.get("id"), run.get("run_attempt")) is not None:
        return create_resume.candidate_policy(policy, run)
    import get_publication_provenance as p
    import get_recurring_entry as entry
    import get_recurring_controller as controller
    import get_recurring_policy_recovery as recovery
    b = _append_run_binding(run.get('id'), run.get('run_attempt'))
    if not b: return policy, False
    old = b['public_policy']
    stable = set(old) - {'code_sha', 'rules_sha256', 'get_policy_sha256'}
    if (p.run_binding(run) in (b['proof']['run_binding'], b['proof']['latest_binding'])
            and all(policy.get(k) == old[k] for k in stable)
            and policy.get('code_sha') != old['code_sha']
            and policy.get('rules_sha256') == entry.rules_sha256()
            and policy.get('get_policy_sha256') == recovery.current_get_policy()
            and policy.get('get_policy_sha256') != old['get_policy_sha256']
            and policy.get('controller_template_sha256') == controller.template_sha256()):
        return deepcopy(old), True
    return policy, False


def recover_checkpoint(client, run_id, run_attempt):
    if create_resume.run_binding(run_id, run_attempt) is not None:
        return create_resume.recover_checkpoint(client, run_id, run_attempt)
    import get_notion_journal_runtime as j
    b = _append_run_binding(run_id, run_attempt)
    if not b: j.fail('journal_provenance_other_controller')
    result = j.recover_anchor(client, run_id=run_id, run_attempt=run_attempt,
        public_policy=b['public_policy'], approved_public_policy_sha256=b['public_policy_sha256'],
        notion_policy_sha256=b['notion_policy_sha256'], include_job_digest=True, include_terminal_report=True)
    keys = ('anchor', 'checkpoint', 'checkpoint_count', 'run_id', 'run_attempt', 'job_id',
        'job_sha256', 'fixed_source_record', 'terminal_report', 'terminal_conclusions', 'run_binding')
    if any(result.get(k) != b['proof'].get(k) for k in keys): j.fail('journal_provenance_record')
    return {**result, 'recovery_anchor': TAG}


def _append_plan_binding(plan):
    o = _codec()
    matches = [b for b in registry() if o.digest(plan) == b['plan_sha256']]
    return matches[0] if len(matches) == 1 else None


def initial_state_valid(state, sha):
    try:
        o = _codec(); j = state['engine_state']['journal']; b = _append_plan_binding(j['plan'])
        if not b or o.digest(state) != sha or sha != b['proof']['anchor']['state_sha256']: return False
        row = j['plan']['records'][b['initial_index']]; op = j['operation']; h = b['hold']
        return (j['plan_sha256'] == b['plan_sha256'] and j['index'] == b['initial_index']
            and len(j['plan']['records']) == b['record_count'] and o.digest(op) == b['operation_sha256']
            and op['kind'] == 'append' and op['phase'] == 'in_flight' and op['response'] is None
            and row['note_id'] == h['note_id'] and row['page_id'] == h['page_id']
            and row['note_id'] not in state['engine_state']['receipts']
            and row['note_id'] not in state['engine_state'].get('unresolved_by_id', {}))
    except Exception:
        return False


def inflight_holds():
    return [deepcopy(b['hold']) for b in registry()]


def exact_hold_present(engine):
    o = _codec(); holds = engine.get('unresolved_by_id', {})
    return any(o.encoded(holds.get(h['note_id'])) == o.encoded(h) for h in inflight_holds())


def allows(plan, journal):
    """Dispatch exact create registrations; retain original append verifier."""
    if create_resume.plan_binding(plan) is not None:
        return create_resume.allows(plan, journal)
    try:
        o = _codec(); b = _append_plan_binding(plan)
        if (not b or set(journal) != {'plan_sha256', 'plan', 'index', 'operation'}
                or journal.get('plan_sha256') != b['plan_sha256'] or o.encoded(journal.get('plan')) != o.encoded(plan)
                or plan.get('historical_only') is not True or plan.get('property_only')
                or plan.get('historical_manifest_sha256') != b['manifest_sha256']
                or len(plan['records']) != b['record_count']
                or any(r['action'] != 'update' or r['old_roots'] for r in plan['records'][b['initial_index']:])): return False
        index = journal['index']
        if type(index) is not int or not b['initial_index'] <= index <= b['record_count']: return False
        import get_notion_historical_active_recovery as original
        return original._operation_valid(plan, journal,
            {**b, 'initial_operation_sha256': b['operation_sha256']})
    except Exception:
        return False


def isolation_successors(binding):
    """Code-pinned original checkpoint proofs, never learned from live state."""
    o = _codec()
    values = bindings.value('get_notion_authenticated_recovery', 'ISOLATION_SUCCESSORS')
    if not isinstance(values, list) or len(values) > 16: o.fail('outbox_state_invalid')
    found = []
    for item in values:
        if set(item) != {'original_run_id','original_run_attempt','policy_sha256','proof'}: o.fail('outbox_state_invalid')
        proof = item['proof']
        if (proof.get('status') != 'journal_provenance_verified'
                or proof.get('private_readback_required') is not True
                or proof.get('terminal_report',{}).get('journal_write_state') != 'confirmed'
                or proof.get('checkpoint_count') != 1
                or proof.get('anchor',{}).get('state_sha256') != proof.get('checkpoint',{}).get('state_sha256')):
            o.fail('outbox_state_invalid')
        if (item['original_run_id'],item['original_run_attempt']) == (binding['proof']['run_id'],binding['proof']['run_attempt']):
            found.append(item)
    if len({v['policy_sha256'] for v in found}) != len(found): o.fail('outbox_state_invalid')
    return found


def prior_isolation(state):
    o = _codec(); j = state['engine_state']['journal']; b = _append_plan_binding(j['plan']) if j else None
    if not b or not allows(j['plan'],j) or not exact_hold_present(state['engine_state']): return None
    matches = [v for v in isolation_successors(b) if v['policy_sha256'] == state['policy_sha256']
               and v['proof']['anchor']['state_sha256'] == o.digest(state)
               and v['proof']['anchor']['state_blob'] == o.e.p.git_blob(o.encoded(state))]
    return matches[0] if len(matches) == 1 else None


def transition_isolated_policy(state, sha, new_policy):
    o = _codec()
    import get_recurring_policy_recovery as policy
    if o.digest(state) != sha or not prior_isolation(state) or new_policy != policy.current_get_policy(): o.fail('outbox_state_invalid')
    result = deepcopy(state); result['policy_sha256'] = new_policy
    return result


def _isolated_variant(state, new_policy):
    b = _append_plan_binding(state['engine_state']['journal']['plan'])
    result = deepcopy(state); result['policy_sha256'] = new_policy
    result['engine_state'].setdefault('unresolved_by_id', {})[b['hold']['note_id']] = deepcopy(b['hold'])
    result['engine_state']['journal']['index'] += 1
    result['engine_state']['journal']['operation'] = None
    return result


def isolate_outer(state, sha, new_policy):
    o = _codec()
    import get_recurring_policy_recovery as policy
    if not initial_state_valid(state, sha) or new_policy != policy.current_get_policy(): o.fail('outbox_state_invalid')
    return _isolated_variant(state,new_policy)


def check_mode(state, found, manifest, expected, other_modes):
    active = state['engine_state']['journal']
    if found.get('recovery_anchor') == create_resume.TAG or (active and create_resume.plan_binding(active['plan']) is not None):
        return create_resume.check_mode(state, found, manifest, expected, other_modes)
    o = _codec(); j = state['engine_state']['journal']; b = _append_plan_binding(j['plan']) if j else None
    if found.get('recovery_anchor') != TAG: return
    if not b or any(other_modes) or expected != b['selection_sha256']: o.fail('outbox_state_invalid')
    import get_notion_historical_input as inputs
    members = inputs.validate_apply_selection(manifest, expected, apply=True)
    if not any(d == b['manifest_sha256'] and m == j['plan']['historical_manifest'] for m,d in members): o.fail('outbox_state_invalid')


def verify_successor(local, remote, loader, head, proof):
    """Authenticate a persisted isolation whose acknowledgement was lost.

    Only one state-only exact isolation commit is admitted. Unrelated commits
    must leave both the journal and every Get source entry untouched. Caller
    still constructs the original RemoteJournal and runs all current guards.
    """
    from types import SimpleNamespace
    import get_notion_remote_journal as j
    import get_recurring_policy_recovery as policy
    o = _codec(); b = _append_run_binding(proof.get('run_id'), proof.get('run_attempt'))
    if (not b or proof.get('recovery_anchor') != TAG or proof.get('anchor') != b['proof']['anchor']
            or proof.get('checkpoint') != b['proof']['checkpoint']): o.fail('outbox_state_invalid')
    if remote.ref(j.REF) != head: o.fail('outbox_state_changed')
    git = j.retention.LocalGit(local.vault)
    def node(commit):
        loader(commit)
        tree, entries = j.RemoteJournal._local_tree(SimpleNamespace(local=git), commit)
        parents = git.run('rev-list', '--parents', '-n', '1', commit).decode().split()[1:]
        if remote.commit(commit) != {'tree': tree, 'parents': parents}: o.fail('outbox_state_invalid')
        return tree, parents, entries
    anchor = deepcopy(b['proof']['anchor']); prior = anchor['commit']; base = node(prior)
    if base[2].get(j.STATE_PATH) != ('100644', 'blob', anchor['state_blob']): o.fail('outbox_state_invalid')
    raw = git.blob(anchor['state_blob'])
    if (len(raw) != b['proof']['checkpoint']['state_bytes'] or o.digest(raw) != anchor['state_sha256']
            or j.retention.oid(raw) != anchor['state_blob'] or remote.blob(anchor['state_blob'], len(raw)) != raw): o.fail('outbox_state_invalid')
    original = j.retention.strict_json(raw)
    if not initial_state_valid(original, anchor['state_sha256']): o.fail('outbox_state_invalid')
    isolated = isolate_outer(original, anchor['state_sha256'], policy.current_get_policy())
    isolated_raw = o.encoded(isolated); isolated_blob = j.retention.oid(isolated_raw)
    variants = {isolated_blob:isolated_raw}
    for item in isolation_successors(b):
        raw_variant = o.encoded(_isolated_variant(original,item['policy_sha256']))
        if (o.digest(raw_variant) != item['proof']['anchor']['state_sha256']
                or j.retention.oid(raw_variant) != item['proof']['anchor']['state_blob']): o.fail('outbox_state_invalid')
        variants[j.retention.oid(raw_variant)] = raw_variant
    previous_state = original; upgraded = False
    node(head)
    git.run('merge-base', '--is-ancestor', prior, head)
    history = git.run('rev-list', '--reverse', '--max-count=257', prior + '..' + head).decode().split()
    if len(history) > 256: o.fail('outbox_state_invalid')
    seen = False
    for commit in history:
        current = node(commit)
        if current[1] != [prior]: o.fail('outbox_state_invalid')
        changed = {k for k in set(base[2]) | set(current[2]) if base[2].get(k) != current[2].get(k)}
        if j.STATE_PATH in changed:
            if changed != {j.STATE_PATH}: o.fail('outbox_state_invalid')
            row = current[2][j.STATE_PATH]
            if row[:2] != ('100644','blob'): o.fail('outbox_state_invalid')
            if not seen:
                wanted = variants.get(row[2])
                if wanted is None: o.fail('outbox_state_invalid')
                seen = True
            else:
                if upgraded or not prior_isolation(previous_state): o.fail('outbox_state_invalid')
                wanted = o.encoded(transition_isolated_policy(previous_state,o.digest(previous_state),policy.current_get_policy()))
                if row[2] != j.retention.oid(wanted): o.fail('outbox_state_invalid')
                upgraded = True
            if git.blob(row[2]) != wanted or remote.blob(row[2],len(wanted)) != wanted: o.fail('outbox_state_invalid')
            previous_state = j.retention.strict_json(wanted)
            anchor = {'commit':commit,'state_blob':row[2],'state_sha256':o.digest(wanted)}
        elif any(k == 'Raw/03 Get' or k.startswith('Raw/03 Get/') for k in changed): o.fail('outbox_state_invalid')
        if current[2].get(j.STATE_PATH) != ('100644', 'blob', anchor['state_blob']): o.fail('outbox_state_invalid')
        prior = commit; base = current
    if remote.ref(j.REF) != head: o.fail('outbox_state_changed')
    return anchor


def transition_manifest(manifest, expected, plan):
    o = _codec(); b = _append_plan_binding(plan)
    if not b: return manifest, expected
    if expected != b['selection_sha256']: o.fail('outbox_state_invalid')
    import get_notion_historical_input as inputs
    found = [(m, d) for m, d in inputs.validate_apply_selection(manifest, expected, apply=True)
             if d == b['manifest_sha256'] and m == plan['historical_manifest']]
    if len(found) != 1: o.fail('outbox_state_invalid')
    return found[0]


def preserve(before, after):
    """CAS-level monotonicity in addition to external checkpoint authentication."""
    active = before['engine_state']['journal']
    if active and create_resume.plan_binding(active['plan']) is not None:
        return create_resume.preserve(before, after)
    import get_notion_outbox as o
    old = before['engine_state']['journal']
    b = _append_plan_binding(old['plan']) if old else None
    if not b: return
    if old is None or old.get('plan_sha256') != b['plan_sha256']: return
    if not allows(old['plan'], old): o.fail('outbox_state_invalid')
    new = after['engine_state']['journal']
    for k in before:
        if k not in ('policy_sha256','engine_state') and before[k] != after[k]: o.fail('outbox_state_invalid')
    import get_recurring_policy_recovery as recovery
    if initial_state_valid(before, o.digest(before)):
        if o.encoded(after) in (o.encoded(before), o.encoded(isolate_outer(before, o.digest(before), recovery.current_get_policy()))): return
        o.fail('outbox_state_invalid')
    if prior_isolation(before):
        if o.encoded(after) in (o.encoded(before),o.encoded(transition_isolated_policy(before,o.digest(before),recovery.current_get_policy()))): return
        o.fail('outbox_state_invalid')
    if before['policy_sha256'] != after['policy_sha256']: o.fail('outbox_state_invalid')
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
    if old['index']==b['initial_index'] and (a['phase'],z['phase'])==('in_flight','prepared'):o.fail('outbox_state_invalid')
    if (a['phase'],z['phase']) not in phases: o.fail('outbox_state_invalid')
    if a['phase'] == 'in_flight' and z['phase'] == 'prepared' and 'write_failure' in a: o.fail('outbox_state_invalid')
    if z['kind'] == 'append':
        permitted = {'phase','response'} if z['phase'] == 'acknowledged' else {'phase'}
        if set(a) != set(z) or any(z[k] != v for k,v in a.items() if k not in permitted): o.fail('outbox_state_invalid')
    elif z['kind'] == 'cleanup':
        # appended() above validates the new native snapshot, roots and body.
        if any(z.get(k) != v for k,v in a.items() if k not in ('kind','phase')): o.fail('outbox_state_invalid')
    else: o.fail('outbox_state_invalid')
