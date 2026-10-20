"""Historical entry through the existing guarded runtime and durable journal."""
from copy import deepcopy
from collections import Counter
import get_notion_executor as e
import get_notion_historical as historical
import get_notion_outbox as outbox
import get_notion_live_transport as transport
import get_notion_pipeline as pipeline
import get_confirmed_source as source_loader


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


def dispatch(local,remote,token,root,store,request,*,manifest,expected,policy_hash,apply,writers_guard):
    historical.validate(manifest,expected,apply=apply)
    if transport.native.sha(manifest['data_source_id'])!=transport.native.DATA_SOURCE_PIN or transport.native.sha(manifest['database_id'])!=transport.native.DATABASE_PIN: e.fail('wrong_parent')
    current,version=store.read();outbox.check_state(current)
    active=current['engine_state']['journal']
    if active is not None and (not active['plan'].get('historical_only') or active['plan'].get('historical_manifest_sha256')!=expected): e.fail('journal_busy')
    loader=source_loader.Loader(local,remote,token,root,policy_hash)
    loader.commit(manifest['source_commit'])
    for row in manifest['records'].values(): loader.blob(manifest['source_commit'],row['path'],e.p.MAX_NOTE)
    publication=historical.Publication(local.vault,manifest,expected)
    api=API(request,manifest['data_source_id'],sorted(manifest['records']))
    from get_notion_runtime import schema
    policy={'data_source_id':manifest['data_source_id'],'id_property':'笔记 ID',
        'title_property':schema(request,manifest['data_source_id']),'allow_create':True,
        'allow_archive':True,'format_accepted':True,'old_writer_evidence':writers_guard()}
    if not e.p.writer_gate(policy): e.fail('writer_gate_unknown')
    adapter=outbox.EngineStore(store);state,sha=adapter.read()
    snapshots={} if active else {ident:api.read_note(ident) for ident in sorted(manifest['records'])}
    if active:
        plan=deepcopy(active['plan'])
        if plan['rules_sha256']!=e.rules_sha256() or plan['publication']!=publication.descriptor or plan['policy']!=policy or plan.get('historical_manifest')!=manifest: e.fail('journal_busy')
    else: plan=historical.prepare(publication,state,sha,snapshots,policy,manifest,expected)
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
    return {**pipeline.public_report(report),'initial_notes':0,'initial_range_candidates':0,
            'queued':len(final['pending_by_id']),'outer_journal_sha256':final_sha,
            'source_chain_complete':final['last_enqueued_publication']==final['observed_publication_tip']}


def capture(local,remote,token,root,store,request,*,candidate,expected,policy_hash,apply,writers_guard):
    """Read Notion only; retain exact native evidence in the ONE private journal."""
    probe=historical.validate_capture(candidate,expected,apply=apply)
    current,version=store.read();outbox.check_state(current)
    if current['engine_state']['journal'] is not None: e.fail('journal_busy')
    if not e.p.writer_gate({'old_writer_evidence':writers_guard()}): e.fail('writer_gate_unknown')
    saved=current.get('historical_captures',{}).get(expected)
    if saved is None:
        loader=source_loader.Loader(local,remote,token,root,policy_hash)
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
        'pages':len(saved['snapshots']),'notion_write_attempts':0,'complete_library_claimed':False}
