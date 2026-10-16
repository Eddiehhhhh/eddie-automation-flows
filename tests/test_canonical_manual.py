"""Unified manual engine tests; real Git objects and simulated remote only."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_canonical_manual as g
from get_canonical_manual import n,m,a,r
import test_legacy_manual as fixtures

class CanonicalManualTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CanonicalPrepareTests(methodName='test_fixed_preview_only_exact_candidate_preserves_every_existing_path');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.reader=self.f.reader;self.evidence=self.f.evidence;self.parent=self.f.parent;self.sha=self.f.sha
        self.f.f.summaries=[v for v in self.f.f.summaries if v.get('id')!='77']
    def prepare(self,parent=None,sha=None,registry=None,source=None,first=True):
        parent=parent or self.parent
        return g.prepare(self.reader,self.evidence,parent,n.git_inventory(self.reader,parent),source or self.f.source(),legacy_sha=sha or self.sha,registry_sha=registry,preview=self.f.preview if first else None)
    def report(self,plan,files):
        return {'status':'canonical_candidate_verified','main_updated':False,'cas_attempts':0,'readback_verified':True,'changed_paths':len(files),'notes_changed':sum(p not in (r.GET+'/'+a.STATE,r.GET+'/'+n.STATE) for p in files),'changed_bytes':sum(map(len,files.values())),'transaction_bytes':plan['transaction_bytes'],**{k:plan[k] for k in ('parent_commit','candidate_commit','state_sha256','registry_sha256','approval_payload_sha256')},'plan_sha256':a.sha(a.encoded(plan)),'conflict_reason_counts':plan['conflict_reason_counts'],**plan['counts']}
    def test_metadata_only_update_reports_state_change_without_note_change(self):
        first,_=self.prepare();notes=deepcopy(self.f.notes);notes['200']['version']=1
        plan,files=self.prepare(first['candidate_commit'],registry=first['registry_sha256'],source=self.f.source(notes),first=False)
        self.assertEqual(plan['counts']['new_state_only'],1);self.assertEqual(plan['counts']['new_updated'],0);self.assertEqual(list(files),[r.GET+'/'+n.STATE]);self.assertEqual(g.public_report(self.report(plan,files))['status'],'canonical_candidate_verified')
        repeat,writes=self.prepare(plan['candidate_commit'],registry=plan['registry_sha256'],source=self.f.source(notes),first=False);self.assertEqual(writes,{})
    def test_transport_envelope_and_json_format_do_not_change_logical_approval(self):
        source=self.f.source();original=source.detail_raw
        source.detail_raw=lambda ident:a.encoded({**json.loads(original(ident)),'request_id':'synthetic-request-one','meta':{'timestamp':'first'}})
        first,files=self.prepare(source=source)
        source=self.f.source();original2=source.detail_raw
        source.detail_raw=lambda ident:json.dumps({**json.loads(original2(ident)),'request_id':'synthetic-request-two','meta':{'timestamp':'second'}},indent=2).encode()
        second,other=self.prepare(source=source)
        self.assertEqual(files,other);self.assertEqual(first['approval_payload_sha256'],second['approval_payload_sha256']);self.assertNotEqual(first['wire_receipts_sha256'],second['wire_receipts_sha256'])
    def test_first_preview_import_one_candidate_and_repeat_noop(self):
        refs=self.f.f.git('show-ref');plan,files=self.prepare()
        self.assertEqual(plan['counts']['new_preview_imported'],2);self.assertEqual(len(files),3);self.assertEqual(plan['state_sha256'],self.sha)
        self.assertEqual(self.reader.read(plan['candidate_commit'],r.GET+'/'+a.STATE),self.f.current[a.STATE]);self.assertEqual(self.f.f.git('show-ref'),refs)
        registry=json.loads(files[r.GET+'/'+n.STATE]);self.assertEqual(registry['scan_cursor'],'201')
        source=self.f.source();repeat,writes=self.prepare(plan['candidate_commit'],registry=plan['registry_sha256'],source=source,first=False)
        self.assertEqual(writes,{});self.assertEqual(source.ids,[]);self.assertEqual(repeat['candidate_commit'],plan['candidate_commit']);self.assertEqual(g.public_report(self.report(repeat,writes))['status'],'canonical_candidate_verified')
    def test_clean_legacy_update_new_import_and_conflict_share_one_candidate(self):
        summaries=deepcopy(self.f.f.summaries)+list(self.f.notes.values());summaries[0].update(updated_at=1776968999,content='Changed exact old source')
        source=self.f.source(summaries=summaries);old=source.detail_raw
        source.detail_raw=lambda ident:a.encoded({'data':{'note':{**self.f.f.notes['1'],'updated_at':1776968999,'content':'Changed exact old source'}}}) if ident=='1' else old(ident)
        plan,files=self.prepare(source=source);self.assertEqual(plan['counts']['legacy_source_updated'],1);self.assertEqual(plan['counts']['new_preview_imported'],2);self.assertEqual(len(files),5)
        self.assertIn(b'Old date bytes',files[r.GET+'/1.md']);self.assertIn(b'User tail',files[r.GET+'/1.md']);self.assertEqual(g.public_report(self.report(plan,files))['status'],'canonical_candidate_verified')
    def test_changed_new_note_fetches_only_needed_record_preserves_user_edits(self):
        first,files=self.prepare();notes=deepcopy(self.f.notes);notes['200'].update(updated_at='2026-10-16 01:02:09',content='Changed summary')
        source=self.f.source(notes);plan,files=self.prepare(first['candidate_commit'],registry=first['registry_sha256'],source=source,first=False)
        self.assertEqual(source.ids,['200']);self.assertEqual(plan['counts']['new_updated'],1);self.assertEqual(len(files),2)
        self.assertIn(b'Changed summary',next(v for p,v in files.items() if p.endswith('.md')))
        self.f.f.git('checkout','-q','--detach',first['candidate_commit']);path=self.f.f.repo/r.GET/self.f.preview['200']['entry']['filePath'];path.write_bytes(path.read_bytes()+b'\nUser edit')
        parent=self.f.f.commit('human edit');self.reader=a.OfflineGit(self.f.f.repo)
        plan,files=self.prepare(parent,registry=first['registry_sha256'],source=self.f.source(notes),first=False)
        self.assertEqual(plan['counts']['new_updated'],0);self.assertEqual(plan['counts']['new_conflicted'],1);self.assertFalse(any(p.endswith('.md') for p in files))
    def test_new_scan_rotates_and_reports_remaining_without_retrying_managed(self):
        first,_=self.prepare();notes=deepcopy(self.f.notes)
        for ident in ('300','301','302'):notes[ident]={**notes['201'],'note_id':ident,'title':ident,'content':'new '+ident}
        with patch.object(g,'MAX_NEW',2):
            source=self.f.source(notes);second,files=self.prepare(first['candidate_commit'],registry=first['registry_sha256'],source=source,first=False)
            self.assertEqual(source.ids,['300','301']);self.assertEqual(second['counts']['new_remaining'],1)
            source=self.f.source(notes);third,files=self.prepare(second['candidate_commit'],registry=second['registry_sha256'],source=source,first=False)
            self.assertEqual(source.ids,['302']);self.assertEqual(third['counts']['new_remaining'],0)
        self.assertEqual(third['counts']['new_managed'],5)
    def test_failed_new_rows_do_not_starve_next_batch_and_cursor_is_validated(self):
        registry=n.initial_registry(self.evidence,self.f.legacy,self.sha,self.f.current);registry['scan_cursor']='201'
        items={str(i):{} for i in range(200,260)}
        first,remaining=g.select_new(items,registry,None);self.assertEqual(len(first),50);self.assertEqual(remaining,10);registry['scan_cursor']=first[-1]
        second,_=g.select_new(items,registry,None);self.assertTrue(set(items)-set(first)<=set(second))
        registry['scan_cursor']='opaque'
        with self.assertRaisesRegex(a.ParityFailure,'canonical_identity'):n.validate_registry(registry,a.sha(a.encoded(registry)),self.evidence,self.f.legacy,self.sha,self.f.current)
    def test_logical_payload_allows_only_unrelated_descendant_rebase(self):
        first,_=self.prepare();self.f.f.git('checkout','-q','--detach',self.parent);(self.f.f.repo/'unrelated.md').write_text('Archive change');parent=self.f.f.commit('unrelated archive');self.reader=a.OfflineGit(self.f.f.repo)
        second,_=self.prepare(parent);self.assertEqual(first['approval_payload_sha256'],second['approval_payload_sha256']);self.assertNotEqual(first['candidate_commit'],second['candidate_commit'])
        changed=deepcopy(self.f.notes);changed['200']['content']='Changed after approval';other,_=self.prepare(parent,source=self.f.source(changed));self.assertNotEqual(other['approval_payload_sha256'],first['approval_payload_sha256'])
    def test_single_cas_uncertain_success_and_noop_use_same_verified_publisher(self):
        plan,files=self.prepare();fixture=fixtures.ManualTests(methodName='test_no_source_change_adopts_only_state_without_claiming_updates');fixture.setUp();self.addCleanup(fixture.doCleanups)
        # Remote must inspect this fixture's actual candidate object store.
        fixture.f=self.f.f;fixture.parent=self.parent;remote=fixture.remote(self.parent,'uncertain_stale')
        result=m.publish(self.reader,remote,plan,files,a.sha(a.encoded(plan)),lambda commit,values:dict(values),verifier=g.verify_plan,sleep=lambda _:None,writers_guard=lambda:None,checkpoint=lambda _:None)
        self.assertTrue(result['main_updated']);self.assertEqual(len(remote.calls),1)
        again,writes=self.prepare(plan['candidate_commit'],registry=plan['registry_sha256'],first=False)
        result=m.publish(self.reader,remote,again,writes,a.sha(a.encoded(again)),lambda commit,values:dict(values),verifier=g.verify_plan,sleep=lambda _:None,writers_guard=lambda:None,checkpoint=lambda _:None);self.assertEqual(result['cas_attempts'],0);self.assertEqual(len(remote.calls),1)
    def test_apply_gate_precedes_source_and_remote_and_report_fails_closed(self):
        source=self.f.source()
        with patch.object(m,'Remote',side_effect=AssertionError('remote forbidden')):
            with self.assertRaisesRegex(m.ManualFailure,'manual_approval_required'):g.run('token',source,self.parent,legacy_sha=self.sha,apply=True)
        self.assertEqual(source.ids,[])
        plan,files=self.prepare();report=self.report(plan,files);self.assertEqual(g.public_report(report)['status'],'canonical_candidate_verified')
        for key,value in [('cas_attempts',1),('main_updated',True),('new_selected',51),('notes_changed',99),('state_sha256','bad')]:self.assertEqual(g.public_report({**report,key:value})['status'],'canonical_failed')
        for value in ({},None,[],{'error':'private response'}):self.assertIsNone(g.public_report(value)['main_updated'])
    def test_default_workflow_is_manual_and_exact_payload_is_required_for_write(self):
        workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml').read_text();step=workflow.split('- name: Prepare or explicitly apply one canonical Get transaction',1)[1].split('- name:',1)[0]
        self.assertIn('if [ "$APPLY" = true ]',step);self.assertIn('--approved-payload-sha256',step);self.assertNotIn('schedule:',workflow);self.assertIn('persist-credentials: false',workflow)

    def test_unselected_failed_new_records_are_retained_until_success(self):
        first,_=self.prepare();notes=deepcopy(self.f.notes)
        notes['300']={**notes['201'],'note_id':'300','content':'Failed source','unknown':'Unrepresented field'}
        notes['301']={**notes['201'],'note_id':'301','content':'Valid source'}
        with patch.object(g,'MAX_NEW',1):
            second,files=self.prepare(first['candidate_commit'],registry=first['registry_sha256'],source=self.f.source(notes),first=False)
            state=json.loads(files[r.GET+'/'+n.STATE]);self.assertIn('300',state['conflictsById']);self.assertNotIn('300',state['newById'])
            third,files=self.prepare(second['candidate_commit'],registry=second['registry_sha256'],source=self.f.source(notes),first=False)
            state=json.loads(files[r.GET+'/'+n.STATE]);self.assertIn('300',state['conflictsById']);self.assertIn('301',state['newById']);self.assertEqual(third['counts']['new_pending'],1)
    def test_missing_output_keeps_uncertain_checkpoint_and_no_false_no_write(self):
        plan,files=self.prepare();checkpoint=self.f.f.base/'attempt.json';output=self.f.f.base/'missing.json';m.write_checkpoint(checkpoint,plan)
        result=g.read_report(output,checkpoint);self.assertIsNone(result['main_updated']);self.assertEqual(result['write_state'],'uncertain');self.assertEqual(result['planned_candidate_commit'],plan['candidate_commit'])
    def test_current_get_change_or_rule_change_alters_approval_digest(self):
        first,_=self.prepare()
        with patch.object(m,'rules_sha256',return_value='a'*64):other,_=self.prepare()
        self.assertNotEqual(first['approval_payload_sha256'],other['approval_payload_sha256'])
        self.f.f.git('checkout','-q','--detach',self.parent);p=self.f.f.repo/r.GET/'unmanaged.md';p.write_bytes(b'User addition');parent=self.f.f.commit('Get user edit');self.reader=a.OfflineGit(self.f.f.repo)
        other,_=self.prepare(parent);self.assertNotEqual(first['approval_payload_sha256'],other['approval_payload_sha256'])

    def test_real_object_only_runtime_prepare_apply_and_noop(self):
        reader=self.reader;local=reader.local;store={oid:local.blob(oid) for mode,kind,oid in reader.tree(self.parent).values() if kind=='blob'}
        asset=reader.tree(self.parent)[r.GET+'/assets/inherited.bin'][2];(self.f.f.repo/'.git/objects'/asset[:2]/asset[2:]).unlink();refs=self.f.f.git('show-ref')
        initial_parent=self.parent
        class Remote:
            def __init__(self):self.local=local;self.head=initial_parent;self.calls=[]
            def metadata(self):return {'full_name':r.REPOSITORY,'private':True}
            def ref(self,name):return self.head if name==r.MAIN_REF else m.PROTECTED[name]
            def check_source_budget(self,commit):return {p:(*row,len(store[row[2]]) if row[2] in store else len(local.blob(row[2]))) for p,row in reader.tree(commit).items() if p.startswith(r.GET+'/')}
            def cas_main(self,old,new):assert old==self.head;self.calls.append((old,new));self.head=new
            def commit(self,commit):return {'tree':local.run('rev-parse',commit+'^{tree}').decode().strip(),'parents':local.run('rev-list','--parents','-n','1',commit).decode().split()[1:]}
            def source_get_tree(self,commit):return reader.source_get_tree(commit)
        remote=Remote()
        def fetch(remote,metadata,token,directory):return {p:store[row[2]] if row[2] in store else local.blob(row[2]) for p,row in metadata.items()}
        with patch.object(m,'Remote',return_value=remote),patch.object(r,'readback_checkout',return_value=local),patch.object(m,'load_evidence',return_value=(reader,self.evidence,self.f.current)),patch.object(m,'load_canonical_inputs',return_value=(self.f.current,self.f.preview)),patch.object(m,'fetch_selected',side_effect=fetch),patch.object(m,'disabled_writers'):
            prepared=g.run('placeholder',self.f.source(),self.parent,legacy_sha=self.sha)
            with self.assertRaisesRegex(m.ManualFailure,'manual_approval_changed'):g.run('placeholder',self.f.source(),self.parent,legacy_sha=self.sha,apply=True,approved_payload_sha256='f'*64,checkpoint=lambda p:None)
            self.assertEqual(remote.calls,[])
            result=g.run('placeholder',self.f.source(),self.parent,legacy_sha=self.sha,apply=True,approved_payload_sha256=prepared['approval_payload_sha256'],checkpoint=lambda p:None)
            assert g.public_report(result)['status']=='canonical_transaction_verified';assert len(remote.calls)==1
            repeat=g.run('placeholder',self.f.source(),remote.head,legacy_sha=result['state_sha256'],registry_sha=result['registry_sha256'])
            assert repeat['changed_paths']==0 and repeat['source_details']==0
        assert refs==self.f.f.git('show-ref');assert not (self.f.f.repo/'.git/objects'/asset[:2]/asset[2:]).exists()

    def test_cli_rejects_missing_apply_permission_before_credentials(self):
        import sync_getnote_to_raw as sync
        with patch.object(sync,'load_credentials',side_effect=AssertionError('credentials forbidden')),patch('builtins.print') as output:
            result=g.main(['--apply','--expected-parent',self.parent,'--legacy-sha256',self.sha,'--checkpoint',str(self.f.f.base/'attempt')])
        self.assertEqual(result,1);report=json.loads(output.call_args[0][0]);self.assertEqual(report['error'],'manual_approval_required');self.assertFalse(report['main_updated'])
    def test_unsupported_source_detail_isolated_and_cannot_advance_owner(self):
        source=self.f.source();original=source.detail_raw
        source.detail_raw=lambda ident:a.encoded({'data':{'note':self.f.notes[ident],'private_unmapped':'text'}}) if ident=='200' else original(ident)
        plan,files=self.prepare(source=source);registry=json.loads(files[r.GET+'/'+n.STATE]);self.assertNotIn('200',registry['newById']);self.assertIn('200',registry['conflictsById']);self.assertIn('201',registry['newById']);self.assertEqual(plan['counts']['new_preview_imported'],1)
