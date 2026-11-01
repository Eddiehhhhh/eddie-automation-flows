"""Hydrate API-visible Notion properties through the caller's read-only client.

Keeps the existing API version and request budget. Source JSON is preserved;
the hydrated page is a derived view. Pagination semantics:
https://developers.notion.com/reference/property-item-object
"""
from copy import deepcopy
import urllib.parse

PROOF_VERSION = 1
MAX_PROPERTY_PAGES = 1000
LIST_TYPES = {'title', 'rich_text', 'people', 'relation'}
DIRECT_TYPES = {'number', 'checkbox', 'select', 'status', 'date', 'url', 'email',
                'phone_number', 'created_by', 'created_time', 'last_edited_by',
                'last_edited_time', 'multi_select', 'files', 'unique_id',
                'verification', 'button', 'place'}


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _property_id(value):
    _require(isinstance(value, str) and bool(value), 'property_id_missing')
    return urllib.parse.unquote(value)


def _item(row, kind=None):
    _require(isinstance(row, dict) and row.get('object') == 'property_item', 'property_item_shape')
    _property_id(row.get('id'))
    actual = row.get('type')
    _require(isinstance(actual, str) and actual in row, 'property_item_shape')
    _require(kind is None or actual == kind, 'property_type_changed')
    _require(actual in LIST_TYPES | DIRECT_TYPES | {'rollup', 'formula'}, 'property_type_unknown')
    if actual in ('title', 'rich_text'):
        rich = row[actual]
        _require(isinstance(rich, dict), 'property_rich_text_shape')
        rich_kind = rich.get('type')
        _require(isinstance(rich_kind, str) and rich_kind in ('text', 'mention', 'equation')
                 and isinstance(rich.get(rich_kind), dict), 'property_rich_text_shape')
    elif actual in ('people', 'relation'):
        _require(isinstance(row[actual], dict) and isinstance(row[actual].get('id'), str), 'property_reference_shape')


def _read_property(page_id, prop, request):
    kind = prop['type']
    ident = _property_id(prop['id'])
    path = '/pages/' + urllib.parse.quote(page_id, safe='') + '/properties/' + urllib.parse.quote(ident, safe='')
    cursor = None
    seen = set()
    rows = []
    envelopes = []
    for _ in range(MAX_PROPERTY_PAGES):
        query = {'page_size': 100}
        if cursor is not None:
            query['start_cursor'] = cursor
        # BudgetExceeded and transport/authentication errors belong to the caller.
        response = request('GET', path + '?' + urllib.parse.urlencode(query))
        _require(isinstance(response, dict), 'property_response_shape')
        envelopes.append(deepcopy(response))
        if kind == 'formula':
            _item(response, 'formula')
            _require(_property_id(response['id']) == ident, 'property_response_id_changed')
            formula = response['formula']
            _require(isinstance(formula, dict), 'property_formula_shape')
            form_type = formula.get('type')
            _require(form_type != 'unsupported', 'property_formula_unsupported')
            _require(form_type in ('string', 'number', 'boolean', 'date') and form_type in formula,
                     'property_formula_incomplete')
            return deepcopy(formula), envelopes
        _require(response.get('object') == 'list' and response.get('type') == 'property_item'
                 and isinstance(response.get('results'), list) and type(response.get('has_more')) is bool
                 and 'next_cursor' in response,
                 'property_list_shape')
        metadata = response.get('property_item')
        _require(isinstance(metadata, dict) and metadata.get('type') == kind, 'property_type_changed')
        _require('next_url' in metadata, 'property_list_shape')
        _require(_property_id(metadata.get('id')) == ident, 'property_response_id_changed')
        for row in response['results']:
            _item(row, kind if kind != 'rollup' else None)
            if kind != 'rollup':
                _require(_property_id(row['id']) == ident, 'property_response_id_changed')
        # An item's id identifies the property, not the row. Never deduplicate it.
        rows.extend(deepcopy(response['results']))
        next_cursor = response.get('next_cursor')
        if response['has_more']:
            _require(isinstance(next_cursor, str) and bool(next_cursor) and next_cursor not in seen,
                     'property_cursor_incomplete')
            seen.add(next_cursor)
            cursor = next_cursor
            continue
        _require(next_cursor is None and metadata.get('next_url') is None, 'property_terminal_inconsistent')
        if kind == 'rollup':
            rollup = deepcopy(metadata.get('rollup'))
            _require(isinstance(rollup, dict) and isinstance(rollup.get('function'), str), 'property_rollup_shape')
            roll_type = rollup.get('type')
            _require(roll_type != 'unsupported', 'property_rollup_unsupported')
            _require(roll_type in ('array', 'number', 'date') and roll_type in rollup,
                     'property_rollup_incomplete')
            if roll_type == 'array':
                rollup['array'] = [{'type': row['type'], row['type']: deepcopy(row[row['type']])} for row in rows]
            return rollup, envelopes
        return [deepcopy(row[kind]) for row in rows], envelopes
    raise ValueError('property_pagination_budget_exceeded')


def hydrate_page(page, request):
    """Return original_page, property_responses, hydrated_page and proof_version.

    request(method, path, payload=None) supplies authentication, serial throttling,
    API version, retries and budget. This helper calls GET only, never source writes.
    Complete means API-visible values, not inaccessible references or UI automation.
    """
    _require(isinstance(page, dict) and isinstance(page.get('id'), str) and bool(page['id'])
             and isinstance(page.get('properties'), dict), 'property_page_shape')
    original = deepcopy(page)
    hydrated = deepcopy(original)
    responses = {}
    seen = set()
    for name, prop in hydrated['properties'].items():
        _require(isinstance(prop, dict), 'property_shape')
        ident = _property_id(prop.get('id'))
        _require(ident not in seen, 'property_duplicate_id')
        seen.add(ident)
        kind = prop.get('type')
        _require(isinstance(kind, str) and kind in LIST_TYPES | DIRECT_TYPES | {'rollup', 'formula'},
                 'property_type_unknown')
        _require(kind in prop, 'property_value_missing')
        if kind in DIRECT_TYPES:
            continue
        if kind == 'relation' and prop.get('has_more') is False:
            _require(isinstance(prop['relation'], list) and all(isinstance(v, dict)
                     and isinstance(v.get('id'), str) for v in prop['relation']), 'property_reference_shape')
            continue
        value, raw = _read_property(original['id'], prop, request)
        prop[kind] = value
        if kind == 'relation':
            prop['has_more'] = False
        responses[prop['id']] = raw
    return {'original_page': original, 'property_responses': responses,
            'hydrated_page': hydrated, 'proof_version': PROOF_VERSION}
