#!/usr/bin/env python3
"""One pinned acknowledged-create readback inspection; no mutation capability."""
import argparse
import hashlib
import json
import os
import re
from urllib.parse import urlsplit, parse_qs
from pathlib import Path
import subprocess
import sys

A='086fc1425a95f7bce6df55b3e36aa449f679a316'
STATE_COMMIT='cd18cea23ef94cba570afd45c47ae16be8af6745'
STATE_BLOB='045b41400e1b539fde0586248723ac8305dd0ab3'
STATE_SHA='25a4ed324a22068d36234bafffcb7c1736a224592a1b0265be9352f1dd998ee0'
STATE_BYTES=673850
PLAN_SHA='a54e2736d552ce4fc4f14810bb052745dfc931468b7d06b6bc5608678bdf18b5'
OP_SHA='03904ded8243ecfd65115107fcd22414a976e242a901a8b5566669c6f9a4680c'
CONTRACT='get-saved-create-inspection-1'

class Blocked(ValueError):pass
def fail(reason):raise Blocked(reason) from None

def inspect(remote, request, writer_guard, asset_get, diagnose):
    import get_legacy_manual as m
    import get_notion_executor as e
    import get_notion_outbox as outbox
    import get_notion_png_store as png_store
    import get_notion_uploaded_png as png
    import get_notion_live_transport as live
    import get_notion_readonly as ro
    stage='state_guard'
    try:
        if m.guard(remote)!=STATE_COMMIT:fail('state_changed')
        root=remote.commit(STATE_COMMIT)['tree']
        for name,kind,mode in (('.sync','tree','040000'),('get-notion','tree','040000'),('state.json','blob','100644')):
            found=[x for x in remote.tree_entries(root) if x.get('path')==name]
            if len(found)!=1 or found[0].get('type')!=kind or found[0].get('mode')!=mode:fail('state_changed')
            root=found[0]['sha']
        if root!=STATE_BLOB:fail('state_changed')
        raw=remote.blob(STATE_BLOB,STATE_BYTES)
        if len(raw)!=STATE_BYTES or hashlib.sha256(raw).hexdigest()!=STATE_SHA:fail('state_changed')
        state=m.strict_capture_json(raw,STATE_BYTES)
        outbox.check_state(state)
        if outbox.encoded(state)!=raw:fail('state_changed')
        journal=state['engine_state']['journal'];op=journal['operation'];plan=journal['plan']
        if journal['index']!=0 or op['kind']!='create' or op['phase']!='acknowledged' or e.digest(plan)!=PLAN_SHA or e.digest(op)!=OP_SHA:fail('operation_changed')
        row=plan['records'][0];source=plan['policy']['data_source_id'];page=op['response']
        rec=png_store.upload_receipts(state)[row['note_id']]
        stage='writers'
        if not e.p.writer_gate({'old_writer_evidence':writer_guard()}):fail('writer_gate_unknown')
        known={page}
        def readonly(method,path,body=None):
            if not ro.read_route(method,path):fail('write_forbidden')
            if method=='POST':
                if path!='/data_sources/'+source+'/query' or not isinstance(body,dict) or set(body)-{'page_size','filter','start_cursor'} or body.get('page_size')!=100 or type(body.get('page_size')) is not int or body.get('filter')!={'property':'笔记 ID','rich_text':{'equals':row['note_id']}}:fail('read_scope_changed')
                cursor=body.get('start_cursor')
                if cursor is not None and (not isinstance(cursor,str) or not 0<len(cursor)<=1024):fail('read_scope_changed')
            elif path in ('/pages/'+page,'/data_sources/'+source):
                if body is not None:fail('read_scope_changed')
            else:
                parts=urlsplit(path);match=re.fullmatch(r'/blocks/([a-f0-9]{32})(/children)?',parts.path)
                if body is not None or not match or match[1] not in known or parts.scheme or parts.netloc or parts.fragment:fail('read_scope_changed')
                if parts.query:
                    query=parse_qs(parts.query,strict_parsing=True,keep_blank_values=True)
                    if not match[2] or set(query)-{'page_size','start_cursor'} or query.get('page_size')!=['100'] or any(len(v)!=1 or len(v[0])>1024 for v in query.values()):fail('read_scope_changed')
            value=request(method,path,body)
            if method=='GET' and '/children' in path and isinstance(value,dict):
                for item in value.get('results',[]):
                    if isinstance(item,dict) and isinstance(item.get('id'),str):known.add(live.native.uuid(item['id']))
            return value
        stage='native_read'
        api=live.API(readonly,source,[row['note_id']])
        snapshot=api.read_note(row['note_id'])
        if snapshot.get('page_id')!=page:fail('saved_page_changed')
        stage='image_readback'
        bound=png.bind_snapshot(snapshot,row['note_id'],rec['receipt'],rec['sha256'],request=readonly,asset_get=asset_get)
        stage='compare'
        result=diagnose(row,bound['snapshot'],page,source)
        stage='final_guard'
        if m.guard(remote)!=STATE_COMMIT or not e.p.writer_gate({'old_writer_evidence':writer_guard()}):fail('state_changed')
        return {'contract':CONTRACT,'status':'inspected','state_sha256':STATE_SHA,'plan_sha256':PLAN_SHA,'operation_sha256':OP_SHA,
                'remote_writes':0,'journal_writes':0,'asset_downloads':bound['asset_downloads'],'comparison':result}
    except Exception as exc:
        allowed={'state_changed','operation_changed','writer_gate_unknown','write_forbidden','read_scope_changed','saved_page_changed','permission_denied','asset_permission_denied','request_budget','response_budget','asset_download_budget','asset_bytes_mismatch','asset_page_changed','asset_block_changed'}
        return {'contract':CONTRACT,'status':'blocked','stage':stage,'reason':str(exc) if str(exc) in allowed else 'inspection_failed','remote_writes':0,'journal_writes':0}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',type=Path,required=True);args=parser.parse_args()
    root=args.runtime.resolve()
    try:
        actual=subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout.decode().strip()
        if actual!=A:fail('runtime_changed')
        sys.path.insert(0,str(root/'scripts'))
        import get_legacy_manual as m
        import get_notion_live_transport as live
        import get_notion_uploaded_png as png
        from get_notion_writer_guard import WriterGuard
        from get_notion_saved_create_diagnostic import diagnose_created
        remote=m.Remote(None,os.environ.get('PRIVATE_VAULT_TOKEN'))
        request=live.Transport(os.environ.get('NOTION_API_KEY'))
        result=inspect(remote,request,WriterGuard(os.environ.get('GITHUB_TOKEN')),png.AssetGet(),diagnose_created)
    except Exception:
        result={'contract':CONTRACT,'status':'blocked','stage':'setup','reason':'inspection_failed','remote_writes':0,'journal_writes':0}
    print(json.dumps(result,ensure_ascii=True,separators=(',',':')))
    return 0 if result['status']=='inspected' else 1

if __name__=='__main__':raise SystemExit(main())
