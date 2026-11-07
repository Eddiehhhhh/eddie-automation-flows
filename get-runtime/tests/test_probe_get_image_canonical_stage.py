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
    def test_prepare_diagnostics_emit_only_static_substage_and_exception_class(self):
        category=probe.canonical_exception_category(
            'detail_parse',ValueError('private URL and response body'))
        self.assertEqual(category,'unexpected_detail_parse_value_error')
        self.assertNotIn('private',category)
        class UnlistedException(Exception):pass
        self.assertEqual(probe.canonical_exception_category(
            'source_validation',UnlistedException('private body')),
            'unexpected_source_validation_other')
        self.assertEqual(probe.canonical_exception_category(
            'untrusted-stage',ValueError('secret')),
            'unexpected_readback_checkout_value_error')

    def test_read_only_mode_returns_before_candidate_publication(self):
        source=(Path(__file__).resolve().parents[1]/'scripts/probe_get_image_canonical_stage.py').read_text()
        diagnostic=source.split('if args.diagnostic_only:',1)[1].split(
            "canonical_substage = 'candidate_publish'",1)[0]
        self.assertIn("'status':'canonical_prepare_verified_read_only'",diagnostic)
        self.assertIn("'candidate_ref_created':False",diagnostic)
        self.assertIn('return 0',diagnostic)
        workflow=Path(__file__).resolve().parents[2]/'.github/workflows/get-notion-incremental.yml'
        self.assertIn('--diagnostic-only',workflow.read_text())

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
        ref = probe.V2_PROOF_REF
        asset_sha='b'*64;updated='2026-11-07T08:09:10Z'
        proof = {'contract':probe.PROOF_CONTRACT,'proof_version':2,'ref':ref,
            'supersedes_ref':probe.V1_PROOF_REF,'source_id':ident,
            'source_field':'content_embed','source_content_sha256':'e'*64,
            'source_json_sha256':'d'*64,'source_summary_sha256':'f'*64,
            'source_detail_updated_at':{'type':'string','value':updated},
            'source_summary_updated_at':{'type':'string','value':updated},
            'source_detail_received_at_utc':'2026-11-07T08:09:11Z',
            'proof_created_at_utc':'2026-11-07T08:09:12Z','parent':'a'*40,
            'note_path':'Raw/03 Get/01 Notes/probe.md','note_sha256':'1'*64,
            'assets':[{'path':'Raw/03 Get/_assets/images/'+asset_sha+'.png','sha256':asset_sha,
                'source_url_sha256':'c'*64,'source_embed_sha256':'2'*64,
                'bytes':100,'mime':'image/png'}]}
        class API:
            def __init__(self,value):self.raw=json.dumps(value,separators=(',',':')).encode()
            def call(self,method,path,**kwargs):
                if path == '/git/ref/heads/' + ref:
                    return {'ref':'refs/heads/'+ref,'object':{'sha':probe.V2_PROOF_COMMIT,'type':'commit'}}
                if path == '/contents/' + probe.PROOF_ROOT + '?ref=' + probe.V2_PROOF_COMMIT:
                    return [{'path':probe.PROOF_ROOT+'/'+ident+'.json','type':'file'}]
                if path == '/contents/' + probe.PROOF_ROOT+'/'+ident+'.json?ref=' + probe.V2_PROOF_COMMIT:
                    return {'path':probe.PROOF_ROOT+'/'+ident+'.json','encoding':'base64',
                        'content':base64.b64encode(self.raw).decode()}
                if path == '/git/commits/' + probe.V2_PROOF_COMMIT:
                    return {'sha':probe.V2_PROOF_COMMIT,'tree':{'sha':'3'*40},
                        'parents':[{'sha':'a'*40}]}
                raise AssertionError('unexpected private proof request')
        self.assertEqual(probe.fetch_proof(API(proof),ref),proof)
        for invalid in (dict(proof,contract='getnote-private-asset-probe-1'),
                        dict(proof,source_field='attachments'),
                        dict(proof,source_json_sha256='not-a-hash'),
                        dict(proof,source_detail_updated_at={'type':'string','value':'bad'})):
            with self.assertRaises(probe.ProbeFailure):probe.fetch_proof(API(invalid),ref)
        with self.assertRaises(probe.ProbeFailure):probe.fetch_proof(API(proof),'codex/get-asset-proof-42297879789-1')

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
        markdown=('../_assets/images/'+sha+'.png').encode()
        files={state_path:json.dumps(registry).encode(),receipt_path:json.dumps(receipt).encode(),
            markdown_path:markdown,asset_path:data}
        plan={'source_scope':'single_note','single_source_id':ident,
            'proposed_content_ids':[ident]}
        url='https://cdn.example.test/image.png?signature=private-value'
        content='caption ![x]('+url+')';updated='2026-11-07T08:09:10Z'
        detail={'id':ident,'content':content,'updated_at':updated}
        summary={'id':ident,'title':'private title','updated_at':updated}
        image.update(reference_sha256=hashlib.sha256(url.encode()).hexdigest(),
            source_sha256=hashlib.sha256(('![x]('+url+')').encode()).hexdigest())
        files[state_path]=json.dumps(registry).encode()
        proof={'source_id':ident,'source_field':'content_embed',
            'source_json_sha256':hashlib.sha256(probe.sync.json_text(detail).encode()).hexdigest(),
            'source_content_sha256':hashlib.sha256(content.encode()).hexdigest(),
            'source_summary_sha256':hashlib.sha256(probe.sync.json_text(summary).encode()).hexdigest(),
            'source_detail_updated_at':{'type':'string','value':updated},
            'source_summary_updated_at':{'type':'string','value':updated},
            'note_path':markdown_path,'note_sha256':hashlib.sha256(markdown).hexdigest(),
            'assets':[{'path':asset_path,'sha256':sha,'bytes':len(data),'mime':'image/png',
                'source_url_sha256':image['reference_sha256'],
                'source_embed_sha256':image['source_sha256']}]}
        actual, matches, actual_markdown = probe.extract_candidate(plan,files,ident,proof,detail,summary)
        self.assertEqual(actual,image)
        self.assertEqual(actual_markdown,markdown_path)
        self.assertTrue(all(matches.values()))
        bad=dict(files);bad[asset_path]=b'changed'
        with self.assertRaises(probe.ProbeFailure):probe.extract_candidate(plan,bad,ident,proof,detail,summary)
        mismatches = [
            dict(proof,source_json_sha256='f'*64),
            dict(proof,source_content_sha256='f'*64),
            dict(proof,source_summary_sha256='f'*64),
            dict(proof,source_detail_updated_at={'type':'string','value':'2026-11-07T08:09:11Z'}),
            dict(proof,note_sha256='f'*64),
            dict(proof,assets=[dict(proof['assets'][0],source_url_sha256='f'*64)]),
            dict(proof,assets=[dict(proof['assets'][0],source_embed_sha256='f'*64)]),
            dict(proof,assets=[dict(proof['assets'][0],sha256='f'*64)]),
            dict(proof,assets=[dict(proof['assets'][0],bytes=len(data)+1)]),
            dict(proof,assets=[dict(proof['assets'][0],mime='image/jpeg')]),
        ]
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch.get('source_field','asset')):
                with self.assertRaises(probe.ProbeFailure):
                    probe.extract_candidate(plan,files,ident,mismatch,detail,summary)

    def test_detail_wire_bytes_are_reused_without_a_second_source_request(self):
        ident='1234567890123456789'
        raw=b'{"success":true,"data":{"id":"'+ident.encode()+b'","content":"one note","updated_at":"2026-11-07T08:09:10Z"}}'
        class Source:
            def __init__(self):self.calls=0
            def detail_raw(self,source_id):
                self.calls+=1
                if source_id!=ident:raise AssertionError('wrong source')
                return raw
        source=Source();replay=probe.TimedReplaySource(source)
        first=replay.detail_raw(ident)
        replay.replaying=True
        second=replay.detail_raw(ident)
        parsed=probe.manual.detail_note(first)
        self.assertEqual(first,raw)
        self.assertEqual(second,raw)
        self.assertEqual(source.calls,1)
        self.assertEqual(parsed['id'],ident)
        self.assertEqual(hashlib.sha256(probe.sync.json_text(parsed).encode()).hexdigest(),
            hashlib.sha256(probe.sync.json_text(probe.manual.detail_note(second)).encode()).hexdigest())

    def test_pinned_gitdata_api_rejects_oversized_tree_response(self):
        class Response:
            status=200
            headers={}
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,limit):return b'x'*limit
        class Opener:
            def open(self,_request,timeout):return Response()
        api=probe.staging.API('test-token')
        api.opener=Opener()
        with self.assertRaises(probe.staging.StageFailure):
            api.call('GET','/git/trees/'+'a'*40)

    def test_candidate_writer_creates_one_run_ref_and_reads_exact_candidate(self):
        import os
        import subprocess
        import tempfile
        from types import SimpleNamespace

        ident='1234567890123456789'
        target='Raw/03 Get/01 Notes/probe.md'
        content=b'one verified candidate\n'
        with tempfile.TemporaryDirectory() as folder:
            repo=Path(folder)/'repo';repo.mkdir()
            def git(*args,env=None):
                return subprocess.check_output(['git','-C',str(repo),*args],env=env,stderr=subprocess.DEVNULL)
            git('init','-q')
            git('config','user.name','fixture');git('config','user.email','fixture@example.invalid')
            (repo/'base.txt').write_bytes(b'base\n');git('add','base.txt');git('commit','-qm','base')
            parent=git('rev-parse','HEAD').decode().strip()
            parent_time=git('show','-s','--format=%ct',parent).decode().strip()
            dest=repo/target;dest.parent.mkdir(parents=True);dest.write_bytes(content)
            git('add',target)
            tree=git('write-tree').decode().strip()
            actor_env=os.environ.copy()
            actor_env.update({'GIT_AUTHOR_NAME':'Get local proposal','GIT_AUTHOR_EMAIL':'get-proposal@example.invalid',
                'GIT_COMMITTER_NAME':'Get local proposal','GIT_COMMITTER_EMAIL':'get-proposal@example.invalid',
                'GIT_AUTHOR_DATE':'@'+parent_time+' +0000','GIT_COMMITTER_DATE':'@'+parent_time+' +0000'})
            candidate=git('commit-tree',tree,'-p',parent,'-m','Prepare canonical Get transaction with receipt',env=actor_env).decode().strip()
            local=SimpleNamespace(run=lambda *args,**kwargs: subprocess.check_output(
                ['git','-C',str(repo),*args],input=kwargs.get('data'),stderr=subprocess.DEVNULL))
            reader=SimpleNamespace(local=local)
            files={target:content}
            plan={'parent_commit':parent,'candidate_commit':candidate,'candidate_tree':tree,
                'source_scope':'single_note','single_source_id':ident,'proposed_content_ids':[ident],
                'changes':[{'path':target}]}
            run_id,attempt=42399999999,1
            ref='refs/heads/codex/get-image-candidate-'+str(run_id)+'-'+str(attempt)
            ref_path='/git/ref/heads/'+ref.removeprefix('refs/heads/')
            class FakeRemote:
                def commit(self,sha):
                    self.asserted=sha
                    return {'tree':tree,'parents':[parent]}
            class FakeAPI:
                def __init__(self,exists=False):self.exists=exists;self.calls=[];self.created=False
                def call(self,method,path,payload=None,**kwargs):
                    self.calls.append((method,path,payload))
                    if method=='GET' and path==ref_path:
                        if not self.created:
                            return {'ref':ref,'object':{'type':'commit','sha':'f'*40}} if self.exists else None
                        return {'ref':ref,'object':{'type':'commit','sha':candidate}}
                    if method=='POST' and path=='/git/blobs':
                        return {'sha':git_blob(content)}
                    if method=='POST' and path=='/git/trees':
                        self.tree_payload=payload;return {'sha':tree}
                    if method=='POST' and path=='/git/commits':
                        self.commit_payload=payload;return {'sha':candidate}
                    if method=='POST' and path=='/git/refs':
                        self.created=True;return {'ref':ref,'object':{'type':'commit','sha':candidate}}
                    raise AssertionError((method,path))
            api=FakeAPI()
            with patch.object(probe.manual,'guard',return_value=parent), \
                 patch.object(probe.manual,'fetch_selected',return_value=files):
                observed,_=probe.publish_candidate_ref(api,'test-token',reader,FakeRemote(),Path(folder),
                    plan,files,run_id,attempt,ident)
            self.assertEqual(observed,ref)
            self.assertEqual(api.tree_payload['base_tree'],git('rev-parse',parent+'^{tree}').decode().strip())
            self.assertEqual(api.tree_payload['tree'],[{'path':target,'mode':'100644','type':'blob','sha':git_blob(content)}])
            self.assertEqual([call[1] for call in api.calls if call[0]=='POST' and call[1]=='/git/refs'],['/git/refs'])
            self.assertFalse(any('/main' in call[1] or 'get-pending' in call[1] for call in api.calls))
            with patch.object(probe.manual,'guard',return_value=parent):
                with self.assertRaises(probe.ProbeFailure):
                    probe.publish_candidate_ref(FakeAPI(exists=True),'test-token',reader,FakeRemote(),Path(folder),
                        plan,files,run_id,attempt,ident)

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
        self.assertIn('codex/get-asset-proof-42327675228-1',text)
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
