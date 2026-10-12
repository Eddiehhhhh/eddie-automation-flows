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

STAGES = frozenset(('configuration', 'list', 'detail', 'validation', 'assets',
                    'write', 'receipt', 'transcript', 'launch', 'snapshot', 'compare'))
ERROR_CODES = frozenset(('missing_credentials', 'api_redirect', 'api_auth',
                         'api_rate_limited', 'api_rejected', 'api_transport',
                         'response_limit', 'sample_types_missing', 'attachment_limit',
                         'asset_failure', 'transcript_pending', 'invalid_report',
                         'unknown_failure', 'snapshot_failure', 'changed_between_runs',
                         'unexpected'))
COUNT_KEYS = ('sample_pages_read', 'details_fetched', 'assets_downloaded',
              'written_or_changed')


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
                        'details_fetched', 'assets_downloaded', 'pending', 'files'):
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
            if type(report.get('identical_hashes_and_mtimes')) is bool:
                safe['identical_hashes_and_mtimes'] = report['identical_hashes_and_mtimes']
    out.write(json.dumps(safe, indent=2) + '\n')


def snapshot(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--full', action='store_true', help='Explicit full-library acceptance; never the default')
    parser.add_argument('--sample-pages', type=int, default=2, help='Bounded sample list pages, 1-3')
    parser.add_argument('--publish-report', type=Path, help='Print only allowlisted aggregate evidence')
    args = parser.parse_args()
    if args.publish_report is not None:
        publish_safe_report(args.publish_report, sys.stdout)
        return 0
    if not 1 <= args.sample_pages <= 3:
        parser.error('sample pages must be between 1 and 3')
    scripts = Path(__file__).resolve().parent
    command = [sys.executable, str(scripts / 'sync_getnote_to_raw.py')]
    if not args.full:
        command += ['--sample', '--sample-pages', str(args.sample_pages)]
    scope = 'full' if args.full else 'sample'
    # TemporaryDirectory is outside the public checkout and removed on success/failure.
    # Existing secrets are inherited in memory only.
    with tempfile.TemporaryDirectory(prefix='source-shadow-') as temp:
        root = Path(temp)
        env = dict(os.environ, VAULT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
        reports = []
        for iteration in range(2):
            try:
                result = subprocess.run(command, env=env, capture_output=True, text=True)
            except (OSError, ValueError):
                print(json.dumps(child_failure('', 1, scope, iteration + 1,
                                               None if args.full else args.sample_pages)))
                return 1
            if result.returncode:
                print(json.dumps(child_failure(result.stdout, result.returncode, scope, iteration + 1,
                                               None if args.full else args.sample_pages)))
                return 1
            try:
                report = json.loads(result.stdout)
            except (TypeError, ValueError):
                report = None
            if not isinstance(report, dict):
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'invalid-source-report', 'stage': 'launch',
                                  'error_code': 'invalid_report', 'run': iteration + 1,
                                  'sample_pages_limit': None if args.full else args.sample_pages,
                                  'published': False}))
                return 1
            if report.get('scope') != scope or report.get('source_complete') is not args.full:
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'invalid-source-report', 'stage': 'launch',
                                  'error_code': 'invalid_report', 'run': iteration + 1,
                                  'sample_pages_limit': None if args.full else args.sample_pages,
                                  'published': False}))
                return 1
            reports.append(report)
            try:
                current = snapshot(root)
            except (OSError, ValueError):
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'failed', 'stage': 'snapshot',
                                  'error_code': 'snapshot_failure', 'run': iteration + 1,
                                  'sample_pages_limit': None if args.full else args.sample_pages,
                                  'published': False}))
                return 1
            if iteration == 0:
                previous = current
        # Live edits between reads are a legitimate mismatch, but still fail acceptance.
        unchanged = current == previous
        print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': args.full and unchanged,
                          'status': (scope + '_ok') if unchanged else 'changed-between-runs',
                          'stage': 'compare', 'error_code': None if unchanged else 'changed_between_runs',
                          'sample_pages_limit': None if args.full else args.sample_pages,
                          'files': len(current), 'fetched': [int(r['fetched']) for r in reports],
                          'written_or_changed': [int(r['written_or_changed']) for r in reports],
                          'identical_hashes_and_mtimes': unchanged, 'published': False}))
        return 0 if unchanged else 1


if __name__ == '__main__':
    raise SystemExit(main())
