"""One exact terminal run13 checkpoint → PNG version queue-policy bridge.

No credentials, HTTP client, ref writes, or old-plan execution exception.
The caller authenticates the new policy, then confirms the single-field state
change by the existing RemoteJournal CAS and NEW policy checkpoint emitter.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
import get_notion_outbox as o

TAG = 'exact_run13_idle_png_predecessor_v1'
RUN = _private_bindings.value('get_notion_png_recovery', 'RUN')
ATTEMPT = _private_bindings.value('get_notion_png_recovery', 'ATTEMPT')
JOB = _private_bindings.value('get_notion_png_recovery', 'JOB')
COMPLETED_AT = _private_bindings.value('get_notion_png_recovery', 'COMPLETED_AT')
PUBLIC_POLICY = _private_bindings.value('get_notion_png_recovery', 'PUBLIC_POLICY')
PUBLIC_SHA = _private_bindings.value('get_notion_png_recovery', 'PUBLIC_SHA')
NOTION_POLICY = _private_bindings.value('get_notion_png_recovery', 'NOTION_POLICY')
CHECKPOINT = _private_bindings.value('get_notion_png_recovery', 'CHECKPOINT')
GET_RECORD = _private_bindings.value('get_notion_png_recovery', 'GET_RECORD')
ANCHOR = _private_bindings.value('get_notion_png_recovery', 'ANCHOR')
STATE_BYTES = _private_bindings.value('get_notion_png_recovery', 'STATE_BYTES')

class StrictSuccess:
    """Inspect existing reads; no extra request, alternate client, or cache."""
    def __init__(self, client): self.client=client
    def json(self, path):
        value=self.client.json(path)
        import get_notion_journal_runtime as c
        root='/actions/runs/'+str(RUN)+'/attempts/'+str(ATTEMPT)
        if path==root:
            if (not isinstance(value,dict) or value.get('event')!='schedule'
                    or value.get('status')!='completed' or value.get('conclusion')!='success'):
                c.fail('journal_provenance_run')
        elif path==root+'/jobs?per_page=100&page=1':
            jobs=value.get('jobs') if isinstance(value,dict) else None
            if not isinstance(jobs,list): c.fail('journal_provenance_jobs')
            selected=[j for j in jobs if isinstance(j,dict) and j.get('name')==PUBLIC_POLICY['job_name']]
            if (len(selected)!=1 or selected[0].get('id')!=JOB
                    or selected[0].get('status')!='completed' or selected[0].get('conclusion')!='success'
                    or selected[0].get('completed_at')!=COMPLETED_AT):
                c.fail('journal_provenance_job')
            steps=selected[0].get('steps')
            if not isinstance(steps,list): c.fail('journal_provenance_steps')
            for name in (PUBLIC_POLICY['publication_step_name'],c.NOTION_STEP):
                rows=[s for s in steps if isinstance(s,dict) and s.get('name')==name]
                if len(rows)!=1 or rows[0].get('status')!='completed' or rows[0].get('conclusion')!='success':
                    c.fail('journal_provenance_steps')
        return value
    def logs(self, job): return self.client.logs(job)


def recover_checkpoint(client, run_id, run_attempt):
    """Only after exact new-controller mismatch, using its same read budget."""
    import get_notion_journal_runtime as c
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(RUN,ATTEMPT):
        c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(StrictSuccess(client),run_id=RUN,run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY),approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY)
    run=result.get('run_binding',{})
    if (result.get('anchor')!=ANCHOR or result.get('job_id')!=JOB
            or result.get('checkpoint_count')!=1 or not o.e.same(result.get('checkpoint'),CHECKPOINT)
            or not o.e.same(result.get('get_step_record'),GET_RECORD)
            or result.get('completed_at')!=COMPLETED_AT
            or run.get('event')!='schedule' or run.get('status')!='completed' or run.get('conclusion')!='success'
            or result.get('private_readback_required') is not True):
        c.fail('journal_provenance_record')
    return {**result,'recovery_anchor':TAG}


def validate_state(state, sha):
    """Fixed original bytes, never self-hashed ownership or current-tree adoption."""
    try:
        o.check_state(state)
        return (sha==ANCHOR['state_sha256'] and o.digest(state)==sha
            and len(o.encoded(state))==STATE_BYTES and state['engine_state']['journal'] is None
            and state['policy_sha256']==PUBLIC_POLICY['get_policy_sha256']
            and len(state['engine_state']['receipts'])==4 and len(state['pending_by_id'])==7
            and len(state['engine_state'].get('unresolved_by_id',{}))==1)
    except Exception:return False


def transition_outer(state, sha, new_get_policy_sha256):
    """Pure one-time candidate. New policy SHA is independently authenticated.

    Caller must first recover this fixed checkpoint and read the exact private
    anchor with RemoteJournal, then CAS/readback before using the new queue.
    After that, only a normal new-policy checkpoint can resume it. This function
    never accepts the resulting state a second time and never re-arms a plan.
    """
    if not validate_state(state,sha):raise ValueError('state_invalid')
    o.p.hex_hash(new_get_policy_sha256,64)
    if new_get_policy_sha256==state['policy_sha256']:raise ValueError('state_invalid')
    result=deepcopy(state);result['policy_sha256']=new_get_policy_sha256
    o.check_state(result)
    return result
