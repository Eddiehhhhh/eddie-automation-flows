"""Read-only, externally anchored evidence for exactly three reviewed PNGs.

The caller authenticates the Git publication and the separate evidence anchor.
This module never publishes, enqueues, changes Markdown, or grants upload/page
permission. OfflineGit must have network/lazy fetch disabled. Before an API
operation the caller refreshes main, loads its object metadata, and supplies
that exact commit to check_current (also required by resolve/read_asset).
"""
from copy import deepcopy
import json
import re
import struct
import urllib.parse
import zlib

import get_notion_sink_plan as p

CONTRACT = 'get-notion-three-png-evidence-1'
PREFIX = '.sync/get-notion/assets/'
MANIFEST_PATH = PREFIX + 'reviewed-png-manifest-v1.json'
MAX_MANIFEST = 32 * 1024
MAX_PNG = 5 * 1024 * 1024
MAX_TOTAL = 15 * 1024 * 1024
MAX_DIMENSION = 10000
MAX_PIXELS = 20000000
encoded = p.encoded
digest = p.digest


class Blocked(ValueError):
    pass


def fail(reason):
    raise Blocked(reason) from None


def keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        fail('asset_evidence_schema')


def hash_value(value, width):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{%d}' % width, value):
        fail('asset_evidence_schema')
    return value


def strict_json(data):
    if not isinstance(data, bytes) or len(data) > MAX_MANIFEST:
        fail('asset_evidence_budget')
    def pairs(rows):
        out = {}
        for key, value in rows:
            if key in out: fail('asset_evidence_schema')
            out[key] = value
        return out
    try:
        value = json.loads(data, object_pairs_hook=pairs,
                           parse_constant=lambda _: fail('asset_evidence_schema'))
        if encoded(value) != data: fail('asset_evidence_schema')
        return value
    except Blocked: raise
    except Exception: fail('asset_evidence_schema')


def exact_reference(data, ident):
    """One original Markdown image URL; never resolve/fetch/normalize its URL."""
    try:
        p.source_body(data, ident)
        text = data.decode('utf-8')
        matches = list(re.finditer(r'!\[[^\]\n]*\]\(([^\s)]+)\)', text))
        if len(matches) != 1 or text.count('![') != 1 or re.search(r'<\s*img\b', text, re.I):
            fail('asset_reference_changed')
        reference = matches[0].group(1)
        parsed = urllib.parse.urlsplit(reference)
        if (parsed.scheme != 'https' or parsed.hostname != 'get-notes.umiwi.com'
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment):
            fail('asset_reference_changed')
        return reference
    except Blocked: raise
    except Exception: fail('asset_reference_changed')


def png_shape(data):
    """Bounded PNG container checks, not a claim of visual/content review.

    Only the already-reviewed noninterlaced 8-bit RGB/RGBA subset is admitted.
    The external anchor binds exact bytes; no newly discovered image qualifies.
    """
    if not isinstance(data, bytes) or not 45 <= len(data) <= MAX_PNG or data[:8] != b'\x89PNG\r\n\x1a\n':
        fail('asset_png_invalid')
    offset = 8; seen = []; shape = None
    while offset < len(data):
        if offset + 12 > len(data) or len(seen) >= 2048: fail('asset_png_invalid')
        size = struct.unpack('>I', data[offset:offset + 4])[0]
        end = offset + size + 12
        if end > len(data): fail('asset_png_invalid')
        kind = data[offset + 4:offset + 8]; payload = data[offset + 8:end - 4]
        if zlib.crc32(kind + payload) & 0xffffffff != struct.unpack('>I', data[end - 4:end])[0]:
            fail('asset_png_invalid')
        if kind not in (b'IHDR', b'sRGB', b'IDAT', b'IEND'): fail('asset_png_invalid')
        if not seen and kind != b'IHDR': fail('asset_png_invalid')
        if kind == b'IHDR':
            if seen or size != 13: fail('asset_png_invalid')
            w, h, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
            if (not 1 <= w <= MAX_DIMENSION or not 1 <= h <= MAX_DIMENSION
                    or w * h > MAX_PIXELS or depth != 8 or color not in (2, 6)
                    or (compression, filtering, interlace) != (0, 0, 0)):
                fail('asset_png_invalid')
            shape = (w, h)
        elif kind == b'sRGB':
            if b'sRGB' in seen or b'IDAT' in seen or size != 1 or payload[0] > 3: fail('asset_png_invalid')
        elif kind == b'IDAT':
            if size == 0: fail('asset_png_invalid')
        elif kind == b'IEND':
            if size != 0 or b'IDAT' not in seen or end != len(data): fail('asset_png_invalid')
        seen.append(kind); offset = end
    if not seen or seen[-1] != b'IEND': fail('asset_png_invalid')
    return shape


class AssetEvidence:
    def __init__(self, reader, publication_descriptor, anchor, expected_anchor_sha, current_commit):
        self.reader = reader
        try:
            keys(anchor, ('contract', 'repository', 'publication_commit', 'asset_commit',
                          'manifest_path', 'manifest_blob', 'manifest_sha256'))
            hash_value(expected_anchor_sha, 64)
            if digest(anchor) != expected_anchor_sha or anchor['contract'] != CONTRACT or anchor['repository'] != p.REPOSITORY:
                fail('asset_evidence_anchor')
            for key in ('publication_commit', 'asset_commit', 'manifest_blob'): hash_value(anchor[key], 40)
            hash_value(anchor['manifest_sha256'], 64)
            if anchor['manifest_path'] != MANIFEST_PATH: fail('asset_evidence_anchor')
            if (not isinstance(publication_descriptor, dict)
                    or publication_descriptor.get('repository') != p.REPOSITORY
                    or publication_descriptor.get('commit') != anchor['publication_commit']
                    or not isinstance(publication_descriptor.get('records'), dict)
                    or not isinstance(publication_descriptor.get('changed_content_ids'), list)
                    or any(not isinstance(i, str) for i in publication_descriptor['changed_content_ids'])
                    or publication_descriptor['changed_content_ids'] != sorted(set(publication_descriptor['changed_content_ids']))):
                fail('asset_source_changed')
            self._anchor = deepcopy(anchor); self._evidence_sha = expected_anchor_sha
            self._publication = deepcopy(publication_descriptor)
            commit = anchor['asset_commit']
            parents = reader.local.run('rev-list', '--parents', '-n', '1', commit).decode().split()
            if len(parents) != 2 or parents[0] != commit: fail('asset_evidence_delta')
            parent = parents[1]
            reader.local.run('merge-base', '--is-ancestor', anchor['publication_commit'], parent)
            before = reader.tree(parent); after = reader.tree(commit)
            if after.get(MANIFEST_PATH) != ('100644', 'blob', anchor['manifest_blob']): fail('asset_evidence_anchor')
            raw = reader.read(commit, MANIFEST_PATH, MAX_MANIFEST)
            if p.git_blob(raw) != anchor['manifest_blob'] or digest(raw) != anchor['manifest_sha256']:
                fail('asset_evidence_anchor')
            manifest = strict_json(raw)
            keys(manifest, ('contract', 'repository', 'publication_commit', 'records'))
            if (manifest['contract'] != CONTRACT or manifest['repository'] != p.REPOSITORY
                    or manifest['publication_commit'] != anchor['publication_commit']
                    or not isinstance(manifest['records'], dict) or len(manifest['records']) != 3):
                fail('asset_evidence_schema')
            self._records = deepcopy(manifest['records'])
            source_tree = reader.tree(anchor['publication_commit'])
            paths = set(); source_paths = set(); total = 0
            for ident, row in self._records.items():
                p.note_id(ident); keys(row, ('source', 'reference_sha256', 'asset'))
                keys(row['source'], ('path', 'blob', 'sha256'))
                keys(row['asset'], ('path', 'blob', 'sha256', 'bytes', 'mime', 'width', 'height'))
                source = row['source']; asset = row['asset']
                path = p.safe_path(source['path']); hash_value(source['blob'], 40); hash_value(source['sha256'], 64)
                if not path.lower().endswith(('.md', '.markdown')) or path in source_paths: fail('asset_evidence_schema')
                source_paths.add(path)
                if ((ident in publication_descriptor['records'] and publication_descriptor['records'][ident] != source)
                        or ident not in publication_descriptor['changed_content_ids']): fail('asset_source_changed')
                if source_tree.get(path) != ('100644', 'blob', source['blob']) or before.get(path) != source_tree.get(path):
                    fail('asset_source_changed')
                data = reader.read(anchor['publication_commit'], path, p.MAX_NOTE)
                if p.git_blob(data) != source['blob'] or digest(data) != source['sha256']: fail('asset_source_changed')
                hash_value(row['reference_sha256'], 64)
                if digest(exact_reference(data, ident).encode('utf-8')) != row['reference_sha256']: fail('asset_reference_changed')
                hash_value(asset['blob'], 40); hash_value(asset['sha256'], 64)
                if asset['path'] != PREFIX + asset['sha256'] + '.png' or asset['path'] in paths: fail('asset_evidence_schema')
                paths.add(asset['path'])
                if (type(asset['bytes']) is not int or not 45 <= asset['bytes'] <= MAX_PNG or asset['mime'] != 'image/png'
                        or any(type(asset[k]) is not int or not 1 <= asset[k] <= MAX_DIMENSION for k in ('width', 'height'))):
                    fail('asset_evidence_schema')
                total += asset['bytes']
                if total > MAX_TOTAL: fail('asset_evidence_budget')
                if after.get(asset['path']) != ('100644', 'blob', asset['blob']): fail('asset_evidence_delta')
            wanted = paths | {MANIFEST_PATH}
            changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
            if changed != wanted or any(path in before for path in wanted): fail('asset_evidence_delta')
            if reader.source_get_tree(parent) != reader.source_get_tree(commit): fail('asset_evidence_delta')
            self.check_current(current_commit)
            # Exactly these three asset blobs, never inherited blobs or URLs.
            for ident in self._records:
                self._read_asset(self._binding(ident))
        except Blocked: raise
        except Exception: fail('asset_evidence_invalid')

    @property
    def descriptor(self):
        return deepcopy(self._anchor)

    @property
    def asset_tree(self):
        return {row['asset']['path']: row['asset']['blob'] for row in self._records.values()}

    @property
    def evidence_sha256(self):
        return self._evidence_sha

    def check_current(self, current_commit):
        try:
            hash_value(current_commit, 40)
            if self.reader.local.run('rev-parse', 'refs/heads/main').decode().strip() != current_commit:
                fail('asset_current_changed')
            self.reader.local.run('merge-base', '--is-ancestor', self._anchor['asset_commit'], current_commit)
            tree = self.reader.tree(current_commit)
            exact = {MANIFEST_PATH: self._anchor['manifest_blob'], **self.asset_tree}
            exact.update({row['source']['path']: row['source']['blob'] for row in self._records.values()})
            if any(tree.get(path) != ('100644', 'blob', blob) for path, blob in exact.items()): fail('asset_current_changed')
            return True
        except Blocked: raise
        except Exception: fail('asset_current_changed')

    def _binding(self, ident):
        if not isinstance(ident, str) or ident not in self._records: fail('asset_reference_changed')
        row = self._records[ident]
        return {'evidence_sha256': self._evidence_sha, 'asset_commit': self._anchor['asset_commit'],
                'manifest_sha256': self._anchor['manifest_sha256'], 'source_commit': self._anchor['publication_commit'],
                'note_id': ident, 'source': deepcopy(row['source']), 'reference_sha256': row['reference_sha256'],
                'asset': deepcopy(row['asset'])}

    def resolve(self, note_id, exact_md_bytes, exact_reference_value, *, current_commit):
        self.check_current(current_commit)
        binding = self._binding(note_id)
        if note_id not in self._publication['records']: fail('asset_source_changed')
        if (not isinstance(exact_md_bytes, bytes) or len(exact_md_bytes) > p.MAX_NOTE
                or digest(exact_md_bytes) != binding['source']['sha256']
                or p.git_blob(exact_md_bytes) != binding['source']['blob']): fail('asset_source_changed')
        if (not isinstance(exact_reference_value, str)
                or exact_reference(exact_md_bytes, note_id) != exact_reference_value
                or digest(exact_reference_value.encode('utf-8')) != binding['reference_sha256']):
            fail('asset_reference_changed')
        return binding

    def _read_asset(self, binding):
        try:
            if not isinstance(binding, dict) or encoded(binding) != encoded(self._binding(binding.get('note_id'))): fail('asset_evidence_anchor')
            asset = binding['asset']
            data = self.reader.read(self._anchor['asset_commit'], asset['path'], asset['bytes'])
            if len(data) != asset['bytes'] or p.git_blob(data) != asset['blob'] or digest(data) != asset['sha256']:
                fail('asset_evidence_anchor')
            if png_shape(data) != (asset['width'], asset['height']): fail('asset_png_invalid')
            return data
        except Blocked: raise
        except Exception: fail('asset_evidence_invalid')

    def read_asset(self, binding, *, current_commit):
        self.check_current(current_commit)
        if not isinstance(binding, dict) or not isinstance(binding.get('note_id'), str) or binding['note_id'] not in self._publication['records']:
            fail('asset_source_changed')
        return self._read_asset(binding)
