"""Two exact publication proofs for the production recovery code repairs.

These are external publication evidence, not values learned from a receipt or
repository state. This exception admits only the original sequence-one receipt
unchanged; all normal parent, complete-delta and ownership checks still run.
New receipts and public records always carry the executing runtime's policy.
"""
from copy import deepcopy
import get_notion_run32_recovery as run32_recovery

OLD_PUBLIC_POLICY_SHA256 = 'bd9828bdba1f61b2c02a23c9ff59ada81d039df25a0d296344e48b6a00745ac7'
_OLD_PUBLIC_POLICY = {
    'code_sha': 'd1a7e5c3b57a107a55ee898a2b5d006edbe165df',
    'contract': 'get-public-actions-provenance-policy-1',
    'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2',
    'get_policy_sha256': '3c910f50e8b269522f156fa43bffebda1123bb4d2776031358ed3bb24f66440a',
    'head_branch': 'main', 'job_name': 'canonical-get',
    'publication_step_name': 'Emit verified canonical publication anchor',
    'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355,
    'rules_sha256': '1fa93e64ff0f54c3f3d88320b1eb121629cb3d36bb9ddcc65bea1f013a632cac',
    'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml',
}
ORIGINAL_RUN_ID = 39017569036
ORIGINAL_RUN_ATTEMPT = 1
ORIGINAL_JOB_ID = 117352723457
ORIGINAL_CONTROLLER_SHA = '94578b66b77fde7e9945013b9d48666b81122e52'
ORIGINAL_RECEIPT_COMMIT = 'a37cc108096210d0fb9847609e906ca22c88acf4'
ORIGINAL_RECEIPT_SHA256 = 'e49b99c42729ca079cdc04ed75145f39b12aad2d1a587dfcd0cafce9344f7293'
ORIGINAL_RECORD_SHA256 = '156ee549a46a3d5c6501fe7e0d729d14905e47723750152db849785968e6854f'

# The later confirmed no-op refreshes provenance, not the original receipt.
# This policy/proof was independently verified before this recovery was built.
NOOP_PUBLIC_POLICY_SHA256 = 'c34865c7c37859dd9410e5c265c8a41c6c5798709e3df492cd172f9931495a0a'
_NOOP_PUBLIC_POLICY = {
    'code_sha': '54977fd93e0f4130dc24dd2083ec2e80e089dbd9',
    'contract': 'get-public-actions-provenance-policy-1',
    'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2',
    'get_policy_sha256': '6faf7c4d3a58c186f9f72fb9dd94d9061b9528c4999381eed67406692fc5c824',
    'head_branch': 'main', 'job_name': 'canonical-get',
    'publication_step_name': 'Emit verified canonical publication anchor',
    'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355,
    'rules_sha256': '58933680530fb1855fdec2094284028f9ca160b1ac91caf21ede5309f3ec1dba',
    'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml',
}
NOOP_RUN_ID = 39087117510
NOOP_RUN_ATTEMPT = 1
NOOP_JOB_ID = 117555904912
NOOP_CONTROLLER_SHA = '07650fabcd856c9dbd04c1b6158c4154de5383d9'
NOOP_RECORD_SHA256 = '94063b329d55b60524d676ddba1d426eb0c14f493baf1a9d6fa72c7bd6ecce2d'


# Exact successful canonical publication from the first scheduled-anchor repair.
SEQUENCE_TWO_RECORD = {'cas_attempts': 1, 'code_sha': '661cc247c6895f0155c9026b9cc7594dd4e60ee6', 'contract': 'get-canonical-publication-anchor-1', 'controller_sha': '397c0d6bade8b28519ddd8dad702e79f30616c65', 'main_updated': True, 'observed_main_commit': '1678207017f96eb8022b4f4fb540b08d8654fb38', 'policy_sha256': 'ff6a5639a337eccde35957d05d05a6c18f9c4168acc51557313271b7e9acb80d', 'readback_verified': True, 'receipt_commit': '1678207017f96eb8022b4f4fb540b08d8654fb38', 'receipt_sequence': 2, 'receipt_sha256': '56ec351baa67ef704d827b545c416633303d15b97fde14be2c139970ea411b91', 'record_sha256': 'd1ec657795827b0372d1b75583a8c6067a6b5fd5deec42d946a61ef894566b95', 'rules_sha256': 'ed8b4df53329adfb5b64df61737ff6eef45f2d7ca6c3a9110f9bc02e24b34288', 'run_attempt': 1, 'run_id': 39264115533, 'status': 'canonical_publication_verified'}
SEQUENCE_TWO_JOB = 118099113768
_SEQUENCE_TWO_POLICY = {'code_sha': '661cc247c6895f0155c9026b9cc7594dd4e60ee6', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': 'ff6a5639a337eccde35957d05d05a6c18f9c4168acc51557313271b7e9acb80d', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': 'ed8b4df53329adfb5b64df61737ff6eef45f2d7ca6c3a9110f9bc02e24b34288', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
SEQUENCE_TWO_PUBLIC_SHA256 = '7bcb42c2f4e33413e6954e0c326a9c846efd2b419b89a0606b109a8b19a50a63'

def sequence_two_policy():
    from get_recurring_entry import a
    if a.sha(a.encoded(_SEQUENCE_TWO_POLICY)) != SEQUENCE_TWO_PUBLIC_SHA256:
        raise ValueError('recovery_policy_changed')
    return deepcopy(_SEQUENCE_TWO_POLICY)


def old_public_policy():
    """Immutable externally approved old P, also used by the exact Notion repair."""
    from get_recurring_entry import a
    if a.sha(a.encoded(_OLD_PUBLIC_POLICY)) != OLD_PUBLIC_POLICY_SHA256:
        raise ValueError('recovery_policy_changed')
    return deepcopy(_OLD_PUBLIC_POLICY)



def noop_public_policy():
    """Exact independently approved policy of the successful run-sixteen manual no-op."""
    from get_recurring_entry import a
    if a.sha(a.encoded(_NOOP_PUBLIC_POLICY)) != NOOP_PUBLIC_POLICY_SHA256:
        raise ValueError('recovery_policy_changed')
    return deepcopy(_NOOP_PUBLIC_POLICY)


def run32_public_policy():
    """Exact completed #32 no-op; the original #26 receipt stays unchanged."""
    from get_recurring_entry import a
    if a.sha(a.encoded(run32_recovery.PUBLIC_POLICY)) != run32_recovery.PUBLIC_SHA:
        raise ValueError('recovery_policy_changed')
    return deepcopy(run32_recovery.PUBLIC_POLICY)


def current_get_policy():
    import get_recurring_entry as entry
    return entry.policy_sha256(entry.rules_sha256(), entry.BOOTSTRAP)


def accepts_receipt(commit, receipt_sha256, value, requested_policy):
    """No arbitrary old receipt, policy choice, reverse migration or relabeling."""
    if (commit == SEQUENCE_TWO_RECORD['receipt_commit']
            and receipt_sha256 == SEQUENCE_TWO_RECORD['receipt_sha256']
            and type(value.get('sequence')) is int and value['sequence'] == 2
            and value.get('policy_sha256') == sequence_two_policy()['get_policy_sha256']
            and requested_policy == current_get_policy()):
        return True
    return (commit == ORIGINAL_RECEIPT_COMMIT
            and receipt_sha256 == ORIGINAL_RECEIPT_SHA256
            and type(value.get('sequence')) is int and value['sequence'] == 1
            and value.get('policy_sha256') == old_public_policy()['get_policy_sha256']
            and requested_policy == current_get_policy())


def candidate_policy(policy, run):
    """Only the fixed reviewed predecessors can use their exact old policy proof."""
    import get_recurring_entry as entry
    old = old_public_policy()
    run_id, attempt, controller = ORIGINAL_RUN_ID, ORIGINAL_RUN_ATTEMPT, ORIGINAL_CONTROLLER_SHA
    event = 'workflow_dispatch'
    if run.get('id') == NOOP_RUN_ID:
        old = noop_public_policy()
        run_id, attempt, controller = NOOP_RUN_ID, NOOP_RUN_ATTEMPT, NOOP_CONTROLLER_SHA
        event = 'workflow_dispatch'
    if run.get('id') == SEQUENCE_TWO_RECORD['run_id']:
        old = sequence_two_policy()
        run_id, attempt, controller = (SEQUENCE_TWO_RECORD[k] for k in ('run_id','run_attempt','controller_sha'))
    if run.get('id') == run32_recovery.RUN:
        old = run32_public_policy()
        run_id, attempt, controller = (run32_recovery.GET_RECORD[k] for k in ('run_id','run_attempt','controller_sha'))
    stable = set(old) - {'code_sha', 'rules_sha256', 'get_policy_sha256'}
    if (all(policy.get(k) == old[k] for k in stable)
            and policy.get('code_sha') != old['code_sha']
            and policy.get('rules_sha256') == entry.rules_sha256()
            and policy.get('get_policy_sha256') == current_get_policy()
            and run.get('id') == run_id
            and run.get('run_attempt') == attempt
            and run.get('head_sha') == controller
            and run.get('event') == event):
        return old, True
    return policy, False


def verify_original_record(record, job_id):
    from get_publication_provenance import fail
    if record['run_id'] == run32_recovery.RUN:
        if job_id != run32_recovery.JOB or record != run32_recovery.GET_RECORD:
            fail('provenance_record')
        return
    if record['run_id'] == SEQUENCE_TWO_RECORD['run_id']:
        if job_id != SEQUENCE_TWO_JOB or record != SEQUENCE_TWO_RECORD:
            fail('provenance_record')
        return
    if record['run_id'] == NOOP_RUN_ID:
        expected_job, expected_record, updated = NOOP_JOB_ID, NOOP_RECORD_SHA256, False
        expected_attempt = NOOP_RUN_ATTEMPT
    elif record['run_id'] == ORIGINAL_RUN_ID:
        expected_job, expected_record, updated = ORIGINAL_JOB_ID, ORIGINAL_RECORD_SHA256, True
        expected_attempt = ORIGINAL_RUN_ATTEMPT
    else:
        fail('provenance_record')
    if (job_id != expected_job or record['record_sha256'] != expected_record
            or record['run_attempt'] != expected_attempt
            or record['receipt_commit'] != ORIGINAL_RECEIPT_COMMIT
            or record['receipt_sha256'] != ORIGINAL_RECEIPT_SHA256
            or record['receipt_sequence'] != 1 or record['main_updated'] is not updated):
        fail('provenance_record')
