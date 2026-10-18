#!/usr/bin/env python3
"""Fixed historical second-upload observation. No mutable-state recovery authority."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
A='086fc1425a95f7bce6df55b3e36aa449f679a316'
STATE_COMMIT='cd18cea23ef94cba570afd45c47ae16be8af6745'
STATE_BLOB='045b41400e1b539fde0586248723ac8305dd0ab3'
CONTRACT='get-saved-upload-inspection-runner-1'

def fail(reason):raise ValueError(reason) from None

def inspect(remote,base):
    import get_legacy_manual as m
    import get_notion_saved_upload_status as diagnostic
    import get_notion_png_upload as upload
    import get_notion_png_transport as transport
    import get_notion_png_store as store
    from get_notion_runtime import ReadOnlyStore
    stage='state_guard';result=None
    try:
        head=m.guard(remote)
        root=remote.commit(STATE_COMMIT)['tree']
        for name,kind,mode in (('.sync','tree','040000'),('get-notion','tree','040000'),('state.json','blob','100644')):
            rows=[r for r in remote.tree_entries(root) if r.get('path')==name]
            if len(rows)!=1 or rows[0].get('type')!=kind or rows[0].get('mode')!=mode:fail('state_changed')
            root=rows[0]['sha']
        if root!=STATE_BLOB:fail('state_changed')
        raw=remote.blob(STATE_BLOB,diagnostic.STATE_BYTES)
        slot=diagnostic.saved_slot(raw)
        state=m.strict_capture_json(raw,diagnostic.STATE_BYTES)
        stage='asset_guard'
        binding=slot['plan']['binding']
        data=remote.blob(binding['git_blob'],binding['bytes'])
        readonly=ReadOnlyStore(state)
        inner=store.UploadStore(readonly,binding['note_id'],slot['plan_sha256'],allow_persist=False)
        request=transport.Transport(base,slot['plan'],slot['plan_sha256'],data,inner,allow_writes=False)
        # The same immutable original upload ID is the only permitted route.
        expected=upload.spec('GET','/file_uploads/'+slot['state']['upload_id'])
        used=False
        def once(spec):
            nonlocal used
            if used or spec!=expected:fail('read_scope_changed')
            used=True
            return request(spec)
        stage='upload_read'
        result=diagnostic.inspect_saved_upload(raw,request=once)
        stage='final_guard'
        if m.guard(remote)!=head:fail('state_changed')
        return result
    except Exception as exc:
        allowed={'state_changed','permission_denied','request_budget','asset_changed','asset_not_committed','read_scope_changed'}
        return {'contract':CONTRACT,'status':'blocked','stage':stage,
            'reason':str(exc) if str(exc) in allowed else 'inspection_failed',
            'requests':result['requests'] if result is not None else 0,
            'external_write_attempts':0,'journal_writes':0,'replacement_authorized':False}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',type=Path,required=True);args=parser.parse_args()
    try:
        root=args.runtime.resolve()
        actual=subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout.decode().strip()
        if actual!=A:fail('runtime_changed')
        sys.path.insert(0,str(root/'scripts'))
        import get_legacy_manual as m
        import get_notion_live_transport as live
        result=inspect(m.Remote(None,os.environ.get('PRIVATE_VAULT_TOKEN')),live.Transport(os.environ.get('NOTION_API_KEY')))
    except Exception:
        result={'contract':CONTRACT,'status':'blocked','stage':'setup','reason':'inspection_failed',
            'requests':0,'external_write_attempts':0,'journal_writes':0,'replacement_authorized':False}
    print(json.dumps(result,ensure_ascii=True,separators=(',',':')))
    return 0 if result['status']=='inspected' else 1
if __name__=='__main__':raise SystemExit(main())
