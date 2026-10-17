from copy import deepcopy
import unittest
import get_notion_journal_runtime as r
import test_notion_journal_runtime as f
class Tests(unittest.TestCase):
 setUp=f.CheckpointTests.setUp
 api=f.CheckpointTests.api
 recover=f.CheckpointTests.recover
 discover=f.DiscoveryTests.discover
 def split(self):
  a=f.DiscoveryTests.api(self);a.overrides['/actions/runs/1001/attempts/1']={**a.row,'updated_at':'2026-10-16T10:06:01Z'};return a
 def test_direct_recover_attempt_newer_than_alias_is_valid(self):
  a=self.split();out=self.recover(a)
  self.assertEqual(out['run_binding']['updated_at'],'2026-10-16T10:06:01Z');self.assertEqual(out['latest_binding']['updated_at'],'2026-10-16T10:06:00Z')
 def test_discover_split_timestamps_valid(self):
  out=self.discover(f.MultiAPI([self.split()]));self.assertEqual(out['run_id'],1001)
 def test_same_attempt_reread_timestamp_drift_rejected(self):
  a=self.split();original=a.json;count=[0]
  def read(path):
   value=original(path)
   if path.endswith('/attempts/1'):
    count[0]+=1
    if count[0]==2:value['updated_at']='2026-10-16T10:06:02Z'
   return value
  a.json=read
  with self.assertRaisesRegex(r.CheckpointFailure,'changed'):self.recover(a)
 def test_alias_backward_from_listing_rejected(self):
  a=self.split();a.overrides['/actions/runs/1001']={**a.row,'updated_at':'2026-10-16T10:05:59Z'}
  with self.assertRaisesRegex(r.CheckpointFailure,'changed'):self.discover(f.MultiAPI([a]))
 def test_semantic_cross_endpoint_drift_rejected(self):
  for key,value in [('conclusion','cancelled'),('head_sha','f'*40),('run_attempt',0)]:
   a=self.split();a.overrides['/actions/runs/1001']={**a.row,key:value}
   with self.assertRaises(r.CheckpointFailure):self.recover(a)
 def prior(self):
  a=self.split();old=deepcopy(a.overrides['/actions/runs/1001/attempts/1'])
  a.row.update(run_attempt=2,status='in_progress',conclusion=None)
  a.overrides['/actions/runs/1001/attempts/1']=old
  return a
 def test_previous_attempt_newer_timestamp_than_latest_alias_valid(self):
  out=self.discover(f.MultiAPI([self.prior()]));self.assertEqual(out['run_attempt'],1);self.assertEqual(out['latest_attempt'],2)
 def test_previous_attempt_candidate_full_timestamp_still_bound(self):
  a=self.prior();original=a.json;count=[0]
  def read(path):
   value=original(path)
   if path.endswith('/attempts/1'):
    count[0]+=1
    if count[0]>1:value['updated_at']='2026-10-16T10:06:02Z'
   return value
  a.json=read
  with self.assertRaisesRegex(r.CheckpointFailure,'changed'):self.discover(f.MultiAPI([a]))
 def test_job_upper_bound_remains_original_attempt(self):
  a=self.split();a.job['completed_at']='2026-10-16T10:06:02Z'
  a.overrides['/actions/runs/1001']={**a.row,'updated_at':'2026-10-16T10:07:00Z'}
  with self.assertRaisesRegex(r.CheckpointFailure,'job'):self.recover(a)
