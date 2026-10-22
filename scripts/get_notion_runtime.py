"""Bounded Git-confirmed Notion runtime. Preparation is read-only by default.

The active controller, source publication and journal each have separate
external anchors. Native content remains private in memory/journal. A new
runtime never trusts a current state merely because it can hash that state.
"""
import get_notion_fixed_source as fixed_auth
import get_private_proof as private_proof
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
from collections import Counter
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import get_notion_executor as e
import get_notion_outbox as outbox
import get_notion_historical as historical
import get_notion_historical_input as historical_input
import get_notion_historical_runtime as historical_runtime
import get_notion_pipeline as pipeline
import get_notion_live_transport as transport
import get_notion_readonly as ro
import get_notion_preserved_image as preserved_image
import get_notion_writer_guard as old_writers
import get_notion_remote_journal as journal
import get_notion_journal_runtime as checkpoints
import get_notion_plan_recovery as plan_recovery
import get_notion_run15_recovery as run15_recovery
import get_notion_run53_recovery as run53_recovery
import get_notion_run57_recovery as run57_recovery
import get_notion_historical_active_recovery as historical_active
import get_notion_idle_recovery as idle_recovery
import get_notion_run16_recovery as run16_recovery
import get_notion_run18_recovery as run18_recovery
import get_notion_run28_recovery as run28_recovery
import get_notion_run32_recovery as run32_recovery
import get_notion_properties_runtime as property_runtime
import get_notion_png_expired_recovery as expired_recovery
import get_notion_png_transport as png_transport
import get_notion_png_recovery as png_recovery
import get_notion_png_loader as png_loader
import get_notion_png_runtime as png_runtime
import get_notion_png_store as png_store
import get_notion_png_upload as png_upload
import get_notion_uploaded_png as png_image

# Deployment anchors are code-reviewed constants, never workflow inputs.
PNG_ASSET_ANCHOR = _private_bindings.value('get_notion_runtime', 'PNG_ASSET_ANCHOR')
PNG_ASSET_ANCHOR_SHA256 = _private_bindings.value('get_notion_runtime', 'PNG_ASSET_ANCHOR_SHA256')
PNG_SEQ2_PUBLICATION = _private_bindings.value('get_notion_runtime', 'PNG_SEQ2_PUBLICATION')
# Populated only after an independently reviewed append-only private asset
# commit. Local downloads cannot activate uploads or manufacture an anchor.
PNG_SEQ2_ASSET_ANCHOR = _private_bindings.value('get_notion_runtime', 'PNG_SEQ2_ASSET_ANCHOR')
PNG_SEQ2_ASSET_ANCHOR_SHA256 = _private_bindings.value('get_notion_runtime', 'PNG_SEQ2_ASSET_ANCHOR_SHA256')
import get_confirmed_source as source_loader
import get_recurring_entry as entry
import get_recurring_runtime as recurring
import get_publication_provenance as provenance
from get_recurring_entry import a,m,r

PRODUCTION_ACTIVATED=True
CONTRACT='get-notion-standing-runtime-1'
MAX_RUN_RECORDS=10
STAGES={'input','policy','writers','clone','journal','source','initial','queue','execute','final'}
ERRORS=set(png_runtime.REASONS)|{'notion_activation_pending','notion_context','notion_scope','notion_state_anchor','notion_source_changed','notion_initial_source','notion_schema','notion_marker','notion_failed','png_anchor_pending','notion_journal_busy','writer_guard_permission','writer_guard_quota','writer_gate_unknown','journal_provenance_missing','asset_permission_denied','permission_denied'}

ERRORS.update(e.properties.REASONS | {'journal_busy'})
ERRORS.update({'png_expired_evidence_invalid','png_expired_missing_unverified','png_expired_status_unverified','png_expired_prestate_changed','png_expired_policy_unchanged','png_expired_plan_scope','png_expired_record_invalid','png_expired_old_upload_reused','png_expired_transition_changed','png_expired_record_changed','approval_missing','asset_changed','asset_not_committed','asset_type','invalid_binding','invalid_identity','invalid_route','journal_invalid','png_loader_anchor','png_loader_blob','png_loader_delta','png_loader_local_changed','png_loader_manifest','png_loader_partial_clone','png_loader_source_changed','png_loader_unrequested_blob','read_budget','request_budget','request_mismatch','response_invalid','unreviewed_asset','upload_identity_changed','upload_metadata_changed','write_not_authorized','write_not_authorized_or_already_attempted','writes_disabled','wrong_destination'})


ERRORS.update(e.REASONS | {'fixed_source_capacity'})

# Fixed program diagnostics only; never raw exception text or private values.
DIAGNOSTIC_ERRORS=frozenset(ERRORS | recurring.PUBLIC_ERROR_CODES | {'notion_journal_mode_busy', 'journal_loader_failed', 'journal_provenance_steps', 'missing_credentials', 'journal_provenance_failed', 'journal_checkpoint_write', 'unread_target', 'journal_loader_commit', 'private_proof_unverified', 'invalid_children', 'outbox_chain_incomplete', 'identity_query_invalid', 'source_content_budget', 'journal_provenance_changed', 'journal_loader_local_mutation', 'unsupported_code_color', 'permission_denied', 'invalid_blocks', 'journal_provenance_record', 'invalid_route', 'source_chain_invalid', 'source_publication_budget', 'journal_provenance_missing', 'journal_loader_cache', 'invalid_approval', 'outbox_source_invalid', 'outbox_state_changed', 'response_budget', 'journal_provenance_workflow', 'source_history_budget', 'asset_write_contract_missing', 'journal_loader_tree', 'journal_provenance_log', 'source_foreign_get_change', 'request_budget', 'journal_loader_unrequested_blob', 'source_chain_budget', 'journal_provenance_controller_step', 'wrong_note_identity', 'outbox_state_invalid', 'journal_provenance_context', 'journal_checkpoint_failed', 'outbox_empty', 'journal_provenance_no_checkpoint', 'duplicate_id', 'read_transport_failed', 'unsupported_source_format', 'journal_provenance_budget', 'outbox_duplicate_id', 'journal_checkpoint_locked', 'journal_checkpoint_budget', 'invalid_cohort', 'journal_loader_blob', 'journal_provenance_jobs', 'journal_checkpoint_receipt', 'journal_checkpoint_context', 'journal_loader_target', 'wrong_schema', 'malformed_payload', 'pagination_budget', 'journal_loader_budget', 'journal_provenance_other_controller', 'outbox_chain_invalid', 'outbox_budget', 'journal_provenance_repository', 'outbox_approval_invalid', 'journal_busy', 'invalid_table', 'outbox_journal_failed', 'unsupported_prepend', 'source_object_invalid', 'journal_provenance_time', 'source_blob_invalid', 'journal_provenance_runs', 'write_uncertain', 'journal_provenance_run', 'journal_provenance_job', 'incomplete_pagination', 'read_api_failed', 'wrong_parent', 'wrong_database', 'write_not_authorized_or_already_attempted'})

def failure_diagnostic(exc):
    reason=str(exc)
    return {'reason':reason if reason in DIAGNOSTIC_ERRORS else 'notion_failed',
        'failure_kind':('missing_key' if isinstance(exc,KeyError) else
            'invalid_type' if isinstance(exc,TypeError) else
            'invalid_value' if isinstance(exc,ValueError) else
            'io_error' if isinstance(exc,OSError) else 'guard_or_runtime')}

def fail(code):raise ValueError(code) from None


class ReadOnlyStore:
    def __init__(self,value):outbox.check_state(value);self.value=deepcopy(value)
    def read(self):return deepcopy(self.value),outbox.digest(self.value)
    def compare_and_set(self,*args):fail('notion_scope')


def scope_for(public_policy):
    return {'contract':CONTRACT,'public_policy_sha256':a.sha(a.encoded(public_policy)),
        'rules_sha256':public_policy['rules_sha256'],'repository':r.REPOSITORY,
        'journal_path':outbox.STATE_PATH,'max_notes':MAX_RUN_RECORDS,'allow_create':True,
        'allow_archive_owned_roots':True,'property_updates':'exact-reviewed-manifest-v1',
        'property_manifest_allowlist_sha256':e.digest(sorted(property_runtime.APPROVED_MANIFESTS)),
        'historical_manifest_allowlist_sha256':e.digest(sorted(historical.APPROVED_MANIFESTS)),
        'historical_capture_allowlist_sha256':e.digest(sorted(historical.APPROVED_CAPTURE_SCOPES)),
        'historical_apply_groups_sha256':e.digest(historical.APPROVED_APPLY_GROUPS),
        'historical_apply_max_scopes':historical.MAX_APPLY_SCOPES,
        'historical_apply_group_max_notes':historical.MAX_APPLY_RUN_RECORDS,
        'historical_capture_groups_sha256':e.digest(historical.APPROVED_CAPTURE_GROUPS),
        'historical_capture_max_scopes':historical.MAX_CAPTURE_SCOPES,
        'historical_capture_group_max_notes':historical.MAX_CAPTURE_RUN_RECORDS,
        'historical_input_anchors_sha256':e.digest(historical_input.INPUT_ANCHORS),
        'incremental_property_scope_sha256':e.digest(e.incremental_properties.SCOPE),
        'new_page_property_scope_sha256':e.digest(e.creation_properties.SCOPE),'uploads':'two-fixed-three-png-cohorts-single-part','png_asset_anchor_sha256':PNG_ASSET_ANCHOR_SHA256,
        'png_seq2_asset_anchor_sha256':PNG_SEQ2_ASSET_ANCHOR_SHA256,
        'data_source_pin':transport.native.DATA_SOURCE_PIN,'database_pin':transport.native.DATABASE_PIN,
        'initial_proof_sha256':ro.HISTORICAL_TEMPLATE['proof_sha256'],
        'initial_image_proof_sha256':preserved_image.TEMPLATE['proof_sha256'],
        'preserve_existing_image_only':True,
        'initial_two_first':True,'uncertain_hold_sha256':plan_recovery.HOLD_SHA256,'unknown_existing_pages':'isolate','plan_authorization':'standing_bounded'}


def schema(request,data_source):
    if transport.native.sha(data_source)!=transport.native.DATA_SOURCE_PIN:fail('notion_schema')
    value=request('GET','/data_sources/'+data_source,None)
    if not isinstance(value,dict) or transport.native.uuid(value.get('id'))!=data_source or transport.native.sha(transport.native.uuid(value.get('parent',{}).get('database_id')))!=transport.native.DATABASE_PIN:fail('notion_schema')
    props=value.get('properties')
    if not isinstance(props,dict) or not isinstance(props.get('笔记 ID'),dict) or props['笔记 ID'].get('type')!='rich_text':fail('notion_schema')
    titles=[name for name,row in props.items() if isinstance(row,dict) and row.get('type')=='title']
    if len(titles)!=1 or not isinstance(titles[0],str) or not titles[0] or len(titles[0])>100:fail('notion_schema')
    return titles[0]


def engine_policy(request,data_source,initial_scope,apply):
    return {'contract':pipeline.CONTRACT,'rules_sha256':e.rules_sha256(),
        'data_source_id':data_source,'data_source_pin':transport.native.DATA_SOURCE_PIN,
        'database_pin':transport.native.DATABASE_PIN,'title_property':schema(request,data_source),
        'allow_apply':apply,'allow_create':True,'allow_archive':True,'format_accepted':True,
        'initial_scope_sha256':e.digest(initial_scope) if initial_scope else None,
        'plan_authorization':'standing_bounded'}


def initial_inputs(local,remote,parent,request):
    """Only fixed two pages + pinned census + two exact committed MD blobs."""
    observation=transport.native.collect(request)
    snapshots=observation['snapshots']
    batch,mapping,blobs=ro.read_committed_sources(remote,parent,snapshots)
    cohort={'commit':parent,'records':{ident:{'path':row['path'],'blob':row['md_blob'],'sha256':e.digest(blobs[row['md_blob']])} for ident,row in mapping.items()}}
    for oid,data in blobs.items():m.import_blob(local,data,oid)
    publication=e.GitPublication(local.vault,parent,parent,parent,initial_cohort=cohort,expected_initial_sha=e.digest(cohort))
    publication.image_adapter=preserved_image.Adapter(publication,request)
    initial_scope={};candidates=0
    for ident,row in mapping.items():
        if e.p.digest(ident)==preserved_image.TEMPLATE['note_id_sha256']:
            snapshots[ident]=publication.image_adapter(snapshots[ident],ident)
            result=preserved_image.initial_proposal(publication,ident,snapshots[ident],row['data_source_id'],remote)
            if result.get('executor_scope') is not None:
                initial_scope[ident]=result['executor_scope'];candidates+=1
            continue
        if e.p.digest(ident)!=ro.HISTORICAL_TEMPLATE['note_id_sha256']:continue
        proof=ro.historical.reconstruct_proof(ro.HISTORICAL_TEMPLATE,ro.HISTORICAL_TEMPLATE_SHA,note_id=ident,page_id=row['page_id'],source_path=row['path'])
        old=remote.check_source_budget(proof['source']['commit']).get(row['path'])
        if old is None or old[:3]!=('100644','blob',proof['source']['blob']) or row['md_blob']!=proof['source']['blob']:fail('notion_initial_source')
        result=ro.historical.propose_from_git(proof,ro.HISTORICAL_TEMPLATE['proof_sha256'],blobs[row['md_blob']],snapshots[ident],row['data_source_id'])
        if result.get('decision')=='candidate' and result.get('executor_scope') is not None:
            initial_scope[ident]=result['executor_scope'];candidates+=1
    return publication,initial_scope,next(iter(mapping.values()))['data_source_id'],{'initial_notes':2,'initial_range_candidates':candidates}


def initial_resume(local,remote,root,plan,token,policy_hash):
    desc=plan['publication'];cohort=desc.get('initial_cohort')
    if cohort is None or len(cohort.get('records',{}))!=2:fail('notion_journal_busy')
    loader=source_loader.Loader(local,remote,token,root,policy_hash)
    loader.commit(desc['commit'])
    for row in cohort['records'].values():loader.blob(desc['commit'],row['path'],e.p.MAX_NOTE)
    publication=e.GitPublication(local.vault,desc['commit'],desc['parent'],desc['commit'],initial_cohort=cohort,expected_initial_sha=e.digest(cohort))
    if not e.same(publication.descriptor,desc):fail('notion_source_changed')
    scope={};ds=plan['policy']['data_source_id']
    for row in plan['records']:
        ident=row['note_id']
        if e.p.digest(ident)==preserved_image.TEMPLATE['note_id_sha256'] and 'before' in row:
            result=preserved_image.initial_proposal(publication,ident,row['before'],ds,remote)
            if result.get('executor_scope') is not None:scope[ident]=result['executor_scope']
            continue
        if e.p.digest(ident)!=ro.HISTORICAL_TEMPLATE['note_id_sha256'] or 'before' not in row:continue
        snapshot=row['before'];src=desc['records'][ident]
        proof=ro.historical.reconstruct_proof(ro.HISTORICAL_TEMPLATE,ro.HISTORICAL_TEMPLATE_SHA,note_id=ident,page_id=snapshot['page_id'],source_path=src['path'])
        old=remote.check_source_budget(proof['source']['commit']).get(src['path'])
        if old is None or old[:3]!=('100644','blob',proof['source']['blob']) or src['blob']!=proof['source']['blob']:fail('notion_initial_source')
        result=ro.historical.propose_from_git(proof,ro.HISTORICAL_TEMPLATE['proof_sha256'],publication.read(ident),snapshot,ds)
        if result.get('executor_scope') is not None:scope[ident]=result['executor_scope']
    if e.digest(scope)!=plan['initial_scope_sha256']:fail('notion_scope')
    return publication,scope,ds,{'initial_notes':2,'initial_range_candidates':len(scope)}


def initial_done(state):
    """A confirmed receipt or explicitly retained record conflict is reviewed.

    This does not claim both samples were written or that assets are supported.
    """
    known=set(state['engine_state']['receipts'])|set(state['engine_state']['failures'])|set(state['engine_state'].get('unresolved_by_id',{}))
    return set(transport.native.PAGE_PINS.values()) <= {transport.native.sha(i) for i in known}


def png_selected(descriptor):
    commit=descriptor.get('commit')
    if commit==PNG_SEQ2_PUBLICATION:pins=png_upload.SEQ2_PINS
    elif PNG_ASSET_ANCHOR is None or commit==PNG_ASSET_ANCHOR.get('publication_commit'):pins=png_upload.PINS
    else:return False
    return any((pin:=pins.get(png_upload.sha(i.encode()))) is not None and row.get('sha256')==pin.get('md_sha256') for i,row in descriptor.get('records',{}).items())


def png_anchor(descriptor):
    if descriptor.get('commit')==PNG_SEQ2_PUBLICATION:
        anchor,sha=PNG_SEQ2_ASSET_ANCHOR,PNG_SEQ2_ASSET_ANCHOR_SHA256
    elif PNG_ASSET_ANCHOR is not None and descriptor.get('commit')==PNG_ASSET_ANCHOR.get('publication_commit'):
        anchor,sha=PNG_ASSET_ANCHOR,PNG_ASSET_ANCHOR_SHA256
    else:fail('png_anchor_pending')
    if anchor is None or sha is None or anchor.get('publication_commit')!=descriptor.get('commit'):
        fail('png_anchor_pending')
    return anchor,sha


def queue_limit(state):
    # Do not upload three long-page images ahead of their page transactions.
    # This exact source cohort progresses one page at a time; old queue and
    # active immutable journal selection retain their existing semantics.
    order=state['rotation_order']
    if (state['engine_state']['journal'] is None and order
            and expired_recovery.only_note_id(state) is None
            and state['pending_by_id'][order[0]]['commit']==PNG_SEQ2_PUBLICATION):
        return 1
    return MAX_RUN_RECORDS


def png_batch_ids(state):
    pending=state['engine_state']['journal']
    if pending is not None:return list(pending['plan']['publication']['records'])
    if state.get('last_enqueued_publication')!=state.get('observed_publication_tip'):return []
    only=expired_recovery.only_note_id(state)
    if only is not None:return [only]
    order=state['rotation_order']
    if not order:return []
    commit=state['pending_by_id'][order[0]]['commit']
    return [i for i in order if state['pending_by_id'][i]['commit']==commit][:queue_limit(state)]


def observe_expired_png(current, loaded, publication, request, writers_guard):
    """Fresh readonly evidence for one fixed transition; no mutable-state trust."""
    if outbox.digest(current)!=expired_recovery.STATE_SHA:fail('notion_state_anchor')
    journal=current['engine_state']['journal'];ident=journal['plan']['records'][1]['note_id']
    slot=current['png_assets']['uploads'][ident];binding=slot['plan']['binding']
    publication.check(publication.descriptor)
    if not e.p.writer_gate({'old_writer_evidence':writers_guard()}):fail('writer_gate_unknown')
    if loaded.before_write() is not True:fail('asset_changed')
    data=publication.read(ident)
    reference=png_loader.evidence.exact_reference(data,ident)
    resolved=loaded.evidence.resolve(ident,data,reference,current_commit=loaded.snapshot_head)
    raw=loaded.evidence.read_asset(resolved,current_commit=loaded.snapshot_head)
    inner=png_store.UploadStore(ReadOnlyStore(current),ident,slot['plan_sha256'],allow_persist=False)
    read=png_transport.Transport(request,slot['plan'],slot['plan_sha256'],raw,inner,allow_writes=False)
    status,value=read(png_upload.spec('GET','/file_uploads/'+slot['state']['upload_id']))
    if status!=200:fail('png_expired_status_unverified')
    api=transport.API(request,binding['data_source_id'],[ident],data_source_pin=transport.native.DATA_SOURCE_PIN,database_pin=transport.native.DATABASE_PIN)
    snapshot=api.read_note(ident)
    result={'upload_response':value,'query_note_id':ident,'data_source_id':binding['data_source_id'],'snapshot':snapshot}
    expired_recovery.proof(slot,result)
    publication.check(publication.descriptor)
    if not e.p.writer_gate({'old_writer_evidence':writers_guard()}):fail('writer_gate_unknown')
    if loaded.before_write() is not True:fail('asset_changed')
    return result


def png_context(local,remote,root,store,request,pub,policy,policy_hash,apply,writers_guard,previous,loaded_assets=None):
    state,sha=store.read()
    selected=png_selected(pub.descriptor)
    if not selected:return pub,None,previous,None,writers_guard,None
    anchor,anchor_sha=png_anchor(pub.descriptor)
    loaded=loaded_assets or png_loader.load(local,remote,pub.descriptor,anchor,anchor_sha,policy_hash)
    asset_key=png_store.asset_key(loaded.evidence.descriptor)
    session=png_runtime.Session(loaded.evidence,pub,store,request,policy,e.digest(policy),
        current_commit=lambda:loaded.snapshot_head,asset_guard=loaded.before_write,writers_guard=writers_guard,apply=apply)
    resumed=state['engine_state']['journal'] is not None
    if resumed:
        # Never recreate uploads/pages for an acknowledged or uncertain journal.
        resolver=png_image.Resolver(loaded.evidence,png_store.upload_receipts(state,asset_key=asset_key),current_commit=lambda:loaded.snapshot_head)
        preparation=None
    else:
        preparation=session.prepare()
        if preparation.get('upload_candidates',0)>2:fail('notion_scope')
        if not apply:return pub,None,previous,None,writers_guard,preparation
        resolver=session.advance_uploads() if preparation['upload_candidates'] else png_image.Resolver(loaded.evidence,png_store.upload_receipts(state,asset_key=asset_key),current_commit=lambda:loaded.snapshot_head)
        state,sha=store.read()
        # Upload CAS changes the outer hash; the original queue descriptor is stale.
        pub=outbox.QueuePublication(local.vault,state,sha,max_records=queue_limit(state))
        session.publication=pub
    image_adapter=png_image.SnapshotAdapter(resolver,store.read,request=request,asset_get=png_image.AssetGet(),previous=previous)
    def candidates(old,new):
        return {i:v for i,v in image_adapter.candidates.items() if i in new['receipts'] and old['receipts'].get(i)!=new['receipts'][i]}
    return pub,resolver,image_adapter,candidates,session.verify_unattached,preparation


class ObservedStore:
    """Capture already confirmed reads only; diagnostics never trigger new I/O."""
    def __init__(self,store):self.store=store;self.observed=None
    def __getattr__(self,name):return getattr(self.store,name)
    def read(self):
        value,sha=self.store.read();outbox.check_state(value)
        if outbox.digest(value)!=sha:fail('notion_state_anchor')
        self.observed=deepcopy(value)
        return value,sha
    def compare_and_set(self,*args):return self.store.compare_and_set(*args)
    def compare_and_set_verified(self,*args):
        fn=getattr(self.store,'compare_and_set_verified',None)
        if fn is None:
            version=self.store.compare_and_set(*args);value,observed=self.read()
            if version!=observed:fail('notion_state_anchor')
        else:value,version=fn(*args)
        outbox.check_state(value)
        if outbox.digest(value)!=version:fail('notion_state_anchor')
        self.observed=deepcopy(value);return value,version
    def diagnostic(self):
        if self.observed is None:return {'last_confirmed_upload_state':'unknown'}
        rows=self.observed.get('png_assets',{}).get('uploads',{}).values()
        phases=Counter();statuses=Counter()
        for row in rows:
            state=png_upload.check_state(row['state'],row['plan']);phases[state['phase']]+=1
            if 'write_http' in state:statuses[str(state['write_http']['status'])]+=1
        return {'last_confirmed_upload_phase_counts':dict(phases),'last_confirmed_post_http_counts':dict(statuses)}

def dispatch(*args,**kwargs):
    args=list(args);store=ObservedStore(args[4]);args[4]=store
    request=args[5];before=len(getattr(request,'attempted',set()))
    try:return _dispatch(*args,**kwargs)
    except Exception as exc:
        exc.png_diagnostic={**store.diagnostic(),'external_write_attempts_observed':len(getattr(request,'attempted',set()))-before}
        raise


def canonical_mode_busy_result():
    # This invocation did not write; an older journal may still have effects.
    return {'contract':CONTRACT,'status':'failed','stage':'journal',
        'reason':'notion_journal_mode_busy','write_state':'not_attempted',
        'external_write_attempts':0,'journal_write_state':'not_attempted',
        'unresolved_effects':'unknown','complete_library_claimed':False}


def canonical_mode_guard(state, sha):
    outbox.check_state(state)
    if sha!=outbox.digest(state):fail('notion_state_anchor')
    pending=state['engine_state']['journal']
    if pending is None:return None
    plan=pending['plan']
    flags=[plan.get(key,False) for key in ('historical_only','property_only')]
    if any(type(flag) is not bool for flag in flags) or all(flags):fail('notion_schema')
    if any(flags):return canonical_mode_busy_result()
    return None


def incremental_scope_for(publication, journal):
    """Only fresh ordinary plans or a saved explicit incremental writer plan."""
    if e.creation_properties.eligible(publication) and (journal is None or e.incremental_properties.enabled(journal['plan'])):
        return deepcopy(e.incremental_properties.SCOPE)
    return None


def _dispatch(local,remote,token,root,store,request,*,tip,policy_hash,apply,writers_guard,initial_factory=initial_inputs,create_recovery=None,approved_recovery_sha=None,loaded_assets=None):
    property_runtime.check_dependency()
    state,sha=store.read();outbox.check_state(state)
    if sha!=outbox.digest(state):fail('notion_state_anchor')
    busy=canonical_mode_guard(state,sha)
    if busy is not None:return busy
    isolated=0
    if plan_recovery.validate_initial_state(state,sha):
        candidate=plan_recovery.isolate_outer(state,sha)
        if apply:
            observed,sha=outbox.confirmed_compare_and_set(store,sha,candidate);state=observed
        else:
            store=ReadOnlyStore(candidate);state,sha=store.read()
        isolated=1
    pending_journal=state['engine_state']['journal'];counts={'initial_notes':0,'initial_range_candidates':0,'unresolved_operations':len(state['engine_state'].get('unresolved_by_id',{})),'quarantine_transitions':isolated if apply else 0,'quarantine_candidates':isolated,'unresolved_effects':'unknown' if state['engine_state'].get('unresolved_by_id') else 'none'}
    if pending_journal is not None:
        plan=pending_journal['plan'];desc=plan['publication']
        if desc.get('initial_cohort') is not None:
            pub,scope,ds,counts=initial_resume(local,remote,root,plan,token,policy_hash)
        else:
            source_loader.load_queue_publication(local,remote,token,root,{k:desc[k] for k in ('commit','parent')}|{'receipt_sha256':desc['outbox_receipt_sha256']},policy_hash)
            pub=outbox.QueuePublication(local.vault,state,sha,resume=True);scope={};ds=plan['policy']['data_source_id']
    elif not initial_done(state):
        parent=a.checked(local.run('rev-parse',r.MAIN_REF).decode().strip(),40)
        pub,scope,ds,counts=initial_factory(local,remote,parent,request)
    else:
        origin={'commit':entry.BOOTSTRAP['commit'],'receipt_sha256':None,'sequence':0}
        chain=source_loader.load_confirmed_chain(local,remote,token,root,tip,origin,state['last_enqueued_publication'] or origin,policy_hash)
        candidate=outbox.enqueue(local.vault,state,sha,chain,outbox.digest(chain))
        if not e.same(candidate,state):
            if apply:
                version=store.compare_and_set(sha,candidate);state,sha=store.read()
                if sha!=version or not e.same(state,candidate):fail('notion_state_anchor')
            else:store=ReadOnlyStore(candidate);state,sha=store.read()
        if state['last_enqueued_publication']!=state['observed_publication_tip']:
            return {'status':'queue_partial','queued':len(state['pending_by_id']),'source_chain_complete':False,'journal_sha256':sha,'write_state':'not_attempted','external_write_attempts':0,'complete_library_claimed':False,**counts}
        if not state['pending_by_id']:
            return {'status':'queue_partial' if state.get('source_state_holds') else 'queue_empty','source_state_unprojected_receipts':len(state.get('source_state_holds',{})),'queued':0,'source_chain_complete':True,'journal_sha256':sha,'write_state':'not_attempted','external_write_attempts':0,'complete_library_claimed':False,**counts}
        first=state['pending_by_id'][state['rotation_order'][0]]
        source_loader.load_queue_publication(local,remote,token,root,{k:first[k] for k in ('commit','parent','receipt_sha256')},policy_hash)
        pub=outbox.QueuePublication(local.vault,state,sha,max_records=queue_limit(state));scope={}
        receipts=state['engine_state']['receipts']
        if receipts:ds=next(iter(receipts.values()))['data_source_id']
        else:ds=next(iter(transport.native.collect(request)['snapshots'].values()))['parent_data_source']
    image_adapter=getattr(pub,'image_adapter',None) or preserved_image.Adapter(pub,request)
    policy=engine_policy(request,ds,scope,apply)
    create_scope=None
    if e.creation_properties.eligible(pub) and not (pending_journal is not None
            and run53_recovery.allows(pending_journal['plan'], pending_journal)):
        create_scope=deepcopy(e.creation_properties.SCOPE)
        policy['property_create_scope_sha256']=e.digest(create_scope)
        if e.properties.REGISTRY in pub.descriptor['content_tree']:
            source_loader.Loader(local,remote,token,root,policy_hash).blob(pub.descriptor['commit'],e.properties.REGISTRY,16*1024*1024)
    incremental_scope=incremental_scope_for(pub,pending_journal)
    if incremental_scope is not None:
        policy['property_incremental_scope_sha256']=e.digest(incremental_scope)
    upload_attempts_before=len(getattr(request,'attempted',set()))
    pub,png_resolver,image_adapter,attachment_provider,page_guard,png_preparation=png_context(local,remote,root,store,request,pub,policy,policy_hash,apply,writers_guard,image_adapter,loaded_assets)
    if png_preparation is not None and not apply:
        return {'status':'png_prepared','png_preparation':png_preparation,'write_state':'not_attempted','external_write_attempts':0,'complete_library_claimed':False,**counts}
    upload_attempts=len(getattr(request,'attempted',set()))-upload_attempts_before
    state,sha=store.read()
    adapter=outbox.EngineStore(store,attachment_provider=attachment_provider)
    def settle(plan,approved):return outbox.settle_empty_plan(store,sha,plan,approved)
    result=pipeline.run(pub,adapter,request,policy,e.digest(policy),apply=apply,writers_guard=page_guard,initial_scope=scope,empty_plan_settle=settle,create_recovery=create_recovery,approved_recovery_sha=approved_recovery_sha,snapshot_adapter=image_adapter,reviewed_png=png_resolver,property_create_scope=create_scope,property_incremental_scope=incremental_scope)
    if png_resolver is not None and apply:
        result={**result,'png_upload_round':upload_attempts>0,'png_upload_attempts':upload_attempts,'png_preparation':png_preparation}
    final,final_sha=store.read()
    return {**result,**counts,'source_state_unprojected_receipts':len(final.get('source_state_holds',{})),'queued':len(final['pending_by_id']),'outer_journal_sha256':final_sha,'source_chain_complete':final['last_enqueued_publication']==final['observed_publication_tip']}


def _safe_result(value):
    if not isinstance(value,dict):return {'contract':CONTRACT,'status':'failed','write_state':'unknown','external_write_attempts':None}
    if e.same(value,canonical_mode_busy_result()):return canonical_mode_busy_result()
    if value.get('status')=='png_prepared':
        report=value.get('png_preparation',{})
        keys=('selected','upload_candidates','blocked','upload_bytes')
        if any(type(report.get(k)) is not int or report[k]<0 or report[k]>(2 if k=='upload_candidates' else 1536000 if k=='upload_bytes' else 3) for k in keys) or report.get('prepared_only') is not True or report.get('remote_writes')!=0 or report.get('journal_writes')!=0:fail('notion_scope')
        reasons=report.get('reason_counts',{})
        if not isinstance(reasons,dict) or any(k not in png_runtime.REASONS or type(v) is not int or v<0 or v>3 for k,v in reasons.items()):fail('notion_scope')
        return {'contract':CONTRACT,'status':'prepared','write_state':'not_attempted','external_write_attempts':0,'png':{k:report[k] for k in keys}|{'reason_counts':reasons},'complete_library_claimed':False}
    if value.get('status')=='failed':
        stages={'policy','writers','source','journal','native_read','prepare','final_guard','execute','queue_settle'}
        trusted_shape=value.get('contract')==pipeline.CONTRACT and value.get('complete_library_claimed') is False
        reason=value.get('reason') if trusted_shape else None;stage=value.get('stage') if trusted_shape else None
        reason=reason if isinstance(reason,str) and reason in pipeline.REASONS else 'other'
        stage=stage if isinstance(stage,str) and stage in stages else 'input'
        known_no_attempt=trusted_shape and value.get('write_state')=='not_attempted' and type(value.get('external_write_attempts')) is int and value['external_write_attempts']==0
        return {'contract':CONTRACT,'status':'failed','stage':stage,'reason':reason,
            'write_state':'not_attempted' if known_no_attempt else 'unknown',
            'unresolved_operations':value.get('unresolved_operations') if type(value.get('unresolved_operations')) is int and 0<=value['unresolved_operations']<=1 else None,
            'unresolved_effects':'unknown',
            'external_write_attempts':0 if known_no_attempt else None,'asset_downloads':value.get('asset_downloads') if type(value.get('asset_downloads')) is int and 0<=value['asset_downloads']<=200 else None,'complete_library_claimed':False}
    base=pipeline.public_report(value)
    if value.get('status') in ('queue_empty','queue_partial'):
        try:
            if type(value['queued']) is not int or not 0<=value['queued']<=2500 or type(value['source_chain_complete']) is not bool or value['write_state']!='not_attempted' or value['external_write_attempts']!=0:raise ValueError()
            e.p.hex_hash(value['journal_sha256'],64)
            base={k:value[k] for k in ('status','queued','source_chain_complete','journal_sha256','write_state','external_write_attempts')}
        except Exception:return {'contract':CONTRACT,'status':'failed','write_state':'unknown','external_write_attempts':None}
    if base.get('status')=='failed':return base
    if value.get('execution_status') in ('verified','partial','blocked'):
        base['execution_status']=value['execution_status']
        reason=value.get('execution_reason')
        base['execution_reason']=reason if reason is None or isinstance(reason,str) and reason in e.REASONS else 'other'
    if 'source_state_unprojected_receipts' in value:
        count=value['source_state_unprojected_receipts']
        if type(count) is not int or not 0<=count<=e.metadata_pending.MAX_SOURCE_STATE_HOLDS:fail('notion_scope')
        base['source_state_unprojected_receipts']=count
    for key,cap in (('initial_notes',2),('initial_range_candidates',2),('queued',2500)):
        if type(value.get(key)) is not int or not 0<=value[key]<=cap:return {'contract':CONTRACT,'status':'failed','write_state':'unknown','external_write_attempts':None}
        base[key]=value[key]
    for key in ('unresolved_operations','quarantine_transitions','quarantine_candidates'):
        count=value.get(key,0)
        if type(count) is not int or not 0<=count<=1:fail('notion_scope')
        base[key]=count
    if value.get('unresolved_effects','none') not in ('none','unknown'):fail('notion_scope')
    base['unresolved_effects']=value.get('unresolved_effects','none')
    if 'outer_journal_sha256' in value:e.p.hex_hash(value['outer_journal_sha256'],64);base['outer_journal_sha256']=value['outer_journal_sha256']
    base.update(contract=CONTRACT,complete_library_claimed=False,get_requests=0)
    if 'asset_downloads' not in base:base['asset_downloads']=0
    return base


def safe_png_diagnostic(value):
    try:
        if not isinstance(value,dict):raise ValueError()
        out={}
        if 'last_confirmed_upload_state' in value:
            if value['last_confirmed_upload_state']!='unknown':raise ValueError()
            out['last_confirmed_upload_state']='unknown'
        for key,allowed in (('last_confirmed_upload_phase_counts',{'prepared','create_in_flight','upload_id_saved','send_in_flight','uploaded_verified'}),('last_confirmed_post_http_counts',{str(n) for n in range(400,600)})):
            if key in value:
                rows=value[key]
                if not isinstance(rows,dict) or any(k not in allowed or type(v) is not int or not 0<=v<=3 for k,v in rows.items()) or sum(rows.values())>3:raise ValueError()
                out[key]=dict(rows)
        if 'external_write_attempts_observed' in value:
            count=value['external_write_attempts_observed']
            if type(count) is not int or not 0<=count<=2000:raise ValueError()
            out['external_write_attempts_observed']=count
        return out
    except Exception:return {'last_confirmed_upload_state':'unknown'}


def safe_result(value):
    result=_safe_result(value)
    if isinstance(value,dict) and isinstance(value.get('png_preparation'),dict):
        prep=_safe_result({'status':'png_prepared','png_preparation':value['png_preparation']})
        result['png']=prep['png']
    if isinstance(value,dict) and 'png_upload_attempts' in value:
        count=value['png_upload_attempts']
        if type(count) is not int or not 0<=count<=6:fail('notion_scope')
        result['png_upload_attempts']=count
        if count:
            previous=result.get('external_write_attempts')
            result['external_write_attempts']=previous+count if type(previous) is int else None
            if result.get('write_state')!='confirmed':result['write_state']='unknown'
    return result


def recovery_mode_guard(state, found, modes):
    """An authenticated canonical continuation never doubles as another mode."""
    pending = state['engine_state']['journal']
    if (pending is not None and any(mode is not None for mode in modes)
            and (found.get('recovery_anchor') in (run53_recovery.TAG, run57_recovery.TAG)
                or run53_recovery.allows(pending['plan'], pending)
                or run57_recovery.allows(pending['plan'], pending))):
        fail('notion_journal_busy')


def run(token,request,writers_guard,*,expected_parent=None,apply=False,public_policy=None,
        approved_public_policy_sha256=None,publication_bytes=None,execution=None,
        public_client=None,checkpoint_stream=None,work=None,create_recovery=None,approved_recovery_sha=None,
        property_manifest=None,approved_property_manifest_sha256=None,
        historical_manifest=None,approved_historical_manifest_sha256=None,
        historical_capture=None,approved_historical_capture_sha256=None,compact_proofs=False,fixed_source=False):
    """Real private read/CAS adapter. Default path cannot persist a journal."""
    stage='input';write_possible=False;prior_effects=None
    try:
        if type(apply) is not bool:fail('notion_scope')
        fixed_selection = fixed_auth.admit(fixed_source, publication_bytes, apply=apply,
            historical_manifest=historical_manifest, historical_capture=historical_capture,
            manifest_sha=approved_historical_manifest_sha256, capture_sha=approved_historical_capture_sha256,
            property_manifest=property_manifest, recovery=create_recovery)
        modes=(property_manifest,historical_manifest,historical_capture)
        if sum(mode is not None for mode in modes)>1 or any(mode is not None for mode in modes) and (create_recovery is not None or approved_recovery_sha is not None or public_policy is None):fail('notion_scope')
        if historical_capture is not None:
            historical_input.validate_capture_selection(historical_capture,approved_historical_capture_sha256,apply=apply)
        elif approved_historical_capture_sha256 is not None:fail('notion_scope')
        if historical_manifest is not None:
            historical_input.validate_apply_selection(historical_manifest,approved_historical_manifest_sha256,apply=apply)
        elif approved_historical_manifest_sha256 is not None:fail('notion_scope')
        if property_manifest is not None:
            property_runtime.authorize(property_manifest,approved_property_manifest_sha256,apply=apply)
        elif approved_property_manifest_sha256 is not None:fail('notion_scope')
        if apply and (not PRODUCTION_ACTIVATED or not recurring.PRODUCTION_ACTIVATED):fail('notion_activation_pending')
        if expected_parent is not None:a.checked(expected_parent,40)
        client=public_client if public_client is not None else provenance.AnonymousGithub()
        public_hash=None;tip=None
        if apply or public_policy is not None:
            stage='policy'
            public_policy=recurring.validate_policy(public_policy,approved_public_policy_sha256,entry.BOOTSTRAP)
            public_hash=a.sha(a.encoded(public_policy))
        if apply:
            if execution is None or checkpoint_stream is None:fail('notion_context')
            recurring.context(public_policy,execution)
            provenance.verify_execution(client,public_policy,execution,'schedule' if os.environ.get('GITHUB_EVENT_NAME')=='schedule' else 'manual')
            if not fixed_source:
                record=provenance.parse_record(publication_bytes,public_policy,{'id':execution['run_id'],'run_attempt':execution['run_attempt'],'head_sha':execution['controller_sha']})
                tip={'commit':record['receipt_commit'],'receipt_sha256':record['receipt_sha256'],'sequence':record['receipt_sequence']}
        stage='writers';writers_guard()
        remote=m.Remote(None,token);parent=m.guard(remote)
        if isinstance(client, provenance._PublicGithub):
            client.private_proof_source=private_proof.PrivateRecords(remote)
        if fixed_source:
            stage='source'
            fixed_auth.reserve_publication_slot(client, public_policy, execution)
        if expected_parent is not None and parent!=expected_parent:fail('notion_source_changed')
        initial_get_tree=remote.source_get_tree(parent)
        with tempfile.TemporaryDirectory(prefix='get-notion-runtime-',dir=work) as folder:
            root=Path(folder);stage='clone'
            local=r.readback_checkout(root/'objects',parent,token);remote.local=local
            remote=journal.ImmutableJournalRemote(remote)
            # Private fresh metadata-only clone, local refs only; source files
            # and inherited asset blobs are never checked out or downloaded.
            local.run('update-ref',r.MAIN_REF,parent)
            loader=checkpoints.ExactJournalLoader(local.vault,remote)
            loader(parent);reader=a.OfflineGit(local.vault)
            def source_guard(head=None):
                current=m.guard(remote)
                if head is not None and current!=head:fail('notion_source_changed')
                if remote.source_get_tree(current)!=initial_get_tree:fail('notion_source_changed')
                return True
            def guarded_writers():source_guard();return writers_guard()
            stage='journal'
            present=outbox.STATE_PATH in reader.tree(parent)
            if present:
                if public_policy is None:fail('notion_state_anchor')
                found=checkpoints.discover_anchor(client,public_policy=public_policy,approved_public_policy_sha256=public_hash,
                    notion_policy_sha256=e.digest(scope_for(public_policy)),exclude_run_id=execution['run_id'] if execution else None,
                    exclude_run_attempt=execution['run_attempt'] if execution else None)
                anchor=found['anchor']
            else:
                anchor={'commit':entry.BOOTSTRAP['commit'],'state_blob':None,'state_sha256':outbox.digest(outbox.initial_state())}
            confirm=lambda receipt:False
            if apply:
                confirm=checkpoints.CheckpointEmitter(checkpoint_stream,compact=compact_proofs,context={**execution,
                    'rules_sha256':public_policy['rules_sha256'],'notion_policy_sha256':e.digest(scope_for(public_policy)),
                    'public_policy_sha256':public_hash})
            if present and found.get('recovery_anchor') == historical_active.PREPARED_TAG:
                # Keep original checkpoint authority untouched; adopt only the
                # independently verified exact prepared successor as read anchor.
                anchor=historical_active.verify_prepared_successor(local,remote,loader,parent,found)
            post_cas_binding=historical_active.prepared_binding()
            post_cas_paths=() if post_cas_binding is None else tuple(sorted({
                path for row in post_cas_binding['known_descendants'] for path in row['changes']}))
            durable=journal.RemoteJournal(local.vault,remote,anchor=anchor,load_commit=loader,guard=source_guard,confirm=confirm,
                post_cas_paths=post_cas_paths,post_cas_mirror=True)
            current,sha=durable.read()
            if present and found.get('recovery_anchor')=='exact_uncertain_transaction_v1' and not plan_recovery.validate_initial_state(current,sha):
                fail('notion_state_anchor')
            if all(mode is None for mode in modes):
                busy=canonical_mode_guard(current,sha)
                if busy is not None:return busy
            old_journal=current['engine_state']['journal']
            recovery_mode_guard(current, found if present else {}, modes)
            historical_active.check_mode(current, found if present else {}, historical_manifest,
                approved_historical_manifest_sha256, (property_manifest, historical_capture))
            historical_bridge_loader=None
            if fixed_source:
                # No old active-plan bridge or journal-selected source fallback.
                fixed_auth.check_state(current, found if present else {})
                historical_bridge_loader=fixed_auth.verify_sources(local, remote, token, root, parent,
                    fixed_selection, public_policy['get_policy_sha256'])
                source_guard(parent); writers_guard()
                fixed_auth.emit(checkpoint_stream, {**execution,
                    'rules_sha256':public_policy['rules_sha256'],
                    'notion_policy_sha256':e.digest(scope_for(public_policy)),
                    'public_policy_sha256':public_hash}, fixed_selection)
            prior_effects=bool(old_journal and (old_journal.get('index') or old_journal.get('operation') and e.operation_has_effects(old_journal['operation'])))
            store=durable if apply else ReadOnlyStore(current)
            # The separate authenticated idle predecessor bridge is deliberately
            # before every mode return. It grants no pending-plan or input scope.
            if present and found.get('recovery_anchor')==idle_recovery.TAG:
                write_possible=apply
                candidate=idle_recovery.transition_outer(current,sha,
                    public_policy['get_policy_sha256'],proof=found)
                if apply:current,sha=outbox.confirmed_compare_and_set(durable,sha,candidate)
                else:store=ReadOnlyStore(candidate);current,sha=store.read()
            if present and found.get('recovery_anchor') in (historical_active.TAG,historical_active.PREPARED_TAG):
                # Complete source/native read-only checks before the FIRST CAS.
                bridge_materialized=historical_runtime.verify_active_transition(local, remote, token, root, current, sha, request,
                    manifest=historical_manifest, expected=approved_historical_manifest_sha256,
                    policy_hash=public_policy['get_policy_sha256'], writers_guard=guarded_writers)
                historical_bridge_loader=bridge_materialized['loader']
                candidate=historical_active.transition_outer(current,sha,
                    public_policy['get_policy_sha256'],proof=found)
                write_possible=apply
                if apply: current,sha=outbox.confirmed_compare_and_set(durable,sha,candidate)
                else: store=ReadOnlyStore(candidate);current,sha=store.read()
            if property_manifest is not None:
                stage='execute';write_possible=apply
                result=property_runtime.dispatch(local,remote,token,root,store,request,
                    manifest=property_manifest,expected=approved_property_manifest_sha256,
                    policy_hash=public_policy['get_policy_sha256'],apply=apply,writers_guard=guarded_writers)
                source_guard()
                if apply and durable.anchor['state_blob'] is not None:
                    verified,verified_sha=durable.read()
                    if durable.compare_and_set(verified_sha,verified)!=verified_sha:fail('notion_state_anchor')
                safe=safe_result(result)
                safe.update(journal_commits=durable.commits_attempted,source_parent=parent,
                    notion_requests=getattr(request,'attempts',0),production_activated=PRODUCTION_ACTIVATED)
                return safe
            if historical_capture is not None:
                stage='execute';write_possible=apply
                def capture_refresh():
                    source_guard()
                    if apply and durable.anchor['state_blob'] is not None:
                        verified,verified_sha=durable.read()
                        if verified['engine_state']['journal'] is not None:fail('journal_busy')
                        if durable.compare_and_set(verified_sha,verified)!=verified_sha:fail('notion_state_anchor')
                if historical_capture.get('contract')==historical.CAPTURE_COLLECTION_CONTRACT:
                    result=historical_runtime.capture_group(local,remote,token,root,store,request,
                        selection=historical_capture,expected=approved_historical_capture_sha256,
                        policy_hash=public_policy['get_policy_sha256'],apply=apply,writers_guard=guarded_writers,
                        durable=durable,emitter=confirm,refresh=capture_refresh,loader=historical_bridge_loader)
                else:
                    result=historical_runtime.capture(local,remote,token,root,store,request,
                        candidate=historical_capture,expected=approved_historical_capture_sha256,
                        policy_hash=public_policy['get_policy_sha256'],apply=apply,writers_guard=guarded_writers,loader=historical_bridge_loader)
                    capture_refresh()
                result.update(journal_commits=durable.commits_attempted,source_parent=parent,
                    notion_requests=getattr(request,'attempts',0),production_activated=PRODUCTION_ACTIVATED)
                return result
            if historical_manifest is not None:
                stage='execute';write_possible=apply
                if historical_manifest.get('contract')==historical.APPLY_COLLECTION_CONTRACT:
                    def apply_refresh():
                        source_guard()
                        verified,verified_sha=durable.read()
                        if verified['engine_state']['journal'] is not None:fail('journal_busy')
                        if durable.compare_and_set(verified_sha,verified)!=verified_sha:fail('notion_state_anchor')
                    result=historical_runtime.apply_group(local,remote,token,root,store,request,
                        selection=historical_manifest,expected=approved_historical_manifest_sha256,
                        policy_hash=public_policy['get_policy_sha256'],apply=apply,writers_guard=guarded_writers,
                        durable=durable,emitter=confirm,refresh=apply_refresh,loader=historical_bridge_loader)
                    result.update(source_parent=parent,production_activated=PRODUCTION_ACTIVATED)
                    return result
                result=historical_runtime.dispatch(local,remote,token,root,store,request,
                    manifest=historical_manifest,expected=approved_historical_manifest_sha256,
                    policy_hash=public_policy['get_policy_sha256'],apply=apply,writers_guard=guarded_writers,
                    loader=historical_bridge_loader)
                source_guard()
                if apply and durable.anchor['state_blob'] is not None:
                    verified,verified_sha=durable.read()
                    if durable.compare_and_set(verified_sha,verified)!=verified_sha:fail('notion_state_anchor')
                safe=safe_result(result)
                safe.update(journal_commits=durable.commits_attempted,source_parent=parent,
                    notion_requests=getattr(request,'attempts',0),production_activated=PRODUCTION_ACTIVATED)
                if old_journal is not None and historical_active.allows(old_journal['plan'],old_journal):
                    safe.update(resumed_prior_plan_sha256=old_journal['plan_sha256'],
                        resumed_prior_rules_sha256=old_journal['plan']['rules_sha256'])
                return safe
            loaded_assets=None
            png_transition=None
            preload_state=current;preload_sha=sha
            if present and found.get('recovery_anchor') in (png_recovery.TAG,run15_recovery.TAG):
                bridge=run15_recovery if found['recovery_anchor']==run15_recovery.TAG else png_recovery
                png_transition=bridge.transition_outer(current,sha,public_policy['get_policy_sha256'])
                preload_state=png_transition;preload_sha=outbox.digest(png_transition)
            confirmed_transition=None
            if present and found.get('recovery_anchor') in (run18_recovery.TAG,run28_recovery.TAG,run32_recovery.TAG,run53_recovery.TAG,run57_recovery.TAG):
                recovery_module={module.TAG:module for module in (run18_recovery,run28_recovery,run32_recovery,run53_recovery,run57_recovery)}[found['recovery_anchor']]
                confirmed_transition=recovery_module.transition_outer(current,sha,public_policy['get_policy_sha256'])
                preload_state=confirmed_transition;preload_sha=outbox.digest(preload_state)
            expired_transition=present and found.get('recovery_anchor')==run16_recovery.TAG
            if expired_transition:
                if sha!=expired_recovery.STATE_SHA or sha!=outbox.digest(current):fail('notion_state_anchor')
                preload_state=deepcopy(current)
                preload_state['policy_sha256']=public_policy['get_policy_sha256']
                preload_sha=outbox.digest(preload_state)
            if tip is not None and preload_state['engine_state']['journal'] is None:
                origin={'commit':entry.BOOTSTRAP['commit'],'receipt_sha256':None,'sequence':0}
                preview_chain=source_loader.load_confirmed_chain(local,remote,token,root,tip,origin,preload_state['last_enqueued_publication'] or origin,public_policy['get_policy_sha256'])
                preload_state=outbox.enqueue(local.vault,preload_state,preload_sha,preview_chain,outbox.digest(preview_chain))
                preload_sha=outbox.digest(preload_state)
            batch=png_batch_ids(preload_state)
            pending_plan=preload_state['engine_state']['journal']
            batch_descriptor=pending_plan['plan']['publication'] if pending_plan is not None else {'commit':preload_state['pending_by_id'][batch[0]]['commit'] if batch else None,'records':{i:preload_state['pending_by_id'][i] for i in batch}}
            png_pending=png_selected(batch_descriptor)
            if png_pending:
                selected_anchor,selected_anchor_sha=png_anchor(batch_descriptor)
                # Hydrate immutable source/asset evidence before our FIRST CAS.
                # The trusted transition candidate selects the current policy bridge.
                pending=preload_state['engine_state']['journal']
                if pending is not None:
                    desc=pending['plan']['publication']
                    row={k:desc[k] for k in ('commit','parent')}|{'receipt_sha256':desc['outbox_receipt_sha256']}
                else:
                    first=preload_state['pending_by_id'][batch[0]]
                    row={k:first[k] for k in ('commit','parent','receipt_sha256')}
                source_loader.load_queue_publication(local,remote,token,root,row,preload_state['policy_sha256'])
                before_pub=outbox.QueuePublication(local.vault,preload_state,preload_sha,resume=pending is not None,max_records=queue_limit(preload_state))
                loaded_assets=png_loader.load(local,remote,before_pub.descriptor,selected_anchor,selected_anchor_sha,preload_state['policy_sha256'])
            if present and found.get('recovery_anchor')=='exact_run10_acknowledged_create_v1':
                if not plan_recovery.validate_run10_state(current,sha):fail('notion_state_anchor')
                candidate=plan_recovery.transition_run10_outer(current,sha,public_policy['get_policy_sha256'])
                if apply:
                    current,sha=outbox.confirmed_compare_and_set(durable,sha,candidate)
                else:
                    store=ReadOnlyStore(candidate);current,sha=store.read()
            if present and found.get('recovery_anchor') in (png_recovery.TAG,run15_recovery.TAG):
                candidate=png_transition
                if candidate is None:fail('notion_state_anchor')
                if apply:current,sha=outbox.confirmed_compare_and_set(durable,sha,candidate)
                else:store=ReadOnlyStore(candidate);current,sha=store.read()
            if confirmed_transition is not None:
                if apply:current,sha=outbox.confirmed_compare_and_set(durable,sha,confirmed_transition)
                else:store=ReadOnlyStore(confirmed_transition);current,sha=store.read()
            if expired_transition:
                if loaded_assets is None:fail('png_anchor_pending')
                observe=lambda:observe_expired_png(current,loaded_assets,before_pub,request,guarded_writers)
                if apply:
                    current,sha=durable.compare_and_set_png_expired(sha,public_policy['get_policy_sha256'],observe)
                else:
                    candidate=expired_recovery.transition(current,new_policy_sha256=public_policy['get_policy_sha256'],evidence=observe())
                    store=ReadOnlyStore(candidate);current,sha=store.read()
            stage='source'
            if tip is not None:
                chain_origin={'commit':entry.BOOTSTRAP['commit'],'receipt_sha256':None,'sequence':0}
                # Verify the current Get marker before any initial Notion call.
                # No successful old source payload is requested again.
                source_loader.load_confirmed_chain(local,remote,token,root,tip,chain_origin,current['last_enqueued_publication'] or chain_origin,public_policy['get_policy_sha256'])
            elif initial_done(current) or plan_recovery.validate_initial_state(current,sha):
                if public_policy is None:fail('notion_marker')
                found=provenance.discover(client,public_policy)
                tip={**found['anchor'],'sequence':found['receipt_sequence']}
            stage='execute';write_possible=apply
            result=dispatch(local,remote,token,root,store,request,tip=tip,
                policy_hash=public_policy['get_policy_sha256'] if public_policy else '0'*64,
                apply=apply,writers_guard=guarded_writers,create_recovery=create_recovery,approved_recovery_sha=approved_recovery_sha,loaded_assets=loaded_assets)
            stage='final';source_guard()
            # Refresh the separately verified Actions anchor even on a no-op.
            # Identical bytes emit a verified cas_attempts=0 checkpoint; this
            # does not create a Git commit or repeat any Notion request.
            if apply and durable.anchor['state_blob'] is not None:
                verified,verified_sha=durable.read()
                if durable.compare_and_set(verified_sha,verified)!=verified_sha:fail('notion_state_anchor')
            safe=safe_result(result)
            safe.update(journal_commits=durable.commits_attempted,source_parent=parent,
                notion_requests=getattr(request,'attempts',0),production_activated=PRODUCTION_ACTIVATED)
            if expired_transition:
                safe['replaced_prior_plan_sha256']=expired_recovery.PLAN_SHA
                safe['replacement_generation']=1
            elif old_journal is not None and plan_recovery.allows(old_journal['plan'],old_journal):
                # Current code/controller/policy remain the actual new values
                # in every checkpoint; this identifies the unchanged prior plan.
                safe['isolated_prior_plan_sha256' if result.get('quarantine_candidates') else 'resumed_prior_plan_sha256']=plan_recovery.OLD_PLAN
                safe['resumed_prior_rules_sha256']=plan_recovery.OLD_ENGINE_RULES
            elif old_journal is not None and plan_recovery.allows_run10(old_journal['plan'],old_journal):
                safe['resumed_prior_plan_sha256']=plan_recovery.RUN10_PLAN
                safe['resumed_prior_rules_sha256']=plan_recovery.RUN10_ENGINE_RULES
            elif old_journal is not None and run15_recovery.allows(old_journal['plan'],old_journal):
                safe['resumed_prior_plan_sha256']=run15_recovery.PLAN_SHA
                safe['resumed_prior_rules_sha256']=run15_recovery.ENGINE_RULES
            elif old_journal is not None and run53_recovery.allows(old_journal['plan'],old_journal):
                safe['resumed_prior_plan_sha256']=run53_recovery.PLAN_SHA
                safe['resumed_prior_rules_sha256']=run53_recovery.ENGINE_RULES
            elif old_journal is not None and run57_recovery.allows(old_journal['plan'],old_journal):
                safe['resumed_prior_plan_sha256']=run57_recovery.PLAN_SHA
                safe['resumed_prior_rules_sha256']=run57_recovery.ENGINE_RULES
            return safe
    except Exception as exc:
        return {'contract':CONTRACT,'status':'failed','stage':stage,**failure_diagnostic(exc),**safe_png_diagnostic(getattr(exc,'png_diagnostic',{})),
            'write_state':'unknown' if write_possible or prior_effects is not False else 'not_attempted',
            'external_write_attempts':None if write_possible or prior_effects is not False else 0,'asset_downloads':getattr(exc,'asset_downloads',None) if type(getattr(exc,'asset_downloads',None)) is int and 0<=exc.asset_downloads<=200 else None,'complete_library_claimed':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',type=Path);parser.add_argument('--approved-policy-sha256')
    source=parser.add_mutually_exclusive_group()
    source.add_argument('--publication-file',type=Path)
    source.add_argument('--fixed-source',action='store_true')
    parser.add_argument('--expected-parent')
    parser.add_argument('--recovery',type=Path);parser.add_argument('--approved-recovery-sha256')
    parser.add_argument('--property-manifest',type=Path);parser.add_argument('--approved-property-manifest-sha256')
    parser.add_argument('--historical-manifest',type=Path);parser.add_argument('--approved-historical-manifest-sha256')
    parser.add_argument('--historical-capture',type=Path);parser.add_argument('--approved-historical-capture-sha256')
    parser.add_argument('--apply',action='store_true');parser.add_argument('--scope-from-policy',action='store_true')
    args=parser.parse_args(argv)
    try:
        # Pending production activation is rejected before credential lookup.
        if args.apply and (not PRODUCTION_ACTIVATED or not recurring.PRODUCTION_ACTIVATED):fail('notion_activation_pending')
        if args.apply!=args.scope_from_policy or (not args.apply and (args.publication_file or args.fixed_source)):fail('notion_scope')
        policy=None;execution=None;raw=None;recovery=None;property_manifest=None;historical_manifest=None;historical_capture=None
        modes=(args.property_manifest,args.historical_manifest,args.historical_capture)
        if sum(mode is not None for mode in modes)>1 or any(mode is not None for mode in modes) and (args.recovery or args.approved_recovery_sha256 or not args.policy):fail('notion_scope')
        if bool(args.historical_capture)!=bool(args.approved_historical_capture_sha256):fail('notion_scope')
        if bool(args.historical_manifest)!=bool(args.approved_historical_manifest_sha256):fail('notion_scope')
        if args.historical_capture:
            capture_limit=historical.MAX_CAPTURE_SCOPES*256*1024+1024
            historical_capture=m.strict_capture_json(a.bounded_file(args.historical_capture,capture_limit),capture_limit)
            historical_input.validate_capture_selection(historical_capture,args.approved_historical_capture_sha256,apply=args.apply)
        if args.historical_manifest:
            apply_limit=historical.MAX_APPLY_SCOPES*256*1024+1024
            historical_manifest=m.strict_capture_json(a.bounded_file(args.historical_manifest,apply_limit),apply_limit)
            historical_input.validate_apply_selection(historical_manifest,args.approved_historical_manifest_sha256,apply=args.apply)
        if bool(args.property_manifest)!=bool(args.approved_property_manifest_sha256) or args.property_manifest and (args.recovery or not args.policy):fail('notion_scope')
        if args.property_manifest:
            property_manifest=m.strict_capture_json(a.bounded_file(args.property_manifest,256*1024),256*1024)
            property_runtime.authorize(property_manifest,args.approved_property_manifest_sha256,apply=args.apply)
        if bool(args.recovery)!=bool(args.approved_recovery_sha256) or args.recovery and not args.apply:fail('notion_scope')
        if args.recovery:
            recovery=m.strict_capture_json(a.bounded_file(args.recovery,16384),16384)
            if e.digest(recovery)!=args.approved_recovery_sha256:fail('notion_scope')
        if args.policy:policy=m.strict_capture_json(a.bounded_file(args.policy,16384),16384)
        if args.apply:
            if args.policy is None or (args.publication_file is None and not args.fixed_source):fail('notion_scope')
            if os.environ.get('GITHUB_REPOSITORY')!=provenance.REPOSITORY or os.environ.get('GITHUB_WORKFLOW_REF')!=provenance.REPOSITORY+'/'+policy['workflow_path']+'@refs/heads/'+policy['head_branch'] or os.environ.get('GITHUB_REF')!='refs/heads/'+policy['head_branch'] or os.environ.get('GITHUB_EVENT_NAME') not in ('schedule','workflow_dispatch'):fail('notion_context')
            execution={'run_id':int(os.environ.get('GITHUB_RUN_ID','0')),'run_attempt':int(os.environ.get('GITHUB_RUN_ATTEMPT','0')),'code_sha':recurring.executed_code_sha(),'controller_sha':os.environ.get('GITHUB_SHA')}
            raw=a.bounded_file(args.publication_file,8192) if args.publication_file else None
        elif args.expected_parent is None:fail('notion_scope')
        request=transport.Transport(os.environ.get('NOTION_API_KEY'))
        github_token=os.environ.get('GITHUB_TOKEN')
        public_client=provenance.PublicGithub(github_token)
        guard=old_writers.WriterGuard(github_token)
        result=run(os.environ.get('PRIVATE_VAULT_TOKEN'),request,guard,expected_parent=args.expected_parent,
            apply=args.apply,public_policy=policy,approved_public_policy_sha256=args.approved_policy_sha256,
            publication_bytes=raw,execution=execution,public_client=public_client,checkpoint_stream=sys.stdout if args.apply else None,create_recovery=recovery,approved_recovery_sha=args.approved_recovery_sha256,property_manifest=property_manifest,approved_property_manifest_sha256=args.approved_property_manifest_sha256,
            historical_manifest=historical_manifest,approved_historical_manifest_sha256=args.approved_historical_manifest_sha256,
            historical_capture=historical_capture,approved_historical_capture_sha256=args.approved_historical_capture_sha256,compact_proofs=True,fixed_source=args.fixed_source)
    except Exception as exc:
        result={'contract':CONTRACT,'status':'failed','stage':'input',**failure_diagnostic(exc),'write_state':'unknown' if args.apply else 'not_attempted','external_write_attempts':None if args.apply else 0}
    import get_private_runtime_report as report
    print(json.dumps(report.public(result, DIAGNOSTIC_ERRORS, allowed_stages=STAGES, allowed_execution_reasons=e.REASONS)),flush=True)
    return exit_status(result)


def exit_status(result):
    if not isinstance(result,dict):return 1
    if result.get('contract')==historical_runtime.APPLY_GROUP_RESULT_CONTRACT:
        selected=result.get('scopes_selected')
        return 0 if (result.get('status')=='applied' and type(selected) is int
            and 1<=selected<=historical.MAX_APPLY_SCOPES
            and result.get('scopes_completed')==selected and result.get('scopes_remaining')==0
            and len(result.get('members',[]))==selected and result.get('reason') is None
            and result.get('journal_write_state')=='confirmed'
            and result.get('write_state') in ('confirmed','not_attempted')) else 1
    if result.get('contract')==historical_runtime.GROUP_RESULT_CONTRACT:
        selected=result.get('scopes_selected')
        return 0 if (result.get('status')=='captured' and type(selected) is int
            and 1<=selected<=historical.MAX_CAPTURE_SCOPES
            and result.get('scopes_completed')==selected and result.get('scopes_remaining')==0
            and len(result.get('members',[]))==selected and result.get('reason') is None
            and result.get('journal_write_state')=='confirmed'
            and result.get('notion_write_attempts')==0) else 1
    if result.get('contract')=='get-notion-historical-capture-result-1' and result.get('status') in ('captured','prepared') and result.get('notion_write_attempts')==0:return 0
    if result.get('status') in ('prepared','queue_empty','queue_partial'):
        return 0 if result.get('write_state')=='not_attempted' else 1
    if result.get('status')=='executed':
        execution=result.get('execution')
        if isinstance(execution,dict) and execution.get('status') in ('verified','partial') and result.get('write_state') in ('confirmed','not_attempted'):
            return 0
    return 1


if __name__=='__main__':raise SystemExit(main())
