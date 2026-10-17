"""In-memory CAS + actual RemoteJournal pre-write guard; no refs/API/network."""
from copy import deepcopy
from pathlib import Path
import sys
import struct
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_png_store as s
import get_notion_png_upload as u
import get_notion_outbox as o
import get_notion_remote_journal as remote

IDENT = '123'
SECOND = '456'
PAGE = '1' * 32
BLOCK = '2' * 32
UPLOAD = '3' * 32
DEST = '4' * 32
COMMIT = 'a' * 40
BLOB = 'b' * 40


def anchor():
    return {'contract': s.EVIDENCE_CONTRACT, 'repository': o.p.REPOSITORY,
        'publication_commit': COMMIT, 'asset_commit': 'c' * 40,
        'manifest_path': s.PREFIX + 'reviewed-png-manifest-v1.json',
        'manifest_blob': 'd' * 40, 'manifest_sha256': 'e' * 64}


def plan(ident=IDENT):
    pin = u.PINS[u.sha(ident.encode())]
    b = dict(pin, note_id=ident, asset_path=s.PREFIX + pin['sha256'] + '.png',
        mime='image/png', data_source_id=DEST, source_commit=COMMIT)
    return {'contract': 'three-png-upload-1', 'binding': b,
        'filename': 'get-' + b['sha256'] + '.png', 'version': u.VERSION,
        'asset_tree_sha256': u.digest({s.PREFIX + r['sha256'] + '.png': r['git_blob'] for r in u.PINS.values()}),
        'multipart_body_sha256': 'f' * 64}


class Store:
    def __init__(self, value): self.value = deepcopy(value); self.calls = []; self.bad = None
    def read(self):
        value = deepcopy(self.value)
        if self.bad == 'readback' and self.calls: value['rotation_order'].append('999')
        return value, o.digest(value)
    def compare_and_set(self, expected, value):
        if expected != o.digest(self.value): raise o.Blocked('outbox_state_changed')
        self.calls.append(deepcopy(value)); self.value = deepcopy(value)
        return 'f' * 64 if self.bad == 'ack' else o.digest(value)


class Tests(unittest.TestCase):
    def setUp(self):
        self.dest = patch.object(u, 'DESTINATION_PIN', u.sha(DEST.encode())); self.dest.start(); self.addCleanup(self.dest.stop)
        self.pins = patch.object(u, 'PINS', {u.sha(ident.encode()): deepcopy(row) for ident,row in zip(('123','456','789'),u.PINS.values())})
        self.pins.start(); self.addCleanup(self.pins.stop)
        self.a = anchor(); self.plan = plan()
        self.outer = s.add_upload(s.initialize(o.initial_state(), self.a, u.digest(self.a)), self.plan, u.digest(self.plan))
        self.store = Store(self.outer)

    def adapter(self, allow=True, ident=IDENT):
        return s.UploadStore(self.store, ident, u.digest(plan(ident)), allow_persist=allow)

    def advance(self, phase, **changes):
        adapter = self.adapter(); value, sha = adapter.read()
        value.update(phase=phase, **changes)
        if phase in ('create_in_flight', 'upload_id_saved'):
            value['request_sha256'] = u.fingerprint(u.create_request(self.plan))
        elif phase in ('send_in_flight', 'uploaded_verified'):
            value['request_sha256'] = u.send_fingerprint(self.plan, value['upload_id'])
        adapter.compare_and_set(sha, value)
        return self.store.value

    def uploaded(self):
        self.advance('create_in_flight')
        self.advance('upload_id_saved', upload_id=UPLOAD, expiry_time='2030-01-01T01:00:00Z')
        self.advance('send_in_flight')
        self.advance('uploaded_verified', expiry_time=None)
        return self.store.value

    def receipt(self):
        return {'note_id': IDENT, 'page_id': PAGE, 'data_source_id': DEST, 'source_commit': COMMIT,
            'md_blob': BLOB, 'renderer_version': o.p.RENDERER,
            'managed_blocks': [{'id': BLOCK, 'normalized_hash': '6' * 64}], 'protected_blocks': [],
            'properties_baseline': {'笔记 ID': {'type': 'rich_text', 'rich_text': [o.p.text_run(IDENT)]}},
            'asset_bindings': {}, 'readback_verified': True}

    def candidate(self):
        assets = self.store.value['png_assets']; row = assets['uploads'][IDENT]
        b = row['plan']['binding']; receipt = s.receipt_for(row)
        return {'note_id': IDENT, 'page_id': PAGE, 'block_id': BLOCK, 'upload_id': UPLOAD,
            'receipt_sha256': u.digest(receipt), 'asset_path': b['asset_path'], 'git_blob': b['git_blob'],
            'sha256': b['sha256'], 'byte_count': b['bytes'], 'mime': 'image/png', 'readback_verified': True,
            'source_commit': COMMIT, 'md_blob': BLOB, 'md_sha256': b['md_sha256'],
            'reference_sha256': b['reference_sha256'], 'evidence_sha256': assets['evidence_sha256'],
            'data_source_id': DEST, 'managed_hash': '6' * 64}

    def queue_and_journal(self):
        value = self.store.value; b = self.plan['binding']
        source = {'path': 'Raw/03 Get/notes/synthetic.md', 'blob': BLOB, 'sha256': b['md_sha256']}
        value.update(origin={'commit': '0' * 40, 'receipt_sha256': None, 'sequence': 0}, policy_sha256='1' * 64,
            last_enqueued_publication={'commit': COMMIT, 'receipt_sha256': '2' * 64, 'sequence': 1},
            observed_publication_tip={'commit': COMMIT, 'receipt_sha256': '2' * 64, 'sequence': 1},
            pending_by_id={IDENT: dict(source, commit=COMMIT, parent='0' * 40, receipt_sha256='2' * 64, sequence=1)},
            rotation_order=[IDENT])
        desc = {'commit': COMMIT, 'initial_cohort': None, 'outbox_receipt_sha256': '2' * 64}
        plan = {'publication': desc, 'records': [{'note_id': IDENT, 'source': source}], 'skipped_conflicts': []}
        value['engine_state']['journal'] = {'plan': plan, 'plan_sha256': o.digest(plan), 'index': 0, 'operation': None}
        o.check_state(value)

    def finish(self, candidates=None):
        provider = None if candidates is None else lambda old, new: candidates
        adapter = o.EngineStore(self.store, attachment_provider=provider)
        inner, sha = adapter.read(); inner['receipts'][IDENT] = self.receipt(); inner['journal'] = None
        adapter.compare_and_set(sha, inner)

    def test_legacy_bytes_unchanged_and_explicit_initialization_is_pure(self):
        old = o.initial_state(); raw = o.encoded(old); o.check_state(old)
        self.assertNotIn('png_assets', old); self.assertEqual(raw, o.encoded(o.initial_state()))
        result = s.initialize(old, self.a, u.digest(self.a)); self.assertEqual(o.encoded(old), raw)
        self.assertEqual({k:v for k,v in result.items() if k != 'png_assets'}, old)
        self.assertEqual(s.initialize(result, self.a, u.digest(self.a)), result)
        with self.assertRaises(s.Blocked): s.initialize(old, self.a, '0' * 64)

    def test_only_three_pins_strict_plan_and_container(self):
        for key, value in [('note_id', '999'), ('reference_sha256', '0'*64), ('bytes', True),
                           ('asset_path', s.PREFIX+'x.png'), ('source_commit', '0'*40)]:
            bad = deepcopy(self.plan); bad['binding'][key] = value
            with self.subTest(key=key), self.assertRaises(Exception): s.add_upload(self.outer, bad, u.digest(bad))
        for key in ('extra',):
            bad = deepcopy(self.outer); bad['png_assets'][key] = None
            with self.assertRaises(o.Blocked): o.check_state(bad)
        bad = deepcopy(self.outer); bad['png_assets']['evidence_sha256'] = '0'*64
        with self.assertRaises(o.Blocked): o.check_state(bad)

    def test_stages_use_same_outer_and_full_readback(self):
        self.uploaded()
        self.assertEqual([v['png_assets']['uploads'][IDENT]['state']['phase'] for v in self.store.calls],
            ['create_in_flight', 'upload_id_saved', 'send_in_flight', 'uploaded_verified'])
        for value in self.store.calls:
            self.assertEqual({k:v for k,v in value.items() if k != 'png_assets'}, o.initial_state())
        receipt = s.receipt_for(self.store.value['png_assets']['uploads'][IDENT])
        self.assertFalse(receipt['native_bytes_verified']); self.assertEqual(receipt['upload_id'], UPLOAD)
        self.assertEqual(receipt['source_binding'], self.plan['binding'])

    def test_default_no_persist_and_bad_boolean(self):
        adapter = s.UploadStore(self.store, IDENT, u.digest(self.plan)); value, sha = adapter.read()
        with self.assertRaisesRegex(s.Blocked, 'persist_disabled'): adapter.compare_and_set(sha, value)
        with self.assertRaises(s.Blocked): self.adapter(1)
        self.assertEqual(self.store.calls, [])

    def test_outer_race_even_when_inner_unchanged(self):
        adapter = self.adapter(); value, sha = adapter.read()
        self.store.value = s.add_upload(self.store.value, plan(SECOND), u.digest(plan(SECOND)))
        with self.assertRaisesRegex(o.Blocked, 'outbox_state_changed'): adapter.compare_and_set(sha, value)
        self.assertEqual(self.store.calls, [])

    def test_bad_ack_or_bad_readback_never_confirms(self):
        for mode in ('ack', 'readback'):
            self.store = Store(self.outer); self.store.bad = mode
            with self.subTest(mode=mode), self.assertRaises(Exception): self.advance('create_in_flight')

    def test_cannot_skip_or_rearm_or_rebind_any_saved_phase(self):
        with self.assertRaises(s.Blocked): self.advance('uploaded_verified', upload_id=UPLOAD, expiry_time=None)
        self.advance('create_in_flight'); self.advance('upload_id_saved', upload_id=UPLOAD, expiry_time='2030-01-01T01:00:00Z')
        for changes in ({'phase':'prepared','upload_id':None,'expiry_time':None,'request_sha256':None},
                        {'upload_id':'9'*32}, {'expiry_time':'2031-01-01T00:00:00Z'}):
            before = deepcopy(self.store.value); after = deepcopy(before)
            after['png_assets']['uploads'][IDENT]['state'].update(changes)
            with self.assertRaises(Exception): s.preserve(before, after)
        self.advance('send_in_flight'); self.advance('uploaded_verified', expiry_time=None)
        before = deepcopy(self.store.value)
        for mutate in (lambda v:v.pop('png_assets'), lambda v:v['png_assets']['uploads'].pop(IDENT),
                       lambda v:v['png_assets']['uploads'][IDENT]['state'].update(expiry_time='2031-01-01T00:00:00Z')):
            after = deepcopy(before); mutate(after)
            with self.assertRaises(Exception): s.preserve(before, after)

    def test_typed_http_evidence_retained_and_never_rearms(self):
        self.advance('create_in_flight')
        adapter = self.adapter(); state, sha = adapter.read()
        state['write_http'] = {'status': 500, 'request_sha256': state['request_sha256']}
        adapter.compare_and_set(sha, state)
        before = deepcopy(self.store.value)
        for value in (None, {'status': 400, 'request_sha256': state['request_sha256']}):
            after = deepcopy(before)
            if value is None: after['png_assets']['uploads'][IDENT]['state'].pop('write_http')
            else: after['png_assets']['uploads'][IDENT]['state']['write_http'] = value
            with self.assertRaises(s.Blocked): s.preserve(before, after)
        with self.assertRaises(Exception): self.advance('upload_id_saved', upload_id=UPLOAD, expiry_time='2030-01-01T01:00:00Z')

    def test_attachment_receipt_dequeue_are_one_cas(self):
        self.uploaded(); self.queue_and_journal(); before = len(self.store.calls); candidate = self.candidate()
        self.finish({IDENT: candidate})
        self.assertEqual(len(self.store.calls), before+1)
        value = self.store.calls[-1]
        self.assertEqual(value['png_assets']['attachments'][IDENT], candidate)
        self.assertEqual(value['engine_state']['receipts'][IDENT], self.receipt())
        self.assertEqual(value['pending_by_id'], {}); self.assertEqual(value['rotation_order'], [])

    def test_no_native_candidate_cannot_finish_or_dequeue(self):
        self.uploaded(); self.queue_and_journal(); before = deepcopy(self.store.value); count = len(self.store.calls)
        with self.assertRaisesRegex(s.Blocked, 'attachment_missing'): self.finish()
        self.assertEqual(self.store.value, before); self.assertEqual(len(self.store.calls), count)

    def test_every_attachment_identity_mismatch_rejected_atomically(self):
        self.uploaded(); self.queue_and_journal(); original = deepcopy(self.store.value)
        for key, value in self.candidate().items():
            bad = self.candidate()
            bad[key] = False if isinstance(value, bool) else value+1 if isinstance(value, int) else '0'*len(value)
            before = len(self.store.calls)
            with self.subTest(key=key), self.assertRaises(Exception): self.finish({IDENT:bad})
            self.assertEqual(self.store.value, original); self.assertEqual(len(self.store.calls), before)

    def test_attachment_cannot_be_added_later_or_deleted_or_rebound(self):
        self.uploaded(); candidate = self.candidate()
        before = deepcopy(self.store.value); before['engine_state']['receipts'][IDENT] = self.receipt()
        after = deepcopy(before); after['png_assets']['attachments'][IDENT] = candidate
        with self.assertRaises(s.Blocked): s.preserve(before, after)
        self.queue_and_journal(); self.finish({IDENT:candidate}); before = deepcopy(self.store.value)
        for key in ('page_id', 'managed_hash', None):
            after = deepcopy(before)
            if key is None: after['png_assets']['attachments'].pop(IDENT)
            else: after['png_assets']['attachments'][IDENT][key] = '9'*len(candidate[key])
            with self.assertRaises(Exception): s.preserve(before, after)

    def test_remote_journal_guard_rejects_deletion_before_build_or_cas(self):
        # Exercise its real CAS implementation with in-memory _current, never Git.
        self.uploaded(); old = deepcopy(self.store.value); bad = deepcopy(old); bad.pop('png_assets')
        journal = object.__new__(remote.RemoteJournal)
        journal._ready = lambda: None
        journal.stage = 'fixture'
        journal._current = lambda **kw: ('a'*40, 'b'*40, {}, old, o.digest(old), 'c'*40, o.encoded(old))
        journal._build = lambda *a: self.fail('must fail before building objects')
        with self.assertRaises(remote.JournalFailure): journal._compare_and_set(o.digest(old), bad)

    def test_hold_and_unrelated_receipts_preserved_by_upload_cas(self):
        # Hold validator remains the existing exact pinned rule; mock only its
        # fixture identity and prove the adapter never removes the stored data.
        hold = {'999': {'opaque': 'retained'}}
        self.store.value['engine_state']['unresolved_by_id'] = hold
        with patch.object(o.e.plan_recovery, 'validate_holds', lambda x:x):
            self.advance('create_in_flight')
            self.assertEqual(self.store.value['engine_state']['unresolved_by_id'], hold)
        self.assertEqual(self.store.value['engine_state']['receipts'], {})

    def test_actual_kernel_uses_adapter_for_every_stage_and_exact_receipt(self):
        data = b'\x89PNG\r\n\x1a\n' + b'0000IHDR' + struct.pack('>II', 1, 1) + b'fixture'
        pin = {'md_sha256': '1'*64, 'reference_sha256': '2'*64,
            'sha256': u.sha(data), 'bytes': len(data), 'git_blob': o.p.git_blob(data)}
        with patch.object(u, 'PINS', {u.sha(b'123'): pin}):
            b = dict(pin, note_id='123', asset_path=s.PREFIX+pin['sha256']+'.png',
                mime='image/png', data_source_id=DEST, source_commit=COMMIT)
            plan = u.prepare(b, u.digest(b), {b['asset_path']:b['git_blob']}, data)
            outer = s.add_upload(s.initialize(o.initial_state(), self.a, u.digest(self.a)), plan, u.digest(plan))
            store = Store(outer); adapter = s.UploadStore(store, '123', u.digest(plan), allow_persist=True)
            calls = []
            def request(req):
                calls.append(req['method'])
                phase = store.value['png_assets']['uploads']['123']['state']['phase']
                if req['method'] == 'POST': self.assertIn(phase, ('create_in_flight','send_in_flight'))
                return 200, {'object':'file_upload','id':UPLOAD,'filename':plan['filename'],
                    'content_type':'image/png','content_length':len(data),
                    'status':'pending' if len(calls)==1 else 'uploaded', 'expiry_time':'2030-01-01T01:00:00Z'}
            engine = u.Upload(plan, u.digest(plan), data, adapter, request, allow_writes=True,
                now=lambda:datetime(2030,1,1,tzinfo=timezone.utc))
            engine.advance(); engine.advance(); result = engine.advance()
            self.assertEqual(calls, ['POST','POST','GET']); self.assertEqual(len(store.calls),4)
            receipts = s.upload_receipts(store.value)
            self.assertEqual(receipts, {'123': {'receipt':result['receipt'],'sha256':u.digest(result['receipt'])}})

    def test_permission_diagnostic_cannot_advance_and_send_error_is_retained(self):
        self.advance('create_in_flight'); self.advance('upload_id_saved', upload_id=UPLOAD, expiry_time='2030-01-01T01:00:00Z')
        self.advance('send_in_flight'); adapter = self.adapter(); state, sha = adapter.read()
        state['write_http'] = {'status': 500, 'request_sha256':state['request_sha256']}
        adapter.compare_and_set(sha, state)
        self.advance('uploaded_verified', expiry_time=None)
        self.assertEqual(self.store.value['png_assets']['uploads'][IDENT]['state']['write_http'], state['write_http'])
        self.store = Store(self.outer); self.advance('create_in_flight')
        adapter = self.adapter(); state, sha = adapter.read()
        state['write_http'] = {'status':403,'request_sha256':state['request_sha256']}
        adapter.compare_and_set(sha,state)
        with self.assertRaises(Exception): self.advance('upload_id_saved',upload_id=UPLOAD,expiry_time='2030-01-01T01:00:00Z')

    def test_provider_exception_and_peer_queue_never_partially_write(self):
        self.uploaded(); self.queue_and_journal()
        peer='999'; pending=deepcopy(self.store.value['pending_by_id'][IDENT]); pending['path']='Raw/03 Get/notes/peer.md'
        self.store.value['pending_by_id'][peer]=pending; self.store.value['rotation_order'].append(peer)
        original=deepcopy(self.store.value)
        def broken(*args): raise ValueError('fixture')
        adapter=o.EngineStore(self.store,attachment_provider=broken); inner,sha=adapter.read()
        inner['receipts'][IDENT]=self.receipt();inner['journal']=None
        with self.assertRaises(ValueError):adapter.compare_and_set(sha,inner)
        self.assertEqual(self.store.value,original)
        self.finish({IDENT:self.candidate()})
        self.assertEqual(self.store.value['pending_by_id'],{peer:pending})
        self.assertEqual(self.store.value['rotation_order'],[peer])

    def test_duplicate_saved_upload_ids_reject(self):
        self.uploaded(); value=s.add_upload(self.store.value,plan(SECOND),u.digest(plan(SECOND)))
        row=value['png_assets']['uploads'][SECOND]
        row['state'].update(phase='upload_id_saved',upload_id=UPLOAD,expiry_time='2030-01-01T01:00:00Z',
            request_sha256=u.fingerprint(u.create_request(row['plan'])))
        with self.assertRaises(o.Blocked):o.check_state(value)

    def test_engine_without_png_still_has_exact_legacy_shape(self):
        store = Store(o.initial_state()); adapter = o.EngineStore(store)
        value, sha = adapter.read(); adapter.compare_and_set(sha, value)
        self.assertEqual(store.value, o.initial_state()); self.assertNotIn('png_assets', store.value)


if __name__ == '__main__': unittest.main()
