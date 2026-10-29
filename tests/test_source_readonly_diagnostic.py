import contextlib
import hashlib
import http.client
import io
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_source_readonly_diagnostic as d

SECRET = 'SYNTHETIC-SECRET-NOT-A-CREDENTIAL'
OID = 'a' * 40


class Response:
    status = 200
    headers = {}
    def __init__(self, data=b'{}', read_error=None, close_error=None):
        self.data = data; self.read_error = read_error; self.close_error = close_error
        self.closed = 0
    def __enter__(self): return self
    def __exit__(self, *args):
        self.closed += 1
        if self.close_error: raise self.close_error
    def read(self, size):
        if self.read_error: raise self.read_error
        return self.data[:size]


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response or Response(); self.error = error; self.calls = 0
    def open(self, request, timeout):
        self.calls += 1
        if self.error: raise self.error
        return self.response


class Tests(unittest.TestCase):
    def setUp(self):
        self.network = []
        def deny(*args, **kwargs): self.network.append(True); raise AssertionError('network forbidden')
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=deny))
        self.stack.enter_context(patch.object(socket, 'getaddrinfo', side_effect=deny))
    def tearDown(self):
        self.stack.close(); self.assertEqual(self.network, [])
    def request(self, path='/git/commits/' + OID, **kwargs):
        return urllib.request.Request(d.PRIVATE_API + path, **kwargs)
    def observed(self, response=None, error=None, channel='private'):
        raw = Opener(response, error); obs = d.Observations()
        return d.ReadOnlyOpener(raw, obs, channel), raw, obs
    def test_private_read_paths(self):
        for path in ('', '/git/ref/heads/main', '/git/commits/'+OID, '/git/trees/'+OID,
                     '/git/trees/'+OID+'?recursive=1', '/git/blobs/'+OID):
            with self.subTest(path=path): self.assertIn(d.endpoint(self.request(path), 'private'), d.STAGES)
    def test_private_unknown_paths_rejected(self):
        for path in ('/git/refs', '/git/ref/heads/other', '/git/commits/main', '/git/commits/'+OID+'?x=1',
                     '/git/trees/'+OID+'?recursive=0', '/git/trees/'+OID+'?recursive=1&x=1',
                     '/git/commits/%61'+OID[1:], 'evil/git/commits/'+OID, '/contents/a'):
            with self.subTest(path=path), self.assertRaises(d.BoundaryError): d.endpoint(self.request(path), 'private')
    def test_all_mutations_and_get_body_denied_before_transport(self):
        for method in ('POST', 'PATCH', 'PUT', 'DELETE', 'HEAD', 'OPTIONS'):
            wrapped, raw, _ = self.observed()
            with self.assertRaises(d.BoundaryError): wrapped.open(self.request(method=method), timeout=60)
            self.assertEqual(raw.calls, 0)
        wrapped, raw, _ = self.observed()
        with self.assertRaises(d.BoundaryError): wrapped.open(self.request(data=b'private', method='GET'), timeout=60)
        self.assertEqual(raw.calls, 0)
    def test_public_fixed_workflow_metadata_is_allowed(self):
        self.assertEqual(d.endpoint(urllib.request.Request(d.PUBLIC_API+'/actions/workflows/387242896'), 'public'), 'source')
    def test_public_original_api_sequences(self):
        for suffix in ('', '/actions/workflows/387242896/runs?per_page=30&page=3', '/actions/runs/123',
                       '/actions/runs/123/attempts/1', '/actions/runs/123/attempts/1/jobs?per_page=100&page=1',
                       '/actions/jobs/456/logs', '/contents/.github/workflows/get-canonical.yml?ref='+OID):
            with self.subTest(suffix=suffix): self.assertEqual(d.endpoint(urllib.request.Request(d.PUBLIC_API+suffix), 'public'), 'source')
    def test_public_unknown_api_ref_or_pagination_rejected(self):
        for suffix in ('/actions/workflows/2', '/actions/workflows/387242896/runs?per_page=100&page=1',
                       '/actions/runs/1/attempts/1/jobs?per_page=100&page=2', '/contents/private?ref='+OID,
                       '/contents/.github/workflows/get-canonical.yml?ref=main'):
            with self.assertRaises(d.BoundaryError): d.endpoint(urllib.request.Request(d.PUBLIC_API+suffix), 'public')
    def test_cdn_no_credentials_and_no_url_confusion(self):
        good='https://fixture.actions.githubusercontent.com/logs/private-query?sig=opaque'
        self.assertEqual(d.endpoint(urllib.request.Request(good), 'public'), 'source')
        for header in ('Authorization', 'authorization', 'Cookie', 'Proxy-Authorization'):
            with self.assertRaises(d.BoundaryError): d.endpoint(urllib.request.Request(good, headers={header:SECRET}), 'public')
        for url in ('http://fixture.actions.githubusercontent.com/a', 'https://evil.example/a',
                    'https://u@fixture.actions.githubusercontent.com/a', 'https://fixture.actions.githubusercontent.com:444/a',
                    'https://fixture.actions.githubusercontent.com/a#fragment'):
            with self.assertRaises(d.BoundaryError): d.endpoint(urllib.request.Request(url), 'public')
    def test_timeouts_fail_before_http(self):
        for timeout in (0, -1, 61, float('nan'), float('inf'), True, '60'):
            wrapped, raw, _ = self.observed()
            with self.assertRaises(d.BoundaryError): wrapped.open(self.request(), timeout=timeout)
            self.assertEqual(raw.calls, 0)
    def test_success_response_bytes_and_cleanup(self):
        response=Response(b'abc');wrapped,raw,obs=self.observed(response)
        with wrapped.open(self.request(), timeout=60) as value:
            self.assertEqual(value.read(10),b'abc');self.assertEqual(value.status,200)
        self.assertEqual(response.closed,1);self.assertEqual(raw.calls,1)
        self.assertEqual(obs.counters['private_read_bytes'],3)
    def test_http_status_classes_do_not_retry(self):
        for status in (301,302,401,403,404,429,500,502,503,504):
            error=urllib.error.HTTPError('https://private.invalid/'+OID,status,SECRET,{'X-RateLimit-Reset':'1700000000','Private':SECRET},io.BytesIO(b'body'))
            wrapped,raw,obs=self.observed(error=error)
            with self.assertRaises(urllib.error.HTTPError) as caught:wrapped.open(self.request(),timeout=60)
            self.assertIs(caught.exception,error);self.assertEqual(raw.calls,1)
            self.assertEqual(obs.failure['http_status'],status);self.assertEqual(obs.failure['phase'],'open')
            error.close()
    def test_open_timeout_tls_and_url_reason_classes(self):
        for error,label in ((TimeoutError(SECRET),'timeout_error'),(ssl.SSLError(SECRET),'tls_error'),
                            (urllib.error.URLError(ssl.SSLError(SECRET)),'tls_error'),
                            (urllib.error.URLError(ConnectionResetError(SECRET)),'url_error')):
            wrapped,raw,obs=self.observed(error=error)
            with self.assertRaises(type(error)):wrapped.open(self.request(),timeout=60)
            self.assertEqual(raw.calls,1);self.assertEqual(obs.failure['error_class'],label)
    def test_partial_read_is_not_retried_and_bound_is_counted(self):
        error=http.client.IncompleteRead(SECRET.encode(),100)
        wrapped,raw,obs=self.observed(Response(read_error=error))
        with self.assertRaises(http.client.IncompleteRead):
            with wrapped.open(self.request(),timeout=60) as value:value.read(4097)
        self.assertEqual(raw.calls,1);self.assertEqual(obs.failure['phase'],'read')
        self.assertEqual(obs.counters['private_failed_read_bound'],4097)
        self.assertNotIn(SECRET,json.dumps(obs.failure))
    def test_close_failure_preserved(self):
        wrapped,raw,obs=self.observed(Response(close_error=OSError(SECRET)))
        with self.assertRaises(OSError):
            with wrapped.open(self.request(),timeout=60) as value:value.read(10)
        self.assertEqual(obs.failure['phase'],'close');self.assertEqual(raw.calls,1)
    def test_http_error_read_and_close_remain_same_error_type(self):
        class Body:
            def read(self,size):raise TimeoutError(SECRET)
            def close(self):raise OSError(SECRET)
        error=urllib.error.HTTPError('https://private.invalid/',503,SECRET,{},Body())
        wrapped,raw,obs=self.observed(error=error)
        with self.assertRaises(urllib.error.HTTPError) as caught:wrapped.open(self.request(),timeout=60)
        with self.assertRaises(TimeoutError):caught.exception.read(17)
        self.assertEqual(obs.failure['phase'],'read')
        with self.assertRaises(OSError):caught.exception.close()
        self.assertEqual(obs.failure['phase'],'close');self.assertEqual(raw.calls,1)
    def test_counter_overflow_rejects(self):
        obs=d.Observations();obs.counters['private_attempts']=d.MAX_COUNTER
        with self.assertRaises(d.BoundaryError):obs.add('private_attempts',1)
    def test_no_mutation_method_exposed(self):
        remote=d.ReadOnlyRemote(NS(cas_main=lambda *a:None,create_only=lambda *a:None))
        for name in ('cas_main','request','create_only','create_blob','create_commit','update_ref'):
            self.assertFalse(hasattr(remote,name))
        with self.assertRaises(d.BoundaryError):remote.ref('refs/heads/other')
    def test_policy_exact_public_digest(self):
        self.assertEqual(hashlib.sha256(d.encoded(d.POLICY)).hexdigest(),d.POLICY_SHA)
        self.assertEqual(d.POLICY['code_sha'],d.RUNTIME_SHA)
    def test_source_once_uses_original_caps_and_reserved_capacity(self):
        observed=[]
        class Client:
            def __init__(self,token):self.requests=0;self.bytes=0;self.opener=Opener()
        class Records:
            def __init__(self,remote):self.nodes=[];self.bytes=0
        def discover(client,policy,**kw):
            observed.append((client.requests,kw));client.requests=16
            return {'status':'public_provenance_capacity_verified'}
        pub=NS(MAX_REQUESTS=40,PublicGithub=Client,validate_policy=lambda p,h:p,discover=discover)
        pp=NS(MAX_COMMITS=1024,MAX_PRIVATE_BYTES=256*1024*1024,PrivateRecords=Records)
        obs=d.Observations();result=d.source_once(pub,pp,NS(),SECRET,obs)
        self.assertEqual(observed,[(3,{'reserve_next_run':True})]);self.assertEqual(result['public_budget_used'],16)
        pub.MAX_REQUESTS=41
        with self.assertRaises(d.BoundaryError):d.source_once(pub,pp,NS(),SECRET,obs)
    def test_source_failure_preserves_error_and_counts(self):
        class Client:
            def __init__(self,token):self.requests=0;self.opener=Opener()
        class Records:
            def __init__(self,remote):self.nodes=[1,2];self.bytes=17
        def discover(client,*args,**kw):client.requests=14;raise ValueError(SECRET)
        pub=NS(MAX_REQUESTS=40,PublicGithub=Client,validate_policy=lambda p,h:p,discover=discover)
        pp=NS(MAX_COMMITS=1024,MAX_PRIVATE_BYTES=256*1024*1024,PrivateRecords=Records)
        obs=d.Observations()
        with self.assertRaises(ValueError):d.source_once(pub,pp,NS(),SECRET,obs)
        self.assertEqual(obs.source_counts['public_budget_used'],14);self.assertEqual(obs.source_counts['private_nodes'],2)
    def test_classification_never_includes_error_text_or_headers(self):
        e=urllib.error.HTTPError('https://private.invalid/'+OID,403,SECRET,{'X-RateLimit-Reset':'nan','Secret':SECRET},io.BytesIO(SECRET.encode()))
        result=json.dumps(d.classify(e));self.assertNotIn(SECRET,result);self.assertNotIn(OID,result);self.assertNotIn('https:',result)
        self.assertIsNone(d.classify(e)['rate_limit_reset_epoch']);e.close()
    def test_wrong_context_denies_before_runtime_and_token(self):
        for env in ({},{'GITHUB_EVENT_NAME':'push'},{'NOTION_API_KEY':SECRET}):
            stream=io.StringIO()
            with patch.dict(os.environ,env,clear=True),patch.object(d,'load_runtime') as load,contextlib.redirect_stderr(stream):
                self.assertEqual(d.main([]),1);load.assert_not_called()
            self.assertNotIn(SECRET,stream.getvalue())
    def test_output_failure_never_success(self):
        with patch.dict(os.environ,{},clear=True),patch('builtins.print',side_effect=OSError(SECRET)):
            self.assertEqual(d.main([]),1)
    def test_no_runtime_writer_entry_or_notion_secret_in_workflow(self):
        path=Path(__file__).resolve().parents[1]/'.github/workflows/cloud-readonly-acceptance.yml'
        text=path.read_text();section=text.split('  source-diagnostic:',1)[1]
        self.assertIn('contents: read',text);self.assertIn('ref: '+d.RUNTIME_SHA,section)
        self.assertIn("inputs.source_diagnostic_only == true && inputs.diagnostic_only != true",section)
        self.assertNotIn('NOTION_API_KEY',section);self.assertNotIn('upload-artifact',section)
        self.assertNotIn('get_notion_runtime.py',section);self.assertNotIn('get_notion_controller_route.py',section)
        self.assertEqual(section.count('persist-credentials: false'),2)
        self.assertLess(section.index('without credentials'),section.index('PRIVATE_VAULT_TOKEN:'))

    def test_runtime_pin_cleanliness_and_module_origin(self):
        good=lambda b:NS(returncode=0,stdout=b)
        for values,passes in (([good((d.RUNTIME_SHA+'\n').encode()),good(b'')],True),
                              ([good(b'wrong\n')],False),
                              ([good((d.RUNTIME_SHA+'\n').encode()),good(b' M scripts/x\n')],False)):
            with patch.object(Path,'is_dir',return_value=True),patch.object(Path,'is_symlink',return_value=False),patch.object(d.subprocess,'run',side_effect=values):
                if passes: runtime=d.load_runtime()
                else:
                    with self.assertRaises(d.BoundaryError):d.load_runtime()
        with patch.object(d.importlib,'import_module',return_value=NS(__file__='/tmp/untrusted.py')):
            with self.assertRaises(d.BoundaryError):d.import_original(runtime,'untrusted')
        with patch.object(Path,'is_symlink',return_value=True),patch.object(d.subprocess,'run') as run:
            with self.assertRaises(d.BoundaryError):d.load_runtime()
            run.assert_not_called()

    def test_enter_error_class_and_no_retry(self):
        response=Response()
        class BadResponse(Response):
            def __enter__(self):raise OSError(SECRET)
        guard,raw,obs=self.observed(BadResponse())
        with self.assertRaises(OSError):
            with guard.open(self.request(),timeout=60):pass
        self.assertEqual(raw.calls,1);self.assertEqual(obs.failure['phase'],'open')

    def test_main_original_bootstrap_and_local_cleanup(self):
        # Original loader and transport, five synthetic GET envelopes only.
        # Pure source client here is separate from the full retained-source
        # integration probe. No private fixture or credential is in this test.
        root=Path(__file__).resolve().parents[1]
        original=root/'runtime' if (root/'runtime/scripts/get_private_runtime_bindings.py').is_file() else root
        spec=importlib.util.spec_from_file_location('synthetic_original_bindings',original/'scripts/get_private_runtime_bindings.py')
        bindings=importlib.util.module_from_spec(spec);spec.loader.exec_module(bindings)
        import get_private_preview_retention as transport
        payload=bindings.encoded({'contract':bindings.CONTRACT,'modules':{'get_synthetic':{'VALUE':{'type':'scalar','value':7}}}})
        digest=hashlib.sha256(payload).hexdigest();blob=hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()
        import base64
        commit,tree='b'*40,'c'*40
        path='.sync/get-notion/runtime-bindings-'+digest+'.json'
        objects={
            '/git/ref/heads/main':{'ref':'refs/heads/main','object':{'type':'commit','sha':commit}},
            '':{'full_name':bindings.PRIVATE_REPOSITORY,'private':True},
            '/git/commits/'+commit:{'sha':commit,'tree':{'sha':tree},'parents':[]},
            '/git/trees/'+tree+'?recursive=1':{'sha':tree,'truncated':False,'tree':[{'path':path,'type':'blob','mode':'100644','size':len(payload),'sha':blob}]},
            '/git/blobs/'+blob:{'sha':blob,'size':len(payload),'encoding':'base64','content':base64.b64encode(payload).decode()}}
        calls=[];paths=[]
        class OriginalFixture:
            def open(self,request,timeout):
                suffix=request.full_url[len(d.PRIVATE_API):];calls.append(suffix)
                return Response(json.dumps(objects[suffix]).encode())
        class Client:
            def __init__(self,token):self.requests=0;self.opener=Opener()
        class Records:
            def __init__(self,remote):self.nodes=[];self.bytes=0
        def discover(client,policy,**kw):
            p=Path(os.environ[bindings.ENV_FILE]);paths.append(p)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
            self.assertEqual(bindings.read_local(p,digest)['get_synthetic']['VALUE'],7)
            return {'status':'public_provenance_capacity_verified'}
        pub=NS(MAX_REQUESTS=40,PublicGithub=Client,validate_policy=lambda p,h:p,discover=discover)
        pp=NS(MAX_COMMITS=1024,MAX_PRIVATE_BYTES=256*1024*1024,PrivateRecords=Records)
        modules={'get_private_runtime_bindings':bindings,'get_private_preview_retention':transport,
                 'get_publication_provenance':pub,'get_private_proof':pp}
        env={'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':d.REPAIR_REF,'GITHUB_REPOSITORY':d.PUBLIC_REPOSITORY,
             'GET_SOURCE_PROOF_DIAGNOSTIC_ONLY':'true','APPLY':'false','NOTION_MODE':'canonical',
             'PRIVATE_VAULT_TOKEN':SECRET,'GITHUB_TOKEN':SECRET}
        output=io.StringIO();stdout=io.StringIO()
        with patch.dict(os.environ,env,clear=True),patch.object(d,'load_runtime',return_value=original), \
             patch.object(d,'import_original',side_effect=lambda runtime,name:modules[name]), \
             patch.object(bindings,'TRUSTED_BUNDLE_SHA256',digest),patch.object(bindings,'PRIVATE_PATH',path), \
             patch.object(transport.urllib.request,'build_opener',return_value=OriginalFixture()), \
             contextlib.redirect_stderr(output),contextlib.redirect_stdout(stdout):
            self.assertEqual(d.main([]),0);self.assertNotIn(bindings.ENV_FILE,os.environ)
        self.assertEqual(len(calls),5);self.assertFalse(paths[0].exists());self.assertFalse(paths[0].parent.parent.exists())
        self.assertEqual(stdout.getvalue(),'');value=json.loads(output.getvalue())
        self.assertTrue(value['source_discovery_pass']);self.assertFalse(value['production_migration_claimed'])
        self.assertEqual(value['private_attempts'],5);self.assertNotIn(SECRET,output.getvalue());self.assertNotIn(blob,output.getvalue())

    def test_all_context_gates_deny_before_runtime(self):
        env={'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':d.REPAIR_REF,'GITHUB_REPOSITORY':d.PUBLIC_REPOSITORY,
             'GET_SOURCE_PROOF_DIAGNOSTIC_ONLY':'true','APPLY':'false','NOTION_MODE':'canonical'}
        for key,value in (('GITHUB_EVENT_NAME','push'),('GITHUB_REF','refs/heads/main'),('GITHUB_REPOSITORY','other/repo'),
                          ('GET_SOURCE_PROOF_DIAGNOSTIC_ONLY','false'),('GET_BINDINGS_DIAGNOSTIC_ONLY','true'),
                          ('APPLY','true'),('NOTION_MODE','historical-apply'),('NOTION_INPUT_SHA256','a'*64),
                          ('GET_PRIVATE_BINDINGS_FILE','/tmp/other'),('NOTION_API_KEY',SECRET)):
            with patch.dict(os.environ,{**env,key:value},clear=True),patch.object(d,'load_runtime') as load,contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(d.main([]),1);load.assert_not_called()


if __name__=='__main__': unittest.main()
