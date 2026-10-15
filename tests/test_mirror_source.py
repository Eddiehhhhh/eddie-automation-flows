import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import unittest
from get_mirror_source import MirrorSource
class Transport:
 DETAIL_PATH='/detail'
 def __init__(self):self.calls=[]
 def fetch_note_pages(self,*a,**kw):
  self.calls.append(('list',kw));yield 1,[{'id':'1','updated_at':'2026-10-12T00:00:00Z'}],False
 def api_get(self,path,params,*a,**kw):
  self.calls.append(('detail',kw));return {'data':{'note':{'id':params['id']}}}
class AdapterTests(unittest.TestCase):
 def test_same_budget_policy_across_list_and_detail(self):
  policy=object();transport=Transport();s=MirrorSource('x','x',policy,transport,lambda n:object())
  s.enumerate();s.detail('1')
  self.assertIs(transport.calls[0][1]['response_budget'],transport.calls[1][1]['response_budget'])
  for _,kw in transport.calls:self.assertIs(kw['request_policy'],policy);self.assertEqual(kw['max_response_bytes'],2097152)
 def test_new_pass_has_new_budget_same_policy(self):
  t=Transport();p=object();s=MirrorSource('x','x',p,t,lambda n:object())
  s.enumerate();first=s.budget;s.enumerate();self.assertIsNot(first,s.budget)
  self.assertTrue(all(kw['request_policy'] is p for _,kw in t.calls))
 def test_no_detail_before_list(self):
  with self.assertRaisesRegex(ValueError,'list_required'):MirrorSource('x','x',object(),Transport(),object).detail('1')
 def test_conflicting_detail_string_ids_rejected(self):
  class Conflict(Transport):
   def api_get(self,*a,**kw):return {'note_id':'1','id':'2'}
  s=MirrorSource('x','x',object(),Conflict(),lambda n:object());s.enumerate()
  with self.assertRaisesRegex(ValueError,'invalid_identity'):s.detail('1')
 def test_transport_failure_propagates(self):
  class Fail(Transport):
   def api_get(self,*a,**kw):raise RuntimeError('quota')
  s=MirrorSource('x','x',object(),Fail(),lambda n:object());s.enumerate()
  with self.assertRaisesRegex(RuntimeError,'quota'):s.detail('1')
if __name__=='__main__':unittest.main()

class FullRunnerTests(unittest.TestCase):
 def simulate(self,failure=None):
  from unittest.mock import patch
  import contextlib,io,json
  import source_sync_mirror_replay as runner
  import source_sync_migration_check as migration
  import source_sync_pack_seed as pack
  import sync_getnote_to_raw as sync
  class FakeSource:
   def __init__(self,*args):self.pages=0;self.budget=None
   def enumerate(self):
    from types import SimpleNamespace
    self.pages=1;self.budget=SimpleNamespace(used=120)
    return [{'id':'123','note_type':'plain_text','title':'fake','content':'fake content','created_at':'2026-10-12T00:00:00Z','updated_at':'2026-10-13T00:00:00Z'}],True
   def detail(self,ident):
    self.budget.used+=200
    if failure:raise failure
    return self.enumerate()[0][0]
  def seed(root,*args):
   path=root/'03 State';path.mkdir();(path/'manifest.json').write_text('{"notes":[]}')
  output=io.StringIO()
  with patch.object(runner,'MirrorSource',FakeSource),patch.object(migration,'private_snapshot',return_value=(None,None,None,None,None,None)),patch.object(pack,'seed',side_effect=seed),patch.object(pack,'audit_actual'),patch.object(sync,'load_credentials',return_value=('fake','fake')),patch.object(sys,'argv',['source_sync_mirror_replay.py']),contextlib.redirect_stdout(output):
   code=runner.main()
  return code,json.loads(output.getvalue())
 def test_full_runner_two_pass_success_with_fake_source(self):
  code,out=self.simulate();self.assertEqual(code,0);self.assertEqual(out['status'],'mirror_replay_ok');self.assertEqual(out['first']['new_or_changed'],1);self.assertEqual(out['second']['fresh_details'],0);self.assertEqual(out['second']['api_response_bytes'],120)
 def test_full_runner_http_failure_keeps_counts_and_safe_status(self):
  code,out=self.simulate(RuntimeError('GetNote HTTP 401'));self.assertEqual(code,1);self.assertEqual(out['stage'],'detail');self.assertEqual(out['http_status'],401);self.assertEqual(out['error'],'api_auth');self.assertEqual(out['first']['enumerated'],1)
 def test_full_runner_rate_failure_keeps_reason(self):
  from source_sync_rate import RateLimitFailure
  code,out=self.simulate(RateLimitFailure('quota_day',429));self.assertEqual(code,1);self.assertEqual(out['rate_limit_reason'],'quota_day');self.assertEqual(out['http_status'],429);self.assertFalse(out['budget'])

 def test_full_runner_asset_error_has_fixed_diagnostic(self):
  from source_sync_assets import AssetError
  code,out=self.simulate(AssetError('run byte limit reached'));self.assertEqual(code,1);self.assertEqual(out['error'],'asset_failed');self.assertEqual(out['asset_error_code'],'run_byte_limit')
 def test_full_runner_unknown_asset_error_is_redacted(self):
  from source_sync_assets import AssetError
  import json
  code,out=self.simulate(AssetError('https://private.test/credential'));self.assertEqual(code,1);self.assertEqual(out['asset_error_code'],'other');self.assertNotIn('private',json.dumps(out))

 def test_every_fixed_asset_diagnostic_survives_public_whitelist(self):
  from source_sync_mirror_replay import ASSET_ERROR_CODES,safe_report
  for value in set(ASSET_ERROR_CODES.values()):
   self.assertEqual(safe_report({'error':'asset_failed','asset_error_code':value})['asset_error_code'],value)
