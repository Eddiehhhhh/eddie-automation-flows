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
import get_recurring_runtime as runtime


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class ProbeStageTests(unittest.TestCase):
    def test_private_proof_reader_returns_id_without_emitting_it(self):
        ident = '1234567890123456789'
        ref = 'codex/get-asset-proof-42297879789-1'
        proof = {'contract':probe.PROOF_CONTRACT,'ref':ref,'source_id':ident,
            'parent':'a'*40,'source_json_sha256':'d'*64,
            'assets':[{'path':'Raw/03 Get/test.png','sha256':'b'*64,
                'source_url_sha256':'c'*64,'bytes':100,'mime':'image/png'}]}
        raw = json.dumps(proof,separators=(',',':')).encode()
        class API:
            def call(self,method,path,**kwargs):
                if path == '/git/ref/heads/' + ref:
                    return {'ref':'refs/heads/'+ref,'object':{'sha':probe.PROOF_COMMIT,'type':'commit'}}
                if path == '/contents/' + probe.PROOF_ROOT + '?ref=' + probe.PROOF_COMMIT:
                    return [{'path':probe.PROOF_ROOT+'/'+ident+'.json','type':'file'}]
                if path == '/contents/' + probe.PROOF_ROOT+'/'+ident+'.json?ref=' + probe.PROOF_COMMIT:
                    return {'path':probe.PROOF_ROOT+'/'+ident+'.json','encoding':'base64',
                        'content':base64.b64encode(raw).decode()}
                raise AssertionError('unexpected private proof request')
        self.assertEqual(probe.fetch_proof(API(),ref),proof)

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
        proof={'source_json_sha256':'d'*64,'assets':[{'sha256':sha,'bytes':len(data),'mime':'image/png'}]}
        self.assertEqual(probe.extract_candidate(plan,files,ident,proof,'d'*64),image)
        bad=dict(files);bad[asset_path]=b'changed'
        with self.assertRaises(probe.ProbeFailure):probe.extract_candidate(plan,bad,ident,proof,'d'*64)
        wrong_asset={'source_json_sha256':'d'*64,'assets':[{'sha256':'e'*64,'bytes':len(data),'mime':'image/png'}]}
        with self.assertRaises(probe.ProbeFailure):probe.extract_candidate(plan,files,ident,wrong_asset,'d'*64)
        with self.assertRaises(probe.ProbeFailure):probe.extract_candidate(plan,files,ident,proof,'f'*64)

    def test_proof_parent_must_be_candidate_ancestor(self):
        class API:
            def __init__(self,success):self.success=success
            def call(self,method,path):
                self.asserted=(method,path)
                if not self.success:
                    return {'status':'diverged','base_commit':{'sha':'a'*40},
                        'merge_base_commit':{'sha':'c'*40},'head_commit':{'sha':'b'*40}}
                return {'status':'ahead','base_commit':{'sha':'a'*40},
                    'merge_base_commit':{'sha':'a'*40},'head_commit':{'sha':'b'*40}}
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
        self.assertIn('private-vault-preflight',text)
        self.assertIn('sparse-checkout-cone-mode: true',text)
        self.assertIn('            scripts\n',text)
        self.assertIn('Scripts/task_preflight.py',text)
        self.assertIn('from task_guard import guard_directory, release_guard',text)
        self.assertIn("steps.private_preflight.outputs.lease_active == 'true'",text)
        self.assertNotIn('lease.unlink',text)


if __name__ == '__main__':
    unittest.main()
