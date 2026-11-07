import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest

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
        actual, matches = probe.extract_candidate(plan,files,ident,proof,'d'*64,'e'*64)
        self.assertEqual(actual,image)
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
                _, observed = probe.extract_candidate(
                    plan,files,ident,altered_proof,source_json,source_content)
                self.assertEqual([key for key,value in observed.items() if not value],[expected_false])
        probe_text=(Path(__file__).resolve().parents[1]/'scripts/probe_get_image_canonical_stage.py').read_text()
        main_body=probe_text.split('def main(argv=None):',1)[1].split("if __name__ == '__main__':",1)[0]
        self.assertNotIn('publish_private_stage(',main_body)
        self.assertIn("'stage_written':False,'main_updated':False",main_body)

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
