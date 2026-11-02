#!/usr/bin/env python3
"""Retain an explicitly authorized Get preview on two fixed private refs only.

This is content validation, not user authentication or production approval.
The trusted caller owns approval of this repository, these refs and this data.
No main update, merge, schedule, deletion or uncertain-write retry is supported.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import tempfile
import math
import signal
import threading
import time
import urllib.error
import urllib.request
import ssl
import http.client

import source_sync_pack_seed as pack_seed

REPOSITORY = 'Eddiehhhhh/eddie-llm-wiki'
REMOTE_URL = 'https://github.com/' + REPOSITORY + '.git'
GET = 'Raw/03 Get'
BACKUP_REF = 'refs/heads/backup/get-cloud-cutover-20261015'
PREVIEW_REF = 'refs/heads/preview/get-cloud-cutover-20261015'
FIDELITY_PREVIEW_REF = 'refs/heads/preview/get-fidelity-20261016'
MAIN_REF = 'refs/heads/main'
PLAN_PATH = GET + '/03 State/cloud-preview-plan.json'
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_FILES = 6000
MAX_BLOB_BYTES = 32 * 1024 * 1024
MAX_GET_BYTES = 256 * 1024 * 1024
ERROR_CODES = frozenset(('invalid_commit','invalid_json','outside_get_scope','unsafe_path','symlink_or_alias',
 'missing_directory','nonregular_file','file_budget','missing_token','invalid_checkout','unsafe_git_config',
 'git_operation_failed','unsupported_object_format','source_head_changed','invalid_source_commit',
 'duplicate_tree_path','remote_redirect_blocked','remote_read_failed','remote_response_budget',
 'remote_schema_invalid','ref_not_allowed','remote_ref_invalid','remote_commit_invalid','remote_blob_invalid',
 'repository_not_private_target','remote_main_changed','ref_creation_unconfirmed','ref_readback_mismatch',
 'backup_ref_conflict','backup_ref_readback_failed','backup_failed','nonregular_ignore_file',
 'ignored_preview_path','ignore_check_failed','preview_not_isolated','unplanned_output_file','invalid_plan',
 'nonregular_source','reserved_plan_path_exists','source_worktree_changed','preview_deletion_forbidden',
 'invalid_plan_row','invalid_plan_path','plan_old_bytes_mismatch','plan_new_bytes_mismatch',
 'unplanned_candidate_change','candidate_changed','local_blob_readback_failed','candidate_tree_mismatch',
 'existing_preview_conflict','remote_blob_readback_failed','preview_ref_changed','backup_not_verified',
 'preview_retention_failed','conflicting_modes','missing_preview_output','retention_failed',
 'checkout_failed','checkout_destination_exists','invalid_report','source_tree_budget','invalid_source_tree',
 'missing_preview_commit','retained_preview_missing','retained_plan_missing','retained_readback_failed','review_get_tree_changed',
 'retained_tree_mismatch','retained_payload_mismatch','retained_blob_missing'))
SHA = re.compile(r'[0-9a-f]{40}\Z')
ROW_KEYS = {'path', 'action', 'old_blob', 'new_blob', 'old_sha256', 'new_sha256', 'old_bytes', 'new_bytes'}
PLAN_KEYS = {'version', 'scope', 'source_commit', 'changes', 'pending', 'status', 'published', 'apply_ready', 'reason'}


class RetentionFailure(RuntimeError):
    """Only fixed codes may leave this module; never upstream errors or paths."""


def fail(code):
    raise RetentionFailure(code if code in ERROR_CODES else 'retention_failed')


def oid(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def checked_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        fail('invalid_commit')
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail('invalid_json')
            result[key] = value
        return result
    def constant(_):
        fail('invalid_json')
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        fail('invalid_json')


def safe_path(value):
    if not isinstance(value, str) or not value.startswith(GET + '/'):
        fail('outside_get_scope')
    if '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail('unsafe_path')
    if any(part in ('', '.', '..', '.git') for part in value.split('/')):
        fail('unsafe_path')
    return value


def regular_root(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        fail('symlink_or_alias')
    if not path.is_dir():
        fail('missing_directory')
    return path.resolve()


def file_bytes(path, limit=None):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        fail('nonregular_file')
    with path.open('rb') as stream:
        value = stream.read() if limit is None else stream.read(limit + 1)
    if limit is not None and len(value) > limit:
        fail('file_budget')
    return value


def safe_environment(token=None):
    # Do not inherit Git routing/tracing/config injection, proxies or auth helpers.
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(('GIT_', 'GCM_')) and k not in ('PRIVATE_VAULT_TOKEN', 'SSH_ASKPASS', 'GETNOTE_API_KEY', 'GETNOTE_CLIENT_ID', 'GET_API_KEY', 'GET_CLIENT_ID')
                   and not k.lower().endswith('_proxy') and k.lower() != 'all_proxy'}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false', GIT_NO_REPLACE_OBJECTS='1')
    settings = [('credential.helper', ''), ('core.hooksPath', os.devnull),
                ('http.followRedirects', 'false'), ('http.proxy', '')]
    if token is not None:
        if not isinstance(token, str) or not token or any(ord(c) <= 32 for c in token):
            fail('missing_token')
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        settings.append(('http.https://github.com/.extraheader', 'AUTHORIZATION: basic ' + auth))
    environment['GIT_CONFIG_COUNT'] = str(len(settings))
    for n, (key, value) in enumerate(settings):
        environment['GIT_CONFIG_KEY_' + str(n)] = key
        environment['GIT_CONFIG_VALUE_' + str(n)] = value
    return environment


class LocalGit:
    def __init__(self, vault, token=None):
        self.vault = regular_root(vault)
        self.env = safe_environment(token)
        # A normal checkout has a real .git directory. Do not follow external
        # worktree pointers, alternates or repository-local URL rewrites.
        if not (self.vault / '.git').is_dir() or (self.vault / '.git').is_symlink():
            fail('invalid_checkout')
        if (self.vault / '.git/objects/info/alternates').exists() or (self.vault / '.git/info/grafts').exists():
            fail('unsafe_git_config')
        raw = self.run('config', '--local', '--no-includes', '--name-only', '--list').decode().splitlines()
        if any(key.lower().startswith(('url.', 'include.', 'includeif.', 'http.', 'credential.', 'filter.'))
               or key.lower() in ('core.sshcommand', 'core.gitproxy', 'core.worktree', 'core.fsmonitor') for key in raw):
            fail('unsafe_git_config')
        allowed_remote = {'remote.origin.url', 'remote.origin.fetch', 'remote.origin.promisor', 'remote.origin.partialclonefilter'}
        if any(key.lower().startswith('remote.') and key.lower() not in allowed_remote for key in raw):
            fail('unsafe_git_config')
        if self.run('for-each-ref', '--format=%(refname)', 'refs/replace').strip():
            fail('unsafe_git_config')

    def command(self, *args, data=None, env=None, cwd=None):
        try:
            return subprocess.run(['git', '-C', str(cwd or self.vault), *args], input=data,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env or self.env, timeout=getattr(self, 'timeout', 600), check=False,
                                  preexec_fn=getattr(self, 'child_limits', None))
        except Exception:
            fail('git_operation_failed')

    def run(self, *args, data=None, env=None, cwd=None):
        result = self.command(*args, data=data, env=env, cwd=cwd)
        if result.returncode:
            fail('git_operation_failed')
        return result.stdout

    def check_source(self, source):
        checked_sha(source)
        if self.run('remote').decode().splitlines() != ['origin'] or self.run('config', '--local', '--get-all', 'remote.origin.url').decode().splitlines() != [REMOTE_URL]:
            fail('unsafe_git_config')
        if self.run('rev-parse', '--show-object-format').strip() != b'sha1':
            fail('unsupported_object_format')
        if self.run('rev-parse', 'HEAD').decode().strip() != source:
            fail('source_head_changed')
        if self.run('cat-file', '-t', source).strip() != b'commit':
            fail('invalid_source_commit')

    def tree(self, revision):
        result = {}
        for record in self.run('ls-tree', '-rz', revision).split(b'\0'):
            if not record:
                continue
            metadata, name = record.split(b'\t', 1)
            mode, kind, blob = metadata.decode().split()
            path = name.decode('utf-8')
            if path in result:
                fail('duplicate_tree_path')
            result[path] = (mode, kind, checked_sha(blob))
        return result

    def blob(self, blob):
        return self.run('cat-file', 'blob', checked_sha(blob))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        fail('remote_redirect_blocked')


# Process-wide: rebuilding an adapter cannot reset retry allowance. Physical
# attempts and application-read bytes are separate from logical proof budgets.
_TREE_RETRY_LIMIT = 8
_TREE_RETRIES_USED = 0
_TREE_READ_METRICS = {'attempts': 0, 'extra_attempts': 0, 'read_bytes': 0, 'charged_bytes': 0, 'read_failures': 0}
_TREE_READ_LOCK = threading.Lock()


class _TreeReadDeadline:
    """One hard wall-clock window around BOTH attempts and complete read.

    Linux CI invokes the synchronous runtime on the main thread. No thread or
    nested alarm fallback is allowed. An existing alarm is never overwritten.
    """
    def __enter__(self):
        self.locked=False;self.installed=False;self.armed=False;self.deadline_expired=False
        if (threading.current_thread() is not threading.main_thread()
                or not all(hasattr(signal,k) for k in ('SIGALRM','ITIMER_REAL','getitimer','setitimer'))
                or not _TREE_READ_LOCK.acquire(blocking=False)):fail('remote_read_failed')
        self.locked=True
        try:
            if signal.getitimer(signal.ITIMER_REAL)!=(0.0,0.0):fail('remote_read_failed')
            self.old=signal.getsignal(signal.SIGALRM)
            self.start=time.monotonic();self.last=self.start
            if not math.isfinite(self.start):fail('remote_read_failed')
            self.deadline=self.start+60.0
            signal.signal(signal.SIGALRM,self.expired);self.installed=True
            signal.setitimer(signal.ITIMER_REAL,self.remaining());self.armed=True
            return self
        except Exception:
            self.__exit__(None,None,None)
            fail('remote_read_failed')

    def expired(self,signum,frame):
        self.deadline_expired=True
        fail('remote_read_failed')

    def remaining(self):
        now=time.monotonic()
        if not math.isfinite(now) or now<self.last or now>=self.deadline:
            self.deadline_expired=True
            fail('remote_read_failed')
        self.last=now
        return self.deadline-now

    def __exit__(self,*unused):
        try:
            if self.armed:signal.setitimer(signal.ITIMER_REAL,0)
        finally:
            try:
                if self.installed:signal.signal(signal.SIGALRM,self.old)
            finally:
                if self.locked:_TREE_READ_LOCK.release()


def _tree_open_transient(error):
    # No exception text, generic URLError, TLS, certificate, permission, redirect
    # or response/read failure grants retry authority.
    if type(error) is urllib.error.HTTPError:
        return error.code in (502,503,504) and (error.headers is None or error.headers.get('Retry-After') in (None,'0'))
    cause=error.reason if type(error) is urllib.error.URLError else error
    return type(cause) in (ConnectionResetError,ConnectionAbortedError,ConnectionRefusedError,TimeoutError)


def _read_error_class(error):
    """Fixed enums only. Never exception text, headers, URL or private payload."""
    if type(error) is urllib.error.HTTPError:return 'http'
    cause=error.reason if type(error) is urllib.error.URLError else error
    if isinstance(cause,ssl.SSLCertVerificationError):return 'certificate'
    if isinstance(cause,ssl.SSLError):return 'tls'
    if type(cause) is TimeoutError:return 'timeout'
    if type(cause) in (ConnectionResetError,ConnectionAbortedError,ConnectionRefusedError):return 'connection'
    if isinstance(cause,http.client.IncompleteRead):return 'incomplete_read'
    if type(error) is urllib.error.URLError:return 'url_error'
    if isinstance(error,RetentionFailure):return 'validation'
    return 'other'


class GitHubRemote:
    """Fixed repository, read-only REST verification and create-only Git refs."""
    def __init__(self, local, token):
        self.local = local
        self.token = token
        # Validate before constructing any credential-bearing request.
        safe_environment(token)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _begin_read(self):
        if not hasattr(self,'_read_metrics'):
            self._read_metrics={'logical_requests':0,'physical_attempts':0,'response_bytes':0,'failed_read_bound':0}
        self._read_metrics['logical_requests']+=1
        self._read_failure=None;self._read_error_id=None;self._read_phase='validation';self._read_status=None

    def _read_error(self,error,*,phase=None,deadline=False):
        status=error.code if type(error) is urllib.error.HTTPError else self._read_status
        self._read_failure={'phase':'deadline' if deadline else phase or self._read_phase,
            'error_class':'deadline' if deadline else _read_error_class(error),
            'http_status':status if type(status) is int and 100<=status<=599 else None}
        self._read_error_id=id(error)

    def read_diagnostic(self):
        if not isinstance(getattr(self,'_read_failure',None),dict):return None
        # Only bounded scalar counters and fixed classifications leave transport.
        value=dict(self._read_failure);value.update(self._read_metrics)
        value.update(tree_extra_used=_TREE_RETRIES_USED,tree_extra_limit=_TREE_RETRY_LIMIT,
                     tree_physical_attempts=_TREE_READ_METRICS['attempts'])
        return value

    def request(self, suffix, *, missing=False, maximum=1024 * 1024):
        if isinstance(suffix,str) and re.fullmatch(r'/git/trees/[0-9a-f]{40}',suffix) and not missing:
            return self._immutable_tree_request(suffix,maximum)
        self._begin_read()
        request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + suffix,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'get-private-preview-retention'})
        try:
            self._read_phase='open';self._read_metrics['physical_attempts']+=1
            with self.opener.open(request, timeout=60) as response:
                self._read_status=response.status
                if response.status != 200:fail('remote_read_failed')
                self._read_phase='read'
                try:data = response.read(maximum + 1)
                except Exception as error:
                    self._read_metrics['failed_read_bound']+=maximum+1;self._read_error(error);raise
                finally:self._read_phase='close'
                self._read_metrics['response_bytes']+=len(data)
            self._read_phase='validation'
            if len(data) > maximum:fail('remote_response_budget')
            value = strict_json(data)
            if not isinstance(value, dict):fail('remote_schema_invalid')
            self._read_failure=None
            return value
        except urllib.error.HTTPError as error:
            status = error.code;self._read_error(error)
            try:error.close()
            except Exception as close_error:self._read_error(close_error,phase='close');raise
            if missing and status == 404:
                self._read_failure=None
                return None
            fail('remote_read_failed')
        except RetentionFailure as error:
            if self._read_error_id!=id(error):self._read_error(error)
            raise
        except Exception as error:
            if self._read_error_id!=id(error):self._read_error(error)
            fail('remote_read_failed')

    def _immutable_tree_request(self,suffix,maximum):
        global _TREE_RETRIES_USED
        self._begin_read()
        if (not re.fullmatch(r'/git/trees/[0-9a-f]{40}',suffix)
                or type(maximum) is not int or not 0<maximum<=16*1024*1024):fail('remote_read_failed')
        request=urllib.request.Request('https://api.github.com/repos/'+REPOSITORY+suffix,
            headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json',
                'X-GitHub-Api-Version':'2022-11-28','User-Agent':'get-private-preview-retention'})
        deadline=None
        try:
            self._read_phase='deadline'
            with _TreeReadDeadline() as deadline:
                for attempt in range(2):
                    timeout=deadline.remaining()
                    _TREE_READ_METRICS['attempts']+=1
                    self._read_phase='open';self._read_metrics['physical_attempts']+=1
                    try:
                        response=self.opener.open(request,timeout=timeout)
                    except Exception as error:
                        self._read_error(error,deadline=getattr(deadline,'deadline_expired',False))
                        retry=_tree_open_transient(error)
                        if isinstance(error,urllib.error.HTTPError):
                            try:error.close()
                            except Exception as close_error:self._read_error(close_error,phase='close');raise
                        deadline.remaining()
                        if not retry or attempt or _TREE_RETRIES_USED>=_TREE_RETRY_LIMIT:fail('remote_read_failed')
                        _TREE_RETRIES_USED+=1;_TREE_READ_METRICS['extra_attempts']+=1
                        self._read_failure=None;self._read_error_id=None
                        continue
                    # Original tree-only open-stage retry policy is unchanged.
                    # No read/close/JSON/schema failure ever retries.
                    with response:
                        self._read_status=response.status
                        if response.status!=200:fail('remote_read_failed')
                        _TREE_READ_METRICS['charged_bytes']+=maximum+1
                        self._read_phase='read'
                        try:data=response.read(maximum+1)
                        except Exception as error:
                            _TREE_READ_METRICS['read_failures']+=1
                            self._read_metrics['failed_read_bound']+=maximum+1
                            self._read_error(error,deadline=getattr(deadline,'deadline_expired',False));raise
                        finally:self._read_phase='close'
                        _TREE_READ_METRICS['read_bytes']+=len(data)
                        _TREE_READ_METRICS['charged_bytes']-=maximum+1-len(data)
                        self._read_metrics['response_bytes']+=len(data)
                    self._read_phase='deadline';deadline.remaining()
                    self._read_phase='validation'
                    if len(data)>maximum:fail('remote_response_budget')
                    value=strict_json(data)
                    if not isinstance(value,dict):fail('remote_schema_invalid')
                    self._read_failure=None
                    return value
        except RetentionFailure as error:
            if getattr(deadline,'deadline_expired',False):self._read_error(error,deadline=True)
            elif self._read_failure is None or (self._read_phase=='close' and self._read_error_id!=id(error)):self._read_error(error)
            raise
        except Exception as error:
            if self._read_error_id!=id(error):self._read_error(error,deadline=getattr(deadline,'deadline_expired',False))
            fail('remote_read_failed')

    def metadata(self):
        return self.request('')

    def ref(self, name):
        if name not in (MAIN_REF, BACKUP_REF, PREVIEW_REF, FIDELITY_PREVIEW_REF):
            fail('ref_not_allowed')
        value = self.request('/git/ref/' + name.removeprefix('refs/'), missing=True)
        if value is None:
            return None
        if value.get('ref') != name or value.get('object', {}).get('type') != 'commit':
            fail('remote_ref_invalid')
        return checked_sha(value['object'].get('sha'))

    def commit(self, commit):
        return self.commit_with_locator(commit)[0]

    def commit_with_locator(self, commit):
        # One physical immutable response, returned together; no mutable
        # last-observation slot, extra HTTP call, or change to commit() shape.
        value = self.request('/git/commits/' + checked_sha(commit))
        if value.get('sha') != commit:
            fail('remote_commit_invalid')
        result = {'tree': checked_sha(value.get('tree', {}).get('sha')),
                  'parents': [checked_sha(p.get('sha')) for p in value.get('parents', [])]}
        import get_notion_idle_rotation as rotation
        locator = rotation.commit_envelope(value, commit)
        return result, locator

    def blob(self, blob, expected_size):
        value = self.request('/git/blobs/' + checked_sha(blob), maximum=2 * expected_size + 65536)
        if value.get('sha') != blob or value.get('encoding') != 'base64' or value.get('size') != expected_size:
            fail('remote_blob_invalid')
        try:
            content = base64.b64decode(value['content'].replace('\n', ''), validate=True)
        except (KeyError, TypeError, ValueError):
            fail('remote_blob_invalid')
        if len(content) != expected_size or oid(content) != blob:
            fail('remote_blob_invalid')
        return content

    def tree_entries(self, revision, recursive=False):
        value = self.request('/git/trees/' + checked_sha(revision) + ('?recursive=1' if recursive else ''), maximum=16 * 1024 * 1024)
        if value.get('truncated') is not False or not isinstance(value.get('tree'), list):
            fail('invalid_source_tree')
        return value['tree']

    def source_get_tree(self, source):
        # Exact tree identity covers all paths, modes and bytes, without reading
        # any non-Get body or interpreting timestamps as source equivalence.
        revision = self.commit(source)['tree']
        for wanted in ('Raw', '03 Get'):
            selected = [entry for entry in self.tree_entries(revision) if entry.get('path') == wanted]
            if len(selected) != 1 or selected[0].get('type') != 'tree' or selected[0].get('mode') != '040000':
                fail('invalid_source_tree')
            revision = checked_sha(selected[0].get('sha'))
        return revision

    def check_source_budget(self, source):
        # Walk only root/Raw metadata and Get. Truncated/oversized reads stop.
        revision = self.source_get_tree(source)
        count = total = 0
        seen = set()
        blobs = {}
        for entry in self.tree_entries(revision, True):
            path = safe_path(GET + '/' + entry.get('path', ''))
            if path in seen:
                fail('invalid_source_tree')
            seen.add(path)
            if entry.get('type') == 'tree' and entry.get('mode') == '040000':
                continue
            size = entry.get('size')
            if entry.get('type') != 'blob' or entry.get('mode') not in ('100644', '100755') or type(size) is not int or size < 0:
                fail('invalid_source_tree')
            checked_sha(entry.get('sha'))
            blobs[path] = (entry['mode'], 'blob', entry['sha'], size)
            count += 1
            total += size
            if count > MAX_FILES or size > MAX_BLOB_BYTES or total > MAX_GET_BYTES:
                fail('source_tree_budget')
        if count == 0:
            fail('invalid_source_tree')
        return blobs

    def create_only(self, name, commit):
        if name not in (BACKUP_REF, PREVIEW_REF, FIDELITY_PREVIEW_REF):
            fail('ref_not_allowed')
        checked_sha(commit)
        # An empty expected value means the branch must not exist at the server.
        # No existing ref can be replaced, even on a race or branch rewind.
        self.local.run('push', '--porcelain', '--no-verify',
                       '--force-with-lease=' + name + ':', REMOTE_URL, commit + ':' + name)


def remote_context(vault, token, remote):
    token = token if token is not None else (None if remote is not None else os.environ.get('PRIVATE_VAULT_TOKEN'))
    local = LocalGit(vault, token)
    return local, remote if remote is not None else GitHubRemote(local, token)


def preflight(local, remote, source, *, allow_main_drift=False):
    local.check_source(source)
    metadata = remote.metadata()
    if metadata.get('full_name') != REPOSITORY or metadata.get('private') is not True:
        fail('repository_not_private_target')
    main = remote.ref(MAIN_REF)
    if main is None or (not allow_main_drift and main != source):
        fail('remote_main_changed')
    checked_sha(main)
    if remote.commit(source).get('tree') != local.run('rev-parse', source + '^{tree}').decode().strip():
        fail('invalid_source_commit')
    return main


def create_once(remote, ref, commit):
    # Never retry a possibly successful mutation. Both success and uncertainty
    # require the same bounded readback. The caller validates the resulting tree.
    try:
        remote.create_only(ref, commit)
    except Exception:
        observed = remote.ref(ref)
        if observed != commit:
            fail('ref_creation_unconfirmed')
        return 'created_verified_after_uncertain_response'
    if remote.ref(ref) != commit:
        fail('ref_readback_mismatch')
    return 'created_verified'


def review_source(remote, source):
    """Allow a pinned historical review only with its existing exact backup.

    A newer main may differ outside Get. This is never an apply/rebase gate:
    the preview keeps source as its parent and cannot write main or old refs.
    """
    checked_sha(source)
    main = remote.ref(MAIN_REF)
    if main is None:
        fail('remote_main_changed')
    checked_sha(main)
    if main != source:
        if remote.ref(BACKUP_REF) != source:
            fail('remote_main_changed')
        if remote.source_get_tree(main) != remote.source_get_tree(source):
            fail('review_get_tree_changed')
        if remote.ref(BACKUP_REF) != source:
            fail('backup_ref_readback_failed')
    if remote.ref(MAIN_REF) != main:
        fail('remote_main_changed')
    return main


def prepare_backup(vault, source_commit, *, token=None, remote=None):
    try:
        local, remote = remote_context(vault, token, remote)
        preflight(local, remote, source_commit, allow_main_drift=True)
        initial_main = review_source(remote, source_commit)
        current = remote.ref(BACKUP_REF)
        if current is None and (initial_main != source_commit or remote.ref(MAIN_REF) != source_commit):
            fail('backup_not_verified')
        if current is not None and current != source_commit:
            fail('backup_ref_conflict')
        status = 'existing_verified' if current == source_commit else create_once(remote, BACKUP_REF, source_commit)
        if remote.ref(BACKUP_REF) != source_commit:
            fail('backup_ref_readback_failed')
        main = review_source(remote, source_commit)
        return {'status': 'backup_' + status, 'backup_verified': True, 'main_updated': False,
                'source_unchanged': True, 'main_advanced': main != source_commit}
    except RetentionFailure:
        raise
    except Exception:
        fail('backup_failed')


def files(root):
    root = regular_root(root)
    result = {}
    total = 0
    for directory, directories, filenames in os.walk(root, followlinks=False):
        for name in directories + filenames:
            path = Path(directory) / name
            if path.is_symlink():
                fail('symlink_or_alias')
        for name in filenames:
            path = Path(directory) / name
            relative = safe_path(GET + '/' + path.relative_to(root).as_posix())
            size = path.stat().st_size
            total += size
            if len(result) >= MAX_FILES or size > MAX_BLOB_BYTES or total > MAX_GET_BYTES:
                fail('source_tree_budget')
            data = file_bytes(path, MAX_BLOB_BYTES)
            result[relative] = (oid(data), sha256(data), len(data))
    return result


def check_ignored(local, source_tree, candidate, paths):
    relevant = {str(Path(path).parent) for path in paths}
    def needed(path):
        if Path(path).name != '.gitignore':
            return False
        parent = str(Path(path).parent)
        return parent == '.' or any(p == parent or p.startswith(parent + '/') for p in relevant)
    ignores = {}
    for path, (mode, kind, blob) in source_tree.items():
        if not needed(path):
            continue
        if mode not in ('100644', '100755') or kind != 'blob':
            fail('nonregular_ignore_file')
        ignores[path] = local.blob(blob)
    with tempfile.TemporaryDirectory(prefix='get-preview-ignore-') as temporary:
        root = Path(temporary)
        local.run('init', '-q', cwd=root, env=safe_environment())
        for candidate_rules in (False, True):
            rules = dict(ignores)
            if candidate_rules:
                for path in candidate.rglob('.gitignore'):
                    relative = GET + '/' + path.relative_to(candidate).as_posix()
                    if needed(relative):
                        rules[relative] = file_bytes(path)
            for path, data in rules.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            result = local.command('-c', 'core.excludesFile=' + os.devnull, 'check-ignore', '--no-index', '-z', '--stdin',
                                   data=('\0'.join(paths) + '\0').encode(), cwd=root, env=safe_environment())
            if result.returncode == 0:
                fail('ignored_preview_path')
            if result.returncode != 1:
                fail('ignore_check_failed')


def build_preview(local, output, source):
    local.check_source(source)
    output = regular_root(output)
    if output.is_relative_to(local.vault) or local.vault.is_relative_to(output):
        fail('preview_not_isolated')
    if {p.name for p in output.iterdir()} != {'candidate', 'plan.json'}:
        fail('unplanned_output_file')
    candidate = regular_root(output / 'candidate')
    plan_bytes = file_bytes(output / 'plan.json', MAX_PLAN_BYTES)
    plan = strict_json(plan_bytes)
    if not isinstance(plan, dict) or not valid_plan_keys(plan) or (type(plan.get('version')) is not int or plan['version'] != 1) or plan.get('scope') != 'get-private-preview':
        fail('invalid_plan')
    if plan.get('source_commit') != source or plan.get('published') is not False or plan.get('apply_ready') is not False:
        fail('invalid_plan')
    if type(plan.get('pending')) is not int or plan['pending'] < 0 or plan.get('status') != ('partial' if plan['pending'] else 'complete'):
        fail('invalid_plan')
    rows = plan.get('changes')
    if not isinstance(rows, list):
        fail('invalid_plan')
    source_tree = local.tree(source)
    source_get = {p: entry for p, entry in source_tree.items() if p.startswith(GET + '/')}
    for path, (mode, kind, _) in source_get.items():
        safe_path(path)
        if mode not in ('100644', '100755') or kind != 'blob':
            fail('nonregular_source')
    if PLAN_PATH in source_tree:
        fail('reserved_plan_path_exists')
    source_files = files(local.vault / GET)
    if set(source_files) != set(source_get) or any(source_files[p][0] != source_get[p][2] for p in source_get):
        fail('source_worktree_changed')
    after = files(candidate)
    if PLAN_PATH in after:
        fail('reserved_plan_path_exists')
    if set(source_get) - set(after):
        fail('preview_deletion_forbidden')
    expected = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            fail('invalid_plan_row')
        if any(type(row.get(k)) is not int or row[k] < 0 for k in ('old_bytes', 'new_bytes')):
            fail('invalid_plan_row')
        path = safe_path(row.get('path'))
        if path in expected or path == PLAN_PATH or path not in after:
            fail('invalid_plan_path')
        old = source_files.get(path)
        new = after[path]
        if row['action'] != ('modify' if old else 'add'):
            fail('preview_deletion_forbidden')
        if (row['old_blob'], row['old_sha256'], row['old_bytes']) != (old if old else (None, None, 0)):
            fail('plan_old_bytes_mismatch')
        if (row['new_blob'], row['new_sha256'], row['new_bytes']) != new:
            fail('plan_new_bytes_mismatch')
        expected[path] = row
    actual = {p for p in after if p not in source_get or after[p][0] != source_get[p][2]}
    if actual != set(expected):
        fail('unplanned_candidate_change')
    payload = {p: file_bytes(candidate / Path(p).relative_to(GET)) for p in expected}
    # Keep the actual private review plan, including partial state. It never
    # becomes an authorization document or a production completion receipt.
    payload[PLAN_PATH] = canonical(plan)
    if len(after) + 1 > MAX_FILES or sum(item[2] for item in after.values()) + len(payload[PLAN_PATH]) > MAX_GET_BYTES:
        fail('source_tree_budget')
    check_ignored(local, source_tree, candidate, list(payload))
    for path, data in payload.items():
        if path != PLAN_PATH and (oid(data), sha256(data), len(data)) != after[path]:
            fail('candidate_changed')
    with tempfile.TemporaryDirectory(prefix='get-preview-index-') as temporary:
        environment = dict(local.env, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
        local.run('read-tree', source, env=environment)
        for path, data in payload.items():
            blob = local.run('hash-object', '-w', '--stdin', data=data, env=environment).decode().strip()
            if blob != oid(data):
                fail('local_blob_readback_failed')
            mode = source_tree[path][0] if path in source_tree else '100644'
            local.run('update-index', '--add', '--cacheinfo', mode + ',' + blob + ',' + path, env=environment)
        tree = checked_sha(local.run('write-tree', env=environment).decode().strip())
        candidate_tree = local.tree(tree)
        wanted = dict(source_tree)
        for path, data in payload.items():
            wanted[path] = (source_tree[path][0] if path in source_tree else '100644', 'blob', oid(data))
        if candidate_tree != wanted:
            fail('candidate_tree_mismatch')
        for path, data in payload.items():
            if local.blob(candidate_tree[path][2]) != data:
                fail('local_blob_readback_failed')
        timestamp = local.run('show', '-s', '--format=%ct', source).decode().strip()
        if not timestamp.isdigit():
            fail('invalid_source_commit')
        environment.update(GIT_AUTHOR_NAME='Get preview retention', GIT_AUTHOR_EMAIL='preview@example.invalid',
                           GIT_COMMITTER_NAME='Get preview retention', GIT_COMMITTER_EMAIL='preview@example.invalid',
                           GIT_AUTHOR_DATE='@' + timestamp + ' +0000', GIT_COMMITTER_DATE='@' + timestamp + ' +0000')
        commit = checked_sha(local.run('commit-tree', tree, '-p', source, '-m', 'Retain private Get cloud migration preview', env=environment).decode().strip())
    local.check_source(source)
    if files(candidate) != after or file_bytes(output / 'plan.json', MAX_PLAN_BYTES) != plan_bytes:
        fail('candidate_changed')
    return {'commit': commit, 'tree': tree, 'payload': payload, 'pending': plan['pending'], 'changed_paths': len(rows), 'preview_ref': preview_ref_for_plan(plan)}


def verify_preview(remote, commit, source, prepared):
    value = remote.commit(commit)
    if value != {'tree': prepared['tree'], 'parents': [source]}:
        fail('existing_preview_conflict')
    for data in prepared['payload'].values():
        if remote.blob(oid(data), len(data)) != data:
            fail('remote_blob_readback_failed')
    if remote.ref(prepared['preview_ref']) != commit:
        fail('preview_ref_changed')


def retain_private_preview(vault, output, source_commit, *, token=None, remote=None):
    try:
        local, remote = remote_context(vault, token, remote)
        preflight(local, remote, source_commit, allow_main_drift=True)
        if remote.ref(BACKUP_REF) != source_commit:
            fail('backup_not_verified')
        prepared = build_preview(local, output, source_commit)
        # No rebase or ref refresh can replace the caller's pinned baseline.
        preflight(local, remote, source_commit, allow_main_drift=True)
        preview_ref = prepared['preview_ref']
        if preview_ref == FIDELITY_PREVIEW_REF:
            review_source(remote, source_commit)
        current = remote.ref(preview_ref)
        if current is None:
            status = create_once(remote, preview_ref, prepared['commit'])
            current = prepared['commit']
        else:
            status = 'existing_verified'
        verify_preview(remote, current, source_commit, prepared)
        if remote.ref(BACKUP_REF) != source_commit:
            fail('backup_ref_readback_failed')
        final_main = remote.ref(MAIN_REF)
        if final_main is None:
            fail('remote_main_changed')
        checked_sha(final_main)
        if preview_ref == FIDELITY_PREVIEW_REF:
            final_main = review_source(remote, source_commit)
        return {'status': 'preview_' + status, 'preview_retained': True, 'preview_only': True, 'backup_verified': True,
                'main_advanced': final_main != source_commit,
                'main_updated': False, 'published': False, 'apply_ready': False, 'source_unchanged': True,
                'pending': prepared['pending'], 'source_status': 'partial' if prepared['pending'] else 'complete',
                'changed_paths': prepared['changed_paths']}
    except RetentionFailure:
        raise
    except Exception:
        fail('preview_retention_failed')


def checkout_private(vault, source_commit, *, token=None):
    """Fresh sparse checkout only. Token is never saved in argv or repository config."""
    try:
        source_commit = checked_sha(source_commit)
        destination = Path(vault).absolute()
        if '..' in destination.parts or any(p.is_symlink() for p in (destination, *destination.parents)):
            fail('symlink_or_alias')
        if destination.exists():
            fail('checkout_destination_exists')
        token = token if token is not None else os.environ.get('PRIVATE_VAULT_TOKEN')
        remote = GitHubRemote(None, token)
        metadata = remote.metadata()
        if metadata.get('full_name') != REPOSITORY or metadata.get('private') is not True:
            fail('repository_not_private_target')
        review_source(remote, source_commit)
        remote.check_source_budget(source_commit)
        destination.mkdir(mode=0o700, parents=True)
        environment = safe_environment(token)
        result = subprocess.run(['git', '-C', str(destination), 'init', '-q'], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        if result.returncode:
            fail('checkout_failed')
        local = LocalGit(destination, token)
        local.run('remote', 'add', 'origin', REMOTE_URL)
        local.run('config', 'remote.origin.promisor', 'true')
        local.run('config', 'remote.origin.partialclonefilter', 'blob:none')
        local.run('config', 'core.sparseCheckout', 'true')
        local.run('config', 'core.sparseCheckoutCone', 'false')
        (destination / '.git/info/sparse-checkout').write_text('/.gitignore\n/Raw/.gitignore\n/Raw/03 Get/\n')
        local.run('fetch', '--depth=1', '--filter=blob:none', '--no-tags', 'origin', source_commit)
        local.run('checkout', '--detach', source_commit)
        local.check_source(source_commit)
        main = review_source(remote, source_commit)
        return {'status': 'private_checkout_verified', 'checkout_verified': True,
                'source_unchanged': True, 'main_updated': False, 'main_advanced': main != source_commit}
    except RetentionFailure:
        raise
    except Exception:
        fail('checkout_failed')



def readback_checkout(destination, preview_commit, token):
    """Fresh object-only partial checkout. No worktree, filters or non-Get blobs.

    Fetching two commit generations supplies the candidate and source tree
    metadata. Payload bodies are fetched explicitly by a bounded batch later.
    This function only changes a new local directory, never a remote ref.
    """
    destination = Path(destination).absolute()
    if '..' in destination.parts or any(p.is_symlink() for p in (destination, *destination.parents)):
        fail('symlink_or_alias')
    if destination.exists():
        fail('checkout_destination_exists')
    destination.mkdir(mode=0o700, parents=True)
    environment = safe_environment(token)
    result = subprocess.run(['git', '-C', str(destination), 'init', '-q'], env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode:
        fail('checkout_failed')
    local = LocalGit(destination, token)
    local.run('remote', 'add', 'origin', REMOTE_URL)
    local.run('config', 'remote.origin.promisor', 'true')
    local.run('config', 'remote.origin.partialclonefilter', 'blob:none')
    local.run('config', 'core.sparseCheckout', 'true')
    local.run('config', 'core.sparseCheckoutCone', 'false')
    (destination / '.git/info/sparse-checkout').write_text('/Raw/03 Get/\n')
    # Disable implicit per-object fetching: every payload fetch below is an
    # explicit batch of validated Get blob OIDs, never a full clone fallback.
    local.env['GIT_NO_LAZY_FETCH'] = '1'
    local.timeout = 120
    def metadata_limits():
        pack_seed.child_limits()
        resource.setrlimit(resource.RLIMIT_FSIZE, (pack_seed.MAX_PACK, pack_seed.MAX_PACK))
    local.child_limits = metadata_limits
    local.run('fetch', '--depth=2', '--filter=blob:none', '--no-tags', '--no-recurse-submodules',
              '--no-write-fetch-head', 'origin', checked_sha(preview_commit))
    local.run('update-ref', '--no-deref', 'HEAD', preview_commit)
    local.check_source(preview_commit)
    # A server which ignored blob:none must not silently widen this recovery.
    inventory = local.run('cat-file', '--batch-all-objects', '--batch-check=%(objecttype)')
    if any(kind not in (b'commit', b'tree') for kind in inventory.splitlines()):
        fail('retained_tree_mismatch')
    return local


def fetch_readback_blobs(token, entries, directory, remaining, protocol):
    """Reuse the exact-object bounded Git transfer, in a fresh offline store.

    The only remote wants are validated Get blob identities. No local cache,
    promisor fetch, non-Get body or alternate transport can satisfy this proof.
    """
    paths = {'objects/' + checked_sha(blob): {'sha': blob, 'size': size}
             for blob, size in entries.items()}
    expected = pack_seed.expected_blobs(paths)
    if remaining <= 0:
        fail('source_tree_budget')
    directory.mkdir(mode=0o700)
    pack = directory / 'readback.pack'
    used = pack_seed.fetch_pack(token, expected, pack, maximum=remaining, protocol=protocol)
    pack_seed.verify_materialize(pack, directory / 'objects.git', expected, paths, directory / 'data')
    return {blob: directory / 'data' / 'objects' / blob for blob in expected}, used


def validate_retained_plan(plan, source, before, after, before_metadata, after_metadata):
    if not isinstance(plan, dict) or not valid_plan_keys(plan):
        fail('invalid_plan')
    if (type(plan.get('version')) is not int or plan['version'] != 1
            or plan.get('scope') != 'get-private-preview' or plan.get('source_commit') != source
            or plan.get('published') is not False or plan.get('apply_ready') is not False
            or type(plan.get('pending')) is not int or not 0 <= plan['pending'] <= MAX_FILES
            or plan.get('status') != ('partial' if plan['pending'] else 'complete')
            or not isinstance(plan.get('reason'), str)):
        fail('invalid_plan')
    rows = plan.get('changes')
    if not isinstance(rows, list) or len(rows) > MAX_FILES:
        fail('invalid_plan')
    expected = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            fail('invalid_plan_row')
        path = safe_path(row.get('path'))
        if path == PLAN_PATH or path in expected or path not in after:
            fail('invalid_plan_path')
        if any(type(row.get(key)) is not int or not 0 <= row[key] <= MAX_BLOB_BYTES for key in ('old_bytes', 'new_bytes')):
            fail('invalid_plan_row')
        if not isinstance(row.get('new_sha256'), str) or re.fullmatch('[0-9a-f]{64}', row['new_sha256']) is None:
            fail('invalid_plan_row')
        checked_sha(row.get('new_blob'))
        old, new = before.get(path), after[path]
        if new[1] != 'blob' or new[0] not in ('100644', '100755'):
            fail('nonregular_source')
        if row['action'] != ('modify' if old else 'add'):
            fail('preview_deletion_forbidden')
        if old is None:
            if row['old_blob'] is not None or row['old_sha256'] is not None or row['old_bytes'] != 0 or new[0] != '100644':
                fail('plan_old_bytes_mismatch')
        else:
            if (old[1] != 'blob' or old[0] not in ('100644', '100755') or old[0] != new[0]
                    or row['old_blob'] != old[2] or row['old_bytes'] != before_metadata[path][3]
                    or not isinstance(row['old_sha256'], str) or re.fullmatch('[0-9a-f]{64}', row['old_sha256']) is None):
                fail('plan_old_bytes_mismatch')
        if row['new_blob'] != new[2] or row['new_bytes'] != after_metadata[path][3]:
            fail('plan_new_bytes_mismatch')
        expected[path] = row
    if PLAN_PATH in before or after.get(PLAN_PATH, ())[:2] != ('100644', 'blob'):
        fail('reserved_plan_path_exists')
    if set(before) - set(after):
        fail('preview_deletion_forbidden')
    actual = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    # Equality covers modes, additions, removals, every non-Get subtree and the
    # dedicated review-plan blob. No unplanned file can hide outside Get.
    if actual != set(expected) | {PLAN_PATH}:
        fail('retained_tree_mismatch')
    return expected


def verify_retained(vault, source_commit, preview_commit, *, token=None, remote=None):
    """Read an already-retained candidate without source API or any remote write.

    A new local object database and explicit Git batch fetch establish fresh
    remote payload reads. This verifies retained bytes and their plan, not
    upstream completeness, the nine pending originals or production readiness.
    """
    stage = 'preflight'
    try:
        source_commit, preview_commit = checked_sha(source_commit), checked_sha(preview_commit)
        token = token if token is not None else (None if remote is not None else os.environ.get('PRIVATE_VAULT_TOKEN'))
        remote = remote if remote is not None else GitHubRemote(None, token)
        metadata = remote.metadata()
        if metadata.get('full_name') != REPOSITORY or metadata.get('private') is not True:
            fail('repository_not_private_target')
        if remote.ref(BACKUP_REF) != source_commit:
            fail('backup_not_verified')
        # Only the two fixed review destinations are eligible. Select by the
        # caller's explicit commit; no arbitrary ref and no ref mutation.
        if remote.ref(PREVIEW_REF) == preview_commit:
            preview_ref = PREVIEW_REF
        elif remote.ref(FIDELITY_PREVIEW_REF) == preview_commit:
            preview_ref = FIDELITY_PREVIEW_REF
        else:
            fail('preview_ref_changed')
        preview_info, source_info = remote.commit(preview_commit), remote.commit(source_commit)
        if preview_info.get('parents') != [source_commit]:
            fail('existing_preview_conflict')
        if remote.ref(MAIN_REF) is None:
            fail('remote_main_changed')
        stage = 'metadata'
        before_metadata = remote.check_source_budget(source_commit)
        after_metadata = remote.check_source_budget(preview_commit)
        if PLAN_PATH not in after_metadata or after_metadata[PLAN_PATH][3] > MAX_PLAN_BYTES:
            fail('retained_plan_missing')
        stage = 'fetch'
        local = readback_checkout(vault, preview_commit, token)
        for commit, info in ((source_commit, source_info), (preview_commit, preview_info)):
            if local.run('rev-parse', commit + '^{tree}').decode().strip() != info.get('tree'):
                fail('invalid_source_commit')
        # Validate the raw local parent field too; replacement/graft mechanisms
        # remain disabled and forbidden in this fresh checkout.
        headers = local.run('cat-file', '-p', preview_commit).split(b'\n\n', 1)[0].splitlines()
        if [line[7:].decode() for line in headers if line.startswith(b'parent ')] != [source_commit]:
            fail('existing_preview_conflict')
        before, after = local.tree(source_commit), local.tree(preview_commit)
        for tree, indexed in ((before, before_metadata), (after, after_metadata)):
            selected = {path: entry for path, entry in tree.items() if path.startswith(GET + '/')}
            if selected != {path: entry[:3] for path, entry in indexed.items()}:
                fail('retained_tree_mismatch')
        stage = 'plan'
        plan_blob = after[PLAN_PATH][2]
        pack_bytes = sum(p.stat().st_size for p in (local.vault / '.git/objects/pack').glob('*.pack'))
        protocol = pack_seed.capabilities(token)
        with tempfile.TemporaryDirectory(prefix='get-retained-plan-') as temporary:
            materialized, used = fetch_readback_blobs(token, {plan_blob: after_metadata[PLAN_PATH][3]},
                Path(temporary) / 'plan', pack_seed.MAX_PACK - pack_bytes, protocol)
            plan_bytes = file_bytes(materialized[plan_blob], MAX_PLAN_BYTES)
        pack_bytes += used
        if len(plan_bytes) > MAX_PLAN_BYTES or len(plan_bytes) != after_metadata[PLAN_PATH][3] or oid(plan_bytes) != plan_blob:
            fail('retained_payload_mismatch')
        plan = strict_json(plan_bytes)
        if preview_ref_for_plan(plan) != preview_ref:fail('invalid_plan')
        # The retention helper wrote this canonical representation. Reject
        # alternate serialization or trailing extra content in a retained plan.
        if canonical(plan) != plan_bytes:
            fail('invalid_plan')
        stage = 'tree'
        expected = validate_retained_plan(plan, source_commit, before, after, before_metadata, after_metadata)
        stage = 'payload'
        requested = {}
        for row in expected.values():
            for prefix in ('old', 'new'):
                blob, size = row[prefix + '_blob'], row[prefix + '_bytes']
                if blob is not None:
                    if blob in requested and requested[blob] != size:
                        fail('invalid_plan_row')
                    requested[blob] = size
        if (len(requested) + 1 > MAX_FILES
                or sum(requested.values()) + len(plan_bytes) > MAX_GET_BYTES):
            fail('source_tree_budget')
        verified_bytes = 0
        if requested:
            with tempfile.TemporaryDirectory(prefix='get-retained-payload-') as temporary:
                materialized, used = fetch_readback_blobs(token, requested, Path(temporary) / 'payload',
                    pack_seed.MAX_PACK - pack_bytes, protocol)
                for row in expected.values():
                    for prefix in ('old', 'new'):
                        blob = row[prefix + '_blob']
                        if blob is None:
                            continue
                        data = file_bytes(materialized[blob])
                        if (oid(data) != blob or sha256(data) != row[prefix + '_sha256'] or len(data) != row[prefix + '_bytes']):
                            fail('retained_payload_mismatch')
                        if prefix == 'new':
                            verified_bytes += len(data)
        # The public counts refer to plan-listed changed outputs; the separate
        # plan_verified flag and byte count account for the additional plan file.
        stage = 'refs'
        local.check_source(preview_commit)
        if remote.ref(preview_ref) != preview_commit:
            fail('preview_ref_changed')
        if remote.ref(BACKUP_REF) != source_commit:
            fail('backup_ref_readback_failed')
        final_main = remote.ref(MAIN_REF)
        if final_main is None:
            fail('remote_main_changed')
        checked_sha(final_main)
        return {'status': 'preview_readback_verified', 'backup_verified': True, 'preview_retained': True,
                'preview_only': True, 'main_updated': False, 'published': False, 'apply_ready': False,
                'source_unchanged': True, 'main_advanced': final_main != source_commit,
                'pending': plan['pending'], 'source_status': plan['status'], 'changed_paths': len(expected),
                'verified_paths': len(expected), 'verified_bytes': verified_bytes,
                'plan_verified': True, 'verified_plan_bytes': len(plan_bytes), 'verification_stage': 'verified'}
    except RetentionFailure as error:
        error.verification_stage = stage
        raise
    except Exception:
        error = RetentionFailure('retained_readback_failed')
        error.verification_stage = stage
        raise error from None


def public_report(value):
    """A strict presentation filter, never authentication or independent proof."""
    statuses = {'private_checkout_verified', 'retention_failed', 'preview_readback_verified'}
    for prefix in ('backup_', 'preview_'):
        statuses.update(prefix + suffix for suffix in ('existing_verified', 'created_verified', 'created_verified_after_uncertain_response'))
    valid = isinstance(value, dict) and isinstance(value.get('status'), str) and value['status'] in statuses
    if valid and value['status'] != 'retention_failed':
        valid = value.get('source_unchanged') is True and value.get('main_updated') is False
        if value['status'].startswith('backup_'):
            valid = valid and value.get('backup_verified') is True
        elif value['status'] == 'private_checkout_verified':
            valid = valid and value.get('checkout_verified') is True
        else:
            valid = (valid and value.get('backup_verified') is True and value.get('preview_retained') is True
                     and value.get('preview_only') is True and value.get('apply_ready') is False
                     and value.get('published') is False and type(value.get('pending')) is int
                     and 0 <= value['pending'] <= 1000000 and type(value.get('changed_paths')) is int
                     and 0 <= value['changed_paths'] <= 1000000
                     and value.get('source_status') == ('partial' if value['pending'] else 'complete')
                     and type(value.get('main_advanced')) is bool)
    if valid and value['status'] == 'preview_readback_verified':
        valid = (value.get('plan_verified') is True and value.get('verification_stage') == 'verified'
                 and type(value.get('verified_paths')) is int and value['verified_paths'] == value['changed_paths']
                 and type(value.get('verified_bytes')) is int and 0 <= value['verified_bytes'] <= MAX_GET_BYTES
                 and type(value.get('verified_plan_bytes')) is int and 0 < value['verified_plan_bytes'] <= MAX_PLAN_BYTES)
    if not valid:
        value = {'status': 'retention_failed', 'error': 'invalid_report'}
    result = {'status': value['status'], 'main_updated': False, 'published': False}
    if value['status'] == 'retention_failed':
        error = value.get('error')
        result['error'] = error if isinstance(error, str) and error in ERROR_CODES else 'retention_failed'
        if value.get('verification_stage') in ('preflight', 'metadata', 'fetch', 'plan', 'tree', 'payload', 'refs'):
            result['verification_stage'] = value['verification_stage']
        return result
    for field in ('backup_verified', 'preview_retained', 'preview_only', 'checkout_verified', 'source_unchanged', 'main_advanced', 'plan_verified'):
        if type(value.get(field)) is bool:
            result[field] = value[field]
    if 'apply_ready' in value:
        result['apply_ready'] = False
    for field in ('pending', 'changed_paths'):
        if type(value.get(field)) is int and 0 <= value[field] <= 1000000:
            result[field] = value[field]
    if value.get('source_status') in ('partial', 'complete'):
        result['source_status'] = value['source_status']
    if value['status'] == 'preview_readback_verified':
        for field in ('verified_paths', 'verified_bytes', 'verified_plan_bytes', 'verification_stage'):
            result[field] = value[field]
    return result


def main(argv=None):
    class QuietParser(argparse.ArgumentParser):
        def error(self, message):
            fail('conflicting_modes')
    parser = QuietParser(description='Authorized fixed private preview retention only')
    # Accept the requested checkout subcommand while keeping the other two
    # established flag interfaces unchanged for the workflow caller.
    parser.add_argument('operation', nargs='?', choices=('checkout', 'prepare', 'retain', 'verify-retained'))
    parser.add_argument('--prepare-backup', action='store_true')
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--preview-output', type=Path)
    parser.add_argument('--source-commit')
    parser.add_argument('--preview-commit')
    parser.add_argument('--publish-report', type=Path)
    try:
        args = parser.parse_args(argv)
    except RetentionFailure:
        print(json.dumps(public_report({'status': 'retention_failed', 'error': 'conflicting_modes'})))
        return 1
    try:
        if args.publish_report is not None:
            if args.operation or args.prepare_backup or args.vault or args.preview_output or args.source_commit or args.preview_commit:
                fail('conflicting_modes')
            result = public_report(strict_json(file_bytes(args.publish_report, 1024 * 1024)))
            print(json.dumps(result, sort_keys=True))
            return 0  # The summary step must not replace the actual operation exit code.
        if args.vault is None or args.source_commit is None:
            fail('invalid_checkout')
        if args.preview_commit is not None and args.operation != 'verify-retained':
            fail('conflicting_modes')
        if args.operation == 'verify-retained':
            if args.prepare_backup or args.preview_output is not None:
                fail('conflicting_modes')
            if args.preview_commit is None:
                fail('missing_preview_commit')
            result = verify_retained(args.vault, args.source_commit, args.preview_commit)
        elif args.operation == 'checkout':
            if args.prepare_backup or args.preview_output is not None:
                fail('conflicting_modes')
            result = checkout_private(args.vault, args.source_commit)
        elif args.prepare_backup or args.operation == 'prepare':
            if args.preview_output is not None or args.operation == 'retain':
                fail('conflicting_modes')
            result = prepare_backup(args.vault, args.source_commit)
        else:
            if args.preview_output is None:
                fail('missing_preview_output')
            result = retain_private_preview(args.vault, args.preview_output, args.source_commit)
        print(json.dumps(public_report(result), sort_keys=True))
        return 2 if result.get('pending') else 0
    except RetentionFailure as error:
        print(json.dumps(public_report({'status': 'retention_failed', 'error': str(error), 'verification_stage': getattr(error, 'verification_stage', None)})))
        return 0 if args.publish_report is not None else 1
    except Exception:
        print(json.dumps(public_report({'status': 'retention_failed', 'error': 'retention_failed'})))
        return 0 if args.publish_report is not None else 1





def valid_plan_keys(plan):
    if not isinstance(plan,dict):return False
    if set(plan)==PLAN_KEYS:return True
    return (set(plan)==PLAN_KEYS|{'state_contract','local_proposal_only','conflicts'}
            and plan['state_contract']=='get-source-raw-local-2'
            and plan['local_proposal_only'] is True
            and type(plan['conflicts']) is int and type(plan.get('pending')) is int
            and 0<=plan['conflicts']<=plan['pending']<=2500)

def preview_ref_for_plan(plan):
    if not valid_plan_keys(plan):fail('invalid_plan')
    return FIDELITY_PREVIEW_REF if plan.get('local_proposal_only') is True else PREVIEW_REF


if __name__ == '__main__':
    raise SystemExit(main())
