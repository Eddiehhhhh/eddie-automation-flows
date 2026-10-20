from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_executor as e
import get_notion_outbox as outbox
import get_notion_properties_runtime as runtime
import get_notion_runtime as main_runtime
import get_notion_live_transport as transport
import get_notion_properties as mapping
import test_notion_properties as f
from test_notion_outbox import Store


class Tests(unittest.TestCase):
    def setUp(self):
        self.f=f.TransactionTests(methodName='test_journaled_backfill_preserves_body_receipt_and_title_and_repeat_is_empty')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store=Store();self.store.value['engine_state']=self.f.f.store.read()[0]
        self.f.f.store=outbox.EngineStore(self.store);self.f.f.api.store=self.f.f.store
        self.schema=self.f.schema;self.schema['Title']={'type':'title','id':'title'};self.schema['笔记 ID']={'type':'rich_text','id':'identity'}
        self.sha=e.digest(self.f.manifest);self.request=transport.Transport('synthetic-test-token')
        self.local=SimpleNamespace(vault=self.f.f.repo)
        self.loads=[]
        loads=self.loads
        class Loader:
            def __init__(self,*a):pass
            def commit(self,commit):loads.append(('commit',commit))
            def blob(self,commit,path,cap):loads.append(('blob',path,cap))
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(runtime.source_loader,'Loader',Loader))
        self.stack.enter_context(patch.object(runtime.transport,'API',return_value=self.f.f.api))
    def call(self,apply=False):
        return runtime.dispatch(self.local,None,None,self.f.f.root,self.store,self.request,
            manifest=self.f.manifest,expected=self.sha,policy_hash='a'*64,apply=apply,
            writers_guard=lambda:deepcopy(self.f.f.policy['old_writer_evidence']))
    def test_readonly_historical_plan_never_changes_queue_or_journal(self):
        before=self.store.read();out=self.call();self.assertEqual(out['status'],'prepared',out)
        self.assertEqual(out['source_mode'],'property_backfill');self.assertEqual(out['actions'],{'update':1})
        self.assertEqual(self.store.read(),before);self.assertFalse(self.f.calls)
        self.assertEqual([x[1] for x in self.loads if x[0]=='blob'],['Raw/03 Get/notes/a.md'])
    def test_self_hash_cannot_activate_unregistered_manifest(self):
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):self.call(True)
        self.assertFalse(self.loads);self.assertFalse(self.f.calls)
    def test_registered_manifest_applies_and_repeated_dispatch_is_noop(self):
        with patch.object(runtime,'APPROVED_MANIFESTS',frozenset({self.sha})):
            out=self.call(True);self.assertEqual(out['execution']['status'],'verified',out)
            self.assertEqual(len(self.f.calls),1)
            again=self.call(True);self.assertEqual(again['selected'],0,again);self.assertEqual(len(self.f.calls),1)
    def test_saved_property_transaction_resumes_read_only_after_uncertain_write(self):
        with patch.object(runtime,'APPROVED_MANIFESTS',frozenset({self.sha})):
            self.f.mode='after';out=self.call(True);self.assertEqual(out['execution']['reason'],'uncertain_properties',out)
            self.f.mode=None;out=self.call(True);self.assertEqual(out['execution']['status'],'verified',out);self.assertEqual(len(self.f.calls),1)
    def test_existing_body_journal_is_not_replaced(self):
        plan=self.f.prepare();plan['property_only']=False
        self.store.value['engine_state']['journal']={'plan':plan,'plan_sha256':e.digest(plan),'index':0,'operation':None}
        before=self.store.read()
        with self.assertRaisesRegex(e.Blocked,'journal_busy'):self.call()
        self.assertEqual(self.store.read(),before);self.assertFalse(self.loads)
    def test_manifest_source_cannot_read_non_get_files(self):
        self.f.manifest['records'][f.f.N]['path']='secrets.txt';self.sha=e.digest(self.f.manifest)
        with self.assertRaises(Exception):self.call()
        self.assertFalse(self.loads)
    def test_runtime_batch_cap_remains_ten(self):
        manifest=deepcopy(self.f.manifest);row=next(iter(manifest['records'].values()))
        manifest['records']={str(1000000000000000000+i):{**row,'path':'Raw/03 Get/notes/'+str(i)+'.md'} for i in range(11)}
        with self.assertRaisesRegex(e.Blocked,'request_budget'):runtime.authorize(manifest,e.digest(manifest),apply=False)
    def test_dependency_is_exactly_pinned_and_workflows_install_before_tests(self):
        root=Path(__file__).resolve().parents[1];lock=(root/'requirements-notion-properties.txt').read_bytes()
        self.assertEqual(e.digest(lock),runtime.DEPENDENCY_LOCK_SHA256)
        self.assertEqual(lock.count(b'--hash=sha256:'),2)
        template=(root/'.github/workflow-templates/get-canonical-controller.yml.disabled').read_text()
        self.assertIn('--require-hashes --only-binary=:all: --no-deps -r requirements-notion-properties.txt',template)
        shadow=(root/'.github/workflows/source-sync-shadow.yml').read_text()
        self.assertLess(shadow.index('Install verified metadata parser'),shadow.index('Offline regression suite'))
    def test_cli_passes_private_manifest_to_real_runtime_interface(self):
        path=self.f.f.root/'manifest.json';path.write_bytes(e.encoded(self.f.manifest))
        policy=self.f.f.root/'policy.json';policy.write_text('{}')
        with patch.object(main_runtime,'run',return_value={'status':'prepared','write_state':'not_attempted'}) as run, patch.object(main_runtime.transport,'Transport'),patch.object(main_runtime.provenance,'PublicGithub'),patch.object(main_runtime.old_writers,'WriterGuard'),contextlib.redirect_stdout(io.StringIO()):
            code=main_runtime.main(['--policy',str(policy),'--expected-parent','a'*40,'--property-manifest',str(path),'--approved-property-manifest-sha256',self.sha])
        self.assertEqual(code,0);self.assertEqual(run.call_args.kwargs['property_manifest'],self.f.manifest)
        self.assertEqual(run.call_args.kwargs['approved_property_manifest_sha256'],self.sha)
    def test_unregistered_apply_manifest_blocked_before_credentials(self):
        path=self.f.f.root/'manifest.json';path.write_bytes(e.encoded(self.f.manifest))
        with patch.object(main_runtime.transport,'Transport',side_effect=AssertionError('credentials must not be accessed')),contextlib.redirect_stdout(io.StringIO()) as output:
            code=main_runtime.main(['--apply','--scope-from-policy','--policy','unread-policy.json','--property-manifest',str(path),'--approved-property-manifest-sha256',self.sha])
        self.assertEqual(code,1);self.assertIn('approval_missing',output.getvalue())

if __name__=='__main__':unittest.main()
