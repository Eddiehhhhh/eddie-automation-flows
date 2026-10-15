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
from get_v7_compat import (note_identity, api_id, timestamp_ms, near, mirror_index,
    recover_unchanged, local_mirror_matches_note)

class ParityFailure(RuntimeError):
    pass

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
    versions={}
    for item in summaries:
        ident=note_identity(item)
        version=timestamp_ms(item.get('updated_at'))
        if not ident or ident in versions or version is None:raise ParityFailure('invalid_list_identity_version')
        versions[ident]=version
    mirrors=read_mirrors(root);validate_manifest(root,mirrors);index,conflicts=mirror_index(mirrors)
    if conflicts:raise ParityFailure('duplicate_mirror_identity')
    current=deepcopy(state)
    counts=dict(enumerated=len(versions),cached_skipped=0,fresh_details=0,adopted_after_detail=0,new_or_changed=0,written_or_changed=0)
    if progress is not None:progress.append(counts)
    writer=writer_factory(root) if writer_factory else None
    preserved=set();changed_paths=set()
    for summary in summaries:
        ident=note_identity(summary)
        decision=recover_unchanged(summary,current,mirrors,now_ms)
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
            counts['new_or_changed']+=1
            if writer is None:raise ParityFailure('original_renderer_required')
            path=writer.write(summary,detail,mirror['path'] if mirror else None)
            current.setdefault('notesByCanonicalId',{})['api:'+ident]={
                'updatedAt':detail_version,'filePath':path,'stem':Path(path).stem,
                'deleted':False,'lastSyncedAt':now_ms}
            changed_paths.add(path)
            counts['written_or_changed']+=1
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
    return result

def replay(root,list_complete,fetch_detail,writer_factory=None,progress=None):
    initial=snapshot(root)
    first,c1,v1=one_pass(root,{},list_complete,fetch_detail,1000,writer_factory,progress)
    after_first=snapshot(root)
    second,c2,v2=one_pass(root,first,list_complete,fetch_detail,2000,writer_factory,progress)
    if v1!=v2:raise ParityFailure('source_changed_between_passes')
    if stable_state(first)!=stable_state(second):raise ParityFailure('second_pass_semantic_state_changed')
    if snapshot(root)!=after_first:raise ParityFailure('data_not_stable')
    return {'mode':'mirror_incremental_parity','first':c1,'second':c2,
        'data_bytes_paths_mtimes_unchanged':snapshot(root)==initial,'second_data_unchanged':True,'state_lastSyncedAt_changed':bool(c2['cached_skipped']),
        'source_content_complete_claimed':False,'renderer_exercised':bool(c1['written_or_changed'])}

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
