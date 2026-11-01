"""Read-only original flomo enrichment for the existing private Notion-stage writer.

No login, refresh, rotation, source writes, independent scheduling, or Raw/01 writes.
Access and signing material are supplied by environment, never tracked config.
"""
from __future__ import annotations
import base64
from copy import deepcopy
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

AREA = Path('original-source')
STATE = AREA / 'state.json'
COVERAGE = AREA / 'coverage.json'
LIMIT = 200
MAX_PAGES = 100
MAX_RESPONSE = 8 * 1024 * 1024
INPUT_FINGERPRINT_VERSION = 2
AUTH_CDN_HOSTS_V1 = frozenset(('static.flomoapp.com',))
AUTH_CDN_HOSTS = frozenset(('static.flomoapp.com', 'flomo.oss-cn-shanghai.aliyuncs.com'))
AUTH_QUERY_KEYS = frozenset(('Expires', 'Signature'))


class OriginalError(ValueError):
    pass


def request_page(token, signing_suffix, updated_at, slug):
    """The existing Obsidian client request contract, access token only."""
    params = {'limit': str(LIMIT), 'latest_updated_at': str(updated_at) if updated_at else '',
              'latest_slug': slug, 'tz': '8:0', 'timestamp': str(int(time.time())),
              'api_key': 'flomo_web', 'app_version': '4.0', 'platform': 'web', 'webp': '1'}
    canonical = '&'.join(f'{key}={params[key]}' for key in sorted(params) if params[key] != '')
    params['sign'] = hashlib.md5((canonical + signing_suffix).encode()).hexdigest()
    request = urllib.request.Request('https://flomoapp.com/api/v1/memo/updated/?' +
        urllib.parse.urlencode(params), headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json', 'Origin': 'https://v.flomoapp.com',
        'Referer': 'https://v.flomoapp.com/', 'device-id': '503b6439-1884-443d-b04e-0828bf9f138f',
        'device-model': 'Chrome', 'platform': 'Web', 'User-Agent': 'Mozilla/5.0'})
    # Account credentials never follow redirects, including redirects to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE + 1)
            length = response.headers.get('Content-Length')
            if response.status != 200 or len(raw) > MAX_RESPONSE or (length and int(length) != len(raw)):
                raise OriginalError('original_incomplete_response')
            value = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise OriginalError('original_access_denied' if exc.code in (401, 403) else
                            'original_rate_limited' if exc.code == 429 else 'original_http_failed') from None
    except (OSError, ValueError):
        raise OriginalError('original_incomplete_response') from None
    if not isinstance(value, dict) or value.get('code') != 0:
        raise OriginalError('original_api_rejected')
    if not isinstance(value.get('data'), list): raise OriginalError('original_incomplete_response')
    return value['data']


def timestamp(value):
    if not isinstance(value, str): raise OriginalError('original_invalid_timestamp')
    try:
        parsed = dt.datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=dt.timezone(dt.timedelta(hours=8)))
    except ValueError: raise OriginalError('original_invalid_timestamp') from None
    return int(parsed.timestamp())


def inventory(token, signing_suffix, reader=request_page):
    """Bounded complete scan; never advance a durable watermark on a partial scan."""
    result, pages, cursors = {}, [], set()
    updated, slug = 0, ''
    for _ in range(MAX_PAGES):
        rows = reader(token, signing_suffix, updated, slug)
        if not isinstance(rows, list) or len(rows) > LIMIT: raise OriginalError('original_invalid_page')
        ids = []
        for memo in rows:
            if not isinstance(memo, dict): raise OriginalError('original_invalid_memo')
            mid = memo.get('slug')
            if not isinstance(mid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', mid):
                raise OriginalError('original_invalid_identity')
            if mid in result: raise OriginalError('original_duplicate_identity')
            timestamp(memo.get('created_at')); timestamp(memo.get('updated_at'))
            if not isinstance(memo.get('content'), str) or not isinstance(memo.get('files'), list):
                raise OriginalError('original_invalid_memo')
            result[mid] = memo; ids.append(mid)
        pages.append({'count': len(rows), 'memo_ids': ids, 'terminal': len(rows) < LIMIT})
        if len(rows) < LIMIT: return result, pages
        next_cursor = (timestamp(rows[-1]['updated_at']), rows[-1]['slug'])
        if next_cursor in cursors or next_cursor[0] < updated:
            raise OriginalError('original_invalid_cursor')
        cursors.add(next_cursor); updated, slug = next_cursor
    raise OriginalError('original_page_budget')


class Images(HTMLParser):
    def __init__(self): super().__init__(); self.urls = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'img':
            self.urls.extend(value for key, value in attrs if key.lower() == 'src' and value)


def image_urls(memo):
    parser = Images(); parser.feed(memo['content'])
    urls = parser.urls
    # Some original memos contain an ordinary image URL instead of an img tag.
    for value in re.findall(r'https?://[^\s<>"\x27]+', html.unescape(memo['content'])):
        value = value.rstrip(').,;')
        if re.search(r'\.(?:png|jpe?g|gif|webp|avif|heic)(?:[?#]|$)', value, re.I): urls.append(value)
    for item in memo['files']:
        if not isinstance(item, dict) or not isinstance(item.get('url'), str):
            raise OriginalError('original_invalid_files')
        urls.append(item['url'])
    return list(dict.fromkeys(html.unescape(value) for value in urls))


def fingerprint_hosts(version):
    if type(version) is not int or version not in (1, INPUT_FINGERPRINT_VERSION):
        raise OriginalError('unsupported_input_fingerprint_version')
    return AUTH_CDN_HOSTS_V1 if version == 1 else AUTH_CDN_HOSTS


def stable_asset_url(value, *, fingerprint_version=INPUT_FINGERPRINT_VERSION):
    """Remove only proved authentication fields; preserve all other URL bytes."""
    hosts = fingerprint_hosts(fingerprint_version)
    if not isinstance(value, str): return value
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in hosts
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return value
    except ValueError: return value
    segments = parsed.query.split('&'); fields = {}
    for segment in segments:
        name, separator, raw = segment.partition('=')
        fields.setdefault(name, []).append((separator, raw))
    for name in ('Expires', 'Signature', 'OSSAccessKeyId'):
        if len(fields.get(name, [])) != 1 or fields[name][0][0] != '=' or not fields[name][0][1]: return value
    expires = fields['Expires'][0][1]
    if not re.fullmatch(r'[1-9][0-9]{0,19}', expires): return value
    signature = urllib.parse.unquote(fields['Signature'][0][1])
    try:
        decoded = base64.b64decode(signature, validate=True)
        if len(decoded) != 20 or base64.b64encode(decoded).decode() != signature: return value
    except (ValueError, TypeError): return value
    # Rebuilding unknown query pairs with urlencode would normalize data too.
    # Keep their original spelling, encoding, values and ordering byte-for-byte.
    question = value.find('?'); fragment = value.find('#', question)
    tail = value[fragment:] if fragment >= 0 else ''
    kept = '&'.join(part for part in segments if part.partition('=')[0] not in AUTH_QUERY_KEYS)
    return value[:question] + ('?' + kept if kept else '') + tail


def input_fingerprint(memo, bridge, *, fingerprint_version=INPUT_FINGERPRINT_VERSION):
    fingerprint_hosts(fingerprint_version)
    projection = deepcopy(memo)
    for item in projection.get('files', []):
        if not isinstance(item, dict): continue
        for field in ('url', 'thumbnail_url'):
            if field in item: item[field] = stable_asset_url(item[field], fingerprint_version=fingerprint_version)
    return bridge.digest(bridge.encoded(projection))


def cached_source(base, bridge, mid, ident, record):
    """Prove old source and every declared byte before using it as comparison."""
    for relative, expected in bridge.prior_files(record).items():
        path = bridge.safe_path(base, relative)
        if not path.is_file() or bridge.digest(path.read_bytes()) != expected: raise OriginalError('stage_edited')
    def item(suffix):
        names = [name for name in record['files'] if name.endswith(suffix)]
        if len(names) != 1: raise OriginalError('original_cache_incomplete')
        relative = names[0]; raw = bridge.safe_path(base, relative).read_bytes(); bridge.reject_credentials(raw)
        if bridge.digest(raw) != record['files'][relative]: raise OriginalError('stage_edited')
        return json.loads(raw), raw, Path(relative).parent
    memo, raw, prefix = item('/original.json')
    metadata, _, _ = item('/metadata.json')
    if (record.get('source_sha256') != bridge.digest(raw) or memo.get('slug') != mid
            or metadata.get('memo_id') != mid
            or metadata.get('notion_page_id') != (None if ident == 'upstream-only' else ident)):
        raise OriginalError('original_cache_identity')
    assets = {}
    for asset in metadata.get('assets', []):
        relative = str(prefix / asset['path']); path = bridge.safe_path(base, relative)
        expected = record['files'].get(relative)
        if not expected or not path.is_file(): raise OriginalError('original_cache_incomplete')
        content = path.read_bytes()
        if bridge.digest(content) != expected or len(content) != asset['bytes']: raise OriginalError('stage_edited')
        key = stable_asset_url(asset['source_url'])
        if key in assets and assets[key] != expected: raise OriginalError('original_cache_asset_ambiguous')
        assets[key] = expected
    if not record.get('degraded') and {stable_asset_url(url) for url in image_urls(memo)} != set(assets):
        raise OriginalError('original_cache_incomplete_assets')
    return memo, assets


def enrich(root, bridge, *, token, signing_suffix, max_pages, downloader, reader=request_page, budget=None):
    """One verified writer; data capture and asset validation have distinct limits."""
    from notion_source_budget import Budget, BudgetExceeded
    base = root / bridge.STAGE; state_path = bridge.safe_path(base, STATE)
    original = state_path.read_bytes() if state_path.exists() else None
    state = json.loads(original) if original else {'version': 1, 'pages': {}}
    if state.get('version') != 1 or not isinstance(state.get('pages'), dict): raise OriginalError('original_invalid_state')
    pending = state.get('asset_change_pending', [])
    cursor = state.get('asset_probe_cursor')
    if (not isinstance(pending, list) or any(not isinstance(key, list) or len(key) != 2
            or any(not isinstance(value, str) for value in key) for key in pending)
            or (cursor is not None and (not isinstance(cursor, list) or len(cursor) != 2
                or any(not isinstance(value, str) for value in cursor)))):
        raise OriginalError('original_invalid_state')
    pending = {tuple(key) for key in pending}
    budget = budget or Budget({'max_pages': 500, 'max_seconds': 300, 'max_requests': 100})
    report = {'enabled': True, 'source_complete': False, 'capture_complete': False, 'media_complete': False,
              'complete': False, 'written': 0, 'unchanged': 0, 'pending': 0, 'held': {}, 'degraded': {},
              'stable_input_unchanged': 0, 'asset_probe_verified': 0, 'asset_probe_changed': 0,
              'soft_exit': None, 'capture_attempted': 0, 'stage_only': True, 'fact_promotion_allowed': False}
    def save_report():
        report['requests'] = budget.requests
        bridge.atomic_write(bridge.safe_path(base, COVERAGE), bridge.encoded(report))
    def persist(candidate):
        nonlocal state, original
        if (state_path.read_bytes() if state_path.exists() else None) != original: raise OriginalError('state_changed')
        raw = bridge.encoded(candidate); bridge.atomic_write(state_path, raw); state, original = candidate, raw
    if not isinstance(token, str) or not token.strip() or not isinstance(signing_suffix, str) or not signing_suffix:
        report['held']['source'] = 'original_missing_access_credential' if not isinstance(token, str) or not token.strip() else 'original_missing_signing_configuration'
        save_report(); return report
    token = token.strip()
    if token.lower().startswith('bearer '): token = token[7:].strip()
    def read_page(*args):
        budget.request(); return reader(*args)
    try: memos, proofs = inventory(token, signing_suffix, read_page)
    except BudgetExceeded as exc:
        report['soft_exit'] = str(exc); save_report(); return report
    except Exception as exc:
        report['held']['source'] = str(exc) if isinstance(exc, OriginalError) else 'original_source_failed'
        save_report(); return report
    try: bridge.reject_credentials(bridge.encoded(proofs))
    except ValueError:
        report['held']['source'] = 'potential_credential_in_source'; save_report(); return report
    report.update({'inventory_complete': True, 'source_memos': len(memos), 'pagination': proofs,
                   'retained_prior_not_observed': sorted(set(state['pages']) - set(memos))})
    notion_path = bridge.safe_path(root, bridge.STATE)
    notion_state = json.loads(notion_path.read_bytes()) if notion_path.exists() else {'pages': {}}
    report['notion_without_original'] = sorted(set(notion_state['pages']) - set(memos))
    for missing in report['notion_without_original']: report['held'][missing + '::original-source'] = 'original_memo_not_observed'
    captures, probes = [], []
    def failure(key, exc):
        code = str(exc)
        if isinstance(exc, urllib.error.HTTPError): code = 'asset_probe_http_' + str(exc.code)
        elif isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)): code = 'asset_probe_transport_failed'
        report['held'][key] = code if isinstance(exc, (OriginalError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError)) or code in {'potential_credential_in_source', 'stage_edited', 'state_changed', 'asset_file_budget',
            'empty_source_asset', 'incomplete_asset', 'unsafe_asset_url', 'unsafe_asset_address'} else 'original_object_failed'
    # First classify real source changes, independently of incidental auth probes.
    for mid, memo in memos.items():
        prior = state['pages'].get(mid, {})
        linked = notion_state['pages'].get(mid, {})
        page_ids = sorted(set(linked.get('source_pages', {})) | ({linked['notion_page_id']} if linked.get('notion_page_id') else set()))
        try:
            budget.check(); raw = bridge.encoded(memo); bridge.reject_credentials(raw)
            bridge.reject_credentials(html.unescape(memo['content']).encode())
            for relative, checksum in bridge.prior_files(prior).items():
                path = bridge.safe_path(base, relative)
                if not path.is_file() or bridge.digest(path.read_bytes()) != checksum: raise OriginalError('retained_history_edited')
            source_hash = bridge.digest(raw); stable = input_fingerprint(memo, bridge)
        except BudgetExceeded as exc:
            report['soft_exit'] = str(exc); report['pending'] += len(page_ids or ['upstream-only']); continue
        except Exception as exc:
            if str(exc) == 'retained_history_edited':
                failure(mid + '::retained_history', OriginalError('stage_edited'))
            else:
                for ident in page_ids or ['upstream-only']: failure(mid + '::' + ident, exc)
            continue
        for ident in page_ids or ['upstream-only']:
            record = prior.get('source_pages', {}).get(ident, {})
            unit = {'mid': mid, 'ident': ident, 'key': mid + '::' + ident, 'memo': memo,
                    'record': record, 'raw': raw, 'source_hash': source_hash, 'stable': stable}
            try:
                if record.get('files'):
                    old_memo, old_assets = cached_source(base, bridge, mid, ident, record)
                    unit['old_assets'] = old_assets
                    old_stable = input_fingerprint(old_memo, bridge)
                    marker = record.get('input_fingerprint_version')
                    if marker is not None:
                        # Version 1 was issued before the second host was proved.
                        # Validate that exact algorithm against the verified raw
                        # snapshot, then compare inputs using the current rules.
                        expected = input_fingerprint(old_memo, bridge, fingerprint_version=marker)
                        if record.get('input_fingerprint') != expected:
                            raise OriginalError('original_cache_input_fingerprint')
                    if source_hash == record.get('source_sha256') and not record.get('degraded') and not old_assets and (mid, ident) not in pending:
                        report['unchanged'] += 1; continue
                    if stable == old_stable and not record.get('degraded'):
                        probes.append(unit); continue
                captures.append(unit)
            except Exception as exc: failure(unit['key'], exc)
    attempted = 0
    def download(url):
        budget.request(); content = downloader(url); budget.asset(content); return content
    def capture(unit, loaded=None):
        nonlocal attempted
        if max_pages and attempted >= max_pages: report['pending'] += 1; return False
        budget.check(); attempted += 1; report['capture_attempted'] = attempted
        mid, ident, memo = unit['mid'], unit['ident'], unit['memo']
        record = state['pages'].get(mid, {}).get('source_pages', {}).get(ident, unit['record'])
        files = {'original.json': unit['raw'], 'original.html': memo['content'].encode()}
        assets, degraded, size = [], [], 0
        for url in image_urls(memo):
            try:
                content = loaded[url] if loaded is not None and url in loaded else download(url)
                if len(content) > bridge.MAX_ASSET_BYTES: raise OriginalError('asset_file_budget')
                size += len(content)
                if size > 200 * 1024 * 1024: raise OriginalError('asset_batch_bound')
                name = 'assets/' + bridge.digest(content); files[name] = content
                assets.append({'source_url': url, 'path': name, 'bytes': len(content)})
            except BudgetExceeded: raise
            except urllib.error.HTTPError as exc: degraded.append({'source_url': url, 'reason': 'asset_http_' + str(exc.code)})
            except Exception as exc:
                code = str(exc); degraded.append({'source_url': url, 'reason': code if code in {
                    'unsafe_asset_url', 'unsafe_asset_address', 'asset_file_budget', 'asset_batch_bound', 'incomplete_asset'} else 'asset_read_failed'})
        metadata = {'memo_id': mid, 'notion_page_id': None if ident == 'upstream-only' else ident,
                    'created_at': memo['created_at'], 'updated_at': memo['updated_at'], 'source_timezone': 'Asia/Shanghai',
                    'assets': assets, 'degraded': degraded, 'stage_only': True, 'fact_promotion_allowed': False}
        files['metadata.json'] = bridge.encoded(metadata)
        lines = ['---', 'stage_only: true', 'fact_promotion_allowed: false', '---', '', '# flomo original source', '',
                 '[Original HTML](original.html)', '[Original JSON](original.json)', '[Source times and media status](metadata.json)', '']
        lines += ['![](' + asset['path'] + ')' for asset in assets]
        files['index.md'] = ('\n'.join(lines) + '\n').encode()
        for name, content in files.items():
            if not name.startswith('assets/'): bridge.reject_credentials(content)
        version = bridge.digest(bridge.encoded({name: bridge.digest(content) for name, content in files.items()}))
        paths = {str(AREA / 'versions' / mid / ident / version / name): content for name, content in files.items()}
        hashes = {name: bridge.digest(content) for name, content in paths.items()}
        if (state_path.read_bytes() if state_path.exists() else None) != original: raise OriginalError('state_changed')
        budget.check()
        for relative, content in paths.items(): bridge.immutable_write(bridge.safe_path(base, relative), content)
        receipt = {'source_sha256': unit['source_hash'], 'input_fingerprint_version': INPUT_FINGERPRINT_VERSION,
                   'input_fingerprint': unit['stable'], 'current_version': version, 'files': hashes,
                   'versions': {**record.get('versions', {}), version: hashes}, 'degraded': [item['reason'] for item in degraded]}
        candidate = deepcopy(state); candidate['pages'].setdefault(mid, {}).setdefault('source_pages', {})[ident] = receipt
        persist(candidate); pending.discard((mid, ident)); report['written'] += 1
        if degraded: report['degraded'][unit['key']] = [item['reason'] for item in degraded]
        return True
    def probe(unit):
        incoming = image_urls(unit['memo']); selected = {}
        for url in incoming: selected[stable_asset_url(url)] = url
        if set(selected) != set(unit['old_assets']): raise OriginalError('original_cache_incomplete_assets')
        loaded = {}; changed = False
        for stable_url, url in selected.items():
            content = download(url); changed = changed or bridge.digest(content) != unit['old_assets'][stable_url]
            for alias in incoming:
                if stable_asset_url(alias) == stable_url: loaded[alias] = content
        # A no-op must also remain safe against edits during the network read.
        cached_source(base, bridge, unit['mid'], unit['ident'], unit['record'])
        report['asset_probe_verified'] += 1
        if changed: report['asset_probe_changed'] += 1
        return changed, loaded
    retry = [dict(unit, probe_first=True) for unit in probes if (unit['mid'], unit['ident']) in pending]
    probes = [unit for unit in probes if (unit['mid'], unit['ident']) not in pending]
    captures.sort(key=lambda unit: (unit['mid'], unit['ident']) not in pending)
    captures = retry + captures
    for index, unit in enumerate(captures):
        try:
            loaded = None
            if unit.get('probe_first'):
                changed, loaded = probe(unit)
                if not changed:
                    pending.discard((unit['mid'], unit['ident'])); report['unchanged'] += 1
                    report['stable_input_unchanged'] += 1; continue
            capture(unit, loaded)
        except BudgetExceeded as exc:
            report['soft_exit'] = str(exc); report['pending'] += len(captures) - index; break
        except Exception as exc: failure(unit['key'], exc)
    # A small cursor applies only to bounded byte validation, not source capture.
    if probes:
        start = next((index for index, unit in enumerate(probes) if [unit['mid'], unit['ident']] == cursor), 0)
        probes = probes[start:] + probes[:start]
    next_cursor = None
    for index, unit in enumerate(probes):
        ident = (unit['mid'], unit['ident']); probe_start_requests = budget.requests
        try:
            changed, loaded = probe(unit)
            if changed:
                pending.add(ident)
                capture(unit, loaded)
            else:
                pending.discard(ident); report['unchanged'] += 1; report['stable_input_unchanged'] += 1
        except BudgetExceeded as exc:
            report['soft_exit'] = str(exc); report['pending'] += len(probes) - index
            following = probes[(index + 1) % len(probes)] if budget.requests > probe_start_requests else unit
            next_cursor = [following['mid'], following['ident']]; break
        except Exception as exc: failure(unit['key'], exc)
    candidate = deepcopy(state)
    if pending: candidate['asset_change_pending'] = [list(key) for key in sorted(pending)]
    else: candidate.pop('asset_change_pending', None)
    if next_cursor: candidate['asset_probe_cursor'] = next_cursor
    else: candidate.pop('asset_probe_cursor', None)
    try:
        if (state_path.read_bytes() if state_path.exists() else None) != original: raise OriginalError('state_changed')
        if candidate != state: persist(candidate)
    except Exception as exc: failure('state', exc)
    try: budget.check()
    except BudgetExceeded as exc: report['soft_exit'] = str(exc)
    report['source_complete'] = not report['held'] and not report['pending'] and not pending
    report['capture_complete'] = report['source_complete']
    report['media_complete'] = not report['degraded'] and report['source_complete']
    report['complete'] = report['source_complete'] and report['media_complete']
    save_report(); return report
