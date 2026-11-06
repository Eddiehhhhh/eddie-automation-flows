"""Isolated private Git Data object identity probe; never updates main."""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

REPO = 'Eddiehhhhh/eddie-llm-wiki'
MESSAGE = 'Prepare canonical Get transaction with receipt'
NAME = 'Get local proposal'
EMAIL = 'get-proposal@example.invalid'
CONTENT = b'get-git-data-identity-probe-v1\n'
OID = re.compile(r'^[a-f0-9]{40}$')
REASONS = {'invalid_oid', 'missing_token', 'run_identity', 'not_private', 'main_ref',
           'parent_changed', 'ref_exists', 'path_exists', 'timestamp', 'blob_mismatch',
           'tree_mismatch', 'commit_mismatch', 'ref_mismatch', 'commit_readback',
           'path_readback', 'blob_readback'}
GIT_OPS = {'rev-parse', 'ls-tree', 'show', 'read-tree', 'hash-object',
           'update-index', 'write-tree', 'commit-tree'}
STAGES = {'repository', 'main_ref', 'staging_ref_read', 'blob_create', 'tree_create',
          'commit_create', 'staging_ref_create', 'commit_read', 'blob_read', 'path_read',
          'api_unknown', 'git_unknown'} | {'git_' + op.replace('-', '_') for op in GIT_OPS}
FAILURES = {'request_budget', 'http_error', 'transport_error', 'response_budget',
            'response_invalid', 'exit_128', 'exit_nonzero', 'timeout', 'spawn_error', 'invalid_output'}


class ProbeFailure(Exception):
    def __init__(self, stage, reason, status=None):
        self.stage, self.reason, self.status = stage, reason, status


def diagnostic(exc):
    if isinstance(exc, ProbeFailure):
        return {'status': 'failed', 'stage': exc.stage if exc.stage in STAGES else 'unknown',
                'reason': exc.reason if exc.reason in FAILURES else 'unexpected',
                **({'http_status': exc.status} if exc.reason == 'http_error' and type(exc.status) is int
                   and 100 <= exc.status <= 599 else {})}
    reason = str(exc) if type(exc) is ValueError and str(exc) in REASONS else 'unexpected'
    return {'status': 'failed', 'stage': 'validation', 'reason': reason}


def api_stage(method, path):
    if path == '': return 'repository'
    if path == '/git/ref/heads/main': return 'main_ref'
    if path.startswith('/git/ref/heads/codex/'): return 'staging_ref_read'
    if method == 'POST' and path in ('/git/blobs', '/git/trees', '/git/commits', '/git/refs'):
        return {'/git/blobs': 'blob_create', '/git/trees': 'tree_create',
                '/git/commits': 'commit_create', '/git/refs': 'staging_ref_create'}[path]
    if method == 'GET' and path.startswith('/git/commits/'): return 'commit_read'
    if method == 'GET' and path.startswith('/git/blobs/'): return 'blob_read'
    if method == 'GET' and path.startswith('/contents/'): return 'path_read'
    return 'api_unknown'


def checked(value):
    if not isinstance(value, str) or not OID.fullmatch(value):
        raise ValueError('invalid_oid')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, token):
        if not isinstance(token, str) or not token:
            raise ValueError('missing_token')
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method, path, payload=None, missing=False):
        stage = api_stage(method, path)
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if data is not None and len(data) > 65536:
            raise ProbeFailure(stage, 'request_budget')
        request = urllib.request.Request('https://api.github.com/repos/' + REPO + path, data=data,
            method=method, headers={'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'get-gitdata-probe'})
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status not in (200, 201):
                    raise ProbeFailure(stage, 'http_error', response.status)
                raw = response.read(1048577)
        except urllib.error.HTTPError as exc:
            if missing and method == 'GET' and exc.code == 404:
                return None
            raise ProbeFailure(stage, 'http_error', exc.code) from None
        except ProbeFailure:
            raise
        except Exception:
            raise ProbeFailure(stage, 'transport_error') from None
        if len(raw) > 1048576:
            raise ProbeFailure(stage, 'response_budget')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProbeFailure(stage, 'response_invalid') from None
        if not isinstance(value, dict):
            raise ProbeFailure(stage, 'response_invalid')
        return value


def git(repo, *args, data=None, env=None):
    stage = 'git_' + args[0].replace('-', '_') if args and args[0] in GIT_OPS else 'git_unknown'
    try:
        return subprocess.check_output(['git', '-C', str(repo), *args], input=data,
                                       env=env, stderr=subprocess.DEVNULL, timeout=60).decode().strip()
    except subprocess.CalledProcessError as exc:
        reason = 'exit_128' if exc.returncode == 128 else 'exit_nonzero'
    except subprocess.TimeoutExpired:
        reason = 'timeout'
    except OSError:
        reason = 'spawn_error'
    except UnicodeError:
        reason = 'invalid_output'
    raise ProbeFailure(stage, reason) from None


def main_ref(api):
    row = api.call('GET', '/git/ref/heads/main')
    if row.get('ref') != 'refs/heads/main' or row.get('object', {}).get('type') != 'commit':
        raise ValueError('main_ref')
    return checked(row['object'].get('sha'))


def run(api, repo, run_id, attempt):
    if not re.fullmatch(r'[0-9]{1,20}', run_id) or not re.fullmatch(r'[0-9]{1,4}', attempt):
        raise ValueError('run_identity')
    info = api.call('GET', '')
    if info.get('private') is not True or info.get('full_name') != REPO:
        raise ValueError('not_private')
    parent = main_ref(api)
    if git(repo, 'rev-parse', 'HEAD') != parent:
        raise ValueError('parent_changed')
    path = '.sync/get-gitdata-probe/' + run_id + '-' + attempt + '.txt'
    ref = 'refs/heads/codex/get-gitdata-probe-' + run_id + '-' + attempt
    if api.call('GET', '/git/ref/' + ref.removeprefix('refs/'), missing=True) is not None:
        raise ValueError('ref_exists')
    if git(repo, 'ls-tree', parent, '--', path):
        raise ValueError('path_exists')
    timestamp = git(repo, 'show', '-s', '--format=%ct', parent)
    if not timestamp.isdigit():
        raise ValueError('timestamp')
    date = datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat().replace('+00:00', 'Z')
    with tempfile.TemporaryDirectory(prefix='get-gitdata-probe-') as folder:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(folder) / 'index'),
                   GIT_AUTHOR_NAME=NAME, GIT_AUTHOR_EMAIL=EMAIL,
                   GIT_COMMITTER_NAME=NAME, GIT_COMMITTER_EMAIL=EMAIL,
                   GIT_AUTHOR_DATE='@' + timestamp + ' +0000', GIT_COMMITTER_DATE='@' + timestamp + ' +0000')
        git(repo, 'read-tree', parent, env=env)
        blob = checked(git(repo, 'hash-object', '-w', '--stdin', data=CONTENT, env=env))
        git(repo, 'update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + path, env=env)
        tree = checked(git(repo, 'write-tree', '--missing-ok', env=env))
        commit = checked(git(repo, 'commit-tree', tree, '-p', parent, '-m', MESSAGE, env=env))
    remote_blob = api.call('POST', '/git/blobs', {'content': base64.b64encode(CONTENT).decode(), 'encoding': 'base64'})
    if remote_blob.get('sha') != blob:
        raise ValueError('blob_mismatch')
    parent_tree = checked(git(repo, 'rev-parse', parent + '^{tree}'))
    remote_tree = api.call('POST', '/git/trees', {'base_tree': parent_tree, 'tree': [
        {'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob}]})
    if remote_tree.get('sha') != tree:
        raise ValueError('tree_mismatch')
    actor = {'name': NAME, 'email': EMAIL, 'date': date}
    remote_commit = api.call('POST', '/git/commits', {'message': MESSAGE + '\n', 'tree': tree,
        'parents': [parent], 'author': actor, 'committer': actor})
    if remote_commit.get('sha') != commit:
        raise ValueError('commit_mismatch')
    if main_ref(api) != parent:
        raise ValueError('parent_changed')
    api.call('POST', '/git/refs', {'ref': ref, 'sha': commit})
    observed = api.call('GET', '/git/ref/' + ref.removeprefix('refs/'))
    if (observed.get('ref') != ref or observed.get('object', {}).get('type') != 'commit'
            or observed.get('object', {}).get('sha') != commit):
        raise ValueError('ref_mismatch')
    check = api.call('GET', '/git/commits/' + commit)
    if (check.get('sha') != commit or check.get('tree', {}).get('sha') != tree
            or [x.get('sha') for x in check.get('parents', [])] != [parent]):
        raise ValueError('commit_readback')
    target = api.call('GET', '/contents/' + path + '?ref=' + commit)
    if (target.get('path') != path or target.get('sha') != blob or target.get('type') != 'file'):
        raise ValueError('path_readback')
    check_blob = api.call('GET', '/git/blobs/' + blob)
    if (check_blob.get('sha') != blob or check_blob.get('encoding') != 'base64'
            or base64.b64decode(check_blob.get('content', ''), validate=False) != CONTENT):
        raise ValueError('blob_readback')
    if main_ref(api) != parent:
        raise ValueError('parent_changed')
    return {'status': 'verified', 'staging_ref': ref}


if __name__ == '__main__':
    try:
        result = run(API(os.environ.get('PRIVATE_VAULT_TOKEN')), Path(os.environ['GITDATA_PROBE_REPO']),
                     os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT'])
    except Exception as exc:
        print(json.dumps(diagnostic(exc)), flush=True)
        raise SystemExit(1) from None
    print(json.dumps(result))
