#!/usr/bin/env python3
"""Read-only preflight for one pinned Get image -> Notion upload."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse

import notion_private_assets as assets
import get_notion_live_transport as notion_transport
import get_notion_native as notion_native
import get_notion_incremental_worker as notion_worker
import get_notion_sink_plan as notion_sink

PROOF_ROOT = '.sync/get-notion-asset-probes/'
PROOF_CONTRACT = 'getnote-private-content-image-probe-1'
CONFIG_PATH = '.sync/get-notion-incremental/config.json'
CONFIG_CONTRACT = 'get-notion-incremental-config-1'
MAX_MANIFEST = 1024 * 1024
MAX_NOTE = 2 * 1024 * 1024


def fail(code: str) -> None:
    raise ValueError(code) from None


def git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(['git', '-C', str(root), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                check=True, timeout=60)
        return result.stdout
    except Exception:
        fail('private_source_unreadable')


def committed_json(root: Path, commit: str, path: str, budget: int) -> dict:
    raw = git(root, 'show', f'{commit}:{path}')
    if len(raw) > budget:
        fail('private_source_over_budget')
    try:
        value = json.loads(raw)
    except Exception:
        fail('private_source_invalid')
    if not isinstance(value, dict):
        fail('private_source_invalid')
    return value


def proof_path(root: Path, commit: str) -> str:
    rows = [line for line in git(root, 'ls-tree', '-r', '--name-only', commit,
                                 '--', PROOF_ROOT).decode().splitlines()
            if line.startswith(PROOF_ROOT) and line.endswith('.json')]
    if len(rows) != 1:
        fail('proof_not_unique')
    return rows[0]


def verify_proof(root: Path, commit: str) -> tuple[str, dict, object, bytes, str]:
    path = proof_path(root, commit)
    proof = committed_json(root, commit, path, MAX_MANIFEST)
    if (proof.get('contract') != PROOF_CONTRACT or proof.get('ref') != 'codex/get-asset-proof-42289447756-1'
            or proof.get('parent') != git(root, 'rev-parse', f'{commit}^').decode().strip()
            or proof.get('source_field') != 'content_embed'
            or not isinstance(proof.get('source_id'), str)
            or not re.fullmatch('[0-9]{19}', proof['source_id'])):
        fail('proof_binding_invalid')
    note_path, manifest_path = proof.get('note_path'), proof.get('manifest_path')
    if (not isinstance(note_path, str)
            or not note_path.startswith(('Raw/03 Get/01 Notes/', 'Raw/03 Get/99 录音卡/'))
            or not note_path.endswith('.md') or not isinstance(manifest_path, str)
            or not manifest_path.startswith('Raw/03 Get/03 State/attachments/')
            or not manifest_path.endswith('.json')):
        fail('proof_paths_invalid')
    note = git(root, 'show', f'{commit}:{note_path}')
    manifest_raw = git(root, 'show', f'{commit}:{manifest_path}')
    if (len(note) > MAX_NOTE or len(manifest_raw) > MAX_MANIFEST
            or hashlib.sha256(note).hexdigest() != proof.get('note_sha256')
            or hashlib.sha256(manifest_raw).hexdigest() != proof.get('manifest_sha256')):
        fail('proof_file_hash_mismatch')
    if not re.search(rb'^note_id:\s*["\']?' + proof['source_id'].encode() + rb'["\']?\s*$', note, re.M):
        fail('proof_note_identity_mismatch')
    try:
        manifest = json.loads(manifest_raw)
    except Exception:
        fail('manifest_invalid')
    asset_rows = proof.get('assets')
    embeds = manifest.get('content_image_embeds') if isinstance(manifest, dict) else None
    if (not isinstance(asset_rows, list) or len(asset_rows) != 1
            or not isinstance(embeds, list) or len(embeds) != 1):
        fail('single_image_required')
    row, embed = asset_rows[0], embeds[0]
    if (not isinstance(row, dict) or not isinstance(embed, dict)
            or embed.get('source_field') != 'content'
            or embed.get('source_content_sha256') != proof.get('source_content_sha256')
            or not isinstance(embed.get('asset_path'), str)
            or 'Raw/03 Get/' + embed['asset_path'] != row.get('path')
            or embed.get('asset_sha256') != row.get('sha256')
            or embed.get('url_sha256') != row.get('source_url_sha256')
            or type(row.get('bytes')) is not int or not 0 < row['bytes'] <= assets.SINGLE_PART_BYTES
            or not re.fullmatch('[0-9a-f]{64}', str(row.get('sha256', '')))
            or not re.fullmatch('[0-9a-f]{64}', str(row.get('source_url_sha256', '')))):
        fail('manifest_proof_mismatch')
    if (not row['path'].startswith('Raw/03 Get/_assets/images/')
            or hashlib.sha256(str(embed.get('url', '')).encode()).hexdigest() != row['source_url_sha256']):
        fail('manifest_source_mismatch')
    relative_asset = os.path.relpath(row['path'], Path(note_path).parent.as_posix()).replace(os.sep, '/')
    encoded_asset = urllib.parse.quote(relative_asset, safe='/._-')
    if not re.search(rb'^!\[[^\]]*\]\(' + re.escape(encoded_asset.encode()) + rb'\)\s*$', note, re.M):
        fail('note_asset_reference_mismatch')
    if (not isinstance(manifest.get('assets'), list) or len(manifest['assets']) != 1
            or manifest['assets'][0].get('path') != embed['asset_path']
            or manifest['assets'][0].get('sha256') != row['sha256']
            or manifest['assets'][0].get('bytes') != row['bytes']):
        fail('manifest_asset_mismatch')
    data_asset = assets.prepare(root, commit, row['path'])
    if (data_asset.sha256 != row['sha256'] or len(data_asset.data) != row['bytes']
            or data_asset.content_type != row.get('mime') or data_asset.block_type != 'image'):
        fail('asset_blob_mismatch')
    if hashlib.sha1(b'blob ' + str(len(data_asset.data)).encode() + b'\0' + data_asset.data).hexdigest() != row.get('path_blob'):
        # Older proof contracts do not carry Git blob IDs. `prepare` already
        # verifies the object identity from the exact commit, so absence is okay.
        if row.get('path_blob') is not None:
            fail('asset_blob_mismatch')
    config = committed_json(root, commit, CONFIG_PATH, MAX_MANIFEST)
    if config.get('contract') != CONFIG_CONTRACT:
        fail('notion_config_invalid')
    try:
        source_id = notion_native.uuid(config.get('data_source_id'))
        if notion_native.sha(source_id) != notion_native.DATA_SOURCE_PIN:
            fail('notion_target_not_pinned')
    except Exception:
        fail('notion_target_not_pinned')
    return proof['source_id'], config, data_asset, note, note_path


def compare_existing_page(root: Path, commit: str, ident: str, note: bytes,
                          note_path: str, schema: dict, snapshot: dict) -> tuple[bool, bool, int, int, int, int, str | None]:
    """Compare all non-image blocks and projected fields without changing Notion."""
    raw_text = note.decode('utf-8')
    proof = committed_json(root, commit, proof_path(root, commit), MAX_MANIFEST)
    image_reference = urllib.parse.quote(
        os.path.relpath(proof['assets'][0]['path'], Path(note_path).parent.as_posix()).replace(os.sep, '/'),
        safe='/._-')
    image_line = re.compile(r'^!\[[^\]]*\]\(' + re.escape(image_reference) + r'\)\s*$', re.M)
    text_without_image, removed = image_line.subn('', raw_text)
    if removed < 1:
        fail('note_asset_reference_mismatch')
    registry = committed_json(root, commit, 'Raw/03 Get/03 State/canonical-registry.json', 4 * 1024 * 1024)
    row = (registry.get('newById') or {}).get(ident)
    if not isinstance(row, dict) or row.get('filePath') != note_path.removeprefix('Raw/03 Get/'):
        fail('source_registry_mismatch')
    props, expected_blocks = notion_worker.desired(text_without_image.encode('utf-8'), ident,
                                                    note_path, row, schema)
    filtered = deepcopy(snapshot)
    rows = filtered.get('recursive_blocks')
    if not isinstance(rows, list):
        fail('notion_snapshot_incomplete')
    image_ids = {value['id'] for value in rows if value.get('type') == 'image'}
    image_rows = [value for value in rows if value.get('type') == 'image']
    for value in rows:
        if not isinstance(value, dict):
            fail('notion_snapshot_incomplete')
        value['children_ids'] = [child for child in value.get('children_ids', []) if child not in image_ids]
        value['has_children'] = bool(value['children_ids'])
    filtered['recursive_blocks'] = [value for value in rows if value.get('type') != 'image']

    actual_semantics = notion_sink.snapshot_tree(filtered)[3]
    content_matches = actual_semantics == notion_sink.comparison_semantics(expected_blocks)
    properties_match = True
    actual_properties = snapshot.get('properties_raw') or {}
    for name, value in props.items():
        if name not in actual_properties:
            properties_match = False
            break
        if name in notion_worker.properties.FIELDS or name in notion_worker.RECORDING_FIELDS or name == '笔记 ID':
            if notion_worker.properties.value(actual_properties[name]) != notion_worker.properties.value(value):
                properties_match = False
                break
        elif [x.get('plain_text') for x in actual_properties[name].get('title', [])] != [x['text']['content'] for x in value['title']]:
            properties_match = False
            break
    target = notion_worker.target_sha(filtered, props.keys()) if properties_match else None
    source_image_matches = 0
    image_unreadable = 0
    # Re-read the one pinned source asset from Git for byte comparison.
    source_asset_row = proof['assets'][0]
    source_asset = assets.prepare(root, commit, source_asset_row['path'])
    for image in image_rows:
        try:
            identity = assets.canonical_native_file({'type': 'image', 'image': image.get('payload') or {}})
            if identity['sha256'] == source_asset.sha256 and identity['size'] == len(source_asset.data):
                source_image_matches += 1
        except assets.AssetHeld:
            image_unreadable += 1
    return content_matches, properties_match, removed, len(image_rows), source_image_matches, image_unreadable, target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--proof-commit', required=True)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.proof_commit):
        fail('proof_commit_invalid')
    if git(args.vault, 'rev-parse', 'HEAD').decode().strip() != args.proof_commit:
        fail('proof_commit_mismatch')
    ident, config, asset, note, note_path = verify_proof(args.vault, args.proof_commit)
    api_key = os.environ.get('NOTION_API_KEY', '')
    if not api_key or any(ord(c) <= 32 for c in api_key):
        fail('notion_credentials_missing')
    me = assets._request(api_key, 'GET', '/users/me')
    limit = (me.get('bot') or {}).get('workspace_limits', {}).get('max_file_upload_size_in_bytes')
    if type(limit) is not int or limit <= 0:
        fail('notion_workspace_limit_unknown')
    if len(asset.data) > limit:
        fail('notion_workspace_limit_exceeded')
    request = notion_transport.Transport(api_key)
    api = notion_transport.API(request, config['data_source_id'], [ident])
    api.schema()
    matches = api.matches(ident)
    if len(matches) > 1:
        fail('notion_duplicate_source_id')
    existing_images = matching_images = unreadable_images = 0
    content_matches = properties_match = False
    owner_status = 'not_checked'
    if matches:
        snapshot = api.read_note(ident)
        content_matches, properties_match, source_image_occurrences, existing_images, matching_images, unreadable_images, target_sha = \
            compare_existing_page(args.vault, args.proof_commit, ident, note, note_path,
                                  api.schema(), snapshot)
        main_before = git(args.vault, 'ls-remote', 'origin', 'refs/heads/main').decode().split()
        if len(main_before) != 2 or not re.fullmatch('[0-9a-f]{40}', main_before[0]):
            fail('private_main_unavailable')
        git(args.vault, 'fetch', '--no-tags', 'origin', 'refs/heads/main')
        main_commit = git(args.vault, 'rev-parse', 'FETCH_HEAD').decode().strip()
        main_after = git(args.vault, 'ls-remote', 'origin', 'refs/heads/main').decode().split()
        if main_after != main_before:
            fail('private_main_changed_during_read')
        owner_path = f'.sync/get-notion-incremental/owned/{ident}.json'
        owned_exists = subprocess.run(['git', '-C', str(args.vault), 'cat-file', '-e',
                                       f'{main_commit}:{owner_path}'], stderr=subprocess.DEVNULL).returncode == 0
        if not owned_exists:
            owner_status = 'absent'
        else:
            owned = committed_json(args.vault, main_commit, owner_path, MAX_MANIFEST)
            source_note_exists = subprocess.run(['git', '-C', str(args.vault), 'cat-file', '-e',
                                                 f'{main_commit}:{note_path}'], stderr=subprocess.DEVNULL).returncode == 0
            source_note = git(args.vault, 'show', f'{main_commit}:{note_path}') if source_note_exists else b''
            owner_status = 'exact' if (
                existing_images == 0 and source_note_exists
                and owned.get('note_id') == ident and owned.get('page_id') == snapshot.get('page_id')
                and owned.get('source_sha256') == hashlib.sha256(source_note).hexdigest()
                and target_sha is not None and owned.get('target_sha256') == target_sha
            ) else 'mismatch'
    # Existing pages are never modified by this probe.
    ready_to_adopt = bool(matches) and content_matches and properties_match and existing_images == 0
    complete_image_match = bool(matches) and content_matches and properties_match and existing_images == 1 and matching_images == 1
    result = {'status': 'dry_run_ready' if not matches else 'existing_page_complete' if complete_image_match else 'existing_page_reviewed',
              'notes': 1, 'image_assets': 1, 'image_bytes': len(asset.data),
              'notion_workspace_limit': limit, 'existing_pages': len(matches),
              'source_image_occurrences': source_image_occurrences if matches else 0,
              'existing_image_blocks': existing_images, 'matching_source_images': matching_images,
              'unreadable_image_blocks': unreadable_images,
              'source_text_matches': content_matches, 'source_properties_match': properties_match,
              'owned_receipt': owner_status, 'safe_for_adopted_append': ready_to_adopt and owner_status == 'absent',
              'writes': 0, 'main_targeted': False}
    print(json.dumps(result, sort_keys=True))
    return 0 if not matches else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValueError as exc:
        code = str(exc)
        if not re.fullmatch('[a-z0-9_]{1,64}', code):
            code = 'probe_failed'
        print(json.dumps({'status': 'held', 'stage': code, 'writes': 0,
                          'main_targeted': False}, sort_keys=True))
        raise SystemExit(2) from None
    except Exception:
        print(json.dumps({'status': 'held', 'stage': 'probe_failed', 'writes': 0,
                          'main_targeted': False}, sort_keys=True))
        raise SystemExit(2) from None
