"""Synthetic-only privacy, dispatch, immutable-runtime and no-write checks."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
# Diagnostic CI has the separately pinned checkout. Ordinary PR CI has the
# reviewed capsule-pin-only successor directly in this branch. Both loader
# identities are checked byte-for-byte below; no pin normalization is allowed.
ORIGINAL = ROOT / 'runtime' if (ROOT / 'runtime/scripts/get_private_runtime_bindings.py').is_file() else ROOT
SPEC = importlib.util.spec_from_file_location('diagnostic', ROOT / 'scripts/get_bindings_readonly_diagnostic.py')
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)
SENTINEL = 'synthetic-private-secret-body-url-do-not-emit'
ENV = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': d.REPAIR_REF,
       'GITHUB_REPOSITORY': d.PUBLIC_REPOSITORY, 'GET_BINDINGS_DIAGNOSTIC_ONLY': 'true',
       'APPLY': 'false', 'NOTION_MODE': 'canonical', 'NOTION_INPUT_SHA256': '',
       'PRIVATE_VAULT_TOKEN': SENTINEL}


class NoBody(io.BytesIO):
    def read(self, *args):
        raise AssertionError('response body must not be read by observer')


class DiagnosticTests(unittest.TestCase):
    def error(self, code=403, headers=None):
        return urllib.error.HTTPError('https://invalid.example/' + SENTINEL, code,
                                      SENTINEL, headers or {}, NoBody(SENTINEL.encode()))

    def test_http_classes_and_no_private_text(self):
        for code in (401, 403, 404, 429, 500):
            with self.subTest(code=code):
                error = self.error(code, {'X-RateLimit-Reset': '1790000000', 'Other': SENTINEL})
                value = d.failure_diagnostic(error)
                self.assertEqual(value, {'contract': d.CONTRACT, 'error_class': 'http_error',
                    'http_status': code, 'rate_limit_reset_epoch': 1790000000})
                self.assertNotIn(SENTINEL, json.dumps(value))
                self.assertFalse(error.fp.closed)
                error.close()

    def test_only_allowlisted_header_is_requested(self):
        class Headers:
            def get(self, key):
                self.assert_key = key
                if key != 'X-RateLimit-Reset': raise AssertionError('other header')
                return '123'
        h = Headers(); value = d.failure_diagnostic(self.error(headers=h))
        self.assertEqual(h.assert_key, 'X-RateLimit-Reset')
        self.assertEqual(value['rate_limit_reset_epoch'], 123)

    def test_invalid_reset_values(self):
        for reset in ('-1', '+1', '1.0', ' 1', '１', SENTINEL, '4102444801', '12345678901', 123, True):
            with self.subTest(reset=reset):
                self.assertIsNone(d.failure_diagnostic(self.error(headers={'X-RateLimit-Reset': reset}))['rate_limit_reset_epoch'])

    def test_invalid_status_types(self):
        for code in (True, '403', 99, 600, None):
            self.assertIsNone(d.failure_diagnostic(self.error(code))['http_status'])

    def test_exception_context_is_bounded_and_cycle_safe(self):
        top = ValueError(SENTINEL); top.__context__ = top
        self.assertEqual(d.failure_diagnostic(top)['error_class'], 'value_error')
        error = self.error(429)
        for _ in range(8):
            value = ValueError(SENTINEL); value.__context__ = error; error = value
        self.assertIsNone(d.failure_diagnostic(error)['http_status'])

    def test_known_non_http_classes(self):
        for error, label in ((TimeoutError(SENTINEL), 'timeout_error'),
                             (PermissionError(SENTINEL), 'permission_error'),
                             (FileExistsError(SENTINEL), 'file_exists_error'),
                             (urllib.error.URLError(SENTINEL), 'url_error')):
            self.assertEqual(d.failure_diagnostic(error)['error_class'], label)


class ReadBoundaryTests(unittest.TestCase):
    def request(self, suffix='', **kwargs):
        return urllib.request.Request(d.PRIVATE_API + suffix, **kwargs)

    def test_exact_five_read_families_and_budget(self):
        opener = Mock(); guard = d.ReadOnlyOpener(opener)
        for suffix in ('/git/ref/heads/main', '', '/git/commits/'+'a'*40,
                       '/git/trees/'+'b'*40+'?recursive=1', '/git/blobs/'+'c'*40):
            req = self.request(suffix); guard.open(req, timeout=60)
            opener.open.assert_called_with(req, timeout=60)
        with self.assertRaises(d.BoundaryError): guard.open(self.request(), timeout=60)
        self.assertEqual(opener.open.call_count, 5)

    def test_writes_and_post_data_are_denied_before_transport(self):
        opener = Mock(); guard = d.ReadOnlyOpener(opener)
        for method in ('POST', 'PATCH', 'PUT', 'DELETE', 'HEAD'):
            with self.assertRaises(d.BoundaryError):
                guard.open(self.request(method=method), timeout=60)
        with self.assertRaises(d.BoundaryError):
            guard.open(self.request(method='GET', data=b'forbidden'), timeout=60)
        opener.open.assert_not_called()

    def test_wrong_urls_refs_queries_and_timeout_denied(self):
        opener = Mock(); guard = d.ReadOnlyOpener(opener)
        for url in ('https://api.notion.com/v1/pages', d.PRIVATE_API+'/contents/secret',
                    d.PRIVATE_API+'/git/ref/heads/other', d.PRIVATE_API+'.evil',
                    d.PRIVATE_API+'/git/trees/'+'a'*40, d.PRIVATE_API+'?x=1',
                    'http://api.github.com/repos/Eddiehhhhh/eddie-llm-wiki'):
            with self.assertRaises(d.BoundaryError):
                guard.open(urllib.request.Request(url), timeout=60)
        with self.assertRaises(d.BoundaryError): guard.open(self.request(), timeout=5)
        opener.open.assert_not_called()

    def test_observer_reraises_exact_error_once_without_reading_body(self):
        error = urllib.error.HTTPError('https://invalid.example',404,SENTINEL,{},NoBody())
        opener = Mock(); opener.open.side_effect = error; guard = d.ReadOnlyOpener(opener)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            guard.open(self.request('/git/ref/heads/main'), timeout=60)
        self.assertIs(caught.exception, error); self.assertEqual(opener.open.call_count,1)
        self.assertEqual(guard.failure['http_status'],404); error.close()

    def test_facade_has_no_mutation_methods(self):
        remote = Mock(); facade = d.ReadOnlyRemote(remote)
        self.assertFalse(hasattr(facade, 'create_only'))
        self.assertFalse(hasattr(facade, 'request'))
        with self.assertRaises(d.BoundaryError): facade.ref('refs/heads/other')
        with self.assertRaises(d.BoundaryError): facade.tree_entries('a'*40, False)
        self.assertEqual(remote.mock_calls, [])
        facade.ref('refs/heads/main'); facade.metadata(); facade.commit('a'*40)
        facade.tree_entries('b'*40, True); facade.blob('c'*40, 2)
        self.assertEqual(len(remote.mock_calls),5)


class MainTests(unittest.TestCase):
    def modules(self):
        return Mock(), Mock(), Mock()

    def invoke(self, modules, env=None, argv=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, ENV if env is None else env, clear=True), \
             patch.object(d, 'load_runtime', return_value=modules), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = d.main([] if argv is None else argv)
        return result, out.getvalue(), err.getvalue()

    def test_success_original_bootstrap_only_private_temp_cleanup(self):
        bindings, route, transport = self.modules(); paths=[]
        def bootstrap(remote, path):
            self.assertIsInstance(remote, d.ReadOnlyRemote)
            self.assertFalse(hasattr(remote, 'create_only')); paths.append(path)
            path.parent.mkdir(mode=0o700); path.write_text(SENTINEL)
            self.assertEqual(path.parent.parent.stat().st_mode & 0o777,0o700)
        bindings.bootstrap.side_effect=bootstrap
        result,out,err=self.invoke((bindings,route,transport))
        self.assertEqual(result,0); self.assertEqual(out,'')
        self.assertEqual(json.loads(err)['error_class'],'none')
        self.assertNotIn(SENTINEL,err); self.assertFalse(paths[0].exists())
        route.selection.assert_called_once_with()
        transport.GitHubRemote.assert_called_once_with(None,SENTINEL)
        bindings.bootstrap.assert_called_once()

    def test_route_failure_occurs_before_token_transport(self):
        bindings, route, transport=self.modules(); route.selection.side_effect=ValueError(SENTINEL)
        result,out,err=self.invoke((bindings,route,transport))
        self.assertEqual(result,1); transport.GitHubRemote.assert_not_called()
        bindings.bootstrap.assert_not_called(); self.assertNotIn(SENTINEL,out+err)

    def test_all_dispatch_gates_fail_closed(self):
        for key,value in (('GITHUB_EVENT_NAME','push'),('GITHUB_REF','refs/heads/main'),
                          ('GITHUB_REPOSITORY','other/repo'),('GET_BINDINGS_DIAGNOSTIC_ONLY','false'),
                          ('APPLY','true'),('NOTION_MODE','historical-apply'),('NOTION_INPUT_SHA256','a'*64)):
            env=dict(ENV); env[key]=value; modules=self.modules()
            result,_,_=self.invoke(modules,env); self.assertEqual(result,1)
            modules[2].GitHubRemote.assert_not_called()
        modules=self.modules(); self.assertEqual(self.invoke(modules,argv=['--arbitrary'])[0],1)
        modules[2].GitHubRemote.assert_not_called()

    def test_missing_secret_makes_no_request(self):
        env=dict(ENV); del env['PRIVATE_VAULT_TOKEN']; modules=self.modules()
        self.assertEqual(self.invoke(modules,env)[0],1); modules[2].GitHubRemote.assert_not_called()

    def test_http_failure_and_temp_are_not_leaked(self):
        modules=self.modules(); modules[0].bootstrap.side_effect=urllib.error.HTTPError(
            'https://invalid.example/'+SENTINEL,403,SENTINEL,{},NoBody())
        result,out,err=self.invoke(modules)
        self.assertEqual(result,1);self.assertEqual(out,'');self.assertNotIn(SENTINEL,err)
        self.assertEqual(json.loads(err)['http_status'],403)


class StaticContractTests(unittest.TestCase):
    def test_default_workflow_is_byte_identical_after_removing_only_new_gate(self):
        workflow=(ROOT/'.github/workflows/cloud-readonly-acceptance.yml').read_text()
        normal=workflow.split('  bindings-diagnostic:\n')[0]
        start=normal.index('    inputs:\n',normal.index('  workflow_dispatch:\n'))
        end=normal.index('permissions:\n',start)
        normal=normal[:start]+normal[end:]
        normal=normal.replace('    if: inputs.diagnostic_only != true\n','')
        self.assertEqual(hashlib.sha256(normal.encode()).hexdigest(),
                         '69ed7fa41b829e3ebc90eb726f0fc96f99d518284ac682f30080631bb41a30a4')

    def test_workflow_default_normal_behavior_and_diagnostic_isolation(self):
        workflow=(ROOT/'.github/workflows/cloud-readonly-acceptance.yml').read_text()
        self.assertIn('default: false',workflow)
        before,diagnostic=workflow.split('  bindings-diagnostic:\n')
        self.assertIn('    if: inputs.diagnostic_only != true\n',before)
        for command in ('python scripts/cloud_private_checkout.py --destination vault',
                        'python scripts/cloud_acceptance.py --vault vault --output /tmp/cloud-acceptance.json'):
            self.assertIn(command,before);self.assertNotIn(command,diagnostic)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.diagnostic_only == true",diagnostic)
        self.assertIn("github.ref == '"+d.REPAIR_REF+"'",diagnostic)
        self.assertIn('ref: '+d.RUNTIME_SHA,diagnostic)
        self.assertEqual(diagnostic.count('persist-credentials: false'),2)
        self.assertEqual(diagnostic.count('secrets.'),1)
        self.assertIn('secrets.PRIVATE_VAULT_TOKEN',diagnostic)
        self.assertIn("APPLY: 'false'",diagnostic)
        for forbidden in ('upload-artifact','GITHUB_STEP_SUMMARY','NOTION_TOKEN','GETNOTE_',
                          'get_recurring_entry','get_notion_executor','get_canonical_manual','curl ','git push'):
            self.assertNotIn(forbidden,diagnostic)
        self.assertIn('permissions:\n  contents: read\n',workflow)

    def test_script_has_no_writer_imports_or_dynamic_dispatch(self):
        source=(ROOT/'scripts/get_bindings_readonly_diagnostic.py').read_text(); tree=ast.parse(source)
        for forbidden in ('get_recurring_entry','get_notion_executor','get_canonical_manual','eval(', 'exec(', 'getattr('):
            self.assertNotIn(forbidden,source)
        calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call)]
        subprocess_calls=[node for node in calls if isinstance(node.func,ast.Attribute)
                          and isinstance(node.func.value,ast.Name) and node.func.value.id=='subprocess']
        self.assertEqual(len(subprocess_calls),1)
        self.assertIn("(('rev-parse', 'HEAD'),",source)
        self.assertIn("(('status', '--porcelain', '--untracked-files=all'),",source)
        self.assertEqual(source.count('bindings.bootstrap('),1)
        self.assertNotIn('TRUSTED_BUNDLE_SHA256 =',source)

    def test_loader_has_exact_reviewed_identity_for_checkout(self):
        raw=(ORIGINAL/'scripts/get_private_runtime_bindings.py').read_bytes()
        expected=('a2b104cddbe11b2b8ae7518ff0e517b95cf8da2b0deec7283c931ed27dc32ad5'
                  if ORIGINAL==ROOT/'runtime' else
                  'd6e568a4e9f14cf4dc0a5b040ccf8965dbf17e3d62a2dc87a9b9d228bcadc12f')
        self.assertEqual(hashlib.sha256(raw).hexdigest(),expected)


class RuntimeIdentityTests(unittest.TestCase):
    def modules(self):
        runtime=ROOT/'runtime'
        return [types.SimpleNamespace(__file__=str(runtime/'scripts'/name)) for name in
                ('get_private_runtime_bindings.py','get_notion_route_selection.py',
                 'get_private_preview_retention.py')]

    def invoke(self, results, modules=None):
        with patch.object(Path,'is_dir',return_value=True), \
             patch.object(d.subprocess,'run',side_effect=results) as run, \
             patch.object(d.importlib,'import_module',side_effect=modules or self.modules()) as imports:
            value=d.load_runtime()
        return value,run,imports

    def test_exact_pin_and_clean_runtime_before_import(self):
        good=lambda value:types.SimpleNamespace(returncode=0,stdout=value)
        value,run,imports=self.invoke([good((d.RUNTIME_SHA+'\n').encode()),good(b'')])
        self.assertEqual(len(value),3);self.assertEqual(run.call_count,2)
        self.assertEqual(imports.call_count,3)
        self.assertEqual(run.call_args_list[0].args[0][-2:],['rev-parse','HEAD'])
        self.assertEqual(run.call_args_list[1].args[0][-3:],['status','--porcelain','--untracked-files=all'])

    def test_wrong_pin_and_dirty_runtime_rejected_before_import(self):
        good=lambda value:types.SimpleNamespace(returncode=0,stdout=value)
        for results in ([good(b'bad\n')], [good((d.RUNTIME_SHA+'\n').encode()),good(b' M scripts/x.py\n')]):
            with patch.object(Path,'is_dir',return_value=True), \
                 patch.object(d.subprocess,'run',side_effect=results), \
                 patch.object(d.importlib,'import_module') as imports:
                with self.assertRaises(d.BoundaryError):d.load_runtime()
                imports.assert_not_called()

    def test_module_origin_must_be_pinned_runtime(self):
        good=lambda value:types.SimpleNamespace(returncode=0,stdout=value)
        modules=self.modules();modules[0].__file__='/tmp/untrusted.py'
        with self.assertRaises(d.BoundaryError):
            self.invoke([good((d.RUNTIME_SHA+'\n').encode()),good(b'')],modules)


class OriginalTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(ORIGINAL/'scripts'))
        import get_private_runtime_bindings as bindings
        import get_private_preview_retention as transport
        cls.bindings,cls.transport=bindings,transport

    def test_actual_404_none_branch_retains_observation_and_closes_unread_body(self):
        error=urllib.error.HTTPError('https://invalid.example',404,SENTINEL,{},NoBody())
        opener=Mock();opener.open.side_effect=error
        with patch.object(self.transport.urllib.request,'build_opener',return_value=opener):
            remote=self.transport.GitHubRemote(None,'synthetic-token')
        guard=d.ReadOnlyOpener(remote.opener);remote.opener=guard
        self.assertIsNone(remote.ref('refs/heads/main'))
        self.assertEqual(guard.failure['http_status'],404)
        self.assertTrue(error.closed);self.assertEqual(opener.open.call_count,1)

    def test_actual_failure_context_survives_old_loader_normalization(self):
        for code in (401,403,429,500):
            with self.subTest(code=code):
                error=urllib.error.HTTPError('https://invalid.example',code,SENTINEL,{},NoBody())
                opener=Mock();opener.open.side_effect=error
                with patch.object(self.transport.urllib.request,'build_opener',return_value=opener):
                    remote=self.transport.GitHubRemote(None,'synthetic-token')
                guard=d.ReadOnlyOpener(remote.opener);remote.opener=guard
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(self.bindings.BindingError) as caught:
                        self.bindings.bootstrap(d.ReadOnlyRemote(remote),Path(tmp)/'private/bindings.json')
                self.assertEqual(guard.failure['http_status'],code)
                self.assertEqual(d.failure_diagnostic(caught.exception)['http_status'],code)
                self.assertTrue(error.closed);self.assertEqual(opener.open.call_count,1)

    def test_original_redirect_handler_refuses_without_following(self):
        response=Mock()
        with self.assertRaises(self.transport.RetentionFailure):
            self.transport.NoRedirect().redirect_request(None,response,302,SENTINEL,{},'https://invalid.example')
        response.read.assert_not_called()


if __name__=='__main__': unittest.main()
