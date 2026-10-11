#!/usr/bin/env python3
"""Run one source twice in a NEW private temporary shadow, expose counts only.

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
    parser.add_argument('--source', choices=('getnote', 'flomo'), required=True)
    parser.add_argument('--start-date', help='Explicit flomo search window (not full-history completeness)')
    parser.add_argument('--end-date')
    args = parser.parse_args()
    if args.source == 'flomo' and not (args.start_date and args.end_date):
        parser.error('flomo requires explicit start and end dates')
    scripts = Path(__file__).resolve().parent
    command = [sys.executable, str(scripts / f'sync_{args.source}_to_raw.py')]
    if args.source == 'flomo':
        command += ['--start-date', args.start_date, '--end-date', args.end_date]
    # TemporaryDirectory is outside the public checkout and removed on success/failure.
    # Existing secrets are inherited in memory only.
    with tempfile.TemporaryDirectory(prefix='source-shadow-') as temp:
        root = Path(temp)
        env = dict(os.environ, VAULT_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
        reports = []
        for iteration in range(2):
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            if result.returncode:
                print(json.dumps({'source': args.source, 'status': 'failed', 'run': iteration + 1, 'exit': result.returncode}))
                return 1
            reports.append(json.loads(result.stdout))
            current = snapshot(root)
            if iteration == 0:
                previous = current
        # Live edits between reads are a legitimate mismatch, but still fail acceptance.
        unchanged = current == previous
        print(json.dumps({'source': args.source, 'status': 'ok' if unchanged else 'changed-between-runs',
                          'files': len(current), 'fetched': [r['fetched'] for r in reports],
                          'written_or_changed': [r['written_or_changed'] for r in reports],
                          'identical_hashes_and_mtimes': unchanged, 'published': False}))
        return 0 if unchanged else 1


if __name__ == '__main__':
    raise SystemExit(main())
