"""Dry-run the Get runtime's shallow-object Git publication without a Get scan."""
import os
from pathlib import Path
import subprocess
import tempfile

import get_legacy_manual as manual
import get_private_preview_retention as retention
import get_recurring_entry as entry
import get_recurring_runtime as runtime


def category(stderr):
    value = stderr.lower()
    if any(x in value for x in (b'authentication failed', b'permission denied', b'403', b'could not read username')):
        return 'authentication_or_permission'
    if any(x in value for x in (b'missing blob', b'missing tree', b'could not read', b'bad object', b'missing object')):
        return 'missing_object'
    if b'shallow' in value:
        return 'shallow_history'
    if any(x in value for x in (b'connection', b'timed out', b'network', b'resolve host')):
        return 'network'
    if any(x in value for x in (b'stale info', b'fetch first', b'non-fast-forward')):
        return 'ref_changed'
    return 'other'


def main():
    token = os.environ['PRIVATE_VAULT_TOKEN']
    remote = manual.Remote(None, token)
    parent = manual.guard(remote)
    with tempfile.TemporaryDirectory(prefix='get-push-diagnostic-') as folder:
        root = Path(folder)
        local = retention.readback_checkout(root / 'objects', entry.BOOTSTRAP['capture_anchor']['capture_commit'], token)
        runtime.fetch_commit(local, parent, entry.MAX_INTERVENING + 2)
        env = dict(local.env, GIT_INDEX_FILE=str(root / 'index'), GIT_AUTHOR_NAME='Get diagnostic',
                   GIT_AUTHOR_EMAIL='get-diagnostic@example.invalid', GIT_COMMITTER_NAME='Get diagnostic',
                   GIT_COMMITTER_EMAIL='get-diagnostic@example.invalid')
        local.run('read-tree', parent, env=env)
        blob = local.run('hash-object', '-w', '--stdin', data=b'probe\n', env=env).decode().strip()
        local.run('update-index', '--add', '--cacheinfo', '100644,' + blob + ',.sync/get-push-diagnostic/probe.txt', env=env)
        tree = local.run('write-tree', '--missing-ok', env=env).decode().strip()
        commit = local.run('commit-tree', tree, '-p', parent, '-m', 'Get push dry-run', env=env).decode().strip()
        pack = subprocess.run(['git', '-C', str(local.vault), 'pack-objects', '--stdout', '--revs', '--thin'],
                              input=(commit + '\n^' + parent + '\n').encode(), stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, env=local.env, timeout=120, check=False,
                              preexec_fn=local.child_limits)
        print('pack=' + ('ok' if pack.returncode == 0 else category(pack.stderr)), flush=True)
        if manual.guard(remote) != parent:
            print('push=ref_changed', flush=True)
            return
        result = local.command('push', '--dry-run', '--porcelain', '--no-verify',
                               '--force-with-lease=' + retention.MAIN_REF + ':' + parent,
                               retention.REMOTE_URL, commit + ':' + retention.MAIN_REF)
        print('push=' + ('dry_run_ok' if result.returncode == 0 else category(result.stderr)), flush=True)
        print('main=' + ('unchanged' if manual.guard(remote) == parent else 'changed'), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('diagnostic_failed') from None
