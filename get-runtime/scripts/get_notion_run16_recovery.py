"""Exact run16 predecessor for the single proven-expired PNG recovery.

This module authenticates the existing durable anchor only. It cannot issue
network writes, reset an upload, amend an old plan or authorize replacement.
The separate replacement contract requires fresh expired and missing-page
observations plus its exact prior state at the atomic transition boundary.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy

TAG = 'exact_run16_expired_png_predecessor_v1'
RUN = _private_bindings.value('get_notion_run16_recovery', 'RUN')
ATTEMPT = _private_bindings.value('get_notion_run16_recovery', 'ATTEMPT')
JOB = _private_bindings.value('get_notion_run16_recovery', 'JOB')
PUBLIC_POLICY = _private_bindings.value('get_notion_run16_recovery', 'PUBLIC_POLICY')
PUBLIC_SHA = _private_bindings.value('get_notion_run16_recovery', 'PUBLIC_SHA')
NOTION_POLICY = _private_bindings.value('get_notion_run16_recovery', 'NOTION_POLICY')
CHECKPOINT = _private_bindings.value('get_notion_run16_recovery', 'CHECKPOINT')
GET_RECORD = _private_bindings.value('get_notion_run16_recovery', 'GET_RECORD')
ANCHOR = _private_bindings.value('get_notion_run16_recovery', 'ANCHOR')
STATE_BYTES = _private_bindings.value('get_notion_run16_recovery', 'STATE_BYTES')


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
