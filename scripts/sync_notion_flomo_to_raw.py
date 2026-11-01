#!/usr/bin/env python3
"""Stage the existing Notion flomo feed in a private vault; never write Raw/01.

Default is inventory only. Apply stores source JSON, readable Markdown and
content-addressed attachments below Raw/02 for later reviewed collector cutover.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
from collections import defaultdict
from copy import deepcopy
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import sync_notion_to_raw as notion
import cloud_private_checkout as private_checkout

STAGE = Path('Raw/02 Notion/01 Databases/碎片中心/flomo-stage')
STATE = Path('.github/state/notion-flomo-sync-state.json')
CURRENT_BASE = Path('Raw/01 Flomo/云端')
MAX_ASSET_BYTES = 50 * 1024 * 1024
CREDENTIAL_SHAPE = re.compile(
    r'(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
    r'|sk-[A-Za-z0-9_-]{20,}|(?:ntn_|secret_)[A-Za-z0-9]{30,})(?![A-Za-z0-9_])'
    r'|-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')


def reject_credentials(raw):
    if CREDENTIAL_SHAPE.search(raw.decode('utf-8')):
        raise ValueError('potential_credential_in_source')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def memo_id(page):
    value = page.get('properties', {}).get('Link', {}).get('url')
    if not isinstance(value, str): return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname not in ('v.flomoapp.com', 'flomoapp.com'):
        return None
    values = urllib.parse.parse_qs(parsed.query).get('memo_id', [])
    if len(values) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', values[0]):
        raise ValueError('invalid_memo_identity')
    if CREDENTIAL_SHAPE.search(values[0]): raise ValueError('potential_credential_in_source')
    return values[0]


def current_artifact(root, path):
    base = root / CURRENT_BASE
    return path == base / 'index.md' or any(path.is_relative_to(base / area)
        for area in ('版本', 'legacy-evidence', '_assets'))


def local_inventory(root):
    paths = defaultdict(list)
    no_id = []
    for path in sorted((root / 'Raw/01 Flomo').rglob('*.md')):
        if current_artifact(root, path): continue
        if path.is_symlink(): raise ValueError('local_symlink')
        parts = path.read_text(encoding='utf-8').split('---', 2)
        match = re.search(r'^memo_id:\s*["\x27]?([^"\x27\s]+)', parts[1], re.M) if len(parts) == 3 else None
        if match: paths[match[1]].append(str(path.relative_to(root)))
        else: no_id.append(str(path.relative_to(root)))
    return dict(paths), no_id


def checked_page(api_key, page):
    """Retain full relation values when Notion's page response truncates them."""
    result = deepcopy(page)
    for value in result.get('properties', {}).values():
        if value.get('type') != 'relation' or not value.get('has_more'): continue
        cursor = None
        seen = set()
        rows = []
        while True:
            query = urllib.parse.urlencode({'page_size': 100, **({'start_cursor': cursor} if cursor else {})})
            prop = urllib.parse.quote(urllib.parse.unquote(value['id']), safe='')
            response = notion.notion_request(api_key, 'GET', f'/pages/{page["id"]}/properties/{prop}?{query}')
            if (not isinstance(response.get('results'), list) or type(response.get('has_more')) is not bool
                    or any(row.get('type') != 'relation' for row in response['results'])):
                raise ValueError('incomplete_relation')
            rows.extend(row['relation'] for row in response['results'])
            if not response['has_more']: break
            cursor = response.get('next_cursor')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError('incomplete_relation')
            seen.add(cursor)
        value['relation'] = rows
        value['has_more'] = False
    return result


def block_tree(api_key, page_id, depth=0, ancestors=()):
    if depth > 30 or page_id in ancestors: raise ValueError('block_cycle')
    rows = notion.retrieve_block_children(api_key, page_id)
    for row in rows:
        if not isinstance(row.get('id'), str) or not isinstance(row.get('type'), str):
            raise ValueError('incomplete_block')
        if row['type'] == 'unsupported': raise ValueError('unsupported_block')
        source = (row.get('synced_block') or {}).get('synced_from') or {}
        if row.get('has_children') or source.get('block_id'):
            row['_children'] = block_tree(api_key, source.get('block_id') or row['id'], depth + 1,
                                          (*ancestors, page_id))
    return rows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def download_asset(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError('unsafe_asset_url')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('unsafe_asset_address')
    address = addresses[0][4][0]
    class PinnedConnection(http.client.HTTPSConnection):
        def connect(self):
            if self.host != parsed.hostname: raise ValueError('asset_host_changed')
            # Connect to the checked IP once; keep TLS hostname verification.
            raw_socket = socket.create_connection((address, 443), self.timeout)
            try: self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
            except Exception:
                raw_socket.close()
                raise
    class PinnedHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request): return self.do_open(PinnedConnection, request)
    # Never attach the Notion credential or follow an unvalidated redirect.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), PinnedHTTPS())
    with opener.open(urllib.request.Request(url, method='GET'), timeout=60) as response:
        raw = response.read(MAX_ASSET_BYTES + 1)
        length = response.headers.get('Content-Length')
        if len(raw) > MAX_ASSET_BYTES: raise ValueError('asset_file_budget')
        if response.status != 200 or (length and int(length) != len(raw)):
            raise ValueError('incomplete_asset')
    return raw


def materialize_assets(value, files, downloader):
    if isinstance(value, list):
        return [materialize_assets(item, files, downloader) for item in value]
    if not isinstance(value, dict): return value
    result = {key: materialize_assets(item, files, downloader) for key, item in value.items()}
    kind = value.get('type')
    if kind in ('file', 'external') and isinstance(value.get(kind), dict) and value[kind].get('url'):
        raw = downloader(value[kind]['url'])
        suffix = Path(urllib.parse.urlsplit(value[kind]['url']).path).suffix.lower()
        suffix = suffix if re.fullmatch(r'\.[a-z0-9]{1,8}', suffix) else '.bin'
        relative = Path('assets') / (digest(raw) + suffix)
        if sum(len(item) for item in files.values()) + len(raw) > 200 * 1024 * 1024:
            raise ValueError('asset_batch_bound')
        files[str(relative)] = raw
        result[kind]['url'] = relative.as_posix()
    return result


def render_record(page, blocks, mid, local_paths, *, embed_images=False, include_text_properties=False):
    cache = {}
    def register(rows):
        for row in rows:
            if '_children' in row:
                cache[row['id']] = row['_children']
                register(row['_children'])
                row['has_children'] = True
    register(blocks)
    title = notion.page_title(page, 'Name')
    properties = notion.summarize_properties(page)
    metadata = {'source_system': 'flomo', 'upstream_system': 'notion', 'memo_id': mid,
                'title': title, 'notion_page_id': page['id'], 'notion_page_url': page.get('url'),
                'created_at': ((page.get('properties', {}).get('Created At') or {}).get('date') or {}).get('start'),
                'notion_created_time': page.get('created_time'),
                'notion_last_edited_time': page.get('last_edited_time'),
                'tags': properties.get('Tags', []), 'notion_properties': properties,
                'local_existing_paths': local_paths, 'stage_only': True}
    body = '\n'.join(notion.render_blocks('', blocks, children_loader=lambda key, ident: cache[ident], embed_images=embed_images))
    if include_text_properties:
        for name, prop in page.get('properties', {}).items():
            if prop.get('type') != 'rich_text': continue
            text = notion.rich_text_plain_text(prop.get('rich_text', []))
            if text.strip(): body += '\n\n## Notion 文本属性：' + str(name).replace('\n', ' ') + '\n\n' + text
    return (notion.render_frontmatter(metadata) + '\n\n' + body + '\n').encode()


def safe_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts: raise ValueError('unsafe_stage_path')
    path = root / relative
    for item in (path, *path.parents):
        if item.is_symlink(): raise ValueError('stage_symlink')
        if item == root: break
    if not path.resolve().is_relative_to(root.resolve()): raise ValueError('unsafe_stage_path')
    return path


def atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
    try: os.replace(temporary, path)
    finally: temporary.unlink(missing_ok=True)
    if path.read_bytes() != raw: raise ValueError('stage_readback_failed')


def immutable_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
    try:
        try: os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != raw: raise ValueError('stage_edited')
    finally: temporary.unlink(missing_ok=True)
    if path.read_bytes() != raw: raise ValueError('stage_readback_failed')


def prior_files(prior):
    result = dict(prior.get('files', {}))
    for files in prior.get('versions', {}).values(): result.update(files)
    for source in prior.get('source_pages', {}).values(): result.update(prior_files(source))
    for files in prior.get('variant_indexes', {}).values(): result.update(files)
    return result


def apply_record(root, mid, files, prior):
    for relative, sha in prior_files(prior).items():
        path = safe_path(root / STAGE, relative)
        if not path.is_file() or digest(path.read_bytes()) != sha: raise ValueError('stage_edited')
    version = digest(encoded({key: digest(raw) for key, raw in files.items()}))
    versioned = {str(Path('versions') / mid / version / rel): raw for rel, raw in files.items()}
    for relative, raw in versioned.items():
        immutable_write(safe_path(root / STAGE, relative), raw)
    return version, {relative: digest(raw) for relative, raw in versioned.items()}


@contextmanager
def writer_lock(root):
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--absolute-git-dir'],
                            capture_output=True, text=True, timeout=15, check=True)
    path = Path(result.stdout.strip()) / 'notion-flomo-stage.lock'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('stage_writer_busy') from None
        yield
    finally: os.close(fd)


def run(root, database_id, api_key, *, apply=False, max_pages=50, downloader=download_asset,
        original_source_enabled=False, original_signing_suffix=None, current_export_enabled=False,
        property_proof_enabled=False, property_proof_batch=None):
    if apply:
        private_checkout.verify_private_checkout(root, os.environ.get('PRIVATE_VAULT_TOKEN'))
        with writer_lock(root):
            result = stage(root, database_id, api_key, apply=True, max_pages=max_pages, downloader=downloader,
                           property_proof_enabled=property_proof_enabled, property_proof_batch=property_proof_batch)
            persistence_due = result.get('budget_reason') == 'job_budget_reserved_for_persistence'
            if persistence_due:
                result['followup_pending'] = {'original_source': original_source_enabled, 'formal_current': current_export_enabled}
            if original_source_enabled and not persistence_due:
                import flomo_original_source as original
                import sys
                supplement = original.enrich(root, sys.modules[__name__],
                    token=os.environ.get('FLOMO_WEB_ACCESS_TOKEN'), signing_suffix=original_signing_suffix,
                    max_pages=max_pages, downloader=downloader)
                result['original_source'] = supplement
                result['complete'] = result['complete'] and supplement['complete']
            if current_export_enabled and not persistence_due:
                import flomo_cloud_current as current
                result['formal_current'] = current.export(root)
                result['complete'] = result['complete'] and result['formal_current']['complete']
            return result
    return stage(root, database_id, api_key, max_pages=max_pages, downloader=downloader,
                 property_proof_enabled=property_proof_enabled, property_proof_batch=property_proof_batch)


def metadata_fingerprint(page):
    """Compare API-visible values without temporary Notion resource URL churn."""
    def stable(value):
        if isinstance(value, list): return [stable(item) for item in value]
        if not isinstance(value, dict): return value
        result = {key: stable(item) for key, item in value.items()}
        if value.get('type') == 'file' and isinstance(result.get('file'), dict):
            result['file'].pop('expiry_time', None)
            url = result['file'].get('url')
            if isinstance(url, str):
                parsed = urllib.parse.urlsplit(url)
                result['file']['url'] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
        return result
    return digest(encoded(stable({key: page.get(key) for key in ('properties', 'cover', 'icon')})))


def source_fingerprints(page, blocks):
    def body(rows):
        return [{'type': row['type'], 'payload': row.get(row['type'], {}),
                 'children': body(row.get('_children', []))} for row in rows]
    properties = {name: {'type': prop['type'], 'value': prop.get(prop['type'])}
                  for name, prop in page['properties'].items()
                  if prop.get('type') not in {'created_time', 'last_edited_time', 'created_by', 'last_edited_by', 'button'}}
    return digest(encoded(body(blocks))), digest(encoded(properties))


def saved_fingerprints(root, mid, source):
    if source.get('body_content_sha256') and source.get('properties_content_sha256'):
        return source['body_content_sha256'], source['properties_content_sha256']
    snapshot = next(rel for rel in source['files'] if rel.endswith('/' + mid + '.json'))
    raw = safe_path(root / STAGE, snapshot).read_bytes()
    reject_credentials(raw)
    value = json.loads(raw)
    return source_fingerprints(value['page'], value['blocks'])


def variant_index(root, mid, group, observed):
    sources = group.get('source_pages', {})
    lines = [notion.render_frontmatter({'source_system': 'flomo', 'memo_id': mid,
             'stage_only': True, 'source_page_ids': sorted(sources)}), '',
             '# 同一 memo 的 Notion 来源版本', '',
             '以下来源并列保留；仅供核对，不代表已合并或已确认事实。', '']
    for number, ident in enumerate(sorted(set(sources) | set(observed)), 1):
        source = sources.get(ident)
        if source:
            note = next(rel for rel in source['files'] if rel.endswith('/' + mid + '.md'))
            target = Path(note).relative_to(Path('versions') / mid)
            lines.append(f'- [来源 {number}](../{target.as_posix()})' +
                         ('（当前集合未再观察到；保留历史）' if ident not in observed else ''))
        else:
            lines.append(f'- 来源 {number}：尚待完整抓取。')
    raw = ('\n'.join(lines) + '\n').encode()
    relative = str(Path('versions') / mid / 'indexes' / (digest(raw) + '.md'))
    immutable_write(safe_path(root / STAGE, relative), raw)
    return relative, {relative: digest(raw)}


def proof_limits(value=None, max_pages=50):
    limits = {'max_pages': 50, 'max_seconds': 300, 'max_requests': 250}
    bounds = {'max_pages': (1, 500), 'max_seconds': (60, 1200), 'max_requests': (20, 2000)}
    if value is not None:
        if not isinstance(value, dict) or set(value) - set(limits): raise ValueError('invalid_property_proof_batch')
        limits.update(value)
    for key, (low, high) in bounds.items():
        if type(limits[key]) is not int or not low <= limits[key] <= high:
            raise ValueError('invalid_property_proof_batch')
    if max_pages: limits['max_pages'] = min(limits['max_pages'], max_pages)
    return limits


def stage(root, database_id, api_key, *, apply=False, max_pages=50, downloader=download_asset,
          property_proof_enabled=False, property_proof_batch=None):
    if type(property_proof_enabled) is not bool: raise ValueError('invalid_property_proof_configuration')
    if not property_proof_enabled:
        return _stage(root, database_id, api_key, apply=apply, max_pages=max_pages, downloader=downloader)
    from notion_source_budget import Budget
    budget = Budget(proof_limits(property_proof_batch, max_pages))
    last_request = [None]
    def observe(method, path):
        if method != 'GET' and not (method == 'POST' and path == '/databases/' + notion.normalize_notion_id(database_id) + '/query'):
            raise ValueError('unexpected_source_mutation')
        budget.request()
        if last_request[0] is not None:
            delay = .7 - (time.monotonic() - last_request[0])
            if delay > 0: time.sleep(delay)
        budget.check()
        last_request[0] = time.monotonic()
    original_downloader = downloader
    def downloader(url):
        budget.request()
        raw = original_downloader(url)
        budget.asset(raw)
        return raw
    observer = notion.REQUEST_OBSERVER.set(observe)
    retries = notion.READ_RETRIES.set(0)
    try:
        return _stage(root, database_id, api_key, apply=apply, max_pages=max_pages,
                      downloader=downloader, proof_budget=budget)
    finally:
        notion.READ_RETRIES.reset(retries)
        notion.REQUEST_OBSERVER.reset(observer)


def _stage(root, database_id, api_key, *, apply=False, max_pages=50, downloader=download_asset, proof_budget=None):
    if max_pages < 0: raise ValueError('invalid_limit')
    local, no_id = local_inventory(root)
    try:
        rows = list(notion.paginate_database(api_key, database_id))
    except Exception as exc:
        from notion_source_budget import BudgetExceeded
        if proof_budget is None or not isinstance(exc, BudgetExceeded): raise
        # A partial metadata traversal cannot prove absence or replace source progress.
        report = {'notion_pages': None, 'flomo_ids': None, 'flomo_source_pages': None,
                  'written': 0, 'unchanged': 0, 'pending': None, 'held': {},
                  'complete': False, 'capture_complete': False,
                  'property_proof_complete': False, 'inventory_complete': False,
                  'budget_reason': str(exc), 'requests': proof_budget.requests,
                  'unmerged_variant_group_count': None, 'semantic_merge_complete': False,
                  'stage_only': True, 'fact_promotion_allowed': False}
        if apply: atomic_write(safe_path(root, STAGE / 'coverage.json'), encoded(report))
        return report
    groups = defaultdict(list)
    page_ids = set()
    identity_holds = {}
    for row in rows:
        if (not isinstance(row.get('id'), str) or row['id'] in page_ids
                or notion.parse_time(row.get('last_edited_time')) is None):
            raise ValueError('incomplete_page_inventory')
        page_ids.add(row['id'])
        try:
            if proof_budget is not None: reject_credentials(encoded(row))
            mid = memo_id(row)
        except ValueError as exc:
            if str(exc) != 'potential_credential_in_source': raise
            identity_holds['page::' + row['id']] = 'potential_credential_in_source'
            continue
        if mid: groups[mid].append(row)
    state_path = safe_path(root, STATE)
    original_state = state_path.read_bytes() if state_path.exists() else None
    state = json.loads(original_state) if original_state else {'version': 1, 'pages': {}}
    if state.get('version') != 1 or not isinstance(state.get('pages'), dict): raise ValueError('invalid_state')
    owners = {}
    for mid, prior in state['pages'].items():
        identities = set(prior.get('source_pages', {}))
        if prior.get('notion_page_id'): identities.add(prior['notion_page_id'])
        for ident in identities:
            if ident in owners and owners[ident] != mid: raise ValueError('invalid_state')
            owners[ident] = mid
    def persist(candidate):
        nonlocal state, original_state
        if (state_path.read_bytes() if state_path.exists() else None) != original_state:
            raise ValueError('state_changed')
        raw = encoded(candidate)
        atomic_write(state_path, raw)
        state, original_state = candidate, raw
    report = {'notion_pages': len(rows), 'flomo_ids': len(groups),
              'flomo_source_pages': sum(len(pages) for pages in groups.values()),
              'duplicate_ids': {mid: [p['id'] for p in pages] for mid, pages in groups.items() if len(pages) > 1},
              'notion_only': sorted(set(groups) - set(local)), 'local_only': sorted(set(local) - set(groups)),
              'local_duplicate_ids': {mid: paths for mid, paths in local.items() if len(paths) > 1},
              'local_no_id': no_id, 'attempted': 0, 'written': 0, 'unchanged': 0, 'pending': 0, 'held': identity_holds,
              'variant_groups': {}, 'unmerged_variant_groups': {}, 'stage_only': True,
              'fact_promotion_allowed': False}
    if proof_budget is not None:
        for ident, mid in owners.items():
            if ident not in page_ids or not any(page['id'] == ident for page in groups.get(mid, [])):
                report['held'][mid + '::' + ident] = 'source_identity_not_observed'
    # Fresh and changed input has priority over historical property-proof backfill.
    def is_backfill_page(mid, page):
        group = state['pages'].get(mid, {})
        prior = group.get('source_pages', {}).get(page['id'], group)
        return (prior.get('notion_page_id') == page['id']
                and prior.get('last_edited_time') == page.get('last_edited_time') and bool(prior.get('files'))
                and (prior.get('property_proof_version') != 1
                     or prior.get('metadata_fingerprint') == metadata_fingerprint(page)))
    def is_backfill(item):
        mid, pages = item
        return all(is_backfill_page(mid, page) for page in pages)
    ordered_groups = sorted(groups.items(), key=is_backfill) if proof_budget else groups.items()
    budget_reason = None
    errors = {'stage_edited', 'source_changed_during_read', 'state_changed', 'source_identity_changed',
              'incomplete_asset', 'incomplete_relation', 'unsupported_block', 'block_cycle',
              'asset_file_budget', 'asset_batch_bound', 'potential_credential_in_source', 'incomplete_property_proof'}
    for mid, pages in ordered_groups:
        if proof_budget is not None: pages = sorted(pages, key=lambda page: is_backfill_page(mid, page))
        group = state['pages'].get(mid, {})
        grouped = len(pages) > 1 or 'source_pages' in group or bool(group.get('notion_page_id')
                    and group['notion_page_id'] != pages[0]['id'])
        if group and any(not safe_path(root / STAGE, rel).is_file()
                or digest(safe_path(root / STAGE, rel).read_bytes()) != sha
                for rel, sha in prior_files(group).items()):
            report['held'][mid] = 'stage_edited'
            continue
        for page in pages:
            ident = page['id']
            failure_key = mid + '::' + ident if grouped else mid
            group = state['pages'].get(mid, {})
            sources = dict(group.get('source_pages', {}))
            if grouped and group.get('notion_page_id') and group['notion_page_id'] not in sources:
                sources[group['notion_page_id']] = {key: deepcopy(value) for key, value in group.items()
                                                   if key not in {'source_pages', 'variant_indexes', 'current_variant_index'}}
            prior = sources.get(ident, {}) if grouped else group
            if (ident in owners and owners[ident] != mid) or (prior and prior.get('notion_page_id') != ident):
                report['held'][failure_key] = 'source_identity_changed'
                continue
            if (prior.get('last_edited_time') == page.get('last_edited_time') and prior.get('files')
                    and (proof_budget is None or (prior.get('property_proof_version') == 1
                        and prior.get('metadata_fingerprint') == metadata_fingerprint(page)))):
                report['unchanged'] += 1
                continue
            if not apply or budget_reason or (max_pages and report['attempted'] >= max_pages):
                report['pending'] += 1
                continue
            report['attempted'] += 1
            try:
                property_capture = None
                if proof_budget is not None:
                    from notion_complete_properties import hydrate_page
                    proof_budget.page()
                    before = notion.notion_request(api_key, 'GET', '/pages/' + ident)
                    if (before.get('id') != ident or before.get('parent', {}).get('database_id', '').replace('-', '')
                            != database_id.replace('-', '') or memo_id(before) != mid):
                        raise ValueError('source_identity_changed')
                    if (before.get('last_edited_time') != page.get('last_edited_time')
                            or before.get('created_time') != page.get('created_time')
                            or metadata_fingerprint(before) != metadata_fingerprint(page)):
                        raise ValueError('source_changed_during_read')
                    property_capture = hydrate_page(before,
                        lambda method, path, payload=None: notion.notion_request(api_key, method, path, payload))
                    complete = property_capture['hydrated_page']
                    reject_credentials(encoded(property_capture))
                else: complete = checked_page(api_key, page)
                blocks = block_tree(api_key, ident)
                reject_credentials(encoded({'page': complete, 'blocks': blocks}))
                files = {}
                material = materialize_assets({'page': complete, 'blocks': blocks}, files, downloader)
                files[mid + '.md'] = render_record(material['page'], material['blocks'], mid, local.get(mid, []),
                                                 include_text_properties=property_capture is not None)
                reject_credentials(files[mid + '.md'])
                snapshot = {'page': complete, 'blocks': blocks, 'materialized': material}
                if property_capture is not None: snapshot['property_capture'] = property_capture
                files[mid + '.json'] = encoded(snapshot)
                latest = notion.notion_request(api_key, 'GET', '/pages/' + ident)
                if (latest.get('last_edited_time') != page.get('last_edited_time')
                        or (proof_budget is not None and (latest.get('id') != ident
                            or memo_id(latest) != mid
                            or latest.get('created_time') != before.get('created_time')
                            or latest.get('parent') != before.get('parent')
                            or metadata_fingerprint(latest) != metadata_fingerprint(before)))):
                    raise ValueError('source_changed_during_read')
                if (state_path.read_bytes() if state_path.exists() else None) != original_state:
                    raise ValueError('state_changed')
                if proof_budget is not None: proof_budget.check()
                version, hashes = apply_record(root, mid, files, prior)
                body_sha, properties_sha = source_fingerprints(complete, blocks)
                receipt = {'notion_page_id': ident, 'last_edited_time': page.get('last_edited_time'),
                           'current_version': version, 'files': hashes,
                           'versions': {**prior.get('versions', {}), version: hashes},
                           'body_content_sha256': body_sha, 'properties_content_sha256': properties_sha}
                if property_capture is not None:
                    receipt.update(property_proof_version=1, metadata_fingerprint=metadata_fingerprint(before))
                candidate = deepcopy(state)
                if grouped:
                    sources[ident] = receipt
                    candidate['pages'][mid] = {**group, 'source_pages': sources}
                else: candidate['pages'][mid] = receipt
                persist(candidate)
                owners[ident] = mid
                report['written'] += 1
            except Exception as exc:
                from notion_source_budget import BudgetExceeded
                if proof_budget is not None and isinstance(exc, BudgetExceeded):
                    budget_reason = str(exc)
                    report['pending'] += 1
                    continue
                code = str(exc)
                if proof_budget is not None and code.startswith('property_'): code = 'incomplete_property_proof'
                report['held'][failure_key] = code if code in errors else 'source_or_stage_failed'
        if grouped:
            group = state['pages'].get(mid, {})
            sources = group.get('source_pages', {})
            observed = sorted(page['id'] for page in pages)
            if sources:
                try:
                    fingerprints = {ident: saved_fingerprints(root, mid, record) for ident, record in sources.items()}
                    bodies = len({pair[0] for pair in fingerprints.values()})
                    properties = len({pair[1] for pair in fingerprints.values()})
                    info = {'source_page_ids': sorted(sources), 'observed_source_page_ids': observed,
                            'body_variant_count': bodies, 'properties_variant_count': properties,
                            'capture_complete': all(ident in sources and sources[ident]['last_edited_time'] == page['last_edited_time']
                                and (proof_budget is None or (sources[ident].get('property_proof_version') == 1
                                    and sources[ident].get('metadata_fingerprint') == metadata_fingerprint(page)))
                                for ident, page in ((page['id'], page) for page in pages))}
                    report['variant_groups'][mid] = info
                    if bodies > 1 or properties > 1: report['unmerged_variant_groups'][mid] = info
                    if apply:
                        relative, hashes = variant_index(root, mid, group, observed)
                        candidate = deepcopy(state)
                        candidate['pages'][mid] = {**group, 'current_variant_index': relative,
                            'variant_indexes': {**group.get('variant_indexes', {}), digest(encoded(hashes)): hashes}}
                        if candidate != state: persist(candidate)
                except Exception as exc:
                    code = str(exc)
                    report['held'][mid + '::variant_index'] = code if code in errors else 'source_or_stage_failed'
    report['capture_complete'] = not report['held'] and not report['pending']
    if proof_budget is not None:
        report.update(property_proof_complete=report['capture_complete'], inventory_complete=True,
                      budget_reason=budget_reason, requests=proof_budget.requests)
    report['complete'] = report['capture_complete']
    report['unmerged_variant_group_count'] = len(report['unmerged_variant_groups'])
    report['semantic_merge_complete'] = report['capture_complete'] and not report['unmerged_variant_groups']
    if apply: atomic_write(safe_path(root, STAGE / 'coverage.json'), encoded(report))
    return report


def verify_remote_state(root):
    private_checkout.verify_private_checkout(root, os.environ.get('PRIVATE_VAULT_TOKEN'))
    state = safe_path(root, STATE)
    import flomo_original_source as original
    supplement = safe_path(root / STAGE, original.STATE)
    import flomo_cloud_current as current
    if not state.exists() and not supplement.exists() and not (root / current.STATE).exists(): return False
    raw = state.read_bytes() if state.exists() else None
    expected = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() if raw is not None else None
    def git(*args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                text=True, timeout=60)
        if result.returncode: raise ValueError('private_state_readback_failed')
        return result.stdout.strip()
    git('fetch', 'origin', 'main')
    if raw is not None and (git('rev-parse', 'HEAD:' + str(STATE)) != expected or git('rev-parse', 'origin/main:' + str(STATE)) != expected):
        raise ValueError('private_state_readback_failed')
    if git('rev-parse', 'HEAD:' + str(STAGE)) != git('rev-parse', 'origin/main:' + str(STAGE)):
        raise ValueError('private_state_readback_failed')
    tree = {}
    for row in git('ls-tree', '-rz', 'HEAD', '--', str(STAGE)).split('\0'):
        if not row: continue
        metadata, path = row.split('\t', 1)
        tree[path] = metadata.split()[2]
    for record in (json.loads(raw)['pages'].values() if raw is not None else []):
        for relative, expected_sha in prior_files(record).items():
            path = safe_path(root, STAGE / relative)
            data = path.read_bytes()
            blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if digest(data) != expected_sha or tree.get(str(STAGE / relative)) != blob:
                raise ValueError('private_state_readback_failed')
    # Supplement state is separate from Notion capture state but shares publication.
    if supplement.exists():
        data = supplement.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if tree.get(str(STAGE / original.STATE)) != blob:
            raise ValueError('private_state_readback_failed')
        for record in json.loads(data)['pages'].values():
            for relative, expected_sha in prior_files(record).items():
                content = safe_path(root / STAGE, relative).read_bytes()
                content_blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
                if digest(content) != expected_sha or tree.get(str(STAGE / relative)) != content_blob:
                    raise ValueError('private_state_readback_failed')
        if supplement.read_bytes() != data: raise ValueError('state_changed')
    current.verify_remote(root, git)
    if (state.read_bytes() if state.exists() else None) != raw: raise ValueError('state_changed')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-id', default=os.environ.get('NOTION_FLOMO_DATABASE_ID'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--verify-remote-state', action='store_true')
    parser.add_argument('--max-pages', type=int, default=50)
    args = parser.parse_args()
    config = {}
    if args.config:
        config = json.loads(args.config.read_bytes())
        if config.get('version') != 1 or not isinstance(config.get('database_id'), str):
            parser.error('invalid private source configuration')
        if any(type(config.get(key, False)) is not bool for key in ('original_source_enabled','current_export_enabled','property_proof_enabled')):
            parser.error('invalid original source configuration')
        args.database_id = config['database_id']
    if args.verify_remote_state:
        if not os.environ.get('VAULT_ROOT'): parser.error('VAULT_ROOT required')
        verified = verify_remote_state(Path(os.environ['VAULT_ROOT']))
        print('Private flomo state and content readback verified.' if verified else 'No flomo object state to verify.')
        return 0
    if not args.database_id or not os.environ.get('VAULT_ROOT'): parser.error('database ID and VAULT_ROOT required')
    result = run(Path(os.environ['VAULT_ROOT']), args.database_id, notion.load_notion_api_key(),
                 apply=args.apply, max_pages=args.max_pages,
                 original_source_enabled=config.get('original_source_enabled', False),
                 original_signing_suffix=os.environ.get('FLOMO_WEB_SIGNING_SUFFIX'),
                 current_export_enabled=config.get('current_export_enabled', False),
                 property_proof_enabled=config.get('property_proof_enabled', False),
                 property_proof_batch=config.get('property_proof_batch'))
    print(json.dumps({key: result[key] for key in ('notion_pages', 'flomo_ids', 'flomo_source_pages', 'written', 'unchanged', 'pending',
        'complete', 'capture_complete', 'unmerged_variant_group_count', 'semantic_merge_complete')}))
    return 0 if result['complete'] or not args.apply else 2


if __name__ == '__main__': raise SystemExit(main())
