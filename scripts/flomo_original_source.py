"""Read-only original flomo enrichment for the existing private Notion-stage writer.

No login, refresh, rotation, source writes, independent scheduling, or Raw/01 writes.
Access and signing material are supplied by environment, never tracked config.
"""
from __future__ import annotations
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


def enrich(root, bridge, *, token, signing_suffix, max_pages, downloader, reader=request_page):
    """Called only under the existing verified private writer lock."""
    base = root / bridge.STAGE
    state_path = bridge.safe_path(base, STATE)
    original = state_path.read_bytes() if state_path.exists() else None
    state = json.loads(original) if original else {'version': 1, 'pages': {}}
    if state.get('version') != 1 or not isinstance(state.get('pages'), dict):
        raise OriginalError('original_invalid_state')
    report = {'enabled': True, 'source_complete': False, 'capture_complete': False, 'media_complete': False,
              'complete': False, 'written': 0, 'unchanged': 0, 'pending': 0,
              'held': {}, 'degraded': {}, 'stage_only': True, 'fact_promotion_allowed': False}
    def save_report(): bridge.atomic_write(bridge.safe_path(base, COVERAGE), bridge.encoded(report))
    if not isinstance(token, str) or not token.strip() or not isinstance(signing_suffix, str) or not signing_suffix:
        report['held']['source'] = 'original_missing_access_credential' if not isinstance(token, str) or not token.strip() else 'original_missing_signing_configuration'
        save_report(); return report
    token = token.strip()
    if token.lower().startswith('bearer '): token = token[7:].strip()
    try: memos, proofs = inventory(token, signing_suffix, reader)
    except Exception as exc:
        report['held']['source'] = str(exc) if isinstance(exc, OriginalError) else 'original_source_failed'
        save_report(); return report
    # Metadata proofs are private, but even a malicious memo ID must not leak a credential.
    try: bridge.reject_credentials(bridge.encoded(proofs))
    except ValueError:
        report['held']['source'] = 'potential_credential_in_source'; save_report(); return report
    report.update({'inventory_complete': True, 'source_memos': len(memos), 'pagination': proofs,
                   'retained_prior_not_observed': sorted(set(state['pages']) - set(memos))})
    notion_state_path = bridge.safe_path(root, bridge.STATE)
    notion_state = json.loads(notion_state_path.read_bytes()) if notion_state_path.exists() else {'pages': {}}
    attempted = 0
    for mid, memo in memos.items():
        prior = state['pages'].get(mid, {})
        if any(not bridge.safe_path(base, rel).is_file() or
               bridge.digest(bridge.safe_path(base, rel).read_bytes()) != sha
               for rel, sha in bridge.prior_files(prior).items()):
            report['held'][mid + '::retained_history'] = 'stage_edited'
            continue
        linked = notion_state['pages'].get(mid, {})
        page_ids = sorted(set(linked.get('source_pages', {})) | ({linked['notion_page_id']} if linked.get('notion_page_id') else set()))
        units = page_ids or ['upstream-only']
        source_hash = bridge.digest(bridge.encoded(memo))
        for ident in units:
            key = mid + '::' + ident
            record = prior.get('source_pages', {}).get(ident, {})
            try:
                for rel, sha in bridge.prior_files(record).items():
                    path = bridge.safe_path(base, rel)
                    if not path.is_file() or bridge.digest(path.read_bytes()) != sha:
                        raise OriginalError('stage_edited')
                if record.get('source_sha256') == source_hash and not record.get('degraded'):
                    report['unchanged'] += 1; continue
                if max_pages and attempted >= max_pages:
                    report['pending'] += 1; continue
                attempted += 1
                raw = bridge.encoded(memo); bridge.reject_credentials(raw)
                # Check before saving original HTML, JSON, links or assets.
                bridge.reject_credentials(html.unescape(memo['content']).encode())
                files = {'original.json': raw, 'original.html': memo['content'].encode()}
                assets, degraded, size = [], [], 0
                for url in image_urls(memo):
                    try:
                        data = downloader(url)
                        if len(data) > bridge.MAX_ASSET_BYTES: raise OriginalError('asset_file_budget')
                        size += len(data)
                        if size > 200 * 1024 * 1024: raise OriginalError('asset_batch_bound')
                        name = 'assets/' + bridge.digest(data)
                        files[name] = data; assets.append({'source_url': url, 'path': name, 'bytes': len(data)})
                    except urllib.error.HTTPError as exc:
                        degraded.append({'source_url': url, 'reason': 'asset_http_' + str(exc.code)})
                    except Exception as exc:
                        reason = str(exc)
                        degraded.append({'source_url': url, 'reason': reason if reason in {
                            'unsafe_asset_url', 'unsafe_asset_address', 'asset_file_budget', 'asset_batch_bound',
                            'incomplete_asset'} else 'asset_read_failed'})
                metadata = {'memo_id': mid, 'notion_page_id': None if ident == 'upstream-only' else ident,
                            'created_at': memo['created_at'], 'updated_at': memo['updated_at'],
                            'source_timezone': 'Asia/Shanghai', 'assets': assets, 'degraded': degraded,
                            'stage_only': True, 'fact_promotion_allowed': False}
                files['metadata.json'] = bridge.encoded(metadata)
                lines = ['---', 'stage_only: true', 'fact_promotion_allowed: false', '---', '',
                         '# flomo original source', '', '[Original HTML](original.html)',
                         '[Original JSON](original.json)', '[Source times and media status](metadata.json)', '']
                lines += ['![](' + asset['path'] + ')' for asset in assets]
                files['index.md'] = ('\n'.join(lines) + '\n').encode()
                for name, data in files.items():
                    if not name.startswith('assets/'): bridge.reject_credentials(data)
                version = bridge.digest(bridge.encoded({name: bridge.digest(data) for name, data in files.items()}))
                paths = {str(AREA / 'versions' / mid / ident / version / name): data for name, data in files.items()}
                hashes = {rel: bridge.digest(data) for rel, data in paths.items()}
                if (state_path.read_bytes() if state_path.exists() else None) != original:
                    raise OriginalError('state_changed')
                for rel, data in paths.items(): bridge.immutable_write(bridge.safe_path(base, rel), data)
                receipt = {'source_sha256': source_hash, 'current_version': version, 'files': hashes,
                           'versions': {**record.get('versions', {}), version: hashes},
                           'degraded': [item['reason'] for item in degraded]}
                candidate = json.loads(json.dumps(state))
                candidate['pages'].setdefault(mid, {}).setdefault('source_pages', {})[ident] = receipt
                if (state_path.read_bytes() if state_path.exists() else None) != original:
                    raise OriginalError('state_changed')
                candidate_raw = bridge.encoded(candidate)
                bridge.atomic_write(state_path, candidate_raw)
                original, state = candidate_raw, candidate
                prior = state['pages'][mid]
                report['written'] += 1
                if degraded: report['degraded'][key] = [item['reason'] for item in degraded]
            except Exception as exc:
                code = str(exc)
                report['held'][key] = code if code in {'potential_credential_in_source', 'stage_edited', 'state_changed'} or isinstance(exc, OriginalError) else 'original_object_failed'
    report['source_complete'] = not report['held'] and not report['pending']
    report['capture_complete'] = report['source_complete']
    report['media_complete'] = not report['degraded'] and report['source_complete']
    report['complete'] = report['source_complete'] and report['media_complete']
    save_report()
    return report
