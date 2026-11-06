#!/usr/bin/env python3
"""Capture one Get note's verified image bytes on an isolated private ref.

This workflow probe never checks out or pushes private main. It commits only the
selected note, its private attachment manifest, content-addressed images and a
small proof record to a run-scoped codex/get-asset-proof-* ref.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import urllib.request
import urllib.parse

import sync_getnote_to_raw as source
from source_sync_assets import AssetStore
from source_sync_store import attachment_manifest, frontmatter, json_text, write_if_changed

PROOF_ROOT = '.sync/get-notion-asset-probes'
QUEUE_PATH = '.sync/get-notion-incremental/queue.json'
REGISTRY_PATH = 'Raw/03 Get/03 State/canonical-registry.json'
MAX_FILES = 8
MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_CANDIDATES = 16
MAX_SCAN_NOTES = 64
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_NOTE_BYTES = 2 * 1024 * 1024
FAILURE_STAGES = {'verify_private_repo', 'load_credentials', 'prepare_private_ref',
                  'select_candidate', 'fetch_get_detail', 'download_bind_attachment',
                  'validate_detail_identity', 'match_image_source', 'publish_private_ref', 'unknown'}
FAILURE_CATEGORIES = {'validation', 'auth_denied', 'not_found', 'rate_limited',
                      'server_error', 'http_error', 'api_rejected', 'transport_error',
                      'redirect_blocked', 'detail_error', 'detail_identity_mismatch',
                      'invalid_attachment_schema', 'no_image_descriptors', 'url_unbound'}
RETRYABLE_DETAIL_FAILURES = {'not_found', 'server_error', 'transport_error'}
CURRENT_STAGE = 'unknown'
CURRENT_FAILURE_CATEGORY = 'validation'
FAILURE_REPORTED = False
CANDIDATE_DIAGNOSTIC_NAMES = (
    'checked', 'detail_ok', 'detail_error', 'attachments_field', 'images_field',
    'files_field', 'resources_field', 'image_descriptors', 'descriptor_urls',
    'no_image_descriptors', 'url_unbound', 'source_url_matches', 'source_url_mismatches',
    'content_embed_urls', 'content_embed_matches', 'content_embed_rejects',
)
CANDIDATE_DIAGNOSTICS = dict.fromkeys(CANDIDATE_DIAGNOSTIC_NAMES, 0)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fail():
    report_failure_stage()
    raise SystemExit('private_asset_probe_failed')


def report_failure_stage():
    global FAILURE_REPORTED
    if FAILURE_REPORTED:
        return
    path = os.environ.get('GITHUB_OUTPUT')
    stage = CURRENT_STAGE if CURRENT_STAGE in FAILURE_STAGES else 'unknown'
    category = CURRENT_FAILURE_CATEGORY if CURRENT_FAILURE_CATEGORY in FAILURE_CATEGORIES else 'validation'
    if path:
        try:
            with open(path, 'a', encoding='utf-8') as output:
                output.write('failure_stage=' + stage + '\n')
                output.write('failure_category=' + category + '\n')
                output.write('candidate_stats=' + ','.join(
                    str(CANDIDATE_DIAGNOSTICS[name]) for name in CANDIDATE_DIAGNOSTIC_NAMES
                ) + '\n')
            FAILURE_REPORTED = True
        except OSError:
            pass


def detail_failure_category(error: Exception) -> str:
    message = str(error)
    http_match = re.fullmatch(r'GetNote HTTP ([1-5][0-9]{2})', message)
    if http_match:
        status = int(http_match[1])
        if status in (401, 403):
            return 'auth_denied'
        if status == 404:
            return 'not_found'
        if status == 429:
            return 'rate_limited'
        if status >= 500:
            return 'server_error'
        return 'http_error'
    if re.fullmatch(r'GetNote API error [0-9]{1,8}', message):
        return 'api_rejected'
    if message == 'GetNote transport failed':
        return 'transport_error'
    if message == 'GetNote redirect blocked':
        return 'redirect_blocked'
    return 'detail_error'


def normalize_image_attachments(note: dict) -> tuple[list[dict], set[str], str]:
    """Reuse Get's image descriptor aliases without inventing source URLs."""
    attachments = note.get('attachments') or []
    if not isinstance(attachments, list):
        return [], set(), 'invalid_attachment_schema'
    normalized = list(attachments)
    image_rows = [item for item in attachments if isinstance(item, dict) and item.get('type') == 'image']
    for field in ('images', 'files', 'resources'):
        values = note.get(field) or []
        if not isinstance(values, list):
            return [], set(), 'invalid_attachment_schema'
        for item in values:
            if not isinstance(item, dict):
                return [], set(), 'invalid_attachment_schema'
            mime = item.get('mime') or item.get('mime_type') or item.get('content_type') or ''
            is_image = field == 'images' or item.get('type') == 'image' or (
                isinstance(mime, str) and mime.lower().startswith('image/'))
            if not is_image:
                continue
            descriptor = dict(item)
            descriptor['type'] = 'image'
            image_rows.append(descriptor)
            normalized.append(descriptor)
    urls = set()
    for item in image_rows:
        for key in ('original_url', 'url'):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(('https://', 'http://')):
                urls.add(value)
    if not image_rows:
        return normalized, urls, 'no_image_descriptors'
    if not urls:
        return normalized, urls, 'url_unbound'
    # Deduplicate aliases by primary URL; preserve every original field in the
    # raw manifest, but give the downloader one image descriptor per source.
    unique = []
    seen = set()
    for item in normalized:
        if item.get('type') != 'image':
            unique.append(item)
            continue
        key = item.get('original_url') or item.get('url')
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique, urls, 'ok'


def explicit_content_image_urls(value: object) -> set[str]:
    """Extract only explicit HTTPS Markdown/HTML image embeds from Get content."""
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_NOTE_BYTES:
        return set()
    urls = set()
    for match in re.finditer(r'!\[[^\]]*\]\((https://[^\s()]+)\)', value):
        urls.add(match[1])
    for match in re.finditer(r'<img\b[^>]*\bsrc=["\'](https://[^"\']+)["\']', value, re.I):
        urls.add(match[1])
    safe = set()
    for url in urls:
        try:
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme == 'https' and parsed.hostname and not parsed.username
                    and not parsed.password and parsed.port in (None, 443)):
                safe.add(url)
        except ValueError:
            continue
    return safe


def private_markdown_image_urls(value: str) -> set[str]:
    """Return only standalone HTTPS embeds supported by the local-link rewriter."""
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_NOTE_BYTES:
        return set()
    urls = set()
    for line in value.splitlines():
        match = re.fullmatch(r'\s*!\[[^\]]*\]\((https://[^\s()]+)\)\s*', line)
        if match:
            urls.add(match[1])
    return urls


def record_candidate_diagnostics(note: dict, descriptors: list[dict], urls: set[str],
                                 status: str, markdown_urls: set[str]) -> None:
    """Keep only bounded counts; never retain IDs, titles, or source URLs."""
    CANDIDATE_DIAGNOSTICS['checked'] += 1
    for field in ('attachments', 'images', 'files', 'resources'):
        value = note.get(field)
        if isinstance(value, list) and value:
            CANDIDATE_DIAGNOSTICS[field + '_field'] += 1
    CANDIDATE_DIAGNOSTICS['image_descriptors'] += sum(
        1 for item in descriptors if isinstance(item, dict) and item.get('type') == 'image'
    )
    CANDIDATE_DIAGNOSTICS['descriptor_urls'] += len(urls)
    if status == 'no_image_descriptors':
        CANDIDATE_DIAGNOSTICS['no_image_descriptors'] += 1
    elif status == 'url_unbound':
        CANDIDATE_DIAGNOSTICS['url_unbound'] += 1
    elif status == 'ok':
        CANDIDATE_DIAGNOSTICS['detail_ok'] += 1
        if markdown_urls <= urls:
            CANDIDATE_DIAGNOSTICS['source_url_matches'] += 1
        else:
            CANDIDATE_DIAGNOSTICS['source_url_mismatches'] += 1


def record_content_embed_diagnostics(content_urls: set[str], markdown_urls: set[str]) -> None:
    CANDIDATE_DIAGNOSTICS['content_embed_urls'] += len(content_urls)
    if len(markdown_urls) == 1 and content_urls == markdown_urls:
        CANDIDATE_DIAGNOSTICS['content_embed_matches'] += 1
    else:
        CANDIDATE_DIAGNOSTICS['content_embed_rejects'] += 1


def git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], input=input,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        fail()
    return result.stdout


def verify_private_repository():
    token = os.environ.get('PRIVATE_VAULT_TOKEN', '')
    if not token or any(ord(c) <= 32 for c in token):
        fail()
    request = urllib.request.Request(
        'https://api.github.com/repos/Eddiehhhhh/eddie-llm-wiki',
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 1024 * 1024:
                fail()
        row = json.loads(raw)
    except Exception:
        fail()
    if (not isinstance(row, dict) or row.get('full_name') != 'Eddiehhhhh/eddie-llm-wiki'
            or row.get('private') is not True or row.get('default_branch') != 'main'):
        fail()


def safe_ref(value: str) -> str:
    if not re.fullmatch(r'codex/get-asset-proof-[A-Za-z0-9-]{1,64}', value):
        fail()
    return value


def pending_image_candidates(root: Path) -> list[tuple[str, set[str]]]:
    """Select bounded IDs privately from the authenticated queue and Markdown."""
    try:
        queue = json.loads(git(root, 'show', 'HEAD:' + QUEUE_PATH))
        registry = json.loads(git(root, 'show', 'HEAD:' + REGISTRY_PATH))
    except (SystemExit, ValueError, UnicodeError):
        fail()
    pending = queue.get('pending') if isinstance(queue, dict) else None
    rows = registry.get('newById') if isinstance(registry, dict) else None
    if (not isinstance(pending, list) or not isinstance(rows, dict) or len(pending) > 5000
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9]{19}', value) for value in pending)
            or len(pending) != len(set(pending))):
        fail()
    candidates = []; scanned_bytes = 0
    for ident in pending[:MAX_SCAN_NOTES]:
        row = rows.get(ident)
        path = row.get('filePath') if isinstance(row, dict) else None
        if (not isinstance(path, str) or not path.endswith('.md') or path.startswith('/')
                or '\\' in path or any(part in ('', '.', '..') for part in PurePosixPath(path).parts)):
            continue
        full = 'Raw/03 Get/' + path
        try:
            size = int(git(root, 'cat-file', '-s', 'HEAD:' + full).decode().strip())
            if size < 0 or size > MAX_NOTE_BYTES or scanned_bytes + size > MAX_SCAN_BYTES:
                continue
            data = git(root, 'show', 'HEAD:' + full).decode('utf-8')
        except (SystemExit, UnicodeError):
            continue
        scanned_bytes += size
        refs = private_markdown_image_urls(data)
        if refs and len(refs) <= MAX_FILES:
            candidates.append((ident, refs))
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def rewrite_remote_image_links(markdown: str, image_assets: list[dict], note_path: Path,
                               descriptors: list[dict] | None = None) -> tuple[str, int, set[str]]:
    """Rewrite only exact Get attachment URLs to their verified local files."""
    raw_dir = next((candidate for candidate in (note_path.parent, *note_path.parents)
                    if candidate.name == '03 Get' and candidate.parent.name == 'Raw'), None)
    if raw_dir is None:
        fail()
    by_url = {}
    for asset in image_assets:
        url = asset.get('url')
        relative = asset.get('path')
        if (asset.get('status') != 'downloaded' or not isinstance(url, str)
                or not isinstance(relative, str) or not re.fullmatch(r'[0-9a-f]{64}', asset.get('sha256', ''))):
            fail()
        absolute = raw_dir / relative
        local = Path(os.path.relpath(absolute, note_path.parent)).as_posix()
        if url in by_url and by_url[url][0] != local:
            fail()
        by_url[url] = (local, url)
    for descriptor in descriptors or []:
        if descriptor.get('type') != 'image':
            continue
        primary = descriptor.get('original_url') or descriptor.get('url')
        binding = by_url.get(primary)
        if binding is None:
            continue
        for key in ('original_url', 'url'):
            alias = descriptor.get(key)
            if isinstance(alias, str):
                if alias in by_url and by_url[alias][0] != binding[0]:
                    fail()
                by_url[alias] = binding
    unmatched = 0
    matched_urls = set()
    lines = []
    image = re.compile(r'^(!\[[^\]]*\]\()([^\s()]+)(\))$')
    for line in markdown.splitlines():
        match = image.fullmatch(line)
        if match and urllib.parse.urlsplit(match[2]).scheme in ('http', 'https'):
            binding = by_url.get(match[2])
            if binding is None:
                unmatched += 1
                lines.append(line)
            else:
                local, primary_url = binding
                matched_urls.add(hashlib.sha256(primary_url.encode()).hexdigest())
                lines.append(match[1] + urllib.parse.quote(local, safe='/._-') + match[3])
        else:
            lines.append(line)
    return '\n'.join(lines) + ('\n' if markdown.endswith('\n') else ''), unmatched, matched_urls


def stage_note(root: Path, note: dict, run_ref: str, base_commit: str,
               source_note: dict | None = None, source_field: str = 'attachments') -> dict:
    if source_field not in {'attachments', 'content_embed'}:
        fail()
    ident = source.note_id_from(note)
    if not re.fullmatch(r'[0-9]{19}', ident):
        fail()
    index = source.IdentityIndex(source.RAW_DIR, 'getnote', 'note_id', boundary=root)
    note_path = source.output_path_for(note, index)
    descriptors, _, descriptor_status = normalize_image_attachments(note)
    if descriptor_status != 'ok':
        fail()
    images = [item for item in descriptors if item.get('type') == 'image']
    if len(images) != 1:
        fail()
    # The existing production downloader supplies HTTPS/IP/redirect, byte,
    # MIME, image-type and content-hash checks. This runner caps the probe.
    bounded_note = dict(note, attachments=images)
    store = AssetStore(source.RAW_DIR, max_file=20 * 1024 * 1024,
                       max_run=MAX_TOTAL_BYTES, max_assets=MAX_FILES)
    assets = store.materialize(bounded_note)
    downloaded_by_path = {}
    for item in assets:
        if item.get('kind') != 'image' or item.get('status') != 'downloaded':
            continue
        old = downloaded_by_path.get(item.get('path'))
        if old is not None and old.get('sha256') != item.get('sha256'):
            fail()
        downloaded_by_path[item['path']] = item
    downloaded = list(downloaded_by_path.values())
    if len(downloaded) != 1 or store.bytes > MAX_TOTAL_BYTES:
        fail()
    if note_path.exists():
        if frontmatter(note_path).get('note_id') != ident:
            fail()
        note_text = note_path.read_text(encoding='utf-8')
    else:
        note_text = source.render_note(note, assets, note_path)
    original_source_note = source_note or note
    source_content_sha256 = None
    if source_field == 'content_embed':
        source_content = original_source_note.get('content')
        content_urls = explicit_content_image_urls(source_content)
        markdown_urls = private_markdown_image_urls(note_text)
        asset_urls = {item.get('original_url') or item.get('url') for item in images}
        if (not isinstance(source_content, str) or source.note_id_from(original_source_note) != ident
                or len(content_urls) != 1 or content_urls != markdown_urls or content_urls != asset_urls):
            fail()
        source_content_sha256 = hashlib.sha256(source_content.encode('utf-8')).hexdigest()
    note_text, unmatched, matched_urls = rewrite_remote_image_links(note_text, downloaded, note_path, images)
    if unmatched:
        # Do not claim the note is ready when a visible image URL has no exact
        # byte-verified descriptor binding.
        fail()
    downloaded_urls = {hashlib.sha256(item['url'].encode()).hexdigest() for item in downloaded}
    unreferenced = downloaded_urls - matched_urls
    if unreferenced:
        links = []
        for asset in downloaded:
            if hashlib.sha256(asset['url'].encode()).hexdigest() not in unreferenced:
                continue
            relative = Path(os.path.relpath(source.RAW_DIR / asset['path'], note_path.parent)).as_posix()
            links.append('![image](' + urllib.parse.quote(relative, safe='/._-') + ')')
        note_text = note_text.rstrip() + '\n\n## 本地附件\n\n' + '\n'.join(links) + '\n'
    cache_id = hashlib.sha256(ident.encode()).hexdigest()
    manifest_path = source.STATE_DIR / 'attachments' / (cache_id + '.json')
    manifest = dict(attachment_manifest(original_source_note), assets=assets)
    if source_field == 'content_embed':
        manifest['content_image_embeds'] = [
            {'source_field': 'content', 'source_content_sha256': source_content_sha256,
             'url': asset['url'], 'url_sha256': hashlib.sha256(asset['url'].encode()).hexdigest(),
             'asset_path': asset['path'], 'asset_sha256': asset['sha256'],
             'bytes': asset['bytes'], 'mime': asset['mime']}
            for asset in downloaded
        ]
    write_if_changed(note_path, note_text)
    write_if_changed(manifest_path, json_text(manifest))

    relative_note = note_path.relative_to(root).as_posix()
    relative_manifest = manifest_path.relative_to(root).as_posix()
    asset_rows = []
    paths = [relative_note, relative_manifest]
    for asset in downloaded:
        asset_path = source.RAW_DIR / asset['path']
        relative_asset = asset_path.relative_to(root).as_posix()
        data = asset_path.read_bytes()
        if (hashlib.sha256(data).hexdigest() != asset['sha256']
                or len(data) != asset['bytes']):
            fail()
        asset_rows.append({'path': relative_asset, 'sha256': asset['sha256'],
                           'source_url_sha256': hashlib.sha256(asset['url'].encode()).hexdigest(),
                           'bytes': len(data), 'mime': asset['mime']})
        paths.append(relative_asset)
    note_bytes = note_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    proof = {'contract': 'getnote-private-content-image-probe-1', 'source_id': ident,
             'source_field': source_field,
             'source_content_sha256': source_content_sha256,
             'source_json_sha256': hashlib.sha256(json_text(original_source_note).encode()).hexdigest(),
             'note_path': relative_note, 'note_sha256': hashlib.sha256(note_bytes).hexdigest(),
             'manifest_path': relative_manifest, 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
             'assets': sorted(asset_rows, key=lambda row: row['path']), 'ref': run_ref,
             'parent': base_commit}
    proof_path = f'{PROOF_ROOT}/{ident}.json'
    write_if_changed(root / proof_path, json.dumps(proof, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n')
    paths.append(proof_path)
    if len(paths) > MAX_FILES + 3:
        fail()
    return {'proof': proof, 'paths': sorted(set(paths))}


def prepare_private_stage(root: Path, ref: str) -> str:
    ref = safe_ref(ref)
    allowed_remote = {'https://github.com/Eddiehhhhh/eddie-llm-wiki.git',
                      'https://github.com/Eddiehhhhh/eddie-llm-wiki',
                      'git@github.com:Eddiehhhhh/eddie-llm-wiki.git'}
    fetch_urls = git(root, 'remote', 'get-url', '--all', 'origin').decode().splitlines()
    push_urls = git(root, 'remote', 'get-url', '--push', '--all', 'origin').decode().splitlines()
    if len(fetch_urls) != 1 or len(push_urls) != 1 or fetch_urls[0] not in allowed_remote or push_urls[0] not in allowed_remote:
        fail()
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        fail()
    if git(root, 'ls-remote', '--heads', 'origin', 'refs/heads/' + ref).strip():
        fail()
    git(root, 'fetch', '--no-tags', 'origin', 'main')
    base = git(root, 'rev-parse', 'origin/main').decode().strip()
    git(root, 'switch', '--create', ref, 'origin/main')
    if git(root, 'rev-parse', 'HEAD').decode().strip() != base:
        fail()
    return base


def publish_private_stage(root: Path, ref: str, paths: list[str]) -> str:
    ref = safe_ref(ref)
    if git(root, 'branch', '--show-current').decode().strip() != ref:
        fail()
    allowed = set(paths)
    status = git(root, 'status', '--porcelain', '--untracked-files=all', '-z').decode().split('\0')
    changed = {item[3:] for item in status if item}
    # Only this probe's note, manifest, assets and proof may be staged.
    if changed - allowed:
        fail()
    git(root, 'add', '--', *paths)
    staged = git(root, 'diff', '--cached', '--name-only', '-z').decode().split('\0')
    if {item for item in staged if item} != allowed:
        fail()
    git(root, 'config', 'user.name', 'github-actions[bot]')
    git(root, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git(root, 'commit', '-m', 'Stage one verified Get image attachment')
    commit = git(root, 'rev-parse', 'HEAD').decode().strip()
    # Empty expected value makes the remote update create-only.
    verify_private_repository()
    git(root, 'push', f'--force-with-lease=refs/heads/{ref}:', 'origin', f'HEAD:refs/heads/{ref}')
    observed = git(root, 'ls-remote', 'origin', 'refs/heads/' + ref).decode().split()
    if len(observed) != 2 or observed != [commit, 'refs/heads/' + ref]:
        fail()
    # Read the pushed commit's exact blobs back through Git and recheck each
    # file's bytes against the private proof before reporting success.
    proof_candidates = [path for path in paths if path.startswith(PROOF_ROOT + '/')]
    if len(proof_candidates) != 1:
        fail()
    proof = json.loads(git(root, 'show', f'{commit}:{proof_candidates[0]}'))
    if git(root, 'rev-parse', commit + '^').decode().strip() != proof.get('parent'):
        fail()
    expected = {proof['note_path']: proof['note_sha256'],
                proof['manifest_path']: proof['manifest_sha256']}
    for item in proof['assets']:
        expected[item['path']] = None
    for path, digest in expected.items():
        data = git(root, 'show', f'{commit}:{path}')
        if digest is not None and hashlib.sha256(data).hexdigest() != digest:
            fail()
    for item in proof['assets']:
        data = git(root, 'show', f"{commit}:{item['path']}")
        if hashlib.sha256(data).hexdigest() != item['sha256'] or len(data) != item['bytes']:
            fail()
    note_data = git(root, 'show', f"{commit}:{proof['note_path']}")
    if not re.search(rb'^note_id:\s*["\']?' + proof['source_id'].encode() + rb'["\']?\s*$', note_data, re.M):
        fail()
    manifest = json.loads(git(root, 'show', f"{commit}:{proof['manifest_path']}"))
    manifest_assets = manifest.get('assets')
    if not isinstance(manifest_assets, list):
        fail()
    by_path = {row.get('path'): row for row in manifest_assets if isinstance(row, dict)}
    if set(by_path) != {item['path'].removeprefix('Raw/03 Get/') for item in proof['assets']}:
        fail()
    for item in proof['assets']:
        row = by_path[item['path'].removeprefix('Raw/03 Get/')]
        if (row.get('status') != 'downloaded' or row.get('sha256') != item['sha256']
                or row.get('bytes') != item['bytes'] or row.get('mime') != item['mime']):
            fail()
        if hashlib.sha256(str(row.get('url', '')).encode()).hexdigest() != item['source_url_sha256']:
            fail()
    if proof.get('source_field') == 'content_embed':
        source_content_sha256 = proof.get('source_content_sha256')
        embedded = manifest.get('content_image_embeds')
        if (not isinstance(source_content_sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', source_content_sha256)
                or not isinstance(embedded, list) or len(embedded) != len(proof['assets'])):
            fail()
        by_asset = {row.get('asset_path'): row for row in embedded if isinstance(row, dict)}
        if len(by_asset) != len(embedded):
            fail()
        for item in proof['assets']:
            row = by_asset.get(item['path'].removeprefix('Raw/03 Get/'))
            if (not isinstance(row, dict) or row.get('source_field') != 'content'
                    or row.get('source_content_sha256') != source_content_sha256
                    or row.get('asset_sha256') != item['sha256']
                    or row.get('bytes') != item['bytes'] or row.get('mime') != item['mime']
                    or hashlib.sha256(str(row.get('url', '')).encode()).hexdigest() != item['source_url_sha256']
                    or row.get('url_sha256') != item['source_url_sha256']):
                fail()
    else:
        descriptors = manifest.get('attachments')
        if not isinstance(descriptors, list):
            fail()
        source_urls = {hashlib.sha256(str(item.get('original_url') or item.get('url') or '').encode()).hexdigest()
                       for item in descriptors if isinstance(item, dict) and item.get('type') == 'image'}
        if not {item['source_url_sha256'] for item in proof['assets']} <= source_urls:
            fail()
    image_paths = set()
    text = note_data.decode('utf-8')
    for line in text.splitlines():
        match = re.fullmatch(r'!\[[^\]]*\]\(([^\s()]+)\)', line)
        if not match:
            continue
        reference = urllib.parse.unquote(match[1])
        path = PurePosixPath(proof['note_path']).parent
        for part in reference.split('/'):
            if part == '..':
                path = path.parent
            elif part in ('', '.'):
                fail()
            else:
                path /= part
        image_paths.add(path.as_posix())
    if image_paths != {item['path'] for item in proof['assets']}:
        fail()
    return commit


def main() -> int:
    global CURRENT_STAGE, CURRENT_FAILURE_CATEGORY
    parser = argparse.ArgumentParser()
    parser.add_argument('--vault', type=Path, default=Path(os.environ.get('VAULT_ROOT', 'vault')))
    args = parser.parse_args()
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    run_attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', run_id) or not re.fullmatch(r'[1-9][0-9]{0,5}', run_attempt):
        fail()
    private_ref = 'codex/get-asset-proof-' + run_id + '-' + run_attempt
    ref = safe_ref(private_ref)
    root = args.vault.resolve()
    if not root.is_dir() or root.is_symlink():
        fail()
    CURRENT_STAGE = 'verify_private_repo'
    verify_private_repository()
    CURRENT_STAGE = 'load_credentials'
    api_key, client_id = source.load_credentials()
    CURRENT_STAGE = 'prepare_private_ref'
    private_ref_base = prepare_private_stage(root, ref)
    # Keep helper globals aligned with the explicit private checkout path.
    source.ROOT = root
    source.RAW_DIR = root / 'Raw' / '03 Get'
    source.NOTES_DIR = source.RAW_DIR / '01 Notes'
    source.INDEX_DIR = source.RAW_DIR / '02 Index'
    source.STATE_DIR = source.RAW_DIR / '03 State'
    CURRENT_STAGE = 'select_candidate'
    candidates = pending_image_candidates(root)
    selected = None
    selected_source_note = None
    selected_source_field = 'attachments'
    for ident, markdown_urls in candidates:
        CURRENT_STAGE = 'fetch_get_detail'
        try:
            note = source.fetch_note_detail(ident, api_key, client_id)
        except Exception as error:
            CANDIDATE_DIAGNOSTICS['checked'] += 1
            CANDIDATE_DIAGNOSTICS['detail_error'] += 1
            CURRENT_FAILURE_CATEGORY = detail_failure_category(error)
            if CURRENT_FAILURE_CATEGORY in RETRYABLE_DETAIL_FAILURES:
                continue
            fail()
        CURRENT_STAGE = 'validate_detail_identity'
        CURRENT_FAILURE_CATEGORY = 'detail_identity_mismatch'
        try:
            detail_id = source.note_id_from(note)
        except Exception:
            fail()
        if detail_id != ident:
            fail()
        CURRENT_FAILURE_CATEGORY = 'validation'
        descriptors, descriptor_urls, descriptor_status = normalize_image_attachments(note)
        record_candidate_diagnostics(note, descriptors, descriptor_urls,
                                     descriptor_status, markdown_urls)
        CURRENT_STAGE = 'match_image_source'
        content_urls = explicit_content_image_urls(note.get('content'))
        record_content_embed_diagnostics(content_urls, markdown_urls)
        if descriptor_status != 'ok':
            CURRENT_FAILURE_CATEGORY = descriptor_status
            if (descriptor_status == 'no_image_descriptors' and len(markdown_urls) == 1
                    and content_urls == markdown_urls):
                url = next(iter(content_urls))
                selected_source_note = note
                selected = dict(note, attachments=[{'type': 'image', 'original_url': url, 'url': url}])
                selected_source_field = 'content_embed'
                break
            continue
        if len(markdown_urls) == 1 and markdown_urls == descriptor_urls:
            selected_source_note = note
            selected = dict(note, attachments=descriptors)
            selected_source_field = 'attachments'
            break
        CURRENT_FAILURE_CATEGORY = 'url_unbound'
    if selected is None:
        fail()
    CURRENT_STAGE = 'download_bind_attachment'
    staged = stage_note(root, selected, ref, private_ref_base, selected_source_note,
                        source_field=selected_source_field)
    # stage_note writes only in the isolated runner checkout; the single push
    # targets the run-scoped private branch and is read back above.
    CURRENT_STAGE = 'publish_private_ref'
    commit = publish_private_stage(root, ref, staged['paths'])
    print(json.dumps({'status': 'verified', 'notes': 1,
                      'image_assets': len(staged['proof']['assets']),
                      'source_field': staged['proof']['source_field'],
                      'private_files_readback': len(staged['paths']),
                      'private_ref': ref, 'commit': commit,
                      'main_targeted': False}, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if exc.code not in (None, 0):
            report_failure_stage()
        raise
    except Exception:
        report_failure_stage()
        raise SystemExit('private_asset_probe_failed')
