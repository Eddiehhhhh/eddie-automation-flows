"""One exact interrupted initial transaction, never a general old-rules bypass.

These hashes come from the reviewed successful public run and private readback.
New execution keeps its actual new code/policy identity. Only an already durable
copy of this unchanged plan may resume; a fresh plan never borrows old rules.
"""
from copy import deepcopy
import get_notion_sink_plan as p

OLD_RUN = 39026550645
OLD_ATTEMPT = 1
OLD_JOB = 117378551709
OLD_CONTROLLER = 'b257083bcc811f17804be95bbf6b206488ffd4cf'
OLD_PUBLIC_SHA = '3f44a9e855935f1d5b350b5aab10fe7e6456abad4c4b16e899b94a96ccea0c2f'
OLD_NOTION_POLICY = '39ff5365a547ce3db73bd07830d127a588370c672ecd9bf89d72756c09cd732a'
OLD_PLAN = 'd5e8809c7dc4c74bd3e9414f42564b11763d85b44b16750fa5718e5ed615ba75'
OLD_ENGINE_RULES = '78d389c249c6f7a1a4c050385a153c933a4cef41f985689f0fadaea3050e5146'
OLD_CHECKPOINT = '5032171fcafaa6eeb9148390023733079be196c976a7617c0dc8ae5e2fcf0ad6'
OLD_CHECKPOINT_COUNT = 1
OLD_ANCHOR = {'commit': 'c1339b2575a4097b5aee8377e7bbb45487eabd0f', 'state_blob': '201ca079f1f048091baeba68e8a50e6daab970a7', 'state_sha256': 'ce90edfcda00d1d11a76aa26deec5afea875a19b429a2181e50bc14b11589192'}
OLD_GET = {'receipt_commit': 'a37cc108096210d0fb9847609e906ca22c88acf4', 'receipt_sha256': 'e49b99c42729ca079cdc04ed75145f39b12aad2d1a587dfcd0cafce9344f7293', 'receipt_sequence': 1, 'record_sha256': 'c8f9767395429c2ec094c28ed0ebd7c901b2f93ffdf75ba3319884e77a3e65b6'}
OLD_PUBLIC_POLICY = {'code_sha': '5a12166c39289b6ce4aabe1fdd73640cc34981ad', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': '6e4df4f707c0e74a1205f1fa6219c51a750866704228d67897bde0263b2130df', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': 'b950cc2dca2ea0742e039bea86afc0e1ff1c75068b85db97438f0a2a79a69662', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
HOLD_SHA256 = '71f4f65f776ef439da053d1316d3aa6c834bb6861114f3e249e3671c025d22d6'
HELD_NOTE_SHA256 = '8754337352a6e2c3958dd5858fd8f8a1e7c87f6a5352439ad37330aecbdfc333'
HELD_PAGE_SHA256 = '92bbf878381321f99f36c6a89828c579f26d196df36292c7364c82f57294320c'
OPERATION_SHA256 = 'd8c863f3a7c5a8d1c8f93274fcc688efaff127b8d2c3aa523a9a680b7a5e9464'
REQUEST_FINGERPRINT = '519337b91cd66299b11c907ea3aad785c3a5c050a121b8d0ac09e7b97035bc8b'


def allows(plan, journal):
    """Caller must first authenticate/read back the persistent state externally."""
    try:
        return (isinstance(plan,dict) and isinstance(journal,dict)
            and plan.get('rules_sha256') == OLD_ENGINE_RULES
            and p.digest(plan) == OLD_PLAN and journal.get('plan_sha256') == OLD_PLAN
            and p.encoded(journal.get('plan')) == p.encoded(plan))
    except Exception:
        return False


def recover_checkpoint(client, run_id, run_attempt):
    """Called only after exact new-controller mismatch, never on denied reads."""
    import get_notion_journal_runtime as checkpoints
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(OLD_RUN,OLD_ATTEMPT):
        checkpoints.fail('journal_provenance_other_controller')
    result=checkpoints.recover_anchor(client,run_id=OLD_RUN,run_attempt=OLD_ATTEMPT,
        public_policy=deepcopy(OLD_PUBLIC_POLICY),approved_public_policy_sha256=OLD_PUBLIC_SHA,
        notion_policy_sha256=OLD_NOTION_POLICY)
    record=result.get('checkpoint',{});get=result.get('get_step_record',{})
    if (result.get('anchor')!=OLD_ANCHOR or result.get('job_id')!=OLD_JOB
            or result.get('checkpoint_count')!=OLD_CHECKPOINT_COUNT or record.get('record_sha256')!=OLD_CHECKPOINT
            or record.get('controller_sha')!=OLD_CONTROLLER
            or any(get.get(k)!=v for k,v in OLD_GET.items())
            or result.get('private_readback_required') is not True):
        checkpoints.fail('journal_provenance_record')
    return {**result,'recovery_anchor':'exact_uncertain_transaction_v1'}


def validate_initial_state(state, sha):
    """The first old checkpoint has one exact envelope, not a self-hashed claim."""
    try:
        journal=state['engine_state']['journal']
        return (sha==OLD_ANCHOR['state_sha256'] and p.digest(state)==sha
            and allows(journal['plan'],journal)
            and all(state[k] is None for k in ('origin','policy_sha256','last_enqueued_publication','observed_publication_tip'))
            and state['pending_by_id']=={} and state['rotation_order']==[])
    except Exception:
        return False


def validate_holds(holds):
    """Only one externally pinned hold; reference retains all old evidence.

    The complete original journal remains in the immutable anchor blob. No
    later source/rule change or retry_conflicts flag can release this hold.
    """
    if not isinstance(holds,dict) or len(holds)>1:raise ValueError('state_invalid')
    for ident,row in holds.items():
        if (p.digest(ident)!=HELD_NOTE_SHA256 or not isinstance(row,dict)
                or row.get('note_id')!=ident or p.digest(row)!=HOLD_SHA256):
            raise ValueError('state_invalid')
    return holds


def held(state, ident, page=None):
    holds=validate_holds(state.get('unresolved_by_id',{}))
    return ident in holds or any(page==row['page_id'] for row in holds.values())


def preserve_holds(before, after):
    old=validate_holds(before.get('unresolved_by_id',{}))
    new=validate_holds(after.get('unresolved_by_id',{}))
    if any(ident not in new or p.encoded(new[ident])!=p.encoded(row) for ident,row in old.items()):
        raise ValueError('state_invalid')


def isolate_outer(state, expected_sha):
    """Pure exact transition, no network and no interpretation of absence.

    The old operation is still unresolved. Only the active execution slot is
    released; the separately pinned note/page remains write-denied forever
    until a separately reviewed recovery contract is introduced.
    """
    if not validate_initial_state(state,expected_sha):raise ValueError('state_invalid')
    engine=state['engine_state'];journal=engine['journal']
    if (journal['index']!=1 or len(journal['plan']['records'])!=2
            or engine.get('unresolved_by_id') or len(engine['receipts'])!=1):
        raise ValueError('state_invalid')
    row=journal['plan']['records'][1];operation=journal['operation']
    if (p.digest(row['note_id'])!=HELD_NOTE_SHA256 or p.digest(row['page_id'])!=HELD_PAGE_SHA256
            or operation.get('kind')!='append' or operation.get('phase')!='in_flight'
            or operation.get('response') is not None or operation.get('archive_index')!=0
            or p.digest(operation)!=OPERATION_SHA256 or row['note_id'] in engine['receipts']):
        raise ValueError('state_invalid')
    hold={'contract':'get-notion-exact-uncertain-hold-1','note_id':row['note_id'],'page_id':row['page_id'],
        'anchor':deepcopy(OLD_ANCHOR),'run_id':OLD_RUN,'run_attempt':OLD_ATTEMPT,
        'checkpoint_sha256':OLD_CHECKPOINT,'plan_sha256':OLD_PLAN,'record_index':1,
        'operation_sha256':OPERATION_SHA256,'request_fingerprint':REQUEST_FINGERPRINT,
        'reason':'uncertain_append','effects':'unresolved','retry_allowed':False}
    validate_holds({row['note_id']:hold})
    result=deepcopy(state);result['engine_state']['journal']=None
    result['engine_state']['unresolved_by_id']={row['note_id']:hold}
    return result
