"""Probe a journal-shaped commit on one private branch, never main."""
import json
import os
import re
import subprocess
import sys

import get_notion_outbox as codec
import get_notion_remote_journal as journal
import get_notion_journal_runtime as checkpoint_runtime
import get_private_preview_retention as retention
import get_private_runtime_report as report


PATH = journal.STATE_PATH
REPOSITORY = journal.REPOSITORY
MAIN_REF = journal.REF
SYNTHETIC = b'{"synthetic_journal_shape_probe":true}\n'


def fail(code):
    raise ValueError(code) from None


def classify_push_error(raw):
    value = raw.lower() if isinstance(raw, bytes) else b''
    if not value:
        return 'none'
    if (any(x in value for x in (b'authentication failed', b'permission denied', b'write access to repository not granted', b'could not read username'))
            or (b'permission to ' in value and b'denied to ' in value)):
        return 'authentication_or_permission'
    if b'repository not found' in value:
        return 'repository_not_found'
    if any(x in value for x in (b'gh006', b'gh013', b'protected branch', b'ruleset', b'pre-receive hook declined')):
        return 'branch_policy_rejection'
    if any(x in value for x in (b'stale info', b'fetch first', b'non-fast-forward')):
        return 'ref_rejected'
    if any(x in value for x in (b'missing blob', b'missing tree', b'missing object', b'bad object', b'could not read object')):
        return 'missing_object'
    if b'shallow' in value:
        return 'shallow_history'
    if any(x in value for x in (b'cannot allocate memory', b'file size limit', b'killed by signal')):
        return 'resource_limit'
    if any(x in value for x in (b'file too large', b'exceeds', b'oversized', b'gh001')):
        return 'size_or_remote_rejection'
    if re.search(rb'\b413\b', value):
        return 'http_payload_too_large'
    if any(x in value for x in (b'429', b'rate limit')) and re.search(rb'\b429\b', value):
        return 'rate_limited'
    if re.search(rb'\b5(?:00|02|03|04)\b', value):
        return 'http_server_error'
    if re.search(rb'\b(?:401|403)\b', value):
        return 'authentication_or_permission'
    if re.search(rb'\b(?:400|404|422)\b', value):
        return 'http_client_error'
    if b'remote unpack failed' in value:
        return 'remote_unpack_failed'
    if any(x in value for x in (b'rpc failed', b'remote end hung up', b'unexpected disconnect', b'sideband packet')):
        return 'transport_disconnect'
    if any(x in value for x in (b'protocol error', b'bad line length', b'invalid packet', b'sideband packet')):
        return 'git_protocol_error'
    if any(x in value for x in (b'connection', b'timed out', b'network', b'resolve host', b'broken pipe')):
        return 'network'
    return 'other'


def run():
    token = os.environ.get('PRIVATE_VAULT_TOKEN')
    if not token:
        fail('probe_credentials')
    remote = retention.GitHubRemote(None, token)
    metadata = remote.metadata()
    if metadata.get('full_name') != REPOSITORY or metadata.get('private') is not True:
        fail('probe_target')
    parent_value = remote.request('/git/ref/heads/main')
    parent = journal.checked(parent_value.get('object', {}).get('sha'), 40)
    run_id = os.environ.get('GITHUB_RUN_ID')
    if not run_id or not run_id.isdecimal() or len(run_id) > 20:
        fail('probe_run_id')
    branch = 'codex/get-journal-shape-probe-' + run_id
    target_ref = 'refs/heads/' + branch
    if remote.request('/git/ref/' + target_ref.removeprefix('refs/'), missing=True) is not None:
        fail('probe_branch_exists')

    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory(prefix='get-journal-shape-', dir=os.environ['RUNNER_TEMP']) as folder:
        local = retention.readback_checkout(Path(folder) / 'objects', parent, token)
        remote.local = local
        wrapped = journal.ImmutableJournalRemote(remote)
        loader = checkpoint_runtime.ExactJournalLoader(local.vault, wrapped)
        loader(parent)
        row = local.tree(parent).get(PATH)
        if row is None or row[:2] != ('100644', 'blob'):
            fail('probe_state_path')
        old_data = local.blob(row[2])
        anchor = {'commit': parent, 'state_blob': row[2], 'state_sha256': codec.digest(old_data)}
        builder = journal.RemoteJournal(local.vault, wrapped, anchor=anchor,
            load_commit=loader, guard=lambda _head: True, confirm=lambda _receipt: True)
        _, before = builder._local_tree(parent)
        commit, tree, _blob, _wanted = builder._build(parent, before, SYNTHETIC)
        changed = local.run('diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '-z', parent, commit).split(b'\0')
        if changed != [PATH.encode(), b'']:
            fail('probe_diff_scope')
        if local.run('rev-list', '--parents', '-n', '1', commit).decode().split() != [commit, parent]:
            fail('probe_parent')
        if remote.ref(MAIN_REF) != parent:
            fail('probe_main_changed_before_push')
        push = subprocess.run(['git', '-C', str(local.vault), '-c', 'http.version=HTTP/1.1', 'push', '--porcelain', '--no-verify',
            '--force-with-lease=' + target_ref + ':', retention.REMOTE_URL, commit + ':' + target_ref],
            env=local.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False, preexec_fn=local.child_limits)
        if push.returncode:
            fail_report = {'status': 'failed', 'stage': 'push_exit',
                'exit_code': push.returncode,
                'stdout_category': classify_push_error(push.stdout),
                'stderr_category': classify_push_error(push.stderr),
                'stdout_words': report.git_word_hits(push.stdout),
                'stderr_words': report.git_word_hits(push.stderr)}
            print('GET_JOURNAL_SHAPE_PROBE ' + json.dumps(fail_report, separators=(',', ':')), flush=True)
            return 1
        after = remote.request('/git/ref/' + target_ref.removeprefix('refs/'))
        commit_response = remote.request('/git/commits/' + commit)
        if (after.get('object', {}).get('sha') != commit or commit_response.get('sha') != commit
                or commit_response.get('tree', {}).get('sha') != tree
                or [item.get('sha') for item in commit_response.get('parents', [])] != [parent]
                or remote.ref(MAIN_REF) != parent):
            fail('probe_readback')
        print(json.dumps({'status': 'journal_shape_push_verified', 'repository': REPOSITORY,
            'branch': branch, 'changed_paths': 1, 'synthetic_bytes': len(SYNTHETIC),
            'parent_verified': True, 'main_unchanged': True}, separators=(',', ':')), flush=True)
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except Exception:
        print('{"status":"journal_shape_probe_failed"}', file=sys.stderr, flush=True)
        raise SystemExit(1)
