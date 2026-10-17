from copy import deepcopy
import unittest
from unittest.mock import patch
import get_notion_content_scope as scope
import get_notion_executor as e
import test_notion_executor as f

class ContentScopeTests(unittest.TestCase):
 def test_exact_reviewed_version_only(self):
  data=f.md(f.N,'Plain reviewed transcript')
  with patch.object(scope,'REVIEWED',{e.digest(f.N):e.digest(data)}),patch.object(scope,'HELD',{}):
   self.assertIsNone(scope.reason(f.N,data))
   self.assertEqual(scope.reason(f.N,data+b'private changed'), 'content_review_required')
 def test_held_and_denied_never_overridden_by_exact_review(self):
  data=f.md(f.N,'ordinary')
  for reason in ('privacy_scope_required','prior_denial'):
   with patch.object(scope,'REVIEWED',{e.digest(f.N):e.digest(data)}),patch.object(scope,'HELD',{e.digest(f.N):reason}):
    self.assertEqual(scope.reason(f.N,data),reason)
 def test_empty_only_for_exact_reviewed_empty_version(self):
  data=f.md(f.N,'')
  with patch.object(scope,'HELD',{e.digest(f.N):'empty_source'}),patch.object(scope,'EMPTY_SHA256',e.digest(data)):
   self.assertEqual(scope.reason(f.N,data),'empty_source')
   self.assertEqual(scope.reason(f.N,data+b'new text'),'content_review_required')
 def test_unreviewed_recording_is_not_inferred_ordinary(self):
  for kind in scope.RECORDINGS:
   data=f.md(f.N,'Unknown transcript').replace(b'title:',('note_type: "'+kind+'"\ntitle:').encode())
   self.assertEqual(scope.reason(f.N,data),'content_review_required')
  self.assertIsNone(scope.reason(f.N,f.md(f.N,'Ordinary supported work record')))
 def test_peer_create_proceeds_without_transmitting_held_record(self):
  x=f.Tests(methodName='test_existing_page_without_ownership_isolated_from_clean_create');x.setUp();self.addCleanup(x.doCleanups)
  (x.source_path.parent/'b.md').write_bytes(f.md(f.N2,'Peer ordinary source'));x.publish()
  with patch.object(scope,'HELD',{e.digest(f.N):'privacy_scope_required'}):
   plan=x.plan(retry_conflicts=True)
   self.assertEqual([r['action'] for r in plan['records']],['blocked','create'])
   self.assertEqual(plan['records'][0]['reason'],'privacy_scope_required')
   result=x.run_plan(plan)
   self.assertEqual(result['created'],1);self.assertEqual(x.writes(),[('create',)])
   self.assertNotIn(f.N,x.store.read()[0]['journal'] or {})
 def test_execute_rechecks_scope_after_preparation(self):
  x=f.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties');x.setUp();self.addCleanup(x.doCleanups);x.publish();plan=x.plan()
  with patch.object(scope,'HELD',{e.digest(f.N):'privacy_scope_required'}):out=x.run_plan(plan)
  self.assertEqual(out['reason'],'privacy_scope_required');self.assertEqual(x.writes(),[])

if __name__=='__main__':unittest.main()

class NewFailureEvidence(unittest.TestCase):
 def test_new_http_failure_records_only_typed_status_and_never_retries(self):
  import get_notion_live_transport as t
  x=f.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties');x.setUp();self.addCleanup(x.doCleanups);x.publish();plan=x.plan()
  row=plan['records'][0];key=t.fingerprint('PATCH','/blocks/'+row['page_id']+'/children',{'children':t.wire_blocks(row['request_blocks'],e.append_previous_type(row)),'after':row['after_anchor']})
  def failure(*args):x.api.before('append');raise t.WriteFailure('write_uncertain',400,key)
  with patch.object(x.api,'append_blocks',side_effect=failure):result=x.run_plan(plan)
  self.assertEqual(result['reason'],'uncertain_append');self.assertEqual(result['write_http_status'],400)
  state=x.store.read()[0];op=state['journal']['operation']
  self.assertEqual(op['phase'],'in_flight');self.assertEqual(op['write_failure']['http_status'],400)
  self.assertEqual(set(op['write_failure']),{'http_status','request_fingerprint'})
  repeat=x.run_plan(plan);self.assertEqual(repeat['reason'],'uncertain_append');self.assertEqual(x.writes(),[('append',)])
 def test_malformed_error_status_never_leaks(self):
  for value in ({'private':'payload'},[],True,'400 PRIVATE',200,700):
   raw={'status':'blocked','reason':'uncertain_append','write_state':'uncertain','write_http_status':value,**{k:0 for k in ('created','updated','noop','blocked','write_calls','external_write_attempts','deferred','retained_conflicts')}}
   safe=e.public_report(raw);self.assertNotIn('write_http_status',safe);self.assertNotIn('PRIVATE',str(safe))

class HttpOriginTests(unittest.TestCase):
 def test_read_failure_not_attributed_to_write(self):
  import get_notion_live_transport as t
  x=f.Tests(methodName='test_existing_page_without_ownership_isolated_from_clean_create');x.setUp();self.addCleanup(x.doCleanups);x.publish();x.api.pages.clear();state,sha=x.store.read();state['receipts']={};x.store.compare_and_set(sha,state);plan=x.plan()
  self.assertEqual(plan['records'][0]['action'],'create')
  for row in plan['records']:
   if row['action']=='create':break
  with patch.object(x.api,'create_page',side_effect=t.Failure('permission_denied',403)):
   result=x.run_plan(plan)
  self.assertIsNone(result.get('write_http_status'));self.assertNotIn('write_http_status',e.public_report(result))
  self.assertNotIn('write_failure',x.store.read()[0]['journal']['operation'])
 def test_wrong_write_fingerprint_is_not_attributed(self):
  import get_notion_live_transport as t
  x=f.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties');x.setUp();self.addCleanup(x.doCleanups);x.publish();plan=x.plan()
  with patch.object(x.api,'append_blocks',side_effect=t.WriteFailure('write_uncertain',400,'0'*64)):
   result=x.run_plan(plan)
  self.assertIsNone(result.get('write_http_status'));self.assertNotIn('write_http_status',e.public_report(result));self.assertNotIn('write_failure',x.store.read()[0]['journal']['operation'])
 def test_real_transport_http_only_and_exact_fingerprint(self):
  import get_notion_live_transport as t
  from test_live_transport import Opener,Response
  import urllib.error,io
  path='/blocks/'+'a'*32;body={'archived':True};key=t.fingerprint('PATCH',path,body)
  o=Opener([urllib.error.HTTPError('private',400,'private',{},io.BytesIO(b'private'))]);transport=t.Transport('synthetic',opener=o,approved_writes=[key])
  with self.assertRaises(t.WriteFailure) as caught:transport('PATCH',path,body)
  self.assertEqual(caught.exception.request_fingerprint,key);self.assertEqual(caught.exception.status,400)
  o=Opener([t.WriteFailure('write_uncertain',400,key)]);transport=t.Transport('synthetic',opener=o,approved_writes=[key])
  with self.assertRaises(t.Failure) as caught:transport('PATCH',path,body)
  self.assertNotIsInstance(caught.exception,t.WriteFailure);self.assertIsNone(caught.exception.status)
