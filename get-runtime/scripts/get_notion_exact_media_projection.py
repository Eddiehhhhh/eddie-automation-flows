"""Private Notion view of one reviewed recording. Original Git bytes stay intact."""
from copy import deepcopy
import hashlib
import json
import get_exact_media_bindings as _private_bindings

ID_SHA = _private_bindings.value('get_notion_exact_media_projection', 'ID_SHA')
SOURCE_SHA = _private_bindings.value('get_notion_exact_media_projection', 'SOURCE_SHA')
IMAGE_LINE_SHA = _private_bindings.value('get_notion_exact_media_projection', 'IMAGE_LINE_SHA')
AUDIO_URL_SHA = _private_bindings.value('get_notion_exact_media_projection', 'AUDIO_URL_SHA')
METADATA_SHA = _private_bindings.value('get_notion_exact_media_projection', 'METADATA_SHA')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def selected(data, ident):
    if not isinstance(ident, str) or sha(ident.encode()) != ID_SHA:
        return False
    if not isinstance(data, bytes) or sha(data) != SOURCE_SHA:
        raise ValueError('content_review_required')
    return True


def safe_text(value):
    raw = json.dumps(value, ensure_ascii=False).lower()
    if any(key in raw for key in ('ossaccesskeyid', 'signature=', 'x-oss-signature')):
        raise ValueError('content_review_required')


def body_view(data, ident, body):
    if not selected(data, ident):
        return body
    lines = body.split('\n')
    indexes = [i for i,line in enumerate(lines) if sha(line.encode()) == IMAGE_LINE_SHA]
    if len(indexes) != 1 or data.decode().split('\n---\n', 1)[1] != body:
        raise ValueError('content_review_required')
    lines[indexes[0]] = '图片和原始录音请在 [Get 原笔记](https://www.biji.com/note/' + ident + ') 中查看。'
    projected = '\n'.join(lines)
    safe_text(projected)
    return projected


def field_view(data, ident, fields, metadata):
    if not selected(data, ident):
        return fields, metadata
    url = fields.get('audio_url')
    if not isinstance(url, str) or sha(url.encode()) != AUDIO_URL_SHA or fields.get('attachments') != [url]:
        raise ValueError('content_review_required')
    note_url = 'https://www.biji.com/note/' + ident
    # This is a link back to the source note. Keep the direct audio URL empty.
    result = {**fields, 'audio_url':'', 'attachments':['原始音频请在 Get 原笔记中打开：' + note_url]}
    safe_text(result)
    if metadata is None:
        raise ValueError('content_review_required')
    rows = metadata.get('attachments') if isinstance(metadata, dict) else None
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if (sha(encoded) != METADATA_SHA or not isinstance(rows,list) or len(rows) != 1
            or rows[0].get('url') != url or set(rows[0]) != {'url','type','title','duration'}):
        raise ValueError('content_review_required')
    clean = {key:deepcopy(value) for key,value in rows[0].items() if key != 'url'}
    clean['source_note_url'] = note_url
    projected = {**metadata, 'attachments':[clean]}
    safe_text(projected['attachments'])
    return result, projected
