"""Full original prepare/publisher verification against an isolated private ref."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import traceback
import get_recurring_runtime as runtime
import get_recurring_entry as entry
import get_publication_provenance as provenance
import get_private_proof as private_proof
from get_canonical_assets import SourceWithAssets
from source_sync_rate import RequestPolicy
from sync_getnote_to_raw import load_credentials

BRANCH='codex/get-canonical-full-runtime-proof'
REF='refs/heads/'+BRANCH
REPO='Eddiehhhhh/eddie-llm-wiki'


def github(path, method='GET', body=None):
    args=['gh','api','--method',method,'repos/'+REPO+'/'+path]
    if body is not None:args+=['--input','-']
    env=dict(os.environ,GH_TOKEN=os.environ['PRIVATE_VAULT_TOKEN'])
    result=subprocess.run(args,input=json.dumps(body) if body else None,text=True,capture_output=True,env=env)
    if result.returncode:raise RuntimeError('isolated_github_request_failed')
    return json.loads(result.stdout)


class IsolatedRemote(runtime.m.Remote):
    def ref(self,name):
        if name==runtime.r.MAIN_REF:
            value=self.request('/git/ref/heads/'+BRANCH)
            if value.get('ref')!=REF or value.get('object',{}).get('type')!='commit':
                raise RuntimeError('isolated_ref_mismatch')
            return value['object']['sha']
        return super().ref(name)

    def cas_main(self,parent,commit):
        if self.ref(runtime.r.MAIN_REF)!=parent:raise RuntimeError('isolated_parent_changed')
        self.local.run('merge-base','--is-ancestor',parent,commit)
        self.local.run('push','--porcelain',runtime.r.REMOTE_URL,commit+':'+REF)


def main():
    token=os.environ['PRIVATE_VAULT_TOKEN']
    main_before=github('git/ref/heads/main')['object']['sha']
    parent=main_before
    try:
        github('git/refs','POST',{'ref':REF,'sha':parent})
    except RuntimeError:
        parent=github('git/ref/heads/'+BRANCH)['object']['sha']
    remote=IsolatedRemote(None,token)
    if remote.source_get_tree(parent)!=remote.source_get_tree(main_before):
        raise RuntimeError('isolated_branch_get_tree_changed')
    text=Path('.github/workflows/get-canonical.yml').read_text()
    policy=json.loads(base64.b64decode(re.search(r"POLICY_B64: '([^']+)'",text).group(1)))
    historical_policy=dict(policy)
    policy['rules_sha256']=entry.rules_sha256()
    policy['get_policy_sha256']=entry.policy_sha256(policy['rules_sha256'],entry.BOOTSTRAP)
    client=provenance.PublicGithub(os.environ.get('GITHUB_TOKEN'))
    client.private_proof_source=private_proof.PrivateRecords(remote)
    with tempfile.TemporaryDirectory(prefix='get-full-entry-') as directory:
        root=Path(directory)
        local=runtime.at_stage('checkout',runtime.r.readback_checkout,root/'objects',entry.BOOTSTRAP['capture_anchor']['capture_commit'],token)
        remote.local=local
        reader,evidence,_=runtime.at_stage('capture_evidence',runtime.m.load_evidence,local,remote,token,root,parent,anchor=entry.BOOTSTRAP['capture_anchor'])
        # Authenticate historical publications using their original public policy.
        # Candidate rules are applied only to the new transaction, never relabel old proof.
        runtime.fetch_commit(local,parent,entry.MAX_INTERVENING+2)
        runtime.fetch_commit(local,entry.BOOTSTRAP['commit'])
        runtime.load_inventory(reader,remote,token,root/'bootstrap',entry.BOOTSTRAP['commit'])
        current=runtime.load_inventory(reader,remote,token,root/'parent',parent)
        anchor=preview=witness=None
        if entry.FULL_RECEIPT in reader.tree(parent):
            verified=runtime.at_stage('receipt_provenance',provenance.discover,client,historical_policy,exclude_run_id=int(os.environ['GITHUB_RUN_ID']),exclude_run_attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),capture_history_witness=True)
            anchor=verified['anchor'];commit=anchor['commit']
            runtime.fetch_commit(local,commit,entry.MAX_INTERVENING+2)
            runtime.load_receipt(reader,remote,token,root,commit)
            receipt=entry.read_receipt(reader,commit,anchor['receipt_sha256'],policy['get_policy_sha256'])
            if receipt['sequence']!=verified['receipt_sequence']:raise RuntimeError('isolated_receipt_sequence')
            before=receipt['parent_commit'];previous=receipt['previous_receipt_commit']
            if provenance.history_witness_required(anchor,receipt):
                witness=verified.get('history_witness') or provenance.verify_history_witness(client,historical_policy,candidate_runs_checked=verified['candidate_runs_checked'])
                runtime.fetch_commit(local,witness.observed_commit)
            runtime.fetch_commit(local,before,entry.MAX_INTERVENING+2)
            runtime.fetch_commit(local,previous)
            if receipt['sequence']>1:runtime.load_receipt(reader,remote,token,root,previous)
            for target in sorted({before,commit}):runtime.load_inventory(reader,remote,token,root/target,target)
        _,_,states=runtime.at_stage('trusted_states',entry.trust_current,reader,evidence,parent,policy['rules_sha256'],anchor,bootstrap=entry.BOOTSTRAP,history_witness=witness)
        _,legacy,registry=states
        runtime.m.selected_baselines(reader,legacy,parent,remote)
        runtime.engine.new_baselines(reader,registry,parent,remote)
        if runtime.n.STATE not in current:
            _,preview=runtime.m.load_canonical_inputs(reader,remote,token,root,parent)
        key,client_id=load_credentials()
        source=SourceWithAssets(runtime.m.c.Source(key,client_id,RequestPolicy(seconds=30*60)),root/'assets')
        plan,files=runtime.at_stage('canonical_prepare',entry.prepare,reader,evidence,parent,source,approved_rules_sha256=policy['rules_sha256'],anchor=anchor,preview=preview,bootstrap=entry.BOOTSTRAP,history_witness=witness)
        def checkpoint(value):runtime.m.write_checkpoint(root/'checkpoint.json',value)
        def readback(commit,values):
            target=root/'readback';target.mkdir()
            return runtime.m.fetch_selected(remote,{p:('100644','blob',runtime.r.oid(data),len(data)) for p,data in values.items()},token,target/'files')
        result=runtime.at_stage('publication',runtime.m.publish,reader,remote,plan,files,runtime.a.sha(runtime.a.encoded(plan)),readback,checkpoint=checkpoint,writers_guard=lambda:runtime.m.disabled_writers(token=os.environ.get('GITHUB_TOKEN')),verifier=lambda rd,pl,fs:entry.verify_plan(rd,evidence,pl,fs,policy['rules_sha256'],bootstrap=entry.BOOTSTRAP,history_witness=witness))
        main_after=github('git/ref/heads/main')['object']['sha']
        if remote.source_get_tree(main_after)!=remote.source_get_tree(main_before):
            raise RuntimeError('production_get_tree_changed_during_probe')
        output={'status':'isolated_full_entry_and_publication_verified','changed_paths':len(files),'result':result,'production_targeted':False,'notion_requested':False}
        Path(os.environ['FULL_PROBE_RESULT']).write_text(json.dumps(output))
        print(json.dumps(output))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc)
        print(json.dumps({'status':'isolated_full_entry_probe_failed','stage':getattr(exc,'runtime_stage','probe_setup'),'error':code if code in runtime.PUBLIC_ERROR_CODES or code.startswith('isolated_') else 'unclassified_probe_error','error_class':type(exc).__name__,'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)[-6:]]}))
        raise SystemExit(1)
