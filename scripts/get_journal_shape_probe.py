"""Probe a journal-shaped commit on one private branch, never main."""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request

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


class ApiWriteFailure(Exception):
    def __init__(self, status=None):
        self.status = status if type(status) is int and 100 <= status <= 599 else None


def api_write(token, method, path, payload):
    request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + path,
        data=json.dumps(payload, separators=(',', ':')).encode(), method=method,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'get-journal-shape-probe',
                 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), retention.NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(1024 * 1024 + 1)
            if response.status not in (200, 201) or len(raw) > 1024 * 1024:
                raise ApiWriteFailure(response.status)
    except urllib.error.HTTPError as error:
        raise ApiWriteFailure(error.code) from None
    except ApiWriteFailure:
        raise
    except Exception:
        raise ApiWriteFailure() from None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ApiWriteFailure(response.status) from None
    if not isinstance(value, dict):
        raise ApiWriteFailure(response.status)
    return value


def api_failure(stage, error):
    status = error.status
    category = ('authentication_or_permission' if status in (401, 403) else
                'rate_limited' if status == 429 else
                'http_server_error' if status is not None and status >= 500 else
                'http_client_error' if status is not None else 'network_or_response')
    print('GET_JOURNAL_SHAPE_PROBE ' + json.dumps({'status': 'failed', 'stage': stage,
        'api_category': category, 'http_status': status}, separators=(',', ':')), flush=True)
    return 1


def parse_commit(local, commit, expected_tree, parent):
    raw = local.run('cat-file', 'commit', commit)
    headers, separator, message = raw.partition(b'\n\n')
    if not separator:
        fail('probe_commit_format')
    values = {}
    for line in headers.splitlines():
        key, space, value = line.partition(b' ')
        if not space or key.decode() in values:
            fail('probe_commit_format')
        values[key.decode()] = value
    if values.get('tree', b'').decode() != expected_tree or values.get('parent', b'').decode() != parent:
        fail('probe_commit_format')

    def identity(raw_value):
        matched = re.fullmatch(rb'(.+) <([^<>]+)> ([0-9]+) ([+-])([0-9]{2})([0-9]{2})', raw_value)
        if matched is None:
            fail('probe_commit_format')
        name, email, seconds, sign, hours, minutes = matched.groups()
        offset = (int(hours) * 60 + int(minutes)) * (1 if sign == b'+' else -1)
        date = datetime.fromtimestamp(int(seconds), timezone(timedelta(minutes=offset))).isoformat(timespec='seconds')
        return {'name': name.decode('utf-8'), 'email': email.decode('ascii'), 'date': date}

    if set(values) != {'tree', 'parent', 'author', 'committer'}:
        fail('probe_commit_format')
    return {'tree': expected_tree, 'parents': [parent], 'message': message.decode('utf-8'),
            'author': identity(values['author']), 'committer': identity(values['committer'])}


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
        local_commit, tree, blob, _wanted = builder._build(parent, before, SYNTHETIC)
        changed = local.run('diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '-z', parent, local_commit).split(b'\0')
        if changed != [PATH.encode(), b'']:
            fail('probe_diff_scope')
        if local.run('rev-list', '--parents', '-n', '1', local_commit).decode().split() != [local_commit, parent]:
            fail('probe_parent')
        if remote.ref(MAIN_REF) != parent:
            fail('probe_main_changed_before_write')
        base_tree = local.run('rev-parse', parent + '^{tree}').decode().strip()
        try:
            blob_result = api_write(token, 'POST', '/git/blobs',
                {'content': SYNTHETIC.decode('utf-8'), 'encoding': 'utf-8'})
            if blob_result.get('sha') != blob:
                fail('probe_blob_mismatch')
            tree_result = api_write(token, 'POST', '/git/trees', {'base_tree': base_tree,
                'tree': [{'path': PATH, 'mode': '100644', 'type': 'blob', 'sha': blob}]})
            if tree_result.get('sha') != tree:
                fail('probe_tree_mismatch')
            commit_result = api_write(token, 'POST', '/git/commits', parse_commit(local, local_commit, tree, parent))
            api_commit = journal.checked(commit_result.get('sha'), 40)
            if api_commit != local_commit:
                fail('probe_api_commit_sha_mismatch')
        except ApiWriteFailure as error:
            return api_failure('git_data_object_create', error)
        api_commit_result = remote.request('/git/commits/' + api_commit)
        if (api_commit_result.get('tree', {}).get('sha') != tree
                or [item.get('sha') for item in api_commit_result.get('parents', [])] != [parent]):
            fail('probe_api_commit_mismatch')
        try:
            ref_result = api_write(token, 'POST', '/git/refs', {'ref': target_ref, 'sha': api_commit})
        except ApiWriteFailure as error:
            try:
                observed = remote.request('/git/ref/' + target_ref.removeprefix('refs/'), missing=True)
            except Exception:
                observed = None
            if not isinstance(observed, dict) or observed.get('object', {}).get('sha') != api_commit:
                return api_failure('create_ref', error)
            ref_result = observed
        after = remote.request('/git/ref/' + target_ref.removeprefix('refs/'))
        commit_response = remote.request('/git/commits/' + api_commit)
        if (ref_result.get('ref') != target_ref or ref_result.get('object', {}).get('sha') != api_commit
                or after.get('object', {}).get('sha') != api_commit or commit_response.get('sha') != api_commit
                or commit_response.get('tree', {}).get('sha') != tree
                or [item.get('sha') for item in commit_response.get('parents', [])] != [parent]
                or remote.ref(MAIN_REF) != parent):
            fail('probe_readback')
        print(json.dumps({'status': 'journal_shape_api_verified', 'repository': REPOSITORY,
            'write_method': 'git_data_api',
            'branch': branch, 'changed_paths': 1, 'synthetic_bytes': len(SYNTHETIC),
            'parent_verified': True, 'main_unchanged': True}, separators=(',', ':')), flush=True)
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except Exception:
        print('{"status":"journal_shape_probe_failed"}', file=sys.stderr, flush=True)
        raise SystemExit(1)
