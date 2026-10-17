"""Same-instance double tickets with the real journal CAS code; no Git/API."""
from copy import deepcopy
import json
import threading
import struct
from datetime import datetime, timezone
import get_notion_live_transport as live
import unittest
from unittest.mock import patch
import test_notion_png_store as fixtures
import get_notion_png_store as s
import get_notion_png_upload as u
import get_notion_png_transport as wire
import get_notion_remote_journal as j
import get_notion_outbox as o


class Backend(j.RemoteJournal):
    """Only object/ref plumbing is replaced; actual CAS/confirm/ticket code runs."""
    def __init__(self, value):
        self.value=deepcopy(value);self.lock=threading.Lock();self.poisoned=False
        self._png_not_dispatched=None;self.recovery=None;self.stage='fixture'
        self.confirm_entered=False;self.calls=[];self.head='1'*40
        self.confirm=lambda _:True;self.confirm_fail=False;self.ack_fail=False
        self.max_commits=100;self.max_batch_bytes=1024*1024;self.poll_attempts=1
        self.commits_attempted=0;self.new_blob_bytes=0;self.charged_blobs=set()
        self.remote=self;self.sleep=lambda _:None
    def _current(self,**kwargs):
        raw=o.encoded(self.value)
        return self.head,'2'*40,{},deepcopy(self.value),o.digest(raw),j.retention.oid(raw),raw
    def _head(self,expected=None):
        if expected is not None and expected!=self.head:raise j.JournalFailure('state_changed')
        return self.head
    def _ref(self,*args):return self.head
    def _build(self,parent,before,data):
        self.built=json.loads(data)
        commit=('%040x'%(len(self.calls)+100));tree='3'*40;blob=j.retention.oid(data)
        return commit,tree,blob,{}
    def cas_main(self,parent,commit):
        if parent!=self.head:raise j.JournalFailure('state_changed')
        self.calls.append((parent,commit));self.head=commit;self.value=deepcopy(self.built)
    def _verify(self,commit,parent,tree,wanted,data):
        if o.encoded(self.value)!=data:raise j.JournalFailure()
        return {'commit':commit,'state_blob':j.retention.oid(data),'state_sha256':o.digest(data)},deepcopy(self.value)


class Tests(fixtures.Tests):
    # Reuse fixture constructors, not inherited whole test execution below.
    def setUp(self):
        super().setUp();self.store=Backend(self.outer)
    def forward(self,send=False):
        adapter=self.adapter()
        if send:
            self.advance('create_in_flight')
            self.advance('upload_id_saved',upload_id=fixtures.UPLOAD,expiry_time='2030-01-01T01:00:00Z')
            adapter=self.adapter()
        before,sha=adapter.read();after=deepcopy(before)
        after['phase']='send_in_flight' if send else 'create_in_flight'
        after['request_sha256']=u.send_fingerprint(self.plan,after['upload_id']) if send else u.fingerprint(u.create_request(self.plan))
        newsha=adapter.compare_and_set(sha,after)
        return adapter,before,after,newsha,u.NotDispatched(after['request_sha256'])

    def test_real_kernel_transport_restore_zero_opener_and_no_post_retry(self):
        data=b'\x89PNG\r\n\x1a\n'+b'0000IHDR'+struct.pack('>II',1,1)+b'fixture'
        pin={'md_sha256':'1'*64,'reference_sha256':'2'*64,'sha256':u.sha(data),
            'bytes':len(data),'git_blob':o.p.git_blob(data)}
        with patch.object(u,'PINS',{u.sha(b'123'):pin}):
            binding=dict(pin,note_id='123',asset_path=s.PREFIX+pin['sha256']+'.png',mime='image/png',
                data_source_id=fixtures.DEST,source_commit=fixtures.COMMIT)
            plan=u.prepare(binding,u.digest(binding),{binding['asset_path']:binding['git_blob']},data)
            initial=s.add_upload(s.initialize(o.initial_state(),self.a,u.digest(self.a)),plan,u.digest(plan))
            now=lambda:datetime(2030,1,1,tzinfo=timezone.utc)
            class Opener:
                calls=0;fake_nd=False
                def open(inner,req,timeout):
                    inner.calls+=1
                    if inner.fake_nd:raise u.NotDispatched(u.fingerprint(u.create_request(plan)))
                    raise TimeoutError('physically entered fixture')
            backend=Backend(initial);adapter=s.UploadStore(backend,'123',u.digest(plan),allow_persist=True)
            opener=Opener();base=live.Transport('synthetic-test-token',opener=opener,sleep=lambda _:None,clock=lambda:1)
            base.attempts=2000
            request=wire.Transport(base,plan,u.digest(plan),data,adapter,allow_writes=True,now=now,before_write=lambda:True)
            engine=u.Upload(plan,u.digest(plan),data,adapter,request,allow_writes=True,now=now)
            with self.assertRaisesRegex(u.Blocked,'^request_not_dispatched$'):engine.advance()
            self.assertEqual(opener.calls,0);self.assertEqual(backend.value,initial)
            self.assertEqual(len(backend.calls),2)
            base.attempts=0
            with self.assertRaisesRegex(u.Blocked,'^request_not_dispatched$'):engine.advance()
            self.assertEqual(opener.calls,0);self.assertEqual(len(backend.calls),2)
            # A fresh instance has the prepared state; it does not inherit a ticket.
            fresh=s.UploadStore(backend,'123',u.digest(plan),allow_persist=True)
            self.assertEqual(fresh.read()[0]['phase'],'prepared');self.assertIsNone(fresh._not_dispatched)
            # Sending can reset only to the exact saved ID/expiry, never create.
            class Reply:
                status=200
                def __enter__(inner):return inner
                def __exit__(inner,*args):pass
                def read(inner,n):
                    return json.dumps({'object':'file_upload','id':fixtures.UPLOAD,
                        'filename':plan['filename'],'content_type':'image/png','status':'pending',
                        'expiry_time':'2030-01-01T01:00:00Z'}).encode()[:n]
            class SuccessOpener:
                calls=0
                def open(inner,*args,**kwargs):inner.calls+=1;return Reply()
            backend=Backend(initial);adapter=s.UploadStore(backend,'123',u.digest(plan),allow_persist=True)
            opener=SuccessOpener();base=live.Transport('synthetic-test-token',opener=opener,sleep=lambda _:None,clock=lambda:1)
            request=wire.Transport(base,plan,u.digest(plan),data,adapter,allow_writes=True,now=now,before_write=lambda:True)
            engine=u.Upload(plan,u.digest(plan),data,adapter,request,allow_writes=True,now=now)
            engine.advance();saved=deepcopy(backend.value);base.attempts=2000
            with self.assertRaisesRegex(u.Blocked,'^request_not_dispatched$'):engine.advance()
            self.assertEqual(backend.value,saved);self.assertEqual(opener.calls,1)
            self.assertEqual(backend.value['png_assets']['uploads']['123']['state']['upload_id'],fixtures.UPLOAD)
            # Actual opener entry can never manufacture pre-dispatch proof.
            backend=Backend(initial);adapter=s.UploadStore(backend,'123',u.digest(plan),allow_persist=True)
            opener=Opener();opener.fake_nd=True
            base=live.Transport('synthetic-test-token',opener=opener,sleep=lambda _:None,clock=lambda:1)
            request=wire.Transport(base,plan,u.digest(plan),data,adapter,allow_writes=True,now=now,before_write=lambda:True)
            engine=u.Upload(plan,u.digest(plan),data,adapter,request,allow_writes=True,now=now)
            with self.assertRaisesRegex(u.Blocked,'^request_uncertain$'):engine.advance()
            self.assertEqual(opener.calls,1);self.assertEqual(len(backend.calls),1)
            self.assertEqual(backend.value['png_assets']['uploads']['123']['state']['phase'],'create_in_flight')

    def test_two_tickets_reset_exact_create_and_send_once(self):
        for send in (False,True):
            self.store=Backend(self.outer);adapter,before,after,sha,exc=self.forward(send)
            if send:exc=u.NotDispatched(after['request_sha256'],'permission_denied')
            outer_before=deepcopy(self.store.value);outer_before['png_assets']['uploads'][fixtures.IDENT]['state']=before
            count=len(self.store.calls)
            # Ordinary full reads neither create nor consume the ticket.
            adapter.read();self.store.read();adapter.read()
            self.assertEqual(adapter.reset_not_dispatched(sha,before,exc),u.digest(before))
            self.assertEqual(self.store.value,outer_before);self.assertEqual(len(self.store.calls),count+1)
            self.assertIsNone(adapter._not_dispatched);self.assertIsNone(self.store._png_not_dispatched)
            with self.assertRaises(s.Blocked):adapter.reset_not_dispatched(sha,before,exc)

    def test_new_upload_store_cannot_recover_old_inflight(self):
        adapter,before,after,sha,exc=self.forward();fresh=self.adapter();fresh.read();count=len(self.store.calls)
        with self.assertRaises(s.Blocked):fresh.reset_not_dispatched(sha,before,exc)
        self.assertEqual(len(self.store.calls),count)

    def test_new_backend_cannot_recover_old_inflight(self):
        adapter,before,after,sha,exc=self.forward();fresh=Backend(self.store.value)
        # Even the original adapter's valid ticket cannot mint the backend one.
        adapter.store=fresh;count=len(fresh.calls)
        with self.assertRaises(j.JournalFailure):adapter.reset_not_dispatched(sha,before,exc)
        self.assertEqual(len(fresh.calls),count)

    def test_wrong_type_fingerprint_or_before_state_destroys_ticket_without_cas(self):
        for kind in ('type','fingerprint','before','expected','reason'):
            self.store=Backend(self.outer);adapter,before,after,sha,exc=self.forward();count=len(self.store.calls)
            if kind=='type':exc=ValueError('request_not_dispatched')
            if kind=='fingerprint':exc=u.NotDispatched('0'*64)
            if kind=='before':before['expiry_time']='2030-01-01T00:00:00Z'
            if kind=='expected':sha='0'*64
            if kind=='reason':exc=u.NotDispatched(after['request_sha256'],'unknown')
            with self.subTest(kind=kind),self.assertRaises(Exception):adapter.reset_not_dispatched(sha,before,exc)
            self.assertEqual(len(self.store.calls),count);self.assertIsNone(adapter._not_dispatched)

    def test_any_other_cas_even_noop_invalidates_backend_ticket(self):
        adapter,before,after,sha,exc=self.forward();value,version=self.store.read()
        self.store.compare_and_set(version,value)
        with self.assertRaises(j.JournalFailure):adapter.reset_not_dispatched(sha,before,exc)

    def test_other_outer_change_invalidates_both_layers(self):
        adapter,before,after,sha,exc=self.forward();value,version=self.store.read()
        changed=s.add_upload(value,fixtures.plan(fixtures.SECOND),u.digest(fixtures.plan(fixtures.SECOND)))
        self.store.compare_and_set(version,changed);adapter.read();count=len(self.store.calls)
        with self.assertRaises(s.Blocked):adapter.reset_not_dispatched(sha,before,exc)
        self.assertEqual(len(self.store.calls),count)

    def test_failed_confirm_cannot_issue_either_ticket(self):
        self.store.confirm=lambda _:False
        adapter=self.adapter();before,sha=adapter.read();after=deepcopy(before)
        after.update(phase='create_in_flight',request_sha256=u.fingerprint(u.create_request(self.plan)))
        with self.assertRaises(j.JournalFailure):adapter.compare_and_set(sha,after)
        self.assertIsNone(adapter._not_dispatched);self.assertIsNone(self.store._png_not_dispatched)
        self.assertTrue(self.store.poisoned)

    def test_reset_confirm_failure_consumes_ticket_and_remains_unknown(self):
        adapter,before,after,sha,exc=self.forward();self.store.confirm=lambda _:False
        with self.assertRaises(j.JournalFailure) as failure:adapter.reset_not_dispatched(sha,before,exc)
        self.assertEqual(failure.exception.outcome,'unknown')
        self.assertIsNone(adapter._not_dispatched);self.assertIsNone(self.store._png_not_dispatched)

    def test_ordinary_cas_cannot_reverse_even_with_ticket(self):
        adapter,before,after,sha,exc=self.forward()
        with self.assertRaises(s.Blocked):adapter.compare_and_set(sha,before)
        value,version=self.store.read();value['png_assets']['uploads'][fixtures.IDENT]['state']=before
        with self.assertRaises(j.JournalFailure):self.store.compare_and_set(version,value)

    def test_backend_without_special_method_never_falls_back(self):
        self.store=fixtures.Store(self.outer);adapter,before,after,sha,exc=self.forward();count=len(self.store.calls)
        with self.assertRaisesRegex(s.Blocked,'unsupported'):adapter.reset_not_dispatched(sha,before,exc)
        self.assertEqual(len(self.store.calls),count)

    def test_restoration_cannot_remove_http_proof_or_other_outer_field(self):
        adapter,before,after,sha,exc=self.forward();current,version=self.store.read()
        current['png_assets']['uploads'][fixtures.IDENT]['state']['write_http']={'status':500,'request_sha256':after['request_sha256']}
        self.store.compare_and_set(version,current);count=len(self.store.calls)
        with self.assertRaises(Exception):adapter.reset_not_dispatched(sha,before,exc)
        self.assertEqual(len(self.store.calls),count)

# Avoid inheriting the 19 separately-run base test methods under a new backend.
for name in list(vars(fixtures.Tests)):
    if name.startswith('test_') and name not in Tests.__dict__:setattr(Tests,name,None)

if __name__=='__main__':unittest.main()
