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
import get_notion_authenticated_recovery as authenticated_recovery


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


def creation_recovery_for(active, manifest, expected, api):
    """One code-pinned unknown CREATE; native reads only, never retry POST.

    Permission is the exact immutable registration, not the freshly generated
    snapshot digest. The engine re-reads and repeats its strict created check.
    """
    from get_notion_live_transport import fingerprint, wire_blocks
    from get_notion_property_transaction import creation_wire_properties
    import get_notion_create_resume as create_resume
    if active is None: return None
    plan = active.get('plan') if isinstance(active, dict) else None
    if not isinstance(plan, dict): e.fail('journal_busy')
    digest = e.digest(plan)
    binding = create_resume.plan_binding(plan)
    if binding is None: return None
    keys = {'plan_sha256', 'manifest_sha256', 'source_commit', 'initial_index',
            'operation_sha256', 'note_id', 'page_id', 'source', 'request_sha256'}
    if (not isinstance(binding, dict) or not keys <= set(binding)
            or binding['plan_sha256'] != digest or active.get('plan_sha256') != digest
            or binding['manifest_sha256'] != expected or plan.get('historical_manifest') != manifest
            or plan.get('historical_manifest_sha256') != expected
            or binding['source_commit'] != manifest['source_commit']
            or type(binding['initial_index']) is not int
            or not 0 <= binding['initial_index'] < len(plan.get('records', []))):
        e.fail('approval_missing')
    e.p.hex_hash(binding['operation_sha256'],64);e.p.hex_hash(binding['request_sha256'],64)
    e.p.note_id(binding['note_id']);e.p.notion_id(binding['page_id'])
    index = active.get('index')
    if type(index) is not int or index < binding['initial_index']:e.fail('journal_busy')
    if index != binding['initial_index']:return None
    row = plan['records'][index]; operation = active.get('operation')
    if (row.get('note_id') != binding['note_id'] or row.get('action') != 'create'
            or row.get('source') != binding['source']
            or manifest['records'].get(row['note_id'], {}).get('mode') != 'new_numeric'
            or not isinstance(operation,dict) or operation.get('kind') != 'create'):
        e.fail('approval_missing')
    # The ack-only variant can verify its exact existing page, never borrow
    # the old in-flight recovery permission or alter the saved response.
    receipt_only=create_resume.receipt_only_binding(plan)
    if receipt_only:
        if not create_resume.allows(plan,active):e.fail('uncertain_create')
    elif (operation.get('phase') != 'in_flight' or operation.get('response') is not None
            or e.digest(operation) != binding['operation_sha256']):
        e.fail('uncertain_create')
    before = operation.get('before')
    if (not isinstance(before,dict) or before.get('complete') is not True
            or before.get('native_blocks_available') is not True
            or before.get('identity_query_complete') is not True
            or before.get('exact_id_matches') != []
            or e.target(before,row['note_id'],manifest['data_source_id']) is not None):
        e.fail('uncertain_create')
    request = {'parent':{'type':'data_source_id','data_source_id':manifest['data_source_id']},
        'properties':creation_wire_properties(row), 'children':wire_blocks(row['request_blocks'],None,uploaded_pngs=row.get('uploaded_pngs'))}
    if fingerprint('POST','/pages',request) != binding['request_sha256']:
        e.fail('approval_missing')
    snapshot = e.read_target(api,row['note_id'],manifest['data_source_id']) if receipt_only else api.read_note(row['note_id'])
    e.created(snapshot,row,manifest['data_source_id'],binding['page_id'])
    return {'plan_sha256':digest,'note_id':row['note_id'],'page_id':binding['page_id'],
            'snapshot_sha256':e.target_sha(snapshot,row['note_id'],manifest['data_source_id'])}

def dispatch(local,remote,token,root,store,request,*,manifest,expected,policy_hash,apply,writers_guard,loader=None,admit=None,verify_reused=False,row_admit=None,settle_row_yield=None,resume_admit=None):
    if settle_row_yield is not None and (row_admit is None or not callable(settle_row_yield)):e.fail('approval_missing')
    if resume_admit is not None and not callable(resume_admit):e.fail('approval_missing')
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
    if active is not None and (historical_active.allows(active['plan'], active)
            or authenticated_recovery.allows(active['plan'], active)):
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
        if ((plan['rules_sha256']!=e.rules_sha256() and not (historical_active.allows(plan, active)
                or authenticated_recovery.allows(plan, active))) or plan['publication']!=publication.descriptor
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
        # Saved prepared CREATE bypasses executor clean-row hooks; reserve it
        # before any resumed native write, without clearing a pending operation.
        if resume_admit is not None and resume_admit(current,plan) is not True:e.fail('request_budget')
        if admit is not None and not (active and active['operation'] is not None) and not admit(current,plan): raise ApplyBoundaryYield()
        if not isinstance(request,transport.Transport): e.fail('approval_missing')
        import get_notion_create_resume as create_resume
        receipt_only=create_resume.receipt_only_binding(plan)
        allowed=frozenset() if receipt_only else pipeline.wire_allowlist(plan,api)
        api.approved=allowed;request.approved=allowed
        recovery_boundary=False
        def ordinary_row(engine, pending_plan):
            nonlocal recovery_boundary
            if receipt_only:
                pending=engine['journal']
                if (pending is None or pending_plan!=plan or not create_resume.allows(plan,pending)
                        or pending['index']!=receipt_only['initial_index']+1 or pending['operation'] is not None):e.fail('approval_missing')
                recovery_boundary=True
                return False
            outer=deepcopy(adapter.outer)
            outer['engine_state']=deepcopy(engine)
            return row_admit(outer,pending_plan)
        create_recovery=creation_recovery_for(active,manifest,expected,api)
        result=e.execute(plan,e.digest(plan),publication,api,adapter,expected_store_sha=sha,writers_guard=writers_guard,
                         create_recovery=create_recovery,approved_recovery_sha=e.digest(create_recovery) if create_recovery is not None else None,
                         authorize_batch=None if receipt_only else pipeline.batch_authorizer(plan,api,request,adapter),
                         authorize_row=ordinary_row if receipt_only or row_admit is not None else None)
        if recovery_boundary:
            # Keep the original receipt CAS and clean-yield clearance CAS
            # separate. No fused checkpoint or truncated/re-authored plan.
            from get_notion_single_create_admission import settle_clean_yield
            result=settle_clean_yield(e,adapter,plan,result)
        elif (settle_row_yield is not None and getattr(row_admit,'clean_boundary_denied',False) is True
                and result.get('status')=='partial' and result.get('reason')=='batch_boundary_yield'):
            result=settle_row_yield(adapter,plan,result)
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
    if active is not None and (historical_active.allows(active['plan'], active)
            or authenticated_recovery.allows(active['plan'], active)):
        result.update(resumed_prior_plan_sha256=active['plan_sha256'],
            resumed_prior_rules_sha256=active['plan']['rules_sha256'])
    if verify_reused:
        result['scope_confirmed']=(final['engine_state']['journal'] is None
            and body_receipts_match(final,manifest))
    return result


def capture(local,remote,token,root,store,request,*,candidate,expected,policy_hash,apply,writers_guard,loader=None):
    """Read Notion only; retain exact native evidence in the ONE private journal."""
    probe=historical.validate_capture(candidate,expected,apply=apply)
    new_ids=historical.creation_ids(probe)
    if new_ids and new_ids != set(candidate['records']):e.fail('approval_missing')
    current,version=store.read();outbox.check_state(current)
    if current['engine_state']['journal'] is not None: e.fail('journal_busy')
    if not e.p.writer_gate({'old_writer_evidence':writers_guard()}): e.fail('writer_gate_unknown')
    saved=current.get('historical_captures',{}).get(expected)
    reused=saved is not None
    if saved is not None and new_ids:
        # Never retrofit immutable old captures or pretend old evidence is fresh.
        historical.check_native_schema_capture(saved)
    if saved is None:
        if loader is None: loader=source_loader.Loader(local,remote,token,root,policy_hash)
        loader.commit(candidate['source_commit'])
        for row in candidate['records'].values():loader.blob(candidate['source_commit'],row['path'],e.p.MAX_NOTE)
        publication=historical.Publication(local.vault,probe,e.digest(probe))
        api=API(request,candidate['data_source_id'],sorted(candidate['records']))
        native_schema=api.schema() if new_ids else None
        if new_ids:historical.native_schema_evidence(native_schema)
        snapshots={ident:api.read_note(ident) for ident in sorted(candidate['records'])}
        manifest=deepcopy(candidate);manifest['contract']=historical.CONTRACT
        for ident,row in manifest['records'].items():
            if row['mode']=='existing_placeholder':
                pin=historical.target_pin(snapshots[ident],ident,manifest['data_source_id'])
                if pin['page_id']!=row['target']['page_id']:e.fail('target_changed')
                row['target']=pin
            elif e.target(snapshots[ident],ident,manifest['data_source_id']) is not None:e.fail('duplicate_id')
        saved={'candidate':deepcopy(candidate),'manifest':manifest,'snapshots':snapshots}
        if new_ids:
            if api.schema()!=native_schema:e.fail('property_schema_changed')
            saved['native_schema_evidence']=historical.native_schema_evidence(native_schema)
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


def held_scope_holds(state, manifest):
    """Exact registered holds in this immutable scope, never absence evidence."""
    engine=state['engine_state']
    holds=e.plan_recovery.validate_holds(engine.get('unresolved_by_id',{}))
    registered=authenticated_recovery.registry()
    legacy=historical_active.inflight_binding();hold=historical_active.inflight_hold()
    if legacy and hold:registered=[{'manifest_sha256':legacy['manifest_sha256'],'hold':hold}]+registered
    member=e.digest(manifest);found={}
    for binding in registered:
        h=binding['hold'];ident=h['note_id'];row=manifest['records'].get(ident)
        if (binding['manifest_sha256']==member and row and row['target']['page_id']==h['page_id']
                and e.same(holds.get(ident),h)):
            found[ident]=h
    return found


def held_scope_settled(state, manifest):
    """May scan past exact held rows; never means confirmed or resolved."""
    holds=held_scope_holds(state,manifest)
    if not holds or any(ident in state['engine_state']['receipts'] for ident in holds):return False
    others=deepcopy(manifest)
    for ident in holds:others['records'].pop(ident)
    return body_receipts_match(state,others)


def verify_held_scope(local,remote,token,root,request,current,manifest,member,policy_hash,writers_guard,loader):
    if not held_scope_settled(current,manifest):e.fail('unresolved_operation')
    historical.validate(manifest,member,apply=True)
    loader.ancestry(manifest['source_commit'],source_loader.entry.a.checked(local.run('rev-parse',source_loader.entry.r.MAIN_REF).decode().strip(),40))
    for row in manifest['records'].values():loader.blob(manifest['source_commit'],row['path'],e.p.MAX_NOTE)
    publication=historical.Publication(local.vault,manifest,member);publication.check(publication.descriptor)
    if not e.p.writer_gate({'old_writer_evidence':writers_guard()}):e.fail('writer_gate_unknown')
    api=API(request,manifest['data_source_id'],sorted(manifest['records']))
    holds=held_scope_holds(current,manifest)
    for ident,row in manifest['records'].items():
        if ident in holds:continue
        prior=current['engine_state']['receipts'][ident];snapshot=api.read_note(ident)
        fresh=e.receipt(snapshot,ident,manifest['data_source_id'],[x['id'] for x in prior['managed_blocks']],publication.descriptor,row,prior['asset_bindings'])
        if fresh!=prior:e.fail('human_edit_conflict')
    publication.check(publication.descriptor)
    if not e.p.writer_gate({'old_writer_evidence':writers_guard()}):e.fail('writer_gate_unknown')


def apply_boundary(request, durable, emitter, *, current, notes, plan=None, row_only=False):
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
        active=current['engine_state']['journal'] if row_only else None
        remaining=len(rows)-(active['index'] if active is not None else 0)
        reserve_rows=min(1,remaining) if row_only else len(rows)
        commits = 6 * reserve_rows + 3 if reserve_rows else 0
        requests = 60 * reserve_rows + 1
        if row_only and active is None:
            # The initial plan CAS is additional to the next clean-row reserve.
            # After it commits, the same row still fits without an empty-plan
            # yield caused solely by double-checking identical headroom.
            commits += 1
            current=deepcopy(current)
            current['engine_state']['journal']={'plan_sha256':e.digest(plan),'plan':deepcopy(plan),'index':0,'operation':None}
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
    current=None
    completed = []; processed = []; held_members = []; status = 'applied'; reason = None; last_sha = None
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
                if held_scope_settled(current,previous):
                    processed.append(previous_hash);held_members.append(previous_hash);continue
                if not body_receipts_match(current, previous): break
                processed.append(previous_hash)
                completed.append({'manifest_sha256': previous_hash,
                    'pages': len(previous['records']), 'reused_receipts': True,
                    'native_reverified': False, 'resumed_journal': False})
        for manifest, member in members[start:]:
            current, last_sha = store.read(); outbox.check_state(current)
            if not (active and active['operation'] is not None) and not apply_boundary(request, durable, emitter, current=current,
                                  notes=len(manifest['records'])):
                raise ApplyBoundaryYield()
            reused = body_receipts_match(current, manifest)
            resuming = active is not None
            execution = None; scope_attempts = len(request.attempted)
            if active is None and held_scope_settled(current,manifest):
                verify_held_scope(local,remote,token,root,request,current,manifest,member,policy_hash,writers_guard,loader)
                observed,observed_sha=store.read()
                if observed_sha!=last_sha:e.fail('state_changed')
                processed.append(member);held_members.append(member)
                continue
            result = dispatch(local, remote, token, root, store, request,
                manifest=manifest, expected=member, policy_hash=policy_hash, apply=True,
                writers_guard=writers_guard, loader=loader, verify_reused=True,
                admit=lambda state, plan: apply_boundary(request, durable, emitter,
                    current=state, notes=len(manifest['records']), plan=plan,row_only=True),
                row_admit=lambda state, plan: apply_boundary(request,durable,emitter,
                    current=state,notes=len(manifest['records']),plan=plan,row_only=True))
            execution = result.get('execution', {})
            last_sha = result['outer_journal_sha256']
            if (not durable.poisoned and not emitter.failed
                    and durable.anchor['state_sha256']==last_sha
                    and execution.get('status')=='partial'
                    and execution.get('reason')=='batch_boundary_yield'
                    and execution.get('write_state') in ('confirmed','not_attempted')):
                current,last_sha=store.read();outbox.check_state(current)
                pending=current['engine_state']['journal']
                if pending is not None and pending['operation'] is not None:e.fail('journal_failed')
                status='partial';reason='batch_boundary_yield';prior_effects=False
                break
            settled_hold=False
            if (historical_active.exact_hold_present(current['engine_state'])
                    or authenticated_recovery.exact_hold_present(current['engine_state'])):
                observed,observed_sha=store.read()
                if observed_sha!=last_sha:e.fail('state_changed')
                current=observed
                settled_hold=(observed['engine_state']['journal'] is None and held_scope_settled(observed,manifest))
            if (durable.poisoned or emitter.failed
                    or durable.anchor['state_sha256'] != last_sha
                    or (result.get('scope_confirmed') is not True and not settled_hold)
                    or execution.get('status') != 'verified'
                    or execution.get('write_state') not in ('confirmed', 'not_attempted')):
                status = 'failed'; reason = execution.get('reason') or 'historical_scope_incomplete'
                break
            if active is not None:
                if len(processed) != start: e.fail('journal_busy')
                active = None; prior_effects = False
            processed.append(member)
            if settled_hold:held_members.append(member)
            else:completed.append({'manifest_sha256': member,
                'pages': len(manifest['records']), 'reused_receipts': reused,
                'native_reverified': not resuming, 'resumed_journal': resuming})
            # A confirmed scope may be reported even if the subsequent fresh
            # source/writer guard rejects continuation. No next member starts.
            if not e.p.writer_gate({'old_writer_evidence': writers_guard()}):
                e.fail('writer_gate_unknown')
            current, fresh_sha = store.read(); outbox.check_state(current)
            if (fresh_sha != last_sha or current['engine_state']['journal'] is not None
                    or not (body_receipts_match(current, manifest) or held_scope_settled(current,manifest))): e.fail('journal_failed')
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
    unresolved_count=0
    if current is not None:
        holds=e.plan_recovery.validate_holds(current['engine_state'].get('unresolved_by_id',{}))
        unresolved_count=sum(any(ident in m['records'] and m['records'][ident]['target']['page_id']==hold['page_id']
            for m,d in members) for ident,hold in holds.items())
    confirmed_pages=0
    for m,d in members:
        for ident,row in m['records'].items():
            one={**m,'records':{ident:row}}
            confirmed_pages+=int(current is not None and body_receipts_match(current,one))
    if current is None:unresolved_count=None;confirmed_pages=None
    if status=='applied' and unresolved_count:status='partial';reason='unresolved_operation'
    uncertain = (durable.poisoned or emitter.failed
        or execution is not None and execution.get('write_state') not in ('confirmed', 'not_attempted')
        or execution is None and len(request.attempted) > scope_attempts
        or status != 'applied' and prior_effects)
    return {'contract': APPLY_GROUP_RESULT_CONTRACT, 'status': status,
        'selection_sha256': expected, 'scopes_selected': len(members),
        'scopes_processed':len(processed),'unresolved_count':unresolved_count,
        'pages_confirmed':confirmed_pages,
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
    import get_notion_create_resume as create_resume
    if create_resume.initial_or_transitioned_state_valid(current,sha):
        return verify_create_resume_transition(local,remote,token,root,current,sha,request,
            manifest=manifest,expected=expected,policy_hash=policy_hash,writers_guard=writers_guard)
    authenticated=authenticated_recovery.initial_state_valid(current,sha)
    if not authenticated and not historical_active.initial_state_valid(current, sha): e.fail('state_changed')
    active = current['engine_state']['journal']; plan = active['plan']
    recovery=authenticated_recovery if authenticated else historical_active
    manifest,expected=recovery.transition_manifest(manifest,expected,plan)
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
        inflight=historical_active.inflight_binding()
        prepared=historical_active.prepared_binding()
        registered=authenticated_recovery.plan_binding(plan) if authenticated else None
        if (registered and sha==registered['proof']['anchor']['state_sha256']
                and (operation['kind'],operation['phase'])==('append','in_flight')):
            expected_native=None
        elif (inflight and historical_active.plan_binding(plan)==inflight
                and sha==inflight['resume_anchor']['state_sha256']
                and (operation['kind'],operation['phase'])==('append','in_flight')):
            expected_native=None
        elif (prepared and historical_active.plan_binding(plan)==prepared
                and sha==prepared['resume_anchor']['state_sha256']
                and (operation['kind'],operation['phase'])==('append','prepared')):
            expected_native=row['before_sha256']
        else:
            if operation['kind'] != 'cleanup' or operation['phase'] != 'verified': e.fail('state_invalid')
            expected_native = e.target_sha(operation['working'], row['note_id'], manifest['data_source_id'])
    snapshot = api.read_note(row['note_id'])
    actual_native=e.target_sha(snapshot,row['note_id'],manifest['data_source_id'])
    if expected_native is None:
        # Unchanged before is still uncertain, not permission to re-append.
        # Original executor will block; a complete append must pass its own check.
        if actual_native!=row['before_sha256']: e.fail('target_changed')
    elif actual_native != expected_native: e.fail('target_changed')
    publication.check(plan['publication'])
    if not e.p.writer_gate({'old_writer_evidence': writers_guard()}): e.fail('writer_gate_unknown')
    return {'source_ancestry_verified': True, 'native_verified': True, 'external_writes': 0, 'loader': loader}


def verify_create_resume_transition(local,remote,token,root,current,sha,request,*,
                                    manifest,expected,policy_hash,writers_guard):
    """Exact unknown CREATE native confirmation before policy-only transition."""
    import get_notion_create_resume as create_resume
    if not create_resume.initial_or_transitioned_state_valid(current,sha):e.fail('state_changed')
    active=current['engine_state']['journal'];plan=active['plan']
    historical.validate(manifest,expected,apply=True)
    if (manifest!=plan.get('historical_manifest') or expected!=plan.get('historical_manifest_sha256')
            or not historical.creation_ids(manifest)):e.fail('approval_missing')
    property_expected=private_input.creation_property_for(expected,required=True)
    raw=private_input.read(remote,property_expected,contract=properties.tx.CONTRACT)
    property_manifest=private_input.strict_capture_json(raw,256*1024)
    if property_manifest!=plan.get('property_manifest') or property_expected!=plan.get('property_manifest_sha256'):
        e.fail('approval_missing')
    historical.bind_creation_properties(manifest,property_manifest,property_expected)
    historical.require_creation_capture(current.get('historical_captures',{}),manifest,expected)
    properties.check_dependency()
    loader=source_loader.Loader(local,remote,token,root,policy_hash)
    head=source_loader.entry.a.checked(local.run('rev-parse',source_loader.entry.r.MAIN_REF).decode().strip(),40)
    loader.ancestry(manifest['source_commit'],head)
    for source in manifest['records'].values():loader.blob(manifest['source_commit'],source['path'],e.p.MAX_NOTE)
    publication=historical.Publication(local.vault,manifest,expected)
    if publication.descriptor!=plan['publication']:e.fail('source_changed')
    api=API(request,manifest['data_source_id'],sorted(manifest['records']))
    schema=api.schema();titles=[name for name,row in schema.items() if isinstance(row,dict) and row.get('type')=='title']
    if len(titles)!=1:e.fail('property_schema_changed')
    policy={'data_source_id':manifest['data_source_id'],'id_property':'笔记 ID','title_property':titles[0],
        'allow_create':True,'allow_archive':True,'format_accepted':True,'old_writer_evidence':writers_guard()}
    if not e.p.writer_gate(policy) or policy!=plan['policy']:e.fail('writer_gate_unknown')
    properties.tx.context(publication,schema,property_manifest,property_expected,policy)
    historical.check_creation_titles(plan,publication,property_manifest,property_expected)
    recovered=creation_recovery_for(active,manifest,expected,api)
    if recovered is None:e.fail('uncertain_create')
    publication.check(plan['publication'])
    if not e.p.writer_gate({'old_writer_evidence':writers_guard()}):e.fail('writer_gate_unknown')
    return {'source_ancestry_verified':True,'native_verified':True,'external_writes':0,'loader':loader}
