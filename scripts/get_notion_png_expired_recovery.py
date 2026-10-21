"""One exact run16 expired-upload replacement. Pure contract; no I/O.

The original complete journal remains at the immutable state commit/blob below.
Only a fresh expired response AND complete exact-ID missing read permit generation
one. An elapsed timestamp, null expiry, 404 or historical diagnosis is insufficient.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
import get_notion_png_upload as u

CONTRACT='get-run16-second-png-expired-recovery-1'
STATE_SHA = _private_bindings.value('get_notion_png_expired_recovery', 'STATE_SHA')
STATE_BYTES = _private_bindings.value('get_notion_png_expired_recovery', 'STATE_BYTES')
STATE_COMMIT = _private_bindings.value('get_notion_png_expired_recovery', 'STATE_COMMIT')
STATE_BLOB = _private_bindings.value('get_notion_png_expired_recovery', 'STATE_BLOB')
PLAN_SHA = _private_bindings.value('get_notion_png_expired_recovery', 'PLAN_SHA')
OP_SHA = _private_bindings.value('get_notion_png_expired_recovery', 'OP_SHA')
JOURNAL_SHA = _private_bindings.value('get_notion_png_expired_recovery', 'JOURNAL_SHA')
SLOT_SHA = _private_bindings.value('get_notion_png_expired_recovery', 'SLOT_SHA')
NOTE_SHA = _private_bindings.value('get_notion_png_expired_recovery', 'NOTE_SHA')
MISSING = _private_bindings.value('get_notion_png_expired_recovery', 'MISSING')
class Blocked(ValueError):pass
def fail(code):raise Blocked(code) from None
def same(a,b):return u.encoded(a)==u.encoded(b)
def keys(v,expected):
    if not isinstance(v,dict) or set(v)!=set(expected):fail('png_expired_evidence_invalid')
def old_anchor():return {'commit':STATE_COMMIT,'blob':STATE_BLOB,'sha256':STATE_SHA,'bytes':STATE_BYTES,
    'journal_sha256':JOURNAL_SHA,'plan_sha256':PLAN_SHA,'operation_sha256':OP_SHA}

def proof(slot,evidence):
    keys(evidence,{'upload_response','query_note_id','data_source_id','snapshot'})
    b=slot['plan']['binding'];v=evidence['upload_response']
    if evidence['query_note_id']!=b['note_id'] or evidence['data_source_id']!=b['data_source_id'] or not same(evidence['snapshot'],MISSING):fail('png_expired_missing_unverified')
    if not isinstance(v,dict) or v.get('object')!='file_upload' or u.uuid(v.get('id'))!=slot['state']['upload_id'] or v.get('status')!='expired':fail('png_expired_status_unverified')
    for key in ('archived','in_trash'):
        if type(v.get(key)) is not bool:fail('png_expired_evidence_invalid')
    if v.get('filename')!=slot['plan']['filename'] or v.get('content_type')!='image/png' or type(v.get('content_length')) is not int or v['content_length']!=b['bytes'] or 'expiry_time' not in v:fail('png_expired_evidence_invalid')
    if v['expiry_time'] is not None:u.expiry_shape(v['expiry_time'])
    # Deliberately never retain upload_url or any response metadata.
    return {'upload_response':{k:deepcopy(v[k]) for k in ('object','id','status','filename','content_type','content_length','expiry_time','archived','in_trash')},
        'query_note_id':b['note_id'],'data_source_id':b['data_source_id'],'snapshot':deepcopy(MISSING)}

ROW_KEYS=('note_id','action','source','properties','rendered','request_blocks','warnings','before_sha256','preserved_asset_bindings','source_assets')
ROW_SEMANTICS_SHA = _private_bindings.value('get_notion_png_expired_recovery', 'ROW_SEMANTICS_SHA')
def normalized_blocks(items,upload_id):
    result=deepcopy(items)
    for node in result:
        if node.get('type')=='image':
            payload=node.get('payload',{})
            if payload.get('kind')!='notion_uploaded_png' or payload.get('upload_id')!=upload_id:fail('png_expired_plan_scope')
            payload['upload_id']='replacement-generation-id'
        node['children']=normalized_blocks(node.get('children',[]),upload_id)
    return result
def row_semantics(row,upload_id):
    value={k:deepcopy(row[k]) for k in ROW_KEYS}
    for k in ('rendered','request_blocks'):value[k]=normalized_blocks(value[k],upload_id)
    return u.digest(value)
PREFLIGHT_SHA = _private_bindings.value('get_notion_png_expired_recovery', 'PREFLIGHT_SHA')
def preflight_sha(rendered,source,properties,upload_id):
    return u.digest({'rendered':normalized_blocks(rendered,upload_id),'source':source,'properties':properties})
def check_preflight(state,ident,rendered,source,properties):
    rec=state.get('png_assets',{}).get('expired_recovery')
    if rec is None:return True
    if ident!=rec['note_id']:fail('png_expired_plan_scope')
    ids=[]
    def images(nodes):
        for node in nodes:
            if node.get('type')=='image':ids.append(node.get('payload',{}).get('upload_id'))
            images(node.get('children',[]))
    images(rendered)
    allowed={rec['old_slot']['state']['upload_id'],state['png_assets']['uploads'][ident]['state']['upload_id']}-{None}
    if len(ids)!=1 or ids[0] not in allowed or preflight_sha(rendered,source,properties,ids[0])!=PREFLIGHT_SHA:fail('png_expired_plan_scope')
    return True
def check_new_row(row,assets):
    rec=assets['expired_recovery'];active=assets['uploads'][rec['note_id']]
    if active['state']['phase']!='uploaded_verified' or row_semantics(row,active['state']['upload_id'])!=ROW_SEMANTICS_SHA:fail('png_expired_plan_scope')
    return True

def only_note_id(state):
    assets=state.get('png_assets',{});rec=assets.get('expired_recovery')
    if rec is None:return None
    check_record(assets,state['engine_state'])
    return rec['note_id'] if rec['note_id'] not in assets['attachments'] else None

def check_record(assets,engine):
    if 'expired_recovery' not in assets:return
    rec=assets['expired_recovery']
    keys(rec,{'contract','generation','old_state','old_slot','note_id','source_binding','evidence','policy_sha256'})
    if rec['contract']!=CONTRACT or type(rec['generation']) is not int or rec['generation']!=1 or not same(rec['old_state'],old_anchor()):fail('png_expired_record_invalid')
    old=rec['old_slot'];ident=rec['note_id'];u.valid_hash(rec['policy_sha256'])
    if u.digest(old)!=SLOT_SHA or u.digest(ident)!=NOTE_SHA or not same(rec['source_binding'],old['plan']['binding']) or old['plan']['binding']['note_id']!=ident:fail('png_expired_record_invalid')
    if not same(proof(old,rec['evidence']),rec['evidence']):fail('png_expired_record_invalid')
    active=assets['uploads'].get(ident)
    if active is None or not same(active['plan'],old['plan']) or active['plan_sha256']!=old['plan_sha256']:fail('png_expired_record_invalid')
    if active['state']['upload_id']==old['state']['upload_id']:fail('png_expired_old_upload_reused')
    journal=engine['journal']
    if journal is not None and u.digest(journal['plan'])==PLAN_SHA:fail('png_expired_plan_scope')
    if ident not in assets['attachments'] and journal is not None:
        rows=journal['plan'].get('records')
        if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('note_id')!=ident or rows[0].get('action')!='create' or u.digest(journal['plan'])==PLAN_SHA:fail('png_expired_plan_scope')
        check_new_row(rows[0],assets)

def transition(before,*,new_policy_sha256,evidence):
    import get_notion_outbox as o
    o.check_state(before);u.valid_hash(new_policy_sha256)
    if len(o.encoded(before))!=STATE_BYTES or o.digest(before)!=STATE_SHA:fail('png_expired_prestate_changed')
    if before['policy_sha256']==new_policy_sha256:fail('png_expired_policy_unchanged')
    j=before['engine_state']['journal'];ident=j['plan']['records'][1]['note_id'];assets=before['png_assets'];slot=assets['uploads'][ident]
    if j['index']!=1 or u.digest(j)!=JOURNAL_SHA or u.digest(j['plan'])!=PLAN_SHA or u.digest(j['operation'])!=OP_SHA or j['operation']['phase']!='prepared' or j['operation']['response'] is not None or u.digest(slot)!=SLOT_SHA:fail('png_expired_prestate_changed')
    if ident in assets['attachments'] or ident in before['engine_state']['receipts'] or 'expired_recovery' in assets:fail('png_expired_prestate_changed')
    result=deepcopy(before);target=result['png_assets']
    target['expired_recovery']={'contract':CONTRACT,'generation':1,'old_state':old_anchor(),
        'old_slot':deepcopy(slot),'note_id':ident,'source_binding':deepcopy(slot['plan']['binding']),
        'evidence':proof(slot,evidence),'policy_sha256':new_policy_sha256}
    target['uploads'][ident]['state']=u.initial_state(slot['plan'])
    result['engine_state']['journal']=None
    result['policy_sha256']=new_policy_sha256
    o.check_state(result)
    return result

def validate_transition(before,after,*,new_policy_sha256,evidence):
    if not same(after,transition(before,new_policy_sha256=new_policy_sha256,evidence=evidence)):fail('png_expired_transition_changed')
    return True

def preserve(before,after):
    a=before.get('png_assets',{}).get('expired_recovery');b=after.get('png_assets',{}).get('expired_recovery')
    if not same(a,b):fail('png_expired_record_changed')
