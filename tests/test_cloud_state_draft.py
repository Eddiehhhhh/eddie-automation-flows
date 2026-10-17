import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from get_mirror_incremental_replay import export_cloud_state,load_cloud_state,one_pass,ParityFailure,snapshot

class CloudStateDraftTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)/'get';self.root.mkdir()
  self.path=self.root/'03 纯文本记录'/'User path.md';self.path.parent.mkdir();self.path.write_text('---\nnote_id: "123"\nupdated_at: "2026-10-13T08:00:00+08:00"\n---\nUser body\n')
  state_dir=self.root/'03 State';state_dir.mkdir();(state_dir/'manifest.json').write_text(json.dumps({'notes':[{'note_id':'123','updated_at':'2026-10-13T08:00:00+08:00','path':'03 纯文本记录/User path.md'}]}))
  self.summary={'id':'123','updated_at':'2026-10-13 08:00:00'};self.before=snapshot(self.root)
 def prepare(self):
  state,count,_=one_pass(self.root,{},lambda:([deepcopy(self.summary)],True),lambda _:deepcopy(self.summary),1000)
  self.assertEqual(count['adopted_after_detail'],1)
  return export_cloud_state(self.root,state)
 def test_cross_process_reuses_verified_state_without_detail(self):
  payload=self.prepare();statefile=Path(self.temp.name)/'state.json';statefile.write_text(json.dumps(payload))
  code="""import json,sys
from pathlib import Path
from get_mirror_incremental_replay import load_cloud_state,one_pass,export_cloud_state
root=Path(sys.argv[1]);payload=json.loads(Path(sys.argv[2]).read_text());state=load_cloud_state(root,payload)
def forbidden(_):raise AssertionError('unchanged must not fetch detail')
result,counts,_=one_pass(root,state,lambda:([{'id':'123','updated_at':'2026-10-13 08:00:00'}],True),forbidden,2000)
assert counts['cached_skipped']==1 and counts['fresh_details']==0
assert export_cloud_state(root,result)==payload
"""
  env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'scripts')+os.pathsep+os.environ.get('PYTHONPATH','')}
  subprocess.run([sys.executable,'-c',code,str(self.root),str(statefile)],env=env,check=True,capture_output=True)
  self.assertEqual(snapshot(self.root),self.before)
 def test_user_edits_missing_and_corrupt_state_fail_closed(self):
  payload=self.prepare();self.path.write_text(self.path.read_text()+'manual edit\n')
  with self.assertRaisesRegex(ParityFailure,'cloud_mirror_changed'):load_cloud_state(self.root,payload)
  self.path.unlink()
  with self.assertRaises(ParityFailure):load_cloud_state(self.root,payload)
  for bad in ({},[],{'version':2},None):
   if bad is None:self.assertEqual(load_cloud_state(self.root,None),{})
   else:
    with self.assertRaises(ParityFailure):load_cloud_state(self.root,bad)
 def test_pending_not_successful_and_forces_next_process_detail(self):
  payload=self.prepare();payload['pendingById']={'456':{'updatedAt':1791849600000}}
  state=load_cloud_state(self.root,payload);calls=[]
  pending={'id':'456','updated_at':'2026-10-13 08:00:00','note_type':'recorder_audio','content':'summary'}
  nextstate,count,_=one_pass(self.root,state,lambda:([self.summary,pending],True),lambda ident:(calls.append(ident) or deepcopy(pending)),2000)
  self.assertEqual(calls,['456']);self.assertEqual(count['pending'],1);self.assertNotIn('api:456',nextstate['notesByCanonicalId'])
  output=export_cloud_state(self.root,nextstate);self.assertEqual(set(output['pendingById']['456']),{'updatedAt'})
  self.assertEqual(snapshot(self.root),self.before)
 def test_state_cannot_inject_path_or_wrong_version(self):
  payload=self.prepare()
  for key,value in [('filePath','../../escape'),('updatedAt',0),('mirrorSha256','wrong'),('deleted',True)]:
   bad=deepcopy(payload);bad['notesByCanonicalId']['api:123'][key]=value
   with self.assertRaises(ParityFailure):load_cloud_state(self.root,bad)

 def test_internal_state_version_types_and_contract_are_strict(self):
  payload=self.prepare()
  for value in (True,1.0,'1'):
   bad=deepcopy(payload);bad['version']=value
   with self.assertRaises(ParityFailure):load_cloud_state(self.root,bad)
  for value in (True,1791849600000.0,'1791849600000',float('inf')):
   bad=deepcopy(payload);bad['notesByCanonicalId']['api:123']['updatedAt']=value
   with self.assertRaises(ParityFailure):load_cloud_state(self.root,bad)
  bad=deepcopy(payload);bad['contract']='unknown'
  with self.assertRaises(ParityFailure):load_cloud_state(self.root,bad)

 def test_bounded_reader_duplicate_keys_and_limits(self):
  from get_mirror_incremental_replay import read_cloud_state
  payload=self.prepare();path=self.root/'03 State'/'cloud-sync-state.json';path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(payload))
  self.assertEqual(read_cloud_state(self.root,path),payload)
  for data in ('null','{"version":1,"version":1}',json.dumps(payload).replace('"updatedAt":','"updatedAt":1,"updatedAt":',1),' '*(2*1024*1024+1),'{"bad":NaN}','['*10000):
   path.write_text(data)
   with self.assertRaises(ParityFailure):read_cloud_state(self.root,path)
  bad=deepcopy(payload);bad['pendingById']={str(i):{'updatedAt':1791849600000} for i in range(2501)}
  with self.assertRaises(ParityFailure):load_cloud_state(self.root,bad)
  outside=Path(self.temp.name)/'outside';outside.write_text(json.dumps(payload));path.unlink();path.symlink_to(outside)
  with self.assertRaises(ParityFailure):read_cloud_state(self.root,path)
 def test_pending_versions_are_not_loose_api_values(self):
  payload=self.prepare()
  for value in (True,'1791849600000',1791849600000.0,0,float('nan')):
   bad=deepcopy(payload);bad['pendingById']={'456':{'updatedAt':value}}
   with self.assertRaises(ParityFailure):load_cloud_state(self.root,bad)

 def test_manifest_missing_or_conflicting_cannot_load_or_export(self):
  payload=self.prepare();manifest=self.root/'03 State'/'manifest.json';manifest.unlink()
  with self.assertRaisesRegex(ParityFailure,'invalid_manifest'):load_cloud_state(self.root,payload)
  manifest.write_text(json.dumps({'notes':[{'note_id':'other','updated_at':self.summary['updated_at'],'path':'03 纯文本记录/User path.md'}]}))
  with self.assertRaisesRegex(ParityFailure,'manifest_identity_conflict'):load_cloud_state(self.root,payload)
 def test_old_success_entry_pending_overlap_retains_old_watermark(self):
  payload=self.prepare();payload['pendingById']={'123':{'updatedAt':1791936000000}}
  state=load_cloud_state(self.root,payload);changed={'id':'123','updated_at':'2026-10-14 08:00:00','note_type':'recorder_audio','content':'summary'};calls=[]
  result,counts,_=one_pass(self.root,state,lambda:([changed],True),lambda i:(calls.append(i) or changed),2000)
  output=export_cloud_state(self.root,result);self.assertEqual(output['notesByCanonicalId'],payload['notesByCanonicalId']);self.assertEqual(counts['pending'],1);self.assertEqual(calls,['123']);self.assertEqual(snapshot(self.root),self.before)

 def test_export_size_is_bounded_even_after_validation(self):
  from unittest.mock import patch
  from get_mirror_incremental_replay import export_cloud_state
  state,count,_=one_pass(self.root,{},lambda:([self.summary],True),lambda _:self.summary,1000)
  with patch('get_mirror_incremental_replay.load_cloud_state',return_value={'synthetic_size_probe':'x'*(2*1024*1024)}):
   with self.assertRaises(ParityFailure):export_cloud_state(self.root,state)
