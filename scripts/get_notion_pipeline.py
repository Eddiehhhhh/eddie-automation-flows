"""Bounded Notion orchestration; injected trusted publication/journal/transport.

Default preparation performs reads only. Live writers must supply an externally
approved policy digest, trusted Git and journal anchors, and fresh old-writer
evidence. Plans/snapshots stay private; only fixed counts and hashes are returned.
"""
from copy import deepcopy
from collections import Counter
import get_notion_executor as e
import get_notion_live_transport as t
from get_notion_preserved_image import IMAGE_FAILURES

CONTRACT='get-notion-bounded-pipeline-1'
REASONS=frozenset(e.REASONS)|frozenset(('policy_invalid','policy_changed','scope_changed','permission_denied','read_transport_failed','read_api_failed','wrong_schema','incomplete_pagination','invalid_cohort','response_budget','malformed_response','asset_permission_denied'))


def policy(value,expected):
    keys={'contract','rules_sha256','data_source_id','data_source_pin','database_pin','title_property','allow_apply','allow_create','allow_archive','format_accepted','initial_scope_sha256','plan_authorization'}
    try:
        if not isinstance(value,dict) or set(value)!=keys or value['contract']!=CONTRACT or e.digest(value)!=expected:e.fail('approval_missing')
        for key in ('rules_sha256','data_source_pin','database_pin'):e.p.hex_hash(value[key],64)
        if value['initial_scope_sha256'] is not None:e.p.hex_hash(value['initial_scope_sha256'],64)
        if value['rules_sha256']!=e.rules_sha256():e.fail('rules_changed')
        if value['plan_authorization'] not in ('exact_plan','standing_bounded'):e.fail('approval_missing')
        ident=e.p.notion_id(value['data_source_id'])
        if ident!=value['data_source_id'] or t.native.sha(ident)!=value['data_source_pin']:e.fail('wrong_parent')
        if not isinstance(value['title_property'],str) or not value['title_property'] or value['title_property']=='笔记 ID' or len(value['title_property'])>100:e.fail('approval_missing')
        if any(type(value[k]) is not bool for k in ('allow_apply','allow_create','allow_archive','format_accepted')):e.fail('approval_missing')
        return deepcopy(value)
    except e.Blocked:raise
    except Exception:e.fail('approval_missing')


def pending(publication,state,engine_policy,initial_scope,retry):
    key=e.digest({'policy':engine_policy,'initial_scope':initial_scope})
    result=[]
    for ident,source in sorted(publication.descriptor['records'].items()):
        prior=state['receipts'].get(ident,{})
        if prior.get('md_blob')==source['blob'] and prior.get('source_commit')==publication.descriptor['commit']:continue
        prior=state['failures'].get(ident,{})
        if not retry and prior.get('md_blob')==source['blob'] and prior.get('source_commit')==publication.descriptor['commit'] and prior.get('policy_sha256')==key:continue
        result.append(ident)
    return result[:e.MAX_RECORDS]


def wire_allowlist(plan,api):
    """Exactly the immutable reviewed plan's possible requests, no payload logs."""
    keys=set()
    for row in plan['records']:
        if row['action']=='create':
            body={'parent':{'type':'data_source_id','data_source_id':api.source},'properties':deepcopy(row['properties']),'children':t.wire_blocks(row['request_blocks'])}
            keys.add(t.fingerprint('POST','/pages',body))
        elif row['action']=='update':
            body={'children':t.wire_blocks(row['request_blocks'])}
            if row['after_anchor'] is not None:body['after']=t.native.uuid(row['after_anchor'])
            keys.add(t.fingerprint('PATCH','/blocks/'+t.native.uuid(row['page_id'])+'/children',body))
            for ident in row['old_roots']:keys.add(t.fingerprint('PATCH','/blocks/'+t.native.uuid(ident),{'archived':True}))
    return frozenset(keys)


def run(publication,store,request,approved_policy,expected_policy_sha256,*,apply=False,writers_guard,initial_scope=None,retry_conflicts=False,approved_plan_sha256=None,create_recovery=None,approved_recovery_sha=None,empty_plan_settle=None,snapshot_adapter=None):
    stage='policy';attempted=False; prior_attempt=None
    try:
        if type(apply) is not bool or type(retry_conflicts) is not bool:e.fail('approval_missing')
        approved=policy(approved_policy,expected_policy_sha256)
        if apply and not approved['allow_apply']:e.fail('approval_missing')
        if create_recovery is not None and (not isinstance(create_recovery,dict) or e.digest(create_recovery)!=approved_recovery_sha):e.fail('approval_missing')
        if approved_recovery_sha is not None:e.p.hex_hash(approved_recovery_sha,64)
        if approved_plan_sha256 is not None:e.p.hex_hash(approved_plan_sha256,64)
        if apply and approved['plan_authorization']=='exact_plan' and approved_plan_sha256 is None:e.fail('approval_missing')
        initial_scope={} if initial_scope is None else initial_scope
        if not isinstance(initial_scope,dict) or (e.digest(initial_scope) if initial_scope else None)!=approved['initial_scope_sha256']:e.fail('approval_missing')
        stage='writers';evidence=writers_guard()
        if not e.p.writer_gate({'old_writer_evidence':evidence}):e.fail('writer_gate_unknown')
        engine_policy={'data_source_id':approved['data_source_id'],'id_property':'笔记 ID','title_property':approved['title_property'],'allow_create':approved['allow_create'],'allow_archive':approved['allow_archive'],'format_accepted':approved['format_accepted'],'old_writer_evidence':evidence}
        stage='source';publication.check(publication.descriptor)
        stage='journal';state,sha=store.read();e.check_state(state)
        if e.digest(state)!=sha:e.fail('state_changed')
        journal=state['journal'];prior_attempt=False
        if journal is not None:
            operation=journal.get('operation')
            prior_attempt=bool(journal.get('index')) or bool(operation and (operation.get('kind') in ('archive','cleanup') or operation.get('phase')!='prepared'))
            plan=deepcopy(journal['plan'])
            if plan.get('rules_sha256')!=approved['rules_sha256'] or not e.same(plan.get('policy'),engine_policy) or plan.get('initial_scope_sha256')!=e.digest(initial_scope) or plan.get('retry_conflicts')!=retry_conflicts:e.fail('journal_busy')
            publication.check(plan['publication']);ids=[r['note_id'] for r in plan['records']]
        else:ids=pending(publication,state,engine_policy,initial_scope,retry_conflicts)
        stage='native_read';snapshots={}
        api=t.API(request,approved['data_source_id'],ids,data_source_pin=approved['data_source_pin'],database_pin=approved['database_pin'],snapshot_adapter=snapshot_adapter) if ids else None
        for ident in ids:snapshots[ident]=api.read_note(ident)
        if journal is None:
            stage='prepare';plan=e.prepare(publication,state,sha,snapshots,engine_policy,initial_scope=initial_scope,expected_scope_sha=approved['initial_scope_sha256'],retry_conflicts=retry_conflicts,preserved_images=getattr(snapshot_adapter,'bindings',{}))
        if apply and approved_plan_sha256 is not None and e.digest(plan)!=approved_plan_sha256:e.fail('approval_missing')
        stage='final_guard';publication.check(plan['publication'])
        if not e.p.writer_gate({'old_writer_evidence':writers_guard()}):e.fail('writer_gate_unknown')
        counts=Counter(r['action'] for r in plan['records']);reasons=Counter(r['reason'] for r in plan['records'] if r['action']=='blocked')
        report={'contract':CONTRACT,'status':'prepared','source_mode':plan['source_mode'],'plan_sha256':e.digest(plan),'journal_sha256':sha,'selected':len(plan['records']),'deferred':len(plan['deferred_ids']),'retained_conflicts':len(plan['skipped_conflicts']),'actions':dict(counts),'reason_counts':dict(reasons),'native_notes_read':len(ids),'get_requests':0,'asset_downloads':getattr(snapshot_adapter,'downloads',0),'external_write_attempts':None if prior_attempt else 0,'write_state':'unknown' if prior_attempt else 'not_attempted','prior_journal_effects':'unresolved' if prior_attempt else 'none','complete_library_claimed':False}
        if snapshot_adapter is not None:report['image_binding']=snapshot_adapter.report()
        if not apply:return public_report(report)
        stage='execute';attempted=True
        if api is not None:
            allowed=wire_allowlist(plan,api);api.approved=allowed
            # The concrete production Transport is write-denying until this
            # already policy-bound, source-bound plan has been fully validated.
            if not isinstance(request,t.Transport):e.fail('approval_missing')
            request.approved=allowed
        result=e.execute(plan,e.digest(plan),publication,api,store,expected_store_sha=sha,writers_guard=writers_guard,create_recovery=create_recovery,approved_recovery_sha=approved_recovery_sha)
        safe=e.public_report(result)
        if not plan['records'] and safe.get('status') in ('verified','partial') and 'outbox_receipt_sha256' in plan['publication']:
            stage='queue_settle'
            if not callable(empty_plan_settle):e.fail('journal_failed')
            settled=empty_plan_settle(deepcopy(plan),e.digest(plan))
            e.p.hex_hash(settled,64)
        report.update(status='executed',asset_downloads=getattr(snapshot_adapter,'downloads',0),execution=safe,external_write_attempts=safe.get('external_write_attempts',0),write_state=safe.get('write_state','unknown'))
        if snapshot_adapter is not None:report['image_binding']=snapshot_adapter.report()
        return public_report(report)
    except Exception as exc:
        reason=str(exc);reason=reason if reason in REASONS else 'other'
        return {'contract':CONTRACT,'status':'failed','stage':stage,'reason':reason,'asset_downloads':getattr(snapshot_adapter,'downloads',0),'external_write_attempts':None if attempted or prior_attempt is not False else 0,'write_state':'unknown' if attempted or prior_attempt is not False else 'not_attempted','complete_library_claimed':False}


def public_report(value):
    unknown={'contract':CONTRACT,'status':'failed','write_state':'unknown','external_write_attempts':None,'complete_library_claimed':False}
    if not isinstance(value,dict) or value.get('contract')!=CONTRACT or value.get('status') not in ('prepared','executed'):return unknown
    try:
        for key in ('plan_sha256','journal_sha256'):e.p.hex_hash(value[key],64)
        for key,cap in (('selected',50),('deferred',2500),('retained_conflicts',2500),('native_notes_read',50)):
            if type(value[key]) is not int or not 0<=value[key]<=cap:return unknown
        if value['get_requests']!=0 or type(value['get_requests']) is not int or type(value['asset_downloads']) is not int or not 0<=value['asset_downloads']<=200 or value['complete_library_claimed'] is not False:return unknown
        actions=value['actions'];reasons=value['reason_counts']
        if not isinstance(actions,dict) or set(actions)-{'create','update','noop','blocked'} or any(type(n) is not int or not 0<=n<=50 for n in actions.values()) or sum(actions.values())!=value['selected']:return unknown
        if not isinstance(reasons,dict) or set(reasons)-e.REASONS or any(type(n) is not int or not 0<=n<=50 for n in reasons.values()) or sum(reasons.values())!=actions.get('blocked',0):return unknown
        if value['source_mode'] not in ('initial_review','changed_content'):return unknown
        keep=('contract','status','source_mode','plan_sha256','journal_sha256','selected','deferred','retained_conflicts','actions','reason_counts','native_notes_read','get_requests','asset_downloads','external_write_attempts','write_state','prior_journal_effects','complete_library_claimed')
        clean={k:deepcopy(value[k]) for k in keep}
        if 'image_binding' in value:
            image=value['image_binding']
            if not isinstance(image,dict) or set(image)!={'verified','failures','host'} or image['host'] not in ('not_observed','exact_known_s3','exact_prod_files_secure','other') or type(image['verified']) is not int or not 0<=image['verified']<=1 or not isinstance(image['failures'],dict) or set(image['failures'])-IMAGE_FAILURES or any(type(n) is not int or not 0<n<=1 for n in image['failures'].values()) or image['verified']+sum(image['failures'].values())>1:return unknown
            clean['image_binding']=deepcopy(image)
        if value['prior_journal_effects'] not in ('none','unresolved'):return unknown
        if value['status']=='prepared':
            if value['prior_journal_effects']=='none':
                if value['external_write_attempts']!=0 or type(value['external_write_attempts']) is not int or value['write_state']!='not_attempted':return unknown
            elif value['external_write_attempts'] is not None or value['write_state']!='unknown':return unknown
        else:
            safe=e.public_report(value['execution'])
            if safe!=value['execution'] or value['write_state']!=safe.get('write_state') or value['external_write_attempts']!=safe.get('external_write_attempts'):return unknown
            clean['execution']=safe
        return clean
    except Exception:return unknown
