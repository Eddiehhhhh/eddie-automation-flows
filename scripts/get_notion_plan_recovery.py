"""One exact interrupted initial transaction, never a general old-rules bypass.

These hashes come from the reviewed successful public run and private readback.
New execution keeps its actual new code/policy identity. Only an already durable
copy of this unchanged plan may resume; a fresh plan never borrows old rules.
"""
from copy import deepcopy
import get_notion_sink_plan as p

OLD_RUN = 39017569036
OLD_ATTEMPT = 1
OLD_JOB = 117352723457
OLD_CONTROLLER = '94578b66b77fde7e9945013b9d48666b81122e52'
OLD_PUBLIC_SHA = 'bd9828bdba1f61b2c02a23c9ff59ada81d039df25a0d296344e48b6a00745ac7'
OLD_NOTION_POLICY = '1d8c999021341aee337c49d0503eacc6edfbde6ea55f3d064235d42ce71e7637'
OLD_PLAN = 'd5e8809c7dc4c74bd3e9414f42564b11763d85b44b16750fa5718e5ed615ba75'
OLD_ENGINE_RULES = '78d389c249c6f7a1a4c050385a153c933a4cef41f985689f0fadaea3050e5146'
OLD_CHECKPOINT = '3d219fb087abba728befe1dfabd13a28348f5f63cfdde8f5f92df88f88b362ec'
OLD_ANCHOR = {'commit':'2b59082552160525b04dde4f3771252357467eb9',
    'state_blob':'800323adc1d7a32404163d698a22b1cb4355ee2d',
    'state_sha256':'49412a1327696c894a10b10a66a0d6e132f06df55c66d7216082b146c7434958'}
OLD_GET = {'receipt_commit':'a37cc108096210d0fb9847609e906ca22c88acf4',
    'receipt_sha256':'e49b99c42729ca079cdc04ed75145f39b12aad2d1a587dfcd0cafce9344f7293',
    'receipt_sequence':1,
    'record_sha256':'156ee549a46a3d5c6501fe7e0d729d14905e47723750152db849785968e6854f'}
OLD_PUBLIC_POLICY = {'code_sha':'d1a7e5c3b57a107a55ee898a2b5d006edbe165df',
    'contract':'get-public-actions-provenance-policy-1',
    'controller_template_sha256':'66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2',
    'get_policy_sha256':'3c910f50e8b269522f156fa43bffebda1123bb4d2776031358ed3bb24f66440a',
    'head_branch':'main','job_name':'canonical-get',
    'publication_step_name':'Emit verified canonical publication anchor',
    'repository':'Eddiehhhhh/eddie-automation-flows','repository_id':1270807355,
    'rules_sha256':'1fa93e64ff0f54c3f3d88320b1eb121629cb3d36bb9ddcc65bea1f013a632cac',
    'workflow_id':387242896,'workflow_path':'.github/workflows/get-canonical.yml'}


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
            or result.get('checkpoint_count')!=31 or record.get('record_sha256')!=OLD_CHECKPOINT
            or record.get('controller_sha')!=OLD_CONTROLLER
            or any(get.get(k)!=v for k,v in OLD_GET.items())
            or result.get('private_readback_required') is not True):
        checkpoints.fail('journal_provenance_record')
    return {**result,'recovery_anchor':'exact_initial_transaction_v1'}


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
