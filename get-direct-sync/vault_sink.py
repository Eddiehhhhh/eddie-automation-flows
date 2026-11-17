"""Readable, path-bound Markdown plus immutable full-source versions in a private repo."""
import base64
import hashlib
import json
import posixpath
import re
import urllib.parse

from core import GitHubSink, Hold, canonical, requires_binary_assets
from render import source_title, tag_names


def filename(title):
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', '_', title)
    clean = re.sub(r'\s+', ' ', clean).strip(' .')
    clean = clean.encode('utf-8')[:237].decode('utf-8', errors='ignore').rstrip(' .')
    return (clean or 'Untitled') + '.md'


def version_path(snapshot, namespace):
    if (not re.fullmatch(r'[0-9]+', snapshot['id'])
            or not re.fullmatch(r'[0-9a-f]{64}', snapshot['version'])):
        raise Hold('source_identity_or_version_invalid')
    return 'get-direct/' + namespace + '/versions/' + snapshot['id'] + '/' + snapshot['version'] + '.json'


def write_verified(sink, journal, key, relative_path, desired, *, immutable=False, require_managed=False):
    """Compare existing ownership, persist intent, conditionally write, then read every byte."""
    if (relative_path.startswith('/') or '\\' in relative_path
            or any(part in ('', '.', '..') for part in relative_path.split('/'))
            or any(ord(char) < 32 for char in relative_path)):
        raise Hold('managed_source_path_invalid')
    state = journal.get(key) or {}
    if state.get('relative_path', relative_path) != relative_path:
        raise Hold('managed_source_path_changed')
    path = '/repos/' + sink.repo + '/contents/' + urllib.parse.quote(relative_path, safe='/')
    query = '?' + urllib.parse.urlencode({'ref': sink.branch})
    previous = state.get('receipt')
    observed = sink.http.request('GET', path + query)
    body = sink.content(observed) if observed else None
    digest = hashlib.sha256(desired).hexdigest()
    if state.get('phase') == 'write_intent':
        if observed and hashlib.sha256(body).hexdigest() == state.get('sha256'):
            previous = {'sha': observed['sha'], 'sha256': state['sha256'],
                        'path': path, 'relative_path': relative_path}
            journal.set(key, {'phase': 'verified', 'relative_path': relative_path, 'receipt': previous})
        else:
            raise Hold('source_write_requires_reconcile')
    if observed:
        owned = (previous is not None and previous.get('path') == path
                 and previous.get('sha') == observed['sha']
                 and previous.get('sha256') == hashlib.sha256(body).hexdigest())
        if require_managed and not owned:
            raise Hold('source_index_unmanaged_target')
        if immutable and body != desired:
            raise Hold('immutable_source_conflict')
        if previous is not None and not owned:
            raise Hold('markdown_human_edit_conflict')
        if body != desired and not owned:
            raise Hold('markdown_human_edit_conflict')
    elif previous is not None:
        raise Hold('managed_source_target_missing')
    if body != desired:
        journal.set(key, {'phase': 'write_intent', 'relative_path': relative_path,
                          'sha256': digest, 'receipt': previous})
        payload = {'message': 'Update verified Get source',
                   'content': base64.b64encode(desired).decode(), 'branch': sink.branch}
        if observed:
            payload['sha'] = observed['sha']
        observed = sink.put_verified(path, payload, desired,
            uncertain_reason='source_write_requires_reconcile',
            mismatch_reason='managed_source_readback_mismatch')
    result = {'sha': observed['sha'], 'sha256': digest, 'path': path, 'relative_path': relative_path}
    journal.set(key, {'phase': 'verified', 'relative_path': relative_path, 'receipt': result})
    return result


def markdown(snapshot, namespace, raw_prefix, manifest=None, *, relative_path=None):
    note = snapshot['note']
    current = relative_path or raw_prefix + '/' + snapshot['id'] + '/' + filename(source_title(snapshot))
    fields = {'source_system': 'getnote', 'note_id': snapshot['id'], 'title': source_title(snapshot),
              'note_type': note.get('note_type', ''), 'created_at': note.get('created_at', ''),
              'updated_at': note.get('updated_at', ''), 'source_version': snapshot['version'],
              'tags': tag_names(note.get('tags', [])),
              'source_json': posixpath.relpath(version_path(snapshot, namespace), posixpath.dirname(current))}
    text = '---\n' + ''.join(key + ': ' + json.dumps(value, ensure_ascii=False) + '\n'
                             for key, value in fields.items()) + '---\n\n' + note['content'].rstrip() + '\n'
    original = (note.get('audio') or {}).get('original') if isinstance(note.get('audio'), dict) else None
    if isinstance(original, str) and original.strip() and original.strip() != note['content'].strip():
        text += '\n## 录音原文\n\n' + original.rstrip() + '\n'
    if manifest and manifest.get('assets'):
        text += '\n## 附件\n\n'
        for index, asset in enumerate(manifest['assets'], 1):
            target = posixpath.relpath('get-direct/' + namespace + '/assets/' + asset['sha256'] + '.bin',
                                       posixpath.dirname(current))
            text += '- [附件 ' + str(index) + '](' + urllib.parse.quote(target, safe='/.-') + ')\n'
    return text.encode()


class VaultGitHubSink:
    def __init__(self, http, repo, branch, namespace, journal, *, raw_prefix):
        parts = raw_prefix.split('/')
        if (not raw_prefix.startswith('Raw/') or '\\' in raw_prefix
                or any(part in ('', '..', '.') or part.startswith('.') for part in parts)
                or any(ord(char) < 32 for char in raw_prefix)):
            raise Hold('invalid_raw_prefix')
        self.sink = GitHubSink(http, repo, branch, namespace)
        self.journal, self.raw_prefix = journal, raw_prefix

    def sync(self, snapshot, receipt=None):
        sink = self.sink
        source_relative = version_path(snapshot, sink.namespace)
        key = 'markdown:' + sink.repo + ':' + sink.branch + ':' + sink.namespace + ':' + snapshot['id']
        baseline = self.journal.get(key) or {}
        current = baseline.get('relative_path')
        if current is None and baseline:
            old = (baseline.get('receipt') or {}).get('path')
            prefix = '/repos/' + sink.repo + '/contents/'
            current = urllib.parse.unquote(old[len(prefix):]) if isinstance(old, str) and old.startswith(prefix) else None
            if current is None and baseline.get('phase') == 'write_intent':
                current = self.raw_prefix + '/' + snapshot['id'] + '.md'
        if current is None:
            current = self.raw_prefix + '/' + snapshot['id'] + '/' + filename(source_title(snapshot))
        legacy = self.raw_prefix + '/' + snapshot['id'] + '.md'
        if (current != legacy and (posixpath.dirname(current) != self.raw_prefix + '/' + snapshot['id']
                                  or not current.endswith('.md'))):
            raise Hold('managed_source_path_changed')
        if not baseline.get('relative_path'):
            self.journal.set(key, {**baseline, 'phase': baseline.get('phase', 'path_bound'),
                                   'relative_path': current})
        result = sink.sync(snapshot, receipt)
        source = canonical({'owner': 'get-direct-v1/' + sink.namespace, **snapshot}).encode()
        source_key = 'immutable:' + sink.repo + ':' + sink.branch + ':' + source_relative
        immutable = write_verified(sink, self.journal, source_key, source_relative, source, immutable=True)
        immutable['version'] = snapshot['version']
        manifest = self.journal.get('asset_manifest:' + snapshot['id'] + ':' + snapshot['version'])
        body = markdown(snapshot, sink.namespace, self.raw_prefix, manifest, relative_path=current)
        md = write_verified(sink, self.journal, key, current, body)
        md['version'] = snapshot['version']
        return {**result, 'markdown': md, 'relative_path': current,
                'source_title': source_title(snapshot), 'note_type': snapshot['note'].get('note_type', ''),
                'updated_at': snapshot['note'].get('updated_at'), 'source_json': immutable,
                'binary_assets_required': requires_binary_assets(snapshot)}
