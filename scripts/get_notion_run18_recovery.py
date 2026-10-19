"""One exact previously verified Notion checkpoint; never authorizes new effects."""
from copy import deepcopy

TAG = 'exact_run18_confirmed_checkpoint_v1'

RUN = 39093261469
ATTEMPT = 1
JOB = 117574291590
PUBLIC_POLICY = {'code_sha': 'e838f97e02bce70a1ec2a4a47e8ad5727855a7c8', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': 'cce463077e9216fda14e19f06fa0d21acd060c5024389b4a603c54309d036efd', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': '6bde255c93626e51c30ed548899f98a381afd251ffb047cbb33207fbd7fc8cf0', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
PUBLIC_SHA = '132e2fc6983116ce7106bafbd6a1930c93bf46581377e87e43e9e44a6bc12d23'
NOTION_POLICY = 'b1487088a5c86444dca87849c59cc818e965e6f2aab4b70571273dd7c9782abb'
CHECKPOINT = {'cas_attempts': 0, 'code_sha': 'e838f97e02bce70a1ec2a4a47e8ad5727855a7c8', 'commit': 'd660f138392333024fa60f4fa075df78d927bbae', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': 'e2d09ed2755ceb4d5326c1f76777b95e9a55d115', 'notion_policy_sha256': 'b1487088a5c86444dca87849c59cc818e965e6f2aab4b70571273dd7c9782abb', 'parent_commit': None, 'previous_record_sha256': None, 'public_policy_sha256': '132e2fc6983116ce7106bafbd6a1930c93bf46581377e87e43e9e44a6bc12d23', 'readback_verified': True, 'record_sha256': '763ac56656eec1c008c1be3e9ef467b13ee5576634398eebfc610b1d406bb8dd', 'rules_sha256': '6bde255c93626e51c30ed548899f98a381afd251ffb047cbb33207fbd7fc8cf0', 'run_attempt': 1, 'run_id': 39093261469, 'sequence': 1, 'state_blob': '35da2421b01e6148cf59fc806075c870ecbc3fd2', 'state_bytes': 73623, 'state_sha256': 'b888cb302aa0a04161a8e9539a929620f3c297be062e65cd1c31d1d8fb3c4ac4', 'status': 'journal_readback_verified', 'tree': '707a651d8dca367c6b32cf2e1c2500152399c2ab'}
GET_RECORD = {'cas_attempts': 0, 'code_sha': 'e838f97e02bce70a1ec2a4a47e8ad5727855a7c8', 'contract': 'get-canonical-publication-anchor-1', 'controller_sha': 'e2d09ed2755ceb4d5326c1f76777b95e9a55d115', 'main_updated': False, 'observed_main_commit': 'd660f138392333024fa60f4fa075df78d927bbae', 'policy_sha256': 'cce463077e9216fda14e19f06fa0d21acd060c5024389b4a603c54309d036efd', 'readback_verified': True, 'receipt_commit': 'a37cc108096210d0fb9847609e906ca22c88acf4', 'receipt_sequence': 1, 'receipt_sha256': 'e49b99c42729ca079cdc04ed75145f39b12aad2d1a587dfcd0cafce9344f7293', 'record_sha256': 'a29f9f43154f35907de97e04afd34939ce92f3950116a2f5589826d2197c788c', 'rules_sha256': '6bde255c93626e51c30ed548899f98a381afd251ffb047cbb33207fbd7fc8cf0', 'run_attempt': 1, 'run_id': 39093261469, 'status': 'canonical_publication_verified'}
ANCHOR = {'commit': 'd660f138392333024fa60f4fa075df78d927bbae', 'state_blob': '35da2421b01e6148cf59fc806075c870ecbc3fd2', 'state_sha256': 'b888cb302aa0a04161a8e9539a929620f3c297be062e65cd1c31d1d8fb3c4ac4'}


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
