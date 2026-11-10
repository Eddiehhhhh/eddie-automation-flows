"""Synthetic remote object graph only; no credentials, network or private data."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_private_runtime_bindings as b
RAW=b.encoded({'contract':b.CONTRACT,'modules':{'get_synthetic':{'VALUE':{'type':'scalar','value':'synthetic only'}}}})
SHA=hashlib.sha256(RAW).hexdigest()
BLOB=hashlib.sha1(b'blob '+str(len(RAW)).encode()+b'\0'+RAW).hexdigest()
class Remote:
    def __init__(self):
        self.meta={'full_name':b.PRIVATE_REPOSITORY,'private':True}
        self.entries=[{'path':b.PRIVATE_PATH.split('/')[-1],'type':'blob','mode':'100644','sha':BLOB,'size':len(RAW)}]
        self.raw=RAW; self.calls=[]
    def ref(self,name): self.calls.append(('ref',name));return '1'*40
    def metadata(self): self.calls.append('metadata');return self.meta
    def commit(self,c):self.calls.append(('commit',c));return {'tree':'2'*40}
    def tree_entries(self,t,r):
        self.calls.append(('tree',t,r))
        if r:raise AssertionError('full recursive tree is forbidden')
        if t=='2'*40:return [{'path':'.sync','type':'tree','mode':'040000','sha':'3'*40}]
        if t=='3'*40:return [{'path':'get-notion','type':'tree','mode':'040000','sha':'4'*40}]
        if t=='4'*40:return self.entries
        raise AssertionError('wrong tree')
    def blob(self,h,n):self.calls.append(('blob',h,n));return self.raw
class Materialization(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'bindings'; self.remote=Remote()
        p=patch.object(b,'TRUSTED_BUNDLE_SHA256',SHA);p.start();self.addCleanup(p.stop)
    def fetch(self):return b.verified_private_blob(self.remote,'1'*40)
    def test_exact_snapshot(self):
        raw,proof=self.fetch(); self.assertEqual(raw,RAW);self.assertEqual(proof['blob'],BLOB);self.assertEqual(len(self.remote.calls),6);self.assertEqual(proof['tree'],'2'*40)
    def test_large_unrelated_root_never_requires_recursive_tree(self):
        original=self.remote.tree_entries
        def large(tree,recursive):
            rows=original(tree,recursive)
            return rows+[{'path':'unrelated-'+str(i),'type':'blob'} for i in range(40000)] if tree=='2'*40 else rows
        self.remote.tree_entries=large
        self.assertEqual(self.fetch()[0],RAW)
        self.assertEqual(sum(isinstance(x,tuple) and x[0]=='tree' for x in self.remote.calls),3)
    def test_wrong_or_duplicate_intermediate_directory_rejected(self):
        original=self.remote.tree_entries
        for mode in ('120000','100644'):
            self.remote.tree_entries=lambda tree,rec,mode=mode: ([{'path':'.sync','type':'tree','mode':mode,'sha':'3'*40}] if tree=='2'*40 else original(tree,rec))
            with self.assertRaises(b.BindingError):self.fetch()
        self.remote.tree_entries=lambda tree,rec: ([{'path':'.sync','type':'tree','mode':'040000','sha':'3'*40}]*2 if tree=='2'*40 else original(tree,rec))
        with self.assertRaises(b.BindingError):self.fetch()
    def test_transport_rejects_truncated_nonrecursive_response(self):
        import get_private_preview_retention as transport
        obj=transport.GitHubRemote(None,'synthetic')
        with patch.object(obj,'request',return_value={'truncated':True,'tree':[]}):
            with self.assertRaises(Exception):obj.tree_entries('2'*40,False)
    def test_public_rejected(self):
        self.remote.meta['private']=False
        with self.assertRaises(b.BindingError):self.fetch()
    def test_other_repo_rejected(self):
        self.remote.meta['full_name']='different/repository'
        with self.assertRaises(b.BindingError):self.fetch()
    def test_missing_path(self):
        self.remote.entries=[]
        with self.assertRaises(b.BindingError):self.fetch()
    def test_duplicate_path(self):
        self.remote.entries*=2
        with self.assertRaises(b.BindingError):self.fetch()
    def test_wrong_blob(self):
        self.remote.entries[0]['sha']='0'*40
        with self.assertRaises(b.BindingError):self.fetch()
    def test_wrong_size(self):
        self.remote.entries[0]['size']+=1
        with self.assertRaises(b.BindingError):self.fetch()
    def test_symlink_mode(self):
        self.remote.entries[0]['mode']='120000'
        with self.assertRaises(b.BindingError):self.fetch()
    def test_tampered_raw(self):
        self.remote.raw=RAW+b' '
        with self.assertRaises(b.BindingError):self.fetch()
    def test_unapproved_bytes_even_valid_blob(self):
        self.remote.raw=b.encoded({'contract':b.CONTRACT,'modules':{'get_synthetic':{'VALUE':{'type':'scalar','value':'different'}}}})
        raw=self.remote.raw;self.remote.entries[0].update(sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(),size=len(raw))
        with self.assertRaises(b.BindingError):self.fetch()
    def test_create_and_readback(self):
        b.materialize(RAW,self.path);self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.path.read_bytes(),RAW)
    def test_no_overwrite(self):
        self.path.write_bytes(b'prior')
        with self.assertRaises(b.BindingError):b.materialize(RAW,self.path)
        self.assertEqual(self.path.read_bytes(),b'prior')
    def test_unsafe_directory(self):
        self.path.parent.chmod(0o777)
        with self.assertRaises(b.BindingError):b.materialize(RAW,self.path)
    def test_symlink_destination(self):
        target=self.path.parent/'target';target.write_bytes(b'prior');self.path.symlink_to(target)
        with self.assertRaises(b.BindingError):b.materialize(RAW,self.path)
        self.assertEqual(target.read_bytes(),b'prior')
    def test_no_raw_error(self):
        def error():raise RuntimeError('private-pretend-secret')
        self.remote.metadata=error
        with self.assertRaisesRegex(b.BindingError,'^private_runtime_bindings_invalid$'):self.fetch()
    def test_bootstrap_private_proof(self):
        destination=self.path/'runtime-bindings.json'
        result=b.bootstrap(self.remote,destination)
        self.assertEqual(result['status'],'private_bindings_materialized')
        self.assertEqual(destination.parent.stat().st_mode&0o777,0o700)
        proof=destination.parent/'readback-proof.json'
        self.assertEqual(proof.stat().st_mode&0o777,0o600)
        self.assertIn(BLOB,proof.read_text())
        self.assertNotIn(BLOB,str(result))
    def test_bootstrap_existing_directory_stops(self):
        self.path.mkdir()
        with self.assertRaises(b.BindingError):b.bootstrap(self.remote,self.path/'runtime-bindings.json')
    def test_bootstrap_invalid_snapshot_no_files(self):
        self.remote.meta['private']=False
        with self.assertRaises(b.BindingError):b.bootstrap(self.remote,self.path/'runtime-bindings.json')
        self.assertFalse(self.path.exists())
    def test_bootstrap_explicit_commit_does_not_read_ref(self):
        b.bootstrap(self.remote,self.path/'runtime-bindings.json','3'*40)
        self.assertNotIn(('ref','refs/heads/main'),self.remote.calls)
        self.assertIn(('commit','3'*40),self.remote.calls)
    def test_cli_missing_token_static_output(self):
        import contextlib,io
        output=io.StringIO()
        with patch.dict(os.environ,{},clear=True),contextlib.redirect_stdout(output):
            self.assertEqual(b.main(['--destination','/sensitive-path']),1)
        self.assertEqual(output.getvalue(),'{"status":"private_runtime_bindings_invalid"}\n')
    def test_template_bootstrap_before_runtime_import(self):
        template=(Path(__file__).resolve().parents[1]/'.github/workflow-templates/get-canonical-controller.yml.disabled').read_text()
        self.assertLess(template.index('Materialize verified private runtime data'),template.index('Validate complete private Notion route before Get'))
        self.assertIn('GET_PRIVATE_BINDINGS_FILE: ${{ runner.temp }}/get-private-bindings/runtime-bindings.json',template)

if __name__=='__main__':unittest.main()
