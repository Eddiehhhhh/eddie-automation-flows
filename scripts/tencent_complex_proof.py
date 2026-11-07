"""Cloud proof for a pre-created private test branch. Never targets main."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import cloud_private_checkout as private
import capture_tencent_complex as capture
import stage_tencent_complex as staging

TEST_PATH = 'Raw/06 TencentDocs/云端验证'
BINDING_PATH = '.sync/tencent-complex/test-binding.json'


def proof_ref(nonce):
    if not re.fullmatch(r'[0-9]{10,20}', nonce or ''):
        raise ValueError('invalid proof nonce')
    return 'codex/tencent-proof-' + nonce


def git_auth(token):
    env = {k:v for k,v in os.environ.items() if k != 'PRIVATE_VAULT_TOKEN' and not k.startswith('GIT_CONFIG_')}
    auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
    env.update({'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'http.https://github.com/.extraheader','GIT_CONFIG_VALUE_0':'AUTHORIZATION: basic '+auth,'GIT_TERMINAL_PROMPT':'0'})
    return env


def run(args, env, *, cwd=None, timeout=900):
    result = subprocess.run(args, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise ValueError('private command failed')
    return result.stdout


def clone_private(destination, ref, token, *, materialize=True):
    if ref != proof_ref(ref.removeprefix('codex/tencent-proof-')) or destination.exists():
        raise ValueError('invalid checkout')
    metadata = private.verify_private_repository(token)
    if metadata.get('full_name') != private.REPOSITORY or metadata.get('private') is not True:
        raise ValueError('private repository required')
    env = git_auth(token)
    args=['git','-c','credential.helper=','-c','core.hooksPath=/dev/null','clone','--filter=blob:none','--single-branch','--branch',ref,'--no-tags']
    if not materialize: args.append('--no-checkout')
    run(args+['https://github.com/'+private.REPOSITORY+'.git',str(destination)],env)


def validate_checkout(vault, ref, token, env):
    private.verify_private_checkout(vault, token)
    branch=run(['git','branch','--show-current'],env,cwd=vault).decode().strip()
    if branch != ref or ref != proof_ref(ref.removeprefix('codex/tencent-proof-')):
        raise ValueError('wrong proof branch')
    if run(['git','status','--porcelain','-z','--untracked-files=all'],env,cwd=vault):
        raise ValueError('dirty checkout')
    run(['git','fetch','origin','main'],env,cwd=vault)
    run(['git','merge-base','--is-ancestor','FETCH_HEAD','HEAD'],env,cwd=vault)
    paths=run(['git','diff','--name-only','-z','FETCH_HEAD','HEAD'],env,cwd=vault).decode().split('\0')
    for path in filter(None,paths):
        if path != BINDING_PATH and not path.startswith(TEST_PATH+'/'):
            raise ValueError('unexpected proof baseline change')


def readback_file(repo, path, env):
    proc=subprocess.Popen(['git','show','HEAD:'+path],cwd=repo,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    digest=hashlib.sha256()
    for chunk in iter(lambda:proc.stdout.read(1024*1024),b''):
        digest.update(chunk)
    _, stderr=proc.communicate(timeout=180)
    if proc.returncode:
        raise ValueError('private object readback failed')
    return digest.hexdigest()



def expected_paths(version_name, manifest):
    base = Path(TEST_PATH) / version_name
    return [str((base/p).as_posix()) for p in ['snapshot.json','excel-manifest.json','阅读入口.md'] + ['excel/'+x['path'] for x in manifest['entries']]] + [TEST_PATH+'/索引.md']


def verify_remote_snapshot(work, vault, ref, token, env, paths):
    mirror=work/'readback'
    clone_private(mirror,ref,token,materialize=False)
    head=run(['git','rev-parse','HEAD'],env,cwd=vault)
    if run(['git','rev-parse','HEAD'],env,cwd=mirror) != head:
        raise ValueError('proof branch changed')
    for path in paths:
        if readback_file(mirror,path,env)!=staging.digest(vault/path):
            raise ValueError('proof readback mismatch')


def proof(nonce, work, token):
    if not token: raise ValueError('private token required')
    ref=proof_ref(nonce); work=Path(work); vault=work/'vault'; env=git_auth(token)
    clone_private(vault,ref,token);validate_checkout(vault,ref,token,env)
    binding=json.loads((vault/BINDING_PATH).read_text(encoding='utf-8'))
    file_id=binding.get('file_id')
    if not isinstance(file_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',file_id):
        raise ValueError('invalid private binding')
    if not os.environ.get('TENCENT_DOCS_TOKEN'):raise ValueError('Tencent credential required')
    capture_dir=work/'capture'
    capture_env=env.copy();capture_env['TENCENT_COMPLEX_FILE_ID']=file_id
    target=vault/TEST_PATH; index=target/'索引.md';old_hash=staging.index_digest(index)
    args=[sys.executable,str(Path(__file__).with_name('capture_tencent_complex.py')),'--output-dir',str(capture_dir)]
    if index.exists():
        content=index.read_text(encoding='utf-8'); match=re.search(r'\((快照-[a-f0-9]{20})/阅读入口\.md\)',content)
        if not match:raise ValueError('invalid proof index')
        args+=['--previous-dir',str(target/match.group(1))]
    run(args,capture_env,timeout=2100)
    if not capture_dir.exists():
        if not index.exists() or not match:
            raise ValueError('missing verified previous proof')
        version_name=match.group(1)
        version=target/version_name
        snapshot=json.loads((version/'snapshot.json').read_text())
        if not capture.prior_complete(version,snapshot):
            raise ValueError('previous proof incomplete')
        manifest=json.loads((version/'excel-manifest.json').read_text())
        expected=expected_paths(version_name,manifest)
        verify_remote_snapshot(work,vault,ref,token,env,expected)
        print(json.dumps({'proof_objects_verified':len(expected),'private_branch_only':True,'unchanged':True}))
        return
    result=staging.stage(capture_dir,vault,TEST_PATH,old_hash)
    version=target/result['version'];manifest=json.loads((version/'excel-manifest.json').read_text())
    expected=expected_paths(result['version'],manifest)
    run(['git','add','--',TEST_PATH],env,cwd=vault)
    tracked=set(run(['git','ls-files','-z','--',TEST_PATH],env,cwd=vault).decode().split('\0'))
    if any(p not in tracked for p in expected):raise ValueError('proof files not staged')
    for path in expected:
        indexed=run(['git','rev-parse',':'+path],env,cwd=vault).decode().strip()
        actual=run(['git','hash-object','--',path],env,cwd=vault).decode().strip()
        if indexed!=actual:raise ValueError('staged object mismatch')
    changed=run(['git','diff','--cached','--name-only','-z'],env,cwd=vault).decode().split('\0')
    if any(not p.startswith(TEST_PATH+'/') for p in changed if p):raise ValueError('unexpected staged path')
    run([sys.executable,str(Path(__file__).with_name('commit_private_vault.py')),'--vault',str(vault),'--message','Verify Tencent complex capture','--ref',ref,'--path',TEST_PATH],env,timeout=1800)
    verify_remote_snapshot(work,vault,ref,token,env,expected)
    print(json.dumps({'proof_objects_verified':len(expected),'private_branch_only':True}))


def main():
    with tempfile.TemporaryDirectory(prefix='tencent-complex-proof-') as temp:
        proof(os.environ.get('TENCENT_PROOF_NONCE',''),temp,os.environ.get('PRIVATE_VAULT_TOKEN',''))

if __name__=='__main__':
    try: main()
    except Exception: raise SystemExit('Tencent cloud proof failed; private details suppressed')
