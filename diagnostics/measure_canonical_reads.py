"""Compare one synthetic immutable no-op fixture against unmodified source.

No network or external writes. Baseline source is loaded only for the three
changed readers; all plans run against exactly the same Git objects and source.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT / 'scripts')]
import test_recurring_entry as fixture
from get_recurring_entry import a, engine, m, n, r
import get_recurring_entry as entry


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def measure(baseline_root, extra_markdown):
    old = module(baseline_root / 'scripts/get_legacy_adapter.py', 'original_adapter')
    old_writer = module(baseline_root / 'scripts/get_canonical_writer.py', 'original_writer')
    f = fixture.RecurringEntryTests(methodName='test_noop_has_no_new_snapshot_or_receipt_or_sink_ids')
    f.setUp()
    try:
        if extra_markdown:
            values = {r.GET + '/03 纯文本记录/synthetic-' + str(i) + '.md': ('Synthetic unmanaged content ' + str(i) + '\n').encode() for i in range(extra_markdown)}
            f.parent = f.commit_change(f.parent, values)
            f.bootstrap.update(commit=f.parent, get_tree=f.reader.source_get_tree(f.parent))
        first, _ = f.prepare(); parent = first['candidate_commit']; anchor = f.anchor(first)
        original_command = r.LocalGit.command
        result = {}; outcomes = []
        for label, reader_type, evidence_type, inventory in (
                ('before', old.OfflineGit, old.CaptureEvidence, old_writer.git_inventory),
                ('after', a.OfflineGit, a.CaptureEvidence, n.git_inventory)):
            counts = Counter(); phases = {}; source = f.f.source()
            def command(self, *args, **kwargs):
                counts[args[0] + (' ' + args[1] if args[0] == 'cat-file' else '')] += 1
                return original_command(self, *args, **kwargs)
            with ExitStack() as stack:
                stack.enter_context(patch.object(r.LocalGit, 'command', command))
                stack.enter_context(patch.object(n, 'git_inventory', inventory))
                start = time.perf_counter(); reader = reader_type(f.f.f.repo)
                evidence = evidence_type(reader, f.evidence.anchor)
                phases['capture_evidence_seconds'] = time.perf_counter() - start
                phase = time.perf_counter()
                # The runtime separately loads bootstrap/current/receipt parents.
                for commit in (f.bootstrap['commit'], parent): inventory(reader, commit)
                _, _, states = entry.trust_current(reader, evidence, parent, f.rules, anchor, bootstrap=f.bootstrap)
                _, legacy, registry = states
                m.selected_baselines(reader, legacy, parent, None)
                engine.new_baselines(reader, registry, parent, None)
                phases['trusted_inputs_seconds'] = time.perf_counter() - phase
                phase = time.perf_counter()
                plan, files = entry.prepare(reader, evidence, parent, source, approved_rules_sha256=f.rules, anchor=anchor, bootstrap=f.bootstrap)
                entry.verify_plan(reader, evidence, plan, files, f.rules, bootstrap=f.bootstrap)
                handoff = entry.confirmed_private_handoff(reader, evidence, plan, files, {'main_updated': False, 'cas_attempts': 0, 'readback_verified': True}, f.rules, bootstrap=f.bootstrap)
                phases['prepare_verify_handoff_seconds'] = time.perf_counter() - phase
                assert files == {} and plan['candidate_commit'] == parent and source.ids == [] and source.lists == 1
                assert handoff['changed_content_ids'] == []
                phases['total_seconds'] = time.perf_counter() - start
            outcomes.append((plan, files, handoff))
            result[label] = {**{k: round(v, 4) for k, v in phases.items()}, 'subprocesses': sum(counts.values()), 'commands': dict(sorted(counts.items())), 'source_enumerations': source.lists, 'detail_reads': len(source.ids), 'changed_paths': len(files), 'cas_attempts': 0}
        assert outcomes[0] == outcomes[1]
        result['same_plan_files_handoff'] = True
        result['fixture_markdown_paths'] = extra_markdown + 7
        result['subprocess_reduction_percent'] = round(100 * (1 - result['after']['subprocesses'] / result['before']['subprocesses']), 2)
        result['elapsed_reduction_percent'] = round(100 * (1 - result['after']['total_seconds'] / result['before']['total_seconds']), 2)
        return result
    finally:
        f.doCleanups()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--extra-markdown', type=int, default=2130)
    args = parser.parse_args()
    print(json.dumps(measure(args.baseline_root, args.extra_markdown), indent=2))
