"""Private Git state branch: atomic lease + full SQLite checkpoint, no lease stealing.

The branch must already exist and be dedicated to this namespace. The caller uses
an empty private runner directory, acquire(path), Store(path, checkpoint=checkpoint),
and finally release(). Only verified completed attempts of this same workflow
may recover a crashed lease; elapsed time never authorizes takeover.
"""
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import time
import urllib.parse
import zlib

from core import CheckpointFailure, Hold, canonical
from retry import HTTPFailure, RetryStopped, TransportFailure

MAX_COMPRESSED = 8 * 1024 * 1024
MAX_RAW = 128 * 1024 * 1024
STATE_FILE = 'get-direct-state.json'
SCHEMA = 'get-direct-remote-state-v1'
WORKFLOW_PATH = '.github/workflows/get-notion-incremental.yml'
_CHECKPOINT_HOLDS = frozenset(('remote_checkpoint_context', 'remote_checkpoint_size',
    'remote_checkpoint_compressed_size', 'remote_checkpoint_invalid', 'remote_checkpoint_corrupt',
    'private_state_branch_required', 'remote_state_branch_missing', 'remote_state_tree_incomplete',
    'remote_state_file_invalid', 'remote_state_blob_invalid', 'remote_state_binding_mismatch',
    'remote_state_lease_invalid', 'remote_state_lease_lost', 'remote_state_blob_write_mismatch',
    'remote_state_ref_update_unconfirmed', 'remote_state_contents_update_unconfirmed',
    'remote_state_publish_head_changed', 'remote_state_publish_parent_changed', 'remote_state_readback_old_head',
    'remote_state_readback_other_head', 'remote_state_readback_content_mismatch'))


def _checkpoint_reason(error):
    """Only trusted exception types and fixed local codes may reach public logs."""
    if isinstance(error, _UnconfirmedContents):
        return error.checkpoint_reason
    if isinstance(error, HTTPFailure) and type(error.status) is int and 100 <= error.status <= 599:
        return 'checkpoint_http_' + str(error.status)
    if isinstance(error, (TransportFailure, TimeoutError)):
        return 'checkpoint_transport_uncertain'
    if isinstance(error, RetryStopped) and error.reason in ('attempt_limit', 'elapsed_budget', 'server_delay_exceeds_budget'):
        return 'checkpoint_retry_' + error.reason
    if (isinstance(error, Hold) and len(error.args) == 1 and
            isinstance(error.args[0], str) and error.args[0] in _CHECKPOINT_HOLDS):
        code = error.args[0]
        if code.startswith('remote_state_'):
            code = code[len('remote_state_'):]
        elif code.startswith('remote_checkpoint_'):
            code = code[len('remote_checkpoint_'):]
        return 'checkpoint_' + code
    if isinstance(error, sqlite3.Error):
        return 'checkpoint_sqlite_error'
    if isinstance(error, OSError):
        return 'checkpoint_os_error'
    return 'checkpoint_other'


class _UnconfirmedContents(Hold):
    def __init__(self, update_error):
        super().__init__('remote_state_contents_update_unconfirmed')
        self.checkpoint_reason = _checkpoint_reason(update_error)


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
        self.recovery_evidence = None
        self._stage = 'context'
        self._pinned_state = None
        self._verified_document = None
        self.prefix = '/repos/' + repo
        self.ref = 'heads/' + urllib.parse.quote(branch, safe='/')

    def _request(self, stage, method, path, body=None):
        self._stage = stage
        return self.http.request(method, path, body)

    def _read_ref(self, stage, known_parents=()):
        path = self.prefix + '/git/ref/' + self.ref
        ref = self._request(stage, 'GET', path)
        # A previously verified parent can briefly reappear after GitHub advances the ref.
        for delay in (1, 2, 4):
            if (ref or {}).get('object', {}).get('sha') not in known_parents:
                break
            time.sleep(delay)
            ref = self._request(stage, 'GET', path)
        return ref

    def _read(self, *, reuse_verified=False):
        repo = self._request('read_repository', 'GET', self.prefix)
        if not repo or repo.get('private') is not True or repo.get('default_branch') == self.branch:
            raise Hold('private_state_branch_required')
        known_parents = self._pinned_state[3] if reuse_verified and self._pinned_state else ()
        ref = self._read_ref('read_ref', known_parents)
        if not ref:
            raise Hold('remote_state_branch_missing')
        head = ref['object']['sha']
        if head in known_parents:
            raise Hold('remote_state_publish_head_changed')
        if reuse_verified and self._pinned_state is not None and head == self._pinned_state[0]:
            return head, self._pinned_state[1], deepcopy(self._verified_document)
        commit = self._request('read_commit', 'GET', self.prefix + '/git/commits/' + head)
        tree_sha = commit['tree']['sha']
        tree = self._request('read_tree', 'GET', self.prefix + '/git/trees/' + tree_sha)
        if tree.get('truncated'):
            raise Hold('remote_state_tree_incomplete')
        entries = [x for x in tree['tree'] if x['path'] == STATE_FILE]
        parents = tuple(parent.get('sha') for parent in commit.get('parents', []))
        if not entries:
            doc = {'schema': SCHEMA, 'binding': self.binding, 'lease': None, 'checkpoint': None}
            self._pinned_state = (head, tree_sha, None, parents)
            self._verified_document = deepcopy(doc)
            return head, tree_sha, doc
        if len(entries) != 1 or entries[0].get('type') != 'blob' or entries[0].get('mode') != '100644':
            raise Hold('remote_state_file_invalid')
        entry = entries[0]
        blob = self._request('read_blob', 'GET', self.prefix + '/git/blobs/' + entry['sha'])
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
        self._pinned_state = (head, tree_sha, entry['sha'], parents)
        self._verified_document = deepcopy(doc)
        return head, tree_sha, doc

    def _publish(self, head, tree_sha, doc, *, recovery=None):
        message = 'Checkpoint private Get synchronization state'
        if recovery is not None:
            if (set(recovery) != {'previous_run_id', 'previous_attempt', 'previous_status', 'checkpoint_sha256'} or
                    type(recovery['previous_run_id']) is not int or recovery['previous_run_id'] <= 0 or
                    type(recovery['previous_attempt']) is not int or recovery['previous_attempt'] <= 0 or
                    recovery['previous_status'] != 'completed' or
                    recovery['checkpoint_sha256'] is not None and
                    not re.fullmatch('[0-9a-f]{64}', recovery['checkpoint_sha256'])):
                raise Hold('remote_state_recovery_evidence_invalid')
            message = ('Recover completed Get sync run ' + str(recovery['previous_run_id']) +
                       ' attempt ' + str(recovery['previous_attempt']) + '; status=completed; checkpoint=' +
                       (recovery['checkpoint_sha256'] or 'none'))
        body = canonical(doc).encode()
        expected_blob = git_blob_sha(body)
        if self._pinned_state is None or self._pinned_state[:2] != (head, tree_sha):
            raise Hold('remote_state_publish_head_changed')
        previous_blob = self._pinned_state[2]
        before = self._read_ref('check_publish_ref', self._pinned_state[3])
        if not before or before.get('object', {}).get('sha') != head:
            raise Hold('remote_state_publish_head_changed')
        payload = {'message': message, 'branch': self.branch,
                   'content': base64.b64encode(body).decode()}
        if previous_blob is not None:
            payload['sha'] = previous_blob
        update_error, response = None, None
        try:
            response = self._request('write_contents', 'PUT', self.prefix + '/contents/' + STATE_FILE, payload)
        except Exception as error:
            update_error = error
        # A one-shot write is never retried, including when its response is lost.
        current, current_tree, readback = self._read()
        # GitHub may briefly serve the pre-write ref; only repeat reads of that exact head.
        for delay in (1, 2, 4):
            if current != head:
                break
            time.sleep(delay)
            current, current_tree, readback = self._read()
        self._stage = 'verify_readback'
        if update_error is not None:
            if current == head or readback != doc:
                self._stage = 'write_contents'
                raise _UnconfirmedContents(update_error) from None
            desired = current
        else:
            desired = response.get('commit', {}).get('sha') if isinstance(response, dict) else None
            if (not isinstance(desired, str) or not re.fullmatch('[0-9a-f]{40}', desired)
                    or response.get('content', {}).get('sha') != expected_blob):
                raise Hold('remote_state_blob_write_mismatch')
            if current != desired:
                raise Hold('remote_state_readback_old_head' if current == head else 'remote_state_readback_other_head')
        if readback != doc:
            raise Hold('remote_state_readback_content_mismatch')
        # The independent read-back already verified the pinned tree and blob.
        self._stage = 'verify_contents_blob'
        if self._pinned_state[:3] != (current, current_tree, expected_blob):
            raise Hold('remote_state_blob_write_mismatch')
        self._stage = 'verify_contents_parent'
        if self._pinned_state[3] != (head,):
            # Contents CAS protects the file; a separate branch-head race still freezes us.
            raise Hold('remote_state_publish_parent_changed')
        return desired



    @staticmethod
    def _owner_attempt(owner):
        if not isinstance(owner, str):
            return None
        match = re.fullmatch(r'(?:get-direct|github|get-run-get-direct)-([1-9][0-9]{0,30})-([1-9][0-9]{0,5})', owner)
        return (int(match[1]), int(match[2])) if match else None

    def _github_attempt(self, runtime_repo, run_id, attempt):
        path = '/repos/' + runtime_repo + '/actions/runs/' + str(run_id) + '/attempts/' + str(attempt)
        try:
            row = self.http.request('GET', path)
        except Exception:
            raise Hold('remote_state_control_plane_unavailable') from None
        if not isinstance(row, dict):
            raise Hold('remote_state_control_plane_unverified')
        repository, head = row.get('repository'), row.get('head_repository')
        wire_path = row.get('path')
        workflow, separator, ref = wire_path.partition('@') if isinstance(wire_path, str) else ('', '', '')
        if (type(row.get('id')) is not int or row['id'] != run_id or
                type(row.get('run_attempt')) is not int or row['run_attempt'] != attempt or
                workflow != WORKFLOW_PATH or separator and
                (not ref or not re.fullmatch(r'[A-Za-z0-9_./-]+', ref) or '..' in ref or '//' in ref) or
                type(row.get('workflow_id')) is not int or row['workflow_id'] <= 0 or
                not isinstance(repository, dict) or not isinstance(head, dict) or
                repository.get('full_name') != runtime_repo or head.get('full_name') != runtime_repo or
                type(repository.get('id')) is not int or repository['id'] <= 0 or
                type(head.get('id')) is not int or repository['id'] != head['id'] or
                repository.get('fork') is not False or head.get('fork') is not False or
                not isinstance(row.get('head_sha'), str) or not re.fullmatch('[0-9a-f]{40}', row['head_sha'])):
            raise Hold('remote_state_control_plane_unverified')
        return row

    def recover_completed_lease(self, *, environ=None):
        """Recover only a completed same-program attempt proven by GitHub's API."""
        if self.lease is not None:
            raise Hold('remote_state_already_acquired')
        env = os.environ if environ is None else environ
        current = self._owner_attempt(self.owner)
        runtime_repo, workflow_ref = env.get('GITHUB_REPOSITORY'), env.get('GITHUB_WORKFLOW_REF')
        run, attempt = env.get('GITHUB_RUN_ID'), env.get('GITHUB_RUN_ATTEMPT')
        if (current is None or not isinstance(runtime_repo, str) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', runtime_repo) or
                runtime_repo.split('/')[1] in ('.', '..') or not isinstance(workflow_ref, str) or
                not workflow_ref.startswith(runtime_repo + '/' + WORKFLOW_PATH + '@') or
                not isinstance(run, str) or not re.fullmatch('[1-9][0-9]{0,30}', run) or
                not isinstance(attempt, str) or not re.fullmatch('[1-9][0-9]{0,5}', attempt) or
                current != (int(run), int(attempt))):
            raise Hold('remote_state_owned_no_takeover')
        ref = workflow_ref[len(runtime_repo + '/' + WORKFLOW_PATH + '@'):]
        if not re.fullmatch(r'refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9_./-]{0,200}', ref) or '..' in ref or '//' in ref:
            raise Hold('remote_state_owned_no_takeover')
        head, tree, doc = self._read()
        previous_lease = doc['lease']
        previous = self._owner_attempt(previous_lease.get('owner')) if previous_lease else None
        if previous is None or previous == current:
            raise Hold('remote_state_owned_no_takeover')
        active = self._github_attempt(runtime_repo, *current)
        completed = self._github_attempt(runtime_repo, *previous)
        if (active.get('status') != 'in_progress' or completed.get('status') != 'completed' or
                active['workflow_id'] != completed['workflow_id'] or
                active['repository']['id'] != completed['repository']['id']):
            raise Hold('remote_state_owner_not_verified_completed')
        fresh_head, fresh_tree, fresh = self._read()
        if fresh_head != head or fresh != doc:
            raise Hold('remote_state_recovery_raced')
        checkpoint = doc['checkpoint']['sha256'] if doc['checkpoint'] else None
        proof = {'previous_run_id': previous[0], 'previous_attempt': previous[1],
                 'previous_status': 'completed', 'checkpoint_sha256': checkpoint}
        fresh['lease'] = None
        recovery_commit = self._publish(fresh_head, fresh_tree, fresh, recovery=proof)
        self.recovery_evidence = {**proof, 'reason': 'verified_completed_same_program_attempt',
            'runtime_repository': runtime_repo, 'workflow_path': WORKFLOW_PATH,
            'previous_owner': previous_lease['owner'], 'previous_head_sha': completed['head_sha'],
            'current_run_id': current[0], 'current_attempt': current[1],
            'current_head_sha': active['head_sha'], 'state_before_head': head,
            'recovery_commit': recovery_commit}
        return dict(self.recovery_evidence)

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
            self.recover_completed_lease()
            head, tree, doc = self._read()
            if doc['lease'] is not None:
                raise Hold('remote_state_recovery_raced')
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
        self._stage = 'context'
        try:
            if self.lease is None or self.path is None or connection.in_transaction:
                raise Hold('remote_checkpoint_context')
            # A fresh ref to the same immutable commit also identifies its verified contents.
            head, tree, doc = self._read(reuse_verified=True)
            if doc['lease'] != self.lease:
                raise Hold('remote_state_lease_lost')
            self._stage = 'sqlite_backup'
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
            self._stage = 'compress_checkpoint'
            if len(packed) > MAX_COMPRESSED:
                raise Hold('remote_checkpoint_compressed_size')
            checkpoint = {'encoding': 'zlib+base64', 'bytes': len(raw),
                          'sha256': hashlib.sha256(raw).hexdigest(),
                          'compressed_sha256': hashlib.sha256(packed).hexdigest(),
                          'data': base64.b64encode(packed).decode()}
            if checkpoint != doc['checkpoint']:
                doc['checkpoint'] = checkpoint
                self._publish(head, tree, doc)
        except Exception as error:
            failure = CheckpointFailure('remote_checkpoint_failed')
            failure.reason = _checkpoint_reason(error)
            failure.stage = self._stage
            raise failure from None

    def release(self):
        if self.lease is None:
            raise Hold('remote_state_not_owned')
        head, tree, doc = self._read()
        if doc['lease'] != self.lease:
            raise Hold('remote_state_release_not_owner')
        doc['lease'] = None
        self._publish(head, tree, doc)
        self.lease = None
