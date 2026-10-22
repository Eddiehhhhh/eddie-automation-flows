"""Historical entry through the existing guarded runtime and durable journal."""
from copy import deepcopy
from collections import Counter
import get_notion_executor as e
import get_notion_historical as historical
import get_notion_outbox as outbox
import get_notion_live_transport as transport
import get_notion_pipeline as pipeline
import get_confirmed_source as source_loader
import get_notion_historical_input as private_input
import get_notion_properties_runtime as properties
import get_notion_remote_journal as journal
import get_notion_journal_runtime as checkpoints
import get_notion_historical_active_recovery as historical_active


class API(transport.API):
    def read_note(self,ident):
        snapshot=super().read_note(ident)
        if len(snapshot['exact_id_matches'])!=1: return snapshot
        page=snapshot['page_id']
        before=self.request('GET','/pages/'+page,None)
        # First cohort excludes presentation/media and locked pages.
        if before.get('cover') is not None or before.get('icon') is not None or before.get('is_locked',False): e.fail('target_invalid')
        if before.get('last_edited_time')!=snapshot['last_edited_time']: e.fail('target_changed')
        for block in snapshot['recursive_blocks']:
            if block['type'] not in ('paragraph','divider','heading_1','heading_2','heading_3','bulleted_list_item','numbered_list_item','quote','to_do','code') or block['children_ids']: e.fail('missing_asset')
        # Notion's comments endpoint omits resolved discussions. No legacy
        # block is archived or rewritten, so every discussion stays attached.
        after=self.request('GET','/pages/'+page,None)
        if transport.native.page_difference_categories(before,after): e.fail('target_changed')
        snapshot['historical_protection']={'cover':None,'icon':None,'comments':'not_read_all_legacy_blocks_preserved','locked':False}
        return snapshot


def dispatch(local,remote,token,root,store,request,*,manifest,expected,policy_hash,apply,writers_guard,loader=None,admit=None,verify_reused=False):
    historical.validate(manifest,expected,apply=apply)
    if transport.native.sha(manifest['data_source_id'])!=transport.native.DATA_SOURCE_PIN or transport.native.sha(manifest['database_id'])!=transport.native.DATABASE_PIN: e.fail('wrong_parent')
    current,version=store.read();outbox.check_state(current)
    active=current['engine_state']['journal']
    if active is not None and (not active['plan'].get('historical_only') or active['plan'].get('historical_manifest_sha256')!=expected): e.fail('journal_busy')
    new_ids=historical.creation_ids(manifest)
    property_expected=private_input.creation_property_for(expected,required=bool(new_ids))
    property_manifest=None;property_schema=None
    if property_expected is not None:
        if not new_ids:e.fail('approval_missing')
        historical.require_creation_capture(current.get('historical_captures',{}),manifest,expected)
        properties.check_dependency()
        raw=private_input.read(remote,property_expected,contract=properties.tx.CONTRACT)
        property_manifest=private_input.strict_capture_json(raw,256*1024)
        historical.bind_creation_properties(manifest,property_manifest,property_expected)
    if loader is None: loader=source_loader.Loader(local,remote,token,root,policy_hash)
    loader.commit(manifest['source_commit'])
    for row in manifest['records'].values(): loader.blob(manifest['source_commit'],row['path'],e.p.MAX_NOTE)
    if active is not None and historical_active.allows(active['plan'], active):
        head = source_loader.entry.a.checked(local.run('rev-parse', source_loader.entry.r.MAIN_REF).decode().strip(), 40)
        loader.ancestry(manifest['source_commit'], head)
    publication=historical.Publication(local.vault,manifest,expected)
    api=API(request,manifest['data_source_id'],sorted(manifest['records']))
    from get_notion_runtime import schema
    if property_manifest is not None:
        property_schema=api.schema()
        titles=[name for name,row in property_schema.items() if isinstance(row,dict) and row.get('type')=='title']
        if len(titles)!=1 or not isinstance(titles[0],str) or not titles[0] or len(titles[0])>100:e.fail('property_schema_changed')
        title_property=titles[0]
    else:title_property=schema(request,manifest['data_source_id'])
    policy={'data_source_id':manifest['data_source_id'],'id_property':'笔记 ID',
        'title_property':title_property,'allow_create':True,
        'allow_archive':True,'format_accepted':True,'old_writer_evidence':writers_guard()}
    if not e.p.writer_gate(policy): e.fail('writer_gate_unknown')
    adapter=outbox.EngineStore(store);state,sha=adapter.read()
    snapshots={} if active else {ident:api.read_note(ident) for ident in sorted(manifest['records'])}
    if verify_reused and not active:
        for ident,row in manifest['records'].items():
            prior=state['receipts'].get(ident)
            if prior is not None:
                fresh=e.receipt(snapshots[ident],ident,manifest['data_source_id'],
                    [block['id'] for block in prior['managed_blocks']],
                    publication.descriptor,row,prior['asset_bindings'])
                if fresh!=prior:e.fail('human_edit_conflict')
    if active:
        plan=deepcopy(active['plan'])
        if ((plan['rules_sha256']!=e.rules_sha256() and not historical_active.allows(plan, active)) or plan['publication']!=publication.descriptor
                or plan['policy']!=policy or plan.get('historical_manifest')!=manifest
                or plan.get('property_manifest_sha256')!=property_expected
                or plan.get('property_manifest')!=property_manifest):e.fail('journal_busy')
        if property_manifest is not None:
            properties.tx.context(publication,property_schema,property_manifest,property_expected,policy)
            historical.check_creation_titles(plan,publication,property_manifest,property_expected)
    else:
        options={} if property_manifest is None else {'property_manifest':property_manifest,
            'expected_property_manifest_sha256':property_expected,'property_schema':property_schema}
        plan=historical.prepare(publication,state,sha,snapshots,policy,manifest,expected,**options)
    actions=Counter(row['action'] for row in plan['records'])
    reasons=Counter(row['reason'] for row in plan['records'] if row['action']=='blocked')
    report={'contract':pipeline.CONTRACT,'status':'prepared','source_mode':'historical_cohort',
        'plan_sha256':e.digest(plan),'journal_sha256':sha,'selected':len(plan['records']),
        'deferred':len(plan['deferred_ids']),'retained_conflicts':len(plan['skipped_conflicts']),
        'actions':dict(actions),'reason_counts':dict(reasons),'native_notes_read':len(snapshots),
        'get_requests':0,'asset_downloads':0,'external_write_attempts':None if active else 0,
        'write_state':'unknown' if active else 'not_attempted','prior_journal_effects':'unresolved' if active else 'none',
        'complete_library_claimed':False}
    if apply:
        if admit is not None and not admit(current,plan): raise ApplyBoundaryYield()
        if not isinstance(request,transport.Transport): e.fail('approval_missing')
        allowed=pipeline.wire_allowlist(plan,api);api.approved=allowed;request.approved=allowed
        result=e.execute(plan,e.digest(plan),publication,api,adapter,expected_store_sha=sha,writers_guard=writers_guard,
                         authorize_batch=pipeline.batch_authorizer(plan,api,request,adapter))
        safe=e.public_report(result)
        report.update(status='executed',execution=safe,external_write_attempts=safe['external_write_attempts'],write_state=safe['write_state'])
    final,final_sha=store.read()
    # Historical receipts do not mutate the canonical event queue or its anchors.
    for key in ('pending_by_id','rotation_order','last_enqueued_publication','observed_publication_tip'):
        if final[key]!=current[key]: e.fail('state_changed')
    result={**pipeline.public_report(report),'initial_notes':0,'initial_range_candidates':0,
            'queued':len(final['pending_by_id']),'outer_journal_sha256':final_sha,
            'source_chain_complete':final['last_enqueued_publication']==final['observed_publication_tip']}
    if apply:
        # Only existing sanitized fixed enums; never publish exception text.
        result.update(execution_status=safe['status'], execution_reason=safe['reason'])
    if active is not None and historical_active.allows(active['plan'], active):
        result.update(resumed_prior_plan_sha256=active['plan_sha256'],
            resumed_prior_rules_sha256=active['plan']['rules_sha256'])
    if verify_reused:
        result['scope_confirmed']=(final['engine_state']['journal'] is None
            and body_receipts_match(final,manifest))
    return result


def capture(local,remote,token,root,store,request,*,candidate,expected,policy_hash,apply,writers_guard,loader=None):
    """Read Notion only; retain exact native evidence in the ONE private journal."""
    probe=historical.validate_capture(candidate,expected,apply=apply)
    current,version=store.read();outbox.check_state(current)
    if current['engine_state']['journal'] is not None: e.fail('journal_busy')
    if not e.p.writer_gate({'old_writer_evidence':writers_guard()}): e.fail('writer_gate_unknown')
    saved=current.get('historical_captures',{}).get(expected)
    reused=saved is not None
    if saved is None:
        if loader is None: loader=source_loader.Loader(local,remote,token,root,policy_hash)
        loader.commit(candidate['source_commit'])
        for row in candidate['records'].values():loader.blob(candidate['source_commit'],row['path'],e.p.MAX_NOTE)
        publication=historical.Publication(local.vault,probe,e.digest(probe))
        api=API(request,candidate['data_source_id'],sorted(candidate['records']))
        snapshots={ident:api.read_note(ident) for ident in sorted(candidate['records'])}
        manifest=deepcopy(candidate);manifest['contract']=historical.CONTRACT
        for ident,row in manifest['records'].items():
            if row['mode']=='existing_placeholder':
                pin=historical.target_pin(snapshots[ident],ident,manifest['data_source_id'])
                if pin['page_id']!=row['target']['page_id']:e.fail('target_changed')
                row['target']=pin
            elif e.target(snapshots[ident],ident,manifest['data_source_id']) is not None:e.fail('duplicate_id')
        saved={'candidate':deepcopy(candidate),'manifest':manifest,'snapshots':snapshots}
        historical.check_captures({expected:saved})
        publication.check(publication.descriptor)
        if not e.p.writer_gate({'old_writer_evidence':writers_guard()}):e.fail('writer_gate_unknown')
        if apply:
            next_state=deepcopy(current);next_state.setdefault('historical_captures',{})[expected]=saved
            outbox.confirmed_compare_and_set(store,version,next_state)
    _,final_sha=store.read()
    return {'contract':'get-notion-historical-capture-result-1','status':'captured' if apply else 'prepared',
        'candidate_sha256':expected,'manifest_sha256':e.digest(saved['manifest']),
        'snapshot_sha256':e.digest(saved['snapshots']),'outer_journal_sha256':final_sha,
        'pages':len(saved['snapshots']),'reused_evidence':reused,
        'notion_write_attempts':0,'complete_library_claimed':False}


GROUP_RESULT_CONTRACT = 'get-notion-historical-capture-group-result-1'
GROUP_REASONS = e.REASONS | {
    'response_budget', 'read_api_failed', 'read_transport_failed', 'permission_denied',
    'malformed_response', 'journal_checkpoint_failed', 'journal_checkpoint_locked',
    'notion_source_changed', 'notion_state_anchor', 'capture_group_failed'}


def capture_metrics(request, durable, emitter):
    """Public counts only, always taken from the original cumulative objects."""
    remaining = max(0, min(1200000, int((request.deadline - request.clock()) * 1000)))
    return {'notion_requests': request.attempts, 'notion_response_bytes': request.bytes,
            'remaining_milliseconds': remaining, 'journal_commits': durable.commits_attempted,
            'new_journal_bytes': durable.new_blob_bytes,
            'checkpoint_sequence': emitter.sequence}


def capture_boundary(request, durable, emitter, *, notes, reused=False, refresh_pending=False):
    """Admission floor, not a promise that unknown pagination/latency will fit.

    Shared transports remain authoritative within a scope. New evidence needs
    one <=32-MiB state CAS and its checkpoint, plus final no-op refresh capacity.
    A cached prefix must be checkpointed before starting another fresh scope.
    """
    if durable.poisoned or emitter.failed: e.fail('journal_failed')
    checkpoints_needed = 1 if reused else 2 + int(refresh_pending)
    if emitter.sequence + checkpoints_needed > checkpoints.MAX_CHECKPOINTS:
        return False
    if reused: return True
    if (durable.commits_attempted >= durable.max_commits
            or durable.new_blob_bytes + journal.MAX_STATE > durable.max_batch_bytes):
        return False
    # Nine is the current flat-existing-page minimum, not a pagination estimate.
    floor = 9 * notes
    if (request.attempts + floor > 2000
            or request.bytes + transport.ro.MAX_RESPONSE > transport.ro.MAX_HTTP_BYTES
            or request.clock() + floor * .4 >= request.deadline):
        return False
    return True


def capture_group(local, remote, token, root, store, request, *, selection, expected,
                  policy_hash, apply, writers_guard, durable, emitter, refresh, loader=None):
    """One bounded capture session. No runtime bootstrap, new counters or writes."""
    members = private_input.validate_capture_selection(selection, expected, apply=apply)
    if expected not in historical.APPROVED_CAPTURE_GROUPS or not apply:
        e.fail('approval_missing')
    if (not isinstance(request, transport.Transport)
            or not isinstance(durable, journal.RemoteJournal)
            or store is not durable
            or not isinstance(emitter, checkpoints.CheckpointEmitter)):
        e.fail('approval_missing')
    completed = []; pending_refresh = False; status = 'captured'; reason = None
    last_sha = None
    try:
        for candidate, member in members:
            current, last_sha = store.read(); outbox.check_state(current)
            if current['engine_state']['journal'] is not None: e.fail('journal_busy')
            saved = current.get('historical_captures', {}).get(member)
            reused = saved is not None
            if not capture_boundary(request, durable, emitter,
                    notes=len(candidate['records']), reused=reused, refresh_pending=pending_refresh):
                status = 'partial'; reason = 'batch_boundary_yield'
                break
            if not reused and pending_refresh:
                refresh()
                pending_refresh = False
            prior_sequence = emitter.sequence
            result = capture(local, remote, token, root, store, request,
                candidate=candidate, expected=member, policy_hash=policy_hash,
                apply=True, writers_guard=writers_guard, loader=loader)
            if (durable.poisoned or emitter.failed
                    or durable.anchor['state_sha256'] != result['outer_journal_sha256']
                    or emitter.sequence != prior_sequence + int(not reused)):
                e.fail('journal_failed')
            # capture returned only after its own exact persisted readback.
            # Preserve that confirmed prefix even if the next freshness gate
            # detects drift and stops the remainder of this group.
            completed.append({key: result[key] for key in (
                'candidate_sha256', 'manifest_sha256', 'snapshot_sha256',
                'pages', 'reused_evidence')})
            last_sha = result['outer_journal_sha256']
            # No member can advance the group after uncertain persistence,
            # foreign journal activity, source drift, or a failed writer guard.
            if not e.p.writer_gate({'old_writer_evidence': writers_guard()}):
                e.fail('writer_gate_unknown')
            current, last_sha = store.read(); outbox.check_state(current)
            observed = current.get('historical_captures', {}).get(member)
            if (durable.poisoned or emitter.failed
                    or current['engine_state']['journal'] is not None
                    or observed is None or observed['candidate'] != candidate
                    or result['outer_journal_sha256'] != last_sha
                    or e.digest(observed['manifest']) != result['manifest_sha256']
                    or e.digest(observed['snapshots']) != result['snapshot_sha256']):
                e.fail('journal_failed')
            pending_refresh = reused
        # Shared same-context no-op refresh, including an all-cached repeat.
        # A boundary yield has not poisoned anything; an exception never resumes.
        if emitter.sequence < checkpoints.MAX_CHECKPOINTS:
            refresh()
        else:
            status = 'partial'; reason = 'batch_boundary_yield'
    except Exception as exc:
        status = 'failed'
        reason = str(exc) if str(exc) in GROUP_REASONS else 'capture_group_failed'
    metrics = capture_metrics(request, durable, emitter)
    return {'contract': GROUP_RESULT_CONTRACT, 'status': status,
            'selection_sha256': expected, 'scopes_selected': len(members),
            'scopes_completed': len(completed), 'scopes_remaining': len(members) - len(completed),
            'pages': sum(row['pages'] for row in completed),
            'scopes_reused': sum(row['reused_evidence'] for row in completed),
            'members': completed, 'reason': reason,
            'outer_journal_sha256': last_sha, 'notion_write_attempts': 0,
            'journal_write_state': 'unknown' if durable.poisoned or emitter.failed else 'confirmed',
            'complete_library_claimed': False, **metrics}


APPLY_GROUP_RESULT_CONTRACT = 'get-notion-historical-apply-group-result-1'


class ApplyBoundaryYield(e.Blocked):
    def __init__(self): super().__init__('batch_boundary_yield')


def body_receipts_match(state, manifest):
    """Match prior-confirmed source/blob/page/DS, not a manifest-digest claim."""
    engine = state['engine_state']
    for ident, row in manifest['records'].items():
        prior = engine['receipts'].get(ident, {})
        if (any(prior.get(key) != value for key, value in {
                'note_id': ident, 'page_id': row['target']['page_id'],
                'data_source_id': manifest['data_source_id'],
                'source_commit': manifest['source_commit'], 'md_blob': row['blob'],
                'renderer_version': e.p.RENDERER, 'readback_verified': True}.items())
                or ident in engine['failures']
                or e.plan_recovery.held(engine, ident, row['target']['page_id'])):
            return False
    return True


def apply_boundary(request, durable, emitter, *, current, notes, plan=None):
    """Reserve a conservative ordinary flat-append scope, never reset counters.

    Admission is not a claim that unknown pagination, latency or external edits
    will fit. Each existing hard cap remains authoritative inside execution.
    Before native planning reserve its reads; after planning reserve all ordinary
    transactions plus one failure/restore per row and a final refresh. State-byte
    headroom is recomputed from the actual growing state and exact plan, not the
    initial ~664 KiB size. No budget-limit failure is retried in this session.
    """
    if durable.poisoned or emitter.failed: e.fail('journal_failed')
    if plan is None:
        requests = 10 * notes + 1; commits = 0; state_bound = len(e.encoded(current))
    else:
        rows = plan['records']
        # This group is intentionally not a second transaction engine.
        if any(row['action'] not in ('update', 'noop', 'blocked')
                or row.get('property_only') or row.get('old_roots')
                or row.get('create_batches') for row in rows):
            e.fail('approval_missing')
        commits = 6 * len(rows) + 3 if rows else 0
        requests = 60 * len(rows) + 1
        # Plan, operation before/working snapshots, new IDs, receipts and
        # normalized wire payloads coexist. Generous growth allowance is bounded
        # by the existing state limit; measured byte accounting still decides.
        growth = sum(4 * len(e.encoded(row.get('rendered', [])))
                     + 1024 * len(row.get('rendered', [])) for row in rows)
        state_bound = len(e.encoded(current)) + 3 * len(e.encoded(plan)) + growth + 16384
    checkpoints_needed = commits + 1
    if (emitter.sequence + checkpoints_needed > checkpoints.MAX_CHECKPOINTS
            or durable.commits_attempted + commits > durable.max_commits
            or state_bound > journal.MAX_STATE
            or durable.new_blob_bytes + commits * state_bound > durable.max_batch_bytes
            or request.attempts + requests > 2000
            or request.bytes + transport.ro.MAX_RESPONSE > transport.ro.MAX_HTTP_BYTES
            or request.clock() + requests * .4 + commits * 2 + 30 >= request.deadline):
        return False
    return True


def apply_group(local, remote, token, root, store, request, *, selection, expected,
                policy_hash, apply, writers_guard, durable, emitter, refresh, loader=None):
    """Bounded historical body scopes inside ONE authenticated runtime session."""
    members = private_input.validate_apply_selection(selection, expected, apply=apply)
    if (expected not in historical.APPROVED_APPLY_GROUPS or not apply
            or not isinstance(request, transport.Transport)
            or not isinstance(durable, journal.RemoteJournal) or store is not durable
            or not isinstance(emitter, checkpoints.CheckpointEmitter)):
        e.fail('approval_missing')
    completed = []; status = 'applied'; reason = None; last_sha = None
    execution = None; prior_effects = False; scope_attempts = len(request.attempted)
    if loader is None: loader = source_loader.Loader(local, remote, token, root, policy_hash)
    try:
        current, last_sha = store.read(); outbox.check_state(current)
        active = current['engine_state']['journal']; start = 0
        if active is not None:
            plan = active['plan']; member_hashes = [member for _, member in members]
            if (plan.get('historical_only') is not True or plan.get('property_only')
                    or plan.get('historical_manifest_sha256') not in member_hashes):
                e.fail('journal_busy')
            start = member_hashes.index(plan['historical_manifest_sha256'])
            if plan.get('historical_manifest') != members[start][0]: e.fail('journal_busy')
            prior_effects = bool(active['index'] or e.operation_has_effects(active['operation']))
            # Recover the active member FIRST, never execute earlier scopes.
            # Report only the contiguous prior-confirmed receipt prefix, even
            # when recovery must stop. These are NOT fresh native observations.
            for previous, previous_hash in members[:start]:
                if not body_receipts_match(current, previous): break
                completed.append({'manifest_sha256': previous_hash,
                    'pages': len(previous['records']), 'reused_receipts': True,
                    'native_reverified': False, 'resumed_journal': False})
        for manifest, member in members[start:]:
            current, last_sha = store.read(); outbox.check_state(current)
            if not apply_boundary(request, durable, emitter, current=current,
                                  notes=len(manifest['records'])):
                raise ApplyBoundaryYield()
            reused = body_receipts_match(current, manifest)
            resuming = active is not None
            execution = None; scope_attempts = len(request.attempted)
            result = dispatch(local, remote, token, root, store, request,
                manifest=manifest, expected=member, policy_hash=policy_hash, apply=True,
                writers_guard=writers_guard, loader=loader, verify_reused=True,
                admit=lambda state, plan: apply_boundary(request, durable, emitter,
                    current=state, notes=len(manifest['records']), plan=plan))
            execution = result.get('execution', {})
            last_sha = result['outer_journal_sha256']
            if (durable.poisoned or emitter.failed
                    or durable.anchor['state_sha256'] != last_sha
                    or result.get('scope_confirmed') is not True
                    or execution.get('status') != 'verified'
                    or execution.get('write_state') not in ('confirmed', 'not_attempted')):
                status = 'failed'; reason = execution.get('reason') or 'historical_scope_incomplete'
                break
            if active is not None:
                if len(completed) != start: e.fail('journal_busy')
                active = None; prior_effects = False
            completed.append({'manifest_sha256': member,
                'pages': len(manifest['records']), 'reused_receipts': reused,
                'native_reverified': not resuming, 'resumed_journal': resuming})
            # A confirmed scope may be reported even if the subsequent fresh
            # source/writer guard rejects continuation. No next member starts.
            if not e.p.writer_gate({'old_writer_evidence': writers_guard()}):
                e.fail('writer_gate_unknown')
            current, fresh_sha = store.read(); outbox.check_state(current)
            if (fresh_sha != last_sha or current['engine_state']['journal'] is not None
                    or not body_receipts_match(current, manifest)): e.fail('journal_failed')
        if status == 'applied': refresh()
    except ApplyBoundaryYield:
        status = 'partial'; reason = 'batch_boundary_yield'
        # Refresh only a genuinely idle confirmed prefix, never an active plan.
        try:
            current, last_sha = store.read()
            if current['engine_state']['journal'] is None: refresh()
        except Exception as exc:
            status = 'failed'; reason = str(exc) if str(exc) in GROUP_REASONS else 'journal_failed'
    except Exception as exc:
        status = 'failed'; reason = str(exc) if str(exc) in GROUP_REASONS else 'historical_group_failed'
    uncertain = (durable.poisoned or emitter.failed
        or execution is not None and execution.get('write_state') not in ('confirmed', 'not_attempted')
        or execution is None and len(request.attempted) > scope_attempts
        or status != 'applied' and prior_effects)
    return {'contract': APPLY_GROUP_RESULT_CONTRACT, 'status': status,
        'selection_sha256': expected, 'scopes_selected': len(members),
        'scopes_completed': len(completed), 'scopes_remaining': len(members)-len(completed),
        'pages': sum(row['pages'] for row in completed), 'members': completed,
        'reason': reason, 'stopped_execution': execution if status != 'applied' else None,
        'outer_journal_sha256': last_sha, 'notion_write_attempts': len(request.attempted),
        'write_state': 'unknown' if uncertain else 'confirmed' if request.attempted else 'not_attempted',
        'journal_write_state': 'unknown' if durable.poisoned or emitter.failed else 'confirmed',
        'complete_library_claimed': False, **capture_metrics(request, durable, emitter)}


def verify_active_transition(local, remote, token, root, current, sha, request, *,
                             manifest, expected, policy_hash, writers_guard):
    """Exact active predecessor, original source ancestry and fresh native guard.

    Read-only. No plan creation, state transition, transport approval or CAS.
    The caller retains original provenance/lease and confirms the later CAS.
    """
    if not historical_active.initial_state_valid(current, sha): e.fail('state_changed')
    active = current['engine_state']['journal']; plan = active['plan']
    historical.validate(manifest, expected, apply=True)
    if expected != plan['historical_manifest_sha256'] or manifest != plan['historical_manifest']: e.fail('approval_missing')
    if historical.creation_ids(manifest) or plan.get('property_manifest') is not None: e.fail('approval_missing')
    loader = source_loader.Loader(local, remote, token, root, policy_hash)
    head = source_loader.entry.a.checked(local.run('rev-parse', source_loader.entry.r.MAIN_REF).decode().strip(), 40)
    loader.ancestry(manifest['source_commit'], head)
    for row in manifest['records'].values(): loader.blob(manifest['source_commit'], row['path'], e.p.MAX_NOTE)
    publication = historical.Publication(local.vault, manifest, expected)
    if publication.descriptor != plan['publication']: e.fail('source_changed')
    api = API(request, manifest['data_source_id'], sorted(manifest['records']))
    from get_notion_runtime import schema
    policy = {'data_source_id': manifest['data_source_id'], 'id_property': '笔记 ID',
        'title_property': schema(request, manifest['data_source_id']), 'allow_create': True,
        'allow_archive': True, 'format_accepted': True, 'old_writer_evidence': writers_guard()}
    if not e.p.writer_gate(policy) or policy != plan['policy']: e.fail('writer_gate_unknown')
    row = plan['records'][active['index']]; operation = active['operation']
    if row['old_roots']: e.fail('state_invalid')
    if operation is None:
        # Exact progressed checkpoint: read first; no previous cleanup is
        # invented, replayed or rewound. The original next-row before pin holds.
        progress = historical_active.progress_binding()
        if not progress or sha != progress['anchor']['state_sha256']: e.fail('state_invalid')
        expected_native = row['before_sha256']
    else:
        prepared=historical_active.prepared_binding()
        if (prepared and historical_active.plan_binding(plan)==prepared
                and sha==prepared['resume_anchor']['state_sha256']
                and (operation['kind'],operation['phase'])==('append','prepared')):
            expected_native=row['before_sha256']
        else:
            if operation['kind'] != 'cleanup' or operation['phase'] != 'verified': e.fail('state_invalid')
            expected_native = e.target_sha(operation['working'], row['note_id'], manifest['data_source_id'])
    snapshot = api.read_note(row['note_id'])
    if e.target_sha(snapshot, row['note_id'], manifest['data_source_id']) != expected_native: e.fail('target_changed')
    publication.check(plan['publication'])
    if not e.p.writer_gate({'old_writer_evidence': writers_guard()}): e.fail('writer_gate_unknown')
    return {'source_ancestry_verified': True, 'native_verified': True, 'external_writes': 0, 'loader': loader}
