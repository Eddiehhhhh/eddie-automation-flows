"""New/changed-only contract reconstruction, not installed-renderer byte parity.

Sources: private tracked 20-field/migration and category contract; public API
canonicalization chooses attachment.url. Audio/video remains remote. Existing
same-version Markdown never enters this module.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit,quote
from get_v7_compat import api_id, timestamp_ms

CATEGORIES={'audio':'01 语音记录','plain_text':'03 纯文本记录','link':'04 链接同步摘抄',
 'img_text':'05 图片笔记','recorder_audio':'99 录音卡笔记','recorder_flash_audio':'99 录音卡笔记'}
RECORDINGS={'recorder_audio','recorder_flash_audio'}

def text(value):return str(value or '').replace('\r\n','\n').strip()
def scalar(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))
def names(value):return [text(v.get('name')) for v in value if isinstance(v,dict) and text(v.get('name'))] if isinstance(value,list) else []
def title_for(note):
 title=text(note.get('title'))
 if not title:
  for line in text(note.get('content')).splitlines():
   title=re.sub(r'^[-*>+#]+\s*','',line).strip()[:50]
   if title:break
 return title or 'untitled'
def safe_filename(value):
 value=re.sub(r'[\\/:*?"<>|]',' ',value)
 value=re.sub(r'[\x00-\x1f\x7f]','',value)
 return re.sub(r'\s+',' ',value).strip().rstrip('. ')[:80].rstrip('. ') or 'untitled'
def destination(root,note,old_path=None):
 if old_path:return Path(old_path)
 kind=note.get('note_type');category=CATEGORIES.get(kind)
 if not category:raise ValueError('unsupported_note_type')
 title=safe_filename(title_for(note));path=Path(category)/(title+'.md')
 if (root/path).exists():path=Path(category)/(title+'-'+api_id(note.get('note_id') or note.get('id'))+'.md')
 if (root/path).exists():raise ValueError('destination_collision')
 return path

def render_time(value, canonical_naive=False):
 # Existing ISO bytes stay unchanged. Numeric API epochs use the established
 # +08:00 frontmatter representation, without guessing any naive timezone.
 if value is None or value=='':return ''
 if canonical_naive and isinstance(value,str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}',value.strip()):
  ms=timestamp_ms(value)
  if ms is None:raise ValueError('invalid_detail_time')
  return dt.datetime.fromtimestamp(ms/1000,dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')
 if isinstance(value,str) and not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',value.strip()):return value
 ms=timestamp_ms(value)
 if ms is None:raise ValueError('invalid_detail_time')
 return dt.datetime.fromtimestamp(ms/1000,dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='milliseconds')

def validate_note_shape(note):
 for field,kind in [('attachments',list),('children_ids',list),('audio',dict),('web_page',dict)]:
  if note.get(field) is not None and not isinstance(note[field],kind):raise ValueError('invalid_attachment')
 attachments=note.get('attachments') or []
 if not isinstance(attachments,list) or any(not isinstance(a,dict) or not isinstance(a.get('url'),str) or not a.get('url') for a in attachments):raise ValueError('invalid_attachment')

def transcript_pending(note):
 if note.get('note_type') not in RECORDINGS:return False
 audio=note.get('audio')
 if audio is not None and not isinstance(audio,dict):raise ValueError('invalid_attachment')
 return not text((audio or {}).get('original'))

def render(note,assets,path,*,faithful=False):
 ident=api_id(note.get('note_id') or note.get('id'));kind=note.get('note_type')
 audio=note.get('audio') or {};audio=audio if isinstance(audio,dict) else {}
 source_text=lambda value: value if isinstance(value,str) else ''
 original=source_text(audio.get('original')) if faithful else text(audio.get('original'));content=source_text(note.get('content')) if faithful else text(note.get('content'))
 if kind in RECORDINGS and not original:raise ValueError('transcript_pending')
 attachments=note.get('attachments') or []
 links=[a.get('url') for a in attachments if isinstance(a,dict) and a.get('type')=='link' and a.get('url')]
 web=note.get('web_page') or {};web=web if isinstance(web,dict) else {}
 weburl=web.get('url') or (links[0] if links else '')
 fields={'source_system':'getnote','note_id':ident,'title':title_for(note),'note_type':kind,
  'source':note.get('source') or '', 'created_at':note.get('created_at','') if faithful else render_time(note.get('created_at')), 'updated_at':note.get('updated_at','') if faithful else render_time(note.get('updated_at'),canonical_naive=True),
  'tags':names(note.get('tags')),'topics':names(note.get('topics')),'is_child_note':bool(note.get('is_child_note')),
  'parent_id':str(note.get('parent_id') or ''),'children_ids':[str(v) for v in note.get('children_ids') or []],
  'share_id':str(note.get('share_id') or ''),'web_url':weburl,'web_domain':urlsplit(weburl).netloc,
  'web_excerpt':web.get('excerpt') or note.get('ref_content') or '',
  'audio_url':audio.get('play_url') or note.get('audio_url') or '',
  'audio_duration':audio.get('duration') or note.get('audio_duration') or 0,
  'audio_transcript':audio.get('transcript') or note.get('audio_transcript') or (original if kind in RECORDINGS | {'audio', 'local_audio'} else ''),
  'attachments':[a.get('url') or '' for a in attachments]}
 lines=['---']+[k+': '+scalar(v) for k,v in fields.items()]+['---','']
 if original and original!=content:
  if content:lines+=['## AI 总结','',content,'']
  lines+=['## 原文','',original,'']
 else:lines+=[original or content or '（空）','']
 if faithful:
  for label,value in [('引用原文',note.get('ref_content')),('网页原文',web.get('content')),('网页 AI 摘要',web.get('excerpt'))]:
   if isinstance(value,str) and value:
    lines += ['## '+label,'',value,'']
 # Keep remote image URLs exactly as supplied in content. Do not transform resize
 # URLs or replace remote image policy with an all-history original download.
 for asset in assets:
  if asset.get('status')=='downloaded':
   relative=os.path.relpath(asset['path'],path.parent)
   lines.append(('!' if asset['kind']=='image' else '')+'['+asset['kind']+']('+quote(relative)+')')
 return ('\n'.join(lines)+'\n' if faithful else '\n'.join(lines).rstrip()+'\n'),fields

class CompatibleWriter:
 def __init__(self,root,asset_store):self.root=Path(root);self.store=asset_store;self.rows={}
 _render=staticmethod(render)
 def write(self,summary,detail,old_path):
  note={**summary,**detail}
  validate_note_shape(note)
  attachments=note.get('attachments') or []
  note['attachments']=[{**a,'original_url':a['url']} for a in attachments]
  # Validate pending before any binary write/download.
  self._render(note,[],Path(old_path or 'placeholder.md'))
  path=destination(self.root,note,old_path)
  assets=self.store.materialize(note)
  content,fields=self._render(note,assets,path)
  target=self.root/path
  if not target.resolve().is_relative_to(self.root.resolve()):raise ValueError('path_escape')
  target.parent.mkdir(parents=True,exist_ok=True)
  target.write_text(content,encoding='utf-8')
  if target.read_bytes()!=content.encode('utf-8'):raise ValueError('readback')
  ident=fields['note_id'];self.rows[ident]={'title':fields['title'],'note_id':ident,'note_type':fields['note_type'],
   'created_at':fields['created_at'],'updated_at':fields['updated_at'],'path':path.as_posix(),'category':path.parts[0]}
  return path.as_posix()
 def finish(self):
  for item in self.store.memo.values():
   binary=(self.root/item['path']).read_bytes()
   if len(binary)!=item['bytes'] or hashlib.sha256(binary).hexdigest()!=item['sha256']:raise ValueError('readback')
  if not self.rows:return
  path=self.root/'03 State'/'manifest.json'
  old=json.loads(path.read_text()) if path.exists() else {'notes':[]}
  prior_rows={api_id(row.get('note_id')):row for row in old['notes']}
  rows=[row for row in old['notes'] if api_id(row.get('note_id')) not in self.rows]+[{**prior_rows.get(ident,{}),**row} for ident,row in self.rows.items()]
  rows.sort(key=lambda n:(str(n.get('updated_at') or n.get('created_at') or ''),n['path']),reverse=True)
  generated=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')
  result={**old,'source_system':'getnote','generated_at':generated,'count':len(rows),'counts_by_category':{},'counts_by_note_type':{},'notes':rows}
  for row in rows:
   for field,target in [('category','counts_by_category'),('note_type','counts_by_note_type')]:
    key=row.get(field) or '';result[target][key]=result[target].get(key,0)+1
  path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
  # Full existing index membership retained, never replaced by changed subset.
  index=self.root/'02 Index'/'全部笔记.md';index.parent.mkdir(parents=True,exist_ok=True)
  index.write_text(render_index(rows,generated),encoding='utf-8')

def markdown_link(path: str) -> str:
    escaped = path.replace(")", "%29").replace("(", "%28")
    return f"[{path}](../{escaped})"


def render_index(notes: list[dict[str, Any]], generated_at: str) -> str:
    counts_by_category: dict[str, int] = {}
    counts_by_type: dict[str, int] = {}
    for note in notes:
        counts_by_category[note["category"]] = counts_by_category.get(note["category"], 0) + 1
        counts_by_type[note["note_type"]] = counts_by_type.get(note["note_type"], 0) + 1

    lines = [
        "# Get 笔记同步索引",
        "",
        f"- 索引生成：{generated_at}",
        f"- 笔记数量：{len(notes)}",
        "- 来源：本地 `Raw/03 Get/` Markdown 文件重建",
        "",
        "## 按目录统计",
        "",
    ]
    for category, count in sorted(counts_by_category.items()):
        lines.append(f"- `{category or '.'}`：{count}")

    lines += ["", "## 按 note_type 统计", ""]
    for note_type, count in sorted(counts_by_type.items()):
        lines.append(f"- `{note_type}`：{count}")

    lines += [
        "",
        "## 最近更新",
        "",
        "| created_at | updated_at | note_type | 标题 | 文件 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for note in notes[:300]:
        title = note["title"].replace("|", "\\|")
        lines.append(
            f"| {note['created_at']} | {note['updated_at']} | `{note['note_type']}` | "
            f"{title} | {markdown_link(note['path'])} |"
        )

    if len(notes) > 300:
        lines += [
            "",
            f"> 仅列出最近更新的 300 条；完整清单位于 `Raw/03 Get/03 State/manifest.json`。",
        ]

    return "\n".join(lines) + "\n"


class FaithfulWriter(CompatibleWriter):
 """New-note writer only. Existing content requires explicit reconciliation.

 The raw-state runner never calls this writer with an old path. Guard here too
 so a future caller cannot accidentally restore the destructive legacy route.
 """
 _render=staticmethod(lambda note,assets,path:render(note,assets,path,faithful=True))
 def write(self,summary,detail,old_path):
  if old_path is not None:raise ValueError('legacy_reconciliation_required')
  return super().write(summary,detail,None)

 def write_update(self,summary,detail,entry):
  """Update only this contract's hash-bound generated notes; retain old bytes."""
  if entry.get('origin')!='faithful-created':raise ValueError('legacy_reconciliation_required')
  path=Path(entry['filePath']);target=self.root/path
  if target.is_symlink() or not target.resolve().is_relative_to(self.root.resolve()):raise ValueError('path_escape')
  old=target.read_bytes();digest=hashlib.sha256(old).hexdigest()
  if digest!=entry['mirrorSha256']:raise ValueError('cloud_mirror_changed')
  try:
   head=old.decode('utf-8').split('\n---\n',1)[0]
   if not head.startswith('---\n'):raise ValueError()
   prior={}
   for line in head.split('\n')[1:]:
    key,sep,value=line.partition(': ')
    if not sep or key in prior:raise ValueError()
    prior[key]=json.loads(value)
  except (UnicodeError,ValueError):raise ValueError('managed_header_changed')
  note={**summary,**detail};validate_note_shape(note)
  note['attachments']=[{**a,'original_url':a['url']} for a in note.get('attachments') or []]
  _,fields=self._render(note,[],path)
  if fields['note_id']!=prior.get('note_id') or scalar(fields.get('created_at'))!=scalar(prior.get('created_at')):raise ValueError('source_field_missing')
  empty=(None,'',[],{},0,False)
  # Without explicit deletion semantics, missing populated fields are a conflict.
  # They never remain silently stale in a supposedly updated current note.
  if any(value not in empty and fields.get(key) in empty for key,value in prior.items() if key!='updated_at'):raise ValueError('source_field_missing')
  if set(prior)-set(fields):raise ValueError('source_field_missing')
  history=list(entry.get('history',[]));archive=Path('03 State/history')/fields['note_id']/(digest+'.md.txt')
  record={'filePath':archive.as_posix(),'sha256':digest,'sourceVersion':entry['sourceVersion']}
  if not any(item['filePath']==record['filePath'] for item in history):
   if len(history)>=100:raise ValueError('source_history_limit')
   history.append(record)
  archived=self.root/archive
  if archived.exists() and archived.read_bytes()!=old:raise ValueError('readback')
  assets=self.store.materialize(note);content,fields=self._render(note,assets,path)
  archived.parent.mkdir(parents=True,exist_ok=True)
  if not archived.exists():archived.write_bytes(old)
  if archived.read_bytes()!=old:raise ValueError('readback')
  target.write_bytes(content.encode('utf-8'))
  if target.read_bytes()!=content.encode('utf-8'):raise ValueError('readback')
  ident=fields['note_id'];self.rows[ident]={'title':fields['title'],'note_id':ident,'note_type':fields['note_type'],
   'created_at':fields['created_at'],'updated_at':fields['updated_at'],'path':path.as_posix(),'category':path.parts[0]}
  return path.as_posix(),history
