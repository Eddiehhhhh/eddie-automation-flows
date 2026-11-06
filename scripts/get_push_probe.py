"""Publish one fixed-content probe to a unique private branch, never to main."""
import os
from pathlib import Path
import re
import tempfile
import time

import get_legacy_manual as manual
import get_private_preview_retention as retention
import get_recurring_entry as entry
import get_recurring_runtime as runtime
from get_push_diagnostic import category

PROBE_PATH = '.sync/get-push-diagnostic/probe.txt'
PROBE_CONTENT = b'probe\n'


def branch_for(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', run_id):
        raise ValueError('invalid_run_id')
    return 'refs/heads/codex/get-push-probe-' + run_id


def branch_ref(remote, branch):
    row = remote.request('/git/ref/' + branch.removeprefix('refs/'), missing=True)
    if row is None:
        return None
    if row.get('ref') != branch or row.get('object', {}).get('type') != 'commit':
        raise ValueError('probe_ref_invalid')
    return retention.checked_sha(row['object'].get('sha'))


def main():
    branch = branch_for(os.environ['GITHUB_RUN_ID'])
    token = os.environ['PRIVATE_VAULT_TOKEN']
    remote = manual.Remote(None, token)
    parent = manual.guard(remote)
    if branch_ref(remote, branch) is not None:
        raise ValueError('probe_branch_exists')
    with tempfile.TemporaryDirectory(prefix='get-push-probe-') as folder:
        root = Path(folder)
        local = retention.readback_checkout(root / 'objects', entry.BOOTSTRAP['capture_anchor']['capture_commit'], token)
        runtime.fetch_commit(local, parent, entry.MAX_INTERVENING + 2)
        env = dict(local.env, GIT_INDEX_FILE=str(root / 'index'), GIT_AUTHOR_NAME='Get diagnostic',
                   GIT_AUTHOR_EMAIL='get-diagnostic@example.invalid', GIT_COMMITTER_NAME='Get diagnostic',
                   GIT_COMMITTER_EMAIL='get-diagnostic@example.invalid')
        if local.run('ls-tree', parent, '--', PROBE_PATH):
            raise ValueError('probe_path_exists')
        local.run('read-tree', parent, env=env)
        blob = local.run('hash-object', '-w', '--stdin', data=PROBE_CONTENT, env=env).decode().strip()
        local.run('update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + PROBE_PATH, env=env)
        tree = local.run('write-tree', '--missing-ok', env=env).decode().strip()
        commit = local.run('commit-tree', tree, '-p', parent, '-m', 'Get push probe', env=env).decode().strip()
        if manual.guard(remote) != parent or branch_ref(remote, branch) is not None:
            raise ValueError('probe_guard_changed')
        try:
            result = local.command('push', '--porcelain', '--no-verify',
                                   '--force-with-lease=' + branch + ':', retention.REMOTE_URL,
                                   commit + ':' + branch)
        except Exception:
            result = None
        observed = None
        for attempt in range(4):
            observed = branch_ref(remote, branch)
            if observed is not None:
                break
            if attempt < 3:
                time.sleep(2 ** attempt)
        if observed != commit:
            reason = 'unexpected_ref' if observed else category(result.stderr) if result else 'transport_or_timeout'
            print('push=' + reason, flush=True)
            raise ValueError('probe_unconfirmed')
        if remote.commit(commit) != {'tree': tree, 'parents': [parent]}:
            raise ValueError('probe_commit_mismatch')
        if local.run('ls-tree', commit, '--', PROBE_PATH).decode().strip() != '100644 blob ' + blob + '\t' + PROBE_PATH:
            raise ValueError('probe_tree_mismatch')
        if remote.blob(blob, len(PROBE_CONTENT)) != PROBE_CONTENT:
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
