#!/usr/bin/env python3
"""Private pinned path-map audit and one real binary-note roundtrip, not full sync."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import unicodedata
import urllib.parse

import source_sync_inventory as inventory
import source_sync_sample as sample
import sync_getnote_to_raw as sync
from source_sync_assets import AssetStore, AUDIO_EXTENSIONS
from source_sync_store import IdentityIndex, attachment_manifest, json_text, write_if_changed
from source_sync_urls import read_previous, stabilize_attachment_urls
from verify_shadow_repeat import snapshot

MIB = 1024 * 1024
MAX_TREE_ENTRIES = 6000
MAX_PRIOR_BYTES = 256 * MIB
MAX_DETAIL_CALLS = 4  # up to three candidates, then one repeat
MAX_ASSETS = 2
COUNTS = ('prior_manifest_notes', 'prior_api_notes', 'prior_non_api_notes_preserved',
          'prior_missing_id_notes_preserved', 'tree_markdown', 'unindexed_markdown',
          'mapped_api_paths', 'detail_calls', 'api_response_bytes', 'downloaded_assets',
          'downloaded_bytes', 'attachment_urls_reused')
FLAGS = ('path_map_ok', 'unselected_placeholders_unchanged', 'sample_real_header_checked',
         'sample_hashes_and_mtimes_equal', 'sample_binary_readback_ok')
ERRORS = ('private_snapshot_invalid', 'path_map_failed', 'binary_candidate_unavailable',
          'detail_failed', 'binary_roundtrip_failed')


def sha(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{40}', value):
        raise ValueError('invalid object')
    return value


def tree_path(value):
    if (type(value) is not str or not value or len(value.encode()) > 1024 or
            '\\' in value or any(ord(c) < 32 for c in value) or
            any(p in ('', '.', '..') for p in value.split('/')) or PurePosixPath(value).is_absolute()):
        raise ValueError('invalid path')
    return value


def validate_tree(value):
    if not isinstance(value, dict) or value.get('truncated') is not False:
        raise ValueError('incomplete tree')
    entries = value.get('tree')
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_TREE_ENTRIES:
        raise ValueError('invalid tree')
    blobs, aliases, total = {}, set(), 0
    for item in entries:
        path = tree_path(item['path'])
        alias = unicodedata.normalize('NFC', path).casefold()
        if alias in aliases:
            raise ValueError('path alias')
        aliases.add(alias)
        sha(item['sha'])
        if item.get('type') == 'tree' and item.get('mode') == '040000':
            continue
        size = item.get('size')
        if item.get('type') != 'blob' or item.get('mode') not in ('100644', '100755') or type(size) is not int or size < 0:
            raise ValueError('unsafe tree entry')
        total += size
        blobs[path] = item
    if total > MAX_PRIOR_BYTES:
        raise ValueError('prior size limit')
    return blobs


def private_snapshot():
    token = os.environ.get('PRIVATE_VAULT_TOKEN')
    if not token:
        raise ValueError('private snapshot unavailable')
    def read(endpoint, limit=MIB, raw=False):
        return json.loads(sample.github_read(endpoint, token, limit, raw=raw))
    if read('', 65536).get('private') is not True:
        raise ValueError('private snapshot unavailable')
    ref = read('/git/ref/heads/main', 65536).get('object', {})
    if ref.get('type') != 'commit':
        raise ValueError('private snapshot unavailable')
    commit = sha(ref.get('sha'))
    tree_sha = sha(read('/git/commits/' + commit, 65536)['tree']['sha'])
    for part in ('Raw', '03 Get'):
        tree = read('/git/trees/' + tree_sha)
        if tree.get('truncated') is not False:
            raise ValueError('incomplete parent tree')
        matches = [i for i in tree['tree'] if i.get('path') == part and i.get('type') == 'tree' and i.get('mode') == '040000']
        if len(matches) != 1:
            raise ValueError('missing source tree')
        tree_sha = sha(matches[0]['sha'])
    blobs = validate_tree(read('/git/trees/' + tree_sha + '?recursive=1', 2 * MIB))
    endpoint = '/contents/' + urllib.parse.quote(sample.MANIFEST_PATH, safe='/') + '?ref=' + commit
    manifest = read(endpoint, sample.MANIFEST_MAX_BYTES, raw=True)
    mapping, meta = inventory.validate_manifest(manifest)
    # Commit and object IDs remain in memory; no raw snapshot content is published.
    return token, commit, blobs, manifest, mapping, meta


def audit_map(root, blobs, manifest, mapping):
    markdown = {p for p in blobs if p.endswith('.md')}
    by_path = {n['path']: n for n in manifest['notes']}
    if not set(by_path) <= markdown:
        raise ValueError('missing indexed path')
    # Explicit placeholders test resolver mappings, NOT private body/header parity.
    for relative in sorted(markdown):
        ident = by_path.get(relative, {}).get('note_id')
        text = '---\nsource_system: getnote\n'
        if ident:
            text += 'note_id: ' + json.dumps(ident) + '\n'
        write_if_changed(root / relative, text + '---\nMAPPING AUDIT PLACEHOLDER\n')
    index = IdentityIndex(root, 'getnote', 'note_id')
    before = snapshot(root)
    for ident, relative in mapping.items():
        if index.path(ident, root / 'changed-title-and-date.md') != root / relative:
            raise ValueError('old mapping changed')
    # A new identity cannot occupy a reserved old path, including unindexed notes.
    occupied = root / sorted(markdown)[0]
    candidate = index.path('mapping-audit-new-identity', occupied)
    if candidate == occupied or candidate.exists() or snapshot(root) != before:
        raise ValueError('mapping audit changed source')
    return {'tree_markdown': len(markdown), 'unindexed_markdown': len(markdown - set(by_path)),
            'mapped_api_paths': len(mapping), 'path_map_ok': True}


def supported_binary_note(note):
    if any(note.get(k) for k in ('images', 'files', 'resources')):
        return False  # Do not pretend unsupported descriptor collections were tested.
    items = note.get('attachments')
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ASSETS:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        url = item.get('original_url') or item.get('url')
        if not isinstance(url, str) or not url:
            return False
        kind = item.get('type') or 'file'
        if kind not in ('image', 'file') or Path(urllib.parse.urlsplit(url).path).suffix.lower() in AUDIO_EXTENSIONS:
            return False
    return True


def detail(ident, key, client, budget, progress):
    if progress['detail_calls'] >= MAX_DETAIL_CALLS:
        raise ValueError('detail cap')
    progress['detail_calls'] += 1
    value = sync.api_get(sync.DETAIL_PATH, {'id': ident, 'image_quality': 'original'}, key, client,
                         max_response_bytes=2 * MIB, response_budget=budget)
    value = value.get('data', value)
    note = value.get('note', value)
    if sample.detail_id(note) != ident or not isinstance(note.get('content'), str):
        raise ValueError('invalid detail')
    return note


def roundtrip(root, relative, first, second_read, fetch=None):
    ident = sample.detail_id(first)
    index = IdentityIndex(root, 'getnote', 'note_id')
    if index.paths.get(ident) != root / relative:
        raise ValueError('real header mismatch')
    old = snapshot(root)
    cache_id = hashlib.sha256(ident.encode()).hexdigest()
    cache = root / '03 State' / 'raw-cache' / (cache_id + '.json')
    manifest = root / '03 State' / 'attachments' / (cache_id + '.json')
    downloaded, byte_count, reused = 0, 0, 0
    for pass_number in range(2):
        note = first if pass_number == 0 else second_read()
        if sample.detail_id(note) != ident or not supported_binary_note(note):
            raise ValueError('binary note changed')
        options = {} if fetch is None else {'fetch': fetch}
        store = AssetStore(root, max_file=2 * MIB, max_run=4 * MIB, max_assets=MAX_ASSETS, **options)
        assets = store.materialize(note)  # Fresh URLs before reuse; no note/cache writes on failure.
        if len(assets) != len(note['attachments']) or any(a.get('status') != 'downloaded' for a in assets):
            raise ValueError('incomplete assets')
        downloaded += len(assets)
        byte_count += store.bytes
        for asset in assets:
            data = (root / asset['path']).read_bytes()
            if len(data) != asset['bytes'] or hashlib.sha256(data).hexdigest() != asset['sha256']:
                raise ValueError('binary readback mismatch')
        note, assets, count = stabilize_attachment_urls(note, assets, read_previous(cache, root), read_previous(manifest, root))
        reused += count
        path = index.path(ident, root / 'changed-title-and-date.md')
        original_raw_dir = sync.RAW_DIR
        try:
            sync.RAW_DIR = root
            rendered = sync.render_note(note, assets, path)
        finally:
            sync.RAW_DIR = original_raw_dir
        if any(urllib.parse.quote(os.path.relpath(root / a['path'], path.parent)) not in rendered for a in assets):
            raise ValueError('local link missing')
        write_if_changed(path, rendered)
        write_if_changed(cache, json_text(note))
        write_if_changed(manifest, json_text(dict(attachment_manifest(note), assets=assets)))
        current = snapshot(root)
        if any(current.get(p) != v for p, v in old.items() if p != relative):
            raise ValueError('unselected path changed')
        if pass_number == 0:
            previous = current
    if current != previous:
        raise ValueError('repeat mismatch')
    return {'downloaded_assets': downloaded, 'downloaded_bytes': byte_count,
            'attachment_urls_reused': reused, 'unselected_placeholders_unchanged': True,
            'sample_real_header_checked': True, 'sample_hashes_and_mtimes_equal': True,
            'sample_binary_readback_ok': True}


def safe_report(value):
    result = {'scope': 'migration_map_and_binary_sample', 'status': 'failed',
              'source_complete': False, 'published': False, 'full_existing_body_replay': False}
    if not isinstance(value, dict):
        return result
    for key in COUNTS:
        count = value.get(key)
        if type(count) is int and 0 <= count <= MAX_PRIOR_BYTES:
            result[key] = count
    for key in FLAGS:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    valid_counts = (all(k in result for k in COUNTS) and result['detail_calls'] <= MAX_DETAIL_CALLS
                    and result['tree_markdown'] <= MAX_TREE_ENTRIES
                    and result['api_response_bytes'] <= 12 * MIB
                    and 2 <= result['downloaded_assets'] <= 2 * MAX_ASSETS
                    and 0 < result['downloaded_bytes'] <= 8 * MIB)
    if value.get('status') == 'mapping_and_binary_ok' and valid_counts and all(result.get(k) is True for k in FLAGS):
        result['status'] = 'mapping_and_binary_ok'
    if value.get('error_code') in ERRORS:
        result['error_code'] = value['error_code']
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish-report', type=Path)
    args = parser.parse_args()
    if args.publish_report:
        try:
            value = json.loads(args.publish_report.read_text())
        except (OSError, ValueError):
            value = None
        print(json.dumps(safe_report(value), indent=2))
        return 0
    report, stage = {'detail_calls': 0}, 'private_snapshot_invalid'
    try:
        token, commit, blobs, manifest, mapping, meta = private_snapshot()
        report.update(meta)
        with tempfile.TemporaryDirectory(prefix='private-get-mapping-') as temp:
            root = Path(temp) / 'get'
            root.mkdir(mode=0o700)
            stage = 'path_map_failed'
            report.update(audit_map(root, blobs, manifest, mapping))
            key, client = sync.load_credentials()
            budget = inventory.ResponseBudget(12 * MIB)
            candidates = sorted((n for n in manifest['notes'] if n.get('note_type') == 'img_text'
                                 and n.get('note_id') in mapping), key=lambda n: n['path'])[:3]
            first = selected = None
            for candidate in candidates:
                stage = 'detail_failed'
                note = detail(candidate['note_id'], key, client, budget, report)
                if supported_binary_note(note):
                    first, selected = note, candidate
                    break
            stage = 'binary_candidate_unavailable'
            if selected is None:
                raise ValueError('no bounded candidate')
            relative = selected['path']
            endpoint = '/contents/' + urllib.parse.quote('Raw/03 Get/' + relative, safe='/') + '?ref=' + commit
            data = sample.github_read(endpoint, token, 2 * MIB, raw=True)
            expected = blobs[relative]
            object_hash = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if len(data) != expected['size'] or object_hash != expected['sha']:
                raise ValueError('pinned body mismatch')
            (root / relative).write_bytes(data)
            stage = 'binary_roundtrip_failed'
            report.update(roundtrip(root, relative, first,
                                    lambda: detail(selected['note_id'], key, client, budget, report)))
            report['api_response_bytes'] = budget.used
            report['status'] = 'mapping_and_binary_ok'
    except (Exception, SystemExit):
        report['error_code'] = stage
    safe = safe_report(report)
    print(json.dumps(safe))
    return 0 if safe['status'] == 'mapping_and_binary_ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
