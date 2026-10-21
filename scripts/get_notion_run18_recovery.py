"""One exact previously verified Notion checkpoint; never authorizes new effects."""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy

TAG = 'exact_run18_confirmed_checkpoint_v1'

RUN = _private_bindings.value('get_notion_run18_recovery', 'RUN')
ATTEMPT = _private_bindings.value('get_notion_run18_recovery', 'ATTEMPT')
JOB = _private_bindings.value('get_notion_run18_recovery', 'JOB')
PUBLIC_POLICY = _private_bindings.value('get_notion_run18_recovery', 'PUBLIC_POLICY')
PUBLIC_SHA = _private_bindings.value('get_notion_run18_recovery', 'PUBLIC_SHA')
NOTION_POLICY = _private_bindings.value('get_notion_run18_recovery', 'NOTION_POLICY')
CHECKPOINT = _private_bindings.value('get_notion_run18_recovery', 'CHECKPOINT')
GET_RECORD = _private_bindings.value('get_notion_run18_recovery', 'GET_RECORD')
ANCHOR = _private_bindings.value('get_notion_run18_recovery', 'ANCHOR')


def recover_checkpoint(client, run_id, run_attempt):
    import get_notion_outbox as o
    import get_notion_journal_runtime as c
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(RUN,ATTEMPT):
        c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(client,run_id=RUN,run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY),approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY)
    if (result.get('anchor')!=ANCHOR or result.get('job_id')!=JOB
            or result.get('checkpoint_count')!=1
            or not o.e.same(result.get('checkpoint'),CHECKPOINT)
            or not o.e.same(result.get('get_step_record'),GET_RECORD)
            or result.get('private_readback_required') is not True):
        c.fail('journal_provenance_record')
    return {**result,'recovery_anchor':TAG}


def transition_outer(state, sha, new_get_policy_sha256):
    """Rebind only the exact idle outbox policy; preserve every saved effect."""
    import get_notion_outbox as o
    import get_recurring_policy_recovery as recovery
    o.check_state(state)
    if (sha != ANCHOR['state_sha256'] or o.digest(state) != sha
            or state['policy_sha256'] != PUBLIC_POLICY['get_policy_sha256']
            or state['engine_state']['journal'] is not None
            or new_get_policy_sha256 != recovery.current_get_policy()):
        o.fail('outbox_state_invalid')
    result=deepcopy(state)
    result['policy_sha256']=new_get_policy_sha256
    o.check_state(result)
    return result
