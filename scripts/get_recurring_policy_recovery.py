"""One explicitly reviewed predecessor for the first production code repair.

These are external publication evidence, not values learned from a receipt or
repository state. This exception admits only the original sequence-one receipt
unchanged; all normal parent, complete-delta and ownership checks still run.
New receipts and public records always carry the executing runtime's policy.
"""
from copy import deepcopy

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


def old_public_policy():
    """Immutable externally approved old P, also used by the exact Notion repair."""
    from get_recurring_entry import a
    if a.sha(a.encoded(_OLD_PUBLIC_POLICY)) != OLD_PUBLIC_POLICY_SHA256:
        raise ValueError('recovery_policy_changed')
    return deepcopy(_OLD_PUBLIC_POLICY)


def current_get_policy():
    import get_recurring_entry as entry
    return entry.policy_sha256(entry.rules_sha256(), entry.BOOTSTRAP)


def accepts_receipt(commit, receipt_sha256, value, requested_policy):
    """No arbitrary old receipt, policy choice, reverse migration or relabeling."""
    return (commit == ORIGINAL_RECEIPT_COMMIT
            and receipt_sha256 == ORIGINAL_RECEIPT_SHA256
            and type(value.get('sequence')) is int and value['sequence'] == 1
            and value.get('policy_sha256') == old_public_policy()['get_policy_sha256']
            and requested_policy == current_get_policy())


def candidate_policy(policy, run):
    """Only the original successful attempt can use old controller/log proof."""
    import get_recurring_entry as entry
    old = old_public_policy()
    stable = set(old) - {'code_sha', 'rules_sha256', 'get_policy_sha256'}
    if (all(policy.get(k) == old[k] for k in stable)
            and policy.get('code_sha') != old['code_sha']
            and policy.get('rules_sha256') == entry.rules_sha256()
            and policy.get('get_policy_sha256') == current_get_policy()
            and run.get('id') == ORIGINAL_RUN_ID
            and run.get('run_attempt') == ORIGINAL_RUN_ATTEMPT
            and run.get('head_sha') == ORIGINAL_CONTROLLER_SHA
            and run.get('event') == 'workflow_dispatch'):
        return old, True
    return policy, False


def verify_original_record(record, job_id):
    from get_publication_provenance import fail
    if (job_id != ORIGINAL_JOB_ID or record['record_sha256'] != ORIGINAL_RECORD_SHA256
            or record['receipt_commit'] != ORIGINAL_RECEIPT_COMMIT
            or record['receipt_sha256'] != ORIGINAL_RECEIPT_SHA256
            or record['receipt_sequence'] != 1 or record['main_updated'] is not True):
        fail('provenance_record')
