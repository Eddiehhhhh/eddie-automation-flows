"""Synthetic capability/current-bound contracts. No private fixtures or network.
Full original source/capture/apply entry replay is a separate local audit.
"""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]/'scripts'
def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':')).encode()
def digest(v):return hashlib.sha256(v if type(v)is bytes else encoded(v)).hexdigest()
class Blocked(ValueError):pass
def fail(reason):raise Blocked(reason)

def module():
    tree=ast.parse((ROOT/'get_notion_historical_refresh.py').read_text())
    code=ast.Module(body=[x for x in tree.body if not isinstance(x,(ast.Import,ast.ImportFrom))],type_ignores=[])
    e=NS(fail=fail,digest=digest,p=NS())
    h=NS(CONTRACT='body',CAPTURE_CONTRACT='capture')
    outbox=NS(digest=digest,check_state=lambda state:None)
    ns={'deepcopy':deepcopy,'e':e,'h':h,'outbox':outbox,'inputs':NS()}
    exec(compile(code,str(ROOT/'get_notion_historical_refresh.py'),'exec'),ns)
    return NS(**ns)

class Capabilities(unittest.TestCase):
    def setUp(self):
        self.m=module();self.state={'engine_state':{'journal':None}}
        self.remote=NS(ref=lambda name:'a'*40)
        self.store=NS(poisoned=False,remote=self.remote,read=lambda:(deepcopy(self.state),digest(self.state)))
        self.session=self.m.Session(self.store,('capture','1'*64,()),self.state,'a'*40,{},None,self.m._SEAL)
        self.old={'contract':'capture','source_commit':'b'*40,'records':{'1000000000000000001':{'blob':'c'*40}}}
        self.candidate=dict(self.old,source_commit='d'*40)
        self.cap=self.m.Capability(self.session,self.old,self.candidate,{'synthetic':'properties'},{'property_manifest':{'synthetic':'properties'}},self.m._SEAL)
        self.session.caps[digest(self.old)]=self.cap
    def test_raw_digest_and_ducktype_are_not_approval(self):
        for cap in (None,{},NS(**self.cap.__dict__) if hasattr(self.cap,'__dict__') else NS(seal=self.m._SEAL)):
            with self.assertRaises(Blocked):self.m.authorize(cap,self.candidate,digest(self.candidate),'capture')
    def test_constructor_needs_private_seal(self):
        with self.assertRaises(Blocked):self.m.Capability(self.session,self.old,self.candidate,{}, {},object())
        with self.assertRaises(Blocked):self.m.Session(self.store,(),self.state,'a'*40,{},None,object())
    def test_exact_kind_and_complete_bytes(self):
        self.assertTrue(self.m.authorize(self.cap,self.candidate,digest(self.candidate),'capture'))
        for value,sha,kind in ((dict(self.candidate,extra=True),digest(self.candidate),'capture'),
                              (self.candidate,'0'*64,'capture'),(self.candidate,digest(self.candidate),'apply'),
                              (self.candidate,digest(self.candidate),'unknown')):
            with self.assertRaises(Blocked):self.m.authorize(self.cap,value,sha,kind)
    def test_mutation_after_issuance_rejects(self):
        for field in ('candidate','manifest','properties','mapping','old'):
            with self.subTest(field=field):
                original=deepcopy(getattr(self.cap,field));getattr(self.cap,field)['tampered']=True
                with self.assertRaises(Blocked):self.cap.check(self.candidate,digest(self.candidate),'capture')
                setattr(self.cap,field,original)
    def test_different_original_selector_rejects(self):
        with self.assertRaises(Blocked):self.session.capability(self.old,'f'*64,'capture')
    def test_same_state_nonget_head_race_rejects(self):
        self.remote.ref=lambda name:'e'*40
        with self.assertRaises(Blocked):self.session.capability(self.old,digest(self.old),'capture')
    def test_changed_state_or_poison_rejects(self):
        self.store.poisoned=True
        with self.assertRaises(Blocked):self.session.capability(self.old,digest(self.old),'capture')
        self.store.poisoned=False;self.state['unexpected']=True
        with self.assertRaises(Blocked):self.session.capability(self.old,digest(self.old),'capture')
    def test_toctou_at_original_cas_boundary_rejects(self):
        with self.assertRaises(Blocked):self.session.before_cas(self.state,dict(self.state,changed=True),'e'*40)
        self.assertIsNone(self.session.pending)
    def test_only_confirmed_own_cas_advances(self):
        after=dict(self.state,changed=True)
        self.session.before_cas(self.state,after,'a'*40)
        with self.assertRaises(Blocked):self.session.confirmed({'cas_attempts':0})
        with self.assertRaises(Blocked):self.session.confirmed({'cas_attempts':1,'state_sha256':'f'*64,'commit':'e'*40})
        self.session.confirmed({'cas_attempts':1,'state_sha256':digest(after),'commit':'e'*40})
        self.assertEqual(self.session.current,after);self.assertEqual(self.session.parent,'e'*40)
        self.assertIsNone(self.session.pending)
    def test_second_cas_before_confirmation_rejects(self):
        self.session.before_cas(self.state,dict(self.state,changed=True),'a'*40)
        with self.assertRaises(Blocked):self.session.before_cas(self.state,dict(self.state,changed=True),'a'*40)

class WholeScope(unittest.TestCase):
    def setUp(self):
        self.m=module();self.ident='1000000000000000001';self.candidate={'records':{self.ident:{}}}
        self.state={'engine_state':{'journal':None,'receipts':{},'failures':{}},'pending_by_id':{},'rotation_order':[],'historical_captures':{}}
    def test_entire_unexecuted_scope(self):
        self.m._empty_scope(self.state,[self.candidate])
    def test_active_or_unknown_is_never_refreshable(self):
        for op in (None,{'phase':'prepared'},{'phase':'in_flight'},{'phase':'acknowledged'}):
            self.state['engine_state']['journal']={'operation':op}
            with self.assertRaises(Blocked):self.m._empty_scope(self.state,[self.candidate])
    def test_any_receipt_failure_or_pending_holds_whole_cohort(self):
        for key in ('receipts','property_receipts','metadata_results','failures','unresolved_by_id'):
            state=deepcopy(self.state);state['engine_state'][key]={self.ident:{}}
            with self.subTest(key=key),self.assertRaises(Blocked):self.m._empty_scope(state,[self.candidate])
        for key in ('pending_by_id','png_assets','png_assets_seq2'):
            state=deepcopy(self.state);state[key]={self.ident:{}}
            with self.subTest(key=key),self.assertRaises(Blocked):self.m._empty_scope(state,[self.candidate])
    def test_existing_capture_not_relabelled(self):
        self.state['historical_captures']['a'*64]={'candidate':self.candidate}
        with self.assertRaises(Blocked):self.m._empty_scope(self.state,[self.candidate])
        self.m._empty_scope(self.state,[self.candidate],allow_captures=('a'*64,))
    def test_pending_rotation_not_ignored(self):
        self.state['rotation_order']=[self.ident]
        with self.assertRaises(Blocked):self.m._empty_scope(self.state,[self.candidate])

if __name__=='__main__':unittest.main()
