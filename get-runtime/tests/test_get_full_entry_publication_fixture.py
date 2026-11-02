"""Complete original entry/publisher on a real synthetic Git repository."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_get_canonical_asset_transaction import Reader
import get_canonical_writer as n
import get_canonical_manual as engine
import get_recurring_entry as entry
import get_legacy_manual as m
import get_canonical_assets as assets

class FullReader(Reader):
 def source_get_tree(self,commit):return self.local.run('rev-parse',commit+':Raw/03 Get').decode().strip()
 def read_many(self,commit,selected):return {p:self.read(commit,p,cap) for p,cap in selected.items()}
 def baseline(self,parent,path,blob,digest):
  data=self.local.blob(blob)
  if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('baseline_missing')
  return data

class Source:
 def __init__(self,notes):self.notes=notes
 def enumerate(self):return deepcopy(self.notes),True
 def detail_raw(self,identity):return json.dumps({'data':next(n for n in self.notes if n['id']==identity)}).encode()

class FullEntry(unittest.TestCase):
 def test_original_prepare_receipt_verify_and_publication(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d).resolve();reader=FullReader(root)
   for args in [('init','-q'),('config','user.name','test'),('config','user.email','test@example.invalid')]:reader.local.run(*args)
   evidence=SimpleNamespace(anchor={'capture_commit':'1'*40,'census_sha256':'2'*64,'ledger_sha256':'3'*64},initial={},old={},conflicts={},source={},census={'eligible':{},'pending_ids':[],'summaries':{}})
   state=n.legacy.initial_state(evidence,{})
   path=root/'Raw/03 Get'/n.legacy.STATE;path.parent.mkdir(parents=True);path.write_bytes(n.legacy.encoded(state))
   reader.local.run('add','--','Raw/03 Get');reader.local.run('commit','-qm','synthetic bootstrap');parent=reader.local.run('rev-parse','HEAD').decode().strip()
   bootstrap={'commit':parent,'get_tree':reader.source_get_tree(parent),'capture_anchor':evidence.anchor,'legacy_sha256':n.legacy.sha(n.legacy.encoded(state)),'legacy_managed':0,'legacy_conflicts':0,'legacy_pending':0,'existing_unmanaged':0}
   notes=[{'id':str(i),'title':'Note '+str(i),'note_type':'plain_text','content':'![x](https://example.com/a.png)','created_at':'2026-10-13T00:00:00Z','updated_at':'2026-10-13T00:00:00Z'} for i in range(1,11)]
   preview={}
   for note in notes:
    path=n.new_path(note,note['id']);raw=n.render_exact(note,path)
    preview[note['id']]={'entry':{'filePath':path,'sourceVersion':n.raw.raw_source_version(note['updated_at']),'sourceContentHash':n.raw._source_content_hash(note),'sourceSummaryHash':n.raw._source_content_hash(note)},'bytes':raw,'anchor':{'commit':parent,'state_sha256':'4'*64,'blob':n.legacy.retention.oid(raw)}}
   source=assets.SourceWithAssets(Source(notes),root/'disposable-assets',fetch=lambda url,max_bytes:(b'image','image/png'))
   plan,files=entry.prepare(reader,evidence,parent,source,approved_rules_sha256=entry.rules_sha256(),preview=preview,bootstrap=bootstrap)
   self.assertEqual(len(plan['proposed_content_ids']),10)
   class Remote:
    head=parent
    def metadata(self):return {'full_name':m.r.REPOSITORY,'private':True}
    def ref(self,name):return self.head if name==m.r.MAIN_REF else m.PROTECTED[name]
    def cas_main(self,before,after):
     if self.head!=before:raise RuntimeError('race')
     reader.local.run('merge-base','--is-ancestor',before,after);self.head=after
    def commit(self,commit):return {'tree':reader.local.run('rev-parse',commit+'^{tree}').decode().strip(),'parents':reader.local.run('rev-list','--parents','-n','1',commit).decode().split()[1:]}
   remote=Remote()
   result=m.publish(reader,remote,plan,files,n.legacy.sha(n.legacy.encoded(plan)),lambda commit,values:{p:reader.read(commit,p,assets.MAX_ASSET) for p in values},checkpoint=lambda pl:m.write_checkpoint(root/'checkpoint.json',pl),writers_guard=lambda:None,verifier=lambda rd,pl,fs:entry.verify_plan(rd,evidence,pl,fs,entry.rules_sha256(),bootstrap=bootstrap))
   self.assertTrue(result['readback_verified']);self.assertEqual(remote.head,plan['candidate_commit']);self.assertEqual(reader.local.run('rev-parse','HEAD').decode().strip(),parent)
   notes[0]['content']='updated source ![x](https://example.com/a.png)';notes[0]['updated_at']='2026-10-14T00:00:00Z'
   anchor={'commit':plan['candidate_commit'],'receipt_sha256':plan['receipt_sha256']}
   second,outputs=entry.prepare(reader,evidence,remote.head,source,approved_rules_sha256=entry.rules_sha256(),anchor=anchor,bootstrap=bootstrap)
   self.assertEqual(second['proposed_content_ids'],['1'])
   registry=json.loads(outputs['Raw/03 Get/'+n.STATE])
   note_path='Raw/03 Get/'+registry['newById']['1']['filePath']
   self.assertEqual(outputs[note_path].count(assets.MARKER.encode()),1)
   self.assertEqual(hashlib.sha256(outputs[note_path]).hexdigest(),registry['newById']['1']['mirror_sha256'])


   from source_sync_assets import AssetError
   def failure(url,max_bytes):raise AssetError('download failed')
   failing=assets.SourceWithAssets(Source(notes),root/'failed-assets',fetch=failure)
   before_state=reader.read(remote.head,'Raw/03 Get/'+n.STATE,n.MAX_STATE)
   with self.assertRaises(AssetError):entry.prepare(reader,evidence,remote.head,failing,approved_rules_sha256=entry.rules_sha256(),anchor=anchor,bootstrap=bootstrap)
   self.assertEqual(reader.read(remote.head,'Raw/03 Get/'+n.STATE,n.MAX_STATE),before_state)
   self.assertEqual(remote.head,plan['candidate_commit'])
