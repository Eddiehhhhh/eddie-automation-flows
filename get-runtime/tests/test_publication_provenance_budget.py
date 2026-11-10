"""Public provenance failures identify only the bounded limit that was hit."""
import importlib
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_runtime_bindings
with patch.object(get_private_runtime_bindings, 'value', return_value=None):
    provenance = importlib.import_module('get_publication_provenance')
    recurring = importlib.import_module('get_recurring_runtime')


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _maximum):
        return self.body


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def open(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)


class PublicBudgetDiagnostics(unittest.TestCase):
    def test_request_limit_has_distinct_fixed_error(self):
        opener = Opener(Response(b'{}'))
        client = provenance.AnonymousGithub(opener=opener)
        client.requests = provenance.MAX_REQUESTS
        with self.assertRaises(provenance.ProvenanceFailure) as raised:
            client.once('https://api.github.com/repos/example/repo', 10)
        self.assertEqual(str(raised.exception), 'provenance_budget_requests')
        self.assertEqual(opener.calls, 0)

    def test_elapsed_limit_has_distinct_fixed_error(self):
        client = provenance.AnonymousGithub(opener=Opener(Response(b'{}')))
        client.elapsed = provenance.MAX_SECONDS
        with self.assertRaises(provenance.ProvenanceFailure) as raised:
            client.once('https://api.github.com/repos/example/repo', 10)
        self.assertEqual(str(raised.exception), 'provenance_budget_elapsed')

    def test_single_response_limit_has_distinct_fixed_error(self):
        client = provenance.AnonymousGithub(opener=Opener(Response(b'123')))
        with self.assertRaises(provenance.ProvenanceFailure) as raised:
            client.once('https://api.github.com/repos/example/repo', 2)
        self.assertEqual(str(raised.exception), 'provenance_budget_response_bytes')

    def test_cumulative_response_limit_has_distinct_fixed_error(self):
        client = provenance.AnonymousGithub(opener=Opener(Response(b'12')))
        client.bytes = provenance.MAX_TOTAL - 1
        with self.assertRaises(provenance.ProvenanceFailure) as raised:
            client.once('https://api.github.com/repos/example/repo', 2)
        self.assertEqual(str(raised.exception), 'provenance_budget_total_bytes')

    def test_log_redirect_counts_both_gets(self):
        opener = Opener(
            Response(b'', 302, {'Location': 'https://logs.blob.core.windows.net/job'}),
            Response(b'ok'),
        )
        client = provenance.AnonymousGithub(opener=opener)
        self.assertEqual(client.logs(1), b'ok')
        self.assertEqual(client.requests, 2)

    def test_single_server_retry_counts_against_physical_read_budget(self):
        opener = Opener(Response(b'busy', 503), Response(b'{}'))
        client = provenance.AnonymousGithub(opener=opener, sleep=lambda _seconds: None)
        self.assertEqual(client.request('/actions/workflows/387242896', 1024), b'{}')
        self.assertEqual(client.requests, 2)
        self.assertEqual(opener.calls, 2)

    def test_public_report_keeps_only_fixed_budget_code(self):
        error = provenance.ProvenanceFailure('provenance_budget_requests')
        error.runtime_stage = 'receipt_provenance'
        report = recurring.public_result(recurring.failure(error))
        self.assertEqual(report['error'], 'provenance_budget_requests')
        self.assertEqual(report['stage'], 'receipt_provenance')
        self.assertNotIn('details', report)

        unknown = recurring.public_result(recurring.failure(RuntimeError('private detail')))
        self.assertNotIn('private detail', str(unknown))


if __name__ == '__main__':
    unittest.main()
