"""Isolated mirror-parity orchestration; no credentials, HTTP or publisher.

Inject a complete-list adapter and a detail adapter with shared request/budget
policy. Unchanged cold mirrors adopt only AFTER a fresh matching detail.
No substitute renderer: new/changed notes stop until the original contract is
available. The CLI is explicitly a synthetic local-evidence smoke test.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import shutil
from get_v7_compat import (note_identity, api_id, timestamp_ms, timestamp_error, timestamp_format, near, mirror_index,
    recover_unchanged, local_mirror_matches_note)

from get_mirror_render import transcript_pending, validate_note_shape

class ParityFailure(RuntimeError):
    pass

DIFF_KEYS=('pending_changed_notes','pending_identity_changed','pending_version_changed',
 'pending_body_changed','pending_audio_changed','pending_attachments_changed',
 'pending_other_changed','pending_signing_only')

def private_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def pending_signature(detail):
    # Keep raw and diagnostic hashes; only strict attachment signing metadata
    # has a separate source-metadata comparison, never a binary-content claim.
    from source_sync_urls import oss_v1_url
    groups={'body':{},'audio':{},'attachments':{},'other':{}}
    for key,value in detail.items():
        group='body' if key in ('content','title','ref_content') else ('audio' if key in ('audio','audio_url','audio_transcript','audio_duration') else ('attachments' if key in ('attachments','images','files','resources') else 'other'))
        groups[group][key]=value
    def signing_view(value,key=''):
        if isinstance(value,dict):return {k:signing_view(v,k) for k,v in value.items()}
        if isinstance(value,list):return [signing_view(v,key) for v in value]
        if key in ('url','original_url','download_url','play_url','audio_url'):
            parsed=oss_v1_url(value)
            if parsed:
                location,params,_=parsed
                return {'__diagnostic_oss_v1__':list(location),'signer':params['OSSAccessKeyId']}
        return value
    normalized=deepcopy(detail)
    # Only these descriptor positions have live signing-only evidence. Tag both
    # raw and normalized values so malformed object values cannot collide.
    attachments=normalized.get('attachments')
    if isinstance(attachments,list):
        for item in attachments:
            if not isinstance(item,dict):continue
            for key in ('url','original_url','download_url'):
                if key not in item:continue
                parsed=oss_v1_url(item[key])
                if parsed:
                    location,params,_=parsed
                    item[key]=['strict-oss-v1',list(location),params['OSSAccessKeyId']]
                else:item[key]=['raw-value',item[key]]
    return {'metadataHash':private_hash(normalized),'detailHash':private_hash(detail),'groups':{k:private_hash(v) for k,v in groups.items()},'signingHash':private_hash(signing_view(detail))}

def pending_differences(left,right):
    result={k:0 for k in DIFF_KEYS}
    result['pending_identity_changed']=len(set(left)^set(right))
    for ident in set(left)&set(right):
        a,b=left[ident],right[ident]
        result['pending_version_changed']+=int(a['updatedAt']!=b['updatedAt'])
        if a['detailHash']==b['detailHash']:continue
        result['pending_changed_notes']+=1
        for group in ('body','audio','attachments','other'):
            result['pending_'+group+'_changed']+=int(a['groups'][group]!=b['groups'][group])
        result['pending_signing_only']+=int(a['signingHash']==b['signingHash'])
    return result

def snapshot(root):
    result={}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ParityFailure('symlink')
        if path.is_file():
            result[path.relative_to(root).as_posix()]=(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns)
    return result

def read_mirrors(root):
    mirrors=[]
    for path in sorted(root.rglob('*.md')):
        if path.is_symlink():raise ParityFailure('symlink')
        text=path.read_text(encoding='utf-8')
        if not text.startswith('---\n'):continue
        lines=text.splitlines()
        closing=next((i for i in range(1,len(lines)) if lines[i]=='---'),None)
        if closing is None:raise ParityFailure('malformed_frontmatter')
        parts=['','\n'.join(lines[1:closing]),'\n'.join(lines[closing+1:])]
        fm={}
        for line in parts[1].splitlines():
            if not line or line.startswith((' ','\t')):continue
            key,sep,value=line.partition(':')
            if not sep or key not in {'note_id','canonicalId','updated_at'}:continue
            value=value.strip()
            if value.startswith('"'):
                try:value=json.loads(value)
                except ValueError:raise ParityFailure('malformed_frontmatter')
            elif value.startswith("'"):
                if not value.endswith("'"):raise ParityFailure('malformed_frontmatter')
                value=value[1:-1].replace("''", "'")
            elif any(c in value for c in ['#','&','*','[','{']):
                raise ParityFailure('unsupported_frontmatter_scalar')
            if key in fm:raise ParityFailure('duplicate_frontmatter_field')
            fm[key]=value
        ident=api_id(fm.get('note_id'))
        canonical=api_id(fm.get('canonicalId'))
        if ident and canonical.startswith('api:') and canonical[4:]!=ident:raise ParityFailure('conflicting_identity_fields')
        if not ident and canonical.startswith('api:'):ident=canonical[4:]
        # Non-API legacy sources are retained; caller chooses actual list IDs.
        if not ident:continue
        updated=fm.get('updated_at')
        if hasattr(updated,'isoformat'):updated=updated.isoformat()
        mirrors.append({'source_id':ident,'path':path.relative_to(root).as_posix(),
            'exists':True,'updated_at':updated,'body_present':bool(parts[2].strip())})
    return mirrors

def validate_manifest(root,mirrors):
    path=root/'03 State'/'manifest.json'
    if not path.exists():return False
    data=json.loads(path.read_text(encoding='utf-8'))
    rows=data.get('notes')
    if not isinstance(rows,list):raise ParityFailure('invalid_manifest')
    actual={m['path']:m for m in mirrors}
    seen=set()
    for row in rows:
        target=row.get('path')
        if not isinstance(target,str) or Path(target).is_absolute() or '..' in Path(target).parts:raise ParityFailure('manifest_path_escape')
        if target in seen:raise ParityFailure('duplicate_manifest_path')
        seen.add(target)
        if not (root/target).is_file():raise ParityFailure('manifest_missing_file')
        mirror=actual.get(target)
        if mirror and api_id(row.get('note_id'))!=mirror['source_id']:raise ParityFailure('manifest_identity_conflict')
        if mirror and mirror['source_id'].isdigit() and not near(timestamp_ms(row.get('updated_at')),timestamp_ms(mirror['updated_at'])):raise ParityFailure('manifest_version_conflict')
    return True

def _one_pass(root,state,list_complete,fetch_detail,now_ms,writer_factory=None,progress=None):
    """list_complete returns (summaries, terminal_verified).

    Adapter MUST prove terminal pagination, bound all requests, and reject cursor
    anomalies. A passed True is a programming contract, never authentication.
    State is a temporary execution overlay, not plugin data.json replacement.
    """
    before=snapshot(root)
    summaries,terminal=list_complete()
    if terminal is not True:raise ParityFailure('incomplete_list')
    counts=dict(enumerated=len(summaries),cached_skipped=0,fresh_details=0,adopted_after_detail=0,new_or_changed=0,written_or_changed=0,pending=0)
    if progress is not None:progress.append(counts)
    for item in summaries:
        key=timestamp_format(item.get('updated_at'));counts[key]=counts.get(key,0)+1
    versions={}
    for item in summaries:
        ident=note_identity(item)
        version=timestamp_ms(item.get('updated_at'))
        if not ident or ident in versions:raise ParityFailure('invalid_list_identity_version')
        if version is None:raise ParityFailure(timestamp_error(item.get('updated_at')))
        versions[ident]=version
    mirrors=read_mirrors(root);validate_manifest(root,mirrors);index,conflicts=mirror_index(mirrors)
    if conflicts:raise ParityFailure('duplicate_mirror_identity')
    current=deepcopy(state)
    prior_pending=current.pop('pendingById',{})
    pending={}
    writer=writer_factory(root) if writer_factory else None
    preserved=set();changed_paths=set()
    for summary in summaries:
        ident=note_identity(summary)
        decision=recover_unchanged(summary,current,mirrors,now_ms) if ident not in prior_pending else {'decision':'detail'}
        mirror=index.get(ident)
        # Additional migration-integrity guard: do not certify an empty mirror.
        if mirror and not mirror['body_present']:raise ParityFailure('empty_mirror')
        if decision['decision']=='skip':
            current=decision['state'];counts['cached_skipped']+=1;preserved.add(mirror['path']);continue
        if decision['decision']=='conflict':raise ParityFailure('duplicate_mirror_identity')
        detail=fetch_detail(ident);counts['fresh_details']+=1
        if note_identity(detail)!=ident:raise ParityFailure('detail_identity')
        detail_version=timestamp_ms(detail.get('updated_at'))
        if not near(detail_version,versions[ident]):raise ParityFailure('source_changed_during_pass')
        note={'sourceIds':{'api':ident},'updatedAt':detail_version}
        entry=(current.get('notesByCanonicalId') or {}).get('api:'+ident)
        # Literal adoptedLocal/wasDeleted scope. An established nondeleted entry
        # with a changed source version must take original normal write logic.
        eligible=entry is None or entry.get('deleted') is True
        trusted=entry.get('updatedAt',0) if entry and entry.get('deleted') else 0
        if eligible and local_mirror_matches_note(mirror,note,trusted):
            current.setdefault('notesByCanonicalId',{})['api:'+ident]={
                **(entry or {}),'updatedAt':detail_version,'filePath':mirror['path'],
                'stem':Path(mirror['path']).stem,'deleted':False,'lastSyncedAt':now_ms}
            counts['adopted_after_detail']+=1;preserved.add(mirror['path'])
        else:
            validate_note_shape({**summary,**detail})
            if transcript_pending({**summary,**detail}):
                counts['pending']+=1
                pending[ident]={'updatedAt':detail_version,**pending_signature(detail)}
                if mirror:preserved.add(mirror['path'])
                # Pending originals never become successfully mirrored state.
                continue
            if ident in prior_pending:raise ParityFailure('pending_changed_between_passes')
            counts['new_or_changed']+=1
            if writer is None:raise ParityFailure('original_renderer_required')
            path=writer.write(summary,detail,mirror['path'] if mirror else None)
            current.setdefault('notesByCanonicalId',{})['api:'+ident]={
                'updatedAt':detail_version,'filePath':path,'stem':Path(path).stem,
                'deleted':False,'lastSyncedAt':now_ms}
            changed_paths.add(path)
            counts['written_or_changed']+=1
    if pending:current['pendingById']=pending
    if writer is not None:writer.finish()
    after=snapshot(root)
    allowed=changed_paths|{'03 State/manifest.json','02 Index/全部笔记.md'} if changed_paths else set()
    if any(before[path]!=after.get(path) for path in before if path not in allowed):raise ParityFailure('old_unselected_data_modified')
    if any(before[path]!=after.get(path) for path in preserved):raise ParityFailure('unchanged_mirror_modified')
    if not counts['written_or_changed'] and after!=before:raise ParityFailure('unexpected_data_mutation')
    validate_manifest(root,read_mirrors(root))
    return current,counts,versions

def one_pass(root,state,list_complete,fetch_detail,now_ms,writer_factory=None,progress=None):
    if writer_factory is None:return _one_pass(root,state,list_complete,fetch_detail,now_ms,progress=progress)
    # Complete pass preparation in a disposable sibling; failed detail/assets/index
    # leaves original temp seed intact. This never targets the private repository.
    with tempfile.TemporaryDirectory(prefix='get-pass-',dir=root.parent) as folder:
        stage=Path(folder)/'stage';shutil.copytree(root,stage,symlinks=True)
        result=_one_pass(stage,state,list_complete,fetch_detail,now_ms,writer_factory,progress)
        if snapshot(stage)!=snapshot(root):
            backup=Path(folder)/'backup';os.replace(root,backup)
            try:os.replace(stage,root)
            except BaseException:
                os.replace(backup,root);raise
        return result

def stable_state(state):
    result=deepcopy(state)
    for item in result.get('notesByCanonicalId',{}).values():item.pop('lastSyncedAt',None)
    for item in result.get('pendingById',{}).values():
        for key in ('detailHash','groups','signingHash'):item.pop(key,None)
    return result

def replay(root,list_complete,fetch_detail,writer_factory=None,progress=None,initial_state=None,state_sink=None):
    initial=snapshot(root)
    first,c1,v1=one_pass(root,deepcopy(initial_state or {}),list_complete,fetch_detail,1000,writer_factory,progress)
    after_first=snapshot(root)
    second,c2,v2=one_pass(root,first,list_complete,fetch_detail,2000,writer_factory,progress)
    if v1!=v2:raise ParityFailure('source_changed_between_passes')
    if stable_state(first).get('pendingById',{})!=stable_state(second).get('pendingById',{}):
        differences=pending_differences(first.get('pendingById',{}),second.get('pendingById',{}))
        c2.update(differences)
        raise ParityFailure('pending_changed_between_passes')
    c2['pending_signing_refreshed']=sum(first['pendingById'][ident]['detailHash']!=item['detailHash'] for ident,item in second.get('pendingById',{}).items())
    if stable_state(first)!=stable_state(second):raise ParityFailure('second_pass_semantic_state_changed')
    if snapshot(root)!=after_first:raise ParityFailure('data_not_stable')
    if state_sink is not None:state_sink(deepcopy(second))
    return {'mode':'mirror_incremental_parity','first':c1,'second':c2,
        'data_bytes_paths_mtimes_unchanged':snapshot(root)==initial,'second_data_unchanged':True,'state_lastSyncedAt_changed':bool(c2['cached_skipped']),
        'pending_set_stable':True,'source_content_complete_claimed':False,'renderer_exercised':bool(c1['written_or_changed'])}

def diagnose_pending(root,list_complete,fetch_detail,progress=None):
    before=snapshot(root)
    summaries,terminal=list_complete()
    if terminal is not True:raise ParityFailure('incomplete_list')
    versions={}
    counts={'enumerated':len(summaries),'fresh_details':0,'pending':0,'diagnostic_probed':0,'diagnostic_rechecks':0,**{k:0 for k in DIFF_KEYS}}
    if progress is not None:progress.append(counts)
    for item in summaries:
        ident=note_identity(item);version=timestamp_ms(item.get('updated_at'))
        if not ident or ident in versions:raise ParityFailure('invalid_list_identity_version')
        if version is None:raise ParityFailure(timestamp_error(item.get('updated_at')))
        versions[ident]=version
    mirrors=read_mirrors(root);validate_manifest(root,mirrors);index,conflicts=mirror_index(mirrors)
    if conflicts:raise ParityFailure('duplicate_mirror_identity')
    for summary in summaries[:64]:
        ident=note_identity(summary);detail=fetch_detail(ident);counts['fresh_details']+=1;counts['diagnostic_probed']+=1
        if note_identity(detail)!=ident:raise ParityFailure('detail_identity')
        version=timestamp_ms(detail.get('updated_at'))
        if not near(version,versions[ident]):raise ParityFailure('source_changed_during_pass')
        mirror=index.get(ident)
        if mirror and not mirror['body_present']:raise ParityFailure('empty_mirror')
        if local_mirror_matches_note(mirror,{'sourceIds':{'api':ident},'updatedAt':version},0):continue
        validate_note_shape({**summary,**detail})
        if not transcript_pending({**summary,**detail}):continue
        counts['pending']+=1
        again=fetch_detail(ident);counts['diagnostic_rechecks']+=1
        if note_identity(again)!=ident:raise ParityFailure('detail_identity')
        again_version=timestamp_ms(again.get('updated_at'))
        if again_version is None:raise ParityFailure('source_changed_during_pass')
        validate_note_shape({**summary,**again})
        changes=pending_differences({ident:{'updatedAt':version,**pending_signature(detail)}},{ident:{'updatedAt':again_version,**pending_signature(again)}})
        for key,value in changes.items():counts[key]+=value
        if counts['pending']>=9:break
    if snapshot(root)!=before:raise ParityFailure('unexpected_data_mutation')
    return {'status':'pending_diagnostic_changed' if any(counts[k] for k in DIFF_KEYS) else ('pending_diagnostic_no_change' if counts['pending'] else 'pending_diagnostic_no_candidates'),
        'diagnostic_truncated':counts['diagnostic_probed']<len(summaries),'first':counts,'diagnostic_only':True,'source_content_complete_claimed':False}

def main():
    p=argparse.ArgumentParser();p.add_argument('--synthetic-local-smoke',type=Path,required=True)
    args=p.parse_args();source=args.synthetic_local_smoke.resolve()
    # Synthetic metadata is never represented as a current API response.
    mirrors=read_mirrors(source)
    summaries=[{'id':m['source_id'],'updated_at':m['updated_at']} for m in mirrors
        if m['source_id'].isdigit() and timestamp_ms(m['updated_at']) is not None]
    with tempfile.TemporaryDirectory(prefix='get-mirror-parity-') as temp:
        root=Path(temp)/'get';shutil.copytree(source,root,symlinks=True)
        byid={s['id']:s for s in summaries}
        result=replay(root,lambda:(deepcopy(summaries),True),lambda ident:deepcopy(byid[ident]))
    result['synthetic_only']=True;result['actual_source_calls']=0
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()

def cloud_version_valid(value):
    return type(value) is int and 0<value<=253402300799999

def cloud_export_version(value):
    if type(value) not in (int,float) or not (0<value<=253402300799999) or int(value)!=value:raise ParityFailure('invalid_cloud_state')
    return int(value)

# LOCAL PRODUCTION DRAFT ONLY: proposed one-file state, not deployed or approved.
def export_cloud_state(root,state):
    """Serialize only successfully verified mirror versions; no source bodies/URLs.

    The caller must commit this exact payload atomically with its verified data.
    Generating it in a preview is not a published watermark or authorization.
    """
    mirrors=read_mirrors(root);index,conflicts=mirror_index(mirrors)
    if conflicts:raise ParityFailure('duplicate_mirror_identity')
    if not validate_manifest(root,mirrors):raise ParityFailure('invalid_manifest')
    notes={}
    for canonical,entry in state.get('notesByCanonicalId',{}).items():
        ident=canonical[4:] if canonical.startswith('api:') else ''
        mirror=index.get(ident)
        if not ident or not mirror or entry.get('deleted') or not mirror['body_present']:
            raise ParityFailure('invalid_cloud_state')
        if entry.get('filePath')!=mirror['path'] or not near(entry.get('updatedAt'),timestamp_ms(mirror['updated_at'])):
            raise ParityFailure('invalid_cloud_state')
        notes[canonical]={'updatedAt':cloud_export_version(entry['updatedAt']),'filePath':mirror['path'],
            'stem':Path(mirror['path']).stem,'deleted':False,
            'mirrorSha256':hashlib.sha256((root/mirror['path']).read_bytes()).hexdigest()}
    pending={ident:{'updatedAt':cloud_export_version(entry['updatedAt'])} for ident,entry in state.get('pendingById',{}).items()}
    payload=load_cloud_state(root,{'version':1,'contract':'get-v7-cloud-mirror-1','source_system':'getnote','notesByCanonicalId':notes,'pendingById':pending})
    if len(json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())>2*1024*1024:raise ParityFailure('invalid_cloud_state')
    return payload

def load_cloud_state(root,payload):
    """Unknown/edited/missing mirrors are never silently treated as unchanged."""
    if payload is None:return {}
    if not isinstance(payload,dict) or set(payload)!={'version','contract','source_system','notesByCanonicalId','pendingById'} or type(payload.get('version')) is not int or payload.get('version')!=1 or payload.get('contract')!='get-v7-cloud-mirror-1' or payload.get('source_system')!='getnote':
        raise ParityFailure('invalid_cloud_state')
    notes=payload['notesByCanonicalId'];pending=payload['pendingById']
    if not isinstance(notes,dict) or not isinstance(pending,dict) or len(notes)>2500 or len(pending)>2500:raise ParityFailure('invalid_cloud_state')
    if len(set(notes)|{'api:'+str(ident) for ident in pending})>2500:raise ParityFailure('invalid_cloud_state')
    mirrors=read_mirrors(root);index,conflicts=mirror_index(mirrors)
    if conflicts:raise ParityFailure('duplicate_mirror_identity')
    if not validate_manifest(root,mirrors):raise ParityFailure('invalid_manifest')
    for canonical,entry in notes.items():
        if not isinstance(canonical,str) or not canonical.startswith('api:') or not isinstance(entry,dict):raise ParityFailure('invalid_cloud_state')
        ident=canonical[4:];mirror=index.get(ident)
        if not ident.isascii() or not ident.isdigit() or len(ident)>64:raise ParityFailure('invalid_cloud_state')
        if set(entry)!={'updatedAt','filePath','stem','deleted','mirrorSha256'} or entry['deleted'] is not False or not mirror or not mirror['body_present']:
            raise ParityFailure('invalid_cloud_state')
        if not cloud_version_valid(entry['updatedAt']) or entry['filePath']!=mirror['path'] or entry['stem']!=Path(mirror['path']).stem or not near(entry['updatedAt'],timestamp_ms(mirror['updated_at'])):
            raise ParityFailure('invalid_cloud_state')
        if entry['mirrorSha256']!=hashlib.sha256((root/mirror['path']).read_bytes()).hexdigest():raise ParityFailure('cloud_mirror_changed')
    for ident,entry in pending.items():
        if not isinstance(ident,str) or not ident.isascii() or not ident.isdigit() or len(ident)>64 or not isinstance(entry,dict) or set(entry)!={'updatedAt'} or not cloud_version_valid(entry['updatedAt']):raise ParityFailure('invalid_cloud_state')
    return deepcopy(payload)

def read_cloud_state(root,path):
    """Bound and strictly parse a state blob from the same pinned private checkout.

    The caller must establish repository/commit provenance before calling; this
    function cannot authenticate a user or make an arbitrary state blob trusted.
    """
    path=Path(path)
    if any(parent.is_symlink() for parent in path.parents):raise ParityFailure('invalid_cloud_state')
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):raise ParityFailure('invalid_cloud_state')
    if not path.exists():return {}
    if not path.is_file():raise ParityFailure('invalid_cloud_state')
    with path.open('rb') as stream:data=stream.read(2*1024*1024+1)
    if len(data)>2*1024*1024:raise ParityFailure('invalid_cloud_state')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ParityFailure('invalid_cloud_state')
            result[key]=value
        return result
    def reject(_):raise ParityFailure('invalid_cloud_state')
    try:payload=json.loads(data,object_pairs_hook=unique,parse_constant=reject)
    except (ValueError,UnicodeError,RecursionError):raise ParityFailure('invalid_cloud_state')
    if payload is None:raise ParityFailure('invalid_cloud_state')
    return load_cloud_state(root,payload)


def prepare_private_preview(vault,output,expected_commit,list_complete,fetch_detail,writer_factory,progress=None):
    """Retain a private candidate plus exact diff; never commit/push or apply.

    Expected SHA binds a previously authenticated checkout, not user approval.
    Caller must verify private repo visibility and authorized source access.
    """
    import subprocess
    import re
    vault=Path(vault);output=Path(output)
    if any(part.is_symlink() for path in (vault,output) for part in (path,*path.parents)):raise ParityFailure('invalid_preview_source')
    vault=vault.resolve()
    if not re.fullmatch('[a-f0-9]{40}',expected_commit):raise ParityFailure('invalid_preview_source')
    def git(*args):
        result=subprocess.run(['git','-C',str(vault),*args],capture_output=True,check=False)
        if result.returncode:raise ParityFailure('invalid_preview_source')
        return result.stdout
    if git('rev-parse','HEAD').decode().strip()!=expected_commit:raise ParityFailure('preview_head_changed')
    if git('status','--porcelain','--untracked-files=all','--','Raw/03 Get'):raise ParityFailure('dirty_preview_source')
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(vault) or vault.is_relative_to(output.resolve()):raise ParityFailure('invalid_preview_output')
    source=vault/'Raw'/'03 Get'
    if any(part.is_symlink() for part in (source,*source.parents)):raise ParityFailure('invalid_preview_source')
    before=snapshot(source)
    tree={}
    for record in git('ls-tree','-rz',expected_commit,'--','Raw/03 Get').split(b'\0'):
        if not record:continue
        metadata,name=record.split(b'\t',1);mode,kind,sha=metadata.decode().split()
        if mode not in ('100644','100755') or kind!='blob':raise ParityFailure('invalid_preview_source')
        path=name.decode();relative=Path(path).relative_to('Raw/03 Get').as_posix();tree[relative]=sha
    if set(tree)!=set(before):raise ParityFailure('untracked_preview_source')
    for relative,sha in tree.items():
        data=(source/relative).read_bytes()
        if hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()!=sha:raise ParityFailure('dirty_preview_source')
    if not validate_manifest(source,read_mirrors(source)):raise ParityFailure('invalid_manifest')
    output.mkdir(parents=True,mode=0o700)
    try:
        candidate=output/'candidate';shutil.copytree(source,candidate,symlinks=True)
        state_path=Path('03 State/cloud-sync-state.json')
        initial=read_cloud_state(candidate,candidate/state_path);accepted=[]
        result=replay(candidate,list_complete,fetch_detail,writer_factory,progress,initial_state=initial,state_sink=accepted.append)
        state=export_cloud_state(candidate,accepted[0])
        encoded=json.dumps(state,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()+b'\n'
        if len(encoded)>2*1024*1024:raise ParityFailure('invalid_cloud_state')
        target=candidate/state_path
        if not target.exists() or target.read_bytes()!=encoded:target.write_bytes(encoded)
        pending=sorted(state['pendingById'])
        receipt={'source_system':'getnote','scope':'full','enumerated':result['first']['enumerated'],'complete':not pending,'pending':len(pending)}
        for rel,value in [('03 State/receipt.json',receipt),('03 State/pending.json',{'source_system':'getnote','pending_ids':pending})]:
            path=candidate/rel;data=(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
            if not path.exists() or path.read_bytes()!=data:path.write_bytes(data)
        after=snapshot(candidate)
        if set(before)-set(after):raise ParityFailure('preview_deleted_path')
        changes=[]
        for rel in sorted(after):
            old=before.get(rel);new=after[rel]
            if old and old[0]==new[0]:continue
            # Ask Git's actual ignore rules, never force-add ignored new notes.
            ignored=subprocess.run(['git','-C',str(vault),'check-ignore','--no-index','-q','--','Raw/03 Get/'+rel],capture_output=True)
            if ignored.returncode==0:raise ParityFailure('preview_ignored_path')
            if ignored.returncode!=1:raise ParityFailure('invalid_preview_source')
            old_bytes=(source/rel).read_bytes() if old else None;new_bytes=(candidate/rel).read_bytes()
            def blob(data):return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest() if data is not None else None
            changes.append({'path':'Raw/03 Get/'+rel,'action':'modify' if old else 'add','old_blob':blob(old_bytes),'new_blob':blob(new_bytes),'old_sha256':old[0] if old else None,'new_sha256':new[0],'old_bytes':len(old_bytes) if old_bytes is not None else 0,'new_bytes':len(new_bytes)})
        if git('rev-parse','HEAD').decode().strip()!=expected_commit or snapshot(source)!=before:raise ParityFailure('preview_source_changed')
        # This file is PRIVATE, includes paths; never print or upload publicly.
        plan={'version':1,'scope':'get-private-preview','source_commit':expected_commit,'changes':changes,'pending':len(pending),'status':'partial' if pending else 'complete','published':False,'apply_ready':False,'reason':'production_apply_jsonl_health_and_publish_gates_pending'}
        (output/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
        return result,{'changed_paths':len(changes),'pending':len(pending),'published':False,'preview_only':True}
    except BaseException:
        shutil.rmtree(output)
        raise
