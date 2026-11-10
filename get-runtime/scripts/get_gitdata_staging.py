"""Stage one verified private Get candidate without updating main."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import urllib.error
import urllib.request

import get_legacy_manual as m
import get_private_preview_retention as r
import get_pending_gate as pending

MAX_BYTES = m.MAX_CHANGED_BYTES
MAX_REQUEST = 4 * ((MAX_BYTES + 2) // 3) + 1048576
MAX_COMPARE_RESPONSE = 2 * 1024 * 1024
NAME = 'Get local proposal'
EMAIL = 'get-proposal@example.invalid'
MESSAGE = 'Prepare canonical Get transaction with receipt\n'


class StageFailure(Exception):
    def __init__(self, message='gitdata_staging_failed', diagnostic=None):
        super().__init__(message)
        if diagnostic is not None:
            self.api_diagnostic = diagnostic


def fail(operation='stage_validation'):
    raise StageFailure('gitdata_staging_failed', {
        'operation': operation, 'failure': 'guard_rejected'}) from None


def api_diagnostic(method, path, kind, status=None):
    if method == 'GET' and path == '/git/ref/heads/codex/get-pending':
        operation = 'pending_ref'
    elif method == 'GET' and re.fullmatch(r'/git/ref/heads/codex/get-stage-[0-9]{1,21}', path):
        operation = 'stage_ref_readback'
    elif method == 'GET' and path == '/git/matching-refs/heads/codex/get-stage-':
        operation = 'stage_refs'
    elif method == 'GET' and re.fullmatch(r'/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}', path):
        operation = 'ancestry_compare'
    elif method == 'POST' and path == '/git/blobs':
        operation = 'blob_create'
    elif method == 'POST' and path == '/git/trees':
        operation = 'tree_create'
    elif method == 'POST' and path == '/git/commits':
        operation = 'commit_create'
    elif method == 'POST' and path == '/git/refs':
        operation = 'stage_ref_create'
    else:
        return None
    result = {'operation': operation, 'failure': kind}
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    return result


def api_fail(method, path, kind, status=None):
    raise StageFailure('gitdata_staging_failed', api_diagnostic(method, path, kind, status)) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, token):
        if not isinstance(token, str) or not token:
            fail()
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method, path, payload=None, missing=False, list_result=False):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if data is not None and len(data) > MAX_REQUEST:
            fail()
        response_limit = (MAX_COMPARE_RESPONSE if method == 'GET' and
                          re.fullmatch(r'/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}', path)
                          else 1048576)
        request = urllib.request.Request('https://api.github.com/repos/' + r.REPOSITORY + path,
            data=data, method=method, headers={'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'get-private-staging'})
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.status not in (200, 201):
                    api_fail(method, path, 'unexpected_status', response.status)
                if list_result and response.headers.get('Link'):
                    api_fail(method, path, 'paginated', response.status)
                raw = response.read(response_limit + 1)
        except urllib.error.HTTPError as exc:
            if missing and method == 'GET' and exc.code == 404:
                return None
            api_fail(method, path, 'http_error', exc.code)
        except StageFailure:
            raise
        except TimeoutError:
            api_fail(method, path, 'timeout')
        except Exception:
            api_fail(method, path, 'transport_error')
        if len(raw) > response_limit:
            api_fail(method, path, 'response_too_large', response.status)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            api_fail(method, path, 'invalid_json', response.status)
        if not isinstance(value, list if list_result else dict):
            api_fail(method, path, 'invalid_shape', response.status)
        return value

    def graphql(self, query, variables):
        def fail_graphql(kind, status=None):
            error=StageFailure('gitdata_staging_failed', {'operation':'graphql_update','failure':kind,
                **({'http_status':status} if type(status) is int and 100<=status<=599 else {})})
            raise error from None
        data = json.dumps({'query': query, 'variables': variables}, separators=(',', ':')).encode()
        request = urllib.request.Request('https://api.github.com/graphql', data=data, method='POST',
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'User-Agent': 'get-private-staging'})
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.status != 200:
                    fail_graphql('unexpected_status', response.status)
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            fail_graphql('http_error', exc.code)
        except StageFailure:
            raise
        except TimeoutError:
            fail_graphql('timeout')
        except Exception:
            fail_graphql('transport_error')
        if len(raw) > 1024 * 1024:
            fail_graphql('response_too_large', response.status)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            fail_graphql('invalid_json', response.status)
        if not isinstance(value, dict) or value.get('errors') or not isinstance(value.get('data'), dict):
            fail_graphql('invalid_shape', response.status)
        return value['data']

    def list_stage_refs(self):
        return self.call('GET', '/git/matching-refs/heads/codex/get-stage-', list_result=True)


def stage(api, reader, remote, plan, files, run_context, readback, expected_anchor):
    parent = m.a.checked(plan['parent_commit'], 40)
    commit = m.a.checked(plan['candidate_commit'], 40)
    tree = m.a.checked(plan['candidate_tree'], 40)
    if not isinstance(files, dict) or not files or sum(len(v) for v in files.values()) > MAX_BYTES:
        fail()
    paths = [row['path'] for row in plan['changes']]
    if len(paths) != len(set(paths)) or set(paths) != set(files):
        fail()
    for path, data in files.items():
        if not isinstance(path, str) or not path.startswith(r.GET + '/') or not isinstance(data, bytes):
            fail()
        r.safe_path(path)
    run_id, attempt = run_context['run_id'], run_context['run_attempt']
    if type(run_id) is not int or type(attempt) is not int or not 1 <= run_id <= 10**20 or not 1 <= attempt <= 9999:
        fail()
    ref = 'refs/heads/codex/get-stage-' + str(run_id)
    ref_path = '/git/ref/' + ref.removeprefix('refs/')
    m.guard(remote, parent)
    if api.call('GET', ref_path, missing=True) is not None:
        fail('stage_ref_precondition')
    parent_tree = m.a.checked(reader.local.run('rev-parse', parent + '^{tree}').decode().strip(), 40)
    timestamp = reader.local.run('show', '-s', '--format=%ct', parent).decode().strip()
    if not timestamp.isdigit():
        fail('candidate_commit_validation')
    actor_line = (NAME + ' <' + EMAIL + '> ' + timestamp + ' +0000').encode()
    expected = (b'tree ' + tree.encode() + b'\nparent ' + parent.encode() + b'\nauthor ' + actor_line
                + b'\ncommitter ' + actor_line + b'\n\n' + MESSAGE.encode())
    raw = reader.local.run('cat-file', 'commit', commit)
    if raw != expected or hashlib.sha1(b'commit ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != commit:
        fail()
    entries = []
    for path, data in sorted(files.items()):
        oid = r.oid(data)
        result = api.call('POST', '/git/blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
        if result.get('sha') != oid:
            fail('blob_readback')
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': oid})
    result = api.call('POST', '/git/trees', {'base_tree': parent_tree, 'tree': entries})
    if result.get('sha') != tree:
        fail('tree_readback')
    date = datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat().replace('+00:00', 'Z')
    actor = {'name': NAME, 'email': EMAIL, 'date': date}
    result = api.call('POST', '/git/commits', {'message': MESSAGE, 'tree': tree,
        'parents': [parent], 'author': actor, 'committer': actor})
    if result.get('sha') != commit:
        fail('commit_readback')
    m.guard(remote, parent)
    try:
        api.call('POST', '/git/refs', {'ref': ref, 'sha': commit})
    except Exception as create_error:
        observed = api.call('GET', ref_path, missing=True)
    else:
        create_error = None
        observed = api.call('GET', ref_path)
    if not isinstance(observed, dict):
        if isinstance(create_error, StageFailure) and hasattr(create_error, 'api_diagnostic'):
            raise create_error
        fail('stage_ref_readback')
    if observed.get('ref') != ref or observed.get('object', {}).get('sha') != commit or observed.get('object', {}).get('type') != 'commit':
        fail('stage_ref_readback')
    if remote.commit(commit) != {'tree': tree, 'parents': [parent]}:
        fail('commit_object_readback')
    metadata = remote.check_source_budget(commit)
    selected = {}
    for path, data in files.items():
        row = metadata.get(path)
        if row != ('100644', 'blob', r.oid(data), len(data)):
            fail('source_tree_readback')
        selected[path] = row
    if readback(commit, selected) != files:
        fail('content_readback')
    m.guard(remote, parent)
    if pending.exact_ref(api) != expected_anchor:
        pending.fail()
    reader.local.run('merge-base', '--is-ancestor', expected_anchor, commit)
    try:
        remote.local.command('push', '--porcelain', '--no-verify',
            '--force-with-lease=' + pending.REF + ':' + expected_anchor,
            r.REMOTE_URL, commit + ':' + pending.REF)
    except Exception:
        pass
    if pending.exact_ref(api) != commit:
        pending.fail()
    m.guard(remote, parent)


def cleanup_run_ref(api, remote, run_context, commit):
    """One exact-lease deletion after confirmed main readback and publication marker."""
    run_id = run_context['run_id']
    if type(run_id) is not int or not 1 <= run_id <= 10**20:
        fail()
    m.a.checked(commit, 40)
    ref = 'refs/heads/codex/get-stage-' + str(run_id)
    path = '/git/ref/' + ref.removeprefix('refs/')
    observed = api.call('GET', path, missing=True)
    if (not isinstance(observed, dict) or observed.get('ref') != ref
            or observed.get('object', {}).get('type') != 'commit'
            or observed.get('object', {}).get('sha') != commit
            or pending.exact_ref(api) != commit):
        return 'retained_or_unknown'
    main = m.guard(remote)
    if not pending.ancestor(api, commit, main):
        return 'retained_or_unknown'
    try:
        result = remote.local.command('push', '--porcelain', '--no-verify',
            '--force-with-lease=' + ref + ':' + commit, r.REMOTE_URL, ':' + ref)
    except Exception:
        result = None
    try:
        observed = api.call('GET', path, missing=True)
    except Exception:
        return 'retained_or_unknown'
    if result is not None and result.returncode == 0 and observed is None:
        return 'removed'
    return 'retained_or_unknown'
