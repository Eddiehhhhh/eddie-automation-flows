"""Verified immutable controller-byte cache; all mutable reads stay physical."""
import base64
from copy import deepcopy
import gc
from pathlib import Path
import sys
import unittest
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_recurring_controller as c
import get_publication_provenance as p
import get_mirror_incremental_replay as mirror
from get_recurring_entry import a, r
import test_publication_provenance as fixture


def content(policy, raw):
    return {'type': 'file', 'path': policy['workflow_path'], 'encoding': 'base64',
            'size': len(raw), 'sha': r.oid(raw), 'content': base64.b64encode(raw).decode()}


class Opener:
    """Only synthetic HTTP responses; never performs a network request."""
    def __init__(self, api):
        self.api = api
        self.calls = []
        self.statuses = []

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        if request.method != 'GET':
            raise AssertionError('read-only transport required')
        if self.statuses:
            return fixture.Response(self.statuses.pop(0))
        if url.startswith('https://fixture.blob.core.windows.net/'):
            if request.has_header('Authorization'):
                raise AssertionError('credential forwarded to CDN')
            return fixture.Response(200, self.api.logs(int(url.rsplit('/', 1)[1])))
        if not (url == p.API or url.startswith(p.API + '/')):
            raise AssertionError('unexpected URL')
        path = url[len(p.API):]
        if path.startswith('/actions/jobs/') and path.endswith('/logs'):
            return fixture.Response(302, headers={
                'Location': 'https://fixture.blob.core.windows.net/' + path.split('/')[3]})
        return fixture.Response(200, a.encoded(self.api.json(path)))


class ControllerReadBudgetTests(unittest.TestCase):
    def client(self, api=None, *, anonymous=False, client_type=None):
        api = api or fixture.FakeAPI()
        opener = Opener(api)
        kwargs = {'opener': opener, 'clock': lambda: 0, 'sleep': lambda _: None}
        if client_type is not None:
            client = client_type('synthetic-noncredential', **kwargs)
        elif anonymous:
            client = p.AnonymousGithub(**kwargs)
        else:
            client = p.PublicGithub('synthetic-noncredential', **kwargs)
        return api, opener, client

    def test_built_in_clients_share_verified_bytes_only_within_their_own_lifetime(self):
        for anonymous in (False, True):
            api, opener, client = self.client(anonymous=anonymous)
            for _ in range(3):
                self.assertEqual(c.verify_remote(client, 'c' * 40, api.policy), 'c' * 40)
            self.assertEqual(client.requests, 1)
            self.assertEqual(len(opener.calls), 1)
            _, _, second = self.client(api, anonymous=anonymous)
            c.verify_remote(second, 'c' * 40, api.policy)
            self.assertEqual(second.requests, 1)
            self.assertEqual(len(api.called), 2)

    def test_each_hit_recompares_exact_policy_not_just_sha_and_path(self):
        api, _, client = self.client()
        c.verify_remote(client, 'c' * 40, api.policy)
        for key, value in (('code_sha', 'f' * 40), ('rules_sha256', 'f' * 64),
                           ('get_policy_sha256', 'f' * 64)):
            changed = {**api.policy, key: value}
            with self.subTest(key=key), self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_mismatch'):
                c.verify_remote(client, 'c' * 40, changed)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_policy'):
            c.verify_remote(client, 'c' * 40, {**api.policy, 'controller_template_sha256': 'f' * 64})
        self.assertEqual(client.requests, 1)
        self.assertEqual(c.verify_remote(client, 'c' * 40, api.policy), 'c' * 40)

    def test_cached_manual_only_controller_never_authorizes_schedule(self):
        api, _, client = self.client()
        path = '/contents/' + api.policy['workflow_path'] + '?ref=' + 'c' * 40
        api.overrides[path] = content(api.policy, c.expected_bytes(api.policy, scheduled=False))
        c.verify_remote(client, 'c' * 40, api.policy, event='workflow_dispatch')
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_mismatch'):
            c.verify_remote(client, 'c' * 40, api.policy, event='schedule')
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller'):
            c.verify_remote(client, 'c' * 40, api.policy, event='pull_request')
        self.assertEqual(client.requests, 1)
        c.verify_remote(client, 'c' * 40, api.policy, event='workflow_dispatch')

    def test_cached_scheduled_controller_is_still_valid_for_dispatch(self):
        api, _, client = self.client()
        c.verify_remote(client, 'c' * 40, api.policy)
        c.verify_remote(client, 'c' * 40, api.policy, event='workflow_dispatch')
        self.assertEqual(client.requests, 1)

    def test_cache_key_includes_full_commit_and_workflow_path(self):
        api, _, client = self.client()
        first = 'c' * 40
        second = 'c' * 39 + 'd'
        c.verify_remote(client, first, api.policy)
        c.verify_remote(client, second, api.policy)
        other = {**api.policy, 'workflow_path': '.github/workflows/other.yml'}
        api.overrides['/contents/' + other['workflow_path'] + '?ref=' + first] = content(other, c.expected_bytes(other))
        c.verify_remote(client, first, other)
        self.assertEqual(client.requests, 3)
        for policy, sha in ((api.policy, first), (api.policy, second), (other, first)):
            c.verify_remote(client, sha, policy)
        self.assertEqual(client.requests, 3)

    def test_short_sha_fails_before_read_or_cache_lookup(self):
        api, _, client = self.client()
        with self.assertRaises(a.ParityFailure):
            c.verify_remote(client, 'c' * 7, api.policy)
        self.assertEqual(client.requests, 0)

    def test_shape_size_encoding_and_blob_failures_never_populate_cache(self):
        policy = fixture.policy()
        valid = content(policy, c.expected_bytes(policy))
        malformed = (
            None, [], {**valid, 'type': 'symlink'}, {**valid, 'path': 'other'},
            {**valid, 'encoding': 'raw'}, {**valid, 'size': True},
            {**valid, 'size': -1}, {**valid, 'size': c.MAX_CONTROLLER + 1},
            {**valid, 'size': valid['size'] + 1}, {**valid, 'sha': 'f' * 40},
            {**valid, 'content': 'not-base64!'}, {**valid, 'content': None})
        for value in malformed:
            api, _, client = self.client()
            path = '/contents/' + policy['workflow_path'] + '?ref=' + 'c' * 40
            api.overrides[path] = value
            failure = mirror.ParityFailure if value is None or isinstance(value, list) else p.ProvenanceFailure
            with self.subTest(value=value), self.assertRaises(failure):
                c.verify_remote(client, 'c' * 40, policy)
            self.assertNotIn(client, c._VERIFIED_CONTROLLER_BYTES)
            api.overrides[path] = deepcopy(valid)
            c.verify_remote(client, 'c' * 40, policy)
            self.assertEqual(client.requests, 2)

    def test_self_consistent_but_wrong_render_is_not_cached(self):
        api, _, client = self.client()
        path = '/contents/' + api.policy['workflow_path'] + '?ref=' + 'c' * 40
        api.overrides[path] = content(api.policy, c.expected_bytes(api.policy) + b'\n')
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_controller_mismatch'):
            c.verify_remote(client, 'c' * 40, api.policy)
        self.assertNotIn(client, c._VERIFIED_CONTROLLER_BYTES)
        del api.overrides[path]
        c.verify_remote(client, 'c' * 40, api.policy)
        self.assertEqual(client.requests, 2)

    def test_denied_reads_are_not_cached_or_retried_by_cache(self):
        api, opener, client = self.client()
        opener.statuses.append(403)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_unavailable'):
            c.verify_remote(client, 'c' * 40, api.policy)
        self.assertEqual(client.requests, 1)
        self.assertNotIn(client, c._VERIFIED_CONTROLLER_BYTES)
        c.verify_remote(client, 'c' * 40, api.policy)
        self.assertEqual(client.requests, 2)

    def test_custom_clients_and_subclasses_keep_fresh_fixture_reads(self):
        api = fixture.FakeAPI()
        for _ in range(2):
            c.verify_remote(api, 'c' * 40, api.policy)
        self.assertEqual(len(api.called), 2)
        class CustomPublic(p.PublicGithub):
            pass
        api, _, client = self.client(client_type=CustomPublic)
        for _ in range(2):
            c.verify_remote(client, 'c' * 40, api.policy)
        self.assertEqual(client.requests, 2)
        self.assertNotIn(client, c._VERIFIED_CONTROLLER_BYTES)

    def test_cache_does_not_keep_transport_or_credentials_alive(self):
        api, _, client = self.client()
        c.verify_remote(client, 'c' * 40, api.policy)
        cached = c._VERIFIED_CONTROLLER_BYTES[client]
        self.assertEqual(set(cached), {(api.policy['workflow_path'], 'c' * 40)})
        self.assertTrue(all(isinstance(raw, bytes) for raw in cached.values()))
        ref = weakref.ref(client)
        del client
        gc.collect()
        self.assertIsNone(ref())

    def test_mutable_repository_runs_jobs_and_logs_are_never_cached(self):
        api, _, client = self.client()
        paths = ('', '/actions/workflows/21', '/actions/runs/1001',
                 '/actions/runs/1001/attempts/1',
                 '/actions/runs/1001/attempts/1/jobs?per_page=100&page=1')
        for _ in range(2):
            for path in paths:
                client.json(path)
            client.logs(1501)
        for path in paths:
            self.assertEqual(api.called.count(path), 2)
        self.assertEqual(api.called.count('logs:1501'), 2)
        self.assertEqual(client.requests, 14)
        self.assertNotIn(client, c._VERIFIED_CONTROLLER_BYTES)

    def test_verified_cache_hit_needs_no_request_but_does_not_reset_hard_cap(self):
        api, _, client = self.client()
        c.verify_remote(client, 'c' * 40, api.policy)
        client.requests = p.MAX_REQUESTS
        c.verify_remote(client, 'c' * 40, api.policy)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'):
            client.json('')
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'):
            c.verify_remote(client, 'd' * 40, api.policy)
        self.assertEqual(client.requests, 32)
        self.assertEqual(p.MAX_REQUESTS, 32)

    def test_four_distinct_candidate_controllers_still_fail_closed_at_32(self):
        rows = [fixture.run(1000 + i, head_sha=format(i + 1, '040x')) for i in range(90)]
        api = fixture.FakeAPI(rows)
        execution = {'run_id': 2000, 'run_attempt': 1, 'code_sha': 'a' * 40, 'controller_sha': 'a' * 40}
        api.overrides['/actions/runs/2000/attempts/1'] = fixture.run(2000, status='in_progress', conclusion=None)
        for page in range(1, 4):
            api.overrides['/actions/workflows/21/runs?per_page=30&page=' + str(page)] = {
                'total_count': 90, 'workflow_runs': rows[(page - 1) * 30:page * 30]}
        _, opener, client = self.client(api)
        p.verify_execution(client, api.policy, execution, 'schedule')
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'):
            p.discover(client, api.policy, exclude_run_id=2000, exclude_run_attempt=1)
        self.assertEqual(client.requests, 32)
        self.assertEqual(len(opener.calls), 32)
        self.assertEqual(len([path for path in api.called if path.startswith('/contents/')]), 5)


if __name__ == '__main__':
    unittest.main()
