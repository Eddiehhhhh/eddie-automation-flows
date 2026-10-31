#!/usr/bin/env python3
"""Budgeted complete API-visible Notion sources, inside the existing private writer."""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.parse

import cloud_private_checkout
import flomo_cloud_current
import notion_complete_properties as complete_properties
import sync_notion_flomo_to_raw as shared
import sync_notion_to_raw as notion
import sync_notehelper_messages_to_raw as notehelper_owner
from sync_notion_articles_to_raw import reject

BASE=Path('Raw/02 Notion/云端原文')
STATE=Path('.github/state/notion-sources-sync-state.json')
CONFIG=Path('.github/state/notion-sources-config.json')
LEGACY_SCOPE=Path('Raw/02 Notion/03 State/.local-sync-state.json')
DOUBAN_BASE=Path('Raw/14 豆瓣影视/云端原文')
DOUBAN_STATE=Path('.github/state/notion-douban-source-state.json')
PROOF_VERSION=1
RENDERER_VERSION=1
UUID=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
MAX_BATCH_BYTES=200*1024*1024


def stamp():return datetime.now(timezone.utc).isoformat()
def encoded(value):return shared.encoded(value)
def digest(raw):return shared.digest(raw)
def link(value):return urllib.parse.quote(str(value),safe='/._-')
def label(value):return str(value).replace('\\','\\\\').replace('\n',' ').replace('[','\\[').replace(']','\\]')
def read(path):return path.read_bytes() if path.exists() else None

def require(value,reason):
    if not value:raise ValueError(reason)


class BudgetExceeded(Exception):pass


class RoutedFlomo(Exception):
    def __init__(self,mid,edited,created):self.mid=mid;self.edited=edited;self.created=created


class Budget:
    def __init__(self,limits,clock=time.monotonic):
        self.clock=clock;self.started=clock();self.limits=limits;self.requests=0;self.pages=0;self.asset_bytes=0
        value=os.environ.get('NOTION_SOURCE_JOB_DEADLINE')
        require(value is None or value.isdigit(),'invalid_source_job_deadline')
        self.job_deadline=int(value) if value else None
    def check(self):
        if self.job_deadline is not None and time.time()>=self.job_deadline-300:
            raise BudgetExceeded('job_budget_reserved_for_persistence')
        if self.clock()-self.started>=self.limits['max_seconds']-min(60,self.limits['max_seconds']/4):
            raise BudgetExceeded('time_budget')
    def request(self):
        self.check()
        if self.requests>=self.limits['max_requests']:raise BudgetExceeded('request_budget')
        self.requests+=1
    def page(self):
        self.check()
        if self.pages>=self.limits['max_pages']:raise BudgetExceeded('page_budget')
        self.pages+=1
    def asset(self,raw):
        require(bool(raw),'empty_source_asset')
        require(len(raw)<=shared.MAX_ASSET_BYTES,'asset_file_budget')
        if self.asset_bytes+len(raw)>MAX_BATCH_BYTES:raise BudgetExceeded('asset_batch_budget')
        self.asset_bytes+=len(raw)


def config(root):
    path=shared.safe_path(root,CONFIG)
    value=json.loads(path.read_bytes()) if path.exists() else {'version':1,'enabled':False}
    require(value.get('version')==1 and type(value.get('enabled')) is bool,'invalid_source_configuration')
    if not value['enabled']:return value
    legacy=json.loads(shared.safe_path(root,LEGACY_SCOPE).read_bytes()).get('databases')
    require(isinstance(legacy,dict) and len(legacy)==11,'missing_legacy_eleven_scope')
    authorized={notion.normalize_notion_id(v['database_id']):v for v in legacy.values()}
    flomo_config_path=shared.safe_path(root,Path('.github/state/notion-flomo-config.json'))
    flomo_id=json.loads(flomo_config_path.read_bytes()).get('database_id') if flomo_config_path.exists() else None
    sources=value.get('sources');require(isinstance(sources,list) and len(sources)==11,'invalid_source_scope')
    ids=set();keys=set()
    for source in sources:
        ident=source.get('database_id');key=source.get('key')
        require(isinstance(ident,str) and UUID.fullmatch(ident) and ident in authorized and ident not in ids,'source_scope_not_authorized')
        require(isinstance(key,str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,47}',key) and key not in keys,'invalid_source_key')
        require(source.get('owner','generic') in ('generic','flomo','workout'),'invalid_source_owner')
        require((source.get('owner')=='flomo')==(ident==flomo_id),'flomo_source_owner_mismatch')
        expected=authorized[ident]
        require(source.get('date_property')==expected.get('date_property'),'source_date_mapping_changed')
        source['name']=expected['database_name'];ids.add(ident);keys.add(key)
    require(ids==set(authorized),'source_scope_not_authorized')
    limits=value.setdefault('batch',{})
    for field,default,maximum in [('max_pages',50,500),('per_source_pages',50,500),('max_seconds',900,1200),('max_requests',500,2000),('max_query_pages',20,100)]:
        v=limits.setdefault(field,default);require(type(v) is int and 1<=v<=maximum,'invalid_source_budget')
    return value


def metadata_fingerprint(page):
    """Catch visible property/formula changes without signed-URL churn."""
    reject(encoded(page))
    def stable(value):
        if isinstance(value,list):return [stable(item) for item in value]
        if not isinstance(value,dict):return value
        result={key:stable(item) for key,item in value.items()}
        if value.get('type')=='file' and isinstance(result.get('file'),dict):
            result['file'].pop('expiry_time',None)
            url=result['file'].get('url')
            if isinstance(url,str):
                parsed=urllib.parse.urlsplit(url);result['file']['url']=urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))
        return result
    return digest(encoded(stable({key:page.get(key) for key in ('properties','cover','icon')})))


def rich_text(rows):
    parts=[]
    for item in rows:
        kind=item.get('type');text=str((item.get('text') or {}).get('content',item.get('plain_text') or ''))
        if kind=='equation':text='$'+str(item.get('equation',{}).get('expression',''))+'$'
        if kind=='mention' and not text:text='[mention '+str(item.get('mention',{}).get('type','unknown'))+']'
        annotations=item.get('annotations') or {}
        if annotations.get('code'):
            fence='`'*max(1,1+max((len(m[0]) for m in re.finditer(r'`+',text)),default=0));text=fence+text+fence
        else:
            for key,mark in [('bold','**'),('italic','*'),('strikethrough','~~')]:
                if annotations.get(key):text=mark+text+mark
            if annotations.get('underline'):text='<u>'+text+'</u>'
        href=item.get('href') or (item.get('text') or {}).get('link',{})
        if isinstance(href,dict):href=href.get('url')
        if href:text='['+text.replace(']','\\]')+']('+str(href).replace(' ','%20')+')'
        parts.append(text)
    return ''.join(parts)


def property_text(prop):
    kind=prop.get('type');value=prop.get(kind)
    if kind in ('title','rich_text'):return rich_text(value or [])
    if kind=='files':
        return '\n'.join('- ['+label(item.get('name') or '附件')+']('+link(item.get(item.get('type'),{}).get('url',''))+')' for item in value or [])
    if kind=='relation':return '\n'.join('- [关联原页](https://www.notion.so/'+item['id'].replace('-','')+')' for item in value or [])
    if kind=='people':return '\n'.join('- '+str(item.get('name') or item.get('id','')) for item in value or [])
    if kind in ('select','status'):return str((value or {}).get('name') or '')
    if kind=='multi_select':return '、'.join(str(v.get('name','')) for v in value or [])
    if kind=='checkbox':return '是' if value else '否'
    if value is None:return ''
    if isinstance(value,(dict,list)):return json.dumps(value,ensure_ascii=False,indent=2)
    return str(value)


def render(page,blocks,source,asset_files):
    page=deepcopy(page);blocks=deepcopy(blocks);cache={}
    def transform(rows):
        for row in rows:
            data=row.get(row['type'],{})
            for key in ('rich_text','caption'):
                if row['type']!='code' and isinstance(data.get(key),list):
                    text=rich_text(data[key]);data[key]=[{'type':'text','text':{'content':text},'plain_text':text}]
            if '_children' in row:
                transform(row['_children']);cache[row['id']]=row['_children'];row['has_children']=True
    transform(blocks)
    title=next((notion.rich_text_plain_text(p.get('title') or []) for p in page['properties'].values() if p.get('type')=='title'),page['id'])
    source_date=None;date_name=None
    for candidate in source.get('date_properties',[source.get('date_property')]):
        date_prop=page['properties'].get(candidate) or {}
        if date_prop.get('type')=='date' and (date_prop.get('date') or {}).get('start'):
            source_date=date_prop['date']['start'];date_name=candidate;break
    metadata={'source_system':'notion-cloud-source','source_representation':'original_record','source_identity':'notion:'+page['id'],
        'source_database_key':source['key'],'source_database_id':source['database_id'],'source_database_name':source['name'],
        'notion_page_id':page['id'],'title':title,'notion_created_time':page.get('created_time'),
        'notion_last_edited_time':page.get('last_edited_time'),'source_date':source_date,
        'source_date_property':date_name,'source_date_basis':'notion_property' if source_date else 'notion_page_created_time',
        'property_proof_version':PROOF_VERSION,'captured_at':stamp(),'legacy_merge_status':'unmerged_legacy_preserved'}
    # Materialized assets are adjacent to the versioned readable note.
    parts=[notion.render_frontmatter(metadata,native_lists=True),'','# '+title,'',
        '- 资料日期：'+str(source_date or '未提供独立资料日期'),
        '- 原页面创建：'+str(page.get('created_time') or ''),
        '- 原页面更新：'+str(page.get('last_edited_time') or ''),'','## 属性','']
    for name,prop in page['properties'].items():
        value=property_text(prop)
        if value:parts.extend(['### '+str(name),'',value,''])
    parts.extend(['## 正文','']);parts.extend(notion.render_blocks('',blocks,children_loader=lambda _,ident:cache[ident],embed_images=True))
    # Covers/icons can be meaningful even when they are not block content.
    used='\n'.join(parts)
    extras=[]
    for area in ('cover','icon'):
        item=page.get(area) or {};url=item.get(item.get('type'),{}).get('url') if isinstance(item.get(item.get('type')),dict) else None
        if url and url not in used:extras.append('!['+area+']('+link(url)+')')
    if extras:parts.extend(['','## 页面附件','',*extras])
    text='\n'.join(parts).rstrip()+'\n'
    text=notehelper_owner.MARKER.sub(lambda match:'<!--source-notion:'+match.group(1)+'-->',text)
    raw=text.encode();reject(raw)
    name=notion.truncate_utf8_bytes(notion.safe_slug(title,page['id'][:8]),156)+'.md'
    return name,raw,metadata


def flomo_delegation(root,row,database_id):
    result={'memo_id':row['flomo_memo_id'],'notion_page_id':row['id'],'owner':'existing_flomo_chain','verified':False,'reason':'delegated_property_proof_pending'}
    try:
        path=shared.safe_path(root,shared.STATE)
        if not path.exists():return result
        group=json.loads(path.read_bytes())['pages'].get(row['flomo_memo_id'],{})
        source=group.get('source_pages',{}).get(row['id']) or (group if group.get('notion_page_id')==row['id'] else {})
        if source.get('property_proof_version')!=PROOF_VERSION or source.get('last_edited_time')!=row['last_edited_time']:return result
        payloads=[]
        for relative,expected in source.get('files',{}).items():
            raw=shared.safe_path(root/shared.STAGE,relative).read_bytes()
            require(digest(raw)==expected,'delegated_source_file_edited')
            if relative.endswith('.json'):payloads.append(json.loads(raw))
        require(len(payloads)==1,'delegated_source_proof_missing')
        proof=payloads[0].get('property_capture',{});original=proof.get('original_page',{})
        require(proof.get('proof_version')==PROOF_VERSION and original.get('id')==row['id'] and proof.get('hydrated_page',{}).get('id')==row['id'],'delegated_source_proof_missing')
        require(original.get('parent',{}).get('database_id')==database_id and shared.memo_id(original)==row['flomo_memo_id'] and shared.memo_id(proof['hydrated_page'])==row['flomo_memo_id'],'delegated_source_identity_changed')
        result.update(verified=True,reason=None)
    except Exception:result['reason']='delegated_source_proof_not_verified'
    return result


def owned_files(record):
    result={}
    for version in record.get('versions',{}).values():result.update(version)
    return result


def check_files(root,files,base=BASE):
    for relative,expected in files.items():
        path=shared.safe_path(root/base,relative)
        require(path.is_file() and digest(path.read_bytes())==expected,'source_owned_content_edited')


def block_tree(page_id,request,depth=0,ancestors=()):
    require(depth<=30 and page_id not in ancestors,'source_block_cycle')
    rows=[];cursor=None;cursors=set();ids=set()
    while True:
        path='/blocks/'+page_id+'/children?'+urllib.parse.urlencode({'page_size':100,**({'start_cursor':cursor} if cursor else {})})
        value=request('GET',path);require(isinstance(value.get('results'),list) and type(value.get('has_more')) is bool,'source_block_pagination_shape')
        for row in value['results']:
            ident=row.get('id');require(isinstance(ident,str) and ident and ident not in ids and isinstance(row.get('type'),str),'source_block_identity')
            require(row['type']!='unsupported','unsupported_source_block');ids.add(ident)
            node=deepcopy(row);synced=(row.get('synced_block') or {}).get('synced_from') or {}
            if row.get('has_children') or synced.get('block_id'):
                node['_children']=block_tree(synced.get('block_id') or ident,request,depth+1,(*ancestors,page_id))
            rows.append(node)
        if not value['has_more']:
            require(value.get('next_cursor') is None,'source_block_terminal_conflict');return rows
        cursor=value.get('next_cursor');require(isinstance(cursor,str) and cursor and cursor not in cursors,'source_block_cursor_incomplete');cursors.add(cursor)


def download_checked(url,budget,downloader):
    parsed=urllib.parse.urlsplit(url)
    require(parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password and parsed.port in (None,443),'unsafe_source_asset_url')
    budget.request();raw=downloader(url);budget.asset(raw);reject(raw);return raw


def materialize(value,budget,downloader):
    files={};cache={};urls={}
    def download(url):
        if url not in cache:
            raw=download_checked(url,budget,downloader);cache[url]=raw;urls[digest(raw)]=url
        return cache[url]
    rendered=shared.materialize_assets(value,files,download)
    renames={};result={}
    for relative,raw in files.items():
        new='assets/'+digest(raw)+flomo_cloud_current.asset_suffix(raw,urls[digest(raw)])
        renames[relative]=new;result[new]=raw
    def rewrite(obj):
        if isinstance(obj,list):return [rewrite(v) for v in obj]
        if isinstance(obj,dict):return {k:rewrite(v) for k,v in obj.items()}
        return renames.get(obj,obj) if isinstance(obj,str) else obj
    return rewrite(rendered),result


def capture_page(root,page,source,prior,request,budget,downloader,base=BASE):
    """Return immutable page files/receipt; caller owns state and the private lock."""
    check_files(root,owned_files(prior),base)
    ident=page['id'];before=request('GET','/pages/'+ident)
    parent=before.get('parent',{});actual_parent=parent.get('database_id') or parent.get('data_source_id')
    require(before.get('id')==ident and actual_parent==source['database_id'],'source_parent_identity_changed')
    require(notion.parse_time(before.get('last_edited_time')) is not None,'source_edit_time_missing')
    require(notion.parse_time(before.get('created_time')) is not None,'source_created_time_missing')
    reject(encoded(before))
    if source.get('owner')=='flomo':
        mid=shared.memo_id(before)
        if mid:raise RoutedFlomo(mid,before['last_edited_time'],before['created_time'])
    full=complete_properties.hydrate_page(before,request);blocks=block_tree(ident,request)
    original={'original_page':full['original_page'],'property_responses':full['property_responses'],'hydrated_page':full['hydrated_page'],
        'blocks':blocks,'property_proof_version':full['proof_version'],'source_database_id':source['database_id']}
    reject(encoded(original))
    material,files=materialize({'page':full['hydrated_page'],'blocks':blocks},budget,downloader)
    original['materialized']=deepcopy(material)
    original['materialized_assets']={name:{'sha256':digest(raw),'size':len(raw)} for name,raw in files.items()}
    filename,markdown,metadata=render(material['page'],material['blocks'],source,files)
    # Preserve image/file embeds that arrived as literal Markdown or HTML text.
    text=markdown.decode();embedded=set(re.findall(r'!\[[^\]\n]*\]\((https?://[^\s)]+)\)',text))
    embedded.update(re.findall(r'<img\b[^>]*\bsrc=["\'](https?://[^"\']+)',text,re.I))
    embedded.update(re.findall(r'📎\s*\[[^\]\n]*\]\((https?://[^\s)]+)\)',text))
    for url in sorted(embedded):
        raw=download_checked(url,budget,downloader)
        relative='assets/'+digest(raw)+flomo_cloud_current.asset_suffix(raw,url);files[relative]=raw
        original.setdefault('literal_assets',{})[url]={'path':relative,'sha256':digest(raw),'size':len(raw)}
        text=text.replace(url,link(relative))
    text=notehelper_owner.MARKER.sub(lambda match:'<!--source-notion:'+match.group(1)+'-->',text)
    markdown=text.encode();reject(markdown)
    files['source.json']=encoded(original);files[filename]=markdown
    after=request('GET','/pages/'+ident)
    require(after.get('id')==ident and after.get('last_edited_time')==before['last_edited_time'] and metadata_fingerprint(after)==metadata_fingerprint(before),'source_changed_during_capture')
    require((after.get('parent',{}).get('database_id') or after.get('parent',{}).get('data_source_id'))==source['database_id'],'source_parent_identity_changed')
    version=digest(encoded({p:digest(raw) for p,raw in files.items()}));prefix=Path('versions')/source['key']/ident/version
    versioned={str(prefix/name):raw for name,raw in files.items()}
    receipt={'notion_page_id':ident,'source_identity':'notion:'+ident,'source_database_id':source['database_id'],
        'last_edited_time':before['last_edited_time'],'metadata_fingerprint':metadata_fingerprint(before),'property_observed_at':stamp(),'notion_created_time':before.get('created_time'),
        'source_date':metadata['source_date'],'renderer_version':RENDERER_VERSION,'property_proof_version':full['proof_version'],'capture_complete':True,
        'current_path':str(prefix/filename),'title':metadata['title'],'files':{p:digest(raw) for p,raw in versioned.items()},
        'versions':{**prior.get('versions',{}),version:{p:digest(raw) for p,raw in versioned.items()}}}
    return receipt,versioned


def safe_failure(exc):
    if isinstance(exc,UnicodeError):return 'source_invalid_utf8'
    reason=str(exc)
    if isinstance(exc,ValueError) and re.fullmatch(r'[a-z_0-9]+',reason):return reason
    cause=getattr(exc,'__cause__',None)
    return 'source_http_'+str(cause.code) if hasattr(cause,'code') else 'source_capture_failed'


def source_coverage(record,held,source_key):
    inventory=record.get('inventory',{});rows=inventory.get('records',[])
    captured=sum(1 for row in rows if row['id'] not in held and record['pages'].get(row['id'],{}).get('capture_complete') and record['pages'][row['id']].get('property_proof_version')==PROOF_VERSION and record['pages'][row['id']].get('renderer_version')==RENDERER_VERSION and record['pages'][row['id']].get('last_edited_time')==row['last_edited_time'] and record['pages'][row['id']].get('metadata_fingerprint')==row.get('metadata_fingerprint'))
    verified=sum(bool(record.get('delegations',{}).get(row['id'],{}).get('verified')) for row in rows if row.get('flomo_memo_id'))
    delegated=sum(bool(row.get('flomo_memo_id')) for row in rows)
    result={'observed':len(rows),'captured':captured,'delegated_verified':verified,'delegated_pending':delegated-verified,
        'pending_observed':len(rows)-captured-verified,'held_observed':sum(row['id'] in held for row in rows),
        'inventory_complete':bool(inventory.get('complete')),'unobserved_remainder_unknown':not inventory.get('complete',False)}
    result['complete']=result['inventory_complete'] and captured+verified==len(rows) and source_key not in held
    return result


def collect(root,settings,api_key,*,apply=False,source_keys=None,request=None,downloader=shared.download_asset,budget=None,base=BASE,state_file=STATE):
    root=Path(root)
    if apply:cloud_private_checkout.verify_private_checkout(root,os.environ.get('PRIVATE_VAULT_TOKEN'))
    budget=budget or Budget(settings['batch']);last_request=[0.0]
    def call(method,path,payload=None):
        require(method=='GET' or (method=='POST' and re.fullmatch(r'/databases/[0-9a-f-]+/query',path)),'source_mutation_forbidden')
        budget.request()
        try:
            if request is None:
                time.sleep(max(0,.7-(time.monotonic()-last_request[0])));last_request[0]=time.monotonic()
                token=notion.READ_RETRIES.set(0)
                try:return notion.notion_request(api_key,method,path,payload)
                finally:notion.READ_RETRIES.reset(token)
            return request(method,path,payload)
        except Exception as exc:
            if getattr(getattr(exc,'__cause__',None),'code',None)==429:raise BudgetExceeded('source_rate_limited') from None
            raise
    with shared.writer_lock(root):
        state_path=shared.safe_path(root,state_file);before=read(state_path);state=json.loads(before) if before else {'version':1,'sources':{},'next_source':0}
        require(state.get('version')==1 and isinstance(state.get('sources'),dict),'invalid_source_state')
        def persist(candidate):
            nonlocal state,before
            if not apply:state=candidate;return
            require(read(state_path)==before,'source_state_changed')
            raw=encoded(candidate);reject(raw);shared.atomic_write(state_path,raw);state,before=candidate,raw
        sources=settings['sources'];keys={s['key'] for s in sources}
        require(set(state['sources'])<=keys,'source_state_scope_changed')
        require(not source_keys or set(source_keys)<=keys,'unknown_selected_source')
        ordered=sources[state.get('next_source',0):]+sources[:state.get('next_source',0)]
        if source_keys:ordered=[s for s in sources if s['key'] in source_keys]
        report={'written':0,'unchanged':0,'held':{},'pending':{},'delegated':{},'soft_exit':None,'selected_sources':[s['key'] for s in ordered],
            'legacy_policy':'preserved_unmerged','configured_scope_complete':False,'selected_scope_complete':False}
        for source in ordered:
            key=source['key'];record=deepcopy(state['sources'].get(key,{'database_id':source['database_id'],'pages':{}}))
            require(record['database_id']==source['database_id'],'source_state_identity_changed')
            def save_source():
                candidate={**state,'sources':{**state['sources'],key:deepcopy(record)}};persist(candidate)
            try:
                budget.check()
                inventory=record.get('inventory')
                if inventory and inventory.get('complete') and all(record['pages'].get(row['id'],{}).get('last_edited_time')==row['last_edited_time'] and record['pages'].get(row['id'],{}).get('property_proof_version')==PROOF_VERSION and record['pages'].get(row['id'],{}).get('renderer_version')==RENDERER_VERSION and record['pages'].get(row['id'],{}).get('metadata_fingerprint')==row.get('metadata_fingerprint') for row in inventory['records'] if not row.get('flomo_memo_id')):
                    record.pop('inventory',None);inventory=None
                if inventory is None:
                    database=call('GET','/databases/'+source['database_id']);require(database.get('id')==source['database_id'],'source_database_identity_changed')
                    reject(encoded(database))
                    date_names=source.get('date_properties') or ([source['date_property']] if source.get('date_property') else [])
                    if date_names:require(any(database.get('properties',{}).get(name,{}).get('type')=='date' for name in date_names),'source_date_property_missing_or_changed')
                    record['database_schema']=database
                    record['coverage']={'complete':False,'inventory_complete':False}
                    inventory={'records':[],'cursor':None,'seen_cursors':[],'query_pages':0,'page_sizes':[],'complete':False,'started_at':stamp()};record['inventory']=inventory
                observed={row['id'] for row in inventory['records']}
                for _ in range(settings['batch']['max_query_pages']):
                    if inventory['complete']:break
                    payload={'page_size':100,'sorts':[{'timestamp':'last_edited_time','direction':'descending'}]}
                    if inventory['cursor']:payload['start_cursor']=inventory['cursor']
                    value=call('POST','/databases/'+source['database_id']+'/query',payload)
                    require(isinstance(value.get('results'),list) and type(value.get('has_more')) is bool,'source_inventory_shape')
                    additions=[];new_ids=set()
                    for page in value['results']:
                        ident=page.get('id');edited=page.get('last_edited_time')
                        require(isinstance(ident,str) and UUID.fullmatch(ident) and ident not in observed and ident not in new_ids and notion.parse_time(edited) is not None,'source_inventory_identity')
                        row={'id':ident,'last_edited_time':edited,'created_time':page.get('created_time') if notion.parse_time(page.get('created_time')) is not None else None}
                        try:row['metadata_fingerprint']=metadata_fingerprint(page)
                        except ValueError as exc:row['route_hold']=safe_failure(exc)
                        if source.get('owner')=='flomo' and not row.get('route_hold'):
                            try:
                                mid=shared.memo_id(page)
                                if mid:row['flomo_memo_id']=mid
                            except ValueError as exc:row['route_hold']=safe_failure(exc)
                        additions.append(row);new_ids.add(ident)
                    next_cursor=value.get('next_cursor')
                    if value['has_more']:
                        require(isinstance(next_cursor,str) and next_cursor and next_cursor not in inventory['seen_cursors'],'source_inventory_cursor_incomplete')
                    else:require(next_cursor is None,'source_inventory_terminal_conflict')
                    inventory['records'].extend(additions);observed.update(new_ids)
                    inventory['query_pages']+=1;inventory['page_sizes'].append(len(value['results']))
                    if value['has_more']:
                        inventory['cursor']=next_cursor;inventory['seen_cursors'].append(next_cursor)
                    else:
                        inventory['cursor']=None;inventory['complete']=True;inventory['terminal_has_more']=False;inventory['finished_at']=stamp()
                    save_source()
                if not inventory['complete']:
                    report['pending'][key]='inventory_query_page_budget';record['coverage']=source_coverage(record,report['held'],key);save_source();continue
                record['retained_not_observed']=sorted(set(record['pages'])-observed)
                attempted=0
                for row in inventory['records']:
                    budget.check()
                    ident=row['id'];prior=record['pages'].get(ident,{})
                    if row.get('route_hold'):
                        report['held'][ident]=row['route_hold'];continue
                    if row.get('flomo_memo_id'):
                        delegated=flomo_delegation(root,row,source['database_id'])
                        record.setdefault('delegations',{})[ident]=delegated
                        report['delegated'][ident]=delegated
                        continue
                    try:
                        check_files(root,owned_files(prior),base)
                        if prior.get('capture_complete') and prior.get('property_proof_version')==PROOF_VERSION and prior.get('renderer_version')==RENDERER_VERSION and prior.get('last_edited_time')==row['last_edited_time'] and prior.get('metadata_fingerprint')==row.get('metadata_fingerprint'):
                            report['unchanged']+=1;continue
                        if not apply:
                            report['pending'][ident]='dry_run';continue
                        if attempted>=settings['batch']['per_source_pages']:
                            report['pending'][key]='per_source_page_budget';break
                        budget.page();attempted+=1
                        receipt,files=capture_page(root,row,source,prior,call,budget,downloader,base)
                        require(read(state_path)==before,'source_state_changed');check_files(root,owned_files(prior),base)
                        for path,raw in files.items():shared.immutable_write(shared.safe_path(root/base,path),raw)
                        row['last_edited_time']=receipt['last_edited_time'];row['created_time']=receipt['notion_created_time'];row['metadata_fingerprint']=receipt['metadata_fingerprint']
                        check_files(root,owned_files(prior),base)
                        record['pages'][ident]=receipt;save_source();report['written']+=1
                    except RoutedFlomo as routed:
                        row.update(flomo_memo_id=routed.mid,last_edited_time=routed.edited,created_time=routed.created)
                        delegated=flomo_delegation(root,row,source['database_id']);record.setdefault('delegations',{})[ident]=delegated;report['delegated'][ident]=delegated;save_source()
                    except BudgetExceeded:raise
                    except Exception as exc:
                        report['held'][ident]=safe_failure(exc)
                        if str(exc)=='source_state_changed':raise
                record['coverage']=source_coverage(record,report['held'],key)
                save_source()
            except BudgetExceeded as exc:
                report['soft_exit']=str(exc);report['pending'][key]=str(exc);record['coverage']=source_coverage(record,report['held'],key);save_source();break
            except Exception as exc:
                if str(exc)=='source_state_changed':raise
                report['held'][key]=safe_failure(exc);record['coverage']=source_coverage(record,report['held'],key);save_source()
            finally:
                candidate={**state,'next_source':(sources.index(source)+1)%len(sources)};persist(candidate)
        report['selected_scope_complete']=all(state['sources'].get(s['key'],{}).get('coverage',{}).get('complete') for s in ordered) and not report['held'] and not report['pending'] and not report['soft_exit']
        report['configured_scope_complete']=all(state['sources'].get(s['key'],{}).get('coverage',{}).get('complete') for s in sources)
        report['api_requests']=budget.requests;report['page_attempts']=budget.pages;report['asset_bytes']=budget.asset_bytes;report['elapsed_seconds']=round(max(0,budget.clock()-budget.started),3)
        if apply:
            index=['---','stage_only: true','---','','# Notion 云端完整原资料','','旧稿及人工内容保留；下列链接指向已保存的当前版本。','']
            for source in sources:
                source_state=state['sources'].get(source['key'],{});coverage=source_state.get('coverage',{})
                progress=('已保存 '+str(coverage.get('captured',0))+' / 本轮已发现 '+str(coverage.get('observed',0))) if coverage else '等待分批同步'
                if coverage and not coverage.get('complete'):progress+='；仍有未完成内容'
                if coverage.get('unobserved_remainder_unknown'):progress+='；来源清点尚未结束'
                if coverage.get('delegated_pending'):progress+='；flomo原链完整属性证明待补 '+str(coverage['delegated_pending'])+' 页'
                index.extend(['## '+source['name'],'',progress,''])
                for ident,entry in sorted(state['sources'].get(source['key'],{}).get('pages',{}).items()):index.append('- ['+label(entry['title'])+']('+link(entry['current_path'])+')'+('（本轮源未观察；原稿保留）' if ident in source_state.get('retained_not_observed',[]) else ''))
                index.append('')
            text=notehelper_owner.MARKER.sub(lambda match:'<!--source-notion:'+match.group(1)+'-->','\n'.join(index))
            raw=text.encode();reject(raw);path=shared.safe_path(root,base/'index.md');existing=read(path)
            if existing is not None and digest(existing)!=state.get('index_sha256') and existing!=raw:
                report['held']['index']='source_index_edited';report['selected_scope_complete']=False;report['configured_scope_complete']=False
            else:
                require(read(path)==existing,'source_index_changed');shared.atomic_write(path,raw);persist({**state,'index_sha256':digest(raw)})
            persist({**state,'coverage':report})
        return report


def active_selection(root,base=BASE,state_file=STATE):
    root=Path(root);result={'current':set(),'edited':set(),'held':{}}
    try:
        path=shared.safe_path(root,state_file)
        if not path.exists():return result
        state=json.loads(path.read_bytes());require(state.get('version')==1,'invalid_source_state')
        for source in state['sources'].values():
            for ident,entry in source['pages'].items():
                relative=Path(entry['current_path']);require(relative.is_relative_to('versions') and relative.suffix=='.md','invalid_source_current_path')
                current=str(base/relative);result['current'].add(current);path=shared.safe_path(root,current)
                if not path.is_file() or digest(path.read_bytes())!=entry['files'].get(str(relative)):
                    result['edited'].add(current);result['held'][ident]='source_current_edited'
    except Exception:result['held']['state']='invalid_source_state'
    return result


def all_active_selection(root):
    result={'current':set(),'edited':set(),'held':{}}
    for base,state_file in ((BASE,STATE),(DOUBAN_BASE,DOUBAN_STATE)):
        current=active_selection(root,base,state_file)
        for key in ('current','edited'):result[key].update(current[key])
        result['held'].update({str(state_file)+':'+key:value for key,value in current['held'].items()})
    return result


def eligible_path(root,path,selection=None):
    if not any(path.is_relative_to(root/base) for base in (BASE,DOUBAN_BASE)):return True
    selection=selection if selection is not None else all_active_selection(root)
    return str(path.relative_to(root)) in selection['current']


def verify_remote(root,base=BASE,state_file=STATE):
    """Read every owned byte, with a constant number of Git subprocesses."""
    root=Path(root);cloud_private_checkout.verify_private_checkout(root,os.environ.get('PRIVATE_VAULT_TOKEN'))
    def git(*args):return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,timeout=90).stdout
    git('fetch','origin','main');raw=shared.safe_path(root,state_file).read_bytes();state=json.loads(raw)
    expected={str(state_file):digest(raw),str(base/'index.md'):state['index_sha256']}
    index=shared.safe_path(root,base/'index.md').read_text(encoding='utf-8')
    targets=re.findall(r'^- \[(?:\\.|[^\]\\])*\]\(([^)\n]+)\)',index,re.M)
    currents=[entry['current_path'] for source in state['sources'].values() for entry in source['pages'].values()]
    require(len(targets)==len(currents) and {urllib.parse.unquote(v) for v in targets}==set(currents),'source_index_current_mismatch')
    for source in state['sources'].values():
        for entry in source['pages'].values():expected.update({str(base/p):value for p,value in owned_files(entry).items()})
    trees=[]
    for ref in ('HEAD','origin/main'):
        tree={}
        for row in git('ls-tree','-rz',ref,'--',str(base),str(state_file)).split(b'\0'):
            if not row:continue
            metadata,name=row.split(b'\t',1);mode,kind,oid=metadata.split()
            require(mode==b'100644' and kind==b'blob','source_remote_file_mode')
            tree[os.fsdecode(name)]=oid.decode()
        trees.append(tree)
    # This also binds retained/unregistered physical history to the fresh remote.
    require(trees[0]==trees[1],'source_remote_tree_mismatch')
    objects={}
    for relative,sha in expected.items():
        path=shared.safe_path(root,relative);size=path.stat().st_size
        local=hashlib.sha256();oid=hashlib.sha1(b'blob '+str(size).encode()+b'\0')
        with path.open('rb') as stream:
            while chunk:=stream.read(1024*1024):local.update(chunk);oid.update(chunk)
        require(local.hexdigest()==sha and trees[1].get(relative)==oid.hexdigest(),'source_remote_readback_failed')
        if oid.hexdigest() in objects:require(objects[oid.hexdigest()]==sha,'source_remote_object_conflict')
        objects[oid.hexdigest()]=sha
    process=subprocess.Popen(['git','-C',str(root),'cat-file','--batch'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    writer_errors=[]
    def send():
        try:
            for oid in objects:process.stdin.write((oid+'\n').encode())
            process.stdin.close()
        except Exception:writer_errors.append(True)
    writer=threading.Thread(target=send,daemon=True);writer.start()
    timer=threading.Timer(600,process.kill);timer.daemon=True;timer.start()
    try:
        for expected_oid,sha in objects.items():
            header=process.stdout.readline().split()
            require(len(header)==3 and header[0].decode()==expected_oid and header[1]==b'blob','source_remote_object_header')
            remaining=int(header[2]);actual=hashlib.sha256();oid=hashlib.sha1(b'blob '+str(remaining).encode()+b'\0')
            while remaining:
                chunk=process.stdout.read(min(1024*1024,remaining));require(bool(chunk),'source_remote_object_short_read')
                actual.update(chunk);oid.update(chunk);remaining-=len(chunk)
            require(process.stdout.read(1)==b'\n' and actual.hexdigest()==sha and oid.hexdigest()==expected_oid,'source_remote_readback_failed')
        writer.join(timeout=5);require(process.wait(timeout=30)==0 and not writer_errors,'source_remote_batch_failed')
    finally:
        timer.cancel()
        if process.poll() is None:process.kill();process.wait()
        process.stdout.close()
    require(shared.safe_path(root,state_file).read_bytes()==raw,'source_state_changed');return True


def douban_source_settings(root):
    settings=config(root)
    value=settings.get('dedicated_douban',{})
    require(isinstance(value,dict) and type(value.get('enabled',False)) is bool,'invalid_douban_source_configuration')
    if not settings['enabled'] or not value.get('enabled'):return None
    ids=set()
    for path in (root/DOUBAN_BASE.parent).rglob('*.md'):
        if path.is_relative_to(root/DOUBAN_BASE):continue
        text=path.read_text(encoding='utf-8')
        if notion.extract_frontmatter_value(text,'source_type')=='douban_movie':
            ident=notion.extract_frontmatter_value(text,'database_id')
            if ident:ids.add(ident)
    require(len(ids)==1 and value.get('database_id') in ids,'douban_source_not_existing_owner')
    require(value.get('date_properties')==['添加日期','日期'],'douban_source_date_mapping_changed')
    limits={**settings['batch'],'max_seconds':min(settings['batch']['max_seconds'],300),'max_requests':min(settings['batch']['max_requests'],250)}
    return {'sources':[{'key':'douban','name':'豆瓣影视','database_id':value['database_id'],'date_properties':value['date_properties'],'owner':'generic'}],'batch':limits}


def run_douban_source(root,api_key,*,apply=False,max_pages=None,request=None,downloader=shared.download_asset):
    settings=douban_source_settings(root)
    require(settings is not None,'douban_source_configuration_disabled')
    if max_pages is not None:
        require(1<=max_pages<=500,'invalid_source_page_budget');settings['batch']['max_pages']=max_pages
    return collect(root,settings,api_key,apply=apply,request=request,downloader=downloader,base=DOUBAN_BASE,state_file=DOUBAN_STATE)


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--source-key',action='append');p.add_argument('--max-pages',type=int);p.add_argument('--check-config',action='store_true');p.add_argument('--check-douban-config',action='store_true');p.add_argument('--verify-remote-state',action='store_true');args=p.parse_args();root=Path(os.environ['VAULT_ROOT'])
    if args.verify_remote_state:
        for base,state_file in ((BASE,STATE),(DOUBAN_BASE,DOUBAN_STATE)):
            if shared.safe_path(root,state_file).exists():verify_remote(root,base,state_file)
        return
    settings=config(root)
    if args.check_douban_config:
        with open(os.environ['GITHUB_OUTPUT'],'a') as stream:stream.write('enabled='+str(douban_source_settings(root) is not None).lower()+'\n')
        return
    if args.check_config:
        with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
            stream.write('enabled='+str(settings['enabled']).lower()+'\n')
        return
    require(settings['enabled'],'source_configuration_disabled')
    if args.max_pages is not None:require(1<=args.max_pages<=500,'invalid_source_page_budget');settings['batch']['max_pages']=args.max_pages
    report=collect(root,settings,notion.load_notion_api_key(),apply=args.apply,source_keys=args.source_key)
    print(json.dumps({k:report[k] for k in ('written','unchanged','selected_scope_complete','configured_scope_complete','soft_exit','api_requests','page_attempts')}))
    if not report['selected_scope_complete']:raise SystemExit(2)

if __name__=='__main__':main()
