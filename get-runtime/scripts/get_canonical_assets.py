"""Exact content-addressed attachments referenced by generated note suffixes."""
import hashlib
import json
import posixpath
import re
from urllib.parse import unquote, urlsplit

MARKER = '<!-- get-cloud-attachments -->'
MAX_ASSET = 25 * 1024 * 1024
MEDIA_TYPES = {'audio','local_audio','recorder_audio','recorder_flash_audio','meeting'}
PATH = re.compile(r'_assets/(?:images|files)/([0-9a-f]{64})\.(?:png|jpg|webp|gif|pdf|bin|json|pending\.json|media\.json)\Z')


def references(notes):
    result = set()
    for path, data in notes.items():
        if not path.endswith('.md'):
            continue
        text = data.decode('utf-8')
        if MARKER not in text:
            continue
        block = text.split(MARKER, 1)[1]
        for target in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)\)', block):
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(target)))
            if not PATH.fullmatch(resolved):
                raise ValueError('invalid_generated_asset_reference')
            result.add(resolved)
    return result


def verify(path, data):
    match = PATH.fullmatch(path)
    if not match or not isinstance(data, bytes) or not 0 < len(data) <= MAX_ASSET:
        raise ValueError('invalid_generated_asset')
    if hashlib.sha256(data).hexdigest() != match.group(1):
        raise ValueError('generated_asset_hash_mismatch')


def append(note_path, original, assets):
    if MARKER.encode() in original:
        raise ValueError('generated_asset_block_already_present')
    lines = ['', MARKER, '## 本地附件', '']
    for path, data in sorted(assets.items()):
        verify(path, data)
        relative = posixpath.relpath(path, posixpath.dirname(note_path))
        if path.endswith('.pending.json'):
            lines.extend(['音视频附件尚待补全，原始链接与笔记来源已保留。','[待补全附件清单]('+relative+')'])
        else:
            lines.append(('!' if path.startswith('_assets/images/') else '')+'[attachment]('+relative+')')
    return original + ('\n'.join(lines)+'\n').encode()


class SourceWithAssets:
    """Delegate the existing source protocol; download only into disposable storage."""
    def __init__(self, source, directory, fetch=None, download_media=True):
        from source_sync_assets import AssetStore
        self.source = source
        self.directory = directory
        self.pending_media = {}
        self.store = AssetStore(directory, allow_media=download_media, **({'fetch':fetch} if fetch else {}))

    def __getattr__(self, name):
        return getattr(self.source, name)

    def attachment_files(self, note, path):
        from copy import deepcopy
        from pathlib import Path
        value = deepcopy(note)
        inline = re.findall(r'!\[[^\]]*\]\((https://[^\s)]+)\)', value.get('content') or '')
        original_attachments = list(value.get('attachments') or [])
        audio = value.get('audio') or {}
        audio_url = audio.get('play_url') or value.get('audio_url')
        descriptors = [('attachments',i) for i in range(len(original_attachments))]
        if audio_url and audio_url not in {a.get('original_url') or a.get('url') for a in original_attachments}:
            original_attachments.append({'type':'audio','url':audio_url})
            descriptors.append(('audio.play_url' if audio.get('play_url') else 'audio_url',None))
        descriptors.extend(('content.inline_images',i) for i in range(len(dict.fromkeys(inline))))
        value['attachments'] = original_attachments + [
            {'type':'image','url':url} for url in dict.fromkeys(inline)]
        if any(a.get('type') in ('audio','video') for a in value['attachments']):
            from get_v7_compat import note_identity
            try: note_identity(note)
            except ValueError: raise ValueError('asset_materialization_required') from None
        rows = self.store.materialize(value)
        from source_sync_assets import AUDIO_EXTENSIONS
        pending = []
        for index, row in enumerate(rows):
            status, kind = row.get('status'), row.get('kind')
            if status == 'downloaded' or (kind == 'link' and status == 'not-downloaded'):
                continue
            if status != 'not-downloaded' or not (kind in ('audio','video') or
                    Path(urlsplit(row.get('url') or '').path).suffix.lower() in AUDIO_EXTENSIONS | {'.mp4','.webm','.mov','.m3u8'}):
                raise ValueError('asset_materialization_required')
            field,position = descriptors[index]
            pending.append({'source_field':field,
                            'source_index':position,
                            'kind':kind,'status':'pending_download','reason':row.get('reason','media_download_pending')})
        result = {row['path']:(Path(self.directory)/row['path']).read_bytes()
                  for row in rows if row['status']=='downloaded'}
        media_rows = [(descriptors[i],row) for i,row in enumerate(rows) if row.get('kind') in ('audio','video') or Path(urlsplit(row.get('url') or '').path).suffix.lower() in AUDIO_EXTENSIONS | {'.mp4','.webm','.mov','.m3u8'}]
        if note.get('note_type') in MEDIA_TYPES and not media_rows:
            pending.append({'source_field':'audio.play_url','source_index':None,'kind':'audio','status':'pending_download','reason':'source_media_url_unavailable'})
        if pending or media_rows:
            from get_v7_compat import note_identity
            try:ident = note_identity(note)
            except ValueError:raise ValueError('asset_materialization_required') from None
            if not re.fullmatch('[0-9]{1,64}',ident):raise ValueError('asset_materialization_required')
            # Signed URLs stay only in source metadata; receipts use stable fields.
            if pending:
                record = {'contract':'get-attachment-pending-1','source_system':'getnote',
                          'source_note_id':ident,'source_updated_at':note.get('updated_at'),
                          'status':'pending_download','resolve':'refresh_source_note_before_download','items':pending}
                data = (json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
                result['_assets/files/'+hashlib.sha256(data).hexdigest()+'.pending.json'] = data
                self.pending_media[ident] = len(pending)
            completed=[(descriptors[i],row) for i,row in enumerate(rows) if row['status']=='downloaded']
            if completed:
                record = {'contract':'get-media-complete-1','status':'partial' if pending else 'complete','source_note_id':ident,'source_updated_at':note.get('updated_at'),'items':[{'source_field':field,'source_index':position,'path':row['path'],'sha256':row['sha256'],'bytes':row['bytes'],'mime':row['mime']} for (field,position),row in completed]}
                data = (json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
                result['_assets/files/'+hashlib.sha256(data).hexdigest()+'.media.json'] = data
        for asset, data in result.items():
            verify(asset, data)
        return result

    def restore_media(self, note, note_path, previous, read_asset):
        """Reuse only hashed prior bytes from the same source version and field."""
        from get_v7_compat import note_identity
        from get_mirror_incremental_replay import raw_source_version
        from source_sync_assets import write_binary
        from pathlib import Path
        ident=note_identity(note)
        for path in references({note_path:previous}):
            if not path.endswith('.media.json'): continue
            data=read_asset(path);verify(path,data);record=json.loads(data)
            if record.get('contract')!='get-media-complete-1' or record.get('source_note_id')!=ident:
                raise ValueError('media_retry_receipt_identity')
            if raw_source_version(record.get('source_updated_at'))!=raw_source_version(note.get('updated_at')): continue
            if not isinstance(record.get('items'),list) or len(record['items'])>128:
                raise ValueError('media_retry_receipt_shape')
            for item in record['items']:
                if not isinstance(item,dict) or not PATH.fullmatch(item.get('path','')):
                    raise ValueError('media_retry_asset_path')
                field,position=item['source_field'],item['source_index']
                if field=='attachments':
                    rows=note.get('attachments') or []
                    if type(position) is not int or not 0<=position<len(rows): raise ValueError('media_retry_field')
                    url=rows[position].get('original_url') or rows[position].get('url')
                elif field=='audio.play_url':url=(note.get('audio') or {}).get('play_url')
                elif field=='audio_url':url=note.get('audio_url')
                elif field=='content.inline_images':
                    rows=list(dict.fromkeys(re.findall(r'!\[[^\]]*\]\((https://[^\s)]+)\)',note.get('content') or '')))
                    if type(position) is not int or not 0<=position<len(rows): raise ValueError('media_retry_field')
                    url=rows[position]
                else:raise ValueError('media_retry_field')
                if not url: continue
                payload=read_asset(item['path']);verify(item['path'],payload)
                if hashlib.sha256(payload).hexdigest()!=item['sha256'] or len(payload)!=item['bytes'] or not isinstance(item['mime'],str) or not item['mime']:
                    raise ValueError('media_retry_asset_mismatch')
                write_binary(self.directory,Path(item['path']),payload)
                self.store.memo[url]={'url':url,'status':'downloaded','path':item['path'],'sha256':item['sha256'],'bytes':item['bytes'],'mime':item['mime']}


def historical_media(data):
    if not data.startswith(b'---\n'): return False
    try:
        header = {k:json.loads(v) for k,v in (line.split(': ',1) for line in data.decode().split('---',2)[1].strip().splitlines())}
    except (ValueError,UnicodeError): return False
    return (header.get('note_type') in MEDIA_TYPES or bool(header.get('audio_url')) or
            any(isinstance(u,str) and urlsplit(u).path.lower().endswith(('.mp3','.m4a','.wav','.aac','.ogg','.flac','.opus','.mp4','.webm','.mov','.m3u8')) for u in header.get('attachments',[])))


def retry_ids(registry, current, limit=2):
    ids=[]
    for identity,row in registry['newById'].items():
        data=current.get(row['filePath'],b'')
        refs=references({row['filePath']:data})
        if any(p.endswith('.pending.json') for p in refs) or (historical_media(data) and not any(p.endswith('.media.json') for p in refs)):
            ids.append(identity)
    ids.sort()
    cursor = registry.get('media_retry_cursor')
    after = [i for i in ids if cursor is None or i > cursor]
    return (after + [i for i in ids if i not in after])[:limit]


def enrich(registry, changes, details, loader, current=None, retry=(), read_asset=None):
    """Return candidate copies with verified binaries and explicit media debt."""
    from copy import deepcopy
    result = deepcopy(registry)
    outputs = dict(changes)
    # Spend the bounded media attempts on rotating debt before newly selected notes.
    for identity in dict.fromkeys((*retry,*result['newById'])):
        row = result['newById'][identity]
        if identity in result.get('conflictsById',{}): continue
        path = row['filePath']
        if identity not in details or details[identity] is None:
            continue
        original = changes.get(path)
        preserved_assets={}
        if original is None:
            if identity not in retry or current is None: continue
            existing = current.get(path,b'')
            if hashlib.sha256(existing).hexdigest() != row['mirror_sha256']:
                raise ValueError('media_retry_baseline_changed')
            from get_mirror_incremental_replay import raw_source_version
            if raw_source_version(details[identity]['updated_at']) != row['source_version']:
                raise ValueError('media_retry_source_version_changed')
            original = existing.split(b'\n<!-- get-cloud-attachments -->',1)[0]
            if read_asset is not None:
                for saved in references({path:existing}):
                    if saved.endswith(('.pending.json','.media.json')): continue
                    data=read_asset(saved);verify(saved,data)
                    preserved_assets[saved]=data
                    if sum(map(len,preserved_assets.values()))>64*1024*1024:
                        raise ValueError('media_retry_restore_budget')
        owner=getattr(loader,'__self__',None)
        if current is not None and read_asset is not None and hasattr(owner,'restore_media'):
            prior=current.get(path,b'')
            if prior and hashlib.sha256(prior).hexdigest()==registry['newById'][identity]['mirror_sha256']:
                owner.restore_media(details[identity],path,prior,read_asset)
        binaries = loader(details[identity], path)
        if not binaries:
            continue
        binaries={**preserved_assets,**binaries}
        enriched = append(path, original, binaries)
        row['mirror_sha256'] = hashlib.sha256(enriched).hexdigest()
        row['baseline_blob'] = hashlib.sha1(b'blob '+str(len(enriched)).encode()+b'\0'+enriched).hexdigest()
        outputs[path] = enriched
        for asset, data in binaries.items():
            if asset in outputs and outputs[asset] != data:
                raise ValueError('generated_asset_collision')
            outputs[asset] = data
    return result, outputs
