"""Synthetic timing tests: no accounts, paths, note IDs or production evidence."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_journal_timing as t

class Tests(unittest.TestCase):
    def test_result_identity_and_single_call(self):
        timer=t.Timer(iter((10, 35)).__next__); value=object(); calls=[]
        self.assertIs(timer.call('push', lambda: (calls.append(1), value)[1]), value)
        self.assertEqual(calls,[1]); self.assertEqual(timer.rows['push'],
            {'calls':1,'errors':0,'timed_calls':1,'elapsed_ns':25})

    def test_original_exception_identity_no_retry(self):
        timer=t.Timer(iter((10, 35)).__next__); error=RuntimeError('synthetic'); calls=[]
        def fail(): calls.append(1); raise error
        with self.assertRaises(RuntimeError) as got: timer.call('push',fail)
        self.assertIs(got.exception,error); self.assertEqual(calls,[1])
        self.assertEqual(timer.rows['push']['errors'],1)

    def test_absent_clock(self):
        with patch.object(t.time,'monotonic_ns',None): timer=t.Timer()
        self.assertEqual(timer.call('push',lambda:7),7)
        self.assertEqual(timer.rows['push']['timed_calls'],0)

    def test_raising_clock_does_not_mask_failure(self):
        def bad(): raise ValueError('clock')
        timer=t.Timer(bad); error=RuntimeError('business')
        def fail():raise error
        with self.assertRaises(RuntimeError) as got:timer.call('push',fail)
        self.assertIs(got.exception,error)

    def test_clock_backwards_and_invalid_values(self):
        for values in ((20,10),(False,20),(10,float('nan'))):
            timer=t.Timer(iter(values).__next__)
            self.assertEqual(timer.call('push',lambda:7),7)
            self.assertEqual(timer.rows['push']['timed_calls'],0)

    def test_output_failure_cannot_replace_business_failure(self):
        error=RuntimeError('business')
        class Example:
            _timing=t.Timer(iter((10,20)).__next__)
            @t.observed('cas_total',emit=True)
            def run(self):raise error
        with patch('builtins.print',side_effect=OSError('output')):
            with self.assertRaises(RuntimeError) as got:Example().run()
        self.assertIs(got.exception,error)

    def test_missing_timer_preserves_legacy_fixture(self):
        class Example:
            @t.observed('cas_total',emit=True)
            def run(self):return 7
        self.assertEqual(Example().run(),7)

    def test_nested_totals_are_explicitly_inclusive(self):
        timer=t.Timer(iter((0,10,30,50)).__next__)
        timer.call('readback',lambda:timer.call('remote_state_blob',lambda:7))
        self.assertEqual(timer.rows['readback']['elapsed_ns'],50)
        self.assertEqual(timer.rows['remote_state_blob']['elapsed_ns'],20)
        stream=io.StringIO()
        with contextlib.redirect_stderr(stream):timer.emit()
        value=json.loads(stream.getvalue().split(' ',1)[1])
        self.assertTrue(value['inclusive']);self.assertEqual(set(value),{'contract','inclusive','phases'})

    def test_unknown_label_is_not_logged(self):
        timer=t.Timer(iter((0,10)).__next__);timer.call('synthetic-secret',lambda:7)
        self.assertEqual(timer.rows,{})

@contextlib.contextmanager
def synthetic_journal(mode):
    """Load the real CAS implementation with self-contained synthetic edges.

    Only the transport, object/state codec and unrelated preservation hooks are
    synthetic. CAS ordering, exception classification, poison and confirmation
    are the actual published methods. All module substitutions are scoped.
    """
    import hashlib
    import importlib.util
    import threading
    from copy import deepcopy
    from types import SimpleNamespace

    encode=lambda value:json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    digest=lambda value:hashlib.sha256(value if isinstance(value,bytes) else encode(value)).hexdigest()
    oid=lambda raw:hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    def preserve(old,new):
        if old.get('protected') != new.get('protected'):raise ValueError('synthetic protection')
    def check(value):
        if set(value) != {'engine_state','revision','protected'}:raise ValueError('synthetic shape')
    codec=SimpleNamespace(encoded=encode,digest=digest,check_state=check,Blocked=ValueError,
        png_store=SimpleNamespace(preserve=preserve,not_dispatched_ticket=lambda *args:None))
    engine=SimpleNamespace(Blocked=ValueError,plan_recovery=SimpleNamespace(preserve_holds=preserve))
    modules={'get_notion_executor':engine,'get_notion_outbox':codec,
        'get_private_preview_retention':SimpleNamespace(oid=oid,GitHubRemote=SimpleNamespace(source_get_tree=lambda *args:None)),
        'get_notion_historical':SimpleNamespace(preserve_captures=preserve),
        'get_notion_historical_active_recovery':SimpleNamespace(preserve=preserve),
        'get_notion_authenticated_recovery':SimpleNamespace(preserve=preserve)}
    path=Path(t.__file__).with_name('get_notion_remote_journal.py')
    spec=importlib.util.spec_from_file_location('_synthetic_timing_journal',path)
    journal=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,modules):
        spec.loader.exec_module(journal)
        class Store(journal.RemoteJournal):
            def __init__(self):
                self._timing=t.Timer();self.poisoned=False;self.recovery=None
                self.confirm_entered=False;self.stage='anchor';self.lock=threading.Lock()
                self._png_not_dispatched=None;self.post_cas_paths=frozenset();self.post_cas_mirror=False
                self.max_commits=400;self.max_batch_bytes=192*1024*1024;self.poll_attempts=2
                self.commits_attempted=0;self.new_blob_bytes=0;self.charged_blobs=set()
                self.old={'engine_state':{'protected':True},'revision':0,'protected':True}
                self.new={**deepcopy(self.old),'revision':1};self.initial='a'*40;self.candidate='b'*40
                self.head=self.initial;self.remote=SimpleNamespace(cas_main=self.push)
                self.calls=[];self.confirmations=[];self.external_writes=[];self.delays=[]
                self.sleep=self.delays.append;self.confirm=self.callback
                self.anchor={'commit':self.initial,'state_blob':None,'state_sha256':digest(self.old)}
            def _current(self,**kwargs):
                self._ready()
                return self.initial,'c'*40,{},deepcopy(self.old),digest(self.old),None,encode(self.old)
            def _build(self,parent,before,data):
                return self.candidate,'d'*40,oid(data),{}
            def _head(self,expected=None):
                if expected is not None and expected != self.head:raise journal.JournalFailure('state_changed')
                return self.head
            def _post_cas_head(self,commit,observed=None):return self._head(commit)
            def push(self,parent,commit):
                self.calls.append((parent,commit))
                if mode=='reject':raise TimeoutError('synthetic push')
                self.head=commit
            def _ref(self):
                if mode=='readback_failure':raise TimeoutError('synthetic readback')
                return self.initial if mode=='stale' else self.head
            def _verify(self,commit,parent,tree,wanted,data):
                receipt={'commit':commit,'state_blob':oid(data),'state_sha256':digest(data),'cas_attempts':1}
                return receipt,deepcopy(self.new)
            def callback(self,receipt):
                self.confirmations.append(deepcopy(receipt))
                return mode!='confirmation_failure'
            def execute(self):
                try:self.compare_and_set_verified(digest(self.old),self.new)
                except journal.JournalFailure as exc:
                    return {'status':'blocked','reason':str(exc),'external_write_attempts':0}
                self.external_writes.append('synthetic write')
                return {'status':'verified','external_write_attempts':1}
        yield Store(),journal,digest


class JournalFailureIntegrationTests(unittest.TestCase):
    def assert_poisoned_without_retry(self,store,journal,digest):
        self.assertTrue(store.poisoned)
        with self.assertRaises(journal.JournalFailure):store.read()
        with self.assertRaises(journal.JournalFailure):
            store.compare_and_set_verified(digest(store.old),store.new)
        self.assertEqual(len(store.calls),1)

    def test_missing_clock_keeps_unknown_poison_and_one_attempt(self):
        with patch.object(t.time,'monotonic_ns',None),synthetic_journal('readback_failure') as (store,journal,digest):
            with self.assertRaises(journal.JournalFailure) as got:
                store.compare_and_set_verified(digest(store.old),store.new)
            self.assertEqual(str(got.exception),'journal_failed')
            self.assertEqual(got.exception.outcome,'unknown')
            self.assertEqual(len(store.calls),1);self.assertEqual(store.confirmations,[])
            self.assert_poisoned_without_retry(store,journal,digest)

    def test_output_failure_does_not_unlock_external_write(self):
        with synthetic_journal('confirmation_failure') as (store,journal,digest):
            with patch('builtins.print',side_effect=OSError('synthetic output failure')):
                result=store.execute()
            self.assertEqual(result,{'status':'blocked','reason':'journal_failed','external_write_attempts':0})
            self.assertEqual(store.external_writes,[]);self.assertTrue(store.poisoned)
            self.assertEqual(store.recovery['commit'],store.head)
            self.assertEqual(len(store.confirmations),1)
            self.assert_poisoned_without_retry(store,journal,digest)

    def test_clock_exception_keeps_stale_cas_no_retry(self):
        for mode in ('reject','stale'):
            with self.subTest(mode=mode),patch.object(t.time,'monotonic_ns',side_effect=RuntimeError('synthetic clock failure')):
                with synthetic_journal(mode) as (store,journal,digest):
                    with self.assertRaises(journal.JournalFailure) as got:
                        store.compare_and_set_verified(digest(store.old),store.new)
                    self.assertEqual(got.exception.outcome,'unknown')
                    self.assertEqual(len(store.calls),1);self.assertEqual(store.confirmations,[])
                    self.assert_poisoned_without_retry(store,journal,digest)

if __name__=='__main__':unittest.main()
