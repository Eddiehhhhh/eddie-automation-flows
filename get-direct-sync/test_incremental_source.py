import copy,tempfile,unittest,urllib.parse
from core import GetSource,Hold,Store,digest,snapshot,canonical
from incremental_source import IncrementalSelector
from test_core import note,Sink
class API:
 def __init__(self,notes,details=None,fail_last=False):self.notes=notes;self.details=details or {n['note_id']:n for n in notes};self.fail_last=fail_last;self.read=[]
 def request(self,method,path,body=None):
  if '/detail?' in path:
   nid=urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['id'][0];self.read.append(nid);return {'success':True,'data':{'note':self.details[nid]}}
  last='cursor=' in path
  if last and self.fail_last:raise Hold('list_failed')
  return {'success':True,'data':{'notes':self.notes[1:] if last else self.notes[:1],'has_more':not last,'cursor':'last'}}
def seed(store,notes):store.accept([{**snapshot(n),'summary_hash':digest(n)} for n in notes])
def scan(store,notes,**kwargs):
 selector=IncrementalSelector(store,**kwargs);api=API(notes);source=GetSource(api);items=source.scan(select_note=selector,on_note_hold=lambda h:None);selector.commit(source,items);return api.read,source
class IncrementalTests(unittest.TestCase):
 def test_audit_eventually_reaches_every_id(self):
  store=Store(':memory:');notes=[note(str(i)) for i in range(1,202)];seed(store,notes);audited=set()
  for _ in range(9):
   read,_=scan(store,notes);self.assertLessEqual(len(read),100);audited.update(read)
  self.assertEqual(audited,{n['note_id'] for n in notes});self.assertEqual(store.pending_count(),0)
 def test_deferred_changes_survive_restart_and_new_arrivals(self):
  with tempfile.TemporaryDirectory() as d:
   path=d+'/s';store=Store(path);old=[note(str(i)) for i in range(1,201)];seed(store,old);changed=[note(str(i),text='changed') for i in range(1,201)]
   first,_=scan(store,changed);self.assertEqual(len(first),100);deferred=set(store.get('incremental_source_v1')['deferred_ids']);self.assertEqual(len(deferred),100)
   store.db.close();store=Store(path);second,_=scan(store,[note(str(i)) for i in range(1000,1100)]+changed);self.assertTrue(deferred.intersection(second));self.assertLessEqual(len(second),100)
   for _ in range(5):scan(store,changed)
   hashes=dict(store.db.execute('SELECT id,summary_hash FROM source_summaries'));self.assertTrue(all(hashes[n['note_id']]==digest(n) for n in changed))
 def test_new_note_receives_slots_despite_full_deferred_backlog(self):
  for summaries in (True,False):
   with self.subTest(summaries=summaries):
    store=Store(':memory:');notes=[note(str(i)) for i in range(1,1001)]
    if summaries:seed(store,notes)
    else:store.accept([snapshot(n) for n in notes])
    changes=[note(str(i),text='changed') for i in range(1,1001)]
    scan(store,changes);backlog=set(store.get('incremental_source_v1')['deferred_ids']);self.assertGreater(len(backlog),75)
    fresh=[note(str(i)) for i in range(2001,2026)];read,_=scan(store,changes+fresh)
    self.assertTrue({n['note_id'] for n in fresh}.issubset(read));self.assertTrue(backlog.intersection(read));self.assertLessEqual(len(read),100)
    for _ in range(21):scan(store,changes+fresh)
    self.assertEqual(store.get('incremental_source_v1')['deferred_ids'],[])
 def test_last_page_failure_never_advances(self):
  store=Store(':memory:');seed(store,[note('1'),note('2')]);previous=store.db.execute('SELECT * FROM source_summaries ORDER BY id').fetchall();selector=IncrementalSelector(store);source=GetSource(API([note('1',text='new'),note('2')],fail_last=True))
  with self.assertRaises(Hold):source.scan(select_note=selector)
  with self.assertRaises(Hold):selector.commit(source,[])
  self.assertEqual(store.db.execute('SELECT * FROM source_summaries ORDER BY id').fetchall(),previous);self.assertIsNone(store.get('incremental_source_v1'));self.assertEqual(store.pending_count(),0)
 def test_paused_excluded_detail_only_edit_discovered(self):
  store=Store(':memory:');notes=[note(str(i)) for i in range(1,5)];seed(store,notes);selector=IncrementalSelector(store,paused_ids={'2'},detail_budget=2,audit_reserve=1)
  details={n['note_id']:copy.deepcopy(n) for n in notes};details['1']['unknown_detail']='edit';api=API(notes,details);source=GetSource(api);items=source.scan(select_note=selector);selector.commit(source,items)
  self.assertEqual(api.read,['1']);self.assertEqual(store.pending_count(),1)
 def test_source_hold_summary_commits_and_reaudits(self):
  store=Store(':memory:');store.accept([]);bad=note('1');bad['note_type']='recorder_audio';bad['audio']={}
  _,source=scan(store,[bad],detail_budget=1,audit_reserve=1);self.assertEqual(len(source.holds),1);self.assertEqual(store.db.execute('SELECT summary_hash FROM source_summaries').fetchone()[0],digest(bad))
  read,_=scan(store,[bad],detail_budget=1,audit_reserve=1);self.assertEqual(read,['1']);self.assertEqual(store.pending_count(),0)
if __name__=='__main__':unittest.main()
