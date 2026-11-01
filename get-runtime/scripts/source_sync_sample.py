"""Select and seed exactly two existing private Get notes; never publish content."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import urllib.parse
import urllib.request

from probe_getnote_auth import NoRedirect, deadline
from source_sync_store import frontmatter

REPOSITORY = 'Eddiehhhhh/eddie-llm-wiki'
API_ROOT = 'https://api.github.com/repos/' + REPOSITORY
MANIFEST_PATH = 'Raw/03 Get/03 State/manifest.json'
MANIFEST_MAX_BYTES = 2 * 1024 * 1024
NOTE_MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 5000
KINDS = {'ordinary': frozenset(('plain_text',)),
         'recording': frozenset(('recorder_audio', 'recorder_flash_audio'))}
CATEGORY_FOLDERS = {'ordinary': '03 纯文本记录', 'recording': '99 录音卡笔记'}


class FixedSampleError(ValueError):
    """A fixed public failure class; no private values in its message."""


def strict_id(value):
    if type(value) is not str or re.fullmatch(r'[0-9]{1,64}', value) is None:
        raise FixedSampleError('invalid fixed sample')
    return value


def detail_id(note):
    if not isinstance(note, dict):
        raise FixedSampleError('invalid fixed sample')
    # The plugin cache can contain a rounded numeric id beside correct note_id.
    # A string note_id is authoritative; a conflicting string alias is not.
    value = strict_id(note.get('note_id', note.get('id')))
    alias = note.get('id')
    if isinstance(alias, str) and strict_id(alias) != value:
        raise FixedSampleError('invalid fixed sample')
    return value


def validate_pair(value):
    if not isinstance(value, dict) or set(value) != {'notes'}:
        raise FixedSampleError('invalid fixed sample')
    notes = value['notes']
    if not isinstance(notes, list) or len(notes) != 2:
        raise FixedSampleError('invalid fixed sample')
    result = {}
    for note in notes:
        if not isinstance(note, dict) or set(note) != {'kind', 'note_id', 'note_type'}:
            raise FixedSampleError('invalid fixed sample')
        kind = note['kind']
        if type(kind) is not str or kind not in KINDS or kind in result:
            raise FixedSampleError('invalid fixed sample')
        if type(note['note_type']) is not str or note['note_type'] not in KINDS[kind]:
            raise FixedSampleError('invalid fixed sample')
        strict_id(note['note_id'])
        result[kind] = note.copy()
    if len({n['note_id'] for n in result.values()}) != 2:
        raise FixedSampleError('invalid fixed sample')
    return [result[kind] for kind in KINDS]


def load_pair(path):
    path = Path(path)
    if path.is_symlink():
        raise FixedSampleError('invalid fixed sample')
    try:
        with path.open('rb') as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise FixedSampleError('invalid fixed sample')
        return validate_pair(json.loads(data))
    except (OSError, ValueError, TypeError):
        raise FixedSampleError('invalid fixed sample') from None


def validate_detail(note, expected):
    if (detail_id(note) != expected['note_id'] or
            note.get('note_type') != expected['note_type'] or
            not isinstance(note.get('content'), str)):
        raise FixedSampleError('invalid fixed sample')


def safe_relative_path(value):
    if (type(value) is not str or len(value.encode('utf-8')) > 1024 or
            '\\' in value or any(ord(c) < 32 for c in value)):
        raise FixedSampleError('invalid private sample')
    path = PurePosixPath(value)
    if (path.is_absolute() or not value or any(p in ('', '.', '..') for p in value.split('/'))
            or path.suffix != '.md'):
        raise FixedSampleError('invalid private sample')
    return path


def select_manifest_pair(manifest):
    if (not isinstance(manifest, dict) or manifest.get('source_system') != 'getnote'
            or not isinstance(manifest.get('notes'), list)
            or not 2 <= len(manifest['notes']) <= MAX_ENTRIES
            or type(manifest.get('count')) is not int
            or manifest['count'] != len(manifest['notes'])):
        raise FixedSampleError('invalid private sample')
    candidates = {kind: [] for kind in KINDS}
    ids, paths = set(), set()
    for note in manifest['notes']:
        if not isinstance(note, dict):
            raise FixedSampleError('invalid private sample')
        for kind, types in KINDS.items():
            if note.get('note_type') not in types:
                continue
            ident = strict_id(note.get('note_id'))
            path = safe_relative_path(note.get('path'))
            if ident in ids or str(path) in paths:
                raise FixedSampleError('invalid private sample')
            ids.add(ident)
            paths.add(str(path))
            if path.parts[0] != CATEGORY_FOLDERS[kind]:
                continue
            candidates[kind].append({'kind': kind, 'note_id': ident,
                                     'note_type': note['note_type'], 'path': str(path)})
    if any(not values for values in candidates.values()):
        raise FixedSampleError('invalid private sample')
    # Freeze one deterministic pair before either Get pass; do not page Get.
    return [min(candidates[kind], key=lambda n: n['path']) for kind in KINDS]


def github_read(endpoint, token, limit, raw=False):
    if (endpoint and not endpoint.startswith('/')) or '://' in endpoint or '\\' in endpoint:
        raise FixedSampleError('invalid private sample')
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    headers = {'Authorization': 'Bearer ' + token,
               'Accept': 'application/vnd.github.raw+json' if raw else 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'getnote-fixed-shadow/1'}
    request = urllib.request.Request(API_ROOT + endpoint, headers=headers, method='GET')
    # One bounded read; no credential-bearing redirects, proxy, retries or URLs in logs.
    with deadline():
        with opener.open(request, timeout=10) as response:
            data = response.read(limit + 1)
    if len(data) > limit:
        raise FixedSampleError('invalid private sample')
    return data


def prepare_private_sample(root, pair_path):
    token = os.environ.get('PRIVATE_VAULT_TOKEN')
    if not token:
        raise FixedSampleError('invalid private sample')
    metadata = json.loads(github_read('', token, 65536))
    if metadata.get('private') is not True:
        raise FixedSampleError('invalid private sample')
    ref = json.loads(github_read('/git/ref/heads/main', token, 65536))
    commit = ref.get('object', {}).get('sha')
    if (ref.get('object', {}).get('type') != 'commit' or type(commit) is not str
            or re.fullmatch(r'[a-f0-9]{40}', commit) is None):
        raise FixedSampleError('invalid private sample')

    def contents(path, maximum):
        endpoint = '/contents/' + urllib.parse.quote(path, safe='/') + '?ref=' + commit
        return github_read(endpoint, token, maximum, raw=True)

    manifest = json.loads(contents(MANIFEST_PATH, MANIFEST_MAX_BYTES))
    selected = select_manifest_pair(manifest)
    files = [(note, contents('Raw/03 Get/' + note['path'], NOTE_MAX_BYTES)) for note in selected]
    seeded = []
    for note, data in files:
        path = root / 'Raw' / '03 Get' / note['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o600)
        fields = frontmatter(path)
        if (fields.get('source_system') != 'getnote' or fields.get('note_id') != note['note_id']
                or fields.get('note_type') != note['note_type']):
            raise FixedSampleError('invalid private sample')
        seeded.append(path)
    pair = {'notes': [{k: n[k] for k in ('kind', 'note_id', 'note_type')} for n in selected]}
    validate_pair(pair)
    pair_path.write_text(json.dumps(pair), encoding='utf-8')
    os.chmod(pair_path, 0o600)
    return seeded
