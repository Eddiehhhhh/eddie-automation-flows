"""Bounded ordinary historical CREATE admission; no new durable protocol.

Uses existing native canonical snapshot cap, never a measured average receipt.
No budget is consumed or changed by this pure calculation.
"""
from copy import deepcopy

def reserve(engine, native, outer, plan):
    if engine.metadata_pending.enabled(plan):
        raise ValueError('unsupported metadata completion')
    value=deepcopy(outer)
    journal=value['engine_state']['journal']
    initial=journal is None
    if initial:
        journal={'plan_sha256':engine.digest(plan),'plan':deepcopy(plan),'index':0,'operation':None}
        value['engine_state']['journal']=journal
    if journal['plan']!=plan or journal['plan_sha256']!=engine.digest(plan) or journal['operation'] is not None:
        raise ValueError('not a clean matching boundary')
    index=journal['index']
    if type(index)is not int or not 0<=index<len(plan['records']):
        raise ValueError('no pending row')
    row=plan['records'][index]; ident=row['note_id']
    if (row['action']!='create' or row.get('create_batches') or row.get('property_only')
            or row.get('source_assets') or row.get('preserved_asset_bindings')
            or ident in value['engine_state']['receipts']):
        raise ValueError('not an ordinary first create')
    def count(rows):
        return sum(1+count(r['children']) for r in rows)
    nodes=count(row['rendered'])
    if nodes>engine.MAX_WRITE_BLOCKS:raise ValueError('not a single request')
    def size():return len(engine.encoded(value))
    stages={}
    if initial:stages['plan']=size()
    absence={'complete':True,'native_blocks_available':True,'identity_query_complete':True,'exact_id_matches':[],'ownership_adopted':False}
    operation={'kind':'create','phase':'prepared','before':absence,'response':None,'archive_index':0}
    journal['operation']=operation;stages['prepared']=size()
    operation['phase']='in_flight';stages['in_flight']=size()
    # This superset covers acknowledged UUID, exact write-failure evidence, or
    # restoring prepared without making another external request.
    operation['phase']='acknowledged';operation['response']='0'*36
    operation['write_failure']={'http_status':599,'request_fingerprint':'0'*64}
    stages['ack_or_restore_or_failure']=size()
    receipt={'note_id':ident,'page_id':'0'*36,'data_source_id':plan['policy']['data_source_id'],
        'source_commit':plan['publication']['commit'],'md_blob':row['source']['blob'],
        'renderer_version':engine.p.RENDERER,'managed_blocks':[{'id':'0'*36,'normalized_hash':'0'*64} for _ in range(nodes)],
        'protected_blocks':[],'properties_baseline':{},'asset_bindings':{},'readback_verified':True,
        'creation_plan_sha256':engine.digest(plan)}
    state=value['engine_state'];state['receipts'][ident]=receipt
    engine.property_transaction.record_creation(state,row,plan)
    state['failures'].pop(ident,None)
    journal.update(index=index+1,operation=None)
    # properties_baseline is a subtree of the canonical native snapshot whose
    # length was checked BEFORE the original receipt path, native.MAX_BYTES.
    stages['receipt']=size()+native.MAX_BYTES
    state['journal']=None;stages['clear']=size()+native.MAX_BYTES
    return {'stages':stages,'bytes':sum(stages.values()),'commits':len(stages),
        'checkpoints':len(stages)+1,'max_state':max(stages.values())}

def admits(bound, durable, emitter, max_checkpoints, max_state):
    if durable.poisoned or emitter.failed:raise ValueError('journal failed')
    return (bound['max_state']<=max_state
        and durable.new_blob_bytes+bound['bytes']<=durable.max_batch_bytes
        and durable.commits_attempted+bound['commits']<=durable.max_commits
        and emitter.sequence+bound['checkpoints']<=max_checkpoints)

def settle_clean_yield(engine, store, plan, result):
    """Original EngineStore CAS, never clear a pending operation.

    Store must be the original EngineStore over RemoteJournal in any integration.
    Uses an existing checkpoint type; creates no receipt, state key or external write.
    """
    if result.get('status')!='partial' or result.get('reason')!='batch_boundary_yield':
        raise ValueError('not a clean budget yield')
    state,version=store.read();engine.check_state(state)
    if version!=engine.digest(state) or version!=result['state_sha256']:
        raise ValueError('state changed')
    journal=state['journal']
    if journal is None:return result
    digest=engine.digest(plan)
    if (journal['plan_sha256']!=digest or journal['plan']!=plan
            or journal['operation'] is not None or not plan.get('historical_only')
            or not plan.get('property_manifest_sha256')):
        raise ValueError('not a settled historical boundary')
    index=journal['index']
    if type(index)is not int or not 0<=index<len(plan['records']):
        raise ValueError('invalid prefix')
    # Mixed completed prefixes keep the original resumable clean journal.
    # They are not claimed to be canonical-refresh-ready by this narrow fix.
    if any(row['action']!='create' or row.get('property_only') for row in plan['records'][:index]):
        return result
    for row in plan['records'][:index]:
        ident=row['note_id'];receipt=state['receipts'].get(ident,{})
        if row['action']!='create' or 'property_created' not in row:
            raise ValueError('unsupported prefix')
        expected={'note_id':ident,'source_commit':plan['publication']['commit'],
            'md_blob':row['source']['blob'],'data_source_id':plan['policy']['data_source_id'],
            'creation_plan_sha256':digest,'readback_verified':True,'renderer_version':engine.p.RENDERER}
        if any(receipt.get(k)!=v for k,v in expected.items()) or ident in state['failures']:
            raise ValueError('unconfirmed prefix')
        expected_state=deepcopy(state)
        engine.property_transaction.record_creation(expected_state,row,plan)
        if state.get('property_receipts',{}).get(ident)!=expected_state['property_receipts'][ident]:
            raise ValueError('unconfirmed paired receipt')
    candidate=deepcopy(state);candidate['journal']=None
    new_version=store.compare_and_set(version,candidate)
    observed,observed_version=store.read();engine.check_state(observed)
    if observed_version!=new_version or new_version!=engine.digest(candidate) or not engine.same(observed,candidate):
        raise ValueError('clear not confirmed')
    return {**result,'state_sha256':new_version}


class Admission:
    def __init__(self, engine, native, durable, emitter, *, max_checkpoints, max_state):
        self.engine=engine;self.native=native;self.durable=durable;self.emitter=emitter
        self.max_checkpoints=max_checkpoints;self.max_state=max_state
        self.clean_boundary_denied=False

    def __call__(self, outer, plan):
        self.clean_boundary_denied=False
        e=self.engine
        try:
            if not plan.get('historical_only') or not plan.get('property_manifest_sha256') or not plan.get('records'):
                e.fail('approval_missing')
            active=outer['engine_state']['journal']
            index=active['index'] if active is not None else 0
            if type(index)is not int or not 0<=index<len(plan['records']):e.fail('state_invalid')
            row=plan['records'][index]
            # Existing multi-batch, blocked/noop and update routes retain their
            # original admission/recovery machinery. This is not a new engine.
            if row['action']!='create' or row.get('create_batches') or row.get('property_only'):
                return True
            if 'property_created' not in row:e.fail('approval_missing')
            if self.emitter.sequence+1>self.max_checkpoints:e.fail('request_budget')
            bound=reserve(e,self.native,outer,plan)
            # A fresh run cannot cure a per-state ceiling. Do not advertise
            # an endlessly repeatable partial yield for permanent headroom.
            if bound['max_state']>self.max_state:e.fail('request_budget')
            accepted=admits(bound,self.durable,self.emitter,self.max_checkpoints,self.max_state)
            self.clean_boundary_denied=not accepted
            return accepted
        except e.Blocked:raise
        except Exception:e.fail('approval_missing')

    def prepared_resume(self, outer, plan):
        """Only reserve a known-not-dispatched prepared single CREATE.

        Original clean-row hooks do not run for a saved operation. Never clear
        or alter that operation: insufficient headroom is a blocked resume,
        not a successful partial yield. Unknown/acknowledged recovery stays
        entirely with the original executor and cannot repeat CREATE.
        """
        journal=outer['engine_state']['journal']
        operation=journal.get('operation') if journal is not None else None
        if not operation or operation.get('kind')!='create' or operation.get('phase')!='prepared':
            return True
        if operation.get('response') is not None:self.engine.fail('state_invalid')
        row=plan['records'][journal['index']]
        if row['action']!='create' or row.get('create_batches') or row.get('property_only'):
            return True
        candidate=deepcopy(outer)
        candidate['engine_state']['journal']['operation']=None
        accepted=self(candidate,plan)
        self.clean_boundary_denied=False
        if not accepted:return False
        bound=reserve(self.engine,self.native,candidate,plan)
        # A restored legacy prepared operation can retain extra before/evidence
        # fields. Do not replace its actual byte size with our fixed absence.
        # Full current state + all possible changed fields bounds flight, ack,
        # known write-failure and restore, even if old extra fields coexist.
        growth=len(self.engine.encoded({'phase':'acknowledged','response':'0'*36,
            'write_failure':{'http_status':599,'request_fingerprint':'0'*64}}))
        original_bound=len(self.engine.encoded(outer))+growth
        for phase in ('prepared','in_flight','ack_or_restore_or_failure'):
            bound['stages'][phase]=max(bound['stages'][phase],original_bound)
        bound['bytes']=sum(bound['stages'].values())
        bound['max_state']=max(bound['stages'].values())
        if bound['max_state']>self.max_state:self.engine.fail('request_budget')
        return admits(bound,self.durable,self.emitter,self.max_checkpoints,self.max_state)

    def settle(self, store, plan, result):
        try:
            if self.durable.poisoned or self.emitter.failed:self.engine.fail('journal_failed')
            if getattr(self.durable,'locator',None) is not None:
                # The bounded current-commit locator can retain this active
                # boundary. Do not clear a partial batch merely to refresh a
                # search slot, and never call it idle or completely accepted.
                if result.get('status')!='partial' or result.get('reason')!='batch_boundary_yield':
                    raise ValueError('not a clean budget yield')
                state,version=store.read();self.engine.check_state(state)
                journal=state['journal']
                if (version!=self.engine.digest(state) or version!=result['state_sha256']
                        or journal is None or journal['plan']!=plan
                        or journal['plan_sha256']!=self.engine.digest(plan)
                        or journal['operation'] is not None
                        or not 0<=journal['index']<len(plan['records'])):
                    raise ValueError('not an active clean boundary')
                return result
            return settle_clean_yield(self.engine,store,plan,result)
        except self.engine.Blocked:raise
        except Exception:self.engine.fail('journal_failed')
