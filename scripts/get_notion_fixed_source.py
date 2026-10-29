"""Registered archive provenance, separate from this run's Get publication.

No new business state, input registrations, transport, credentials or budget.
This marker proves which already-reviewed archive scope the current controller
verified, not who originally created the archive or a new Get publication.
"""
import get_notion_executor as e
import get_notion_historical as h
import get_notion_historical_input as inputs

MARKER = 'GET_NOTION_FIXED_SOURCE_V1 '
CONTRACT = 'get-notion-fixed-source-1'
MODES = ('historical-capture', 'historical-apply')


def admit(fixed, publication_bytes, *, apply, historical_manifest=None,
          historical_capture=None, manifest_sha=None, capture_sha=None,
          property_manifest=None, recovery=None):
    if type(fixed) is not bool: e.fail('notion_scope')
    if not fixed: return None
    if (not apply or publication_bytes is not None or property_manifest is not None
            or recovery is not None or (historical_manifest is None) == (historical_capture is None)):
        e.fail('notion_scope')
    mode = 'historical-apply' if historical_manifest is not None else 'historical-capture'
    value = historical_manifest if historical_manifest is not None else historical_capture
    expected = manifest_sha if historical_manifest is not None else capture_sha
    from get_notion_controller_route import selection
    selected = selection()
    if selected[:2] != (mode, expected): e.fail('approval_missing')
    validate = inputs.validate_apply_selection if mode == 'historical-apply' else inputs.validate_capture_selection
    members = validate(value, expected, apply=True)
    for _, digest in members: inputs.anchor_for(digest)
    return mode, expected, members


def check_state(current, found):
    """Fixed archive authority is separate from exact active-plan recovery."""
    from get_notion_idle_recovery import TAG
    from get_notion_blocked_idle_recovery import TAG as BLOCKED_TAG
    import get_notion_historical_active_recovery as active_recovery
    import get_notion_authenticated_recovery as authenticated_recovery
    import get_notion_create_resume as create_resume
    import get_notion_clean_active_resume as clean_active
    import get_notion_checkpoint_resume as checkpoint_resume
    import get_recurring_policy_recovery as policy_recovery
    tag=found.get('recovery_anchor')
    active=current['engine_state']['journal']
    b=active_recovery.plan_binding(active['plan']) if active else None
    exact=bool(b and b in (active_recovery.prepared_binding(),active_recovery.inflight_binding())
        and active and active_recovery.plan_binding(active['plan'])==b
        and active_recovery.allows(active['plan'],active))
    exact_new = bool(active and authenticated_recovery.allows(active['plan'], active))
    exact_create = bool(active and create_resume.allows(active['plan'],active))
    if tag == checkpoint_resume.TAG:
        if not active or not checkpoint_resume.initial_or_transitioned_state_valid(current,e.digest(current)):e.fail('journal_busy')
    elif tag == clean_active.TAG:
        if not active or not clean_active.initial_or_transitioned_state_valid(current,e.digest(current)):e.fail('journal_busy')
    elif tag == create_resume.TAG:
        if not exact_create: e.fail('journal_busy')
    elif tag == authenticated_recovery.TAG:
        if not exact_new or exact_create: e.fail('journal_busy')
    elif tag not in (None,TAG,BLOCKED_TAG):
        if tag not in (active_recovery.PREPARED_TAG,active_recovery.INFLIGHT_TAG) or not exact: e.fail('journal_busy')
    if active is not None and active['plan']['rules_sha256']!=e.rules_sha256():
        if not (exact or exact_new) or (tag is None and current['policy_sha256']!=policy_recovery.current_get_policy()):e.fail('journal_busy')


def verify_sources(local, remote, token, root, parent, selection, policy_hash, *, refresh_session=None):
    """Before first CAS: reviewed digest, original blobs, ancestry and fresh tree.

    Loader performs Git-only reads. Each member retains the original 10-note
    and media-free limits; group validation retains its three-scope ceiling.
    Existing dispatch repeats native/source/write guards for every member.
    """
    import get_confirmed_source as source
    mode, _, members = selection
    loader = source.Loader(local, remote, token, root, policy_hash) if refresh_session is None else refresh_session.loader
    if refresh_session is not None:
        from get_notion_historical_refresh import Session
        if type(refresh_session) is not Session or refresh_session.selection != selection:e.fail('approval_missing')
    for value, expected in members:
        inputs.anchor_for(expected)
        capability = None
        if refresh_session is not None:
            kind = 'capture' if mode == 'historical-capture' else 'apply'
            capability = refresh_session.capability(value, expected, kind)
            value = capability.candidate if kind == 'capture' else capability.manifest
            expected = e.digest(value)
        probe = h.validate_capture(value, expected, apply=True, refresh_capability=capability) if mode == 'historical-capture' else h.validate(value, expected, apply=True, refresh_capability=capability)
        loader.ancestry(value['source_commit'], parent)
        for row in value['records'].values(): loader.blob(value['source_commit'], row['path'], e.p.MAX_NOTE)
        publication = h.Publication(local.vault, probe, e.digest(probe))
        publication.check(publication.descriptor)
    return loader


def record(context, mode, expected):
    import get_notion_journal_runtime as checkpoints
    from get_notion_controller_route import preflight
    checkpoints.context(context)
    if mode not in MODES: e.fail('approval_missing')
    preflight('workflow_dispatch', True, mode, expected)
    unsigned = {**context, 'contract': CONTRACT, 'mode': mode, 'selection_sha256': expected}
    return {**unsigned, 'record_sha256': e.digest(unsigned)}


def emit(stream, context, selection):
    mode, expected, _ = selection
    line = MARKER + e.encoded(record(context, mode, expected)).decode() + '\n'
    if stream.write(line) != len(line): e.fail('journal_checkpoint_failed')
    stream.flush()


def parse(raw, context, run):
    import get_notion_remote_journal as journal
    if run.get('event') != 'workflow_dispatch' or not isinstance(raw, bytes) or len(raw) > 8192:
        e.fail('notion_scope')
    value = journal.retention.strict_json(raw)
    if not isinstance(value, dict) or value != record(context, value.get('mode'), value.get('selection_sha256')):
        e.fail('notion_scope')
    return value


def reserve_publication_slot(client, policy, execution):
    """Read-only admission: leave one original discovery slot for this run.

    This is not a publication or state authority; discard the returned anchor.
    Reuse the same client, its remaining physical budgets, and all original
    attempt/controller/receipt checks. No Get API request is made.
    """
    import get_publication_provenance as p
    try:
        p.discover(client, policy, exclude_run_id=execution['run_id'],
            exclude_run_attempt=execution['run_attempt'], reserve_next_run=True)
    except p.ProvenanceFailure as exc:
        if str(exc) == 'provenance_no_verified_publication':
            p.fail('fixed_source_capacity')
        raise
