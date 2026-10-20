"""Typed Get metadata projection. Pure, bounded and fail-closed.

Only the four source-backed metadata fields are writable. Unknown options,
missing timestamps and manual changes stay visible as held fields. This module
never creates schema options, adopts a page, clears a value, or exports bodies.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import re

import get_notion_sink_plan as p

CONTRACT = 'get-notion-properties-1'
FIELDS = {'创建时间': ('created_at', 'date'), '更新时间': ('updated_at', 'date'),
          '标签': ('tags', 'multi_select'), '笔记类型': ('note_type', 'select')}
REGISTRY = 'Raw/03 Get/03 State/canonical-registry.json'
REASONS = frozenset(('property_schema_changed', 'property_source_invalid',
    'property_timezone_required', 'property_timestamp_mismatch',
    'property_option_missing', 'property_manual_edit', 'property_unowned_value',
    'property_source_empty', 'property_source_missing', 'property_budget', 'property_parser_unavailable'))


def fail(reason):
    raise ValueError(reason if reason in REASONS else 'property_source_invalid') from None


def schema_projection(properties):
    if not isinstance(properties, dict): fail('property_schema_changed')
    result = {}
    for name, (_, kind) in FIELDS.items():
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
            row['options'] = dict(sorted(values.items()))
        result[name] = row
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
    if kind in ('title', 'rich_text'):
        return {'type': kind, kind: p.normalize_rich(property_value.get(kind))}
    fail('property_schema_changed')


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
            or any(not isinstance(x, str) or not x or len(x) > 100 or x != x.strip() for x in raw)
            or len(set(raw)) != len(raw)):
        fail('property_source_invalid')
    return raw


def project(data, ident, schema, timestamps=None):
    """Return wire values and held-field reasons, without changing any input."""
    fields = source_fields(data, ident)
    timestamps = timestamps or {}
    result, held = {}, {}
    for name, (source, kind) in FIELDS.items():
        try:
            if name not in schema or schema[name].get('type') != kind: fail('property_schema_changed')
            if source not in fields: fail('property_source_missing')
            raw = fields[source]
            if raw in ('', None, []): fail('property_source_empty')
            if kind == 'date':
                key = 'created_at' if source == 'created_at' else 'edited_at'
                payload = {'start': zoned_date(raw, timestamps.get(key))}
            elif kind == 'select':
                if not isinstance(raw, str) or raw != raw.strip() or len(raw) > 100: fail('property_source_invalid')
                if raw not in schema[name]['options']: fail('property_option_missing')
                payload = {'name': raw}
            else:
                raw = tag_names(raw)
                if not raw: fail('property_source_empty')
                if any(x not in schema[name]['options'] for x in raw): fail('property_option_missing')
                payload = [{'name': x} for x in sorted(raw)]
            result[name] = {'type': kind, kind: payload}
        except ValueError as exc:
            held[name] = str(exc) if str(exc) in REASONS else 'property_source_invalid'
    if len(p.encoded(result)) > 32 * 1024: fail('property_budget')
    return result, held


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
        if row['contract'] != CONTRACT or row['readback_verified'] is not True: fail('property_source_invalid')
        p.notion_id(row['page_id']); p.notion_id(row['data_source_id'])
        for key, width in (('source_commit', 40), ('md_blob', 40), ('manifest_sha256', 64)): p.hex_hash(row[key], width)
        if not isinstance(row['managed'], dict) or set(row['managed']) - set(FIELDS): fail('property_source_invalid')
        for name, prop in row['managed'].items():
            if value(prop) != prop or prop.get('type') != FIELDS[name][1]: fail('property_source_invalid')
        if not isinstance(row['held'], dict) or set(row['held']) - set(FIELDS) or any(reason not in REASONS for reason in row['held'].values()): fail('property_source_invalid')
    return receipts
