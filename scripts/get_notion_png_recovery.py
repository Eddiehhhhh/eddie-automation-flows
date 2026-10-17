"""One exact terminal run13 checkpoint → PNG version queue-policy bridge.

No credentials, HTTP client, ref writes, or old-plan execution exception.
The caller authenticates the new policy, then confirms the single-field state
change by the existing RemoteJournal CAS and NEW policy checkpoint emitter.
"""
from copy import deepcopy
import get_notion_outbox as o

TAG = 'exact_run13_idle_png_predecessor_v1'
RUN = 39052510643
ATTEMPT = 1
JOB = 117453779434
COMPLETED_AT = '2026-10-17T17:25:24Z'
PUBLIC_POLICY = {'code_sha': 'b061ef1eab804693d3987f60971b37a1297c90d7', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': 'cc204614334fadaa21e20d6816f70518dbab56c31ab96f24b4f08176822faed5', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': '19bb3c40ee1b6fef1fd98b64a25fd44fbfce6a1d03b75afb921fe18ab112b2ee', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
PUBLIC_SHA = 'f3fb5d9e326a59d166b638712fd02107c4e3c2d9fd1c29534398dd8d55efce18'
NOTION_POLICY = '9dd3cc5228132cf2e00fd913b11f6324b4016afce1f96ed8fa716d989aa28151'
CHECKPOINT = {'cas_attempts': 0, 'code_sha': 'b061ef1eab804693d3987f60971b37a1297c90d7', 'commit': '8012fba794ad32655e5f97449f05f8449835e670', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': '4361f151af7c4206f526e7510e9d24b12f88551f', 'notion_policy_sha256': '9dd3cc5228132cf2e00fd913b11f6324b4016afce1f96ed8fa716d989aa28151', 'parent_commit': None, 'previous_record_sha256': None, 'public_policy_sha256': 'f3fb5d9e326a59d166b638712fd02107c4e3c2d9fd1c29534398dd8d55efce18', 'readback_verified': True, 'record_sha256': '4acb6c13cfe2067ecd24af6b35ad4a04fd98e4b789599555ccc5e520e6dd2e2e', 'rules_sha256': '19bb3c40ee1b6fef1fd98b64a25fd44fbfce6a1d03b75afb921fe18ab112b2ee', 'run_attempt': 1, 'run_id': 39052510643, 'sequence': 1, 'state_blob': '97fee9101beb3de30e485dde27fc6c72f9aa7b6a', 'state_bytes': 40988, 'state_sha256': '483e1f9c34de46489a37ca3cba1ae738abf34bb7ce4157657c41ff94c5cbb96a', 'status': 'journal_readback_verified', 'tree': '186f6a1b8f476c67e9bb3e82059051a8eac1840a'}
GET_RECORD = {'cas_attempts': 0, 'code_sha': 'b061ef1eab804693d3987f60971b37a1297c90d7', 'contract': 'get-canonical-publication-anchor-1', 'controller_sha': '4361f151af7c4206f526e7510e9d24b12f88551f', 'main_updated': False, 'observed_main_commit': '8012fba794ad32655e5f97449f05f8449835e670', 'policy_sha256': 'cc204614334fadaa21e20d6816f70518dbab56c31ab96f24b4f08176822faed5', 'readback_verified': True, 'receipt_commit': 'a37cc108096210d0fb9847609e906ca22c88acf4', 'receipt_sequence': 1, 'receipt_sha256': 'e49b99c42729ca079cdc04ed75145f39b12aad2d1a587dfcd0cafce9344f7293', 'record_sha256': 'b57c2589a7358b2413c5e1a5921f3d765a936db387023b87fe90648dc9e8f297', 'rules_sha256': '19bb3c40ee1b6fef1fd98b64a25fd44fbfce6a1d03b75afb921fe18ab112b2ee', 'run_attempt': 1, 'run_id': 39052510643, 'status': 'canonical_publication_verified'}
ANCHOR = {'commit': '8012fba794ad32655e5f97449f05f8449835e670', 'state_blob': '97fee9101beb3de30e485dde27fc6c72f9aa7b6a', 'state_sha256': '483e1f9c34de46489a37ca3cba1ae738abf34bb7ce4157657c41ff94c5cbb96a'}
STATE_BYTES = 40988

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
