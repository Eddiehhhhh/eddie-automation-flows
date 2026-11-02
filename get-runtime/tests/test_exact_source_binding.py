"""Fixed historical source proof: bounded, exact and fail-closed."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_exact_source_proof as exact
import get_private_preview_retention as retention

class SourceProof(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.repo=self.root/'repo';self.repo.mkdir()
  self.git('init','-q');self.git('config','user.name','Test');self.git('config','user.email','test@example.invalid')
  self.commits=[]
  for i in range(4):
   (self.repo/'note').write_text(str(i));self.git('add','note');self.git('commit','-qm',str(i));self.commits.append(self.git('rev-parse','HEAD').strip())
  self.git('branch','-M','main');self.remote=types.SimpleNamespace(token='fixture')
  self.patch=patch.multiple(retention,REMOTE_URL=str(self.repo),safe_environment=lambda token:dict(os.environ,GIT_TERMINAL_PROMPT='0'));self.patch.start();self.addCleanup(self.patch.stop)
 def git(self,*args):return subprocess.check_output(['git','-C',str(self.repo),*args],text=True)
 def chain(self,head,observed,origin):
  with tempfile.TemporaryDirectory(dir=self.root) as d:return exact._chain(Path(d),self.remote,head,observed,origin)
 def test_valid_exact_ancestor(self):
  roots=self.chain(self.commits[3],self.commits[2],self.commits[0]);self.assertEqual(set(roots),{self.commits[2],self.commits[0]})
 def test_foreign_or_reversed_ancestor_rejected(self):
  with self.assertRaises(ValueError):self.chain(self.commits[3],'0'*40,self.commits[0])
  with self.assertRaises(ValueError):self.chain(self.commits[3],self.commits[0],self.commits[2])
 def test_wrong_head_or_budget_fails_closed(self):
  with self.assertRaises(ValueError):self.chain(self.commits[2],self.commits[1],self.commits[0])
  with patch.object(exact,'MAX_CHAIN',2):
   with self.assertRaises(ValueError):self.chain(self.commits[3],self.commits[2],self.commits[0])
 def test_corrupt_commit_hash_fails_closed(self):
  with patch.object(exact.hashlib,'sha1',return_value=types.SimpleNamespace(hexdigest=lambda:'0'*40)):
   with self.assertRaises(ValueError):self.chain(self.commits[3],self.commits[2],self.commits[0])
 def test_wrong_tree_or_blob_fails_closed(self):
  record={'observed_main_commit':'a'*40,'receipt_commit':'b'*40,'receipt_sha256':'c'*64,'receipt_sequence':2}
  row={'get_record':record,'run_binding':{'id':exact.ORIGINAL_RUN_ID,'run_attempt':1},'public_policy':{'p':1}}
  registry=types.SimpleNamespace(registry=lambda:[row]);provenance=types.SimpleNamespace(run_binding=lambda run:run)
  proof=types.SimpleNamespace(pointer=lambda *args:{'digest':exact.ORIGINAL_POINTER_SHA256},sha=lambda raw:'x'*64)
  class Source:
   head='d'*40; remote=None
   def guard(self):return self.head
   def read_node(self,commit):return {'tree':'1'*40,'parent':'2'*40}
   def object(self,*args):return b'receipt'
  modules={'get_publication_registered_predecessor':registry,'get_publication_provenance':provenance,'get_private_proof':proof}
  with patch.dict(sys.modules,modules), patch.object(exact,'_chain',return_value={'a'*40:'0'*40,'b'*40:'0'*40}):
   with self.assertRaisesRegex(ValueError,'exact_proof_tree'):exact.resolve(Source(),{'digest':exact.ORIGINAL_POINTER_SHA256},{'p':1},row['run_binding'])
  with patch.dict(sys.modules,modules), patch.object(exact,'_chain',return_value={'a'*40:'1'*40,'b'*40:'1'*40}):
   with self.assertRaisesRegex(ValueError,'exact_proof_receipt'):exact.resolve(Source(),{'digest':exact.ORIGINAL_POINTER_SHA256},{'p':1},row['run_binding'])
 def test_adjacent_run_or_digest_never_selects(self):
  # The early fixed identity test runs before any registry or network access.
  modules={'get_publication_registered_predecessor':types.SimpleNamespace(), 'get_publication_provenance':types.SimpleNamespace(), 'get_private_proof':types.SimpleNamespace()}
  with patch.dict(sys.modules,modules):
   self.assertIsNone(exact.resolve(None,{'digest':exact.ORIGINAL_POINTER_SHA256},{},{'id':exact.ORIGINAL_RUN_ID+1,'run_attempt':1}))
   self.assertIsNone(exact.resolve(None,{'digest':'0'*64},{},{'id':exact.ORIGINAL_RUN_ID,'run_attempt':1}))

if __name__=='__main__':unittest.main()
