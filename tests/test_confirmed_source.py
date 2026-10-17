from copy import deepcopy
from pathlib import Path
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_confirmed_source as source
import get_notion_outbox as outbox
import get_recurring_entry as entry

POLICY = 'a' * 64
N = lambda x: str(1000000000000000000 + x)


def md(ident, body):
    return ('---\nnote_id: ' + json.dumps(ident) + '\ntitle: "Synthetic"\n---\n' + body + '\n').encode()


class Remote:
    def __init__(self, repo):
        self.repo = entry.a.OfflineGit(repo); self.calls = []; self.metadata = []
        self.mutate = None

    def request(self, path, **options):
        self.calls.append(path)
        if not path.startswith('/git/blobs/'):
            raise AssertionError('Only exact selected blobs may be requested')
        oid = path.removeprefix('/git/blobs/')
        data = self.repo.local.blob(oid)
        value = {'sha': oid, 'size': len(data), 'encoding': 'base64', 'content': base64.b64encode(data).decode()}
        return self.mutate(value) if self.mutate else value

    def check_source_budget(self, commit):
        self.metadata.append(commit)
        return {path: (*row, int(self.repo.local.run('cat-file', '-s', row[2])))
                for path, row in self.repo.tree(commit).items() if path.startswith(entry.r.GET + '/')}


class Tests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name); self.repo = self.root / 'repo'; self.repo.mkdir()
        self.git('init', '-q', '-b', 'main'); self.git('config', 'user.name', 'Synthetic'); self.git('config', 'user.email', 'synthetic@example.invalid')
        self.state = entry.r.GET + '/' + entry.a.STATE
        self.write(self.state, b'{}'); self.write(entry.r.GET + '/assets/opaque.bin', b'inherited asset')
        self.origin = {'commit': self.commit(), 'receipt_sha256': None, 'sequence': 0}; self.tip = self.origin; self.rows = []
        self.remote = Remote(self.repo)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True).stdout

    def write(self, path, data):
        target = self.repo / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)

    def commit(self):
        self.git('add', '.'); self.git('commit', '-qm', 'synthetic')
        return self.git('rev-parse', 'HEAD').decode().strip()

    def publication(self, changes, *, mutate=None):
        parent = self.git('rev-parse', 'HEAD').decode().strip(); reader = entry.a.OfflineGit(self.repo)
        for ident, body in changes.items():
            self.write(entry.r.GET + '/notes/' + ident + '.md', md(ident, body))
        self.write(self.state, json.dumps({'n': self.tip['sequence'] + 1}).encode()); self.git('add', '.')
        tree = self.git('write-tree').decode().strip(); draft = self.git('commit-tree', tree, '-p', parent, '-m', 'draft').decode().strip()
        counts = {k: 0 for k in entry.engine.COUNTS if k not in ('changed_paths', 'notes_changed', 'changed_bytes', 'transaction_bytes')}
        receipt = {'contract': entry.RECEIPT_CONTRACT, 'policy_sha256': POLICY, 'sequence': self.tip['sequence'] + 1,
            'parent_commit': parent, 'previous_receipt_commit': self.tip['commit'], 'previous_receipt_sha256': self.tip['receipt_sha256'],
            'before_get_tree': reader.source_get_tree(parent), 'previous_legacy_sha256': 'b' * 64,
            'previous_registry_sha256': None if self.tip['sequence'] == 0 else 'c' * 64, 'state_sha256': 'b' * 64, 'registry_sha256': 'c' * 64,
            'delta_sha256': entry.a.sha(entry.a.encoded(entry.delta(reader, parent, draft))), 'engine_payload_sha256': 'd' * 64,
            'source_summary_sha256': 'e' * 64, 'detail_receipts_sha256': 'f' * 64, 'changed_content_ids': sorted(changes), 'counts': counts, 'conflict_reason_counts': {}}
        if mutate: mutate(receipt)
        raw = entry.a.encoded(receipt); self.write(entry.FULL_RECEIPT, raw); commit = self.commit()
        row = {'commit': commit, 'parent': parent, 'receipt_sha256': entry.a.sha(raw)}; self.rows.append(row)
        self.tip = {'commit': commit, 'receipt_sha256': row['receipt_sha256'], 'sequence': receipt['sequence']}
        return row

    def clone(self, *, shallow=False):
        target = self.root / ('clone-' + str(len(list(self.root.glob('clone-*')))))
        self.git('config', 'uploadpack.allowFilter', 'true')
        args = ['git', 'clone', '-q', '--filter=blob:none', '--no-checkout']
        if shallow: args += ['--depth=1']
        subprocess.run(args + ['file://' + str(self.repo), str(target)], check=True, capture_output=True)
        return entry.r.LocalGit(target)

    def load(self, local=None, after=None, **kw):
        local = local or self.clone()
        chain = source.load_confirmed_chain(local, self.remote, None, self.root, kw.get('tip', self.tip), self.origin, after or self.origin, kw.get('policy', POLICY))
        return local, chain

    def ingest(self, local, chain, state=None):
        state = state or outbox.initial_state()
        return outbox.enqueue(local.vault, state, outbox.digest(state), chain, outbox.digest(chain))

    def test_exact_first_batch_then_noop_and_state_only(self):
        self.publication({N(i): 'body' for i in range(10)}); self.publication({})
        local, chain = self.load(); value = self.ingest(local, chain)
        self.assertEqual(len(value['pending_by_id']), 10); self.assertEqual(value['last_enqueued_publication'], self.tip)
        _, noop = self.load(local, after=self.tip); self.assertEqual(noop['publications'], [])
        self.assertEqual(self.ingest(local, noop, value), value)
        pub = outbox.QueuePublication(local.vault, value, outbox.digest(value))
        self.assertEqual(len(pub.descriptor['records']), 10)

    def test_only_receipts_and_actual_changed_md_fetched_no_assets(self):
        self.publication({N(1): 'one'}); self.publication({N(1): 'new', N(2): 'two'})
        local, chain = self.load()
        reader = entry.a.OfflineGit(local.vault)
        asset = reader.tree(self.tip['commit'])[entry.r.GET + '/assets/opaque.bin'][2]
        self.assertTrue(reader.local.run('cat-file', '--batch-check', data=(asset + '\n').encode()).endswith(b' missing\n'))
        expected = set()
        for row in self.rows:
            expected.add(reader.tree(row['commit'])[entry.FULL_RECEIPT][2])
            expected.update(item['after'][2] for item in entry.delta(reader, row['parent'], row['commit']) if item['path'].endswith('.md'))
        self.assertEqual(set(self.remote.calls), {'/git/blobs/' + oid for oid in expected})
        calls = self.remote.calls[:]; self.load(local); self.assertEqual(self.remote.calls, calls)

    def test_event_prefix_preserves_tip_then_resumes(self):
        for i in range(3): self.publication({N(i): 'body'})
        with patch.object(source, 'MAX_EVENTS', 2): local, chain = self.load()
        self.assertEqual(chain['publications'], self.rows[:2]); self.assertEqual(chain['tip'], self.tip)
        value = self.ingest(local, chain)
        with self.assertRaisesRegex(outbox.Blocked, 'outbox_chain_incomplete'): outbox.QueuePublication(local.vault, value, outbox.digest(value))
        _, next_chain = self.load(local, after=value['last_enqueued_publication'])
        self.assertEqual(next_chain['publications'], self.rows[2:]); done = self.ingest(local, next_chain, value)
        self.assertEqual(len(done['pending_by_id']), 3)

    def test_byte_prefix_checks_metadata_before_fetch(self):
        self.publication({N(1): 'a' * 30}); self.publication({N(2): 'b' * 90})
        cap = len(md(N(1), 'a' * 30)) + 1
        with patch.object(source, 'MAX_CONTENT_BYTES', cap): local, chain = self.load()
        self.assertEqual(chain['publications'], self.rows[:1])
        oid = entry.a.OfflineGit(local.vault).tree(self.tip['commit'])[entry.r.GET + '/notes/' + N(2) + '.md'][2]
        self.assertNotIn('/git/blobs/' + oid, self.remote.calls)

    def test_publication_prefix_bound(self):
        for _ in range(3): self.publication({})
        with patch.object(outbox, 'MAX_CHAIN', 1): _, chain = self.load()
        self.assertEqual(chain['publications'], self.rows[:1])

    def test_foreign_edit_revert_between_receipts_rejected_before_md(self):
        self.publication({N(1): 'one'}); path = entry.r.GET + '/notes/' + N(1) + '.md'; old = (self.repo / path).read_bytes()
        self.write(path, b'foreign'); self.commit(); self.write(path, old); self.commit(); self.publication({N(2): 'two'})
        with self.assertRaisesRegex(source.SourceFailure, 'source_foreign_get_change'): self.load()
        self.assertEqual(len(self.remote.calls), 2)  # receipts only

    def test_foreign_state_after_tip_rejected(self):
        self.publication({N(1): 'one'}); self.write(entry.r.GET + '/03 State/foreign.json', b'{}'); self.commit()
        with self.assertRaisesRegex(source.SourceFailure, 'source_foreign_get_change'): self.load()
        self.assertEqual(self.remote.calls, [])

    def test_unrelated_journal_commit_is_preserved_without_ref_write(self):
        self.publication({N(1): 'one'}); self.write('.sync/get-notion/state.json', b'{}'); head = self.commit()
        local, chain = self.load(); self.ingest(local, chain)
        self.assertEqual(local.run('rev-parse', 'refs/heads/main').decode().strip(), head)
        self.assertEqual(local.run('rev-parse', 'HEAD').decode().strip(), head)

    def test_wrong_receipt_hash_policy_sequence_parent_or_delta_rejects(self):
        self.publication({N(1): 'one'})
        for changed in ({'receipt_sha256': 'f' * 64}, {'sequence': 2}):
            with self.assertRaises(Exception): self.load(tip=dict(self.tip, **changed))
        with self.assertRaises(Exception): self.load(policy='f' * 64)
        for field in ('parent_commit', 'delta_sha256', 'previous_legacy_sha256'):
            self.publication({}, mutate=lambda v, k=field: v.update({k: '1' * (40 if k == 'parent_commit' else 64)}))
            with self.assertRaises(Exception): self.load()

    def test_missing_chain_link_rejects(self):
        self.publication({N(1): 'one'}); self.publication({}, mutate=lambda v: v.update(previous_receipt_commit=self.origin['commit']))
        with self.assertRaises(Exception): self.load()

    def test_forged_md_ids_fail_when_materialized(self):
        self.publication({N(1): 'one'}, mutate=lambda v: v.update(changed_content_ids=[N(2)]))
        with self.assertRaises(Exception): self.load()

    def test_asset_delta_or_foreign_non_get_delta_rejects(self):
        for path in (entry.r.GET + '/assets/new.bin', 'unrelated.txt'):
            self.write(path, b'foreign'); self.publication({})
            with self.assertRaisesRegex(source.SourceFailure, 'source_foreign_get_change'): self.load()

    def test_receipt_budget_hard_stop_not_partial_trust(self):
        self.publication({}); self.publication({})
        with patch.object(source, 'MAX_RECEIPT_BYTES', 1):
            with self.assertRaisesRegex(source.SourceFailure, 'source_chain_budget'): self.load()

    def test_corrupt_remote_blob_is_not_imported(self):
        self.publication({N(1): 'one'}); self.remote.mutate = lambda row: dict(row, content=base64.b64encode(b'bad').decode())
        local = self.clone()
        with self.assertRaisesRegex(source.SourceFailure, 'source_blob_invalid'): self.load(local)
        receipt_oid = entry.a.OfflineGit(local.vault).tree(self.tip['commit'])[entry.FULL_RECEIPT][2]
        self.assertTrue(entry.a.OfflineGit(local.vault).local.run('cat-file', '--batch-check', data=(receipt_oid + '\n').encode()).endswith(b' missing\n'))

    def test_shallow_partial_clone_loads_only_metadata_and_selected_blobs(self):
        self.publication({N(1): 'one'}); self.publication({N(2): 'two'})
        local, chain = self.load(self.clone(shallow=True)); self.assertEqual(chain['publications'], self.rows)
        self.ingest(local, chain)

    def test_pending_original_publication_reloads_full_actual_diff_once(self):
        first = self.publication({N(1): 'one', N(2): 'two'}); self.publication({N(1): 'new'})
        local, chain = self.load(); value = self.ingest(local, chain)
        # Fresh partial clone simulates recovery on a new runner.
        fresh = self.clone(shallow=True); calls = len(self.remote.calls)
        receipt, actual = source.load_queue_publication(fresh, self.remote, None, self.root, first, POLICY)
        self.assertEqual(set(actual), {N(1), N(2)}); self.assertEqual(len(self.remote.calls) - calls, 3)
        calls = len(self.remote.calls); source.load_queue_publication(fresh, self.remote, None, self.root, first, POLICY)
        self.assertEqual(len(self.remote.calls), calls)
        value['rotation_order'] = [N(2), N(1)]
        pub = outbox.QueuePublication(fresh.vault, value, outbox.digest(value))
        self.assertEqual(pub.descriptor['commit'], first['commit']); self.assertEqual(set(pub.descriptor['records']), {N(2)})

    def test_pending_wrong_parent_or_hash_rejects(self):
        row = self.publication({N(1): 'one'}); local = self.clone()
        for key, value in (('parent', 'f' * 40), ('receipt_sha256', 'f' * 64)):
            with self.assertRaises(Exception): source.load_queue_publication(local, self.remote, None, self.root, dict(row, **{key: value}), POLICY)

    def test_merge_history_rejected(self):
        self.publication({N(1): 'one'})
        self.git('branch', 'side'); self.write('outside-a', b'a'); self.commit()
        self.git('checkout', '-q', 'side'); self.write('outside-b', b'b'); self.commit()
        self.git('checkout', '-q', 'main'); self.git('merge', '-qm', 'merge', 'side')
        self.publication({N(2): 'two'})
        with self.assertRaisesRegex(entry.EntryFailure, 'entry_non_linear_history'): self.load()

    def test_nonzero_approved_origin_and_after_empty_noop(self):
        self.publication({N(1): 'old'}); self.origin = deepcopy(self.tip)
        self.publication({N(2): 'new'})
        local, chain = self.load(); value = self.ingest(local, chain)
        self.assertEqual(set(value['pending_by_id']), {N(2)})
        calls = self.remote.calls[:]; _, empty = self.load(local, after=self.tip)
        self.assertEqual(empty['publications'], []); self.assertEqual(self.remote.calls, calls)

    def test_state_only_origin_advances_without_content_or_asset_fetch(self):
        self.publication({}); local, chain = self.load(); value = self.ingest(local, chain)
        self.assertFalse(value['pending_by_id']); self.assertEqual(value['last_enqueued_publication'], self.tip)
        self.assertEqual(len(self.remote.calls), 1)

    def test_first_publication_too_large_cannot_skip_to_smaller(self):
        self.publication({N(1): 'one', N(2): 'two'}); self.publication({})
        with patch.object(source, 'MAX_EVENTS', 1):
            with self.assertRaisesRegex(source.SourceFailure, 'source_publication_budget'): self.load()
        # No Markdown bodies were requested after chain verification.
        self.assertEqual(len(self.remote.calls), 2)

    def test_object_loader_never_uses_write_remote_or_updates_ref(self):
        self.publication({N(1): 'one'}); local = self.clone(); commands = []
        run = local.run
        def checked(*args, **kw):
            commands.append(args)
            if args[0] in ('push', 'update-ref', 'checkout', 'reset', 'commit', 'commit-tree'):
                raise AssertionError('Unexpected local or external ref mutation')
            return run(*args, **kw)
        local.run = checked
        self.load(local)
        self.assertFalse(any(c[0] == 'fetch' for c in commands))

    def test_bootstrap_no_receipt_is_empty_no_fetch(self):
        local, chain = self.load(); self.assertEqual(chain['publications'], []); self.assertEqual(self.remote.calls, [])
        self.ingest(local, chain)

if __name__ == '__main__': unittest.main()
