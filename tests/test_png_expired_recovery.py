"""One fixed recovery with synthetic pins; no private data or network."""
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch
import get_notion_png_expired_recovery as x
import get_notion_png_store as s
import get_notion_png_upload as u
import get_notion_outbox as o
import get_notion_remote_journal as remote
import test_notion_png_store as f
from test_notion_remote_journal import Fixture,run

class Tests(unittest.TestCase):
    adapter=f.Tests.adapter
    advance=f.Tests.advance
    uploaded=f.Tests.uploaded
    def setUp(self):
        f.Tests.setUp(self)
        self.before=deepcopy(self.uploaded())
        row={'note_id':f.IDENT,'action':'create','source':{'blob':f.BLOB,'path':'Raw/03 Get/a.md','sha256':'a'*64},
            'properties':{},'rendered':[{'type':'image','payload':{'kind':'notion_uploaded_png','upload_id':f.UPLOAD,'sha256':'a'*64},'children':[]}],
            'request_blocks':[],'warnings':[],'before_sha256':u.digest(None),'preserved_asset_bindings':{},'source_assets':{}}
        plan={'records':[{'note_id':'456'},row]}
        op={'kind':'create','phase':'prepared','response':None,'before':deepcopy(x.MISSING),'archive_index':0}
        journal={'plan':plan,'plan_sha256':u.digest(plan),'index':1,'operation':op}
        self.before['engine_state']['journal']=journal
        self.before['policy_sha256']='7'*64
        for key in ('origin','last_enqueued_publication','observed_publication_tip'):
            self.before[key]={'commit':'a'*40,'sequence':0,'receipt_sha256':None}
        slot=self.before['png_assets']['uploads'][f.IDENT]
        self.pins=patch.multiple(x,STATE_SHA=o.digest(self.before),STATE_BYTES=len(o.encoded(self.before)),PLAN_SHA=u.digest(plan),
            OP_SHA=u.digest(op),JOURNAL_SHA=u.digest(journal),SLOT_SHA=u.digest(slot),NOTE_SHA=u.digest(f.IDENT),ROW_SEMANTICS_SHA=x.row_semantics(row,f.UPLOAD),PREFLIGHT_SHA=x.preflight_sha(row['rendered'],row['source'],row['properties'],f.UPLOAD))
        self.pins.start();self.addCleanup(self.pins.stop)
        self.evidence={'query_note_id':f.IDENT,'data_source_id':f.DEST,'snapshot':deepcopy(x.MISSING),
            'upload_response':{'object':'file_upload','id':f.UPLOAD,'status':'expired','filename':slot['plan']['filename'],
             'content_type':'image/png','content_length':slot['plan']['binding']['bytes'],'expiry_time':None,'archived':False,'in_trash':False}}
    def make(self,before=None,evidence=None):
        return x.transition(self.before if before is None else before,new_policy_sha256='8'*64,evidence=self.evidence if evidence is None else evidence)
    def test_exact_transition_preserves_every_other_byte(self):
        after=self.make();restored=deepcopy(after)
        restored['policy_sha256']=self.before['policy_sha256'];restored['engine_state']['journal']=self.before['engine_state']['journal']
        del restored['png_assets']['expired_recovery'];restored['png_assets']['uploads'][f.IDENT]=self.before['png_assets']['uploads'][f.IDENT]
        self.assertEqual(o.encoded(restored),o.encoded(self.before));self.assertEqual(after['png_assets']['uploads'][f.IDENT]['state'],u.initial_state(self.plan))
        x.check_record(after['png_assets'],after['engine_state'])
        with self.assertRaises(Exception):s.preserve(self.before,after)
    def test_bad_state_and_phase_never_rearm(self):
        for key,value in [('phase','in_flight'),('phase','acknowledged'),('response','2'*32)]:
            bad=deepcopy(self.before);bad['engine_state']['journal']['operation'][key]=value
            with self.assertRaises(Exception):self.make(bad)
        for path in ('uploads','attachments'):
            bad=deepcopy(self.before);bad['png_assets'][path]={'modified':{}}
            with self.assertRaises(Exception):self.make(bad)
        with self.assertRaises(x.Blocked):self.make(self.make())
    def test_fresh_exact_expired_and_exact_missing_both_required(self):
        for key,value in [('status','uploaded'),('status','failed'),('id','9'*32),('content_length',True),('archived',None)]:
            e=deepcopy(self.evidence);e['upload_response'][key]=value
            with self.assertRaises(Exception):self.make(evidence=e)
        for key,value in [('query_note_id','456'),('data_source_id','9'*32),('snapshot',{})]:
            e=deepcopy(self.evidence);e[key]=value
            with self.assertRaises(Exception):self.make(evidence=e)
        e=deepcopy(self.evidence);e['snapshot']['exact_id_matches']=['9'*32]
        with self.assertRaises(Exception):self.make(evidence=e)
    def test_record_cannot_be_removed_changed_or_old_plan_restored(self):
        after=self.make();x.preserve(after,deepcopy(after))
        for kind in ('remove','generation','old_slot','journal'):
            bad=deepcopy(after)
            if kind=='remove':del bad['png_assets']['expired_recovery']
            elif kind=='generation':bad['png_assets']['expired_recovery']['generation']=2
            elif kind=='old_slot':bad['png_assets']['expired_recovery']['old_slot']['state']['upload_id']='8'*32
            else:bad['engine_state']['journal']=deepcopy(self.before['engine_state']['journal'])
            with self.assertRaises(Exception):
                x.preserve(after,bad);x.check_record(bad['png_assets'],bad['engine_state'])
    def test_new_plan_only_same_text_source_properties_image_id(self):
        after=self.make();active=after['png_assets']['uploads'][f.IDENT]
        active['state']=deepcopy(self.before['png_assets']['uploads'][f.IDENT]['state']);active['state'].update(upload_id='8'*32,request_sha256=u.send_fingerprint(self.plan,'8'*32))
        row=deepcopy(self.before['engine_state']['journal']['plan']['records'][1]);row['rendered'][0]['payload']['upload_id']='8'*32
        x.check_new_row(row,after['png_assets'])
        for key,value in [('source',{}),('properties',{'changed':True}),('warnings',['changed'])]:
            bad=deepcopy(row);bad[key]=value
            with self.assertRaises(x.Blocked):x.check_new_row(bad,after['png_assets'])
    def test_saved_generation_resumes_same_id_not_second_transition(self):
        after=self.make();self.assertEqual(x.only_note_id(after),f.IDENT)
        with self.assertRaises(x.Blocked):self.make(after)
        self.assertEqual(after['png_assets']['expired_recovery']['generation'],1)

    def test_preflight_before_upload_matches_source_properties_and_text(self):
        after=self.make();row=self.before['engine_state']['journal']['plan']['records'][1]
        self.assertTrue(x.check_preflight(after,f.IDENT,row['rendered'],row['source'],row['properties']))
        for key in ('rendered','source','properties'):
            bad=deepcopy(row)
            if key=='rendered':bad[key].append({'type':'paragraph','payload':{'text':'changed'},'children':[]})
            else:bad[key]['changed']=True
            with self.assertRaises(x.Blocked):x.check_preflight(after,f.IDENT,bad['rendered'],bad['source'],bad['properties'])
    def test_queue_selects_only_replacement_even_when_peer_first(self):
        after=self.make();actual={}
        for ident in ('456',f.IDENT):
            actual[ident]={'path':'Raw/03 Get/'+ident+'.md','blob':'b'*40,'sha256':'c'*64}
            after['pending_by_id'][ident]={**actual[ident],'commit':'a'*40,'parent':'b'*40,'receipt_sha256':'c'*64,'sequence':1}
        after['rotation_order']=['456',f.IDENT]
        after['last_enqueued_publication']=after['observed_publication_tip']={'commit':'a'*40,'sequence':1,'receipt_sha256':'c'*64}
        class Git:
            def __init__(self,*a):pass
            def tree(self,*a):return {}
        with patch.object(o.git,'OfflineGit',Git),patch.object(o,'verified_publication',return_value=({'sequence':1},actual)),patch.object(o.QueuePublication,'check'):
            pub=o.QueuePublication('/synthetic',after,o.digest(after))
        self.assertEqual(list(pub.descriptor['records']),[f.IDENT])
    def test_real_git_bad_fresh_evidence_has_no_cas(self):
        with tempfile.TemporaryDirectory() as folder:
            git=Fixture(folder);head=git.other_commit(remote.STATE_PATH,o.encoded(self.before))
            blob=run(git.bare,'rev-parse',head+':'+remote.STATE_PATH).decode().strip()
            store=git.store({'commit':head,'state_blob':blob,'state_sha256':o.digest(self.before)})
            evidence=deepcopy(self.evidence);evidence['upload_response']['status']='uploaded'
            with self.assertRaises(remote.JournalFailure):store.compare_and_set_png_expired(o.digest(self.before),'8'*64,lambda:evidence)
            self.assertEqual(git.calls,[]);self.assertEqual(git.head(),head)

    def test_real_git_special_cas_and_fresh_instance_no_second_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            git=Fixture(folder);head=git.other_commit(remote.STATE_PATH,o.encoded(self.before))
            blob=run(git.bare,'rev-parse',head+':'+remote.STATE_PATH).decode().strip()
            anchor={'commit':head,'state_blob':blob,'state_sha256':o.digest(self.before)}
            store=git.store(anchor);observations=[]
            def observe():observations.append(True);return deepcopy(self.evidence)
            git.mode='accepted_throw'
            value,sha=store.compare_and_set_png_expired(o.digest(self.before),'8'*64,observe)
            self.assertEqual(len(git.calls),1);self.assertEqual(len(observations),1);self.assertEqual(sha,o.digest(value))
            self.assertEqual(value['png_assets']['expired_recovery']['generation'],1)
            fresh=git.store(store.anchor)
            self.assertEqual(fresh.read()[1],sha)
            with self.assertRaises(remote.JournalFailure):fresh.compare_and_set_png_expired(sha,'8'*64,observe)
            self.assertEqual(len(git.calls),1);self.assertEqual(len(observations),1)
    def test_fixed_permission_reason_survives_without_private_suffix(self):
        for reason,expected in [('permission_denied','permission_denied'),('permission_denied PRIVATE','journal_failed')]:
            with tempfile.TemporaryDirectory() as folder:
                git=Fixture(folder);head=git.other_commit(remote.STATE_PATH,o.encoded(self.before))
                blob=run(git.bare,'rev-parse',head+':'+remote.STATE_PATH).decode().strip()
                store=git.store({'commit':head,'state_blob':blob,'state_sha256':o.digest(self.before)})
                def observe():raise ValueError(reason)
                with self.assertRaises(remote.JournalFailure) as caught:store.compare_and_set_png_expired(o.digest(self.before),'8'*64,observe)
                self.assertEqual(str(caught.exception),expected);self.assertEqual(caught.exception.outcome,'not_attempted');self.assertEqual(git.calls,[])

    def test_real_git_ordinary_cas_cannot_replace_or_remove_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            git=Fixture(folder);head=git.other_commit(remote.STATE_PATH,o.encoded(self.before))
            blob=run(git.bare,'rev-parse',head+':'+remote.STATE_PATH).decode().strip()
            store=git.store({'commit':head,'state_blob':blob,'state_sha256':o.digest(self.before)})
            with self.assertRaises(remote.JournalFailure):store.compare_and_set(o.digest(self.before),self.make())
            self.assertEqual(git.calls,[])

if __name__=='__main__':unittest.main()
