"""Lossless text fallback: includes all fields, transcripts and attachment URLs.
Does not claim attachment byte archival or stable hosted Notion media.
"""
import json

def source_title(snapshot):
    for value in (snapshot['note'].get('title'),snapshot['note'].get('display_title')):
        if isinstance(value,str) and value.strip():return value
    return snapshot['id']

def tag_names(values):
    from core import Hold
    if not isinstance(values,list):raise Hold('source_tag_shape')
    result=[]
    for item in values:
        name=item.get('name') if isinstance(item,dict) else item
        if not isinstance(name,str) or not name.strip():raise Hold('source_tag_shape')
        result.append(name)
    return result

def render_text(snapshot):
    note=snapshot['note']
    return '# '+source_title(snapshot)+'\n\n'+note['content']+'\n\n完整源数据（含录音原文、附件及元数据）\n'+json.dumps(note,ensure_ascii=False,sort_keys=True,indent=2)
