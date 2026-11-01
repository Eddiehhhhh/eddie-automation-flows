"""Render captured flomo evidence into readable, immutable Raw/01 current notes.

Local-only transformation inside the existing verified private flomo writer.
No source API, independent writer, scheduling, deletion or legacy modification.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import urllib.parse

import sync_flomo_to_raw as flomo
import sync_notion_flomo_to_raw as shared
import flomo_original_source as original

BASE = shared.CURRENT_BASE
STATE = Path('.github/state/flomo-cloud-current-state.json')
RENDERER_VERSION = 1


def reject(raw):
    text=raw.decode('utf-8',errors='ignore')
    if shared.CREDENTIAL_SHAPE.search(text) or re.search(r'(?<![A-Za-z0-9_])(?:ntn_[A-Za-z0-9]{40,}|secret_[A-Za-z0-9]{32,})(?![A-Za-z0-9_])',text):
        raise ValueError('potential_credential_in_source')


def link(path): return urllib.parse.quote(str(path).replace(os.sep,'/'),safe='/._-')


def label(text): return str(text).replace('\n',' ').replace('[','\\[').replace(']','\\]')


class ReadableHTML(HTMLParser):
    """Preserve flomo's editor text/order, paragraphs, lists, links and code."""
    def __init__(self,content,assets):
        super().__init__(convert_charrefs=True)
        self.output=[]; self.plain=[]; self.assets=assets; self.anchors=[]; self.lists=[];self.pre=0;self.quotes=[];self.inlinecode=0
        self.fence='`'*max(3,1+max((len(m[0]) for m in re.finditer(r'`+',content)),default=0))
        self.feed(content); self.close()
    def handle_starttag(self,tag,attrs):
        data=dict(attrs)
        if tag in ('p','div','br','li','tr') or re.fullmatch('h[1-6]',tag):self.plain.append('\n')
        if tag in ('p','div','section','article','table','tr','blockquote'): self.output.append('\n\n')
        if re.fullmatch('h[1-6]',tag): self.output.append('\n\n'+'#'*int(tag[1])+' ')
        if tag=='blockquote':self.quotes.append(len(self.output))
        if tag=='br': self.output.append('\n')
        if tag in ('ul','ol'): self.lists.append([tag,0]); self.output.append('\n')
        if tag=='li':
            if self.lists:self.lists[-1][1]+=1
            marker=str(self.lists[-1][1])+'. ' if self.lists and self.lists[-1][0]=='ol' else '- '
            self.output.append('\n'+'  '*max(0,len(self.lists)-1)+marker)
        if tag in ('td','th'):self.output.append('\t')
        if tag in ('strong','b'):self.output.append('**')
        if tag in ('em','i'):self.output.append('*')
        if tag in ('del','s'):self.output.append('~~')
        if tag=='pre':self.pre+=1;self.output.append('\n\n'+self.fence+'\n')
        if tag=='code' and not self.pre:self.inlinecode+=1;self.output.append('`')
        if tag in ('script','style','iframe'):self.output.append('\n'+self.fence+'html\n<'+tag+'>')
        if tag=='a':
            url=data.get('href') or ''
            self.anchors.append(url);self.output.append('[')
        if tag=='img':
            url=html.unescape(data.get('src') or '')
            target=self.assets.get(url)
            self.output.append('!['+label(data.get('alt') or '')+']('+target+')' if target else
                '[图片尚未保存]('+url+')' if url.startswith(('https://','http://')) else '[图片引用未解析]')
    def handle_endtag(self,tag):
        if tag=='blockquote' and self.quotes:
            start=self.quotes.pop();text=''.join(self.output[start:]).strip()
            self.output[start:]=['\n\n'+'\n'.join('> '+line for line in text.splitlines())+'\n\n']
        if tag in ('strong','b'):self.output.append('**')
        if tag in ('em','i'):self.output.append('*')
        if tag in ('del','s'):self.output.append('~~')
        if tag=='code' and not self.pre:self.output.append('`');self.inlinecode=max(0,self.inlinecode-1)
        if tag=='pre':self.pre=max(0,self.pre-1);self.output.append('\n'+self.fence+'\n\n')
        if tag in ('script','style','iframe'):self.output.append('</'+tag+'>\n'+self.fence+'\n')
        if tag=='a':
            url=self.anchors.pop() if self.anchors else ''
            self.output.append(']('+self.assets.get(url,url)+')' if urllib.parse.urlsplit(url).scheme in
                               ('http','https','mailto','flomo') else ']')
        if tag in ('ul','ol') and self.lists:self.lists.pop();self.output.append('\n')
        if tag in ('p','div','section','article','table','tr','blockquote') or re.fullmatch('h[1-6]',tag):
            self.output.append('\n\n')
    def handle_data(self,data):
        self.plain.append(data)
        if not self.pre and not self.inlinecode:
            data=data.replace('<','&lt;').replace('>','&gt;')
        if not self.pre and not self.inlinecode and not self.anchors:
            for url,target in self.assets.items():data=data.replace(url,'['+label(url)+']('+target+')')
        self.output.append(data)
    def markdown(self):return ''.join(self.output).strip()


def sources(record):
    result=dict(record.get('source_pages',{}))
    if record.get('notion_page_id') and record['notion_page_id'] not in result:
        result[record['notion_page_id']]={key:value for key,value in record.items() if key!='source_pages'}
    return result


def owned_files(record):
    result=dict(record.get('files',{}))
    for files in record.get('versions',{}).values():result.update(files)
    return result


def check_files(root,files):
    for rel,sha in files.items():
        path=shared.safe_path(root,rel)
        if not path.is_file() or shared.digest(path.read_bytes())!=sha:raise ValueError('owned_content_edited')


def snapshot(root,record,suffix,evidence):
    match=[rel for rel in record['files'] if rel.endswith(suffix)]
    if len(match)!=1:raise ValueError('source_snapshot_incomplete')
    rel=match[0]; path=shared.safe_path(root,shared.STAGE/rel)
    raw=path.read_bytes()
    if shared.digest(raw)!=record['files'][rel]:raise ValueError('source_snapshot_edited')
    reject(raw);evidence[str(shared.STAGE/rel)]=shared.digest(raw)
    return json.loads(raw),Path(rel).parent


def asset_suffix(raw,url):
    for signature,suffix in [(b'\x89PNG','.png'),(b'\xff\xd8\xff','.jpg'),(b'GIF8','.gif'),(b'%PDF-','.pdf')]:
        if raw.startswith(signature):return suffix
    if raw[:4]==b'RIFF' and raw[8:12]==b'WEBP':return '.webp'
    suffix=Path(urllib.parse.urlsplit(url).path).suffix.lower()
    return suffix if re.fullmatch(r'\.[a-z0-9]{1,8}',suffix) else '.bin'


def add_asset(root,relative,expected,url,files,evidence):
    path=shared.safe_path(root,shared.STAGE/relative);raw=path.read_bytes()
    if shared.digest(raw)!=expected:raise ValueError('source_asset_edited')
    reject(raw)
    rel=str(BASE/'_assets'/(shared.digest(raw)+asset_suffix(raw,url)))
    files[rel]=raw;evidence[str(shared.STAGE/relative)]=expected
    return '../../../_assets/'+link(Path(rel).name)


def legacy_assets(root,relative,raw):
    """Resolve local Markdown/wiki embeds without reading outside the vault."""
    text=raw.decode();result={}
    targets=[(value,marker=='!') for marker,value in re.findall(r'(!?)\[[^\]]*\]\(<?([^\n>]+?)>?\)',text)]
    targets += [(value,True) for value in re.findall(r'!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]',text)]
    for value,embedded in targets:
        target=value.strip()
        if urllib.parse.urlsplit(target).scheme or target.startswith('#'):continue
        target=urllib.parse.unquote(target.split('#',1)[0])
        if not target:continue
        local=(root/relative).parent/target
        if not local.exists():local=root/target
        if not local.exists():continue
        try:rel=local.resolve().relative_to(root.resolve())
        except ValueError:raise ValueError('legacy_asset_outside_root') from None
        for ancestor in (local,*local.parents):
            if ancestor.is_symlink():raise ValueError('legacy_asset_symlink')
            if ancestor==root:break
        if not rel.is_relative_to(BASE.parent):
            if embedded:raise ValueError('legacy_asset_outside_source')
            continue
        if any(part in ('.git','.github','.obsidian','.agents','.env') for part in rel.parts):
            raise ValueError('legacy_asset_private_configuration')
        path=shared.safe_path(root,rel)
        if not path.is_file():continue
        if path.stat().st_size>shared.MAX_ASSET_BYTES:raise ValueError('legacy_asset_read_budget')
        data=path.read_bytes();reject(data)
        result[value]=(str(rel),data)
    return result


def original_body(root,record,files,evidence):
    memo,parent=snapshot(root,record,'/original.json',evidence)
    metadata,_=snapshot(root,record,'/metadata.json',evidence)
    local={}
    for asset in metadata.get('assets',[]):
        relative=str(parent/asset['path'])
        if relative not in record['files']:raise ValueError('source_asset_incomplete')
        local[asset['source_url']]=add_asset(root,relative,record['files'][relative],asset['source_url'],files,evidence)
    parser=ReadableHTML(memo['content'],local)
    body=parser.markdown()
    attachments=[]
    for item in memo.get('files',[]):
        url=item.get('url',''); target=local.get(url,url)
        image=Path(target).suffix.lower() in ('.png','.jpg','.jpeg','.gif','.webp','.svg','.heic','.avif','.tif','.tiff')
        attachments.append(('!' if image and url in local else '')+'['+label(item.get('name') or '附件')+']('+target+')')
    if attachments:body+='\n\n## 附件\n\n'+'\n\n'.join(attachments)
    title=next((flomo.clean_title(flomo.strip_tags(part)) for part in ''.join(parser.plain).splitlines() if flomo.strip_tags(part)), '')
    title=title or 'flomo '+memo['slug']
    return memo,title,body,metadata.get('degraded',[]),local


def notion_body(root,mid,record,files,evidence):
    captured,parent=snapshot(root,record,'/'+mid+'.json',evidence)
    material=deepcopy(captured.get('materialized') or {'page':captured['page'],'blocks':captured['blocks']})
    def rewrite(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key=='url' and isinstance(item,str) and item.startswith('assets/'):
                    relative=str(parent/item)
                    if relative not in record['files']:raise ValueError('source_asset_incomplete')
                    value[key]=add_asset(root,relative,record['files'][relative],item,files,evidence)
                else:rewrite(item)
        elif isinstance(value,list):
            for item in value:rewrite(item)
    rewrite(material)
    rendered=shared.render_record(material['page'],material['blocks'],mid,[],embed_images=True,
        include_text_properties=(captured.get('property_capture') or {}).get('proof_version')==1,
        property_renderer_revision=record.get('property_renderer_revision',1)).decode()
    body=rendered.split('---',2)[2].lstrip()
    page=captured['page']; title=shared.notion.page_title(page,'Name')
    for prop_name,prop in material['page'].get('properties',{}).items():
        if prop.get('type')!='files':continue
        lines=[]
        for attachment in prop.get('files',[]):
            target=(attachment.get(attachment.get('type')) or {}).get('url')
            if not isinstance(target,str):continue
            image=Path(target).suffix.lower() in ('.png','.jpg','.jpeg','.gif','.webp','.svg','.heic','.avif')
            lines.append(('!' if image else '')+'['+label(attachment.get('name') or '附件')+']('+target+')')
        if lines:body+='\n\n### '+str(prop_name)+'\n\n'+'\n\n'.join(lines)

    md=next((rel for rel in record['files'] if rel.endswith('/'+mid+'.md')),None)
    if not md:raise ValueError('source_snapshot_incomplete')
    evidence[str(shared.STAGE/md)]=record['files'][md]
    return page,title,body,md


def render(root,mid,notion_record,original_record,legacy,status,now):
    files,evidence={},{}
    variants=[]
    for ident,record in sorted(sources(notion_record).items()):
        page,title,body,path=notion_body(root,mid,record,files,evidence)
        variants.append((ident,page,title,body,path))
    originals=[]
    for ident,record in sorted(original_record.get('source_pages',{}).items()):
        memo,title,body,degraded,url_assets=original_body(root,record,files,evidence)
        originals.append((original.timestamp(memo['updated_at']),shared.digest(shared.encoded(memo)),memo,title,body,degraded,ident,url_assets))
    degraded=[]
    if originals:
        newest=max(item[0] for item in originals)
        selected={item[1]:item for item in originals if item[0]==newest}
        current=list(selected.values())
        first=current[0];memo,title=first[2:4]
        names={item[2] for item in variants}
        if title=='flomo '+mid and len(names)==1:title=next(iter(names))
        body=first[4] if len(current)==1 else '\n\n'.join('## 原源并列版本 '+str(i+1)+'\n\n'+item[4] for i,item in enumerate(current))
        if len(current)>1:degraded.append('original_same_time_variants')
        degraded.extend(reason['reason'] for item in current for reason in item[5])
        created_values={item[2]['created_at'] for item in current};updated_values={item[2]['updated_at'] for item in current}
        created=flomo.parse_time(next(iter(created_values))).isoformat() if len(created_values)==1 else None
        updated=flomo.parse_time(next(iter(updated_values))).isoformat() if len(updated_values)==1 else None
        tags=memo.get('tags',[]);representation='original_primary'
    elif variants:
        titles={item[2] for item in variants}
        title=next(iter(titles)) if len(titles)==1 else 'flomo '+mid+'（Notion 多版本）'
        body='\n\n'.join('## Notion 来源 '+str(i+1)+'：'+item[2]+'\n\n'+item[3] for i,item in enumerate(variants))
        dates={((item[1].get('properties',{}).get('Created At') or {}).get('date') or {}).get('start') for item in variants}
        created=next(iter(dates)) if len(dates)==1 else None;updated=None
        tags=sorted({tag for item in variants for tag in shared.notion.summarize_properties(item[1]).get('Tags',[]) if isinstance(tag,str)})
        representation='notion_variants';degraded.append('original_source_unavailable')
    else:raise ValueError('source_snapshot_incomplete')
    destination=root/BASE/'版本'/mid/'VERSION'
    body+='\n\n## 来源与未合并版本\n\n'
    if not originals:body+='原 flomo 正文尚未取得或本轮未观察到；以下保留 Notion 来源，不据此判断删除。\n\n'
    if len(variants)>1:body+='Notion 来源并列保留，差异尚未合并；原源正文不抹去这些来源。\n\n'
    for i,(_,page,name,_,relative) in enumerate(variants,1):
        target=link(os.path.relpath(root/shared.STAGE/relative,destination))
        variant_tags=shared.notion.summarize_properties(page).get('Tags',[])
        body+='- [Notion 来源 '+str(i)+'：'+label(name)+']('+target+')'
        if variant_tags:body+='（标签：'+label('、'.join(str(tag) for tag in variant_tags))+'）'
        source_date=((page.get('properties',{}).get('Created At') or {}).get('date') or {}).get('start')
        if source_date:body+='（Notion 记录日期：'+label(source_date)+'）'
        body+='\n'
    seen_originals=set()
    for item in originals:
        if item[1] in seen_originals:continue
        seen_originals.add(item[1]);record=original_record['source_pages'][item[6]]
        references=[]
        for suffix,description in (('/original.json','原源 JSON'),('/original.html','原源 HTML'),('/metadata.json','原始时间与附件状态')):
            rel=next(path for path in record['files'] if path.endswith(suffix))
            references.append('['+description+']('+link(os.path.relpath(root/shared.STAGE/rel,destination))+')')
        body+='- flomo 原源证据：'+' · '.join(references)+'\n'
    legacy_files={}
    for rel in legacy:
        path=shared.safe_path(root,rel);raw=path.read_bytes();reject(raw)
        legacy_files[rel]=shared.digest(raw)
        copy=str(BASE/'legacy-evidence'/(shared.digest(raw)+'.md'));files[copy]=raw
        legacy_body=raw.decode().split('---',2)[2].lstrip() if raw.startswith(b'---\n') else raw.decode()
        for item in originals:
            for url,target in item[7].items():
                legacy_body=legacy_body.replace('('+url+')','('+target+')').replace('(<'+url+'>)','('+target+')')
        for target,(asset_rel,asset_raw) in legacy_assets(root,rel,raw).items():
            asset=str(BASE/'_assets'/(shared.digest(asset_raw)+asset_suffix(asset_raw,asset_rel)))
            files[asset]=asset_raw;evidence[asset_rel]=shared.digest(asset_raw)
            local_target='../../../_assets/'+link(Path(asset).name)
            legacy_body=legacy_body.replace('('+target+')','('+local_target+')').replace('(<'+target+'>)','('+local_target+')')
            legacy_body=re.sub(r'!\[\['+re.escape(target)+r'(?:\|([^\]]*))?\]\]',
                               lambda match:'!['+label(match[1] or '')+']('+local_target+')',legacy_body)
        body+='\n\n## 既有本地版本（保留，未合并）\n\n'+legacy_body
        body+='\n\n[保留的本地原件]('+link(os.path.relpath(path,destination))+') · [本次不可变快照]('+link(os.path.relpath(root/copy,destination))+')\n'
    dates=[updated or '',*[item[1].get('last_edited_time','') for item in variants]]
    parsed_dates=[flomo.parse_time(value) for value in dates if value]
    evidence_updated=max((value for value in parsed_dates if value),default=None)
    evidence_updated=evidence_updated.isoformat() if evidence_updated else None
    metadata={'source_system':'flomo','memo_id':mid,'title':title,'aliases':[title],
        'created_at':created,'updated_at':updated,'source_evidence_updated_at':evidence_updated,
        'created_at_precision':'second' if originals and created else 'date_or_source_precision' if created else 'unknown',
        'current_renderer_version':RENDERER_VERSION,'current_exported_at':now,'original_source_status':status,'representation':representation,
        'tags':tags,'tags_origin':'flomo_original' if originals else 'notion_variants_unmerged',
        'upstream_deleted_at':memo.get('deleted_at') if originals else None,
        'notion_source_page_ids':[item[0] for item in variants],
        'stage_only':False,'formal_current':True,'unmerged_evidence':len(variants)>1 or bool(legacy),
        'media_complete':not degraded,'degraded_reasons':sorted(set(degraded))}
    revisions={ident:record.get('property_renderer_revision',1) for ident,record in sources(notion_record).items()
               if record.get('property_proof_version')==1}
    if revisions:metadata['notion_property_renderer_revisions']=revisions
    if metadata['upstream_deleted_at']:
        body='> 原源带有删除标记。本文保留历史证据，不代表内容继续有效。\n\n'+body
    raw=(shared.notion.render_frontmatter(metadata,native_lists=True)+'\n\n# '+html.escape(title.replace('\n',' '),quote=False)+'\n\n'+body+'\n').encode()
    reject(raw)
    return title,raw,files,evidence,legacy_files,metadata


def state_bytes(path):return path.read_bytes() if path.exists() else None


def export(root):
    """Caller owns verified private checkout + existing single-writer lock."""
    root=Path(root);path=shared.safe_path(root,STATE);before=state_bytes(path)
    state=json.loads(before) if before else {'version':1,'pages':{}}
    if state.get('version')!=1 or not isinstance(state.get('pages'),dict):raise ValueError('invalid_current_state')
    notion_path=shared.safe_path(root,shared.STATE)
    notion_state=json.loads(notion_path.read_bytes()) if notion_path.exists() else {'pages':{}}
    original_path=shared.safe_path(root,shared.STAGE/original.STATE)
    original_state=json.loads(original_path.read_bytes()) if original_path.exists() else {'pages':{}}
    coverage_path=shared.safe_path(root,shared.STAGE/original.COVERAGE)
    source_report=json.loads(coverage_path.read_bytes()) if coverage_path.exists() else {}
    legacy,_=shared.local_inventory(root)
    report={'written':0,'unchanged':0,'held':{},'degraded':{},'capture_complete':False,'media_complete':False}
    def persist(candidate):
        nonlocal state,before
        if state_bytes(path)!=before:raise ValueError('state_changed')
        raw=shared.encoded(candidate);shared.atomic_write(path,raw);state,before=candidate,raw
    for mid in sorted(set(notion_state['pages'])|set(original_state['pages'])):
        try:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',mid):raise ValueError('invalid_memo_identity')
            reject(mid.encode())
            prior=state['pages'].get(mid,{})
            check_files(root,owned_files(prior))
            notion_record=notion_state['pages'].get(mid,{})
            original_record=original_state['pages'].get(mid,{})
            stage_files={str(shared.STAGE/rel):sha for record in (notion_record,original_record) for rel,sha in shared.prior_files(record).items()}
            check_files(root,stage_files)
            legacy_hashes={rel:shared.digest(shared.safe_path(root,rel).read_bytes()) for rel in legacy.get(mid,[])}
            status='captured' if original_record else 'not_observed' if mid in source_report.get('notion_without_original',[]) else 'not_yet_captured'
            if original_record and mid in source_report.get('retained_prior_not_observed',[]):status='retained_not_observed'
            local_assets={asset_rel:shared.digest(data) for rel in legacy.get(mid,[])
                          for asset_rel,data in legacy_assets(root,rel,shared.safe_path(root,rel).read_bytes()).values()}
            fingerprint=shared.digest(shared.encoded({'sources':stage_files,'legacy':legacy_hashes,
                'legacy_assets':local_assets,'original_status':status,'renderer_version':RENDERER_VERSION}))
            if prior.get('input_fingerprint')==fingerprint:
                report['unchanged']+=1
                if prior.get('degraded_reasons'):report['degraded'][mid]=prior['degraded_reasons']
                continue
            title,raw,files,evidence,legacy_files,metadata=render(root,mid,notion_record,original_record,legacy.get(mid,[]),status,datetime.now(flomo.TZ).isoformat())
            if sum(len(data) for data in files.values())>200*1024*1024:raise ValueError('current_asset_batch_bound')
            version=shared.digest(shared.encoded({'md':shared.digest(raw),'files':{rel:shared.digest(data) for rel,data in files.items()},'evidence':evidence}))
            name=flomo.truncate_utf8_bytes(flomo.safe_slug(flomo.clean_title(title),mid),156)+'.md'
            relative=str(BASE/'版本'/mid/version/name);files[relative]=raw
            if state_bytes(path)!=before:raise ValueError('state_changed')
            for rel,data in files.items():shared.immutable_write(shared.safe_path(root,rel),data)
            hashes={rel:shared.digest(data) for rel,data in files.items()}
            receipt={'current_path':relative,'current_version':version,'title':title,'files':hashes,
                'versions':{**prior.get('versions',{}),version:hashes},'source_files':{**stage_files,**evidence},
                'legacy_files':legacy_files,'input_fingerprint':fingerprint,
                'degraded_reasons':metadata['degraded_reasons'],'original_source_status':status}
            candidate=deepcopy(state);candidate['pages'][mid]=receipt;persist(candidate)
            report['written']+=1
            if metadata['degraded_reasons']:report['degraded'][mid]=metadata['degraded_reasons']
        except Exception as exc:
            reason=str(exc);allowed={'owned_content_edited','source_snapshot_edited','source_asset_edited','source_snapshot_incomplete',
                'source_asset_incomplete','legacy_asset_outside_source','legacy_asset_private_configuration','stage_edited','stage_readback_failed','legacy_asset_outside_root','legacy_asset_symlink','legacy_asset_read_budget','current_asset_batch_bound','state_changed','potential_credential_in_source','invalid_memo_identity'}
            key=mid if reason!='potential_credential_in_source' or not shared.CREDENTIAL_SHAPE.search(mid) else 'untrusted_identity'
            report['held'][key]=reason if reason in allowed else 'current_export_failed'
    index=('---\nstage_only: true\n---\n\n# flomo 云端当前笔记\n\n'+
        '\n'.join('- ['+label(record['title'])+']('+link(os.path.relpath(root/record['current_path'],root/BASE))+')'
                  for mid,record in sorted(state['pages'].items()))+'\n').encode()
    index_path=shared.safe_path(root,BASE/'index.md');existing=state_bytes(index_path)
    if existing is not None and shared.digest(existing)!=state.get('index_sha256') and existing!=index:
        report['held']['index']='current_index_edited'
    else:
        shared.atomic_write(index_path,index)
        candidate=deepcopy(state);candidate['index_sha256']=shared.digest(index);persist(candidate)
    report['capture_complete']=not report['held']
    report['media_complete']=report['capture_complete'] and not report['degraded']
    report['complete']=report['media_complete']
    shared.atomic_write(shared.safe_path(root,BASE/'coverage.json'),shared.encoded(report))
    return report


def active_selection(root):
    """Fail closed on suppression; keep human-edited selected notes discoverable."""
    root=Path(root);result={'current':set(),'superseded':set(),'edited':set(),'held':{}}
    try:
        path=shared.safe_path(root,STATE)
        if not path.exists():return result
        state=json.loads(path.read_bytes())
        if state.get('version')!=1 or not isinstance(state.get('pages'),dict):raise ValueError('invalid_current_state')
        for mid,record in state['pages'].items():
            current=record['current_path'];shared.safe_path(root,current)
            if not Path(current).is_relative_to(BASE/'版本'):raise ValueError('invalid_current_path')
            result['current'].add(current)
            try:
                check_files(root,record['files']);check_files(root,record['source_files'])
            except Exception:
                result['held'][mid]='current_not_verified';result['edited'].add(current)
                continue
            if record.get('degraded_reasons'):
                result['held'][mid]='current_media_or_source_incomplete'
                continue
            for rel,sha in record['legacy_files'].items():
                legacy=shared.safe_path(root,rel)
                if not Path(rel).is_relative_to(BASE.parent) or Path(rel).is_relative_to(BASE):continue
                if legacy.is_file() and shared.digest(legacy.read_bytes())==sha:result['superseded'].add(rel)
                elif legacy.is_file():result['edited'].add(rel)
    except Exception:result['held']['state']='invalid_current_state';result['superseded'].clear()
    return result


def eligible_path(root,path,selection):
    relative=str(path.relative_to(root)) if path.is_relative_to(root) else ''
    return relative not in selection['superseded'] and not (
        shared.current_artifact(root,path) and relative not in selection['current'])


def verify_remote(root,git):
    path=shared.safe_path(root,STATE)
    if not path.exists():return False
    raw=path.read_bytes();state=json.loads(raw)
    expected=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    if git('rev-parse','HEAD:'+str(STATE))!=expected or git('rev-parse','origin/main:'+str(STATE))!=expected:
        raise ValueError('private_current_readback_failed')
    files={str(BASE/'index.md'):state['index_sha256']}
    for record in state['pages'].values():
        files.update(owned_files(record));files.update(record['source_files'])
    for rel,sha in files.items():
        rawfile=shared.safe_path(root,rel).read_bytes()
        blob=hashlib.sha1(b'blob '+str(len(rawfile)).encode()+b'\0'+rawfile).hexdigest()
        if shared.digest(rawfile)!=sha or git('rev-parse','HEAD:'+rel)!=blob or git('rev-parse','origin/main:'+rel)!=blob:
            raise ValueError('private_current_readback_failed')
    if path.read_bytes()!=raw:raise ValueError('state_changed')
    return True
