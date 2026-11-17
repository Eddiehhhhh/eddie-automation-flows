"""Lossless text fallback: includes all fields, transcripts and attachment URLs.
Does not claim attachment byte archival or stable hosted Notion media.
"""
import json
def render_text(snapshot):
    note=snapshot['note']
    return '# '+str(note.get('title',''))+'\n\n'+note['content']+'\n\n完整源数据（含录音原文、附件及元数据）\n'+json.dumps(note,ensure_ascii=False,sort_keys=True,indent=2)
