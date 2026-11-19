"""Deterministic Notion display blocks; byte-faithful Get source lives in GitHub."""
from hashlib import sha256
import json
import re
from urllib.parse import quote, urlsplit

from assets import asset_references
from core import Hold, canonical

FORMAT = 'notion-native-v2'
MAX_APPEND_BYTES = 400000
TEXT_TYPES = frozenset(('paragraph', 'heading_1', 'heading_2', 'heading_3', 'heading_4', 'bulleted_list_item',
                        'numbered_list_item', 'quote', 'code', 'divider'))
ANNOTATIONS = ('bold', 'italic', 'strikethrough', 'underline', 'code')
INLINE = re.compile(r'\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`|\[([^]\n]+)\]\((https?://[^\s)]+)\)')
IMAGE = re.compile(r'!\[([^]\n]*)\]\(([^)\n]+)\)')


def source_url(base, namespace, sid, version):
    if not re.fullmatch(r'[0-9]+', sid) or not re.fullmatch(r'[a-f0-9]{64}', version):
        raise Hold('native_source_identity_invalid')
    return base + '/get-direct/' + quote(namespace, safe='') + '/versions/' + sid + '/' + version + '.json'


def text_items(text, chunks, annotation=None, url=None):
    output = []
    for chunk in chunks(text):
        item = {'type': 'text', 'text': {'content': chunk}}
        if url:
            item['text']['link'] = {'url': url}
        if annotation:
            item['annotations'] = {annotation: True}
        output.append(item)
    return output


def _inline(text, chunks):
    items, offset = [], 0
    for match in INLINE.finditer(text):
        if match.start() > offset:
            items.extend(text_items(text[offset:match.start()], chunks))
        a, b, code, label, url = match.groups()
        items.extend(text_items(a or b or code or label, chunks,
                               'bold' if a or b else 'code' if code else None, url))
        offset = match.end()
    if offset < len(text):
        items.extend(text_items(text[offset:], chunks))
    return items


def _block(kind, items, **fields):
    return {'object': 'block', 'type': kind, kind: {'rich_text': items, **fields}}


def _text_blocks(kind, items):
    group, size = [], 0
    # Account for the enclosing block, list delimiters and request wrapper.
    limit = MAX_APPEND_BYTES - 1024
    for item in items:
        needed = len(json.dumps(item).encode()) + 2
        if group and (len(group) == 100 or size + needed > limit):
            yield _block(kind, group)
            group, size = [], 0
        group.append(item)
        size += needed
    yield _block(kind, group)


def sections(snapshot, chunks):
    note = snapshot['note']
    urls = {ref['url'] for ref in asset_references(note)}
    content = IMAGE.sub(lambda match: '图片' + ('（' + match[1] + '）' if match[1] else '') + '见附件'
                        if match[2] in urls else match[0], note['content'])
    original = note.get('audio')
    original = original.get('original') if isinstance(original, dict) else None
    if isinstance(original, str):
        original = original.strip()
    if note.get('note_type') in ('recorder_audio', 'recorder_flash_audio') and original:
        return [('summary', '录音智能总结', 'heading_1', render(_hide_urls(content, urls), chunks)),
                ('original', '原文', 'heading_1',
                 [_block('paragraph', text_items(part, chunks))
                  for part in chunks(_hide_urls(original, urls))])]
    if original and original != content.strip():
        content += '\n\n## 录音原文\n\n' + original
    return [('body', None, 'toggle', render(_hide_urls(content, urls), chunks))]


def _hide_urls(text, urls):
    for url in sorted(urls, key=len, reverse=True):
        text = text.replace(url, '[见已同步附件]')
    return text


def render(content, chunks):
    blocks = []
    code = None
    for line in content.splitlines():
        if line.lstrip().startswith('```'):
            if code is not None:
                for text in chunks('\n'.join(code)):
                    blocks.append(_block('code', text_items(text, chunks), language='plain text'))
                code = None
            else:
                code = []
            continue
        if code is not None:
            code.append(line)
            continue
        if not line.strip():
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)$', line)
        bullet = re.match(r'^\s*[-*+]\s+(.*)$', line)
        number = re.match(r'^\s*[0-9]+[.)]\s+(.*)$', line)
        quote_line = re.match(r'^\s*>\s?(.*)$', line)
        if re.fullmatch(r'\s*(?:---+|\*\*\*+|___+)\s*', line):
            blocks.append({'object': 'block', 'type': 'divider', 'divider': {}})
            continue
        kind, text = ('heading_' + str(min(4, len(heading[1]))), heading[2]) if heading else (
            ('bulleted_list_item', bullet[1]) if bullet else
            ('numbered_list_item', number[1]) if number else
            ('quote', quote_line[1]) if quote_line else ('paragraph', line))
        items = _inline(text, chunks)
        blocks.extend(_text_blocks(kind, items))
    if code is not None:
        for text in chunks('\n'.join(code)):
            blocks.append(_block('code', text_items(text, chunks), language='plain text'))
    return blocks


def _link_with_root_path(url):
    if not url:
        return url
    parsed = urlsplit(url)
    if parsed.scheme in ('http', 'https') and parsed.netloc and not parsed.path:
        # Notion writes a slash for a bare host. Keep all other URL characters.
        start = url.find('://') + 3 + len(parsed.netloc)
        return url[:start] + '/' + url[start:]
    return url


def read_rich(items):
    normalized = []
    for item in items:
        text = item.get('text', {})
        content = text.get('content')
        link = text.get('link')
        annotations = item.get('annotations', {})
        if (item.get('type') != 'text' or not isinstance(content, str)
                or item.get('plain_text', content) != content
                or annotations.get('color', 'default') != 'default'):
            raise Hold('managed_content_modified')
        url = link.get('url') if link else None
        if _link_with_root_path(item.get('href', url)) != _link_with_root_path(url):
            raise Hold('managed_content_modified')
        style = (_link_with_root_path(url), tuple(key for key in ANNOTATIONS if annotations.get(key)))
        if normalized and normalized[-1][1:] == style:
            normalized[-1] = (normalized[-1][0] + content, *style)
        else:
            normalized.append((content, *style))
    return normalized


def read_blocks(blocks):
    normalized = []
    for block in blocks:
        kind = block.get('type')
        payload = block.get(kind, {})
        if (kind not in TEXT_TYPES or block.get('has_children') or block.get('archived')
                or block.get('in_trash') or payload.get('color', 'default') != 'default'
                or payload.get('is_toggleable') or (kind == 'code' and payload.get('caption'))):
            raise Hold('managed_content_modified')
        normalized.append((kind, read_rich(payload.get('rich_text', [])),
                           payload.get('language') if kind == 'code' else None))
    return normalized


def body_digest(blocks):
    return sha256(canonical(blocks).encode()).hexdigest()


def matches_title(items, expected):
    try:
        return read_rich(items) == read_rich(expected)
    except Hold:
        return False
