import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_pending_gate as gate


class API:
    def __init__(self):
        self.anchor = 'a' * 40; self.refs = []; self.bad_compare = set()
    def call(self, method, path, missing=False):
        if path.startswith('/git/ref/'):
            return None if self.anchor is None else {'ref': gate.REF, 'object': {'type': 'commit', 'sha': self.anchor}}
        older, newer = path.removeprefix('/compare/').split('...')
        return {'status': 'diverged' if older in self.bad_compare else 'ahead',
                'merge_base_commit': {'sha': older}}
    def list_stage_refs(self): return self.refs


class Client:
    def __init__(self): self.runs = {}
    def json(self, path): return self.runs[int(path.rsplit('/', 1)[1])]


class PendingGate(unittest.TestCase):
    def setUp(self):
        self.api = API(); self.client = Client(); self.policy = {'repository_id': 7, 'workflow_id': 8}
        self.parent = 'b' * 40
        self.verify = patch.object(gate.provenance, 'check_run_identity', return_value=None)
        self.verify.start(); self.addCleanup(self.verify.stop)
    def ref(self, run_id, sha, workflow=8):
        self.api.refs.append({'ref': gate.PREFIX + str(run_id), 'object': {'type': 'commit', 'sha': sha}})
        repo = {'id': 7, 'full_name': gate.provenance.REPOSITORY, 'private': False}
        self.client.runs[run_id] = {'id': run_id, 'repository': dict(repo), 'head_repository': dict(repo), 'workflow_id': workflow}
    def test_published_anchor_and_prior_official_refs_continue(self):
        self.ref(1, 'c' * 40); self.ref(2, self.api.anchor)
        self.assertEqual(gate.check(self.api, self.client, self.policy, self.parent), self.api.anchor)
    def test_noncanonical_workflow_with_official_stage_prefix_stops(self):
        self.ref(3, 'd' * 40, workflow=99)
        with self.assertRaisesRegex(ValueError, 'pending_unresolved'):
            gate.check(self.api, self.client, self.policy, self.parent)
    def test_unpublished_official_ref_or_unpublished_fixed_anchor_stops(self):
        self.ref(1, 'c' * 40); self.api.bad_compare.add('c' * 40)
        with self.assertRaisesRegex(ValueError, 'pending_unresolved'):
            gate.check(self.api, self.client, self.policy, self.parent)
        self.api.refs.clear(); self.api.bad_compare = {self.api.anchor}
        with self.assertRaisesRegex(ValueError, 'pending_unresolved'):
            gate.check(self.api, self.client, self.policy, self.parent)
    def test_missing_anchor_and_unverifiable_run_stop(self):
        self.api.anchor = None
        with self.assertRaisesRegex(ValueError, 'pending_unresolved'):
            gate.check(self.api, self.client, self.policy, self.parent)
        self.api.anchor = 'a' * 40; self.ref(1, 'c' * 40); self.client.runs[1]['repository']['id'] = 9
        with self.assertRaisesRegex(ValueError, 'pending_unresolved'):
            gate.check(self.api, self.client, self.policy, self.parent)


if __name__ == '__main__': unittest.main()
