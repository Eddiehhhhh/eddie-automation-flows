import unittest
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock
import get_notion_runtime as r
class Tests(unittest.TestCase):
 def setUp(self):
  self.ident='123';self.pub=NS(descriptor={'records':{'123':{'sha256':'a'*64}},'commit':'a'*40})
  self.state={'engine_state':{'journal':None}};self.store=Mock();self.store.read.return_value=(self.state,'fresh-sha')
  self.loaded=NS(evidence=object(),snapshot_head='fixed-head',before_write=lambda:True)
  self.session=Mock();self.session.prepare.return_value={'prepared_only':True,'selected':3,'upload_candidates':2,'blocked':1,'upload_bytes':10,'remote_writes':0,'journal_writes':0}
  self.session.advance_uploads.return_value=object()
  self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
  for name,value in [('PNG_ASSET_ANCHOR',{'publication_commit':'a'*40}),('PNG_ASSET_ANCHOR_SHA256','a'*64)]:self.stack.enter_context(patch.object(r,name,value))
  self.stack.enter_context(patch.object(r.png_upload,'PINS',{r.png_upload.sha(b'123'):{'md_sha256':'a'*64}}))
  self.load=self.stack.enter_context(patch.object(r.png_loader,'load',return_value=self.loaded))
  self.stack.enter_context(patch.object(r.png_runtime,'Session',return_value=self.session))
  self.queue=self.stack.enter_context(patch.object(r.outbox,'QueuePublication',return_value=self.pub))
  self.adapter=self.stack.enter_context(patch.object(r.png_image,'SnapshotAdapter',return_value=NS(candidates={})))
  self.resolver=self.stack.enter_context(patch.object(r.png_image,'Resolver',return_value=object()))
  self.stack.enter_context(patch.object(r.png_store,'upload_receipts',return_value={}))
 def run_context(self,apply):return r.png_context(NS(vault='fixture'),None,None,self.store,None,self.pub,{},'policy',apply,lambda:True,None)
 def test_default_anchor_missing_blocks_before_load(self):
  with patch.object(r,'PNG_ASSET_ANCHOR',None):
   with self.assertRaisesRegex(ValueError,'png_anchor_pending'):self.run_context(True)
  self.load.assert_not_called();self.session.advance_uploads.assert_not_called()
 def test_prepare_only_has_no_upload_cas_or_queue_rebuild(self):
  out=self.run_context(False);self.session.prepare.assert_called_once();self.session.advance_uploads.assert_not_called();self.queue.assert_not_called();self.store.compare_and_set.assert_not_called();self.assertTrue(out[-1]['prepared_only'])
 def test_apply_rebuilds_publication_from_fresh_outer_sha(self):
  out=self.run_context(True);self.session.advance_uploads.assert_called_once();self.queue.assert_called_once_with('fixture',self.state,'fresh-sha',max_records=r.MAX_RUN_RECORDS);self.assertIs(out[4],self.session.verify_unattached)
 def test_acknowledged_resume_never_uploads_or_rebuilds_saved_plan(self):
  self.state['engine_state']['journal']={'operation':{'phase':'acknowledged'}}
  self.run_context(True);self.session.prepare.assert_not_called();self.session.advance_uploads.assert_not_called();self.queue.assert_not_called();self.resolver.assert_called_once()
 def test_attachment_provider_only_changed_receipts(self):
  adapter=NS(candidates={'x':{'binding':1},'y':{'binding':2}});self.adapter.return_value=adapter
  provider=self.run_context(True)[3]
  self.assertEqual(provider({'receipts':{'y':1}},{'receipts':{'x':2,'y':1}}),{'x':{'binding':1}})
 def test_preparation_public_report_is_bounded_no_private(self):
  value={'status':'png_prepared','png_preparation':self.session.prepare.return_value|{'private':'secret'}}
  report=r.safe_result(value);self.assertEqual(report['status'],'prepared');self.assertNotIn('secret',str(report));self.assertEqual(report['external_write_attempts'],0)

 def test_third_ready_candidate_blocks_before_upload(self):
  self.session.prepare.return_value['upload_candidates']=3
  with self.assertRaisesRegex(ValueError,'notion_scope'):self.run_context(True)
  self.session.advance_uploads.assert_not_called()
 def test_post_upload_pipeline_failure_cannot_report_zero_writes(self):
  value={'contract':r.pipeline.CONTRACT,'status':'failed','stage':'prepare','reason':'source_changed','complete_library_claimed':False,'write_state':'not_attempted','external_write_attempts':0,'png_upload_attempts':4}
  out=r.safe_result(value);self.assertEqual(out['external_write_attempts'],4);self.assertEqual(out['write_state'],'unknown')
 def test_fixed_anchor_hash(self):
  import json,pathlib
  # The actual module constants are patched in this fixture; validate deployment source separately.
  source=pathlib.Path(r.__file__).read_text()
  self.assertIn('a8137204a877ea80202dfb0f4d88e667a5d2ca17c9893e9c192da75c141a980c',source)

 def test_zero_ready_skips_upload_round_and_retains_capacity_reason(self):
  self.session.prepare.return_value.update(upload_candidates=0,reason_counts={'request_budget':1})
  self.run_context(True);self.session.advance_uploads.assert_not_called()
  result=r.safe_result({'status':'png_prepared','png_preparation':self.session.prepare.return_value})
  self.assertEqual(result['png']['reason_counts'],{'request_budget':1});self.assertEqual(result['write_state'],'not_attempted');self.assertEqual(r.exit_status(result),0)
 def test_diagnostic_filters_arbitrary_exception_content(self):
  self.assertNotIn('PRIVATE',str(r.safe_png_diagnostic({'private':'PRIVATE','external_write_attempts_observed':2})))
  for value in ({'last_confirmed_post_http_counts':{'200':1}},{'last_confirmed_upload_phase_counts':{'secret':1}},{'external_write_attempts_observed':True}):
   self.assertEqual(r.safe_png_diagnostic(value),{'last_confirmed_upload_state':'unknown'})

 def test_capacity_pending_does_not_activate_png_for_other_publication(self):
  state={'engine_state':{'journal':None},'rotation_order':['new','123'],'pending_by_id':{'new':{'commit':'new-commit'},'123':{'commit':'old-commit'}}}
  self.assertEqual(r.png_batch_ids(state),['new'])
  self.pub.descriptor['records']={'new':{}}
  self.run_context(True);self.load.assert_not_called();self.session.advance_uploads.assert_not_called()

 def test_changed_same_id_or_new_publication_skips_fixed_asset_loader(self):
  for delta in ({'commit':'b'*40},{'records':{'123':{'sha256':'b'*64}}}):
   original=deepcopy(self.pub.descriptor);self.pub.descriptor.update(delta)
   self.run_context(True);self.load.assert_not_called();self.pub.descriptor=original

 def test_incomplete_prefix_does_not_select_png(self):
  state={'engine_state':{'journal':None},'last_enqueued_publication':{'sequence':1},'observed_publication_tip':{'sequence':2},'rotation_order':['123'],'pending_by_id':{'123':{'commit':'a'*40}}}
  self.assertEqual(r.png_batch_ids(state),[])
 def test_observed_diagnostic_never_reads_again(self):
  store=r.ObservedStore(Mock());store.observed=None
  self.assertEqual(store.diagnostic(),{'last_confirmed_upload_state':'unknown'});store.store.read.assert_not_called()

 def test_fixed_loader_response_reason_never_leaks_suffix(self):
  for reason in ('png_loader_source_changed','response_invalid','upload_metadata_changed'):
   self.assertIn(reason,r.ERRORS)
   with patch.object(r,'PRODUCTION_ACTIVATED',True),patch.object(r.recurring,'PRODUCTION_ACTIVATED',True),patch.object(r.provenance,'AnonymousGithub',side_effect=ValueError(reason+' PRIVATE')):
    out=r.run(None,None,lambda:True)
   self.assertEqual(out['reason'],'notion_failed');self.assertNotIn('PRIVATE',str(out))
