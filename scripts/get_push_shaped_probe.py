"""Probe a parent-leased multi-file push to one new private branch."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import get_legacy_manual as manual
import get_private_preview_retention as retention
import get_recurring_entry as entry
import get_recurring_runtime as runtime
from get_push_probe import branch_for, branch_ref

PREFIX = '.sync/get-push-shaped-probe/'
NOTE_BYTES = 128 * 1024
STATE_BYTES = 1024 * 1024


def classify(raw):
    value = raw.lower() if isinstance(raw, bytes) else b''
    if not value:
        return 'none'
    if any(x in value for x in (b'authentication failed', b'permission denied', b'403', b'could not read username')):
        return 'authentication_or_permission'
    if b'shallow' in value:
        return 'shallow_history'
    if any(x in value for x in (b'missing blob', b'missing tree', b'missing object', b'bad object', b'could not read', b'unable to read')):
        return 'missing_object'
    if any(x in value for x in (b'stale info', b'fetch first', b'non-fast-forward')):
        return 'ref_changed'
    if any(x in value for x in (b'cannot allocate memory', b'file size limit', b'killed by signal')):
        return 'resource_limit'
    if any(x in value for x in (b'timed out', b'connection', b'network', b'resolve host', b'broken pipe')):
        return 'network'
    if any(x in value for x in (b'file too large', b'exceeds', b'oversized', b'413', b'remote rejected', b'[remote rejected]', b'pre-receive hook declined', b'gh001', b'gh013')):
        return 'size_or_remote_rejection'
    return 'other'


def run_git(local, stage, *args):
    if stage not in ('seed_branch', 'merge_base', 'update_branch'):
        raise ValueError('invalid_stage')
    started = time.monotonic()
    result = None
    error = None
    try:
        result = subprocess.run(['git', '-C', str(local.vault), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=local.env, timeout=local.timeout,
                                check=False, preexec_fn=local.child_limits)
    except subprocess.TimeoutExpired:
        error = 'timeout'
    except OSError:
        error = 'spawn_error'
    except Exception:
        error = 'other'
    elapsed = min(3600000, max(0, int((time.monotonic() - started) * 1000)))
    report = {'stage': stage, 'exit_code': result.returncode if result is not None else None,
              'exception': error, 'stdout_category': classify(result.stdout) if result else 'none',
              'stderr_category': classify(result.stderr) if result else 'none', 'elapsed_ms': elapsed}
    print(json.dumps(report, separators=(',', ':')), flush=True)
    return result is not None and result.returncode == 0


def synthetic(index, size):
    return hashlib.shake_256(('get-shaped-probe-' + str(index)).encode()).hexdigest(size // 2).encode()


def main():
    branch = branch_for(os.environ['GITHUB_RUN_ID'])
    token = os.environ['PRIVATE_VAULT_TOKEN']
    remote = manual.Remote(None, token)
    parent = manual.guard(remote)
    if branch_ref(remote, branch) is not None:
        raise ValueError('probe_branch_exists')
    with tempfile.TemporaryDirectory(prefix='get-push-shaped-probe-') as folder:
        root = Path(folder)
        local = retention.readback_checkout(root / 'objects', entry.BOOTSTRAP['capture_anchor']['capture_commit'], token)
        runtime.fetch_commit(local, parent, entry.MAX_INTERVENING + 2)
        if local.run('ls-tree', parent, '--', PREFIX):
            raise ValueError('probe_path_exists')
        if manual.guard(remote) != parent or branch_ref(remote, branch) is not None:
            raise ValueError('probe_guard_changed')
        seeded = run_git(local, 'seed_branch', 'push', '--porcelain', '--no-verify',
                         '--force-with-lease=' + branch + ':', retention.REMOTE_URL, parent + ':' + branch)
        if branch_ref(remote, branch) != parent or not seeded:
            raise ValueError('probe_seed_unconfirmed')
        env = dict(local.env, GIT_INDEX_FILE=str(root / 'index'), GIT_AUTHOR_NAME='Get diagnostic',
                   GIT_AUTHOR_EMAIL='get-diagnostic@example.invalid', GIT_COMMITTER_NAME='Get diagnostic',
                   GIT_COMMITTER_EMAIL='get-diagnostic@example.invalid')
        local.run('read-tree', parent, env=env)
        rows = []
        for index in range(44):
            size = NOTE_BYTES if index < 42 else STATE_BYTES
            data = synthetic(index, size)
            path = PREFIX + ('note-%02d.md' % index if index < 42 else 'state-%02d.json' % index)
            blob = local.run('hash-object', '-w', '--stdin', data=data, env=env).decode().strip()
            local.run('update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + path, env=env)
            rows.append((path, blob, size))
        tree = local.run('write-tree', '--missing-ok', env=env).decode().strip()
        commit = local.run('commit-tree', tree, '-p', parent, '-m', 'Get shaped push probe', env=env).decode().strip()
        if not run_git(local, 'merge_base', 'merge-base', '--is-ancestor', parent, commit):
            raise ValueError('probe_merge_base_failed')
        if manual.guard(remote) != parent or branch_ref(remote, branch) != parent:
            raise ValueError('probe_guard_changed')
        pushed = run_git(local, 'update_branch', 'push', '--porcelain', '--no-verify',
                         '--force-with-lease=' + branch + ':' + parent, retention.REMOTE_URL, commit + ':' + branch)
        observed = branch_ref(remote, branch)
        if observed != commit or not pushed:
            print('probe_ref=' + ('candidate' if observed == commit else 'parent' if observed == parent else 'other'), flush=True)
            raise ValueError('probe_unconfirmed')
        if remote.commit(commit) != {'tree': tree, 'parents': [parent]}:
            raise ValueError('probe_commit_mismatch')
        for path, blob, size in rows:
            if local.run('ls-tree', commit, '--', path).decode().strip() != '100644 blob ' + blob + '\t' + path:
                raise ValueError('probe_tree_mismatch')
            if len(remote.blob(blob, size)) != size:
                raise ValueError('probe_blob_mismatch')
        print('probe=verified', flush=True)
        if manual.guard(remote) != parent:
            print('main=changed', flush=True)
            raise ValueError('main_changed')
        print('main=unchanged', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('probe_failed') from None
