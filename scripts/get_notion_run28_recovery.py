"""One exact previously verified Notion checkpoint; never authorizes new effects."""
from copy import deepcopy

TAG = 'exact_run28_confirmed_checkpoint_v1'

RUN = 39276816133
ATTEMPT = 1
JOB = 118143161864
PUBLIC_POLICY = {'code_sha': '5996f6e94db4cc61c407e291ebffa92a7964a02f', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': 'fb37df47d2e5c83b8ff32e0da42ff2afe85cc6ab0a229559e8a81e7117b364ab', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': '470fdace19b761aed392c667332bbffbffe64ac627a446eadaf7382277f825aa', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
PUBLIC_SHA = '72fc288b4484c6dfcd009fcbfbee852c84ce52b6bca28d606a9389cfe7253307'
NOTION_POLICY = 'f5ce20df88cbd68c16ffd9341775dfba3fbd41cf73328d70e3a9e9d8c558cd78'
CHECKPOINT = {'cas_attempts': 0, 'code_sha': '5996f6e94db4cc61c407e291ebffa92a7964a02f', 'commit': '4118be8800d3599053f2e65305775d36e08c7d38', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': '30ba7b80903574f73ccfa493ff16ab6ff4ce44b9', 'notion_policy_sha256': 'f5ce20df88cbd68c16ffd9341775dfba3fbd41cf73328d70e3a9e9d8c558cd78', 'parent_commit': None, 'previous_record_sha256': 'e67467c4caa5cb564fb42f9ab94b1172e53a30da0ba5f9c2d05ac2fe11338de8', 'public_policy_sha256': '72fc288b4484c6dfcd009fcbfbee852c84ce52b6bca28d606a9389cfe7253307', 'readback_verified': True, 'record_sha256': 'e3a07f8405b590a3358de9eb34df67f026907c754c7a0ffb1f7c2528477bf31a', 'rules_sha256': '470fdace19b761aed392c667332bbffbffe64ac627a446eadaf7382277f825aa', 'run_attempt': 1, 'run_id': 39276816133, 'sequence': 6, 'state_blob': '92cd647b1efd9dba1b4018cc431b7dad176a4049', 'state_bytes': 75804, 'state_sha256': '897f347ed1ac1808abb960e929b3da3efc088d9d6bc5f99f198577ddeb1a85d1', 'status': 'journal_readback_verified', 'tree': '9cecb88bb25f794ae1e2bfbfba33039fe7069525'}
GET_RECORD = {'cas_attempts': 0, 'code_sha': '5996f6e94db4cc61c407e291ebffa92a7964a02f', 'contract': 'get-canonical-publication-anchor-1', 'controller_sha': '30ba7b80903574f73ccfa493ff16ab6ff4ce44b9', 'main_updated': False, 'observed_main_commit': '3c4b93a5a97d040020a2f654ad9181b0664ff8aa', 'policy_sha256': 'fb37df47d2e5c83b8ff32e0da42ff2afe85cc6ab0a229559e8a81e7117b364ab', 'readback_verified': True, 'receipt_commit': '1678207017f96eb8022b4f4fb540b08d8654fb38', 'receipt_sequence': 2, 'receipt_sha256': '56ec351baa67ef704d827b545c416633303d15b97fde14be2c139970ea411b91', 'record_sha256': 'bc11c78f6acb350f8121e18dc0038a503afccda38485386f1049713ffef5a714', 'rules_sha256': '470fdace19b761aed392c667332bbffbffe64ac627a446eadaf7382277f825aa', 'run_attempt': 1, 'run_id': 39276816133, 'status': 'canonical_publication_verified'}
ANCHOR = {'commit': '4118be8800d3599053f2e65305775d36e08c7d38', 'state_blob': '92cd647b1efd9dba1b4018cc431b7dad176a4049', 'state_sha256': '897f347ed1ac1808abb960e929b3da3efc088d9d6bc5f99f198577ddeb1a85d1'}


def recover_checkpoint(client, run_id, run_attempt):
    import get_notion_outbox as o
    import get_notion_journal_runtime as c
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(RUN,ATTEMPT):
        c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(client,run_id=RUN,run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY),approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY)
    if (result.get('anchor')!=ANCHOR or result.get('job_id')!=JOB
            or result.get('checkpoint_count')!=6
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
