"""Exact run16 predecessor for the single proven-expired PNG recovery.

This module authenticates the existing durable anchor only. It cannot issue
network writes, reset an upload, amend an old plan or authorize replacement.
The separate replacement contract requires fresh expired and missing-page
observations plus its exact prior state at the atomic transition boundary.
"""
from copy import deepcopy

TAG = 'exact_run16_expired_png_predecessor_v1'
RUN = 39087117510
ATTEMPT = 1
JOB = 117555904912
PUBLIC_POLICY = {'code_sha': '54977fd93e0f4130dc24dd2083ec2e80e089dbd9', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': '6faf7c4d3a58c186f9f72fb9dd94d9061b9528c4999381eed67406692fc5c824', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': '58933680530fb1855fdec2094284028f9ca160b1ac91caf21ede5309f3ec1dba', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
PUBLIC_SHA = 'c34865c7c37859dd9410e5c265c8a41c6c5798709e3df492cd172f9931495a0a'
NOTION_POLICY = 'a6babaa293cbe19349bb46be82a4c097edafb423d08e76680a9b48290571d18c'
CHECKPOINT = {'cas_attempts': 0, 'code_sha': '54977fd93e0f4130dc24dd2083ec2e80e089dbd9', 'commit': '427f68d9066ff0fa0cfe5e7aba5785115166f12f', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': '07650fabcd856c9dbd04c1b6158c4154de5383d9', 'notion_policy_sha256': 'a6babaa293cbe19349bb46be82a4c097edafb423d08e76680a9b48290571d18c', 'parent_commit': None, 'previous_record_sha256': '7f7a87c9a6db9131b9183b5dc7204aee8407ccfdb929e5510820062464e8ad54', 'public_policy_sha256': 'c34865c7c37859dd9410e5c265c8a41c6c5798709e3df492cd172f9931495a0a', 'readback_verified': True, 'record_sha256': '7b58828840d1ddcd56cb211d6e7121740cfa07f49df53f8a874d36abad9c31f8', 'rules_sha256': '58933680530fb1855fdec2094284028f9ca160b1ac91caf21ede5309f3ec1dba', 'run_attempt': 1, 'run_id': 39087117510, 'sequence': 4, 'state_blob': '0b73977c64c1d75339706d5905afb481ab4e711f', 'state_bytes': 688249, 'state_sha256': 'dca881ff71adbb73fede0c42ed9eb68d5a3447b64eef62710d149a797d9a3146', 'status': 'journal_readback_verified', 'tree': 'b04348c954304a7d5ffe385d7c214c90034463dd'}
GET_RECORD = {'cas_attempts': 0, 'code_sha': '54977fd93e0f4130dc24dd2083ec2e80e089dbd9', 'contract': 'get-canonical-publication-anchor-1', 'controller_sha': '07650fabcd856c9dbd04c1b6158c4154de5383d9', 'main_updated': False, 'observed_main_commit': 'f9ad7c0b66d8b50553e69b1aa3d85d0afc9180ea', 'policy_sha256': '6faf7c4d3a58c186f9f72fb9dd94d9061b9528c4999381eed67406692fc5c824', 'readback_verified': True, 'receipt_commit': 'a37cc108096210d0fb9847609e906ca22c88acf4', 'receipt_sequence': 1, 'receipt_sha256': 'e49b99c42729ca079cdc04ed75145f39b12aad2d1a587dfcd0cafce9344f7293', 'record_sha256': '94063b329d55b60524d676ddba1d426eb0c14f493baf1a9d6fa72c7bd6ecce2d', 'rules_sha256': '58933680530fb1855fdec2094284028f9ca160b1ac91caf21ede5309f3ec1dba', 'run_attempt': 1, 'run_id': 39087117510, 'status': 'canonical_publication_verified'}
ANCHOR = {'commit': '427f68d9066ff0fa0cfe5e7aba5785115166f12f', 'state_blob': '0b73977c64c1d75339706d5905afb481ab4e711f', 'state_sha256': 'dca881ff71adbb73fede0c42ed9eb68d5a3447b64eef62710d149a797d9a3146'}
STATE_BYTES = 688249


def recover_checkpoint(client, run_id, run_attempt):
    """One exact predecessor, using the caller's shared bounded reader."""
    import get_notion_outbox as o
    import get_notion_journal_runtime as c
    if type(run_id) is not int or type(run_attempt) is not int or (run_id,run_attempt)!=(RUN,ATTEMPT):
        c.fail('journal_provenance_other_controller')
    result=c.recover_anchor(client,run_id=RUN,run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY),approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY)
    if (result.get('anchor')!=ANCHOR or result.get('job_id')!=JOB
            or result.get('checkpoint_count')!=4
            or not o.e.same(result.get('checkpoint'),CHECKPOINT)
            or not o.e.same(result.get('get_step_record'),GET_RECORD)
            or result.get('private_readback_required') is not True):
        c.fail('journal_provenance_record')
    return {**result,'recovery_anchor':TAG}
