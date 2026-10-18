"""One immutable run15 second-upload status observation; no write capability.

An injected runtime performs exactly one GET of the ID selected from the pinned
private journal. No credential loading, network startup, CAS, retry or CLI.
This observation never authorizes replacement or establishes page nonexistence.
"""
from datetime import datetime, timezone
import hashlib
import get_legacy_manual as manual
import get_notion_outbox as outbox
import get_notion_png_upload as upload

CONTRACT='get-saved-second-upload-status-1'
STATE_SHA='25a4ed324a22068d36234bafffcb7c1736a224592a1b0265be9352f1dd998ee0'
STATE_BYTES=673850
PLAN_SHA='a54e2736d552ce4fc4f14810bb052745dfc931468b7d06b6bc5608678bdf18b5'
OP_SHA='03904ded8243ecfd65115107fcd22414a976e242a901a8b5566669c6f9a4680c'
SLOT_SHA='b7ed0948cee5182596c6b55208ebc8128ae274739aefedb94789d17cc7fe209d'
ID_SHA='a3dbe65af9e77917fba6769ae530a6521c0fe13a7475d96aadcc5cfd2c4f9dcf'
STATUSES=frozenset(('pending','uploaded','expired','failed'))

class Blocked(ValueError): pass

def fail(reason):raise Blocked(reason) from None

def saved_slot(raw):
    if type(raw) is not bytes or len(raw)!=STATE_BYTES or hashlib.sha256(raw).hexdigest()!=STATE_SHA:fail('state_changed')
    value=manual.strict_capture_json(raw,STATE_BYTES);outbox.check_state(value)
    if outbox.encoded(value)!=raw:fail('state_changed')
    journal=value['engine_state']['journal']
    if journal['index']!=0 or outbox.digest(journal['plan'])!=PLAN_SHA or outbox.digest(journal['operation'])!=OP_SHA:fail('operation_changed')
    records=journal['plan']['records']
    if len(records)!=2 or records[1]['action']!='create':fail('operation_changed')
    slot=value['png_assets']['uploads'][records[1]['note_id']]
    if outbox.digest(slot)!=SLOT_SHA or slot['state']['phase']!='uploaded_verified':fail('upload_binding_changed')
    ident=upload.uuid(slot['state']['upload_id'])
    if outbox.digest(ident)!=ID_SHA:fail('upload_binding_changed')
    return slot

def inspect_saved_upload(raw_state, *, request, now=None):
    """request(spec) -> (HTTP status, parsed JSON), supplied by trusted runtime.

    The runtime must retain the existing fixed-host/version/no-redirect,
    permission/budget protections. It may use the reviewed PNG transport with
    allow_writes=False. This function only emits the exact GET spec below.
    """
    report={'contract':CONTRACT,'status':'blocked','reason':'state_invalid',
        'requests':0,'external_write_attempts':0,'journal_writes':0,
        'replacement_authorized':False,'state_sha256':STATE_SHA}
    try:
        slot=saved_slot(raw_state)
        clock=now or (lambda:datetime.now(timezone.utc))
        moment=clock()
        if not isinstance(moment,datetime) or moment.tzinfo is None or moment.utcoffset() is None:fail('clock_invalid')
        if not callable(request):fail('request_invalid')
        ident=slot['state']['upload_id'];plan=slot['plan'];binding=plan['binding']
        report['requests']=1
        status,value=request(upload.spec('GET','/file_uploads/'+ident))
        if type(status) is not int:fail('response_invalid')
        if status in (401,403):fail('permission_denied')
        if status==429:fail('request_budget')
        if status!=200:fail('read_failed')
        if not isinstance(value,dict) or value.get('object')!='file_upload' or upload.uuid(value.get('id'))!=ident:fail('upload_identity_changed')
        if value.get('status') not in STATUSES:fail('response_invalid')
        for key in ('archived','in_trash'):
            if key in value and type(value[key]) is not bool:fail('response_invalid')
        if value.get('filename')!=plan['filename'] or value.get('content_type')!=binding['mime'] or type(value.get('content_length')) is not int or value['content_length']!=binding['bytes']:fail('upload_metadata_changed')
        if 'expiry_time' not in value:fail('response_invalid')
        moment=clock()
        if not isinstance(moment,datetime) or moment.tzinfo is None or moment.utcoffset() is None:fail('clock_invalid')
        expiry=value['expiry_time'];elapsed=None
        if expiry is not None:
            upload.expiry_shape(expiry)
            elapsed=datetime.fromisoformat(expiry.replace('Z','+00:00'))<=moment
        proof={'upload_id':ident,'status':value['status'],'expiry_time':expiry,
            'filename':value['filename'],'content_type':value['content_type'],
            'content_length':value['content_length'],
            'archived':value.get('archived'),'in_trash':value.get('in_trash')}
        report.update(status='inspected',reason='status_observed',upload_status=value['status'],
            expiry_null=expiry is None,deadline_elapsed=elapsed,
            observed_expired=value['status']=='expired',
            archived=value.get('archived'),in_trash=value.get('in_trash'),
            observation_sha256=outbox.digest(proof),upload_slot_sha256=SLOT_SHA)
    except Exception as exc:
        allowed={'state_changed','operation_changed','upload_binding_changed','clock_invalid','request_invalid',
            'permission_denied','request_budget','read_failed','response_invalid','upload_identity_changed',
            'upload_metadata_changed','invalid_expiry'}
        report['reason']=str(exc) if str(exc) in allowed else 'inspection_failed'
    return report
