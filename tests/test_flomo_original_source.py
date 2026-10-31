"""Original-source reader fixtures are synthetic; no network or real account data."""
import copy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import flomo_original_source as source
import sync_notion_flomo_to_raw as bridge


def memo(mid='YWJj', content='<p>Original source</p>', files=None, updated='2026-01-01 12:34:56'):
    return {'slug': mid, 'content': content, 'created_at': '2025-01-02 01:02:03',
            'updated_at': updated, 'deleted_at': None, 'files': [] if files is None else files,
            'tags': ['sample/tag']}


class OriginalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / bridge.STAGE
        self.notions = {'version': 1, 'pages': {'YWJj': {'notion_page_id': 'page-one',
                        'last_edited_time': 'unchanged', 'files': {}, 'versions': {}}}}
        path = self.root / bridge.STATE; path.parent.mkdir(parents=True)
        path.write_bytes(bridge.encoded(self.notions)); self.original_notion = path.read_bytes()

    def enrich(self, values=None, **kwargs):
        return source.enrich(self.root, bridge, token='synthetic-access', signing_suffix='test-suffix',
            max_pages=0, downloader=lambda url: b'\x89PNGsynthetic',
            reader=lambda *args: values if values is not None else [memo()], **kwargs)

    def state(self): return json.loads((self.base / source.STATE).read_bytes())
    def read_source(self, mid='YWJj', page='page-one'):
        record = self.state()['pages'][mid]['source_pages'][page]
        rel = next(rel for rel in record['files'] if rel.endswith('/original.json'))
        return json.loads((self.base / rel).read_bytes()), record

    def test_original_json_seconds_html_and_notion_unchanged(self):
        original = memo(files=[{'url': 'https://assets.invalid/photo', 'type': 'image'}])
        result = self.enrich([original]); captured, record = self.read_source()
        self.assertTrue(result['complete']); self.assertEqual(captured, original)
        self.assertEqual(captured['created_at'], '2025-01-02 01:02:03')
        self.assertTrue(any('/assets/' in p for p in record['files']))
        self.assertEqual((self.root / bridge.STATE).read_bytes(), self.original_notion)
        self.assertFalse((self.root / 'Raw/01 Flomo').exists())
        self.assertEqual(self.enrich([original])['unchanged'], 1)

    def test_variants_and_1550_legacy_receipts_unchanged(self):
        self.notions['pages'] = {'legacy-' + str(i): {'notion_page_id': 'old-' + str(i),
            'current_version': 'old', 'files': {}, 'versions': {}} for i in range(1550)}
        self.notions['pages']['YWJj'] = {'notion_page_id': 'page-one',
            'source_pages': {'page-one': {'files': {}}, 'page-two': {'files': {}}}, 'files': {}}
        path = self.root / bridge.STATE; path.write_bytes(bridge.encoded(self.notions)); before = path.read_bytes()
        self.assertEqual(self.enrich()['written'], 2)
        self.assertEqual(set(self.state()['pages']['YWJj']['source_pages']), {'page-one', 'page-two'})
        self.assertEqual(path.read_bytes(), before)

    def test_original_only_and_disappearance_are_retained(self):
        self.enrich([memo('b3JwaGFu')]); before = self.state()
        self.assertIn('upstream-only', before['pages']['b3JwaGFu']['source_pages'])
        result = self.enrich([])
        self.assertFalse(result['capture_complete'])
        self.assertEqual(self.state(), before)
        self.assertEqual(result['retained_prior_not_observed'], ['b3JwaGFu'])

    def test_not_observed_original_is_explicit_hold_not_deletion_or_success(self):
        self.enrich()
        original=(self.base/source.STATE).read_bytes()
        result=self.enrich([])
        self.assertTrue(result['inventory_complete'])
        self.assertEqual(result['notion_without_original'],['YWJj'])
        self.assertEqual(result['held']['YWJj::original-source'],'original_memo_not_observed')
        self.assertFalse(result['capture_complete']);self.assertFalse(result['complete'])
        self.assertEqual((self.base/source.STATE).read_bytes(),original)
        self.assertEqual((self.root/bridge.STATE).read_bytes(),self.original_notion)

    def test_eleven_upstream_only_memos_survive_later_empty_inventory(self):
        values=[memo('orphan'+str(i)) for i in range(11)]
        result=self.enrich(values)
        self.assertEqual(result['written'],11)
        before=(self.base / source.STATE).read_bytes()
        result=self.enrich([])
        self.assertEqual(len(result['retained_prior_not_observed']),11)
        self.assertEqual((self.base / source.STATE).read_bytes(),before)
        self.assertEqual(len(list((self.base/source.AREA).rglob('original.json'))),11)

    def test_new_edit_is_new_immutable_version_and_manual_edit_holds(self):
        self.enrich(); _, first = self.read_source()
        self.enrich([memo(content='<p>Changed</p>')]); _, second = self.read_source()
        self.assertEqual(len(second['versions']), 2)
        for path, sha in first['files'].items():
            self.assertEqual(bridge.digest((self.base / path).read_bytes()), sha)
        edited = self.base / next(iter(first['files'])); edited.write_bytes(b'user edit')
        before = (self.base / source.STATE).read_bytes()
        result = self.enrich([memo(content='<p>Again</p>')])
        self.assertEqual(result['held']['YWJj::retained_history'], 'stage_edited')
        self.assertEqual((self.base / source.STATE).read_bytes(), before)
        self.assertEqual(edited.read_bytes(), b'user edit')

    def test_credential_gate_does_not_drop_other_object(self):
        bad = memo(content='<p>' + 'ghp_' + 'A' * 36 + '</p>')
        result = self.enrich([bad, memo('b2s')])
        self.assertEqual(result['held']['YWJj::page-one'], 'potential_credential_in_source')
        self.assertNotIn('YWJj', self.state()['pages'])
        self.assertFalse((self.base / source.AREA / 'versions/YWJj').exists())
        self.assertEqual(result['written'], 1)
        self.assertNotIn('ghp_', (self.base / source.COVERAGE).read_text())

    def test_missing_access_or_signing_configuration_holds_no_objects(self):
        for token, suffix, expected in [(None, 's', 'original_missing_access_credential'),
                                        ('t', None, 'original_missing_signing_configuration')]:
            result = source.enrich(self.root, bridge, token=token, signing_suffix=suffix,
                max_pages=0, downloader=mock.Mock(), reader=mock.Mock())
            self.assertEqual(result['held']['source'], expected)
        self.assertFalse((self.base / source.STATE).exists())
        self.assertFalse((self.base / source.AREA / 'versions').exists())

    def test_403_is_saved_as_degraded_not_image_success_then_retry(self):
        value = memo(content='<p>https://assets.invalid/external.jpg</p>')
        fail = mock.Mock(side_effect=urllib.error.HTTPError('https://assets.invalid',403,'denied',{},None))
        result = source.enrich(self.root, bridge, token='t', signing_suffix='s', max_pages=0,
            downloader=fail, reader=lambda *args: [value])
        self.assertTrue(result['source_complete']); self.assertFalse(result['media_complete'])
        self.assertEqual(result['degraded']['YWJj::page-one'], ['asset_http_403'])
        _, first = self.read_source(); self.assertFalse(any('/assets/' in p for p in first['files']))
        self.assertTrue(self.enrich([value])['complete'])
        _, second = self.read_source(); self.assertEqual(len(second['versions']), 2)

    def test_image_src_and_body_links_are_deduplicated_and_all_files_preserved(self):
        value = memo(content='<img src="https://assets.invalid/a.jpg?x=1&amp;y=2"> '
            'https://assets.invalid/a.jpg?x=1&amp;y=2',
            files=[{'url':'https://assets.invalid/document.pdf','type':'file'}])
        self.assertEqual(source.image_urls(value), ['https://assets.invalid/a.jpg?x=1&y=2',
                         'https://assets.invalid/document.pdf'])

    def test_progress_limit_persists_each_success_and_resumes(self):
        values = [memo(), memo('b2s')]
        result = source.enrich(self.root, bridge, token='t', signing_suffix='s', max_pages=1,
            downloader=mock.Mock(), reader=lambda *args: values)
        self.assertEqual((result['written'], result['pending']), (1,1))
        self.assertEqual(self.enrich(values)['written'], 1)

    def test_bad_pagination_no_objects_or_watermark(self):
        with mock.patch.object(source,'LIMIT',1):
            result = self.enrich([memo()])
        self.assertEqual(result['held']['source'], 'original_duplicate_identity')
        self.assertFalse((self.base / source.STATE).exists())

    def test_complete_pagination_has_terminal_proof(self):
        calls = []
        def reader(token, suffix, updated, slug):
            calls.append((updated,slug)); return [memo()] if not updated else []
        with mock.patch.object(source,'LIMIT',1):
            rows, pages = source.inventory('t','s',reader)
        self.assertEqual(len(rows),1); self.assertEqual(len(pages),2)
        self.assertTrue(pages[-1]['terminal']); self.assertFalse(pages[0]['terminal'])
        self.assertEqual(calls[1], (source.timestamp(memo()['updated_at']), 'YWJj'))

    def test_state_concurrent_change_does_not_advance_progress(self):
        def download(url):
            path = self.base / source.STATE; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'{"version":1,"pages":{},"other_writer":true}'); return b'bytes'
        result = source.enrich(self.root, bridge, token='t',signing_suffix='s',max_pages=0,
            downloader=download,reader=lambda *args:[memo(files=[{'url':'https://assets.invalid/a'}])])
        self.assertEqual(result['held']['YWJj::page-one'], 'state_changed')
        self.assertTrue(self.state()['other_writer'])

    def test_existing_private_gate_runs_before_original_writes(self):
        with mock.patch.object(bridge.private_checkout,'verify_private_checkout',side_effect=ValueError('private proof failed')):
            with self.assertRaises(ValueError):
                bridge.run(self.root,'db','key',apply=True,original_source_enabled=True)
        self.assertFalse(self.base.exists())

    def test_disabled_and_dry_run_never_read_original(self):
        with mock.patch.object(bridge,'stage',return_value={'complete':True}), \
             mock.patch.object(bridge.private_checkout,'verify_private_checkout'), \
             mock.patch.object(bridge,'writer_lock',return_value=nullcontext()), \
             mock.patch.object(source,'enrich') as enrich:
            bridge.run(self.root,'db','key',apply=True)
            bridge.run(self.root,'db','key',apply=False,original_source_enabled=True)
            enrich.assert_not_called()

    def test_original_missing_access_keeps_successful_notion_progress(self):
        with mock.patch.object(bridge,'stage',return_value={'complete':True,'capture_complete':True}), \
             mock.patch.object(bridge.private_checkout,'verify_private_checkout'), \
             mock.patch.object(bridge,'writer_lock',return_value=nullcontext()), \
             mock.patch.dict(os.environ,{},clear=True):
            result=bridge.run(self.root,'db','key',apply=True,original_source_enabled=True)
        self.assertTrue(result['capture_complete']); self.assertFalse(result['complete'])
        self.assertEqual((self.root / bridge.STATE).read_bytes(),self.original_notion)

    def test_remote_readback_requires_original_state_json_html_and_asset_blobs(self):
        from types import SimpleNamespace
        self.enrich([memo(files=[{'url':'https://assets.invalid/a'}])])
        self.enrich([memo(content='<p>changed</p>',files=[{'url':'https://assets.invalid/b'}])])
        raw=(self.root / bridge.STATE).read_bytes()
        expected=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        all_paths=[p for p in self.base.rglob('*') if p.is_file()]
        required=[p for p in all_paths if p.name in ('state.json','original.json','original.html') or p.parent.name=='assets']
        for omitted in [None,*required]:
            def git(args,**kwargs):
                if 'ls-tree' not in args:return SimpleNamespace(returncode=0,stdout=expected)
                rows=[]
                for path in all_paths:
                    if path==omitted:continue
                    data=path.read_bytes(); blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                    rows.append('100644 blob '+blob+'\t'+str(path.relative_to(self.root))+'\0')
                return SimpleNamespace(returncode=0,stdout=''.join(rows))
            with self.subTest(omitted=omitted),mock.patch.object(bridge.private_checkout,'verify_private_checkout'), \
                 mock.patch.object(bridge.subprocess,'run',side_effect=git):
                if omitted is None:self.assertTrue(bridge.verify_remote_state(self.root))
                else:
                    with self.assertRaises(ValueError):bridge.verify_remote_state(self.root)
        (self.root / bridge.STATE).unlink()
        omitted=None
        with mock.patch.object(bridge.private_checkout,'verify_private_checkout'), \
             mock.patch.object(bridge.subprocess,'run',side_effect=git):
            self.assertTrue(bridge.verify_remote_state(self.root))

    def test_state_and_object_failures_do_not_pollute_next_unit(self):
        real=bridge.atomic_write
        failed=False
        def write(path,raw):
            nonlocal failed
            if path==self.base / source.STATE and not failed:
                failed=True; raise OSError('synthetic state failure')
            return real(path,raw)
        with mock.patch.object(bridge,'atomic_write',side_effect=write):
            result=self.enrich([memo(),memo('b2s')])
        self.assertEqual(result['written'],1)
        self.assertEqual(set(self.state()['pages']),{'b2s'})
        self.assertEqual(self.enrich([memo(),memo('b2s')])['written'],1)

    def test_partial_source_inventory_does_not_claim_source_capture_complete(self):
        result=source.enrich(self.root,bridge,token='t',signing_suffix='s',max_pages=1,
            downloader=mock.Mock(),reader=lambda *args:[memo(),memo('b2s')])
        self.assertTrue(result['inventory_complete'])
        self.assertFalse(result['source_complete']);self.assertFalse(result['media_complete'])

    def test_request_is_get_no_redirect_and_no_refresh(self):
        class Response:
            status=200; headers={}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"code":0,"data":[]}'
        opener=mock.Mock(); opener.open.return_value=Response()
        with mock.patch.object(source.urllib.request,'build_opener',return_value=opener) as builder:
            source.request_page('synthetic-access','test-suffix',0,'')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_method(),'GET')
        self.assertTrue(request.full_url.startswith('https://flomoapp.com/api/v1/memo/updated/?'))
        self.assertNotIn('synthetic-access',request.full_url)
        self.assertIsNone(builder.call_args.args[0].redirect_request(None,None,None,None,None))
        params=__import__('urllib.parse',fromlist=['parse_qs']).parse_qs(request.full_url.split('?',1)[1])
        signature=params.pop('sign')[0]
        canonical='&'.join(f'{k}={params[k][0]}' for k in sorted(params))
        self.assertEqual(signature,hashlib.md5((canonical+'test-suffix').encode()).hexdigest())

    def test_downloader_never_receives_account_token(self):
        downloader=mock.Mock(return_value=b'image')
        source.enrich(self.root,bridge,token='synthetic-access',signing_suffix='s',max_pages=0,
            downloader=downloader,reader=lambda *args:[memo(files=[{'url':'https://assets.invalid/a'}])])
        downloader.assert_called_once_with('https://assets.invalid/a')


if __name__=='__main__':unittest.main()
