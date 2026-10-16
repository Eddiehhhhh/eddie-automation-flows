"""Local-only legacy ownership/update proposals. No filesystem or API writer.

Capture is not adoption: proposals remain blocked from apply and are not accepted
by the live faithful-state loader. Production must separately approve exact maps.
"""
from copy import deepcopy
import hashlib
import json
import re
from pathlib import PurePosixPath
from get_v7_compat import note_identity

from get_mirror_incremental_replay import (
    SOURCE_TEXT_FIELDS, capture_json_bytes, raw_source_version,
    observation_detail, verify_legacy_observation, ParityFailure,
)
from get_mirror_render import validate_note_shape
from source_sync_mirror_replay import legacy_document

CONTRACT='get-legacy-owned-spans-local-1'
MARKERS=('## 🧠 摘要\n','## 📝 正文\n','## 🔗 来源\n','## 📎 附件\n')
MAX_NOTE=1024*1024
ROLE_FIELDS={'frontmatter:audio_transcript':{'audio.original','audio.transcript','audio_transcript'},'frontmatter:web_excerpt':{'ref_content','web_page.content','web_page.excerpt'}}

def digest(data):return hashlib.sha256(data).hexdigest()
def stop(reason):raise ParityFailure(reason)

def regions(data):
    """Unambiguous structural slots; never locate source via substring coverage."""
    if not isinstance(data,bytes) or len(data)>MAX_NOTE:stop('legacy_note_budget')
    fields,body=legacy_document(data)
    head_end=data.index(b'\n---\n')+5
    body_start=len(data)-len(body.encode())
    if head_end!=body_start:stop('legacy_structure_ambiguous')
    positions=[]
    for marker in MARKERS:
        encoded=marker.encode()
        if data[body_start:].count(encoded)!=1:stop('legacy_structure_ambiguous')
        positions.append(data.index(encoded,body_start))
    if positions!=sorted(positions):stop('legacy_structure_ambiguous')
    out={}
    for index,key in ((0,'summary'),(1,'body')):
        start=positions[index]+len(MARKERS[index].encode());end=positions[index+1]
        if data[end-2:end]!=b'\n\n':stop('legacy_structure_ambiguous')
        out[key]={'start':start,'length':end-start-2,'encoding':'utf8','bytes':data[start:end-2]}
    seen=set();cursor=4;header_values={}
    for line in data[4:head_end-5].split(b'\n'):
        key,separator,raw=line.partition(b':')
        if not separator or key in seen:stop('legacy_structure_ambiguous')
        seen.add(key);name=key.decode();header_values[name]=raw.strip()
        if name in ('audio_transcript','web_excerpt') and raw.strip():
            token=raw.strip()
            try:value=json.loads(token)
            except (ValueError,UnicodeError):value=None
            if isinstance(value,str):
                start=cursor+len(key)+1+len(raw)-len(raw.lstrip())
                out['frontmatter:'+name]={'start':start,'length':len(token),'encoding':'json-string','bytes':token,'value':value}
            elif fields.get(name):stop('legacy_text_scalar_unsupported')
        cursor+=len(line)+1
    return fields,out,header_values

def text_values(note):
    try:validate_note_shape(note)
    except (ValueError,TypeError):stop('source_shape_unsupported')
    out={}
    for path in SOURCE_TEXT_FIELDS:
        value=note;present=True
        for part in path.split('.'):
            if not isinstance(value,dict):stop('source_shape_unsupported')
            if part not in value:present=False;break
            value=value[part]
            if value is None and part!=path.split('.')[-1]:present=False;break
        if present and value is not None and not isinstance(value,str):stop('source_shape_unsupported')
        out[path]=('absent',None) if not present else (('null',None) if value is None else ('text',value))
    return out

def text_receipts(values):
    return {key:{'kind':kind,'sha256':digest(value.encode()) if kind=='text' else None,'bytes':len(value.encode()) if kind=='text' else 0}
            for key,(kind,value) in values.items()}

def metadata_hash(note):
    value=deepcopy(note);value.pop('updated_at',None)
    for path in SOURCE_TEXT_FIELDS:
        components=path.split('.');parent=value
        for key in components[:-1]:
            parent=parent.get(key) if isinstance(parent,dict) else None
        if isinstance(parent,dict):parent.pop(components[-1],None)
    return digest(capture_json_bytes(value))

def has_unresolved_references(note,data,header):
    audio=note.get('audio') or {};web=note.get('web_page') or {}
    if note.get('attachments') or note.get('audio_url') or audio.get('play_url') or web.get('url'):return True
    if any(header.get(key,b'') not in (b'',b'""',b"''",b'[]',b'null',b'~') for key in ('attachments','audio_url','web_url')):return True
    # References are not fetched or inferred complete by a text-only proposal.
    return bool(re.search(rb'https?://|!\[|\]\(|<(?:img\b|audio\b|video\b|source\b)|(?m:^\s*\[[^\]]+\]:)',data+b'\n'+capture_json_bytes(note),re.IGNORECASE))

def _slot_text(slot):
    return slot.get('value') if slot['encoding']=='json-string' else slot['bytes'].decode('utf-8')

def _slot_receipt(key,slot,owners):
    return {'region':key,'start':slot['start'],'length':slot['length'],'sha256':digest(slot['bytes']),
            'encoding':slot['encoding'],'fields':sorted(owners)}

def ownership_shape_hash(spans):
    try:shape=sorted([{'region':span['region'],'encoding':span['encoding'],'fields':span['fields']} for span in spans],key=lambda row:row['region'])
    except (TypeError,KeyError):stop('legacy_state_untrusted')
    return digest(capture_json_bytes(shape))

def propose_ownership(census,record,old,*,observation_sha256,_prevalidated_census=False,census_sha256=None):
    """Build a review proposal from a validated immutable capture and old bytes."""
    census_hash=census_sha256 if _prevalidated_census else digest(capture_json_bytes(census))
    if not isinstance(census_hash,str) or not re.fullmatch('[a-f0-9]{64}',census_hash):stop('legacy_state_untrusted')
    ident=verify_legacy_observation(census,census_hash,record,observation_sha256,_prevalidated_census=_prevalidated_census)
    if record['disposition']!='observed_unreconciled':stop('source_capture_unresolved')
    if digest(old)!=record['mirror']['sha256']:stop('legacy_baseline_changed')
    note={**census['summaries'][ident],**observation_detail(record)}
    fields,slots,header=regions(old)
    if fields.get('note_id')!=ident:stop('legacy_identity_changed')
    values=text_values(note)
    if has_unresolved_references(note,old,header):stop('legacy_references_unresolved')
    present={key:text for key,(kind,text) in values.items() if kind=='text' and text}
    if not present:stop('source_text_empty')
    allowed=ROLE_FIELDS
    owners={key:[path for path,text in present.items() if _slot_text(slot)==text and (key not in allowed or path in allowed[key])] for key,slot in slots.items()}
    covered=set(path for paths in owners.values() for path in paths)
    if covered!=set(present):stop('source_text_unmapped')
    # Nonempty named legacy source fields must not become silently stale while
    # the body advances. Other unknown/user regions remain byte-preserved.
    for name in ('audio_transcript','web_excerpt'):
        if fields.get(name) and not owners.get('frontmatter:'+name):stop('legacy_named_text_unmapped')
    owned=[_slot_receipt(key,slots[key],paths) for key,paths in owners.items() if paths]
    owned.sort(key=lambda x:x['start'])
    return {'contract':CONTRACT,'origin':'legacy-reconciliation-proposal','local_proposal_only':True,
        'blocked_from_apply':True,'mirror_adopted':False,'identity':ident,'filePath':record['mirror']['filePath'],
        'capture_evidence':{'census_sha256':census_hash,'observation_sha256':observation_sha256,
                            'source_commit':census['source_commit'],'old_git_blob':record['mirror']['gitBlob']},
        'mirror_sha256':digest(old),'source_sha256':digest(capture_json_bytes(note)),
        'source_version':raw_source_version(note.get('updated_at')),'metadata_sha256':metadata_hash(note),
        'source_text':text_receipts(values),'owned_spans':owned,'ownership_shape_sha256':ownership_shape_hash(owned),'history_appended':False}

def propose_update(state,baseline,current,source,*,state_sha256):
    """Three-way proposal: captured baseline, current local bytes, fresh source.

    Exact structural slots permit edits in unowned regions without discarding
    them. state_sha256 must be obtained from separately reviewed immutable
    state, never freshly computed from untrusted supplied state. Any changed
    owned slot, new unmapped field, alias split, deletion or
    non-text metadata change is an explicit conflict. No file/state is written.
    """
    if digest(capture_json_bytes(state))!=state_sha256:stop('legacy_state_changed')
    if not isinstance(state,dict) or state.get('contract')!=CONTRACT or state.get('origin')!='legacy-reconciliation-proposal' or state.get('local_proposal_only') is not True or state.get('blocked_from_apply') is not True:stop('legacy_state_untrusted')
    expected_keys={'contract','origin','local_proposal_only','blocked_from_apply','identity','filePath','capture_evidence','mirror_sha256','source_sha256','source_version','metadata_sha256','source_text','owned_spans','history_appended','mirror_adopted','ownership_shape_sha256'}
    if set(state)!=expected_keys or state['history_appended'] is not False or state['mirror_adopted'] is not False or not isinstance(state['source_text'],dict) or set(state['source_text'])!=set(SOURCE_TEXT_FIELDS) or not isinstance(state['owned_spans'],list) or not 1<=len(state['owned_spans'])<=4:stop('legacy_state_untrusted')
    evidence=state['capture_evidence']
    if not isinstance(evidence,dict) or set(evidence)!={'census_sha256','observation_sha256','source_commit','old_git_blob'}:stop('legacy_state_untrusted')
    for key,length in (('census_sha256',64),('observation_sha256',64),('source_commit',40),('old_git_blob',40)):
        if not isinstance(evidence[key],str) or not re.fullmatch('[a-f0-9]{'+str(length)+'}',evidence[key]):stop('legacy_state_untrusted')
    path=state['filePath']
    if not isinstance(path,str) or not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or str(PurePosixPath(path))!=path or '\\' in path or any(ord(char)<32 for char in path):stop('legacy_state_untrusted')
    token=state['source_version']
    if not isinstance(token,dict) or set(token)!={'type','value'} or token!=raw_source_version(token['value']):stop('legacy_state_untrusted')
    if not isinstance(state['identity'],str) or not re.fullmatch('[0-9]{1,64}',state['identity']):stop('legacy_state_untrusted')
    for name in ('mirror_sha256','source_sha256','metadata_sha256','ownership_shape_sha256'):
        if not isinstance(state[name],str) or not re.fullmatch('[a-f0-9]{64}',state[name]):stop('legacy_state_untrusted')
    for receipt in state['source_text'].values():
        if not isinstance(receipt,dict) or set(receipt)!={'kind','sha256','bytes'} or type(receipt['bytes']) is not int or not 0<=receipt['bytes']<=MAX_NOTE:stop('legacy_state_untrusted')
        if receipt['kind']=='text':
            if not isinstance(receipt['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',receipt['sha256']):stop('legacy_state_untrusted')
        elif receipt['kind'] not in ('absent','null') or receipt['sha256'] is not None or receipt['bytes']!=0:stop('legacy_state_untrusted')
    if digest(baseline)!=state['mirror_sha256']:stop('legacy_baseline_changed')
    fields,old_slots,_=regions(baseline);current_fields,slots,header=regions(current)
    ident=state['identity']
    if fields.get('note_id')!=ident or current_fields.get('note_id')!=ident or note_identity(source)!=ident:stop('legacy_identity_changed')
    values=text_values(source)
    if has_unresolved_references(source,current,header):stop('legacy_references_unresolved')
    if metadata_hash(source)!=state['metadata_sha256']:stop('source_metadata_changed')
    version=raw_source_version(source.get('updated_at'));receipts=text_receipts(values)
    # The approved ownership identity is stable. Later equal text must never
    # gain a new owner/alias merely by coincidence. The external trusted state
    # hash binds this immutable shape digest and all evolving span receipts.
    if ownership_shape_hash(state['owned_spans'])!=state['ownership_shape_sha256']:stop('legacy_state_untrusted')
    expected_regions={span['region'] for span in state['owned_spans']}
    for name in ('audio_transcript','web_excerpt'):
        if fields.get(name) and 'frontmatter:'+name not in expected_regions:stop('legacy_state_untrusted')
        if current_fields.get(name) and 'frontmatter:'+name not in expected_regions:stop('legacy_named_text_unmapped')
    required={key for key,receipt in state['source_text'].items() if receipt['kind']=='text' and receipt['bytes']>0}
    if {key for span in state['owned_spans'] for key in span['fields']}!=required:stop('legacy_state_untrusted')
    owned_fields=set();patches=[];previous_end=-1;seen_regions=set()
    for span in state['owned_spans']:
        if not isinstance(span,dict) or set(span)!={'region','start','length','sha256','encoding','fields'} or type(span['start']) is not int or type(span['length']) is not int or not isinstance(span['fields'],list) or any(not isinstance(path,str) for path in span['fields']) or span['fields']!=sorted(set(span['fields'])):stop('legacy_state_untrusted')
        key=span['region'];old=old_slots.get(key);slot=slots.get(key)
        if key in seen_regions or not old or not slot or span['start']<previous_end:stop('legacy_state_untrusted')
        if span!={**_slot_receipt(key,old,span['fields'])}:stop('legacy_state_untrusted')
        previous_end=span['start']+span['length'];seen_regions.add(key)
        if slot['bytes']!=old['bytes']:stop('legacy_owned_text_edited')
        if not span['fields'] or any(path not in SOURCE_TEXT_FIELDS for path in span['fields']):stop('legacy_state_untrusted')
        expected=_slot_text(old)
        if any(state['source_text'][path]!={'kind':'text','sha256':digest(expected.encode()),'bytes':len(expected.encode())} for path in span['fields']):stop('legacy_state_untrusted')
        owned_fields.update(span['fields'])
        texts=[values[path][1] for path in span['fields']]
        if any(values[path][0]!='text' or not values[path][1] for path in span['fields']):stop('source_text_missing')
        if len(set(texts))!=1:stop('source_alias_diverged')
        replacement=slot['bytes'] if texts[0]==expected else (texts[0].encode() if slot['encoding']=='utf8' else json.dumps(texts[0],ensure_ascii=False).encode())
        patches.append((slot['start'],slot['length'],replacement,key,span['fields']))
    if not patches:stop('legacy_state_untrusted')
    for path,receipt in receipts.items():
        if path not in owned_fields and receipt!=state['source_text'][path]:stop('source_text_unmapped')
    result=current
    for start,length,replacement,_,_ in sorted(patches,reverse=True):result=result[:start]+replacement+result[start+length:]
    _,new_slots,_=regions(result)
    proposed=deepcopy(state);proposed.update(mirror_sha256=digest(result),source_sha256=digest(capture_json_bytes(source)),
        source_version=version,source_text=receipts,owned_spans=sorted([_slot_receipt(key,new_slots[key],owners) for _,_,_,key,owners in patches],key=lambda x:x['start']))
    return {'classification':'unchanged' if state['source_sha256']==proposed['source_sha256'] else ('source_version_only' if result==current else 'source_updated'),
            'bytes':result,'proposed_state':proposed,'unowned_local_edits_preserved':current!=baseline,
            'blocked_from_apply':True,'mirror_adopted':False,'history_appended':False}
