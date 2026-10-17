"""Exact three-PNG source and native readback adapter. No credentials or writes.

An upload handle comes only from a separately trusted durable upload receipt.
The native API does not echo that handle: actual block bytes and the complete
page/tree are rechecked before the semantic comparison may use the receipt.
"""
from copy import deepcopy
import re
import time
import urllib.error
import urllib.request

import get_notion_native as n
import get_notion_sink_plan as p
import get_notion_existing_image as old
import get_notion_png_upload as upload
import get_notion_png_evidence as evidence_module

KIND='notion_uploaded_png'
MAX_BYTES=512000

class Blocked(ValueError):pass
def fail(reason):raise Blocked(reason) from None

def receipt(value,expected_sha):
    """The hash is external trust; it never replaces the strict pinned schema."""
    try:
        keys={'upload_id','plan_sha256','source_binding','content_length','content_type',
              'native_bytes_verified','binding_sha256','kind','sha256','byte_count','mime'}
        if not isinstance(value,dict) or set(value)!=keys or upload.digest(value)!=expected_sha:fail('asset_receipt_invalid')
        b=value['source_binding'];ident=b['note_id'];pin=upload.PINS.get(upload.sha(ident.encode()))
        if pin is None or any(b.get(k)!=v for k,v in pin.items()):fail('asset_receipt_invalid')
        if set(b)!={'note_id','md_sha256','asset_path','git_blob','sha256','bytes','mime','data_source_id','source_commit','reference_sha256'}:fail('asset_receipt_invalid')
        if upload.digest(b)!=value['binding_sha256'] or upload.sha(upload.uuid(b['data_source_id']).encode())!=upload.DESTINATION_PIN:fail('asset_receipt_invalid')
        if b['asset_path']!='.sync/get-notion/assets/'+b['sha256']+'.png':fail('asset_receipt_invalid')
        p.hex_hash(b['source_commit'],40);p.hex_hash(value['plan_sha256'],64)
        if value['kind']!=KIND or value['native_bytes_verified'] is not False:fail('asset_receipt_invalid')
        if value['mime']!=value['content_type'] or value['mime']!=b['mime'] or value['mime']!='image/png':fail('asset_receipt_invalid')
        if any(type(value[k]) is not int or value[k]!=b['bytes'] for k in ('byte_count','content_length')):fail('asset_receipt_invalid')
        if value['sha256']!=b['sha256'] or not 0<b['bytes']<=MAX_BYTES:fail('asset_receipt_invalid')
        if upload.uuid(value['upload_id'])!=value['upload_id']:fail('asset_receipt_invalid')
        return deepcopy(value)
    except Blocked:raise
    except Exception:fail('asset_receipt_invalid')

def semantic(value,caption):
    return {'kind':KIND,'upload_id':value['upload_id'],'sha256':value['sha256'],
            'byte_count':value['byte_count'],'mime':'image/png','caption':p.normalize_rich(caption)}

class Resolver:
    """The external-reference exception is only this anchored three-item map."""
    def __init__(self,evidence,receipts,*,current_commit,preview=False):
        if not isinstance(evidence,evidence_module.AssetEvidence) or not callable(current_commit) or type(preview) is not bool:fail('asset_receipt_invalid')
        if not isinstance(receipts,dict) or len(receipts)>3:fail('asset_receipt_invalid')
        self.evidence=evidence;self.current_commit=current_commit;self.preview=preview;self.receipts={}
        for ident,row in receipts.items():
            if not isinstance(row,dict) or set(row)!={'receipt','sha256'}:fail('asset_receipt_invalid')
            value=receipt(row['receipt'],row['sha256'])
            if value['source_binding']['note_id']!=ident:fail('asset_receipt_invalid')
            self.receipts[ident]=(value,row['sha256'])
    def bound(self,ident,data,reference,caption):
        bound=self.evidence.resolve(ident,data,reference,current_commit=self.current_commit())
        raw=self.evidence.read_asset(bound,current_commit=self.current_commit())
        asset=bound['asset'];source=bound['source']
        used={'path':asset['path'],'git_blob':asset['blob'],'sha256':asset['sha256'],
              'evidence_sha256':bound['evidence_sha256'],'reference_sha256':bound['reference_sha256']}
        if ident in self.receipts:
            value,sha=self.receipts[ident];b=value['source_binding']
            wanted={'note_id':ident,'md_sha256':source['sha256'],'asset_path':asset['path'],
                    'git_blob':asset['blob'],'sha256':asset['sha256'],'bytes':asset['bytes'],
                    'mime':asset['mime'],'source_commit':bound['source_commit'],
                    'reference_sha256':bound['reference_sha256']}
            if any(b.get(k)!=v for k,v in wanted.items()):fail('asset_receipt_invalid')
            used.update(upload_receipt_sha256=sha,upload_id=value['upload_id'])
            return semantic(value,caption),used
        if not self.preview:fail('missing_asset')
        # This has no guessed upload ID and cannot pass either write encoder.
        return {'kind':'notion_png_preflight','sha256':asset['sha256'],'byte_count':len(raw),
                'mime':'image/png','caption':p.normalize_rich(caption)},used
    def manifest_binding(self):
        self.evidence.check_current(self.current_commit())
        anchor=self.evidence._anchor
        return {'path':anchor['manifest_path'],'git_blob':anchor['manifest_blob'],'sha256':anchor['manifest_sha256']}
    def may_retry(self,ident,source,failed):
        if failed.get('reason')!='missing_asset' or ident not in self.receipts or self.preview:return False
        value,_=self.receipts[ident];b=value['source_binding']
        return (failed.get('source_commit')==b['source_commit'] and failed.get('md_blob')==source.get('blob')
                and source.get('sha256')==b['md_sha256'])
    def handles(self):
        return {v['upload_id']:{'sha256':v['sha256'],'byte_count':v['byte_count'],'mime':v['mime']}
                for v,_ in self.receipts.values()}

def preflight_blocks(blocks):
    """Check the actual structure and conservative size before uploading bytes."""
    import get_notion_executor as engine
    import get_notion_live_transport as live
    rows=deepcopy(blocks);images=0
    def walk(items):
        nonlocal images
        for row in items:
            if row['type']=='image':
                value=p.normalize_payload('image',row['payload'])
                if value.get('kind')!='notion_png_preflight' or row['children']:fail('asset_receipt_invalid')
                images+=1
                # Validation only, never a returned write plan: account for a
                # real image's bounded handle overhead while preserving count.
                row.update(type='paragraph',payload={'rich_text':value['caption']+[p.text_run('x'*256)],'color':'default'})
            walk(row['children'])
    walk(rows)
    if images!=1:fail('asset_image_count')
    request=engine.request_blocks(rows);live.wire_blocks(request,None)
    return {'images':images,'root_blocks':len(blocks),'total_blocks':sum(1 for _ in _walk(blocks)),
            'write_ready':False}

def _walk(rows):
    for row in rows:
        yield row
        yield from _walk(row['children'])

class AssetGet:
    """Only native Notion URLs; no credentials, cookies, redirects or retries."""
    def __init__(self,*,opener=None,clock=time.monotonic):
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),old.NoRedirect())
        self.clock=clock;self.downloads=0;self.response_bytes=0;self.elapsed=0.0
    def __call__(self,url):
        old.checked_url(url)
        if self.downloads>=12 or self.response_bytes>=8*1024*1024 or self.elapsed>=120:fail('asset_download_budget')
        self.downloads+=1;start=self.clock();denied=False
        try:
            with self.opener.open(urllib.request.Request(url,method='GET'),timeout=min(15,120-self.elapsed)) as response:
                if response.status in (401,403):denied=True;fail('asset_permission_denied')
                if response.status!=200:fail('asset_http_failed')
                data=response.read(min(MAX_BYTES+1,8*1024*1024-self.response_bytes+1));self.response_bytes+=len(data)
            if len(data)>MAX_BYTES or self.response_bytes>8*1024*1024:fail('asset_download_budget')
            return data
        except urllib.error.HTTPError as exc:
            denied=exc.code in (401,403);exc.close()
            fail('asset_permission_denied' if denied else 'asset_http_failed')
        except Blocked:raise
        except Exception:fail('asset_transport_failed')
        finally:
            self.elapsed+=max(0,self.clock()-start)
            if self.elapsed>=120 and not denied:fail('asset_download_budget')

def bind_snapshot(snapshot,ident,value,expected_receipt_sha,*,request,asset_get):
    """Normalize exactly one native PNG after fresh page/block/byte checks.

    The complete snapshot and non-image fields are copied unchanged. The caller
    still performs the executor's exact created/appended/owned-tree comparison.
    """
    downloads=0
    try:
        value=receipt(value,expected_receipt_sha);b=value['source_binding']
        if ident!=b['note_id']:fail('asset_receipt_invalid')
        if not isinstance(snapshot,dict) or snapshot.get('complete') is not True or snapshot.get('identity_query_complete') is not True:fail('asset_snapshot_invalid')
        if snapshot.get('exact_id_matches')==[]:return {'snapshot':deepcopy(snapshot),'binding':None,'asset_downloads':0}
        if snapshot.get('native_blocks_available') is not True:fail('asset_snapshot_invalid')
        page=n.uuid(snapshot['page_id'])
        if [n.uuid(x) for x in snapshot['exact_id_matches']]!=[page] or snapshot['id_property']!=ident or n.uuid(snapshot['parent_data_source'])!=upload.uuid(b['data_source_id']):fail('asset_snapshot_invalid')
        rows=snapshot['recursive_blocks']
        if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):fail('asset_snapshot_invalid')
        images=[r for r in rows if r.get('type')=='image']
        if len(images)!=1:fail('asset_image_count')
        row=images[0];bid=n.uuid(row['id']);parent=n.uuid(row['parent_id'])
        if row.get('has_children') is not False or row.get('children_ids')!=[]:fail('asset_parent_changed')
        def check_page(v):
            if n.uuid(v.get('id'))!=page or v.get('archived') is not False or v.get('in_trash',False) is not False or v.get('last_edited_time')!=snapshot['last_edited_time'] or n.encoded(v.get('properties'))!=n.encoded(snapshot['properties_raw']) or n.uuid(v.get('parent',{}).get('data_source_id'))!=upload.uuid(b['data_source_id']):fail('asset_page_changed')
        def check_block(v):
            wanted='page_id' if parent==page else 'block_id'
            if n.uuid(v.get('id'))!=bid or v.get('parent',{}).get('type')!=wanted or n.uuid(v['parent'].get(wanted))!=parent:fail('asset_parent_changed')
            return old.block_semantic(v)
        first_page=request('GET','/pages/'+page,None);check_page(first_page)
        before=request('GET','/blocks/'+bid,None);initial=check_block(before)
        if n.encoded(old.image_semantic(row['payload']))!=n.encoded(initial['image']):fail('asset_caption_changed')
        seen=set()
        def download(payload):
            nonlocal downloads
            url=old.checked_url(payload['file']['url'])
            if url in seen:return
            if downloads>=3:fail('asset_download_budget')
            downloads+=1;data=asset_get(url)
            if not isinstance(data,bytes) or len(data)!=b['bytes'] or p.digest(data)!=b['sha256'] or p.git_blob(data)!=b['git_blob']:fail('asset_bytes_mismatch')
            if data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR':fail('asset_media_mismatch')
            seen.add(url)
        download(row['payload']);download(before['image'])
        after=request('GET','/blocks/'+bid,None)
        if n.encoded(check_block(after))!=n.encoded(initial):fail('asset_block_changed')
        download(after['image'])
        last_page=request('GET','/pages/'+page,None);check_page(last_page)
        if n.page_difference_categories(first_page,last_page):fail('asset_page_changed')
        result=deepcopy(snapshot)
        for item in result['recursive_blocks']:
            if n.uuid(item['id'])==bid:item['payload']=semantic(value,initial['image']['caption'])
        p.snapshot_tree(result)
        binding={'note_id':ident,'page_id':page,'block_id':bid,'upload_id':value['upload_id'],
                 'receipt_sha256':expected_receipt_sha,'asset_path':b['asset_path'],'git_blob':b['git_blob'],
                 'sha256':b['sha256'],'byte_count':b['bytes'],'mime':'image/png','readback_verified':True}
        return {'snapshot':result,'binding':binding,'asset_downloads':downloads}
    except Blocked as exc:exc.asset_downloads=downloads;raise
    except old.Blocked as exc:
        allowed={'asset_host_unsupported','asset_source_type_unsupported','asset_caption_invalid','asset_block_invalid',
                 'asset_permission_denied','asset_download_budget','asset_http_failed','asset_transport_failed'}
        out=Blocked(str(exc) if str(exc) in allowed else 'asset_binding_invalid');out.asset_downloads=downloads;raise out from None
    except Exception as exc:
        reason='permission_denied' if str(exc)=='permission_denied' or isinstance(exc,urllib.error.HTTPError) and exc.code in (401,403) else 'asset_binding_invalid'
        out=Blocked(reason);out.asset_downloads=downloads;raise out from None

class SnapshotAdapter:
    """Join a saved page response or owned block receipt with uploaded bytes.

    `read_state` must be the externally anchored durable envelope read. Existing
    receipt bindings must be passed from that SAME validated envelope, never
    learned from today's page. Candidate bindings do not grant ownership: the
    engine's complete created() readback must still succeed before atomic save.
    """
    def __init__(self,resolver,read_state,*,request,asset_get,previous=None):
        if not isinstance(resolver,Resolver) or resolver.preview or not callable(read_state):fail('asset_receipt_invalid')
        self.resolver=resolver;self.read_state=read_state;self.request=request;self.asset_get=asset_get;self.previous=previous
        self.png_downloads=0;self.candidates={};self.verified=0
    @property
    def downloads(self):return self.png_downloads+getattr(self.previous,'downloads',0)
    @property
    def bindings(self):return getattr(self.previous,'bindings',{})
    def report(self):
        return self.previous.report() if self.previous is not None else {'verified':0,'failures':{},'host':'not_observed'}
    def __call__(self,snapshot,ident):
        import get_notion_executor as engine
        if ident not in self.resolver.receipts:
            return self.previous(snapshot,ident) if self.previous is not None else snapshot
        value,sha=self.resolver.receipts[ident];source=value['source_binding']
        if snapshot.get('complete') is True and snapshot.get('identity_query_complete') is True and snapshot.get('exact_id_matches')==[]:
            return snapshot
        self.resolver.evidence.check_current(self.resolver.current_commit())
        outer,version=self.read_state()
        import get_notion_outbox as outbox
        try:outbox.check_state(outer)
        except Exception:fail('asset_receipt_invalid')
        # The caller validates the whole envelope; check all engine invariants
        # again and bind this normalization to its exact returned version.
        if not isinstance(outer,dict) or p.digest(outer)!=version:fail('asset_receipt_invalid')
        state=outer.get('engine_state');engine.check_state(state)
        page=p.notion_id(snapshot.get('page_id'))
        old=state['receipts'].get(ident);attachment=outer.get('png_assets',{}).get('attachments',{}).get(ident)
        mode=None
        if isinstance(old,dict) and isinstance(attachment,dict):
            if (old.get('page_id')!=page or old.get('source_commit')!=source['source_commit']
                    or old.get('md_blob')!=self.resolver.evidence._records[ident]['source']['blob']
                    or old.get('data_source_id')!=source['data_source_id']):fail('asset_receipt_invalid')
            mode='owned'
        else:
            journal=state['journal']
            if not isinstance(journal,dict):fail('asset_receipt_invalid')
            plan=journal['plan'];index=journal['index'];operation=journal['operation']
            if not 0<=index<len(plan['records']) or not isinstance(operation,dict):fail('asset_receipt_invalid')
            row=plan['records'][index]
            if (row.get('note_id')!=ident or row.get('action')!='create' or operation.get('kind')!='create'
                    or operation.get('phase')!='acknowledged' or operation.get('response')!=page
                    or plan['publication']['commit']!=source['source_commit']
                    or row['source']['sha256']!=source['md_sha256']
                    or row.get('uploaded_pngs',{}).get(value['upload_id'])!=self.resolver.handles()[value['upload_id']]):fail('asset_receipt_invalid')
            mode='acknowledged'
        try:result=bind_snapshot(snapshot,ident,value,sha,request=self.request,asset_get=self.asset_get)
        except Exception as exc:
            self.png_downloads+=getattr(exc,'asset_downloads',0);raise
        self.png_downloads+=result['asset_downloads'];binding=result['binding']
        view=engine.target(result['snapshot'],ident,source['data_source_id'])
        block=binding['block_id'];managed_hash=p.block_hash(view['nodes'][block])
        joined={**binding,'source_commit':source['source_commit'],'md_blob':self.resolver.evidence._records[ident]['source']['blob'],
                'md_sha256':source['md_sha256'],'reference_sha256':source['reference_sha256'],
                'evidence_sha256':self.resolver.evidence.evidence_sha256,'data_source_id':source['data_source_id'],
                'managed_hash':managed_hash}
        if mode=='owned':
            if attachment!=joined or {'id':block,'normalized_hash':managed_hash} not in old['managed_blocks']:fail('asset_receipt_invalid')
        self.candidates[ident]=joined;self.verified+=1
        return result['snapshot']
