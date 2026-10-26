"""One immutable clean-active continuation; never generic old-plan authority.

Private registration binds the original failed proof, exact state/plan, paired
receipt prefix and ordered remainder. Every original runtime guard still runs.
"""
from copy import deepcopy
from pathlib import Path
import re
import os
import json
import get_private_runtime_bindings as bindings

TAG = 'exact_registered_clean_active_resume_v1'
KEYS = {'contract','proof','public_policy','public_policy_sha256','notion_policy_sha256',
        'plan_sha256','plan_bytes','engine_rules_sha256','manifest_sha256','property_manifest_sha256',
        'source_commit','initial_index','record_count','prefix','remaining','invariant_sha256','target_code_sha256'}


def target_material_sha256(document):
    """Additional target pin; NEVER substitutes for the original rules hash.

    Only the loader's one capsule self-reference is normalized to avoid a hash
    cycle. All original rules/policy/checkpoint hashes remain byte-exact.
    """
    import get_notion_executor as e
    rows={};total=0;paths=sorted(Path(__file__).resolve().parent.glob('*.py'))
    if not paths or len(paths)>500:e.fail('request_budget')
    for path in paths:
        raw=e.git.bounded_file(path,2*1024*1024);total+=len(raw)
        if total>16*1024*1024:e.fail('request_budget')
        if path.name=='get_private_runtime_bindings.py':
            raw,n=re.subn(rb"TRUSTED_BUNDLE_SHA256 = '[0-9a-f]{64}'",b"TRUSTED_BUNDLE_SHA256 = '<capsule-self-reference>'",raw)
            if n!=1:e.fail('approval_missing')
        rows[path.name]=e.digest(raw)
    # Include all typed capsule bytes, normalizing ONLY this target's own
    # digest. A later capsule-only release cannot inherit the bridge silently.
    document=deepcopy(document)
    try:
        registry=document['modules']['get_notion_clean_active_resume']['REGISTRY']
        if registry['type']!='list' or len(registry['value'])!=1:e.fail('approval_missing')
        entry=registry['value'][0]
        if entry['type']!='dict':e.fail('approval_missing')
        target=entry['value']['target_code_sha256']
        if set(target)!={'type','value'} or target['type']!='scalar' or not re.fullmatch('[0-9a-f]{64}',target['value']):e.fail('approval_missing')
        target['value']='<target-self-reference>'
    except Exception:e.fail('approval_missing')
    return e.digest({'scripts':rows,'capsule':e.digest(bindings.encoded(document))})


def _capsule_bytes():
    """Read exact independently pinned bytes; never caller-supplied authority."""
    import get_notion_executor as e
    path=os.environ.get(bindings.ENV_FILE)
    bindings.read_local(path,bindings.TRUSTED_BUNDLE_SHA256)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as handle:raw=handle.read(bindings.MAX_BYTES+1)
    if len(raw)>bindings.MAX_BYTES or e.digest(raw)!=bindings.TRUSTED_BUNDLE_SHA256:e.fail('approval_missing')
    return raw


def target_code_sha256():
    return target_material_sha256(json.loads(_capsule_bytes()))


def registry():
    import get_notion_outbox as o
    import get_notion_journal_runtime as c
    rows=bindings.value('get_notion_clean_active_resume','REGISTRY')
    if not isinstance(rows,list) or len(rows)!=1:o.fail('outbox_state_invalid')
    b=rows[0]
    if not isinstance(b,dict) or set(b)!=KEYS or b['contract']!='get-notion-exact-clean-active-1':o.fail('outbox_state_invalid')
    policy=c.provenance.validate_policy(b['public_policy'],b['public_policy_sha256'])
    for key in ('plan_sha256','engine_rules_sha256','manifest_sha256','property_manifest_sha256','notion_policy_sha256','invariant_sha256','target_code_sha256'):c.journal.checked(b[key],64)
    c.journal.checked(b['source_commit'],40)
    if (type(b['initial_index']) is not int or b['initial_index']!=6 or type(b['record_count']) is not int or b['record_count']!=10
            or type(b['plan_bytes']) is not int or not 0<b['plan_bytes']<=o.e.MAX_STATE):o.fail('outbox_state_invalid')
    p=b['proof'];run=p['run_binding'];latest=p['latest_binding'];cp=p['checkpoint']
    expected_report={'contract':'get-private-runtime-report-2','status':'executed','business_health_asserted':False,
        'complete_library_claimed':False,'execution_status':'blocked','execution_reason':'transport_read_failed',
        'write_state':'uncertain','external_write_attempts':6,'asset_downloads':0}
    if (p['status']!='journal_provenance_verified' or p['terminal_report']!=expected_report
            or p['terminal_conclusions']!={'job':'failure','notion':'failure','get':'skipped'}
            or p['get_step_record'] is not None or p['get_run_success_claim'] is not False
            or p['private_readback_required'] is not True or p.get('clock_witness') is not None
            or c.provenance.run_binding(run)!=run or c.provenance.run_binding(latest)!=latest
            or run['event']!='workflow_dispatch' or run['status']!='completed' or run['conclusion']!='failure'
            or latest['run_attempt']!=run['run_attempt'] or p['run_id']!=run['id'] or p['run_attempt']!=run['run_attempt']
            or type(p['checkpoint_count']) is not int or p['checkpoint_count']!=26):o.fail('outbox_state_invalid')
    c._same_run(run,latest,cross_endpoint=True)
    context={'run_id':run['id'],'run_attempt':run['run_attempt'],'code_sha':policy['code_sha'],
        'controller_sha':run['head_sha'],'rules_sha256':policy['rules_sha256'],
        'notion_policy_sha256':b['notion_policy_sha256'],'public_policy_sha256':b['public_policy_sha256']}
    c.context(context)
    if (set(cp)!=c.RECORD_KEYS or any(cp[k]!=context[k] for k in c.CONTEXT_KEYS)
            or cp['sequence']!=26 or cp['cas_attempts']!=0 or cp['parent_commit'] is not None
            or cp['readback_verified'] is not True or cp['status']!='journal_readback_verified'
            or o.digest({k:v for k,v in cp.items() if k!='record_sha256'})!=cp['record_sha256']
            or p['anchor']!={k:cp[k] for k in ('commit','state_blob','state_sha256')}):o.fail('outbox_state_invalid')
    fixed=c.fixed_auth.parse(o.encoded(p['fixed_source_record']),context,run)
    if fixed['mode']!='historical-apply' or fixed['selection_sha256']!=b['manifest_sha256']:o.fail('outbox_state_invalid')
    if not isinstance(b['prefix'],list) or len(b['prefix'])!=6 or not isinstance(b['remaining'],list) or len(b['remaining'])!=4:o.fail('outbox_state_invalid')
    ids=[]
    for row in b['prefix']:
        if set(row)!={'note_id','page_id','body_receipt_sha256','property_receipt_sha256'}:o.fail('outbox_state_invalid')
        o.e.p.note_id(row['note_id']);o.e.p.notion_id(row['page_id']);ids.append(row['note_id'])
        for key in ('body_receipt_sha256','property_receipt_sha256'):c.journal.checked(row[key],64)
    for row in b['remaining']:
        if set(row)!={'note_id','source','request_sha256'}:o.fail('outbox_state_invalid')
        o.e.p.note_id(row['note_id']);ids.append(row['note_id']);c.journal.checked(row['request_sha256'],64)
        if set(row['source'])!={'path','blob','sha256'}:o.fail('outbox_state_invalid')
        c.journal.checked(row['source']['blob'],40);c.journal.checked(row['source']['sha256'],64)
    if len(set(ids))!=10:o.fail('outbox_state_invalid')
    return deepcopy(rows)


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
    if b and target_code_sha256()==b['target_code_sha256'] and p.run_binding(run) in (b['proof']['run_binding'],b['proof']['latest_binding']) and idle._forward_policy(policy,b):return deepcopy(b['public_policy']),True
    return policy,False


def recover_checkpoint(client,run_id,run_attempt):
    import get_notion_journal_runtime as c
    b=run_binding(run_id,run_attempt)
    if not b or target_code_sha256()!=b['target_code_sha256']:c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(client,run_id=run_id,run_attempt=run_attempt,public_policy=b['public_policy'],
        approved_public_policy_sha256=b['public_policy_sha256'],notion_policy_sha256=b['notion_policy_sha256'],
        include_job_digest=True,include_terminal_report=True)
    if result!=b['proof']:c.fail('journal_provenance_record')
    return {**result,'recovery_anchor':TAG}


def allows(plan,journal):
    import get_notion_executor as e
    from get_notion_create_resume import request_sha
    try:
        b=plan_binding(plan)
        if (not b or target_code_sha256()!=b['target_code_sha256'] or len(e.encoded(plan))!=b['plan_bytes']
                or plan['rules_sha256']!=b['engine_rules_sha256'] or plan.get('historical_only') is not True or plan.get('property_only')
                or plan['historical_manifest_sha256']!=b['manifest_sha256'] or plan['property_manifest_sha256']!=b['property_manifest_sha256']
                or plan['publication']['commit']!=b['source_commit'] or len(plan['records'])!=10
                or set(journal)!={'plan_sha256','plan','index','operation'} or journal['plan']!=plan or journal['plan_sha256']!=b['plan_sha256']
                or type(journal['index']) is not int or not 6<=journal['index']<=10):return False
        for row,pin in zip(plan['records'],b['prefix']+b['remaining']):
            if row['note_id']!=pin['note_id'] or row['action']!='create':return False
            if 'source' in pin and (row['source']!=pin['source'] or request_sha(row,plan)!=pin['request_sha256']):return False
        index=journal['index'];op=journal['operation']
        if index==10:return op is None
        if op is None:return True
        row=plan['records'][index]
        if (set(op) not in ({'kind','phase','before','response','archive_index'},{'kind','phase','before','response','archive_index','write_failure'})
                or op['kind']!='create' or type(op['archive_index']) is not int or op['archive_index']!=0
                or e.target_sha(op['before'],row['note_id'],plan['policy']['data_source_id'])!=row['before_sha256']
                or e.target(op['before'],row['note_id'],plan['policy']['data_source_id']) is not None):return False
        if op['phase'] in ('prepared','in_flight'):
            if op['response'] is not None:return False
        elif op['phase']=='acknowledged':e.p.notion_id(op['response'])
        else:return False
        if 'write_failure' in op:
            f=op['write_failure']
            if op['phase']!='in_flight' or set(f)!={'http_status','request_fingerprint'} or type(f['http_status']) is not int or not 400<=f['http_status']<=599 or f['request_fingerprint']!=request_sha(row,plan):return False
        return True
    except Exception:return False


def invariant(state,b):
    value=deepcopy(state);value.pop('policy_sha256',None);j=value['engine_state']['journal']
    if j is not None:j['index']=b['initial_index'];j['operation']=None
    for row in b['remaining']:
        for field in ('receipts','property_receipts'):value['engine_state'].get(field,{}).pop(row['note_id'],None)
    return value


def state_valid(state):
    import get_notion_outbox as o
    try:
        o.check_state(state);j=state['engine_state']['journal'];b=plan_binding(j['plan'])
        if not b or not allows(j['plan'],j) or o.digest(invariant(state,b))!=b['invariant_sha256']:return False
        a=state['engine_state']
        for pin in b['prefix']:
            ident=pin['note_id'];body=a['receipts'].get(ident)
            if (not body or body.get('page_id')!=pin['page_id'] or o.digest(body)!=pin['body_receipt_sha256']
                    or o.digest(a.get('property_receipts',{}).get(ident))!=pin['property_receipt_sha256']):return False
        for i,row in enumerate(j['plan']['records']):
            ident=row['note_id']
            if ident in a['failures'] or ident in a.get('unresolved_by_id',{}):return False
            if i<j['index']:
                body=a['receipts'].get(ident)
                if not body or body.get('readback_verified') is not True:return False
                paired=deepcopy(a);o.e.property_transaction.record_creation(paired,row,j['plan'])
                if a.get('property_receipts',{}).get(ident)!=paired['property_receipts'][ident]:return False
            elif ident in a['receipts'] or ident in a.get('property_receipts',{}):return False
        return True
    except Exception:return False


def initial_state_valid(state,sha):
    import get_notion_outbox as o
    try:
        b=plan_binding(state['engine_state']['journal']['plan']);raw=o.encoded(state)
        return bool(b and state_valid(state) and o.digest(raw)==sha==b['proof']['anchor']['state_sha256']
            and len(raw)==b['proof']['checkpoint']['state_bytes'] and o.e.p.git_blob(raw)==b['proof']['anchor']['state_blob']
            and state['policy_sha256']==b['public_policy']['get_policy_sha256']
            and state['engine_state']['journal']['index']==6 and state['engine_state']['journal']['operation'] is None)
    except Exception:return False


def initial_or_transitioned_state_valid(state,sha):
    import get_notion_outbox as o
    import get_recurring_policy_recovery as policy
    if initial_state_valid(state,sha):return True
    try:
        if o.digest(state)!=sha or state['policy_sha256']!=policy.current_get_policy():return False
        b=plan_binding(state['engine_state']['journal']['plan']);old=deepcopy(state);old['policy_sha256']=b['public_policy']['get_policy_sha256']
        return initial_state_valid(old,o.digest(old))
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
        if not b or any(x is not None for x in other_modes) or not state_valid(state):o.fail('outbox_state_invalid')
        transition_manifest(manifest,expected,j['plan'])


class _NativeWitness:
    def __init__(self,state_sha,plan_sha,seal):
        if seal is not _NATIVE_SEAL:raise ValueError('outbox_state_invalid')
        self.state_sha=state_sha;self.plan_sha=plan_sha;self.seal=seal
_NATIVE_SEAL=object()


def verify_native(current,api):
    """Fresh native pagination/readback only; saved receipts never make snapshots."""
    import get_notion_executor as e
    if not state_valid(current):e.fail('state_changed')
    j=current['engine_state']['journal'];plan=j['plan'];a=current['engine_state'];ds=plan['policy']['data_source_id']
    for i,row in enumerate(plan['records']):
        ident=row['note_id'];snapshot=e.read_target(api,ident,ds)
        if i<j['index']:
            prior=a['receipts'][ident];managed=e.created(snapshot,row,ds,prior['page_id'])
            fresh=e.receipt(snapshot,ident,ds,managed,plan['publication'],row['source'],row.get('preserved_asset_bindings'))
            fresh['creation_plan_sha256']=e.digest(plan)
            if fresh!=prior:e.fail('human_edit_conflict')
        elif i==j['index'] and j['operation'] is not None and j['operation']['phase']=='acknowledged':
            e.created(snapshot,row,ds,j['operation']['response'])
        else:
            if i==j['index'] and j['operation'] is not None and j['operation']['phase']=='in_flight':e.fail('uncertain_create')
            if e.target(snapshot,ident,ds) is not None or e.target_sha(snapshot,ident,ds)!=row['before_sha256']:e.fail('target_changed')
    return _NativeWitness(e.digest(current),e.digest(plan),_NATIVE_SEAL)


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

def _transition_bytes(state,sha,new_policy,*,proof):
    import get_notion_outbox as o
    import get_recurring_policy_recovery as r
    b=plan_binding(state['engine_state']['journal']['plan'])
    if not b or not authenticated_proof_matches(proof,b) or new_policy!=r.current_get_policy() or not initial_or_transitioned_state_valid(state,sha):o.fail('outbox_state_invalid')
    out=deepcopy(state);out['policy_sha256']=new_policy;o.check_state(out);return out


def transition_outer(state,sha,new_policy,*,proof,native):
    import get_notion_outbox as o
    if (not isinstance(native,_NativeWitness) or native.seal is not _NATIVE_SEAL or native.state_sha!=sha
            or native.plan_sha!=state['engine_state']['journal']['plan_sha256']):o.fail('outbox_state_invalid')
    return _transition_bytes(state,sha,new_policy,proof=proof)


def preserve(before,after):
    """Exact-plan forward CAS, no old data deletion or unresolved-write retry."""
    import get_notion_outbox as o
    import get_notion_executor as e
    import get_recurring_policy_recovery as policy
    old=before['engine_state']['journal'];b=plan_binding(old['plan']) if old else None
    if not b:return
    o.check_state(before);o.check_state(after)
    if not state_valid(before):o.fail('outbox_state_invalid')
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
        if old['index']!=10 or old['operation'] is not None or after!=wanted:o.fail('outbox_state_invalid')
        return
    if not state_valid(after) or not old['index']<=new['index']<=old['index']+1:o.fail('outbox_state_invalid')
    if new['index']==old['index']:
        wanted=deepcopy(before);wanted['engine_state']['journal']=new
        if after!=wanted:o.fail('outbox_state_invalid')
        x=old['operation'];y=new['operation']
        if x is None:
            if y is None:return
            if y['phase']!='prepared':o.fail('outbox_state_invalid')
            return
        if y is None:o.fail('outbox_state_invalid')
        allowed={('prepared','in_flight'),('in_flight','acknowledged')}
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
            or op['phase']!='acknowledged' or ident in a['receipts']):o.fail('outbox_state_invalid')
    body=z['receipts'].get(ident)
    if not isinstance(body,dict):o.fail('outbox_state_invalid')
    page=op['response']
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
    desired=_transition_bytes(original,anchor['state_sha256'],policy.current_get_policy(),proof=proof)
    target=o.encoded(desired);oid=j.retention.oid(target)
    node(head);git.run('merge-base','--is-ancestor',prior,head)
    history=git.run('rev-list','--reverse','--max-count=257',prior+'..'+head).decode().split()
    if len(history)>256:o.fail('outbox_state_invalid')
    seen=False;capsule_seen=False
    capsule_raw=_capsule_bytes();capsule_oid=j.retention.oid(capsule_raw)
    capsule_path='.sync/get-notion/runtime-bindings-'+bindings.TRUSTED_BUNDLE_SHA256+'.json'
    for commit in history:
        current=node(commit)
        if current[1]!=[prior]:o.fail('outbox_state_invalid')
        changed={k for k in set(base[2])|set(current[2]) if base[2].get(k)!=current[2].get(k)}
        if j.STATE_PATH in changed:
            if seen or changed!={j.STATE_PATH} or current[2][j.STATE_PATH]!=('100644','blob',oid) or git.blob(oid)!=target or remote.blob(oid,len(target))!=target:o.fail('outbox_state_invalid')
            seen=True;anchor={'commit':commit,'state_blob':oid,'state_sha256':o.digest(target)}
        else:
            if (capsule_seen or changed!={capsule_path} or capsule_path in base[2]
                    or current[2].get(capsule_path)!=('100644','blob',capsule_oid)
                    or git.blob(capsule_oid)!=capsule_raw or remote.blob(capsule_oid,len(capsule_raw))!=capsule_raw):o.fail('outbox_state_invalid')
            capsule_seen=True
        prior=commit;base=current
    if remote.ref(j.REF)!=head:o.fail('outbox_state_changed')
    return anchor


