"""Real immutable Git objects; no credentials, network, or external writes."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_legacy_adapter as a
import get_canonical_manual as engine
from git_object_fixture import remove_objects


class CanonicalReadEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q'); self.git('config', 'user.name', 'Read test')
        self.git('config', 'user.email', 'read@example.invalid')
        self.values = {a.retention.GET + '/' + str(i) + '.md': ('Exact ' + str(i)).encode() for i in range(65)}
        self.values[a.retention.GET + '/alias.md'] = self.values[a.retention.GET + '/0.md']
        for path, data in self.values.items():
            target = self.repo / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        self.head = self.commit(); self.reader = a.OfflineGit(self.repo)
        self.limits = {p: a.owned.MAX_NOTE for p in self.values}

    def git(self, *args, data=None):
        return subprocess.run(['git', '-C', str(self.repo), *args], input=data, capture_output=True, check=True).stdout

    def commit(self):
        self.git('add', '--all'); self.git('commit', '-qm', 'Synthetic read fixture')
        return self.git('rev-parse', 'HEAD').decode().strip()

    def test_batched_read_exact_deduplicated_and_bounded_without_ref_or_index_changes(self):
        refs = self.git('show-ref'); index = self.git('ls-files', '--stage')
        original = self.reader.local.command
        with patch.object(self.reader.local, 'command', wraps=original) as command:
            self.assertEqual(self.reader.read_many(self.head, self.limits), self.values)
            batch = [c for c in command.call_args_list if c.args[:2] == ('cat-file', '--batch')]
            self.assertEqual([len(c.kwargs['data'].splitlines()) for c in batch], [64, 1])
        self.assertEqual(self.git('show-ref'), refs); self.assertEqual(self.git('ls-files', '--stage'), index)

    def test_cap_and_duplicate_oid_smaller_cap_preflight_before_content(self):
        original = self.reader.local.command
        for limits in ({**self.limits, a.retention.GET + '/alias.md': 1}, self.limits):
            with patch.object(a, 'MAX_TREE', 1 if limits == self.limits else a.MAX_TREE), patch.object(self.reader.local, 'command', wraps=original) as command:
                with self.assertRaisesRegex(a.ParityFailure, 'adapter_(blob|tree)_budget'):
                    self.reader.read_many(self.head, limits)
                self.assertFalse(any(c.args[:2] == ('cat-file', '--batch') for c in command.call_args_list))

    def test_batch_byte_budget_splits_requests_without_dropping_paths(self):
        original = self.reader.local.command
        with patch.object(a, 'MAX_STATE', 16), patch.object(self.reader.local, 'command', wraps=original) as command:
            self.assertEqual(self.reader.read_many(self.head, self.limits), self.values)
        sizes = {a.retention.oid(v).encode(): len(v) for v in self.values.values()}
        for call in command.call_args_list:
            if call.args[:2] == ('cat-file', '--batch'):
                self.assertLessEqual(sum(sizes[oid] for oid in call.kwargs['data'].splitlines()), 16)

    def test_canonical_inventory_preserves_aggregate_budget_error(self):
        with patch.object(a, 'MAX_TREE', 1), self.assertRaisesRegex(a.ParityFailure, 'canonical_budget'):
            engine.n.git_inventory(self.reader, self.head)

    def test_unknown_paths_symbolic_refs_noncommits_and_bad_modes_rejected(self):
        for commit, limits in [('HEAD', self.limits), ('f' * 40, self.limits), (a.retention.oid(next(iter(self.values.values()))), self.limits), (self.head, {'absent.md': 10})]:
            with self.assertRaises((a.ParityFailure, a.retention.RetentionFailure)):
                self.reader.read_many(commit, limits)
        target = self.repo / a.retention.GET / 'link.md'; target.symlink_to('0.md'); head = self.commit()
        with self.assertRaisesRegex(a.ParityFailure, 'adapter_missing_blob'):
            self.reader.read_many(head, {a.retention.GET + '/link.md': 10})

    def test_changed_commit_never_reuses_old_path_bytes(self):
        self.assertEqual(self.reader.read_many(self.head, self.limits), self.values)
        path = next(iter(self.values)); (self.repo / path).write_bytes(b'Changed'); head = self.commit()
        self.assertEqual(self.reader.read_many(head, self.limits), {**self.values, path: b'Changed'})
        self.assertEqual(self.reader.read_many(self.head, self.limits), self.values)

    def test_success_does_not_cache_blob_bytes_or_missing_objects(self):
        self.reader.read_many(self.head, self.limits)
        blob = a.retention.oid(next(iter(self.values.values())))
        obj, original = remove_objects(self.repo, blob)[blob]
        with self.assertRaisesRegex(a.ParityFailure, 'adapter_blob_readback'):
            self.reader.read_many(self.head, self.limits)
        obj.write_bytes(original)
        self.assertEqual(self.reader.read_many(self.head, self.limits), self.values)

    def test_malformed_preflight_and_content_fail_closed(self):
        original = self.reader.local.command
        self.reader.tree(self.head)
        for stage in ('probe', 'content'):
            for mutation in ('truncated', 'extra', 'wrong_oid', 'wrong_type', 'wrong_size', 'wrong_body', 'stderr', 'nonzero'):
                def corrupt(*args, **kwargs):
                    result = original(*args, **kwargs)
                    selected = args[0] == 'cat-file' and (args[1].startswith('--batch-check') if stage == 'probe' else args[1] == '--batch')
                    if not selected: return result
                    data = result.stdout
                    if mutation == 'truncated': data = data[:-4]
                    elif mutation == 'extra': data += b'extra\n'
                    elif mutation == 'wrong_oid': data = b'f' * 40 + data[40:]
                    elif mutation == 'wrong_type': data = data.replace(b' blob ', b' tree ', 1)
                    elif mutation == 'wrong_size': data = data.replace(b' blob 7\n', b' blob 8\n', 1)
                    elif mutation == 'wrong_body': data = (data.replace(b'Exact 0', b'Wrong 0', 1) if stage == 'content' else data.replace(b' blob ', b' missing ', 1))
                    return SimpleNamespace(stdout=data, stderr=b'warning' if mutation == 'stderr' else b'', returncode=1 if mutation == 'nonzero' else 0)
                with self.subTest(stage=stage, mutation=mutation), patch.object(self.reader.local, 'command', side_effect=corrupt):
                    with self.assertRaises(a.ParityFailure): self.reader.read_many(self.head, self.limits)

    def test_interrupt_after_partial_batch_does_not_publish_or_poison_retry(self):
        original = self.reader.local.command; batches = 0
        def interrupted(*args, **kwargs):
            nonlocal batches
            if args[:2] == ('cat-file', '--batch'):
                batches += 1
                if batches == 2: raise KeyboardInterrupt()
            return original(*args, **kwargs)
        refs = self.git('show-ref')
        with patch.object(self.reader.local, 'command', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.reader.read_many(self.head, self.limits)
        self.assertEqual(self.git('show-ref'), refs)
        self.assertEqual(self.reader.read_many(self.head, self.limits), self.values)

    def test_pinned_parent_baseline_skips_history_but_keeps_hash_and_path_checks(self):
        path = next(iter(self.values)); value = self.values[path]; blob = a.retention.oid(value)
        original = self.reader.local.command
        with patch.object(self.reader.local, 'command', wraps=original) as command:
            self.assertEqual(self.reader.baseline(self.head, path[len(a.retention.GET) + 1:], blob, a.sha(value)), value)
        self.assertFalse(any(c.args[0] == 'rev-list' for c in command.call_args_list))
        with self.assertRaisesRegex(a.ParityFailure, 'adapter_baseline_hash'):
            self.reader.baseline(self.head, path[len(a.retention.GET) + 1:], blob, 'f' * 64)
        with self.assertRaisesRegex(a.ParityFailure, 'baseline_missing'):
            self.reader.baseline(self.head, 'unknown.md', blob, a.sha(value))
        (self.repo / path).write_bytes(b'Later'); head = self.commit()
        with patch.object(self.reader.local, 'command', wraps=original) as command:
            self.assertEqual(self.reader.baseline(head, path[len(a.retention.GET) + 1:], blob, a.sha(value)), value)
        self.assertTrue(any(c.args[:2] == ('rev-list', '--max-count=256') for c in command.call_args_list))

    def test_current_loader_batches_missing_probe_and_fetches_only_missing_oid(self):
        metadata = {p: ('100644', 'blob', a.retention.oid(v), len(v)) for p, v in self.values.items()}
        remote = SimpleNamespace(check_source_budget=lambda commit: metadata)
        original = self.reader.local.command
        with patch.object(engine.m, 'fetch_selected', return_value={}) as fetch, patch.object(self.reader.local, 'command', wraps=original) as command:
            current = engine.load_current(self.reader, remote, 'unused', self.repo, self.head)
        self.assertEqual(current, {p[len(a.retention.GET) + 1:]: v for p, v in self.values.items()})
        self.assertEqual(fetch.call_args.args[1], {})
        probes = [c for c in command.call_args_list if c.args[0] == 'cat-file' and c.args[1].startswith('--batch-check')]
        self.assertEqual(len(probes), 2)  # One availability check, one bounded content preflight.
        blob = a.retention.oid(next(iter(self.values.values())))
        obj, data = remove_objects(self.repo, blob)[blob]
        missing = {p: row for p, row in metadata.items() if row[2] == blob}
        with patch.object(engine.m, 'fetch_selected', return_value={p: self.values[p] for p in missing}) as fetch:
            engine.load_current(self.reader, remote, 'unused', self.repo, self.head)
        self.assertEqual(fetch.call_args.args[1], missing)

    def test_current_loader_unknown_probe_stops_before_fetch(self):
        metadata = {p: ('100644', 'blob', a.retention.oid(v), len(v)) for p, v in self.values.items()}
        remote = SimpleNamespace(check_source_budget=lambda commit: metadata)
        with patch.object(self.reader.local, 'command', return_value=SimpleNamespace(stdout=b'unknown\\n', stderr=b'', returncode=0)), patch.object(engine.m, 'fetch_selected') as fetch:
            with self.assertRaisesRegex(engine.m.ManualFailure, 'manual_readback'):
                engine.load_current(self.reader, remote, 'unused', self.repo, self.head)
        fetch.assert_not_called()


if __name__ == '__main__': unittest.main()
