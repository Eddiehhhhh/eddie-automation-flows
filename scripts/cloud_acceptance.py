#!/usr/bin/env python3
"""Read-only cloud acceptance. Never writes the vault or calls an LLM/API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import stat
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def report_status(body):
    """Accept one unambiguous formal status; never trust the first success."""
    values = re.findall(r'^\s*-\s*(?:执行结果|最终状态)[：:]\s*(.*?)\s*$', body, re.M)
    statuses = []
    for value in values:
        if value.startswith('`') and value.endswith('`'):
            value = value[1:-1]
        if not re.fullmatch(r'[a-z_]+', value):
            return None
        statuses.append(value)
    return statuses[0] if statuses and len(set(statuses)) == 1 else None


def vault_fingerprint(root):
    """Include ignored paths, empty directories and links; never follow links.

    Only root .git is excluded as Git runtime metadata. Compare contents/types/
    permission bits, not atime/mtime (reads can update atime).
    """
    def fail_unreadable(error):
        raise error
    entries = [('.', 'directory', stat.S_IMODE(root.lstat().st_mode))]
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail_unreadable):
        current = Path(folder)
        if current == root:
            dirs[:] = [name for name in dirs if name != '.git']
            files = [name for name in files if name != '.git']
        for name in sorted(dirs + files):
            path = current / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                entries.append((relative, 'link', os.readlink(path)))
            elif stat.S_ISDIR(mode):
                entries.append((relative, 'directory', stat.S_IMODE(mode)))
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
                entries.append((relative, 'file', stat.S_IMODE(mode), digest.hexdigest()))
            else:
                raise ValueError('Unsupported vault file type')
    return hashlib.sha256(json.dumps(sorted(entries), ensure_ascii=True).encode()).hexdigest()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def health(root, now):
    """Same completion/cursor contract as big_system_guard; no Mac probes."""
    day = now.astimezone(SHANGHAI).date().isoformat()
    proof = read_json(root / 'Raw/00 Meta/source-arrival-status.json')
    source_ok = (proof.get('checked_date') == day and
                 proof.get('overall_status') in ('ok', 'healthy') and
                 proof.get('registered_missing') == [])
    report = root / f'Raw/09 ChatGPT/{day}-每日升格扫描.md'
    promotion = 'blocked'
    if not report.exists() and now.astimezone(SHANGHAI).hour < 12:
        promotion = 'pending_schedule'
    elif report.exists():
        body = report.read_text(encoding='utf-8')
        status = report_status(body)
        head = re.search(r'^- 扫描终点：`([0-9a-f]{40})`$', body, re.M)
        if not head:
            head = re.search(r'^- 扫描范围：`[0-9a-f]{40}\.\.([0-9a-f]{40})`$', body, re.M)
        state = read_json(root / 'Raw/09 ChatGPT/_daily-promotion-state.json')
        if status and head and status in ('success', 'no_wiki_update'):
            runs = state.get('runs') or state.get('daily_runs') or {}
            scan = state.get('raw_scan') or {}
            result = scan.get('result', '')
            valid_result = isinstance(result, str) and (result.startswith('success') or
                             (status == 'no_wiki_update' and result == 'no_wiki_update'))
            if (runs.get(day, {}).get('effective_status') == status and
                state.get('raw_cursor', {}).get('last_processed_head_sha') == head[1] and
                state.get('last_processed_head_sha') == head[1] and
                scan.get('head_sha') == head[1] and scan.get('cursor_advanced') is True and valid_result):
                promotion = 'healthy'
    hermes = read_json(root / 'Raw/00 Meta/hermes-runtime-state.json')
    return {'source_arrival': 'healthy' if source_ok else 'blocked',
            'promotion': promotion,
            'hermes_policy': 'disabled_by_owner' if hermes.get('status') == 'disabled_by_owner' else 'unverified',
            'local_maintenance': 'not_checked_cloud',
            'scheduler_control': 'not_checked_cloud'}


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, timeout=60).decode().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = args.vault.resolve()
    output = args.output.resolve()
    if root == output or root in output.parents:
        raise ValueError('output must be outside vault')
    now = datetime.now(SHANGHAI)
    sha = git(root, 'rev-parse', 'HEAD')
    before = git(root, 'status', '--porcelain', '--untracked-files=all')
    if before:
        raise ValueError('requires clean isolated snapshot')
    before_fingerprint = vault_fingerprint(root)
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('run_iteration_pipeline.py')),
        '--mode', 'weekly', '--dry-run', '--format', 'json'],
        env={**os.environ, 'VAULT_ROOT': str(root), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, timeout=300, check=True)
    weekly = json.loads(result.stdout)
    counts = weekly['audit']['counts']
    # Explicit projection: never emit titles, paths, IDs, warnings or exception text.
    safe_counts = {k: int(counts[k]) for k in ('total_candidates', 'high_priority_candidates')}
    unchanged = (vault_fingerprint(root) == before_fingerprint and
                 git(root, 'status', '--porcelain', '--untracked-files=all') == before)
    if not unchanged:
        raise ValueError('dry run changed vault')
    period = weekly['period']['label']
    payload = {'schema_version': 1, 'mode': 'readonly_acceptance', 'production_enabled': False,
        'acceptance_passed': True, 'vault_unchanged': unchanged,
        'snapshot_digest': hashlib.sha256(sha.encode()).hexdigest(),
        'run_key': hashlib.sha256(('weekly:' + period + ':' + sha).encode()).hexdigest(),
        'weekly': {'period': period, 'counts': safe_counts, 'llm_synthesis': 'not_executed'},
        'health': health(root, now)}
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        # Public runner logs must not contain private filenames or exception payloads.
        print('Cloud acceptance failed; private details suppressed.', file=sys.stderr)
        raise SystemExit(1)
