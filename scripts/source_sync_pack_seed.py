#!/usr/bin/env python3
"""Fetch only pinned Get blob objects over bounded Git protocol v2; no checkout."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import signal
import ssl
import struct
import subprocess
import tempfile
import urllib.request

import source_sync_migration_check as migration
from source_sync_store import IdentityIndex
from probe_getnote_auth import NoRedirect

MIB = 1024 * 1024
MAX_PACK = 128 * MIB
MAX_EXPANDED = 256 * MIB
MAX_BLOB = 32 * MIB
MAX_FILES = 6000
GIT_URL = 'https://github.com/Eddiehhhhh/eddie-llm-wiki.git'
COUNTS = ('seed_files', 'seed_bytes', 'unique_blobs', 'pack_bytes', 'probe_pack_bytes',
          'actual_markdown', 'actual_identity_owners', 'matched_manifest_api_paths')
FLAGS = ('bounded_blob_probe_ok', 'pack_object_set_exact', 'seed_blob_hashes_exact',
         'all_actual_headers_checked', 'manifest_api_paths_exact')
DIAGNOSTIC_FLAGS = ('git_v2_advertised', 'git_fetch_advertised', 'git_sha1_implicit')
STAGES = ('capabilities', 'small_transfer', 'small_index', 'full_transfer', 'full_index', 'identities')
REASONS = {'protocol v2 unavailable':'git_v2_unavailable', 'fetch unavailable':'git_fetch_unavailable',
           'unsupported object format':'git_object_format', 'unexpected response section':'git_response_section',
           'Git transfer rejected':'git_remote_rejected', 'Git fetch rejected':'git_http_response',
           'unexpected pack object count':'pack_object_count', 'offline Git verification failed':'offline_git',
           'pack object set differs':'pack_object_set', 'truncated packet':'wire_truncated',
           'unexpected trailing response':'wire_trailing', 'pack byte limit':'pack_limit',
           'actual identity mapping differs':'actual_identity_mapping'}
ERRORS = ('snapshot_failed', 'protocol_probe_failed', 'pack_transfer_failed',
          'pack_validation_failed', 'actual_identity_failed')


@contextlib.contextmanager
def deadline(seconds):
    def expired(*args):
        raise TimeoutError('bounded Git deadline')
    old = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def pkt(data):
    if not isinstance(data, bytes) or len(data) > 65516:
        raise ValueError('invalid packet')
    return ('%04x' % (len(data) + 4)).encode() + data


class Wire:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.used = stream, maximum, 0

    def exact(self, size):
        if size < 0 or self.used + size > self.maximum:
            raise ValueError('wire limit')
        data = self.stream.read(size)
        self.used += len(data)
        if len(data) != size:
            raise ValueError('truncated packet')
        return data

    def packet(self):
        raw = self.exact(4)
        if any(c not in b'0123456789abcdef' for c in raw):
            raise ValueError('invalid framing')
        size = int(raw, 16)
        if size in (0, 1, 2):
            return size
        if not 4 <= size <= 65520:
            raise ValueError('invalid packet size')
        return self.exact(size - 4)


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                      urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def request(token, endpoint, data=None):
    if endpoint not in ('/info/refs?service=git-upload-pack', '/git-upload-pack'):
        raise ValueError('unsupported Git endpoint')
    # Existing runner credential is used transiently for the same repository only.
    # No credential helper, persisted Git remote, redirect, proxy or child env.
    headers = {'Authorization': 'Basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode(),
               'Git-Protocol': 'version=2', 'User-Agent': 'getnote-bounded-objects/1',
               'Accept-Encoding': 'identity'}
    if data is not None:
        headers['Content-Type'] = 'application/x-git-upload-pack-request'
        headers['Accept'] = 'application/x-git-upload-pack-result'
    return urllib.request.Request(GIT_URL + endpoint, data=data, headers=headers,
                                  method='POST' if data is not None else 'GET')


def capabilities(token, report=None):
    report = {} if report is None else report
    with deadline(30), opener().open(request(token, '/info/refs?service=git-upload-pack'), timeout=20) as response:
        if response.status != 200:
            raise ValueError('Git discovery rejected')
        wire = Wire(response, 65536)
        if wire.packet() != b'version 2\n':
            raise ValueError('protocol v2 unavailable')
        report['git_v2_advertised'] = True
        values = []
        while True:
            value = wire.packet()
            if value == 0:
                break
            if not isinstance(value, bytes):
                raise ValueError('invalid capabilities')
            values.append(value.rstrip(b'\n'))
        if not any(v == b'fetch' or v.startswith(b'fetch=') for v in values):
            raise ValueError('fetch unavailable')
        report['git_fetch_advertised'] = True
        formats = [v for v in values if v.startswith(b'object-format=')]
        # Protocol v2 explicitly defaults to SHA-1 if this optional capability is absent.
        if formats and formats != [b'object-format=sha1']:
            raise ValueError('unsupported object format')
        report['git_sha1_implicit'] = not formats


def decode_pack(stream, destination, objects, maximum):
    wire = Wire(stream, maximum + MIB)
    if wire.packet() != b'packfile\n':
        raise ValueError('unexpected response section')
    total, prefix, progress = 0, bytearray(), 0
    with Path(destination).open('wb') as out:
        while True:
            value = wire.packet()
            if value == 0:
                break
            if not isinstance(value, bytes) or not value:
                raise ValueError('invalid pack section')
            if value[0] == 2:
                progress += len(value)
                if progress > 65536:
                    raise ValueError('excess progress')
                continue
            if value[0] != 1:
                raise ValueError('Git transfer rejected')
            data = value[1:]
            total += len(data)
            if total > maximum:
                raise ValueError('pack byte limit')
            if len(prefix) < 12:
                prefix.extend(data[:12-len(prefix)])
                if len(prefix) == 12:
                    magic, version, count = struct.unpack('>4sII', prefix)
                    if magic != b'PACK' or version not in (2, 3) or count != objects:
                        raise ValueError('unexpected pack object count')
            out.write(data)
    if len(prefix) != 12 or total < 32:
        raise ValueError('incomplete pack')
    # Stateless response end is optional, but other trailing payload is forbidden.
    tail = stream.read(5)
    if tail not in (b'', b'0002'):
        raise ValueError('unexpected trailing response')
    return total


def fetch_pack(token, expected, destination, maximum=MAX_PACK):
    if not 1 <= len(expected) <= MAX_FILES:
        raise ValueError('object count limit')
    body = pkt(b'command=fetch\n') + b'0001' + pkt(b'no-progress\n')
    body += b''.join(pkt(('want ' + migration.sha(oid) + '\n').encode()) for oid in sorted(expected))
    body += pkt(b'done\n') + b'0000'
    if len(body) > 512 * 1024:
        raise ValueError('request size limit')
    with deadline(300), opener().open(request(token, '/git-upload-pack', body), timeout=20) as response:
        if response.status != 200 or response.headers.get_content_type() != 'application/x-git-upload-pack-result':
            raise ValueError('Git fetch rejected')
        return decode_pack(response, destination, len(expected), maximum)


def child_limits():
    resource.setrlimit(resource.RLIMIT_AS, (1024 * MIB, 1024 * MIB))
    resource.setrlimit(resource.RLIMIT_FSIZE, (300 * MIB, 300 * MIB))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))


def git_run(directory, arguments, stdin=None, stdout=None):
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                   'HOME': str(directory), 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_LAZY_FETCH': '1',
                   'GIT_TERMINAL_PROMPT': '0', 'GIT_DIR': str(directory)}
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *arguments],
                            stdin=stdin, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
                            env=environment, timeout=125, preexec_fn=child_limits)
    if result.returncode:
        raise ValueError('offline Git verification failed')
    return result.stdout


def expected_blobs(blobs):
    if not 1 <= len(blobs) <= MAX_FILES or sum(b['size'] for b in blobs.values()) > MAX_EXPANDED:
        raise ValueError('seed size limit')
    expected = {}
    for path, item in blobs.items():
        migration.tree_path(path)
        oid = migration.sha(item['sha'])
        size = item['size']
        if type(size) is not int or not 0 <= size <= MAX_BLOB or (oid in expected and expected[oid] != size):
            raise ValueError('invalid blob size')
        expected[oid] = size
    return expected


def verify_materialize(pack, git_dir, expected, paths, root):
    git_dir.mkdir()
    git_run(git_dir, ['init', '--bare', '--template=', str(git_dir)])
    with Path(pack).open('rb') as source:
        git_run(git_dir, ['index-pack', '--stdin', '--strict', '--max-input-size=' + str(MAX_PACK)], stdin=source)
    listing = git_run(git_dir, ['cat-file', '--batch-all-objects', '--batch-check=%(objectname) %(objecttype) %(objectsize)'])
    actual = {}
    for line in listing.decode('ascii').splitlines():
        oid, kind, size = line.split()
        if kind != 'blob' or oid in actual:
            raise ValueError('unexpected object')
        actual[oid] = int(size)
    if actual != expected:
        raise ValueError('pack object set differs')
    request_path, batch_path = git_dir / 'wanted', git_dir / 'bodies'
    request_path.write_text(''.join(oid + '\n' for oid in sorted(expected)))
    with request_path.open('rb') as requests, batch_path.open('wb') as output:
        git_run(git_dir, ['cat-file', '--batch'], stdin=requests, stdout=output)
    if batch_path.stat().st_size > MAX_EXPANDED + 128 * MAX_FILES:
        raise ValueError('expanded object limit')
    by_oid = {}
    for path, item in paths.items():
        by_oid.setdefault(item['sha'], []).append(path)
    with batch_path.open('rb') as source:
        for oid, size in sorted(expected.items()):
            if source.readline(128) != (oid + ' blob ' + str(size) + '\n').encode():
                raise ValueError('invalid batch header')
            data = source.read(size)
            if len(data) != size or source.read(1) != b'\n':
                raise ValueError('truncated object')
            if hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + data).hexdigest() != oid:
                raise ValueError('blob identity mismatch')
            for relative in by_oid[oid]:
                target = root / migration.tree_path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        if source.read(1):
            raise ValueError('extra object data')
    request_path.unlink()
    batch_path.unlink()


def seed(root, work, token, blobs, report):
    expected = expected_blobs(blobs)
    report['seed_stage'] = 'capabilities'
    capabilities(token, report)
    # First prove exact-object transfer on a <=64KiB Get Markdown; no wider fallback.
    options = [(p, b) for p, b in blobs.items() if p.endswith('.md') and 0 < b['size'] <= 65536]
    if not options:
        raise ValueError('no bounded probe')
    path, blob = min(options, key=lambda pair: pair[1]['size'])
    probe = work / 'probe.pack'
    report['seed_stage'] = 'small_transfer'
    report['probe_pack_bytes'] = fetch_pack(token, {blob['sha']:blob['size']}, probe, maximum=2*MIB)
    report['seed_stage'] = 'small_index'
    verify_materialize(probe, work/'probe.git', {blob['sha']:blob['size']}, {path:blob}, work/'probe')
    report['bounded_blob_probe_ok'] = True
    pack = work / 'source.pack'
    report['seed_stage'] = 'full_transfer'
    report['pack_bytes'] = fetch_pack(token, expected, pack)
    report['seed_stage'] = 'full_index'
    verify_materialize(pack, work/'objects.git', expected, blobs, root)
    report.update(seed_files=len(blobs), seed_bytes=sum(b['size'] for b in blobs.values()),
                  unique_blobs=len(expected), pack_object_set_exact=True, seed_blob_hashes_exact=True)


def audit_actual(root, mapping, report):
    index = IdentityIndex(root, 'getnote', 'note_id')
    if any(index.paths.get(ident) != root / path for ident, path in mapping.items()):
        raise ValueError('actual identity mapping differs')
    report.update(actual_markdown=len(list(root.rglob('*.md'))), actual_identity_owners=len(index.paths),
                  matched_manifest_api_paths=len(mapping), all_actual_headers_checked=True,
                  manifest_api_paths_exact=True)


def safe_report(value):
    result = {'scope':'pinned_get_blob_seed', 'status':'failed', 'source_complete':False, 'published':False}
    if not isinstance(value, dict):
        return result
    for key in COUNTS:
        if type(value.get(key)) is int and 0 <= value[key] <= MAX_EXPANDED:
            result[key] = value[key]
    for key in FLAGS + DIAGNOSTIC_FLAGS:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if value.get('status') == 'real_seed_ok' and all(result.get(k) is True for k in FLAGS):
        result['status'] = 'real_seed_ok'
    if value.get('error_code') in ERRORS:
        result['error_code'] = value['error_code']
    if value.get('seed_stage') in STAGES:
        result['seed_stage'] = value['seed_stage']
    if value.get('failure_reason') in REASONS.values():
        result['failure_reason'] = value['failure_reason']
    if type(value.get('http_status')) is int and 400 <= value['http_status'] <= 599:
        result['http_status'] = value['http_status']
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish-report', type=Path)
    args = parser.parse_args()
    if args.publish_report:
        try:
            value = json.loads(args.publish_report.read_text())
        except (ValueError, OSError):
            value = None
        print(json.dumps(safe_report(value), indent=2))
        return 0
    report, stage = {}, 'snapshot_failed'
    try:
        token, commit, blobs, manifest, mapping, meta = migration.private_snapshot()
        with tempfile.TemporaryDirectory(prefix='private-get-pack-') as directory:
            work = Path(directory)
            root = work / 'vault' / 'Raw' / '03 Get'
            root.mkdir(parents=True, mode=0o700)
            stage = 'protocol_probe_failed'
            seed(root, work, token, blobs, report)
            stage = 'actual_identity_failed'
            report['seed_stage'] = 'identities'
            audit_actual(root, mapping, report)
            report['status'] = 'real_seed_ok'
    except (Exception, SystemExit) as error:
        if stage == 'protocol_probe_failed' and report.get('bounded_blob_probe_ok'):
            stage = 'pack_validation_failed'
        report['error_code'] = stage
        if str(error) in REASONS:
            report['failure_reason'] = REASONS[str(error)]
        if isinstance(error, urllib.error.HTTPError):
            report['http_status'] = error.code
    safe = safe_report(report)
    print(json.dumps(safe))
    return 0 if safe['status'] == 'real_seed_ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
