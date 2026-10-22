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
PROGRESS_BINDING = _private_bindings.value('get_notion_historical_active_recovery', 'PROGRESS_BINDING')
UNCONFIRMED_PREPARED_BINDING = _private_bindings.value('get_notion_historical_active_recovery', 'UNCONFIRMED_PREPARED_BINDING')
PREPARED_TAG = 'exact_registered_unconfirmed_prepared_v1'


def _validated_binding(value, *, progress=False):
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
        if not isinstance(b,dict) or set(b) != (keys | {'latest_binding'} if progress else keys): return None
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
        if 'latest_binding' in b:
            if (p.run_binding(b['latest_binding']) != b['latest_binding']
                    or p.run_semantic_binding(b['latest_binding']) != p.run_semantic_binding(b['run_binding'])): return None
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



def progress_binding():
    import get_notion_outbox as o
    base = binding(); prior = successor_binding(); progress = _validated_binding(PROGRESS_BINDING, progress=True)
    same = ('plan_sha256','plan_bytes','engine_rules_sha256','manifest_sha256')
    if (not base or not prior or not progress or 'latest_binding' not in progress
            or any(base[k] != progress[k] for k in same)
            or progress['initial_index'] != base['initial_index'] + 1
            or progress['initial_operation_sha256'] != o.digest(None)
            or progress['run_binding']['id'] in (base['run_binding']['id'], prior['run_binding']['id'])
            or progress['public_policy'] in (base['public_policy'], prior['public_policy'])):
        return None
    return progress


def _run_binding(run_id, attempt=None):
    for b in (binding(), successor_binding(), progress_binding(), prepared_binding()):
        if (b and type(run_id) is int and run_id == b['run_binding']['id']
                and (attempt is None or type(attempt) is int and attempt == b['run_binding']['run_attempt'])):
            return b
    return None


def _state_binding(sha):
    for b in (binding(), successor_binding(), progress_binding(), prepared_binding()):
        if b and sha == b.get('resume_anchor', b['anchor'])['state_sha256']: return b
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
    if (p.run_binding(run) in (b['run_binding'], b.get('latest_binding', b['run_binding']))
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
    if b==prepared_binding(): return recover_prepared_checkpoint(client,b)
    # The full original verifier reconstructs compact proofs using the private
    # persisted records. Saved expanded logs never replace production retrieval.
    result = c.recover_anchor(client, run_id=run_id, run_attempt=run_attempt,
        public_policy=deepcopy(b['public_policy']),
        approved_public_policy_sha256=b['public_policy_sha256'],
        notion_policy_sha256=b['notion_policy_sha256'], require_success=True)
    expected = {'anchor': b['anchor'], 'job_id': b['job_id'],
        'job_sha256': b['job_sha256'], 'checkpoint_count': b['checkpoint_count'],
        'checkpoint': b['checkpoint'], 'get_step_record': b['get_record'],
        'run_binding': b['run_binding'], 'latest_binding': b.get('latest_binding', b['run_binding']),
        'private_readback_required': True, 'clock_witness': None}
    if any(result.get(k) != v for k, v in expected.items()): c.fail('journal_provenance_record')
    return {**result, 'recovery_anchor': TAG}


def _operation_valid(plan, journal, b):
    import get_notion_executor as e
    index = journal['index']; operation = journal['operation']
    if index == b['initial_index'] and b!=prepared_binding():
        return operation is not None and e.digest(operation) == b['initial_operation_sha256']
    if index == b['initial_index'] and operation is not None and operation.get('phase')=='prepared':
        return e.digest(operation)==b['initial_operation_sha256']
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
    b = plan_binding(plan)
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
        return bool(b and sha == b.get('resume_anchor', b['anchor'])['state_sha256'] == o.digest(raw)
            and len(raw) == b['state_bytes'] and retention.oid(raw) == b.get('resume_anchor', b['anchor'])['state_blob']
            and state['policy_sha256'] == b['public_policy']['get_policy_sha256']
            and allows(j['plan'], j) and j['index'] == b['initial_index'])
    except Exception:
        return False


def check_mode(state, found, manifest, expected, other_modes):
    import get_notion_outbox as o
    j = state['engine_state']['journal']; b = plan_binding(j['plan']) if j else binding()
    if found.get('recovery_anchor') in (TAG, PREPARED_TAG) or b and j and j.get('plan_sha256') == b['plan_sha256']:
        if (not b or any(x is not None for x in other_modes) or manifest is None
                or expected != b['manifest_sha256'] or o.digest(manifest) != expected
                or j is None or not allows(j['plan'], j)
                or manifest != j['plan']['historical_manifest']): o.fail('outbox_state_invalid')


def transition_outer(state, sha, new_policy, *, proof):
    import get_notion_outbox as o
    import get_recurring_policy_recovery as recovery
    b = _state_binding(sha)
    if (not initial_state_valid(state, sha) or not b
            or proof.get('recovery_anchor') != (PREPARED_TAG if b==prepared_binding() else TAG)
            or proof.get('status') != 'journal_provenance_verified'
            or proof.get('private_readback_required') is not True
            or proof.get('latest_binding') != b.get('latest_binding', b['run_binding'])
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
    old = before['engine_state']['journal']
    b = plan_binding(old['plan']) if old else binding()
    if not b: return
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

def prepared_binding():
    """One code-pinned failed fixed run and its separately verified successor."""
    import get_notion_outbox as o
    import get_publication_provenance as p
    try:
        b = deepcopy(UNCONFIRMED_PREPARED_BINDING)
        keys = {'public_policy','public_policy_sha256','notion_policy_sha256','run_binding',
            'job_id','job_sha256','get_record','fixed_source_record','checkpoint_count','latest_binding',
            'checkpoint','anchor','resume_anchor','resume_tree','original_state_bytes',
            'state_bytes','plan_sha256','plan_bytes','engine_rules_sha256','manifest_sha256',
            'initial_index','initial_operation_sha256','known_descendants','deployment_objects'}
        if not isinstance(b,dict) or set(b)!=keys: return None
        p.validate_policy(b['public_policy'],b['public_policy_sha256'])
        if (p.run_binding(b['run_binding'])!=b['run_binding'] or b['run_binding']['status']!='completed'
                or b['run_binding']['conclusion']!='failure' or b['get_record'] is not None
                or p.run_binding(b['latest_binding'])!=b['latest_binding']
                or any(b['latest_binding'][k]!=v for k,v in b['run_binding'].items() if k!='updated_at')
                or b['checkpoint_count']!=31 or b['initial_index']!=6): return None
        for key in ('job_sha256','notion_policy_sha256','plan_sha256','engine_rules_sha256','manifest_sha256','initial_operation_sha256'):
            p.a.checked(b[key],64)
        c=b['checkpoint']; a=b['anchor']; z=b['resume_anchor']; run=b['run_binding']; policy=b['public_policy']
        if (a!={k:c[k] for k in ('commit','state_blob','state_sha256')}
                or c['sequence']!=b['checkpoint_count'] or c['state_bytes']!=b['original_state_bytes']
                or c['record_sha256']!=o.digest({k:v for k,v in c.items() if k!='record_sha256'})
                or c['readback_verified'] is not True or c['run_id']!=run['id'] or c['run_attempt']!=run['run_attempt']
                or c['code_sha']!=policy['code_sha'] or c['controller_sha']!=run['head_sha']
                or c['rules_sha256']!=policy['rules_sha256'] or c['public_policy_sha256']!=b['public_policy_sha256']
                or c['notion_policy_sha256']!=b['notion_policy_sha256']
                or set(z)!={'commit','state_blob','state_sha256'} or a==z): return None
        for obj in (a,z):
            p.a.checked(obj['commit'],40);p.a.checked(obj['state_blob'],40);p.a.checked(obj['state_sha256'],64)
        p.a.checked(b['resume_tree'],40)
        for key in ('state_bytes','original_state_bytes','plan_bytes'):
            if type(b[key]) is not int or not 0<b[key]<=o.e.MAX_STATE:return None
        from get_notion_fixed_source import parse
        ctx={k:c[k] for k in ('run_id','run_attempt','code_sha','controller_sha','rules_sha256','notion_policy_sha256','public_policy_sha256')}
        if parse(o.encoded(b['fixed_source_record']),ctx,run)!=b['fixed_source_record']:return None
        if b['fixed_source_record']['mode']!='historical-apply' or b['fixed_source_record']['selection_sha256']!=b['manifest_sha256']:return None
        if not isinstance(b['known_descendants'],list) or len(b['known_descendants'])!=2:return None
        parent=z['commit']
        for entry in b['known_descendants']:
            if not isinstance(entry,dict) or set(entry)!={'parent','commit','changes'} or entry['parent']!=parent:return None
            p.a.checked(entry['commit'],40)
            if not isinstance(entry['changes'],dict) or not entry['changes']:return None
            for path,entry_row in entry['changes'].items():
                if not isinstance(path,str) or not path.startswith('Raw/') or '..' in path.split('/') or path.startswith('Raw/03 Get/'):return None
                if not isinstance(entry_row,list) or len(entry_row)!=3 or entry_row[:2]!=['100644','blob']:return None
                p.a.checked(entry_row[2],40)
            parent=entry['commit']
        if not isinstance(b['deployment_objects'],dict) or len(b['deployment_objects'])>8:return None
        for path,oid in b['deployment_objects'].items():
            if not path.startswith('.sync/get-notion/recovery-evidence/') or '..' in path.split('/'):return None
            p.a.checked(oid,40)
        return b
    except Exception:
        return None


def plan_binding(plan):
    b=prepared_binding()
    if b and isinstance(plan,dict) and plan.get('historical_manifest_sha256')==b['manifest_sha256']:
        return b
    return binding()


def recover_prepared_checkpoint(client, b):
    import get_notion_journal_runtime as c
    result=c.recover_anchor(client,run_id=b['run_binding']['id'],run_attempt=b['run_binding']['run_attempt'],
        public_policy=deepcopy(b['public_policy']),approved_public_policy_sha256=b['public_policy_sha256'],
        notion_policy_sha256=b['notion_policy_sha256'],include_job_digest=True)
    expected={'anchor':b['anchor'],'checkpoint':b['checkpoint'],'checkpoint_count':b['checkpoint_count'],
        'job_id':b['job_id'],'job_sha256':b['job_sha256'],'run_binding':b['run_binding'],
        'latest_binding':b['latest_binding'],'get_step_record':None,'fixed_source_record':b['fixed_source_record'],
        'private_readback_required':True,'get_run_success_claim':False}
    if any(result.get(k)!=v for k,v in expected.items()):c.fail('journal_provenance_record')
    return {**result,'recovery_anchor':PREPARED_TAG}


def verify_prepared_successor(local, remote, loader, head, proof):
    """Read-only dual-anchor authentication; no journal store exists yet."""
    import get_notion_outbox as o
    import get_notion_remote_journal as j
    from types import SimpleNamespace
    b=prepared_binding()
    if (not b or proof.get('recovery_anchor')!=PREPARED_TAG or proof.get('anchor')!=b['anchor']
            or proof.get('checkpoint')!=b['checkpoint'] or proof.get('fixed_source_record')!=b['fixed_source_record']):o.fail('outbox_state_invalid')
    git=j.retention.LocalGit(local.vault)
    def node(commit):
        loader(commit)
        tree,entries=j.RemoteJournal._local_tree(SimpleNamespace(local=git),commit)
        parents=git.run('rev-list','--parents','-n','1',commit).decode().split()[1:]
        if remote.commit(commit)!={'tree':tree,'parents':parents}:o.fail('outbox_state_invalid')
        return tree,parents,entries
    def state(commit,anchor,maximum):
        tree,parents,entries=node(commit);row=entries.get(j.STATE_PATH)
        if row!=('100644','blob',anchor['state_blob']):o.fail('outbox_state_invalid')
        raw=git.blob(anchor['state_blob'])
        if (len(raw)!=maximum or o.digest(raw)!=anchor['state_sha256'] or j.retention.oid(raw)!=anchor['state_blob']
                or remote.blob(anchor['state_blob'],len(raw))!=raw):o.fail('outbox_state_invalid')
        value=j.retention.strict_json(raw);o.check_state(value)
        if o.encoded(value)!=raw:o.fail('outbox_state_invalid')
        return value,(tree,parents,entries)
    before,old=state(b['anchor']['commit'],b['anchor'],b['original_state_bytes'])
    after,new=state(b['resume_anchor']['commit'],b['resume_anchor'],b['state_bytes'])
    if new[0]!=b['resume_tree'] or new[1]!=[b['anchor']['commit']]:o.fail('outbox_state_invalid')
    changed={k for k in set(old[2])|set(new[2]) if old[2].get(k)!=new[2].get(k)}
    if changed!={j.STATE_PATH}:o.fail('outbox_state_invalid')
    a=before['engine_state']['journal'];z=after['engine_state']['journal'];op=z['operation']
    normalized=deepcopy(after);normalized['engine_state']['journal']['operation']=None
    if (normalized!=before or a['index']!=b['initial_index'] or a['operation'] is not None
            or o.digest(op)!=b['initial_operation_sha256'] or not initial_state_valid(after,o.digest(after))
            or set(op)!={'kind','phase','before','response','archive_index'}
            or (op['kind'],op['phase'],op['response'],op['archive_index'])!=('append','prepared',None,0)):
        o.fail('outbox_state_invalid')
    row=z['plan']['records'][z['index']]
    captured=[v['snapshots'][row['note_id']] for v in before['historical_captures'].values() if row['note_id'] in v['snapshots']]
    if len(captured)!=1 or op['before']!=captured[0] or o.e.target_sha(captured[0],row['note_id'],z['plan']['policy']['data_source_id'])!=row['before_sha256']:o.fail('outbox_state_invalid')
    history=git.run('rev-list','--reverse','--max-count=257',b['resume_anchor']['commit']+'..'+head).decode().split()
    git.run('merge-base','--is-ancestor',b['resume_anchor']['commit'],head)
    if not 2<=len(history)<=256:o.fail('outbox_state_invalid')
    prior=b['resume_anchor']['commit'];prior_node=new
    allowed=dict(b['deployment_objects'])
    capsule_path='.sync/get-notion/runtime-bindings-'+_private_bindings.TRUSTED_BUNDLE_SHA256+'.json'
    import os
    raw_path=os.environ[_private_bindings.ENV_FILE]
    _private_bindings.read_local(raw_path,_private_bindings.TRUSTED_BUNDLE_SHA256)
    # Re-read only exact code-pinned bytes; no unbounded or link-following read.
    fd=os.open(raw_path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as handle:
        raw_bytes=handle.read(_private_bindings.MAX_BYTES+1)
    if len(raw_bytes)>_private_bindings.MAX_BYTES or o.digest(raw_bytes)!=_private_bindings.TRUSTED_BUNDLE_SHA256:o.fail('outbox_state_invalid')
    allowed[capsule_path]=j.retention.oid(raw_bytes)
    for i,commit in enumerate(history):
        current=node(commit)
        if current[1]!=[prior]:o.fail('outbox_state_invalid')
        delta={k for k in set(prior_node[2])|set(current[2]) if prior_node[2].get(k)!=current[2].get(k)}
        if i<2:
            expected=b['known_descendants'][i]
            if commit!=expected['commit'] or prior!=expected['parent'] or delta!=set(expected['changes']):o.fail('outbox_state_invalid')
            if any(current[2].get(k)!=tuple(v) for k,v in expected['changes'].items()):o.fail('outbox_state_invalid')
        elif any(k not in allowed or current[2].get(k)!=('100644','blob',allowed[k]) or k in prior_node[2] for k in delta):o.fail('outbox_state_invalid')
        if current[2].get(j.STATE_PATH)!=new[2][j.STATE_PATH]:o.fail('outbox_state_invalid')
        prior=commit;prior_node=current
    if remote.ref(j.REF)!=head:o.fail('outbox_state_changed')
    return deepcopy(b['resume_anchor'])
