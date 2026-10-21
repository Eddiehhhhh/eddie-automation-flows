"""Synthetic immutable history and opaque pointer tests, without real objects."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_private_proof as p
POLICY={'code_sha':'1'*40,'rules_sha256':'2'*64,'get_policy_sha256':'3'*64}
RUN={'id':11,'run_attempt':1,'head_sha':'4'*40}
CONTEXT={'run_id':11,'run_attempt':1,'code_sha':'1'*40,'controller_sha':'4'*40,'rules_sha256':'2'*64}
STATE=p.encoded({'contract':'synthetic-only','private_note':'not for public output'})
RECEIPT=p.public_encoded({'parent_commit':'b'*40,'sequence':1,'policy_sha256':'3'*64})
class Remote:
    def __init__(self):
        self.head='a'*40;self.meta={'full_name':p.REPOSITORY,'private':True};self.calls=[]
        self.commits={'a'*40:{'tree':'c'*40,'parents':['b'*40]},'b'*40:{'tree':'d'*40,'parents':[]}}
        self.entries={'c'*40:[self.row('journal',STATE),self.row('publication',RECEIPT)],'d'*40:[]}
        self.blobs={p.oid(STATE):STATE,p.oid(RECEIPT):RECEIPT}
    def row(self,kind,raw):return {'path':p.PATHS[kind],'type':'blob','mode':'100644','sha':p.oid(raw),'size':len(raw)}
    def metadata(self):self.calls.append('metadata');return deepcopy(self.meta)
    def ref(self,r):self.calls.append(('ref',r));return self.head
    def commit(self,c):self.calls.append(('commit',c));return deepcopy(self.commits[c])
    def tree_entries(self,t,r):self.calls.append(('tree',t,r));return deepcopy(self.entries[t])
    def blob(self,h,n):self.calls.append(('blob',h,n));return self.blobs[h]

def journal(cas=1):
    value={**CONTEXT,'contract':'get-notion-journal-checkpoint-1','status':'journal_readback_verified',
        'notion_policy_sha256':'5'*64,'public_policy_sha256':p.sha(p.public_encoded(POLICY)),'sequence':1,'previous_record_sha256':None,
        'parent_commit':'b'*40 if cas else None,'commit':'a'*40,'tree':'c'*40,'state_blob':p.oid(STATE),
        'state_sha256':p.sha(STATE),'state_bytes':len(STATE),'cas_attempts':cas,'readback_verified':True}
    value['record_sha256']=p.sha(value);return value

def publication(updated=True):
    value={**CONTEXT,'contract':'get-canonical-publication-anchor-1','status':'canonical_publication_verified',
        'policy_sha256':'3'*64,'observed_main_commit':'a'*40,'receipt_commit':'a'*40,'receipt_sha256':p.sha(RECEIPT),
        'receipt_sequence':1,'main_updated':updated,'cas_attempts':int(updated),'readback_verified':True}
    value['record_sha256']=p.sha(p.public_encoded(value));return value

class Proof(unittest.TestCase):
    def setUp(self):self.remote=Remote();self.source=p.PrivateRecords(self.remote)
    def resolve(self,kind,record,**kw):return self.source.resolve(p.pointer(kind,record),policy=POLICY,run=RUN,notion_policy_sha256='5'*64,**kw)
    def test_journal_exact(self):self.assertEqual(self.resolve('journal',journal()),journal())
    def test_journal_noop(self):self.assertEqual(self.resolve('journal',journal(0)),journal(0))
    def test_publication_exact(self):self.assertEqual(self.resolve('publication',publication()),publication())
    def test_publication_noop(self):self.assertEqual(self.resolve('publication',publication(False)),publication(False))
    def test_public_no_private_fields(self):
        value=p.line('journal',journal())
        for secret in ('private_note','not for public output','state_blob','state_bytes','commit','a'*40,'b'*40,p.oid(STATE)):
            self.assertNotIn(secret,value)
    def test_full_record_coverage(self):
        original=p.pointer('journal',journal())
        for key in journal():
            altered=deepcopy(journal());altered[key]='altered'
            try:self.assertNotEqual(p.pointer('journal',altered),original)
            except p.PrivateProofError:pass
    def test_domain_separation(self):
        record=publication();self.assertNotEqual(p.pointer('publication',record)['digest'],p.sha(record))
    def test_wrong_context(self):
        with self.assertRaises(p.PrivateProofError):self.source.resolve(p.pointer('journal',journal()),policy=POLICY,run={**RUN,'id':12},notion_policy_sha256='5'*64)
    def test_wrong_policy(self):
        with self.assertRaises(p.PrivateProofError):self.source.resolve(p.pointer('journal',journal()),policy={**POLICY,'rules_sha256':'9'*64},run=RUN,notion_policy_sha256='5'*64)
    def test_tampered_digest(self):
        v=p.pointer('journal',journal());v['digest']='0'*64
        with self.assertRaises(p.PrivateProofError):self.source.resolve(v,policy=POLICY,run=RUN,notion_policy_sha256='5'*64)
    def test_private_repo_required(self):
        self.remote.meta['private']=False
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',journal())
    def test_exact_repo(self):
        self.remote.meta['full_name']='other/repo'
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',journal())
    def test_merge_rejected(self):
        self.remote.commits['a'*40]['parents'].append('9'*40)
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',journal())
    def test_bad_blob(self):
        self.remote.blobs[p.oid(RECEIPT)]=RECEIPT+b' '
        with self.assertRaises(p.PrivateProofError):self.resolve('publication',publication())
    def test_duplicate_path(self):
        self.remote.entries['c'*40].append(self.remote.row('journal',STATE))
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',journal())
    def test_symlink(self):
        self.remote.entries['c'*40][0]['mode']='120000'
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',journal())
    def test_head_changed(self):
        fn=self.remote.tree_entries
        def change(h,n):self.remote.head='b'*40;return fn(h,n)
        self.remote.tree_entries=change
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',journal())
    def test_bytes_budget(self):
        with patch.object(p,'MAX_PRIVATE_BYTES',1),self.assertRaises(p.PrivateProofError):self.resolve('publication',publication())
    def test_no_network_writes_interface(self):
        self.resolve('journal',journal())
        self.assertTrue(all((v if isinstance(v,str) else v[0]) in ('metadata','ref','commit','tree','blob') for v in self.remote.calls))
    def test_expand_preserves_timestamp(self):
        raw=('2026-01-01T12:00:00.123Z '+p.line('journal',journal())+'\n').encode()
        result=p.expand(raw,lambda _:journal())
        self.assertTrue(result.startswith(b'2026-01-01T12:00:00.123Z GET_NOTION_JOURNAL_V1 '))
    def test_old_logs_unchanged(self):
        raw=b'old arbitrary\r\nGET_NOTION_JOURNAL_V1 unchanged\n';self.assertEqual(p.expand(raw,None),raw)
    def test_missing_resolver(self):
        with self.assertRaises(p.PrivateProofError):p.expand(p.line('journal',journal()).encode(),None)
    def test_wrong_resolver_record(self):
        with self.assertRaises(p.PrivateProofError):p.expand(p.line('journal',journal()).encode(),lambda _:journal(0))
    def test_pointer_extra_field(self):
        v=p.pointer('journal',journal());v['private']='forbidden'
        with self.assertRaises(p.PrivateProofError):p.checked_pointer(v)
    def test_duplicate_json(self):
        with self.assertRaises(p.PrivateProofError):p.strict(b'{"a":1,"a":1}',100)
    def test_unknown_kind(self):
        with self.assertRaises(p.PrivateProofError):p.pointer('other',journal())
    def test_sequence_prev_binding(self):
        v=journal();v['sequence']=2
        with self.assertRaises(p.PrivateProofError):p.pointer('journal',v)
    def test_immutable_snapshot_cache(self):
        self.resolve('publication',publication());calls=len([v for v in self.remote.calls if isinstance(v,tuple) and v[0]=='blob'])
        self.resolve('publication',publication());self.assertEqual(calls,len([v for v in self.remote.calls if isinstance(v,tuple) and v[0]=='blob']))
    def test_journal_metadata_only_not_body_verification(self):
        self.remote.blobs[p.oid(STATE)]=STATE+b' '
        self.assertEqual(self.resolve('journal',journal()),journal())
        self.assertFalse(any(isinstance(v,tuple) and v[0]=='blob' for v in self.remote.calls))
        # Resolution is not final state acceptance. Actual bytes still must pass.
        with self.assertRaises(p.PrivateProofError):self.source.object(self.source.node(0),'journal')
    def test_object_digest_not_independent_authority(self):
        value=p.pointer('journal',journal());value['object_digest']='9'*64
        with self.assertRaises(p.PrivateProofError):self.source.resolve(value,policy=POLICY,run=RUN,notion_policy_sha256='5'*64)
    def test_tree_metadata_tampering(self):
        for field,value in [('size',len(STATE)+1),('sha','8'*40)]:
            self.setUp();self.remote.entries['c'*40][0][field]=value
            with self.assertRaises(p.PrivateProofError):self.resolve('journal',journal())
    def test_object_digest_required(self):
        value=p.pointer('journal',journal());del value['object_digest']
        with self.assertRaises(p.PrivateProofError):p.checked_pointer(value)
    def test_publication_object_digest_forbidden(self):
        value=p.pointer('publication',publication());value['object_digest']='9'*64
        with self.assertRaises(p.PrivateProofError):p.checked_pointer(value)
    def test_opaque_state_digest_explicit(self):
        value=p.pointer('journal',journal())
        self.assertEqual(value['object_digest'],p.sha(STATE))
        self.assertEqual(set(value),{'contract','kind','sequence','previous','digest','object_digest'})
    def test_max_state_bound(self):
        self.remote.entries['c'*40][0]['size']=p.MAX_OBJECT+1
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',journal())
    def test_history_budget_exhaustion(self):
        self.source=p.PrivateRecords(self.remote,max_commits=1)
        value=journal();value['commit']='b'*40;value['tree']='d'*40
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',value)
    def test_cycle_rejected(self):
        self.remote.commits['b'*40]['parents']=['a'*40]
        value=journal();value['state_sha256']='9'*64;value['record_sha256']='8'*64
        with self.assertRaises(p.PrivateProofError):self.resolve('journal',value)
if __name__=='__main__':unittest.main()

