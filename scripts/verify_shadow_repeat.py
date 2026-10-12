#!/usr/bin/env python3
"""Run Get twice in a NEW private temporary shadow, expose counts only.

Never publish, import local credentials, or write to the production vault.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, parse_qs

from source_sync_sample import prepare_private_sample

STAGES = frozenset(('configuration', 'list', 'detail', 'validation', 'assets',
                    'write', 'receipt', 'transcript', 'launch', 'snapshot', 'compare', 'sample_seed'))
ERROR_CODES = frozenset(('missing_credentials', 'api_redirect', 'api_auth',
                         'api_rate_limited', 'api_rejected', 'api_transport',
                         'response_limit', 'sample_types_missing', 'attachment_limit',
                         'asset_failure', 'transcript_pending', 'invalid_report',
                         'unknown_failure', 'snapshot_failure', 'changed_between_runs',
                         'unexpected', 'fixed_sample_invalid', 'private_sample_failed', 'legacy_path_mismatch'))
COUNT_KEYS = ('sample_pages_read', 'details_fetched', 'assets_downloaded',
              'written_or_changed')
DETAIL_GROUPS = ('content', 'title', 'source_timestamps', 'audio_original',
                 'audio_transcript', 'audio_play_url', 'audio_other',
                 'attachments', 'audit_fields', 'other')
ATTACHMENT_FIELDS = ('attachments', 'images', 'files', 'resources')
ATTACHMENT_CHANGES = ('added', 'removed', 'reordered', 'invalid_shape', 'url_query_only',
                      'url_location', 'stable_id', 'stable_path', 'size', 'mime',
                      'name', 'updated_timestamp', 'unknown_changed_keys')
DESCRIPTOR_GROUPS = {
    'stable_id': ('id', 'file_id', 'attachment_id'),
    'stable_path': ('path', 'file_path', 'key'),
    'size': ('size', 'file_size', 'byte_size'),
    'mime': ('mime', 'mime_type', 'content_type', 'type'),
    'name': ('name', 'file_name', 'filename', 'title'),
    'updated_timestamp': ('updated_at', 'update_time', 'updated_time'),
}

# Only fixed, public signing parameter names can appear in diagnostic output.
# Unrecognized upstream names and every parameter value remain private.
QUERY_KEYS = frozenset(('OSSAccessKeyId', 'Expires', 'Signature', 'x-oss-security-token',
    'x-oss-signature', 'x-oss-date', 'x-oss-expires', 'x-oss-credential',
    'x-oss-signature-version', 'x-oss-process',
    'q-sign-algorithm', 'q-ak', 'q-sign-time', 'q-key-time', 'q-header-list',
    'q-url-param-list', 'q-signature',
    'X-Amz-Algorithm', 'X-Amz-Credential', 'X-Amz-Date', 'X-Amz-Expires',
    'X-Amz-SignedHeaders', 'X-Amz-Signature', 'X-Amz-Security-Token',
    'Key-Pair-Id', 'Policy', 'e', 'token', '_upt', 'auth_key'))
QUERY_COUNTS = ('unknown_keys', 'invalid_query', 'url', 'original_url', 'download_url',
                'expiry_unknown', 'expiry_expired', 'expiry_under_hour',
                'expiry_under_day', 'expiry_at_least_day')


def attachment_query_diagnostics(before, after, now=None):
    """Classify refreshed URLs without disclosing hosts, paths, names or values."""
    present, changed = {}, {}
    counts = dict.fromkeys(QUERY_COUNTS, 0)
    now = time.time() if now is None else now
    for field in ATTACHMENT_FIELDS:
        left, right = before.get(field) or [], after.get(field) or []
        if not isinstance(left, list) or not isinstance(right, list):
            continue
        for old, new in zip(left, right):
            if not isinstance(old, dict) or not isinstance(new, dict):
                continue
            for key in ('url', 'original_url', 'download_url'):
                a, b = old.get(key), new.get(key)
                if not query_only_change(a, b):
                    continue
                counts[key] += 1
                try:
                    qa, qb = [parse_qs(urlsplit(v).query, keep_blank_values=True,
                                       max_num_fields=64) for v in (a, b)]
                except ValueError:
                    counts['invalid_query'] += 1
                    continue
                for name in set(qa) | set(qb):
                    if name in QUERY_KEYS:
                        present[name] = present.get(name, 0) + 1
                        if qa.get(name) != qb.get(name):
                            changed[name] = changed.get(name, 0) + 1
                    else:
                        counts['unknown_keys'] += 1
                expiry = None
                for name in ('Expires', 'e', 'q-sign-time'):
                    values = qa.get(name, [])
                    if len(values) == 1:
                        value = values[0].split(';')[-1]
                        if value.isascii() and value.isdigit() and len(value) <= 12:
                            expiry = int(value)
                            break
                bucket = ('expiry_unknown' if expiry is None else
                          'expiry_expired' if expiry <= now else
                          'expiry_under_hour' if expiry - now < 3600 else
                          'expiry_under_day' if expiry - now < 86400 else
                          'expiry_at_least_day')
                counts[bucket] += 1
    return {'query_keys_present': present, 'query_keys_changed': changed,
            'query_refresh_counts': counts}


def query_only_change(old, new):
    if not isinstance(old, str) or not isinstance(new, str) or old == new:
        return False
    a, b = urlsplit(old), urlsplit(new)
    return (a.scheme == b.scheme == 'https' and
            (a.netloc, a.path, a.fragment) == (b.netloc, b.path, b.fragment)
            and a.query != b.query)


def attachment_changes(before, after):
    changes = dict.fromkeys(ATTACHMENT_CHANGES, 0)
    missing = object()
    def identity(value):
        for key in DESCRIPTOR_GROUPS['stable_id']:
            ident = value.get(key)
            if isinstance(ident, (str, int)) and not isinstance(ident, bool):
                return (key, str(ident))
        for key in ('original_url', 'url', 'download_url'):
            value_url = value.get(key)
            if isinstance(value_url, str):
                url = urlsplit(value_url)
                return ('url', url.scheme, url.netloc, url.path, url.fragment)
        return None
    for field in ATTACHMENT_FIELDS:
        left, right = before.get(field), after.get(field)
        if left == right:
            continue
        left = [] if left is None else left
        right = [] if right is None else right
        if (not isinstance(left, list) or not isinstance(right, list) or
                any(not isinstance(n, dict) for n in left + right)):
            changes['invalid_shape'] += 1
            continue
        changes['added'] += max(0, len(right) - len(left))
        changes['removed'] += max(0, len(left) - len(right))
        old_ids, new_ids = [identity(n) for n in left], [identity(n) for n in right]
        if (old_ids and None not in old_ids + new_ids and
                len(set(old_ids)) == len(old_ids) and len(set(new_ids)) == len(new_ids)
                and set(old_ids) == set(new_ids)):
            changes['reordered'] += int(old_ids != new_ids)
            by_id = dict(zip(new_ids, right))
            right = [by_id[ident] for ident in old_ids]
        for old, new in zip(left, right):
            old, new = old.copy(), new.copy()
            for key in ('url', 'original_url', 'download_url'):
                a, b = old.pop(key, missing), new.pop(key, missing)
                if a != b:
                    changes['url_query_only' if query_only_change(a, b) else 'url_location'] += 1
            for group, keys in DESCRIPTOR_GROUPS.items():
                pairs = [(old.pop(key, missing), new.pop(key, missing)) for key in keys]
                changes[group] += int(any(a != b for a, b in pairs))
            changes['unknown_changed_keys'] += sum(old.get(k, missing) != new.get(k, missing)
                                                   for k in set(old) | set(new))
    return changes


def private_detail_snapshot(root):
    """Keep two bounded raw responses in memory; never print keys or values."""
    directory = root / 'Raw' / '03 Get' / '03 State' / 'raw-cache'
    paths = list(directory.glob('*.json'))
    if len(paths) != 2:
        raise ValueError('invalid private snapshot')
    result = {}
    for path in paths:
        if path.is_symlink():
            raise ValueError('invalid private snapshot')
        with path.open('rb') as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError('invalid private snapshot')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('invalid private snapshot')
        result[path.name] = value
    return result


def detail_groups(note):
    rest = note.copy()
    audio = rest.pop('audio', None)
    audio = audio.copy() if isinstance(audio, dict) else {'invalid': audio}
    groups = {key: rest.pop(key, None) for key in ('content', 'title')}
    groups['source_timestamps'] = [rest.pop(k, None) for k in ('created_at', 'updated_at')]
    groups.update({'audio_' + k: audio.pop(k, None) for k in ('original', 'transcript', 'play_url')})
    groups['audio_other'] = audio
    groups['attachments'] = [rest.pop(k, None) for k in ('attachments', 'images', 'files', 'resources')]
    groups['audit_fields'] = [rest.pop(k, None) for k in ('fetched_at', 'synced_at', 'request_id')]
    groups['other'] = rest
    return groups


def private_detail_changes(before, after):
    """Expose fixed field-group counts, never private hashes, names, IDs or URLs."""
    if set(before) != set(after) or len(before) != 2:
        raise ValueError('invalid private snapshot')
    counts = dict.fromkeys(DETAIL_GROUPS, 0)
    attachment_counts = dict.fromkeys(ATTACHMENT_CHANGES, 0)
    query_only = 0
    query_diagnostics = {'query_keys_present': {}, 'query_keys_changed': {},
                         'query_refresh_counts': dict.fromkeys(QUERY_COUNTS, 0)}
    for key in before:
        left, right = detail_groups(before[key]), detail_groups(after[key])
        for group in DETAIL_GROUPS:
            counts[group] += int(left[group] != right[group])
        query_only += int(query_only_change(left['audio_play_url'], right['audio_play_url']))
        for group, count in attachment_changes(before[key], after[key]).items():
            attachment_counts[group] += count
        for group, values in attachment_query_diagnostics(before[key], after[key]).items():
            for name, count in values.items():
                query_diagnostics[group][name] = query_diagnostics[group].get(name, 0) + count
    return {'detail_change_counts': counts, 'audio_query_only_refreshes': query_only,
            'attachment_change_counts': attachment_counts, **query_diagnostics,
            'content_and_original_equal': counts['content'] == counts['audio_original'] == 0}


def safe_count(value):
    return value if type(value) is int and 0 <= value <= 1000000 else None


def safe_enum(value, allowed):
    return type(value) is str and value in allowed


def child_failure(stdout, returncode, scope, iteration, sample_pages):
    report = {'source': 'getnote', 'scope': scope, 'source_complete': False,
              'status': 'failed', 'run': iteration, 'sample_pages_limit': sample_pages,
              'stage': 'launch', 'error_code': 'unknown_failure', 'published': False}
    try:
        child = json.loads(stdout)
    except (TypeError, ValueError):
        return report
    if not isinstance(child, dict):
        return report
    if (child.get('status') == 'failed' and safe_enum(child.get('stage'), STAGES)
            and safe_enum(child.get('error_code'), ERROR_CODES)):
        report['stage'], report['error_code'] = child['stage'], child['error_code']
        for key in COUNT_KEYS:
            value = safe_count(child.get(key))
            if value is not None:
                report[key] = value
    elif returncode == 2 and safe_count(child.get('pending')):
        report['stage'], report['error_code'] = 'transcript', 'transcript_pending'
        for src, dst in (('sample_pages', 'sample_pages_read'), ('fetched', 'details_fetched'),
                         ('pending', 'pending')):
            value = safe_count(child.get(src))
            if value is not None:
                report[dst] = value
    return report


def publish_safe_report(path, out):
    """Write only fixed enums and bounded counters from the private result file."""
    try:
        report = json.loads(Path(path).read_text())
    except (OSError, UnicodeError, ValueError):
        report = None
    safe = {'source': 'getnote', 'status': 'invalid_report', 'published': False}
    if isinstance(report, dict) and safe_enum(report.get('scope'), ('sample', 'full')):
        status = report.get('status')
        if safe_enum(status, ('sample_ok', 'full_ok', 'failed', 'changed-between-runs',
                              'invalid-source-report')):
            safe.update(scope=report['scope'], status=status,
                        source_complete=(report['scope'] == 'full' and status == 'full_ok'
                                         and report.get('source_complete') is True))
            for key in ('sample_pages_limit', 'run', 'sample_pages_read',
                        'details_fetched', 'assets_downloaded', 'pending', 'files', 'seeded_existing_notes'):
                value = safe_count(report.get(key))
                if value is not None:
                    safe[key] = value
            if safe_enum(report.get('stage'), STAGES):
                safe['stage'] = report['stage']
            if safe_enum(report.get('error_code'), ERROR_CODES):
                safe['error_code'] = report['error_code']
            for key in ('fetched', 'written_or_changed'):
                value = report.get(key)
                if isinstance(value, list) and len(value) == 2 and all(safe_count(x) is not None for x in value):
                    safe[key] = value
            for key in ('identical_hashes_and_mtimes', 'legacy_paths_preserved'):
                if type(report.get(key)) is bool:
                    safe[key] = report[key]
            counts = report.get('detail_change_counts')
            if (isinstance(counts, dict) and set(counts) == set(DETAIL_GROUPS)
                    and all(type(v) is int and 0 <= v <= 2 for v in counts.values())):
                safe['detail_change_counts'] = counts
            attachment_counts = report.get('attachment_change_counts')
            if (isinstance(attachment_counts, dict) and set(attachment_counts) == set(ATTACHMENT_CHANGES)
                    and all(safe_count(v) is not None for v in attachment_counts.values())):
                safe['attachment_change_counts'] = attachment_counts
            for group in ('query_keys_present', 'query_keys_changed', 'query_refresh_counts'):
                values = report.get(group)
                allowed = QUERY_COUNTS if group == 'query_refresh_counts' else QUERY_KEYS
                if (isinstance(values, dict) and set(values) <= set(allowed)
                        and all(safe_count(v) is not None for v in values.values())):
                    safe[group] = values
            refreshes = report.get('audio_query_only_refreshes')
            if type(refreshes) is int and 0 <= refreshes <= 2:
                safe['audio_query_only_refreshes'] = refreshes
            if type(report.get('content_and_original_equal')) is bool:
                safe['content_and_original_equal'] = report['content_and_original_equal']
    out.write(json.dumps(safe, indent=2) + '\n')


def snapshot(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample-existing', action='store_true',
                        help='Freeze two IDs from the existing private manifest; seed their old paths')
    parser.add_argument('--full', action='store_true', help='Explicit full-library acceptance; never the default')
    parser.add_argument('--sample-pages', type=int, default=2, help='Bounded sample list pages, 1-3')
    parser.add_argument('--publish-report', type=Path, help='Print only allowlisted aggregate evidence')
    args = parser.parse_args()
    if args.publish_report is not None:
        publish_safe_report(args.publish_report, sys.stdout)
        return 0
    if args.sample_existing and args.full:
        parser.error('existing sample cannot use full mode')
    if not 1 <= args.sample_pages <= 3:
        parser.error('sample pages must be between 1 and 3')
    scripts = Path(__file__).resolve().parent
    command = [sys.executable, str(scripts / 'sync_getnote_to_raw.py')]
    if not args.full:
        command += ['--sample', '--sample-pages', str(args.sample_pages)]
    scope = 'full' if args.full else 'sample'
    sample_pages = None if args.full else (0 if args.sample_existing else args.sample_pages)
    # TemporaryDirectory is outside the public checkout and removed on success/failure.
    # Existing secrets are inherited in memory only.
    with tempfile.TemporaryDirectory(prefix='source-shadow-') as temp:
        root = Path(temp) / 'vault'
        root.mkdir(mode=0o700)
        seeded = []
        if args.sample_existing:
            pair_path = Path(temp) / 'pair.json'
            try:
                seeded = prepare_private_sample(root, pair_path)
            except Exception:
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'failed', 'stage': 'sample_seed',
                                  'error_code': 'private_sample_failed', 'published': False,
                                  'sample_pages_limit': 0}))
                return 1
            command += ['--sample-pair', str(pair_path)]
        env = {key: value for key, value in os.environ.items() if key != 'PRIVATE_VAULT_TOKEN'}
        env.update(VAULT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
        reports = []
        detail_evidence = {}
        for iteration in range(2):
            try:
                result = subprocess.run(command, env=env, capture_output=True, text=True)
            except (OSError, ValueError):
                print(json.dumps(child_failure('', 1, scope, iteration + 1,
                                               sample_pages)))
                return 1
            if result.returncode:
                print(json.dumps(child_failure(result.stdout, result.returncode, scope, iteration + 1,
                                               sample_pages)))
                return 1
            try:
                report = json.loads(result.stdout)
            except (TypeError, ValueError):
                report = None
            if not isinstance(report, dict):
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'invalid-source-report', 'stage': 'launch',
                                  'error_code': 'invalid_report', 'run': iteration + 1,
                                  'sample_pages_limit': sample_pages,
                                  'published': False}))
                return 1
            if report.get('scope') != scope or report.get('source_complete') is not args.full:
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'invalid-source-report', 'stage': 'launch',
                                  'error_code': 'invalid_report', 'run': iteration + 1,
                                  'sample_pages_limit': sample_pages,
                                  'published': False}))
                return 1
            if args.sample_existing and (type(report.get('fetched')) is not int
                    or report['fetched'] != 2 or type(report.get('sample_pages')) is not int
                    or report['sample_pages'] != 0):
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'invalid-source-report', 'stage': 'launch',
                                  'error_code': 'invalid_report', 'run': iteration + 1,
                                  'sample_pages_limit': 0, 'published': False}))
                return 1
            reports.append(report)
            try:
                current = snapshot(root)
                if args.sample_existing:
                    current_details = private_detail_snapshot(root)
                    if iteration == 0:
                        previous_details = current_details
                    else:
                        detail_evidence = private_detail_changes(previous_details, current_details)
            except (OSError, ValueError):
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'failed', 'stage': 'snapshot',
                                  'error_code': 'snapshot_failure', 'run': iteration + 1,
                                  'sample_pages_limit': sample_pages,
                                  'published': False}))
                return 1
            if iteration == 0:
                previous = current
        # Live edits between reads are a legitimate mismatch, but still fail acceptance.
        legacy_paths_preserved = (not seeded or
            set((root / 'Raw' / '03 Get').rglob('*.md')) == set(seeded))
        unchanged = current == previous and legacy_paths_preserved
        print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': args.full and unchanged,
                          'status': (scope + '_ok') if unchanged else 'changed-between-runs',
                          'stage': 'compare', 'error_code': None if unchanged else (
                              'legacy_path_mismatch' if not legacy_paths_preserved else 'changed_between_runs'),
                          'sample_pages_limit': sample_pages,
                          'seeded_existing_notes': len(seeded),
                          'legacy_paths_preserved': legacy_paths_preserved if seeded else None,
                          'files': len(current), 'fetched': [int(r['fetched']) for r in reports],
                          'written_or_changed': [int(r['written_or_changed']) for r in reports],
                          'identical_hashes_and_mtimes': unchanged, 'published': False,
                          **detail_evidence}))
        return 0 if unchanged else 1


if __name__ == '__main__':
    raise SystemExit(main())
