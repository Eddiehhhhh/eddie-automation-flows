from copy import deepcopy
from pathlib import Path
from git_object_fixture import assert_missing, disable_auto_maintenance, remove_objects
import json,sys,subprocess,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_outbox as o
import get_notion_executor as e
import get_recurring_entry as r

P='a'*64
N=lambda x:str(1000000000000000000+x)
def md(ident,text):return ('---\nnote_id: '+json.dumps(ident)+'\ntitle: "Synthetic"\n---\n'+text+'\n').encode()
class Store:
 def __init__(self):self.value=o.initial_state();self.count=0
 def read(self):return deepcopy(self.value),o.digest(self.value)
 def compare_and_set(self,sha,value):
  o.check_state(value)
  if sha!=o.digest(self.value):raise o.Blocked('outbox_state_changed')
  self.value=deepcopy(value);self.count+=1;return o.digest(value)
class Tests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
  self.git('init','-q','-b','main');self.git('config','user.name','Synthetic');self.git('config','user.email','synthetic@example.invalid')
  disable_auto_maintenance(self.root)
  self.write('Raw/03 Get/03 State/legacy.json',b'{}');self.origin={'commit':self.commit(),'receipt_sha256':None,'sequence':0};self.last=self.origin;self.rows=[];self.store=Store()
 def git(self,*args):return subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True).stdout
 def write(self,path,data):p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
 def commit(self):self.git('add','.');self.git('commit','-qm','fixture');return self.git('rev-parse','HEAD').decode().strip()
 def publication(self,changes,*,bad_ids=None,bad_previous=None):
  parent=self.git('rev-parse','HEAD').decode().strip();reader=o.git.OfflineGit(self.root)
  for ident,text in changes.items():self.write('Raw/03 Get/notes/'+ident+'.md',md(ident,text))
  # A local object-only draft tree allows the receipt to hash the exact delta;
  # it never moves a ref or makes a source read.
  self.write('Raw/03 Get/03 State/legacy.json',json.dumps({'n':self.last['sequence']+1}).encode());self.git('add','.')
  tree=self.git('write-tree').decode().strip();draft=self.git('commit-tree',tree,'-p',parent,'-m','object draft').decode().strip()
  counts={k:0 for k in r.engine.COUNTS if k not in ('changed_paths','notes_changed','changed_bytes','transaction_bytes')};counts['legacy_source_updated']=len(changes)
  receipt={'contract':r.RECEIPT_CONTRACT,'policy_sha256':P,'sequence':self.last['sequence']+1,'parent_commit':parent,'previous_receipt_commit':self.last['commit'],'previous_receipt_sha256':self.last['receipt_sha256'],'before_get_tree':reader.source_get_tree(parent),'previous_legacy_sha256':'b'*64,'previous_registry_sha256':None if self.last['sequence']==0 else 'c'*64,'state_sha256':'b'*64,'registry_sha256':'c'*64,'delta_sha256':r.a.sha(r.a.encoded(r.delta(reader,parent,draft))),'engine_payload_sha256':'d'*64,'source_summary_sha256':'e'*64,'detail_receipts_sha256':'f'*64,'changed_content_ids':sorted(changes) if bad_ids is None else bad_ids,'counts':counts,'conflict_reason_counts':{}}
  if bad_previous:receipt['previous_receipt_commit']=bad_previous
  raw=r.a.encoded(receipt);self.write(r.FULL_RECEIPT,raw);commit=self.commit();row={'commit':commit,'parent':parent,'receipt_sha256':o.digest(raw)}
  self.last={'commit':commit,'receipt_sha256':row['receipt_sha256'],'sequence':receipt['sequence']};self.rows.append(row);return row
 def ingest(self,rows=None,*,chain_changes=None):
  state,sha=self.store.read();chain={'repository':o.p.REPOSITORY,'policy_sha256':P,'origin':self.origin,'after':state['last_enqueued_publication'] or self.origin,'tip':self.last,'publications':self.rows if rows is None else rows}
  if chain_changes:chain.update(chain_changes)
  value=o.enqueue(self.root,state,sha,chain,o.digest(chain));self.store.compare_and_set(sha,value);return value
 def pub(self):
  state,sha=self.store.read();return o.QueuePublication(self.root,state,sha)
 def policy(self):
  return {'data_source_id':'d'*32,'id_property':'笔记 ID','title_property':'Title','allow_create':True,'allow_archive':True,'format_accepted':True,'old_writer_evidence':{'fresh_readback_verified':True,'writers':{name:{'disabled':True,'active_runs':0} for name in ('getnote-to-notion','notion-to-getnote')}}}
 def blocked_batch(self):
  pub=self.pub();adapter=o.EngineStore(self.store);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{},self.policy());out=e.execute(plan,e.digest(plan),pub,None,adapter,expected_store_sha=sha,writers_guard=lambda:self.policy()['old_writer_evidence']);return plan,out
 def test_51_actual_changed_notes_persist_all_and_rotate_blocked_first_50(self):
  self.publication({N(i):'body' for i in range(51)});self.ingest();self.assertEqual(len(self.store.value['pending_by_id']),51)
  first=list(self.pub().descriptor['records']);self.assertEqual(len(first),50)
  plan,out=self.blocked_batch();self.assertEqual(out['blocked'],50);self.assertEqual(len(self.store.value['pending_by_id']),51)
  self.assertEqual(self.store.value['rotation_order'][0],N(50));self.assertIn(N(50),self.pub().descriptor['records']);self.assertNotIn('receipts',self.store.value)
 def test_smaller_runtime_batch_retains_51_and_rotates_next_ten(self):
  self.publication({N(i):'body' for i in range(51)});self.ingest()
  state,sha=self.store.read();pub=o.QueuePublication(self.root,state,sha,max_records=10)
  self.assertEqual(len(pub.descriptor['records']),10);adapter=o.EngineStore(self.store);inner,inner_sha=adapter.read()
  plan=e.prepare(pub,inner,inner_sha,{},self.policy());e.execute(plan,e.digest(plan),pub,None,adapter,expected_store_sha=inner_sha,writers_guard=lambda:self.policy()['old_writer_evidence'])
  state,sha=self.store.read();second=o.QueuePublication(self.root,state,sha,max_records=10)
  self.assertEqual(len(state['pending_by_id']),51);self.assertTrue(set(pub.descriptor['records']).isdisjoint(second.descriptor['records']))
 def test_batch_limit_strict_and_resume_preserves_original_plan(self):
  self.publication({N(i):'body' for i in range(12)});self.ingest();state,sha=self.store.read()
  for limit in (True,False,0,51,1.0,'10'):
   with self.assertRaises(o.Blocked):o.QueuePublication(self.root,state,sha,max_records=limit)
  pub=o.QueuePublication(self.root,state,sha,max_records=10);adapter=o.EngineStore(self.store);inner,inner_sha=adapter.read();plan=e.prepare(pub,inner,inner_sha,{},self.policy())
  inner['journal']={'plan':plan,'plan_sha256':e.digest(plan),'index':0,'operation':{'kind':'append','phase':'in_flight'}};adapter.compare_and_set(inner_sha,inner)
  state,sha=self.store.read();resumed=o.QueuePublication(self.root,state,sha,resume=True,max_records=1)
  self.assertEqual(resumed.descriptor,pub.descriptor);self.assertEqual(len(resumed.descriptor['records']),10)
 def test_new_publication_coalesces_same_id_and_retains_other_pending(self):
  old=self.publication({N(1):'one',N(2):'two'});self.ingest();new=self.publication({N(1):'changed',N(3):'three'});self.ingest([new])
  pending=self.store.value['pending_by_id'];self.assertEqual(set(pending),{N(1),N(2),N(3)});self.assertEqual(pending[N(1)]['commit'],new['commit']);self.assertEqual(pending[N(2)]['commit'],old['commit'])
 def test_state_only_advances_cursor_but_does_not_drop_or_add_ids(self):
  self.publication({N(1):'one'});self.ingest();pending=deepcopy(self.store.value['pending_by_id']);row=self.publication({});self.ingest([row]);self.assertEqual(self.store.value['pending_by_id'],pending);self.assertEqual(self.store.value['last_enqueued_publication'],self.last)
  before=self.store.read();self.ingest([]);self.assertEqual(self.store.read(),before)
 def test_state_only_initial_receipt_produces_zero_queue(self):
  self.publication({});self.ingest();self.assertFalse(self.store.value['pending_by_id'])
  with self.assertRaisesRegex(o.Blocked,'outbox_empty'):self.pub()
 def test_fixed_receipt_hash_parent_ids_policy_and_chain_link_are_verified(self):
  self.publication({N(1):'one'},bad_ids=[])
  with self.assertRaisesRegex(o.Blocked,'outbox_chain_invalid'):self.ingest()
  self.assertEqual(self.store.value,o.initial_state())
 def test_wrong_receipt_hash_and_policy_fail_without_queue_mutation(self):
  self.publication({N(1):'one'});bad=deepcopy(self.rows);bad[0]['receipt_sha256']='f'*64
  with self.assertRaises(Exception):self.ingest(bad)
  with self.assertRaises(Exception):self.ingest(chain_changes={'policy_sha256':'f'*64})
  self.assertEqual(self.store.value,o.initial_state())
 def test_cannot_skip_an_intermediate_get_publication(self):
  self.publication({N(1):'one'});last=self.publication({N(2):'two'})
  with self.assertRaisesRegex(o.Blocked,'outbox_chain_invalid'):self.ingest([last])
 def test_foreign_get_edit_and_revert_between_receipts_rejects(self):
  first=self.publication({N(1):'one'});self.ingest();path='Raw/03 Get/notes/'+N(1)+'.md';old=(self.root/path).read_bytes();self.write(path,md(N(1),'foreign'));self.commit();self.write(path,old);self.commit();row=self.publication({N(2):'two'})
  with self.assertRaises(Exception):self.ingest([row])
  self.assertEqual(self.store.value['last_enqueued_publication']['commit'],first['commit'])
 def test_unrelated_state_commits_preserved_and_queue_source_still_bound(self):
  self.publication({N(1):'one'});self.ingest();self.write('.sync/get-notion/fixture.json',b'{}');self.commit();pub=self.pub();pub.check(pub.descriptor)
  self.write('Raw/03 Get/notes/'+N(1)+'.md',md(N(1),'foreign'));self.commit()
  with self.assertRaisesRegex(e.Blocked,'source_changed'):self.pub()
 def test_inflight_journal_blocks_new_enqueue_and_is_not_replaced(self):
  self.publication({N(1):'one'});self.ingest();pub=self.pub();adapter=o.EngineStore(self.store);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{},self.policy());state['journal']={'plan':plan,'plan_sha256':e.digest(plan),'index':0,'operation':{'kind':'append','phase':'in_flight'}};adapter.compare_and_set(sha,state)
  row=self.publication({N(2):'two'});before=self.store.read()
  with self.assertRaisesRegex(o.Blocked,'journal_busy'):self.ingest([row])
  self.assertEqual(self.store.read(),before)
 def test_queue_codec_rejects_missing_ids_duplicate_order_and_bad_rows(self):
  self.publication({N(1):'one'});self.ingest();valid=self.store.value
  for change in ({'rotation_order':[]},{'rotation_order':[N(1),N(1)]},{'pending_by_id':[]},{'extra':'private'}):
   with self.assertRaises(o.Blocked):o.check_state(dict(valid,**change))
  bad=deepcopy(valid);bad['pending_by_id'][N(1)]['sequence']=True
  with self.assertRaises(o.Blocked):o.check_state(bad)
 def test_outer_cas_rejects_concurrent_queue_even_with_same_engine_hash(self):
  self.publication({N(1):'one'});self.ingest();adapter=o.EngineStore(self.store);state,sha=adapter.read();row=self.publication({N(2):'two'});self.ingest([row])
  with self.assertRaisesRegex(o.Blocked,'outbox_state_changed'):adapter.compare_and_set(sha,state)
 def test_real_create_dequeues_atomically_and_repeat_does_not_redeliver(self):
  from test_notion_executor import API
  self.publication({N(1):'one'});self.ingest();pub=self.pub();adapter=o.EngineStore(self.store);api=API(adapter);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{N(1):api.read_note(N(1))},self.policy())
  result=e.execute(plan,e.digest(plan),pub,api,adapter,expected_store_sha=sha,writers_guard=lambda:self.policy()['old_writer_evidence']);self.assertEqual(result['created'],1);self.assertEqual(result['status'],'verified');self.assertEqual(self.store.value['pending_by_id'],{});self.assertIn(N(1),self.store.value['engine_state']['receipts'])
  self.ingest([]);self.assertFalse(self.store.value['rotation_order'])
 def test_create_uncertain_retains_queue_and_original_plan(self):
  from test_notion_executor import API
  self.publication({N(1):'one'});self.ingest();pub=self.pub();adapter=o.EngineStore(self.store);api=API(adapter);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{N(1):api.read_note(N(1))},self.policy());api.throw='create_after'
  result=e.execute(plan,e.digest(plan),pub,api,adapter,expected_store_sha=sha,writers_guard=lambda:self.policy()['old_writer_evidence']);self.assertEqual(result['reason'],'uncertain_create');self.assertIn(N(1),self.store.value['pending_by_id']);self.assertEqual(self.store.value['engine_state']['journal']['plan'],plan)
  value,version=self.store.read();resumed=o.QueuePublication(self.root,value,version,resume=True);self.assertEqual(resumed.descriptor,pub.descriptor)
  writes=len([x for x in api.calls if x[0]!='read']);result=e.execute(plan,e.digest(plan),resumed,api,adapter,expected_store_sha=adapter.read()[1],writers_guard=lambda:self.policy()['old_writer_evidence']);self.assertEqual(result['reason'],'uncertain_create');self.assertEqual(writes,len([x for x in api.calls if x[0]!='read']))
 def test_all_skipped_batch_rotation_is_persisted_without_new_api_calls(self):
  self.publication({N(i):'body' for i in range(51)});self.ingest();self.blocked_batch();self.blocked_batch()
  # All 51 now have the same-source conflict; a subsequent all-skipped plan
  # still rotates its selected subset so a later clean peer can be reached.
  pub=self.pub();adapter=o.EngineStore(self.store);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{},self.policy());self.assertEqual(plan['records'],[])
  before=self.store.value['rotation_order'][:];outer=self.store.read()[1];o.settle_empty_plan(self.store,outer,plan,e.digest(plan));self.assertNotEqual(self.store.value['rotation_order'],before);self.assertEqual(set(before),set(self.store.value['rotation_order']))
  with self.assertRaises(o.Blocked):o.settle_empty_plan(self.store,self.store.read()[1],plan,'f'*64)
 def test_missing_inherited_asset_blob_is_never_fetched(self):
  self.write('Raw/03 Get/assets/a.jpg',b'opaque asset');asset_commit=self.commit();self.origin={'commit':asset_commit,'receipt_sha256':None,'sequence':0};self.last=self.origin
  blob=self.git('rev-parse','HEAD:Raw/03 Get/assets/a.jpg').decode().strip();self.publication({N(1):'one'})
  object_path,_=remove_objects(self.root,blob)[blob]
  self.ingest();pub=self.pub();self.assertEqual(pub.read(N(1)),md(N(1),'one'));self.assertFalse(object_path.exists());self.assertEqual(pub.descriptor['content_tree']['Raw/03 Get/assets/a.jpg'],blob);assert_missing(self.root,blob)
 def test_peer_source_commit_stays_original_after_new_id_coalesces(self):
  first=self.publication({N(1):'one',N(2):'two'});self.ingest();second=self.publication({N(1):'new'});self.ingest([second]);self.blocked_batch()
  pub=self.pub();self.assertEqual(pub.descriptor['commit'],first['commit']);self.assertEqual(set(pub.descriptor['records']),{N(2)});self.assertIn(N(2),pub.descriptor['changed_content_ids'])
 def test_enqueue_expected_state_and_chain_hash_fail_closed(self):
  self.publication({N(1):'one'});state,sha=self.store.read();chain={'repository':o.p.REPOSITORY,'policy_sha256':P,'origin':self.origin,'after':self.origin,'tip':self.last,'publications':self.rows}
  for a,b in (('f'*64,o.digest(chain)),(sha,'f'*64)):
   with self.assertRaises(o.Blocked):o.enqueue(self.root,state,a,chain,b)
 def test_queue_and_receipt_completion_same_cas_survives_store_failure(self):
  from test_notion_executor import API
  self.publication({N(1):'one'});self.ingest();pub=self.pub();adapter=o.EngineStore(self.store);api=API(adapter);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{N(1):api.read_note(N(1))},self.policy());original=self.store.compare_and_set
  def fail_final(expected,value):
   if value['engine_state']['receipts']:raise RuntimeError('synthetic journal unavailable')
   return original(expected,value)
  self.store.compare_and_set=fail_final
  out=e.execute(plan,e.digest(plan),pub,api,adapter,expected_store_sha=sha,writers_guard=lambda:self.policy()['old_writer_evidence']);self.assertEqual(out['reason'],'journal_failed');self.assertIn(N(1),self.store.value['pending_by_id']);self.assertFalse(self.store.value['engine_state']['receipts'])
  self.store.compare_and_set=original;value,outer=self.store.read();resumed=o.QueuePublication(self.root,value,outer,resume=True);writes=len([x for x in api.calls if x[0]!='read'])
  out=e.execute(plan,e.digest(plan),resumed,api,adapter,expected_store_sha=adapter.read()[1],writers_guard=lambda:self.policy()['old_writer_evidence']);self.assertEqual(out['status'],'verified');self.assertFalse(self.store.value['pending_by_id']);self.assertEqual(writes,len([x for x in api.calls if x[0]!='read']))
 def test_blocked_first_50_do_not_starve_clean_51st_record(self):
  from test_notion_executor import API
  self.publication({N(i):'body' for i in range(51)});self.ingest();self.blocked_batch();pub=self.pub();adapter=o.EngineStore(self.store);api=API(adapter);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{N(50):api.read_note(N(50))},self.policy())
  self.assertEqual([row['note_id'] for row in plan['records']],[N(50)])
  out=e.execute(plan,e.digest(plan),pub,api,adapter,expected_store_sha=sha,writers_guard=lambda:self.policy()['old_writer_evidence']);self.assertEqual(out['created'],1);self.assertNotIn(N(50),self.store.value['pending_by_id']);self.assertEqual(len(self.store.value['pending_by_id']),50)
 def test_bounded_chain_and_aggregate_events_fail_without_partial_dequeue(self):
  from unittest.mock import patch
  self.publication({N(1):'one',N(2):'two'});before=self.store.read()
  with patch.object(o,'MAX_RECORDS',1):
   with self.assertRaisesRegex(o.Blocked,'outbox_budget'):self.ingest()
  self.assertEqual(self.store.read(),before)
  with patch.object(o,'MAX_CHAIN',0):
   with self.assertRaisesRegex(o.Blocked,'outbox_budget'):self.ingest()
  self.assertEqual(self.store.read(),before)
 def test_receipt_sequence_and_parent_tamper_are_rejected(self):
  row=self.publication({N(1):'one'});bad=deepcopy(row);bad['parent']=row['commit']
  with self.assertRaisesRegex(o.Blocked,'outbox_chain_invalid'):self.ingest([bad])
  self.assertEqual(self.store.value,o.initial_state())
 def test_persistent_cursor_survives_serialization_and_new_process(self):
  self.publication({N(1):'one'});self.ingest();data=o.encoded(self.store.value);loaded=json.loads(data);o.check_state(loaded);self.assertEqual(o.digest(data),o.digest(loaded));self.store.value=loaded
  row=self.publication({N(2):'two'});self.ingest([row]);self.assertEqual(set(self.store.value['pending_by_id']),{N(1),N(2)});self.assertEqual(self.store.value['last_enqueued_publication']['sequence'],2)
 def test_already_verified_source_receipt_can_clear_queue_without_new_read_or_write(self):
  from test_notion_executor import API
  self.publication({N(1):'one'});self.ingest();old_pending=deepcopy(self.store.value['pending_by_id']);pub=self.pub();adapter=o.EngineStore(self.store);api=API(adapter);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{N(1):api.read_note(N(1))},self.policy());out=e.execute(plan,e.digest(plan),pub,api,adapter,expected_store_sha=sha,writers_guard=lambda:self.policy()['old_writer_evidence']);self.assertEqual(out['created'],1)
  # Explicit trusted seed of a delivery already covered by this engine receipt.
  self.store.value['pending_by_id']=old_pending;self.store.value['rotation_order']=[N(1)];pub=self.pub();adapter=o.EngineStore(self.store);state,sha=adapter.read();plan=e.prepare(pub,state,sha,{},self.policy());self.assertEqual(plan['records'],[])
  calls=len(api.calls);o.settle_empty_plan(self.store,self.store.read()[1],plan,e.digest(plan));self.assertEqual(self.store.value['pending_by_id'],{});self.assertEqual(len(api.calls),calls)
 def test_omitted_latest_publication_is_not_silently_treated_as_noop(self):
  first=self.publication({N(1):'one'});self.publication({N(2):'two'})
  first_anchor={'commit':first['commit'],'receipt_sha256':first['receipt_sha256'],'sequence':1}
  with self.assertRaises(Exception):self.ingest([first],chain_changes={'tip':first_anchor})
  self.assertEqual(self.store.value,o.initial_state())
 def test_bounded_prefix_persists_then_continues_before_any_dispatch(self):
  first=self.publication({N(1):'one'});second=self.publication({N(2):'two'});tip=deepcopy(self.last);self.ingest([first])
  self.assertEqual(self.store.value['last_enqueued_publication']['sequence'],1);self.assertEqual(self.store.value['observed_publication_tip'],tip);self.assertIn(N(1),self.store.value['pending_by_id'])
  with self.assertRaisesRegex(o.Blocked,'outbox_chain_incomplete'):self.pub()
  self.ingest([second]);self.assertEqual(self.store.value['last_enqueued_publication'],tip);self.assertEqual(set(self.store.value['pending_by_id']),{N(1),N(2)});self.assertIn(N(1),self.pub().descriptor['records'])
 def test_partial_traversal_coalesces_latest_same_id_before_dispatch(self):
  first=self.publication({N(1):'one',N(2):'two'});second=self.publication({N(1):'new'});self.ingest([first]);self.assertEqual(self.store.value['pending_by_id'][N(1)]['commit'],first['commit'])
  self.ingest([second]);self.assertEqual(self.store.value['pending_by_id'][N(1)]['commit'],second['commit']);self.assertEqual(self.store.value['pending_by_id'][N(2)]['commit'],first['commit'])
 def test_tip_cannot_regress_or_change_at_equal_sequence(self):
  first=self.publication({N(1):'one'});self.ingest();old=deepcopy(self.last);second=self.publication({N(2):'two'});self.ingest([second]);before=self.store.read()
  with self.assertRaisesRegex(o.Blocked,'outbox_chain_invalid'):self.ingest([],chain_changes={'tip':old})
  self.assertEqual(self.store.read(),before)
if __name__=='__main__':unittest.main()
