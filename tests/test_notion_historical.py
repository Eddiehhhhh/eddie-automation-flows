from copy import deepcopy
from pathlib import Path
import sys
import json
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_historical as h
import get_notion_historical_runtime as hr
import get_notion_executor as e
import get_notion_outbox as outbox
import get_notion_runtime as rt
import get_notion_readonly as ro
from test_notion_executor import Tests as Fixture, N, N2, DS, md, paragraph, API


class Tests(Fixture):
    # This class inherits the full existing engine fault/recovery suite as well.
    def setUp(self):
        super().setUp()
    def historical(self):
        self.source_path.write_bytes(md(N,'Historical substantive body'))
        commit=self.commit('historical source')
        self.api.make(N,[paragraph('Source metadata'),paragraph('（无文本内容）')])
        self.store.compare_and_set(self.store.read()[1],e.initial_state())
        data=self.source_path.read_bytes();source={'path':self.source_path.relative_to(self.repo).as_posix(),'blob':e.p.git_blob(data),'sha256':e.digest(data)}
        review={'source_sha256':source['sha256'],'review_evidence_sha256':'a'*64,'destination_data_source_id':DS,'credential_free':True,'transmission_approved':True,'media_free':True}
        manifest={'contract':h.CONTRACT,'source_commit':commit,'data_source_id':DS,'database_id':'b'*32,'audit_sha256':'c'*64,'user_scope_sha256':'d'*64,
                  'records':{N:{**source,'mode':'existing_placeholder','review':review,'target':h.target_pin(self.api.pages[N],N,DS)}}}
        self.manifest=manifest;self.publication=h.Publication(self.repo,manifest,e.digest(manifest))
        return manifest
    def historical_plan(self):
        state,sha=self.store.read()
        return h.prepare(self.publication,state,sha,{N:self.api.read_note(N)},self.policy,self.manifest,e.digest(self.manifest))
    def test_historical_pinned_existing_identity_and_recoverable_roots(self):
        self.historical();before=deepcopy(self.api.pages[N]);plan=self.historical_plan()
        self.assertEqual(plan['source_mode'],'historical_cohort');self.assertEqual(plan['publication']['changed_content_ids'],[])
        self.assertEqual(plan['records'][0]['action'],'update');self.assertEqual(self.store.read()[0]['receipts'],{})
        self.assertEqual(self.run_plan(plan)['status'],'verified')
        self.assertEqual(self.api.pages[N]['page_id'],before['page_id']);self.assertEqual(self.api.pages[N]['recursive_blocks'][0],before['recursive_blocks'][0])
        self.assertEqual(self.writes(),[('append',)])
        self.assertEqual(self.historical_plan()['records'],[])
    def test_historical_changed_source_with_existing_receipt_is_held_without_duplicate(self):
        self.historical();first=self.historical_plan()
        self.assertEqual(self.run_plan(first)['status'],'verified')
        first_snapshot=deepcopy(self.api.pages[N]);first_receipt=deepcopy(self.store.read()[0]['receipts'][N])
        self.assertEqual(self.historical_plan()['records'],[])
        self.source_path.write_bytes(md(N,'Second reviewed historical body'))
        commit=self.commit('second historical source');data=self.source_path.read_bytes()
        manifest=deepcopy(self.manifest);manifest['source_commit']=commit
        row=manifest['records'][N];row.update(blob=e.p.git_blob(data),sha256=e.digest(data))
        row['review']['source_sha256']=row['sha256']
        row['target']=h.target_pin(self.api.pages[N],N,DS)
        self.manifest=manifest;self.publication=h.Publication(self.repo,manifest,e.digest(manifest))
        second=self.historical_plan()
        self.assertEqual(second['records'][0]['action'],'blocked')
        self.assertEqual(second['records'][0]['reason'],'content_review_required')
        self.assertEqual(self.run_plan(second)['blocked'],1)
        self.assertEqual(self.api.pages[N],first_snapshot)
        self.assertEqual(self.store.read()[0]['receipts'][N],first_receipt)
        self.assertEqual(self.writes(),[('append',)])
        self.assertEqual(self.historical_plan()['records'],[])
        protected={block['id'] for block in first_receipt['protected_blocks']}
        managed={block['id'] for block in first_receipt['managed_blocks']}
        self.assertFalse(protected & managed)
        self.assertTrue(managed)

    def test_historical_manual_addition_after_approval_is_held(self):
        self.historical();s=self.api.pages[N];nodes,_=self.api.rows([paragraph('Human note')],s['page_id']);s['recursive_blocks']+=nodes
        self.assertEqual(self.historical_plan()['records'][0]['reason'],'target_changed');self.assertEqual(self.writes(),[])
    def test_historical_duplicate_exact_identity_is_held(self):
        self.historical();self.api.pages[N]['exact_id_matches'].append('f'*32)
        self.assertEqual(self.historical_plan()['records'][0]['reason'],'duplicate_id')
    def test_historical_placeholder_edit_is_held(self):
        self.historical();self.api.pages[N]['recursive_blocks'][1]['payload']['rich_text']=[e.p.text_run('Human replacement')]
        self.assertEqual(self.historical_plan()['records'][0]['reason'],'target_changed')
    def test_historical_source_drift_stops_before_write(self):
        self.historical();plan=self.historical_plan();self.source_path.write_bytes(md(N,'changed'));self.commit('drift')
        self.assertEqual(self.run_plan(plan)['reason'],'source_changed');self.assertEqual(self.writes(),[])
    def test_historical_append_timeout_resumes_without_reappend(self):
        self.historical();plan=self.historical_plan();self.api.throw='append_after'
        self.assertEqual(self.run_plan(plan)['reason'],'uncertain_append');self.api.throw=None
        self.assertEqual(self.run_plan(plan)['status'],'verified');self.assertEqual(self.writes(),[('append',)])
    def test_historical_before_effect_timeout_never_retries(self):
        self.historical();plan=self.historical_plan();self.api.throw='append_before'
        self.assertEqual(self.run_plan(plan)['reason'],'uncertain_append');self.api.throw=None
        self.assertEqual(self.run_plan(plan)['reason'],'uncertain_append');self.assertEqual(self.writes(),[('append',)])
    def test_historical_unknown_resolved_discussions_keep_every_old_block(self):
        self.historical();before=deepcopy(self.api.pages[N]);plan=self.historical_plan()
        self.assertEqual(plan['records'][0]['old_roots'],[])
        self.api.throw='archive_before'
        self.assertEqual(self.run_plan(plan)['status'],'verified')
        after={r['id']:r for r in self.api.pages[N]['recursive_blocks']}
        for old in before['recursive_blocks']:self.assertEqual(after[old['id']],old)
        receipt=self.store.read()[0]['receipts'][N]
        self.assertTrue({r['id'] for r in before['recursive_blocks']}<={r['id'] for r in receipt['protected_blocks']})
        self.assertEqual(self.writes(),[('append',)])
    def test_historical_inflight_other_journal_is_held(self):
        self.historical();plan=self.historical_plan();self.api.throw='append_before';self.run_plan(plan)
        with self.assertRaisesRegex(e.Blocked,'journal_busy'):self.historical_plan()
    def test_historical_registration_is_separate_from_cli_hash(self):
        m=self.historical()
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):h.validate(m,e.digest(m),apply=True)
        with patch.object(h,'APPROVED_MANIFESTS',frozenset([e.digest(m)])):h.validate(m,e.digest(m),apply=True)
    def test_historical_privacy_and_all_recording_media_are_held(self):
        m=self.historical();m['records'][N]['review']['credential_free']=False
        with self.assertRaisesRegex(e.Blocked,'privacy_scope_required'):h.validate(m,e.digest(m))
        for value in [md(N,'password=syntheticSecret123'),md(N,'![asset](image.png)'),md(N,'text').replace(b'title:',b'note_type: recorder_audio\ntitle:')]:
            with self.assertRaises(e.Blocked):h.source_check(value,N)
    def test_historical_capture_is_not_ownership_approval(self):
        m=self.historical();candidate=deepcopy(m);candidate['contract']=h.CAPTURE_CONTRACT
        candidate['records'][N]['target']={'page_id':m['records'][N]['target']['page_id']}
        h.validate_capture(candidate,e.digest(candidate))
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):h.validate_capture(candidate,e.digest(candidate),apply=True)
        capture={'candidate':candidate,'manifest':m,'snapshots':{N:deepcopy(self.api.pages[N])}}
        h.check_captures({e.digest(candidate):capture})
        outer=outbox.initial_state();outer['historical_captures']={e.digest(candidate):capture};outbox.check_state(outer)
        with self.assertRaisesRegex(e.Blocked,'state_changed'):h.preserve_captures(outer,outbox.initial_state())
    def test_historical_outer_queue_and_anchors_are_unchanged(self):
        self.historical();plan=self.historical_plan()
        class Store:
            def __init__(self):self.value=outbox.initial_state()
            def read(self):return deepcopy(self.value),outbox.digest(self.value)
            def compare_and_set(self,sha,value):
                assert sha==outbox.digest(self.value);outbox.check_state(value);self.value=deepcopy(value);return outbox.digest(self.value)
            def confirm(self,value):return True
        outer=Store();adapter=outbox.EngineStore(outer);self.api.store=adapter
        before=outer.read()[0];state,sha=adapter.read()
        plan=h.prepare(self.publication,state,sha,{N:self.api.read_note(N)},self.policy,self.manifest,e.digest(self.manifest))
        with patch.object(outbox,'confirmed_compare_and_set',lambda store,sha,value:(value,store.compare_and_set(sha,value))):
            result=e.execute(plan,e.digest(plan),self.publication,self.api,adapter,expected_store_sha=sha,writers_guard=lambda:self.policy['old_writer_evidence'])
        self.assertEqual(result['status'],'verified')
        for key in ('pending_by_id','rotation_order','last_enqueued_publication','observed_publication_tip'):self.assertEqual(outer.value[key],before[key])
    def test_historical_main_cli_is_present_and_default_inert(self):
        import subprocess
        result=subprocess.run([sys.executable,str(Path(rt.__file__)),'--help'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0);self.assertIn('--historical-capture',result.stdout);self.assertIn('--historical-manifest',result.stdout)

if __name__=='__main__':unittest.main()

class CaptureTests(Tests):
    def test_capture_only_durable_evidence_without_notion_writes(self):
        from types import SimpleNamespace
        m=self.historical();candidate=deepcopy(m);candidate['contract']=h.CAPTURE_CONTRACT
        candidate['records'][N]['target']={'page_id':m['records'][N]['target']['page_id']};expected=e.digest(candidate)
        class Store:
            def __init__(self):self.value=outbox.initial_state();self.commits=0
            def read(self):return deepcopy(self.value),outbox.digest(self.value)
            def compare_and_set(self,sha,value):
                assert sha==outbox.digest(self.value);outbox.check_state(value);self.value=deepcopy(value);self.commits+=1;return outbox.digest(value)
        class Loader:
            def __init__(self,*args):pass
            def commit(self,*args):pass
            def blob(self,*args):pass
        store=Store();before=deepcopy(store.value)
        with patch.object(h,'APPROVED_CAPTURE_SCOPES',frozenset([expected])),patch.object(hr.source_loader,'Loader',Loader),patch.object(hr,'API',return_value=self.api),patch.object(outbox,'confirmed_compare_and_set',lambda store,sha,value:(value,store.compare_and_set(sha,value))):
            result=hr.capture(SimpleNamespace(vault=self.repo),None,None,self.root,store,None,candidate=candidate,expected=expected,policy_hash='f'*64,apply=True,writers_guard=lambda:self.policy['old_writer_evidence'])
            self.assertEqual(result['status'],'captured');self.assertEqual(store.commits,1);self.assertEqual(self.writes(),[])
            saved=store.value['historical_captures'][expected];self.assertEqual(saved['manifest'],m)
            self.assertEqual(store.value['engine_state'],before['engine_state'])
            result=hr.capture(SimpleNamespace(vault=self.repo),None,None,self.root,store,None,candidate=candidate,expected=expected,policy_hash='f'*64,apply=True,writers_guard=lambda:self.policy['old_writer_evidence'])
            self.assertEqual(store.commits,1);self.assertEqual(self.writes(),[])
