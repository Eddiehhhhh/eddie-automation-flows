"""Private Notion view of one reviewed recording. Original Git bytes stay intact."""
from copy import deepcopy
import hashlib
import json

ID_SHA = '6f4340202c4bc131642b9e447933724dc3f4dff4721cba228716280da1b2868e'
SOURCE_SHA = '41b28d545280e9ba762ac755781444a64b698feb384629dc49fdd7d6f73c4d42'
IMAGE_LINE_SHA = '2087f79635e4229846d98735a1c568ce3123b15790a74e7f1b59f946339fd5fb'
AUDIO_URL_SHA = '52b06d2a24c04a44bb5bad0a1ad19acc5b1fbf92dce931736b681d59bda09e8d'
METADATA_SHA = 'f8f5e65d9d4a26a2b1b4163ec356989f5dae8e2019899d063da7976f33b9c138'


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
