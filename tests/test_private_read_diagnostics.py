"""Sanitized production read diagnostics; original retry outcomes unchanged."""
import contextlib,http.client,io,json,ssl,unittest,urllib.error
from unittest.mock import patch
import get_private_preview_retention as r
import get_private_proof as proof
from test_private_tree_retry import Response,Open,PATH

class Tests(unittest.TestCase):
 def setUp(self):
  self.extra=patch.object(r,'_TREE_RETRIES_USED',0);self.extra.start();self.addCleanup(self.extra.stop)
  self.metrics=patch.dict(r._TREE_READ_METRICS,{k:0 for k in r._TREE_READ_METRICS});self.metrics.start();self.addCleanup(self.metrics.stop)
 def remote(self,*events):
  x=r.GitHubRemote.__new__(r.GitHubRemote);x.token='synthetic-private-secret';x.local=None;x.opener=Open(*events);return x
 def http(self,status):return urllib.error.HTTPError('https://invalid.invalid/private',status,'private',{},io.BytesIO(b'private body'))
 def failed(self,x,path=PATH,maximum=1024):
  with self.assertRaises(r.RetentionFailure):x.request(path,maximum=maximum)
  return x.read_diagnostic()
 def test_http_denials_fixed_status_no_retry(self):
  for status in (401,403,404,429):
   x=self.remote(self.http(status),Response());d=self.failed(x)
   self.assertEqual((d['phase'],d['error_class'],d['http_status']),('open','http',status));self.assertEqual(d['physical_attempts'],1);self.assertEqual(d['tree_extra_used'],0)
 def test_transient_twice_retains_final_class_and_global_count(self):
  x=self.remote(self.http(503),self.http(503));d=self.failed(x)
  self.assertEqual(d['http_status'],503);self.assertEqual(d['error_class'],'http');self.assertEqual(d['physical_attempts'],2);self.assertEqual(d['tree_extra_used'],1)
 def test_exhausted_global_budget_visible_without_extra_open(self):
  with patch.object(r,'_TREE_RETRIES_USED',8):
   x=self.remote(self.http(503),Response());d=self.failed(x)
   self.assertEqual(d['tree_extra_used'],8);self.assertEqual(d['tree_extra_limit'],8);self.assertEqual(d['physical_attempts'],1)
 def test_open_exception_classes_do_not_print_messages(self):
  cases=[(ssl.SSLError('private'),'tls'),(ssl.SSLCertVerificationError('private'),'certificate'),(urllib.error.URLError(ssl.SSLError('private')),'tls'),(urllib.error.URLError('private'),'url_error'),(PermissionError('private'),'other')]
  for error,kind in cases:
   x=self.remote(error,Response());d=self.failed(x);self.assertEqual(d['error_class'],kind);self.assertEqual(d['physical_attempts'],1)
 def test_timeout_and_connection_exhaustion(self):
  for error,kind in [(TimeoutError('private'),'timeout'),(ConnectionResetError('private'),'connection')]:
   x=self.remote(error,error);d=self.failed(x);self.assertEqual(d['error_class'],kind);self.assertEqual(d['physical_attempts'],2)
 def test_partial_body_bounded_charge_and_one_open(self):
  x=self.remote(Response(read_error=http.client.IncompleteRead(b'private',20)),Response());d=self.failed(x,maximum=40)
  self.assertEqual((d['phase'],d['error_class']),('read','incomplete_read'));self.assertEqual(d['failed_read_bound'],41);self.assertEqual(d['physical_attempts'],1)
 def test_close_exception_is_not_read_or_open(self):
  class BadClose(Response):
   def __exit__(self,*args):raise RuntimeError('private close data')
  x=self.remote(BadClose(),Response());d=self.failed(x);self.assertEqual((d['phase'],d['error_class']),('close','other'));self.assertEqual(d['physical_attempts'],1)
 def test_read_error_replaced_by_close_error_reports_close(self):
  class BadClose(Response):
   def __exit__(self,*args):raise RuntimeError('private close data')
  x=self.remote(BadClose(read_error=TimeoutError('private')));d=self.failed(x);self.assertEqual(d['phase'],'close')
 def test_fixed_code_close_exception_replaces_prior_read_error(self):
  class BadClose(Response):
   def __exit__(self,*args):raise r.RetentionFailure('remote_read_failed')
  x=self.remote(BadClose(read_error=TimeoutError('private')));d=self.failed(x)
  self.assertEqual((d['phase'],d['error_class']),('close','validation'));self.assertEqual(d['physical_attempts'],1)
 def test_json_shape_and_size_failures_are_validation_no_retry(self):
  for data in (b'invalid',b'[]',b'{"x":1,"x":2}',b'x'*41):
   x=self.remote(Response(data),Response());d=self.failed(x,maximum=40);self.assertEqual(d['phase'],'validation');self.assertEqual(d['physical_attempts'],1)
 def test_deadline_does_not_become_retryable_timeout(self):
  x=self.remote(ConnectionResetError(),Response())
  with patch.object(r.time,'monotonic',side_effect=[0,0,0,60]):d=self.failed(x)
  self.assertEqual((d['phase'],d['error_class']),('deadline','deadline'));self.assertEqual(d['physical_attempts'],1)
 def test_no_commit_or_recursive_retry_added(self):
  for path in ('/git/commits/'+'a'*40,PATH+'?recursive=1'):
   x=self.remote(self.http(503),Response());d=self.failed(x,path)
   self.assertEqual((d['phase'],d['error_class']),('open','http'));self.assertEqual(d['physical_attempts'],1);self.assertEqual(d['tree_physical_attempts'],0)
 def test_logical_and_physical_counters_are_distinct(self):
  x=self.remote(self.http(503),Response(),self.http(403));self.assertEqual(x.request(PATH),{});d=self.failed(x)
  self.assertEqual(d['logical_requests'],2);self.assertEqual(d['physical_attempts'],3);self.assertEqual(d['tree_extra_used'],1)
 def test_success_clears_prior_failure_and_missing404_is_not_failure(self):
  x=self.remote(self.http(403),Response());self.failed(x);self.assertEqual(x.request(PATH),{});self.assertIsNone(x.read_diagnostic())
  x=self.remote(self.http(404));self.assertIsNone(x.request('/git/ref/heads/main',missing=True));self.assertIsNone(x.read_diagnostic())
 def test_only_fixed_safe_scalars_are_printed(self):
  x=self.remote(self.http(403));self.failed(x);resolver=proof.PrivateRecords(x);resolver._diagnostic={'stage':'tree','reason':'transport'}
  text=io.StringIO()
  with contextlib.redirect_stderr(text):resolver._report_failure()
  rows=[json.loads(v)for v in text.getvalue().splitlines()];self.assertEqual(len(rows),2)
  self.assertEqual(rows[0],{'contract':'get-private-proof-diagnostic-1','stage':'tree','reason':'transport'})
  self.assertEqual(rows[1]['contract'],'get-private-transport-diagnostic-1')
  for marker in ('synthetic-private-secret','invalid.invalid','private body','a'*40,'Authorization'):
   self.assertNotIn(marker,text.getvalue())
 def test_unknown_fields_or_invalid_counters_not_printed(self):
  for change in ({'url':'private'},{'error_class':'private'},{'phase':'private'},{'physical_attempts':True},{'tree_extra_used':9}):
   x=self.remote(self.http(403));self.failed(x);detail=x.read_diagnostic();detail.update(change);x.read_diagnostic=lambda:detail
   resolver=proof.PrivateRecords(x);text=io.StringIO()
   with contextlib.redirect_stderr(text):resolver._report_failure()
   self.assertEqual(len(text.getvalue().splitlines()),1)
 def test_diagnostic_failure_never_changes_rejection(self):
  x=self.remote(self.http(403));self.failed(x);x.read_diagnostic=lambda:(_ for _ in ()).throw(ValueError('private'));resolver=proof.PrivateRecords(x)
  with contextlib.redirect_stderr(io.StringIO()):resolver._report_failure()

if __name__=='__main__':unittest.main()
