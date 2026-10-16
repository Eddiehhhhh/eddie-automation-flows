"""One bounded manual canonical Get engine; preparation is the default.

Publication requires an exact externally approved logical payload. Scheduled
callers must supply independently verified prior success anchors; this module
never treats a freshly hashed untrusted state as that external anchor.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import tempfile

import get_canonical_writer as n
import get_legacy_manual as m
from get_legacy_manual import a,c,r,strict_capture_json,raw_source_version

CONTRACT='get-canonical-manual-transaction-1'
MAX_NEW=50

def load_states(reader,evidence,parent,current,legacy_sha,registry_sha):
    if a.STATE not in current:m.fail('manual_state_anchor')
    legacy=a.validate_state(strict_capture_json(current[a.STATE],a.MAX_STATE),legacy_sha,evidence)
    if n.STATE in current:
        if not registry_sha:m.fail('manual_state_anchor')
        registry=n.validate_registry(strict_capture_json(current[n.STATE],n.MAX_STATE),registry_sha,evidence,legacy,legacy_sha,current)
        if a.encoded(registry)!=current[n.STATE]:m.fail('manual_state_anchor')
    else:
        if registry_sha:m.fail('manual_state_anchor')
        registry=n.initial_registry(evidence,legacy,legacy_sha,current)
    return legacy,registry

def select_new(items,registry,preview):
    candidates=sorted(set(items)-set(registry['reserved'])-set(registry['newById']))
    if preview is not None:
        if len(preview)!=m.CANONICAL_COUNT or registry['newById']:m.fail('manual_scope')
        selected=sorted(preview)
        if set(selected)&set(registry['reserved']):m.fail('manual_scope')
    else:
        cursor=registry['scan_cursor'];after=[i for i in candidates if cursor is None or i>cursor]
        selected=(after+[i for i in candidates if i not in after])[:MAX_NEW]
    return selected,len(set(candidates)-set(selected))

def canonical_payload(reader,plan,current):
    return {'contract':'get-canonical-approved-payload-1','rules_sha256':m.rules_sha256(),
      'before_get_tree':reader.source_get_tree(plan['parent_commit']),
      'current_sha256':{p:a.sha(v) for p,v in sorted(current.items())},
      **{k:deepcopy(plan[k]) for k in ('capture_anchor','previous_legacy_sha256','previous_registry_sha256','state_sha256','registry_sha256','changes','counts','source_summary_sha256','detail_receipts_sha256','initial_preview_import','conflict_reason_counts')}}

def verify_plan(reader,plan,files):
    if plan.get('contract')!=CONTRACT or plan.get('local_proposal_only') is not True or plan.get('apply_ready') is not False:m.fail('manual_plan')
    parent=a.checked(plan['parent_commit'],40);commit=a.checked(plan['candidate_commit'],40)
    reader.local.run('merge-base','--is-ancestor',parent,commit)
    before=reader.tree(parent);after=reader.tree(commit)
    if reader.local.run('rev-parse',commit+'^{tree}').decode().strip()!=plan['candidate_tree']:m.fail('manual_plan')
    changed={p for p in set(before)|set(after) if before.get(p)!=after.get(p)}
    if changed!=set(files) or changed!={x['path'] for x in plan['changes']} or len(changed)!=len(plan['changes']):m.fail('manual_plan')
    for row in plan['changes']:
        p=row['path'];r.safe_path(p)
        if not p.startswith(r.GET+'/') or p not in after:m.fail('manual_scope')
        data=files[p];cap=n.MAX_STATE if p in (r.GET+'/'+a.STATE,r.GET+'/'+n.STATE) else a.owned.MAX_NOTE
        if not isinstance(data,bytes) or len(data)>cap or before.get(p,[None,None,None])[2]!=row['before_blob'] or after[p]!=('100644','blob',r.oid(data)) or row['after_blob']!=r.oid(data) or row['sha256']!=a.sha(data) or row['bytes']!=len(data) or reader.read(commit,p,cap)!=data:m.fail('manual_plan')
    if sum(map(len,files.values()))>m.MAX_CHANGED_BYTES or plan['transaction_bytes']!=sum(len(data) for oid,data in {r.oid(v):v for v in files.values()}.items() if oid not in {x[2] for x in before.values()}):m.fail('manual_budget')
    if commit!=parent and reader.local.run('rev-list','--parents','-n','1',commit).decode().split()!=[commit,parent]:m.fail('manual_plan')

def prepare(reader,evidence,parent,current,source,*,legacy_sha,registry_sha=None,preview=None):
    legacy,registry=load_states(reader,evidence,parent,current,legacy_sha,registry_sha)
    first=n.STATE not in current
    if first!=(preview is not None):m.fail('manual_scope')
    summaries,terminal=source.enumerate();items=a.source_snapshot(summaries,terminal)
    selected,remaining=select_new(items,registry,preview)
    legacy_needed={i for i,row in legacy['legacyById'].items() if i in items and (raw_source_version(items[i].get('updated_at'))!=row['ownership']['source_version'] or a.sha(a.encoded(items[i]))!=row['summary_sha256'])}
    new_needed={i for i,row in registry['newById'].items() if i in items and (raw_source_version(items[i].get('updated_at'))!=row['source_version'] or n.raw._source_content_hash(items[i])!=row['summary_sha256'])}
    needed=sorted((legacy_needed|new_needed|set(selected))&set(items));details={};receipts={};unavailable=0
    for ident in needed:
        try:
            raw=source.detail_raw(ident);details[ident]=m.detail_note(raw)
            receipts[ident]={'wire_sha256':a.sha(raw),'wire_bytes':len(raw),'semantic_sha256':a.sha(a.encoded(details[ident]))}
        except RuntimeError as exc:
            if str(exc)!='GetNote HTTP 404':raise
            details[ident]=None;unavailable+=1
    proposal,states,counts=n.prepare(reader,parent,evidence,legacy_sha,registry_sha,summaries,terminal,details,selected_new=selected,preview=preview)
    next_registry=states['registry']
    if selected:next_registry['scan_cursor']=selected[-1]
    # Materialize the final state once directly on the same parent. The earlier
    # pure candidate is an unreferenced object, not a second reachable snapshot.
    changes={row['path']:reader.read(proposal['candidate_commit'],r.GET+'/'+row['path'],a.owned.MAX_NOTE) for row in proposal['changes'] if row['path'] not in (a.STATE,n.STATE)}
    final=n.git_candidate(reader,parent,states['legacy'],next_registry,changes,current)
    before=reader.tree(parent);after=reader.tree(final['candidate_commit']);files={r.GET+'/'+row['path']:reader.read(final['candidate_commit'],r.GET+'/'+row['path'],n.MAX_STATE if row['path'] in (a.STATE,n.STATE) else a.owned.MAX_NOTE) for row in final['changes']}
    rows=[{**row,'path':r.GET+'/'+row['path']} for row in final['changes']]
    all_owned={r.GET+'/'+row['ownership']['filePath'] for row in legacy['legacyById'].values()}|{r.GET+'/'+row['filePath'] for row in next_registry['newById'].values()}|{r.GET+'/'+a.STATE,r.GET+'/'+n.STATE}
    if not set(files)<=all_owned or any(p not in after for p in files):m.fail('manual_scope')
    counts.update(legacy_managed=len(states['legacy']['legacyById']),legacy_conflicts=len(states['legacy']['conflictsById']),legacy_pending=len(states['legacy']['pending_ids']),existing_unmanaged=sum(v=='existing_unmanaged' for v in next_registry['reserved'].values()),new_managed=len(next_registry['newById']),new_selected=len(selected),new_remaining=remaining,new_pending=len(next_registry['conflictsById']),source_details=len(details),details_unavailable=unavailable)
    unique={r.oid(data):data for data in files.values()};existing={row[2] for row in before.values()}
    plan={'contract':CONTRACT,**{k:final[k] for k in ('parent_commit','candidate_commit','candidate_tree','registry_sha256')},'state_sha256':final['legacy_sha256'],'previous_legacy_sha256':legacy_sha,'previous_registry_sha256':registry_sha,'capture_anchor':evidence.anchor,'changes':rows,'counts':counts,'conflict_reason_counts':n.reason_counts(next_registry['conflictsById']),'source_summary_sha256':a.sha(a.encoded(summaries)),'detail_receipts_sha256':a.sha(a.encoded({i:{'semantic_sha256':v['semantic_sha256']} for i,v in receipts.items()})), 'wire_receipts_sha256':a.sha(a.encoded(receipts)),'initial_preview_import':first,'transaction_bytes':sum(len(v) for oid,v in unique.items() if oid not in existing),'local_proposal_only':True,'apply_ready':False}
    plan['approval_payload_sha256']=a.sha(a.encoded(canonical_payload(reader,plan,current)))
    verify_plan(reader,plan,files)
    return plan,files

def new_baselines(reader,registry,parent,remote):
    total=0
    for ident,row in registry['newById'].items():
        try:reader.baseline(parent,row['filePath'],row['baseline_blob'],row['mirror_sha256']);continue
        except a.ParityFailure as exc:
            if str(exc)!='baseline_missing':raise
        path=r.GET+'/'+row['filePath'];commits=reader.local.run('rev-list','--max-count=256',parent,'--',path).decode().splitlines();blob=row['baseline_blob']
        if not any(reader.tree(commit).get(path)==('100644','blob',blob) for commit in [parent]+commits):continue
        value=remote.request('/git/blobs/'+blob,maximum=2*a.owned.MAX_NOTE+65536,missing=True)
        if value is None:continue
        size=value.get('size')
        if value.get('sha')!=blob or value.get('encoding')!='base64' or type(size) is not int or not 0<=size<=a.owned.MAX_NOTE:m.fail('manual_readback')
        try:data=m.base64.b64decode(value['content'].replace('\n',''),validate=True)
        except Exception:m.fail('manual_readback')
        total+=len(data)
        if len(data)!=size or total>m.MAX_CHANGED_BYTES or a.sha(data)!=row['mirror_sha256']:m.fail('manual_readback')
        m.import_blob(reader.local,data,blob)

def load_current(reader,remote,token,root,parent):
    metadata=remote.check_source_budget(parent);prefix=r.GET+'/'
    selected={p:row for p,row in metadata.items() if n.is_markdown(p) or p in (prefix+a.STATE,prefix+n.STATE)}
    for p,row in selected.items():
        if row[:2]!=('100644','blob') or row[3]>(n.MAX_STATE if p in (prefix+a.STATE,prefix+n.STATE) else a.owned.MAX_NOTE):m.fail('manual_budget')
    missing={p:row for p,row in selected.items() if reader.local.run('cat-file','--batch-check',data=(row[2]+'\n').encode()).endswith(b' missing\n')}
    for p,data in m.fetch_selected(remote,missing,token,root/'canonical-current').items():m.import_blob(reader.local,data,missing[p][2])
    return n.git_inventory(reader,parent)

def run(token,source,expected_parent,*,legacy_sha,registry_sha=None,apply=False,approved_payload_sha256=None,checkpoint=None,work=None):
    if apply:
        try:a.checked(approved_payload_sha256,64)
        except Exception:m.fail('manual_approval_required')
    a.checked(expected_parent,40);a.checked(legacy_sha,64)
    if registry_sha:a.checked(registry_sha,64)
    remote=m.Remote(None,token);parent=m.guard(remote)
    if parent!=expected_parent and (not apply or remote.source_get_tree(parent)!=remote.source_get_tree(expected_parent)):m.fail('manual_parent_changed')
    with tempfile.TemporaryDirectory(prefix='get-canonical-manual-',dir=work) as folder:
        root=Path(folder);local=r.readback_checkout(root/'objects',m.CAPTURE,token);remote.local=local
        reader,evidence,_=m.load_evidence(local,remote,token,root,parent,anchor=m.ANCHOR)
        if parent!=expected_parent:
            try:local.run('merge-base','--is-ancestor',expected_parent,parent)
            except Exception:m.fail('manual_parent_changed')
        current=load_current(reader,remote,token,root,parent)
        legacy,registry=load_states(reader,evidence,parent,current,legacy_sha,registry_sha)
        m.selected_baselines(reader,legacy,parent,remote);new_baselines(reader,registry,parent,remote)
        preview=None
        if n.STATE not in current:current,preview=m.load_canonical_inputs(reader,remote,token,root,parent)
        m.guard(remote,parent);plan,files=prepare(reader,evidence,parent,current,source,legacy_sha=legacy_sha,registry_sha=registry_sha,preview=preview);m.guard(remote,parent)
        if apply and plan['approval_payload_sha256']!=approved_payload_sha256:m.fail('manual_approval_changed')
        result={'status':'canonical_candidate_verified','main_updated':False,'cas_attempts':0,'readback_verified':True,'changed_paths':len(files),'notes_changed':sum(p not in (r.GET+'/'+a.STATE,r.GET+'/'+n.STATE) for p in files),'changed_bytes':sum(map(len,files.values())),'transaction_bytes':plan['transaction_bytes'],**{k:plan[k] for k in ('parent_commit','candidate_commit','state_sha256','registry_sha256','approval_payload_sha256')},'plan_sha256':a.sha(a.encoded(plan)),'conflict_reason_counts':plan['conflict_reason_counts'],**plan['counts']}
        if apply:
            def readback(commit,files):return m.fetch_selected(remote,{p:('100644','blob',r.oid(data),len(data)) for p,data in files.items()},token,root/'readback')
            result.update(m.publish(reader,remote,plan,files,result['plan_sha256'],readback,checkpoint=checkpoint,verifier=verify_plan,writers_guard=lambda:m.disabled_writers(token=os.environ.get('GITHUB_TOKEN'))));result['status']='canonical_transaction_verified'
        return result

COUNTS=('changed_paths','notes_changed','changed_bytes','transaction_bytes','legacy_managed','legacy_conflicts','legacy_pending','existing_unmanaged','new_managed','new_selected','new_remaining','new_pending','source_details','details_unavailable','legacy_unchanged','legacy_source_version_only','legacy_source_updated','legacy_conflicted','legacy_details_used','legacy_outside_legacy_cohort','new_created','new_preview_imported','new_updated','new_state_only','new_unchanged','new_conflicted','new_detail_required')
HASHES=(('parent_commit',40),('candidate_commit',40),('state_sha256',64),('registry_sha256',64),('approval_payload_sha256',64),('plan_sha256',64))

def public_report(value):
    bad={'status':'canonical_failed','main_updated':None,'write_state':'unknown'}
    if not isinstance(value,dict):return bad
    if value.get('status') not in ('canonical_candidate_verified','canonical_transaction_verified'):
        clean=m.public_report(value);clean['status']='canonical_failed';return clean
    if value.get('readback_verified') is not True or type(value.get('cas_attempts')) is not int or value['cas_attempts'] not in (0,1) or type(value.get('main_updated')) is not bool or value['main_updated']!=(value['cas_attempts']==1):return bad
    if value['status']=='canonical_candidate_verified' and value['cas_attempts']!=0:return bad
    if any(type(value.get(k)) is not int or not 0<=value[k]<=m.MAX_CHANGED_BYTES for k in COUNTS):return bad
    if not 1<=value['legacy_managed']<=m.MAX_MANAGED or value['legacy_pending']!=9 or value['new_selected']>MAX_NEW or value['existing_unmanaged']>n.MAX_EXISTING_OWNERS or value['new_managed']>n.MAX_RECORDS:return bad
    if not 0<=value['notes_changed']<=value['changed_paths']<=value['legacy_managed']+value['new_managed']+2:return bad
    if value['new_created']+value['new_preview_imported']+value['new_updated']+value['legacy_source_updated']!=value['notes_changed']:return bad
    if not n.valid_reason_counts(value.get('conflict_reason_counts',{}),value['new_pending']):return bad
    result={k:value[k] for k in COUNTS};result['conflict_reason_counts']=value.get('conflict_reason_counts',{})
    try:
        for k,width in HASHES:result[k]=a.checked(value.get(k),width)
    except Exception:return bad
    for k in ('detail_requests','api_requests','http_attempts','http_retries'):
        if type(value.get(k)) is int and 0<=value[k]<=c.MAX_RAW:result[k]=value[k]
    result.update(status=value['status'],main_updated=value['main_updated'],cas_attempts=value['cas_attempts'],readback_verified=True,asset_downloads=0,notion_writes=0)
    return result

def read_report(path,checkpoint=None):
    try:value=strict_capture_json(a.bounded_file(Path(path),2*c.MIB),2*c.MIB)
    except Exception:value={}
    clean=public_report(value)
    if checkpoint and clean['status']=='canonical_failed':
        try:
            receipt=strict_capture_json(a.bounded_file(Path(checkpoint),4096),4096)
            keys={'contract','planned_parent_commit','planned_candidate_commit','planned_state_sha256','planned_plan_sha256','planned_payload_sha256','planned_changed_paths'}
            if set(receipt)!=keys or receipt['contract']!='get-legacy-write-attempt-1':return clean
            for key in keys-{'contract','planned_changed_paths'}:a.checked(receipt[key],40 if key.endswith('_commit') else 64)
            if type(receipt['planned_changed_paths']) is not int or not 1<=receipt['planned_changed_paths']<=n.MAX_RECORDS+2:return clean
            clean.update({k:v for k,v in receipt.items() if k!='contract'})
            if clean.get('write_state')=='unknown':clean['write_state']='uncertain'
        except Exception:pass
    return clean

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('expected-parent','legacy-sha256','registry-sha256','approved-payload-sha256','checkpoint','publish-report'):parser.add_argument('--'+key)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args(argv)
    if args.publish_report:print(json.dumps(read_report(args.publish_report,args.checkpoint)));return 0
    try:
        a.checked(args.expected_parent,40);a.checked(args.legacy_sha256,64)
        if args.registry_sha256:a.checked(args.registry_sha256,64)
        if args.apply:
            if not args.checkpoint:m.fail('manual_checkpoint')
            try:a.checked(args.approved_payload_sha256,64)
            except Exception:m.fail('manual_approval_required')
        m.disabled_writers(token=os.environ.get('GITHUB_TOKEN'))
        import sync_getnote_to_raw as sync
        from source_sync_rate import RequestPolicy
        key,client=sync.load_credentials();source=c.Source(key,client,RequestPolicy(seconds=35*60))
        result=run(os.environ.get('PRIVATE_VAULT_TOKEN'),source,args.expected_parent,legacy_sha=args.legacy_sha256,registry_sha=args.registry_sha256,apply=args.apply,approved_payload_sha256=args.approved_payload_sha256,checkpoint=(lambda plan:m.write_checkpoint(args.checkpoint,plan)) if args.checkpoint else None)
        result.update(detail_requests=source.details,api_requests=source.api_requests,http_attempts=source.http_attempts,http_retries=max(0,source.http_attempts-source.api_requests))
    except Exception as exc:result={'error':str(exc) if str(exc) in m.ERRORS else 'manual_failed','write_state':getattr(exc,'write_state','not_attempted')}
    clean=public_report(result);print(json.dumps(clean));return 1 if clean['status']=='canonical_failed' else 0

if __name__=='__main__':raise SystemExit(main())
