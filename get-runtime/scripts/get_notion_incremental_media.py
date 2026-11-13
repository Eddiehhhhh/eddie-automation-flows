"""Bind Get inline images to private Git blobs and verified Notion uploads.

This module never follows source URLs. It consumes only the canonical URL-free
registry manifest and delegates native uploads to the repository's existing
checkpointed `notion_private_assets` implementation.
"""
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
from pathlib import PurePosixPath
import re
import sys

_repo_scripts = Path(__file__).resolve().parents[2] / 'scripts'
if str(_repo_scripts) not in sys.path:
    sys.path.insert(0, str(_repo_scripts))

import notion_private_assets as private_assets

CONTRACT = 'get-notion-incremental-media-1'
STATE_DIR = '.sync/get-notion-incremental/media'
MAX_IMAGES = 8
MAX_ASSET_BYTES = 16 * 1024 * 1024
ALLOWED_MIME = frozenset(('image/png', 'image/jpeg', 'image/webp', 'image/gif'))


def stop(code):
    raise ValueError(code) from None


def _caption(payload):
    rows = payload.get('caption', []) if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 100:
        stop('existing_content_differs')
    defaults = {'bold': False, 'italic': False, 'strikethrough': False,
                'underline': False, 'code': False, 'color': 'default'}
    result = []
    for row in rows:
        text = row.get('text') if isinstance(row, dict) else None
        if (not isinstance(row, dict) or row.get('type') != 'text'
                or not isinstance(text, dict) or not isinstance(text.get('content'), str)):
            stop('existing_content_differs')
        link = text.get('link')
        if link is not None and (not isinstance(link, dict) or not isinstance(link.get('url'), str)):
            stop('existing_content_differs')
        annotations = row.get('annotations') or {}
        if not isinstance(annotations, dict) or set(annotations) - set(defaults):
            stop('existing_content_differs')
        value = {'type': 'text', 'text': {'content': text['content'], 'link': link},
                 'annotations': {**defaults, **annotations}}
        if (type(value['annotations']['color']) is not str
                or any(type(value['annotations'][key]) is not bool for key in defaults if key != 'color')):
            stop('existing_content_differs')
        result.append(value)
    return result


def _source_body(data, ident):
    try:
        text = data.decode('utf-8')
        if not text.startswith('---\n') or '\n---\n' not in text:
            stop('source_content_held')
        head, body = text[4:].split('\n---\n', 1)
        fields = {}
        for line in head.splitlines():
            key, sep, value = line.partition(': ')
            if not sep or key in fields:
                stop('source_content_held')
            fields[key] = value
        import json
        if json.loads(fields['note_id']) != ident:
            stop('source_content_held')
        return body
    except ValueError:
        stop('source_content_held')
    except Exception:
        stop('source_content_held')


def _manifest(row):
    metadata = row.get('source_metadata')
    value = metadata.get('inline_images') if isinstance(metadata, dict) else None
    if (not isinstance(value, dict) or set(value) != {'contract', 'images'}
            or value.get('contract') != 'get-inline-image-manifest-1'
            or not isinstance(value.get('images'), list)
            or len(value['images']) > MAX_IMAGES):
        stop('source_content_held')
    return value['images']


def source_assets(root, commit, ident, data, path, registry_row):
    """Return exact immutable assets after matching source refs and Git blobs."""
    try:
        from get_notion_credential_projection import render_body
        body = render_body(data, ident, _source_body(data, ident))
        refs = []
        for match in re.finditer(r'!\[([^\]\r\n]*)\]\(([^\s()<>]+)\)', body):
            reference = match.group(2)
            if (reference.startswith('/') or '\\' in reference or ':' in reference
                    or '%' in reference or '\0' in reference):
                stop('source_content_held')
            parts = list(PurePosixPath(path).parent.parts)
            for part in reference.split('/'):
                if part == '..':
                    if len(parts) <= 2:
                        stop('source_content_held')
                    parts.pop()
                elif part in ('', '.'):
                    stop('source_content_held')
                else:
                    parts.append(part)
            refs.append({'reference': reference, 'asset_path': '/'.join(parts)})
        if body.count('![') != len(refs):
            stop('source_content_held')
        metadata = registry_row.get('source_metadata')
        manifest = metadata.get('inline_images') if isinstance(metadata, dict) else None
        if not refs and manifest is None:
            return []
        rows = _manifest(registry_row)
        if len(refs) != len(rows):
            stop('source_content_held')
        assets = []
        by_path = {}
        for ref, row in zip(refs, rows):
            expected = {'reference_sha256', 'source_sha256', 'asset_path', 'asset_blob',
                        'asset_sha256', 'asset_bytes', 'asset_mime'}
            if not isinstance(row, dict) or set(row) != expected:
                stop('source_content_held')
            for key in ('reference_sha256', 'source_sha256', 'asset_sha256'):
                if not isinstance(row[key], str) or re.fullmatch('[a-f0-9]{64}', row[key]) is None:
                    stop('source_content_held')
            if (row['asset_path'] != ref['asset_path']
                    or row['asset_mime'] not in ALLOWED_MIME
                    or type(row['asset_bytes']) is not int
                    or not 1 <= row['asset_bytes'] <= MAX_ASSET_BYTES
                    or not isinstance(row['asset_path'], str)
                    or not row['asset_path'].startswith('Raw/03 Get/_assets/images/')
                    or '\\' in row['asset_path'] or '..' in PurePosixPath(row['asset_path']).parts
                    or PurePosixPath(row['asset_path']).is_absolute()
                    or PurePosixPath(row['asset_path']).as_posix() != row['asset_path']
                    or re.fullmatch('[a-f0-9]{40}', row['asset_blob']) is None):
                stop('source_content_held')
            suffix = {'image/png': '.png', 'image/jpeg': '.jpg',
                      'image/webp': '.webp', 'image/gif': '.gif'}[row['asset_mime']]
            if not row['asset_path'].endswith('/' + row['asset_sha256'] + suffix):
                stop('source_content_held')
            prior = by_path.get(row['asset_path'])
            physical = (row['asset_blob'], row['asset_sha256'], row['asset_bytes'], row['asset_mime'])
            if prior is not None:
                if prior != physical:
                    stop('source_content_held')
                asset = next(item['asset'] for item in assets if item['path'] == row['asset_path'])
            else:
                asset = private_assets.prepare(root, commit, row['asset_path'])
                if (asset.path != row['asset_path'] or asset.blob != row['asset_blob']
                        or asset.sha256 != row['asset_sha256'] or len(asset.data) != row['asset_bytes']
                        or asset.content_type != row['asset_mime'] or asset.block_type != 'image'):
                    stop('source_content_held')
                by_path[row['asset_path']] = physical
            assets.append({'reference': ref['reference'], 'reference_sha256': row['reference_sha256'],
                           'source_sha256': row['source_sha256'], 'path': asset.path,
                           'asset': asset})
        return assets
    except (private_assets.AssetHeld, ValueError):
        stop('source_content_held')
    except Exception:
        stop('source_content_held')


def media_state_key(asset):
    return asset.blob + ':' + asset.sha256


def receipts_from_main(root, ident, data):
    path = STATE_DIR + '/' + ident + '.json'
    import subprocess
    try:
        tree = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-z', 'HEAD', '--', path],
                                       stderr=subprocess.DEVNULL)
        entries = [row for row in tree.split(b'\0') if row]
        if not entries:
            return {}
        if len(entries) != 1:
            stop('source_content_held')
        meta, found = entries[0].split(b'\t', 1)
        mode, kind, _ = meta.decode().split()
        if found.decode() != path or mode != '100644' or kind != 'blob':
            stop('source_content_held')
        raw = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:' + path],
                                      stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        stop('source_content_held')
    except ValueError:
        raise
    except Exception:
        stop('source_content_held')
    import json
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'contract', 'note_id', 'source_sha256', 'assets'}
                or value['contract'] != CONTRACT or value['note_id'] != ident
                or value['source_sha256'] != hashlib.sha256(data).hexdigest()
                or not isinstance(value['assets'], dict) or len(value['assets']) > MAX_IMAGES):
            stop('source_content_held')
        return value['assets']
    except ValueError:
        stop('source_content_held')
    except Exception:
        stop('source_content_held')


def upload_handles(assets, receipts):
    """Validate durable receipts and return the wire guard's upload map."""
    handles = {}
    for item in assets:
        asset = item['asset']
        receipt = receipts.get(media_state_key(asset))
        if not isinstance(receipt, dict) or receipt.get('phase') != 'verified':
            stop('missing_asset')
        source_commit = receipt.get('source_commit')
        sources = receipt.get('sources')
        if (not isinstance(source_commit, str) or re.fullmatch('[a-f0-9]{40}', source_commit) is None
                or not isinstance(sources, dict) or sources.get(source_commit, {}).get(asset.path) != asset.blob
                or receipt.get('source_path') != asset.path
                or receipt.get('source_blob') != asset.blob
                or receipt.get('sha256') != asset.sha256
                or receipt.get('size') != len(asset.data)
                or receipt.get('content_type') != asset.content_type
                or not isinstance(receipt.get('upload_id'), str)):
            stop('missing_asset')
        upload_id = receipt['upload_id']
        value = {'sha256': asset.sha256, 'byte_count': len(asset.data), 'mime': asset.content_type}
        if upload_id in handles and handles[upload_id] != value:
            stop('missing_asset')
        handles[upload_id] = value
    return handles


def match_existing_images(snapshot, assets):
    """Reuse only exact native bytes, one block per source image occurrence."""
    if (not isinstance(snapshot, dict) or snapshot.get('complete') is not True
            or snapshot.get('native_blocks_available') is not True):
        stop('missing_asset')
    rows = snapshot.get('recursive_blocks')
    if not isinstance(rows, list):
        stop('missing_asset')
    images = [row for row in rows if isinstance(row, dict) and row.get('type') == 'image']
    if not images:
        return None
    if len(images) != len(assets):
        stop('existing_content_differs')
    observed = []
    for row in images:
        try:
            identity = private_assets.canonical_native_file(
                {'type': 'image', 'image': row.get('payload')})
        except private_assets.AssetHeld:
            stop('existing_content_differs')
        observed.append(identity)
    used = set()
    bindings = []
    for item in assets:
        asset = item['asset']
        found = [(index, images[index]) for index, identity in enumerate(observed)
                 if index not in used and identity.get('type') == 'image'
                 and identity.get('sha256') == asset.sha256
                 and identity.get('size') == len(asset.data)]
        if len(found) != 1:
            stop('existing_content_differs')
        index, row = found[0]
        used.add(index)
        caption = _caption(row.get('payload', {}))
        bindings.append({'block_id': row.get('id'), 'asset': asset,
                         'caption': caption})
    if len(used) != len(images):
        stop('existing_content_differs')
    return bindings


def render_assets(assets, receipts=None, *, preview=False):
    """Build sink inputs only from verified private blobs and upload receipts."""
    receipts = {} if receipts is None else receipts
    tree = {}
    by_reference = {}
    for item in assets:
        asset = item['asset']
        receipt = receipts.get(media_state_key(asset))
        if preview:
            upload_id = '0' * 32
        else:
            if not isinstance(receipt, dict) or receipt.get('phase') != 'verified':
                stop('missing_asset')
            if (receipt.get('source_path') != asset.path or receipt.get('source_blob') != asset.blob
                    or receipt.get('sha256') != asset.sha256 or receipt.get('size') != len(asset.data)
                    or receipt.get('content_type') != asset.content_type):
                stop('missing_asset')
            upload_id = receipt.get('upload_id')
            if not isinstance(upload_id, str) or re.fullmatch('[0-9a-f-]{32,36}', upload_id) is None:
                stop('missing_asset')
        row = {'readback_verified': True, 'path': asset.path, 'bytes': asset.data,
               'git_blob': asset.blob, 'sha256': asset.sha256, 'notion_file_id': upload_id}
        if item['reference'] in by_reference and by_reference[item['reference']] != row:
            stop('source_content_held')
        by_reference[item['reference']] = row
        tree[asset.path] = asset.blob
    return by_reference, tree


def image_count(blocks):
    return sum((1 if row['type'] == 'image' else 0) + image_count(row['children'])
               for row in blocks)


def bind_existing_blocks(blocks, bindings):
    """Bind renderer image positions to already-present, hash-matched blocks."""
    cursor = 0

    def walk(items):
        nonlocal cursor
        for row in items:
            if row['type'] == 'image':
                if cursor >= len(bindings):
                    stop('existing_content_differs')
                binding = bindings[cursor]
                asset = binding['asset']
                block_id = binding.get('block_id')
                if not isinstance(block_id, str) or re.fullmatch('[0-9a-f]{32}', block_id) is None:
                    stop('existing_content_differs')
                actual_caption = binding.get('caption', [])
                if (not isinstance(actual_caption, list)
                        or actual_caption != row['payload'].get('caption', [])):
                    stop('existing_content_differs')
                row['payload'] = {'kind': 'notion_existing_media_block',
                                  'block_id': block_id, 'sha256': asset.sha256,
                                  'byte_count': len(asset.data), 'mime': asset.content_type,
                                  'caption': actual_caption}
                cursor += 1
            walk(row['children'])
    walk(blocks)
    if cursor != len(bindings):
        stop('existing_content_differs')
    return blocks


def bind_existing_snapshot(snapshot, bindings):
    value = deepcopy(snapshot)
    by_id = {row['block_id']: row for row in bindings}
    seen = set()
    for row in value.get('recursive_blocks', []):
        binding = by_id.get(row.get('id')) if row.get('type') == 'image' else None
        if binding is None:
            continue
        asset = binding['asset']
        row['payload'] = {'kind': 'notion_existing_media_block',
                          'block_id': row['id'], 'sha256': asset.sha256,
                          'byte_count': len(asset.data), 'mime': asset.content_type,
                          'caption': binding.get('caption', [])}
        seen.add(row['id'])
    if seen != set(by_id):
        stop('existing_content_differs')
    return value


def bind_uploaded_snapshot(snapshot, assets, receipts):
    """Download the created native blocks and bind each to exact Git bytes."""
    from copy import deepcopy
    value = deepcopy(snapshot)
    expected = Counter(item['asset'].sha256 for item in assets)
    observed = Counter()
    by_sha = {item['asset'].sha256: item['asset'] for item in assets}
    for row in value.get('recursive_blocks', []):
        if row.get('type') != 'image':
            continue
        prior = row.get('payload')
        if prior.get('kind') == 'notion_incremental_uploaded_media':
            asset = by_sha.get(prior.get('sha256'))
            receipt = receipts.get(media_state_key(asset)) if asset is not None else None
            if (value.get('media_readback_verified') is not True or receipt is None
                    or prior.get('upload_id') != receipt.get('upload_id')
                    or prior.get('byte_count') != len(asset.data)
                    or prior.get('mime') != asset.content_type):
                stop('missing_asset')
            observed[asset.sha256] += 1
            continue
        candidates = [asset for asset in by_sha.values()
                      if asset.block_type == 'image']
        match = None
        for asset in candidates:
            try:
                private_assets.verify_uploaded_block(
                    asset, {'type': 'image', 'image': row.get('payload')})
            except private_assets.AssetHeld:
                continue
            match = asset
            break
        if match is None:
            stop('missing_asset')
        receipt = receipts.get(media_state_key(match))
        if not isinstance(receipt, dict) or not isinstance(receipt.get('upload_id'), str):
            stop('missing_asset')
        observed[match.sha256] += 1
        if observed[match.sha256] > expected[match.sha256]:
            stop('missing_asset')
        row['payload'] = {'kind': 'notion_incremental_uploaded_media',
                          'upload_id': receipt['upload_id'], 'sha256': match.sha256,
                          'byte_count': len(match.data), 'mime': match.content_type,
                          'caption': _caption(row.get('payload', {}))}
    if observed != expected:
        stop('missing_asset')
    value['media_readback_verified'] = True
    return value


def snapshot_adapter(assets, receipts):
    def adapt(snapshot, ident):
        if any(row.get('type') == 'image' for row in snapshot.get('recursive_blocks', [])):
            return bind_uploaded_snapshot(snapshot, assets, receipts)
        return snapshot
    return adapt


def publish_missing(assets, token, receipts, checkpoint):
    """Publish/recover one file at a time; unknown writes stay held by the helper."""
    state = deepcopy(receipts)
    unique = {}
    for item in assets:
        asset = item['asset']
        unique[media_state_key(asset)] = asset
    for key, asset in unique.items():
        prior = state.get(key)
        result = private_assets.publish(
            asset, token,
            checkpoint=lambda value, key=key: checkpoint({**state, key: value}),
            prior_receipt=prior)
        if result.get('status') != 'ready' or not isinstance(result.get('receipt'), dict):
            stop('missing_asset')
        state[key] = result['receipt']
    return state


def bind_uploaded_blocks(blocks, assets, receipts):
    """Replace renderer file identities with byte-bound upload semantics."""
    handles = upload_handles(assets, receipts)
    by_reference = {}
    for item in assets:
        asset = item['asset']
        receipt = receipts[media_state_key(asset)]
        by_reference[item['reference']] = (asset, receipt)
    rows = []

    def walk(items):
        for row in items:
            if row['type'] == 'image':
                payload = row['payload']
                selected = next((pair for pair in by_reference.values()
                                 if pair[0].sha256 == payload.get('sha256')), None)
                if selected is None:
                    stop('missing_asset')
                asset, receipt = selected
                upload_id = receipt.get('upload_id')
                if handles.get(upload_id) != {'sha256': asset.sha256,
                        'byte_count': len(asset.data), 'mime': asset.content_type}:
                    stop('missing_asset')
                row['payload'] = {'kind': 'notion_incremental_uploaded_media',
                                  'upload_id': upload_id, 'sha256': asset.sha256,
                                  'byte_count': len(asset.data), 'mime': asset.content_type,
                                  'caption': payload.get('caption', [])}
                rows.append(asset.sha256)
            walk(row['children'])
    walk(blocks)
    if Counter(rows) != Counter(item['asset'].sha256 for item in assets):
        # Repeated references may share one asset but still require one image block per reference.
        expected = Counter(item['asset'].sha256 for item in assets)
        if Counter(rows) != expected:
            stop('missing_asset')
    return handles
