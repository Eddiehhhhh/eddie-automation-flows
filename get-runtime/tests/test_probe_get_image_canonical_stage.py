import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import test_get_canonical_inline_image_binding as image_fixture
import probe_get_image_canonical_stage as probe
import get_private_preflight_rules as preflight_rules
import get_recurring_runtime as runtime


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class ProbeStageTests(unittest.TestCase):
    def test_current_private_route_fixture_requires_both_github_rule_paths(self):
        docs={
            preflight_rules.AGENT_RULES: (
                '`Wiki/06 Systems/01 Routing/GitHub AI 接入入口.md` '
                'and `Wiki/06 Systems/02 Operations/GitHub 自动化与发布经验.md`'),
            preflight_rules.ROUTING_INDEX: (
                '| GitHub、Actions、PR、push、secret、deploy | '
                '`Wiki/06 Systems/02 Operations/GitHub 自动化与发布经验.md` |'),
            preflight_rules.GITHUB_ENTRY: '`Raw/00 Meta/Cloud Inbox/README.md`',
            preflight_rules.GITHUB_OPERATIONS: '# GitHub 自动化与发布经验',
            preflight_rules.CLOUD_INBOX: '# Cloud Inbox',
        }
        self.assertTrue(preflight_rules.valid_github_route(docs))
        docs[preflight_rules.ROUTING_INDEX]=docs[preflight_rules.ROUTING_INDEX].replace(
            'GitHub、Actions、PR、push、secret、deploy',
            'GitHub / Actions / PR / push / secret / deploy')
        self.assertFalse(preflight_rules.valid_github_route(docs))
        del docs[preflight_rules.AGENT_RULES]
        self.assertFalse(preflight_rules.valid_github_route(docs))

    def test_private_proof_reader_returns_id_without_emitting_it(self):
        ident = '1234567890123456789'
        ref = 'codex/get-asset-proof-42297879789-1'
        proof = {'contract':probe.PROOF_CONTRACT,'ref':ref,'source_id':ident,
            'source_field':'content_embed','source_content_sha256':'e'*64,
            'parent':'a'*40,'source_json_sha256':'d'*64,
            'assets':[{'path':'Raw/03 Get/test.png','sha256':'b'*64,
                'source_url_sha256':'c'*64,'bytes':100,'mime':'image/png'}]}
        class API:
            def __init__(self,value):self.raw=json.dumps(value,separators=(',',':')).encode()
            def call(self,method,path,**kwargs):
                if path == '/git/ref/heads/' + ref:
                    return {'ref':'refs/heads/'+ref,'object':{'sha':probe.PROOF_COMMIT,'type':'commit'}}
                if path == '/contents/' + probe.PROOF_ROOT + '?ref=' + probe.PROOF_COMMIT:
                    return [{'path':probe.PROOF_ROOT+'/'+ident+'.json','type':'file'}]
                if path == '/contents/' + probe.PROOF_ROOT+'/'+ident+'.json?ref=' + probe.PROOF_COMMIT:
                    return {'path':probe.PROOF_ROOT+'/'+ident+'.json','encoding':'base64',
                        'content':base64.b64encode(self.raw).decode()}
                raise AssertionError('unexpected private proof request')
        self.assertEqual(probe.fetch_proof(API(proof),ref),proof)
        generic_asset_proof=dict(proof,contract='getnote-private-asset-probe-1')
        with self.assertRaises(probe.ProbeFailure):probe.fetch_proof(API(generic_asset_proof),ref)
        attachment_only_proof=dict(proof,source_field='attachments')
        with self.assertRaises(probe.ProbeFailure):probe.fetch_proof(API(attachment_only_proof),ref)

    def test_candidate_requires_one_bound_private_image_and_receipt(self):
        ident='1234567890123456789';data=image_fixture.tiny_png();sha=hashlib.sha256(data).hexdigest()
        root=runtime.r.GET+'/'
        state_path=root+runtime.engine.n.STATE
        receipt_path=runtime.entry.FULL_RECEIPT
        note_path='01 Notes/probe.md';markdown_path=root+note_path
        asset_path=root+'_assets/images/'+sha+'.png'
        image={'reference_sha256':'a'*64,'source_sha256':'b'*64,'asset_path':asset_path,
            'asset_blob':git_blob(data),'asset_sha256':sha,'asset_bytes':len(data),'asset_mime':'image/png'}
        registry={'newById':{ident:{'filePath':note_path,'source_metadata':{'inline_images':{'images':[image]}}}}}
        receipt={'contract':runtime.entry.RECEIPT_CONTRACT,'source_scope':'single_note',
            'changed_content_ids':[ident],'sequence':5}
        files={state_path:json.dumps(registry).encode(),receipt_path:json.dumps(receipt).encode(),
            markdown_path:('![x](../_assets/images/'+sha+'.png)').encode(),asset_path:data}
        plan={'source_scope':'single_note','single_source_id':ident,
            'proposed_content_ids':[ident]}
        proof={'source_field':'content_embed','source_json_sha256':'d'*64,
            'source_content_sha256':'e'*64,
            'assets':[{'sha256':sha,'bytes':len(data),'mime':'image/png'}]}
        actual, matches, markdown = probe.extract_candidate(plan,files,ident,proof,'d'*64,'e'*64)
        self.assertEqual(actual,image)
        self.assertEqual(markdown,markdown_path)
        self.assertEqual(matches,{
            'source_json_match':True,'source_content_match':True,
            'image_sha256_match':True,'image_bytes_match':True,'image_mime_match':True})
        bad=dict(files);bad[asset_path]=b'changed'
        with self.assertRaises(probe.ProbeFailure):probe.extract_candidate(plan,bad,ident,proof,'d'*64,'e'*64)
        checks = [
            ({**proof,'source_json_sha256':'f'*64},'d'*64,'e'*64,'source_json_match'),
            ({**proof,'source_content_sha256':'f'*64},'d'*64,'e'*64,'source_content_match'),
            ({**proof,'assets':[{'sha256':'f'*64,'bytes':len(data),'mime':'image/png'}]},
                'd'*64,'e'*64,'image_sha256_match'),
            ({**proof,'assets':[{'sha256':sha,'bytes':len(data)+1,'mime':'image/png'}]},
                'd'*64,'e'*64,'image_bytes_match'),
            ({**proof,'assets':[{'sha256':sha,'bytes':len(data),'mime':'image/jpeg'}]},
                'd'*64,'e'*64,'image_mime_match'),
        ]
        for altered_proof, source_json, source_content, expected_false in checks:
            with self.subTest(expected_false=expected_false):
                _, observed, _ = probe.extract_candidate(
                    plan,files,ident,altered_proof,source_json,source_content)
                self.assertEqual([key for key,value in observed.items() if not value],[expected_false])
        probe_text=(Path(__file__).resolve().parents[1]/'scripts/probe_get_image_canonical_stage.py').read_text()
        main_body=probe_text.split('def main(argv=None):',1)[1].split("if __name__ == '__main__':",1)[0]
        self.assertNotIn('publish_private_stage(',main_body)
        self.assertIn("'private_proof_ref_created':True",main_body)

    def test_new_proof_binds_current_detail_list_timestamp_and_image_without_payload(self):
        ident='1234567890123456789';data=image_fixture.tiny_png();sha=hashlib.sha256(data).hexdigest()
        updated='2026-11-07T08:09:10Z';url='https://cdn.example.test/image.png?signature=private-value'
        detail={'id':ident,'content':'caption ![image]('+url+')','updated_at':updated}
        summary={'id':ident,'title':'private title','updated_at':updated}
        image={'reference_sha256':hashlib.sha256(url.encode()).hexdigest(),
            'source_sha256':hashlib.sha256(('![image]('+url+')').encode()).hexdigest(),
            'asset_path':'Raw/03 Get/_assets/images/'+sha+'.png','asset_sha256':sha,
            'asset_bytes':len(data),'asset_mime':'image/png'}
        files={'Raw/03 Get/01 Notes/probe.md':b'private body','Raw/03 Get/_assets/images/'+sha+'.png':data}
        proof=probe.build_current_proof(proof_ref='codex/get-asset-proof-42320-1',
            source_id=ident,old_proof_ref='codex/get-asset-proof-42297879789-1',
            parent='a'*40,detail=detail,summary=summary,image=image,
            markdown_path='Raw/03 Get/01 Notes/probe.md',files=files,
            source_json_sha256=hashlib.sha256(probe.sync.json_text(detail).encode()).hexdigest(),
            source_content_sha256=hashlib.sha256(detail['content'].encode()).hexdigest(),
            detail_received_at_utc='2026-11-07T08:09:11Z',created_at_utc='2026-11-07T08:09:12Z')
        self.assertEqual(proof['source_detail_updated_at'],{'type':'string','value':updated})
        self.assertEqual(proof['source_summary_updated_at'],proof['source_detail_updated_at'])
        self.assertEqual(proof['source_detail_received_at_utc'],'2026-11-07T08:09:11Z')
        self.assertEqual(proof['proof_created_at_utc'],'2026-11-07T08:09:12Z')
        self.assertEqual(proof['assets'][0]['source_url_sha256'],image['reference_sha256'])
        serialized=json.dumps(proof,ensure_ascii=False)
        self.assertNotIn(url,serialized)
        self.assertNotIn(detail['content'],serialized)
        self.assertNotIn('private-value',serialized)
        mismatch=dict(summary,updated_at='2026-11-07T08:10:10Z')
        with self.assertRaises(probe.ProbeFailure):
            probe.build_current_proof(proof_ref='codex/get-asset-proof-42320-1',
                source_id=ident,old_proof_ref='codex/get-asset-proof-42297879789-1',
                parent='a'*40,detail=detail,summary=mismatch,image=image,
                markdown_path='Raw/03 Get/01 Notes/probe.md',files=files,
                source_json_sha256=hashlib.sha256(probe.sync.json_text(detail).encode()).hexdigest(),
                source_content_sha256=hashlib.sha256(detail['content'].encode()).hexdigest(),
                detail_received_at_utc='2026-11-07T08:09:11Z',created_at_utc='2026-11-07T08:09:12Z')

    def test_new_proof_writer_is_create_only_and_isolated(self):
        source=(Path(__file__).resolve().parents[1]/'scripts/probe_get_image_canonical_stage.py').read_text()
        body=source.split('def publish_current_proof(',1)[1].split('\n\ndef TimedReplaySource',1)[0]
        self.assertIn("api.call('POST', '/git/refs', {'ref': 'refs/heads/' + proof_ref",body)
        self.assertIn("api.call('GET', ref_path",body)
        self.assertNotIn("'force': True",body)
        self.assertNotIn("'/git/refs/heads/main'",body)
        self.assertIn("if manual.guard(remote) != parent",body)
        self.assertIn("if manual.guard(remote) != parent",body.split('api.call(\'POST\', \'/git/refs\'',1)[1])

    def test_compare_readback_rejects_pagination(self):
        class Response:
            status=200
            def __init__(self,link=None):
                self.headers={} if link is None else {'Link':link}
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,_limit):return b'{"status":"ahead"}'
        class Opener:
            def __init__(self,response):self.response=response
            def open(self,_request,timeout):
                self.timeout=timeout
                return self.response
        with patch.object(probe.urllib.request,'build_opener',return_value=Opener(Response())):
            self.assertEqual(probe.compare_unpaginated('token','a'*40,'b'*40),{'status':'ahead'})
        with patch.object(probe.urllib.request,'build_opener',return_value=Opener(Response('<https://api.github.com/?page=2>; rel="next"'))):
            with self.assertRaises(probe.ProbeFailure) as caught:
                probe.compare_unpaginated('token','a'*40,'b'*40)
        self.assertEqual(caught.exception.category,'proof_compare_incomplete')

    def test_new_proof_writer_creates_only_one_run_ref_and_reads_it_back(self):
        parent='a'*40;tree_sha='b'*40;proof_ref='codex/get-asset-proof-42320-1'
        proof_path=probe.PROOF_ROOT+'/1234567890123456789.json'
        proof={'contract':probe.PROOF_CONTRACT,'proof_version':2,'ref':proof_ref,
            'source_id':'1234567890123456789','parent':parent,
            'proof_created_at_utc':'2026-11-07T08:09:12Z'}
        class FakeRemote:
            def __init__(self,*args):pass
            def commit(self,commit_sha):return {'tree':tree_sha,'parents':[parent]}
        class FakeAPI:
            def __init__(self):self.calls=[];self.created=None;self.proof_bytes=None;self.oid=None;self.commit_sha=None
            def call(self,method,path,payload=None,**kwargs):
                self.calls.append((method,path,payload))
                ref_path='/git/ref/heads/'+proof_ref
                if method=='GET' and path==ref_path:
                    if self.created is None:return None
                    return {'ref':'refs/heads/'+proof_ref,'object':{'type':'commit','sha':self.commit_sha}}
                if method=='GET' and path=='/contents/'+probe.PROOF_ROOT+'?ref='+parent:return None
                if method=='GET' and path=='/contents/'+proof_path+'?ref='+parent:return None
                if method=='POST' and path=='/git/blobs':
                    self.proof_bytes=base64.b64decode(payload['content'],validate=True)
                    self.oid=git_blob(self.proof_bytes)
                    return {'sha':self.oid}
                if method=='POST' and path=='/git/trees':
                    self.assert_payload=payload
                    return {'sha':tree_sha}
                if method=='POST' and path=='/git/commits':
                    actor=payload['author'];timestamp=actor['date'].replace('Z','+00:00')
                    from datetime import datetime
                    epoch=str(int(datetime.fromisoformat(timestamp).timestamp()))
                    actor_line=(actor['name']+' <'+actor['email']+'> '+epoch+' +0000').encode()
                    message=payload['message'].encode()
                    raw=(b'tree '+tree_sha.encode()+b'\nparent '+parent.encode()+b'\nauthor '+actor_line+
                        b'\ncommitter '+actor_line+b'\n\n'+message)
                    self.commit_sha=hashlib.sha1(b'commit '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
                    return {'sha':self.commit_sha}
                if method=='POST' and path=='/git/refs':
                    self.created=payload['ref']
                    return {'ref':self.created,'object':{'type':'commit','sha':payload['sha']}}
                if method=='GET' and path=='/contents/'+probe.PROOF_ROOT+'?ref='+self.commit_sha:
                    return [{'path':proof_path,'type':'file'}]
                if method=='GET' and path=='/contents/'+proof_path+'?ref='+self.commit_sha:
                    return {'path':proof_path,'encoding':'base64',
                        'content':base64.b64encode(self.proof_bytes).decode()}
                raise AssertionError((method,path))
        api=FakeAPI()
        def compare_for(api, files=None):
            return {'status':'ahead','ahead_by':1,'behind_by':0,
                'base_commit':{'sha':parent},'merge_base_commit':{'sha':parent},
                'commits':[{'sha':api.commit_sha}],
                'files':files or [{'filename':proof_path,'status':'added','deletions':0}]}
        with patch.object(probe.manual,'Remote',FakeRemote), patch.object(probe.manual,'guard',return_value=parent), \
                patch.object(probe,'compare_unpaginated',side_effect=lambda *args: compare_for(api)):
            observed_ref,commit=probe.publish_current_proof(api,'token',parent,'c'*40,proof)
        self.assertEqual(observed_ref,proof_ref)
        self.assertEqual(commit,api.commit_sha)
        self.assertEqual(api.assert_payload['base_tree'],'c'*40)
        self.assertEqual(api.assert_payload['tree'],[{'path':proof_path,'mode':'100644','type':'blob','sha':api.oid}])
        ref_posts=[call for call in api.calls if call[:2]==('POST','/git/refs')]
        self.assertEqual(len(ref_posts),1)
        self.assertEqual(ref_posts[0][2]['ref'],'refs/heads/'+proof_ref)
        self.assertNotIn(('POST','/git/refs/heads/main',None),api.calls)

        api_with_sibling=FakeAPI()
        with patch.object(probe.manual,'Remote',FakeRemote), patch.object(probe.manual,'guard',return_value=parent), \
                patch.object(probe,'compare_unpaginated',side_effect=lambda *args: compare_for(api_with_sibling,[
                    {'filename':proof_path,'status':'added','deletions':0},
                    {'filename':'.sync/unrelated','status':'added','deletions':0}])):
            with self.assertRaises(probe.ProbeFailure):
                probe.publish_current_proof(api_with_sibling,'token',parent,'c'*40,proof)
        self.assertFalse(any(call[0:2]==('POST','/git/refs') for call in api_with_sibling.calls))
        FakeRemote.include_sibling=False

    def test_proof_parent_must_be_candidate_ancestor(self):
        class API:
            def __init__(self,success):self.success=success
            def call(self,method,path):
                self.asserted=(method,path)
                if not self.success:
                    return {'status':'ahead','ahead_by':1,'behind_by':0,
                        'base_commit':{'sha':'a'*40},'merge_base_commit':{'sha':'c'*40},
                        'commits':[{'sha':'b'*40}]}
                # Shape taken from the live GitHub Compare API response: it
                # carries base_commit/merge_base_commit and commits, but no
                # top-level head_commit field.
                return {'status':'ahead','ahead_by':1,'behind_by':0,
                    'base_commit':{'sha':'a'*40},'merge_base_commit':{'sha':'a'*40},
                    'commits':[{'sha':'b'*40}]}
        proof={'parent':'a'*40}
        api=API(True)
        probe.verify_proof_parent(api,proof,'b'*40)
        self.assertEqual(api.asserted,('GET','/compare/'+'a'*40+'...'+'b'*40+'?per_page=1&page=1'))
        with self.assertRaises(probe.ProbeFailure):
            probe.verify_proof_parent(API(False),proof,'b'*40)

    def test_runner_does_not_use_source_id_as_workflow_output(self):
        workflow=Path(__file__).resolve().parents[2]/'.github/workflows/get-notion-incremental.yml'
        text=workflow.read_text()
        self.assertNotIn('single_note_id:',text)
        self.assertNotIn('GET_SINGLE_NOTE_ID',text)
        self.assertIn('probe_get_image_canonical_stage.py',text)
        self.assertIn('runtime-reviewed',text)
        self.assertIn('f6dcd238afd3599f37d8b2a6420cb6cc9b47f143',text)
        self.assertIn('private-vault-preflight',text)
        self.assertIn('sparse-checkout-cone-mode: true',text)
        self.assertIn('            scripts\n',text)
        self.assertIn('Scripts/task_preflight.py',text)
        self.assertIn('get_private_preflight_rules',text)
        self.assertIn('from task_guard import guard_directory, release_guard',text)
        self.assertIn("steps.private_preflight.outputs.lease_active == 'true'",text)
        self.assertNotIn('lease.unlink',text)
        probe_text=(Path(__file__).resolve().parents[1]/'scripts/probe_get_image_canonical_stage.py').read_text()
        self.assertIn('runtime.entry.prepare(',probe_text)
        self.assertIn('runtime.load_trusted_inputs(',probe_text)
        self.assertNotIn('runtime.validate_single_note_scope =',probe_text)
        self.assertNotIn('runtime.run(',probe_text)


if __name__ == '__main__':
    unittest.main()
