"""Exact authenticated unknown-create continuation, never inferred ownership.

One code-pinned plan/source/page grant; native snapshots remain runtime evidence.
No isolation, append authority, arbitrary plan or property-only permission.
"""
from copy import deepcopy
import get_private_runtime_bindings as bindings
TAG='exact_registered_create_resume_v1'
ACK_CONTRACT='get-notion-exact-acknowledged-create-receipt-1'
RECEIPT_CONTRACT='get-notion-exact-persisted-create-receipt-1'
KEYS={'proof','public_policy','public_policy_sha256','notion_policy_sha256',
      'plan_sha256','manifest_sha256','source_commit','initial_index','operation_sha256',
      'note_id','page_id','source','request_sha256','record_count','selection_sha256'}


def registry():
    import get_notion_outbox as o
    import get_notion_journal_runtime as c
    entries=bindings.value('get_notion_create_resume','REGISTRY')
    if not isinstance(entries,list) or len(entries)>16: o.fail('outbox_state_invalid')
    seen=set()
    for b in entries:
        if not isinstance(b,dict) or set(b) not in (KEYS,KEYS|{'acknowledged_successor'},KEYS|{'receipt_successor'}): o.fail('outbox_state_invalid')
        p=b['proof'];policy=c.provenance.validate_policy(b['public_policy'],b['public_policy_sha256'])
        for k in ('plan_sha256','manifest_sha256','operation_sha256','request_sha256','notion_policy_sha256','selection_sha256'):c.journal.checked(b[k],64)
        c.journal.checked(b['source_commit'],40);o.e.p.note_id(b['note_id']);o.e.p.notion_id(b['page_id'])
        if (type(b['initial_index']) is not int or type(b['record_count']) is not int
                or not 0<=b['initial_index']<b['record_count']<=o.e.MAX_RECORDS
                or not isinstance(b['source'],dict) or set(b['source'])!={'path','blob','sha256'}):o.fail('outbox_state_invalid')
        c.journal.checked(b['source']['blob'],40);c.journal.checked(b['source']['sha256'],64)
        key=(p['run_id'],p['run_attempt'])
        if key in seen:o.fail('outbox_state_invalid')
        seen.add(key)
        run=p['run_binding'];latest=p['latest_binding'];cp=p['checkpoint']
        if (c.provenance.run_binding(run)!=run or c.provenance.run_binding(latest)!=latest
                or run['event']!='workflow_dispatch' or run['status']!='completed' or run['conclusion']!='failure'
                or latest['status']!='completed' or latest['conclusion']!='failure'
                or latest['run_attempt']!=run['run_attempt'] or key!=(run['id'],run['run_attempt'])
                or p['status']!='journal_provenance_verified' or p.get('get_step_record') is not None
                or p.get('get_run_success_claim') is not False or p.get('private_readback_required') is not True
                or p.get('terminal_conclusions')!={'job':'failure','notion':'failure','get':'skipped'}
                or p.get('clock_witness') is not None):o.fail('outbox_state_invalid')
        c._same_run(run,latest,cross_endpoint=True)
        report=p['terminal_report']
        if (report.get('contract')!='get-private-runtime-report-2' or report.get('status')!='failed'
                or report.get('write_state')!='unknown' or report.get('reason')!='journal_failed'
                or report.get('business_health_asserted') is not False or report.get('complete_library_claimed') is not False):o.fail('outbox_state_invalid')
        context={'run_id':run['id'],'run_attempt':run['run_attempt'],'code_sha':policy['code_sha'],
            'controller_sha':run['head_sha'],'rules_sha256':policy['rules_sha256'],
            'notion_policy_sha256':b['notion_policy_sha256'],'public_policy_sha256':b['public_policy_sha256']}
        c.context(context)
        if (set(cp)!=c.RECORD_KEYS or any(cp[k]!=context[k] for k in c.CONTEXT_KEYS)
                or type(p['checkpoint_count']) is not int or not 1<=p['checkpoint_count']<=c.MAX_CHECKPOINTS
                or cp['sequence']!=p['checkpoint_count'] or cp['readback_verified'] is not True
                or cp['status']!='journal_readback_verified'
                or o.digest({k:v for k,v in cp.items() if k!='record_sha256'})!=cp['record_sha256']
                or p['anchor']!={k:cp[k] for k in ('commit','state_blob','state_sha256')}):o.fail('outbox_state_invalid')
        if 'acknowledged_successor' in b:
            successor=b['acknowledged_successor']
            if (not isinstance(successor,dict) or set(successor)!={'contract','anchor','tree','state_bytes','operation_sha256'}
                    or successor['contract']!=ACK_CONTRACT or not isinstance(successor['anchor'],dict)
                    or set(successor['anchor'])!={'commit','state_blob','state_sha256'}
                    or type(successor['state_bytes']) is not int or not 0<successor['state_bytes']<=c.journal.MAX_STATE):o.fail('outbox_state_invalid')
            for key,width in (('commit',40),('state_blob',40),('state_sha256',64)):
                c.journal.checked(successor['anchor'][key],width)
                if successor['anchor'][key]==p['anchor'][key]:o.fail('outbox_state_invalid')
            c.journal.checked(successor['tree'],40);c.journal.checked(successor['operation_sha256'],64)
            if successor['operation_sha256']==b['operation_sha256']:o.fail('outbox_state_invalid')
        if 'receipt_successor' in b:
            successor=b['receipt_successor']
            if (not isinstance(successor,dict) or set(successor)!={'contract','anchor','tree','state_bytes'}
                    or successor['contract']!=RECEIPT_CONTRACT or not isinstance(successor['anchor'],dict)
                    or set(successor['anchor'])!={'commit','state_blob','state_sha256'}
                    or type(successor['state_bytes']) is not int or not 0<successor['state_bytes']<=c.journal.MAX_STATE):o.fail('outbox_state_invalid')
            for key,width in (('commit',40),('state_blob',40),('state_sha256',64)):
                c.journal.checked(successor['anchor'][key],width)
                if successor['anchor'][key]==p['anchor'][key]:o.fail('outbox_state_invalid')
            c.journal.checked(successor['tree'],40)
            # This capability only yields before a remaining row. It never
            # makes the executor complete/re-enter the last original row.
            if b['initial_index']+1>=b['record_count']:o.fail('outbox_state_invalid')
        fixed=c.fixed_auth.parse(o.encoded(p['fixed_source_record']),context,run)
        if b['selection_sha256']!=b['manifest_sha256'] or fixed['mode']!='historical-apply' or fixed['selection_sha256']!=b['manifest_sha256']:o.fail('outbox_state_invalid')
    return deepcopy(entries)


def run_binding(run_id,attempt=None):
    rows=[b for b in registry() if type(run_id) is int and b['proof']['run_id']==run_id and (attempt is None or type(attempt) is int and b['proof']['run_attempt']==attempt)]
    return rows[0] if len(rows)==1 else None


def plan_binding(plan):
    import get_notion_executor as e
    rows=[b for b in registry() if isinstance(plan,dict) and e.digest(plan)==b['plan_sha256']]
    return rows[0] if len(rows)==1 else None


def candidate_policy(policy,run):
    import get_notion_idle_recovery as idle
    import get_publication_provenance as p
    b=run_binding(run.get('id'),run.get('run_attempt'))
    if b and p.run_binding(run) in (b['proof']['run_binding'],b['proof']['latest_binding']) and idle._forward_policy(policy,b):return deepcopy(b['public_policy']),True
    return policy,False


def recover_checkpoint(client,run_id,run_attempt):
    import get_notion_journal_runtime as c
    b=run_binding(run_id,run_attempt)
    if not b:c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(client,run_id=run_id,run_attempt=run_attempt,public_policy=b['public_policy'],
        approved_public_policy_sha256=b['public_policy_sha256'],notion_policy_sha256=b['notion_policy_sha256'],
        include_job_digest=True,include_terminal_report=True)
    keys=('anchor','checkpoint','checkpoint_count','run_id','run_attempt','job_id','job_sha256','fixed_source_record',
          'terminal_report','terminal_conclusions','run_binding','latest_binding','get_step_record','get_run_success_claim','private_readback_required')
    if any(result.get(k)!=b['proof'].get(k) for k in keys) or result.get('clock_witness') is not None:c.fail('journal_provenance_record')
    return {**result,'recovery_anchor':TAG}


def request_sha(row,plan):
    import get_notion_executor as e
    body={'parent':{'type':'data_source_id','data_source_id':plan['policy']['data_source_id']},'properties':e.property_transaction.creation_wire_properties(row),'children':e.transport.wire_blocks(row['request_blocks'],None,uploaded_pngs=row.get('uploaded_pngs'))}
    return e.transport.fingerprint('POST','/pages',body)


def allows(plan,journal):
    import get_notion_executor as e
    try:
        b=plan_binding(plan)
        if (not b or not isinstance(journal,dict) or set(journal)!={'plan_sha256','plan','index','operation'}
                or journal['plan_sha256']!=b['plan_sha256'] or journal['plan']!=plan
                or plan.get('historical_only') is not True or plan.get('property_only')
                or plan['historical_manifest_sha256']!=b['manifest_sha256']
                or plan['publication']['commit']!=b['source_commit'] or len(plan['records'])!=b['record_count']
                or type(journal['index']) is not int or not b['initial_index']<=journal['index']<=b['record_count']
                or any(r['action']!='create' for r in plan['records'][b['initial_index']:])):return False
        index=journal['index'];op=journal['operation'];row=plan['records'][b['initial_index']]
        if row['note_id']!=b['note_id'] or row['source']!=b['source'] or request_sha(row,plan)!=b['request_sha256']:return False
        if 'receipt_successor' in b:
            # The response and receipt already exist. Neither replay the
            # acknowledged operation nor prepare any remaining CREATE.
            return index==b['initial_index']+1 and op is None
        if 'acknowledged_successor' in b:
            # This capability can confirm one existing page, never create the
            # remaining rows or fall back to its original in-flight state.
            if index==b['initial_index']:
                return (isinstance(op,dict) and set(op)=={'kind','phase','before','response','archive_index'}
                    and e.digest(op)==b['acknowledged_successor']['operation_sha256']
                    and op['kind']=='create' and op['phase']=='acknowledged'
                    and op['response']==b['page_id'] and type(op['archive_index']) is int and op['archive_index']==0)
            return index==b['initial_index']+1 and op is None
        if index==b['initial_index']:return op is not None and e.digest(op)==b['operation_sha256'] and op['kind']=='create' and op['phase']=='in_flight' and op['response'] is None
        if index==b['record_count']:return op is None
        if op is None:return True
        row=plan['records'][index]
        if (set(op) not in ({'kind','phase','before','response','archive_index'},{'kind','phase','before','response','archive_index','write_failure'})
                or op['kind']!='create' or type(op['archive_index']) is not int or op['archive_index']!=0
                or e.target_sha(op['before'],row['note_id'],plan['policy']['data_source_id'])!=row['before_sha256']):return False
        if op['phase'] in ('prepared','in_flight'):
            if op['response'] is not None:return False
        elif op['phase']=='acknowledged':e.p.notion_id(op['response'])
        else:return False
        if 'write_failure' in op:
            f=op['write_failure'];body={'parent':{'type':'data_source_id','data_source_id':plan['policy']['data_source_id']},'properties':e.property_transaction.creation_wire_properties(row),'children':e.transport.wire_blocks(row['request_blocks'],None,uploaded_pngs=row.get('uploaded_pngs'))}
            if op['phase']!='in_flight' or set(f)!={'http_status','request_fingerprint'} or type(f['http_status']) is not int or not 400<=f['http_status']<=599 or f['request_fingerprint']!=e.transport.fingerprint('POST','/pages',body):return False
        return True
    except Exception:return False


def initial_state_valid(state,sha):
    import get_notion_outbox as o
    try:
        o.check_state(state);j=state['engine_state']['journal'];b=plan_binding(j['plan']);raw=o.encoded(state)
        successor=(b.get('receipt_successor') or b.get('acknowledged_successor')) if b else None
        anchor=successor['anchor'] if successor else b['proof']['anchor'] if b else None
        size=successor['state_bytes'] if successor else b['proof']['checkpoint']['state_bytes'] if b else None
        receipt=bool(b and 'receipt_successor' in b)
        return bool(b and o.digest(raw)==sha==anchor['state_sha256']
            and len(raw)==size and o.e.p.git_blob(raw)==anchor['state_blob']
            and state['policy_sha256']==b['public_policy']['get_policy_sha256'] and j['index']==b['initial_index']+int(receipt)
            and allows(j['plan'],j) and (b['note_id'] in state['engine_state']['receipts']) is receipt
            and b['note_id'] not in state['engine_state'].get('unresolved_by_id',{}))
    except Exception:return False


def transition_manifest(manifest,expected,plan):
    import get_notion_outbox as o
    b=plan_binding(plan)
    if not b or expected!=b['manifest_sha256'] or o.digest(manifest)!=expected or manifest!=plan['historical_manifest']:o.fail('outbox_state_invalid')
    return manifest,expected


def check_mode(state,found,manifest,expected,other_modes):
    import get_notion_outbox as o
    j=state['engine_state']['journal'];b=plan_binding(j['plan']) if j else None
    if found.get('recovery_anchor')==TAG or b:
        if not b or any(x is not None for x in other_modes) or not allows(j['plan'],j):o.fail('outbox_state_invalid')
        transition_manifest(manifest,expected,j['plan'])



def authenticated_proof_matches(proof,binding):
    """Accept only original bounded discovery counters beside exact authority."""
    import get_publication_provenance as provenance
    if not isinstance(proof,dict):return False
    diagnostics={'candidate_runs_checked','candidate_attempts_checked','provenance_reads','provenance_bytes'}
    present=diagnostics & set(proof)
    if present:
        if present!=diagnostics or any(type(proof[k]) is not int for k in diagnostics):return False
        if (not 1<=proof['candidate_runs_checked']<=4
                or proof['candidate_attempts_checked']!=proof['candidate_runs_checked']
                or not 1<=proof['provenance_reads']<=40
                or not 1<=proof['provenance_bytes']<=provenance.MAX_TOTAL):return False
    return {k:v for k,v in proof.items() if k not in diagnostics}=={**binding['proof'],'recovery_anchor':TAG}

def transition_outer(state,sha,new_policy,*,proof):
    import get_notion_outbox as o
    import get_recurring_policy_recovery as r
    b=plan_binding(state['engine_state']['journal']['plan'])
    if not b or not authenticated_proof_matches(proof,b) or new_policy!=r.current_get_policy() or o.digest(state)!=sha:o.fail('outbox_state_invalid')
    if state['policy_sha256']==new_policy:
        original=deepcopy(state);original['policy_sha256']=b['public_policy']['get_policy_sha256']
        if not initial_state_valid(original,o.digest(original)):o.fail('outbox_state_invalid')
        return deepcopy(state)
    if not initial_state_valid(state,sha):o.fail('outbox_state_invalid')
    out=deepcopy(state);out['policy_sha256']=new_policy;o.check_state(out);return out


def preserve(before,after):
    """Exact-plan forward CAS, no old data deletion or unresolved-write retry."""
    import get_notion_outbox as o
    import get_notion_executor as e
    import get_recurring_policy_recovery as policy
    old=before['engine_state']['journal'];b=plan_binding(old['plan']) if old else None
    if not b:return
    o.check_state(before);o.check_state(after)
    if not allows(old['plan'],old):o.fail('outbox_state_invalid')
    if before==after:return
    if before['policy_sha256']!=after['policy_sha256']:
        wanted=deepcopy(before);wanted['policy_sha256']=policy.current_get_policy()
        if not initial_state_valid(before,o.digest(before)) or after!=wanted:o.fail('outbox_state_invalid')
        return
    if before['policy_sha256']!=policy.current_get_policy():o.fail('outbox_state_invalid')
    for k in set(before)|set(after):
        if k!='engine_state' and before.get(k)!=after.get(k):o.fail('outbox_state_invalid')
    a=before['engine_state'];z=after['engine_state'];new=z['journal']
    if new is None:
        wanted=deepcopy(before);wanted['engine_state']['journal']=None
        last=b['initial_index']+1 if receipt_only_binding(old['plan']) else b['record_count']
        if old['index']!=last or old['operation'] is not None or after!=wanted:o.fail('outbox_state_invalid')
        if receipt_only_binding(old['plan']):
            # The ordinary clean-yield path checks every completed prefix row.
            # Retain a CAS-level paired-receipt check for this exact fifth row.
            row=old['plan']['records'][b['initial_index']];ident=b['note_id'];body=a['receipts'].get(ident,{})
            expected={'note_id':ident,'page_id':b['page_id'],'data_source_id':old['plan']['policy']['data_source_id'],
                'source_commit':b['source_commit'],'md_blob':row['source']['blob'],
                'creation_plan_sha256':b['plan_sha256'],'readback_verified':True}
            if any(body.get(k)!=v for k,v in expected.items()) or ident in a['failures']:o.fail('outbox_state_invalid')
            paired=deepcopy(a);e.property_transaction.record_creation(paired,row,old['plan'])
            if a.get('property_receipts',{}).get(ident)!=paired['property_receipts'][ident]:o.fail('outbox_state_invalid')
        return
    if not allows(new['plan'],new) or not old['index']<=new['index']<=old['index']+1:o.fail('outbox_state_invalid')
    if new['index']==old['index']:
        wanted=deepcopy(before);wanted['engine_state']['journal']=new
        if after!=wanted:o.fail('outbox_state_invalid')
        x=old['operation'];y=new['operation']
        if x is None:
            if y is None:return
            if y['phase']!='prepared':o.fail('outbox_state_invalid')
            return
        if y is None:o.fail('outbox_state_invalid')
        if old['index']==b['initial_index']:
            if y!=x:o.fail('outbox_state_invalid')
            return
        allowed={('prepared','in_flight'),('in_flight','acknowledged'),('in_flight','prepared')}
        if x['phase']==y['phase']:
            if x==y:return
            if x['phase']!='in_flight' or 'write_failure' in x or set(y)!=set(x)|{'write_failure'} or any(y[k]!=v for k,v in x.items()):o.fail('outbox_state_invalid')
            return
        if (x['phase'],y['phase']) not in allowed or ('write_failure' in x and y['phase']=='prepared'):o.fail('outbox_state_invalid')
        fields={'phase','response'} if y['phase']=='acknowledged' else {'phase'}
        if set(x)!=set(y) or any(y[k]!=v for k,v in x.items() if k not in fields):o.fail('outbox_state_invalid')
        return
    row=old['plan']['records'][old['index']];ident=row['note_id'];op=old['operation']
    if (new['operation'] is not None or op is None or op['kind']!='create'
            or op['phase'] not in ('in_flight','acknowledged') or ident in a['receipts']):o.fail('outbox_state_invalid')
    body=z['receipts'].get(ident)
    if not isinstance(body,dict):o.fail('outbox_state_invalid')
    page=b['page_id'] if old['index']==b['initial_index'] else op['response']
    if page is None:o.fail('outbox_state_invalid')
    if (body.get('page_id')!=page or body.get('note_id')!=ident or body.get('data_source_id')!=old['plan']['policy']['data_source_id']
            or body.get('source_commit')!=b['source_commit'] or body.get('md_blob')!=row['source']['blob']
            or body.get('creation_plan_sha256')!=b['plan_sha256'] or body.get('protected_blocks')!=[]
            or body.get('asset_bindings')!=row.get('preserved_asset_bindings',{})):o.fail('outbox_state_invalid')
    # Reconstruct exactly the submitted block tree with the observed native IDs.
    # The receipt may not substitute arbitrary valid-looking block hashes.
    observed=body['managed_blocks'];position=[0]
    def walk(block,parent):
        if position[0]>=len(observed):o.fail('outbox_state_invalid')
        at=position[0];position[0]+=1;ident=observed[at]['id']
        children=[walk(child,ident) for child in block['children']]
        node={'id':ident,'parent_id':parent,'type':block['type'],
              'payload':e.p.normalize_payload(block['type'],block['payload']),'children_ids':children}
        if observed[at]['normalized_hash']!=e.p.block_hash(node):o.fail('outbox_state_invalid')
        return ident
    for block in row['request_blocks']:walk(block,page)
    if position[0]!=len(observed):o.fail('outbox_state_invalid')
    for name,prop in e.property_transaction.creation_readback_properties(row,page).items():
        try:
            if e.properties.value(body['properties_baseline'][name])!=e.properties.value(prop):o.fail('outbox_state_invalid')
        except Exception:o.fail('outbox_state_invalid')
    wanted=deepcopy(before);wanted['engine_state']['journal']=deepcopy(new)
    wanted['engine_state']['receipts'][ident]=deepcopy(body)
    wanted['engine_state']['failures'].pop(ident,None)
    e.property_transaction.record_creation(wanted['engine_state'],row,old['plan'])
    if after!=wanted:o.fail('outbox_state_invalid')


def verify_successor(local,remote,loader,head,proof):
    """Only exact policy-only CAS after its confirmation was lost; no progress guess."""
    from types import SimpleNamespace
    import get_notion_remote_journal as j
    import get_notion_outbox as o
    import get_recurring_policy_recovery as policy
    b=run_binding(proof.get('run_id'),proof.get('run_attempt'))
    if not b or not authenticated_proof_matches(proof,b):o.fail('outbox_state_invalid')
    if 'acknowledged_successor' in b or 'receipt_successor' in b:return _verify_acknowledged_successor(local,remote,loader,head,proof,b)
    if remote.ref(j.REF)!=head:o.fail('outbox_state_changed')
    git=j.retention.LocalGit(local.vault)
    def node(commit):
        loader(commit);tree,entries=j.RemoteJournal._local_tree(SimpleNamespace(local=git),commit)
        parents=git.run('rev-list','--parents','-n','1',commit).decode().split()[1:]
        if remote.commit(commit)!={'tree':tree,'parents':parents}:o.fail('outbox_state_invalid')
        return tree,parents,entries
    anchor=deepcopy(b['proof']['anchor']);prior=anchor['commit'];base=node(prior)
    if base[2].get(j.STATE_PATH)!=('100644','blob',anchor['state_blob']):o.fail('outbox_state_invalid')
    raw=git.blob(anchor['state_blob'])
    if len(raw)!=b['proof']['checkpoint']['state_bytes'] or o.digest(raw)!=anchor['state_sha256'] or j.retention.oid(raw)!=anchor['state_blob'] or remote.blob(anchor['state_blob'],len(raw))!=raw:o.fail('outbox_state_invalid')
    original=j.retention.strict_json(raw)
    desired=transition_outer(original,anchor['state_sha256'],policy.current_get_policy(),proof=proof)
    target=o.encoded(desired);oid=j.retention.oid(target)
    node(head);git.run('merge-base','--is-ancestor',prior,head)
    history=git.run('rev-list','--reverse','--max-count=257',prior+'..'+head).decode().split()
    if len(history)>256:o.fail('outbox_state_invalid')
    seen=False
    for commit in history:
        current=node(commit)
        if current[1]!=[prior]:o.fail('outbox_state_invalid')
        changed={k for k in set(base[2])|set(current[2]) if base[2].get(k)!=current[2].get(k)}
        if j.STATE_PATH in changed:
            if seen or changed!={j.STATE_PATH} or current[2][j.STATE_PATH]!=('100644','blob',oid) or git.blob(oid)!=target or remote.blob(oid,len(target))!=target:o.fail('outbox_state_invalid')
            seen=True;anchor={'commit':commit,'state_blob':oid,'state_sha256':o.digest(target)}
        # Original source_guard and RemoteJournal still authenticate every unrelated path.
        prior=commit;base=current
    return anchor


def initial_or_transitioned_state_valid(state,sha):
    import get_notion_outbox as o
    import get_recurring_policy_recovery as policy
    if initial_state_valid(state,sha):return True
    try:
        if o.digest(state)!=sha or state['policy_sha256']!=policy.current_get_policy():return False
        b=plan_binding(state['engine_state']['journal']['plan']);old=deepcopy(state);old['policy_sha256']=b['public_policy']['get_policy_sha256']
        return initial_state_valid(old,o.digest(old))
    except Exception:return False


def receipt_only_binding(plan):
    """The immutable ack capability grants no native write authority."""
    b=plan_binding(plan)
    return b if b and ('acknowledged_successor' in b or 'receipt_successor' in b) else None


def _verify_acknowledged_successor(local,remote,loader,head,proof,b):
    """Authenticate a checkpoint and its exact typed child separately.

    The original proof is never changed or promoted to a nonexistent checkpoint.
    Only the verified read anchor may advance, then by one policy-only CAS.
    """
    from types import SimpleNamespace
    import get_notion_remote_journal as j
    import get_notion_outbox as o
    import get_recurring_policy_recovery as policy
    if not authenticated_proof_matches(proof,b):o.fail('outbox_state_invalid')
    if remote.ref(j.REF)!=head:o.fail('outbox_state_changed')
    git=j.retention.LocalGit(local.vault)
    def node(commit):
        loader(commit);tree,entries=j.RemoteJournal._local_tree(SimpleNamespace(local=git),commit)
        parents=git.run('rev-list','--parents','-n','1',commit).decode().split()[1:]
        if remote.commit(commit)!={'tree':tree,'parents':parents}:o.fail('outbox_state_invalid')
        return tree,parents,entries
    def state(anchor,size):
        current=node(anchor['commit'])
        if current[2].get(j.STATE_PATH)!=('100644','blob',anchor['state_blob']):o.fail('outbox_state_invalid')
        raw=git.blob(anchor['state_blob'])
        if (len(raw)!=size or o.digest(raw)!=anchor['state_sha256'] or j.retention.oid(raw)!=anchor['state_blob']
                or remote.blob(anchor['state_blob'],size)!=raw):o.fail('outbox_state_invalid')
        value=j.retention.strict_json(raw);o.check_state(value)
        if o.encoded(value)!=raw:o.fail('outbox_state_invalid')
        return value,raw,current
    authenticated=b['proof']['anchor'];successor=b.get('receipt_successor') or b['acknowledged_successor']
    before,before_raw,old=state(authenticated,b['proof']['checkpoint']['state_bytes'])
    after,after_raw,base=state(successor['anchor'],successor['state_bytes'])
    if base[0]!=successor['tree'] or base[1]!=[authenticated['commit']]:o.fail('outbox_state_invalid')
    changed={k for k in set(old[2])|set(base[2]) if old[2].get(k)!=base[2].get(k)}
    if changed!={j.STATE_PATH}:o.fail('outbox_state_invalid')
    if 'receipt_successor' in b:
        verify_receipt_successor_state(before,after,b)
    else:
        pending=before['engine_state']['journal'];op=pending['operation']
        if (pending['index']!=b['initial_index'] or not isinstance(op,dict)
                or set(op)!={'kind','phase','before','response','archive_index'}
                or (op['kind'],op['phase'],op['response'],op['archive_index'])!=('create','in_flight',None,0)
                or o.digest(op)!=b['operation_sha256']):o.fail('outbox_state_invalid')
        restored=deepcopy(after)
        restored['engine_state']['journal']['operation']['phase']='in_flight'
        restored['engine_state']['journal']['operation']['response']=None
        if o.encoded(restored)!=before_raw or not initial_state_valid(after,o.digest(after)):o.fail('outbox_state_invalid')
    # Exact digest equality above binds every other state byte, including the
    # original plan, index, before snapshot, captures, receipts and holds.
    desired=transition_outer(after,o.digest(after),policy.current_get_policy(),proof=proof)
    target=o.encoded(desired);oid=j.retention.oid(target)
    anchor=deepcopy(successor['anchor']);prior=anchor['commit']
    node(head);git.run('merge-base','--is-ancestor',prior,head)
    history=git.run('rev-list','--reverse','--max-count=257',prior+'..'+head).decode().split()
    if len(history)>256:o.fail('outbox_state_invalid')
    seen=False
    for commit in history:
        current=node(commit)
        if current[1]!=[prior]:o.fail('outbox_state_invalid')
        changed={k for k in set(base[2])|set(current[2]) if base[2].get(k)!=current[2].get(k)}
        if j.STATE_PATH in changed:
            if (seen or changed!={j.STATE_PATH} or current[2][j.STATE_PATH]!=('100644','blob',oid)
                    or git.blob(oid)!=target or remote.blob(oid,len(target))!=target):o.fail('outbox_state_invalid')
            seen=True;anchor={'commit':commit,'state_blob':oid,'state_sha256':o.digest(target)}
        elif any(k=='Raw/03 Get' or k.startswith('Raw/03 Get/') for k in changed):o.fail('outbox_state_invalid')
        if current[2].get(j.STATE_PATH)!=('100644','blob',anchor['state_blob']):o.fail('outbox_state_invalid')
        prior=commit;base=current
    if remote.ref(j.REF)!=head:o.fail('outbox_state_changed')
    return anchor


def verify_receipt_successor_state(before,after,b):
    """Only the original acknowledged CREATE's exact receipt completion.

    This is Git/state validation, not native ownership evidence. The runtime
    must independently read and reconstruct the full native receipt before CAS.
    """
    import get_notion_outbox as o
    import get_notion_executor as e
    o.check_state(before);o.check_state(after)
    if 'receipt_successor' not in b or not initial_state_valid(after,o.digest(after)):o.fail('outbox_state_invalid')
    pending=before['engine_state']['journal'];op=pending['operation']
    if (pending['plan_sha256']!=b['plan_sha256'] or e.digest(pending['plan'])!=b['plan_sha256']
            or pending['index']!=b['initial_index'] or not isinstance(op,dict)
            or set(op)!={'kind','phase','before','response','archive_index'}
            or (op['kind'],op['phase'],op['response'])!=('create','acknowledged',b['page_id'])
            or type(op['archive_index']) is not int or op['archive_index']!=0
            or o.digest(op)!=b['operation_sha256']):o.fail('outbox_state_invalid')
    row=pending['plan']['records'][b['initial_index']];ident=b['note_id']
    if ident in before['engine_state']['receipts'] or ident in before['engine_state'].get('property_receipts',{}):o.fail('outbox_state_invalid')
    body=after['engine_state']['receipts'].get(ident,{})
    expected={'note_id':ident,'page_id':b['page_id'],'data_source_id':pending['plan']['policy']['data_source_id'],
        'source_commit':b['source_commit'],'md_blob':row['source']['blob'],'creation_plan_sha256':b['plan_sha256'],
        'readback_verified':True,'protected_blocks':[],'asset_bindings':row.get('preserved_asset_bindings',{})}
    if any(body.get(k)!=v for k,v in expected.items()):o.fail('outbox_state_invalid')
    wanted=deepcopy(before);wanted['engine_state']['journal'].update(index=b['initial_index']+1,operation=None)
    wanted['engine_state']['receipts'][ident]=deepcopy(body)
    wanted['engine_state']['failures'].pop(ident,None)
    e.property_transaction.record_creation(wanted['engine_state'],row,pending['plan'])
    if wanted!=after:o.fail('outbox_state_invalid')
