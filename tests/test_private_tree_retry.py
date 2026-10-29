"""Synthetic fixed-tree transport failures; no credentials or network."""
import contextlib
import io
import signal
import ssl
import threading
import time
import urllib.error
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
import get_private_preview_retention as r

PATH='/git/trees/'+'a'*40
class Response:
    status=200
    def __init__(self,data=b'{}',read_error=None,delay=0):self.data=data;self.error=read_error;self.delay=delay;self.closed=False
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def read(self,n):
        if self.delay:time.sleep(self.delay)
        if self.error:raise self.error
        return self.data[:n]
class Open:
    def __init__(self,*events):self.events=list(events);self.calls=[]
    def open(self,request,timeout):
        self.calls.append((request.full_url,request.get_method(),dict(request.header_items()),timeout))
        event=self.events.pop(0)
        if isinstance(event,BaseException):raise event
        return event
class Tests(unittest.TestCase):
    def setUp(self):
        self.p=patch.object(r,'_TREE_RETRIES_USED',0);self.p.start();self.addCleanup(self.p.stop)
        self.metrics=patch.dict(r._TREE_READ_METRICS,{k:0 for k in r._TREE_READ_METRICS});self.metrics.start();self.addCleanup(self.metrics.stop)
    def remote(self,*events):
        x=r.GitHubRemote.__new__(r.GitHubRemote);x.token='synthetic-secret';x.local=None;x.opener=Open(*events);return x
    def error(self,status,headers=None):return urllib.error.HTTPError('https://invalid.invalid/private',status,'private',headers or {},io.BytesIO(b'private body'))
    def test_open_transient_once_same_url_headers_method(self):
        for error in (ConnectionResetError(),ConnectionAbortedError(),ConnectionRefusedError(),TimeoutError(),urllib.error.URLError(ConnectionResetError()),self.error(502),self.error(503),self.error(504)):
            with self.subTest(error=type(error).__name__):
                x=self.remote(error,Response());self.assertEqual(x.request(PATH),{})
                self.assertEqual(len(x.opener.calls),2);self.assertEqual(x.opener.calls[0][:3],x.opener.calls[1][:3]);self.assertEqual(x.opener.calls[0][1],'GET')
                self.assertLessEqual(x.opener.calls[1][3],x.opener.calls[0][3]);self.assertGreater(x.opener.calls[1][3],0)
        self.assertEqual(r._TREE_RETRIES_USED,8);self.assertEqual(r._TREE_READ_METRICS['attempts'],16)
    def test_global_cap_survives_new_remote(self):
        with patch.object(r,'_TREE_RETRIES_USED',8):
            x=self.remote(ConnectionResetError(),Response())
            with self.assertRaises(r.RetentionFailure):x.request(PATH)
            self.assertEqual(len(x.opener.calls),1)
    def test_only_one_extra_attempt_even_second_transient(self):
        x=self.remote(ConnectionResetError(),ConnectionResetError(),Response())
        with self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertEqual(len(x.opener.calls),2)
    def test_security_unknown_status_retry_after_never_retried(self):
        errors=[self.error(n) for n in (301,302,307,308,400,401,403,404,429,500,501,505)]
        errors += [self.error(503,{'Retry-After':'1'}),self.error(503,{'Retry-After':'999'}),self.error(503,{'Retry-After':'invalid'}),
            ssl.SSLError('private'),ssl.SSLCertVerificationError('private'),urllib.error.URLError(ssl.SSLError('private')),
            urllib.error.URLError('private'),PermissionError('private'),ValueError('private'),OSError('private')]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                x=self.remote(error,Response())
                with self.assertRaisesRegex(r.RetentionFailure,'^remote_read_failed$'):x.request(PATH)
                self.assertEqual(len(x.opener.calls),1)
    def test_second_denial_never_fallback(self):
        x=self.remote(ConnectionResetError(),self.error(403),Response())
        with self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertEqual(len(x.opener.calls),2)
    def test_nonimmutable_endpoints_keep_one_attempt(self):
        for path in ('','/git/ref/heads/main','/git/commits/'+'a'*40,'/git/blobs/'+'a'*40,PATH+'?recursive=1',PATH+'?recursive=0',PATH+'/',PATH.replace('/trees/','/trees/%2f'),'/git/trees/HEAD'):
            with self.subTest(path=path):
                x=self.remote(ConnectionResetError(),Response())
                with self.assertRaises(r.RetentionFailure):x.request(path)
                self.assertEqual(len(x.opener.calls),1)
    def test_body_errors_never_retry_conservative_charge(self):
        x=self.remote(Response(read_error=ConnectionResetError('private')),Response())
        with self.assertRaises(r.RetentionFailure):x.request(PATH,maximum=100)
        self.assertEqual(len(x.opener.calls),1);self.assertEqual(r._TREE_READ_METRICS['charged_bytes'],101)
        self.assertEqual(r._TREE_READ_METRICS['read_failures'],1)
    def test_bad_json_shape_or_oversize_not_retried(self):
        for data in (b'{"x":1,"x":2}',b'[]',b'invalid',b'x'*101):
            x=self.remote(Response(data),Response())
            with self.assertRaises(r.RetentionFailure):x.request(PATH,maximum=100)
            self.assertEqual(len(x.opener.calls),1)
    def test_deadline_exhaustion_no_second_attempt(self):
        for values in ([0,0,0,60],[0,0,0,61],[0,0,0,float('nan')],[0,0,0,-1]):
            x=self.remote(ConnectionResetError(),Response())
            with patch.object(r.time,'monotonic',side_effect=values),self.assertRaises(r.RetentionFailure):x.request(PATH)
            self.assertEqual(len(x.opener.calls),1)
    def test_remaining_budget_not_reset(self):
        x=self.remote(ConnectionResetError(),Response())
        with patch.object(r.time,'monotonic',side_effect=[0,0,1,20,21,30]):self.assertEqual(x.request(PATH),{})
        self.assertEqual([v[3] for v in x.opener.calls],[59,39])
    def test_existing_alarm_and_reentrant_denied_without_http(self):
        x=self.remote(Response())
        with patch.object(signal,'getitimer',return_value=(10.0,0.0)),self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertEqual(x.opener.calls,[])
        with r._TreeReadDeadline(),self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertEqual(x.opener.calls,[])
    def test_thread_denied_without_http(self):
        x=self.remote(Response());errors=[]
        def worker():
            try:x.request(PATH)
            except r.RetentionFailure:errors.append(True)
        t=threading.Thread(target=worker);t.start();t.join();self.assertEqual(errors,[True]);self.assertEqual(x.opener.calls,[])
    def test_setup_failures_restore_handler_no_http(self):
        old=signal.getsignal(signal.SIGALRM)
        x=self.remote(Response())
        with patch.object(signal,'setitimer',side_effect=OSError('private')),self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertEqual(signal.getsignal(signal.SIGALRM),old);self.assertEqual(x.opener.calls,[])
        with patch.object(signal,'signal',side_effect=OSError('private')),self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertEqual(x.opener.calls,[])
    def test_real_alarm_covers_slow_body_and_restores(self):
        old=signal.getsignal(signal.SIGALRM);real=signal.setitimer
        def shortened(which,seconds,*args):return real(which,0.03 if seconds>0 else seconds,*args)
        response=Response(delay=0.2);x=self.remote(response,Response());start=time.monotonic()
        with patch.object(signal,'setitimer',side_effect=shortened),self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertLess(time.monotonic()-start,0.18);self.assertEqual(len(x.opener.calls),1)
        self.assertTrue(response.closed);self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0));self.assertEqual(signal.getsignal(signal.SIGALRM),old)
    def test_no_exception_or_private_output(self):
        out=io.StringIO();err=io.StringIO();x=self.remote(PermissionError('synthetic-secret-private'))
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),self.assertRaisesRegex(r.RetentionFailure,'^remote_read_failed$'):x.request(PATH)
        self.assertEqual(out.getvalue()+err.getvalue(),'')

class Additional(unittest.TestCase):
    setUp=Tests.setUp
    remote=Tests.remote
    error=Tests.error
    def test_setup_elapsed_time_is_subtracted_before_arming(self):
        x=self.remote(Response())
        with patch.object(r.time,'monotonic',side_effect=[0,10,11,12]),patch.object(signal,'setitimer',wraps=signal.setitimer) as arm:
            self.assertEqual(x.request(PATH),{})
        self.assertEqual(arm.call_args_list[0].args,(signal.ITIMER_REAL,50))
        self.assertEqual(x.opener.calls[0][3],49)
    def test_nonfinite_start_and_missing_signal_zero_http(self):
        for start in (float('nan'),float('inf'),float('-inf')):
            x=self.remote(Response())
            with patch.object(r.time,'monotonic',return_value=start),self.assertRaises(r.RetentionFailure):x.request(PATH)
            self.assertEqual(x.opener.calls,[])
        x=self.remote(Response());old=signal.SIGALRM
        try:
            del signal.SIGALRM
            with self.assertRaises(r.RetentionFailure):x.request(PATH)
        finally:signal.SIGALRM=old
        self.assertEqual(x.opener.calls,[])
    def test_real_deadline_inside_slow_open_no_retry(self):
        x=self.remote(Response());real=signal.setitimer;old=signal.getsignal(signal.SIGALRM)
        def open_slow(request,timeout):time.sleep(0.2);return Response()
        x.opener.open=open_slow
        with patch.object(signal,'setitimer',side_effect=lambda w,s:real(w,0.03 if s>0 else s)),self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertEqual(r._TREE_RETRIES_USED,0);self.assertEqual(signal.getsignal(signal.SIGALRM),old);self.assertFalse(r._TREE_READ_LOCK.locked())
    def test_error_response_closed_before_second_denial(self):
        first=self.error(503);second=self.error(403);x=self.remote(first,second)
        with self.assertRaises(r.RetentionFailure):x.request(PATH)
        self.assertTrue(first.fp.closed);self.assertTrue(second.fp.closed)
    def test_all_exit_paths_restore_timer_handler_and_lock(self):
        old=signal.getsignal(signal.SIGALRM)
        class CloseError(Response):
            def __exit__(self,*args):raise ValueError('private close')
        for response in (Response(),CloseError(),Response(b'invalid')):
            x=self.remote(response)
            try:x.request(PATH)
            except r.RetentionFailure:pass
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0));self.assertEqual(signal.getsignal(signal.SIGALRM),old);self.assertFalse(r._TREE_READ_LOCK.locked())
