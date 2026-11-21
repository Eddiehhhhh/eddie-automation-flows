import base64, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import tencent_complex_proof as p

class ProofBoundaryTests(unittest.TestCase):
    def test_proof_ref_rejects_main_other_branches_and_injection(self):
        self.assertEqual(p.proof_ref('1234567890'),'codex/tencent-proof-1234567890')
        for bad in ('main','codex/foo','','123456789','1234567890;whoami','123456789012345678901'):
            with self.subTest(value=bad), self.assertRaises(ValueError):p.proof_ref(bad)
    def test_token_is_only_in_subprocess_environment(self):
        with patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN':'test-token','GIT_CONFIG_KEY_9':'bad'}):
            env=p.git_auth('test-token')
        self.assertNotIn('PRIVATE_VAULT_TOKEN',env);self.assertNotIn('GIT_CONFIG_KEY_9',env)
        self.assertEqual(env['GIT_CONFIG_COUNT'],'1');self.assertIn(base64.b64encode(b'x-access-token:test-token').decode(),env['GIT_CONFIG_VALUE_0'])
    def test_public_repo_rejected_without_git(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(p.private,'verify_private_repository',return_value={'private':False}),patch.object(p,'run') as run:
            with self.assertRaises(ValueError):p.clone_private(Path(temp)/'v','codex/tencent-proof-1234567890','test')
            run.assert_not_called()
    def test_git_failure_does_not_echo_private_text(self):
        proc=subprocess.CompletedProcess([],1,stdout=b'PRIVATE_DATA',stderr=b'PRIVATE_TOKEN')
        with patch.object(p.subprocess,'run',return_value=proc):
            with self.assertRaisesRegex(ValueError,'private command failed') as err:p.run(['x'],{})
            self.assertNotIn('PRIVATE_TOKEN',str(err.exception));self.assertNotIn('PRIVATE_DATA',str(err.exception))

    def test_isolated_end_to_end_does_not_touch_main(self):
        import contextlib, io, hashlib, json, shutil
        nonce='1234567890'; ref=p.proof_ref(nonce)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);bare=root/'remote.git';seed=root/'seed';work=root/'work';work.mkdir()
            def git(*args,cwd=None):
                return subprocess.run(['git',*args],cwd=cwd,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            git('init','--bare',str(bare));git('init','-b','main',str(seed));git('config','user.name','test',cwd=seed);git('config','user.email','test@example.invalid',cwd=seed)
            (seed/'base').write_text('base');git('add','base',cwd=seed);git('commit','-m','base',cwd=seed);git('remote','add','origin',str(bare),cwd=seed);git('push','origin','main',cwd=seed)
            main_before=git('rev-parse','main',cwd=seed).stdout.strip();git('checkout','-b',ref,cwd=seed)
            binding=seed/p.BINDING_PATH;binding.parent.mkdir(parents=True);binding.write_text(json.dumps({'file_id':'fixture'}));git('add',p.BINDING_PATH,cwd=seed);git('commit','-m','fixture',cwd=seed);git('push','origin',ref,cwd=seed)
            fixture=root/'capture';xml=fixture/'excel/xl/workbook.xml';xml.parent.mkdir(parents=True)
            data=b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="A"/></sheets></workbook>';xml.write_bytes(data)
            snapshot={'file_id':'fixture','sheets':[{'metadata':{'sheet_name':'A','sheet_id':'s','sheet_type':'worksheet'},'cells':[{'row':0,'col':0,'value_type':'STRING','string_value':'test'}]}]}
            (fixture/'snapshot.json').write_text(json.dumps(snapshot));(fixture/'excel-manifest.json').write_text(json.dumps({'entries':[{'path':'xl/workbook.xml','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}))
            orig_run=p.run
            def fake_run(args,env,**kwargs):
                if len(args)>1 and str(args[1]).endswith('capture_tencent_complex.py'):
                    shutil.copytree(fixture,Path(args[args.index('--output-dir')+1]));return b''
                return orig_run(args,env,**kwargs)
            def local_clone(dest,branch,token,*,materialize=True):
                args=['clone','--branch',branch]
                if not materialize:args+=['--no-checkout']
                git(*args,str(bare),str(dest))
            with patch.object(p,'clone_private',side_effect=local_clone),patch.object(p.private,'verify_private_checkout'),patch.object(p,'run',side_effect=fake_run),patch.dict(os.environ,{'TENCENT_DOCS_TOKEN':'fixture'}),contextlib.redirect_stdout(io.StringIO()):
                p.proof(nonce,work,'fixture')
            self.assertEqual(main_before,git('--git-dir',str(bare),'rev-parse','refs/heads/main').stdout.strip())
            result=git('--git-dir',str(bare),'ls-tree','-r','--name-only',ref).stdout.decode();self.assertIn('snapshot.json',result)
            proof_before=git('--git-dir',str(bare),'rev-parse',ref).stdout.strip()
            work2=root/'work2';work2.mkdir()
            def unchanged_run(args,env,**kwargs):
                if len(args)>1 and str(args[1]).endswith('capture_tencent_complex.py'):return b''
                return orig_run(args,env,**kwargs)
            with patch.object(p,'clone_private',side_effect=local_clone),patch.object(p.private,'verify_private_checkout'),patch.object(p,'run',side_effect=unchanged_run),patch.dict(os.environ,{'TENCENT_DOCS_TOKEN':'fixture'}),contextlib.redirect_stdout(io.StringIO()) as output:
                p.proof(nonce,work2,'fixture')
            self.assertIn('"unchanged": true',output.getvalue())
            self.assertEqual(proof_before,git('--git-dir',str(bare),'rev-parse',ref).stdout.strip())
            self.assertEqual(main_before,git('--git-dir',str(bare),'rev-parse','refs/heads/main').stdout.strip())
    def test_unrelated_private_branch_delta_is_rejected(self):
        ref=p.proof_ref('1234567890')
        with tempfile.TemporaryDirectory() as tmp,patch.object(p.private,'verify_private_checkout'),patch.object(p,'run',side_effect=[ref.encode(),b'',b'',b'',b'unrelated-file\0']):
            with self.assertRaisesRegex(ValueError,'unexpected proof baseline change'):p.validate_checkout(Path(tmp),ref,'test',{})
    def test_workflow_only_manual_and_no_private_artifacts(self):
        path=Path(__file__).resolve().parents[1]/'.github/workflows/tencent-complex-proof.yml';text=path.read_text()
        self.assertIn('workflow_dispatch:',text);self.assertNotIn('schedule:',text);self.assertNotIn('upload-artifact',text)
        self.assertIn('group: eddie-private-vault-writers',text);self.assertIn('run_private_command.py',text)
