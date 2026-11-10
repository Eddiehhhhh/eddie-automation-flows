"""Only the exact reviewed read-only probes may be excluded from recovery."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


CHECK = r'''
import copy, sys, types
bindings = types.ModuleType('get_private_runtime_bindings')
bindings.value = lambda module, key: {}
sys.modules['get_private_runtime_bindings'] = bindings
import get_notion_journal_runtime as m
policy = {'repository_id': 1270807355, 'workflow_id': 387242896,
          'workflow_path': '.github/workflows/get-canonical.yml', 'head_branch': 'main'}
repo = {'id': 1270807355, 'full_name': 'Eddiehhhhh/eddie-automation-flows', 'private': False}
for ident, sha, conclusion in ((42275323017, 'eabb1ed873d4a38e492a8295f40ec2d963bb8c58', 'success'),
                               (42274751706, '970b47ebef92c422bc5449063081b0addf8cb7d4', 'failure')):
    run = {'id': ident, 'run_attempt': 1, 'repository': repo,
           'head_repository': copy.deepcopy(repo), 'workflow_id': 387242896,
           'path': policy['workflow_path'], 'head_branch': m.READ_ONLY_PROBE_BRANCH,
           'head_sha': sha, 'event': 'workflow_dispatch', 'status': 'completed',
           'conclusion': conclusion, 'created_at': '2026-11-07T00:00:00Z',
           'updated_at': '2026-11-07T00:01:00Z'}
    assert m._known_read_only_probe(run, policy)
    for key, value in (('workflow_id', 1), ('path', 'wrong'), ('head_branch', 'main'),
                       ('head_sha', '0'*40), ('event', 'push'), ('status', 'queued'),
                       ('conclusion', 'cancelled'), ('run_attempt', 2)):
        changed = copy.deepcopy(run); changed[key] = value
        try: m._known_read_only_probe(changed, policy)
        except m.CheckpointFailure: pass
        else: raise AssertionError(f'{key} must fail closed')
    for key in ('repository', 'head_repository'):
        changed = copy.deepcopy(run); changed[key] = {**repo, 'id': 1}
        try: m._known_read_only_probe(changed, policy)
        except m.CheckpointFailure: pass
        else: raise AssertionError(f'{key} must fail closed')
other = copy.deepcopy(run); other['id'] += 1
assert not m._known_read_only_probe(other, policy)
try: m._latest_run(other, policy)
except m.CheckpointFailure: pass
else: raise AssertionError('unregistered branch run must fail closed')
import get_notion_idle_rotation as rotation
import get_notion_runtime as runtime
import get_private_runtime_report as report
for error, expected in ((rotation.RotationFailure('rotation_policy_changed'), 'rotation_policy_changed'),
                        (rotation.RotationFailure('private note title'), 'journal_provenance_failed'),
                        (ValueError('private note title'), 'journal_provenance_failed')):
    try: m.fail_unexpected_provenance(error)
    except m.CheckpointFailure as caught: assert str(caught) == expected
    else: raise AssertionError('provenance failures must remain fail closed')
assert report.public({'status':'failed','reason':'rotation_policy_changed'},
                     runtime.DIAGNOSTIC_ERRORS)['reason'] == 'rotation_policy_changed'
'''


class ReadOnlyProbeRuns(unittest.TestCase):
    def test_exact_reviewed_runs_are_excluded_and_all_others_fail_closed(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        env = dict(os.environ, PYTHONPATH=str(scripts))
        result = subprocess.run([sys.executable, '-c', CHECK], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
