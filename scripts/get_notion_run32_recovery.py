"""One exact completed idle #32 checkpoint; never authorizes new effects."""
from copy import deepcopy

TAG = 'exact_run32_confirmed_checkpoint_v1'
RUN = 39302062156
ATTEMPT = 1
JOB = 118229528448
PUBLIC_POLICY = {'code_sha': '8fc30465a467437f35eb83fee483b549ed8dbc3d', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': 'c1856af75550501eb9460d53d08e900a1d3e8f2922a7a3062520f7dbfaae6c59', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': '94ccc816957eb16fa2b5aa5fdb06c55acaf5304bf9ebb2e0c8be02ef3dc42bfd', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
PUBLIC_SHA = '34af7ef86c2a016a87d18906181eeb493241d679aa5849af183250a8df232c6e'
NOTION_POLICY = 'd73ae0c6e75380b07505ec1a3a3ad0d13e0a85b6f1f8713310e183aa1d5656cc'
CHECKPOINT = {'cas_attempts': 0, 'code_sha': '8fc30465a467437f35eb83fee483b549ed8dbc3d', 'commit': '45178f56f0d1d1f97d23d355875af0c0be833d3d', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': 'a18096387e8c7d62f77e0fb9e51d2f83d7356edb', 'notion_policy_sha256': 'd73ae0c6e75380b07505ec1a3a3ad0d13e0a85b6f1f8713310e183aa1d5656cc', 'parent_commit': None, 'previous_record_sha256': '10d7f05ab14411d80c2c65bea7126cf1a27c9f18037ba2454f67c43e718196b8', 'public_policy_sha256': '34af7ef86c2a016a87d18906181eeb493241d679aa5849af183250a8df232c6e', 'readback_verified': True, 'record_sha256': '24ed72fdc9f06f4aae762632ebe9c35f5f8e9c12743d417f28a39880837e0ce2', 'rules_sha256': '94ccc816957eb16fa2b5aa5fdb06c55acaf5304bf9ebb2e0c8be02ef3dc42bfd', 'run_attempt': 1, 'run_id': 39302062156, 'sequence': 3, 'state_blob': 'df45022d4c5a75db2a8e0ddf9684c1fc33665fa6', 'state_bytes': 113301, 'state_sha256': '2ed534aa7813fdd473cff25decf88365ed2e746c5730266926d088761f7970b1', 'status': 'journal_readback_verified', 'tree': 'f499ef94ccd73dff7b80a09ee2e06e8b0241ce7d'}
GET_RECORD = {'cas_attempts': 0, 'code_sha': '8fc30465a467437f35eb83fee483b549ed8dbc3d', 'contract': 'get-canonical-publication-anchor-1', 'controller_sha': 'a18096387e8c7d62f77e0fb9e51d2f83d7356edb', 'main_updated': False, 'observed_main_commit': '579b23ff45731eb4988dbc31458cd553d9682c61', 'policy_sha256': 'c1856af75550501eb9460d53d08e900a1d3e8f2922a7a3062520f7dbfaae6c59', 'readback_verified': True, 'receipt_commit': '1678207017f96eb8022b4f4fb540b08d8654fb38', 'receipt_sequence': 2, 'receipt_sha256': '56ec351baa67ef704d827b545c416633303d15b97fde14be2c139970ea411b91', 'record_sha256': '4dd55a68df978be244c2a453e5c63f37946dff43ea516af5b3e9370ba941be91', 'rules_sha256': '94ccc816957eb16fa2b5aa5fdb06c55acaf5304bf9ebb2e0c8be02ef3dc42bfd', 'run_attempt': 1, 'run_id': 39302062156, 'status': 'canonical_publication_verified'}
ANCHOR = {'commit': '45178f56f0d1d1f97d23d355875af0c0be833d3d', 'state_blob': 'df45022d4c5a75db2a8e0ddf9684c1fc33665fa6', 'state_sha256': '2ed534aa7813fdd473cff25decf88365ed2e746c5730266926d088761f7970b1'}


def recover_checkpoint(client, run_id, run_attempt):
    import get_notion_outbox as o
    import get_notion_journal_runtime as c
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(RUN,ATTEMPT):
        c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(client,run_id=RUN,run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY),approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY)
    if (result.get('anchor')!=ANCHOR or result.get('job_id')!=JOB
            or result.get('checkpoint_count')!=3
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
