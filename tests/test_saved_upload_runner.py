"""Synthetic immutable state plus real readonly stores and physical GET adapter."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'diagnostics'))
import run_saved_upload_status as r
import get_notion_saved_upload_status as d
import get_notion_png_upload as u
import get_notion_png_store as s
import get_notion_outbox as o
import get_legacy_manual as m
import get_notion_live_transport as live
import test_png_upload as fixture
from test_png_transport import Reply
from test_notion_png_store import anchor

class Tests(unittest.TestCase):
    setUp=fixture.Tests.setUp
    response=fixture.Tests.response
    def setup_runner(self):
        # Synthetic fixture only; no private journal or files in this test.
        self.b['asset_path']=s.PREFIX+self.b['sha256']+'.png'
        self.plan=u.prepare(self.b,u.digest(self.b),{self.b['asset_path']:self.b['git_blob']},self.data)
        a=anchor();a['publication_commit']=self.b['source_commit']
        self.outer=s.add_upload(s.initialize(o.initial_state(),a,u.digest(a)),self.plan,u.digest(self.plan))
        self.slot=self.outer['png_assets']['uploads']['123']
        self.slot['state'].update(phase='uploaded_verified',upload_id='2'*32,expiry_time='2030-01-01T01:00:00Z',request_sha256=u.send_fingerprint(self.plan,'2'*32))
        self.raw=o.encoded(self.outer);self.head='9'*40;self.blobs=[];self.requests=[]
        owner=self
        class Remote:
            def commit(self,commit):
                owner.assertEqual(commit,r.STATE_COMMIT);return {'tree':'root'}
            def tree_entries(self,tree):
                name,next_,kind,mode={'root':('.sync','sync','tree','040000'),'sync':('get-notion','notion','tree','040000'),'notion':('state.json',r.STATE_BLOB,'blob','100644')}[tree]
                return [{'path':name,'sha':next_,'type':kind,'mode':mode}]
            def blob(self,oid,size):
                owner.blobs.append(oid)
                return owner.raw if oid==r.STATE_BLOB else owner.data
        class Opener:
            def open(self,req,timeout):
                owner.requests.append(req)
                return Reply(owner.response('expired'))
        self.remote=Remote();self.base=live.Transport('test-only',opener=Opener(),clock=lambda:1,sleep=lambda _:None)
    def run_fixture(self,heads=None):
        with patch.object(m,'guard',side_effect=heads or [self.head,self.head]),patch.object(d,'saved_slot',return_value=self.slot):
            return r.inspect(self.remote,self.base)
    def test_later_main_exact_historical_state_real_transport_single_get(self):
        self.setup_runner();value=self.run_fixture()
        self.assertEqual(value['status'],'inspected',value)
        self.assertEqual(value['upload_status'],'expired')
        self.assertEqual(len(self.requests),1);self.assertEqual(self.requests[0].method,'GET')
        self.assertEqual(self.requests[0].full_url,'https://api.notion.com/v1/file_uploads/'+'2'*32)
        self.assertEqual(self.requests[0].get_header('Notion-version'),'2025-09-03')
        self.assertFalse(value['replacement_authorized']);self.assertEqual(value['journal_writes'],0)
    def test_bad_state_zero_notion_and_no_asset_read(self):
        self.setup_runner()
        with patch.object(m,'guard',return_value=self.head):value=r.inspect(self.remote,self.base)
        self.assertEqual(value['status'],'blocked');self.assertEqual(self.requests,[]);self.assertEqual(self.blobs,[r.STATE_BLOB])
    def test_final_head_change_invalidates_observation(self):
        self.setup_runner();value=self.run_fixture([self.head,'8'*40])
        self.assertEqual(value['reason'],'state_changed');self.assertEqual(value['requests'],1);self.assertNotIn('upload_status',value)
    def test_changed_asset_zero_physical_requests(self):
        self.setup_runner();self.data=b'wrong';value=self.run_fixture()
        self.assertEqual(value['reason'],'asset_changed');self.assertEqual(self.requests,[])
    def test_request_failure_is_private_and_not_retried(self):
        self.setup_runner()
        def bad(*a,**kw):raise RuntimeError('PRIVATE TOKEN URL')
        self.base.opener.open=bad;value=self.run_fixture()
        self.assertEqual(value['status'],'blocked');self.assertNotIn('PRIVATE',json.dumps(value));self.assertEqual(self.base.attempts,1)
    def test_wrapper_rejects_write_wrong_id_and_second_get(self):
        for kind in ('write','other','repeat'):
            self.setup_runner()
            def probe(raw,*,request):
                spec=u.spec('GET','/file_uploads/'+'2'*32)
                if kind=='write':spec=u.create_request(self.plan)
                elif kind=='other':spec=u.spec('GET','/file_uploads/'+'3'*32)
                if kind=='repeat':request(spec)
                request(spec)
            with patch.object(d,'inspect_saved_upload',side_effect=probe):value=self.run_fixture()
            self.assertEqual(value['reason'],'read_scope_changed')
            self.assertEqual(len(self.requests),1 if kind=='repeat' else 0)
    def test_permission_failure_stops_after_single_get(self):
        import urllib.error
        for code in (401,403):
            self.setup_runner()
            def denied(*a,**kw):raise urllib.error.HTTPError('https://api.notion.com',code,'PRIVATE',{},None)
            self.base.opener.open=denied;value=self.run_fixture()
            self.assertEqual(value['reason'],'permission_denied');self.assertEqual(self.base.attempts,1)
    def test_workflow_fixed_runtime_and_isolated_job(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/source-sync-shadow.yml').read_text()
        self.assertIn("inputs.mode != 'saved_create_inspection' && inputs.mode != 'saved_upload_inspection'",text)
        job=text.split('  saved-upload-inspection:')[1]
        self.assertIn('ref: '+r.A,job);self.assertIn('run_saved_upload_status.py --runtime inspected-runtime',job)
        self.assertNotIn('GITHUB_TOKEN:',job)

if __name__=='__main__':unittest.main()
