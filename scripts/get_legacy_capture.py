#!/usr/bin/env python3
"""One approved private source-observation round; never changes source notes.

The fixed reconciliation branch is append-only through expected-head commits.
Receipts are evidence at capture time, not writer admission or current readiness.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from get_mirror_incremental_replay import (capture_json_bytes,strict_capture_json,freeze_legacy_census,
    validate_legacy_census,make_legacy_observation,verify_legacy_observation,observation_detail,ParityFailure)
import get_private_preview_retention as retention
from source_sync_mirror_replay import (LEGACY_SOURCE,LEGACY_OLD_PREVIEW,LEGACY_NEW_PREVIEW,legacy_document)

MIB=1024*1024
REF='refs/heads/reconcile/get-legacy-baselines-20261016'
PREFIX=retention.GET+'/03 State/reconciliation/20261016/'
CENSUS=PREFIX+'census.json'
LEDGER=PREFIX+'ledger.json'
MAX_RAW=128*MIB
MAX_TOTAL=192*MIB
MAX_BATCH=16*MIB
MAX_RESPONSE=2*MIB
MAX_BATCHES=39
LEDGER_CONTRACT='get-legacy-capture-ledger-1'
PROTECTED={retention.BACKUP_REF:LEGACY_SOURCE,retention.PREVIEW_REF:LEGACY_OLD_PREVIEW,retention.FIDELITY_PREVIEW_REF:LEGACY_NEW_PREVIEW}
ERRORS={'capture_target','capture_refs_changed','capture_scope_changed','capture_census_changed','capture_invalid_census','capture_receipt_changed','capture_invalid_receipt','capture_invalid_detail','capture_detail_identity','capture_mirror_changed','capture_mirror_identity','capture_source_changed','capture_commit_cap','capture_raw_budget','capture_total_budget','capture_batch_budget','capture_invalid_ledger','capture_uncertain_write','capture_ref_conflict','capture_readback','capture_invalid_json','capture_response_budget','capture_api_error','capture_unselected_identity','capture_source_missing','capture_census_budget','capture_cohort','capture_mirror','capture_source_identity','incomplete_list','capture_failed','capture_source_failed','capture_ledger_budget','capture_existing_ref','capture_unsupported'}

ERRORS |= set(retention.ERROR_CODES)
ERRORS |= {'capture_git_tree_read','capture_git_index','capture_git_blob','capture_git_tree_write','capture_git_commit'}

def fail(code):raise ParityFailure(code if code in ERRORS else 'capture_failed')
def digest(value):return hashlib.sha256(value).hexdigest()
def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
def record_path(ident):
    if not isinstance(ident,str) or not re.fullmatch('[0-9]{1,64}',ident):fail('capture_cohort')
    return PREFIX+'observations/'+ident+'.json'

def public_report(value):
    result={'mode':'legacy_capture','status':'failed','main_updated':False,'mirror_adopted':0,'apply_ready':False,'source_content_complete_claimed':False}
    if not isinstance(value,dict):return result
    for key in ('eligible','captured','remaining','source_missing','source_version_changed','source_transcript_unavailable','capture_batches','raw_bytes','retained_bytes','fresh_details','detail_requests','http_attempts','http_retries','api_requests','cas_attempts','cas_retries'):
        if type(value.get(key)) is int and 0<=value[key]<=MAX_TOTAL:result[key]=value[key]
    if isinstance(value.get('error'),str) and value.get('error') in ERRORS:result['error']=value['error']
    if value.get('rate_limit_reason') in ('qps_global','qps_bucket','quota_day','quota_month','not_member','unknown'):result['rate_limit_reason']=value['rate_limit_reason']
    valid=all(k in result for k in ('eligible','captured','remaining','source_missing','capture_batches','raw_bytes','retained_bytes'))
    if valid and 0<=result['eligible']<=1923 and result['captured']+result['remaining']+result['source_missing']==result['eligible'] and result['capture_batches']<=MAX_BATCHES and result['raw_bytes']<=MAX_RAW and result['retained_bytes']<=MAX_TOTAL and value.get('refs_verified') is True and value.get('readback_verified') is True:
        if value.get('status')=='capture_complete_for_review' and result['remaining']==0 and 'error' not in result:result['status']='capture_complete_for_review'
        elif value.get('status')=='capture_partial':result['status']='capture_partial'
        result['refs_verified']=True;result['readback_verified']=True
    return result

class Source:
    """Existing fixed-host HTTP policy, strict raw decoding for this path only."""
    def __init__(self,key,client,policy,transport=None,budget=None):
        import sync_getnote_to_raw as sync
        from source_sync_inventory import ResponseBudget
        self.transport=transport or sync;self.key=key;self.client=client;self.http_attempts=0;self.api_requests=0;self.capture_metrics={};self.list_receipts=[]
        owner=self
        class CountedPolicy:
            def before_request(self):policy.before_request();owner.http_attempts+=1
            def rate_limited(self,*args,**kwargs):return policy.rate_limited(*args,**kwargs)
        self.policy=CountedPolicy()
        self.budget=budget or ResponseBudget(MAX_RAW);self.details=0
    def request(self,path,params):
        self.api_requests+=1
        raw=self.transport.api_get(path,params,self.key,self.client,max_response_bytes=MAX_RESPONSE,response_budget=self.budget,request_policy=self.policy,capture_raw=True)
        value=strict_capture_json(raw)
        if value.get('success') is False:fail('capture_api_error')
        return raw,value
    def enumerate(self):
        items=[];seen=set();cursors=set();cursor=None
        for page in range(150):
            raw,value=self.request(self.transport.LIST_PATH,{} if cursor is None else {'cursor':cursor})
            payload=value.get('data',value)
            if not isinstance(payload,dict) or type(payload.get('has_more')) is not bool:fail('incomplete_list')
            keys=[k for k in ('notes','list','records','items') if k in payload]
            if len(keys)!=1 or not isinstance(payload[keys[0]],list):fail('incomplete_list')
            for item in payload[keys[0]]:
                from get_v7_compat import note_identity
                if not isinstance(item,dict):fail('capture_cohort')
                ident=note_identity(item)
                if not re.fullmatch('[0-9]{1,64}',ident) or ident in seen:fail('capture_cohort')
                seen.add(ident);items.append(item)
                if len(items)>2500:fail('capture_cohort')
            self.list_receipts.append({'sequence':page+1,'wire_bytes':len(raw),'wire_sha256':digest(raw),'semantic_sha256':digest(capture_json_bytes(value)),'items_sha256':digest(capture_json_bytes(payload[keys[0]])),'ids':[note_identity(item) for item in payload[keys[0]]],'has_more':payload['has_more']})
            if payload['has_more'] is False:
                if not items:fail('incomplete_list')
                return items,True
            if not payload[keys[0]]:fail('incomplete_list')
            value=payload.get('cursor',payload.get('next_cursor'))
            if not isinstance(value,(str,int)) or isinstance(value,bool):fail('incomplete_list')
            cursor=str(value)
            if not cursor or cursor in cursors:fail('incomplete_list')
            cursors.add(cursor)
        fail('incomplete_list')
    def detail_raw(self,ident):
        record_path(ident)
        self.details+=1
        if self.details>1923:fail('capture_cohort')
        return self.request(self.transport.DETAIL_PATH,{'id':ident,'image_quality':'original'})[0]

def proposal(record,census,old_bytes):
    """Exact body comparison and reconstructible proposed text, no ownership guess."""
    from get_mirror_render import render
    fields,body=legacy_document(old_bytes)
    note={**census['summaries'][record['identity']],**observation_detail(record)}
    result={'contract':'get-legacy-text-proposal-1','disposition':'unresolved_legacy','mirror_adopted':False,
        'local_body_sha256':digest(body.encode()),'preserve_frontmatter_bytes':True,'preserve_unknown_fields':True,
        'preserve_existing_dates':True,'text_only_comparison':True,'attachment_downloads':0,
        'candidate_recipe':'faithful-render-v1-no-assets','candidate_body_sha256':None,'body_exact':False,
        'ownership_map':None,'historical_source_update_claimed':False,'blocked_from_apply':True,'byte_diff':None}
    try:
        rendered,_=render(note,[],Path(record['mirror']['filePath']),faithful=True)
        _,candidate=legacy_document(rendered.encode())
        result['candidate_body_sha256']=digest(candidate.encode());result['body_exact']=body==candidate
        if body!=candidate:result['byte_diff']={'operation':'replace_bytes','start':len(old_bytes)-len(body.encode()),'old_length':len(body.encode()),'old_sha256':digest(body.encode()),'new_utf8':candidate,'result_sha256':digest(old_bytes[:len(old_bytes)-len(body.encode())]+candidate.encode())}
        result['disposition']='exact_body_requires_metadata_review' if body==candidate and not note.get('attachments') else 'unresolved_legacy'
    except (ValueError,TypeError,AttributeError,KeyError):result['disposition']='source_render_unavailable'
    if record['disposition']!='observed_unreconciled':result['disposition']=record['disposition']
    return result

class Remote(retention.GitHubRemote):
    def ref(self,name):
        if name!=REF:return super().ref(name)
        value=self.request('/git/ref/'+name.removeprefix('refs/'),missing=True)
        if value is None:return None
        if value.get('ref')!=name or value.get('object',{}).get('type')!='commit':fail('capture_ref_conflict')
        return retention.checked_sha(value['object'].get('sha'))
    def cas(self,expected,commit):
        retention.checked_sha(commit)
        if expected is not None:retention.checked_sha(expected)
        # Local descendant check AND exact remote lease. No unrelated ref allowed.
        if expected is not None:self.local.run('merge-base','--is-ancestor',expected,commit)
        self.local.run('push','--porcelain','--no-verify','--force-with-lease='+REF+':'+(expected or ''),retention.REMOTE_URL,commit+':'+REF)

def guard(remote,census=None):
    metadata=remote.metadata()
    if metadata.get('full_name')!=retention.REPOSITORY or metadata.get('private') is not True:fail('capture_target')
    if any(remote.ref(ref)!=sha for ref,sha in PROTECTED.items()):fail('capture_refs_changed')
    main=remote.ref(retention.MAIN_REF)
    if main is None:fail('capture_refs_changed')
    tree=remote.source_get_tree(main)
    if census and (tree!=census['get_tree'] or remote.source_get_tree(census['source_commit'])!=census['get_tree']):fail('capture_source_changed')
    if not census and tree!=remote.source_get_tree(LEGACY_SOURCE):fail('capture_source_changed')
    return main,tree

def fetch_blobs(remote,metadata,token,directory):
    """Exact selected Git blobs only; no worktree/assets/non-Get payload."""
    import source_sync_pack_seed as pack
    requested={sha:size for mode,kind,sha,size in metadata.values()}
    if any(mode!='100644' or kind!='blob' or size>(16*MIB if path==CENSUS else (5*MIB if path.startswith(PREFIX+'observations/') else MAX_RESPONSE)) for path,(mode,kind,sha,size) in metadata.items()) or sum(requested.values())>MAX_TOTAL:fail('capture_mirror')
    protocol=pack.capabilities(token)
    paths,used=retention.fetch_readback_blobs(token,requested,directory,pack.MAX_PACK,protocol)
    result={}
    for path,(_,_,sha,size) in metadata.items():
        data=retention.file_bytes(paths[sha],16*MIB if path==CENSUS else 5*MIB)
        if len(data)!=size or retention.oid(data)!=sha:fail('capture_readback')
        result[path]=data
    return result

def authorized_cohort(remote):
    preview_meta=remote.check_source_budget(LEGACY_NEW_PREVIEW)
    old_meta=remote.check_source_budget(LEGACY_OLD_PREVIEW)
    state_path=retention.GET+'/03 State/cloud-sync-state.json'
    def read_state(meta,commit):
        mode,kind,blob,size=meta[state_path]
        if mode!='100644' or kind!='blob' or size>2*MIB:fail('capture_cohort')
        return strict_capture_json(remote.blob(blob,size))
    state=read_state(preview_meta,LEGACY_NEW_PREVIEW);prior=read_state(old_meta,LEGACY_OLD_PREVIEW)
    conflicts=state.get('conflictsById');managed=state.get('notesByCanonicalId');pending=state.get('pendingById')
    if state.get('contract')!='get-source-raw-local-2' or state.get('local_proposal_only') is not True or not isinstance(conflicts,dict) or len(conflicts)!=1931 or not isinstance(managed,dict) or len(managed)!=10 or not isinstance(pending,dict) or len(pending)!=1 or not isinstance(prior.get('pendingById'),dict):fail('capture_cohort')
    excluded=set(prior['pendingById'])&set(conflicts)
    if len(excluded)!=8:fail('capture_cohort')
    candidates={i:e for i,e in conflicts.items() if i not in excluded}
    if set(excluded)&set(pending) or len(excluded|set(pending))!=9:fail('capture_cohort')
    return preview_meta,candidates,excluded,pending,managed

def validate_authorized_cohort(census,candidates,pending,source_meta):
    if set(census['eligible'])!=set(candidates) or set(census['pending_ids'])!=set(pending):fail('capture_cohort')
    for ident,entry in census['eligible'].items():
        if entry['filePath']!=candidates[ident]['filePath'] or entry['gitBlob']!=source_meta[retention.GET+'/'+entry['filePath']][2]:fail('capture_mirror_changed')

def initial_ledger(census,census_hash):
    return {'contract':LEDGER_CONTRACT,'census_sha256':census_hash,'source_commit':census['source_commit'],
        'capture_batches':0,'finalized':False,'records':{},'raw_bytes':len(capture_json_bytes(census['summaries'])),
        'retained_bytes':0,'source_missing_ids':census['source_missing_ids'],'current_completeness_claimed':False,'mirror_adopted':0}

def validate_ledger(census,ledger,records):
    validate_legacy_census(census)
    expected={'contract','census_sha256','source_commit','capture_batches','finalized','records','raw_bytes','retained_bytes','source_missing_ids','current_completeness_claimed','mirror_adopted'}
    if not isinstance(ledger,dict) or set(ledger)!=expected or ledger['contract']!=LEDGER_CONTRACT or ledger['census_sha256']!=digest(capture_json_bytes(census)) or ledger['source_commit']!=census['source_commit'] or ledger['source_missing_ids']!=census['source_missing_ids'] or ledger['current_completeness_claimed'] is not False or type(ledger['mirror_adopted']) is not int or ledger['mirror_adopted']!=0:fail('capture_invalid_ledger')
    if type(ledger['capture_batches']) is not int or not 0<=ledger['capture_batches']<=MAX_BATCHES or type(ledger['finalized']) is not bool or not isinstance(ledger['records'],dict) or set(ledger['records'])!=set(records):fail('capture_invalid_ledger')
    for key,limit in (('raw_bytes',MAX_RAW),('retained_bytes',MAX_TOTAL)):
        if type(ledger[key]) is not int or not 0<=ledger[key]<=limit:fail('capture_invalid_ledger')
    raw=len(capture_json_bytes(census['summaries']))
    for ident,entry in ledger['records'].items():
        value=records[ident]
        if not isinstance(entry,dict) or set(entry)!={'sha256','bytes','disposition'} or entry['bytes']!=len(capture_json_bytes(value)) or entry['sha256']!=digest(capture_json_bytes(value)) or entry['disposition']!=value.get('observation',{}).get('disposition'):fail('capture_invalid_ledger')
        if set(value)!={'observation','proposal'}:fail('capture_invalid_receipt')
        observation=value['observation']
        if verify_legacy_observation(census,ledger['census_sha256'],observation,digest(capture_json_bytes(observation)),_prevalidated_census=True)!=ident:fail('capture_invalid_receipt')
        raw+=len(capture_json_bytes(observation['payload']))
    if raw!=ledger['raw_bytes']:fail('capture_invalid_ledger')
    remaining=set(census['eligible'])-set(census['source_missing_ids'])-set(records)
    if ledger['finalized'] and remaining:fail('capture_invalid_ledger')
    return sorted(remaining)

def make_commit(local,parent,files):
    def checked(stage,operation,*args,**kwargs):
        try:return operation(*args,**kwargs)
        except retention.RetentionFailure:fail(stage)
    before=checked('capture_git_tree_read',local.tree,parent)
    if not files or any(not p.startswith(PREFIX) for p in files):fail('capture_scope_changed')
    with tempfile.TemporaryDirectory(prefix='get-capture-index-') as temporary:
        env=dict(local.env,GIT_INDEX_FILE=str(Path(temporary)/'index'))
        checked('capture_git_index',local.run,'read-tree',parent,env=env)
        for path,data in files.items():
            retention.safe_path(path)
            blob=checked('capture_git_blob',local.run,'hash-object','-w','--stdin',data=data,env=env).decode().strip()
            if blob!=retention.oid(data):fail('capture_readback')
            checked('capture_git_index',local.run,'update-index','--add','--cacheinfo','100644,'+blob+','+path,env=env)
        # Existing blob OIDs are retained from the verified parent tree. They
        # intentionally are absent from this object-only clone; do not lazily
        # fetch them merely to write tree metadata. Every new blob is present
        # and read back below, and the complete resulting tree is checked.
        tree=checked('capture_git_tree_write',local.run,'write-tree','--missing-ok',env=env).decode().strip()
        after=checked('capture_git_tree_read',local.tree,tree)
        wanted=dict(before)
        for path,data in files.items():
            wanted[path]=('100644','blob',retention.oid(data))
            if checked('capture_git_blob',local.blob,retention.oid(data))!=data:fail('capture_readback')
        if after!=wanted:fail('capture_scope_changed')
        env.update(GIT_AUTHOR_NAME='Get reconciliation',GIT_AUTHOR_EMAIL='get-reconciliation@users.noreply.github.com',GIT_COMMITTER_NAME='Get reconciliation',GIT_COMMITTER_EMAIL='get-reconciliation@users.noreply.github.com')
        commit=checked('capture_git_commit',local.run,'commit-tree',tree,'-p',parent,'-m','Retain bounded private Get reconciliation evidence',env=env).decode().strip()
    changed={p for p in set(before)|set(after) if before.get(p)!=after.get(p)}
    if changed!=set(files):fail('capture_scope_changed')
    return commit

def publish_batch(local,remote,census,expected,files,token,readback):
    guard(remote,census)
    if remote.ref(REF)!=expected:fail('capture_ref_conflict')
    parent=expected or census['source_commit'];commit=make_commit(local,parent,files)
    # Recheck after preparation so disappearing/moving protected refs stop writes.
    guard(remote,census)
    if remote.ref(REF)!=expected:fail('capture_ref_conflict')
    if not hasattr(remote,'metrics'):remote.metrics={}
    for attempt in range(3):
        remote.metrics['cas_attempts']=remote.metrics.get('cas_attempts',0)+1
        if attempt:remote.metrics['cas_retries']=remote.metrics.get('cas_retries',0)+1
        try:
            remote.cas(expected,commit);break
        except Exception:
            # Retry the SAME already-built commit only after proving the prior
            # write did not happen. Never read source again or create a new ref.
            try:actual=remote.ref(REF)
            except Exception:fail('capture_uncertain_write')
            if actual==commit:break
            if actual!=expected or attempt==2:fail('capture_uncertain_write')
            guard(remote,census)
    if remote.ref(REF)!=commit or remote.commit(commit).get('parents')!=[parent]:fail('capture_readback')
    observed=readback(commit,files)
    if observed!=files:fail('capture_readback')
    guard(remote,census)
    if remote.ref(REF)!=commit:fail('capture_ref_conflict')
    return commit

def capture_batches(census,ledger,records,source,old,commit,*,clock=now,stop=None):
    """Validated partial batches are committed before a read error is reported."""
    census_hash=digest(capture_json_bytes(census));remaining=validate_ledger(census,ledger,records)
    pending_files={CENSUS:capture_json_bytes(census)} if ledger['capture_batches']==0 else {}
    error=None;fresh=0;rate_reason=None
    while remaining or pending_files:
        if ledger['capture_batches']>=MAX_BATCHES:error='capture_commit_cap';break
        batch={};raw_added=0
        while remaining and len(batch)<50:
            # Reserve bounded response/receipt/ledger space BEFORE reading. A
            # successful receipt never gets discarded because of a later cap.
            reserved=5*MIB
            if ledger['raw_bytes']+raw_added+MAX_RESPONSE+1>MAX_RAW:error='capture_raw_budget';break
            used=sum(map(len,pending_files.values()))+sum(len(capture_json_bytes(v)) for v in batch.values())
            if used+reserved+MIB>MAX_BATCH:break
            if ledger['retained_bytes']+used+reserved+MIB>MAX_TOTAL:error='capture_total_budget';break
            if stop is not None and stop():error='capture_source_failed';break
            ident=remaining[0]
            try:
                raw=source.detail_raw(ident);fresh+=1
                observation=make_legacy_observation(census,census_hash,ident,raw,old[ident],clock(),_prevalidated_census=True)
                value={'observation':observation,'proposal':proposal(observation,census,old[ident])}
                encoded=capture_json_bytes(value)
                if len(encoded)>reserved:fail('capture_response_budget')
                verify_legacy_observation(census,census_hash,observation,digest(capture_json_bytes(observation)),_prevalidated_census=True)
            except Exception as exc:
                error=str(exc) if str(exc) in ERRORS else 'capture_source_failed'
                if type(exc).__name__=='RateLimitFailure':rate_reason=getattr(exc,'reason','unknown')
                break
            batch[ident]=value;raw_added+=len(capture_json_bytes(observation['payload']));remaining.pop(0)
        if batch or pending_files:
            candidate=deepcopy(ledger);candidate['capture_batches']+=1;candidate['raw_bytes']+=raw_added
            if candidate['raw_bytes']>MAX_RAW:fail('capture_raw_budget')
            files=dict(pending_files)
            for ident,value in batch.items():
                encoded=capture_json_bytes(value);files[record_path(ident)]=encoded
                candidate['records'][ident]={'sha256':digest(encoded),'bytes':len(encoded),'disposition':value['observation']['disposition']}
            # Count full ledger blobs across history, conservatively, not just
            # current tree size. Iterate until the decimal count is stable.
            base=ledger['retained_bytes']+sum(map(len,files.values()))
            candidate['retained_bytes']=base
            for _ in range(4):candidate['retained_bytes']=base+len(capture_json_bytes(candidate))
            files[LEDGER]=capture_json_bytes(candidate)
            if len(files[LEDGER])>MIB:fail('capture_ledger_budget')
            if sum(map(len,files.values()))>MAX_BATCH or candidate['retained_bytes']>MAX_TOTAL:fail('capture_batch_budget')
            commit(files)
            ledger=candidate;records.update(batch);pending_files={}
        if error:break
        if not batch and not pending_files and remaining:fail('capture_batch_budget')
    if not remaining and not ledger['finalized']:
        candidate=deepcopy(ledger);candidate['finalized']=True
        base=ledger['retained_bytes']
        for _ in range(4):candidate['retained_bytes']=base+len(capture_json_bytes(candidate))
        if candidate['retained_bytes']>MAX_TOTAL:error='capture_total_budget'
        else:commit({LEDGER:capture_json_bytes(candidate)});ledger=candidate
    validate_ledger(census,ledger,records)
    counts={k:sum(v['observation']['disposition']==k for v in records.values()) for k in ('source_version_changed','source_transcript_unavailable')}
    result={'status':'capture_complete_for_review' if ledger['finalized'] and not error else 'capture_partial','eligible':len(census['eligible']),'captured':len(records),'remaining':len(remaining),'source_missing':len(census['source_missing_ids']),'capture_batches':ledger['capture_batches'],'raw_bytes':ledger['raw_bytes'],'retained_bytes':ledger['retained_bytes'],'fresh_details':fresh,'refs_verified':True,'readback_verified':True,**counts}
    if error:result['error']=error
    if rate_reason is not None:result['rate_limit_reason']=rate_reason
    result['detail_requests']=getattr(source,'details',fresh);result['http_attempts']=getattr(source,'http_attempts',fresh)
    result['api_requests']=getattr(source,'api_requests',fresh);result['http_retries']=max(0,result['http_attempts']-result['api_requests'])
    return ledger,result

def scope_metadata(remote,commit):
    entries=remote.tree_entries(remote.source_get_tree(commit),True);out={}
    for row in entries:
        path=retention.GET+'/'+row.get('path','')
        if not path.startswith(PREFIX):continue
        if row.get('type')=='tree' and row.get('mode')=='040000':continue
        retention.safe_path(path)
        size=row.get('size')
        if row.get('type')!='blob' or row.get('mode')!='100644' or type(size) is not int or size<0 or size>16*MIB or path in out:fail('capture_scope_changed')
        out[path]=('100644','blob',retention.checked_sha(row.get('sha')),size)
    if len(out)>1925 or sum(v[3] for v in out.values())>MAX_TOTAL:fail('capture_total_budget')
    return out

def verify_scope_history(local,head,census,ledger,remote):
    """Bound commit count and immutable receipts; recompute history storage."""
    base=census['source_commit'];base_tree=local.tree(base);commit=head;chain=[]
    while commit!=base:
        if len(chain)>=40:fail('capture_commit_cap')
        headers=local.run('cat-file','-p',commit).split(b'\n\n',1)[0].splitlines()
        parents=[h[7:].decode() for h in headers if h.startswith(b'parent ')]
        if len(parents)!=1:fail('capture_scope_changed')
        chain.append((commit,parents[0]));commit=parents[0]
    if len(chain)!=ledger['capture_batches']+int(ledger['finalized']):fail('capture_invalid_ledger')
    # Immutable payloads appear once; ledger versions count each newly retained
    # blob across Git history. Server supplies only ledger bytes here.
    previous=base_tree;total=0
    for position,(commit,_) in enumerate(reversed(chain)):
        current=local.tree(commit)
        changed={p for p in set(previous)|set(current) if previous.get(p)!=current.get(p)}
        if not changed or LEDGER not in changed or any(not p.startswith(PREFIX) or p not in current for p in changed):fail('capture_scope_changed')
        if any(p!=LEDGER and p in previous for p in changed):fail('capture_scope_changed')
        if any(v!=current.get(p) for p,v in base_tree.items()):fail('capture_scope_changed')
        metadata=scope_metadata(remote,commit)
        if (CENSUS in changed)!=(position==0) or sum(p.startswith(PREFIX+'observations/') for p in changed)>50 or sum(metadata[p][3] for p in changed)>MAX_BATCH:fail('capture_invalid_ledger')
        if ledger['finalized'] and position==len(chain)-1 and changed!={LEDGER}:fail('capture_invalid_ledger')
        for path in changed:
            if metadata.get(path,())[:3]!=current[path]:fail('capture_scope_changed')
            total+=metadata[path][3]
        previous=current
    if total!=ledger['retained_bytes'] or total>MAX_TOTAL:fail('capture_invalid_ledger')

def retain_verified_ledger_blob(local,data,expected_oid):
    """Keep the already-verified mutable-path base for Git delta packing.

    Immutable census/receipts and old notes stay absent from the object store.
    This imports only bytes already fetched and hash-validated for resume.
    """
    if len(data)>MIB or retention.oid(data)!=expected_oid:fail('capture_invalid_ledger')
    blob=local.run('hash-object','-w','--stdin',data=data).decode().strip()
    if blob!=expected_oid or local.blob(blob)!=data:fail('capture_readback')

def run(token,source,work):
    remote=Remote(None,token);source.capture_metrics={};remote.metrics=source.capture_metrics
    main,get_tree=guard(remote);head=remote.ref(REF)
    with tempfile.TemporaryDirectory(prefix='get-legacy-capture-',dir=work) as folder:
        root=Path(folder)
        # Object-only, bounded Get payload fetches; never checkout media/assets.
        local=retention.readback_checkout(root/'objects',head or main,token);remote.local=local
        serial=[0]
        def readback(commit,files):
            if remote.commit(commit)['tree']!=local.run('rev-parse',commit+'^{tree}').decode().strip():fail('capture_readback')
            serial[0]+=1
            metadata={p:('100644','blob',retention.oid(data),len(data)) for p,data in files.items()}
            return fetch_blobs(remote,metadata,token,root/('readback-'+str(serial[0])))
        if head:
            metadata=scope_metadata(remote,head)
            if CENSUS not in metadata or LEDGER not in metadata:fail('capture_existing_ref')
            # Initial census can exceed a source-response cap; read separately
            # with its explicitly bounded 16 MiB repository blob allowance.
            census_bytes=remote.blob(metadata[CENSUS][2],metadata[CENSUS][3]);census=strict_capture_json(census_bytes,16*MIB)
            validate_legacy_census(census)
            if not census['page_receipts']:fail('capture_invalid_census')
            if capture_json_bytes(census)!=census_bytes:fail('capture_invalid_census')
            ledger_bytes=remote.blob(metadata[LEDGER][2],metadata[LEDGER][3]);ledger=strict_capture_json(ledger_bytes,MIB)
            if capture_json_bytes(ledger)!=ledger_bytes:fail('capture_invalid_ledger')
            retain_verified_ledger_blob(local,ledger_bytes,metadata[LEDGER][2])
            local.run('fetch','--depth=42','--filter=blob:none','--no-tags','--no-recurse-submodules','--no-write-fetch-head','origin',head)
            local.run('fetch','--depth=1','--filter=blob:none','--no-tags','--no-recurse-submodules','--no-write-fetch-head','origin',census['source_commit'])
            wanted={p:v for p,v in metadata.items() if p not in (CENSUS,LEDGER)}
            fetched=fetch_blobs(remote,wanted,token,root/'receipts') if wanted else {}
            records={}
            for path,data in fetched.items():
                ident=path.removeprefix(PREFIX+'observations/').removesuffix('.json')
                if record_path(ident)!=path:fail('capture_scope_changed')
                value=strict_capture_json(data,5*MIB)
                if capture_json_bytes(value)!=data:fail('capture_invalid_receipt')
                records[ident]=value
            remaining=validate_ledger(census,ledger,records)
            verify_scope_history(local,head,census,ledger,remote)
            guard(remote,census)
            if remote.ref(REF)!=head:fail('capture_ref_conflict')
            source_meta=remote.check_source_budget(census['source_commit'])
            _,candidates,excluded,pending,_=authorized_cohort(remote)
            validate_authorized_cohort(census,candidates,excluded|set(pending),source_meta)
            needed={retention.GET+'/'+entry['filePath']:source_meta[retention.GET+'/'+entry['filePath']] for entry in census['eligible'].values()}
            fetched=fetch_blobs(remote,needed,token,root/'old') if needed else {}
            old={i:fetched[retention.GET+'/'+entry['filePath']] for i,entry in census['eligible'].items()}
            for ident,entry in census['eligible'].items():
                if retention.oid(old[ident])!=entry['gitBlob'] or digest(old[ident])!=entry['sha256'] or legacy_document(old[ident])[0].get('note_id')!=ident:fail('capture_mirror_changed')
            for ident,value in records.items():
                if value['proposal']!=proposal(value['observation'],census,old[ident]):fail('capture_invalid_receipt')
        else:
            if scope_metadata(remote,main):fail('capture_existing_ref')
            source_meta=remote.check_source_budget(main)
            preview_meta,candidates,excluded,pending,managed=authorized_cohort(remote)
            selected={}
            for ident,entry in candidates.items():
                if entry.get('reason')!='legacy_baseline_missing' or entry.get('sourceContentHash') is not None:fail('capture_cohort')
                path=retention.GET+'/'+entry['filePath'];retention.safe_path(path)
                if source_meta.get(path)!=preview_meta.get(path):fail('capture_mirror_changed')
                selected[path]=source_meta[path]
            managed_meta={}
            for canonical,entry in managed.items():
                path=retention.GET+'/'+entry['filePath'];retention.safe_path(path)
                if not canonical.startswith('api:') or entry.get('origin')!='faithful-created':fail('capture_cohort')
                managed_meta[path]=preview_meta[path]
            all_old=fetch_blobs(remote,selected,token,root/'old')
            new_notes=fetch_blobs(remote,managed_meta,token,root/'managed')
            for entry in managed.values():
                if digest(new_notes[retention.GET+'/'+entry['filePath']])!=entry['mirrorSha256']:fail('capture_mirror_changed')
            paths={};old={}
            for ident,entry in candidates.items():
                path=retention.GET+'/'+entry['filePath'];data=all_old[path]
                fields,_=legacy_document(data)
                if fields.get('note_id')!=ident:fail('capture_mirror_identity')
                old[ident]=data;paths[ident]={'filePath':entry['filePath'],'gitBlob':selected[path][2],'sha256':digest(data)}
            summaries,terminal=source.enumerate()
            census,census_hash=freeze_legacy_census(summaries,terminal,paths,excluded|set(pending),main,get_tree,now(),page_receipts=source.list_receipts)
            if not census['page_receipts']:fail('capture_invalid_census')
            ledger=initial_ledger(census,census_hash);records={}
            guard(remote,census)
            if remote.ref(REF) is not None:fail('capture_ref_conflict')
        def commit(files):
            nonlocal head
            head=publish_batch(local,remote,census,head,files,token,readback)
        _,result=capture_batches(census,ledger,records,source,old,commit)
        guard(remote,census)
        if remote.ref(REF)!=head:fail('capture_ref_conflict')
        result.update(cas_attempts=remote.metrics.get('cas_attempts',0),cas_retries=remote.metrics.get('cas_retries',0))
        return result

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--publish-report');args=parser.parse_args(argv)
    if args.publish_report:
        try:value=json.loads(Path(args.publish_report).read_text())
        except Exception:value={}
        print(json.dumps(public_report(value)));return 0
    try:
        import sync_getnote_to_raw as sync
        from source_sync_rate import RequestPolicy
        key,client=sync.load_credentials();token=os.environ.get('PRIVATE_VAULT_TOKEN')
        source=Source(key,client,RequestPolicy(seconds=105*60))
        result=run(token,source,None)
    except (Exception,SystemExit) as exc:
        result={'error':str(exc) if str(exc) in ERRORS else 'capture_failed'}
        if type(exc).__name__=='RateLimitFailure':result['rate_limit_reason']=getattr(exc,'reason','unknown')
        if 'source' in locals():
            result.update(detail_requests=source.details,api_requests=source.api_requests,http_attempts=source.http_attempts,http_retries=max(0,source.http_attempts-source.api_requests),**source.capture_metrics)
    clean=public_report(result);print(json.dumps(clean));return 0 if clean['status']=='capture_complete_for_review' else (2 if clean['status']=='capture_partial' else 1)

if __name__=='__main__':raise SystemExit(main())
