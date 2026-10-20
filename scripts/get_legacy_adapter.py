"""Offline transactional legacy adoption/update proposals; no network or publisher.

All commit and hash anchors are caller-supplied external trust inputs. This
candidate contract is deliberately incompatible with the live faithful loader.
A candidate is never an authorization to adopt state or publish a watermark.
"""
from copy import deepcopy
import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

import get_legacy_owned_spans as owned
import get_legacy_capture as capture
import get_private_preview_retention as retention
from get_mirror_incremental_replay import (ParityFailure, capture_json_bytes,
    strict_capture_json, validate_legacy_census, observation_detail, raw_source_version)
from get_v7_compat import note_identity

CONTRACT='get-legacy-transaction-proposal-1'
STATE='03 State/legacy-reconciliation-state.json'
OVERLAY_CONTRACT='get-legacy-mutable-metadata-1'
MUTABLE_METADATA=('version','display_title','timestamps.edited_at','timestamps.record_updated_at')
MAX_STATE=8*1024*1024
MAX_TREE=300*1024*1024
IMMUTABLE=('contract','origin','local_proposal_only','blocked_from_apply','mirror_adopted',
 'identity','filePath','capture_evidence','metadata_sha256','ownership_shape_sha256','history_appended')
CONFLICTS={'legacy_current_changed','legacy_missing','baseline_missing','source_missing',
 'source_version_mismatch','detail_identity','legacy_owned_text_edited','source_metadata_changed',
 'source_text_missing','source_text_unmapped','source_alias_diverged','legacy_named_text_unmapped',
 'legacy_references_unresolved','legacy_structure_ambiguous','source_shape_unsupported',
 'source_capture_unresolved','source_text_empty','legacy_text_scalar_unsupported','legacy_note_budget','legacy_identity_changed'}

def fail(code):raise ParityFailure(code)
def sha(data):return hashlib.sha256(data).hexdigest()
def encoded(value):return capture_json_bytes(value)
def checked(value,width):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{'+str(width)+'}',value):fail('adapter_anchor')
    return value

def path_ok(value):
    if not isinstance(value,str):fail('adapter_path')
    retention.safe_path(retention.GET+'/'+value)
    if str(Path(value))!=value:fail('adapter_path')
    return value

class OfflineGit:
    """Read-only Git object access, with every transport explicitly disabled."""
    def __init__(self,root):
        self.local=retention.LocalGit(root)
        env=self.local.env;n=int(env['GIT_CONFIG_COUNT'])
        env.update(GIT_NO_LAZY_FETCH='1',GIT_CONFIG_COUNT=str(n+1))
        env['GIT_CONFIG_KEY_'+str(n)]='protocol.allow';env['GIT_CONFIG_VALUE_'+str(n)]='never'
        self.cache={}
    def tree(self,commit):
        checked(commit,40)
        if commit not in self.cache:
            if self.local.run('cat-file','-t',commit).strip()!=b'commit':fail('adapter_anchor')
            self.cache[commit]=self.local.tree(commit)
        return self.cache[commit]
    def read(self,commit,path,maximum=MAX_TREE):
        row=self.tree(commit).get(path)
        if not row or row[:2]!=('100644','blob'):fail('adapter_missing_blob')
        size=self.local.run('cat-file','-s',row[2]).decode().strip()
        if not size.isdigit() or int(size)>maximum:fail('adapter_blob_budget')
        data=self.local.blob(row[2])
        if len(data)!=int(size) or retention.oid(data)!=row[2]:fail('adapter_blob_readback')
        return data
    def read_many(self,commit,limits):
        """Batch exact immutable paths; retain every size, mode and OID check.

        No blob bytes or missing-object results survive this call. Preflight
        sizes before content, then bound each batch by 64 objects / 8 MiB
        (a single caller-authorized larger object is read alone).
        """
        tree=self.tree(commit);paths={};caps={}
        for path,maximum in limits.items():
            row=tree.get(path)
            if not row or row[:2]!=('100644','blob'):fail('adapter_missing_blob')
            blob=checked(row[2],40);paths[path]=blob
            caps[blob]=min(maximum,caps.get(blob,maximum))
        if not caps:return {}
        ids=list(caps);request=('\n'.join(ids)+'\n').encode()
        probe=self.local.command('cat-file','--batch-check=%(objectname) %(objecttype) %(objectsize)',data=request)
        if probe.returncode or probe.stderr:fail('adapter_blob_readback')
        rows=probe.stdout.splitlines();sizes={}
        if len(rows)!=len(ids):fail('adapter_blob_readback')
        for blob,row in zip(ids,rows):
            parts=row.split()
            if len(parts)!=3 or parts[:2]!=[blob.encode(),b'blob'] or not parts[2].isdigit():fail('adapter_blob_readback')
            size=int(parts[2])
            if size>caps[blob]:fail('adapter_blob_budget')
            sizes[blob]=size
        if sum(sizes[blob] for blob in paths.values())>MAX_TREE:fail('adapter_tree_budget')
        values={};offset=0
        while offset<len(ids):
            batch=[];total=0
            while offset<len(ids) and len(batch)<64:
                blob=ids[offset]
                if batch and total+sizes[blob]>MAX_STATE:break
                batch.append(blob);total+=sizes[blob];offset+=1
            result=self.local.command('cat-file','--batch',data=('\n'.join(batch)+'\n').encode())
            if result.returncode or result.stderr:fail('adapter_blob_readback')
            cursor=0
            for blob in batch:
                header=(blob+' blob '+str(sizes[blob])+'\n').encode();start=cursor+len(header);end=start+sizes[blob]
                if result.stdout[cursor:start]!=header or result.stdout[end:end+1]!=b'\n':fail('adapter_blob_readback')
                data=result.stdout[start:end]
                if len(data)!=sizes[blob] or retention.oid(data)!=blob:fail('adapter_blob_readback')
                values[blob]=data;cursor=end+1
            if cursor!=len(result.stdout):fail('adapter_blob_readback')
        return {path:values[blob] for path,blob in paths.items()}
    def source_get_tree(self,commit):
        self.tree(commit)
        return checked(self.local.run('rev-parse',commit+':'+retention.GET).decode().strip(),40)
    def tree_entries(self,tree,recursive=True):
        rows=[]
        for item in self.local.run('ls-tree','-rlz',checked(tree,40)).split(b'\0'):
            if not item:continue
            metadata,path=item.split(b'\t',1);mode,kind,oid,size=metadata.decode().split()
            rows.append({'path':path.decode(),'mode':mode,'type':kind,'sha':oid,'size':int(size) if size.isdigit() else None})
        return rows
    def baseline(self,prior_commit,path,blob,digest):
        """Prove the baseline belonged to this path in pinned prior history."""
        full=retention.GET+'/'+path_ok(path);checked(blob,40);checked(digest,64)
        def candidates():
            yield checked(prior_commit,40)
            yield from self.local.run('rev-list','--max-count=256',prior_commit,'--',full).decode().splitlines()
        for commit in candidates():
            if self.tree(commit).get(full)==('100644','blob',blob):
                probe=self.local.command('cat-file','--batch-check=%(objectname) %(objecttype) %(objectsize)',data=(blob+'\n').encode())
                if probe.returncode or probe.stderr:fail('adapter_baseline_readback')
                if probe.stdout==(blob+' missing\n').encode():fail('baseline_missing')
                data=self.read(commit,full,owned.MAX_NOTE)
                if sha(data)!=digest:fail('adapter_baseline_hash')
                return data
        fail('baseline_missing')

class CaptureEvidence:
    def __init__(self,repo,anchor):
        if not isinstance(anchor,dict) or set(anchor)!={'capture_commit','census_sha256','ledger_sha256'}:fail('adapter_anchor')
        checked(anchor['capture_commit'],40);checked(anchor['census_sha256'],64);checked(anchor['ledger_sha256'],64)
        self.anchor=deepcopy(anchor);self.repo=repo;head=anchor['capture_commit']
        census_bytes=repo.read(head,capture.CENSUS,16*capture.MIB);ledger_bytes=repo.read(head,capture.LEDGER,capture.MIB)
        if sha(census_bytes)!=anchor['census_sha256'] or sha(ledger_bytes)!=anchor['ledger_sha256']:fail('adapter_anchor')
        self.census=strict_capture_json(census_bytes,16*capture.MIB);self.ledger=strict_capture_json(ledger_bytes,capture.MIB)
        validate_legacy_census(self.census)
        if encoded(self.census)!=census_bytes or encoded(self.ledger)!=ledger_bytes:fail('adapter_anchor')
        if repo.source_get_tree(self.census['source_commit'])!=self.census['get_tree']:fail('adapter_source_tree')
        self.records={}
        receipts=repo.read_many(head,{capture.record_path(i):5*capture.MIB for i in self.ledger['records']})
        for ident in self.ledger['records']:
            data=receipts.pop(capture.record_path(ident))
            value=strict_capture_json(data,5*capture.MIB)
            if encoded(value)!=data:fail('adapter_receipt')
            self.records[ident]=value
        if capture.validate_ledger(self.census,self.ledger,self.records) or not self.ledger['finalized']:fail('adapter_incomplete_capture')
        capture.verify_scope_history(repo.local,head,self.census,self.ledger,repo)
        self.old={};self.initial={};self.conflicts={};self.source={}
        old_notes=repo.read_many(self.census['source_commit'],{retention.GET+'/'+row['filePath']:owned.MAX_NOTE for row in self.census['eligible'].values()})
        for ident,mirror in self.census['eligible'].items():
            old=old_notes[retention.GET+'/'+mirror['filePath']]
            if retention.oid(old)!=mirror['gitBlob'] or sha(old)!=mirror['sha256']:fail('adapter_old_hash')
            self.old[ident]=old
            if ident not in self.records:
                self.conflicts[ident]='source_missing';continue
            record=self.records[ident]['observation']
            if self.records[ident]['proposal']!=capture.proposal(record,self.census,old):fail('adapter_receipt')
            self.source[ident]={**self.census['summaries'][ident],**observation_detail(record)}
            try:self.initial[ident]=owned.propose_ownership(self.census,record,old,observation_sha256=sha(encoded(record)),_prevalidated_census=True,census_sha256=anchor['census_sha256'])
            except ParityFailure as exc:
                if str(exc) not in CONFLICTS:raise
                self.conflicts[ident]=str(exc)


def bounded_file(path,maximum):
    """Preflight regular-file size, cap the read, and reject read-time changes."""
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(descriptor,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:fail('adapter_alias')
        if before.st_size>maximum:fail('adapter_file_budget')
        data=stream.read(before.st_size+1);after=os.fstat(stream.fileno())
        if len(data)!=before.st_size or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):fail('adapter_file_changed')
        return data


def tree_snapshot(root):
    root=retention.regular_root(root);out={};inodes=set();total=0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():fail('adapter_alias')
        if path.is_dir():continue
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or (info.st_dev,info.st_ino) in inodes:fail('adapter_alias')
        inodes.add((info.st_dev,info.st_ino));rel=path.relative_to(root).as_posix();path_ok(rel)
        if total+info.st_size>MAX_TREE:fail('adapter_tree_budget')
        data=bounded_file(path,MAX_TREE-total);total+=len(data)
        out[rel]={'sha256':sha(data),'blob':retention.oid(data),'bytes':len(data)}
    return out

def root_digest(snapshot):return sha(encoded(snapshot))

def initial_state(evidence,current):
    rows={};conflicts=deepcopy(evidence.conflicts)
    for ident,ownership in evidence.initial.items():
        path=ownership['filePath'];data=current.get(path)
        if data!=evidence.old[ident]:conflicts[ident]='legacy_missing' if data is None else 'legacy_current_changed';continue
        rows[ident]={'ownership':deepcopy(ownership),'baseline_blob':retention.oid(data),
          'summary_sha256':sha(encoded(evidence.census['summaries'][ident]))}
    return {'contract':CONTRACT,'local_proposal_only':True,'mirror_adopted':False,'apply_ready':False,
      'capture_anchor':deepcopy(evidence.anchor),'adoption_ids':sorted(rows),'legacyById':rows,
      'conflictsById':conflicts,'pending_ids':evidence.census['pending_ids']}


def validate_state(payload,expected_sha,evidence):
    checked(expected_sha,64)
    if sha(encoded(payload))!=expected_sha:fail('adapter_state_hash')
    keys={'contract','local_proposal_only','mirror_adopted','apply_ready','capture_anchor','adoption_ids','legacyById','conflictsById','pending_ids'}
    if not isinstance(payload,dict) or set(payload)!=keys or payload['contract']!=CONTRACT or payload['local_proposal_only'] is not True or payload['mirror_adopted'] is not False or payload['apply_ready'] is not False or payload['capture_anchor']!=evidence.anchor or payload['pending_ids']!=evidence.census['pending_ids']:fail('adapter_state_schema')
    rows=payload['legacyById'];conflicts=payload['conflictsById'];ids=payload['adoption_ids']
    if not isinstance(rows,dict) or not isinstance(conflicts,dict) or not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or ids!=sorted(set(ids)) or set(ids)!=set(rows) or set(rows)-set(evidence.initial):fail('adapter_state_schema')
    if set(rows)|set(conflicts)!=set(evidence.census['eligible']) or any(not isinstance(reason,str) or reason not in CONFLICTS for reason in conflicts.values()):fail('adapter_state_schema')
    if any(conflicts.get(i)!=reason for i,reason in evidence.conflicts.items()):fail('adapter_state_schema')
    paths=set()
    for ident,row in rows.items():
        if not isinstance(row,dict) or set(row) not in ({'ownership','baseline_blob','summary_sha256'},{'ownership','baseline_blob','summary_sha256','source_overlay'}):fail('adapter_state_schema')
        if 'source_overlay' in row and row['source_overlay'] is None:fail('adapter_state_schema')
        validate_overlay(row.get('source_overlay'),evidence.source[ident])
        checked(row['baseline_blob'],40);checked(row['summary_sha256'],64);state=row['ownership'];original=evidence.initial[ident]
        if not isinstance(state,dict) or any(state.get(k)!=original[k] for k in IMMUTABLE):fail('adapter_ownership_changed')
        if state['filePath'] in paths:fail('adapter_state_schema')
        paths.add(state['filePath'])
        if owned.ownership_shape_hash(state.get('owned_spans'))!=original['ownership_shape_sha256']:fail('adapter_ownership_changed')
    if len(encoded(payload))>MAX_STATE:fail('adapter_state_budget')
    return deepcopy(payload)


def metadata_values(note):
    values={}
    for path in MUTABLE_METADATA:
        parts=path.split('.');parent=note
        for key in parts[:-1]:
            if not isinstance(parent,dict) or key not in parent:parent=None;break
            parent=parent[key]
        if isinstance(parent,dict) and parts[-1] in parent:values[path]=deepcopy(parent[parts[-1]])
    return values


def validate_metadata_values(values):
    for path,value in values.items():
        if path=='version':
            if type(value) is not int or value<0:fail('source_metadata_changed')
        elif path=='display_title':
            if not isinstance(value,str):fail('source_metadata_changed')
        else:
            try:raw_source_version(value)
            except ParityFailure:fail('source_metadata_changed')


def set_metadata_values(note,values):
    note=deepcopy(note)
    for path,value in values.items():
        parts=path.split('.');parent=note
        for key in parts[:-1]:
            if not isinstance(parent,dict) or not isinstance(parent.get(key),dict):fail('source_metadata_changed')
            parent=parent[key]
        if parts[-1] not in parent:fail('source_metadata_changed')
        parent[parts[-1]]=deepcopy(value)
    return note


def validate_overlay(overlay,original):
    if overlay is None:return
    if not isinstance(overlay,dict) or set(overlay)!={'contract','fields','actual_source_sha256'} or overlay['contract']!=OVERLAY_CONTRACT or not isinstance(overlay['fields'],dict):fail('adapter_state_schema')
    checked(overlay['actual_source_sha256'],64)
    initial=metadata_values(original);values=overlay['fields']
    if set(values)!=set(initial) or encoded(values)==encoded(initial):fail('adapter_state_schema')
    try:
        validate_metadata_values(initial);validate_metadata_values(values)
        if any(initial[key] not in (None,'') and values[key] in (None,'') for key in initial):fail('source_metadata_changed')
    except ParityFailure:fail('adapter_state_schema')


def actual_source(row,core,original):
    overlay=row.get('source_overlay');validate_overlay(overlay,original)
    if overlay is None:return core
    actual=set_metadata_values(core,overlay['fields'])
    if sha(encoded(actual))!=overlay['actual_source_sha256']:fail('adapter_state_source_hash')
    return actual


def normalize_source(note,previous,original):
    """Only four explicitly mutable metadata values may evolve.

    The span state remains bound to capture metadata; an independent outer
    receipt binds the full actual source. No timestamp conversion or source
    text normalization occurs. All other metadata still passes the exact hash.
    """
    initial=metadata_values(original);old=metadata_values(previous);new=metadata_values(note)
    if set(new)!=set(initial) or set(old)!=set(initial):fail('source_metadata_changed')
    if encoded(new)!=encoded(initial) or encoded(old)!=encoded(initial):
        validate_metadata_values(initial);validate_metadata_values(old);validate_metadata_values(new)
        if any(old[key] not in (None,'') and new[key] in (None,'') for key in old):fail('source_metadata_changed')
    return set_metadata_values(note,initial)


def source_overlay(note,original):
    values=metadata_values(note)
    if encoded(values)==encoded(metadata_values(original)):return None
    return {'contract':OVERLAY_CONTRACT,'fields':values,'actual_source_sha256':sha(encoded(note))}


def reconstructed_source(evidence,ident,ownership,baseline):
    """Known text evolves; every non-text field remains pinned to capture."""
    note=deepcopy(evidence.source[ident]);_,slots,_=owned.regions(baseline)
    for span in ownership['owned_spans']:
        slot=slots.get(span['region'])
        if not slot:fail('adapter_state_schema')
        text=owned._slot_text(slot)
        for name in span['fields']:
            parts=name.split('.');parent=note
            for key in parts[:-1]:parent=parent[key]
            parent[parts[-1]]=text
    note['updated_at']=ownership['source_version']['value']
    if sha(encoded(note))!=ownership['source_sha256']:fail('adapter_state_source_hash')
    # Exercises the full owned-state schema/receipt/byte-position validator,
    # including unchanged records, before trusting cached source versions.
    owned.propose_update(ownership,baseline,baseline,note,state_sha256=sha(encoded(ownership)))
    return note


def source_snapshot(summaries,terminal):
    if terminal is not True or not isinstance(summaries,list) or not 0<=len(summaries)<=2500:fail('adapter_incomplete_list')
    out={}
    for summary in summaries:
        try:ident=note_identity(summary)
        except (ValueError,AttributeError,TypeError):fail('adapter_source_identity')
        if ident in out:fail('adapter_duplicate_source')
        raw_source_version(summary.get('updated_at'));out[ident]=summary
    return out


def propose_updates(evidence,state,state_sha,current,repo,prior_commit,summaries,terminal,details):
    state=validate_state(state,state_sha,evidence);items=source_snapshot(summaries,terminal)
    if repo.read(prior_commit,retention.GET+'/'+STATE,MAX_STATE)!=encoded(state) or current.get(STATE)!=encoded(state):fail('adapter_state_commit')
    if not isinstance(details,dict):fail('adapter_detail_schema')
    rows=state['legacyById'];conflicts={i:r for i,r in state['conflictsById'].items() if i not in rows}
    changed={};counts={'unchanged':0,'source_version_only':0,'source_updated':0,'conflicted':0,'details_used':0,
      'outside_legacy_cohort':len(set(items)-set(evidence.census['eligible'])-set(evidence.census['pending_ids']))}
    needed=set()
    for ident,row in rows.items():
        ownership=row['ownership'];summary=items.get(ident)
        if summary is not None and (raw_source_version(summary.get('updated_at'))!=ownership['source_version'] or sha(encoded(summary))!=row['summary_sha256']):needed.add(ident)
    if set(details)!=needed:fail('adapter_detail_set')
    for ident,row in rows.items():
        ownership=row['ownership'];path=ownership['filePath'];summary=items.get(ident)
        try:
            baseline=repo.baseline(prior_commit,path,row['baseline_blob'],ownership['mirror_sha256'])
            core=reconstructed_source(evidence,ident,ownership,baseline)
            previous=actual_source(row,core,evidence.source[ident])
            if path not in current:fail('legacy_missing')
            if summary is None:fail('source_missing')
            try:_,_,current_header=owned.regions(current[path])
            except (ValueError,UnicodeError):fail('legacy_structure_ambiguous')
            owned.text_values(previous)
            if owned.has_unresolved_references(previous,current[path],current_header):fail('legacy_references_unresolved')
            note=previous
            if ident in needed:
                detail=details[ident];counts['details_used']+=1
                try:detail_id=note_identity(detail)
                except (ValueError,AttributeError,TypeError):fail('detail_identity')
                if detail_id!=ident:fail('detail_identity')
                try:detail_version=raw_source_version(detail.get('updated_at'))
                except ParityFailure:fail('source_version_mismatch')
                if detail_version!=raw_source_version(summary.get('updated_at')):fail('source_version_mismatch')
                note={**summary,**detail}
            owned.text_values(note)
            if owned.has_unresolved_references(note,current[path],current_header):fail('legacy_references_unresolved')
            normalized=normalize_source(note,previous,evidence.source[ident])
            result=owned.propose_update(ownership,baseline,current[path],normalized,state_sha256=sha(encoded(ownership)))
            classification=result['classification']
            if classification=='unchanged' and encoded(note)!=encoded(previous):classification='source_version_only'
            counts[classification]+=1
            if result['bytes']!=current[path]:changed[path]=result['bytes']
            rows[ident]={'ownership':result['proposed_state'],'baseline_blob':retention.oid(result['bytes']),
                         'summary_sha256':sha(encoded(summary))}
            overlay=source_overlay(note,evidence.source[ident])
            if overlay is not None:rows[ident]['source_overlay']=overlay
        except ParityFailure as exc:
            if str(exc) not in CONFLICTS:raise
            conflicts[ident]=str(exc);counts['conflicted']+=1
    state['conflictsById']=conflicts
    validate_state(state,sha(encoded(state)),evidence) # Trusted producer output only.
    return state,changed,counts


def rename_noreplace(source,target):
    """Atomic local publication without replacing even an empty foreign dir.

    The supported execution target is Linux. An unavailable no-replace primitive
    fails closed; there is no unsafe check-then-replace fallback.
    """
    try:
        rename=ctypes.CDLL(None,use_errno=True).renameat2
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    except (AttributeError,OSError):fail('adapter_atomic_unavailable')
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];rename.restype=ctypes.c_int
    source=Path(source);target=Path(target)
    source_fd=os.open(source.parent,flags)
    try:
        target_fd=os.open(target.parent,flags)
        try:
            if rename(source_fd,os.fsencode(source.name),target_fd,os.fsencode(target.name),1)!=0:
                code=ctypes.get_errno()
                if code in (errno.EEXIST,errno.ENOTEMPTY):fail('adapter_output_conflict')
                if code in (errno.ENOSYS,errno.EINVAL,errno.ENOTSUP):fail('adapter_atomic_unavailable')
                fail('adapter_candidate_publish')
        finally:os.close(target_fd)
    finally:os.close(source_fd)


def candidate(root,output,state,changes,*,expected_tree_sha,operation,check=None):
    """Write a fresh local candidate atomically; input tree is never modified."""
    root=retention.regular_root(root);output=Path(output).absolute();before=tree_snapshot(root)
    if operation not in ('adoption','update') or (operation=='adoption' and STATE in before):fail('adapter_operation')
    if root_digest(before)!=checked(expected_tree_sha,64):fail('adapter_source_changed')
    if '..' in output.parts or any(p.is_symlink() for p in (output,*output.parents)) or output.is_relative_to(root) or root.is_relative_to(output):fail('adapter_output')
    data=encoded(state)
    if len(data)>MAX_STATE:fail('adapter_state_budget')
    writes={**changes,STATE:data}
    if any(path_ok(p)!=p or (p!=STATE and not p.endswith('.md')) or not isinstance(v,bytes) for p,v in writes.items()):fail('adapter_write_scope')
    intended=deepcopy(before)
    for path,value in writes.items():intended[path]={'sha256':sha(value),'blob':retention.oid(value),'bytes':len(value)}
    diff=[{'path':path,'action':'modify' if path in before else 'add','before':before.get(path),'after':intended[path]} for path in sorted(intended) if intended[path]!=before.get(path)]
    if operation=='adoption' and any(row['path']!=STATE for row in diff):fail('adapter_adoption_changed_note')
    plan={'contract':CONTRACT,'operation':operation,'source_tree_sha256':expected_tree_sha,'candidate_tree_sha256':root_digest(intended),
      'state_sha256':sha(data),'changes':diff,'local_proposal_only':True,'mirror_adopted':False,'apply_ready':False}
    if output.exists():
        if not output.is_dir() or (output/'plan.json').is_symlink() or not (output/'plan.json').is_file() or (output/'plan.json').stat().st_nlink!=1:fail('adapter_output_conflict')
        prior=strict_capture_json(bounded_file(output/'plan.json',MAX_STATE),MAX_STATE)
        if prior!=plan or tree_snapshot(output/'candidate')!=intended:fail('adapter_output_conflict')
        if tree_snapshot(root)!=before:fail('adapter_source_changed')
        return plan
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='get-legacy-candidate-',dir=output.parent) as folder:
        staging=Path(folder)/'result';staging.mkdir();destination=staging/'candidate';shutil.copytree(root,destination)
        for path,value in writes.items():
            target=destination/path;target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists() or bounded_file(target,MAX_TREE)!=value:target.write_bytes(value)
        if check is not None:check(destination)
        if tree_snapshot(destination)!=intended:fail('adapter_candidate_readback')
        (staging/'plan.json').write_bytes(encoded(plan))
        if strict_capture_json(bounded_file(staging/'plan.json',MAX_STATE),MAX_STATE)!=plan:fail('adapter_candidate_readback')
        if tree_snapshot(root)!=before:fail('adapter_source_changed')
        rename_noreplace(staging,output)
    return plan


def git_candidate(repo,parent,state,changes,current,*,operation):
    """Create local immutable proposal objects only; never update any Git ref.

    Inherited assets may be absent locally. Their exact tree entries survive;
    every changed blob is present and read back. Publication would separately
    require a compare-and-swap against this plan's exact parent commit.
    """
    before=repo.tree(parent);local=repo.local;state_path=retention.GET+'/'+STATE
    for path,row in before.items():
        if path.startswith(retention.GET+'/'):
            retention.safe_path(path)
            if row[:2]!=('100644','blob'):fail('adapter_alias')
    if operation not in ('adoption','update') or (operation=='adoption' and state_path in before):fail('adapter_operation')
    # Every evaluated current path, including unchanged managed notes, belongs
    # to this exact immutable parent. Checking only writes permits stale adoption.
    managed={row['ownership']['filePath'] for row in state['legacyById'].values()}
    for rel in set(current)|managed:
        old=before.get(retention.GET+'/'+path_ok(rel));value=current.get(rel)
        if (old is None)!=(value is None):fail('adapter_source_changed')
        if old is not None and (old[:2]!=('100644','blob') or retention.oid(value)!=old[2]):fail('adapter_source_changed')
    if operation=='adoption':
        for row in state['legacyById'].values():
            value=current.get(row['ownership']['filePath'])
            if value is None or sha(value)!=row['ownership']['mirror_sha256'] or retention.oid(value)!=row['baseline_blob']:fail('adapter_source_changed')
    data=encoded(state)
    if len(data)>MAX_STATE:fail('adapter_state_budget')
    relative={**changes,STATE:data};writes={};rows=[]
    for rel,value in sorted(relative.items()):
        path=retention.GET+'/'+path_ok(rel)
        if not isinstance(value,bytes) or (rel!=STATE and (not rel.endswith('.md') or rel not in current)):fail('adapter_write_scope')
        old=before.get(path)
        if old is not None and old[:2]!=('100644','blob'):fail('adapter_alias')
        if old is not None and (rel not in current or retention.oid(current[rel])!=old[2]):fail('adapter_source_changed')
        if old is None and rel in current:fail('adapter_source_changed')
        if old==('100644','blob',retention.oid(value)):continue
        writes[path]=value;rows.append({'path':rel,'before_blob':old[2] if old else None,'after_blob':retention.oid(value),'sha256':sha(value),'bytes':len(value)})
    if operation=='adoption' and any(row['path']!=STATE for row in rows):fail('adapter_adoption_changed_note')
    wanted=dict(before)
    for path,value in writes.items():wanted[path]=('100644','blob',retention.oid(value))
    if writes:
        with tempfile.TemporaryDirectory(prefix='get-legacy-proposal-index-') as folder:
            env=dict(local.env,GIT_INDEX_FILE=str(Path(folder)/'index'))
            local.run('read-tree',parent,env=env)
            for path,value in writes.items():
                blob=local.run('hash-object','-w','--stdin',data=value,env=env).decode().strip()
                if blob!=retention.oid(value):fail('adapter_blob_readback')
                local.run('update-index','--add','--cacheinfo','100644,'+blob+','+path,env=env)
            tree=checked(local.run('write-tree','--missing-ok',env=env).decode().strip(),40)
            if local.tree(tree)!=wanted:fail('adapter_candidate_readback')
            for path,value in writes.items():
                if local.blob(wanted[path][2])!=value:fail('adapter_candidate_readback')
            timestamp=local.run('show','-s','--format=%ct',parent).decode().strip()
            if not timestamp.isdigit():fail('adapter_anchor')
            env.update(GIT_AUTHOR_NAME='Get local proposal',GIT_AUTHOR_EMAIL='get-proposal@example.invalid',GIT_COMMITTER_NAME='Get local proposal',GIT_COMMITTER_EMAIL='get-proposal@example.invalid',GIT_AUTHOR_DATE='@'+timestamp+' +0000',GIT_COMMITTER_DATE='@'+timestamp+' +0000')
            commit=checked(local.run('commit-tree',tree,'-p',parent,'-m','Prepare local Get legacy proposal',env=env).decode().strip(),40)
        if local.run('rev-list','--parents','-n','1',commit).decode().strip().split()!=[commit,parent] or repo.tree(commit)!=wanted:fail('adapter_candidate_readback')
    else:commit=parent;tree=local.run('rev-parse',parent+'^{tree}').decode().strip()
    return {'contract':CONTRACT,'operation':operation,'parent_commit':parent,'candidate_commit':commit,'candidate_tree':tree,
      'state_sha256':sha(data),'changes':rows,'local_proposal_only':True,'mirror_adopted':False,'apply_ready':False}

def git_current(repo,commit,evidence):
    tree=repo.tree(commit);paths={row['filePath'] for row in evidence.initial.values()}|{STATE};current={}
    for path in paths:
        full=retention.GET+'/'+path
        if full in tree:current[path]=repo.read(commit,full,MAX_STATE if path==STATE else owned.MAX_NOTE)
    return current

def save_git_plan(output,plan,state):
    output=Path(output).absolute()
    if '..' in output.parts or any(p.is_symlink() for p in (output,*output.parents)):fail('adapter_output')
    values={'plan.json':encoded(plan),'state.json':encoded(state)}
    if output.exists():
        if not output.is_dir() or {p.name for p in output.iterdir()}!=set(values):fail('adapter_output_conflict')
        for name,data in values.items():
            path=output/name
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink!=1 or bounded_file(path,MAX_STATE)!=data:fail('adapter_output_conflict')
        return
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='get-legacy-plan-',dir=output.parent) as folder:
        staged=Path(folder)/'result';staged.mkdir()
        for name,data in values.items():
            (staged/name).write_bytes(data)
            if bounded_file(staged/name,MAX_STATE)!=data:fail('adapter_candidate_readback')
        rename_noreplace(staged,output)


def load_current(root):
    snapshot=tree_snapshot(root)
    current={path:bounded_file(Path(root)/path,entry['bytes']) for path,entry in snapshot.items()}
    if any(sha(current[path])!=entry['sha256'] for path,entry in snapshot.items()):fail('adapter_source_changed')
    return current,snapshot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    source_group=parser.add_mutually_exclusive_group(required=True);source_group.add_argument('--root',type=Path);source_group.add_argument('--git-base')
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--capture-commit',required=True)
    parser.add_argument('--census-sha256',required=True);parser.add_argument('--ledger-sha256',required=True)
    parser.add_argument('--expected-tree-sha256');parser.add_argument('--prior-commit');parser.add_argument('--state-sha256');parser.add_argument('--source-snapshot',type=Path)
    args=parser.parse_args()
    try:
        repo=OfflineGit(args.repo);evidence=CaptureEvidence(repo,{'capture_commit':args.capture_commit,'census_sha256':args.census_sha256,'ledger_sha256':args.ledger_sha256})
        if args.git_base:
            if args.expected_tree_sha256 or (args.prior_commit and args.prior_commit!=args.git_base):fail('adapter_arguments')
            current=git_current(repo,args.git_base,evidence)
        else:
            if not args.expected_tree_sha256:fail('adapter_arguments')
            current,snapshot=load_current(args.root)
        if args.prior_commit:
            if not args.state_sha256 or not args.source_snapshot:fail('adapter_arguments')
            state_bytes=repo.read(args.prior_commit,retention.GET+'/'+STATE,MAX_STATE)
            if current.get(STATE)!=state_bytes:fail('adapter_state_worktree_changed')
            state=strict_capture_json(state_bytes,MAX_STATE);source=strict_capture_json(bounded_file(args.source_snapshot,MAX_TREE),MAX_TREE)
            if set(source)!={'summaries','terminal','details'}:fail('adapter_detail_schema')
            state,changes,counts=propose_updates(evidence,state,args.state_sha256,current,repo,args.prior_commit,source['summaries'],source['terminal'],source['details']);operation='update'
        else:
            if args.state_sha256 or args.source_snapshot or STATE in current:fail('adapter_arguments')
            state=initial_state(evidence,current);changes={};counts={};operation='adoption'
        if args.git_base:
            plan=git_candidate(repo,args.git_base,state,changes,current,operation=operation);save_git_plan(args.output,plan,state)
        else:plan=candidate(args.root,args.output,state,changes,expected_tree_sha=args.expected_tree_sha256,operation=operation)
        print(json.dumps({'status':'local_candidate_verified','operation':operation,'managed_proposals':len(state['legacyById']),
          'conflicts':len(state['conflictsById']),'pending':len(state['pending_ids']),'changed_paths':len(plan['changes']),
          'state_bytes':len(encoded(state)),'mirror_adopted':False,'apply_ready':False,**counts}))
        return 0
    except Exception:
        print(json.dumps({'status':'local_candidate_failed','mirror_adopted':False,'apply_ready':False}))
        return 1

if __name__=='__main__':raise SystemExit(main())
