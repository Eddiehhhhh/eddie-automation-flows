"""Exact seven-blob hydration and fresh read-only guards for reviewed PNGs.

No credentials, ref updates, upload/page calls, or journal writes. The caller
authenticates the original publication and the external asset anchor. The
existing injected Loader/Remote do only bounded object reads. Local main stays
fixed; fresh remote heads may advance through unrelated/journal commits.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import get_confirmed_source as source_loader
import get_legacy_manual as m
import get_notion_png_evidence as evidence
import get_notion_remote_journal as journal
import get_notion_journal_runtime as journal_loader


class Blocked(ValueError): pass


def fail(reason): raise Blocked(reason) from None


class MetadataLoader(source_loader.Loader):
    def fetch(self, commit):
        # Detect a server ignoring blob:none. This is a post-fetch fail-closed
        # check, not a promise that a nonconforming server sent no extra bytes.
        view = SimpleNamespace(local=self.reader.local)
        before = journal_loader.ExactJournalLoader._blobs(view)
        super().fetch(commit)
        if journal_loader.ExactJournalLoader._blobs(view) - before:
            fail('png_loader_unrequested_blob')


def layout(local):
    root = Path(local.vault) / '.git'
    def data(name):
        path = root / name
        return None if not path.exists() else m.a.bounded_file(path, 16 * 1024 * 1024)
    return (local.run('for-each-ref', '--format=%(refname) %(objectname)'),
            data('HEAD'), data('index'), data('FETCH_HEAD'))


class LoadedAssets:
    def __init__(self, local, remote, publication, anchor, expected_anchor_sha, policy_hash):
        # Validate external scope before the first remote call or hydration.
        evidence.keys(anchor, ('contract', 'repository', 'publication_commit', 'asset_commit',
                              'manifest_path', 'manifest_blob', 'manifest_sha256'))
        evidence.hash_value(expected_anchor_sha, 64)
        if (evidence.digest(anchor) != expected_anchor_sha or anchor['contract'] != evidence.CONTRACT
                or anchor['repository'] != evidence.p.REPOSITORY
                or anchor['manifest_path'] != evidence.MANIFEST_PATH): fail('png_loader_anchor')
        for key in ('publication_commit', 'asset_commit', 'manifest_blob'): evidence.hash_value(anchor[key], 40)
        evidence.hash_value(anchor['manifest_sha256'], 64)
        if (not isinstance(publication, dict) or publication.get('commit') != anchor['publication_commit']
                or publication.get('repository') != evidence.p.REPOSITORY): fail('png_loader_anchor')
        if (local.run('config', '--get', 'remote.origin.partialclonefilter').strip() != b'blob:none'
                or local.run('config', '--get', 'remote.origin.promisor').strip() != b'true'):
            fail('png_loader_partial_clone')
        self.local = local; self.remote = remote; self.anchor = deepcopy(anchor)
        self._layout = layout(local)
        self.snapshot_head = m.a.checked(local.run('rev-parse', m.r.MAIN_REF).decode().strip(), 40)
        self._last_verified_head = self.snapshot_head
        self.loader = MetadataLoader(local, remote, None, None, policy_hash)
        head = m.guard(remote, self.snapshot_head)
        self.loader.commit(head); self.loader.commit(anchor['asset_commit'])
        self.loader.commit(anchor['publication_commit'])
        parent = source_loader.entry.single_parent(self.loader.reader, anchor['asset_commit'])
        self.loader.ancestry(anchor['publication_commit'], parent)
        self.loader.ancestry(anchor['asset_commit'], head)
        _, before = self._tree(parent); _, after = self._tree(anchor['asset_commit'])
        _, source = self._tree(anchor['publication_commit'])
        if after.get(evidence.MANIFEST_PATH) != ('100644', 'blob', anchor['manifest_blob']):
            fail('png_loader_anchor')
        raw = self._blob(anchor['asset_commit'], evidence.MANIFEST_PATH, evidence.MAX_MANIFEST)
        if evidence.digest(raw) != anchor['manifest_sha256']: fail('png_loader_anchor')
        manifest = evidence.strict_json(raw)
        evidence.keys(manifest, ('contract', 'repository', 'publication_commit', 'records'))
        if (manifest['contract'] != evidence.CONTRACT or manifest['repository'] != evidence.p.REPOSITORY
                or manifest['publication_commit'] != anchor['publication_commit']
                or not isinstance(manifest['records'], dict) or len(manifest['records']) != 3):
            fail('png_loader_manifest')
        # Preflight all sizes/path/OID membership and the entire append-only
        # delta BEFORE any of the six note/image bodies is requested.
        selected = {(anchor['asset_commit'], evidence.MANIFEST_PATH): evidence.MAX_MANIFEST}
        exact = {evidence.MANIFEST_PATH: anchor['manifest_blob']}; assets = set(); notes = set(); total = 0
        for ident, row in manifest['records'].items():
            evidence.p.note_id(ident); evidence.keys(row, ('source', 'reference_sha256', 'asset'))
            evidence.keys(row['source'], ('path', 'blob', 'sha256'))
            evidence.keys(row['asset'], ('path', 'blob', 'sha256', 'bytes', 'mime', 'width', 'height'))
            src = row['source']; asset = row['asset']; path = evidence.p.safe_path(src['path'])
            for item in (src, asset):
                evidence.hash_value(item['blob'], 40); evidence.hash_value(item['sha256'], 64)
            evidence.hash_value(row['reference_sha256'], 64)
            if (not path.lower().endswith(('.md', '.markdown')) or path in notes
                    or source.get(path) != ('100644', 'blob', src['blob']) or before.get(path) != source.get(path)
                    or ident not in publication.get('changed_content_ids', [])
                    or ident in publication.get('records', {}) and publication['records'][ident] != src):
                fail('png_loader_manifest')
            if (asset['path'] != evidence.PREFIX + asset['sha256'] + '.png' or asset['path'] in assets
                    or type(asset['bytes']) is not int or not 45 <= asset['bytes'] <= evidence.MAX_PNG
                    or asset['mime'] != 'image/png'
                    or after.get(asset['path']) != ('100644', 'blob', asset['blob'])):
                fail('png_loader_manifest')
            total += asset['bytes']; notes.add(path); assets.add(asset['path'])
            exact.update({path: src['blob'], asset['path']: asset['blob']})
            selected[(anchor['publication_commit'], path)] = evidence.p.MAX_NOTE
            selected[(anchor['asset_commit'], asset['path'])] = asset['bytes']
        changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
        if (total > evidence.MAX_TOTAL or changed != assets | {evidence.MANIFEST_PATH}
                or any(p in before for p in changed)
                or self.loader.reader.source_get_tree(parent) != self.loader.reader.source_get_tree(anchor['asset_commit'])):
            fail('png_loader_delta')
        self._exact = exact
        self._get_tree = self.loader.reader.source_get_tree(head)
        self._checked_head(head)
        for (commit, path), cap in selected.items(): self._blob(commit, path, cap)
        self.evidence = evidence.AssetEvidence(self.loader.reader, publication, anchor, expected_anchor_sha, head)
        self.fresh_guard(head)

    def _tree(self, commit):
        self.loader.commit(commit)
        # Existing read-only validator rehashes commit and every tree, never
        # inherited blobs. A cached tree cannot hide local object corruption.
        return journal.RemoteJournal._local_tree(SimpleNamespace(local=self.loader.reader.local), commit)

    def _blob(self, commit, path, cap):
        row = self.loader.reader.tree(commit).get(path)
        if row is None or row[:2] != ('100644', 'blob'): fail('png_loader_blob')
        probe = self.loader.reader.local.command('cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)', data=(row[2]+'\n').encode())
        if probe.returncode or probe.stderr: fail('png_loader_blob')
        return self.loader.blob(commit, path, cap)

    def _checked_head(self, head):
        _, tree = self._tree(head)
        if self.loader.reader.source_get_tree(head) != self._get_tree:
            fail('png_loader_source_changed')
        if any(tree.get(path) != ('100644', 'blob', oid) for path, oid in self._exact.items()):
            fail('png_loader_source_changed')

    def fresh_guard(self, expected_head=None):
        """Call immediately before EACH external write, including upload POSTs.

        Returns the freshly checked remote head. It is not a lease across the
        following external API call. Never cache this result or update local
        main from it; Resolver.current_commit uses the fixed snapshot_head.
        """
        if layout(self.local) != self._layout: fail('png_loader_local_changed')
        head = m.guard(self.remote)
        if expected_head is not None and head != expected_head: fail('png_loader_source_changed')
        self.loader.ancestry(self._last_verified_head, head)
        self.loader.ancestry(self.anchor['asset_commit'], head)
        self._checked_head(head)
        # Close the metadata-fetch window with a second head read. Protected
        # refs and repository metadata were freshly checked by guard above.
        if self.remote.ref(m.r.MAIN_REF) != head: fail('png_loader_source_changed')
        if layout(self.local) != self._layout: fail('png_loader_local_changed')
        self._last_verified_head = head
        return head

    def before_write(self):
        """Literal-True callback for the upload transport's final POST gate."""
        self.fresh_guard()
        return True


def load(local, remote, publication_descriptor, anchor, expected_anchor_sha, policy_hash):
    return LoadedAssets(local, remote, publication_descriptor, anchor, expected_anchor_sha, policy_hash)
