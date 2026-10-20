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

A='40b120de009b9e7cc68f7811614c77fe4e17d385'
STATE_COMMIT='9ce4a19e7c44f7d1a938b9fc0add46b460ec84da'
STATE_BLOB='a4e06fe9c2d0965b62318f0f73ae4f87af68fad2'
STATE_SHA='3f20fb41b81896c51c4e76e4503d305e586774e23699a4055c69b66a9292922a'
STATE_BYTES=1234228
PLAN_SHA='863aa55bccdee516b92cc30bd0369b4e43e32e634200deffe64d6085b5830d0d'
OP_SHA='68b2fb7529aa6a08dfca5865e210b62b404f2813a64ec6f7c3f0c2c69ae7f4d4'
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
        if (journal['index']!=0 or op['kind']!='create_batches' or op['phase']!='acknowledged'
                or op.get('batch_index')!=0 or e.digest(plan)!=PLAN_SHA or e.digest(op)!=OP_SHA):fail('operation_changed')
        row=plan['records'][0];source=plan['policy']['data_source_id'];page=op['page_id']
        e.create_batches.validate_plan(row,source)
        e.create_batches.validate_operation(op,row)
        spec=row['create_batches']['batches'][0]
        if op['batches'][0]['response']!=page:fail('operation_changed')
        # The frozen plan pins 98 root subtrees / 100 recursive blocks.
        # Inspect only the acknowledged prefix, not the unsent second batch.
        prefix={**row,'rendered':row['rendered'][:spec['end']]}
        rec=png_store.upload_receipts(state,asset_key='png_assets_seq2')[row['note_id']]
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
        result=diagnose(prefix,bound['snapshot'],page,source)
        stage='final_guard'
        if m.guard(remote)!=STATE_COMMIT or not e.p.writer_gate({'old_writer_evidence':writer_guard()}):fail('state_changed')
        return {'contract':CONTRACT,'status':'inspected','state_sha256':STATE_SHA,'plan_sha256':PLAN_SHA,'operation_sha256':OP_SHA,
                'remote_writes':0,'journal_writes':0,'asset_downloads':bound['asset_downloads'],
                'batch_index':0,'expected_prefix_roots':spec['end'],
                'expected_prefix_blocks':spec['total_blocks'],'comparison':result}
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
