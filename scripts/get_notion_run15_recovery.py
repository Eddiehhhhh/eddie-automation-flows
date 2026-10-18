"""Exact run15 acknowledged PNG create recovery; pure bridge, no writes.

Only a caller-authenticated durable journal may use allows(). Preparation never
receives this exception. Saved page/upload handles and plan remain unchanged.
"""
from copy import deepcopy
import get_notion_sink_plan as p

TAG = 'exact_run15_acknowledged_png_create_v1'
RUN = 39058433489
ATTEMPT = 1
JOB = 117471110813
PUBLIC_POLICY = {'code_sha': '086fc1425a95f7bce6df55b3e36aa449f679a316', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': '4a3dd63ea5f98cd5f7845926a9d9f5285a474d9034751c886cebfd9e380932ec', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': 'ed9c832a4498ae6f09c07091e8983a2c9738687a7eee717d9e83d1630c066ccb', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
PUBLIC_SHA = 'd5dee1f47ffbe9ff65acd346480947f41edbe7cca68b3d017b11cbbce38b58c8'
NOTION_POLICY = '6dd0c640b83fadca02b08a2a938ed5f76160f436443965ca760d087abf6995e2'
CHECKPOINT = {'cas_attempts': 0, 'code_sha': '086fc1425a95f7bce6df55b3e36aa449f679a316', 'commit': 'cd18cea23ef94cba570afd45c47ae16be8af6745', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': '1a9c013c1928caa3a2f1dde6e8295dd3a8d53f23', 'notion_policy_sha256': '6dd0c640b83fadca02b08a2a938ed5f76160f436443965ca760d087abf6995e2', 'parent_commit': None, 'previous_record_sha256': '26db23b571b5fadae05859b69c66af36ef74b369956a74b3c3c93ea654c8ac74', 'public_policy_sha256': 'd5dee1f47ffbe9ff65acd346480947f41edbe7cca68b3d017b11cbbce38b58c8', 'readback_verified': True, 'record_sha256': '2c7495c7fd2e3294ff48bd2df59050a02a83aceda723c199bc9fbc3350bfa318', 'rules_sha256': 'ed9c832a4498ae6f09c07091e8983a2c9738687a7eee717d9e83d1630c066ccb', 'run_attempt': 1, 'run_id': 39058433489, 'sequence': 15, 'state_blob': '045b41400e1b539fde0586248723ac8305dd0ab3', 'state_bytes': 673850, 'state_sha256': '25a4ed324a22068d36234bafffcb7c1736a224592a1b0265be9352f1dd998ee0', 'status': 'journal_readback_verified', 'tree': '6a8aad0fa379f1ca0caf7826c438d653ee4a1d8d'}
GET_RECORD = {'cas_attempts': 0, 'code_sha': '086fc1425a95f7bce6df55b3e36aa449f679a316', 'contract': 'get-canonical-publication-anchor-1', 'controller_sha': '1a9c013c1928caa3a2f1dde6e8295dd3a8d53f23', 'main_updated': False, 'observed_main_commit': '58d3eb9c10c306605d2713afcf0968a4bc439aa9', 'policy_sha256': '4a3dd63ea5f98cd5f7845926a9d9f5285a474d9034751c886cebfd9e380932ec', 'readback_verified': True, 'receipt_commit': 'a37cc108096210d0fb9847609e906ca22c88acf4', 'receipt_sequence': 1, 'receipt_sha256': 'e49b99c42729ca079cdc04ed75145f39b12aad2d1a587dfcd0cafce9344f7293', 'record_sha256': '22041ff25d642a6526e80d1064dddf50c392ef4435289128c915f392f54a7ad3', 'rules_sha256': 'ed9c832a4498ae6f09c07091e8983a2c9738687a7eee717d9e83d1630c066ccb', 'run_attempt': 1, 'run_id': 39058433489, 'status': 'canonical_publication_verified'}
ANCHOR = {'commit': 'cd18cea23ef94cba570afd45c47ae16be8af6745', 'state_blob': '045b41400e1b539fde0586248723ac8305dd0ab3', 'state_sha256': '25a4ed324a22068d36234bafffcb7c1736a224592a1b0265be9352f1dd998ee0'}
STATE_BYTES = 673850
PLAN_SHA = 'a54e2736d552ce4fc4f14810bb052745dfc931468b7d06b6bc5608678bdf18b5'
OP_SHA = '03904ded8243ecfd65115107fcd22414a976e242a901a8b5566669c6f9a4680c'
ENGINE_RULES = '8864f763427a4f9a4dad677c0ef55888e884e9f6a3d8b8b1772aa51a463549b7'


def allows(plan, journal):
    """Exact original plan only, including later authenticated resume checkpoints."""
    try:
        return (isinstance(plan, dict) and isinstance(journal, dict)
            and plan.get('rules_sha256') == ENGINE_RULES
            and p.digest(plan) == PLAN_SHA and journal.get('plan_sha256') == PLAN_SHA
            and p.encoded(journal.get('plan')) == p.encoded(plan))
    except Exception: return False


def recover_checkpoint(client, run_id, run_attempt):
    """Reuse the caller's bounded provenance reader; no fallback target search."""
    import get_notion_outbox as o
    import get_notion_journal_runtime as c
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(RUN,ATTEMPT):
        c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(client,run_id=RUN,run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY),approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY)
    if (result.get('anchor')!=ANCHOR or result.get('job_id')!=JOB
            or result.get('checkpoint_count')!=15
            or not o.e.same(result.get('checkpoint'),CHECKPOINT)
            or not o.e.same(result.get('get_step_record'),GET_RECORD)
            or result.get('private_readback_required') is not True):
        c.fail('journal_provenance_record')
    return {**result,'recovery_anchor':TAG}


def validate_state(state, sha):
    import get_notion_outbox as o
    try:
        o.check_state(state)
        journal=state['engine_state']['journal'];assets=state['png_assets']
        return (sha==ANCHOR['state_sha256'] and o.digest(state)==sha
            and len(o.encoded(state))==STATE_BYTES
            and state['policy_sha256']==PUBLIC_POLICY['get_policy_sha256']
            and allows(journal['plan'],journal) and journal['index']==0
            and journal['operation']['kind']=='create'
            and journal['operation']['phase']=='acknowledged'
            and o.digest(journal['operation'])==OP_SHA
            and len(state['engine_state']['receipts'])==4
            and len(state['pending_by_id'])==7
            and len(state['engine_state']['unresolved_by_id'])==1
            and len(assets['uploads'])==2 and not assets['attachments']
            and all(x['state']['phase']=='uploaded_verified' for x in assets['uploads'].values()))
    except Exception: return False


def transition_outer(state, sha, new_get_policy_sha256):
    """Only the outer policy changes; caller confirms CAS and NEW checkpoint.

    The existing engine resumes its acknowledged saved page by readback. This
    function cannot create a page, upload a file, reset an operation or remove a
    hold. Its output cannot be passed through this old-anchor transition again.
    """
    import get_notion_outbox as o
    if not validate_state(state,sha): raise ValueError('state_invalid')
    o.p.hex_hash(new_get_policy_sha256,64)
    if new_get_policy_sha256==state['policy_sha256']: raise ValueError('state_invalid')
    result=deepcopy(state);result['policy_sha256']=new_get_policy_sha256
    o.check_state(result)
    return result
