"""Injected official REST transport and <=50-ID adapter; inert on import.

No credential loading, workflow entry, uploads or automatic write retries.
Private snapshots remain caller-owned memory. Never log requests/responses.
"""
from copy import deepcopy
import get_notion_list_semantics as list_semantics
import re
import time
import urllib.request
import urllib.error
import get_notion_native as native
import get_notion_readonly as ro
from get_mirror_incremental_replay import strict_capture_json

VERSION='2025-09-03'
class Failure(ValueError):
    def __init__(self,reason,status=None):super().__init__(reason);self.status=status

class WriteFailure(Failure):
    """Observed HTTP response to one exact physically attempted write."""
    def __init__(self,reason,status,request_fingerprint):
        super().__init__(reason,status);self.request_fingerprint=request_fingerprint

class NotDispatched(Failure):
    """Exact write rejected before opener/physical-attempt accounting."""
    def __init__(self,request_fingerprint):
        super().__init__('request_not_dispatched')
        self.request_fingerprint=request_fingerprint

def fail(reason,status=None):raise Failure(reason,status) from None

def fingerprint(method,path,body):return native.sha({'method':method,'path':path,'body':body})

class Transport:
    def __init__(self,token,*,opener=None,sleep=time.sleep,clock=time.monotonic,approved_writes=()):
        if not isinstance(token,str) or not token or any(ord(c)<=32 for c in token):fail('missing_credentials')
        if any(not isinstance(x,str) or not re.fullmatch('[a-f0-9]{64}',x) for x in approved_writes):fail('invalid_approval')
        self._token=token;self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),ro.NoRedirect())
        self.sleep=sleep;self.clock=clock;self.deadline=clock()+1500;self.last=None
        self.attempts=0;self.bytes=0;self.approved=frozenset(approved_writes);self.attempted=set()
    def __call__(self,method,path,body=None):
        if not isinstance(path,str):fail('invalid_route')
        read=bool(ro.read_route(method,path))
        write=(method=='POST' and path=='/pages') or (method=='PATCH' and re.fullmatch(r'/blocks/[a-f0-9]{32}(?:/children)?',path)) or (method=='PATCH' and re.fullmatch(r'/pages/[a-f0-9]{32}',path) and isinstance(body,dict) and set(body)=={'properties'})
        if not read and not write:fail('invalid_route')
        key=fingerprint(method,path,body)
        if not read and (key not in self.approved or key in self.attempted):fail('write_not_authorized_or_already_attempted')
        try:data=None if body is None else native.encoded(body)
        except Exception:fail('malformed_payload')
        def before_network_budget():
            if not read:raise NotDispatched(key)
            fail('request_budget')
        def closure_budget(*,before=True):
            reservation=getattr(self,'closure_reservation',None)
            if reservation is None:return
            from get_notion_idle_rotation import ClosureReservation
            try:
                if type(reservation) is not ClosureReservation or reservation.request is not self:
                    raise ValueError('closure budget')
                if reservation.active and not read:raise ValueError('closure read only')
                reservation.check()
            except Exception:
                if before:before_network_budget()
                fail('response_budget' if read else 'write_uncertain')
        def read_body(response):
            # Charge inside the response context: a close failure must not erase
            # bytes already read. A bounded read which raises has an unknown
            # partial body, conservatively charged at its entire requested bound.
            # This changes no retry/authorization rule and never logs the body.
            try:
                raw=response.read(ro.MAX_RESPONSE+1)
            except Exception:
                self.bytes+=ro.MAX_RESPONSE+1
                raise
            if not isinstance(raw,bytes):
                self.bytes+=ro.MAX_RESPONSE+1
                fail('malformed_response' if read else 'write_uncertain')
            self.bytes+=len(raw)
            return raw
        if data is not None and len(data)>(16384 if read else 500000):before_network_budget()
        for attempt in range(3 if read else 1):
            closure_budget()
            if self.attempts>=2000 or self.clock()>=self.deadline:before_network_budget()
            if self.last is not None:self.sleep(max(0,.4-(self.clock()-self.last)))
            if self.clock()>=self.deadline:before_network_budget()
            closure_budget()
            self.last=self.clock();self.attempts+=1
            req=urllib.request.Request('https://api.notion.com/v1'+path,data=data,method=method,headers={'Authorization':'Bearer '+self._token,'Notion-Version':VERSION,'Content-Type':'application/json'})
            if not read:self.attempted.add(key)
            observed_status=None
            try:
                with self.opener.open(req,timeout=30) as response:
                    observed_status=response.status
                    if observed_status not in (200,201):
                        if read:fail('read_api_failed',observed_status)
                        raise WriteFailure('permission_denied' if observed_status in (401,403) else 'write_uncertain',observed_status,key)
                    raw=read_body(response)
                closure_budget(before=False)
                if len(raw)>ro.MAX_RESPONSE or self.bytes>ro.MAX_HTTP_BYTES:fail('response_budget' if read else 'write_uncertain')
                try:result=strict_capture_json(raw,ro.MAX_RESPONSE)
                except Exception:fail('malformed_response' if read else 'write_uncertain')
                if not isinstance(result,dict) or result.get('object')=='error':fail('read_api_failed' if read else 'write_uncertain')
                return result
            except urllib.error.HTTPError as exc:
                status=exc.code;retry=exc.headers.get('Retry-After','') if exc.headers else ''
                # Never retry write requests, even for 429 or service errors.
                if not read:
                    exc.close()
                    raise WriteFailure('permission_denied' if status in (401,403) else 'write_uncertain',status,key) from None
                try:raw=read_body(exc)
                finally:exc.close()
                closure_budget(before=False)
                if len(raw)>ro.MAX_RESPONSE or self.bytes>ro.MAX_HTTP_BYTES:fail('response_budget')
                try:err=strict_capture_json(raw,ro.MAX_RESPONSE)
                except Exception:err={}
                extra=err.get('additional_data') if isinstance(err,dict) else None
                if status in (401,403):fail('permission_denied',status)
                if isinstance(extra,dict) and extra.get('rate_limit_reason')=='public_api_request_blocked':fail('permission_denied',status)
                if status not in (429,529,500,502,503,504) or attempt==2:fail('read_api_failed',status)
                delay=int(retry) if re.fullmatch('[0-9]+',retry) else 2**attempt
                if self.clock()+delay>=self.deadline:fail('request_budget')
                self.sleep(delay)
            except WriteFailure as exc:
                if not read and type(observed_status) is int and 400<=observed_status<=599 and exc.status==observed_status and exc.request_fingerprint==key:raise
                fail('read_transport_failed' if read else 'write_uncertain')
            except NotDispatched:fail('read_transport_failed' if read else 'write_uncertain')
            except Failure:raise
            except ro.ReadFailure:fail('invalid_route')
            except (TimeoutError,urllib.error.URLError,ConnectionError):
                if not read:fail('write_uncertain')
                if attempt==2:fail('read_transport_failed')
                self.sleep(2**attempt)
            except Exception:fail('read_transport_failed' if read else 'write_uncertain')
        fail('read_transport_failed')


def wire_blocks(rows, previous_type=list_semantics.UNKNOWN,*,uploaded_pngs=None):
    count=[0]
    def split_runs(runs):
        out=[]
        for run in runs:
            text=run.get('text',{}).get('content')
            if run.get('type')=='text' and isinstance(text,str) and len(text)>2000:
                for start in range(0,len(text),2000):
                    part=deepcopy(run);part['text']['content']=text[start:start+2000];out.append(part)
            else:out.append(run)
        if len(out)>100:fail('request_budget')
        return out
    def walk(items,depth):
        if not isinstance(items,list) or len(items)>100 or depth>2:fail('request_budget')
        result=[]
        for row in items:
            if not isinstance(row,dict) or set(row)!={'type','payload','children'}:fail('invalid_blocks')
            kind=row['type'];payload=deepcopy(row['payload']);count[0]+=1
            if count[0]>1000:fail('request_budget')
            if kind=='image':
                try:payload=ro.planner.normalize_payload('image',payload)
                except Exception:fail('asset_write_contract_missing')
                if payload.get('kind')!='notion_uploaded_png' or row['children'] or not isinstance(uploaded_pngs,dict):fail('asset_write_contract_missing')
                wanted={k:payload[k] for k in ('sha256','byte_count','mime')}
                if uploaded_pngs.get(payload['upload_id'])!=wanted:fail('asset_write_contract_missing')
                result.append({'object':'block','type':'image','image':{'type':'file_upload','file_upload':{'id':payload['upload_id']},'caption':split_runs(payload['caption'])}})
                continue
            if kind not in ro.planner.KINDS or not isinstance(payload,dict) or count[0]>1000:fail('invalid_blocks')
            try:payload=ro.planner.normalize_payload(kind,payload)
            except Exception:fail('invalid_blocks')
            if 'children' in payload:fail('invalid_blocks')
            for key in ('rich_text','caption'):
                if key in payload:payload[key]=split_runs(payload[key])
            if kind=='table_row':payload['cells']=[split_runs(cell) for cell in payload['cells']]
            if kind=='code':
                if payload.pop('color','default')!='default':fail('unsupported_code_color')
            if row['children'] and kind not in ('paragraph','bulleted_list_item','numbered_list_item','quote','to_do','callout','table'):fail('invalid_children')
            if kind=='table':
                if not row['children'] or any(not isinstance(c,dict) or c.get('type')!='table_row' or not isinstance(c.get('payload'),dict) or len(c['payload'].get('cells',[]))!=payload['table_width'] for c in row['children']):fail('invalid_table')
            children=walk(row['children'],depth+1) if row['children'] else []
            if children:payload['children']=children
            result.append({'object':'block','type':kind,kind:payload})
        return result
    try: normalized=list_semantics.defaults(rows,previous_type,wire=True)
    except (KeyError,TypeError,ValueError):fail('unsupported_source_format')
    value=walk(normalized,0)
    def limits(x):
        if isinstance(x,list):
            if len(x)>100:fail('request_budget')
            for y in x:limits(y)
        elif isinstance(x,dict):
            if isinstance(x.get('content'),str) and len(x['content'])>2000:fail('request_budget')
            if isinstance(x.get('url'),str) and len(x['url'])>2000:fail('request_budget')
            for y in x.values():limits(y)
    limits(value)
    if len(native.encoded(value))>500000:fail('request_budget')
    return value

class API:
    """Executor interface. Approval of writes is separate from read allowlist."""
    def __init__(self,request,data_source_id,note_ids,*,approved_writes=(),data_source_pin=native.DATA_SOURCE_PIN,database_pin=native.DATABASE_PIN,snapshot_adapter=None,uploaded_pngs=None):
        self.snapshot_adapter=snapshot_adapter
        self.uploaded_pngs=deepcopy(uploaded_pngs) if uploaded_pngs is not None else {}
        self.request=request;self.source=native.uuid(data_source_id)
        if native.sha(self.source)!=data_source_pin:fail('wrong_parent')
        if not isinstance(note_ids,list) or not 1<=len(note_ids)<=50 or any(not isinstance(i,str) or not re.fullmatch('[0-9]{19}',i) for i in note_ids) or len(set(note_ids))!=len(note_ids):fail('invalid_cohort')
        self.ids=frozenset(note_ids);self.ds_pin=data_source_pin;self.db_pin=database_pin
        self.approved=frozenset(approved_writes);self.attempted=set();self.known_pages=set();self.known_blocks=set();self.page_roots={};self.page_root_types={}
    def schema(self):
        value=self.request('GET','/data_sources/'+self.source,None)
        if not isinstance(value,dict) or value.get('object')!='data_source' or native.uuid(value.get('id'))!=self.source:fail('wrong_parent')
        parent=value.get('parent');props=value.get('properties')
        if not isinstance(parent,dict) or native.sha(native.uuid(parent.get('database_id')))!=self.db_pin:fail('wrong_database')
        if not isinstance(props,dict) or not isinstance(props.get('笔记 ID'),dict) or props['笔记 ID'].get('type')!='rich_text':fail('wrong_schema')
        return deepcopy(props)
    def matches(self,ident):
        matches=[];cursor=None;seen=set()
        for _ in range(100):
            payload={'page_size':100,'filter':{'property':'笔记 ID','rich_text':{'equals':ident}}}
            if cursor:payload['start_cursor']=cursor
            result=self.request('POST','/data_sources/'+self.source+'/query',payload)
            if not isinstance(result,dict) or result.get('object')!='list' or not isinstance(result.get('results'),list) or len(result['results'])>100 or type(result.get('has_more')) is not bool:fail('incomplete_pagination')
            for row in result['results']:
                if not isinstance(row,dict) or row.get('object')!='page' or native.note_id(row)!=ident:fail('identity_query_invalid')
                matches.append(native.uuid(row.get('id')))
            if not result['has_more']:
                if result.get('next_cursor') is not None:fail('incomplete_pagination')
                return matches
            cursor=result.get('next_cursor')
            if not isinstance(cursor,str) or not cursor or cursor in seen or len(cursor)>1024:fail('incomplete_pagination')
            seen.add(cursor)
        fail('pagination_budget')
    def read_note(self,ident):
        if ident not in self.ids:fail('invalid_cohort')
        self.schema();matches=self.matches(ident)
        if len(matches)!=1:return {'complete':True,'native_blocks_available':True,'identity_query_complete':True,'exact_id_matches':matches,'ownership_adopted':False}
        page=matches[0]
        def selected(method,path,body):
            if method=='POST' and path=='/search':return {'object':'list','results':[{'object':'page','id':page}],'has_more':False,'next_cursor':None}
            return self.request(method,path,body)
        result=native.collect(selected,page_pins={native.sha(page):native.sha(ident)},data_source_pin=self.ds_pin,database_pin=self.db_pin)['snapshots'][ident]
        self.page_roots[page]=[x['id'] for x in result['recursive_blocks'] if x['parent_id']==page]
        self.page_root_types[page]={x['id']:x['type'] for x in result['recursive_blocks'] if x['parent_id']==page}
        self.known_pages.add(page);self.known_blocks.update(x['id'] for x in result['recursive_blocks'])
        result['asset_blockers']=['native_image_binding_required'] if any(x['type']=='image' for x in result['recursive_blocks']) else []
        return self.snapshot_adapter(result,ident) if self.snapshot_adapter is not None else result
    def _write(self,method,path,body):
        key=fingerprint(method,path,body)
        if key not in self.approved or key in self.attempted:fail('write_not_authorized_or_already_attempted')
        self.attempted.add(key)
        try:return self.request(method,path,body)
        except NotDispatched as exc:
            if exc.request_fingerprint!=key:fail('write_uncertain')
            raise
    def preflight_append(self,page_id,blocks,after):
        page=native.uuid(page_id)
        if page not in self.known_pages:fail('unread_target')
        previous=None if after is None else self.page_root_types.get(page,{}).get(native.uuid(after),list_semantics.UNKNOWN)
        payload={'children':wire_blocks(blocks,previous,uploaded_pngs=self.uploaded_pngs)}
        if after is None and self.page_roots.get(page):fail('unsupported_prepend')
        if after is not None:
            after=native.uuid(after)
            if after not in self.page_roots.get(page,[]):fail('unread_target')
            payload['after']=after
        return {'method':'PATCH','path':'/blocks/'+page+'/children','body':payload}
    def append_blocks(self,page_id,blocks,after):
        request=self.preflight_append(page_id,blocks,after)
        value=self._write(request['method'],request['path'],request['body'])
        if value.get('object')!='list' or value.get('has_more') is not False or not isinstance(value.get('results'),list) or len(value['results'])!=len(blocks):fail('write_uncertain')
        ids=[native.uuid(x.get('id')) for x in value['results'] if isinstance(x,dict) and x.get('object')=='block']
        if len(ids)!=len(blocks) or len(set(ids))!=len(ids):fail('write_uncertain')
        return ids
    def archive_block(self,block_id):
        ident=native.uuid(block_id)
        if ident not in self.known_blocks:fail('unread_target')
        value=self._write('PATCH','/blocks/'+ident,{'archived':True})
        if value.get('object')!='block' or native.uuid(value.get('id'))!=ident or value.get('archived') is not True:fail('write_uncertain')
        return ident
    def create_page(self,data_source,ident,properties,blocks):
        if native.uuid(data_source)!=self.source or ident not in self.ids:fail('wrong_parent')
        if native.note_id({'properties':properties})!=ident:fail('wrong_note_identity')
        self.schema()
        if self.matches(ident):fail('duplicate_id')
        payload={'parent':{'type':'data_source_id','data_source_id':self.source},'properties':deepcopy(properties),'children':wire_blocks(blocks,None,uploaded_pngs=self.uploaded_pngs)}
        value=self._write('POST','/pages',payload)
        if value.get('object')!='page' or native.note_id(value)!=ident:fail('write_uncertain')
        parent=value.get('parent')
        if not isinstance(parent,dict) or native.uuid(parent.get('data_source_id'))!=self.source:fail('write_uncertain')
        return native.uuid(value.get('id'))

    def update_properties(self,page_id,properties):
        import get_notion_properties as mapping
        page=native.uuid(page_id)
        if page not in self.known_pages:fail('unread_target')
        if not isinstance(properties,dict) or not properties or set(properties)-set(mapping.FIELDS):fail('invalid_approval')
        schema=mapping.schema_projection(self.schema())
        for name,value in properties.items():
            if value.get('type')!=mapping.FIELDS[name][1] or mapping.empty(value):fail('invalid_approval')
            kind=value['type']
            options=[value[kind]['name']] if kind=='select' else [x['name'] for x in value[kind]] if kind=='multi_select' else []
            if any(option not in schema[name]['options'] for option in options):fail('wrong_schema')
        result=self._write('PATCH','/pages/'+page,{'properties':deepcopy(properties)})
        if result.get('object')!='page' or native.uuid(result.get('id'))!=page or native.uuid(result.get('parent',{}).get('data_source_id'))!=self.source or result.get('archived') is True or result.get('in_trash') is True:fail('write_uncertain')
        return page
