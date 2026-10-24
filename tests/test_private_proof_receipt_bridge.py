"""Synthetic predecessor receipts; no private capsule or production IDs."""
from copy import deepcopy
import sys
import types
import unittest
from unittest.mock import patch
from test_private_proof import p, Remote, POLICY, RUN, publication

class ReceiptBridge(unittest.TestCase):
    def setUp(self):
        self.remote=Remote()
        self.receipt={'parent_commit':'b'*40,'sequence':1,'policy_sha256':'9'*64}
        self.raw=p.public_encoded(self.receipt)
        self.remote.entries['c'*40]=[self.remote.row('publication',self.raw)]
        self.remote.blobs[p.oid(self.raw)]=self.raw
        self.record=publication(False)
        self.record['receipt_sha256']=p.sha(self.raw)
        self.record.pop('record_sha256')
        self.record['record_sha256']=p.sha(p.public_encoded(self.record))
        self.calls=[]
    def accepted(self,origin,digest,receipt,policy):
        self.calls.append((origin,digest,deepcopy(receipt),policy))
        return (origin,digest,receipt,policy)==('a'*40,p.sha(self.raw),self.receipt,POLICY['get_policy_sha256'])
    def resolve(self,accept=None,policy=POLICY):
        module=types.SimpleNamespace(accepts_receipt=accept or self.accepted,current_get_policy=lambda:POLICY['get_policy_sha256'])
        with patch.dict(sys.modules,{'get_recurring_policy_recovery':module}):
            return p.PrivateRecords(self.remote).resolve(p.pointer('publication',self.record),policy=policy,run=RUN)
    def test_exact_bridge(self):
        self.assertEqual(self.resolve(),self.record)
        self.assertEqual(self.calls,[('a'*40,p.sha(self.raw),self.receipt,POLICY['get_policy_sha256'])])
    def test_bridge_denial(self):
        with self.assertRaises(p.PrivateProofError):self.resolve(lambda *a:False)
    def test_external_digest_still_required(self):
        self.record['receipt_sha256']='0'*64
        with self.assertRaises(p.PrivateProofError):self.resolve()
    def test_origin_parent_authenticated_before_bridge(self):
        self.remote.commits['a'*40]['parents']=['e'*40]
        with self.assertRaises(p.PrivateProofError):self.resolve()
        self.assertEqual(self.calls,[])
    def test_actual_blob_bytes_required(self):
        self.remote.blobs[p.oid(self.raw)]=self.raw+b'\n'
        with self.assertRaises(p.PrivateProofError):self.resolve()
        self.assertEqual(self.calls,[])
    def test_sequence_mismatch_never_invokes_bridge(self):
        self.record['receipt_sequence']=2
        with self.assertRaises(p.PrivateProofError):self.resolve()
        self.assertEqual(self.calls,[])
    def test_changed_requested_policy_not_admitted(self):
        with self.assertRaises(p.PrivateProofError):self.resolve(policy={**POLICY,'get_policy_sha256':'8'*64})
    def test_bridge_exception_fails_closed(self):
        def broken(*args):raise ValueError('do not print a private message')
        with self.assertRaisesRegex(p.PrivateProofError,'^private_proof_unverified$'):self.resolve(broken)

if __name__=='__main__':unittest.main()
