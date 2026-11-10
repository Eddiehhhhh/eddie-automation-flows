"""One authenticated registered observer may cover a bounded 1358-commit tail."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_legacy_adapter as adapter
import get_publication_provenance as provenance
import get_recurring_entry as entry


class RegisteredHistory(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.tmp.cleanup)
  cls.root=Path(cls.tmp.name).resolve();cls.env=adapter.retention.safe_environment(None)
  cls.git('init','-q');cls.git('config','user.name','Test');cls.git('config','user.email','test@example.invalid')
  note=cls.root/'Raw/03 Get/note.md';note.parent.mkdir(parents=True);note.write_text('original')
  receipt=cls.root/entry.FULL_RECEIPT;receipt.parent.mkdir();receipt.write_bytes(entry.a.encoded({'sequence':2}))
  cls.git('add','--all');cls.git('commit','-qm','synthetic authenticated receipt');cls.base=cls.git('rev-parse','HEAD')
  wire=[]
  for branch,offset,tamper in [('straight',1,False),('tampered',10000,True)]:
   for i in range(1359):
    mark=offset+i;parent=cls.base if i==0 else ':'+str(mark-1);message='history '+str(i)
    change=''
    if tamper and i in (100,101):
     value='changed' if i==100 else 'original'
     change='M 100644 inline "Raw/03 Get/note.md"\ndata '+str(len(value))+'\n'+value+'\n'
    wire.append('commit refs/heads/'+branch+'\nmark :'+str(mark)+'\ncommitter Test <test@example.invalid> '+str(1700000000+i)+' +0000\ndata '+str(len(message))+'\n'+message+'\nfrom '+parent+'\n'+change+'\n')
  cls.git('fast-import','--quiet',payload=''.join(wire))
  chain=cls.git('rev-list','--first-parent','--reverse','refs/heads/straight','^'+cls.base).splitlines()
  assert len(chain)==1359
  cls.observed=chain[0];cls.target=chain[-1];cls.tampered=cls.git('rev-parse','refs/heads/tampered')
  cls.reader=adapter.OfflineGit(cls.root)
  record={'receipt_commit':cls.base,'receipt_sha256':entry.a.sha(receipt.read_bytes()),'receipt_sequence':2,'observed_main_commit':cls.observed,'record_sha256':'f'*64}
  # Synthetic registration boundary; the production registry retains its full original validation.
  cls.row={'run_binding':{'id':40657306710,'run_attempt':1},'job_id':7,'job_sha256':'a'*64,'get_record':record}
  cls.verified={'run_id':40657306710,'run_attempt':1,'job_id':7,'anchor':{'commit':cls.base,'receipt_sha256':record['receipt_sha256']},'receipt_sequence':2,'record_sha256':record['record_sha256'],'verified_details':{'run_binding':cls.row['run_binding'],'job_sha256':cls.row['job_sha256'],'record':record}}
 @classmethod
 def git(cls,*args,payload=None):
  return subprocess.check_output(['git','-C',str(cls.root),*args],env=cls.env,input=payload,text=True,stderr=subprocess.DEVNULL).strip()
 def witness(self):
  with patch.object(provenance.registered_predecessor,'registry',return_value=[self.row]):
   return provenance._registered_history_witness_from_verified(deepcopy(self.verified))
 def test_01_registered_observation_accepts_real_1358_tail(self):
  witness=self.witness();self.assertEqual(entry.MAX_INTERVENING,1024)
  self.assertEqual(entry.history_limit(witness,self.base),2048)
  self.assertEqual(entry.history_limit(witness,'0'*40),1024)
  entry.unchanged_get_history(self.reader,self.base,self.target,history_witness=witness)
 def test_02_same_fields_without_authorization_keep_1024(self):
  authorized=self.witness()
  ordinary=entry.VerifiedHistoryWitness(authorized.receipt_commit,authorized.receipt_sha256,authorized.receipt_sequence,authorized.observed_commit,authorized.record_sha256)
  self.assertEqual(entry.history_limit(ordinary,self.base),1024)
  with self.assertRaisesRegex(RuntimeError,'entry_history_bound'):
   entry.unchanged_get_history(self.reader,self.base,self.target,history_witness=ordinary)
 def test_03_intermediate_get_edit_then_revert_still_rejected(self):
  self.assertEqual(self.reader.source_get_tree(self.base),self.reader.source_get_tree(self.tampered))
  with self.assertRaisesRegex(RuntimeError,'entry_foreign_get_change'):
   entry.unchanged_get_history(self.reader,self.base,self.tampered,history_witness=self.witness())
 def test_04_mutated_run_job_or_record_never_authorizes(self):
  for field,value in [('run_id',40657306711),('anchor',{'commit':'0'*40,'receipt_sha256':'1'*64}),('receipt_sequence',3)]:
   changed=deepcopy(self.verified);changed[field]=value
   with patch.object(provenance.registered_predecessor,'registry',return_value=[self.row]):
    with self.assertRaisesRegex(RuntimeError,'provenance_history_witness'):
     provenance._registered_history_witness_from_verified(changed)
  changed=deepcopy(self.verified);changed['verified_details']['job_sha256']='0'*64
  with patch.object(provenance.registered_predecessor,'registry',return_value=[self.row]):
   with self.assertRaisesRegex(RuntimeError,'provenance_history_witness'):
    provenance._registered_history_witness_from_verified(changed)

if __name__=='__main__':unittest.main()
