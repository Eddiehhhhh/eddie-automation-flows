"""Existing-token injection and redirect boundary; all HTTP responses synthetic."""
import io
import email.message
import urllib.request
import urllib.response
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_publication_provenance as p
import get_recurring_runtime as get
import get_notion_runtime as notion
import test_publication_provenance as fixture


class Tests(unittest.TestCase):
    def client(self, responses):
        class Opener:
            def __init__(self): self.calls = []
            def open(self, request, timeout):
                self.calls.append(request)
                if not responses: raise AssertionError('No extra request permitted')
                return responses.pop(0)
        opener = Opener()
        return p.PublicGithub('existing-runtime-token', opener=opener, sleep=lambda _: None), opener

    def headers(self, request):
        return {k.lower(): v for k, v in request.header_items()}


    def stdlib_client(self, responses, *, legacy=False):
        # Use the real urllib response/error/redirect chain, not an opener that
        # hands fabricated 302 responses directly to once(). No sockets exist.
        calls = []
        class SyntheticHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, request):
                calls.append(request)
                if not responses: raise AssertionError('No additional request')
                status, fields, raw = responses.pop(0)
                headers = email.message.Message()
                for key, value in fields.items(): headers[key] = value
                response = urllib.response.addinfourl(io.BytesIO(raw), headers,
                                                     request.full_url, status)
                response.msg = 'Synthetic'
                return response
        original = urllib.request.build_opener
        def build(*handlers):
            if legacy:
                handlers = tuple(p.m.r.NoRedirect() if isinstance(h, p.InspectRedirect) else h for h in handlers)
            return original(*handlers, SyntheticHTTPS())
        with patch.object(p.urllib.request, 'build_opener', side_effect=build):
            client = p.PublicGithub('existing-runtime-token', sleep=lambda _: None)
        return client, calls

    def test_real_urllib_chain_reproduces_old_precheck_failure(self):
        client, calls = self.stdlib_client([(302, {'Location': 'https://fixture.blob.core.windows.net/log'}, b'')], legacy=True)
        with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_transport$'):
            client.logs(123)
        self.assertEqual(len(calls), 1)

    def test_real_urllib_chain_exposes_302_without_automatic_follow(self):
        client, calls = self.stdlib_client([
            (302, {'Location': 'https://fixture.blob.core.windows.net/log?signed=ephemeral'}, b''),
            (200, {}, b'fixed fixture logs')])
        self.assertEqual(client.logs(123), b'fixed fixture logs')
        self.assertEqual(client.requests, 2); self.assertEqual(len(calls), 2)
        self.assertEqual(self.headers(calls[0])['authorization'], 'Bearer existing-runtime-token')
        self.assertNotIn('authorization', self.headers(calls[1]))

    def test_real_urllib_chain_redirects_outside_log_gate_never_follow(self):
        for status in (301, 302, 303, 307, 308):
            for invalid in ('https://evil.invalid/leak', p.API + '/other'):
                client, calls = self.stdlib_client([(status, {'Location': invalid}, b'')])
                with self.assertRaises(p.ProvenanceFailure): client.logs(123)
                self.assertEqual(len(calls), 1)
        client, calls = self.stdlib_client([(302, {'Location': 'https://fixture.blob.core.windows.net/log'}, b'')])
        with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_unavailable$'): client.json('')
        self.assertEqual(len(calls), 1)
        client, calls = self.stdlib_client([
            (302, {'Location': 'https://fixture.blob.core.windows.net/log'}, b''),
            (302, {'Location': 'https://other.blob.core.windows.net/log'}, b'')])
        with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_logs_unavailable$'): client.logs(123)
        self.assertEqual(len(calls), 2); self.assertNotIn('authorization', self.headers(calls[1]))

    def test_real_urllib_chain_denials_are_terminal(self):
        for status in (401, 403, 429):
            client, calls = self.stdlib_client([(status, {}, b'private error body')])
            with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_logs_unavailable$'): client.logs(123)
            self.assertEqual(len(calls), 1)

    def test_fixed_public_json_get_has_only_explicit_existing_token(self):
        client, opener = self.client([fixture.Response(200)])
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'ambient-wrong', 'PRIVATE_VAULT_TOKEN': 'private-wrong'}):
            self.assertEqual(client.json(''), {})
        req = opener.calls[0]
        self.assertEqual(req.full_url, p.API); self.assertEqual(req.get_method(), 'GET')
        self.assertEqual(self.headers(req)['authorization'], 'Bearer existing-runtime-token')
        self.assertNotIn('cookie', self.headers(req)); self.assertEqual(client.credentials_used, 1)

    def test_signed_log_redirect_never_receives_authorization(self):
        for host in ('test.blob.core.windows.net', 'test.actions.githubusercontent.com', 'test.githubusercontent.com'):
            client, opener = self.client([fixture.Response(302, headers={'Location': 'https://' + host + '/logs?signed=ephemeral'}), fixture.Response(200, b'logs')])
            self.assertEqual(client.logs(123), b'logs')
            self.assertEqual(self.headers(opener.calls[0])['authorization'], 'Bearer existing-runtime-token')
            self.assertNotIn('authorization', self.headers(opener.calls[1]))
            self.assertNotIn('cookie', self.headers(opener.calls[1])); self.assertEqual(client.requests, 2)

    def test_token_never_sent_to_other_repo_origin_or_api_lookalike(self):
        for url in ('https://api.github.com/repos/Other/private', p.API + '-evil/path',
                    'https://api.github.com.evil.invalid/repos/' + p.REPOSITORY):
            client, opener = self.client([fixture.Response(200)])
            client.once(url, 4096)  # direct boundary injection; no real HTTP
            self.assertNotIn('authorization', self.headers(opener.calls[0]))

    def test_invalid_redirects_stop_before_second_request(self):
        for url in ('https://api.github.com/repos/Other/private', 'https://evil.invalid/log',
                    'https://user:password@a.blob.core.windows.net/log', 'http://a.blob.core.windows.net/log'):
            client, opener = self.client([fixture.Response(302, headers={'Location': url})])
            with self.assertRaises(p.ProvenanceFailure): client.logs(123)
            self.assertEqual(len(opener.calls), 1)

    def test_auth_permission_quota_denial_never_retries_or_falls_back(self):
        for status in (401, 403, 429):
            for logs in (False, True):
                client, opener = self.client([fixture.Response(status), fixture.Response(200)])
                with self.assertRaises(p.ProvenanceFailure):
                    client.logs(123) if logs else client.json('')
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(self.headers(opener.calls[0])['authorization'], 'Bearer existing-runtime-token')

    def test_cdn_denial_does_not_retry_api_or_forward_token(self):
        client, opener = self.client([fixture.Response(302, headers={'Location': 'https://a.blob.core.windows.net/log'}), fixture.Response(403)])
        with self.assertRaises(p.ProvenanceFailure): client.logs(123)
        self.assertEqual(len(opener.calls), 2); self.assertNotIn('authorization', self.headers(opener.calls[1]))

    def test_transient_retry_keeps_same_identity_and_original_bounds(self):
        client, opener = self.client([fixture.Response(503), fixture.Response(200)])
        self.assertEqual(client.json(''), {})
        self.assertTrue(all(self.headers(req)['authorization'] == 'Bearer existing-runtime-token' for req in opener.calls))
        self.assertEqual(client.requests, 2); self.assertEqual(p.MAX_REQUESTS, 32)
        self.assertEqual(p.MAX_TOTAL, 16 * 1024 * 1024); self.assertEqual(p.MAX_SECONDS, 120)
        client.requests = p.MAX_REQUESTS
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'): client.json('')
        self.assertEqual(len(opener.calls), 2)

    def test_missing_bad_token_never_uses_ambient_credentials(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'ambient', 'PRIVATE_VAULT_TOKEN': 'private'}):
            for token in (None, '', False, 'a\nb', 'a b', 'a\x7fb'):
                with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_credentials$'):
                    p.PublicGithub(token)

    def test_explicit_anonymous_mode_still_ignores_environment(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'ambient'}):
            client = p.AnonymousGithub()
        self.assertEqual(client.credentials_used, 0); self.assertIsNone(client._token)

    def test_get_missing_token_stops_before_source_credentials_or_guard(self):
        policy = fixture.policy(rules_sha256=get.entry.rules_sha256(), get_policy_sha256=get.entry.policy_sha256(get.entry.rules_sha256(), get.entry.BOOTSTRAP))
        import sync_getnote_to_raw as sync
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy'; path.write_bytes(p.a.encoded(policy))
            with patch.dict(os.environ, {}, clear=True), patch.object(sync, 'load_credentials') as credentials, patch.object(get.m, 'disabled_writers') as guard, contextlib.redirect_stdout(io.StringIO()) as output:
                code = get.main(['--policy', str(path), '--approved-policy-sha256', p.a.sha(p.a.encoded(policy))])
        self.assertEqual(code, 1); credentials.assert_not_called(); guard.assert_not_called()
        self.assertNotIn('ambient', output.getvalue())

    def test_notion_cli_injects_same_existing_token_reader_and_writer_guard(self):
        result = {'status': 'queue_empty', 'write_state': 'not_attempted'}
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'existing-notion-runtime-token'}), patch.object(notion.transport, 'Transport'), patch.object(notion.old_writers, 'WriterGuard') as guard, patch.object(notion, 'run', return_value=result) as run, patch.object(p, 'AnonymousGithub', side_effect=AssertionError('No anonymous fallback')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(notion.main(['--expected-parent', 'a' * 40]), 0)
        guard.assert_called_once_with('existing-notion-runtime-token')
        self.assertIsInstance(run.call_args.kwargs['public_client'], p.PublicGithub)
        self.assertEqual(run.call_args.kwargs['public_client'].credentials_used, 1)

    def test_controller_only_maps_existing_token_in_both_steps(self):
        raw = p.controller.template_bytes()
        self.assertEqual(raw.count(b'GITHUB_TOKEN: ${{ github.token }}'), 2)
        self.assertIn(b'permissions:\n  contents: read\n', raw)
        self.assertNotIn(b'actions: read', raw); self.assertNotIn(b'actions: write', raw)

    def test_authenticated_discovery_reports_existing_credentials_honestly(self):
        api = fixture.FakeAPI(); api.credentials_used = 1
        result = p.discover(api, fixture.policy())
        self.assertEqual(result['credentials_used'], 1); self.assertEqual(result['new_permissions'], 0)

if __name__ == '__main__': unittest.main()
