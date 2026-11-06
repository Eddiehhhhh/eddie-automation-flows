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

MAX_BYTES = m.MAX_CHANGED_BYTES
MAX_REQUEST = 4 * ((MAX_BYTES + 2) // 3) + 1048576
NAME = 'Get local proposal'
EMAIL = 'get-proposal@example.invalid'
MESSAGE = 'Prepare canonical Get transaction with receipt\n'


class StageFailure(Exception):
    pass


def fail():
    raise StageFailure('gitdata_staging_failed') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, token):
        if not isinstance(token, str) or not token:
            fail()
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method, path, payload=None, missing=False):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if data is not None and len(data) > MAX_REQUEST:
            fail()
        request = urllib.request.Request('https://api.github.com/repos/' + r.REPOSITORY + path,
            data=data, method=method, headers={'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'get-private-staging'})
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.status not in (200, 201):
                    fail()
                raw = response.read(1048577)
        except urllib.error.HTTPError as exc:
            if missing and method == 'GET' and exc.code == 404:
                return None
            fail()
        except Exception:
            fail()
        if len(raw) > 1048576:
            fail()
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            fail()
        if not isinstance(value, dict):
            fail()
        return value


def stage(api, reader, remote, plan, files, run_context, readback):
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
        fail()
    parent_tree = m.a.checked(reader.local.run('rev-parse', parent + '^{tree}').decode().strip(), 40)
    timestamp = reader.local.run('show', '-s', '--format=%ct', parent).decode().strip()
    if not timestamp.isdigit():
        fail()
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
            fail()
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': oid})
    result = api.call('POST', '/git/trees', {'base_tree': parent_tree, 'tree': entries})
    if result.get('sha') != tree:
        fail()
    date = datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat().replace('+00:00', 'Z')
    actor = {'name': NAME, 'email': EMAIL, 'date': date}
    result = api.call('POST', '/git/commits', {'message': MESSAGE, 'tree': tree,
        'parents': [parent], 'author': actor, 'committer': actor})
    if result.get('sha') != commit:
        fail()
    m.guard(remote, parent)
    try:
        api.call('POST', '/git/refs', {'ref': ref, 'sha': commit})
    except Exception:
        observed = api.call('GET', ref_path, missing=True)
    else:
        observed = api.call('GET', ref_path)
    if not isinstance(observed, dict):
        fail()
    if observed.get('ref') != ref or observed.get('object', {}).get('sha') != commit or observed.get('object', {}).get('type') != 'commit':
        fail()
    if remote.commit(commit) != {'tree': tree, 'parents': [parent]}:
        fail()
    metadata = remote.check_source_budget(commit)
    selected = {}
    for path, data in files.items():
        row = metadata.get(path)
        if row != ('100644', 'blob', r.oid(data), len(data)):
            fail()
        selected[path] = row
    if readback(commit, selected) != files:
        fail()
    m.guard(remote, parent)
