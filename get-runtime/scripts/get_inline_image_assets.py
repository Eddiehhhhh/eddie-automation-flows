"""Conservative extraction of explicit Markdown image embeds from Get text.

The caller must fetch each returned URL with source_sync_assets.AssetStore.
URLs are returned only to the caller and are never included in the manifest.
Unsupported image syntax fails closed instead of silently dropping media.
"""
from __future__ import annotations

import hashlib
import os
import re
import zlib
from pathlib import PurePosixPath
from urllib.parse import quote

MAX_IMAGES_PER_NOTE = 8
MAX_URL_BYTES = 8192


class ImageReferenceError(ValueError):
    pass


def _fail():
    raise ImageReferenceError('inline_image_reference_unsupported') from None


def validate_image_bytes(data, mime):
    """Reject HTML/garbage responses that merely claim an image Content-Type."""
    if not isinstance(data,bytes) or not data:
        _fail()
    png_ok=False
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        offset=8;chunks=[];valid=True
        while offset+12<=len(data):
            size=int.from_bytes(data[offset:offset+4],'big');kind=data[offset+4:offset+8]
            end=offset+12+size
            if end>len(data):valid=False;break
            payload=data[offset+8:offset+8+size];crc=int.from_bytes(data[offset+8+size:end],'big')
            if zlib.crc32(kind+payload)&0xffffffff!=crc:valid=False;break
            chunks.append((kind,payload));offset=end
            if kind==b'IEND':break
        png_ok=(valid and offset==len(data) and chunks and chunks[0][0]==b'IHDR'
                and len(chunks[0][1])==13 and any(kind==b'IDAT' for kind,_ in chunks)
                and chunks[-1]==(b'IEND',b''))
    valid = {
        'image/png': png_ok,
        'image/jpeg': data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9') and len(data)>=6,
        'image/gif': data.startswith((b'GIF87a',b'GIF89a')) and data.endswith(b';') and len(data)>=14,
        'image/webp': len(data)>=16 and data.startswith(b'RIFF') and data[8:12]==b'WEBP' and int.from_bytes(data[4:8],'little')+8==len(data),
    }
    if mime not in valid or not valid[mime]:
        _fail()
    return data


def extract(body: str):
    """Return parsed explicit Markdown image refs with exact source spans.

    Supported grammar is intentionally narrow: `![alt](https://URL)` where
    the URL contains no whitespace, angle brackets, or parentheses. This is
    sufficient for the verified Get embed shape and avoids guessing at nested
    Markdown/link syntax. All other `![` forms are held for review.
    """
    if not isinstance(body, str):
        _fail()
    rows = []
    pattern = re.compile(r'!\[([^\]\r\n]{0,512})\]\((https://[^\s()<>]{1,8192})\)')
    for match in pattern.finditer(body):
        url = match.group(2)
        if len(url.encode('utf-8')) > MAX_URL_BYTES:
            _fail()
        rows.append({'start': match.start(), 'end': match.end(), 'alt': match.group(1),
                     'url': url, 'reference_sha256': hashlib.sha256(url.encode()).hexdigest(),
                     'source_sha256': hashlib.sha256(match.group(0).encode()).hexdigest()})
    # Do not treat non-Markdown images or malformed image-like syntax as text.
    if (body.count('![') != len(rows) or len(rows) > MAX_IMAGES_PER_NOTE
            or re.search(r'<\s*img\b', body, re.I)):
        _fail()
    return rows


def rewrite(body: str, references, replacements):
    """Replace only parsed image tokens; every other source byte stays intact."""
    if not isinstance(references, list) or len(references) != len(replacements):
        _fail()
    if not references:
        return body
    pieces = []
    cursor = 0
    for row, target in zip(references, replacements):
        if (not isinstance(row, dict) or type(row.get('start')) is not int
                or type(row.get('end')) is not int or row['start'] < cursor
                or row['end'] <= row['start'] or row['end'] > len(body)
                or not isinstance(target, str) or not target or '\n' in target or '\r' in target):
            _fail()
        original = body[row['start']:row['end']]
        if hashlib.sha256(original.encode()).hexdigest() != row.get('source_sha256'):
            _fail()
        pieces.extend((body[cursor:row['start']], '![' + row['alt'] + '](' + target + ')'))
        cursor = row['end']
    pieces.append(body[cursor:])
    return ''.join(pieces)


def public_manifest(references, assets):
    """Return non-secret bindings; never persist signed source URLs."""
    if not isinstance(references, list) or not isinstance(assets, list) or len(references) != len(assets):
        _fail()
    rows = []
    for ref, asset in zip(references, assets):
        if (not isinstance(asset, dict) or not isinstance(asset.get('path'), str)
                or not re.fullmatch(r'_assets/images/[0-9a-f]{64}\.(?:png|jpg|webp|gif)', asset['path'])
                or not re.fullmatch(r'[0-9a-f]{64}', asset.get('sha256', ''))
                or type(asset.get('bytes')) is not int or asset['bytes'] <= 0
                or not isinstance(asset.get('mime'), str) or not asset['mime'].startswith('image/')):
            _fail()
        rows.append({'reference_sha256': ref['reference_sha256'],
                     'source_sha256': ref['source_sha256'], 'asset_path': asset['path'],
                     'asset_sha256': asset['sha256'], 'asset_bytes': asset['bytes'],
                     'asset_mime': asset['mime']})
    return {'contract': 'get-inline-image-manifest-1', 'images': rows}


def materialize_body(body: str, note_path: str, asset_store):
    """Download parsed embeds through the existing bounded AssetStore.

    Returns rewritten body and a URL-free manifest. The caller must place all
    returned files and metadata in the same canonical candidate transaction.
    """
    references = extract(body)
    if not references:
        return body, [], {'contract': 'get-inline-image-manifest-1', 'images': []}
    attachments = [{'type': 'image', 'url': row['url']} for row in references]
    assets = asset_store.materialize({'attachments': attachments})
    if not isinstance(assets, list) or len(assets) != len(references):
        _fail()
    replacements = []
    for asset in assets:
        if not isinstance(asset, dict) or asset.get('status') != 'downloaded':
            _fail()
        rel = os.path.relpath(asset['path'], PurePosixPath(note_path).parent)
        replacements.append(quote(rel))
    return rewrite(body, references, replacements), assets, public_manifest(references, assets)
