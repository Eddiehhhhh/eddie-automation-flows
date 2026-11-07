"""Typed projection of canonical Get frontmatter into Notion properties.

The source body remains a page body; this module projects its fixed metadata
fields only. Multi-select options may be added by Notion when the integration
has parent data-source write permission. Their option catalog is not part of
the schema identity, so that expected addition does not look like a conflict.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import math
import re
from urllib.parse import urlsplit

import get_notion_sink_plan as p

CONTRACT = 'get-notion-properties-2'
LEGACY_CONTRACT = 'get-notion-properties-1'
FIELDS = {
    '来源系统': ('source_system', 'rich_text'),
    '来源': ('source', 'rich_text'),
    '创建时间': ('created_at', 'date'),
    '创建时间原值': ('created_at', 'rich_text'),
    '更新时间': ('updated_at', 'date'),
    '更新时间原值': ('updated_at', 'rich_text'),
    '标签': ('tags', 'multi_select'),
    '主题': ('topics', 'multi_select'),
    '笔记类型': ('note_type', 'select'),
    '是否子笔记': ('is_child_note', 'checkbox'),
    '父笔记ID': ('parent_id', 'rich_text'),
    '子笔记ID': ('children_ids', 'rich_text'),
    '分享ID': ('share_id', 'rich_text'),
    '网页链接': ('web_url', 'url'),
    '网页域名': ('web_domain', 'rich_text'),
    '网页摘要': ('web_excerpt', 'rich_text'),
    '音频链接': ('audio_url', 'url'),
    '音频时长': ('audio_duration', 'number'),
    '音频转写': ('audio_transcript', 'rich_text'),
    '附件链接': ('attachments', 'rich_text'),
    '附件原始属性': ('attachment_metadata', 'rich_text'),
}
LEGACY_FIELDS = {'创建时间': ('created_at', 'date'), '更新时间': ('updated_at', 'date'),
                 '标签': ('tags', 'multi_select'), '笔记类型': ('note_type', 'select')}
REGISTRY = 'Raw/03 Get/03 State/canonical-registry.json'
MAX_PROPERTIES_BYTES = 390_000  # Leave room for the PATCH envelope under the 400 KB request cap.
REASONS = frozenset(('property_schema_changed', 'property_source_invalid',
    'property_timezone_required', 'property_timestamp_mismatch',
    'property_option_missing', 'property_manual_edit', 'property_unowned_value',
    'property_source_empty', 'property_source_missing', 'property_budget', 'property_parser_unavailable',
    'property_date_projection_invalid', 'property_option_permission_denied'))


NOTE_TYPE_LABELS = {
    'audio': '音频笔记',
    'local_audio': '本地音频笔记',
    'plain_text': '文字笔记',
    'link': '链接笔记',
    'img_text': '图文笔记',
    'recorder_audio': '录音卡笔记',
    'recorder_flash_audio': '录音卡闪念笔记',
}


def fail(reason):
    raise ValueError(reason if reason in REASONS else 'property_source_invalid') from None


def confirmed_option_refusal(reason, status, request_fingerprint, expected_fingerprint,
                             before_sha256, after_sha256):
    """Return a durable held marker only for the exact rejected, unchanged write."""
    if (reason != 'property_option_permission_denied' or status != 403
            or not isinstance(request_fingerprint, str)
            or request_fingerprint != expected_fingerprint
            or not isinstance(before_sha256, str) or before_sha256 != after_sha256):
        return None
    return {'reason': reason, 'status': 403, 'request_fingerprint': request_fingerprint}


def schema_projection(properties, fields=None, *, include_options=False):
    if not isinstance(properties, dict): fail('property_schema_changed')
    fields = FIELDS if fields is None else fields
    if not isinstance(fields, dict) or not fields or type(include_options) is not bool: fail('property_schema_changed')
    result = {}
    for name, (_, kind) in fields.items():
        prop = properties.get(name)
        if (not isinstance(prop, dict) or prop.get('type') != kind
                or not isinstance(prop.get('id'), str) or not prop['id']):
            fail('property_schema_changed')
        row = {'id': prop['id'], 'type': kind}
        if kind in ('select', 'multi_select'):
            options = prop.get(kind, {}).get('options')
            if not isinstance(options, list) or len(options) > 2500: fail('property_schema_changed')
            values = {}
            for option in options:
                if (not isinstance(option, dict) or not isinstance(option.get('id'), str)
                        or not option['id'] or not isinstance(option.get('name'), str)
                        or not option['name'] or option['name'] in values): fail('property_schema_changed')
                values[option['name']] = option['id']
            if include_options:
                row['options'] = dict(sorted(values.items()))
            # Option names are values, not structural schema. Notion can add
            # select/multi-select options on page write when the integration
            # can edit the parent data source. Do not hash this catalog.
        result[name] = row
    return result


def native_schema_projection(properties):
    """Keep native schema structure while excluding mutable option catalogs."""
    if not isinstance(properties, dict): fail('property_schema_changed')
    result = deepcopy(properties)
    for name, prop in result.items():
        if not isinstance(name, str) or not isinstance(prop, dict): fail('property_schema_changed')
        kind = prop.get('type')
        if kind in ('select', 'multi_select'):
            config = prop.get(kind)
            if not isinstance(config, dict) or not isinstance(config.get('options'), list):
                fail('property_schema_changed')
            config.pop('options')
    return result


def source_fields(data, ident):
    p.source_body(data, ident)
    head = data.decode('utf-8')[4:].split('\n---\n', 1)[0]
    result = {}
    for line in head.splitlines():
        key, _, raw = line.partition(': ')
        if key not in {x[0] for x in FIELDS.values()}: continue
        try: result[key] = json.loads(raw)
        except ValueError:
            # Legacy unquoted YAML flow tags use the same bounded decoder as
            # quoted flow tags. Do not broaden any other metadata scalar.
            if key == 'tags' and raw.startswith('[') and raw.endswith(']'):
                result[key] = raw
                continue
            # Old exporter emits plain ISO dates/scalars, never arbitrary YAML.
            if raw.startswith(('"', "'", '[', '{')): fail('property_source_invalid')
            result[key] = raw
    return result


def zoned_date(value, evidence=None):
    if not isinstance(value, str) or not value: fail('property_source_invalid')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try: return date.fromisoformat(value).isoformat()
        except ValueError: fail('property_source_invalid')
    try: parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError: fail('property_source_invalid')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not isinstance(evidence, str): fail('property_timezone_required')
        try: exact = datetime.fromisoformat(evidence.replace('Z', '+00:00'))
        except ValueError: fail('property_timezone_required')
        if exact.tzinfo is None or exact.utcoffset() is None: fail('property_timezone_required')
        if exact.replace(tzinfo=None) != parsed: fail('property_timestamp_mismatch')
        parsed = exact
    elif evidence is not None:
        try: exact = datetime.fromisoformat(evidence.replace('Z', '+00:00'))
        except (AttributeError, ValueError): fail('property_timestamp_mismatch')
        if exact.tzinfo is None or exact != parsed: fail('property_timestamp_mismatch')
    return parsed.isoformat()


def value(property_value):
    """Stable comparison strips server option IDs/colors, not meaningful values."""
    if not isinstance(property_value, dict): fail('property_schema_changed')
    kind = property_value.get('type')
    if kind == 'date':
        item = property_value.get('date')
        if item is None: return {'type': kind, kind: None}
        if (not isinstance(item, dict) or not isinstance(item.get('start'), str)
                or item.get('end') is not None or item.get('time_zone') is not None): fail('property_schema_changed')
        start = zoned_date(item['start'])
        if 'T' in start:
            start = datetime.fromisoformat(start).astimezone(timezone.utc).isoformat()
        return {'type': kind, kind: {'start': start}}
    if kind == 'select':
        item = property_value.get(kind)
        if item is None: return {'type': kind, kind: None}
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name']: fail('property_schema_changed')
        return {'type': kind, kind: {'name': item['name']}}
    if kind == 'multi_select':
        items = property_value.get(kind)
        if (not isinstance(items, list) or len(items) > 100
                or any(not isinstance(i, dict) or not isinstance(i.get('name'), str) or not i['name'] for i in items)): fail('property_schema_changed')
        names = [i['name'] for i in items]
        if len(set(names)) != len(names): fail('property_schema_changed')
        return {'type': kind, kind: [{'name': name} for name in sorted(names)]}
    if kind == 'checkbox':
        item = property_value.get(kind)
        if type(item) is not bool: fail('property_schema_changed')
        return {'type': kind, kind: item}
    if kind == 'number':
        item = property_value.get(kind)
        if item is not None and (type(item) not in (int, float) or not math.isfinite(item)):
            fail('property_schema_changed')
        return {'type': kind, kind: item}
    if kind == 'url':
        item = property_value.get(kind)
        if item is not None and (not isinstance(item, str) or not item): fail('property_schema_changed')
        return {'type': kind, kind: item}
    if kind in ('title', 'rich_text'):
        return {'type': kind, kind: p.normalize_rich(property_value.get(kind))}
    fail('property_schema_changed')


DATE_WIRE_CONTRACT = 'get-notion-date-minute-wire-1'


def _date_wire_projection(properties):
    """A deliberate wire representation, not a tolerant readback comparator.

    Source properties retain exact seconds. Date-only and null values remain
    exact; non-null ranges/zones and naive timestamps are never repaired.
    """
    if not isinstance(properties, dict) or any(not isinstance(k, str) for k in properties):
        fail('property_date_projection_invalid')
    wire = deepcopy(properties); fields = []
    for name, prop in properties.items():
        normalized = value(prop)
        if normalized['type'] != 'date' or normalized['date'] is None: continue
        start = normalized['date']['start']
        if 'T' not in start: continue
        instant = datetime.fromisoformat(start)
        wire[name]['date']['start'] = instant.replace(second=0, microsecond=0).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        fields.append(name)
    return wire, sorted(fields)


def date_wire_binding(properties):
    """Create explicit metadata for a NEW approved plan; never edit its source."""
    wire, fields = _date_wire_projection(properties)
    if not fields: return None
    return {'contract': DATE_WIRE_CONTRACT, 'fields': fields,
            'source_sha256': p.digest(properties), 'wire_sha256': p.digest(wire)}


def date_wire_properties(properties, binding=None):
    """Unbound legacy values remain byte-exact; a bound mapping is deterministic."""
    if binding is None: return deepcopy(properties)
    try:
        expected = date_wire_binding(properties)
        if expected is None or not isinstance(binding, dict) or binding != expected:
            fail('property_date_projection_invalid')
        wire, _ = _date_wire_projection(properties)
        return wire
    except ValueError:
        fail('property_date_projection_invalid')


def empty(prop):
    normalized = value(prop)
    return normalized[normalized['type']] in (None, [])


def tag_names(raw):
    if isinstance(raw, str):
        if len(raw.encode('utf-8')) > 32 * 1024 or not raw.startswith('[') or not raw.endswith(']'):
            fail('property_source_invalid')
        try:
            import yaml
            if yaml.__version__ != '6.0.3': fail('property_parser_unavailable')
        except ImportError:
            fail('property_parser_unavailable')
        try:
            # Legacy exporter quoted one YAML flow sequence. Reject references,
            # explicit tags and unbounded token streams before safe construction.
            for count, token in enumerate(yaml.scan(raw)):
                if count >= 500 or isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)):
                    fail('property_source_invalid')
            raw = yaml.safe_load(raw)
        except (yaml.YAMLError, RecursionError):
            fail('property_source_invalid')
    if (not isinstance(raw, list) or len(raw) > 100
            or any(not isinstance(x, str) or not x or len(x) > 100 or x != x.strip() or ',' in x for x in raw)
            or len(set(raw)) != len(raw)):
        fail('property_source_invalid')
    return raw


def _rich_text_payload(raw):
    """Keep source text exact while respecting Notion's per-run text limit."""
    if not isinstance(raw, str): fail('property_source_invalid')
    if not raw: fail('property_source_empty')
    if len(raw.encode('utf-8')) > 200_000: fail('property_budget')
    chunks = [raw[i:i + 2000] for i in range(0, len(raw), 2000)]
    if len(chunks) > 100: fail('property_budget')
    return [p.text_run(chunk) for chunk in chunks]


def _list_text(raw):
    if not isinstance(raw, list):
        fail('property_source_invalid')
    if any(not isinstance(item, str) for item in raw):
        fail('property_source_invalid')
    return json.dumps(raw, ensure_ascii=False, separators=(',', ':'))


def _url(raw):
    if not isinstance(raw, str) or not raw: fail('property_source_invalid')
    try: parsed = urlsplit(raw)
    except ValueError: fail('property_source_invalid')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or any(ord(c) < 32 for c in raw):
        fail('property_source_invalid')
    return raw


def project(data, ident, schema, timestamps=None, source_metadata=None):
    """Return wire values and held-field reasons, without changing any input."""
    fields = source_fields(data, ident)
    from get_notion_exact_media_projection import field_view
    fields, source_metadata = field_view(data, ident, fields, source_metadata)
    timestamps = timestamps or {}
    result, held = {}, {}
    for name, (source, kind) in FIELDS.items():
        try:
            if name not in schema or schema[name].get('type') != kind: fail('property_schema_changed')
            if source == 'attachment_metadata':
                if not isinstance(source_metadata, dict) or 'attachments' not in source_metadata:
                    fail('property_source_missing')
                attachments = source_metadata['attachments']
                if not isinstance(attachments, list): fail('property_source_invalid')
                raw = json.dumps(attachments, ensure_ascii=False, separators=(',', ':'))
            else:
                if source not in fields: fail('property_source_missing')
                raw = fields[source]
            if raw is None or raw == '' or raw == []:
                # An explicitly present empty value is a real source value.
                # Existing ownership decides later whether it may clear Notion.
                if kind in ('rich_text', 'multi_select'):
                    payload = []
                elif kind in ('select', 'date', 'number', 'url'):
                    payload = None
                else:
                    fail('property_source_empty')
                result[name] = {'type': kind, kind: payload}
                continue
            if kind == 'date':
                key = 'created_at' if source == 'created_at' else 'edited_at'
                payload = {'start': zoned_date(raw, timestamps.get(key))}
            elif kind == 'select':
                if not isinstance(raw, str) or raw != raw.strip() or len(raw) > 100: fail('property_source_invalid')
                if ',' in raw or (source == 'note_type' and raw not in NOTE_TYPE_LABELS):
                    fail('property_source_invalid')
                payload = {'name': NOTE_TYPE_LABELS[raw] if source == 'note_type' else raw}
            elif kind == 'multi_select':
                raw = tag_names(raw)
                payload = [{'name': x} for x in sorted(raw)]
            elif kind == 'rich_text':
                if source in ('children_ids', 'attachments') and raw:
                    raw = _list_text(raw)
                payload = _rich_text_payload(raw)
            elif kind == 'checkbox':
                if type(raw) is not bool: fail('property_source_invalid')
                payload = raw
            elif kind == 'number':
                if type(raw) not in (int, float) or not math.isfinite(raw) or raw < 0:
                    fail('property_source_invalid')
                payload = raw
            elif kind == 'url':
                payload = _url(raw)
            else:
                fail('property_schema_changed')
            result[name] = {'type': kind, kind: payload}
        except ValueError as exc:
            held[name] = str(exc) if str(exc) in REASONS else 'property_source_invalid'
    bounded = {}
    for name, prop in result.items():
        candidate = {**bounded, name: prop}
        if len(p.encoded(candidate)) > MAX_PROPERTIES_BYTES:
            held[name] = 'property_budget'
        else:
            bounded[name] = prop
    return bounded, held


def three_way(desired, current, managed, *, fill_empty):
    """Only update a last-written value or an explicitly permitted empty field."""
    if type(fill_empty) is not bool or not isinstance(managed, dict) or set(managed) - set(FIELDS): fail('property_source_invalid')
    patch, next_managed, held = {}, deepcopy(managed), {}
    for name, proposed in desired.items():
        try:
            if name not in FIELDS or name not in current: fail('property_schema_changed')
            actual, wanted = value(current[name]), value(proposed)
            if actual == wanted:
                # Matching a source value is not evidence of ownership.
                continue
            baseline = managed.get(name)
            if baseline is not None:
                if actual != value(baseline): fail('property_manual_edit')
            elif not fill_empty or not empty(current[name]):
                fail('property_unowned_value')
            patch[name] = deepcopy(proposed)
            next_managed[name] = wanted
        except ValueError as exc:
            held[name] = str(exc) if str(exc) in REASONS else 'property_source_invalid'
    return patch, next_managed, held


def check_receipts(receipts):
    if not isinstance(receipts, dict) or len(receipts) > 2500: fail('property_budget')
    for ident, row in receipts.items():
        p.note_id(ident)
        if not isinstance(row, dict) or set(row) != {'contract', 'page_id', 'data_source_id', 'source_commit', 'md_blob', 'manifest_sha256', 'managed', 'held', 'readback_verified'}: fail('property_source_invalid')
        if row['contract'] not in (CONTRACT, LEGACY_CONTRACT) or row['readback_verified'] is not True: fail('property_source_invalid')
        allowed_fields = FIELDS if row['contract'] == CONTRACT else LEGACY_FIELDS
        p.notion_id(row['page_id']); p.notion_id(row['data_source_id'])
        for key, width in (('source_commit', 40), ('md_blob', 40), ('manifest_sha256', 64)): p.hex_hash(row[key], width)
        if not isinstance(row['managed'], dict) or set(row['managed']) - set(allowed_fields): fail('property_source_invalid')
        for name, prop in row['managed'].items():
            if value(prop) != prop or prop.get('type') != allowed_fields[name][1]: fail('property_source_invalid')
        if not isinstance(row['held'], dict) or set(row['held']) - set(allowed_fields) or any(reason not in REASONS for reason in row['held'].values()): fail('property_source_invalid')
    return receipts
