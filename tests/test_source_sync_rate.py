import concurrent.futures
import datetime
import email.utils
import io
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from source_sync_rate import RequestPolicy, RateLimitFailure, retry_seconds, REASONS
import sync_getnote_to_raw as sync
import source_sync_full_replay as replay
import source_sync_inventory as inventory


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def sleep(self, delay): self.now += delay


class RateTests(unittest.TestCase):
    def policy(self, seconds=6600):
        clock = Clock()
        return clock, RequestPolicy(seconds, clock=clock, wall=lambda: 0, sleep=clock.sleep, jitter=lambda: 0)

    def test_retry_after_seconds_dates_invalid_and_expired(self):
        for value, expected in [('120',120), (120,120), (1.5,1.5),
                ('Thu, 01 Jan 1970 00:02:00 GMT',120),
                ('Wed, 31 Dec 1969 16:02:00 -0800',120),
                ('Thu, 01 Jan 1970 00:02:00',0),
                ('PRIVATE URL SECRET',0), ('-2',0), (True,0), (float('nan'),0),
                ('Wed, 31 Dec 1969 00:00:00 GMT',0)]:
            self.assertEqual(retry_seconds(value,0), expected)

    def test_shared_spacing_and_cooldown_across_threads(self):
        clock, policy = self.policy()
        stamps=[]
        def run():
            policy.before_request()
            stamps.append(clock())
        # Virtual-clock test serializes advancement but uses separate workers.
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for _ in range(3): pool.submit(run).result()
        self.assertEqual(stamps,[0,1,2])
        policy.rate_limited(0, '120', {'error':{'reason':'qps_bucket'}},429)
        run(); self.assertEqual(stamps[-1],122)
        run(); self.assertEqual(stamps[-1],123)

    def test_exponential_backoff_bounded_jitter(self):
        clock, policy = self.policy()
        policy.jitter=lambda: 5
        for attempt, delay in enumerate((65,125,245,485)):
            before=clock(); policy.rate_limited(attempt)
            policy.before_request(); self.assertEqual(clock()-before,delay)
        with self.assertRaises(RateLimitFailure): policy.rate_limited(4)
        with self.assertRaises(RateLimitFailure): policy.before_request()

    def test_long_server_wait_stops_without_sleep_or_shortening(self):
        for value in ('9'*10000, 10**400, float('inf'), '6600', '999999999999999999999999999999999', 'Thu, 01 Jan 1970 02:00:00 GMT'):
            clock, policy=self.policy()
            with self.assertRaises(RateLimitFailure) as caught: policy.rate_limited(0,value,http_status=429)
            self.assertTrue(caught.exception.budget)
            self.assertEqual(clock(),0)
            with self.assertRaises(RateLimitFailure): policy.before_request()

    def test_quota_and_membership_stop_all_waiters_immediately(self):
        for reason in ('quota_day','quota_month','not_member'):
            clock,policy=self.policy()
            with self.assertRaises(RateLimitFailure) as caught:
                policy.rate_limited(0,data={'error':{'reason':reason}},http_status=429)
            self.assertEqual(caught.exception.reason,reason)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                futures=[pool.submit(policy.before_request) for _ in range(3)]
                for future in futures:
                    with self.assertRaises(RateLimitFailure): future.result()
            self.assertEqual(clock(),0)
        for code in (42900,10201):
            _,policy=self.policy()
            with self.assertRaises(RateLimitFailure): policy.rate_limited(0,data={'error':{'code':code}})

    def test_deadline_and_sleep_do_not_hold_lock(self):
        clock,policy=self.policy(2)
        policy.before_request()
        def sleep(delay):
            self.assertTrue(policy.lock.acquire(False));policy.lock.release()
            clock.sleep(delay)
        policy.sleep=sleep
        policy.before_request()
        with self.assertRaises(RateLimitFailure):policy.before_request()
        self.assertEqual(clock(),1)

    def http_error(self, reason=None, header='0', body=None):
        body = body if body is not None else json.dumps({'error':{'reason':reason,'message':'PRIVATE BODY SECRET URL ID'}}).encode()
        return urllib.error.HTTPError('https://PRIVATE/ID?SECRET',429,'PRIVATE',{'Retry-After':header},io.BytesIO(body))

    def test_actual_helper_http429_uses_policy_five_attempts_then_stops(self):
        clock,policy=self.policy()
        opener=Mock();opener.open.side_effect=lambda *a,**k: (_ for _ in ()).throw(self.http_error('qps_global'))
        budget=inventory.ResponseBudget(100000)
        with patch.object(sync.urllib.request,'build_opener',return_value=opener):
            with self.assertRaises(RateLimitFailure) as caught:
                sync.api_get(sync.DETAIL_PATH,{'id':'PRIVATE'},'SECRET','SECRET',2048,budget,policy)
        self.assertEqual(opener.open.call_count,5); self.assertEqual(clock(),900)
        self.assertEqual(caught.exception.reason,'qps_global')
        self.assertGreater(budget.used,0)
        safe=replay.safe_report(dict(stage='detail',**replay.failure_details('detail',caught.exception)))
        self.assertEqual(safe['rate_limit_reason'],'qps_global');self.assertEqual(safe['http_status'],429)
        self.assertNotIn('PRIVATE',json.dumps(safe));self.assertNotIn('SECRET',json.dumps(safe))

    def test_actual_helper_quota_reads_bounded_body_and_stops_once(self):
        _,policy=self.policy(); error=self.http_error('quota_day')
        opener=Mock();opener.open.side_effect=error
        with patch.object(sync.urllib.request,'build_opener',return_value=opener):
            with self.assertRaises(RateLimitFailure): sync.api_get(sync.LIST_PATH,{},'K','C',2048,inventory.ResponseBudget(4096),policy)
        self.assertEqual(opener.open.call_count,1); self.assertTrue(error.closed)

    def test_oversized_and_malformed_error_body_never_echoed(self):
        for body in (b'PRIVATE'*2000,b'PRIVATE', b'[]',b'{"error": ["PRIVATE"]}'):
            _,policy=self.policy();reads=[]
            def err(*args,**kwargs):
                error=self.http_error(body=body)
                read=error.read
                error.read=lambda size:(reads.append(size),read(size))[1]
                return error
            opener=Mock();opener.open.side_effect=lambda *a,**k: (_ for _ in ()).throw(err())
            with patch.object(sync.urllib.request,'build_opener',return_value=opener):
                with self.assertRaises(RateLimitFailure) as caught: sync.api_get(sync.LIST_PATH,{},'K','C',16384,inventory.ResponseBudget(100000),policy)
            self.assertEqual(reads,[8193]*5);self.assertEqual(caught.exception.reason,'unknown')
            self.assertNotIn('PRIVATE',str(caught.exception))

    def test_error_body_obeys_existing_byte_limit(self):
        _,policy=self.policy();opener=Mock();opener.open.side_effect=self.http_error(body=b'x'*100)
        budget=inventory.ResponseBudget(30)
        with patch.object(sync.urllib.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(RuntimeError,'byte limit'):sync.api_get(sync.LIST_PATH,{},'K','C',20,budget,policy)
        self.assertEqual(budget.used,21);self.assertEqual(opener.open.call_count,1)

    def test_list_pagination_forwards_same_policy(self):
        _,policy=self.policy()
        with patch.object(sync,'api_get',return_value={'data':{'notes':[],'has_more':False}}) as api:
            list(sync.fetch_note_pages('K','C',request_policy=policy))
        self.assertIs(api.call_args.kwargs['request_policy'],policy)

    def test_unknown_reason_redacted_by_second_filter(self):
        for value in ('PRIVATE',{},[],True,None):
            safe=replay.safe_report({'stage':'detail','error_code':'api_rate_limited','rate_limit_reason':value})
            self.assertNotIn('rate_limit_reason',safe)
        for reason in REASONS+('unknown',):
            safe=replay.safe_report({'stage':'detail','error_code':'api_rate_limited','rate_limit_reason':reason})
            self.assertEqual(safe['rate_limit_reason'],reason)

    def test_http_date_is_honored_by_actual_helper(self):
        clock,policy=self.policy();success=Mock();success.__enter__=Mock(return_value=success)
        success.__exit__=Mock(return_value=False);success.headers={};success.read1.side_effect=[b'{}',b'']
        opener=Mock();opener.open.side_effect=[self.http_error('qps_global','Thu, 01 Jan 1970 00:03:00 GMT'),success]
        with patch.object(sync.urllib.request,'build_opener',return_value=opener):
            self.assertEqual(sync.api_get(sync.LIST_PATH,{},'K','C',2048,inventory.ResponseBudget(4096),policy),{})
        self.assertEqual(clock(),180);self.assertEqual(opener.open.call_count,2)

    def test_success_status_api_quota_stops_without_retry(self):
        for error in ({'code':42900}, {'code':10201}, {'code':10202,'reason':'quota_day'}):
            _,policy=self.policy();response=Mock();response.__enter__=Mock(return_value=response)
            response.__exit__=Mock(return_value=False)
            response.read.return_value=json.dumps({'success':False,'error':error}).encode()
            opener=Mock();opener.open.return_value=response
            with patch.object(sync.urllib.request,'build_opener',return_value=opener):
                with self.assertRaises(RateLimitFailure):sync.api_get(sync.LIST_PATH,{},'K','C',2048,request_policy=policy)
            self.assertEqual(opener.open.call_count,1)

    def test_waiting_worker_rechecks_shared_stop(self):
        clock,policy=self.policy();policy.rate_limited(0)
        def sleep(delay):
            clock.sleep(delay)
            try: policy.rate_limited(0,data={'error':{'reason':'quota_month'}})
            except RateLimitFailure: pass
        policy.sleep=sleep
        with self.assertRaises(RateLimitFailure) as caught:policy.before_request()
        self.assertEqual(caught.exception.reason,'quota_month');self.assertEqual(clock(),1)

    def test_policy_identity_propagates_through_both_replay_passes(self):
        # Existing complete replay fixture still validates byte and mtime safety.
        import tempfile
        from test_source_sync_full_replay import FullReplayTests
        fixture=FullReplayTests()
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);root=fixture.seed(work);_,policy=self.policy()
            original=replay.one_pass
            seen=[]
            def one_pass(*args,**kwargs):
                seen.append(args[-1])
                return original(*args,**kwargs)
            original_replay=replay.replay
            def paced(*args,**kwargs):
                kwargs['request_policy']=policy
                return original_replay(*args,**kwargs)
            # Let the existing API fixture accept the new optional keyword.
            with patch.object(replay,'one_pass',side_effect=one_pass), patch.object(replay,'replay',side_effect=paced):
                result=fixture.execute(root,work,request_policy=policy)
            self.assertEqual(seen,[policy,policy]);self.assertEqual(result['status'],'full_replay_ok')


    def test_truncated_429_body_charges_partial_bytes_and_closes(self):
        for maximum, expected_exception in ((100, http.client.IncompleteRead),
                                             (3, inventory.SourceLimit)):
            _,policy=self.policy();error=self.http_error()
            error.read=Mock(side_effect=http.client.IncompleteRead(b'PRIVATE',20))
            opener=Mock();opener.open.side_effect=error
            budget=inventory.ResponseBudget(maximum)
            with patch.object(sync.urllib.request,'build_opener',return_value=opener):
                with self.assertRaises(expected_exception):
                    sync.api_get(sync.LIST_PATH,{},'K','C',100,budget,policy)
            self.assertEqual(budget.used,7)
            self.assertEqual(opener.open.call_count,1)
            self.assertTrue(error.closed)
