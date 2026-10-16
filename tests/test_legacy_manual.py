"""No-network manual transaction, real Git object and bounded readback tests."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_legacy_manual as m
import get_legacy_adapter as a
import test_legacy_adapter as fixture

class ManualTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LegacyAdapterTests(methodName='test_capture_history_and_exact_first_adoption_state_only');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.reader=self.f.reader;self.evidence=self.f.evidence;self.parent=self.f.source
        self.current=a.git_current(self.reader,self.parent,self.evidence)
        self.initial=a.sha(a.encoded(a.initial_state(self.evidence,self.current)))
    def source(self,changes=None,terminal=True):
        notes=deepcopy(self.f.notes);summaries=deepcopy(self.f.summaries)
        for ident,change in (changes or {}).items():
            notes[ident].update(change)
            for summary in summaries:
                if summary['id']==ident:summary.update(change)
        class Source:
            def __init__(self):self.ids=[];self.lists=0
            def enumerate(s):s.lists+=1;return summaries,terminal
            def detail_raw(s,ident):s.ids.append(ident);return a.encoded({'success':True,'data':{'note':notes[ident]}})
        return Source()
    def prepare(self,source=None,parent=None,state_sha=None):
        parent=parent or self.parent;current=a.git_current(self.reader,parent,self.evidence)
        return m.prepare(self.reader,self.evidence,parent,current,source or self.source(),initial_sha=self.initial,state_sha=state_sha)
    def remote(self,parent=None,mode=None):
        test=self
        class Remote:
            def __init__(s):s.head=parent or test.parent;s.calls=[];s.stale=0
            def metadata(s):return {'full_name':a.retention.REPOSITORY,'private':True}
            def ref(s,name):
                if name!=a.retention.MAIN_REF:return m.PROTECTED[name]
                if s.stale:s.stale-=1;return parent or test.parent
                return s.head
            def cas_main(s,old,new):
                s.calls.append((old,new));assert s.head==old
                if mode=='unchanged_failure':raise RuntimeError('private error')
                s.head=new
                if mode=='uncertain_stale':s.stale=2;raise RuntimeError('private error')
                if mode=='uncertain_success':raise RuntimeError('private error')
                if mode=='stale':s.stale=2
                if mode=='foreign':s.head='f'*40
            def commit(s,commit):return {'tree':test.f.git('rev-parse',commit+'^{tree}').decode().strip(),'parents':test.f.git('rev-list','--parents','-n','1',commit).decode().split()[1:]}
        return Remote()
    def publish(self,plan,files,remote=None,readback=None):
        return m.publish(self.reader,remote or self.remote(),plan,files,a.sha(a.encoded(plan)),readback or (lambda commit,values:dict(values)),sleep=lambda _:None,writers_guard=lambda:None,checkpoint=lambda plan:None)
    def test_bootstrap_and_real_source_update_form_one_atomic_candidate(self):
        before=self.f.git('show-ref');source=self.source({'1':{'content':'Updated source','updated_at':1776968996}})
        plan,files=self.prepare(source)
        self.assertEqual(source.ids,['1']);self.assertEqual(plan['counts']['source_updated'],1);self.assertTrue(plan['bootstrap'])
        self.assertEqual(set(files),{a.retention.GET+'/1.md',a.retention.GET+'/'+a.STATE})
        data=files[a.retention.GET+'/1.md'];self.assertIn(b'Updated source',data);self.assertIn(b'Old date bytes',data);self.assertIn(b'User tail',data)
        intermediate=self.f.git('rev-parse',plan['candidate_commit']+'^').decode().strip()
        self.assertEqual(self.f.git('rev-parse',intermediate+'^').decode().strip(),self.parent)
        self.assertEqual(self.reader.read(intermediate,a.retention.GET+'/1.md'),self.f.old['1'])
        self.assertEqual(self.f.git('show-ref'),before)
        remote=self.remote();result=self.publish(plan,files,remote)
        self.assertEqual(len(remote.calls),1);self.assertTrue(result['main_updated']);self.assertEqual(remote.head,plan['candidate_commit'])
    def test_no_source_change_adopts_only_state_without_claiming_updates(self):
        source=self.source();plan,files=self.prepare(source)
        self.assertEqual(source.ids,[]);self.assertEqual(plan['counts']['source_updated'],0)
        self.assertEqual(set(files),{a.retention.GET+'/'+a.STATE});self.assertEqual(plan['conflicts'],3);self.assertEqual(plan['pending'],9)
    def test_second_manual_run_unchanged_no_new_commit_or_write(self):
        plan,files=self.prepare();head=plan['candidate_commit'];source=self.source()
        again,values=self.prepare(source,head,plan['state_sha256'])
        self.assertEqual(again['candidate_commit'],head);self.assertEqual(values,{});self.assertEqual(source.ids,[])
        remote=self.remote(head);result=self.publish(again,values,remote);self.assertEqual(remote.calls,[]);self.assertFalse(result['main_updated'])
    def test_changed_source_after_published_bootstrap_uses_external_state_anchor(self):
        first,_=self.prepare();source=self.source({'2':{'content':'Second update','updated_at':1776968997}})
        plan,files=self.prepare(source,first['candidate_commit'],first['state_sha256'])
        self.assertEqual(source.ids,['2']);self.assertEqual(plan['counts']['source_updated'],1)
        self.assertIn(a.retention.GET+'/2.md',files);self.assertEqual(plan['parent_commit'],first['candidate_commit'])
    def test_missing_wrong_state_hash_or_bootstrap_changed_stops_before_source(self):
        source=self.source()
        with self.assertRaisesRegex(a.ParityFailure,'manual_initial_changed'):
            m.prepare(self.reader,self.evidence,self.parent,self.current,source,initial_sha='f'*64)
        self.assertEqual(source.lists,0)
        first,_=self.prepare()
        with self.assertRaisesRegex(a.ParityFailure,'manual_state_anchor'):self.prepare(source,first['candidate_commit'])
        self.assertEqual(source.lists,0)
        with self.assertRaisesRegex(a.ParityFailure,'adapter_state_hash'):self.prepare(source,first['candidate_commit'],'f'*64)
        self.assertEqual(source.lists,0)
    def test_incomplete_list_and_source_error_never_build_published_result(self):
        with self.assertRaises(a.ParityFailure):self.prepare(self.source(terminal=False))
        source=self.source({'1':{'updated_at':1776968998}})
        source.detail_raw=lambda ident:(_ for _ in ()).throw(RuntimeError('Private payload'))
        with self.assertRaises(RuntimeError):self.prepare(source)
    def test_unknown_detail_sibling_and_missing_text_remain_record_conflicts(self):
        source=self.source({'1':{'updated_at':1776968998},'2':{'content':'Clean','updated_at':1776968998}});original=source.detail_raw
        def detail(ident):
            if ident=='1':return a.encoded({'data':{'note':self.f.notes['1'],'other_text':'Unknown source'}})
            return original(ident)
        source.detail_raw=detail;plan,files=self.prepare(source)
        self.assertNotIn(a.retention.GET+'/1.md',files);self.assertIn(a.retention.GET+'/2.md',files);self.assertEqual(plan['counts']['conflicted'],1)
    def test_state_version_stays_old_for_owned_user_edit_and_other_record_updates(self):
        first,_=self.prepare();head=first['candidate_commit'];self.f.git('checkout','-q','-B','local-main',head)
        path=self.f.repo/a.retention.GET/'1.md';path.write_bytes(path.read_bytes().replace(b'Exact source 1',b'User edited source'))
        parent=self.f.commit('user edit');self.reader=a.OfflineGit(self.f.repo)
        source=self.source({'1':{'content':'Conflict','updated_at':1776968999},'2':{'content':'Safe','updated_at':1776968999}})
        plan,files=self.prepare(source,parent,first['state_sha256']);state=json.loads(files[a.retention.GET+'/'+a.STATE])
        previous=json.loads(self.reader.read(head,a.retention.GET+'/'+a.STATE))
        self.assertEqual(state['legacyById']['1'],previous['legacyById']['1']);self.assertEqual(plan['counts']['conflicted'],1)
        self.assertNotIn(a.retention.GET+'/1.md',files);self.assertIn(a.retention.GET+'/2.md',files)
    def test_one_write_even_after_uncertain_success_or_stale_visibility(self):
        for mode in ('uncertain_success','uncertain_stale','stale'):
            plan,files=self.prepare();remote=self.remote(mode=mode);result=self.publish(plan,files,remote)
            self.assertEqual(len(remote.calls),1);self.assertTrue(result['readback_verified'])
    def test_foreign_head_and_unconfirmed_failure_stop_without_second_write(self):
        for mode,reason in (('foreign','manual_readback_ref'),('unchanged_failure','manual_uncertain_write')):
            plan,files=self.prepare();remote=self.remote(mode=mode)
            with self.assertRaisesRegex(a.ParityFailure,reason) as raised:self.publish(plan,files,remote)
            if mode=='unchanged_failure':self.assertEqual(raised.exception.write_state,'uncertain')
            self.assertEqual(len(remote.calls),1)
    def test_readback_mismatch_after_write_stops_without_blind_rollback(self):
        plan,files=self.prepare();remote=self.remote()
        with self.assertRaisesRegex(a.ParityFailure,'manual_readback'):self.publish(plan,files,remote,lambda commit,values:{})
        self.assertEqual(len(remote.calls),1);self.assertEqual(remote.head,plan['candidate_commit'])
    def test_main_race_before_write_is_rejected(self):
        plan,files=self.prepare();remote=self.remote();remote.head='e'*40
        with self.assertRaisesRegex(a.ParityFailure,'manual_parent_changed'):self.publish(plan,files,remote)
        self.assertEqual(remote.calls,[])
    def test_tampered_candidate_files_and_plan_hash_stop_before_write(self):
        plan,files=self.prepare();remote=self.remote();bad=dict(files);bad[next(iter(bad))]=b'Corrupt'
        with self.assertRaises(a.ParityFailure):self.publish(plan,bad,remote)
        with self.assertRaises(a.ParityFailure):m.publish(self.reader,remote,plan,files,'f'*64,lambda c,f:f)
        self.assertEqual(remote.calls,[])
    def test_real_bare_git_one_push_keeps_parent_assets_and_other_refs(self):
        bare=self.f.base/'remote.git';subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
        self.f.git('push',str(bare),self.parent+':refs/heads/main',self.f.head+':refs/heads/evidence')
        plan,files=self.prepare(self.source({'1':{'content':'Actual Git update','updated_at':1776968996}}))
        remote=self.remote();calls=[]
        def cas(old,new):
            calls.append((old,new));self.f.git('push','--porcelain','--no-verify','--force-with-lease=refs/heads/main:'+old,str(bare),new+':refs/heads/main');remote.head=new
        remote.cas_main=cas;self.publish(plan,files,remote)
        main=subprocess.check_output(['git','--git-dir',str(bare),'rev-parse','refs/heads/main']).decode().strip()
        self.assertEqual(main,plan['candidate_commit']);self.assertEqual(len(calls),1)
        self.assertEqual(subprocess.check_output(['git','--git-dir',str(bare),'show',main+':'+a.retention.GET+'/assets/inherited.bin']),b'Inherited capture asset')
        self.assertEqual(subprocess.check_output(['git','--git-dir',str(bare),'rev-parse','refs/heads/evidence']).decode().strip(),self.f.head)
    def test_public_filter_never_echoes_private_payload_or_bad_types(self):
        for value in (None,[],{'error':{}},{'error':'Private error','private':'sensitive'}):
            result=m.public_report(value);self.assertEqual(result,{'status':'manual_failed','main_updated':None,'write_state':'unknown'})
    def test_detail_404_is_record_conflict_and_auth_error_stops_every_write(self):
        source=self.source({'1':{'updated_at':1776968998},'2':{'content':'Clean','updated_at':1776968998}});read=source.detail_raw
        def unavailable(ident):
            if ident=='1':raise RuntimeError('GetNote HTTP 404')
            return read(ident)
        source.detail_raw=unavailable;plan,files=self.prepare(source)
        state=json.loads(files[a.retention.GET+'/'+a.STATE]);self.assertEqual(state['conflictsById']['1'],'source_missing');self.assertEqual(plan['counts']['details_unavailable'],1)
        self.assertIn(a.retention.GET+'/2.md',files)
        source.detail_raw=lambda ident:(_ for _ in ()).throw(RuntimeError('GetNote HTTP 401'))
        with self.assertRaisesRegex(RuntimeError,'401'):self.prepare(source)
    def test_failure_report_preserves_confirmed_and_uncertain_write_outcome(self):
        plan,files=self.prepare();remote=self.remote()
        try:self.publish(plan,files,remote,lambda c,f:{})
        except m.ManualFailure as exc:
            result=m.public_report({'error':str(exc),'write_state':exc.write_state})
        self.assertTrue(result['main_updated']);self.assertEqual(result['cas_attempts'],1);self.assertEqual(result['write_state'],'confirmed')
        result=m.public_report({'error':'manual_uncertain_write','write_state':'uncertain'})
        self.assertIsNone(result['main_updated']);self.assertEqual(result['cas_attempts'],1)
    def test_old_writer_disabled_and_all_active_status_checks_are_read_only(self):
        class Response:
            status=200
            def __init__(s,value):s.data=a.encoded(value)
            def __enter__(s):return s
            def __exit__(s,*args):pass
            def read(s,maximum):return s.data
        class Opener:
            def __init__(s,bad=None):s.calls=[];s.bad=bad
            def open(s,request,timeout):
                s.calls.append(request);name=request.full_url.split('/workflows/')[1].split('/')[0]
                if '/runs?' in request.full_url:return Response({'total_count':1 if s.bad=='active' else 0,'workflow_runs':[]})
                return Response({'path':'.github/workflows/'+name,'state':'active' if s.bad=='enabled' else 'disabled_manually'})
        opener=Opener();m.disabled_writers(opener=opener)
        self.assertEqual(len(opener.calls),12);self.assertTrue(all(r.get_method()=='GET' and not r.has_header('Authorization') for r in opener.calls))
        for mode in ('active','enabled'):
            with self.assertRaisesRegex(m.ManualFailure,'manual_target'):m.disabled_writers(opener=Opener(mode))
    def test_workflow_manual_entry_defaults_to_prepare_and_shares_writer_lock(self):
        workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml').read_text()
        block=workflow.split('legacy_apply:',1)[1].split('sample_pages:',1)[0]
        self.assertIn('default: false',block);self.assertIn("inputs.mode == 'legacy_manual' && 'eddie-private-vault-writers'",workflow)
        self.assertIn('args=(--expected-parent "$EXPECTED_PARENT")',workflow);self.assertNotIn('schedule:',workflow)
        self.assertNotIn('actions: write',workflow);self.assertNotIn('contents: write',workflow)
        step=workflow.split('Prepare or explicitly apply one bounded legacy transaction',1)[1].split('- name:',1)[0]
        self.assertIn('if [[ "$APPLY" == true ]]; then args+=(--apply); fi',step)
        self.assertNotIn('NOTION_',step);self.assertNotIn('GITHUB_TOKEN:',step)
    def test_oversized_candidate_and_conflicting_public_flags_fail_closed(self):
        with patch.object(m,'MAX_CHANGED_BYTES',100),self.assertRaisesRegex(a.ParityFailure,'manual_budget'):self.prepare()
        value={'status':'manual_candidate_verified','main_updated':True,'cas_attempts':1,'readback_verified':True,'managed':2,'pending':9,'notes_updated':0,'changed_paths':1,'changed_bytes':100,'state_sha256':'a'*64,'plan_sha256':'b'*64,'parent_commit':'c'*40,'candidate_commit':'d'*40}
        self.assertEqual(m.public_report(value)['status'],'manual_failed')
    def test_run_loads_real_git_evidence_bootstraps_updates_and_resumes_noop(self):
        remote=self.remote();local=self.reader.local;real_run=local.run
        remote.source_get_tree=self.reader.source_get_tree;remote.tree_entries=self.reader.tree_entries
        def metadata(commit):
            tree=self.reader.tree(commit)
            return {p:(*row,int(real_run('cat-file','-s',row[2]))) for p,row in tree.items() if p.startswith(a.retention.GET+'/')}
        remote.check_source_budget=metadata
        def blobs(remote,metadata,token,directory):return {p:local.blob(row[2]) for p,row in metadata.items()}
        def git(*args,**kwargs):
            if args[0]=='fetch':return b''
            return real_run(*args,**kwargs)
        candidates={i:{'filePath':v['filePath']} for i,v in self.evidence.census['eligible'].items()}
        with patch.object(m,'CAPTURE',self.f.head),patch.object(m,'ANCHOR',self.f.anchor),patch.object(m,'INITIAL_SHA',self.initial),patch.object(m,'Remote',lambda *args:remote),patch.object(m.r,'readback_checkout',lambda *args:local),patch.object(local,'run',side_effect=git),patch.object(m.c,'fetch_blobs',side_effect=blobs),patch.object(m,'fetch_selected',side_effect=blobs),patch.object(m.c,'authorized_cohort',return_value=({},candidates,set(self.evidence.census['pending_ids']),{},{})),patch.object(m,'disabled_writers',lambda:None):
            source=self.source({'1':{'content':'End to end','updated_at':1776968996}})
            result=m.run('placeholder',source,self.parent,apply=True,work=self.f.base,checkpoint=lambda plan:None)
            self.assertEqual(result['notes_updated'],1);self.assertEqual(result['cas_attempts'],1);self.assertEqual(source.ids,['1']);self.assertEqual(m.public_report(result)['status'],'manual_transaction_verified')
            head=remote.head;state=result['state_sha256'];source=self.source({'1':{'content':'End to end','updated_at':1776968996}})
            again=m.run('placeholder',source,head,state_sha=state,apply=True,work=self.f.base,checkpoint=lambda plan:None)
            self.assertEqual(again['notes_updated'],0);self.assertEqual(again['changed_paths'],0);self.assertEqual(again['candidate_commit'],head)
            self.assertEqual(source.ids,[]);self.assertEqual(len(remote.calls),1);self.assertEqual(m.public_report(again)['status'],'manual_transaction_verified')
    def test_selected_state_blob_larger_than_source_response_cap_roundtrips(self):
        data=b'x'*(2*m.c.MIB+20);path=self.f.base/'large-state';path.write_bytes(data);blob=a.retention.oid(data)
        import source_sync_pack_seed as pack
        with patch.object(pack,'capabilities',return_value={}),patch.object(m.r,'fetch_readback_blobs',return_value=({blob:path},len(data))):
            values=m.fetch_selected(None,{a.retention.GET+'/'+a.STATE:('100644','blob',blob,len(data))},'placeholder',self.f.base/'readback')
        self.assertEqual(values[a.retention.GET+'/'+a.STATE],data)
    def test_new_unknown_envelope_source_is_not_silently_discarded(self):
        note=deepcopy(self.f.notes['1'])
        self.assertIsNone(m.detail_note(a.encoded({'data':{'note':note},'new_source_text':'Unknown'})))
        self.assertEqual(m.detail_note(a.encoded({'success':True,'data':{'note':note},'meta':{'response_only':True},'request_id':'opaque','error':None})),note)
    def test_missing_interrupted_result_never_claims_main_unchanged(self):
        path=self.f.base/'missing-result';result=m.read_report(path)
        self.assertIsNone(result['main_updated']);self.assertEqual(result['write_state'],'unknown')
        plan,_=self.prepare();checkpoint=self.f.base/'checkpoint';m.write_checkpoint(checkpoint,plan)
        result=m.read_report(path,checkpoint)
        self.assertIsNone(result['main_updated']);self.assertEqual(result['write_state'],'uncertain')
        self.assertEqual(result['planned_candidate_commit'],plan['candidate_commit']);self.assertNotIn('changes',result)
        path.write_text('{broken');self.assertEqual(m.read_report(path,checkpoint)['planned_state_sha256'],plan['state_sha256'])
    def test_checkpoint_must_finish_before_single_cas_and_cannot_clobber(self):
        plan,files=self.prepare();remote=self.remote();checkpoint=self.f.base/'checkpoint';checkpoint.write_text('Existing foreign data')
        with self.assertRaisesRegex(m.ManualFailure,'manual_checkpoint'):
            m.publish(self.reader,remote,plan,files,a.sha(a.encoded(plan)),lambda c,f:f,writers_guard=lambda:None,checkpoint=lambda p:m.write_checkpoint(checkpoint,p))
        self.assertEqual(remote.calls,[]);self.assertEqual(checkpoint.read_text(),'Existing foreign data')
        checkpoint.unlink()
        m.publish(self.reader,remote,plan,files,a.sha(a.encoded(plan)),lambda c,f:f,writers_guard=lambda:None,checkpoint=lambda p:m.write_checkpoint(checkpoint,p))
        self.assertEqual(len(remote.calls),1);self.assertEqual(json.loads(checkpoint.read_text()),m.checkpoint_data(plan))
    def test_checkpoint_does_not_override_known_confirmed_failure_or_echo_private_fields(self):
        plan,_=self.prepare();checkpoint=self.f.base/'checkpoint';m.write_checkpoint(checkpoint,plan)
        path=self.f.base/'result';path.write_text(json.dumps({'error':'manual_readback','write_state':'confirmed'}))
        result=m.read_report(path,checkpoint);self.assertTrue(result['main_updated']);self.assertEqual(result['write_state'],'confirmed')
        payload=json.loads(checkpoint.read_text());payload['private']='Secret';checkpoint.write_text(json.dumps(payload))
        result=m.read_report(self.f.base/'absent',checkpoint);self.assertEqual(result,{'status':'manual_failed','main_updated':None,'write_state':'unknown'})
    def test_storage_cap_includes_intermediate_initial_state_blob(self):
        source=self.source({'1':{'content':'Fresh changed source','updated_at':1776968998}})
        plan,files=self.prepare(source);final_bytes=sum(map(len,files.values()))
        self.assertGreater(plan['transaction_bytes'],final_bytes)
        initial=a.encoded(a.initial_state(self.evidence,self.current))
        self.assertEqual(plan['transaction_bytes'],final_bytes+len(initial))
        with patch.object(m,'MAX_CHANGED_BYTES',final_bytes+1),self.assertRaisesRegex(a.ParityFailure,'manual_budget'):
            self.prepare(self.source({'1':{'content':'Fresh changed source','updated_at':1776968998}}))
        first,_=self.prepare();again,values=self.prepare(parent=first['candidate_commit'],state_sha=first['state_sha256'])
        self.assertEqual(again['transaction_bytes'],0);self.assertEqual(values,{})
