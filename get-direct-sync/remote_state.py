"""Private Git state branch: atomic lease + full SQLite checkpoint, no lease stealing.

The branch must already exist and be dedicated to this namespace. The caller uses
an empty private runner directory, acquire(path), Store(path, checkpoint=checkpoint),
and finally release(). A crashed lease needs explicit owner recovery; no expiry.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import urllib.parse
import zlib

from core import CheckpointFailure, Hold, canonical

MAX_COMPRESSED = 8 * 1024 * 1024
MAX_RAW = 128 * 1024 * 1024
STATE_FILE = 'get-direct-state.json'
SCHEMA = 'get-direct-remote-state-v1'


def git_blob_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def unpack_checkpoint(checkpoint):
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, dict) or checkpoint.get('encoding') != 'zlib+base64':
        raise Hold('remote_checkpoint_invalid')
    size = checkpoint.get('bytes')
    if type(size) is not int or not 0 < size <= MAX_RAW:
        raise Hold('remote_checkpoint_size')
    try:
        packed = base64.b64decode(checkpoint['data'], validate=True)
        if len(packed) > MAX_COMPRESSED or hashlib.sha256(packed).hexdigest() != checkpoint['compressed_sha256']:
            raise Hold('remote_checkpoint_corrupt')
        dec = zlib.decompressobj()
        raw = dec.decompress(packed, MAX_RAW + 1)
        if len(raw) != size or not dec.eof or dec.unused_data or dec.unconsumed_tail:
            raise Hold('remote_checkpoint_corrupt')
        if not raw.startswith(b'SQLite format 3\0') or hashlib.sha256(raw).hexdigest() != checkpoint['sha256']:
            raise Hold('remote_checkpoint_corrupt')
        return raw
    except Hold:
        raise
    except Exception:
        raise Hold('remote_checkpoint_corrupt') from None


class RemoteState:
    def __init__(self, http, repo, branch, namespace, owner):
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_-]{0,120}', branch) or
                '//' in branch or branch.endswith('/') or branch in ('main', 'master') or
                not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', namespace) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', owner)):
            raise Hold('remote_state_configuration')
        self.http, self.repo, self.branch = http, repo, branch
        self.binding = {'repo': repo, 'branch': branch, 'namespace': namespace}
        self.owner = owner
        self.lease = None
        self.path = None
        self.prefix = '/repos/' + repo
        self.ref = 'heads/' + urllib.parse.quote(branch, safe='/')

    def _read(self):
        repo = self.http.request('GET', self.prefix)
        if not repo or repo.get('private') is not True or repo.get('default_branch') == self.branch:
            raise Hold('private_state_branch_required')
        ref = self.http.request('GET', self.prefix + '/git/ref/' + self.ref)
        if not ref:
            raise Hold('remote_state_branch_missing')
        head = ref['object']['sha']
        commit = self.http.request('GET', self.prefix + '/git/commits/' + head)
        tree_sha = commit['tree']['sha']
        tree = self.http.request('GET', self.prefix + '/git/trees/' + tree_sha)
        if tree.get('truncated'):
            raise Hold('remote_state_tree_incomplete')
        entries = [x for x in tree['tree'] if x['path'] == STATE_FILE]
        if not entries:
            return head, tree_sha, {'schema': SCHEMA, 'binding': self.binding,
                                    'lease': None, 'checkpoint': None}
        if len(entries) != 1 or entries[0].get('type') != 'blob' or entries[0].get('mode') != '100644':
            raise Hold('remote_state_file_invalid')
        entry = entries[0]
        blob = self.http.request('GET', self.prefix + '/git/blobs/' + entry['sha'])
        try:
            if blob.get('encoding') != 'base64':
                raise Hold('remote_state_blob_invalid')
            raw = base64.b64decode(blob['content'].replace('\n', ''), validate=True)
            if len(raw) > MAX_COMPRESSED * 4 // 3 + 32768 or git_blob_sha(raw) != entry['sha'] or blob.get('sha') != entry['sha']:
                raise Hold('remote_state_blob_invalid')
            doc = json.loads(raw)
        except Hold:
            raise
        except Exception:
            raise Hold('remote_state_blob_invalid') from None
        if (doc.get('schema') != SCHEMA or doc.get('binding') != self.binding or
                set(doc) != {'schema', 'binding', 'lease', 'checkpoint'}):
            raise Hold('remote_state_binding_mismatch')
        lease = doc['lease']
        if lease is not None and (not isinstance(lease, dict) or set(lease) != {'owner', 'lease_id'} or
                                  not isinstance(lease['owner'], str) or
                                  not isinstance(lease['lease_id'], str)):
            raise Hold('remote_state_lease_invalid')
        unpack_checkpoint(doc['checkpoint'])
        return head, tree_sha, doc

    def _publish(self, head, tree_sha, doc):
        body = canonical(doc).encode()
        blob = self.http.request('POST', self.prefix + '/git/blobs', {
            'encoding': 'base64', 'content': base64.b64encode(body).decode()})
        if blob.get('sha') != git_blob_sha(body):
            raise Hold('remote_state_blob_write_mismatch')
        tree = self.http.request('POST', self.prefix + '/git/trees', {
            'base_tree': tree_sha, 'tree': [{'path': STATE_FILE, 'mode': '100644',
                                          'type': 'blob', 'sha': blob['sha']}]})
        commit = self.http.request('POST', self.prefix + '/git/commits', {
            'message': 'Checkpoint private Get synchronization state',
            'tree': tree['sha'], 'parents': [head]})
        desired = commit['sha']
        try:
            self.http.request('PATCH', self.prefix + '/git/refs/' + self.ref,
                              {'sha': desired, 'force': False})
        except Exception:
            observed = self.http.request('GET', self.prefix + '/git/ref/' + self.ref)
            if not observed or observed.get('object', {}).get('sha') != desired:
                raise Hold('remote_state_ref_update_unconfirmed') from None
        current, _, readback = self._read()
        if current != desired or readback != doc:
            raise Hold('remote_state_readback_mismatch')
        return desired

    def acquire(self, sqlite_path):
        if self.lease is not None:
            raise Hold('remote_state_already_acquired')
        path = Path(sqlite_path)
        parent = path.parent
        if (not path.is_absolute() or not parent.is_dir() or parent.is_symlink() or
                parent.absolute() != parent.resolve() or
                parent.stat().st_uid != os.getuid() or stat.S_IMODE(parent.stat().st_mode) & 0o077 or
                path.exists() or path.is_symlink()):
            raise Hold('remote_state_private_empty_path_required')
        head, tree, doc = self._read()
        if doc['lease'] is not None:
            raise Hold('remote_state_owned_no_takeover')
        raw = unpack_checkpoint(doc['checkpoint'])
        lease = {'owner': self.owner, 'lease_id': secrets.token_hex(16)}
        doc['lease'] = lease
        self._publish(head, tree, doc)
        self.lease, self.path = lease, path
        if raw is not None:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            if hashlib.sha256(path.read_bytes()).hexdigest() != doc['checkpoint']['sha256']:
                raise Hold('remote_state_restore_mismatch')
            check = sqlite3.connect('file:' + urllib.parse.quote(str(path)) + '?mode=ro', uri=True)
            try:
                if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise Hold('remote_state_sqlite_corrupt')
            finally:
                check.close()
        return {'restored': raw is not None, 'checkpoint_sha256':
                doc['checkpoint']['sha256'] if doc['checkpoint'] else None}

    def checkpoint(self, connection):
        try:
            if self.lease is None or self.path is None or connection.in_transaction:
                raise Hold('remote_checkpoint_context')
            head, tree, doc = self._read()
            if doc['lease'] != self.lease:
                raise Hold('remote_state_lease_lost')
            fd, backup_path = tempfile.mkstemp(prefix='.sqlite-checkpoint-', dir=str(self.path.parent))
            os.close(fd)
            try:
                backup = sqlite3.connect(backup_path)
                try:
                    connection.backup(backup)
                finally:
                    backup.close()
                if os.stat(backup_path).st_size > MAX_RAW:
                    raise Hold('remote_checkpoint_size')
                raw = Path(backup_path).read_bytes()
            finally:
                os.unlink(backup_path)
            packed = zlib.compress(raw)
            if len(packed) > MAX_COMPRESSED:
                raise Hold('remote_checkpoint_compressed_size')
            checkpoint = {'encoding': 'zlib+base64', 'bytes': len(raw),
                          'sha256': hashlib.sha256(raw).hexdigest(),
                          'compressed_sha256': hashlib.sha256(packed).hexdigest(),
                          'data': base64.b64encode(packed).decode()}
            if checkpoint != doc['checkpoint']:
                doc['checkpoint'] = checkpoint
                self._publish(head, tree, doc)
        except Exception:
            raise CheckpointFailure('remote_checkpoint_failed') from None

    def release(self):
        if self.lease is None:
            raise Hold('remote_state_not_owned')
        head, tree, doc = self._read()
        if doc['lease'] != self.lease:
            raise Hold('remote_state_release_not_owner')
        doc['lease'] = None
        self._publish(head, tree, doc)
        self.lease = None
