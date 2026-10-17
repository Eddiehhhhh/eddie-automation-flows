"""Bounded manual legacy Get transactions. Preparation is the default.

This entry is intentionally separate from scheduled/new-note/Notion writers.
An apply invocation requires an external approved logical-payload hash. Only
unrelated main changes can be rebased under that unchanged payload binding. State hashes are externally supplied trust inputs, never learned
from state being validated. No credentials or private text enter public output.
"""
from copy import deepcopy
from contextlib import contextmanager
import argparse
import base64
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request

import get_legacy_adapter as a
import get_legacy_capture as c
import get_private_preview_retention as r
from get_mirror_incremental_replay import strict_capture_json, raw_source_version

CONTRACT='get-legacy-manual-transaction-1'
CAPTURE='7f6966122e430a14d0268f93035a8fcf5d3d1d99'
ANCHOR={'capture_commit':CAPTURE,'census_sha256':'da35f84480f45e5c9ce8f37fb980e3b6fb5d6ae7181514fb090e378337c2989c','ledger_sha256':'0fcf2dc608815b0b850411d3334d4878705b546aca64d1cd448913602c6eead9'}
INITIAL_SHA='b55b4d236979ba4a15d3580950713944b0f13ce30c51bad5f11db1a25646a7d1'
MAX_CHANGED_BYTES=64*c.MIB
MAX_MANAGED=1152
PROTECTED={**c.PROTECTED,c.REF:CAPTURE}
ERRORS={'manual_failed','manual_target','manual_ref_changed','manual_parent_changed','manual_initial_changed','manual_state_anchor','manual_source_payload','manual_budget','manual_plan','manual_scope','manual_readback','manual_readback_ref','manual_readback_transport','manual_uncertain_write','manual_write_unconfirmed','manual_rollback_conflict','manual_checkpoint','manual_approval_changed','manual_approval_required'}

class ManualFailure(a.ParityFailure):
    def __init__(self,code,write_state='not_attempted',attempts=0):
        super().__init__(code if code in ERRORS else 'manual_failed')
        self.write_state=write_state;self.attempts=attempts

def fail(code,write_state='not_attempted',attempts=0):raise ManualFailure(code,write_state,attempts)

def disabled_writers(token=None,opener=None):
    """Read-only old-writer gate, using the existing Actions runtime token."""
    opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),r.NoRedirect())
    headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    if token:headers['Authorization']='Bearer '+token
    for name in ('sync-getnote.yml','sync-getnote-voice-to-flomo.yml'):
        base='https://api.github.com/repos/Eddiehhhhh/eddie-automation-flows/actions/workflows/'+name
        for suffix in ('','/runs?status=queued&per_page=1','/runs?status=in_progress&per_page=1','/runs?status=waiting&per_page=1','/runs?status=pending&per_page=1','/runs?status=requested&per_page=1'):
            try:
                with opener.open(urllib.request.Request(base+suffix,headers=headers),timeout=30) as response:
                    raw=response.read(1024*1024+1)
                    if response.status!=200 or len(raw)>1024*1024:fail('manual_target')
                value=strict_capture_json(raw)
            except Exception:fail('manual_target')
            if suffix:
                if type(value.get('total_count')) is not int or value['total_count']!=0 or value.get('workflow_runs')!=[]:fail('manual_target')
            elif value.get('path')!='.github/workflows/'+name or value.get('state') not in ('disabled_manually','disabled_inactivity','disabled_fork'):fail('manual_target')


class Remote(c.Remote):
    """The only write primitive is one exact-lease fast-forward of fixed main."""
    def cas_main(self,parent,commit):
        a.checked(parent,40);a.checked(commit,40)
        self.local.run('merge-base','--is-ancestor',parent,commit)
        self.local.run('push','--porcelain','--no-verify','--force-with-lease='+r.MAIN_REF+':'+parent,r.REMOTE_URL,commit+':'+r.MAIN_REF)

def guard(remote,parent=None):
    metadata=remote.metadata()
    if metadata.get('full_name')!=r.REPOSITORY or metadata.get('private') is not True:fail('manual_target')
    for ref,oid in PROTECTED.items():
        if remote.ref(ref)!=oid:fail('manual_ref_changed')
    head=remote.ref(r.MAIN_REF);a.checked(head,40)
    if parent is not None and head!=parent:fail('manual_parent_changed')
    return head


def import_blob(local,data,oid):
    if r.oid(data)!=oid or local.run('hash-object','-w','--stdin',data=data).decode().strip()!=oid:fail('manual_readback')


def load_evidence(local,remote,token,root,parent,anchor=ANCHOR):
    """Selected evidence/current/baseline blobs; inherited assets stay absent."""
    head=anchor['capture_commit'];a.checked(parent,40)
    for commit,depth in ((head,42),(parent,256)):
        local.run('fetch','--depth='+str(depth),'--filter=blob:none','--no-tags','--no-recurse-submodules','--no-write-fetch-head','origin',commit)
    metadata=c.scope_metadata(remote,head)
    if c.CENSUS not in metadata or c.LEDGER not in metadata:fail('manual_state_anchor')
    values=c.fetch_blobs(remote,metadata,token,root/'capture')
    for path,data in values.items():import_blob(local,data,metadata[path][2])
    census=strict_capture_json(values[c.CENSUS],16*c.MIB);ledger=strict_capture_json(values[c.LEDGER],c.MIB)
    if a.sha(values[c.CENSUS])!=anchor['census_sha256'] or a.sha(values[c.LEDGER])!=anchor['ledger_sha256']:fail('manual_state_anchor')
    local.run('fetch','--depth=1','--filter=blob:none','--no-tags','--no-recurse-submodules','--no-write-fetch-head','origin',census['source_commit'])
    history=c.verify_scope_history(local,head,census,ledger,remote)
    historical=c.fetch_blobs(remote,history,token,root/'history')
    for path,data in historical.items():import_blob(local,data,history[path][2])
    source_meta=remote.check_source_budget(census['source_commit'])
    _,candidates,excluded,pending,_=c.authorized_cohort(remote)
    c.validate_authorized_cohort(census,candidates,excluded|set(pending),source_meta)
    old_meta={r.GET+'/'+entry['filePath']:source_meta[r.GET+'/'+entry['filePath']] for entry in census['eligible'].values()}
    old=c.fetch_blobs(remote,old_meta,token,root/'old')
    for path,data in old.items():import_blob(local,data,old_meta[path][2])
    reader=a.OfflineGit(local.vault);evidence=a.CaptureEvidence(reader,anchor)
    current_meta=remote.check_source_budget(parent)
    wanted={r.GET+'/'+row['filePath'] for row in evidence.initial.values()}|{r.GET+'/'+a.STATE}
    selected={path:current_meta[path] for path in wanted if path in current_meta}
    # State can exceed the ordinary response cap; exact OID/length still checked.
    for path,row in selected.items():
        cap=a.MAX_STATE if path==r.GET+'/'+a.STATE else a.owned.MAX_NOTE
        if row[:2]!=('100644','blob') or row[3]>cap:fail('manual_budget')
    current_values=fetch_selected(remote,selected,token,root/'current')
    for path,data in current_values.items():import_blob(local,data,selected[path][2])
    return reader,evidence,a.git_current(reader,parent,evidence)


def fetch_selected(remote,metadata,token,directory):
    import source_sync_pack_seed as pack
    requested={row[2]:row[3] for row in metadata.values()}
    if sum(requested.values())>MAX_CHANGED_BYTES+a.MAX_STATE:fail('manual_budget')
    if not requested:return {}
    protocol=pack.capabilities(token)
    files,_=r.fetch_readback_blobs(token,requested,directory,pack.MAX_PACK,protocol)
    result={}
    for path,(_,_,oid,size) in metadata.items():
        data=r.file_bytes(files[oid],size)
        if len(data)!=size or r.oid(data)!=oid:fail('manual_readback')
        result[path]=data
    return result


def selected_baselines(reader,state,parent,remote):
    """Fetch only proven same-path prior blobs missing from the local ODB."""
    total=0
    for row in state['legacyById'].values():
        ownership=row['ownership'];path=ownership['filePath'];blob=row['baseline_blob']
        try:reader.baseline(parent,path,blob,ownership['mirror_sha256']);continue
        except a.ParityFailure as exc:
            if str(exc)!='baseline_missing':raise
        full=r.GET+'/'+path
        commits=reader.local.run('rev-list','--max-count=256',parent,'--',full).decode().splitlines()
        if not any(reader.tree(commit).get(full)==('100644','blob',blob) for commit in [parent]+commits):continue
        # No arbitrary OID request: identity/path/shape/hash were validated and
        # this exact OID was found at the same path in pinned prior history.
        value=remote.request('/git/blobs/'+blob,maximum=2*a.owned.MAX_NOTE+65536,missing=True)
        if value is None:continue
        size=value.get('size')
        if value.get('sha')!=blob or value.get('encoding')!='base64' or type(size) is not int or not 0<=size<=a.owned.MAX_NOTE:fail('manual_readback')
        try:data=base64.b64decode(value['content'].replace('\n',''),validate=True)
        except Exception:fail('manual_readback')
        total+=len(data)
        if len(data)!=size or total>MAX_CHANGED_BYTES or a.sha(data)!=ownership['mirror_sha256']:fail('manual_readback')
        import_blob(reader.local,data,blob)


def detail_note(raw):
    payload=strict_capture_json(raw,c.MAX_RESPONSE)
    if payload.get('success') is False:return None
    if 'data' in payload and set(payload)-{'data','success','error','meta','request_id'}:return None
    value=payload.get('data',payload)
    if not isinstance(value,dict):return None
    if 'note' in value:
        # Never silently disregard a newly appearing sibling source object.
        if set(value)!={'note'}:return None
        value=value['note']
    return value if isinstance(value,dict) else None


def rules_sha256():
    """Bind the executed Get rules, transport, wrapper and manual workflow."""
    root=Path(__file__).resolve().parents[1];scripts=root/'scripts'
    paths=set(scripts.glob('get_*.py'))|set(scripts.glob('source_sync_*.py'))
    paths|={scripts/'sync_getnote_to_raw.py',scripts/'run_private_command.py',scripts/'probe_getnote_auth.py',scripts/'verify_shadow_repeat.py',root/'.github/workflows/source-sync-shadow.yml'}
    return a.sha(a.encoded({str(path.relative_to(root)):a.sha(a.bounded_file(path,2*c.MIB)) for path in sorted(paths)}))


def approval_payload(reader,plan,current):
    """Commit-independent permission: all Get bytes/rules/evidence and exact diff."""
    return {'contract':'get-legacy-approved-payload-1','rules_sha256':rules_sha256(),
      'before_get_tree':reader.source_get_tree(plan['parent_commit']),
      'current_sha256':{path:a.sha(data) for path,data in sorted(current.items())},
      **{key:deepcopy(plan[key]) for key in ('initial_state_sha256','previous_state_sha256','state_sha256',
        'capture_anchor','bootstrap','changes','managed','conflicts','pending','counts','source_details','source_summary_sha256')}}


def transaction_bytes(reader,parent,commit):
    """Distinct new blob payload across both local commits, not just tip diff."""
    existing={row[2] for row in reader.tree(parent).values()};seen=set();total=0;cursor=commit;count=0
    while cursor!=parent:
        count+=1
        if count>2:fail('manual_plan')
        chain=reader.local.run('rev-list','--parents','-n','1',cursor).decode().split()
        if len(chain)!=2 or chain[0]!=cursor:fail('manual_plan')
        before=reader.tree(chain[1]);after=reader.tree(cursor)
        for path,row in after.items():
            if before.get(path)==row or row[2] in existing or row[2] in seen:continue
            r.safe_path(path)
            cap=a.MAX_STATE if path==r.GET+'/'+a.STATE else a.owned.MAX_NOTE
            data=reader.read(cursor,path,cap);seen.add(row[2]);total+=len(data)
            if total>MAX_CHANGED_BYTES:fail('manual_budget')
        cursor=chain[1]
    return total


def prepare(reader,evidence,parent,current,source,*,initial_sha=INITIAL_SHA,state_sha=None):
    a.checked(parent,40)
    original_current=dict(current);first=a.STATE not in current
    if first:
        if state_sha is not None:fail('manual_state_anchor')
        state=a.initial_state(evidence,current)
        if a.sha(a.encoded(state))!=initial_sha:fail('manual_initial_changed')
        adopted=a.git_candidate(reader,parent,state,{},current,operation='adoption')
        prior=adopted['candidate_commit'];current={**current,a.STATE:a.encoded(state)};trusted=initial_sha
    else:
        if state_sha is None:fail('manual_state_anchor')
        state=strict_capture_json(current[a.STATE],a.MAX_STATE)
        a.validate_state(state,state_sha,evidence);prior=parent;trusted=state_sha
    if not 1<=len(state['legacyById'])<=MAX_MANAGED:fail('manual_budget')
    summaries,terminal=source.enumerate();items=a.source_snapshot(summaries,terminal)
    needed=sorted(ident for ident,row in state['legacyById'].items() if ident in items and
       (raw_source_version(items[ident].get('updated_at'))!=row['ownership']['source_version'] or a.sha(a.encoded(items[ident]))!=row['summary_sha256']))
    details={};unavailable=set()
    for ident in needed:
        try:details[ident]=detail_note(source.detail_raw(ident))
        except RuntimeError as exc:
            if str(exc)!='GetNote HTTP 404':raise
            unavailable.add(ident);details[ident]=None
    proposed,changes,counts=a.propose_updates(evidence,state,trusted,current,reader,prior,summaries,terminal,details)
    for ident in unavailable:
        if proposed['conflictsById'].get(ident)!='detail_identity':fail('manual_plan')
        proposed['conflictsById'][ident]='source_missing'
    counts['details_unavailable']=len(unavailable)
    update=a.git_candidate(reader,prior,proposed,changes,current,operation='update')
    before=reader.tree(parent);after=reader.tree(update['candidate_commit'])
    changed={p for p in set(before)|set(after) if before.get(p)!=after.get(p)}
    allowed={r.GET+'/'+a.STATE}|{r.GET+'/'+row['ownership']['filePath'] for row in state['legacyById'].values()}
    if not changed<=allowed or any(p not in after for p in changed):fail('manual_scope')
    files={p:reader.read(update['candidate_commit'],p,a.MAX_STATE if p==r.GET+'/'+a.STATE else a.owned.MAX_NOTE) for p in changed}
    if sum(map(len,files.values()))>MAX_CHANGED_BYTES:fail('manual_budget')
    rows=[{'path':p,'before_blob':before.get(p,[None,None,None])[2],'after_blob':after[p][2],'sha256':a.sha(files[p]),'bytes':len(files[p])} for p in sorted(files)]
    plan={'contract':CONTRACT,'parent_commit':parent,'candidate_commit':update['candidate_commit'],'candidate_tree':update['candidate_tree'],
      'initial_state_sha256':initial_sha,'previous_state_sha256':state_sha,'state_sha256':update['state_sha256'],
      'capture_anchor':evidence.anchor,'bootstrap':first,'changes':rows,'managed':len(proposed['legacyById']),
      'conflicts':len(proposed['conflictsById']),'pending':len(proposed['pending_ids']),'counts':counts,
      'transaction_bytes':transaction_bytes(reader,parent,update['candidate_commit']),'source_details':len(details),'source_summary_sha256':a.sha(a.encoded(summaries)),
      'local_proposal_only':True,'apply_ready':False}
    plan['approval_payload_sha256']=a.sha(a.encoded(approval_payload(reader,plan,original_current)))
    verify_plan(reader,plan,files)
    return plan,files


def verify_plan(reader,plan,files):
    if plan.get('contract')!=CONTRACT or plan.get('local_proposal_only') is not True or plan.get('apply_ready') is not False:fail('manual_plan')
    parent=plan['parent_commit'];commit=plan['candidate_commit'];a.checked(parent,40);a.checked(commit,40)
    reader.local.run('merge-base','--is-ancestor',parent,commit)
    before=reader.tree(parent);after=reader.tree(commit)
    if reader.local.run('rev-parse',commit+'^{tree}').decode().strip()!=plan['candidate_tree']:fail('manual_plan')
    changed={p for p in set(before)|set(after) if before.get(p)!=after.get(p)}
    if changed!=set(files) or changed!={row['path'] for row in plan['changes']} or len(plan['changes'])!=len(changed):fail('manual_plan')
    for row in plan['changes']:
        p=row['path'];r.safe_path(p);data=files[p]
        if before.get(p,[None,None,None])[2]!=row['before_blob'] or after.get(p)!=('100644','blob',r.oid(data)) or row['after_blob']!=r.oid(data) or row['sha256']!=a.sha(data) or row['bytes']!=len(data) or reader.read(commit,p,a.MAX_STATE if p==r.GET+'/'+a.STATE else a.owned.MAX_NOTE)!=data:fail('manual_plan')
    if sum(map(len,files.values()))>MAX_CHANGED_BYTES or transaction_bytes(reader,parent,commit)!=plan.get('transaction_bytes'):fail('manual_budget')


def publish(reader,remote,plan,files,expected_plan_sha,readback,*,sleep=time.sleep,writers_guard=disabled_writers,checkpoint=None,verifier=None):
    """One CAS maximum. Uncertain writes are read back, never repeated here."""
    if a.sha(a.encoded(plan))!=expected_plan_sha:fail('manual_plan')
    (verifier or verify_plan)(reader,plan,files);parent=plan['parent_commit'];commit=plan['candidate_commit']
    guard(remote,parent)
    if not files:return {'main_updated':False,'cas_attempts':0,'readback_verified':True}
    writers_guard();guard(remote,parent)
    if checkpoint is None:fail('manual_checkpoint')
    checkpoint(plan)
    guard(remote,parent)
    uncertain=False;confirmed=False
    try:remote.cas_main(parent,commit);confirmed=True
    except Exception:uncertain=True
    try:
        for attempt in range(6):
            try:observed=guard(remote)
            except Exception:fail('manual_readback_transport')
            if observed==commit:
                confirmed=True;break
            if observed!=parent:fail('manual_readback_ref')
            if attempt==5:fail('manual_uncertain_write' if uncertain else 'manual_readback_ref')
            sleep(2**attempt)
        actual=remote.commit(commit)
        expected_parents=reader.local.run('rev-list','--parents','-n','1',commit).decode().strip().split()[1:]
        if actual.get('tree')!=plan['candidate_tree'] or actual.get('parents')!=expected_parents or readback(commit,files)!=files:fail('manual_readback')
        guard(remote,commit)
    except Exception as exc:fail(str(exc) if str(exc) in ERRORS else 'manual_readback','confirmed' if confirmed else 'uncertain',1)
    return {'main_updated':True,'cas_attempts':1,'readback_verified':True}



def run(token,source,expected_parent,*,state_sha=None,apply=False,work=None,checkpoint=None,approved_payload_sha256=None):
    if apply:
        try:a.checked(approved_payload_sha256,64)
        except Exception:fail('manual_approval_required')
    remote=Remote(None,token);parent=guard(remote)
    if parent!=expected_parent:
        if not apply or remote.source_get_tree(expected_parent)!=remote.source_get_tree(parent):fail('manual_parent_changed')
    approved_parent=expected_parent;expected_parent=parent
    with tempfile.TemporaryDirectory(prefix='get-legacy-manual-',dir=work) as folder:
        root=Path(folder);local=r.readback_checkout(root/'objects',CAPTURE,token);remote.local=local
        reader,evidence,current=load_evidence(local,remote,token,root,expected_parent,anchor=ANCHOR)
        if approved_parent!=expected_parent:
            try:local.run('merge-base','--is-ancestor',approved_parent,expected_parent)
            except Exception:fail('manual_parent_changed')
        if a.STATE in current:
            if state_sha is None:fail('manual_state_anchor')
            state=strict_capture_json(current[a.STATE],a.MAX_STATE);a.validate_state(state,state_sha,evidence)
            selected_baselines(reader,state,expected_parent,remote)
        guard(remote,expected_parent)
        plan,files=prepare(reader,evidence,expected_parent,current,source,initial_sha=INITIAL_SHA,state_sha=state_sha)
        guard(remote,expected_parent)
        if apply and plan['approval_payload_sha256']!=approved_payload_sha256:fail('manual_approval_changed')
        result={'status':'manual_candidate_verified','main_updated':False,'cas_attempts':0,'readback_verified':True,
          'managed':plan['managed'],'conflicts':plan['conflicts'],'pending':plan['pending'],'changed_paths':len(files),
          'notes_updated':sum(p!=r.GET+'/'+a.STATE for p in files),'changed_bytes':sum(map(len,files.values())),'transaction_bytes':plan['transaction_bytes'],
          'state_sha256':plan['state_sha256'],'approval_payload_sha256':plan['approval_payload_sha256'],'plan_sha256':a.sha(a.encoded(plan)),
          'parent_commit':plan['parent_commit'],'candidate_commit':plan['candidate_commit'],**plan['counts']}
        if apply:
            def readback(commit,files):
                metadata={p:('100644','blob',r.oid(data),len(data)) for p,data in files.items()}
                return fetch_selected(remote,metadata,token,root/'readback')
            result.update(publish(reader,remote,plan,files,result['plan_sha256'],readback,writers_guard=disabled_writers,checkpoint=checkpoint))
            result['status']='manual_transaction_verified'
        return result


# This mode is preparation-only. No canonical plan is accepted by publish().
CANONICAL_COUNT=10
CANONICAL_CONTRACT='get-canonical-preview-proposal-1'

CANONICAL_STAGES={'initial_guard','capture_load','inventory_preview_load','inventory','source_list','source_detail','candidate','final_guard'}
CANONICAL_REASONS={'canonical_identity','canonical_duplicate_identity','canonical_budget','canonical_alias','canonical_path','canonical_preview_anchor','canonical_preview_schema','canonical_preview_blob','canonical_baseline_identity','canonical_baseline_version','canonical_generated_header','adapter_missing_blob','adapter_blob_budget','adapter_blob_readback','adapter_state_hash','adapter_state_schema','git_operation_failed','capture_readback','source_tree_budget','incomplete_list','capture_cohort','capture_api_error','other'}

@contextmanager
def canonical_stage(name):
    try:yield
    except Exception as exc:
        if not getattr(exc,'canonical_stage',None):exc.canonical_stage=name
        raise



def load_canonical_inputs(reader,remote,token,root,parent):
    """Read all current Markdown identities and the fixed preview; no assets."""
    import get_canonical_writer as n
    local=remote.local
    local.run('fetch','--depth=1','--filter=blob:none','--no-tags','--no-recurse-submodules','--no-write-fetch-head','origin',n.PREVIEW)
    snapshots={parent:remote.check_source_budget(parent),n.PREVIEW:remote.check_source_budget(n.PREVIEW)}
    for commit,metadata in snapshots.items():
        if commit==parent:
            selected={p:row for p,row in metadata.items() if n.is_markdown(p) or p in (r.GET+'/'+a.STATE,r.GET+'/'+n.STATE)}
        else:
            statepath=r.GET+'/'+n.PREVIEW_STATE
            if statepath not in metadata:fail('manual_state_anchor')
            selected={statepath:metadata[statepath]}
        missing={}
        for path,row in selected.items():
            rel=path[len(r.GET)+1:]
            cap=n.MAX_STATE if rel in (a.STATE,n.STATE,n.PREVIEW_STATE) else a.owned.MAX_NOTE
            if row[:2]!=('100644','blob') or type(row[3]) is not int or not 0<=row[3]<=cap:fail('manual_budget')
            if reader.tree(commit).get(path)!=row[:3]:fail('manual_readback')
            probe=reader.local.command('cat-file','--batch-check=%(objectname) %(objecttype) %(objectsize)',data=(row[2]+'\n').encode())
            if probe.returncode or probe.stderr:fail('manual_readback')
            if probe.stdout==(row[2]+' missing\n').encode():missing[path]=row
            elif probe.stdout!=(row[2]+' blob '+str(row[3])+'\n').encode():fail('manual_readback')
        values=fetch_selected(remote,missing,token,root/('current-canonical' if commit==parent else 'preview-state'))
        for path,data in values.items():import_blob(local,data,selected[path][2])
    with canonical_stage('inventory'):current=n.git_inventory(reader,parent)
    # Anchor and schema gate before reading any preview path from untrusted data.
    state_bytes=reader.read(n.PREVIEW,r.GET+'/'+n.PREVIEW_STATE,2*c.MIB)
    if a.sha(state_bytes)!=n.PREVIEW_STATE_SHA:fail('manual_state_anchor')
    state=strict_capture_json(state_bytes,2*c.MIB);rows=state.get('notesByCanonicalId')
    if not isinstance(rows,dict) or len(rows)!=CANONICAL_COUNT:fail('manual_scope')
    selected={}
    for row in rows.values():
        if not isinstance(row,dict):fail('manual_scope')
        path=r.GET+'/'+n.path_ok(row.get('filePath'));metadata=snapshots[n.PREVIEW]
        if path in selected or path not in metadata:fail('manual_scope')
        value=metadata[path]
        if value[:2]!=('100644','blob') or type(value[3]) is not int or not 0<=value[3]<=a.owned.MAX_NOTE or reader.tree(n.PREVIEW).get(path)!=value[:3]:fail('manual_budget')
        selected[path]=value
    values=fetch_selected(remote,selected,token,root/'preview-notes')
    for path,data in values.items():import_blob(local,data,selected[path][2])
    preview=n.preview_records(reader,n.PREVIEW,n.PREVIEW_STATE_SHA)
    if len(preview)!=CANONICAL_COUNT:fail('manual_scope')
    return current,preview


def canonical_reference_receipt(note,data):
    """Exact named URL fields only; never claim binary availability/readability."""
    import get_canonical_writer as n
    fields=n.json_header(data);audio=note.get('audio') or {};web=note.get('web_page') or {}
    attachments=note.get('attachments') or []
    links=[entry['url'] for entry in attachments if entry['type']=='link']
    expected={'attachments':[entry['url'] for entry in attachments],
      'audio_url':audio.get('play_url') or note.get('audio_url') or '',
      'web_url':web.get('url') or (links[0] if links else '')}
    if any(fields.get(key)!=value for key,value in expected.items()):fail('manual_readback')
    return {'contract':'get-canonical-reference-evidence-1','named_fields':expected,
      'attachment_types':[entry['type'] for entry in attachments],
      'reference_count':len(attachments)+bool(expected['audio_url'])+bool(expected['web_url']),
      'bytes_sha256':a.sha(data),'binary_downloads':0,'reference_content_verified':False}


def prepare_canonical_preview(reader,evidence,parent,current,preview,source,*,state_sha,apply=False):
    """Only the ten absent preview identities. Every existing file is frozen."""
    if apply:fail('manual_scope')
    import get_canonical_writer as n
    if n.STATE in current or a.STATE not in current or len(preview)!=CANONICAL_COUNT:fail('manual_state_anchor')
    legacy=a.validate_state(strict_capture_json(current[a.STATE],a.MAX_STATE),state_sha,evidence)
    if a.encoded(legacy)!=current[a.STATE] or reader.read(parent,r.GET+'/'+a.STATE,a.MAX_STATE)!=current[a.STATE]:fail('manual_state_anchor')
    registry=n.initial_registry(evidence,legacy,state_sha,current)
    # Bind and validate the full inventory against the exact parent before Get.
    before=reader.tree(parent);prefix=r.GET+'/'
    evaluated={p[len(prefix):] for p in before if p.startswith(prefix) and (n.is_markdown(p) or p[len(prefix):] in (a.STATE,n.STATE))}
    if set(current)!=evaluated or any(before.get(prefix+p)!=('100644','blob',r.oid(data)) for p,data in current.items()):fail('manual_parent_changed')
    selected=sorted(preview)
    if len(set(selected))!=CANONICAL_COUNT or set(selected)&set(registry['reserved']):fail('manual_scope')
    if any(prefix+n.path_ok(saved['entry']['filePath']) in before for saved in preview.values()):fail('manual_scope')
    with canonical_stage('source_list'):
        summaries,terminal=source.enumerate();items=a.source_snapshot(summaries,terminal)
    details={};receipts={};unavailable=0
    for ident in selected:
        if ident not in items:continue
        try:
            with canonical_stage('source_detail'):
                raw=source.detail_raw(ident);details[ident]=detail_note(raw)
            receipts[ident]={'wire_sha256':a.sha(raw),'wire_bytes':len(raw),'detail_sha256':a.sha(a.encoded(details[ident]))}
        except RuntimeError as exc:
            if str(exc)!='GetNote HTTP 404':raise
            unavailable+=1;details[ident]=None;receipts[ident]={'unavailable':True}
    proposed,changes,counts=n.propose_new(evidence,legacy,state_sha,registry,a.sha(a.encoded(registry)),current,summaries,terminal,details,selected_new=selected,preview=preview)
    if counts['created'] or counts['updated'] or counts['unchanged'] or counts['preview_imported']+counts['conflicted']!=CANONICAL_COUNT:fail('manual_plan')
    from get_mirror_render import RECORDINGS
    references={};audio_notes=0;empty_text_notes=0
    for ident,row in proposed['newById'].items():
        data=changes[row['filePath']];note=n.source_note(items[ident],details[ident])
        references[ident]=canonical_reference_receipt(note,data)
        audio_notes+=note.get('note_type') in (RECORDINGS|{'audio'})
        empty_text_notes+=note.get('note_type')=='plain_text' and not n.text_fields(note)
    plan=n.git_candidate(reader,parent,legacy,proposed,changes,current)
    after=reader.tree(plan['candidate_commit'])
    allowed={prefix+n.STATE}|{prefix+saved['entry']['filePath'] for saved in preview.values()}
    changed={p for p in set(before)|set(after) if before.get(p)!=after.get(p)}
    if not changed<=allowed or any(after.get(p)!=row for p,row in before.items()) or set(after)-set(before)!=changed:fail('manual_scope')
    files={p:reader.read(plan['candidate_commit'],p,n.MAX_STATE if p==prefix+n.STATE else a.owned.MAX_NOTE) for p in changed}
    if len(files)!=len(changes)+1 or sum(map(len,files.values()))>MAX_CHANGED_BYTES:fail('manual_budget')
    legacy_deferred=sum(ident not in items or raw_source_version(items[ident].get('updated_at'))!=row['ownership']['source_version'] or a.sha(a.encoded(items[ident]))!=row['summary_sha256'] for ident,row in legacy['legacyById'].items())
    reasons=list(proposed['conflictsById'].values())
    evidence_payload={'contract':CANONICAL_CONTRACT,'rules_sha256':rules_sha256(),'capture_anchor':evidence.anchor,
      'preview_commit':n.PREVIEW,'preview_state_sha256':n.PREVIEW_STATE_SHA,'legacy_state_sha256':state_sha,
      'before_get_tree':reader.source_get_tree(parent),'selected':selected,'source_summary_sha256':a.sha(a.encoded(summaries)),
      'detail_receipts':receipts,'reference_receipts':references,'registry_sha256':plan['registry_sha256'],'changes':plan['changes']}
    result={'status':'canonical_preview_candidate_verified','main_updated':False,'cas_attempts':0,'apply_ready':False,
      'current_preserved':True,'readback_verified':True,'selected':CANONICAL_COUNT,'notes_proposed':len(changes),
      'conflicted':counts['conflicted'],'conflict_reason_counts':n.reason_counts(proposed['conflictsById']),'changed_paths':len(files),'changed_bytes':sum(map(len,files.values())),
      'source_details':len(details),'details_unavailable':unavailable,'legacy_managed':len(legacy['legacyById']),
      'legacy_conflicts':len(legacy['conflictsById']),'legacy_pending':len(legacy['pending_ids']),'legacy_deferred':legacy_deferred,
      'imported_audio_notes':audio_notes,'imported_empty_plain_text_notes':empty_text_notes,
      'reference_notes':sum(bool(x['reference_count']) for x in references.values()),
      'reference_entries':sum(x['reference_count'] for x in references.values()),'asset_downloads':0,
      'source_changed':sum(x in ('preview_source_changed','preview_render_changed') for x in reasons),
      'source_unavailable':sum(x in ('source_missing','detail_identity','source_transcript_pending') for x in reasons),
      'other_conflicts':sum(x not in ('preview_source_changed','preview_render_changed','source_missing','detail_identity','source_transcript_pending') for x in reasons),
      'parent_commit':parent,'candidate_commit':plan['candidate_commit'],'candidate_tree':plan['candidate_tree'],
      'legacy_state_sha256':state_sha,'registry_sha256':plan['registry_sha256'],'plan_sha256':a.sha(a.encoded(plan)),
      'proposal_sha256':a.sha(a.encoded(evidence_payload)),'detail_receipts_sha256':a.sha(a.encoded(receipts)),
      'reference_receipts_sha256':a.sha(a.encoded(references))}
    return result


def run_canonical_preview(token,source,expected_parent,*,state_sha,apply=False,work=None):
    if apply:fail('manual_scope')
    a.checked(expected_parent,40);a.checked(state_sha,64)
    remote=Remote(None,token)
    with canonical_stage('initial_guard'):guard(remote,expected_parent)
    with tempfile.TemporaryDirectory(prefix='get-canonical-prepare-',dir=work) as folder:
        root=Path(folder)
        with canonical_stage('capture_load'):
            local=r.readback_checkout(root/'objects',CAPTURE,token);remote.local=local
            reader,evidence,_=load_evidence(local,remote,token,root,expected_parent,anchor=ANCHOR)
        with canonical_stage('inventory_preview_load'):current,preview=load_canonical_inputs(reader,remote,token,root,expected_parent)
        with canonical_stage('initial_guard'):guard(remote,expected_parent)
        with canonical_stage('candidate'):result=prepare_canonical_preview(reader,evidence,expected_parent,current,preview,source,state_sha=state_sha)
        with canonical_stage('final_guard'):guard(remote,expected_parent)
        result['refs_verified']=True
        return result


def public_canonical_report(value):
    clean={'status':'manual_failed','main_updated':None,'write_state':'unknown'}
    if any(value.get(k) is not expected for k,expected in [('main_updated',False),('apply_ready',False),('current_preserved',True),('readback_verified',True),('refs_verified',True)]):return clean
    if value.get('cas_attempts')!=0 or type(value.get('cas_attempts')) is not int or value.get('asset_downloads')!=0 or type(value.get('asset_downloads')) is not int:return clean
    counts=('selected','notes_proposed','conflicted','changed_paths','changed_bytes','source_details','details_unavailable','legacy_managed','legacy_conflicts','legacy_pending','legacy_deferred','imported_audio_notes','imported_empty_plain_text_notes','reference_notes','reference_entries','source_changed','source_unavailable','other_conflicts')
    if any(type(value.get(key)) is not int or not 0<=value[key]<=MAX_CHANGED_BYTES for key in counts):return clean
    if value['selected']!=CANONICAL_COUNT or value['notes_proposed']+value['conflicted']!=CANONICAL_COUNT or value['changed_paths']!=value['notes_proposed']+1:return clean
    if value['source_changed']+value['source_unavailable']+value['other_conflicts']!=value['conflicted']:return clean
    if not 0<=value['details_unavailable']<=value['source_details']<=CANONICAL_COUNT:return clean
    if not 1<=value['legacy_managed']<=MAX_MANAGED or value['legacy_pending']!=9 or value['legacy_deferred']>value['legacy_managed']:return clean
    if value['imported_audio_notes']+value['imported_empty_plain_text_notes']>value['notes_proposed'] or value['reference_notes']>value['notes_proposed']:return clean
    out={key:value[key] for key in counts}
    for key,width in [('parent_commit',40),('candidate_commit',40),('candidate_tree',40),('legacy_state_sha256',64),('registry_sha256',64),('plan_sha256',64),('proposal_sha256',64),('detail_receipts_sha256',64),('reference_receipts_sha256',64)]:
        try:out[key]=a.checked(value.get(key),width)
        except Exception:return clean
    for key in ('detail_requests','api_requests','http_attempts','http_retries'):
        if type(value.get(key)) is int and 0<=value[key]<=c.MAX_RAW:out[key]=value[key]
    import get_canonical_writer as n
    reasons=value.get('conflict_reason_counts',{})
    if not n.valid_reason_counts(reasons,value['conflicted']):return clean
    out['conflict_reason_counts']=reasons
    out.update(status='canonical_preview_candidate_verified',main_updated=False,cas_attempts=0,apply_ready=False,current_preserved=True,readback_verified=True,refs_verified=True,asset_downloads=0,reference_content_verified=False)
    return out


def public_report(value):
    clean={'status':'manual_failed','main_updated':None,'write_state':'unknown'}
    if not isinstance(value,dict):return clean
    if value.get('status')=='canonical_preview_candidate_verified':return public_canonical_report(value)
    if isinstance(value.get('error'),str) and value['error'] in ERRORS:
        clean['error']=value['error']
        if isinstance(value.get('error_stage'),str) and value['error_stage'] in CANONICAL_STAGES:clean['error_stage']=value['error_stage']
        if isinstance(value.get('error_reason'),str) and value['error_reason'] in CANONICAL_REASONS:clean['error_reason']=value['error_reason']
        for key in ('detail_requests','api_requests','http_attempts','http_retries'):
            if type(value.get(key)) is int and 0<=value[key]<=c.MAX_RAW:clean[key]=value[key]
        write_state=value.get('write_state','unknown')
        if write_state in ('not_attempted','unchanged','uncertain','confirmed'):
            clean['write_state']=write_state;clean['main_updated']=True if write_state=='confirmed' else (None if write_state=='uncertain' else False)
            clean['cas_attempts']=1 if write_state!='not_attempted' else 0
    if value.get('status') not in ('manual_candidate_verified','manual_transaction_verified') or value.get('readback_verified') is not True:return clean
    for key in ('managed','conflicts','pending','changed_paths','notes_updated','changed_bytes','transaction_bytes','cas_attempts','unchanged','source_version_only','source_updated','conflicted','details_used','outside_legacy_cohort','details_unavailable','detail_requests','api_requests','http_attempts','http_retries'):
        if type(value.get(key)) is int and 0<=value[key]<=c.MAX_RAW:clean[key]=value[key]
    for key,width in (('state_sha256',64),('approval_payload_sha256',64),('plan_sha256',64),('parent_commit',40),('candidate_commit',40)):
        try:clean[key]=a.checked(value.get(key),width)
        except Exception:return {'status':'manual_failed','main_updated':None,'write_state':'unknown'}
    if not 1<=clean.get('managed',0)<=MAX_MANAGED or clean.get('pending')!=9 or clean.get('cas_attempts') not in (0,1):return {'status':'manual_failed','main_updated':None,'write_state':'unknown'}
    if not (0<=clean.get('notes_updated',-1)<=clean.get('changed_paths',-1)<=clean['managed']+1 and 0<=clean.get('changed_bytes',-1)<=MAX_CHANGED_BYTES):return {'status':'manual_failed','main_updated':None,'write_state':'unknown'}
    if not 0<=clean.get('transaction_bytes',-1)<=MAX_CHANGED_BYTES:return {'status':'manual_failed','main_updated':None,'write_state':'unknown'}
    if type(value.get('main_updated')) is not bool or value['main_updated']!=(clean['cas_attempts']==1):return {'status':'manual_failed','main_updated':None,'write_state':'unknown'}
    if value['status']=='manual_candidate_verified' and clean['cas_attempts']!=0:return {'status':'manual_failed','main_updated':None,'write_state':'unknown'}
    clean.pop('write_state',None)
    clean.update(status=value['status'],main_updated=value.get('main_updated') is True,readback_verified=True)
    return clean


def checkpoint_data(plan):
    return {'contract':'get-legacy-write-attempt-1','planned_parent_commit':plan['parent_commit'],
      'planned_candidate_commit':plan['candidate_commit'],'planned_state_sha256':plan['state_sha256'],
      'planned_plan_sha256':a.sha(a.encoded(plan)),'planned_payload_sha256':plan['approval_payload_sha256'],'planned_changed_paths':len(plan['changes'])}


def write_checkpoint(path,plan):
    path=Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):fail('manual_checkpoint')
    data=a.encoded(checkpoint_data(plan))
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        if a.bounded_file(path,4096)!=data:fail('manual_checkpoint')
    except Exception:fail('manual_checkpoint')


def read_report(path,checkpoint=None):
    try:value=strict_capture_json(a.bounded_file(Path(path),a.MAX_STATE),a.MAX_STATE)
    except Exception:value={}
    clean=public_report(value)
    if checkpoint and clean['status']=='manual_failed':
        try:
            receipt=strict_capture_json(a.bounded_file(Path(checkpoint),4096),4096)
            if set(receipt)!={'contract','planned_parent_commit','planned_candidate_commit','planned_state_sha256','planned_plan_sha256','planned_payload_sha256','planned_changed_paths'} or receipt['contract']!='get-legacy-write-attempt-1':return clean
            for key,width in (('planned_parent_commit',40),('planned_candidate_commit',40),('planned_state_sha256',64),('planned_plan_sha256',64),('planned_payload_sha256',64)):a.checked(receipt[key],width)
            if type(receipt['planned_changed_paths']) is not int or not 1<=receipt['planned_changed_paths']<=MAX_MANAGED+1:return clean
            clean.update({k:v for k,v in receipt.items() if k!='contract'})
            if clean.get('write_state')=='unknown':clean['write_state']='uncertain'
        except Exception:pass
    return clean


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--expected-parent');parser.add_argument('--state-sha256');parser.add_argument('--approved-payload-sha256');parser.add_argument('--publish-report');parser.add_argument('--checkpoint');parser.add_argument('--apply',action='store_true');parser.add_argument('--canonical-preview',action='store_true');args=parser.parse_args(argv)
    if args.publish_report:
        print(json.dumps(read_report(args.publish_report,args.checkpoint)));return 0
    try:
        a.checked(args.expected_parent,40)
        if args.canonical_preview and (args.apply or args.approved_payload_sha256 or not args.state_sha256):fail('manual_scope')
        if args.apply and not args.checkpoint:fail('manual_checkpoint')
        if args.apply:
            try:a.checked(args.approved_payload_sha256,64)
            except Exception:fail('manual_approval_required')
        if args.state_sha256:a.checked(args.state_sha256,64)
        disabled_writers()
        import sync_getnote_to_raw as sync
        from source_sync_rate import RequestPolicy
        key,client=sync.load_credentials();source=c.Source(key,client,RequestPolicy(seconds=35*60))
        if args.canonical_preview:
            result=run_canonical_preview(os.environ.get('PRIVATE_VAULT_TOKEN'),source,args.expected_parent,state_sha=args.state_sha256)
        else:
            result=run(os.environ.get('PRIVATE_VAULT_TOKEN'),source,args.expected_parent,state_sha=args.state_sha256,apply=args.apply,approved_payload_sha256=args.approved_payload_sha256,checkpoint=(lambda plan:write_checkpoint(args.checkpoint,plan)) if args.checkpoint else None)
        result.update(detail_requests=source.details,api_requests=source.api_requests,http_attempts=source.http_attempts,http_retries=max(0,source.http_attempts-source.api_requests))
    except (Exception,SystemExit) as exc:
        result={'error':str(exc) if str(exc) in ERRORS else 'manual_failed','write_state':getattr(exc,'write_state','not_attempted')}
        if args.canonical_preview:
            result.update(error_stage=getattr(exc,'canonical_stage',None),error_reason=str(exc) if str(exc) in CANONICAL_REASONS else 'other')
            if 'source' in locals():result.update(detail_requests=source.details,api_requests=source.api_requests,http_attempts=source.http_attempts,http_retries=max(0,source.http_attempts-source.api_requests))
    clean=public_report(result);print(json.dumps(clean));return 0 if clean['status']!='manual_failed' else 1

if __name__=='__main__':raise SystemExit(main())
