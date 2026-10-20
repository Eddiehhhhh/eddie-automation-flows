"""Exact #53/attempt 2 verified-prefix bridge; no new plan or remote effects.

All pins are independently observed public hashes or hash-only private state
bindings. The private journal and page/content IDs are deliberately not included.
"""
from copy import deepcopy

# A separate independent review must activate this one-way deployment bridge.
ACTIVATED = True
TAG = 'exact_run53_verified_batch_resume_v1'
RUN = 39378084527
ATTEMPT = 2
JOB = 118493764976
JOB_SHA256 = '3095159913705f480221d54ef3c1fe2830bc85801f97838004978c0c4e5454c8'
PUBLIC_POLICY = {'code_sha': '6ad627793c23e80ad2ca9568048eb4e595c881a0', 'contract': 'get-public-actions-provenance-policy-1', 'controller_template_sha256': '66207da3cafd49778570d32ca61c23bfa13d86a4cebd49fabccaebe952f855d2', 'get_policy_sha256': 'd0aa4217dd51e45eeb35534e166eabed54d00c2f3a0f4bead237a92e00f04978', 'head_branch': 'main', 'job_name': 'canonical-get', 'publication_step_name': 'Emit verified canonical publication anchor', 'repository': 'Eddiehhhhh/eddie-automation-flows', 'repository_id': 1270807355, 'rules_sha256': 'bd7e0653a0f78620eb78e3ef6a4216fc00aac3e82f4dee758e4923b5d84c1977', 'workflow_id': 387242896, 'workflow_path': '.github/workflows/get-canonical.yml'}
PUBLIC_SHA = '8bb310898e2298fbec6952e19d8f6446635b92a979a8358172a92b416aca2984'
NOTION_POLICY = '20e4c0491a361a6cb059d6382dea6cd2b63fafc9a551b75bc445cada08073b22'
CHECKPOINT = {'cas_attempts': 0, 'code_sha': '6ad627793c23e80ad2ca9568048eb4e595c881a0', 'commit': 'a3433308257901ced493ea2e21fffa2dad6bdcb1', 'contract': 'get-notion-journal-checkpoint-1', 'controller_sha': 'ad4ae476594aed495bf82ae4776059c15cdf3b28', 'notion_policy_sha256': '20e4c0491a361a6cb059d6382dea6cd2b63fafc9a551b75bc445cada08073b22', 'parent_commit': None, 'previous_record_sha256': '08555bfb24f3f8f7fc65068ccfadd9c51711cddf3c9de45552c78934ec0b9a20', 'public_policy_sha256': '8bb310898e2298fbec6952e19d8f6446635b92a979a8358172a92b416aca2984', 'readback_verified': True, 'record_sha256': 'd0e7cb436508041e4bf268ac5cee8fac34b2c6b96c62c3ec1e1769f1fcd06798', 'rules_sha256': 'bd7e0653a0f78620eb78e3ef6a4216fc00aac3e82f4dee758e4923b5d84c1977', 'run_attempt': 2, 'run_id': 39378084527, 'sequence': 9, 'state_blob': '011b7b511785c999375895880eef98a7de88596c', 'state_bytes': 1989077, 'state_sha256': '7beba811fdd52d8f4e651eb8a5244e95f7147c0225700a9735ca14912a38b4b3', 'status': 'journal_readback_verified', 'tree': '1f2e3b065bb26e90486f712e3852fdea3eeda351'}
GET_RECORD = {'cas_attempts': 0, 'code_sha': '6ad627793c23e80ad2ca9568048eb4e595c881a0', 'contract': 'get-canonical-publication-anchor-1', 'controller_sha': 'ad4ae476594aed495bf82ae4776059c15cdf3b28', 'main_updated': False, 'observed_main_commit': 'b0527f3acf9d1bed7962b9052bbbb340c9ec0031', 'policy_sha256': 'd0aa4217dd51e45eeb35534e166eabed54d00c2f3a0f4bead237a92e00f04978', 'readback_verified': True, 'receipt_commit': '1678207017f96eb8022b4f4fb540b08d8654fb38', 'receipt_sequence': 2, 'receipt_sha256': '56ec351baa67ef704d827b545c416633303d15b97fde14be2c139970ea411b91', 'record_sha256': '1010963f5e8040e292e184a7e565597560d9c538b999fb7d2fe57507580a8d45', 'rules_sha256': 'bd7e0653a0f78620eb78e3ef6a4216fc00aac3e82f4dee758e4923b5d84c1977', 'run_attempt': 2, 'run_id': 39378084527, 'status': 'canonical_publication_verified'}
ANCHOR = {'commit': 'a3433308257901ced493ea2e21fffa2dad6bdcb1', 'state_blob': '011b7b511785c999375895880eef98a7de88596c', 'state_sha256': '7beba811fdd52d8f4e651eb8a5244e95f7147c0225700a9735ca14912a38b4b3'}
STATE_BYTES = 1989077
PLAN_SHA = '88374b0b1a612d3b99643644e4ccfbc6a1bf55a8d8f90cd7c792a95448a6740b'
PLAN_BYTES = 1221505
OP_SHA = 'a41c928106e5caff17350993db12585de0f9e51bf7294750b6e91e6e41be09fe'
ENGINE_RULES = '0a01d0a9895b21c2be31066daa16890c1ad12bef9fbb60308c54df9d0ed6dd85'
VERIFIED_BATCHES = 20
TOTAL_BATCHES = 23
PREFIX_SHA = 'b38a6c48f767d7bd12a609c4bee7a06cd9ae3c47c94073bceed7f57e8c0f99da'
PAGE_SHA = '92b856d9319b79bae8851a3d0facebbd98853a3afafa0a7c9df92d282432d679'
PROPERTIES_SHA = 'c196dafc936032fffa5a9c9aa27767574dffea79b6b3d71e3005cd407daf5d5c'
ATTEMPT_CLOCK = {'id': 39378084527, 'run_attempt': 2, 'workflow_id': 387242896, 'path': '.github/workflows/get-canonical.yml', 'head_sha': 'ad4ae476594aed495bf82ae4776059c15cdf3b28', 'head_branch': 'main', 'event': 'schedule', 'status': 'completed', 'conclusion': 'success', 'created_at': '2026-10-20T06:45:28Z', 'updated_at': '2026-10-20T07:06:13Z', 'run_started_at': '2026-10-20T06:45:27Z'}
ALIAS_CLOCK = {'id': 39378084527, 'run_attempt': 2, 'workflow_id': 387242896, 'path': '.github/workflows/get-canonical.yml', 'head_sha': 'ad4ae476594aed495bf82ae4776059c15cdf3b28', 'head_branch': 'main', 'event': 'schedule', 'status': 'completed', 'conclusion': 'success', 'created_at': '2026-10-20T06:02:04Z', 'updated_at': '2026-10-20T07:06:13Z', 'run_started_at': '2026-10-20T06:45:27Z'}


def clock_witness(attempt, alias):
    """One exact directional endpoint discrepancy, never a wider clock limit.

    Require every checked raw field, including run_started_at. Missing values
    in older reduced bindings are not inferred from the other endpoint.
    """
    if not ACTIVATED or not isinstance(attempt, dict) or not isinstance(alias, dict):
        return None
    if any(k not in attempt or k not in alias for k in ATTEMPT_CLOCK):
        return None
    before = {k: attempt[k] for k in ATTEMPT_CLOCK}
    after = {k: alias[k] for k in ALIAS_CLOCK}
    if before != ATTEMPT_CLOCK or after != ALIAS_CLOCK:
        return None
    return {'attempt': before, 'alias': after}


def allows(plan, journal):
    """Only this unchanged plan and authenticated forward-prefix checkpoints.

    Callers must first authenticate the journal externally. This is never a
    preparation exception or authority to adopt unknown remote effects.
    """
    import get_notion_executor as e
    try:
        if (not ACTIVATED or not isinstance(plan, dict) or not isinstance(journal, dict)
                or plan.get('rules_sha256') != ENGINE_RULES
                or e.digest(plan) != PLAN_SHA or len(e.encoded(plan)) != PLAN_BYTES
                or journal.get('plan_sha256') != PLAN_SHA
                or e.encoded(journal.get('plan')) != e.encoded(plan)
                or len(plan['records']) != 1
                or plan['records'][0].get('action') != 'create'
                or len(plan['records'][0]['create_batches']['batches']) != TOTAL_BATCHES
                or type(journal.get('index')) is not int):
            return False
        if journal['index'] == 1:
            return journal.get('operation') is None
        if journal['index'] != 0:
            return False
        operation = journal['operation']
        e.create_batches.validate_operation(operation, plan['records'][0])
        return (operation['batch_index'] >= VERIFIED_BATCHES - 1
                and e.digest(operation['page_id']) == PAGE_SHA
                and operation['properties_sha256'] == PROPERTIES_SHA
                and len(operation['batches']) >= VERIFIED_BATCHES
                and all(b['phase'] == 'verified' for b in operation['batches'][:VERIFIED_BATCHES])
                and e.digest(operation['batches'][:VERIFIED_BATCHES]) == PREFIX_SHA)
    except Exception:
        return False


def recover_checkpoint(client, run_id, run_attempt):
    import get_notion_journal_runtime as c
    if (not ACTIVATED or type(run_id) is not int or type(run_attempt) is not int
            or (run_id, run_attempt) != (RUN, ATTEMPT)):
        c.fail('journal_provenance_other_controller')
    result = c.recover_anchor(client, run_id=RUN, run_attempt=ATTEMPT,
        public_policy=deepcopy(PUBLIC_POLICY), approved_public_policy_sha256=PUBLIC_SHA,
        notion_policy_sha256=NOTION_POLICY, require_success=True)
    if (result.get('anchor') != ANCHOR or result.get('job_id') != JOB
            or result.get('job_sha256') != JOB_SHA256
            or result.get('checkpoint_count') != 9
            or result.get('checkpoint') != CHECKPOINT
            or result.get('get_step_record') != GET_RECORD
            or result.get('private_readback_required') is not True
            or result.get('clock_witness') != {'attempt': ATTEMPT_CLOCK, 'alias': ALIAS_CLOCK}):
        c.fail('journal_provenance_record')
    return {**result, 'recovery_anchor': TAG}


def validate_state(state, sha):
    import get_notion_outbox as o
    import get_private_preview_retention as retention
    try:
        o.check_state(state)
        raw = o.encoded(state)
        journal = state['engine_state']['journal']
        operation = journal['operation']
        return (sha == ANCHOR['state_sha256'] and o.digest(state) == sha
                and len(raw) == STATE_BYTES and retention.oid(raw) == ANCHOR['state_blob']
                and state['policy_sha256'] == PUBLIC_POLICY['get_policy_sha256']
                and allows(journal['plan'], journal) and journal['index'] == 0
                and operation['phase'] == 'verified'
                and operation['batch_index'] == VERIFIED_BATCHES - 1
                and len(operation['batches']) == VERIFIED_BATCHES
                and o.digest(operation) == OP_SHA)
    except Exception:
        return False


def transition_outer(state, sha, new_get_policy_sha256):
    """Change ONLY the outer policy, before ordinary confirmed CAS/checkpoint."""
    import get_notion_outbox as o
    import get_recurring_policy_recovery as recovery
    if not validate_state(state, sha) or new_get_policy_sha256 != recovery.current_get_policy():
        o.fail('outbox_state_invalid')
    if new_get_policy_sha256 == state['policy_sha256']:
        o.fail('outbox_state_invalid')
    result = deepcopy(state)
    result['policy_sha256'] = new_get_policy_sha256
    o.check_state(result)
    return result
