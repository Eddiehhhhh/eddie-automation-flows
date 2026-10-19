"""Two exact independent PNG cohorts; synthetic content and injected I/O only."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import get_notion_outbox as o
import get_notion_png_store as s
import get_notion_png_upload as u
import get_notion_uploaded_png as image
import get_notion_png_evidence as evidence
import test_notion_png_store as old
from test_notion_png_store_not_dispatched import Backend
from test_notion_png_evidence import Fixture, png

NEW = '901'
NEW_COMMIT = '9' * 40
KEY = 'png_assets_seq2'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.legacy = old.Tests(methodName='test_legacy_bytes_unchanged_and_explicit_initialization_is_pure')
        self.legacy.setUp(); self.addCleanup(self.legacy.doCleanups)
        # Two historical attached successes, one unknown create, pending/hold.
        self.legacy.uploaded(); self.legacy.queue_and_journal()
        self.legacy.finish({old.IDENT:self.legacy.candidate()})
        self.before = s.add_upload(self.legacy.store.value, old.plan(old.SECOND), u.digest(old.plan(old.SECOND)))
        second = self.before['png_assets']['uploads'][old.SECOND]
        second['state'].update(phase='uploaded_verified',upload_id='5'*32,
            request_sha256=u.send_fingerprint(second['plan'],'5'*32))
        previous=self.before['engine_state']['receipts'][old.IDENT]
        receipt=deepcopy(previous);receipt.update(note_id=old.SECOND,page_id='6'*32,
            managed_blocks=[{'id':'8'*32,'normalized_hash':'9'*64}],
            properties_baseline={'笔记 ID':{'type':'rich_text','rich_text':[o.p.text_run(old.SECOND)]}})
        self.before['engine_state']['receipts'][old.SECOND]=receipt
        binding=second['plan']['binding'];saved=s.receipt_for(second)
        candidate=deepcopy(self.before['png_assets']['attachments'][old.IDENT])
        candidate.update(note_id=old.SECOND,page_id='6'*32,block_id='8'*32,upload_id='5'*32,
            receipt_sha256=u.digest(saved),asset_path=binding['asset_path'],git_blob=binding['git_blob'],
            sha256=binding['sha256'],byte_count=binding['bytes'],md_sha256=binding['md_sha256'],
            reference_sha256=binding['reference_sha256'],managed_hash='9'*64)
        self.before['png_assets']['attachments'][old.SECOND]=candidate
        self.before=s.add_upload(self.before,old.plan('789'),u.digest(old.plan('789')))
        unknown=self.before['png_assets']['uploads']['789']
        unknown['state'].update(phase='create_in_flight',request_sha256=u.fingerprint(u.create_request(unknown['plan'])))
        self.before['pending_by_id']['789']={'commit':old.COMMIT,'parent':'0'*40,'receipt_sha256':'2'*64,
            'sequence':1,'path':'Raw/03 Get/notes/unknown.md','blob':'b'*40,'sha256':unknown['plan']['binding']['md_sha256']}
        self.before['rotation_order']=['789']
        self.hold_patch=patch.object(o.e.plan_recovery,'validate_holds',lambda value:value)
        self.hold_patch.start();self.addCleanup(self.hold_patch.stop)
        self.before['engine_state']['unresolved_by_id']={'999':{'note_id':'999','page_id':'4'*32,'reason':'synthetic_hold'}}
        o.check_state(self.before)
        self.legacy_raw=o.encoded(self.before['png_assets'])
        self.images = {str(i):png(i - 895, 2) for i in (901, 902, 903)}
        self.pins = {u.sha(ident.encode()):{'md_sha256':str(i)*64, 'reference_sha256':str(i+3)*64,
                     'sha256':u.sha(raw), 'bytes':len(raw), 'git_blob':o.p.git_blob(raw)}
                     for i,(ident,raw) in enumerate(self.images.items(),1)}
        self.patch = patch.object(u,'SEQ2_PINS',self.pins);self.patch.start();self.addCleanup(self.patch.stop)
        self.anchor = {**old.anchor(), 'publication_commit':NEW_COMMIT,
                       'manifest_path':u.SEQ2_MANIFEST, 'asset_commit':'8'*40}
        self.plan = self.make_plan(NEW)
        self.value = s.add_upload(s.initialize(self.before,self.anchor,u.digest(self.anchor)),self.plan,u.digest(self.plan))
        self.backend = Backend(self.value)

    def make_plan(self,ident):
        pin = u.SEQ2_PINS[u.sha(ident.encode())]
        binding = {**pin,'note_id':ident,'asset_path':s.PREFIX+pin['sha256']+'.png',
                   'mime':'image/png','data_source_id':old.DEST,'source_commit':NEW_COMMIT}
        tree = {s.PREFIX+p['sha256']+'.png':p['git_blob'] for p in u.SEQ2_PINS.values()}
        return u.prepare(binding,u.digest(binding),tree,self.images[ident])

    def adapter(self):
        return s.UploadStore(self.backend,NEW,u.digest(self.plan),asset_key=KEY,allow_persist=True)

    def forward(self):
        adapter=self.adapter();before,sha=adapter.read();after=deepcopy(before)
        after.update(phase='create_in_flight',request_sha256=u.fingerprint(u.create_request(self.plan)))
        result=adapter.compare_and_set(sha,after)
        return adapter,before,after,result

    def assert_legacy(self,value):
        self.assertEqual(o.encoded(value['png_assets']),self.legacy_raw)
        for ident in (old.IDENT,old.SECOND):
            self.assertEqual(value['engine_state']['receipts'][ident],self.before['engine_state']['receipts'][ident])
        self.assertEqual(value['engine_state']['unresolved_by_id'],self.before['engine_state']['unresolved_by_id'])
        self.assertEqual(value['pending_by_id']['789'],self.before['pending_by_id']['789'])

    def test_explicit_extension_retains_legacy_plan_bytes_and_unknown_slot(self):
        o.check_state(self.value);self.assert_legacy(self.value)
        self.assertEqual({k:v for k,v in self.value.items() if k!=KEY},self.before)
        self.assertEqual(len(u.PINS),3);self.assertEqual(len(u.SEQ2_PINS),3)
        for row in self.value['png_assets']['uploads'].values():
            s.check_plan(row['plan'],row['plan_sha256'],self.value['png_assets']['evidence_anchor'])
        self.assertEqual(s.upload_receipts(self.value).keys(),{old.IDENT,old.SECOND})
        self.assertEqual(s.upload_receipts(self.value,asset_key=KEY),{})

    def test_full_new_upload_and_scoped_receipts_leave_legacy_unchanged(self):
        calls=[]
        def request(req):
            calls.append(req['method'])
            return 200,{'object':'file_upload','id':'7'*32,'status':'pending' if len(calls)==1 else 'uploaded',
                'filename':self.plan['filename'],'content_type':'image/png','content_length':len(self.images[NEW]),
                'expiry_time':'2030-01-01T01:00:00Z'}
        kernel=u.Upload(self.plan,u.digest(self.plan),self.images[NEW],self.adapter(),request,
                        allow_writes=True,now=lambda:datetime(2030,1,1,tzinfo=timezone.utc))
        kernel.advance();kernel.advance();kernel.advance()
        self.assertEqual(calls,['POST','POST','GET']);self.assert_legacy(self.backend.value)
        self.assertEqual(set(s.upload_receipts(self.backend.value,asset_key=KEY)),{NEW})
        self.assertEqual(set(s.upload_receipts(self.backend.value)),{old.IDENT,old.SECOND})

    def test_new_attachment_receipt_dequeue_is_one_cas_and_preserves_old(self):
        row=self.backend.value[KEY]['uploads'][NEW]
        row['state'].update(phase='uploaded_verified',upload_id='7'*32,
            request_sha256=u.send_fingerprint(row['plan'],'7'*32))
        binding=row['plan']['binding'];saved=s.receipt_for(row)
        value=self.backend.value;tip={'commit':NEW_COMMIT,'receipt_sha256':'a'*64,'sequence':2}
        value['last_enqueued_publication']=deepcopy(tip);value['observed_publication_tip']=deepcopy(tip)
        source={'path':'Raw/03 Get/notes/new.md','blob':'a'*40,'sha256':binding['md_sha256']}
        value['pending_by_id'][NEW]={**source,**tip,'parent':old.COMMIT}
        value['rotation_order'].append(NEW)
        plan={'publication':{'commit':NEW_COMMIT,'initial_cohort':None,'outbox_receipt_sha256':'a'*64},
              'records':[{'note_id':NEW,'source':source}],'skipped_conflicts':[]}
        value['engine_state']['journal']={'plan':plan,'plan_sha256':o.digest(plan),'index':0,'operation':None}
        receipt=deepcopy(value['engine_state']['receipts'][old.IDENT])
        receipt.update(note_id=NEW,page_id='9'*32,source_commit=NEW_COMMIT,md_blob=source['blob'],
            managed_blocks=[{'id':'a'*32,'normalized_hash':'b'*64}],
            properties_baseline={'笔记 ID':{'type':'rich_text','rich_text':[o.p.text_run(NEW)]}})
        candidate=deepcopy(value['png_assets']['attachments'][old.IDENT])
        candidate.update(note_id=NEW,page_id='9'*32,block_id='a'*32,upload_id='7'*32,
            receipt_sha256=u.digest(saved),asset_path=binding['asset_path'],git_blob=binding['git_blob'],
            sha256=binding['sha256'],byte_count=binding['bytes'],md_blob=source['blob'],md_sha256=binding['md_sha256'],
            source_commit=NEW_COMMIT,reference_sha256=binding['reference_sha256'],
            evidence_sha256=value[KEY]['evidence_sha256'],managed_hash='b'*64)
        o.check_state(value)
        adapter=o.EngineStore(self.backend,attachment_provider=lambda old,new:{NEW:candidate})
        inner,sha=adapter.read();inner['receipts'][NEW]=receipt;inner['journal']=None
        count=len(self.backend.calls);adapter.compare_and_set(sha,inner)
        self.assertEqual(len(self.backend.calls),count+1);self.assert_legacy(self.backend.value)
        self.assertEqual(self.backend.value[KEY]['attachments'][NEW],candidate)
        self.assertEqual(self.backend.value['engine_state']['receipts'][NEW],receipt)
        self.assertNotIn(NEW,self.backend.value['pending_by_id'])
        self.assertEqual(self.backend.value['rotation_order'],['789'])

    def test_new_typed_reset_is_exact_scoped_and_single_use(self):
        adapter,before,after,sha=self.forward()
        self.assertEqual(adapter._not_dispatched['asset_key'],KEY)
        self.assertEqual(self.backend._png_not_dispatched['asset_key'],KEY)
        proof=u.NotDispatched(after['request_sha256'])
        adapter.reset_not_dispatched(sha,before,proof)
        self.assertEqual(self.backend.value,self.value);self.assert_legacy(self.backend.value)
        with self.assertRaises(s.Blocked):adapter.reset_not_dispatched(sha,before,proof)

    def test_wrong_scope_cannot_reset_unknown_upload(self):
        adapter,before,after,sha=self.forward()
        adapter._not_dispatched['asset_key']='png_assets'
        with self.assertRaises(Exception):adapter.reset_not_dispatched(sha,before,u.NotDispatched(after['request_sha256']))
        self.assertEqual(self.backend.value[KEY]['uploads'][NEW]['state']['phase'],'create_in_flight')
        self.assert_legacy(self.backend.value)
        fresh=self.adapter()
        with self.assertRaisesRegex(u.Blocked,'create_uncertain_no_retry'):
            u.Upload(self.plan,u.digest(self.plan),self.images[NEW],fresh,lambda _:self.fail('no write'),allow_writes=True).advance()

    def test_wrong_manifest_cohort_tree_or_publication_rejects(self):
        for field,value in (('source_commit',old.COMMIT),('note_id',old.IDENT)):
            wrong=deepcopy(self.plan);wrong['binding'][field]=value
            with self.subTest(field=field),self.assertRaises(Exception):s.add_upload(self.value,wrong,u.digest(wrong),asset_key=KEY)
        wrong=deepcopy(self.plan)
        wrong['asset_tree_sha256']=u.digest({s.PREFIX+p['sha256']+'.png':p['git_blob'] for p in {**u.PINS,**u.SEQ2_PINS}.values()})
        with self.assertRaises(s.Blocked):s.check_plan(wrong,u.digest(wrong),self.anchor)
        swapped=deepcopy(self.value);swapped['png_assets'],swapped[KEY]=swapped[KEY],swapped['png_assets']
        with self.assertRaises(o.Blocked):o.check_state(swapped)

    def test_null_or_third_container_and_deleting_either_cohort_reject(self):
        for key in s.ASSET_KEYS:
            wrong=deepcopy(self.value);wrong[key]=None
            with self.assertRaises(o.Blocked):o.check_state(wrong)
            wrong=deepcopy(self.value);del wrong[key]
            with self.assertRaises(Exception):s.preserve(self.value,wrong)
        wrong=deepcopy(self.value);wrong['png_assets_seq3']=deepcopy(wrong[KEY])
        with self.assertRaises(o.Blocked):o.check_state(wrong)

    def test_duplicate_handles_across_cohorts_reject(self):
        wrong=deepcopy(self.value);row=wrong[KEY]['uploads'][NEW]
        row['state'].update(phase='upload_id_saved',upload_id=old.UPLOAD,
            expiry_time='2030-01-01T01:00:00Z',request_sha256=u.fingerprint(u.create_request(row['plan'])))
        with self.assertRaises(o.Blocked):o.check_state(wrong)

    def test_extension_cannot_use_legacy_expiry_recovery(self):
        wrong=deepcopy(self.value);wrong[KEY]['expired_recovery']={}
        with self.assertRaises(o.Blocked):o.check_state(wrong)

    def test_new_initialization_cannot_rearm_legacy_unknown(self):
        wrong=deepcopy(self.value);row=wrong['png_assets']['uploads']['789']
        row['state']=u.initial_state(row['plan'])
        with self.assertRaises(s.Blocked):s.preserve(self.before,wrong)

    def test_exact_oversize_pin_only(self):
        data=b'\x89PNG\r\n\x1a\n0000IHDR'+b'x'*(513575-16)
        pin=deepcopy(u.SEQ2_PINS[u.sha(NEW.encode())]);pin.update(bytes=len(data),sha256=u.sha(data),git_blob=o.p.git_blob(data))
        pins=deepcopy(u.SEQ2_PINS);pins[u.sha(NEW.encode())]=pin
        with patch.object(u,'SEQ2_PINS',pins):
            self.images[NEW]=data;plan=self.make_plan(NEW)
            s.check_plan(plan,u.digest(plan),self.anchor)
            self.assertTrue(u.reviewed_oversize(data));self.assertFalse(u.reviewed_oversize(data[:-1]+b'y'))
            self.assertTrue(u.valid_semantic_size(u.sha(data),len(data)))
            self.assertFalse(u.valid_semantic_size('0'*64,len(data)))
            self.assertFalse(u.valid_semantic_size(u.sha(data),len(data)-1))
            class Response:
                status=200
                def __init__(self,raw):self.raw=raw
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self,cap):return self.raw[:cap]
            class Opener:
                raw=data
                def open(self,*args,**kwargs):return Response(self.raw)
            opener=Opener();fetch=image.AssetGet(opener=opener,clock=lambda:0)
            url='https://prod-files-secure.s3.us-west-2.amazonaws.com/synthetic?signature=fixture'
            self.assertEqual(fetch(url),data)
            opener.raw=data[:-1]+b'y'
            with self.assertRaisesRegex(image.Blocked,'asset_download_budget'):fetch(url)
            bad=deepcopy(plan);bad['binding']['bytes']+=1
            with self.assertRaises(s.Blocked):s.check_plan(bad,u.digest(bad),self.anchor)
        self.assertEqual(u.byte_limit(u.sha(old.IDENT.encode())),512000)


class EvidenceTests(unittest.TestCase):
    def test_new_append_only_manifest_leaves_old_paths_unchanged(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        f=Fixture(Path(tmp.name)/'git');legacy=f.asset_commit
        # A second synthetic publication uses new identities and new PNG bytes.
        old_manifest=f.run('show',legacy+':'+evidence.MANIFEST_PATH)
        f.data={};f.urls={};f.images={};f.records={}
        for i in range(3):
            ident=str(900+i);url='https://get-notes.umiwi.com/new-synthetic-'+str(i)+'.png'
            data=('---\nnote_id: "'+ident+'"\ntitle: "Synthetic"\n---\n![Image]('+url+')\n').encode()
            path='Raw/03 Get/notes/'+ident+'.md';f.write(path,data)
            f.data[ident]=data;f.urls[ident]=url;f.images[ident]=png(20+i,2)
            f.records[ident]={'path':path,'blob':evidence.p.git_blob(data),'sha256':evidence.digest(data)}
        parent=legacy;f.publication=f.commit();f.parent=f.publication
        f.descriptor={'repository':evidence.p.REPOSITORY,'commit':f.publication,'parent':parent,
            'changed_content_ids':sorted(f.records),'records':deepcopy(f.records),
            'content_tree':{v['path']:v['blob'] for v in f.records.values()}}
        f.manifest={'contract':evidence.CONTRACT,'repository':evidence.p.REPOSITORY,'publication_commit':f.publication,'records':{}}
        for ident,src in f.records.items():
            raw=f.images[ident];w,h=evidence.png_shape(raw);sha=evidence.digest(raw)
            f.manifest['records'][ident]={'source':src,'reference_sha256':evidence.digest(f.urls[ident].encode()),
                'asset':{'path':evidence.PREFIX+sha+'.png','blob':evidence.p.git_blob(raw),'sha256':sha,
                         'bytes':len(raw),'mime':'image/png','width':w,'height':h}}
        with patch.object(evidence,'MANIFEST_PATH',evidence.SEQ2_MANIFEST_PATH):f.publish()
        ev=f.evidence();self.assertEqual(ev.descriptor['manifest_path'],evidence.SEQ2_MANIFEST_PATH)
        self.assertEqual(f.run('show','HEAD:'+u.LEGACY_MANIFEST),old_manifest)
        for ident in f.records:
            bound=ev.resolve(ident,f.data[ident],f.urls[ident],current_commit=f.asset_commit)
            self.assertEqual(ev.read_asset(bound,current_commit=f.asset_commit),f.images[ident])
        wrong=deepcopy(f.anchor);wrong['manifest_path']=u.LEGACY_MANIFEST
        with self.assertRaises(evidence.Blocked):f.evidence(anchor=wrong,expected=evidence.digest(wrong))

    def test_second_manifest_hydrates_exactly_seven_blobs(self):
        import test_notion_png_loader as loader_fixture
        fixture=loader_fixture.Tests(methodName='test_real_partial_clone_only_seven_blobs_and_no_ref_or_index_changes')
        with patch.object(evidence,'MANIFEST_PATH',evidence.SEQ2_MANIFEST_PATH):fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.test_real_partial_clone_only_seven_blobs_and_no_ref_or_index_changes()
        self.assertEqual(fixture.f.anchor['manifest_path'],evidence.SEQ2_MANIFEST_PATH)

    def test_long_preflight_splits_all_roots_without_real_upload_handle(self):
        rows=[o.p.block('paragraph',{'rich_text':[o.p.text_run('Synthetic paragraph')]}) for _ in range(268)]
        rows[3]=o.p.block('image',{'kind':'notion_png_preflight','sha256':'a'*64,'byte_count':100,'mime':'image/png','caption':[]})
        props={'笔记 ID':{'type':'rich_text','rich_text':[o.p.text_run('901')]}}
        original=deepcopy(rows)
        report=image.preflight_blocks(rows,properties=props,data_source=old.DEST)
        self.assertEqual((report['root_blocks'],report['total_blocks'],report['write_ready']),(268,268,False))
        self.assertEqual(rows,original)
        with self.assertRaisesRegex(ValueError,'request_budget'):image.preflight_blocks(rows)


if __name__=='__main__':unittest.main()
