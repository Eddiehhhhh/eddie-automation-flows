"""Synthetic public-time accounting; no wall waits, HTTP, or credentials."""
import unittest
from test_publication_provenance import p, Response


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class PublicTransportClockTests(unittest.TestCase):
    def client(self, responses, *, opening=0, reading=0, sleep=None):
        clock = Clock()
        class SlowResponse(Response):
            def read(self, cap):
                clock.advance(reading)
                return super().read(cap)
        class Opener:
            def __init__(self): self.calls = []; self.timeouts = []
            def open(self, request, timeout):
                self.calls.append(request); self.timeouts.append(timeout)
                clock.advance(opening)
                status, body, headers = responses.pop(0)
                return SlowResponse(status, body, headers)
        opener = Opener()
        client = p.PublicGithub('synthetic-existing-token', opener=opener, clock=clock,
                                sleep=sleep if sleep is not None else clock.advance)
        return client, clock, opener

    def test_idle_before_and_between_phases_does_not_consume_public_budget(self):
        client, clock, opener = self.client([(200, b'{}', {})] * 2, opening=2, reading=3)
        clock.advance(121)
        self.assertEqual(client.json(''), {})
        clock.advance(600)  # Private capture/object work between public phases.
        self.assertEqual(client.json(''), {})
        self.assertEqual(client.elapsed, 10)
        self.assertEqual(client.requests, 2); self.assertEqual(len(opener.calls), 2)

    def test_open_and_body_read_time_accumulate_and_exhaust_at_same_120_seconds(self):
        client, clock, opener = self.client([(200, b'{}', {})] * 4, opening=10, reading=30)
        self.assertEqual(client.json(''), {}); clock.advance(300)
        self.assertEqual(client.json(''), {})
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'): client.json('')
        self.assertEqual(client.elapsed, 120); self.assertEqual(client.requests, 3)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'): client.json('')
        self.assertEqual(len(opener.calls), 3)

    def test_remaining_transport_seconds_limit_next_socket_timeout(self):
        client, clock, opener = self.client([(200, b'{}', {})], opening=0.25, reading=0.25)
        client.elapsed = 119; clock.advance(500)
        self.assertEqual(client.json(''), {})
        self.assertEqual(opener.timeouts, [1]); self.assertEqual(client.elapsed, 119.5)

    def test_retry_sleep_actual_duration_counts_toward_same_budget(self):
        client, clock, opener = self.client([(503, b'{}', {}), (200, b'{}', {})], opening=2, reading=3)
        self.assertEqual(client.json(''), {})
        self.assertEqual(client.elapsed, 11); self.assertEqual(client.requests, 2)
        self.assertEqual(opener.timeouts, [15, 15])

    def test_retry_sleep_can_exhaust_budget_without_issuing_retry(self):
        client, clock, opener = self.client([(503, b'{}', {}), (200, b'{}', {})], opening=118)
        client.sleep = lambda requested: clock.advance(3)
        with self.assertRaisesRegex(p.ProvenanceFailure, 'provenance_budget'): client.json('')
        self.assertEqual(client.elapsed, 121); self.assertEqual(len(opener.calls), 1)

    def test_failed_open_and_read_are_charged_without_revealing_body(self):
        for failure in ('open', 'read'):
            client, clock, opener = self.client([])
            class BadResponse(Response):
                def read(self, cap):
                    clock.advance(9); raise OSError('private provider body')
            def bad_open(request, timeout):
                clock.advance(7)
                if failure == 'open': raise OSError('private provider body')
                return BadResponse(200)
            opener.open = bad_open
            with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_transport$'): client.json('')
            self.assertEqual(client.elapsed, 7 if failure == 'open' else 16)
            self.assertEqual(client.requests, 1)

    def test_auth_denial_is_charged_once_without_retry_or_anonymous_fallback(self):
        client, clock, opener = self.client([(403, b'{}', {})], opening=2, reading=3)
        with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_unavailable$'): client.json('')
        self.assertEqual(client.elapsed, 5); self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0].get_header('Authorization'), 'Bearer synthetic-existing-token')

    def test_redirect_and_cdn_read_time_both_count_without_forwarding_token(self):
        client, clock, opener = self.client([
            (302, b'', {'Location': 'https://logs.blob.core.windows.net/signed'}),
            (200, b'plain log', {})], opening=2, reading=3)
        self.assertEqual(client.logs(123), b'plain log')
        self.assertEqual(client.elapsed, 10); self.assertEqual(client.requests, 2)
        self.assertIsNone(opener.calls[1].get_header('Authorization'))

    def test_idle_does_not_reset_requests_or_bytes(self):
        client, clock, opener = self.client([(200, b'{}', {})])
        client.requests = p.MAX_REQUESTS - 1; client.bytes = p.MAX_TOTAL - 1
        clock.advance(500)
        with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_budget$'): client.json('')
        self.assertEqual(client.requests, p.MAX_REQUESTS); self.assertEqual(client.bytes, p.MAX_TOTAL + 1)
        clock.advance(500)
        with self.assertRaisesRegex(p.ProvenanceFailure, '^provenance_budget$'): client.json('')
        self.assertEqual(len(opener.calls), 1)


if __name__ == '__main__': unittest.main()
