#!/usr/bin/env python3
"""Bounded two-pass migration in a real private Get seed; aggregate output only."""
import argparse
import concurrent.futures
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import threading
import time

import source_sync_assets as assets
import source_sync_inventory as inventory
import source_sync_migration_check as migration
import source_sync_pack_seed as pack
from source_sync_sample import detail_id, FixedSampleError
import sync_getnote_to_raw as sync
from source_sync_store import IdentityIndex, json_text, write_if_changed
from verify_shadow_repeat import snapshot

MIB = 1024 * 1024
MAX_NOTES = 2500  # 1932 observed; ~29% bounded growth headroom.
MAX_PAGES = 150   # 97 observed at 20/page; no completeness from cap alone.
MAX_RESPONSE = 2 * MIB
MAX_API_BYTES = 256 * MIB  # Per pass, includes list, detail and retried bodies.
MAX_ASSETS = 256
MAX_ASSET_BYTES = 200 * MIB  # Per pass, includes failed/retried body bytes.
MAX_FILE = 25 * MIB
SEGMENT = 64
STAGES = ('snapshot', 'seed', 'list', 'detail', 'assets', 'write', 'preservation', 'compare')
ERRORS = ('limit', 'source_failed', 'identity', 'unsupported_assets', 'asset_failed',
          'old_path_changed', 'old_file_changed', 'source_changed', 'repeat_changed',
          'transcript_pending', 'list_detail_mismatch', 'unexpected', 'interrupted')
API_ERRORS = ('api_auth', 'api_rate_limited', 'api_transport', 'api_rejected',
              'api_redirect', 'response_limit', 'response_invalid')
ERRORS += API_ERRORS
FAILURE_KINDS = ('value_error', 'type_error', 'attribute_error', 'key_error',
                 'os_error', 'system_exit', 'other')
FLAGS = ('real_seed_verified', 'enumeration_complete', 'old_paths_preserved',
         'old_unselected_files_preserved', 'binary_readback_exact',
         'source_id_sets_equal', 'hashes_and_mtimes_equal')
COUNTS = ('seed_files', 'seed_bytes', 'actual_markdown', 'matched_manifest_api_paths',
          'preserved_old_files', 'unselected_old_markdown', 'changed_second_files')
PASS_COUNTS = ('pages', 'source_notes', 'details_fetched', 'api_response_bytes',
               'downloaded_assets', 'downloaded_bytes', 'asset_transfer_bytes',
               'remote_only_assets', 'written_or_changed', 'attachment_urls_reused',
               'pending', 'segments_completed', 'elapsed_seconds')


class ReplayFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class BytePool:
    """Reserve complete per-call allowances before concurrent requests.

    ResponseBudget's one-byte overflow sentinel is reserved too. Retried partial
    bodies consume the same reservation, so concurrency cannot exceed the pool.
    """
    def __init__(self, maximum, used=0):
        self.maximum, self.used, self.reserved = maximum, used, 0
        self.lock = threading.Lock()

    @contextlib.contextmanager
    def request(self):
        with self.lock:
            amount = min(MAX_RESPONSE + 1, self.maximum - self.used - self.reserved)
            if amount < 2:
                raise ReplayFailure('limit')
            self.reserved += amount
        budget = inventory.ResponseBudget(amount - 1)
        try:
            yield budget
        finally:
            with self.lock:
                self.used += budget.used
                self.reserved -= amount
                if self.used > self.maximum:
                    raise ReplayFailure('limit')


class TransferBudget:
    def __init__(self, maximum):
        self.maximum, self.used = maximum, 0

    def read_limit(self, maximum):
        left = self.maximum - self.used
        if left <= 0:
            raise ReplayFailure('limit')
        return min(maximum, left)

    def consume(self, amount):
        self.used += amount
        if self.used > self.maximum:
            raise ReplayFailure('limit')


@contextlib.contextmanager
def sync_paths(root):
    names = ('ROOT', 'RAW_DIR', 'NOTES_DIR', 'INDEX_DIR', 'STATE_DIR', 'VICKY_INDEX_PATH')
    before = {n: getattr(sync, n) for n in names}
    sync.ROOT, sync.RAW_DIR = root.parents[1], root
    sync.NOTES_DIR, sync.INDEX_DIR = root/'01 Notes', root/'02 Index'
    sync.STATE_DIR = root/'03 State'
    sync.VICKY_INDEX_PATH = sync.INDEX_DIR/'给Vicky的新增入口.md'
    try:
        yield
    finally:
        for name, value in before.items():
            setattr(sync, name, value)


def enumerate_ids(key, client, budget, report, versions=None):
    ids = []
    for page, batch, more in sync.fetch_note_pages(key, client, max_pages=MAX_PAGES,
            max_response_bytes=MAX_RESPONSE, response_budget=budget):
        report['pages'] = page
        for value in batch:
            ident = detail_id(value)
            if not re.fullmatch(r'[0-9]{1,64}', ident):
                raise ReplayFailure('identity')
            if versions is not None:
                updated = value.get('updated_at')
                if not isinstance(updated, str) or not updated.strip():
                    raise ReplayFailure('list_detail_mismatch')
                versions[ident] = updated
            ids.append(ident)
            if len(ids) > MAX_NOTES:
                raise ReplayFailure('limit')
    if not ids:
        raise ReplayFailure('identity')
    report['source_notes'] = len(ids)
    return sorted(ids)


def acquire_details(ids, key, client, pool, checkpoint, report, versions=None):
    checkpoint.mkdir(parents=True)
    def fetch(ident):
        with pool.request() as budget:
            payload = sync.api_get(sync.DETAIL_PATH, {'id': ident, 'image_quality': 'original'},
                key, client, max_response_bytes=MAX_RESPONSE, response_budget=budget)
        payload = payload.get('data', payload)
        note = payload.get('note', payload)
        if detail_id(note) != ident or not isinstance(note.get('content'), str):
            raise ReplayFailure('identity')
        if versions is not None and note.get('updated_at') != versions[ident]:
            raise ReplayFailure('list_detail_mismatch')
        # Production downloader supports attachments; don't silently claim others.
        if any(note.get(k) for k in ('images', 'files', 'resources')):
            raise ReplayFailure('unsupported_assets')
        return note
    notes = []
    for start in range(0, len(ids), SEGMENT):
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(fetch, ident) for ident in ids[start:start+SEGMENT]]
            try:
                for future in concurrent.futures.as_completed(futures):
                    note = future.result()
                    ident = sync.note_id_from(note)
                    # Private same-run checkpoint; never uploaded or cached externally.
                    write_if_changed(checkpoint/(hashlib.sha256(ident.encode()).hexdigest()+'.json'), json_text(note))
                    notes.append(note)
                    report['details_fetched'] = len(notes)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        report['segments_completed'] += 1
    return sorted(notes, key=sync.note_id_from)


def acquire_assets(root, notes, report, fetch=None):
    budget = TransferBudget(MAX_ASSET_BYTES)
    def bounded_fetch(url, max_bytes):
        return assets.fetch_asset(url, max_bytes=max_bytes, transfer_budget=budget)
    store = assets.AssetStore(root, fetch=fetch or bounded_fetch, max_file=MAX_FILE,
                              max_run=MAX_ASSET_BYTES, max_assets=MAX_ASSETS)
    by_id = {}
    try:
        for note in notes:
            items = store.materialize(note)
            by_id[sync.note_id_from(note)] = items
            report['remote_only_assets'] += sum(a['status'] == 'not-downloaded' for a in items)
        # Each distinct downloaded binary is read back, not merely an HTTP success.
        for item in store.memo.values():
            data = (root/item['path']).read_bytes()
            if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ReplayFailure('asset_failed')
    finally:
        report.update(downloaded_assets=len(store.memo), downloaded_bytes=store.bytes,
                      asset_transfer_bytes=budget.used)
    return by_id


def one_pass(root, checkpoint, key, client, report, stage, asset_fetch=None):
    started = time.monotonic()
    budget = inventory.ResponseBudget(64*MIB)
    pool = None
    try:
        stage('list')
        versions = {}
        ids = enumerate_ids(key, client, budget, report, versions)
        pool = BytePool(MAX_API_BYTES, budget.used)
        stage('detail')
        notes = acquire_details(ids, key, client, pool, checkpoint, report, versions)
        stage('assets')
        index = IdentityIndex(root, 'getnote', 'note_id')
        by_id = acquire_assets(root, notes, report, asset_fetch)
        stage('write')
        with sync_paths(root), contextlib.redirect_stdout(io.StringIO()) as captured:
            sync.persist_details(notes, by_id, index, scope='full')
        result = json.loads(captured.getvalue())
        for field in ('written_or_changed', 'attachment_urls_reused', 'pending'):
            report[field] = result[field]
        return set(ids), index
    finally:
        report['api_response_bytes'] = pool.used if pool else budget.used
        report['elapsed_seconds'] = int(time.monotonic() - started)


def preserved_files(root, before, old_index, ids):
    allowed = {str(p.relative_to(root)) for i, p in old_index.paths.items() if i in ids}
    for ident in ids:
        key = hashlib.sha256(ident.encode()).hexdigest()+'.json'
        allowed.update(('03 State/raw-cache/'+key, '03 State/attachments/'+key))
    allowed.update(('03 State/pending.json', '03 State/receipt.json'))
    unchanged = {p:v for p,v in before.items() if p not in allowed}
    current = snapshot(root)
    if any(current.get(p) != value for p, value in unchanged.items()):
        raise ReplayFailure('old_file_changed')
    return unchanged


def replay(root, work, key, client, report, asset_fetch=None):
    before = snapshot(root)
    old_index = IdentityIndex(root, 'getnote', 'note_id')
    prior_ids = None
    for number in (1, 2):
        part = dict.fromkeys(PASS_COUNTS, 0)
        report['passes'].append(part)
        ids, index = one_pass(root, work/('pass-'+str(number)), key, client, part,
                             lambda stage: report.update(stage=stage), asset_fetch)
        report['stage'] = 'preservation'
        if any(index.paths.get(i) != path for i, path in old_index.paths.items()):
            raise ReplayFailure('old_path_changed')
        kept = preserved_files(root, before, old_index, ids)
        report.update(old_paths_preserved=True, old_unselected_files_preserved=True,
            binary_readback_exact=True, preserved_old_files=len(kept),
            unselected_old_markdown=sum(p.endswith('.md') for p in kept))
        current = snapshot(root)
        if number == 1:
            first, prior_ids = current, ids
        else:
            report['stage'] = 'compare'
            if ids != prior_ids:
                raise ReplayFailure('source_changed')
            report['source_id_sets_equal'] = True
            changed = sum(first.get(p) != current.get(p) for p in first.keys() | current.keys())
            report['changed_second_files'] = changed
            if changed or part['written_or_changed']:
                raise ReplayFailure('repeat_changed')
            report.update(enumeration_complete=True, hashes_and_mtimes_equal=True)
    if any(p['pending'] for p in report['passes']):
        raise ReplayFailure('transcript_pending')
    report['status'] = 'full_replay_ok'


def failure_details(stage, error):
    """Classify failures without publishing exception text or upstream values.

    Reuse the sync CLI's fixed public codes. HTTP status is accepted only from
    its exact locally generated HTTP message, never parsed from a URL/body.
    No retry count is inferred: the existing API helper does not expose it.
    """
    if isinstance(error, ReplayFailure):
        return {'error_code': error.code}
    if isinstance(error, (inventory.SourceLimit, TimeoutError)):
        return {'error_code': 'limit'}
    if isinstance(error, assets.AssetError):
        return {'error_code': 'asset_failed'}
    if stage not in ('list', 'detail'):
        return {'error_code': 'source_failed'}
    if isinstance(error, FixedSampleError):
        return {'error_code': 'identity'}
    if isinstance(error, (json.JSONDecodeError, UnicodeError)):
        return {'error_code': 'response_invalid'}
    code = sync.safe_error_code(stage, error)
    result = {'error_code': code if code in API_ERRORS else 'source_failed'}
    if isinstance(error, RuntimeError):
        message = str(error)
        match = re.fullmatch(r'GetNote HTTP ([45][0-9]{2})', message)
        if match:
            result['http_status'] = int(match[1])
        elif message in ('invalid Get response length', 'incomplete Get response body',
                         'invalid Get list payload', 'invalid has_more',
                         'official cursor response missing has_more',
                         'empty page claims more records'):
            result['error_code'] = 'response_invalid'
    if result['error_code'] == 'source_failed':
        result['failure_kind'] = next((kind for cls, kind in (
            (ValueError, 'value_error'), (TypeError, 'type_error'),
            (AttributeError, 'attribute_error'), (KeyError, 'key_error'),
            (OSError, 'os_error'), (SystemExit, 'system_exit'))
            if isinstance(error, cls)), 'other')
    return result


def safe_report(value):
    out = {'scope':'real_baseline_full_replay', 'status':'failed', 'source_complete':False,
           'published':False}
    if not isinstance(value, dict):
        return out
    for field in COUNTS:
        if type(value.get(field)) is int and 0 <= value[field] <= 1024*MIB:
            out[field] = value[field]
    for field in FLAGS:
        if type(value.get(field)) is bool:
            out[field] = value[field]
    if value.get('stage') in STAGES:
        out['stage'] = value['stage']
    if value.get('error_code') in ERRORS:
        out['error_code'] = value['error_code']
    if (out.get('error_code') in ('api_auth', 'api_rate_limited', 'api_rejected')
            and type(value.get('http_status')) is int and 400 <= value['http_status'] <= 599):
        out['http_status'] = value['http_status']
    if out.get('error_code') == 'source_failed' and value.get('failure_kind') in FAILURE_KINDS:
        out['failure_kind'] = value['failure_kind']
    passes = value.get('passes')
    if isinstance(passes, list) and len(passes) <= 2:
        out['passes'] = [{k:p[k] for k in PASS_COUNTS if type(p.get(k)) is int
                         and 0 <= p[k] <= 1024*MIB} for p in passes if isinstance(p, dict)]
    valid = (len(out.get('passes', [])) == 2 and all(
        all(k in p for k in PASS_COUNTS) and 0 < p['source_notes'] <= MAX_NOTES
        and p['details_fetched'] == p['source_notes'] and 0 < p['pages'] <= MAX_PAGES
        and p['api_response_bytes'] <= MAX_API_BYTES and p['downloaded_assets'] <= MAX_ASSETS
        and p['asset_transfer_bytes'] <= MAX_ASSET_BYTES and p['pending'] == 0
        for p in out.get('passes', [])))
    if value.get('status') == 'full_replay_ok' and valid and all(out.get(k) is True for k in FLAGS):
        out.update(status='full_replay_ok', source_complete=True)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish-report', type=Path)
    args = parser.parse_args()
    if args.publish_report:
        try:
            value = json.loads(args.publish_report.read_text())
        except (ValueError, OSError):
            value = None
        print(json.dumps(safe_report(value), indent=2))
        return 0
    report = {'passes': [], 'stage':'snapshot'}
    try:
        token, commit, blobs, manifest, mapping, meta = migration.private_snapshot()
        with tempfile.TemporaryDirectory(prefix='private-get-full-') as directory:
            work = Path(directory)
            root = work/'vault'/'Raw'/'03 Get'
            root.mkdir(parents=True, mode=0o700)
            report['stage'] = 'seed'
            seed_report = {}
            pack.seed(root, work, token, blobs, seed_report)
            pack.audit_actual(root, mapping, seed_report)
            report.update({k:seed_report[k] for k in COUNTS if k in seed_report})
            report['real_seed_verified'] = True
            key, client = sync.load_credentials()
            # SIGALRM interrupts the main thread; active workers may still unwind.
            # The outer 115min command timeout remains the true hard bound.
            with pack.deadline(110*60):
                replay(root, work, key, client, report)
    except (Exception, SystemExit) as error:
        report.update(failure_details(report.get('stage'), error))
    safe = safe_report(report)
    print(json.dumps(safe))
    return 0 if safe['status'] == 'full_replay_ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
