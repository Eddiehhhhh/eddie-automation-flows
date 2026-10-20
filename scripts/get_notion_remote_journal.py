"""Injected private Git journal backend; no HTTP, credentials, CLI or deployment.

The caller supplies externally trusted commit/blob/state anchors, an exact-OID
object loader, source/writer guards and a durable confirmation callback. The
callback must return True before the executor may perform its next Notion write.
Only the fixed journal path is changed; one CAS at most per call. Uncertain
outcomes are read back, never retried. A poisoned instance cannot be reused.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import tempfile
import threading
import time

import get_notion_executor as engine
import get_notion_outbox as codec
import get_private_preview_retention as retention

REPOSITORY = 'Eddiehhhhh/eddie-llm-wiki'
REF = 'refs/heads/main'
STATE_PATH = '.sync/get-notion/state.json'
MAX_STATE = 32 * 1024 * 1024
MAX_COMMITS = 400
MAX_BATCH_BYTES = 192 * 1024 * 1024
MAX_HISTORY = 256
RECEIPT_CONTRACT = 'get-notion-remote-journal-receipt-1'


class JournalFailure(engine.Blocked):
    """Fixed public reason, plus private bounded recovery metadata only."""
    def __init__(self, reason='journal_failed', *, stage='read', unknown=False,
                 recovery=None):
        super().__init__(reason)
        self.stage = stage
        self.outcome = 'unknown' if unknown else 'not_attempted'
        self.recovery = deepcopy(recovery)

    def public_report(self):
        return {'status': self.outcome, 'stage': self.stage}


def checked(value, width):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{%d}' % width, value):
        raise JournalFailure('state_invalid', stage='anchor')
    return value


class ImmutableJournalRemote:
    """Bounded per-run reuse of exact Git objects; never cache mutable guards.

    Repository identity, refs, writer checks and transport writes always reach
    the original remote. Each candidate readback starts a new cache generation,
    so its commit, journal trees and state bytes are fetched remotely again.
    Cached responses are copied; no failed response or local-only object enters
    this cache. Eviction changes performance only, never read/write budgets.
    """
    def __init__(self, remote, *, max_cache_bytes=MAX_BATCH_BYTES):
        if type(max_cache_bytes) is not int or not 1 <= max_cache_bytes <= MAX_BATCH_BYTES:
            raise JournalFailure('request_budget', stage='budget')
        self.remote = remote
        self.max_cache_bytes = max_cache_bytes
        self.cache = OrderedDict()
        self.cache_bytes = 0

    def __getattr__(self, name):
        return getattr(self.remote, name)

    def begin_readback(self):
        self.cache.clear()
        self.cache_bytes = 0

    def _object(self, key, read):
        if key in self.cache:
            value, size = self.cache.pop(key)
            self.cache[key] = value, size
            return deepcopy(value)
        value = read()
        size = len(value) if isinstance(value, bytes) else len(codec.encoded(value))
        if size <= self.max_cache_bytes:
            while self.cache and self.cache_bytes + size > self.max_cache_bytes:
                _, (_, removed) = self.cache.popitem(last=False)
                self.cache_bytes -= removed
            self.cache[key] = deepcopy(value), size
            self.cache_bytes += size
        return value

    def commit(self, oid):
        checked(oid, 40)
        def read():
            value = self.remote.commit(oid)
            if not isinstance(value, dict) or not isinstance(value.get('parents'), list):
                raise JournalFailure('state_invalid', stage='readback')
            checked(value.get('tree'), 40)
            for parent in value['parents']: checked(parent, 40)
            return value
        return self._object(('commit', oid), read)

    def tree_entries(self, oid, recursive=False):
        checked(oid, 40)
        if type(recursive) is not bool:
            raise JournalFailure('state_invalid', stage='readback')
        def read():
            value = self.remote.tree_entries(oid, recursive) if recursive else self.remote.tree_entries(oid)
            if not isinstance(value, list):
                raise JournalFailure('state_invalid', stage='readback')
            for row in value:
                if not isinstance(row, dict):
                    raise JournalFailure('state_invalid', stage='readback')
                checked(row.get('sha'), 40)
            return value
        return self._object(('tree', oid, recursive), read)

    def blob(self, oid, size):
        checked(oid, 40)
        if type(size) is not int or size < 0:
            raise JournalFailure('state_invalid', stage='readback')
        def read():
            value = self.remote.blob(oid, size)
            if not isinstance(value, bytes) or len(value) != size or retention.oid(value) != oid:
                raise JournalFailure('state_invalid', stage='readback')
            return value
        return self._object(('blob', oid, size), read)

    # Route immutable lookups inside this guard through the cache as well.
    # The caller still performs its complete fresh repository/ref guards.
    source_get_tree = retention.GitHubRemote.source_get_tree


class RemoteJournal:
    """Implements read() and compare_and_set(expected_sha, state).

    remote: metadata(), ref(REF), commit(oid), blob(oid, size),
            cas_main(expected_parent, candidate_commit).
    load_commit(oid): hydrate only exact commit/tree/journal objects in root;
                     never advance refs/index or materialize inherited assets.
    guard(head): independently revalidate caller's source/protected/writer gates.
    confirm(receipt): durably retain verified provenance; only literal True
                      permits success. This is not an authorization mechanism.

    anchor keys are commit, state_blob (None only for proven absence), and
    state_sha256. They must come from trusted approval/success provenance, not
    from hashing the state currently under inspection. A fresh process needs a
    new externally verified anchor after earlier journal writes.
    """
    def __init__(self, root, remote, *, anchor, load_commit, guard, confirm,
                 max_commits=MAX_COMMITS, max_batch_bytes=MAX_BATCH_BYTES,
                 poll_attempts=6, sleep=time.sleep):
        self.poisoned = False
        self._png_not_dispatched = None
        self.lock = threading.Lock()
        self.recovery = None
        self.stage = 'anchor'
        if (not isinstance(anchor, dict)
                or set(anchor) != {'commit', 'state_blob', 'state_sha256'}):
            raise JournalFailure('state_invalid', stage='anchor')
        checked(anchor['commit'], 40); checked(anchor['state_sha256'], 64)
        if anchor['state_blob'] is not None: checked(anchor['state_blob'], 40)
        for value, limit in ((max_commits, MAX_COMMITS),
                             (max_batch_bytes, MAX_BATCH_BYTES), (poll_attempts, 6)):
            if type(value) is not int or not 1 <= value <= limit:
                raise JournalFailure('request_budget', stage='budget')
        if not all(callable(f) for f in (load_commit, guard, confirm, sleep)):
            raise JournalFailure('state_invalid', stage='anchor')
        self.anchor = deepcopy(anchor)
        self.remote = remote
        self.load_commit = load_commit
        self.guard = guard
        self.confirm = confirm
        self.sleep = sleep
        self.max_commits = max_commits
        self.max_batch_bytes = max_batch_bytes
        self.poll_attempts = poll_attempts
        self.commits_attempted = 0
        self.new_blob_bytes = 0
        self.charged_blobs = {anchor['state_blob']} if anchor['state_blob'] else set()
        # Separate offline LocalGit: never alter a caller's credentialed transport.
        try:
            self.local = retention.LocalGit(root)
        except Exception:
            raise JournalFailure('state_invalid', stage='anchor') from None
        env = self.local.env
        count = int(env['GIT_CONFIG_COUNT'])
        env.update(GIT_NO_LAZY_FETCH='1', GIT_CONFIG_COUNT=str(count + 1))
        env['GIT_CONFIG_KEY_' + str(count)] = 'protocol.allow'
        env['GIT_CONFIG_VALUE_' + str(count)] = 'never'
        if self.local.run('rev-parse', '--show-object-format').strip() != b'sha1':
            raise JournalFailure('state_invalid', stage='anchor')

    def _ready(self):
        if self.poisoned:
            raise JournalFailure(stage='locked', unknown=True, recovery=self.recovery)

    def _ref(self, expected=None):
        self.stage = 'guard'
        meta = self.remote.metadata()
        if meta.get('full_name') != REPOSITORY or meta.get('private') is not True:
            raise JournalFailure(stage='guard')
        head = checked(self.remote.ref(REF), 40)
        if expected is not None and head != expected:
            raise JournalFailure('state_changed', stage='guard')
        return head

    def _head(self, expected=None):
        head = self._ref(expected)
        if self.guard(head) is not True:
            raise JournalFailure(stage='guard')
        return head

    def _local_tree(self, head):
        # Git can read a loose object stored under the wrong OID. Rehash the
        # commit and every inherited tree before trusting a cached remote OID;
        # asset blobs are deliberately neither read nor hydrated.
        raw = self.local.run('cat-file', 'commit', head)
        if hashlib.sha1(b'commit ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != head:
            raise JournalFailure('state_invalid', stage='load')
        root = checked(self.local.run('rev-parse', head + '^{tree}').decode().strip(), 40)
        trees = {root}; entries = {}
        rows = self.local.run('ls-tree', '-rtz', head)
        if len(rows) > MAX_BATCH_BYTES:
            raise JournalFailure('request_budget', stage='load')
        for row in rows.split(b'\0'):
            if not row: continue
            meta, path = row.split(b'\t', 1)
            mode, kind, oid = meta.decode().split(); checked(oid, 40)
            path = path.decode()
            if kind == 'tree': trees.add(oid)
            elif path in entries:
                raise JournalFailure('state_invalid', stage='load')
            else: entries[path] = (mode, kind, oid)
        ordered = sorted(trees)
        packed = self.local.run('cat-file', '--batch', data=('\n'.join(ordered) + '\n').encode())
        if len(packed) > MAX_BATCH_BYTES:
            raise JournalFailure('request_budget', stage='load')
        offset = 0
        for oid in ordered:
            end = packed.index(b'\n', offset)
            header = packed[offset:end].split()
            if len(header) != 3 or header[:2] != [oid.encode(), b'tree'] or not header[2].isdigit():
                raise JournalFailure('state_invalid', stage='load')
            size = int(header[2]); offset = end + 1
            data = packed[offset:offset + size]; offset += size
            if (len(data) != size or packed[offset:offset + 1] != b'\n'
                    or hashlib.sha1(b'tree ' + str(size).encode() + b'\0' + data).hexdigest() != oid):
                raise JournalFailure('state_invalid', stage='load')
            offset += 1
        if offset != len(packed):
            raise JournalFailure('state_invalid', stage='load')
        return root, entries

    def _commit(self, head):
        self.stage = 'load'
        self.load_commit(head)
        if self.local.run('cat-file', '-t', head).strip() != b'commit':
            raise JournalFailure('state_invalid', stage='load')
        tree, entries = self._local_tree(head)
        parents = self.local.run('rev-list', '--parents', '-n', '1', head).decode().split()
        if not parents or parents[0] != head:
            raise JournalFailure('state_invalid', stage='load')
        remote = self.remote.commit(head)
        if remote.get('tree') != tree or remote.get('parents') != parents[1:]:
            raise JournalFailure('state_invalid', stage='readback')
        return tree, parents[1:], entries

    def _state(self, tree):
        row = tree.get(STATE_PATH)
        if row is None:
            value = codec.initial_state()
            return value, codec.digest(value), None, codec.encoded(value)
        if row[:2] != ('100644', 'blob'):
            raise JournalFailure('state_invalid', stage='readback')
        size = self.local.run('cat-file', '-s', checked(row[2], 40)).decode().strip()
        if not size.isdigit() or int(size) > MAX_STATE:
            raise JournalFailure('request_budget', stage='readback')
        data = self.local.blob(row[2])
        if (len(data) != int(size) or len(data) > MAX_STATE
                or retention.oid(data) != row[2]
                or self.remote.blob(row[2], len(data)) != data):
            raise JournalFailure('state_invalid', stage='readback')
        value = retention.strict_json(data)
        codec.check_state(value)
        if codec.encoded(value) != data:
            raise JournalFailure('state_invalid', stage='readback')
        return value, codec.digest(data), row[2], data

    def _current(self, *, recheck=True):
        head = self._head()
        _, _, anchored_tree = self._commit(self.anchor['commit'])
        _, digest, blob, _ = self._state(anchored_tree)
        if (digest != self.anchor['state_sha256'] or blob != self.anchor['state_blob']):
            raise JournalFailure('state_changed', stage='anchor')
        tree_oid, _, tree = self._commit(head)
        self.local.run('merge-base', '--is-ancestor', self.anchor['commit'], head)
        history = self.local.run('rev-list', '--max-count=' + str(MAX_HISTORY + 1),
                                 self.anchor['commit'] + '..' + head).splitlines()
        if len(history) > MAX_HISTORY:
            raise JournalFailure('request_budget', stage='history')
        # Detect changed-then-restored journals too; do not trust tip equality.
        touched = self.local.run('rev-list', '--full-history', '--max-count=1',
                                 self.anchor['commit'] + '..' + head, '--', STATE_PATH)
        if touched.strip():
            raise JournalFailure('state_changed', stage='history')
        value, digest, blob, data = self._state(tree)
        if digest != self.anchor['state_sha256'] or blob != self.anchor['state_blob']:
            raise JournalFailure('state_changed', stage='readback')
        if recheck: self._head(head)
        return head, tree_oid, tree, value, digest, blob, data

    def read(self):
        if not self.lock.acquire(blocking=False):
            raise JournalFailure('journal_busy', stage='locked')
        try:
            return self._read()
        finally:
            self.lock.release()

    def _read(self):
        self._ready()
        try:
            _, _, _, value, digest, _, _ = self._current()
            ticket = getattr(self, '_png_not_dispatched', None)
            if ticket is not None and digest != ticket['after_sha256']: self._png_not_dispatched = None
            return deepcopy(value), digest
        except JournalFailure:
            raise
        except Exception:
            raise JournalFailure(stage=self.stage) from None

    def _build(self, parent, before, data):
        self.stage = 'prepare'
        blob = retention.oid(data)
        with tempfile.TemporaryDirectory(prefix='get-notion-journal-index-') as directory:
            env = dict(self.local.env, GIT_INDEX_FILE=str(Path(directory) / 'index'))
            self.local.run('read-tree', parent, env=env)
            if self.local.run('hash-object', '-w', '--stdin', data=data, env=env).decode().strip() != blob:
                raise JournalFailure(stage='prepare')
            self.local.run('update-index', '--add', '--cacheinfo',
                           '100644,' + blob + ',' + STATE_PATH, env=env)
            tree_oid = checked(self.local.run('write-tree', '--missing-ok', env=env).decode().strip(), 40)
            wanted = dict(before, **{STATE_PATH: ('100644', 'blob', blob)})
            if self.local.tree(tree_oid) != wanted or self.local.blob(blob) != data:
                raise JournalFailure(stage='prepare')
            env.update(GIT_AUTHOR_NAME='Get journal', GIT_AUTHOR_EMAIL='get-journal@users.noreply.github.com',
                       GIT_COMMITTER_NAME='Get journal', GIT_COMMITTER_EMAIL='get-journal@users.noreply.github.com')
            commit = checked(self.local.run('commit-tree', tree_oid, '-p', parent,
                '-m', 'Persist private Notion transaction journal', env=env).decode().strip(), 40)
        if self.local.run('rev-list', '--parents', '-n', '1', commit).decode().split() != [commit, parent]:
            raise JournalFailure(stage='prepare')
        return commit, tree_oid, blob, wanted

    def _verify(self, commit, parent, tree_oid, wanted, data):
        self.stage = 'readback'
        if isinstance(self.remote, ImmutableJournalRemote):
            self.remote.begin_readback()
        actual_tree, parents, tree = self._commit(commit)
        if actual_tree != tree_oid or parents != [parent] or tree != wanted:
            raise JournalFailure(stage='readback')
        value, digest, blob, actual = self._state(tree)
        if actual != data or digest != codec.digest(data) or blob != retention.oid(data):
            raise JournalFailure(stage='readback')
        self._head(commit)
        return {'contract': RECEIPT_CONTRACT, 'repository': REPOSITORY, 'ref': REF,
                'path': STATE_PATH, 'parent_commit': parent, 'commit': commit,
                'tree': tree_oid, 'state_blob': blob, 'state_sha256': digest,
                'state_bytes': len(data), 'cas_attempts': 1, 'readback_verified': True}, value

    def _confirm(self, receipt, value):
        self.stage = 'confirm'
        self.confirm_entered = True
        self.recovery = deepcopy(receipt)
        if self.confirm(deepcopy(receipt)) is not True:
            raise JournalFailure(stage='confirm', unknown=True, recovery=receipt)
        self._head(receipt['commit'])
        self.anchor = {key: receipt[key] for key in ('commit', 'state_blob', 'state_sha256')}
        return deepcopy(value), receipt['state_sha256']

    def compare_and_set(self, expected_sha, value):
        return self.compare_and_set_verified(expected_sha, value)[1]

    def compare_and_set_verified(self, expected_sha, value):
        """Return the exact confirmed remote readback within the CAS lock.

        Adapters may consume this instead of issuing a second durable read.
        This is not a cached read API: every call performs its own guarded CAS
        or fully validated no-op, checkpoint confirmation and final head guard.
        """
        if not self.lock.acquire(blocking=False):
            raise JournalFailure('journal_busy', stage='locked')
        try:
            return self._compare_and_set(expected_sha, value)
        finally:
            self.lock.release()

    def compare_and_set_png_not_dispatched(self, expected_sha, value, typed_evidence):
        """Single-use same-process rollback of an unattempted PNG request only."""
        if not self.lock.acquire(blocking=False):
            raise JournalFailure('journal_busy', stage='locked')
        try:
            ticket = getattr(self, '_png_not_dispatched', None)
            self._png_not_dispatched = None
            if ticket is None:
                raise JournalFailure('state_changed', stage='anchor')
            return self._compare_and_set(expected_sha, value,
                png_reset_ticket=ticket, typed_evidence=typed_evidence)
        finally:
            self.lock.release()

    def compare_and_set_png_expired(self, expected_sha, new_policy_sha256, observe):
        """One fixed run16 transition, fresh read evidence inside the CAS lock."""
        if not self.lock.acquire(blocking=False):
            raise JournalFailure('journal_busy', stage='locked')
        try:
            if not callable(observe):
                raise JournalFailure('state_changed', stage='anchor')
            return self._compare_and_set(expected_sha, None,
                png_expired=(new_policy_sha256, observe))
        finally:
            self.lock.release()

    def _compare_and_set(self, expected_sha, value, *, png_reset_ticket=None, typed_evidence=None, png_expired=None):
        # Every CAS attempt consumes the old ticket, including a no-op/failure.
        self._png_not_dispatched = None
        self._ready()
        self.recovery = None
        self.confirm_entered = False
        attempted = False
        try:
            checked(expected_sha, 64)
            if png_expired is not None:
                import get_notion_png_expired_recovery as expired
                parent, old_tree, before, old_value, digest, old_blob, old_data = self._current(recheck=False)
                if expected_sha != digest or digest != expired.STATE_SHA:
                    raise JournalFailure('state_changed', stage='anchor')
                policy, observe = png_expired
                fresh_evidence = observe()
                self._head(parent)
                value = expired.transition(old_value, new_policy_sha256=policy, evidence=fresh_evidence)
            data = codec.encoded(value)
            if len(data) > MAX_STATE:
                raise JournalFailure('request_budget', stage='budget')
            codec.check_state(value)
            if png_expired is None:
                parent, old_tree, before, old_value, digest, old_blob, old_data = self._current(recheck=False)
            engine.plan_recovery.preserve_holds(old_value['engine_state'],value['engine_state'])
            import get_notion_historical as historical
            historical.preserve_captures(old_value,value)
            if png_expired is not None:
                expired.validate_transition(old_value, value, new_policy_sha256=policy, evidence=fresh_evidence)
            elif png_reset_ticket is None:
                codec.png_store.preserve(old_value,value)
            else:
                codec.png_store.check_not_dispatched(png_reset_ticket,old_value,value,typed_evidence)
            if expected_sha != digest:
                raise JournalFailure('state_changed', stage='anchor')
            blob = retention.oid(data)
            if data == old_data and old_blob is not None:
                self._head(parent)
                return self._confirm({'contract': RECEIPT_CONTRACT, 'repository': REPOSITORY,
                    'ref': REF, 'path': STATE_PATH, 'parent_commit': None, 'commit': parent,
                    'tree': old_tree, 'state_blob': old_blob, 'state_sha256': digest,
                    'state_bytes': len(data), 'cas_attempts': 0, 'readback_verified': True}, old_value)
            added_bytes = 0 if blob in self.charged_blobs else len(data)
            if (self.commits_attempted >= self.max_commits
                    or self.new_blob_bytes + added_bytes > self.max_batch_bytes):
                raise JournalFailure('request_budget', stage='budget')
            commit, tree_oid, blob, wanted = self._build(parent, before, data)
            self._head(parent)
            self.stage = 'cas'
            self.commits_attempted += 1
            self.new_blob_bytes += added_bytes
            self.charged_blobs.add(blob)
            attempted = True
            try:
                self.remote.cas_main(parent, commit)
            except Exception:
                pass  # Ambiguous response: exact readback, NEVER a second CAS.
            self.stage = 'poll'
            for attempt in range(self.poll_attempts):
                # Poll only mutable identity/ref. Full source guard follows the
                # complete readback and runs again after durable confirmation.
                observed = self._ref()
                if observed == commit: break
                if observed != parent or attempt == self.poll_attempts - 1:
                    raise JournalFailure(stage='poll', unknown=True)
                self.sleep(2 ** attempt)
            receipt, observed_value = self._verify(commit, parent, tree_oid, wanted, data)
            result = self._confirm(receipt, observed_value)
            if png_reset_ticket is None and png_expired is None:
                self._png_not_dispatched = codec.png_store.not_dispatched_ticket(old_value,observed_value)
            return result
        except Exception as exc:
            # Callback/readback failures may follow a committed journal. Preserve
            # only verified candidate metadata; block all subsequent API eligibility.
            expired_reason = None
            if png_expired is not None and isinstance(exc, ValueError):
                code = str(exc)
                if code in ('permission_denied', 'request_budget', 'response_invalid',
                        'png_expired_status_unverified', 'png_expired_missing_unverified',
                        'png_expired_evidence_invalid', 'png_expired_prestate_changed',
                        'png_expired_policy_unchanged', 'png_expired_plan_scope'):
                    expired_reason = code
            unknown = attempted or self.confirm_entered
            if unknown:
                self.poisoned = True
                raise JournalFailure(expired_reason or 'journal_failed', stage=self.stage, unknown=True,
                                     recovery=self.recovery) from None
            if isinstance(exc, JournalFailure): raise
            if expired_reason is not None:
                raise JournalFailure(expired_reason, stage=self.stage) from None
            reason = str(exc) if isinstance(exc, engine.Blocked) and str(exc) in (
                'state_invalid', 'state_changed', 'request_budget') else ('request_budget' if isinstance(exc, codec.Blocked) and str(exc) == 'outbox_budget' else 'journal_failed')
            raise JournalFailure(reason, stage=self.stage) from None
