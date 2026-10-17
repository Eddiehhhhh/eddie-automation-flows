"""Bounded Git-confirmed Notion runtime. Preparation is read-only by default.

The active controller, source publication and journal each have separate
external anchors. Native content remains private in memory/journal. A new
runtime never trusts a current state merely because it can hash that state.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import get_notion_executor as e
import get_notion_outbox as outbox
import get_notion_pipeline as pipeline
import get_notion_live_transport as transport
import get_notion_readonly as ro
import get_notion_preserved_image as preserved_image
import get_notion_writer_guard as old_writers
import get_notion_remote_journal as journal
import get_notion_journal_runtime as checkpoints
import get_confirmed_source as source_loader
import get_recurring_entry as entry
import get_recurring_runtime as recurring
import get_publication_provenance as provenance
from get_recurring_entry import a,m,r

PRODUCTION_ACTIVATED=True
CONTRACT='get-notion-standing-runtime-1'
MAX_RUN_RECORDS=10
STAGES={'input','policy','writers','clone','journal','source','initial','queue','execute','final'}
ERRORS={'notion_activation_pending','notion_context','notion_scope','notion_state_anchor','notion_source_changed','notion_initial_source','notion_schema','notion_marker','notion_failed','notion_journal_busy','writer_guard_permission','writer_guard_quota','writer_gate_unknown','journal_provenance_missing','asset_permission_denied','permission_denied'}


def fail(code):raise ValueError(code) from None


class ReadOnlyStore:
    def __init__(self,value):outbox.check_state(value);self.value=deepcopy(value)
    def read(self):return deepcopy(self.value),outbox.digest(self.value)
    def compare_and_set(self,*args):fail('notion_scope')


def scope_for(public_policy):
    return {'contract':CONTRACT,'public_policy_sha256':a.sha(a.encoded(public_policy)),
        'rules_sha256':public_policy['rules_sha256'],'repository':r.REPOSITORY,
        'journal_path':outbox.STATE_PATH,'max_notes':MAX_RUN_RECORDS,'allow_create':True,
        'allow_archive_owned_roots':True,'property_updates':False,'uploads':False,
        'data_source_pin':transport.native.DATA_SOURCE_PIN,'database_pin':transport.native.DATABASE_PIN,
        'initial_proof_sha256':ro.HISTORICAL_TEMPLATE['proof_sha256'],
        'initial_image_proof_sha256':preserved_image.TEMPLATE['proof_sha256'],
        'preserve_existing_image_only':True,
        'initial_two_first':True,'unknown_existing_pages':'isolate','plan_authorization':'standing_bounded'}


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
    known=set(state['engine_state']['receipts'])|set(state['engine_state']['failures'])
    return set(transport.native.PAGE_PINS.values()) <= {transport.native.sha(i) for i in known}


def dispatch(local,remote,token,root,store,request,*,tip,policy_hash,apply,writers_guard,initial_factory=initial_inputs,create_recovery=None,approved_recovery_sha=None):
    state,sha=store.read();outbox.check_state(state)
    if sha!=outbox.digest(state):fail('notion_state_anchor')
    pending_journal=state['engine_state']['journal'];counts={'initial_notes':0,'initial_range_candidates':0}
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
            return {'status':'queue_empty','queued':0,'source_chain_complete':True,'journal_sha256':sha,'write_state':'not_attempted','external_write_attempts':0,'complete_library_claimed':False,**counts}
        first=state['pending_by_id'][state['rotation_order'][0]]
        source_loader.load_queue_publication(local,remote,token,root,{k:first[k] for k in ('commit','parent','receipt_sha256')},policy_hash)
        pub=outbox.QueuePublication(local.vault,state,sha,max_records=MAX_RUN_RECORDS);scope={}
        receipts=state['engine_state']['receipts']
        if receipts:ds=next(iter(receipts.values()))['data_source_id']
        else:ds=next(iter(transport.native.collect(request)['snapshots'].values()))['parent_data_source']
    image_adapter=getattr(pub,'image_adapter',None) or preserved_image.Adapter(pub,request)
    policy=engine_policy(request,ds,scope,apply)
    adapter=outbox.EngineStore(store)
    def settle(plan,approved):return outbox.settle_empty_plan(store,sha,plan,approved)
    result=pipeline.run(pub,adapter,request,policy,e.digest(policy),apply=apply,writers_guard=writers_guard,initial_scope=scope,empty_plan_settle=settle,create_recovery=create_recovery,approved_recovery_sha=approved_recovery_sha,snapshot_adapter=image_adapter)
    final,final_sha=store.read()
    return {**result,**counts,'queued':len(final['pending_by_id']),'outer_journal_sha256':final_sha,'source_chain_complete':final['last_enqueued_publication']==final['observed_publication_tip']}


def safe_result(value):
    if not isinstance(value,dict):return {'contract':CONTRACT,'status':'failed','write_state':'unknown','external_write_attempts':None}
    if value.get('status')=='failed':
        stages={'policy','writers','source','journal','native_read','prepare','final_guard','execute','queue_settle'}
        trusted_shape=value.get('contract')==pipeline.CONTRACT and value.get('complete_library_claimed') is False
        reason=value.get('reason') if trusted_shape else None;stage=value.get('stage') if trusted_shape else None
        reason=reason if isinstance(reason,str) and reason in pipeline.REASONS else 'other'
        stage=stage if isinstance(stage,str) and stage in stages else 'input'
        known_no_attempt=trusted_shape and value.get('write_state')=='not_attempted' and type(value.get('external_write_attempts')) is int and value['external_write_attempts']==0
        return {'contract':CONTRACT,'status':'failed','stage':stage,'reason':reason,
            'write_state':'not_attempted' if known_no_attempt else 'unknown',
            'external_write_attempts':0 if known_no_attempt else None,'asset_downloads':value.get('asset_downloads') if type(value.get('asset_downloads')) is int and 0<=value['asset_downloads']<=200 else None,'complete_library_claimed':False}
    base=pipeline.public_report(value)
    if value.get('status') in ('queue_empty','queue_partial'):
        try:
            if type(value['queued']) is not int or not 0<=value['queued']<=2500 or type(value['source_chain_complete']) is not bool or value['write_state']!='not_attempted' or value['external_write_attempts']!=0:raise ValueError()
            e.p.hex_hash(value['journal_sha256'],64)
            base={k:value[k] for k in ('status','queued','source_chain_complete','journal_sha256','write_state','external_write_attempts')}
        except Exception:return {'contract':CONTRACT,'status':'failed','write_state':'unknown','external_write_attempts':None}
    if base.get('status')=='failed':return base
    for key,cap in (('initial_notes',2),('initial_range_candidates',2),('queued',2500)):
        if type(value.get(key)) is not int or not 0<=value[key]<=cap:return {'contract':CONTRACT,'status':'failed','write_state':'unknown','external_write_attempts':None}
        base[key]=value[key]
    if 'outer_journal_sha256' in value:e.p.hex_hash(value['outer_journal_sha256'],64);base['outer_journal_sha256']=value['outer_journal_sha256']
    base.update(contract=CONTRACT,complete_library_claimed=False,get_requests=0)
    if 'asset_downloads' not in base:base['asset_downloads']=0
    return base


def run(token,request,writers_guard,*,expected_parent=None,apply=False,public_policy=None,
        approved_public_policy_sha256=None,publication_bytes=None,execution=None,
        public_client=None,checkpoint_stream=None,work=None,create_recovery=None,approved_recovery_sha=None):
    """Real private read/CAS adapter. Default path cannot persist a journal."""
    stage='input';write_possible=False;prior_effects=None
    try:
        if type(apply) is not bool:fail('notion_scope')
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
            record=provenance.parse_record(publication_bytes,public_policy,{'id':execution['run_id'],'run_attempt':execution['run_attempt'],'head_sha':execution['controller_sha']})
            tip={'commit':record['receipt_commit'],'receipt_sha256':record['receipt_sha256'],'sequence':record['receipt_sequence']}
        stage='writers';writers_guard()
        remote=m.Remote(None,token);parent=m.guard(remote)
        if expected_parent is not None and parent!=expected_parent:fail('notion_source_changed')
        initial_get_tree=remote.source_get_tree(parent)
        with tempfile.TemporaryDirectory(prefix='get-notion-runtime-',dir=work) as folder:
            root=Path(folder);stage='clone'
            local=r.readback_checkout(root/'objects',parent,token);remote.local=local
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
                confirm=checkpoints.CheckpointEmitter(checkpoint_stream,context={**execution,
                    'rules_sha256':public_policy['rules_sha256'],'notion_policy_sha256':e.digest(scope_for(public_policy)),
                    'public_policy_sha256':public_hash})
            durable=journal.RemoteJournal(local.vault,remote,anchor=anchor,load_commit=loader,guard=source_guard,confirm=confirm)
            current,sha=durable.read()
            old_journal=current['engine_state']['journal']
            prior_effects=bool(old_journal and (old_journal.get('index') or old_journal.get('operation') and (old_journal['operation'].get('phase')!='prepared' or old_journal['operation'].get('kind') in ('cleanup','archive'))))
            store=durable if apply else ReadOnlyStore(current)
            stage='source'
            if tip is not None:
                chain_origin={'commit':entry.BOOTSTRAP['commit'],'receipt_sha256':None,'sequence':0}
                # Verify the current Get marker before any initial Notion call.
                # No successful old source payload is requested again.
                source_loader.load_confirmed_chain(local,remote,token,root,tip,chain_origin,current['last_enqueued_publication'] or chain_origin,public_policy['get_policy_sha256'])
            elif initial_done(current):
                if public_policy is None:fail('notion_marker')
                found=provenance.discover(client,public_policy)
                tip={**found['anchor'],'sequence':found['receipt_sequence']}
            stage='execute';write_possible=apply
            result=dispatch(local,remote,token,root,store,request,tip=tip,
                policy_hash=public_policy['get_policy_sha256'] if public_policy else '0'*64,
                apply=apply,writers_guard=guarded_writers,create_recovery=create_recovery,approved_recovery_sha=approved_recovery_sha)
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
            return safe
    except Exception as exc:
        reason=str(exc);reason=reason if reason in ERRORS else 'notion_failed'
        return {'contract':CONTRACT,'status':'failed','stage':stage,'reason':reason,
            'write_state':'unknown' if write_possible or prior_effects is not False else 'not_attempted',
            'external_write_attempts':None if write_possible or prior_effects is not False else 0,'asset_downloads':getattr(exc,'asset_downloads',None) if type(getattr(exc,'asset_downloads',None)) is int and 0<=exc.asset_downloads<=200 else None,'complete_library_claimed':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',type=Path);parser.add_argument('--approved-policy-sha256')
    parser.add_argument('--publication-file',type=Path);parser.add_argument('--expected-parent')
    parser.add_argument('--recovery',type=Path);parser.add_argument('--approved-recovery-sha256')
    parser.add_argument('--apply',action='store_true');parser.add_argument('--scope-from-policy',action='store_true')
    args=parser.parse_args(argv)
    try:
        # Pending production activation is rejected before credential lookup.
        if args.apply and (not PRODUCTION_ACTIVATED or not recurring.PRODUCTION_ACTIVATED):fail('notion_activation_pending')
        if args.apply!=args.scope_from_policy or (not args.apply and args.publication_file):fail('notion_scope')
        policy=None;execution=None;raw=None;recovery=None
        if bool(args.recovery)!=bool(args.approved_recovery_sha256) or args.recovery and not args.apply:fail('notion_scope')
        if args.recovery:
            recovery=m.strict_capture_json(a.bounded_file(args.recovery,16384),16384)
            if e.digest(recovery)!=args.approved_recovery_sha256:fail('notion_scope')
        if args.policy:policy=m.strict_capture_json(a.bounded_file(args.policy,16384),16384)
        if args.apply:
            if args.policy is None or args.publication_file is None:fail('notion_scope')
            if os.environ.get('GITHUB_REPOSITORY')!=provenance.REPOSITORY or os.environ.get('GITHUB_WORKFLOW_REF')!=provenance.REPOSITORY+'/'+policy['workflow_path']+'@refs/heads/'+policy['head_branch'] or os.environ.get('GITHUB_REF')!='refs/heads/'+policy['head_branch'] or os.environ.get('GITHUB_EVENT_NAME') not in ('schedule','workflow_dispatch'):fail('notion_context')
            execution={'run_id':int(os.environ.get('GITHUB_RUN_ID','0')),'run_attempt':int(os.environ.get('GITHUB_RUN_ATTEMPT','0')),'code_sha':recurring.executed_code_sha(),'controller_sha':os.environ.get('GITHUB_SHA')}
            raw=a.bounded_file(args.publication_file,8192)
        elif args.expected_parent is None:fail('notion_scope')
        request=transport.Transport(os.environ.get('NOTION_API_KEY'))
        github_token=os.environ.get('GITHUB_TOKEN')
        public_client=provenance.PublicGithub(github_token)
        guard=old_writers.WriterGuard(github_token)
        result=run(os.environ.get('PRIVATE_VAULT_TOKEN'),request,guard,expected_parent=args.expected_parent,
            apply=args.apply,public_policy=policy,approved_public_policy_sha256=args.approved_policy_sha256,
            publication_bytes=raw,execution=execution,public_client=public_client,checkpoint_stream=sys.stdout if args.apply else None,create_recovery=recovery,approved_recovery_sha=args.approved_recovery_sha256)
    except Exception as exc:
        reason=str(exc);result={'contract':CONTRACT,'status':'failed','stage':'input','reason':reason if reason in ERRORS else 'notion_failed','write_state':'unknown' if args.apply else 'not_attempted','external_write_attempts':None if args.apply else 0}
    print(json.dumps(result),flush=True)
    return exit_status(result)


def exit_status(result):
    if not isinstance(result,dict):return 1
    if result.get('status') in ('prepared','queue_empty','queue_partial'):
        return 0 if result.get('write_state')=='not_attempted' else 1
    if result.get('status')=='executed':
        execution=result.get('execution')
        if isinstance(execution,dict) and execution.get('status') in ('verified','partial') and result.get('write_state') in ('confirmed','not_attempted'):
            return 0
    return 1


if __name__=='__main__':raise SystemExit(main())
