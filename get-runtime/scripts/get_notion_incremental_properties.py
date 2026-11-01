"""Bounded four-field continuation of a NEW ordinary row's verified body.

No second writer, plan row or journal. The old observation facet and create-only
scope never activate this phase. Source-state-only publication holds are not
note-level inputs and are deliberately outside this contract.
"""
from copy import deepcopy
import get_notion_properties as props
import get_notion_property_transaction as tx
import get_notion_metadata_pending as metadata

CONTRACT = 'get-notion-incremental-properties-1'
KIND = 'body_verified_properties'
SCOPE = {'contract': CONTRACT, 'fields': sorted(props.FIELDS),
    'target': 'ordinary_outbox_body_verified_pages', 'fill_empty': True,
    'ownership': 'prior_verified_property_receipt_only',
    'schema_options': 'existing_only', 'title': 'observe_only',
    'source_state_only': 'hold_unprojected'}
PHASES = frozenset(('body_verified', 'prepared', 'in_flight', 'acknowledged', 'verified'))


def engine():
    import get_notion_executor
    return get_notion_executor


def validate_scope(scope):
    if scope != SCOPE: engine().fail('approval_missing')


def enabled(plan):
    return isinstance(plan, dict) and 'property_incremental_scope' in plan


def check_plan(plan):
    e = engine()
    validate_scope(plan.get('property_incremental_scope'))
    if (plan.get('property_incremental_scope_sha256') != e.digest(SCOPE)
            or not metadata.enabled(plan) or plan.get('property_only')
            or plan.get('historical_only') or plan['publication'].get('initial_cohort') is not None
            or not plan['publication'].get('outbox_receipt_sha256')
            or any(k.startswith('historical_') for k in plan['publication'])):
        e.fail('approval_missing')


def adapter(row, operation):
    """Pure wire-validator view, never an executable property-only plan row."""
    return {'property_only': True, 'note_id': row['note_id'],
        'page_id': operation['body_receipt']['page_id'],
        'property_patch': deepcopy(operation['intent']['patch']),
        **({'property_patch_date_wire': deepcopy(operation['intent']['date_wire'])}
           if operation['intent']['date_wire'] is not None else {})}


def outcome(row, plan, before, prior, schema_ok):
    e = engine(); bound = row['metadata_completion']
    view = e.target(before, row['note_id'], plan['policy']['data_source_id'])
    held = deepcopy(bound['held'])
    managed = deepcopy(prior.get('managed', {})) if prior else {}
    desired = props.date_wire_properties(bound['desired'], bound.get('date_wire'))
    safe = {name: value for name, value in desired.items() if name in props.FIELDS and name not in held}
    patch, next_managed, conflicts = props.three_way(safe, view['properties'], managed, fill_empty=True)
    held.update(conflicts)
    title = bound['title_property']
    if title in desired and title not in held:
        try:
            if props.value(view['properties'].get(title)) != props.value(desired[title]):
                held[title] = 'title_ownership_required'
        except ValueError:
            held[title] = 'property_schema_changed'
    if schema_ok is False:
        held.update({name: 'property_schema_changed' for name in set(desired) | set(held)})
        patch, next_managed = {}, managed
    elif schema_ok is None:
        if patch:
            held.update({name: 'property_schema_changed' for name in patch})
            patch, next_managed = {}, managed
        title_create = (row['action'] == 'create' and not bound['required_fields']
            and not bound['held'] and set(desired) == {title})
        if not held and not title_create: held[title] = 'metadata_evidence_required'
    # Exact source precision is immutable; only its explicit wire projection is
    # compared/written. Matching observations never become managed ownership.
    source_patch = {name: deepcopy(bound['desired'][name]) for name in patch}
    date_wire=props.date_wire_binding(source_patch)
    fingerprint=e.transport.fingerprint('PATCH','/pages/'+view['page_id'],
        {'properties':props.date_wire_properties(source_patch,date_wire)})
    return {'patch': source_patch, 'date_wire': date_wire,
        'request_fingerprint':fingerprint,
        'managed': next_managed, 'held': held, 'schema_ok': schema_ok}


def validate_operation(operation, row, plan, value):
    e = engine(); check_plan(plan)
    keys = {'kind', 'phase', 'body_receipt', 'before', 'prior_properties',
        'plan_sha256', 'facet_sha256', 'intent', 'response'}
    if (not isinstance(operation, dict) or set(operation) != keys or operation['kind'] != KIND
            or operation['phase'] not in PHASES or row['action'] == 'blocked'
            or operation['plan_sha256'] != e.digest(plan)
            or operation['facet_sha256'] != e.digest(row.get('metadata_completion'))):
        e.fail('state_invalid')
    bound = row['metadata_completion']; body = operation['body_receipt']; ident = row['note_id']
    if (not isinstance(bound, dict) or bound.get('contract') != metadata.CONTRACT
            or bound['source_commit'] != plan['publication']['commit']
            or bound['md_blob'] != row['source']['blob']
            or not e.same(body, value['receipts'].get(ident))
            or not e.same(operation['prior_properties'], value.get('property_receipts', {}).get(ident))
            or body['source_commit'] != bound['source_commit'] or body['md_blob'] != bound['md_blob']
            or body['data_source_id'] != plan['policy']['data_source_id']
            or row.get('page_id', body['page_id']) != body['page_id']):
        e.fail('state_invalid')
    view = e.target(operation['before'], ident, body['data_source_id'])
    proof = e.receipt(operation['before'], ident, body['data_source_id'],
        [b['id'] for b in body['managed_blocks']], plan['publication'], row['source'],
        row.get('preserved_asset_bindings'))
    if 'creation_plan_sha256' in body: proof['creation_plan_sha256'] = body['creation_plan_sha256']
    if view is None or not e.same(proof, body): e.fail('state_invalid')
    managed={b['id'] for b in body['managed_blocks']}
    roots=[root for root in view['roots'] if root in managed]
    if managed != {node for root in roots for node in e.subtree(view['nodes'],root)}:e.fail('state_invalid')
    first=view['roots'].index(roots[0]) if roots else 0
    previous=view['nodes'][view['roots'][first-1]]['type'] if first else None
    actual=[e.semantic(view['nodes'],root) for root in roots]
    if not e.same(e.p.comparison_semantics(actual,previous),e.p.comparison_semantics(row['rendered'],previous)):
        e.fail('state_invalid')
    prior = operation['prior_properties']
    if prior is not None and any(prior.get(key) != body[key] for key in ('page_id', 'data_source_id')):
        e.fail('state_invalid')
    if operation['phase'] == 'body_verified':
        if operation['intent'] is not None or operation['response'] is not None: e.fail('state_invalid')
        return
    intent = operation['intent']
    if (not isinstance(intent, dict) or intent.get('schema_ok') not in (True, False, None)
            or type(intent.get('schema_ok')) not in (bool, type(None))
            or not e.same(intent, outcome(row, plan, operation['before'], prior, intent['schema_ok']))):
        e.fail('state_invalid')
    tx.wire(adapter(row, operation))
    if operation['phase'] in ('prepared', 'in_flight') and operation['response'] is not None: e.fail('state_invalid')
    if operation['phase'] == 'acknowledged' and operation['response'] != body['page_id']: e.fail('state_invalid')
    if not intent['patch'] and (operation['phase'] in ('in_flight','acknowledged') or operation['response'] is not None):e.fail('state_invalid')
    if operation['phase']=='verified' and operation['response'] not in (None,body['page_id']):e.fail('state_invalid')


def same_schema(api, bound):
    e = engine(); schema = api.schema()
    return (e.digest(props.schema_projection(schema)) == bound['schema_sha256']
        and (bound.get('native_schema_sha256') is None or e.digest(schema) == bound['native_schema_sha256']))


def finish(row, plan, publication, api, store, value, version, counts,
           writers_guard, snapshot=None, authorize=None):
    """Return a resolved outcome; caller atomically advances the same row/CAS."""
    e = engine(); check_plan(plan); journal = value['journal']; ident = row['note_id']
    source = plan['policy']['data_source_id']; bound = row['metadata_completion']
    operation = journal['operation']
    if operation is None or operation.get('kind') != KIND:
        if snapshot is None: e.fail('state_invalid')
        operation = {'kind': KIND, 'phase': 'body_verified',
            'body_receipt': deepcopy(value['receipts'][ident]), 'before': deepcopy(snapshot),
            'prior_properties': deepcopy(value.get('property_receipts', {}).get(ident)),
            'plan_sha256': e.digest(plan), 'facet_sha256': e.digest(bound),
            'intent': None, 'response': None}
        journal['operation'] = operation
        validate_operation(operation, row, plan, value)
        version = store.compare_and_set(version, value)
    validate_operation(operation, row, plan, value)
    counts['body_verified'] += 1
    counts['metadata_pending'] += 1

    def guard(*, page=True):
        publication.check(plan['publication']); publication.check_assets(row.get('source_assets', {}))
        reason = e.content_scope.reason(ident, publication.read(ident))
        if reason: e.fail(reason)
        fresh, sha = store.read()
        if sha != version or e.digest(fresh) != version: e.fail('state_changed')
        if e.plan_recovery.held(fresh, ident, operation['body_receipt']['page_id']): e.fail('unresolved_operation')
        if not e.p.writer_gate({'old_writer_evidence': writers_guard()}): e.fail('writer_gate_unknown')
        if not same_schema(api, bound): e.fail('property_schema_changed')
        if page and e.target_sha(e.read_target(api, ident, source), ident, source) != e.target_sha(operation['before'], ident, source):
            e.fail('target_changed')

    if operation['phase'] == 'body_verified':
        # A source-held field/title does not suppress safe siblings. A fully
        # held/no-write outcome avoids another full native/image read.
        preview = outcome(row, plan, operation['before'], operation['prior_properties'], True)
        schema_ok = None
        if bound['schema_sha256'] is not None and (preview['patch'] or not preview['held']):
            try: schema_ok = same_schema(api, bound)
            except e.transport.Failure as exc:
                e.fail(str(exc) if str(exc) in e.REASONS else 'transport_read_failed')
            except ValueError: schema_ok = False
            fresh = e.read_target(api, ident, source)
            snapshot = fresh
            if e.target_sha(fresh, ident, source) != e.target_sha(operation['before'], ident, source):
                e.fail('target_changed')
        elif bound['schema_sha256'] is not None:
            # Equality/known holds do not authorize a request.
            schema_ok = None
        operation['intent'] = outcome(row, plan, operation['before'], operation['prior_properties'], schema_ok)
        operation['phase'] = 'prepared'
        if operation['intent']['patch']:
            version = store.compare_and_set(version, value)
    intent = operation['intent']; wire_row = adapter(row, operation); spec = tx.wire(wire_row)
    key = e.transport.fingerprint(spec['method'], spec['path'], spec['body'])
    if operation['phase'] == 'prepared' and intent['patch']:
        guard()
        operation['phase'] = 'in_flight'; version = store.compare_and_set(version, value)
        try:
            guard()
            if authorize is not None: authorize(row, operation, spec)
        except Exception:
            # This invocation has not called the API. Failure to persist this
            # restoration leaves an uncertain journal, never an unsafe retry.
            operation['phase'] = 'prepared'; version = store.compare_and_set(version, value)
            raise
        try:
            counts['write_calls'] += 1
            response = api.update_properties(operation['body_receipt']['page_id'], spec['body']['properties'])
        except e.transport.NotDispatched as exc:
            if exc.request_fingerprint != key: e.fail('uncertain_properties')
            counts['write_calls'] -= 1
            operation['phase'] = 'prepared'; version = store.compare_and_set(version, value)
            e.fail('request_not_dispatched')
        except Exception:
            e.fail('uncertain_properties')
        if response != operation['body_receipt']['page_id']: e.fail('uncertain_properties')
        operation.update(phase='acknowledged', response=response)
        version = store.compare_and_set(version, value)
    if intent['patch']:
        # Unknown/acknowledged attempts only reconcile. Never call PATCH again.
        guard(page=False)
        final = e.read_target(api, ident, source)
        try: tx.verify_after(operation['before'], final, wire_row, source)
        except e.Blocked:
            if operation['phase'] == 'in_flight': e.fail('uncertain_properties')
            raise
    else:
        # In the uninterrupted path this is the caller's strict body snapshot,
        # or the fresh post-schema observation. On resume, refresh it.
        final = snapshot if snapshot is not None else e.read_target(api, ident, source)
        tx.verify_after(operation['before'], final, wire_row, source)
    publication.check(plan['publication']); publication.check_assets(row.get('source_assets', {}))
    operation['phase'] = 'verified'
    view = e.target(final, ident, source)
    body = deepcopy(operation['body_receipt'])
    # Observed properties only: no body rewrite, block ownership change or
    # title ownership. Metadata completion hashes this exact observation.
    body['properties_baseline'] = deepcopy(view['properties'])
    value['receipts'][ident] = body
    held4 = {name: reason for name, reason in intent['held'].items() if name in props.FIELDS}
    prior = operation['prior_properties'] or {}
    if intent['patch'] or held4 != prior.get('held', {}):
        value.setdefault('property_receipts', {})[ident] = {'contract': props.CONTRACT,
            'page_id': body['page_id'], 'data_source_id': source,
            'source_commit': bound['source_commit'], 'md_blob': bound['md_blob'],
            'manifest_sha256': plan['property_incremental_scope_sha256'],
            'managed': deepcopy(intent['managed']), 'held': held4, 'readback_verified': True}
    value.setdefault('metadata_results', {})[ident] = {'contract': metadata.CONTRACT,
        'source_commit': bound['source_commit'], 'md_blob': bound['md_blob'],
        'page_id': body['page_id'], 'data_source_id': source,
        'plan_sha256': e.digest(plan), 'facet_sha256': e.digest(bound),
        'properties_sha256': e.digest(view['properties']),
        'held': deepcopy(intent['held']), 'readback_verified': True,
        'property_write_verified': bool(intent['patch'])}
    counts['metadata_pending'] -= not bool(intent['held'])
    counts['properties_updated'] += bool(intent['patch'])
    counts['retained_conflicts'] += bool(intent['held'])
    return version
