"""Local canonical Get proposal: one identity owner, no network or ref writes.

Legacy updates remain delegated to the reviewed span adapter. This module plans
only absent/newly managed files, with immutable preview and external state
anchors. Publication, schedules, assets and Notion are deliberately separate.
"""
from copy import deepcopy
from pathlib import Path
import json
import re

import get_legacy_adapter as legacy
import get_mirror_incremental_replay as raw
from get_mirror_render import render,validate_note_shape,transcript_pending,CATEGORIES,safe_filename,title_for
from get_v7_compat import note_identity
from source_sync_mirror_replay import legacy_document

CONTRACT='get-canonical-local-registry-1'
STATE='03 State/canonical-registry.json'
MAX_RECORDS=2500
MAX_EXISTING_OWNERS=5000
MAX_NEW=50
MAX_STATE=8*1024*1024
PREVIEW='013328cc962d9921befb3e25710ba699dd7e46a9'
PREVIEW_STATE='03 State/cloud-sync-state.json'
PREVIEW_STATE_SHA='d05c1aa4773421d0d73a60a6d1cf430158440422a31d89c006accc524e600645'
KNOWN={'id','note_id','title','content','note_type','source','created_at','updated_at','tags','topics',
 'is_child_note','parent_id','children_ids','share_id','ref_content','audio','web_page','audio_url',
 'audio_duration','audio_transcript','attachments','note_url'}
EXTENSIONS={'children_count','chapter_timeline','version','meeting_todos','timestamps','content_state','display_title','entry_type','reference','metadata_state','timeline'}

CONFLICT_REASONS={'source_unknown_fields','source_metadata_shape','source_structured_text_unmapped','source_payload_incomplete','source_shape_unsupported','source_shadowed_field','source_transcript_pending','source_text_missing','source_version_race','detail_identity','detail_required','source_missing','preview_source_changed','preview_render_changed','asset_materialization_required','identity_reserved','destination_collision','local_note_edited','source_note_type_changed','source_created_changed','source_unknown_changed','source_field_missing','baseline_missing','canonical_budget','canonical_note_budget','other'}

def reason_counts(conflicts):
    result={}
    for reason in conflicts.values():
        key=reason if reason in CONFLICT_REASONS else 'other';result[key]=result.get(key,0)+1
    return result

def valid_reason_counts(value,total):
    return isinstance(value,dict) and not set(value)-CONFLICT_REASONS and all(type(v) is int and 0<=v<=MAX_RECORDS for v in value.values()) and sum(value.values())==total


def fail(code):raise raw.ParityFailure(code)

def identity(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9]{1,64}',value):fail('canonical_identity')
    return value

def path_ok(path):
    legacy.path_ok(path)
    if not path.endswith('.md') or path.split('/')[0] not in set(CATEGORIES.values()):fail('canonical_path')
    if any(len(part.encode())>255 for part in Path(path).parts):fail('canonical_path')
    return path


def is_markdown(path):return Path(path).suffix.lower() in ('.md','.markdown')

def api_identity(value):return isinstance(value,str) and re.fullmatch('[0-9]{1,64}',value) is not None

def inventory_identity(value):
    # Preserve historical 32-hex Get IDs verbatim, never reinterpret them as API IDs.
    if api_identity(value) or isinstance(value,str) and re.fullmatch('[0-9a-fA-F]{32}',value):return value
    fail('canonical_identity')

def owner_budget(identities):
    values=set(identities)
    return len(values)<=MAX_EXISTING_OWNERS and sum(api_identity(i) for i in values)<=MAX_RECORDS


def inventory(current):
    """All current Get Markdown bytes must come from one externally pinned tree."""
    result={}
    for path,data in current.items():
        legacy.path_ok(path)
        if not is_markdown(path):continue
        if not isinstance(data,bytes) or len(data)>legacy.owned.MAX_NOTE:fail('canonical_note_budget')
        if not data.startswith((b'---',b'\xef\xbb\xbf---')):continue
        fields,_=legacy_document(data);ident=fields.get('note_id')
        if not ident:continue
        inventory_identity(ident)
        if ident in result:fail('canonical_duplicate_identity')
        result[ident]=path
    if not owner_budget(result):fail('canonical_budget')
    return result


def initial_registry(evidence,legacy_state,legacy_sha,current):
    state=legacy.validate_state(legacy_state,legacy_sha,evidence);present=inventory(current)
    reserved={ident:'blocked' for ident in state['conflictsById']}
    reserved.update({ident:'legacy' for ident in state['legacyById']})
    reserved.update({ident:'pending' for ident in state['pending_ids']})
    reserved.update({ident:'existing_unmanaged' for ident in present if ident not in reserved})
    return {'contract':CONTRACT,'local_proposal_only':True,'apply_ready':False,
      'capture_anchor':deepcopy(evidence.anchor),'legacy_state_sha256':legacy_sha,
      'reserved':reserved,'newById':{},'conflictsById':{},'scan_cursor':None}


def validate_registry(state,expected_sha,evidence,legacy_state,legacy_sha,current):
    if legacy.sha(legacy.encoded(state))!=legacy.checked(expected_sha,64):fail('canonical_state_hash')
    base=initial_registry(evidence,legacy_state,legacy_sha,current)
    if not isinstance(state,dict) or set(state)!=set(base) or state.get('contract')!=CONTRACT or state.get('local_proposal_only') is not True or state.get('apply_ready') is not False or state.get('capture_anchor')!=evidence.anchor:fail('canonical_state_schema')
    new=state['newById'];reserved=state['reserved'];conflicts=state['conflictsById']
    if state['scan_cursor'] is not None:identity(state['scan_cursor'])
    if any(not isinstance(x,dict) for x in (new,reserved,conflicts)) or set(new)&set(reserved):fail('canonical_owner_overlap')
    authoritative=set(legacy_state['legacyById'])|set(legacy_state['conflictsById'])|set(legacy_state['pending_ids'])
    if set(new)&authoritative:fail('canonical_owner_overlap')
    if {i:k for i,k in base['reserved'].items() if i not in new}!=reserved:fail('canonical_reserved_changed')
    if state['legacy_state_sha256']!=legacy_sha:fail('canonical_legacy_binding')
    present=inventory(current);paths=set();legacy_paths={row['filePath'] for row in evidence.census['eligible'].values()}
    keys={'filePath','mirror_sha256','baseline_blob','source_version','summary_sha256','source_sha256','unknown_sha256','text_fields','created_at','preview_anchor','source_metadata'}
    for ident,row in new.items():
        identity(ident)
        if not isinstance(row,dict) or set(row)!=keys:fail('canonical_state_schema')
        path=path_ok(row['filePath'])
        if path in paths or path in legacy_paths or (ident in present and present[ident]!=path):fail('canonical_duplicate_path')
        paths.add(path)
        for key in ('mirror_sha256','summary_sha256','source_sha256','unknown_sha256'):legacy.checked(row[key],64)
        legacy.checked(row['baseline_blob'],40)
        if not raw._raw_valid(row['source_version']) or not raw._raw_valid(row['created_at']):fail('canonical_state_schema')
        if not isinstance(row['text_fields'],list) or row['text_fields']!=sorted(set(row['text_fields'])) or set(row['text_fields'])-set(raw.SOURCE_TEXT_FIELDS):fail('canonical_state_schema')
        if not isinstance(row['source_metadata'],dict):fail('canonical_state_schema')
        validate_extensions(row['source_metadata'],metadata_only=True)
        anchor=row['preview_anchor']
        if anchor is not None and (not isinstance(anchor,dict) or set(anchor)!={'commit','state_sha256','blob'}):fail('canonical_state_schema')
        if anchor is not None:
            for key,width in (('commit',40),('state_sha256',64),('blob',40)):legacy.checked(anchor[key],width)
    for ident,reason in conflicts.items():
        identity(ident)
        if not isinstance(reason,str) or not re.fullmatch('[a-z_]{1,64}',reason):fail('canonical_state_schema')
    if not owner_budget(set(reserved)|set(new)) or len(legacy.encoded(state))>MAX_STATE:fail('canonical_budget')
    return deepcopy(state)


def preview_records(reader,commit=PREVIEW,state_sha=PREVIEW_STATE_SHA):
    """Reader must resolve exact commit paths, never arbitrary object existence."""
    legacy.checked(commit,40);legacy.checked(state_sha,64)
    data=reader.read(commit,legacy.retention.GET+'/'+PREVIEW_STATE,2*1024*1024)
    if legacy.sha(data)!=state_sha:fail('canonical_preview_anchor')
    state=raw.strict_capture_json(data);notes=state.get('notesByCanonicalId')
    if state.get('contract')!=raw.RAW_CONTRACT or state.get('local_proposal_only') is not True or not isinstance(notes,dict) or not 1<=len(notes)<=MAX_NEW:fail('canonical_preview_schema')
    out={};paths=set()
    for canonical,row in notes.items():
        ident=identity(canonical[4:] if isinstance(canonical,str) and canonical.startswith('api:') else '')
        keys={'sourceVersion','filePath','mirrorSha256','sourceContentHash','sourceSummaryHash','sourceUnknownHash','sourceTextFields','origin','history'}
        if not isinstance(row,dict) or set(row)!=keys or row.get('origin')!='faithful-created' or row.get('history')!=[] or not raw._raw_valid(row['sourceVersion']):fail('canonical_preview_schema')
        for key in ('mirrorSha256','sourceContentHash','sourceSummaryHash','sourceUnknownHash'):legacy.checked(row[key],64)
        fields=row['sourceTextFields']
        if not isinstance(fields,list) or any(not isinstance(x,str) or x not in raw.SOURCE_TEXT_FIELDS for x in fields) or fields!=sorted(set(fields)):fail('canonical_preview_schema')
        path=path_ok(row.get('filePath'))
        if path in paths:fail('canonical_duplicate_path')
        paths.add(path);blob=reader.read(commit,legacy.retention.GET+'/'+path,legacy.owned.MAX_NOTE)
        if legacy.sha(blob)!=row.get('mirrorSha256') or legacy_document(blob)[0].get('note_id')!=ident:fail('canonical_preview_blob')
        header=json_header(blob)
        baseline_header(ident,{'created_at':raw.raw_source_version(header.get('created_at')),'source_version':row['sourceVersion'],'filePath':path},blob)
        out[ident]={'entry':deepcopy(row),'bytes':blob,'anchor':{'commit':commit,'state_sha256':state_sha,'blob':legacy.retention.oid(blob)}}
    return out


def source_note(summary,detail):
    if not isinstance(detail,dict) or note_identity(detail)!=note_identity(summary):fail('detail_identity')
    if raw.raw_source_version(detail.get('updated_at'))!=raw.raw_source_version(summary.get('updated_at')):fail('source_version_race')
    note={**summary,**detail};validate_note_shape(note);raw.source_text_fields(note)
    validate_metadata(note)
    if set(text_fields(summary))-set(text_fields(note)):fail('source_text_missing')
    if set(note)-KNOWN-EXTENSIONS:fail('source_unknown_fields')
    validate_extensions(note)
    if note.get('note_url') not in (None,'','https://www.biji.com/note/'+note_identity(note)):fail('source_unknown_fields')
    for name,allowed in [('audio',{'original','transcript','play_url','duration','sentences'}),('web_page',{'url','content','excerpt'})]:
        if note.get(name) and set(note[name])-allowed:fail('source_unknown_fields')
    for nested,flat in [('transcript','audio_transcript'),('play_url','audio_url'),('duration','audio_duration')]:
        first=(note.get('audio') or {}).get(nested);second=note.get(flat)
        if first not in (None,'',0) and second not in (None,'',0) and legacy.encoded(first)!=legacy.encoded(second):fail('source_shadowed_field')
    validate_collections(note)
    if transcript_pending(note):fail('source_transcript_pending')
    raw.raw_source_version(note.get('created_at'))
    return note


def validate_collections(note):
    for field in ('tags','topics'):
        values=note.get(field)
        if values is not None and (not isinstance(values,list) or any(not isinstance(x,dict) or 'name' not in x or set(x)-({'id','name','type'} if field=='tags' else {'id','name'}) or not isinstance(x['name'],str) or ('id' in x and (type(x['id']) not in (str,int))) or ('type' in x and not isinstance(x['type'],str)) for x in values)):fail('source_unknown_fields')
    # Existing remote audio/video/link policy only; no implicit binary fetching.
    for item in note.get('attachments') or []:
        if item.get('type') not in ('audio','video','link'):fail('asset_materialization_required')
        if set(item)-{'type','url','title','size','duration'}:fail('source_unknown_fields')
        if 'title' in item and not isinstance(item['title'],str):fail('source_shape_unsupported')
        if 'size' in item and (type(item['size']) is not int or item['size']<0):fail('source_shape_unsupported')
    for item in note.get('attachments') or []:
        value=item.get('duration')
        if value is not None and (type(value) not in (int,float) or not __import__('math').isfinite(value) or value<0):fail('source_shape_unsupported')

def source_metadata(note):
    # Exact secondary metadata remains in the same private registry. Timed
    # sentence/chapter text is retained here, not claimed as rendered in MD.
    # This replaces one current metadata value, never appends snapshots.
    result=deepcopy({k:v for k,v in note.items() if k in EXTENSIONS|{'tags','topics','attachments'}})
    if isinstance(note.get('audio'),dict) and 'sentences' in note['audio']:result['audio']={'sentences':deepcopy(note['audio']['sentences'])}
    return result

def metadata_missing(before,after):
    if isinstance(before,dict):
        return not isinstance(after,dict) or any(k not in after or metadata_missing(v,after[k]) for k,v in before.items())
    if isinstance(before,list):
        if not isinstance(after,list) or len(after)<len(before):return True
        return any(metadata_missing(x,y) for x,y in zip(before,after))
    return before not in (None,'',False,0) and after in (None,'',[],{},False,0)

def immutable_unknown_hash(note):
    value=deepcopy({k:v for k,v in note.items() if k not in EXTENSIONS})
    if isinstance(value.get('audio'),dict):value['audio'].pop('sentences',None)
    return raw._source_unknown_hash(value)

def validate_extensions(note,*,metadata_only=False):
    if metadata_only and set(note)-EXTENSIONS-{'tags','topics','attachments','audio'}:fail('canonical_state_schema')
    if metadata_only:
        validate_note_shape(note);validate_collections(note)
        if 'audio' in note and (not isinstance(note['audio'],dict) or set(note['audio'])!={'sentences'}):fail('canonical_state_schema')
    sentences=(note.get('audio') or {}).get('sentences')
    if sentences is not None:
        if not isinstance(sentences,list):fail('source_metadata_shape')
        for row in sentences:
            if not isinstance(row,dict) or set(row)!={'end_ms','files','is_me','speaker_id','speaker_name','start_ms','text'}:fail('source_metadata_shape')
            if any(type(row[k]) is not int or row[k]<0 for k in ('start_ms','end_ms')) or row['end_ms']<row['start_ms'] or type(row['is_me']) is not bool or type(row['speaker_id']) not in (str,int) or not isinstance(row['speaker_name'],str) or not isinstance(row['text'],str) or not isinstance(row['files'],list):fail('source_metadata_shape')
            if row['files']:fail('asset_materialization_required')
    for key in ('children_count','version'):
        if key in note and (type(note[key]) is not int or note[key]<0):fail('source_metadata_shape')
    for key in ('display_title','entry_type'):
        if key in note and not isinstance(note[key],str):fail('source_metadata_shape')
    for key in ('chapter_timeline','meeting_todos'):
        if key not in note:continue
        value=note[key]
        if not isinstance(value,dict) or set(value)!={'items','source'} or not isinstance(value['items'],list) or not isinstance(value['source'],str):fail('source_metadata_shape')
        if key=='meeting_todos' and value['items']:fail('source_structured_text_unmapped')
        if key=='chapter_timeline':
            for row in value['items']:
                if not isinstance(row,dict) or set(row)!={'start_ms','title'} or type(row['start_ms']) is not int or row['start_ms']<0 or not isinstance(row['title'],str):fail('source_metadata_shape')
    if 'timeline' in note:
        value=note['timeline']
        if not isinstance(value,dict) or set(value)!={'moments','resources','schema_version','version'} or any(type(value[k]) is not int or value[k]<0 for k in ('schema_version','version')) or any(not isinstance(value[k],list) for k in ('moments','resources')):fail('source_metadata_shape')
        if value['moments'] or value['resources']:fail('source_structured_text_unmapped')
    if 'reference' in note and note['reference'] is not None:fail('source_structured_text_unmapped')
    if 'timestamps' in note:
        value=note['timestamps']
        if not isinstance(value,dict) or set(value)!={'created_at','edited_at','record_updated_at'}:fail('source_metadata_shape')
        for item in value.values():raw.raw_source_version(item)
    for key,fields,expected in [('content_state',{'content','ref_content'},'full'),('metadata_state',{'reference','topics'},'complete')]:
        if key not in note:continue
        value=note[key]
        if not isinstance(value,dict) or set(value)!=fields or any(not isinstance(v,str) for v in value.values()):fail('source_metadata_shape')
        if any(v!=expected for v in value.values()):fail('source_payload_incomplete')


def validate_metadata(note):
    import math
    for name in ('title','source','audio_url'):
        if note.get(name) is not None and not isinstance(note[name],str):fail('source_shape_unsupported')
    if note.get('is_child_note') is not None and type(note['is_child_note']) is not bool:fail('source_shape_unsupported')
    for name in ('parent_id','share_id'):
        value=note.get(name)
        if value is not None and (type(value) not in (str,int) or isinstance(value,str) and value!=value.strip()):fail('source_shape_unsupported')
    for child in note.get('children_ids') or []:
        if type(child) not in (str,int) or not re.fullmatch('[0-9]{1,64}',str(child)):fail('source_shape_unsupported')
    for parent,names in ((note.get('audio') or {},('play_url',)),(note.get('web_page') or {},('url',))):
        for name in names:
            if parent.get(name) is not None and not isinstance(parent[name],str):fail('source_shape_unsupported')
    for duration in (note.get('audio_duration'),(note.get('audio') or {}).get('duration')):
        if duration is not None and (type(duration) not in (int,float) or not math.isfinite(duration) or duration<0):fail('source_shape_unsupported')


def baseline_header(ident,row,data):
    fields=json_header(data)
    keys={'source_system','note_id','title','note_type','source','created_at','updated_at','tags','topics','is_child_note','parent_id','children_ids','share_id','web_url','web_domain','web_excerpt','audio_url','audio_duration','audio_transcript','attachments'}
    if set(fields)!=keys or fields['source_system']!='getnote' or fields['note_id']!=ident:fail('canonical_baseline_identity')
    if raw.raw_source_version(fields['created_at'])!=row['created_at'] or raw.raw_source_version(fields['updated_at'])!=row['source_version']:fail('canonical_baseline_version')
    if CATEGORIES.get(fields['note_type'])!=row['filePath'].split('/')[0]:fail('canonical_baseline_type')
    for name in ('title','source','parent_id','share_id','web_url','web_domain','web_excerpt','audio_url','audio_transcript'):
        if not isinstance(fields[name],str):fail('canonical_generated_header')
    if type(fields['is_child_note']) is not bool:fail('canonical_generated_header')
    for name in ('tags','topics','children_ids','attachments'):
        if not isinstance(fields[name],list) or any(not isinstance(x,str) for x in fields[name]):fail('canonical_generated_header')
    return fields

def text_fields(note):
    fields=[]
    for name in raw.SOURCE_TEXT_FIELDS:
        value=note
        for part in name.split('.'):
            value=value.get(part) if isinstance(value,dict) else None
        if isinstance(value,str) and value!='':fields.append(name)
    return sorted(fields)


def render_exact(note,path):
    data,fields=render(note,[],Path(path),faithful=True)
    # Preserve exact human-supplied title/tag/topic text, while keeping the
    # shared preview's readable derived fallback for absent/empty titles.
    head,sep,body=data.partition('\n---\n')
    if not sep:fail('canonical_generated_header')
    exact={}
    if isinstance(note.get('title'),str) and note['title']:exact['title']=note['title']
    for name in ('tags','topics'):
        if isinstance(note.get(name),list):exact[name]=[x['name'] for x in note[name]]
    for name,value in exact.items():
        old=name+': '+json.dumps(fields[name],ensure_ascii=False,separators=(',',':'))
        new=name+': '+json.dumps(value,ensure_ascii=False,separators=(',',':'))
        lines=head.split('\n')
        if lines.count(old)!=1:fail('canonical_generated_header')
        head='\n'.join(new if line==old else line for line in lines)
    return (head+sep+body).encode()


def new_path(note,ident):
    category=CATEGORIES.get(note.get('note_type'))
    if not category:fail('source_shape_unsupported')
    title=safe_filename(title_for(note));suffix='-'+ident+'.md';budget=255-len(suffix.encode())
    while len(title.encode())>budget:title=title[:-1]
    return path_ok(category+'/'+title+suffix)


def record(note,summary,path,data,anchor=None):
    return {'filePath':path,'mirror_sha256':legacy.sha(data),'baseline_blob':legacy.retention.oid(data),
      'source_version':raw.raw_source_version(note['updated_at']),'summary_sha256':raw._source_content_hash(summary),
      'source_sha256':raw._source_content_hash(note),'unknown_sha256':immutable_unknown_hash(note),
      'text_fields':text_fields(note),'created_at':raw.raw_source_version(note['created_at']),
      'preview_anchor':deepcopy(anchor),'source_metadata':source_metadata(note)}


def propose_new(evidence,legacy_state,legacy_sha,registry,registry_sha,current,summaries,terminal,details,*,selected_new=(),preview=None,baseline_errors=None,allow_fresh_preview=False):
    """Return actual candidate bytes and state; no side effects or source calls."""
    state=validate_registry(registry,registry_sha,evidence,legacy_state,legacy_sha,current)
    items=legacy.source_snapshot(summaries,terminal);preview=preview or {};selected=list(selected_new)
    if len(selected)>MAX_NEW or len(selected)!=len(set(selected)):fail('canonical_selection')
    for ident in selected:identity(ident)
    present=inventory(current);occupied=set(current);changes={};conflicts=deepcopy(state['conflictsById']);counts=dict.fromkeys(('created','preview_imported','updated','state_only','unchanged','conflicted','detail_required'),0)
    targets=sorted(set(state['newById'])|set(selected))
    for ident in targets:
        conflicts.pop(ident,None)
        old=state['newById'].get(ident)
        try:
            if ident in (baseline_errors or {}):fail(baseline_errors[ident])
            if ident in state['reserved']:fail('identity_reserved')
            if ident not in items:fail('source_missing')
            summary=items[ident];token=raw.raw_source_version(summary.get('updated_at'))
            if old:
                path=old['filePath'];before=current.get(path)
                if before is None or legacy.sha(before)!=old['mirror_sha256']:fail('local_note_edited')
                baseline_header(ident,old,before)
                if token==old['source_version'] and raw._source_content_hash(summary)==old['summary_sha256']:
                    counts['unchanged']+=1;continue
            elif ident in present:fail('identity_reserved')
            if not old and not owner_budget(set(state['reserved'])|set(state['newById'])|{ident}):fail('canonical_budget')
            if ident not in details:counts['detail_required']+=1;fail('detail_required')
            note=source_note(summary,details[ident])
            if old:
                if json_header(before)['note_type']!=note.get('note_type'):fail('source_note_type_changed')
                if raw.raw_source_version(note['created_at'])!=old['created_at']:fail('source_created_changed')
                if immutable_unknown_hash(note)!=old['unknown_sha256']:fail('source_unknown_changed')
                if metadata_missing(old['source_metadata'],source_metadata(note)):fail('source_field_missing')
                if set(old['text_fields'])-set(text_fields(note)):fail('source_text_missing')
                data=render_exact(note,path)
                # No silently emptied named metadata or removed fields.
                old_fields=json_header(before);new_fields=json_header(data)
                empty=(None,'',[],{},0,False)
                if set(old_fields)-set(new_fields) or any(v not in empty and new_fields.get(k) in empty for k,v in old_fields.items() if k!='updated_at'):fail('source_field_missing')
                anchor=old['preview_anchor'];outcome='updated' if data!=before else 'state_only'
            elif ident in preview:
                saved=preview[ident];entry=saved['entry'];path=path_ok(entry['filePath'])
                if path in occupied:fail('destination_collision')
                data=render_exact(note,path)
                exact=(token==entry['sourceVersion'] and raw._source_content_hash(note)==entry['sourceContentHash'] and raw._source_content_hash(summary)==entry['sourceSummaryHash'] and data==saved['bytes'])
                if exact:outcome='preview_imported'
                else:
                    if not allow_fresh_preview:fail('preview_source_changed')
                    # The preview is an immutable selection/audit anchor, not an
                    # existing main owner. A fresh absent identity can be created
                    # from its fully validated current source without rewriting it.
                    path=new_path(note,ident)
                    if path in occupied:fail('destination_collision')
                    data=render_exact(note,path);outcome='created'
                anchor=saved['anchor']
            else:
                category=CATEGORIES.get(note.get('note_type'))
                if not category:fail('source_shape_unsupported')
                path=new_path(note,ident)
                if path in occupied:fail('destination_collision')
                data=render_exact(note,path);anchor=None;outcome='created'
            if len(data)>legacy.owned.MAX_NOTE:fail('canonical_note_budget')
            state['newById'][ident]=record(note,summary,path,data,anchor);changes[path]=data;occupied.add(path);counts[outcome]+=1
        except (raw.ParityFailure,ValueError,TypeError) as exc:
            reason=str(exc)
            conflicts[ident]=reason if re.fullmatch('[a-z_]{1,64}',reason or '') else 'source_shape_unsupported'
            counts['conflicted']+=1
    state['legacy_state_sha256']=legacy_sha;state['conflictsById']=conflicts
    if len(legacy.encoded(state))>MAX_STATE:fail('canonical_budget')
    validate_registry(state,legacy.sha(legacy.encoded(state)),evidence,legacy_state,legacy_sha,{**current,**changes})
    return state,changes,counts


def json_header(data):
    try:
        header=data.decode().split('\n---\n',1)[0]
        if not header.startswith('---\n'):raise ValueError()
        pairs=[line.split(': ',1) for line in header.split('\n')[1:]]
        if len({p[0] for p in pairs})!=len(pairs):raise ValueError()
        return {key:json.loads(value) for key,value in pairs}
    except (ValueError,UnicodeError):fail('canonical_generated_header')


def git_inventory(repo,parent):
    """Read every Markdown identity from one immutable tree, without assets."""
    tree=repo.tree(parent);current={};total=0
    for path,row in tree.items():
        if not path.startswith(legacy.retention.GET+'/'):continue
        legacy.retention.safe_path(path)
        if row[:2]!=('100644','blob'):fail('canonical_alias')
        rel=path[len(legacy.retention.GET)+1:]
        if is_markdown(rel) or rel in (STATE,legacy.STATE):
            maximum=MAX_STATE if rel in (STATE,legacy.STATE) else legacy.owned.MAX_NOTE
            data=repo.read(parent,path,maximum);total+=len(data)
            if total>legacy.MAX_TREE:fail('canonical_budget')
            current[rel]=data
    inventory(current)
    return current


def git_candidate(repo,parent,legacy_state,registry,changes,current):
    """Materialize one local immutable candidate; no refs/index/worktree writes."""
    import tempfile
    before=repo.tree(parent);local=repo.local;prefix=legacy.retention.GET+'/'
    evaluated={p[len(prefix):] for p in before if p.startswith(prefix) and (is_markdown(p) or p[len(prefix):] in (STATE,legacy.STATE))}
    if set(current)!=evaluated:fail('canonical_current_set')
    for rel,value in current.items():
        if before.get(prefix+legacy.path_ok(rel))!=('100644','blob',legacy.retention.oid(value)):fail('canonical_parent_changed')
    allowed={row['ownership']['filePath'] for row in legacy_state['legacyById'].values()}|{row['filePath'] for row in registry['newById'].values()}
    if set(changes)-allowed:fail('canonical_write_scope')
    for rel,data in changes.items():
        if not isinstance(data,bytes) or len(data)>legacy.owned.MAX_NOTE:fail('canonical_note_budget')
    relative={**changes,legacy.STATE:legacy.encoded(legacy_state),STATE:legacy.encoded(registry)}
    if any(len(relative[x])>MAX_STATE for x in (STATE,legacy.STATE)):fail('canonical_budget')
    writes={};rows=[];wanted=dict(before)
    for rel,value in sorted(relative.items()):
        path=prefix+legacy.path_ok(rel);blob=legacy.retention.oid(value);old=before.get(path)
        if old==('100644','blob',blob):continue
        if old is not None and old[:2]!=('100644','blob'):fail('canonical_alias')
        writes[path]=value;wanted[path]=('100644','blob',blob)
        rows.append({'path':rel,'before_blob':old[2] if old else None,'after_blob':blob,'sha256':legacy.sha(value),'bytes':len(value)})
    if sum(len(x) for x in writes.values())>64*1024*1024 or len(writes)>MAX_RECORDS+2:fail('canonical_budget')
    if writes:
        with tempfile.TemporaryDirectory(prefix='get-canonical-index-') as folder:
            env=dict(local.env,GIT_INDEX_FILE=str(Path(folder)/'index'))
            local.run('read-tree',parent,env=env)
            for path,value in writes.items():
                blob=local.run('hash-object','-w','--stdin',data=value,env=env).decode().strip()
                if blob!=legacy.retention.oid(value):fail('canonical_readback')
                local.run('update-index','--add','--cacheinfo','100644,'+blob+','+path,env=env)
            tree=legacy.checked(local.run('write-tree','--missing-ok',env=env).decode().strip(),40)
            if local.tree(tree)!=wanted or any(local.blob(wanted[p][2])!=v for p,v in writes.items()):fail('canonical_readback')
            timestamp=local.run('show','-s','--format=%ct',parent).decode().strip()
            if not timestamp.isdigit():fail('canonical_parent_changed')
            env.update(GIT_AUTHOR_NAME='Get local proposal',GIT_AUTHOR_EMAIL='get-proposal@example.invalid',GIT_COMMITTER_NAME='Get local proposal',GIT_COMMITTER_EMAIL='get-proposal@example.invalid',GIT_AUTHOR_DATE='@'+timestamp+' +0000',GIT_COMMITTER_DATE='@'+timestamp+' +0000')
            commit=legacy.checked(local.run('commit-tree',tree,'-p',parent,'-m','Prepare local canonical Get proposal',env=env).decode().strip(),40)
        if local.run('rev-list','--parents','-n','1',commit).decode().strip().split()!=[commit,parent] or repo.tree(commit)!=wanted:fail('canonical_readback')
    else:commit=parent;tree=local.run('rev-parse',parent+'^{tree}').decode().strip()
    return {'contract':CONTRACT,'parent_commit':parent,'candidate_commit':commit,'candidate_tree':tree,
      'registry_sha256':legacy.sha(relative[STATE]),'legacy_sha256':legacy.sha(relative[legacy.STATE]),
      'changes':rows,'local_proposal_only':True,'mirror_adopted':False,'apply_ready':False}


def prepare(repo,parent,evidence,legacy_sha,registry_sha,summaries,terminal,details,*,selected_new=(),preview=None):
    """Single owner dispatcher: legacy spans plus generated-new-note updates.

    External anchor hashes come from reviewed immutable receipts, never from
    untrusted input recomputation. The input legacy state must already belong
    to parent. This cannot bootstrap or publish the pending formal adoption.
    """
    current=git_inventory(repo,parent)
    if legacy.STATE not in current:fail('canonical_legacy_state_missing')
    legacy_state=legacy.validate_state(raw.strict_capture_json(current[legacy.STATE],MAX_STATE),legacy_sha,evidence)
    baseline_errors={}
    if STATE in current:
        if not registry_sha:fail('canonical_state_hash')
        registry=validate_registry(raw.strict_capture_json(current[STATE],MAX_STATE),registry_sha,evidence,legacy_state,legacy_sha,current)
        if legacy.encoded(registry)!=current[STATE]:fail('canonical_state_schema')
        for ident,row in registry['newById'].items():
            try:
                baseline=repo.baseline(parent,row['filePath'],row['baseline_blob'],row['mirror_sha256'])
                baseline_header(ident,row,baseline)
            except raw.ParityFailure as exc:
                if str(exc)!='baseline_missing':raise
                baseline_errors[ident]='baseline_missing'
    else:
        if registry_sha:fail('canonical_state_hash')
        registry=initial_registry(evidence,legacy_state,legacy_sha,current);registry_sha=legacy.sha(legacy.encoded(registry))
    if not isinstance(details,dict):fail('canonical_details')
    items=legacy.source_snapshot(summaries,terminal)
    legacy_needed={i for i,row in legacy_state['legacyById'].items() if i in items and (raw.raw_source_version(items[i].get('updated_at'))!=row['ownership']['source_version'] or legacy.sha(legacy.encoded(items[i]))!=row['summary_sha256'])}
    unknown=set(details)-(legacy_needed|set(registry['newById'])|set(selected_new))
    if unknown:fail('canonical_details')
    # Missing legacy detail is an individual unresolved record, never a reason
    # to reroute the identity through the new-note renderer.
    next_legacy,legacy_changes,old_counts=legacy.propose_updates(evidence,legacy_state,legacy_sha,current,repo,parent,summaries,terminal,{i:details.get(i) for i in legacy_needed})
    next_sha=legacy.sha(legacy.encoded(next_legacy))
    registry=deepcopy(registry);registry['legacy_state_sha256']=next_sha
    producer_registry_sha=legacy.sha(legacy.encoded(registry))
    next_registry,new_changes,new_counts=propose_new(evidence,next_legacy,next_sha,registry,producer_registry_sha,current,summaries,terminal,{i:v for i,v in details.items() if i not in legacy_needed},selected_new=selected_new,preview=preview,baseline_errors=baseline_errors,allow_fresh_preview=True)
    if set(legacy_changes)&set(new_changes):fail('canonical_owner_overlap')
    plan=git_candidate(repo,parent,next_legacy,next_registry,{**legacy_changes,**new_changes},current)
    return plan,{'legacy':next_legacy,'registry':next_registry},{'legacy_'+k:v for k,v in old_counts.items()}|{'new_'+k:v for k,v in new_counts.items()}


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('repo','output','source_snapshot'):parser.add_argument('--'+name.replace('_','-'),type=Path,required=True)
    for name in ('parent','capture_commit','census_sha256','ledger_sha256','legacy_sha256'):parser.add_argument('--'+name.replace('_','-'),required=True)
    parser.add_argument('--registry-sha256');parser.add_argument('--import-fixed-preview',action='store_true')
    args=parser.parse_args()
    try:
        repo=legacy.OfflineGit(args.repo);evidence=legacy.CaptureEvidence(repo,{'capture_commit':args.capture_commit,'census_sha256':args.census_sha256,'ledger_sha256':args.ledger_sha256})
        source=raw.strict_capture_json(legacy.bounded_file(args.source_snapshot,128*1024*1024),128*1024*1024)
        if set(source)!={'summaries','terminal','details','selected_new'}:fail('canonical_source_schema')
        preview=preview_records(repo) if args.import_fixed_preview else None
        if preview is not None and set(source['selected_new'])!=set(preview):fail('canonical_selection')
        plan,states,counts=prepare(repo,args.parent,evidence,args.legacy_sha256,args.registry_sha256,source['summaries'],source['terminal'],source['details'],selected_new=source['selected_new'],preview=preview)
        legacy.save_git_plan(args.output,plan,states)
        print(json.dumps({'status':'canonical_local_candidate_verified','changed_paths':len(plan['changes']),'registry_sha256':plan['registry_sha256'],'legacy_sha256':plan['legacy_sha256'],'apply_ready':False,'mirror_adopted':False,**counts}));return 0
    except Exception:
        print(json.dumps({'status':'canonical_local_candidate_failed','apply_ready':False,'mirror_adopted':False}));return 1

if __name__=='__main__':raise SystemExit(main())
