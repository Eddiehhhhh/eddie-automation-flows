"""Synthetic combined-release CLI and cross-mode regression; never network."""
from copy import deepcopy
import contextlib
import io
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import get_notion_runtime as rt
import get_notion_executor as e
import get_notion_outbox as outbox
import get_notion_historical as historical
import get_notion_historical_runtime as hr
import get_notion_properties_runtime as pr
import test_notion_properties_runtime as pf
import test_notion_historical as hf
import test_notion_outbox as of
import test_notion_runtime as rf


class Tests(unittest.TestCase):
    def property_fixture(self):
        f=pf.Tests(methodName='test_readonly_historical_plan_never_changes_queue_or_journal')
        f.setUp();self.addCleanup(f.doCleanups);return f

    def historical_fixture(self):
        f=hf.Tests(methodName='test_historical_pinned_existing_identity_and_recoverable_roots')
        f.setUp();self.addCleanup(f.doCleanups);f.historical();return f

    def invoke_cli(self,mode,manifest,repo,store,request,writers):
        """Actual main -> run -> specialized dispatch, with only remote I/O faked."""
        commit=manifest['source_commit'];root=Path(repo).parent
        input_path=root/(mode+'.json');input_path.write_bytes(e.encoded(manifest))
        policy_path=root/'combined-policy.json';policy_path.write_text(json.dumps({'get_policy_sha256':'a'*64}))
        store.anchor={'state_blob':None};store.commits_attempted=0
        remote=SimpleNamespace(source_get_tree=lambda _: 'b'*40)
        local=SimpleNamespace(vault=repo,run=e.git.OfflineGit(repo).local.run)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(rt.recurring,'validate_policy',side_effect=lambda value,*_:value))
            stack.enter_context(patch.object(rt.m,'Remote',return_value=remote))
            stack.enter_context(patch.object(rt.m,'guard',return_value=commit))
            stack.enter_context(patch.object(rt.r,'readback_checkout',return_value=local))
            stack.enter_context(patch.object(rt.journal,'ImmutableJournalRemote',side_effect=lambda value:value))
            stack.enter_context(patch.object(rt.checkpoints,'ExactJournalLoader',return_value=lambda _:None))
            stack.enter_context(patch.object(rt.journal,'RemoteJournal',return_value=store))
            stack.enter_context(patch.object(rt.transport,'Transport',return_value=request))
            stack.enter_context(patch.object(rt.provenance,'PublicGithub'))
            stack.enter_context(patch.object(rt.old_writers,'WriterGuard',return_value=writers))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code=rt.main(['--expected-parent',commit,'--policy',str(policy_path),
                    '--'+mode,str(input_path),'--approved-'+mode+'-sha256',e.digest(manifest)])
        return code,json.loads(output.getvalue())

    def test_real_property_cli_reports_prepared_source_mode(self):
        f=self.property_fixture();before=f.store.read()
        code,result=self.invoke_cli('property-manifest',f.f.manifest,f.f.f.repo,f.store,f.request,
            lambda:deepcopy(f.f.f.policy['old_writer_evidence']))
        self.assertEqual(code,0,result);self.assertEqual(result['source_mode'],'property_backfill')
        self.assertEqual(result['initial_notes'],0);self.assertEqual(result['initial_range_candidates'],0)
        self.assertEqual(f.store.read(),before);self.assertFalse(f.f.calls)

    def test_real_historical_cli_reports_prepared_source_mode(self):
        f=self.historical_fixture();store=of.Store();before=store.read()
        with patch.object(hr,'API',return_value=f.api),patch.object(rt,'schema',return_value='Title'), \
                patch.object(rt.transport.native,'DATA_SOURCE_PIN',rt.transport.native.sha(hf.DS)), \
                patch.object(rt.transport.native,'DATABASE_PIN',rt.transport.native.sha('b'*32)):
            code,result=self.invoke_cli('historical-manifest',f.manifest,f.repo,store,None,
                lambda:deepcopy(f.policy['old_writer_evidence']))
        self.assertEqual(code,0,result);self.assertEqual(result['source_mode'],'historical_cohort')
        self.assertEqual(result['actions'],{'update':1});self.assertEqual(store.read(),before);self.assertFalse(f.writes())

    def test_real_capture_cli_is_readonly_and_reports_hashes_only(self):
        f=self.historical_fixture();store=of.Store();before=store.read();candidate=deepcopy(f.manifest)
        candidate['contract']=historical.CAPTURE_CONTRACT
        candidate['records'][hf.N]['target']={'page_id':f.manifest['records'][hf.N]['target']['page_id']}
        with patch.object(hr,'API',return_value=f.api):
            code,result=self.invoke_cli('historical-capture',candidate,f.repo,store,None,
                lambda:deepcopy(f.policy['old_writer_evidence']))
        self.assertEqual(code,0,result);self.assertEqual(result['notion_write_attempts'],0)
        self.assertEqual(result['candidate_sha256'],e.digest(candidate));self.assertEqual(store.read(),before)
        self.assertNotIn(hf.N,json.dumps(result));self.assertFalse(f.writes())

    def test_cli_modes_are_mutually_exclusive_before_file_or_credentials(self):
        modes=('property-manifest','historical-manifest','historical-capture')
        for pair in itertools.combinations(modes,2):
            argv=['--policy','unread-policy','--expected-parent','a'*40]
            for name in pair:argv+=['--'+name,'unread-private','--approved-'+name+'-sha256','a'*64]
            with self.subTest(pair=pair),patch.object(rt.a,'bounded_file',side_effect=AssertionError('must not read')), \
                    patch.object(rt.transport,'Transport',side_effect=AssertionError('must not access credentials')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(rt.main(argv),1)
            self.assertEqual(json.loads(output.getvalue())['reason'],'notion_scope')

    def test_each_cli_mode_requires_matching_expected_hash_before_credentials(self):
        for name in ('property-manifest','historical-manifest','historical-capture'):
            for tail in (['--'+name,'unread-private'],['--approved-'+name+'-sha256','a'*64]):
                with self.subTest(mode=name,tail=tail),patch.object(rt.a,'bounded_file',side_effect=AssertionError('must not read')), \
                        patch.object(rt.transport,'Transport',side_effect=AssertionError('must not access credentials')), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(rt.main(['--policy','unread-policy','--expected-parent','a'*40]+tail),1)
                self.assertEqual(json.loads(output.getvalue())['reason'],'notion_scope')

    def test_direct_runtime_rejects_conflicting_modes_and_recovery_before_remote(self):
        modes=('property_manifest','historical_manifest','historical_capture')
        cases=[{a:{},b:{}} for a,b in itertools.combinations(modes,2)]
        cases += [{name:{},'create_recovery':{}} for name in modes]
        for kwargs in cases:
            with self.subTest(keys=sorted(kwargs)),patch.object(rt.m,'Remote',side_effect=AssertionError('no remote')):
                result=rt.run(None,None,None,public_policy={},**kwargs)
            self.assertEqual(result['reason'],'notion_scope',result)

    def test_unregistered_apply_modes_cannot_access_credentials(self):
        f=self.historical_fixture();p=self.property_fixture();candidate=deepcopy(f.manifest)
        candidate['contract']=historical.CAPTURE_CONTRACT
        candidate['records'][hf.N]['target']={'page_id':f.manifest['records'][hf.N]['target']['page_id']}
        for name,manifest in (('property-manifest',p.f.manifest),('historical-manifest',f.manifest),('historical-capture',candidate)):
            path=f.root/(name+'.json');path.write_bytes(e.encoded(manifest))
            with self.subTest(mode=name),patch.object(rt.transport,'Transport',side_effect=AssertionError('no credentials')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code=rt.main(['--apply','--scope-from-policy','--policy','unread-policy','--'+name,str(path),'--approved-'+name+'-sha256',e.digest(manifest)])
            self.assertEqual(code,1);self.assertEqual(json.loads(output.getvalue())['reason'],'approval_missing')

    def test_scope_binds_each_independent_activation_and_create_permission(self):
        policy={'rules_sha256':'a'*64};base=rt.scope_for(policy)
        cases=((pr,'APPROVED_MANIFESTS',frozenset({'b'*64}),'property_manifest_allowlist_sha256'),
            (historical,'APPROVED_MANIFESTS',frozenset({'b'*64}),'historical_manifest_allowlist_sha256'),
            (historical,'APPROVED_CAPTURE_SCOPES',frozenset({'b'*64}),'historical_capture_allowlist_sha256'),
            (rt.historical_input,'INPUT_ANCHORS',{'b'*64:{'synthetic':True}},'historical_input_anchors_sha256'),
            (e.creation_properties,'SCOPE',{'synthetic':True},'new_page_property_scope_sha256'))
        for module,name,value,key in cases:
            with self.subTest(name=name,key=key),patch.object(module,name,value):
                self.assertNotEqual(rt.scope_for(policy)[key],base[key]);self.assertNotEqual(e.digest(rt.scope_for(policy)),e.digest(base))

    @staticmethod
    def queued(store,ident,source,commit):
        origin={'commit':'1'*40,'receipt_sha256':None,'sequence':0}
        tip={'commit':commit,'receipt_sha256':'2'*64,'sequence':1}
        store.value.update(origin=origin,policy_sha256='3'*64,last_enqueued_publication=tip,observed_publication_tip=tip,
            pending_by_id={ident:{**source,'commit':commit,'parent':origin['commit'],'receipt_sha256':tip['receipt_sha256'],'sequence':1}},rotation_order=[ident])
        other=str(int(ident)+1)
        store.value['pending_by_id'][other]={**store.value['pending_by_id'][ident],'path':'Raw/03 Get/notes/other.md','blob':'4'*40,'sha256':'5'*64}
        store.value['rotation_order'].append(other)
        outbox.check_state(store.value)

    def test_property_apply_does_not_dequeue_same_identity_or_drop_capture(self):
        p=self.property_fixture();h=self.historical_fixture();candidate=deepcopy(h.manifest);candidate['contract']=historical.CAPTURE_CONTRACT
        candidate['records'][hf.N]['target']={'page_id':h.manifest['records'][hf.N]['target']['page_id']}
        capture={e.digest(candidate):{'candidate':candidate,'manifest':h.manifest,'snapshots':{hf.N:deepcopy(h.api.pages[hf.N])}}}
        p.store.value['historical_captures']=capture
        self.queued(p.store,hf.N,p.f.publication.descriptor['records'][hf.N],p.f.manifest['source_commit'])
        before=p.store.read()[0]
        with patch.object(pr,'APPROVED_MANIFESTS',frozenset({p.sha})):result=p.call(True)
        self.assertEqual(rt.safe_result(result)['status'],'executed',result)
        for key in ('pending_by_id','rotation_order','last_enqueued_publication','observed_publication_tip','historical_captures'):
            self.assertEqual(p.store.value[key],before[key])

    def test_historical_apply_does_not_dequeue_same_identity_or_get_create_exemption(self):
        f=self.historical_fixture();store=of.Store();source=f.publication.descriptor['records'][hf.N]
        self.queued(store,hf.N,source,f.manifest['source_commit']);before=store.read()[0]
        adapter=outbox.EngineStore(store);f.api.store=adapter;state,sha=adapter.read()
        plan=historical.prepare(f.publication,state,sha,{hf.N:f.api.read_note(hf.N)},f.policy,f.manifest,e.digest(f.manifest))
        self.assertFalse(e.creation_properties.eligible(f.publication));self.assertNotIn('property_create_scope',plan)
        result=e.execute(plan,e.digest(plan),f.publication,f.api,adapter,expected_store_sha=sha,writers_guard=lambda:f.policy['old_writer_evidence'])
        self.assertEqual(result['status'],'verified',result)
        for key in ('pending_by_id','rotation_order','last_enqueued_publication','observed_publication_tip'):
            self.assertEqual(store.value[key],before[key])

    def test_normal_canonical_engine_cas_preserves_existing_capture(self):
        f=rf.RuntimeTests(methodName='test_fresh_blob_none_prepare_creates_no_journal_then_real_dispatch_and_repeat')
        f.setUp();self.addCleanup(f.doCleanups);h=self.historical_fixture()
        candidate=deepcopy(h.manifest);candidate['contract']=historical.CAPTURE_CONTRACT
        candidate['records'][hf.N]['target']={'page_id':h.manifest['records'][hf.N]['target']['page_id']}
        saved={e.digest(candidate):{'candidate':candidate,'manifest':h.manifest,'snapshots':{hf.N:deepcopy(h.api.pages[hf.N])}}}
        f.store.value['historical_captures']=deepcopy(saved)
        f.f.publication({rf.s.N(1):'Synthetic canonical source'});result=f.invoke(f.f.clone(),True)
        self.assertEqual(result['execution']['created'],1,result)
        self.assertEqual(f.store.value['historical_captures'],saved)

    def test_property_report_preserves_blocked_record_reason(self):
        f=self.property_fixture();original=pr.tx.prepare
        def blocked(*args,**kwargs):
            plan=original(*args,**kwargs);plan['records'][0].update(action='blocked',reason='property_manual_edit');return plan
        with patch.object(pr.tx,'prepare',side_effect=blocked):result=f.call()
        safe=rt.safe_result(result)
        self.assertEqual(safe['status'],'prepared',safe);self.assertEqual(safe['reason_counts'],{'property_manual_edit':1})


if __name__=='__main__':unittest.main()
