"""Offline, read-only Get Markdown -> Notion planning. No mutation transport.

Inputs are caller-supplied immutable publication, tree, and target readback
evidence. External hashes in policy pin ownership receipts; hashing a newly
observed page is never ownership adoption. Every operation below is a preview.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
import get_notion_list_semantics as list_semantics
import hashlib
import ipaddress
import json
import re
from pathlib import PurePosixPath

CONTRACT = 'get-notion-read-only-plan-1'
RENDERER = 'get-notion-native-subset-3'
REPOSITORY = 'Eddiehhhhh/eddie-llm-wiki'
DATA_SOURCE_SHA256 = _private_bindings.value('get_notion_sink_plan', 'DATA_SOURCE_SHA256')
COHORT_SHA256 = _private_bindings.value('get_notion_sink_plan', 'COHORT_SHA256')
MAX_NOTE = 1024 * 1024
MAX_TARGET = 16 * 1024 * 1024
MAX_ASSET = 16 * 1024 * 1024
MAX_BLOCKS = 10000
KINDS = frozenset(('paragraph', 'heading_1', 'heading_2', 'heading_3', 'heading_4',
    'bulleted_list_item', 'numbered_list_item', 'to_do', 'quote', 'code',
    'divider', 'image', 'table', 'table_row', 'callout'))
TEXT_KINDS = KINDS - {'divider', 'image', 'table', 'table_row'}
ANNOTATIONS = {'bold': False, 'italic': False, 'strikethrough': False,
               'underline': False, 'code': False, 'color': 'default'}
# Get category badges are visible literals, not reference-link labels. Keep
# this grammar bounded: generic bracket syntax still needs a real parser.
LITERAL_BADGE = r'[\U0001f300-\U0001faff] [\u3400-\u4dbf\u4e00-\u9fff]{1,16}'
LITERAL_BRACKET = r'\[(?:[0-9]{2}:[0-5][0-9]:[0-5][0-9]|' + LITERAL_BADGE + r')\]'


class Blocked(ValueError):
    pass


def stop(reason):
    raise Blocked(reason)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def hex_hash(value, width):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{%d}' % width, value):
        stop('incomplete_source')
    return value


def notion_id(value):
    if not isinstance(value, str):
        stop('incomplete_target_read')
    compact = value.replace('-', '')
    if not re.fullmatch('[0-9a-f]{32}', compact):
        stop('incomplete_target_read')
    return compact


def note_id(value):
    if not isinstance(value, str):
        stop('id_not_string')
    if not re.fullmatch('[0-9]{1,64}', value):
        stop('unmapped_id')
    return value


def safe_path(value):
    if not isinstance(value, str) or not value.startswith('Raw/03 Get/'):
        stop('incomplete_source')
    if '\\' in value or any(ord(c) < 32 for c in value):
        stop('incomplete_source')
    path = PurePosixPath(value)
    if str(path) != value or '..' in path.parts or '.' in path.parts:
        stop('incomplete_source')
    return value


def text_run(value, **flags):
    annotations = {**ANNOTATIONS, **flags}
    return {'type': 'text', 'text': {'content': value, 'link': None},
            'annotations': annotations}


def rich_text(value):
    """A deliberately bounded inline grammar; ambiguous syntax is blocked."""
    if not isinstance(value, str):
        stop('unsupported_source_format')
    out = []
    def plain_text(text):
        # Only an attribute-free HTML line break is supported. Intraword
        # underscores (including URL query names) and literal > are plain text.
        if re.search(r'\\<[bB][rR] ?/?>', text):
            stop('unsupported_source_format')
        text = re.sub(r'<[bB][rR] ?/?>', '\n', text)
        if '![' in text or re.search(r'(?<!\w)_|_(?!\w)|__|~~|<', text):
            stop('unsupported_source_format')
        return text
    token = re.compile(r'(\\[\\`*\[\]()~]|`[^`\n]+`|\*\*[^*]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s()]+\)|' + LITERAL_BRACKET + r')')
    cursor = 0
    for match in token.finditer(value):
        plain = value[cursor:match.start()]
        plain = plain_text(plain)
        if any(x in plain for x in ('*', '`', '[', ']')):
            stop('unsupported_source_format')
        if plain:
            out.append(text_run(plain))
        part = match.group()
        if part.startswith('\\'):
            out.append(text_run(part[1:]))
        elif part.startswith('`'):
            out.append(text_run(part[1:-1], code=True))
        elif re.fullmatch(LITERAL_BRACKET, part):
            # Explicit source literals, never reference definitions/links. A
            # real inline link wins the token alternative before this branch.
            if ((match.start() and value[match.start() - 1] == '!')
                    or value[match.end():].lstrip(' \t\r\n').startswith(('[', '(', ':'))):
                stop('unsupported_source_format')
            out.append(text_run(part))
        elif part.startswith('**'):
            out.append(text_run(plain_text(part[2:-2]), bold=True))
        elif part.startswith('*'):
            out.append(text_run(plain_text(part[1:-1]), italic=True))
        else:
            # An unsupported inline image must not lose its image semantics
            # by becoming an exclamation mark followed by a caption link.
            if match.start() and value[match.start() - 1] == '!':
                stop('unsupported_source_format')
            label, url = part[1:].split('](', 1)
            if r'\_' in label:
                # Decode only single underscore escapes in a plain link label.
                # Validate the other segments, not the decoded literal, and
                # never transform the destination or accept nested markup.
                if re.search(r'\\(?!_)|[*`\[\]]', label):
                    stop('unsupported_source_format')
                label = '_'.join(plain_text(piece) for piece in label.split(r'\_'))
            else:
                label = plain_text(label)
            run = text_run(label)
            run['text']['link'] = {'url': url[:-1]}
            out.append(run)
        cursor = match.end()
    tail = value[cursor:]
    tail = plain_text(tail)
    if any(x in tail for x in ('*', '`', '[', ']')):
        stop('unsupported_source_format')
    if tail:
        out.append(text_run(tail))
    return out


def normalize_rich(values):
    if not isinstance(values, list):
        stop('incomplete_target_read')
    out = []
    for value in values:
        if not isinstance(value, dict) or value.get('type') != 'text':
            stop('incomplete_target_read')
        text = value.get('text')
        annotations = value.get('annotations', {})
        if not isinstance(text, dict) or set(text) - {'content', 'link'} or not isinstance(text.get('content'), str):
            stop('incomplete_target_read')
        if not isinstance(annotations, dict) or set(annotations) - set(ANNOTATIONS):
            stop('incomplete_target_read')
        annotations = {**ANNOTATIONS, **annotations}
        if any(type(annotations[k]) is not bool for k in ANNOTATIONS if k != 'color') or not isinstance(annotations['color'], str):
            stop('incomplete_target_read')
        link = text.get('link')
        if link is not None and (not isinstance(link, dict) or set(link) != {'url'} or not isinstance(link['url'], str)):
            stop('incomplete_target_read')
        item = {'type': 'text', 'text': {'content': text['content'], 'link': link}, 'annotations': annotations}
        # API text chunk boundaries do not change semantic text or annotations.
        if out and out[-1]['annotations'] == annotations and out[-1]['text']['link'] == link:
            out[-1]['text']['content'] += text['content']
        else:
            out.append(deepcopy(item))
    return out


def normalize_payload(kind, payload):
    if kind not in KINDS or not isinstance(payload, dict):
        stop('incomplete_target_read')
    value = deepcopy(payload)
    allowed = {'rich_text', 'color'}
    if kind in TEXT_KINDS:
        allowed |= {'checked'} if kind == 'to_do' else set()
        allowed |= {'language', 'caption'} if kind == 'code' else set()
        allowed |= {'icon'} if kind in ('callout','paragraph') else set()
        allowed |= {'list_start_index','list_format'} if kind == 'numbered_list_item' else set()
        allowed |= {'is_toggleable'} if kind.startswith('heading_') else set()
        if set(value) - allowed:
            stop('incomplete_target_read')
        if kind=='paragraph' and 'icon' in value:
            if value['icon'] is not None: stop('incomplete_target_read')
            value.pop('icon')
        if kind=='numbered_list_item':
            # Only the default numeric format/start is supported. A reset at 1
            # remains explicit: it is not interchangeable with a continuation.
            if 'list_format' in value:
                if value['list_format']!='numbers': stop('incomplete_target_read')
                value.pop('list_format')
            if 'list_start_index' in value and (type(value['list_start_index']) is not int or value['list_start_index']!=1):
                stop('incomplete_target_read')
        value['rich_text'] = normalize_rich(value.get('rich_text'))
        value.setdefault('color', 'default')
        if not isinstance(value['color'], str):
            stop('incomplete_target_read')
        if kind.startswith('heading_'):
            if value.get('is_toggleable', False) is not False:
                stop('incomplete_target_read')
            value.pop('is_toggleable', None)
        if kind == 'to_do' and type(value.get('checked')) is not bool:
            stop('incomplete_target_read')
        if kind == 'code':
            if not isinstance(value.get('language'), str):
                stop('incomplete_target_read')
            value['caption'] = normalize_rich(value.get('caption', []))
    elif kind == 'divider':
        if value:
            stop('incomplete_target_read')
    elif kind == 'table':
        if set(value) != {'table_width', 'has_column_header', 'has_row_header'} or type(value['table_width']) is not int or not 1 <= value['table_width'] <= 20 or type(value['has_column_header']) is not bool or type(value['has_row_header']) is not bool:
            stop('incomplete_target_read')
    elif kind == 'table_row':
        if set(value) != {'cells'} or not isinstance(value['cells'], list):
            stop('incomplete_target_read')
        value['cells'] = [normalize_rich(cell) for cell in value['cells']]
    elif kind == 'image':
        import get_notion_png_upload as png_upload
        # Collector must resolve the persistent upload identity against an
        # independently confirmed asset binding. Signed URLs are never identity.
        if value.get('kind') == 'notion_png_preflight':
            if set(value) != {'kind','sha256','byte_count','mime','caption'} or not png_upload.valid_semantic_size(value['sha256'], value['byte_count']) or value['mime'] != 'image/png':
                stop('missing_asset')
        elif value.get('kind') == 'notion_uploaded_png':
            if set(value) != {'kind','upload_id','sha256','byte_count','mime','caption'} or not png_upload.valid_semantic_size(value['sha256'], value['byte_count']) or value['mime'] != 'image/png':
                stop('missing_asset')
            value['upload_id'] = notion_id(value['upload_id'])
        elif value.get('kind') == 'notion_existing_image_block':
            if set(value) != {'kind','block_id','sha256','byte_count','mime','caption'} or type(value['byte_count']) is not int or not 0 < value['byte_count'] <= MAX_ASSET or value['mime'] != 'image/jpeg':
                stop('missing_asset')
            value['block_id'] = notion_id(value['block_id'])
        else:
            if set(value) != {'file_identity', 'sha256', 'caption'}:
                stop('missing_asset')
            value['file_identity'] = notion_id(value['file_identity'])
        hex_hash(value['sha256'], 64)
        value['caption'] = normalize_rich(value['caption'])
    return value


def block(kind, payload, children=None):
    return {'type': kind, 'payload': normalize_payload(kind, payload), 'children': children or []}


def comparison_http_empty_path(url):
    """Comparison-only HTTP(S) empty-path rule, preserving every other byte.

    RFC 9110 4.2.3 and RFC 3986 6.2.3 permit an empty HTTP(S) path to
    compare as '/'. This deliberately does NOT normalize scheme/host case,
    ports, percent encodings, query/fragment delimiters or any nonempty path.
    Only a conservative legal ASCII absolute-URI subset without userinfo is
    eligible; malformed/unsupported values remain exact, never repaired.
    """
    if not isinstance(url, str) or not url.isascii(): return url
    atom = r"(?:[A-Za-z0-9._~!$&'()*+,;=:@/?-]|%[0-9A-Fa-f]{2})"
    match = re.fullmatch(r"(?P<scheme>https?)://(?P<authority>[^/?#]+)(?P<path>/)?"
                         + r"(?P<suffix>(?:\?" + atom + r"*)?(?:#" + atom + r"*)?)", url, re.IGNORECASE)
    if match is None or match['path'] is not None: return url
    authority = match['authority']
    if '@' in authority: return url
    if authority.startswith('['):
        host = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::([0-9]+))?", authority)
        if host is None: return url
        try: ipaddress.IPv6Address(host[1])
        except ValueError: return url
    else:
        regname = r"(?:[A-Za-z0-9._~!$&'()*+,;=-]|%[0-9A-Fa-f]{2})+"
        host = re.fullmatch(r"(" + regname + r")(?::([0-9]+))?", authority)
        if host is None: return url
    # A port must be an actual bounded TCP port; leave empty/invalid ports exact.
    if host[2] is not None and (len(host[2]) > 5 or int(host[2]) > 65535): return url
    at = len(match['scheme']) + 3 + len(authority)
    return url[:at] + '/' + url[at:]


def _comparison_payload(kind, payload):
    value = normalize_payload(kind, payload)
    def rich(runs):
        for run in runs:
            link = run['text']['link']
            if link is not None: link['url'] = comparison_http_empty_path(link['url'])
        # Link normalization can make adjacent equally styled chunks identical.
        return normalize_rich(runs)
    for key in ('rich_text', 'caption'):
        if key in value: value[key] = rich(value[key])
    if 'cells' in value: value['cells'] = [rich(cell) for cell in value['cells']]
    return value


def comparison_semantics(rows, previous_type=list_semantics.UNKNOWN):
    """Compare semantic trees using the same payload normalization as readback.

    Compaction retains original rich-text runs, while wire encoding and native
    readback merge adjacent runs with identical annotations and links. Normalize
    only copies for comparison; approved plans, requests and receipt node hashes
    are never rewritten. HTTP(S) empty paths compare as '/' only in these
    copies. Root/child boundaries and ordering remain exact.
    """
    def walk(items, depth):
        if not isinstance(items, list) or depth > 32:
            stop('incomplete_target_read')
        result = []
        for row in items:
            if not isinstance(row, dict) or set(row) != {'type', 'payload', 'children'}:
                stop('incomplete_target_read')
            result.append({'type': row['type'],
                'payload': _comparison_payload(row['type'], row['payload']),
                'children': walk(row['children'], depth + 1)})
        return result
    return list_semantics.defaults(walk(rows, 0), previous_type)


def source_body(data, ident):
    if not isinstance(data, bytes) or len(data) > MAX_NOTE:
        stop('incomplete_source')
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        stop('incomplete_source')
    if not text.startswith('---\n') or '\n---\n' not in text:
        stop('incomplete_source')
    head, body = text[4:].split('\n---\n', 1)
    fields = {}
    for line in head.splitlines():
        key, sep, value = line.partition(': ')
        if not sep or key in fields:
            stop('incomplete_source')
        fields[key] = value
    try:
        actual = json.loads(fields['note_id'])
    except (KeyError, ValueError):
        stop('incomplete_source')
    if note_id(actual) != ident:
        stop('incomplete_source')
    return body


def compact_paragraph_pairs(blocks):
    """Fit one request by joining only disjoint adjacent paragraph pairs.

    This deliberately changes a pair of block-level paragraph boundaries into
    an internal blank line. Every source rich-text run is retained verbatim and
    in order, with exactly one plain double-newline run inserted per pair.
    No structural block, child, color boundary or request limit is crossed.
    Return the input unchanged in value if the bounded pairing cannot fit.
    """
    limit = 100
    if not limit < len(blocks) <= 2 * limit:
        return deepcopy(blocks), 0
    needed = len(blocks) - limit
    def eligible(row):
        return (isinstance(row, dict) and set(row) == {'type', 'payload', 'children'}
                and row['type'] == 'paragraph' and row['children'] == []
                and isinstance(row['payload'], dict)
                and 'rich_text' in row['payload']
                and not set(row['payload']) - {'rich_text', 'color'}
                and isinstance(row['payload']['rich_text'], list)
                and all(isinstance(run, dict) and run.get('type') == 'text'
                    and isinstance(run.get('text'), dict)
                    and isinstance(run['text'].get('content'), str)
                    for run in row['payload']['rich_text']))
    output = []; merged = 0; index = 0
    while index < len(blocks):
        left = blocks[index]
        if merged < needed and index + 1 < len(blocks):
            right = blocks[index + 1]
            if (eligible(left) and eligible(right)
                    and left['payload'].get('color', 'default') == right['payload'].get('color', 'default')):
                runs = left['payload']['rich_text'] + [text_run('\n\n')] + right['payload']['rich_text']
                # Respect the unchanged 100-run/2000-character wire limits.
                chunks = sum(max(1, (len(run['text']['content']) + 1999) // 2000) for run in runs)
                if chunks <= 100:
                    joined = deepcopy(left); joined['payload']['rich_text'] = deepcopy(runs)
                    output.append(joined); merged += 1; index += 2
                    continue
        output.append(deepcopy(left)); index += 1
    return (output, merged) if len(output) <= limit else (deepcopy(blocks), 0)


def ordinary_prose_indent(line):
    """Recognize up to three prose spaces without stripping any source text.

    Indented block syntax and four-space code remain outside this subset.
    The caller must also establish that this is not a list/quote continuation.
    """
    match = re.fullmatch(r' {1,3}(\S.*)', line)
    if match is None:
        return False
    value = match[1]
    return not (re.match(r'^(?:#|>|[-+*](?:[ \t]|$)|[0-9]+[.)][ \t]|\||```|~~~|!\[|<)', value)
                or re.fullmatch(r'[-*_=]+', value))


def literal_hashtag_prefix(line):
    """Bounded root prose, not ATX: 1-6 hashes followed by non-whitespace.

    CommonMark 0.31.2 section 4.2 requires a space, tab or end of line
    after the opening ATX hashes. No indentation or empty heading is added.
    Inline markup still passes through the unchanged rich-text grammar.
    """
    return re.match(r'^#{1,6}[^#\s]', line) is not None


def paragraph_interrupt(line):
    """Only a start-at-one ordered marker interrupts an open paragraph.

    A numeric-dot phrase otherwise stays in that same paragraph, including its
    marker and whitespace. At a block boundary the existing list parser still
    requires representable numbering. This does not relax list starts/resets.
    """
    if ordinary_prose_indent(line) or literal_hashtag_prefix(line):
        return False
    ordered = re.match(r'^([0-9]+)\. ', line)
    if ordered and (len(ordered[1]) > 9 or ordered[1].lstrip('0') != '1'):
        return False
    return re.match(r'^(?:#|>|[-+*] |[0-9]+\. |\||```|!\[|<| |\t)', line) is not None


def render_markdown(data, ident, assets, source_path, content_tree, *, preserved_image=None,reviewed_png=None):
    """Native preview subset. Unsupported input is blocked, never flattened."""
    if preserved_image is not None:
        # A preserved native block is not a writable file identity. Only the
        # strict source projection may omit its already-present image reference.
        from get_notion_image_projection import project_image_source
        if not isinstance(preserved_image,dict) or set(preserved_image)!={'source_binding','source_binding_sha256','native_binding'}:stop('missing_asset')
        source_binding=preserved_image['source_binding'];native_binding=preserved_image['native_binding']
        try:projected=project_image_source(data,ident,source_path,content_tree,source_binding,preserved_image['source_binding_sha256'])
        except ValueError:stop('missing_asset')
        if not isinstance(native_binding,dict) or native_binding.get('kind')!='notion_existing_image_block' or native_binding.get('readback_verified') is not True:stop('missing_asset')
        for left,right in (('asset_path','asset_path'),('asset_blob','git_blob'),('asset_sha256','content_sha256'),('asset_bytes','byte_count')):
            if projected[left]!=native_binding.get(right):stop('missing_asset')
        rendered,used,warnings=render_markdown(projected['markdown_bytes'],ident,assets,source_path,content_tree)
        if used:stop('missing_asset')
        binding={'path':native_binding['asset_path'],'git_blob':native_binding['git_blob'],
            'sha256':native_binding['content_sha256'],'preserved_block_id':native_binding['block_id'],
            'page_id':native_binding['page_id'],'projection_sha256':projected['projection_sha256']}
        return rendered,{'preserved_existing_image':binding},warnings+['existing_image_block_preserved']
    from get_notion_credential_projection import render_body
    lines = render_body(data, ident, source_body(data, ident)).splitlines()
    output = []; warnings = []; used = {}; index = 0
    def bound_image(reference, caption=''):
        if reviewed_png is not None and ':' in reference:
            from get_notion_uploaded_png import Resolver
            if not isinstance(reviewed_png,Resolver):stop('missing_asset')
            try:payload,binding=reviewed_png.bound(ident,data,reference,[text_run(caption)] if caption else [])
            except ValueError:stop('missing_asset')
            if used:stop('missing_asset')
            used['uploaded_png']=binding
            used['uploaded_png_manifest']=reviewed_png.manifest_binding()
            return block('image',payload)
        entry = assets.get(reference)
        if not isinstance(entry, dict) or entry.get('readback_verified') is not True or not isinstance(entry.get('bytes'), bytes):
            stop('missing_asset')
        if reference.startswith('/') or ':' in reference or '\\' in reference or '%' in reference:
            stop('missing_asset')
        parts = list(PurePosixPath(source_path).parent.parts)
        for part in reference.split('/'):
            if part == '..':
                if len(parts) <= 2: stop('missing_asset')
                parts.pop()
            elif part in ('', '.'):
                stop('missing_asset')
            else:
                parts.append(part)
        path = safe_path(entry.get('path')); value = entry['bytes']
        if len(value) > MAX_ASSET: stop('missing_asset')
        blob_id = git_blob(value)
        if '/'.join(parts) != path or entry.get('git_blob') != blob_id or content_tree.get(path) != blob_id or entry.get('sha256') != digest(value):
            stop('missing_asset')
        file_id = notion_id(entry.get('notion_file_id'))
        used[reference] = {'path': path, 'git_blob': blob_id, 'sha256': digest(value), 'notion_file_id': file_id}
        return block('image', {'file_identity': file_id, 'sha256': digest(value), 'caption': rich_text(caption)})

    def wiki_image(value):
        # No aliases, dimensions, fragments or guessed attachment types.
        match = re.fullmatch(r'([^!]*?)!\[\[([^\[\]\n|#]+)\]\]', value)
        if not match:
            return None
        label, reference = match.groups()
        if not re.search(r'\.(?:png|jpe?g|gif|webp)$', reference, re.I):
            stop('unsupported_source_format')
        return label, bound_image(reference)

    def continued_bullet(start, first):
        # Exactly '*   text' (or -/+): its Markdown content column is four.
        # Equal indentation continues the same item. Blank-separated ordinary
        # paragraphs become children; code/nested block syntax is never flattened.
        paragraphs = [[first]]; cursor = start + 1
        while cursor < len(lines):
            next_line = cursor
            while next_line < len(lines) and not lines[next_line].strip(): next_line += 1
            if next_line == len(lines) or not lines[next_line].startswith('    '):
                break
            content = lines[next_line][4:]
            if (not content or content[0].isspace()
                    or re.match(r'^(?:#|>|[-+*](?:[ \t]|$)|[0-9]+[.)][ \t]|\||```|~~~|!\[|<)', content)
                    or re.fullmatch(r'[-*_=]+', content)):
                stop('unsupported_source_format')
            if next_line > cursor: paragraphs.append([])
            paragraphs[-1].append(content); cursor = next_line + 1
        return ['\n'.join(part) for part in paragraphs], cursor

    while index < len(lines):
        line = lines[index]
        if line.startswith('>') and line != '>' and not line.startswith('> '):
            stop('unsupported_source_format')
        if not line.strip():
            index += 1; continue
        if line.startswith('```'):
            language = line[3:].strip() or 'plain text'; index += 1; code = []
            while index < len(lines) and lines[index] != '```':
                code.append(lines[index]); index += 1
            if index == len(lines):
                stop('unsupported_source_format')
            output.append(block('code', {'rich_text': [text_run('\n'.join(code))], 'language': language, 'caption': []})); index += 1; continue
        image = re.fullmatch(r'!\[([^\]]*)\]\(([^\s()]+)\)', line)
        if image:
            output.append(bound_image(image[2], image[1])); index += 1; continue
        if line.startswith('![['):
            embedded = wiki_image(line)
            if embedded is None or embedded[0]: stop('unsupported_source_format')
            output.append(embedded[1]); index += 1; continue
        heading = re.fullmatch(r'(#{1,6}) (.*)', line)
        if heading:
            level = len(heading[1]); rt = rich_text(heading[2])
            if level <= 4:
                output.append(block('heading_' + str(level), {'rich_text': rt}))
            else:
                # Explicit visible level marker avoids silently equating H4/H5.
                for run in rt: run['annotations']['bold'] = True
                output.append(block('paragraph', {'rich_text': [text_run('H%d · ' % level, bold=True)] + rt}))
                warnings.append('heading_level_fallback')
            index += 1; continue
        if line in ('---', '***', '___'):
            output.append(block('divider', {})); index += 1; continue
        if line.startswith('|'):
            table = []
            while index < len(lines) and lines[index].startswith('|'):
                row = lines[index]
                if not row.endswith('|') or '\\|' in row:
                    stop('unsupported_source_format')
                table.append([x.strip() for x in row[1:-1].split('|')]); index += 1
            if len(table) < 2 or not all(re.fullmatch(r':?-{3,}:?', cell) for cell in table[1]) or len({len(row) for row in table}) != 1:
                stop('unsupported_source_format')
            rows = [block('table_row', {'cells': [rich_text(cell) for cell in row]}) for row in table[:1] + table[2:]]
            output.append(block('table', {'table_width': len(table[0]), 'has_column_header': True, 'has_row_header': False}, rows)); continue
        listing = re.fullmatch(r'( *)(?:([-+*]) |([0-9]+)\. )(.*)', line)
        if listing:
            if listing[1]:
                stop('unsupported_source_format')
            kind = 'bulleted_list_item' if listing[2] else 'numbered_list_item'; value = listing[4]
            aligned = re.fullmatch(r'[-+*]   (\S.*)', line)
            if aligned: value = aligned[1]
            if value.startswith('>'): stop('unsupported_source_format')
            embedded = wiki_image(value)
            if embedded is not None:
                label, image_block = embedded
                output.append(block(kind, {'rich_text': rich_text(label)}, [image_block]))
                index += 1; continue
            task = re.fullmatch(r'\[([ xX])\] (.*)', value)
            paragraphs, following = continued_bullet(index, value) if aligned and not task else ([task[2] if task else value], index + 1)
            payload = {'rich_text': rich_text(paragraphs[0])}
            children = [block('paragraph', {'rich_text': rich_text(part)}) for part in paragraphs[1:]]
            if kind=='numbered_list_item' and not task:
                if len(listing[3]) > 9: stop('unsupported_source_format')
                # CommonMark 5.3: blank lines loosen a list, not restart it.
                # Only the first marker in the same top-level ordered-list
                # group sets its start; subsequent marker numbers are ignored.
                # Check both source adjacency and semantic output: task items,
                # intervening blocks and other list kinds cannot join this group.
                prior = index - 1
                while prior >= 0 and not lines[prior].strip(): prior -= 1
                same_group = (prior >= 0
                    and re.fullmatch(r'[0-9]{1,9}\. .*', lines[prior]) is not None
                    and bool(output) and output[-1]['type'] == 'numbered_list_item')
                if not same_group:
                    if int(listing[3]) != 1: stop('unsupported_source_format')
                    payload['list_start_index'] = 1
            if task: kind = 'to_do'; payload['checked'] = task[1].lower() == 'x'
            output.append(block(kind, payload, children)); index = following; continue
        if line == '>' or line.startswith('> '):
            # A bare marker is an empty quote paragraph, not a blank line.
            output.append(block('quote', {'rich_text': rich_text(line[2:])})); index += 1; continue
        prose_indent = ordinary_prose_indent(line)
        if prose_indent and output and output[-1]['type'] in ('bulleted_list_item', 'numbered_list_item', 'to_do', 'quote'):
            # These spaces could belong to the previous container. Unsupported
            # lazy/nested continuations must not become new root paragraphs.
            stop('unsupported_source_format')
        if (line.startswith(('<', ' ', '\t', '#', '![', '![[', '~~~'))
                and not prose_indent and not literal_hashtag_prefix(line)):
            stop('unsupported_source_format')
        paragraph_start = index
        paragraph = [line]; index += 1
        paragraph_kind = 'paragraph'
        while index < len(lines) and lines[index].strip() and not paragraph_interrupt(lines[index]):
            if re.fullmatch(r'-{3,}|={3,}', lines[index]):
                # Root-level Setext has priority over a thematic break only
                # when it closes an ordinary open paragraph. Preserve the
                # complete multiline inline content; never insert a blank,
                # strip metadata text, or reinterpret a container as a title.
                # Indentation, hard breaks and unimplemented block markers
                # remain outside this deliberately narrow native subset.
                if (any(part != part.strip() or part.endswith('\\')
                        or re.match(r'^(?:[-+*](?:[ \t]|$)|[0-9]+[.)][ \t]|~~~)', part)
                        or re.fullmatch(r'[-*_=]+', part)
                        for part in paragraph)
                        or (paragraph_start > 0 and lines[paragraph_start - 1].strip()
                            and output and output[-1]['type'] in
                            ('bulleted_list_item', 'numbered_list_item', 'to_do', 'quote'))):
                    stop('unsupported_source_format')
                paragraph_kind = 'heading_2' if lines[index][0] == '-' else 'heading_1'
                index += 1
                break
            if lines[index] in ('___', '***'):
                break
            paragraph.append(lines[index]); index += 1
        if (any(literal_hashtag_prefix(part) for part in paragraph)
                and paragraph_start > 0 and lines[paragraph_start - 1].strip()
                and output and output[-1]['type'] in
                ('bulleted_list_item', 'numbered_list_item', 'to_do', 'quote')):
            # A new literal-hash line cannot turn an unsupported lazy
            # container continuation into a separate root paragraph.
            stop('unsupported_source_format')
        output.append(block(paragraph_kind, {'rich_text': rich_text('\n'.join(paragraph))}))
    output, joined_pairs = compact_paragraph_pairs(output)
    if joined_pairs: warnings.append('paragraph_boundaries_compacted')
    if len(encoded(output)) > MAX_TARGET or len(output) > MAX_BLOCKS:
        stop('incomplete_source')
    return output, used, sorted(set(warnings))


def snapshot_tree(snapshot):
    if isinstance(snapshot, dict) and snapshot.get('native_blocks_available') is not True:
        stop('missing_block_ownership')
    if not isinstance(snapshot, dict) or snapshot.get('complete') is not True:
        stop('incomplete_target_read')
    if len(encoded(snapshot)) > MAX_TARGET:
        stop('incomplete_target_read')
    page = notion_id(snapshot.get('page_id')); rows = snapshot.get('recursive_blocks')
    if not isinstance(rows, list) or len(rows) > MAX_BLOCKS or not isinstance(snapshot.get('properties_raw'), dict):
        stop('incomplete_target_read')
    nodes = {}; order = []
    for row in rows:
        if not isinstance(row, dict): stop('incomplete_target_read')
        ident = notion_id(row.get('id')); parent = notion_id(row.get('parent_id'))
        if ident in nodes or ident == page or not isinstance(row.get('children_ids'), list): stop('incomplete_target_read')
        children = [notion_id(x) for x in row['children_ids']]
        if len(children) != len(set(children)) or type(row.get('has_children')) is not bool or row['has_children'] != bool(children): stop('incomplete_target_read')
        nodes[ident] = {'id': ident, 'parent_id': parent, 'type': row.get('type'), 'payload': normalize_payload(row.get('type'), row.get('payload')), 'children_ids': children}
        if nodes[ident]['type']=='image' and nodes[ident]['payload'].get('kind')=='notion_existing_image_block':
            if nodes[ident]['payload']['block_id']!=ident or children:stop('missing_asset')
        order.append(ident)
    roots = [i for i in order if nodes[i]['parent_id'] == page]; visited = []
    def walk(ident, depth):
        if depth > 32 or ident in visited or ident not in nodes: stop('incomplete_target_read')
        visited.append(ident); row = nodes[ident]
        children = []
        for child in row['children_ids']:
            if child not in nodes or nodes[child]['parent_id'] != ident: stop('incomplete_target_read')
            children.append(walk(child, depth + 1))
        if row['type'] == 'table' and (not children or any(x['type'] != 'table_row' or len(x['payload']['cells']) != row['payload']['table_width'] for x in children)): stop('incomplete_target_read')
        return {'type': row['type'], 'payload': row['payload'], 'children': children}
    semantic = [walk(i, 0) for i in roots]
    if set(visited) != set(nodes): stop('incomplete_target_read')
    return nodes, roots, visited, semantic


def property_identity(properties):
    if not isinstance(properties, dict): stop('incomplete_target_read')
    value = properties.get('笔记 ID')
    if not isinstance(value, dict) or value.get('type') != 'rich_text': stop('incomplete_target_read')
    runs = normalize_rich(value.get('rich_text'))
    return note_id(''.join(x['text']['content'] for x in runs))


def block_hash(row):
    return digest(row)


def writer_gate(policy):
    evidence = policy.get('old_writer_evidence')
    if not isinstance(evidence, dict) or evidence.get('fresh_readback_verified') is not True: return False
    rows = evidence.get('writers')
    wanted = {'getnote-to-notion', 'notion-to-getnote'}
    return isinstance(rows, dict) and set(rows) == wanted and all(isinstance(v, dict) and v.get('disabled') is True and type(v.get('active_runs')) is int and v['active_runs'] == 0 for v in rows.values())


def record_plan(ident, batch, mapping, blobs, assets, snapshot, receipt, policy, *, preserved_image=None,reviewed_png=None):
    result = {'note_id': ident, 'mode': 'read_only', 'apply_ready': False, 'decision': 'blocked', 'reason_codes': [],
        'target': None, 'source_commit': batch.get('commit'), 'source_blob': None, 'renderer_version': RENDERER,
        'before_hash': None, 'after_hash': None, 'operations': [], 'protected_fields': ['all_page_properties', 'all_unowned_blocks'],
        'pending_assets': [], 'receipt_advance': False}
    try:
        note_id(ident)
        if ident not in policy['allowlist']: stop('privacy_hold')
        if not isinstance(batch.get('content_complete'), dict) or batch['content_complete'].get(ident) is not True: stop('incomplete_source')
        if not isinstance(mapping, dict): stop('unmapped_id')
        source_path = safe_path(mapping.get('path')); source_blob = hex_hash(mapping.get('md_blob'), 40)
        result['source_blob'] = source_blob
        if batch['content_tree'].get(source_path) != source_blob: stop('incomplete_source')
        data = blobs.get(source_blob)
        if not isinstance(data, bytes) or git_blob(data) != source_blob: stop('incomplete_source')
        expected_sha = batch['content_sha256'].get(source_path)
        if not isinstance(expected_sha, str) or digest(data) != expected_sha: stop('incomplete_source')
        rendered, bindings, warnings = render_markdown(data, ident, assets, source_path, batch['content_tree'], preserved_image=preserved_image,reviewed_png=reviewed_png)
        result['after_hash'] = digest(rendered); result['render_warnings'] = warnings
        result['asset_bindings_sha256'] = digest(bindings)
        if not isinstance(snapshot, dict): stop('missing_target')
        matches = snapshot.get('exact_id_matches')
        if snapshot.get('identity_query_complete') is not True or not isinstance(matches, list): stop('incomplete_target_read')
        if len(matches) > 1: stop('duplicate_id')
        if not matches: stop('missing_target')
        target = notion_id(mapping.get('page_id')); result['target'] = target
        if notion_id(matches[0]) != target or notion_id(snapshot.get('page_id')) != target: stop('unmapped_id')
        if notion_id(mapping.get('data_source_id')) != notion_id(policy['data_source_id']) or notion_id(snapshot.get('parent_data_source')) != notion_id(policy['data_source_id']): stop('wrong_parent')
        if note_id(snapshot.get('id_property')) != ident: stop('unmapped_id')
        if property_identity(snapshot.get('properties_raw')) != ident: stop('unmapped_id')
        nodes, roots, order, _ = snapshot_tree(snapshot)
        result['before_hash'] = digest({'nodes': [nodes[i] for i in order], 'properties': snapshot['properties_raw']})
        if not isinstance(receipt, dict) or receipt.get('readback_verified') is not True: stop('missing_block_ownership')
        if not isinstance(policy.get('ownership_sha256'), dict) or policy['ownership_sha256'].get(ident) != digest(receipt): stop('missing_block_ownership')
        if receipt.get('renderer_version') != RENDERER or notion_id(receipt.get('page_id')) != target or notion_id(receipt.get('data_source_id')) != notion_id(policy['data_source_id']) or note_id(receipt.get('note_id')) != ident: stop('missing_block_ownership')
        if not isinstance(receipt.get('properties_baseline'), dict) or not isinstance(receipt.get('asset_bindings'), dict): stop('missing_block_ownership')
        hex_hash(receipt.get('source_commit'), 40); hex_hash(receipt.get('md_blob'), 40)
        managed = receipt.get('managed_blocks'); protected = receipt.get('protected_blocks')
        if not isinstance(managed, list) or not managed or not isinstance(protected, list): stop('missing_block_ownership')
        owned = []
        for row in managed:
            if not isinstance(row, dict): stop('missing_block_ownership')
            block_id = notion_id(row.get('id')); owned.append(block_id)
            if block_id not in nodes or row.get('normalized_hash') != block_hash(nodes[block_id]): stop('human_edit_conflict')
        protected_ids = []
        for row in protected:
            if not isinstance(row, dict): stop('missing_block_ownership')
            hex_hash(row.get('normalized_hash'), 64)
            protected_ids.append(notion_id(row.get('id')))
        if len(protected_ids) != len(protected) or len(set(owned + protected_ids)) != len(owned + protected_ids): stop('missing_block_ownership')
        if any(i not in nodes for i in protected_ids) or [i for i in order if i in owned] != owned: stop('human_edit_conflict')
        # An owned subtree cannot absorb a new unowned child on update.
        if any(child not in owned for i in owned for child in nodes[i]['children_ids']): stop('human_edit_conflict')
        managed_roots = [i for i in roots if i in owned]
        if any(nodes[i]['parent_id'] != target and nodes[i]['parent_id'] not in owned for i in owned): stop('missing_block_ownership')
        def semantic(i): return {'type': nodes[i]['type'], 'payload': nodes[i]['payload'], 'children': [semantic(x) for x in nodes[i]['children_ids']]}
        current = [semantic(i) for i in managed_roots]
        result['protected_block_ids'] = [i for i in order if i not in owned]
        result['properties_sha256'] = digest(snapshot['properties_raw'])
        result['protected_blocks_sha256'] = digest([nodes[i] for i in order if i not in owned])
        start=roots.index(managed_roots[0]) if managed_roots else 0
        previous=nodes[roots[start-1]]['type'] if start else None
        if comparison_semantics(current,previous) == comparison_semantics(rendered,previous):
            result['decision'] = 'noop_verified'
            return result
        result['decision'] = 'preview_diff'
        if not writer_gate(policy): result['reason_codes'].append('writer_gate_unknown')
        if policy.get('format_accepted') is not True: result['reason_codes'].append('format_not_accepted')
        result['reason_codes'].append('authorization_missing')
        # Preview only. No transport consumes these records, and the plan never
        # proposes deleting/replacing a page or modifying any page property.
        result['operations'] = [{'operation': 'preview_owned_region', 'managed_block_ids': owned,
            'expected_before_hash': digest(current), 'proposed_after_hash': digest(rendered),
            'proposed_blocks': rendered, 'executable': False}]
        return result
    except (Blocked, ValueError, TypeError, KeyError, UnicodeError) as exc:
        reason = str(exc) if isinstance(exc, Blocked) else 'incomplete_source'
        result.update(decision='blocked', reason_codes=[reason], operations=[])
        if reason == 'missing_asset': result['pending_assets'] = ['unresolved_committed_asset']
        return result


def plan_sink(committed_batch, identity_map, source_blobs, assets, target_snapshot, ownership_receipt, policy):
    """Plan each changed ID independently; a state-only commit enqueues nothing.

    Snapshots/receipts are dictionaries keyed by full string Get ID. The policy
    must pin repository, data source, commit, exact allowlist and ownership SHA.
    content_tree and content_sha256 must come from a verified committed tree.
    """
    output = {'contract': CONTRACT, 'mode': 'read_only', 'apply_ready': False,
              'decision': 'blocked', 'reason_codes': [], 'records': [],
              'operations': [], 'receipt_advance': False, 'enqueue_count': 0}
    try:
        if not isinstance(committed_batch, dict) or not isinstance(policy, dict): stop('incomplete_source')
        if policy.get('read_only') is not True or any(policy.get(k) is not False for k in ('allow_create', 'allow_property_updates', 'allow_deletes')): stop('authorization_missing')
        if policy.get('repository') != REPOSITORY or committed_batch.get('repository') != REPOSITORY: stop('uncommitted_source')
        if digest(notion_id(policy.get('data_source_id'))) != DATA_SOURCE_SHA256 or not isinstance(policy.get('allowlist'), list) or len(policy['allowlist']) != 2 or digest(sorted(policy['allowlist'])) != COHORT_SHA256: stop('privacy_hold')
        if committed_batch.get('publication_status') in ('prepared', 'dry_run', 'canonical_preview_candidate_verified'): stop('dry_run_source')
        initial = policy.get('initial_two_preview')
        allowed_status = ('canonical_published', 'published') if initial is not None else ('canonical_published',)
        if committed_batch.get('publication_status') not in allowed_status or committed_batch.get('remote_readback_verified') is not True: stop('uncommitted_source')
        commit = hex_hash(committed_batch.get('commit'), 40); hex_hash(committed_batch.get('parent'), 40)
        if policy.get('expected_source_commit') != commit: stop('uncommitted_source')
        ids = committed_batch.get('changed_content_ids')
        if not isinstance(ids, list): stop('incomplete_source')
        for ident in ids: note_id(ident)
        if len(ids) != len(set(ids)): stop('duplicate_id')
        if initial is not None:
            if ids or not isinstance(initial, dict) or set(initial) != {'source_commit', 'ids', 'authorized_read_only'} or initial.get('authorized_read_only') is not True or initial.get('source_commit') != commit or initial.get('ids') != sorted(policy['allowlist']): stop('authorization_missing')
            ids = list(initial['ids'])
            output['source_mode'] = 'initial_two_preview'
        if not ids:
            output.update(decision='noop_verified', reason_codes=['state_only_commit'])
            return output
        if len(ids) > 2 or not isinstance(committed_batch.get('content_tree'), dict) or not isinstance(committed_batch.get('content_sha256'), dict): stop('incomplete_source')
        if any(not isinstance(x, dict) for x in (identity_map, source_blobs, assets, target_snapshot, ownership_receipt)): stop('incomplete_source')
        pages = []
        for ident, mapping in identity_map.items():
            note_id(ident)
            if not isinstance(mapping, dict): stop('unmapped_id')
            pages.append(notion_id(mapping.get('page_id')))
        if len(pages) != len(set(pages)): stop('duplicate_id')
        for ident in ids:
            output['records'].append(record_plan(ident, committed_batch, identity_map.get(ident), source_blobs, assets,
                target_snapshot.get(ident), ownership_receipt.get(ident), policy))
        decisions = {x['decision'] for x in output['records']}
        output['decision'] = 'blocked' if 'blocked' in decisions else ('preview_diff' if 'preview_diff' in decisions else 'noop_verified')
        output['reason_codes'] = sorted({r for x in output['records'] for r in x['reason_codes']})
        output['operations'] = [op for x in output['records'] for op in x['operations']]
        output['plan_sha256'] = digest(output)
        return output
    except (Blocked, ValueError, TypeError, KeyError) as exc:
        output['reason_codes'] = [str(exc) if isinstance(exc, Blocked) else 'incomplete_source']
        return output


def inspect_uncertain_operation(journal, snapshot):
    """Readback classifier only. A timeout never authorizes a second request."""
    result = {'decision': 'blocked', 'reason_codes': ['uncertain_operation'], 'operations': [],
              'retry_allowed': False, 'receipt_advance': False}
    try:
        if not isinstance(journal, dict) or not isinstance(snapshot, dict) or journal.get('status') not in ('uncertain', 'prepared'): return result
        if snapshot.get('identity_query_complete') is not True or len(snapshot.get('exact_id_matches', [])) != 1: return result
        page = notion_id(journal.get('page_id'))
        if notion_id(snapshot.get('page_id')) != page or notion_id(snapshot['exact_id_matches'][0]) != page: return result
        if note_id(snapshot.get('id_property')) != note_id(journal.get('note_id')) or notion_id(snapshot.get('parent_data_source')) != notion_id(journal.get('data_source_id')): return result
        if property_identity(snapshot.get('properties_raw')) != journal['note_id']: return result
        nodes, _, order, _ = snapshot_tree(snapshot)
        actual = digest({'nodes': [nodes[i] for i in order], 'properties': snapshot['properties_raw']})
        if actual == journal.get('expected_after_hash'):
            result.update(decision='noop_verified', reason_codes=['effect_observed_no_retry'])
        return result
    except (Blocked, ValueError, TypeError, KeyError):
        return result


def public_summary(plan):
    """Only bounded status/count output; never emit IDs, URLs, blocks or paths."""
    rows = plan.get('records', []) if isinstance(plan, dict) else []
    if not isinstance(rows, list) or len(rows) > 2 or any(not isinstance(x, dict) for x in rows): rows = []
    return {'contract': CONTRACT, 'mode': 'read_only', 'apply_ready': False,
        'decision': plan.get('decision') if isinstance(plan, dict) and plan.get('decision') in ('noop_verified', 'preview_diff', 'blocked') else 'blocked',
        'records': len(rows), 'noop_verified': sum(x.get('decision') == 'noop_verified' for x in rows),
        'preview_diff': sum(x.get('decision') == 'preview_diff' for x in rows),
        'blocked': sum(x.get('decision') == 'blocked' for x in rows), 'writes': 0, 'receipt_advance': False}
