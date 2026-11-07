"""Read-only completion fence for NEW ordinary outbox plans.

This is not a property writer or an ownership/adoption receipt. Native equality
can finish this observation only; differing metadata stays held in the same CAS
as body completion and queue rotation. Old immutable plans are never migrated.
"""
from copy import deepcopy
import json
import get_notion_properties as props

CONTRACT = 'get-notion-metadata-completion-1'
REASONS = frozenset(('metadata_sync_pending', 'metadata_evidence_required',
    'title_ownership_required')) | props.REASONS


def engine():
    import get_notion_executor
    return get_notion_executor


def eligible(publication):
    d = publication.descriptor
    return (d.get('initial_cohort') is None and 'outbox_receipt_sha256' in d
        and not any(k.startswith('historical_') for k in d))


def enabled(plan):
    return isinstance(plan, dict) and plan.get('metadata_completion_contract') == CONTRACT


def facet(publication, ident, data, policy, metadata, state, native_schema=None):
    e = engine(); source = publication.descriptor['records'][ident]
    desired, held = {}, {}
    try:
        fields = props.source_fields(data, ident)
        header = data.decode().split('\n---\n', 1)[0]
        titles = [x[7:] for x in header.splitlines() if x.startswith('title: ')]
        if len(titles) != 1: raise ValueError()
        try: title = json.loads(titles[0])
        except ValueError: title = titles[0]
        if not isinstance(title, str) or not title: raise ValueError()
        desired[policy['title_property']] = {'type': 'title', 'title': [e.p.text_run(title)]}
    except Exception:
        fields = {}; held[policy['title_property']] = 'property_source_invalid'
    required = {name for name, (key, _) in props.FIELDS.items() if key in fields}
    # Missing values must not erase an earlier metadata obligation/owned field.
    prior = state.get('property_receipts', {}).get(ident, {})
    required.update(prior.get('managed', {})); required.update(prior.get('held', {}))
    previous = state.get('metadata_results', {}).get(ident, {})
    required.update(name for name in previous.get('held', {}) if name in props.FIELDS)
    schema_sha = None; assets = {}
    if metadata is not None:
        required.update(props.FIELDS)
    if required:
        if metadata is None:
            held.update({name: 'property_schema_changed' for name in required})
        else:
            projection, times, selected, binding = metadata
            if ident not in selected: e.fail('property_source_invalid')
            projected, failures = props.project(data, ident, projection, times.get(ident))
            desired.update({name: value for name, value in projected.items() if name in required})
            held.update({name: failures[name] for name in required if name in failures})
            schema_sha = binding['schema_sha256']
            if binding['registry'] is not None:
                assets[props.REGISTRY] = binding['registry']['blob']
    result = {'contract': CONTRACT, 'source_commit': publication.descriptor['commit'],
        'md_blob': source['blob'], 'title_property': policy['title_property'],
        'desired': desired, 'held': held, 'required_fields': sorted(required),
        'schema_sha256': schema_sha, 'source_assets': assets,
        'native_schema_sha256': e.digest(native_schema) if native_schema is not None else None}
    date_binding = props.date_wire_binding(desired)
    if date_binding is not None: result['date_wire'] = date_binding
    return result


def record(value, row, plan, snapshot, api, counts):
    """After strict native body readback, before its one atomic completion CAS."""
    if not enabled(plan): return
    e = engine(); ident = row['note_id']; bound = row.get('metadata_completion')
    if not isinstance(bound, dict) or bound.get('contract') != CONTRACT: e.fail('state_invalid')
    if bound['source_commit'] != plan['publication']['commit'] or bound['md_blob'] != row['source']['blob']:
        e.fail('state_invalid')
    view = e.target(snapshot, ident, plan['policy']['data_source_id'])
    body = value['receipts'].get(ident, {})
    if view is None or any(body.get(k) != v for k, v in (
            ('page_id', view['page_id']), ('data_source_id', view['data_source_id']),
            ('source_commit', bound['source_commit']), ('md_blob', bound['md_blob']))):
        e.fail('state_invalid')
    held = deepcopy(bound['held'])
    schema_checked = not held and bound['schema_sha256'] is not None
    if schema_checked:
        try:
            fresh_schema = api.schema()
            same_schema = (e.digest(props.schema_projection(fresh_schema)) == bound['schema_sha256']
                and (bound.get('native_schema_sha256') is None or e.digest(fresh_schema) == bound['native_schema_sha256']))
        except Exception: same_schema = False
        if not same_schema:
            held.update({name: 'property_schema_changed' for name in bound['desired']})
            held.update({name: 'property_schema_changed' for name in list(held)})
    # Schema reading is not atomic with page reading. Re-read after it and
    # reject drift before the completion CAS, without retrying a body write.
    # No intervening schema read means the caller's strict final snapshot is
    # already fresh. Known source-held rows cannot complete, so do not spend a
    # second full native/image read merely to rediscover their held status.
    if schema_checked:
        fresh = e.read_target(api, ident, plan['policy']['data_source_id'])
        if e.target_sha(fresh, ident, plan['policy']['data_source_id']) != e.digest(view):
            e.fail('target_changed')
    desired = props.date_wire_properties(bound['desired'], bound.get('date_wire'))
    prior = value.get('property_receipts', {}).get(ident, {})
    managed = prior.get('managed', {}) if (prior.get('page_id') == view['page_id']
        and prior.get('data_source_id') == view['data_source_id']) else {}
    for name, wanted in desired.items():
        if name in held: continue
        try:
            actual = props.value(view['properties'].get(name))
            if actual == props.value(wanted): continue  # Never creates ownership.
            if name == bound['title_property']:
                held[name] = 'title_ownership_required'
            elif name in managed and actual != props.value(managed[name]):
                held[name] = 'property_manual_edit'
            elif name not in managed and not props.empty(view['properties'][name]):
                held[name] = 'property_unowned_value'
            else:
                held[name] = 'metadata_sync_pending'
        except Exception:
            held[name] = 'property_schema_changed'
    # The only no-schema completion is an actual current title-only CREATE.
    # Its exact immutable create request and strict native creation readback
    # already prove the title. Existing pages and prior metadata obligations
    # cannot borrow this exception or turn missing schema into 'unchanged'.
    title_only_create = (row.get('action') == 'create' and not bound['required_fields']
        and not bound['held'] and set(bound['desired']) == {bound['title_property']})
    if not held and not schema_checked and not title_only_create:
        held[bound['title_property']] = 'metadata_evidence_required'
    value.setdefault('metadata_results', {})[ident] = {
        'contract': CONTRACT, 'source_commit': bound['source_commit'], 'md_blob': bound['md_blob'],
        'page_id': view['page_id'], 'data_source_id': view['data_source_id'],
        'plan_sha256': e.digest(plan), 'facet_sha256': e.digest(bound),
        'properties_sha256': e.digest(view['properties']), 'held': held, 'readback_verified': True}
    counts['body_verified'] += 1
    counts['metadata_pending'] += bool(held)
    if held and not row.get('held_fields'): counts['retained_conflicts'] += 1


def complete(state, plan, row):
    """The only additional dequeue predicate; legacy active plans stay exact."""
    if not enabled(plan): return True
    e = engine(); result = state.get('metadata_results', {}).get(row['note_id'], {})
    body = state['receipts'].get(row['note_id'], {})
    return (row.get('action') != 'blocked' and isinstance(row.get('metadata_completion'), dict)
        and result.get('readback_verified') is True and not result.get('held')
        and result.get('plan_sha256') == e.digest(plan)
        and result.get('facet_sha256') == e.digest(row['metadata_completion'])
        and result.get('source_commit') == plan['publication']['commit']
        and result.get('md_blob') == row['source']['blob']
        and all(result.get(k) == body.get(k) for k in ('page_id', 'data_source_id'))
        and result.get('properties_sha256') == e.digest(body.get('properties_baseline')))


def check_results(results):
    e = engine()
    if not isinstance(results, dict) or len(results) > 2500: e.fail('state_invalid')
    keys = {'contract', 'source_commit', 'md_blob', 'page_id', 'data_source_id',
        'plan_sha256', 'facet_sha256', 'properties_sha256', 'held', 'readback_verified'}
    for ident, row in results.items():
        e.p.note_id(ident)
        if not isinstance(row, dict) or not keys <= set(row) <= keys | {'property_write_verified'} or row['contract'] != CONTRACT or row['readback_verified'] is not True:
            e.fail('state_invalid')
        if 'property_write_verified' in row and type(row['property_write_verified']) is not bool:e.fail('state_invalid')
        for key in ('source_commit', 'md_blob'): e.p.hex_hash(row[key], 40)
        for key in ('page_id', 'data_source_id'): e.p.notion_id(row[key])
        for key in ('plan_sha256', 'facet_sha256', 'properties_sha256'): e.p.hex_hash(row[key], 64)
        if (not isinstance(row['held'], dict) or len(row['held']) > 5
                or any(not isinstance(name, str) or not name or reason not in REASONS for name, reason in row['held'].items())):
            e.fail('state_invalid')


SOURCE_STATE_CONTRACT = 'get-notion-source-state-unprojected-1'
MAX_SOURCE_STATE_HOLDS = 500


def check_source_state_holds(rows):
    """Publication-level signals, never a count/list of unsynced notes."""
    e = engine()
    if not isinstance(rows, dict) or len(rows) > MAX_SOURCE_STATE_HOLDS: e.fail('state_invalid')
    for receipt_sha, row in rows.items():
        e.p.hex_hash(receipt_sha, 64)
        if (not isinstance(row, dict) or set(row) != {'contract','commit','parent','receipt_sha256',
                'sequence','legacy_source_version_only','new_state_only','reason'}
                or row['contract'] != SOURCE_STATE_CONTRACT
                or row['reason'] != 'source_state_only_unprojected'
                or row['receipt_sha256'] != receipt_sha): e.fail('state_invalid')
        for key in ('commit','parent'): e.p.hex_hash(row[key], 40)
        if type(row['sequence']) is not int or not 1 <= row['sequence'] <= 1000000: e.fail('state_invalid')
        for key in ('legacy_source_version_only','new_state_only'):
            if type(row[key]) is not int or not 0 <= row[key] <= 2500: e.fail('state_invalid')
        if row['legacy_source_version_only'] + row['new_state_only'] == 0: e.fail('state_invalid')


def retain_source_state_signal(value, publication, receipt):
    """Only called after full receipt/provenance/delta verification in enqueue.

    A changed source object with identical Markdown can be mere freshness. The
    receipt gives no note IDs or field deltas: hold for classification, do not
    pretend it authorizes a property patch or proves a visible Notion mismatch.
    """
    counts = {key: receipt['counts'][key] for key in ('legacy_source_version_only','new_state_only')}
    if not any(counts.values()): return
    row = {'contract': SOURCE_STATE_CONTRACT, **deepcopy(publication),
        'sequence': receipt['sequence'], **counts, 'reason': 'source_state_only_unprojected'}
    holds = value.setdefault('source_state_holds', {})
    prior = holds.get(publication['receipt_sha256'])
    if prior is not None and prior != row: engine().fail('state_invalid')
    if prior is None and len(holds) >= MAX_SOURCE_STATE_HOLDS:
        engine().fail('source_state_hold_budget')
    holds[publication['receipt_sha256']] = row
    check_source_state_holds(holds)
