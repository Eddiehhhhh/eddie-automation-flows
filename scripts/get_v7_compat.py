"""Offline port of the committed v7 mirror fast path, not a publisher.

No new raw-cache format or binary acquisition is introduced. Caller supplies
the existing plugin state and real mirror evidence. Source list freshness and
authenticated execution approval are external responsibilities.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import PurePosixPath


def api_id(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)):
        return ''
    return str(value).strip()

def note_identity(note):
    # Match reviewed source_sync_sample.detail_id: a conflicting string alias
    # is rejected; a legacy numeric alias cannot override authoritative note_id.
    ident=api_id(note.get('note_id',note.get('id')))
    alias=note.get('id')
    if not ident or (isinstance(alias,str) and api_id(alias)!=ident):
        raise ValueError('invalid_identity')
    return ident

def timestamp_ms(value):
    """Supported ISO source contract. Invalid/unknown values never mean unchanged."""
    if not isinstance(value,str) or not value.strip(): return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        # Never guess the runner timezone for an unqualified source timestamp.
        if parsed.tzinfo is None:return None
        return parsed.timestamp()*1000
    except (ValueError,OverflowError):return None

def state_timestamp(value):
    # Match JS Number for normal finite positive existing plugin epoch-ms fields.
    if isinstance(value,bool):return None
    try:
        result=float(value)
        return result if 0<result<float('inf') else None
    except (ValueError,TypeError,OverflowError):return None

def near(left,right):
    return left is not None and right is not None and abs(left-right)<1000

def mirror_index(mirrors):
    """Mirrors are actual parsed Markdown evidence, not manifest placeholders.

    Each has path, source_id, exists, updated_at. Duplicate actual identities
    are an explicit migration conflict, rather than inheriting JS first-wins.
    """
    index={};conflicts=set()
    for item in mirrors:
        if item.get('exists') is not True:continue
        ident=api_id(item.get('source_id'))
        if not ident:continue
        if ident in index:conflicts.add(ident)
        else:index[ident]=item
    return index,conflicts

def recover_unchanged(summary,state,mirrors,now_ms):
    """Faithful v7 list-stage decision; returns a copied plugin state.

    It does NOT inspect content hashes, note_type, pending transcription, asset
    integrity or local updated_at. Therefore user-edited body bytes are retained,
    but these checks must not be claimed as having happened.
    """
    try:ident=note_identity(summary)
    except ValueError:return {'decision':'detail','reason':'missing_id','state':state}
    original=state;result=deepcopy(state)
    if not ident:return {'decision':'detail','reason':'missing_id','state':original}
    entries=result.get('notesByCanonicalId') or {}
    entry=entries.get('api:'+ident)
    if not isinstance(entry,dict):return {'decision':'detail','reason':'missing_state','state':original}
    remote=timestamp_ms(summary.get('updated_at'))
    trusted=state_timestamp(entry.get('updatedAt'))
    if not near(remote,trusted):return {'decision':'detail','reason':'version_changed_or_unknown','state':original}
    index,conflicts=mirror_index(mirrors)
    if ident in conflicts:return {'decision':'conflict','reason':'duplicate_api_id','state':original}
    mirror=index.get(ident)
    if mirror is None:return {'decision':'detail','reason':'missing_verified_mirror','state':original}
    entry['filePath']=mirror['path']
    entry['stem']=PurePosixPath(mirror['path']).stem
    entry['deleted']=False
    entry['lastSyncedAt']=now_ms
    return {'decision':'skip','reason':'v7_verified_state_version_and_local_id','path':mirror['path'],'state':result}

def local_mirror_matches_note(mirror,note,trusted_state_updated_at=0):
    """Post-detail v5/v7 helper; caller controls adoptedLocal/wasDeleted guard."""
    ident=api_id((note.get('sourceIds') or {}).get('api'))
    remote=state_timestamp(note.get('updatedAt'))
    if not mirror or mirror.get('exists') is not True or not ident or remote is None:return False
    if api_id(mirror.get('source_id'))!=ident:return False
    return near(timestamp_ms(mirror.get('updated_at')),remote) or near(state_timestamp(trusted_state_updated_at),remote)

def classify_list(summaries,state,mirrors,now_ms):
    counts={'enumerated':0,'cached_skipped':0,'needs_detail':0,'conflicts':0}
    decisions={};seen=set();current=state
    for summary in summaries:
        ident=note_identity(summary)
        if not ident or ident in seen:raise ValueError('invalid or repeated source ID')
        seen.add(ident);counts['enumerated']+=1
        item=recover_unchanged(summary,current,mirrors,now_ms)
        decisions[ident]={k:v for k,v in item.items() if k!='state'}
        if item['decision']=='skip':counts['cached_skipped']+=1;current=item['state']
        elif item['decision']=='conflict':counts['conflicts']+=1
        else:counts['needs_detail']+=1
    return counts,decisions,current
