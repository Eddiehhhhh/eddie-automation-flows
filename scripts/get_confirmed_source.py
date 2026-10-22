"""Read-only, bounded exact-object loader for the private Notion outbox.

The caller MUST independently authenticate tip (Actions marker/discovery) and
origin/after (approved origin or confirmed journal). This loader does not turn
Git hashes into authorship. It writes only missing objects to an existing local
ODB, never a ref, source Get API, asset, journal, or external destination.
"""
import base64
from copy import deepcopy
from pathlib import Path

import get_notion_outbox as outbox
import get_recurring_entry as entry

MAX_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_CONTENT_BYTES = 128 * 1024 * 1024
MAX_EVENTS = 2500
MAX_LINKS = 4096
MAX_COMMITS = 16384
MAX_FETCHES = 256


class SourceFailure(ValueError):
    pass


def fail(reason):
    raise SourceFailure(reason) from None


class Loader:
    def __init__(self, local, remote, token, root, policy_hash):
        entry.a.checked(policy_hash, 64)
        self.local = local
        self.reader = entry.a.OfflineGit(local.vault)
        self.remote = remote
        # token/root retain the shared runtime adapter signature. Credentials
        # are already encapsulated in the injected LocalGit/Remote, never read
        # from the environment here or sent to a different transport.
        self.policy = policy_hash
        self.commits = set(); self.fetches = 0
        self.receipts = {}; self.receipt_bytes = 0
        self.loaded_bytes = 0; self.metadata = {}

    def object_info(self, oid):
        entry.a.checked(oid, 40)
        raw = self.reader.local.run('cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)', data=(oid + '\n').encode())
        if raw == (oid + ' missing\n').encode():
            return None
        parts = raw.decode().split()
        if len(parts) != 3 or parts[0] != oid or not parts[2].isdigit():
            fail('source_object_invalid')
        return parts[1], int(parts[2])

    def fetch(self, commit):
        self.fetches += 1
        if self.fetches > MAX_FETCHES:
            fail('source_history_budget')
        self.local.run('fetch', '--depth=' + str(entry.MAX_INTERVENING + 2),
            '--filter=blob:none', '--no-tags', '--no-recurse-submodules',
            '--no-write-fetch-head', 'origin', entry.a.checked(commit, 40))

    def commit(self, commit):
        entry.a.checked(commit, 40)
        if commit in self.commits:
            # Other exact-object readers share this ODB and may fetch a
            # shallower tip after our first traversal. Object identity is
            # immutable, but shallow-boundary metadata is not. Recheck it
            # without resetting this loader's cumulative history budget.
            shallow = Path(self.local.vault) / '.git/shallow'
            if shallow.exists() and commit in entry.a.bounded_file(shallow, 2 * 1024 * 1024).decode().splitlines():
                self.fetch(commit)
            return
        if len(self.commits) >= MAX_COMMITS:
            fail('source_history_budget')
        info = self.object_info(commit)
        if info is None:
            self.fetch(commit)
        elif info[0] != 'commit':
            fail('source_object_invalid')
        # A shallow boundary hides parents from rev-list. Deepen only that
        # boundary; an already available complete object is never re-fetched.
        shallow = Path(self.local.vault) / '.git/shallow'
        if shallow.exists() and commit in entry.a.bounded_file(shallow, 2 * 1024 * 1024).decode().splitlines():
            self.fetch(commit)
        self.reader.tree(commit)
        self.commits.add(commit)

    def blob(self, commit, path, cap):
        self.commit(commit)
        row = self.reader.tree(commit).get(path)
        if row is None or row[:2] != ('100644', 'blob'):
            fail('source_object_invalid')
        oid = row[2]; info = self.object_info(oid)
        if info is None:
            value = self.remote.request('/git/blobs/' + oid, maximum=2 * cap + 65536)
            if not isinstance(value, dict) or value.get('sha') != oid or value.get('encoding') != 'base64' or type(value.get('size')) is not int or not 0 <= value['size'] <= cap or not isinstance(value.get('content'), str) or len(value['content']) > 2 * cap + 65536:
                fail('source_blob_invalid')
            try:
                data = base64.b64decode(value['content'].replace('\n', ''), validate=True)
            except (ValueError, TypeError):
                fail('source_blob_invalid')
            if len(data) != value['size'] or entry.r.oid(data) != oid:
                fail('source_blob_invalid')
            entry.m.import_blob(self.reader.local, data, oid)
            info = ('blob', len(data))
        if info[0] != 'blob' or info[1] > cap:
            fail('source_blob_invalid')
        return self.reader.read(commit, path, cap)

    def receipt(self, anchor):
        outbox.anchor(anchor)
        self.commit(anchor['commit'])
        if anchor['sequence'] == 0:
            if entry.FULL_RECEIPT in self.reader.tree(anchor['commit']):
                fail('source_chain_invalid')
            return None
        key = (anchor['commit'], anchor['receipt_sha256'])
        if key not in self.receipts:
            # Cap the final fetch before importing or decoding its payload.
            remaining = MAX_RECEIPT_BYTES - self.receipt_bytes
            if remaining <= 0 or remaining < entry.MAX_RECEIPT and self.blob_size(anchor['commit'], entry.FULL_RECEIPT) > remaining:
                fail('source_chain_budget')
            raw = self.blob(anchor['commit'], entry.FULL_RECEIPT, min(entry.MAX_RECEIPT, remaining))
            self.receipt_bytes += len(raw)
            value = entry.read_receipt(self.reader, anchor['commit'], anchor['receipt_sha256'], self.policy)
            self.receipts[key] = value
        value = self.receipts[key]
        if value['sequence'] != anchor['sequence']:
            fail('source_chain_invalid')
        return value

    def unchanged(self, ancestor, descendant):
        self.commit(ancestor); cursor = descendant
        expected = self.reader.source_get_tree(ancestor)
        for _ in range(entry.MAX_INTERVENING + 1):
            self.commit(cursor)
            if self.reader.source_get_tree(cursor) != expected:
                fail('source_foreign_get_change')
            if cursor == ancestor:
                return
            cursor = entry.single_parent(self.reader, cursor)
        fail('source_history_budget')

    def ancestry(self, ancestor, descendant):
        # Pending sources may be older than the fresh runner's shallow tip.
        # Load only linear commit/tree metadata, allowing later publications.
        cursor = descendant
        for _ in range(MAX_COMMITS):
            self.commit(cursor)
            if cursor == ancestor:
                return
            cursor = entry.single_parent(self.reader, cursor)
            self.reader.cache.clear()
        fail('source_history_budget')

    def publication(self, anchor):
        receipt = self.receipt(anchor)
        if receipt is None:
            fail('source_chain_invalid')
        commit = anchor['commit']; parent = receipt['parent_commit']
        self.commit(parent)
        if entry.single_parent(self.reader, commit) != parent or self.reader.source_get_tree(parent) != receipt['before_get_tree']:
            fail('source_chain_invalid')
        delta = entry.delta(self.reader, parent, commit)
        if entry.a.sha(entry.a.encoded(delta)) != receipt['delta_sha256']:
            fail('source_chain_invalid')
        paths = []
        for row in delta:
            path = row['path']; after = row['after']
            if after is None or after[:2] != ['100644', 'blob']:
                fail('source_foreign_get_change')
            if path in entry.STATE_PATHS:
                continue
            if not path.startswith(entry.r.GET + '/') or not path.lower().endswith(('.md', '.markdown')):
                fail('source_foreign_get_change')
            entry.r.safe_path(path); paths.append(path)
        if len(paths) != len(receipt['changed_content_ids']):
            fail('source_chain_invalid')
        return receipt, {'commit': commit, 'parent': parent, 'receipt_sha256': anchor['receipt_sha256']}, paths

    def blob_size(self, commit, path):
        row = self.reader.tree(commit).get(path)
        if row is None or row[:2] != ('100644', 'blob'):
            fail('source_blob_invalid')
        info = self.object_info(row[2])
        if info is not None:
            if info[0] != 'blob':
                fail('source_blob_invalid')
            return info[1]
        if commit not in self.metadata:
            self.metadata[commit] = self.remote.check_source_budget(commit)
        metadata = self.metadata[commit].get(path)
        if not isinstance(metadata, tuple) or len(metadata) != 4 or metadata[:3] != row or type(metadata[3]) is not int or metadata[3] < 0:
            fail('source_blob_invalid')
        return metadata[3]

    def materialize(self, row, paths, remaining):
        # Inspect metadata first so truncation NEVER fetches a body beyond the
        # byte budget. Publications are atomic; a cursor cannot split one.
        sizes = {path: self.blob_size(row['commit'], path) for path in paths}
        if any(size > outbox.p.MAX_NOTE for size in sizes.values()):
            fail('source_blob_invalid')
        used = sum(sizes.values())
        if used > remaining:
            return None
        if self.loaded_bytes + used > MAX_CONTENT_BYTES:
            fail('source_content_budget')
        for path, size in sizes.items():
            data = self.blob(row['commit'], path, size)
            if len(data) != size:
                fail('source_blob_invalid')
        self.loaded_bytes += used
        return used


def load_confirmed_chain(local, remote, token, root, tip, origin, after, policy_hash):
    """Return enqueue's private chain envelope; no state/ref/remote writes.

    tip/origin/after are exact {commit, receipt_sha256, sequence} anchors.
    Entire backwards receipt chain is proven first (16 MiB / 4096 links hard
    stop), then only the earliest <=500 publication /2500 event /128 MiB prefix
    is materialized. Incomplete prefixes cannot execute in QueuePublication.
    """
    for value in (tip, origin, after):
        outbox.anchor(value)
    if not origin['sequence'] <= after['sequence'] <= tip['sequence'] or after['sequence'] == origin['sequence'] and after != origin or tip['sequence'] == after['sequence'] and tip != after:
        fail('source_chain_invalid')
    loader = Loader(local, remote, token, root, policy_hash)
    loader.receipt(origin); loader.receipt(after)
    head = entry.a.checked(loader.reader.local.run('rev-parse', entry.r.MAIN_REF).decode().strip(), 40)
    loader.unchanged(tip['commit'], head)
    rows = []; cursor = deepcopy(tip)
    while cursor != after:
        if len(rows) >= MAX_LINKS or cursor['sequence'] <= after['sequence']:
            fail('source_chain_budget')
        receipt, row, paths = loader.publication(cursor)
        previous = {'commit': receipt['previous_receipt_commit'], 'receipt_sha256': receipt['previous_receipt_sha256'], 'sequence': receipt['sequence'] - 1}
        prior = loader.receipt(previous)
        if prior is None:
            if receipt['previous_registry_sha256'] is not None:
                fail('source_chain_invalid')
        elif prior['state_sha256'] != receipt['previous_legacy_sha256'] or prior['registry_sha256'] != receipt['previous_registry_sha256']:
            fail('source_chain_invalid')
        loader.unchanged(previous['commit'], row['parent'])
        rows.append((row, paths)); cursor = previous
        # Keep receipt/link evidence, not thousands of duplicate whole trees.
        loader.reader.cache.clear()
    # Even an empty chain loads the trusted tip receipt for enqueue's readback.
    loader.receipt(tip)
    selected = []; budget = {'bytes': 0, 'events': 0, 'receipt_bytes': 0}
    # enqueue rereads origin only for a brand-new journal. Reserve it always so
    # the same result is safe both for initialization and subsequent cursors.
    for anchor in (origin, tip):
        if anchor['sequence']:
            budget['receipt_bytes'] += len(loader.reader.read(anchor['commit'], entry.FULL_RECEIPT, entry.MAX_RECEIPT))
    cursor = deepcopy(after)
    for row, paths in reversed(rows):
        size = len(loader.reader.read(row['commit'], entry.FULL_RECEIPT, entry.MAX_RECEIPT))
        if len(selected) >= outbox.MAX_CHAIN or budget['events'] + len(paths) > MAX_EVENTS or budget['receipt_bytes'] + size > MAX_RECEIPT_BYTES:
            break
        used = loader.materialize(row, paths, MAX_CONTENT_BYTES - budget['bytes'])
        if used is None:
            break
        receipt, records = outbox.verified_publication(loader.reader, row, policy_hash, cursor, budget)
        selected.append(row)
        cursor = {'commit': row['commit'], 'receipt_sha256': row['receipt_sha256'], 'sequence': receipt['sequence']}
        loader.reader.cache.clear(); loader.metadata.clear()
    if rows and not selected:
        fail('source_publication_budget')
    result = {'repository': outbox.p.REPOSITORY, 'policy_sha256': policy_hash,
              'origin': deepcopy(origin), 'after': deepcopy(after), 'tip': deepcopy(tip), 'publications': selected}
    if len(outbox.encoded(result)) > 512 * 1024:
        fail('source_chain_budget')
    return result


def load_queue_publication(local, remote, token, root, publication, policy_hash):
    """Load one original pending publication's entire genuine Markdown diff.

    publication is the exact trusted pending/journal row containing only commit,
    parent, receipt_sha256. Return (receipt, actual_records), as the outbox's
    verified_publication does. Then construct QueuePublication normally; it
    verifies selected pending IDs and current-main content against this source.
    """
    if not isinstance(publication, dict) or set(publication) != {'commit', 'parent', 'receipt_sha256'}:
        fail('source_chain_invalid')
    for key in ('commit', 'parent'):
        entry.a.checked(publication[key], 40)
    entry.a.checked(publication['receipt_sha256'], 64)
    loader = Loader(local, remote, token, root, policy_hash)
    head = entry.a.checked(loader.reader.local.run('rev-parse', entry.r.MAIN_REF).decode().strip(), 40)
    loader.ancestry(publication['commit'], head)
    loader.blob(publication['commit'], entry.FULL_RECEIPT, entry.MAX_RECEIPT)
    receipt = entry.read_receipt(loader.reader, publication['commit'], publication['receipt_sha256'], policy_hash)
    anchor = {'commit': publication['commit'], 'receipt_sha256': publication['receipt_sha256'], 'sequence': receipt['sequence']}
    _, row, paths = loader.publication(anchor)
    if row != publication or len(paths) > MAX_EVENTS:
        fail('source_chain_invalid')
    if loader.materialize(row, paths, MAX_CONTENT_BYTES) is None:
        fail('source_publication_budget')
    return outbox.verified_publication(loader.reader, row, policy_hash)
