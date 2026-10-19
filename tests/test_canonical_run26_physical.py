"""Recorded public sequence-two proof through current-policy physical gates."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'diagnostics')]
import replay_canonical_run26_physical as physical


class Tests(unittest.TestCase):
    def test_manual_and_scheduled_current_policy_reach_exact_sequence_two(self):
        for trigger in ('manual','schedule'):
            result=physical.replay(trigger)
            self.assertEqual(result['physical_requests'],19)
            self.assertEqual(result['max_physical_requests'],32)
            self.assertEqual(result['candidate_attempts_checked'],4)
            self.assertEqual(result['listing_run_numbers'],list(range(29,0,-1)))
            self.assertEqual(result['selected_run_number'],26)
            self.assertEqual(result['selected_receipt_sequence'],2)
            self.assertEqual(result['candidate_rules_sha256'],physical.p.entry.rules_sha256())
            self.assertEqual(result['candidate_get_policy_sha256'],physical.p.recovery.current_get_policy())
            self.assertEqual(result['record_sha256'],physical.p.recovery.SEQUENCE_TWO_RECORD['record_sha256'])
            self.assertTrue(result['execution_verification_included'])
            self.assertTrue(result['current_attempt_excluded'])
            self.assertTrue(result['log_redirect_included'])
            self.assertFalse(result['cdn_authorization_forwarded'])
            self.assertEqual(result['network_calls'],0)
            self.assertFalse(result['private_state_replayed'])
            self.assertTrue(result['private_receipt_readback_required'])
            self.assertFalse(any('/39279452717/attempts/' in route for route in result['api_routes']))
            self.assertTrue(any('/39276816133/attempts/' in route for route in result['api_routes']))
            self.assertTrue(any('/39273266609/attempts/' in route for route in result['api_routes']))

    def test_budget_exhaustion_and_log_denial_fail_without_older_fallback(self):
        p=physical.p
        with patch.object(p,'MAX_REQUESTS',14):
            with self.assertRaises(p.ProvenanceFailure):physical.replay()
        policy=physical.shared.policy();opener=physical.Opener(policy);normal=opener.open
        def denied(request,timeout):
            if request.full_url.endswith('/actions/jobs/'+str(p.recovery.SEQUENCE_TWO_JOB)+'/logs'):
                opener.calls.append(request.full_url)
                return physical.shared.Response(b'{}',403)
            return normal(request,timeout)
        opener.open=denied
        client=p.PublicGithub('synthetic-noncredential',opener=opener,clock=lambda:0,sleep=lambda _:None)
        with self.assertRaises(p.ProvenanceFailure):
            p.discover(client,policy,exclude_run_id=physical.shared.SYNTHETIC_RUN,exclude_run_attempt=1)
        self.assertEqual(sum('/actions/jobs/' in url for url in opener.calls),1)
        self.assertFalse(any('/39093261469/attempts/' in url for url in opener.calls))
        self.assertFalse(opener.cdn_auth)

    def test_self_rehashed_different_sequence_two_marker_cannot_be_compatibility_anchor(self):
        p=physical.p;policy=physical.shared.policy();opener=physical.Opener(policy)
        line=opener.api.canonical['marker_lines'][0]
        prefix,encoded=line.split(p.MARKER,1);record=json.loads(encoded)
        record['receipt_sha256']='0'*64
        record.pop('record_sha256');record['record_sha256']=p.a.sha(p.a.encoded(record))
        opener.api.canonical['marker_lines']=[prefix+p.MARKER+p.a.encoded(record).decode().strip()]
        client=p.PublicGithub('synthetic-noncredential',opener=opener,clock=lambda:0,sleep=lambda _:None)
        with self.assertRaisesRegex(p.ProvenanceFailure,'provenance_record'):
            p.discover(client,policy,exclude_run_id=physical.shared.SYNTHETIC_RUN,exclude_run_attempt=1)
        self.assertFalse(any('/39093261469/attempts/' in url for url in opener.calls))


if __name__=='__main__':unittest.main()
