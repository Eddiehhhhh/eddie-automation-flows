import unittest
from retry import HTTPFailure, TransportFailure, RetryStopped, RetryTransport, retry_after_seconds

class Clock:
    def __init__(self): self.now=0.0; self.delays=[]
    def mono(self): return self.now
    def sleep(self,seconds): self.delays.append(seconds); self.now+=seconds
class Script:
    def __init__(self, results): self.results=list(results); self.calls=[]
    def request(self,*args):
        self.calls.append(args)
        value=self.results.pop(0)
        if isinstance(value,Exception): raise value
        return value
class Tests(unittest.TestCase):
    def setup_transport(self,values,**kw):
        self.clock=Clock();self.raw=Script(values)
        return RetryTransport(self.raw,sleep=self.clock.sleep,monotonic=self.clock.mono,wall_time=lambda:0,random_value=lambda:1,**kw)
    def test_read_transient_eventual_success(self):
        r=self.setup_transport([HTTPFailure(503),TransportFailure(),{'ok':True}])
        self.assertEqual(r.request('GET','/path'),{'ok':True})
        self.assertEqual(self.clock.delays,[0.5,1.0]);self.assertEqual(len(self.raw.calls),3)
    def test_write_429_never_retried(self):
        for method in ('POST','PUT','PATCH','DELETE'):
            r=self.setup_transport([HTTPFailure(429,'1'),{}],allow_notion_readonly_posts=True)
            with self.assertRaises(HTTPFailure): r.request(method,'/v1/pages',{'private':'not logged'})
            self.assertEqual(len(self.raw.calls),1);self.assertEqual(self.clock.delays,[])
    def test_write_transport_ambiguous_never_retried(self):
        r=self.setup_transport([TransportFailure(),{}])
        with self.assertRaises(TransportFailure):r.request('PATCH','/v1/blocks/block/children')
        self.assertEqual(len(self.raw.calls),1)
    def test_post_queries_opt_in_exact_routes(self):
        path='/v1/databases/11111111-1111-4111-8111-111111111111/query'
        for allowed in (False,True):
            r=self.setup_transport([HTTPFailure(429,'2'),{}],allow_notion_readonly_posts=allowed)
            if allowed:self.assertEqual(r.request('POST',path,{'filter':{}}),{})
            else:
                with self.assertRaises(HTTPFailure):r.request('POST',path,{})
            self.assertEqual(len(self.raw.calls),2 if allowed else 1)
    def test_search_post_opt_in(self):
        r=self.setup_transport([TransportFailure(),{}],allow_notion_readonly_posts=True)
        r.request('POST','/v1/search',{});self.assertEqual(len(self.raw.calls),2)
    def test_route_lookalikes_not_retried(self):
        for path in ('/v1/search?x=1','/v1/search/','/v1/databases/../query','/v1/databases/db/query','/v1/pages/query'):
            r=self.setup_transport([HTTPFailure(503),{}],allow_notion_readonly_posts=True)
            with self.assertRaises(HTTPFailure):r.request('POST',path,{})
            self.assertEqual(len(self.raw.calls),1)
    def test_auth_and_validation_errors_not_retried(self):
        for status in (400,401,403,404,409,422,501):
            r=self.setup_transport([HTTPFailure(status),{}])
            with self.assertRaises(HTTPFailure):r.request('GET','/path')
            self.assertEqual(len(self.raw.calls),1)
    def test_attempt_bound(self):
        r=self.setup_transport([HTTPFailure(503)]*4,max_attempts=3)
        with self.assertRaises(RetryStopped) as cm:r.request('GET','/path')
        self.assertEqual(cm.exception.attempts,3);self.assertEqual(cm.exception.reason,'attempt_limit')
        self.assertEqual(len(self.raw.calls),3)
    def test_retry_after_seconds(self):
        r=self.setup_transport([HTTPFailure(429,'3'),{}])
        r.request('GET','/path');self.assertEqual(self.clock.delays,[3.0])
    def test_http_date(self):
        self.assertEqual(retry_after_seconds('Thu, 01 Jan 1970 00:00:07 GMT',0),7)
        self.assertEqual(retry_after_seconds('Thu, 01 Jan 1970 00:00:00 GMT',7),0)
    def test_malformed_header_ignored(self):
        for value in ('secret_url', '-1','1.5','NaN','Infinity','a'*257,'Thu, 01 Jan 1970 00:00:07',None):
            self.assertIsNone(retry_after_seconds(value,0))
    def test_server_delay_not_truncated_into_early_retry(self):
        r=self.setup_transport([HTTPFailure(429,'1000'),{}])
        with self.assertRaises(RetryStopped) as cm:r.request('GET','/path')
        self.assertEqual(cm.exception.reason,'server_delay_exceeds_budget')
        self.assertEqual(len(self.raw.calls),1);self.assertEqual(self.clock.delays,[])
    def test_elapsed_budget(self):
        r=self.setup_transport([HTTPFailure(503)]*9,elapsed_budget=1,max_attempts=9)
        with self.assertRaises(RetryStopped) as cm:r.request('GET','/path')
        self.assertEqual(cm.exception.reason,'elapsed_budget');self.assertEqual(len(self.raw.calls),2)
    def test_backoff_cap(self):
        r=self.setup_transport([HTTPFailure(503)]*4+[{}],base_delay=1,max_delay=2,max_attempts=5)
        r.request('GET','/path');self.assertEqual(self.clock.delays,[1,2,2,2])
    def test_pacing_including_writes(self):
        r=self.setup_transport([{},{}],min_interval=1)
        r.request('GET','/path');r.request('POST','/v1/pages',{})
        self.assertEqual(self.clock.delays,[1]);self.assertEqual(len(self.raw.calls),2)
    def test_no_sensitive_failure_rendering(self):
        err=HTTPFailure(429,'https://secret.example/?token=SECRET')
        self.assertNotIn('SECRET',str(err));self.assertNotIn('SECRET',repr(err))
        r=self.setup_transport([err]*3,max_attempts=2)
        with self.assertRaises(RetryStopped) as cm:r.request('GET','/private?token=SECRET',{'secret':'SECRET'})
        self.assertNotIn('SECRET',str(cm.exception));self.assertEqual(cm.exception.__suppress_context__,True)
    def test_bad_config_rejected(self):
        for kwargs in ({'max_attempts':0},{'max_attempts':True},{'base_delay':float('nan')},{'base_delay':31},{'min_interval':-1},{'allow_notion_readonly_posts':'yes'}):
            with self.assertRaises(ValueError):self.setup_transport([{}],**kwargs)
    def test_untyped_errors_not_retried(self):
        r=self.setup_transport([ValueError('shape_error'),{}])
        with self.assertRaises(ValueError):r.request('GET','/path')
        self.assertEqual(len(self.raw.calls),1)
if __name__=='__main__':unittest.main()
