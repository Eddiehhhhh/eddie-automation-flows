"""Synthetic exact-prepared continuation tests; real proof replay is private.

These adapters never turn saved receipt data into a live native witness.
"""
import ast,json,sys,unittest
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import patch
from test_notion_clean_active_resume import Fixture as OldFixture,RealGitFixture
import test_notion_clean_active_resume as old_tests
from test_notion_exact_ack_recovery import Blocked,encoded,digest,oid,extract,module,record_creation,fail,SCRIPTS

class Fixture(OldFixture):
 def setUp(self):
  super().setUp();s=deepcopy(self.start);j=s['engine_state']['journal'];rows=j['plan']['records']
  ident=rows[5]['note_id']
  for field in ('receipts','property_receipts'):s['engine_state'][field].pop(ident)
  j['index']=5;s=self.prepared(s);j=s['engine_state']['journal'];b=deepcopy(self.binding)
  for key in ('schema_exceptions','prior_capsule','policy_progress'):b.pop(key)
  b.update(contract='get-notion-exact-prepared-checkpoint-1',initial_index=5,prefix=b['prefix'][:5],
   remaining=[{'note_id':r['note_id'],'source':r['source'],'request_sha256':self.cr.request_sha(r,j['plan'])}for r in rows[5:]],
   initial_operation_sha256=digest(j['operation']))
  proof=b['proof'];proof['checkpoint_count']=23;proof['terminal_report']['external_write_attempts']=5
  cp=proof['checkpoint'];cp.update(sequence=23,state_bytes=len(encoded(s)),state_blob=oid(encoded(s)),state_sha256=digest(s));cp['record_sha256']=digest({k:v for k,v in cp.items()if k!='record_sha256'});proof['anchor']={k:cp[k]for k in ('commit','state_blob','state_sha256')}
  source_run=dict(proof['run_binding'],id=99,event='schedule',status='completed',conclusion='success')
  b['successor_chain']=[{'commit':'9'*40,'parent':proof['anchor']['commit'],'before_tree':cp['tree'],'after_tree':'8'*40,'changes_sha256':'e'*64,'changed_paths':1}]
  b['source_history_edges']=1
  b['source_binding']={'public_policy':deepcopy(b['public_policy']),'public_policy_sha256':b['public_policy_sha256'],
   'run_binding':source_run,'job_id':101,'job_sha256':digest({'id':101}),
   'get_record':{'observed_main_commit':'9'*40,'main_updated':False,'cas_attempts':0,'readback_verified':True}}
  b['negative_bindings']=[{'run_binding':dict(proof['run_binding'],id=i),'latest_binding':dict(proof['run_binding'],id=i),'job_id':100+i,'job_sha256':digest({'id':100+i}),'log_sha256':digest(b'synthetic')}for i in (81,82,83)]
  self.checkpoints.fail=fail
  self.provenance.publication_line=lambda r:encoded(r).decode();self.provenance.parse_record=lambda raw,*a:json.loads(raw);self.provenance.positive=lambda x:x if type(x)is int and x>0 else fail('bad_id')
  ns={'deepcopy':deepcopy,'bindings':self.bindings}
  tree=ast.parse((SCRIPTS/'get_notion_checkpoint_resume.py').read_text());names={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}|{'TAG','KEYS','_NATIVE_SEAL','SCOPE_SHA256'}
  extract('get_notion_checkpoint_resume.py',names,ns);ns['target_code_sha256']=lambda:'e'*64
  b['invariant_sha256']=digest(ns['invariant'](s,b));self.entries=[b]
  ns['SCOPE_SHA256']=digest({k:v for k,v in b.items()if k!='target_code_sha256'})
  self.bridge=module('get_notion_checkpoint_resume',**{k:v for k,v in ns.items()if not k.startswith('__')});self.ns=ns;self.binding=b;self.start=s;self.native_state=s;self.native_reads.clear()
  self.auth={**proof,'recovery_anchor':self.bridge.TAG};self.witness=self.bridge.verify_native(s,object());self.new=self.bridge.transition_outer(s,digest(s),'0'*64,proof=self.auth,native=self.witness)
  self.p=NS(run_binding=deepcopy,fail=fail,MAX_TOTAL=1000000)
  extra=patch.dict(sys.modules,{'get_notion_idle_recovery':NS(_forward_policy=lambda policy,b:policy.get('target')=='approved'), 'get_publication_provenance':self.p});extra.start();self.addCleanup(extra.stop)
 def repin(self):self.ns['SCOPE_SHA256']=digest({k:v for k,v in self.binding.items()if k!='target_code_sha256'})

class ExactBinding(Fixture):
 def test_exact_prepared_binding_and_failure_unchanged(self):
  self.assertEqual(self.bridge.registry(),[self.binding]);self.assertTrue(self.bridge.initial_state_valid(self.start,digest(self.start)))
  self.assertEqual(self.binding['proof']['terminal_report']['write_state'],'uncertain');self.assertEqual(self.binding['proof']['checkpoint_count'],23)
 def test_changed_scope_cannot_be_learned_from_current(self):
  for mutate in (lambda b:b.update(initial_index=6),lambda b:b['proof']['terminal_report'].update(write_state='confirmed'),lambda b:b['source_binding']['get_record'].update(main_updated=True),lambda b:b['remaining'].reverse(),lambda b:b.update(extra=True)):
   bad=deepcopy(self.binding);mutate(bad);self.entries=[bad]
   with self.assertRaises(Blocked):self.bridge.registry()
  self.entries=[self.binding]
 def test_prepared_exact_operation_and_null_response_required(self):
  for mutate in (lambda j:j.update(operation=None),lambda j:j['operation'].update(response='a'*32),lambda j:j['operation'].update(phase='in_flight'),lambda j:j.update(index=4)):
   bad=deepcopy(self.start);mutate(bad['engine_state']['journal']);self.assertFalse(self.bridge.initial_state_valid(bad,digest(bad)))
 def test_target_change_disables_source_and_journal_coordinates(self):
  self.assertEqual(self.bridge.discovery_locator({'target':'approved'}),(77,1));self.assertEqual(self.bridge.source_discovery_locator({'target':'approved'}),(99,1))
  self.ns['target_code_sha256']=lambda:'f'*64
  self.assertIsNone(self.bridge.discovery_locator({'target':'approved'}));self.assertIsNone(self.bridge.source_discovery_locator({'target':'approved'}));self.assertFalse(self.bridge.allows(self.start['engine_state']['journal']['plan'],self.start['engine_state']['journal']))
 def test_unapproved_target_cannot_see_extra_source_identity(self):
  self.assertIsNone(self.bridge.source_discovery_locator({'target':'other'}));self.assertIsNone(self.bridge.discovery_locator({'target':'other'}))
  r=deepcopy(self.binding['source_binding']['run_binding']);r['id']=100
  self.assertFalse(self.bridge.source_candidate_policy({'target':'approved'},r)[1])
 def test_source_record_full_binding_no_alias_mixing(self):
  b=self.binding['source_binding'];self.assertTrue(self.bridge.verify_source_record(b['get_record'],101,job={'id':101},run=b['run_binding']))
  for record,job in ((dict(b['get_record'],cas_attempts=1),{'id':101}),(b['get_record'],{'id':102})):
   with self.assertRaises(Blocked):self.bridge.verify_source_record(record,101,job=job,run=b['run_binding'])
 def test_other_state_and_receipt_mutations_reject(self):
  for mutate in (lambda s:s['sentinel'].append('changed'),lambda s:s['engine_state']['failures'].update(other='new'),lambda s:s['engine_state']['receipts'].pop(next(iter(s['engine_state']['receipts']))),lambda s:s['engine_state']['journal']['plan'].update(rules_sha256='0'*64)):
   bad=deepcopy(self.start);mutate(bad);self.assertFalse(self.bridge.state_valid(bad))

class ExactNegativeEvidence(Fixture):
 def test_exact_negative_selection_only_for_target(self):
  n=self.binding['negative_bindings'][0]
  self.assertEqual(self.bridge.negative_binding({'target':'approved'},n['latest_binding']),n)
  self.assertIsNone(self.bridge.negative_binding({'target':'other'},n['latest_binding']))
  self.assertIsNone(self.bridge.negative_binding({'target':'approved'},dict(n['latest_binding'],id=201)))
 def test_same_registered_id_changed_alias_is_hard_failure(self):
  n=self.binding['negative_bindings'][0]
  for change in ({'run_attempt':2},{'head_sha':'0'*40},{'conclusion':'success'},{'created_at':'changed'},{'updated_at':'changed'}):
   with self.assertRaises(Blocked):self.bridge.negative_binding({'target':'approved'},dict(n['latest_binding'],**change))
 def test_previous_attempt_cannot_hide_changed_current_alias(self):
  n=self.binding['negative_bindings'][0]
  with self.assertRaises(Blocked):self.bridge.negative_binding({'target':'approved'},n['latest_binding'],latest=dict(n['latest_binding'],run_attempt=2,status='in_progress',conclusion=None))
 def test_full_observation_and_no_alias_normalization(self):
  n=self.binding['negative_bindings'][0];p=self.binding['public_policy'];job={'id':n['job_id']}
  self.bridge.check_negative_observation(n,p,n['run_binding'],job,b'synthetic')
  for changed in (dict(n['run_binding'],created_at='different'),dict(n['run_binding'],run_attempt=2),dict(n['run_binding'],head_sha='0'*40)):
   with self.assertRaises(Blocked):self.bridge.check_negative_observation(n,p,changed,job,b'synthetic')
 def test_wrong_binding_policy_job_raw_reject(self):
  n=self.binding['negative_bindings'][0];p=self.binding['public_policy'];r=n['run_binding'];job={'id':n['job_id']}
  for args in (({},p,r,job,b'synthetic'),(n,{},r,job,b'synthetic'),(n,p,r,{'id':7},b'synthetic'),(n,p,r,dict(job,extra=True),b'synthetic'),(n,p,r,job,b'changed'),(n,p,r,job,'synthetic')):
   with self.assertRaises(Blocked):self.bridge.check_negative_observation(*args)
 def test_only_original_missing_is_negative(self):
  n=self.binding['negative_bindings'][0];c=self.checkpoints;c.CheckpointFailure=Blocked
  def recover(client,**kw):
   self.assertEqual(kw['public_policy'],self.binding['public_policy']);self.assertEqual(kw['exact_missing_binding'],n);raise Blocked('journal_provenance_missing')
  c.recover_anchor=recover
  self.bridge.require_missing_negative(object(),{'target':'approved'},n['latest_binding'])
 def test_transport_budget_hash_and_valid_checkpoint_do_not_fall_through(self):
  n=self.binding['negative_bindings'][0];c=self.checkpoints;c.CheckpointFailure=Blocked
  for reason in ('journal_provenance_budget','journal_provenance_failed','journal_provenance_changed','journal_provenance_other_controller'):
   def recover(*a,**kw):raise Blocked(reason)
   c.recover_anchor=recover
   with self.assertRaises(Blocked):self.bridge.require_missing_negative(object(),{'target':'approved'},n['latest_binding'])
  c.recover_anchor=lambda *a,**kw:{'status':'journal_provenance_verified'}
  with self.assertRaises(Blocked):self.bridge.require_missing_negative(object(),{'target':'approved'},n['latest_binding'])
 def test_scope_pin_covers_all_negative_coordinates(self):
  for change in (lambda n:n.update(extra=True),lambda n:n['latest_binding'].update(run_attempt=2),lambda n:n.update(log_sha256='0'*64),lambda n:n.update(job_id=123)):
   b=deepcopy(self.binding);change(b['negative_bindings'][0]);self.entries=[b]
   with self.assertRaises(Blocked):self.bridge.registry()

class NativeAndCas(Fixture):
 def test_full_ten_native_read_and_only_outer_policy_change(self):
  self.assertEqual(self.native_reads,[r['note_id']for r in self.start['engine_state']['journal']['plan']['records']]);self.bridge.preserve(self.start,self.new)
  old=deepcopy(self.new);old['policy_sha256']=self.start['policy_sha256'];self.assertEqual(old,self.start)
 def test_missing_and_stale_native_witness_refused(self):
  for witness in (None,{},self.bridge._NativeWitness('f'*64,self.binding['plan_sha256'],self.ns['_NATIVE_SEAL'])):
   with self.assertRaises(Blocked):self.bridge.transition_outer(self.start,digest(self.start),'0'*64,proof=self.auth,native=witness)
 def test_native_transport_or_prefix_change_stops(self):
  old=self.engine.read_target;self.engine.read_target=lambda *a:fail('transport_read_failed')
  with self.assertRaises(Blocked):self.bridge.verify_native(self.start,object())
  self.engine.read_target=old;self.native_state=deepcopy(self.start);next(iter(self.native_state['engine_state']['receipts'].values()))['page_id']='f'*32
  with self.assertRaises(Blocked):self.bridge.verify_native(self.start,object())
 def test_inflight_never_reset_or_treated_absent(self):
  flight=deepcopy(self.new);flight['engine_state']['journal']['operation']['phase']='in_flight';self.bridge.preserve(self.new,flight)
  with self.assertRaises(Blocked):self.bridge.verify_native(flight,object())
  with self.assertRaises(Blocked):self.bridge.preserve(flight,self.new)
 def test_prepared_cannot_clear_or_skip_ack(self):
  for mutate in (lambda j:j.update(operation=None),lambda j:j.update(index=6),lambda j:j['operation'].update(phase='acknowledged',response='a'*32)):
   bad=deepcopy(self.new);mutate(bad['engine_state']['journal'])
   with self.assertRaises(Blocked):self.bridge.preserve(self.new,bad)
 def test_five_rows_exact_forward_then_clear(self):
  s=self.new
  for _ in range(5):s=self.advance(s)
  self.assertEqual(s['engine_state']['journal']['index'],10);clear=deepcopy(s);clear['engine_state']['journal']=None;self.bridge.preserve(s,clear)
  self.assertEqual(len(clear['engine_state']['receipts']),10)
 def test_current_receipt_wrong_pair_or_other_change_rejected(self):
  z=deepcopy(self.new);z['engine_state']['journal']['operation']['phase']='in_flight';self.bridge.preserve(self.new,z)
  ack=deepcopy(z);ack['engine_state']['journal']['operation'].update(phase='acknowledged',response='f'*32);self.bridge.preserve(z,ack)
  done=deepcopy(ack);j=done['engine_state']['journal'];r=j['plan']['records'][j['index']];done['engine_state']['receipts'][r['note_id']]=self.body(j['plan'],r,'e'*32);record_creation(done['engine_state'],r,j['plan']);j.update(index=6,operation=None)
  with self.assertRaises(Blocked):self.bridge.preserve(ack,done)

class ExactGitFixture(RealGitFixture,Fixture):
 def register_prior_capsule(self,parent):
  before=self.node(parent);child=self.commit(self.start,parent,extra=('Raw/99 Synthetic/source-boundary.md',b'source boundary'))
  after=self.node(child);changes=self.bridge.successor_changes(before[2],after[2])
  self.binding['successor_chain']=[{'commit':child,'parent':parent,'before_tree':before[0],'after_tree':after[0],'changes_sha256':digest(changes),'changed_paths':len(changes)}]
  self.binding['source_history_edges']=1;self.source_base=child;self.head=child
  self.binding['source_binding']['get_record']['observed_main_commit']=child
  self.repin();return child
 def add_capsule(self,parent=None,state=None):
  self.head=self.commit(state or self.start,parent or self.source_base,extra=(self.capsule_path,self.capsule))
  return self.head
 def known_chain(self):
  before=self.node(self.base);c=self.commit(self.start,self.base,extra=('Raw/99 Synthetic/item.md',b'synthetic'))
  after=self.node(c);changes=self.bridge.successor_changes(before[2],after[2])
  self.binding['successor_chain']=[{'commit':c,'parent':self.base,'before_tree':before[0],'after_tree':after[0],
      'changes_sha256':digest(changes),'changed_paths':len(changes)}]
  self.binding['source_binding']['get_record']['observed_main_commit']=c;self.repin();return c
 def node(self,commit):
  from types import SimpleNamespace
  import get_notion_remote_journal as j
  t,entries=j.RemoteJournal._local_tree(SimpleNamespace(local=j.retention.LocalGit(self.root)),commit)
  return t,self.git('rev-list','--parents','-n','1',commit).decode().split()[1:],entries

class ExactGitSuccessor(ExactGitFixture):
 def test_capsule_only_and_exact_policy_only_successor(self):
  self.add_capsule();self.assertEqual(self.verify(),self.binding['proof']['anchor'])
  self.head=self.commit(self.new,self.head);out=self.verify();self.assertEqual(out['commit'],self.head);self.assertEqual(out['state_sha256'],digest(self.new))
 def test_real_known_chain_then_capsule(self):
  self.add_capsule(self.known_chain());self.assertEqual(self.verify(),self.binding['proof']['anchor'])
 def test_missing_capsule_or_unknown_extra_edge_refused(self):
  with self.assertRaises(Blocked):self.verify()
  self.add_capsule();self.head=self.commit(self.start,self.head,extra=('Raw/99 Synthetic/unknown.md',b'unknown'))
  with self.assertRaises(Blocked):self.verify()
 def test_wrong_registered_parent_or_digest(self):
  c=self.known_chain();self.add_capsule(c)
  for key,value in (('parent','f'*40),('changes_sha256','f'*64),('changed_paths',2)):
   old=self.binding['successor_chain'][0][key];self.binding['successor_chain'][0][key]=value;self.repin()
   with self.assertRaises(Blocked):self.verify()
   self.binding['successor_chain'][0][key]=old;self.repin()
 def test_policy_cas_extra_state_diff_or_two_cas_refused(self):
  self.add_capsule();cap=self.head;bad=deepcopy(self.new);bad['sentinel'].append('changed');self.head=self.commit(bad,cap)
  with self.assertRaises(Blocked):self.verify()
  self.head=self.commit(self.new,cap);self.head=self.commit(self.new,self.head)
  with self.assertRaises(Blocked):self.verify()
 def test_changed_source_or_extra_capsule_leaf_refused(self):
  self.add_capsule();cap=self.head
  self.head=self.commit(self.start,cap,extra=('Raw/03 Get/changed.md',b'changed'))
  with self.assertRaises(Blocked):self.verify()
 def test_tail_ref_drift_refused(self):
  self.add_capsule();original=self.remote.ref;calls=[]
  def drifting(ref):
   calls.append(ref);return self.head if len(calls)==1 else 'f'*40
  self.remote.ref=drifting
  with self.assertRaises(Blocked):self.verify()
 def test_merge_refused(self):
  self.add_capsule();cap=self.head;other=self.commit(self.start,self.base,extra=('Raw/99 Synthetic/other.md',b'x'))
  self.head=self.git('commit-tree',self.node(cap)[0],'-p',cap,'-p',other,'-m','Synthetic merge').decode().strip()
  with self.assertRaises(Blocked):self.verify()

class SourcePrefixBoundary(ExactGitFixture):
 def suffix(self,path='Raw/99 Synthetic/later.md',state=None):
  parent=self.binding['successor_chain'][-1]['commit'];before=self.node(parent)
  child=self.commit(state or self.start,parent,extra=(path,b'later actual fixture'))
  after=self.node(child);changes=self.bridge.successor_changes(before[2],after[2])
  self.binding['successor_chain'].append({'commit':child,'parent':parent,'before_tree':before[0],'after_tree':after[0],'changes_sha256':digest(changes),'changed_paths':len(changes)})
  self.repin();return child
 def test_observed_prefix_does_not_skip_later_registered_suffix(self):
  observed=self.binding['source_binding']['get_record']['observed_main_commit'];tail=self.suffix();self.add_capsule(tail)
  self.assertNotEqual(observed,tail);self.assertEqual(self.verify(),self.binding['proof']['anchor'])
 def test_wrong_boundary_boolean_zero_negative_overflow_or_later_rejected(self):
  tail=self.suffix();self.add_capsule(tail)
  for value in(True,False,0,-1,2,3):
   self.binding['source_history_edges']=value;self.repin()
   with self.subTest(value=value),self.assertRaises(Blocked):self.bridge.registry()
 def test_suffix_protected_changes_never_inherit_source_proof(self):
  for path in('Raw/03 Get/changed.md','.sync/get-notion/unknown.json','Schema/rule.md','AGENTS.md'):
   original=deepcopy(self.binding['successor_chain']);tail=self.suffix(path);self.add_capsule(tail)
   with self.subTest(path=path),self.assertRaises(Blocked):self.verify()
   self.binding['successor_chain']=original;self.repin()
 def test_suffix_missing_reordered_unknown_or_bad_hash_rejected(self):
  tail=self.suffix();self.add_capsule(tail);original=deepcopy(self.binding['successor_chain'])
  for change in(lambda x:x.pop(),lambda x:x.reverse(),lambda x:x[-1].update(changes_sha256='f'*64),lambda x:x[-1].update(parent='f'*40)):
   self.binding['successor_chain']=deepcopy(original);change(self.binding['successor_chain']);self.repin()
   with self.assertRaises(Blocked):self.verify()
  self.binding['successor_chain']=original;self.repin();self.head=self.commit(self.start,self.head,extra=('Raw/99 Synthetic/future.md',b'unknown'))
  with self.assertRaises(Blocked):self.verify()

class NewFourSlotDiscovery(old_tests.FourSlotDiscovery):
 def setUp(self):
  super().setUp();auth=self.ns['authenticated_recovery'];auth.checkpoint_resume=auth.clean_active;auth.clean_active=NS(discovery_locator=lambda policy:None);auth.checkpoint_resume.negative_binding=lambda *a,**kw:None;auth.checkpoint_resume.recover_checkpoint=auth.recover_checkpoint
 def test_three_recent_and_exact_fourth_no_fifth(self):
  result=self.discover();self.assertEqual(self.recoveries,[(6,1),(5,1),(4,1)])
  self.assertEqual(result['candidate_attempts_checked'],4);self.assertEqual(self.trace.count('/actions/runs/1'),1)
 def test_fixed_only_deduplicated_not_repeated_four_times(self):
  self.rows=[self.fixed];result=self.discover();self.assertEqual(self.recoveries,[])
  self.assertEqual(result['candidate_attempts_checked'],1)
 def test_missing_fixed_from_listing_still_requires_exact_alias(self):
  self.rows=self.rows[1:];check=lambda p,r:(p,r==self.fixed)
  self.ns['authenticated_recovery'].checkpoint_resume.candidate_policy=check;self.ns['authenticated_recovery'].candidate_policy=check
  self.assertEqual(self.discover()['candidate_attempts_checked'],4)
 def test_transport_or_budget_failures_are_not_negative_candidates(self):
  for reason in ('journal_provenance_budget','journal_provenance_failed'):
   self.recoveries=[]
   def broken(*a,**kw):self.recoveries.append(kw['run_id']);raise self.error(reason)
   self.ns['recover_anchor']=broken
   with self.assertRaises(self.error):self.discover()
   self.assertEqual(self.recoveries,[6])

if __name__=='__main__':unittest.main()
