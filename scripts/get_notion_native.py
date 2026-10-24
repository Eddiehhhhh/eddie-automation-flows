"""Bounded read-only Notion native collector. No credentials, IO or writer.

request(method, relative_path, json_payload_or_None) is injected by caller.
Use Notion-Version 2025-09-03. Never log callback payloads/results/errors.
Returned snapshots contain private content and MUST remain temporary in memory.
"""
import get_private_runtime_bindings as _private_bindings
import hashlib
import json
import re
from copy import deepcopy
from urllib.parse import urlencode

PAGE_PINS = _private_bindings.value('get_notion_native', 'PAGE_PINS')
DATA_SOURCE_PIN = _private_bindings.value('get_notion_native', 'DATA_SOURCE_PIN')
DATABASE_PIN = _private_bindings.value('get_notion_native', 'DATABASE_PIN')
MAX_PAGES = 100
MAX_BLOCKS = 10000
MAX_BYTES = 16 * 1024 * 1024

class Blocked(ValueError):
    """Only static, public-safe reason codes may appear in this exception."""

def fail(reason):
    raise Blocked(reason) from None

def encoded(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    except Exception:
        fail('malformed_response')

def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else encoded(value)).hexdigest()

def uuid(value):
    if not isinstance(value, str): fail('malformed_identity')
    value = value.replace('-', '').lower()
    if not re.fullmatch('[0-9a-f]{32}', value): fail('malformed_identity')
    return value

def note_id(page):
    props = page.get('properties')
    if not isinstance(props, dict): fail('id_property_invalid')
    prop = props.get('笔记 ID')
    if not isinstance(prop, dict): fail('id_property_invalid')
    parts = prop.get('rich_text')
    if prop.get('type') != 'rich_text' or not isinstance(parts, list) or len(parts) >= 25: fail('id_property_invalid')
    if any(not isinstance(x, dict) or x.get('type') != 'text' or not isinstance(x.get('text'), dict) or not isinstance(x['text'].get('content'), str) for x in parts): fail('id_property_invalid')
    value = ''.join(x['text']['content'] for x in parts)
    if not re.fullmatch('[0-9]{19}', value): fail('id_property_invalid')
    return value

def page_difference_categories(before, after):
    """Ignore only top-level request correlation; return fixed, value-free labels.

    Unknown fields remain strictly compared. Never return original keys or values.
    """
    categories = set()
    groups = {
        'object': 'identity', 'id': 'identity', 'parent': 'parent',
        'properties': 'properties', 'last_edited_time': 'edit_metadata',
        'last_edited_by': 'edit_metadata', 'created_time': 'creation_metadata',
        'created_by': 'creation_metadata', 'archived': 'lifecycle',
        'in_trash': 'lifecycle', 'is_locked': 'lifecycle',
        'icon': 'presentation', 'cover': 'presentation',
        'url': 'location', 'public_url': 'location',
    }
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ('malformed_envelope',)
    for key in (set(before) | set(after)) - {'request_id'}:
        if key not in before or key not in after or encoded(before[key]) != encoded(after[key]):
            categories.add(groups.get(key, 'other_page_field'))
    return tuple(sorted(categories))


def collect(request, *, page_pins=None, data_source_pin=DATA_SOURCE_PIN, database_pin=DATABASE_PIN):
    """Return private snapshots plus safe counts. Never treats observation as ownership.

    Pins are injectable for synthetic tests; production callers MUST use defaults.
    """
    pins = PAGE_PINS if page_pins is None else page_pins
    budget = {'requests': 0, 'bytes': 0}
    def call(method, path, body=None):
        # No arbitrary paths or mutating endpoints; POSTs here are read-only queries.
        valid = (method == 'POST' and path == '/search') or (method == 'POST' and re.fullmatch(r'/data_sources/[0-9a-f]{32}/query', path)) or (method == 'GET' and re.fullmatch(r'/(pages|data_sources)/[0-9a-f]{32}', path)) or (method == 'GET' and re.fullmatch(r'/blocks/[0-9a-f]{32}/children(?:\?.*)?', path))
        if not valid: fail('invalid_read_route')
        budget['requests'] += 1
        if budget['requests'] > 2000: fail('request_budget')
        try:
            result = request(method, path, body)
        except Exception as exc:
            status = getattr(exc, 'status', getattr(exc, 'code', None))
            fail('permission_denied' if status in (401, 403) else 'read_transport_failed')
        if not isinstance(result, dict): fail('malformed_response')
        if result.get('object') == 'error':
            fail('permission_denied' if result.get('status') in (401, 403) else 'read_api_failed')
        budget['bytes'] += len(encoded(result))
        if budget['bytes'] > 64 * MAX_BYTES: fail('response_budget')
        return result
    def pages(method, path, payload=None):
        cursor = None; seen = set()
        for _ in range(MAX_PAGES):
            body = deepcopy(payload) if payload is not None else None
            target = path
            if method == 'POST':
                body = body or {}; body['page_size'] = 100
                if cursor: body['start_cursor'] = cursor
            else:
                query = {'page_size': 100}
                if cursor: query['start_cursor'] = cursor
                target += '?' + urlencode(query)
            value = call(method, target, body)
            rows = value.get('results'); more = value.get('has_more')
            if value.get('object') != 'list' or not isinstance(rows, list) or len(rows) > 100 or type(more) is not bool: fail('incomplete_pagination')
            for row in rows:
                if not isinstance(row, dict): fail('malformed_response')
                yield row
            if not more:
                if value.get('next_cursor') is not None: fail('incomplete_pagination')
                return
            cursor = value.get('next_cursor')
            if not isinstance(cursor, str) or not cursor or len(cursor)>1024 or cursor in seen: fail('incomplete_pagination')
            seen.add(cursor)
        fail('pagination_budget')
    found = {}
    for row in pages('POST', '/search', {'filter': {'value': 'page', 'property': 'object'}}):
        if row.get('object') != 'page': fail('malformed_response')
        ident = uuid(row.get('id')); pin = sha(ident)
        if pin in pins:
            if pin in found: fail('duplicate_search_identity')
            found[pin] = ident
        if len(found) == len(pins): break
    if set(found) != set(pins): fail('target_not_visible')
    snapshots = {}; totals = 0
    for pin, ident in sorted(found.items()):
        page = call('GET', '/pages/' + ident)
        if page.get('object') != 'page' or uuid(page.get('id')) != ident or page.get('archived') is not False or page.get('in_trash', False) is not False: fail('wrong_page')
        parent = page.get('parent')
        if not isinstance(parent, dict): fail('wrong_parent')
        if parent.get('type') != 'data_source_id': fail('unsupported_parent_api_version')
        source = uuid(parent.get('data_source_id'))
        if sha(source) != data_source_pin: fail('wrong_parent')
        ds = call('GET', '/data_sources/' + source)
        if ds.get('object') != 'data_source' or uuid(ds.get('id')) != source: fail('wrong_parent')
        ds_parent = ds.get('parent')
        if not isinstance(ds_parent, dict) or sha(uuid(ds_parent.get('database_id'))) != database_pin: fail('wrong_database')
        schema = ds.get('properties')
        if not isinstance(schema, dict) or not isinstance(schema.get('笔记 ID'), dict) or schema['笔记 ID'].get('type') != 'rich_text': fail('wrong_schema')
        nid = note_id(page)
        if sha(nid) != pins[pin] or nid in snapshots: fail('wrong_note_identity')
        # Existing properties are protected. Conservatively refuse possibly truncated fields.
        props = page.get('properties')
        if not isinstance(props, dict): fail('incomplete_properties')
        for prop in props.values():
            if not isinstance(prop, dict): fail('incomplete_properties')
            kind = prop.get('type')
            if kind in ('rich_text','title','relation') and not isinstance(prop.get(kind), list): fail('incomplete_properties')
            if prop.get('has_more') or (kind in ('rich_text','title','relation') and len(prop[kind]) >= 25): fail('incomplete_properties')
        matches = []
        for row in pages('POST', '/data_sources/' + source + '/query', {'filter': {'property': '笔记 ID', 'rich_text': {'equals': nid}}}):
            if row.get('object') != 'page' or note_id(row) != nid: fail('identity_query_invalid')
            matches.append(uuid(row.get('id')))
        if matches != [ident]: fail('duplicate_or_missing_id')
        rows = []; seen = set()
        def children(parent_id, depth=0):
            if depth > 32: fail('depth_budget')
            ids = []
            for block in pages('GET', '/blocks/' + parent_id + '/children'):
                bid = uuid(block.get('id')); kind = block.get('type')
                if block.get('object') != 'block' or bid == ident or bid in seen: fail('invalid_block_tree')
                if block.get('archived') is not False or block.get('in_trash', False) is not False: fail('invalid_block_tree')
                actual = block.get('parent')
                if not isinstance(actual, dict): fail('wrong_block_parent')
                pk = actual.get('type')
                if pk not in ('page_id','block_id') or uuid(actual.get(pk)) != parent_id: fail('wrong_block_parent')
                if not isinstance(kind, str) or kind == 'unsupported' or not isinstance(block.get(kind), dict) or type(block.get('has_children')) is not bool: fail('unsupported_block')
                seen.add(bid); ids.append(bid)
                if len(seen) > MAX_BLOCKS: fail('block_budget')
                out = {'id': bid, 'parent_id': parent_id, 'type': kind, 'payload': deepcopy(block[kind]), 'children_ids': [], 'has_children': block['has_children']}
                rows.append(out)
                if block['has_children']:
                    out['children_ids'] = children(bid, depth+1)
                    if not out['children_ids']: fail('incomplete_children')
            return ids
        children(ident)
        final = call('GET', '/pages/' + ident)
        differences = page_difference_categories(page, final)
        if differences:
            error = Blocked('page_changed_during_read')
            error.difference_categories = differences
            raise error from None
        snapshot = {'complete': True, 'native_blocks_available': True, 'page_id': ident, 'parent_data_source': source, 'id_property': nid, 'identity_query_complete': True, 'exact_id_matches': matches, 'last_edited_time': page.get('last_edited_time'), 'properties_raw': deepcopy(props), 'recursive_blocks': rows, 'ownership_adopted': False}
        if not isinstance(snapshot['last_edited_time'], str) or len(encoded(snapshot)) > MAX_BYTES: fail('incomplete_target_read')
        snapshots[nid] = snapshot; totals += len(rows)
    # Hash raw snapshots only as an observation checksum, NEVER a managed baseline.
    return {'snapshots': snapshots, 'report': {'status':'native_read_verified','pages':len(snapshots),'blocks':totals,'requests':budget['requests'],'snapshot_sha256':sha(snapshots),'ownership_adopted':False,'external_writes':0}}
