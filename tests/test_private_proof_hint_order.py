"""Synthetic search ordering; locators never replace external proof authority."""
import unittest
from copy import deepcopy
from test_private_proof import p, Remote, POLICY, RUN, publication, RECEIPT

class HintOrder(unittest.TestCase):
 def setUp(self):
  self.remote=Remote();r=self.remote
  r.commits={'a'*40:{'tree':'c'*40,'parents':['d'*40]},'d'*40:{'tree':'f'*40,'parents':['b'*40]},'e'*40:{'tree':'7'*40,'parents':['b'*40]},'b'*40:{'tree':'8'*40,'parents':[]}}
  self.set_hint('e'*40)
  r.entries['f'*40]=[r.row('publication',RECEIPT)];r.entries['7'*40]=[r.row('publication',RECEIPT)];r.entries['8'*40]=[]
  self.record=publication(False);self.record['receipt_commit']='d'*40;self.rehash()
 def set_hint(self,commit):
  raw=p.encoded({'last_enqueued_publication':{'commit':commit,'sequence':1}})
  self.remote.entries['c'*40]=[self.remote.row('journal',raw),self.remote.row('publication',RECEIPT)];self.remote.blobs[p.oid(raw)]=raw
 def rehash(self):self.record.pop('record_sha256',None);self.record['record_sha256']=p.sha(p.public_encoded(self.record))
 def resolve(self):return p.PrivateRecords(self.remote).resolve(p.pointer('publication',self.record),policy=POLICY,run=RUN)
 def test_valid_hint_wrong_origin_preserves_fallback(self):self.assertEqual(self.resolve(),self.record)
 def test_fallback_resolves_before_reading_older_unrelated_node(self):
  del self.remote.commits['b'*40]
  self.assertEqual(self.resolve(),self.record)
  self.assertNotIn(('commit','b'*40),self.remote.calls)
 def test_valid_hint_advances_observed_node_before_deep_fallback(self):
  self.set_hint('d'*40);self.record['observed_main_commit']='d'*40;self.rehash()
  self.assertEqual(self.resolve(),self.record)
  self.assertNotIn(('commit','b'*40),self.remote.calls)
 def test_wrong_hint_parent_does_not_authorize(self):
  self.remote.commits['e'*40]['parents']=['9'*40]
  self.assertEqual(self.resolve(),self.record)
 def test_hint_wrong_blob_not_authorized(self):
  raw=p.public_encoded({'parent_commit':'b'*40,'sequence':2,'policy_sha256':'3'*64})
  self.remote.entries['7'*40]=[self.remote.row('publication',raw)];self.remote.blobs[p.oid(raw)]=raw
  self.assertEqual(self.resolve(),self.record)
 def test_bad_hint_actual_bytes_fail_not_swallowed(self):
  self.remote.blobs[p.oid(RECEIPT)]=RECEIPT+b'\n'
  with self.assertRaises(p.PrivateProofError):self.resolve()
 def test_authorized_locator_cannot_replace_full_record(self):
  self.record['receipt_commit']='9'*40;self.rehash()
  with self.assertRaises(p.PrivateProofError):self.resolve()
 def test_private_denial_not_swallowed(self):
  self.remote.commit=lambda sha: (_ for _ in ()).throw(PermissionError('private detail'))
  with self.assertRaisesRegex(p.PrivateProofError,'^private_proof_unverified$'):self.resolve()
 def test_malformed_hint_commit_object_not_swallowed(self):
  self.remote.commits['e'*40]['parents']='not a list'
  with self.assertRaises(p.PrivateProofError):self.resolve()

if __name__=='__main__':unittest.main()
