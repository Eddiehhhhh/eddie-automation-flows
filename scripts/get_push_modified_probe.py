"""Probe a parent-leased update with twenty deliberately absent old blobs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import get_legacy_manual as manual
import get_private_preview_retention as retention
import get_private_runtime_report as report
import get_recurring_entry as entry
import get_recurring_runtime as runtime
from get_push_probe import branch_for, branch_ref
from get_push_shaped_probe import PREFIX, NOTE_BYTES, synthetic

SOURCE_REF = 'refs/heads/codex/get-push-probe-42238209410'
SOURCE_COMMIT = '64f5b23ac6793d9d4aeb4c24bcc3ee68642d4c88'
CHANGED_BYTES = 176 * 1024


def run_git(local, stage, *args):
    if stage not in ('seed_branch', 'merge_base', 'update_branch'):
        raise ValueError('invalid_stage')
    started = time.monotonic()
    result = None
    kind = None
    try:
        result = subprocess.run(['git', '-C', str(local.vault), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=local.env, timeout=local.timeout,
                                check=False, preexec_fn=local.child_limits)
    except subprocess.TimeoutExpired:
        kind = 'timeout'
    except OSError:
        kind = 'spawn_error'
    except Exception:
        kind = 'other'
    data = {'stage': stage, 'exit_code': result.returncode if result is not None else None,
            'exception': kind, 'stdout_words': report.git_word_hits(result.stdout) if result else [],
            'stderr_words': report.git_word_hits(result.stderr) if result else [],
            'elapsed_ms': min(3600000, max(0, int((time.monotonic() - started) * 1000)))}
    print(json.dumps(data, separators=(',', ':')), flush=True)
    return result is not None and result.returncode == 0


def changed_content(index):
    return hashlib.shake_256(('get-modified-probe-' + str(index)).encode()).hexdigest(CHANGED_BYTES // 2).encode()


def require_missing(local, oids):
    output = local.run('cat-file', '--batch-check=%(objectname) %(objecttype)',
                       data=('\n'.join(oids) + '\n').encode()).decode('ascii').splitlines()
    if output != [oid + ' missing' for oid in oids]:
        raise ValueError('old_blobs_not_all_missing')


def require_present(local, oids):
    output = local.run('cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)',
                       data=('\n'.join(oids) + '\n').encode()).decode('ascii').splitlines()
    if output != [oid + ' blob ' + str(NOTE_BYTES) for oid in oids]:
        raise ValueError('old_blobs_not_all_present')


def hydrate_old(local, remote, token, root, paths, oids):
    if len(paths) != 20 or len(oids) != 20 or len(set(paths)) != 20 or len(set(oids)) != 20:
        raise ValueError('invalid_old_blob_selection')
    metadata = {path: ('100644', 'blob', oid, NOTE_BYTES) for path, oid in zip(paths, oids)}
    values = manual.fetch_selected(remote, metadata, token, root / 'old-probe-blobs')
    if set(values) != set(paths):
        raise ValueError('old_blob_readback_mismatch')
    for index, (path, oid) in enumerate(zip(paths, oids)):
        data = values[path]
        if data != synthetic(index, NOTE_BYTES) or retention.oid(data) != oid:
            raise ValueError('old_blob_readback_mismatch')
        manual.import_blob(local, data, oid)
    require_present(local, oids)


def main(hydrate=False):
    if type(hydrate) is not bool:
        raise ValueError('invalid_probe_mode')
    branch = branch_for(os.environ['GITHUB_RUN_ID'])
    token = os.environ['PRIVATE_VAULT_TOKEN']
    remote = manual.Remote(None, token)
    main = manual.guard(remote)
    if branch_ref(remote, SOURCE_REF) != SOURCE_COMMIT or branch_ref(remote, branch) is not None:
        raise ValueError('probe_ref_changed')
    with tempfile.TemporaryDirectory(prefix='get-modified-blob-probe-') as folder:
        root = Path(folder)
        local = retention.readback_checkout(root / 'objects', entry.BOOTSTRAP['capture_anchor']['capture_commit'], token)
        runtime.fetch_commit(local, SOURCE_COMMIT, entry.MAX_INTERVENING + 2)
        paths = [PREFIX + 'note-%02d.md' % index for index in range(20)]
        old_oids = [retention.oid(synthetic(index, NOTE_BYTES)) for index in range(20)]
        for path, oid in zip(paths, old_oids):
            if local.run('ls-tree', SOURCE_COMMIT, '--', path).decode().strip() != '100644 blob ' + oid + '\t' + path:
                raise ValueError('probe_source_mismatch')
        require_missing(local, old_oids)
        print('old_blobs_missing=20', flush=True)
        if manual.guard(remote) != main or branch_ref(remote, SOURCE_REF) != SOURCE_COMMIT or branch_ref(remote, branch) is not None:
            raise ValueError('probe_guard_changed')
        if hydrate:
            hydrate_old(local, remote, token, root, paths, old_oids)
            if manual.guard(remote) != main or branch_ref(remote, SOURCE_REF) != SOURCE_COMMIT or branch_ref(remote, branch) is not None:
                raise ValueError('probe_guard_changed')
            print('old_blobs_hydrated=20', flush=True)
        seeded = run_git(local, 'seed_branch', 'push', '--porcelain', '--no-verify',
                         '--force-with-lease=' + branch + ':', retention.REMOTE_URL, SOURCE_COMMIT + ':' + branch)
        if branch_ref(remote, branch) != SOURCE_COMMIT or not seeded:
            raise ValueError('probe_seed_unconfirmed')
        env = dict(local.env, GIT_INDEX_FILE=str(root / 'index'), GIT_AUTHOR_NAME='Get diagnostic',
                   GIT_AUTHOR_EMAIL='get-diagnostic@example.invalid', GIT_COMMITTER_NAME='Get diagnostic',
                   GIT_COMMITTER_EMAIL='get-diagnostic@example.invalid')
        local.run('read-tree', SOURCE_COMMIT, env=env)
        rows = []
        for index, path in enumerate(paths):
            data = changed_content(index)
            oid = local.run('hash-object', '-w', '--stdin', data=data, env=env).decode().strip()
            local.run('update-index', '--cacheinfo', '100644,' + oid + ',' + path, env=env)
            rows.append((path, oid))
        tree = local.run('write-tree', '--missing-ok', env=env).decode().strip()
        commit = local.run('commit-tree', tree, '-p', SOURCE_COMMIT, '-m', 'Get modified blob probe', env=env).decode().strip()
        changed = local.run('diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '-z', SOURCE_COMMIT, commit)
        if changed.split(b'\0') != [path.encode() for path in paths] + [b'']:
            raise ValueError('probe_delta_mismatch')
        if hydrate:
            require_present(local, old_oids)
        else:
            require_missing(local, old_oids)
        state = 'old_blobs_present_before_push' if hydrate else 'old_blobs_missing_before_push'
        print(json.dumps({'changed_paths': len(paths), 'changed_bytes': len(paths) * CHANGED_BYTES,
                          state: len(old_oids)}, separators=(',', ':')), flush=True)
        if not run_git(local, 'merge_base', 'merge-base', '--is-ancestor', SOURCE_COMMIT, commit):
            raise ValueError('probe_merge_base_failed')
        if manual.guard(remote) != main or branch_ref(remote, SOURCE_REF) != SOURCE_COMMIT or branch_ref(remote, branch) != SOURCE_COMMIT:
            raise ValueError('probe_guard_changed')
        pushed = run_git(local, 'update_branch', 'push', '--porcelain', '--no-verify',
                         '--force-with-lease=' + branch + ':' + SOURCE_COMMIT, retention.REMOTE_URL, commit + ':' + branch)
        observed = branch_ref(remote, branch)
        if observed != commit or not pushed:
            print('probe_ref=' + ('candidate' if observed == commit else 'parent' if observed == SOURCE_COMMIT else 'other'), flush=True)
            try:
                print('source_branch=' + ('unchanged' if branch_ref(remote, SOURCE_REF) == SOURCE_COMMIT else 'changed'), flush=True)
                print('main=' + ('unchanged' if manual.guard(remote) == main else 'changed'), flush=True)
            except Exception:
                print('main_or_source=unverified', flush=True)
            raise ValueError('probe_unconfirmed')
        if remote.commit(commit) != {'tree': tree, 'parents': [SOURCE_COMMIT]}:
            raise ValueError('probe_commit_mismatch')
        for path, oid in rows:
            if local.run('ls-tree', commit, '--', path).decode().strip() != '100644 blob ' + oid + '\t' + path:
                raise ValueError('probe_tree_mismatch')
            if len(remote.blob(oid, CHANGED_BYTES)) != CHANGED_BYTES:
                raise ValueError('probe_blob_mismatch')
        if branch_ref(remote, SOURCE_REF) != SOURCE_COMMIT or manual.guard(remote) != main:
            raise ValueError('probe_ref_changed')
        print('probe=verified', flush=True)
        print('source_branch=unchanged', flush=True)
        print('main=unchanged', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('probe_failed') from None
