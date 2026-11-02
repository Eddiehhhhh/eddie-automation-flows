"""Exact content-addressed attachments referenced by generated note suffixes."""
import hashlib
import posixpath
import re
from urllib.parse import unquote

MARKER = '<!-- get-cloud-attachments -->'
MAX_ASSET = 25 * 1024 * 1024
PATH = re.compile(r'_assets/(?:images|files)/([0-9a-f]{64})\.(?:png|jpg|webp|gif|pdf|bin)\Z')


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
        lines.append(('!' if path.startswith('_assets/images/') else '')+'[attachment]('+relative+')')
    return original + ('\n'.join(lines)+'\n').encode()


class SourceWithAssets:
    """Delegate the existing source protocol; download only into disposable storage."""
    def __init__(self, source, directory, fetch=None):
        from source_sync_assets import AssetStore
        self.source = source
        self.directory = directory
        self.store = AssetStore(directory, **({'fetch':fetch} if fetch else {}))

    def __getattr__(self, name):
        return getattr(self.source, name)

    def attachment_files(self, note, path):
        from copy import deepcopy
        from pathlib import Path
        value = deepcopy(note)
        inline = re.findall(r'!\[[^\]]*\]\((https://[^\s)]+)\)', value.get('content') or '')
        value['attachments'] = list(value.get('attachments') or []) + [
            {'type':'image','url':url} for url in dict.fromkeys(inline)]
        rows = self.store.materialize(value)
        if any(row.get('status') != 'downloaded' and
               not (row.get('kind') == 'link' and row.get('status') == 'not-downloaded')
               for row in rows):
            raise ValueError('asset_materialization_required')
        result = {row['path']:(Path(self.directory)/row['path']).read_bytes()
                  for row in rows if row['status']=='downloaded'}
        for asset, data in result.items():
            verify(asset, data)
        return result


def enrich(registry, changes, details, loader):
    """Return candidate copies only after all attachment downloads succeed."""
    from copy import deepcopy
    result = deepcopy(registry)
    outputs = dict(changes)
    for identity, row in result['newById'].items():
        path = row['filePath']
        if path not in changes or identity not in details:
            continue
        binaries = loader(details[identity], path)
        if not binaries:
            continue
        enriched = append(path, changes[path], binaries)
        row['mirror_sha256'] = hashlib.sha256(enriched).hexdigest()
        row['baseline_blob'] = hashlib.sha1(b'blob '+str(len(enriched)).encode()+b'\0'+enriched).hexdigest()
        outputs[path] = enriched
        for asset, data in binaries.items():
            if asset in outputs and outputs[asset] != data:
                raise ValueError('generated_asset_collision')
            outputs[asset] = data
    return result, outputs
