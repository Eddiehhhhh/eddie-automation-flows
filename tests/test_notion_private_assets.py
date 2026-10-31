"""Synthetic Git/Notion fixtures only; tests never contact a service."""
import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import notion_private_assets as assets

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2r8sAAAAASUVORK5CYII=')
UPLOAD='11111111-1111-4111-8111-111111111111'
URL='https://s3.us-west-2.amazonaws.com/secure.notion-static.com/synthetic/file?signature=synthetic'


class PrivateAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        repository=mock.patch.object(assets.private_checkout,'REPOSITORY','example/private-fixture')
        repository.start();self.addCleanup(repository.stop)
        self.git('init','-q');self.git('config','user.name','fixture');self.git('config','user.email','fixture@example.invalid')
        self.git('remote','add','origin','https://github.com/example/private-fixture.git')
        path=self.root/'assets/source.png';path.parent.mkdir();path.write_bytes(PNG)
        (self.root/'assets/document.pdf').write_bytes(b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n')
        (self.root/'assets/plain.txt').write_text('Synthetic text\n')
        self.git('add','assets/source.png','assets/document.pdf','assets/plain.txt');self.git('commit','-qm','fixture')
        self.commit=self.git('rev-parse','HEAD').strip()
        self.asset=assets.prepare(self.root,self.commit,'assets/source.png')
        self.events=[];self.calls=[]

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.root),*args],capture_output=True,text=True,check=True).stdout

    def uploaded(self,**updates):
        result={'object':'file_upload','id':UPLOAD,'status':'uploaded','content_length':len(self.asset.data),
                'filename':self.asset.filename,'content_type':self.asset.content_type,'expiry_time':None}
        result.update(updates);return result

    def request(self,key,method,path,body=None,content_type=None):
        self.calls.append((method,path,body,content_type))
        if path=='/users/me':return {'type':'bot','bot':{'workspace_limits':{'max_file_upload_size_in_bytes':5*1024*1024}}}
        if method=='POST' and path=='/file_uploads':return {'object':'file_upload','id':UPLOAD,'status':'pending'}
        return self.uploaded()

    def publish(self,prior=None,request=None,callback=None):
        with mock.patch.object(assets,'_request',side_effect=request or self.request):
            return assets.publish(self.asset,'synthetic-key',callback or self.events.append,prior)

    def test_pin_reads_git_bytes_not_dirty_worktree_and_detects_real_mime(self):
        (self.root/'assets/source.png').write_bytes(b'changed working copy')
        value=assets.prepare(self.root,self.commit,'assets/source.png')
        self.assertEqual(value.data,PNG);self.assertEqual(value.content_type,'image/png')
        self.assertEqual(value.block_type,'image');self.assertNotIn('source.png',value.filename)
        self.assertNotIn('source.png',repr(value))
        self.assertEqual(value.sha256,hashlib.sha256(PNG).hexdigest())

    def test_pdf_and_regular_file_types_from_content(self):
        self.assertEqual(assets.prepare(self.root,self.commit,'assets/document.pdf').block_type,'pdf')
        self.assertEqual(assets.prepare(self.root,self.commit,'assets/plain.txt').block_type,'file')

    def test_dynamic_untracked_outside_and_symlink_sources_are_rejected(self):
        (self.root/'untracked.png').write_bytes(PNG)
        for path in ('untracked.png','../outside.png','/outside.png','assets/../source.png','assets//source.png'):
            with self.subTest(path=path),self.assertRaises(assets.AssetHeld):assets.prepare(self.root,self.commit,path)
        (self.root/'assets/source.png').unlink();(self.root/'assets/source.png').symlink_to('/tmp/not-a-fixture')
        with self.assertRaises(assets.AssetHeld):assets.prepare(self.root,self.commit,'assets/source.png')
        with self.assertRaises(assets.AssetHeld):assets.prepare(self.root,'HEAD','assets/plain.txt')

    def test_committed_symlink_is_not_a_regular_blob(self):
        (self.root/'link').symlink_to('assets/plain.txt');self.git('add','link');self.git('commit','-qm','link fixture')
        with self.assertRaises(assets.AssetHeld):assets.prepare(self.root,self.git('rev-parse','HEAD').strip(),'link')

    def test_read_budget_lfs_and_credential_sources_hold_without_upload(self):
        with mock.patch.object(assets,'SOURCE_READ_BYTES',1):
            with self.assertRaises(assets.AssetHeld) as error:assets.prepare(self.root,self.commit,'assets/source.png')
            self.assertEqual(error.exception.code,'source_read_budget')
        for name,data,expected in [('pointer',b'version https://git-lfs.github.com/spec/v1\n','source_lfs_payload_unavailable'),
                                  ('credential',('ghp_'+'A'*36).encode(),'potential_credential_in_asset')]:
            (self.root/name).write_bytes(data);self.git('add',name);self.git('commit','-qm','synthetic fixture')
            commit=self.git('rev-parse','HEAD').strip()
            with self.assertRaises(assets.AssetHeld) as error:assets.prepare(self.root,commit,name)
            self.assertEqual(error.exception.code,expected)

    def test_same_content_multiple_private_paths_reuse_upload_aliases(self):
        result=self.publish()
        other=replace(self.asset,path='assets/duplicate.png')
        with mock.patch.object(assets,'_request',side_effect=self.request):
            result=assets.publish(other,'synthetic-key',self.events.append,result['receipt'])
        self.assertEqual(set(result['receipt']['sources'][self.commit]),{'assets/source.png','assets/duplicate.png'})

    def test_private_fallback_is_clickable_commit_permalink_without_credentials(self):
        self.assertEqual(self.asset.fallback_url,'https://github.com/example/private-fixture/blob/'+self.commit+'/assets/source.png')
        self.assertNotIn('raw.githubusercontent',self.asset.fallback_url)

    def test_private_fallback_handles_only_the_guard_approved_ssh_alias(self):
        self.git('remote','set-url','origin','ssh://git@github-eddie-llm-wiki/example/private-fixture.git')
        self.assertTrue(assets.private_git_link(self.root,self.commit,'assets/source.png').startswith('https://github.com/example/private-fixture/blob/'))
        self.git('remote','set-url','origin','ssh://git@unknown-host/example/private-fixture.git')
        self.assertIsNone(assets.private_git_link(self.root,self.commit,'assets/source.png'))

    def test_success_has_pre_and_post_checkpoints_and_native_block(self):
        result=self.publish()
        self.assertEqual(result['status'],'ready')
        self.assertEqual([e['phase'] for e in self.events],['prepared','created','dispatched','sent','verified'])
        self.assertEqual(result['block']['image']['file_upload']['id'],UPLOAD)
        self.assertEqual([c[0] for c in self.calls],['GET','POST','POST','GET'])
        self.assertIn(PNG,self.calls[2][2]);self.assertIn('multipart/form-data',self.calls[2][3])

    def test_readback_metadata_mismatch_is_held(self):
        def request(*args):
            result=self.request(*args)
            return self.uploaded(content_length=1) if args[1]=='GET' and args[2].startswith('/file_uploads/') else result
        result=self.publish(request=request)
        self.assertEqual(result['reason'],'asset_upload_metadata_mismatch');self.assertIsNone(result['block'])

    def test_ready_prior_is_verified_reused_without_new_upload(self):
        prior=self.publish()['receipt'];self.calls.clear();self.events.clear()
        result=self.publish(prior)
        self.assertEqual(result['status'],'ready');self.assertEqual([c[0] for c in self.calls],['GET'])
        self.assertEqual(len(self.events),1)

    def test_prior_binding_mismatch_never_calls_api(self):
        prior=self.publish()['receipt'];prior['sha256']='0'*64;self.calls.clear()
        self.assertEqual(self.publish(prior)['reason'],'asset_receipt_binding_mismatch');self.assertEqual(self.calls,[])

    def test_unknown_create_no_id_never_creates_again(self):
        def request(*args):
            if args[1:] == ('POST','/file_uploads',{'mode':'single_part','filename':self.asset.filename,'content_type':self.asset.content_type}):
                raise assets.UnknownAssetWrite('asset_write_unknown')
            return self.request(*args)
        result=self.publish(request=request)
        self.assertEqual(result['reason'],'asset_create_unknown_no_retry')
        self.assertEqual(result['receipt']['phase'],'create_unknown');self.calls.clear()
        second=self.publish(result['receipt'])
        self.assertEqual(second['status'],'held');self.assertEqual(self.calls,[])

    def test_unknown_send_get_only_recovers_or_holds(self):
        def request(*args):
            if args[1]=='POST' and args[2].endswith('/send'):raise assets.UnknownAssetWrite('asset_write_unknown')
            return self.request(*args)
        first=self.publish(request=request)
        self.assertEqual(first['reason'],'asset_send_unknown_readback_required');self.calls.clear()
        recovered=self.publish(first['receipt'])
        self.assertEqual(recovered['status'],'ready');self.assertEqual([c[0] for c in self.calls],['GET'])
        self.calls.clear()
        held=self.publish(first['receipt'],request=lambda *args:self.uploaded(status='pending'))
        self.assertEqual(held['status'],'held');self.assertEqual(self.calls,[])

    def test_created_checkpoint_can_send_once_after_safe_restart(self):
        first=self.publish()['receipt'];first.update({'phase':'created','no_retry':False})
        self.calls.clear();seen=False
        def request(*args):
            nonlocal seen
            if args[1]=='GET' and args[2].startswith('/file_uploads/') and not seen:
                seen=True;self.calls.append((args[1],args[2],None,None));return self.uploaded(status='pending')
            return self.request(*args)
        self.assertEqual(self.publish(first,request=request)['status'],'ready')
        self.assertEqual(sum(c[0]=='POST' for c in self.calls),1)

    def test_failed_prewrite_checkpoint_sends_no_api_mutation(self):
        def fail(receipt):raise OSError('synthetic checkpoint unavailable')
        result=self.publish(callback=fail)
        self.assertEqual(result['reason'],'asset_checkpoint_failed')
        self.assertTrue(all(c[0]=='GET' for c in self.calls))

    def test_workspace_limit_is_read_not_assumed_and_unknown_is_held(self):
        for value,expected in [({},'asset_workspace_limit_unknown'),
                ({'bot':{'workspace_limits':{'max_file_upload_size_in_bytes':1}}},'asset_workspace_limit_exceeded')]:
            self.calls.clear()
            result=self.publish(request=lambda *args:value)
            self.assertEqual(result['reason'],expected);self.assertFalse(result['block'])

    def test_large_single_part_requires_explicit_fallback_and_no_api(self):
        with mock.patch.object(assets,'SINGLE_PART_BYTES',1):result=self.publish()
        self.assertEqual(result['reason'],'asset_multipart_required');self.assertEqual(self.calls,[])
        self.assertTrue(result['fallback_requires_private_github_access'])

    def test_expired_or_archived_upload_is_not_reused(self):
        prior=self.publish()['receipt']
        for updates in ({'expiry_time':'2000-01-01T00:00:00Z'},{'archived':True},{'status':'expired'}):
            result=self.publish(prior,request=lambda *args:self.uploaded(**updates))
            self.assertEqual(result['status'],'held')

    def test_returned_bytes_must_match_source_before_stable_identity(self):
        block={'type':'image','image':{'type':'file','file':{'url':URL}}}
        with mock.patch.object(assets,'_download_returned_asset',return_value=PNG) as download:
            self.assertEqual(assets.verify_uploaded_block(self.asset,block),self.asset.sha256)
            identity=assets.canonical_asset_identity(self.asset,block)
            self.assertNotIn('url',identity);self.assertEqual(identity['sha256'],self.asset.sha256)
            download.assert_called_with(URL)
        with mock.patch.object(assets,'_download_returned_asset',return_value=b'different'):
            with self.assertRaises(assets.AssetHeld):assets.verify_uploaded_block(self.asset,block)

    def test_human_native_file_uses_real_byte_identity_without_git_claim(self):
        block={'type':'file','file':{'type':'file','file':{'url':URL}}}
        with mock.patch.object(assets,'_download_returned_asset',return_value=b'human file'):
            result=assets.canonical_native_file(block)
        self.assertEqual(result,{'type':'file','sha256':hashlib.sha256(b'human file').hexdigest(),'size':10})
        self.assertNotIn('source_commit',result)
        with mock.patch.object(assets,'_download_returned_asset',side_effect=ValueError('asset_file_budget')):
            with self.assertRaises(assets.AssetHeld) as error:assets.canonical_native_file(block)
            self.assertEqual(error.exception.code,'asset_target_read_budget')

    def test_download_urls_reject_external_private_http_auth_or_redirect_hosts(self):
        for url in ('http://s3.us-west-2.amazonaws.com/a','https://127.0.0.1/a',
                    'https://notionusercontent.com.evil.invalid/a','https://user:pass@file.notion.so/a',
                    'https://file.notion.so:444/a','https://other.invalid/a'):
            with self.subTest(url=url),self.assertRaises(assets.AssetHeld):assets.validate_download_url(url)
        block={'type':'image','image':{'type':'external','external':{'url':URL}}}
        with self.assertRaises(assets.AssetHeld):assets.verify_uploaded_block(self.asset,block)

    def test_transport_posts_once_rejects_redirect_and_redacts_error(self):
        opener=mock.Mock();opener.open.side_effect=TimeoutError('synthetic private URL and name')
        with mock.patch.object(assets.urllib.request,'build_opener',return_value=opener):
            with self.assertRaises(assets.UnknownAssetWrite) as error:
                assets._request('synthetic-key','POST','/file_uploads',{})
        self.assertEqual(str(error.exception),'asset_write_unknown');self.assertEqual(opener.open.call_count,1)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Notion-version'),assets.NOTION_VERSION)
        self.assertIsNone(assets._NoRedirect().redirect_request(None,None,None,None,None))

    def test_limit_rejections_stay_explicit_no_upload_retry(self):
        for code,reason in [(403,'asset_access_or_limit_denied'),(429,'asset_rate_limited'),(400,'asset_validation_rejected')]:
            opener=mock.Mock();opener.open.side_effect=urllib.error.HTTPError('https://api.notion.com',code,'denied',{},io.BytesIO(b'private response'))
            with self.subTest(code=code),mock.patch.object(assets.urllib.request,'build_opener',return_value=opener):
                with self.assertRaises(assets.AssetHeld) as error:assets._request('synthetic-key','POST','/file_uploads',{})
                self.assertEqual(error.exception.code,reason);self.assertEqual(opener.open.call_count,1)


if __name__=='__main__':unittest.main()
