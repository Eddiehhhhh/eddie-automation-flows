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


def snapshot(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--full', action='store_true', help='Explicit full-library acceptance; never the default')
    parser.add_argument('--sample-pages', type=int, default=2, help='Bounded sample list pages, 1-3')
    args = parser.parse_args()
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
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            if result.returncode:
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'failed', 'run': iteration + 1, 'exit': result.returncode}))
                return 1
            report = json.loads(result.stdout)
            if report.get('scope') != scope or report.get('source_complete') is not args.full:
                print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': False,
                                  'status': 'invalid-source-report', 'run': iteration + 1}))
                return 1
            reports.append(report)
            current = snapshot(root)
            if iteration == 0:
                previous = current
        # Live edits between reads are a legitimate mismatch, but still fail acceptance.
        unchanged = current == previous
        print(json.dumps({'source': 'getnote', 'scope': scope, 'source_complete': args.full and unchanged,
                          'status': (scope + '_ok') if unchanged else 'changed-between-runs',
                          'sample_pages_limit': None if args.full else args.sample_pages,
                          'files': len(current), 'fetched': [int(r['fetched']) for r in reports],
                          'written_or_changed': [int(r['written_or_changed']) for r in reports],
                          'identical_hashes_and_mtimes': unchanged, 'published': False}))
        return 0 if unchanged else 1


if __name__ == '__main__':
    raise SystemExit(main())

