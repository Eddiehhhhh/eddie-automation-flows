#!/usr/bin/env python3
"""Read-only preflight for one pinned Get image -> Notion upload."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse

import notion_private_assets as assets
import get_notion_live_transport as notion_transport
import get_notion_native as notion_native

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
        value = json.loads(raw, object_pairs_hook=unique_pairs)
    except Exception:
        fail('private_source_invalid')
    if not isinstance(value, dict):
        fail('private_source_invalid')
    return value


def unique_pairs(rows: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in rows:
        if key in value:
            fail('private_source_duplicate_key')
        value[key] = item
    return value


def proof_path(root: Path, commit: str) -> str:
    rows = [line for line in git(root, 'ls-tree', '-r', '--name-only', commit,
                                 '--', PROOF_ROOT).decode().splitlines()
            if line.startswith(PROOF_ROOT) and line.endswith('.json')]
    if len(rows) != 1:
        fail('proof_not_unique')
    return rows[0]


def verify_proof(root: Path, commit: str) -> tuple[str, dict, object]:
    path = proof_path(root, commit)
    proof = committed_json(root, commit, path, MAX_MANIFEST)
    if (proof.get('contract') != PROOF_CONTRACT or proof.get('ref') != 'codex/get-asset-proof-42289447756-1'
            or proof.get('parent') != git(root, 'rev-parse', f'{commit}^').decode().strip()
            or proof.get('source_field') != 'content_embed'
            or not isinstance(proof.get('source_id'), str)
            or not re.fullmatch('[0-9]{19}', proof['source_id'])):
        fail('proof_binding_invalid')
    if Path(path).stem != proof['source_id'] or not re.fullmatch('[0-9a-f]{64}', str(proof.get('source_content_sha256', ''))):
        fail('proof_binding_invalid')
    note_path, manifest_path = proof.get('note_path'), proof.get('manifest_path')
    if (not isinstance(note_path, str) or not note_path.startswith('Raw/03 Get/01 Notes/')
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
        manifest = json.loads(manifest_raw, object_pairs_hook=unique_pairs)
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
            or embed.get('asset_path') != row.get('path')
            or embed.get('asset_sha256') != row.get('sha256')
            or embed.get('url_sha256') != row.get('source_url_sha256')
            or type(row.get('bytes')) is not int or not 0 < row['bytes'] <= assets.SINGLE_PART_BYTES
            or not re.fullmatch('[0-9a-f]{64}', str(row.get('sha256', '')))
            or not re.fullmatch('[0-9a-f]{64}', str(row.get('source_url_sha256', '')))):
        fail('manifest_proof_mismatch')
    if (not row['path'].startswith('Raw/03 Get/_assets/images/')
            or not isinstance(embed.get('url'), str) or not embed['url'].startswith('https://')
            or hashlib.sha256(str(embed.get('url', '')).encode()).hexdigest() != row['source_url_sha256']):
        fail('manifest_source_mismatch')
    relative_asset = os.path.relpath(row['path'], Path(note_path).parent.as_posix()).replace(os.sep, '/')
    encoded_asset = urllib.parse.quote(relative_asset, safe='/._-')
    if not re.search(rb'^!\[[^\]]*\]\(' + re.escape(encoded_asset.encode()) + rb'\)\s*$', note, re.M):
        fail('note_asset_reference_mismatch')
    if (not isinstance(manifest.get('assets'), list) or len(manifest['assets']) != 1
            or manifest['assets'][0].get('path') != row['path']
            or manifest['assets'][0].get('sha256') != row['sha256']
            or manifest['assets'][0].get('bytes') != row['bytes']):
        fail('manifest_asset_mismatch')
    data_asset = assets.prepare(root, commit, row['path'])
    if (data_asset.sha256 != row['sha256'] or len(data_asset.data) != row['bytes']
            or data_asset.content_type != row.get('mime') or data_asset.block_type != 'image'):
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
    return proof['source_id'], config, data_asset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--proof-commit', required=True)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.proof_commit):
        fail('proof_commit_invalid')
    if git(args.vault, 'rev-parse', 'HEAD').decode().strip() != args.proof_commit:
        fail('proof_commit_mismatch')
    ident, config, asset = verify_proof(args.vault, args.proof_commit)
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
    # Existing pages are never modified by this probe. A later write path must
    # use the production worker's ownership/update transaction after review.
    result = {'status': 'dry_run_ready' if not matches else 'held_existing_page',
              'notes': 1, 'image_assets': 1, 'image_bytes': len(asset.data),
              'notion_workspace_limit': limit, 'existing_pages': len(matches),
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
