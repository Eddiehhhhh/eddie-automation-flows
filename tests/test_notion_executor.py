from copy import deepcopy
from pathlib import Path
import json
import os
import shutil
from unittest.mock import patch
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_executor as e
import get_notion_sink_plan as p

N='1234567890123456789'; N2='2234567890123456789'; DS='d'*32

def md(note,body,title='Synthetic title'):
    return ('---\nnote_id: '+json.dumps(note)+'\ntitle: '+json.dumps(title)+'\n---\n'+body+'\n').encode()

def paragraph(text):return p.block('paragraph',{'rich_text':[p.text_run(text)]})

def missing():return {'complete':True,'native_blocks_available':True,'identity_query_complete':True,'exact_id_matches':[]}

class API:
    def __init__(self,store):self.pages={};self.serial=100;self.edits=0;self.calls=[];self.store=store;self.throw=None;self.hook=None
    def new_id(self):self.serial+=1;return '%032x'%self.serial
    def rows(self,blocks,parent):
        out=[]
        def build(block,parent):
            ident=self.new_id();children=[]
            row={'id':ident,'parent_id':parent,'type':block['type'],'payload':deepcopy(block['payload']),'children_ids':children,'has_children':bool(block['children'])}
            out.append(row)
            for child in block['children']:children.append(build(child,ident))
            return ident
        roots=[build(block,parent) for block in blocks];return out,roots
    def make(self,note,blocks,props=None):
        page=self.new_id();rows,roots=self.rows(blocks,page)
        snapshot={'complete':True,'native_blocks_available':True,'identity_query_complete':True,'exact_id_matches':[page],
            'page_id':page,'parent_data_source':DS,'id_property':note,'last_edited_time':'2026-10-16T00:00:00Z',
            'properties_raw':props or {'笔记 ID':{'type':'rich_text','rich_text':[p.text_run(note)]},'Title':{'type':'title','title':[p.text_run('User title')]},'User':{'type':'rich_text','rich_text':[p.text_run('User-owned property')]}},'recursive_blocks':rows}
        self.pages[note]=snapshot;return roots
    def read_note(self,note):
        self.calls.append(('read',note))
        return deepcopy(self.pages.get(note,missing()))
    def before(self,kind):
        value,_=self.store.read();op=value['journal']['operation']
        assert op['kind']==kind and op['phase']=='in_flight'
        self.calls.append((kind,))
        if self.hook:self.hook(kind)
        if self.throw==kind+'_before':raise TimeoutError('PRIVATE SECRET')
    def edited(self,snapshot):
        self.edits+=1;snapshot['last_edited_time']='edit-'+str(self.edits)
    def append_blocks(self,page,blocks,after):
        self.before('append');s=next(x for x in self.pages.values() if x['page_id']==page)
        new,roots=self.rows(blocks,page);rows=s['recursive_blocks'];position=0
        if after is not None:
            view=e.target(s,s['id_property'],DS);last=e.subtree(view['nodes'],after)[-1]
            position=next(i for i,r in enumerate(rows) if r['id']==last)+1
        rows[position:position]=new;self.edited(s)
        if self.throw=='append_after':raise TimeoutError('PRIVATE SECRET')
        return roots
    def archive_block(self,root):
        self.before('archive');s=next(x for x in self.pages.values() if any(r['id']==root for r in x['recursive_blocks']))
        view=e.target(s,s['id_property'],DS);removed=set(e.subtree(view['nodes'],root))
        s['recursive_blocks']=[r for r in s['recursive_blocks'] if r['id'] not in removed];self.edited(s)
        if self.throw=='archive_after':raise TimeoutError('PRIVATE SECRET')
    def create_page(self,ds,note,props,blocks):
        self.before('create');assert ds==DS and note not in self.pages
        self.make(note,blocks,deepcopy(props));self.edited(self.pages[note])
        if self.throw=='create_after':raise TimeoutError('PRIVATE SECRET')
        return self.pages[note]['page_id']

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.git('init','-q','-b','main');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        self.source_path=self.repo/'Raw/03 Get/notes/a.md';self.source_path.parent.mkdir(parents=True)
        self.source_path.write_bytes(md(N,'Old source'))
        self.parent=self.commit('old')
        self.store=e.LocalJournal(self.root);self.api=API(self.store)
        roots=self.api.make(N,[paragraph('User prefix'),paragraph('Old source'),paragraph('User tail')]);self.owned=roots[1:2]
        oldrow={'path':'Raw/03 Get/notes/a.md','blob':p.git_blob(self.source_path.read_bytes()),'sha256':e.digest(self.source_path.read_bytes())}
        state=e.initial_state();state['receipts'][N]=e.receipt(self.api.pages[N],N,DS,self.owned,{'commit':self.parent},oldrow)
        self.store.compare_and_set(e.digest(e.initial_state()),state)
        self.policy={'data_source_id':DS,'id_property':'笔记 ID','title_property':'Title','allow_create':True,'allow_archive':True,'format_accepted':True,
            'old_writer_evidence':{'fresh_readback_verified':True,'writers':{name:{'disabled':True,'active_runs':0} for name in ('getnote-to-notion','notion-to-getnote')}}}
    def git(self,*args):return subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True).stdout
    def commit(self,message):self.git('add','.');self.git('commit','-qm',message);return self.git('rev-parse','HEAD').decode().strip()
    def publish(self,body='New source'):
        self.source_path.write_bytes(md(N,body));commit=self.commit('new')
        self.publication=e.GitPublication(self.repo,commit,self.parent,commit);return self.publication
    def plan(self,**kwargs):
        state,sha=self.store.read();return e.prepare(self.publication,state,sha,{i:self.api.read_note(i) for i in self.publication.descriptor['records']},self.policy,**kwargs)
    def run_plan(self,plan,**kwargs):return e.execute(plan,e.digest(plan),self.publication,self.api,self.store,expected_store_sha=self.store.read()[1],writers_guard=lambda:deepcopy(self.policy['old_writer_evidence']),**kwargs)
    def writes(self):return [x for x in self.api.calls if x[0]!='read']
    def test_real_git_update_journal_before_writes_preserves_unowned_and_properties(self):
        self.publish();before=deepcopy(self.api.pages[N]);plan=self.plan();self.assertEqual(plan['records'][0]['action'],'update')
        result=self.run_plan(plan);self.assertEqual(result['status'],'verified');self.assertEqual(result['updated'],1)
        self.assertEqual(self.writes(),[('append',),('archive',)])
        after=self.api.pages[N];self.assertEqual(after['properties_raw'],before['properties_raw'])
        self.assertEqual([r['payload']['rich_text'][0]['text']['content'] for r in after['recursive_blocks']],['User prefix','New source','User tail'])
        self.assertEqual(after['recursive_blocks'][0],before['recursive_blocks'][0]);self.assertEqual(after['recursive_blocks'][-1],before['recursive_blocks'][-1])
        state,_=self.store.read();self.assertIsNone(state['journal']);self.assertTrue(state['receipts'][N]['readback_verified'])
        repeat=self.plan();self.assertEqual(repeat['records'],[]);calls=len(self.api.calls)
        self.assertEqual(self.run_plan(repeat)['write_calls'],0);self.assertEqual(len(self.api.calls),calls)
    def test_state_only_git_commit_enqueues_nothing(self):
        path=self.repo/'Raw/03 Get/03 State/other.json';path.parent.mkdir();path.write_text('{}');commit=self.commit('state')
        self.publication=e.GitPublication(self.repo,commit,self.parent,commit);plan=self.plan();self.assertTrue(plan['state_only'])
        self.assertEqual(self.run_plan(plan)['write_calls'],0);self.assertEqual(self.api.calls,[])
    def test_missing_approval_and_state_anchor_stop_before_journal_or_api(self):
        self.publish();plan=self.plan();before=self.store.read()
        out=e.execute(plan,'0'*64,self.publication,self.api,self.store,expected_store_sha=before[1],writers_guard=lambda:self.policy['old_writer_evidence']);self.assertEqual(out['reason'],'approval_missing')
        out=e.execute(plan,e.digest(plan),self.publication,self.api,self.store,expected_store_sha='0'*64,writers_guard=lambda:self.policy['old_writer_evidence']);self.assertEqual(out['reason'],'state_changed')
        self.assertEqual(self.store.read(),before);self.assertEqual(self.writes(),[])
    def test_existing_page_without_ownership_isolated_from_clean_create(self):
        state,sha=self.store.read();state['receipts']={};self.store.compare_and_set(sha,state)
        (self.source_path.parent/'b.md').write_bytes(md(N2,'Second source'));self.publish();plan=self.plan()
        self.assertEqual([r['action'] for r in plan['records']],['blocked','create']);self.assertEqual(plan['records'][0]['reason'],'missing_block_ownership')
        out=self.run_plan(plan);self.assertEqual(out['created'],1);self.assertEqual(out['blocked'],1);self.assertEqual(self.writes(),[('create',)])
    def test_duplicate_id_and_user_edit_are_record_conflicts(self):
        self.publish();self.api.pages[N]['exact_id_matches'].append('f'*32)
        self.assertEqual(self.plan()['records'][0]['reason'],'duplicate_id')
        self.api.pages[N]['exact_id_matches'].pop();self.api.pages[N]['recursive_blocks'][1]['payload']['rich_text']=[p.text_run('User edit')]
        self.assertEqual(self.plan()['records'][0]['reason'],'human_edit_conflict');self.assertEqual(self.writes(),[])
    def test_source_and_target_change_after_plan_prevent_writes(self):
        self.publish();plan=self.plan();self.api.pages[N]['properties_raw']['Extra']={'type':'number','number':1}
        self.assertEqual(self.run_plan(plan)['reason'],'target_changed');self.assertEqual(self.writes(),[])
        self.source_path.write_bytes(md(N,'Concurrent source'));self.commit('changed')
        self.assertEqual(self.run_plan(plan)['reason'],'source_changed');self.assertEqual(self.writes(),[])
    def test_append_timeout_after_effect_recovers_read_only_without_duplicate(self):
        self.publish();plan=self.plan();self.api.throw='append_after'
        out=self.run_plan(plan);self.assertEqual(out['reason'],'uncertain_append');self.assertEqual(self.writes(),[('append',)])
        self.api.throw=None;out=self.run_plan(plan);self.assertEqual(out['status'],'verified');self.assertEqual(self.writes(),[('append',),('archive',)])
    def test_append_timeout_without_effect_never_retries(self):
        self.publish();plan=self.plan();self.api.throw='append_before';self.run_plan(plan);self.api.throw=None
        for _ in range(2):self.assertEqual(self.run_plan(plan)['reason'],'uncertain_append')
        self.assertEqual(self.writes(),[('append',)]);self.assertEqual(self.api.pages[N]['recursive_blocks'][1]['id'],self.owned[0])
    def test_archive_timeout_after_effect_recovers_without_repeat(self):
        self.publish();plan=self.plan();self.api.throw='archive_after';out=self.run_plan(plan);self.assertEqual(out['reason'],'uncertain_archive')
        self.api.throw=None;self.assertEqual(self.run_plan(plan)['status'],'verified');self.assertEqual(self.writes(),[('append',),('archive',)])
    def test_archive_timeout_without_effect_never_retries(self):
        self.publish();plan=self.plan();self.api.throw='archive_before';self.run_plan(plan);self.api.throw=None
        self.assertEqual(self.run_plan(plan)['reason'],'uncertain_archive');self.assertEqual(self.writes(),[('append',),('archive',)])
    def test_human_addition_after_append_blocks_archive(self):
        self.publish();plan=self.plan();self.api.throw='append_after';self.run_plan(plan);self.api.throw=None
        rows,_=self.api.rows([paragraph('Human concurrent addition')],self.api.pages[N]['page_id']);self.api.pages[N]['recursive_blocks']+=rows
        out=self.run_plan(plan);self.assertEqual(out['reason'],'readback_mismatch');self.assertEqual(self.writes(),[('append',)])
    def test_new_create_receipt_and_uncertain_create_never_adopts_observation_alone(self):
        del self.api.pages[N];state,sha=self.store.read();state['receipts']={};self.store.compare_and_set(sha,state)
        self.publish();plan=self.plan();self.api.throw='create_after';self.assertEqual(self.run_plan(plan)['reason'],'uncertain_create');self.api.throw=None
        self.assertEqual(self.run_plan(plan)['reason'],'uncertain_create');self.assertEqual(self.writes(),[('create',)]);self.assertEqual(self.store.read()[0]['receipts'],{})
        recovery={'plan_sha256':e.digest(plan),'note_id':N,'page_id':self.api.pages[N]['page_id'],'snapshot_sha256':e.target_sha(self.api.pages[N],N,DS)}
        self.assertEqual(self.run_plan(plan,create_recovery=recovery,approved_recovery_sha='f'*64)['reason'],'uncertain_create')
        out=self.run_plan(plan,create_recovery=recovery,approved_recovery_sha=e.digest(recovery));self.assertEqual(out['status'],'verified');self.assertEqual(self.writes(),[('create',)])
    def test_explicit_reviewed_initial_range_is_pinned_not_observation_adoption(self):
        state,sha=self.store.read();state['receipts']={};self.store.compare_and_set(sha,state);self.publish()
        scope={N:{'page_id':self.api.pages[N]['page_id'],'snapshot_sha256':e.target_sha(self.api.pages[N],N,DS),'managed_block_ids':self.owned}}
        with self.assertRaises(e.Blocked):self.plan(initial_scope=scope,expected_scope_sha='f'*64)
        plan=self.plan(initial_scope=scope,expected_scope_sha=e.digest(scope));self.assertEqual(plan['records'][0]['action'],'update')
        self.assertEqual(self.store.read()[0]['receipts'],{});self.assertEqual(self.run_plan(plan)['status'],'verified')
    def test_archive_and_format_gates_are_explicit(self):
        self.publish();self.policy['allow_archive']=False;self.assertEqual(self.plan()['records'][0]['reason'],'archive_not_approved')
        self.policy['format_accepted']=False;self.assertEqual(self.plan()['records'][0]['reason'],'format_not_approved')
    def test_long_text_split_semantics_and_request_bounds(self):
        self.publish('x'*4200);plan=self.plan();runs=plan['records'][0]['request_blocks'][0]['payload']['rich_text'];self.assertEqual([len(x['text']['content']) for x in runs],[2000,2000,200])
        self.assertEqual(self.run_plan(plan)['status'],'verified')
        with self.assertRaises(e.Blocked):e.request_blocks([paragraph('x') for _ in range(101)])
    def test_public_report_contains_only_fixed_aggregate_fields(self):
        value=e.public_report({'status':'blocked','reason':'PRIVATE BODY','created':0,'updated':0,'noop':0,'blocked':1,'write_calls':0,'external_write_attempts':0,'deferred':0,'retained_conflicts':0,'write_state':'unknown','page_id':N})
        self.assertNotIn(N,str(value));self.assertNotIn('PRIVATE',str(value));self.assertNotIn('page_id',value)

    def test_fifty_blocked_records_do_not_starve_later_clean_record(self):
        ids=[str(1000000000000000000+i) for i in range(50)];clean='9000000000000000000'
        for ident in ids+[clean]:
            (self.source_path.parent/(ident+'.md')).write_bytes(md(ident,'Fresh source'))
            if ident in ids:self.api.make(ident,[paragraph('Unowned existing')])
        commit=self.commit('cohort');self.publication=e.GitPublication(self.repo,commit,self.parent,commit)
        first=self.plan();self.assertEqual(len(first['records']),50);self.assertEqual(first['deferred_ids'],[clean])
        self.assertEqual(self.run_plan(first)['blocked'],50)
        state,_=self.store.read();self.assertEqual(len(state['failures']),50);self.assertFalse(set(ids)&set(state['receipts']))
        second=self.plan();self.assertEqual([r['note_id'] for r in second['records']],[clean]);self.assertEqual(self.run_plan(second)['created'],1)
        self.assertEqual(self.plan()['records'],[])
        self.assertEqual(len(self.plan(retry_conflicts=True)['records']),50)
    def test_initial_two_cohort_is_separate_exact_approval_without_faked_changes(self):
        (self.source_path.parent/'b.md').write_bytes(md(N2,'Second source'));commit=self.commit('second')
        records={}
        for ident,path in ((N,self.source_path),(N2,self.source_path.parent/'b.md')):
            data=path.read_bytes();records[ident]={'path':path.relative_to(self.repo).as_posix(),'blob':p.git_blob(data),'sha256':e.digest(data)}
        cohort={'commit':commit,'records':records}
        with self.assertRaises(e.Blocked):e.GitPublication(self.repo,commit,commit,commit,initial_cohort=cohort,expected_initial_sha='0'*64)
        self.publication=e.GitPublication(self.repo,commit,commit,commit,initial_cohort=cohort,expected_initial_sha=e.digest(cohort))
        state,sha=self.store.read();state['receipts']={};self.store.compare_and_set(sha,state)
        self.api.make(N2,[paragraph('Existing unowned')]);plan=self.plan()
        self.assertEqual(plan['publication']['changed_content_ids'],[]);self.assertTrue(plan['state_only']);self.assertEqual(plan['source_mode'],'initial_review')
        self.assertEqual(len(plan['records']),2);self.assertTrue(all(r['reason']=='missing_block_ownership' for r in plan['records']))
        self.assertEqual(self.run_plan(plan)['blocked'],2);self.assertEqual(self.writes(),[])
        wrong=deepcopy(cohort);wrong['records'][N]['blob']='f'*40
        with self.assertRaises(e.Blocked):e.GitPublication(self.repo,commit,commit,commit,initial_cohort=wrong,expected_initial_sha=e.digest(wrong))
        with self.assertRaises(e.Blocked):e.GitPublication(self.repo,commit,self.parent,commit,initial_cohort=cohort,expected_initial_sha=e.digest(cohort))
    def test_same_commit_asset_tree_available_in_actual_blob_none_clone_without_fetch(self):
        data=b'synthetic retained image evidence';asset=self.source_path.parent/'a.jpeg';asset.write_bytes(data)
        self.parent=self.commit('inherited asset');self.publish('![[a.jpeg]]')
        commit=self.publication.descriptor['commit'];bare=self.root/'remote.git';clone=self.root/'partial'
        subprocess.run(['git','clone','-q','--bare',str(self.repo),str(bare)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(bare),'config','uploadpack.allowFilter','true'],check=True)
        subprocess.run(['git','clone','-q','--filter=blob:none','--no-checkout',bare.as_uri(),str(clone)],check=True,capture_output=True)
        note=self.source_path.read_bytes();subprocess.run(['git','-C',str(clone),'hash-object','-w','--stdin'],input=note,check=True,capture_output=True)
        self.publication=e.GitPublication(clone,commit,self.parent,commit)
        oid=p.git_blob(data);self.assertEqual(self.publication.descriptor['content_tree'][asset.relative_to(self.repo).as_posix()],oid)
        self.assertNotEqual(self.publication.repo.local.command('cat-file','-e',oid).returncode,0)
        assets={'a.jpeg':{'readback_verified':True,'bytes':data,'path':asset.relative_to(self.repo).as_posix(),'git_blob':oid,'sha256':e.digest(data),'notion_file_id':'e'*32}}
        self.assertEqual(self.plan(assets=assets)['records'][0]['reason'],'asset_write_contract_missing')
        self.assertNotEqual(self.publication.repo.local.command('cat-file','-e',oid).returncode,0)
    def test_state_outside_get_tree_and_descendant_publication_guard(self):
        self.assertEqual(e.STATE_PATH,'.sync/get-notion/state.json')
        self.publish();plan=self.plan();path=self.repo/e.STATE_PATH;path.parent.mkdir(parents=True);path.write_text('{}');self.commit('independent sink journal')
        self.assertEqual(self.run_plan(plan)['status'],'verified')
        self.source_path.write_bytes(md(N,'Foreign edit'));self.commit('foreign');self.assertEqual(self.run_plan(plan)['reason'],'source_changed')
    def test_journal_compare_and_set_failure_before_attempt_can_resume_safely(self):
        self.publish();plan=self.plan();original=self.store.compare_and_set
        def reject(expected,value):
            op=(value.get('journal') or {}).get('operation')
            if op and op['phase']=='in_flight':raise OSError('PRIVATE')
            return original(expected,value)
        with patch.object(self.store,'compare_and_set',side_effect=reject):out=self.run_plan(plan)
        self.assertEqual(out['reason'],'journal_failed');self.assertEqual(self.writes(),[])
        self.assertEqual(self.store.read()[0]['journal']['operation']['phase'],'prepared')
        self.assertEqual(self.run_plan(plan)['status'],'verified');self.assertEqual(self.writes(),[('append',),('archive',)])
    def test_journal_failure_after_effect_recovers_without_duplicate(self):
        self.publish();plan=self.plan();original=self.store.compare_and_set
        def reject(expected,value):
            op=(value.get('journal') or {}).get('operation')
            if op and op['phase']=='acknowledged':raise OSError('PRIVATE')
            return original(expected,value)
        with patch.object(self.store,'compare_and_set',side_effect=reject):out=self.run_plan(plan)
        self.assertEqual(out['write_state'],'uncertain');self.assertEqual(self.store.read()[0]['journal']['operation']['phase'],'in_flight')
        self.assertEqual(self.run_plan(plan)['status'],'verified');self.assertEqual(self.writes(),[('append',),('archive',)])
    def test_stale_store_cas_never_overwrites_concurrent_state(self):
        value,sha=self.store.read();foreign=deepcopy(value);foreign['failures'][N2]={'md_blob':'1'*40,'source_commit':'2'*40,'policy_sha256':'3'*64,'reason':'missing_target'}
        expected=self.store.compare_and_set(sha,foreign)
        with self.assertRaises(e.Blocked):self.store.compare_and_set(sha,value)
        self.assertEqual(self.store.read(),(foreign,expected))
    def test_fresh_writer_gate_prevents_attempt_after_preparation(self):
        self.publish();plan=self.plan();evidence=deepcopy(self.policy['old_writer_evidence']);evidence['writers']['getnote-to-notion']['disabled']=False
        out=e.execute(plan,e.digest(plan),self.publication,self.api,self.store,expected_store_sha=self.store.read()[1],writers_guard=lambda:evidence)
        self.assertEqual(out['reason'],'writer_gate_unknown');self.assertEqual(self.writes(),[])
    def test_empty_created_page_can_receive_later_update_without_adopting_unowned_blocks(self):
        del self.api.pages[N];state,sha=self.store.read();state['receipts']={};self.store.compare_and_set(sha,state)
        self.publish('');plan=self.plan();self.assertEqual(self.run_plan(plan)['created'],1)
        state,_=self.store.read();self.assertEqual(state['receipts'][N]['managed_blocks'],[])
        extra,_=self.api.rows([paragraph('Human tail')],self.api.pages[N]['page_id']);self.api.pages[N]['recursive_blocks']+=extra
        self.parent=self.publication.descriptor['commit'];self.publish('Later content');plan=self.plan();self.assertEqual(plan['records'][0]['action'],'update')
        self.assertEqual(self.run_plan(plan)['updated'],1);self.assertEqual(self.writes(),[('create',),('append',)])
        self.assertEqual(self.api.pages[N]['recursive_blocks'][-1],extra[0])
    def test_owned_missing_page_never_recreated(self):
        self.publish();del self.api.pages[N];self.assertEqual(self.plan()['records'][0]['reason'],'missing_target');self.assertEqual(self.writes(),[])
    def test_malformed_cached_receipt_cannot_short_circuit_queue(self):
        self.publish();state,sha=self.store.read();state['receipts'][N]['md_blob']=self.publication.descriptor['records'][N]['blob'];state['receipts'][N]['source_commit']=self.publication.descriptor['commit'];state['receipts'][N]['managed_blocks']=[]
        with self.assertRaises(e.Blocked):e.prepare(self.publication,state,e.digest(state),{},self.policy)
    def test_missing_malformed_report_keeps_effects_unknown(self):
        for value in (None,{},[],{'status':'verified'}, {'status':'verified','write_state':'not_attempted'}):
            out=e.public_report(value);self.assertEqual(out['write_state'],'unknown');self.assertIsNone(out['external_write_attempts']);self.assertNotIn('external_writes',out)
    def test_typed_property_change_after_append_is_not_preserved(self):
        self.api.pages[N]['properties_raw']['Flag']={'type':'number','number':1};state,sha=self.store.read();state['receipts'][N]['properties_baseline']=deepcopy(self.api.pages[N]['properties_raw']);self.store.compare_and_set(sha,state)
        self.publish();plan=self.plan();self.api.throw='append_after';self.run_plan(plan);self.api.throw=None
        self.api.pages[N]['properties_raw']['Flag']['number']=True
        self.assertEqual(self.run_plan(plan)['reason'],'readback_mismatch');self.assertEqual(self.writes(),[('append',)])

    def test_local_preflight_failure_never_marks_external_attempt(self):
        self.publish();plan=self.plan()
        def preflight(*args):raise ValueError('unsupported_prepend')
        self.api.preflight_append=preflight
        out=self.run_plan(plan);self.assertEqual(out['status'],'partial');self.assertEqual(out['blocked'],1);self.assertEqual(out['write_calls'],0)
        self.assertIsNone(self.store.read()[0]['journal']);self.assertEqual(self.store.read()[0]['failures'][N]['reason'],'unsupported_prepend');self.assertEqual(self.writes(),[])
    def test_asset_reference_is_rechecked_at_current_descendant_without_binary_read(self):
        data=b'retained synthetic image';asset=self.source_path.parent/'a.jpeg';asset.write_bytes(data);self.parent=self.commit('asset')
        self.publish('![[a.jpeg]]');assets={'a.jpeg':{'readback_verified':True,'bytes':data,'path':asset.relative_to(self.repo).as_posix(),'git_blob':p.git_blob(data),'sha256':e.digest(data),'notion_file_id':'e'*32}}
        blocks,_,_=p.render_markdown(self.source_path.read_bytes(),N,assets,'Raw/03 Get/notes/a.md',self.publication.descriptor['content_tree'])
        roots=self.api.make(N,blocks);state,sha=self.store.read();state['receipts'][N]=e.receipt(self.api.pages[N],N,DS,roots,{'commit':self.parent},{'blob':'1'*40});self.store.compare_and_set(sha,state)
        plan=self.plan(assets=assets);self.assertEqual(plan['records'][0]['action'],'noop')
        asset.write_bytes(b'foreign asset change');self.commit('changed asset')
        self.assertEqual(self.run_plan(plan)['reason'],'source_changed');self.assertEqual(self.writes(),[])
    def test_normal_publication_parent_must_be_exact_first_parent(self):
        self.publish();commit=self.publication.descriptor['commit']
        with self.assertRaises(e.Blocked):e.GitPublication(self.repo,commit,commit,commit)

    def initial_fixture(self):
        (self.source_path.parent/'b.md').write_bytes(md(N2,'Second source'));commit=self.commit('second')
        records={ident:{'path':path.relative_to(self.repo).as_posix(),'blob':p.git_blob(path.read_bytes()),'sha256':e.digest(path.read_bytes())} for ident,path in ((N,self.source_path),(N2,self.source_path.parent/'b.md'))}
        cohort={'commit':commit,'records':records};self.publication=e.GitPublication(self.repo,commit,commit,commit,initial_cohort=cohort,expected_initial_sha=e.digest(cohort))
        self.api.make(N2,[paragraph('User prefix'),paragraph('Second preview'),paragraph('User tail')])
        historical={ident:{'page_id':self.api.pages[ident]['page_id'],'blocks':[paragraph(body)],'evidence_sha256':'a'*64} for ident,body in ((N,'Old source'),(N2,'Second preview'))}
        return historical
    def test_initial_candidate_matches_unique_prior_preview_without_adoption(self):
        historical=self.initial_fixture();before=self.store.read()
        out=e.propose_initial_scope(self.publication,self.api.pages,DS,historical,e.digest(historical))
        self.assertEqual(len(out['scope']),2);self.assertEqual(out['blocked'],{});self.assertFalse(out['ownership_adopted']);self.assertTrue(out['requires_explicit_scope_approval'])
        self.assertEqual(out['scope'][N]['managed_block_ids'],self.owned);self.assertEqual(out['proof'][N]['protected_blocks'],2)
        self.assertEqual(self.store.read(),before);self.assertEqual(self.writes(),[])
    def test_initial_candidate_duplicate_sequence_or_changed_text_isolated(self):
        historical=self.initial_fixture();extra,_=self.api.rows([paragraph('Old source')],self.api.pages[N]['page_id']);self.api.pages[N]['recursive_blocks']+=extra
        out=e.propose_initial_scope(self.publication,self.api.pages,DS,historical,e.digest(historical));self.assertIn(N,out['blocked']);self.assertIn(N2,out['scope'])
        self.api.pages[N2]['recursive_blocks'][1]['payload']['rich_text']=[p.text_run('Human edit')]
        out=e.propose_initial_scope(self.publication,self.api.pages,DS,historical,e.digest(historical));self.assertEqual(out['scope'],{})
    def test_initial_candidate_requires_separate_historical_evidence_anchor(self):
        historical=self.initial_fixture()
        with self.assertRaises(e.Blocked):e.propose_initial_scope(self.publication,self.api.pages,DS,historical,'f'*64)

    def test_malformed_reason_never_escapes_public_filter(self):
        for reason in ({},[],1,True):
            row={'status':'blocked','reason':reason,'created':0,'updated':0,'noop':0,'blocked':0,'write_calls':0,'external_write_attempts':0,'deferred':0,'retained_conflicts':0,'write_state':'unknown'}
            out=e.public_report(row);self.assertEqual(out['reason'],'other');self.assertEqual(out['write_state'],'unknown')

    def test_rule_digest_binds_real_transitive_dependency_bytes(self):
        copied=self.root/'rules';shutil.copytree(Path(e.__file__).parent,copied,ignore=shutil.ignore_patterns('__pycache__'))
        with patch.object(e,'__file__',str(copied/'get_notion_executor.py')):
            original=e.rules_sha256()
            for name in ('get_mirror_incremental_replay.py','source_sync_pack_seed.py'):
                path=copied/name;data=path.read_bytes();path.write_bytes(data+b'\n# synthetic rule change\n')
                self.assertNotEqual(e.rules_sha256(),original);path.write_bytes(data);self.assertEqual(e.rules_sha256(),original)

    def test_known_preflight_conflict_does_not_block_clean_peer(self):
        (self.source_path.parent/'b.md').write_bytes(md(N2,'Second source'));self.publish();plan=self.plan()
        def preflight(*args):raise ValueError('unsupported_prepend')
        self.api.preflight_append=preflight
        out=self.run_plan(plan);self.assertEqual(out['blocked'],1);self.assertEqual(out['created'],1);self.assertEqual(self.writes(),[('create',)])
        state,_=self.store.read();self.assertIsNone(state['journal']);self.assertEqual(state['receipts'][N]['source_commit'],self.parent);self.assertEqual(state['receipts'][N2]['source_commit'],self.publication.descriptor['commit'])
    def test_uncertain_first_record_never_skips_to_clean_peer(self):
        (self.source_path.parent/'b.md').write_bytes(md(N2,'Second source'));self.publish();plan=self.plan();self.api.throw='append_after'
        out=self.run_plan(plan);self.assertEqual(out['reason'],'uncertain_append');self.assertNotIn(N2,self.api.pages);self.assertEqual(self.writes(),[('append',)])
        state,_=self.store.read();self.assertEqual(state['journal']['index'],0);self.assertNotIn(N,state['failures']);self.assertNotIn(N2,state['receipts'])

if __name__=='__main__':unittest.main()
