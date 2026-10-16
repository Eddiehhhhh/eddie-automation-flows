"""Read-only binding of one previously verified image; no upload/write payloads."""
from copy import deepcopy
import time
import urllib.request
import urllib.error
import urllib.parse
import get_notion_native as n
import get_notion_sink_plan as p
import get_notion_image_projection as projection

SOURCE_PIN='b2b1b0b3008aee968561b9ef6b16f8bc2fc28de430b42e28d1cc3985f2ec7c0e'
PAGE_PIN='e5b9feb9ef5c24c9636bbaee769dcced53828c929615b7305c83ce6fb032246d'
MAX_BYTES=191784
# Exact second host observed in authenticated native image response in run 60.
HOSTS=frozenset(('s3.us-west-2.amazonaws.com','prod-files-secure.s3.us-west-2.amazonaws.com'))
KIND='notion_existing_image_block'
class Blocked(ValueError):pass
def fail(reason):raise Blocked(reason) from None

def checked_url(url):
    try:
        x=urllib.parse.urlsplit(url)
        if not isinstance(url,str) or len(url)>16384 or any(ord(c)<=32 for c in url) or x.scheme!='https' or x.hostname not in HOSTS or x.port not in (None,443) or x.username or x.password or x.fragment:fail('asset_host_unsupported')
    except Blocked:raise
    except Exception:fail('asset_host_unsupported')
    return url

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None
class AssetGet:
    """No credentials, redirects, cookies or retries. Call only with API URL."""
    def __init__(self,*,opener=None,clock=time.monotonic):
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        self.downloads=0;self.response_bytes=0;self.elapsed=0.0;self.clock=clock
    def __call__(self,url):
        checked_url(url)
        if self.downloads>=200 or self.response_bytes>=40*1024*1024 or self.elapsed>=120:fail('asset_download_budget')
        self.downloads+=1;start=self.clock();permission_denied=False
        try:
            with self.opener.open(urllib.request.Request(url,method='GET'),timeout=min(15,120-self.elapsed)) as response:
                if response.status in (401,403):
                    permission_denied=True;fail('asset_permission_denied')
                if response.status!=200:fail('asset_http_failed')
                data=response.read(min(MAX_BYTES+1,40*1024*1024-self.response_bytes+1))
                self.response_bytes+=len(data)
            if len(data)>MAX_BYTES:fail('asset_byte_budget')
            if self.response_bytes>40*1024*1024:fail('asset_download_budget')
            return data
        except urllib.error.HTTPError as exc:
            permission_denied=exc.code in (401,403)
            fail('asset_permission_denied' if permission_denied else 'asset_http_failed')
        except Blocked:raise
        except Exception:fail('asset_transport_failed')
        finally:
            self.elapsed+=max(0,self.clock()-start)
            if self.elapsed>=120 and not permission_denied:fail('asset_download_budget')

def image_semantic(payload):
    if not isinstance(payload,dict) or set(payload)-{'type','file','caption'} or payload.get('type')!='file' or not isinstance(payload.get('file'),dict) or set(payload['file'])!={'url','expiry_time'} or not isinstance(payload['file']['expiry_time'],str):fail('asset_source_type_unsupported')
    checked_url(payload['file']['url'])
    try:caption=p.normalize_rich(payload.get('caption',[]))
    except Exception:fail('asset_caption_invalid')
    return {'type':'file','caption':caption}

def block_semantic(block):
    if not isinstance(block,dict) or block.get('object')!='block' or block.get('type')!='image' or block.get('has_children') is not False or block.get('archived') is not False or block.get('in_trash',False) is not False or not isinstance(block.get('last_edited_time'),str):fail('asset_block_invalid')
    result=deepcopy(block);result.pop('request_id',None);result['image']=image_semantic(result['image'])
    return result

def bind_existing_image(snapshot,*,source_binding,expected_source_sha256,source_commit,content_tree,request,asset_get):
    """Private in-memory result. Evidence proves bytes, never text ownership."""
    downloads=0
    try:
        if expected_source_sha256!=SOURCE_PIN:fail('asset_binding_invalid')
        b=projection.check_binding(source_binding,expected_source_sha256)
        p.hex_hash(source_commit,40)
        if content_tree.get(b['asset_path'])!=b['asset_blob'] or b['asset_bytes']!=MAX_BYTES:fail('asset_source_changed')
        if not isinstance(snapshot,dict) or snapshot.get('complete') is not True or snapshot.get('identity_query_complete') is not True or snapshot.get('native_blocks_available') is not True:fail('asset_snapshot_invalid')
        page=n.uuid(snapshot['page_id'])
        if p.digest(page)!=PAGE_PIN or snapshot['id_property']!=b['note_id'] or [n.uuid(x) for x in snapshot['exact_id_matches']]!=[page]:fail('asset_snapshot_invalid')
        rows=snapshot['recursive_blocks'];images=[r for r in rows if r.get('type')=='image']
        if len(images)!=1:fail('asset_image_count')
        row=images[0];bid=n.uuid(row['id'])
        if n.uuid(row['parent_id'])!=page or row['has_children'] is not False or row['children_ids']!=[]:fail('asset_parent_changed')
        first_page=request('GET','/pages/'+page,None)
        def check_page(value):
            if n.uuid(value.get('id'))!=page or value.get('archived') is not False or value.get('in_trash',False) is not False or value.get('last_edited_time')!=snapshot['last_edited_time'] or n.encoded(value.get('properties'))!=n.encoded(snapshot['properties_raw']) or n.uuid(value.get('parent',{}).get('data_source_id'))!=n.uuid(snapshot['parent_data_source']):fail('asset_page_changed')
        check_page(first_page)
        before=request('GET','/blocks/'+bid,None)
        def check_block(value):
            if n.uuid(value.get('id'))!=bid or value.get('parent',{}).get('type')!='page_id' or n.uuid(value['parent'].get('page_id'))!=page:fail('asset_parent_changed')
            return block_semantic(value)
        initial=check_block(before)
        if n.encoded(image_semantic(row['payload']))!=n.encoded(initial['image']):fail('asset_caption_changed')
        seen=set()
        def download(payload):
            nonlocal downloads
            url=checked_url(payload['file']['url'])
            if url in seen:return
            if downloads>=3:fail('asset_download_budget')
            downloads+=1;data=asset_get(url)
            if not isinstance(data,bytes) or len(data)!=b['asset_bytes'] or p.digest(data)!=b['asset_sha256'] or p.git_blob(data)!=b['asset_blob']:fail('asset_bytes_mismatch')
            if not data.startswith(b'\xff\xd8\xff') or not data.endswith(b'\xff\xd9'):fail('asset_media_mismatch')
            seen.add(url)
        download(row['payload']);download(before['image'])
        after=request('GET','/blocks/'+bid,None)
        if n.encoded(check_block(after))!=n.encoded(initial):fail('asset_block_changed')
        download(after['image'])
        last_page=request('GET','/pages/'+page,None);check_page(last_page)
        if n.page_difference_categories(first_page,last_page):fail('asset_page_changed')
        result=deepcopy(snapshot)
        for item in result['recursive_blocks']:
            if item['id']==row['id']:item['payload']={'kind':KIND,'block_id':bid,'sha256':b['asset_sha256'],'byte_count':b['asset_bytes'],'mime':'image/jpeg','caption':initial['image']['caption']}
        binding={'kind':KIND,'asset_path':b['asset_path'],'git_blob':b['asset_blob'],'content_sha256':b['asset_sha256'],'block_id':bid,'page_id':page,'byte_count':b['asset_bytes'],'mime':'image/jpeg','readback_verified':True,'source_commit':source_commit,'source_binding_sha256':expected_source_sha256}
        return {'snapshot':result,'binding':binding,'asset_downloads':downloads}
    except Blocked as exc:exc.asset_downloads=downloads;raise
    except Exception as original:
        # Authenticated Notion permission failure is a whole-run stop signal.
        reason='permission_denied' if str(original)=='permission_denied' or isinstance(original,urllib.error.HTTPError) and original.code in (401,403) else 'asset_binding_invalid'
        exc=Blocked(reason);exc.asset_downloads=downloads;raise exc from None
