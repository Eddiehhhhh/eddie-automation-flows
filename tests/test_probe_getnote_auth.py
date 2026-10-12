import contextlib
import email.message
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import probe_getnote_auth as p

SECRET = 'SYNTHETIC-SECRET-DO-NOT-PRINT'
PRIVATE = 'SYNTHETIC-NOTE-CONTENT-DO-NOT-PRINT'


class Response(io.BytesIO):
    status = 200


class ProbeTests(unittest.TestCase):
    def invoke(self, payload=None, error=None, raw=None):
        opener = unittest.mock.Mock()
        response = Response(raw if raw is not None else json.dumps(payload).encode())
        opener.open.side_effect = error
        if error is None:
            opener.open.return_value = response
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'GETNOTE_API_KEY': SECRET, 'GETNOTE_CLIENT_ID': SECRET}), patch.object(p.urllib.request, 'build_opener', return_value=opener), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main()
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(SECRET, text)
        self.assertNotIn(PRIVATE, text)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, p.URL)
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
        return code, json.loads(out.getvalue())

    def test_success_exposes_only_count_and_safe_verdict(self):
        code, result = self.invoke({'success': True, 'data': {'notes': [{'content': PRIVATE, 'title': SECRET}]}})
        self.assertEqual(code, 0)
        self.assertEqual(result, {'service':'getnote','authentication':'accepted','category':'ok','http_status':200,'count':1})

    def test_empty_success_does_not_trigger_pagination(self):
        code, result = self.invoke({'success':True,'data':{'notes':[],'has_more':True,'cursor':SECRET}})
        self.assertEqual(code, 0)
        self.assertEqual(result['count'], 0)

    def test_http_failures_are_nonzero_and_never_print_body_or_exception(self):
        for status in (301,302,307,308,401,403,429,500,503):
            error = urllib.error.HTTPError(p.URL+'?private='+SECRET,status,PRIVATE,{},io.BytesIO(PRIVATE.encode()))
            code, result = self.invoke(error=error)
            self.assertEqual(code, 1)
            self.assertEqual(result['http_status'], status)
            self.assertNotEqual(result['authentication'],'accepted')

    def test_api_errors_do_not_echo_remote_messages(self):
        for api_code, expected in [(10001,'auth_rejected'),(10201,'membership_required'),(10202,'rate_limited'),(42900,'rate_limited'),(12345,'api_error')]:
            code,result=self.invoke({'success':False,'error':{'code':api_code,'message':PRIVATE,'reason':SECRET}})
            self.assertEqual(code,1);self.assertEqual(result['category'],expected)

    def test_unknown_shapes_and_exception_messages_are_suppressed(self):
        for payload in [None,[],{}, {'data':{'notes':[]}}, {'success':True,'data':{'notes':PRIVATE}}]:
            self.assertEqual(self.invoke(payload)[0],1)
        for error in [TimeoutError(SECRET),RuntimeError(PRIVATE),urllib.error.URLError(SECRET)]:
            self.assertEqual(self.invoke(error=error)[0],1)
        self.assertEqual(self.invoke(raw=SECRET.encode())[0],1)
        with patch.object(p,'MAX_RESPONSE_BYTES',10):
            self.assertEqual(self.invoke(raw=b'x'*11)[1]['category'],'response_too_large')

    def test_missing_configuration_performs_no_request(self):
        with patch.dict(os.environ,{},clear=True),patch.object(p.urllib.request,'build_opener') as opener:
            self.assertEqual(p.probe()['category'],'missing_configuration')
            opener.assert_not_called()

    def test_all_redirects_blocked_by_real_opener_without_destination_request(self):
        class FakeHTTPS(urllib.request.HTTPSHandler):
            def __init__(self, target):
                super().__init__();self.target=target;self.calls=[]
            def https_open(self, request):
                self.calls.append(request.full_url)
                headers=email.message.Message();headers['Location']=self.target
                response=urllib.response.addinfourl(io.BytesIO(PRIVATE.encode()), headers,request.full_url,302)
                response.msg='Found'
                return response
        for target in ['https://evil.invalid/?secret='+SECRET, p.URL+'/other']:
            handler=FakeHTTPS(target)
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),p.NoRedirect(),handler)
            req=urllib.request.Request(p.URL,headers={'Authorization':SECRET,'X-Client-ID':SECRET})
            with self.assertRaises(p.RedirectBlocked): opener.open(req,timeout=1)
            self.assertEqual(handler.calls,[p.URL])

    def test_hard_deadline_interrupts_slow_read_without_network(self):
        class SlowResponse(Response):
            def read(self,*args):
                time.sleep(.3)
                return b'{}'
        with patch.dict(os.environ,{'GETNOTE_API_KEY':SECRET,'GETNOTE_CLIENT_ID':SECRET}),patch.object(p,'DEADLINE_SECONDS',.02),patch.object(p.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=SlowResponse()
            started=time.monotonic();result=p.probe()
            self.assertEqual(result['category'],'timeout')
            self.assertLess(time.monotonic()-started,.2)
            self.assertEqual(opener.return_value.open.call_count,1)


if __name__ == '__main__': unittest.main()
