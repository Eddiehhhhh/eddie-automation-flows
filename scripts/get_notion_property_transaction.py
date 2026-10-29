"""Exact-manifest metadata backfill using the existing durable engine journal.

No CLI, credentials, schema writes or automatic activation. The caller supplies
an independently approved manifest hash and a trusted source publication. Body
receipts and pending body work are never advanced by a property-only result.
"""
from copy import deepcopy
import json

import get_notion_properties as props

CONTRACT = 'get-notion-property-scope-1'


def engine():
    import get_notion_executor
    return get_notion_executor


def registry_timestamps(publication, binding, selected):
    e = engine()
    if binding is None: return {}
    if (not isinstance(binding, dict) or set(binding) != {'path', 'blob', 'sha256'}
            or binding['path'] != props.REGISTRY): e.fail('property_source_invalid')
    if publication.descriptor['content_tree'].get(props.REGISTRY) != binding['blob']: e.fail('property_source_invalid')
    data = publication.repo.read(publication.descriptor['commit'], props.REGISTRY, 16 * 1024 * 1024)
    if e.p.git_blob(data) != binding['blob'] or e.digest(data) != binding['sha256']: e.fail('property_source_invalid')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result: e.fail('property_source_invalid')
            result[key] = item
        return result
    try: registry = json.loads(data, object_pairs_hook=unique)
    except Exception: e.fail('property_source_invalid')
    result = {}
    for ident, source in selected.items():
        row = registry.get('newById', {}).get(ident)
        if row is None: continue
        if (not isinstance(row, dict) or row.get('baseline_blob') != source['blob']
                or row.get('mirror_sha256') != source['sha256']
                or row.get('filePath') != source['path'].removeprefix('Raw/03 Get/')): e.fail('property_source_invalid')
        times = row.get('source_metadata', {}).get('timestamps', {})
        if not isinstance(times, dict) or set(times) - {'created_at', 'edited_at', 'record_updated_at'}: e.fail('property_source_invalid')
        # record_updated_at is sync freshness, never user modification time.
        result[ident] = {k: times[k] for k in ('created_at', 'edited_at') if k in times}
    publication.check_assets({binding['path']: binding['blob']})
    return result


def context(publication, schema, manifest, approved_manifest_sha256, policy):
    e = engine()
    keys = {'contract', 'data_source_id', 'schema_sha256', 'fields', 'fill_empty', 'records', 'registry', 'source_commit'}
    if (not isinstance(manifest, dict) or set(manifest) != keys or manifest['contract'] != CONTRACT
            or e.digest(manifest) != approved_manifest_sha256 or manifest['fields'] != sorted(props.FIELDS)
            or manifest['fill_empty'] is not True): e.fail('approval_missing')
    e.p.hex_hash(approved_manifest_sha256, 64)
    source = e.p.notion_id(manifest['data_source_id'])
    if policy.get('data_source_id') != source or not e.p.writer_gate(policy): e.fail('approval_missing')
    projection = props.schema_projection(schema)
    if e.digest(projection) != manifest['schema_sha256']: e.fail('property_schema_changed')
    wanted = manifest['records']
    if not isinstance(wanted, dict) or not 1 <= len(wanted) <= e.MAX_RECORDS: e.fail('request_budget')
    publication.check(publication.descriptor)
    if manifest['source_commit'] != publication.descriptor['commit']: e.fail('source_invalid')
    selected = {}
    for ident, binding in wanted.items():
        e.p.note_id(ident)
        if not isinstance(binding, dict) or not {'path', 'blob', 'sha256', 'privacy_review_sha256'} <= set(binding) <= {'path', 'blob', 'sha256', 'privacy_review_sha256', 'property_adoption'}: e.fail('approval_missing')
        e.p.hex_hash(binding['privacy_review_sha256'], 64)
        row = {k: binding[k] for k in ('path', 'blob', 'sha256')}
        if publication.descriptor['records'].get(ident) != row: e.fail('source_invalid')
        selected[ident] = row
    times = registry_timestamps(publication, manifest['registry'], selected)
    return projection, times, selected


def prepare(publication, state, expected_state_sha, snapshots, schema, manifest,
            approved_manifest_sha256, policy):
    """Pure plan. Hashing an observation is not approval or page ownership."""
    e = engine()
    e.check_state(state)
    if e.digest(state) != expected_state_sha or state['journal'] is not None: e.fail('state_changed')
    projection, times, selected = context(publication, schema, manifest, approved_manifest_sha256, policy)
    source = manifest['data_source_id']
    records = []; skipped_conflicts = []
    for ident, source_row in sorted(selected.items()):
        prior = state.get('property_receipts', {}).get(ident)
        if prior and prior['manifest_sha256'] == approved_manifest_sha256 and prior['source_commit'] == publication.descriptor['commit'] and prior['md_blob'] == source_row['blob']:
            if prior['held']: skipped_conflicts.append(ident)
            continue
        data = publication.read(ident)
        reason = e.content_scope.reason(ident, data)
        if reason: e.fail(reason)
        snapshot = snapshots.get(ident)
        view = e.target(snapshot, ident, source)
        owned = state['receipts'].get(ident)
        if (view is None or not owned or owned['page_id'] != view['page_id']
                or owned['data_source_id'] != source or owned['md_blob'] != source_row['blob']): e.fail('missing_block_ownership')
        if e.plan_recovery.held(state, ident, view['page_id']): e.fail('unresolved_operation')
        if prior and (prior['page_id'] != view['page_id'] or prior['data_source_id'] != source): e.fail('missing_block_ownership')
        desired, held = props.project(data, ident, projection, times.get(ident))
        managed_before = deepcopy(prior['managed']) if prior else {}
        adoption = manifest['records'][ident].get('property_adoption')
        if adoption is not None:
            if (not isinstance(adoption, dict) or set(adoption) != {'page_id', 'snapshot_sha256', 'prior_writer_proof_sha256', 'fields'}
                    or adoption['page_id'] != view['page_id'] or adoption['snapshot_sha256'] != e.digest(view)
                    or not isinstance(adoption['fields'], dict) or not adoption['fields']
                    or set(adoption['fields']) - set(props.FIELDS) or set(adoption['fields']) & set(managed_before)):
                e.fail('missing_block_ownership')
            e.p.hex_hash(adoption['prior_writer_proof_sha256'], 64)
            for name, baseline in adoption['fields'].items():
                if props.value(view['properties'].get(name)) != props.value(baseline): e.fail('human_edit_conflict')
                managed_before[name] = props.value(baseline)
        # Compare the explicit native precision, retaining exact source values
        # in the immutable patch and binding its distinct wire representation.
        projected = props.date_wire_properties(desired, props.date_wire_binding(desired))
        wire_patch, managed, conflicts = props.three_way(projected, view['properties'], managed_before, fill_empty=True)
        patch = {name: deepcopy(desired[name]) for name in wire_patch}
        held.update(conflicts)
        records.append({'note_id': ident, 'source': source_row, 'page_id': view['page_id'],
            'action': 'update' if patch else 'noop', 'reason': None, 'property_only': True,
            'before': deepcopy(snapshot), 'before_sha256': e.digest(view), 'property_patch': patch,
            'property_managed': managed, 'held_fields': held, 'property_schema_sha256': manifest['schema_sha256'],
            'source_assets': {} if manifest['registry'] is None else {props.REGISTRY: manifest['registry']['blob']}})
        binding = props.date_wire_binding(patch)
        if binding is not None: records[-1]['property_patch_date_wire'] = binding
    return {'contract': e.CONTRACT, 'publication': deepcopy(publication.descriptor),
        'state_sha256': expected_state_sha, 'rules_sha256': e.rules_sha256(), 'policy': deepcopy(policy),
        'policy_sha256': e.digest({'policy': policy, 'property_manifest_sha256': approved_manifest_sha256}),
        'initial_scope_sha256': e.digest({}), 'retry_conflicts': False, 'records': records,
        'deferred_ids': [], 'skipped_conflicts': skipped_conflicts, 'state_only': True, 'source_mode': 'property_backfill',
        'property_only': True, 'property_manifest': deepcopy(manifest), 'property_manifest_sha256': approved_manifest_sha256}


def creation_wire_properties(row):
    """Exact approved request values; old unbound plan requests stay unchanged."""
    return props.date_wire_properties(row['properties'], row.get('property_date_wire'))


def creation_readback_properties(row, expected_page):
    """Only the exact activated legacy row/page bridge may differ from wire."""
    legacy = engine().run57_recovery.creation_properties(row, expected_page)
    return legacy if legacy is not None else creation_wire_properties(row)


def patch_wire_properties(row):
    return props.date_wire_properties(row['property_patch'], row.get('property_patch_date_wire'))


def wire(row):
    e = engine()
    if not row.get('property_only') or not isinstance(row.get('property_patch'), dict) or set(row['property_patch']) - set(props.FIELDS): e.fail('approval_missing')
    for name, value in row['property_patch'].items():
        if value.get('type') != props.FIELDS[name][1] or props.empty(value): e.fail('approval_missing')
    body = {'properties': patch_wire_properties(row)}
    if len(e.encoded(body)) > 32 * 1024: e.fail('request_budget')
    return {'method': 'PATCH', 'path': '/pages/' + e.p.notion_id(row['page_id']), 'body': body}


def verify_after(before_snapshot, snapshot, row, data_source):
    e = engine()
    before, after = e.target(before_snapshot, row['note_id'], data_source), e.target(snapshot, row['note_id'], data_source)
    if before is None or after is None: e.fail('readback_mismatch')
    for key in ('page_id', 'data_source_id', 'note_id', 'nodes', 'roots', 'order', 'semantic'):
        if not e.same(before[key], after[key]): e.fail('readback_mismatch')
    if set(before['properties']) != set(after['properties']): e.fail('readback_mismatch')
    expected_patch = patch_wire_properties(row)
    for name, actual in after['properties'].items():
        if name in row['property_patch']:
            try: matches = props.value(actual) == props.value(expected_patch[name])
            except ValueError: matches = False
            if not matches: e.fail('readback_mismatch')
        elif not e.same(actual, before['properties'][name]): e.fail('readback_mismatch')
    return after


def execute_row(row, plan, publication, api, store, value, version, counts, writers_guard):
    e = engine()
    journal = value['journal']; source = plan['policy']['data_source_id']; ident = row['note_id']
    if (not plan.get('property_only') or e.digest(plan.get('property_manifest')) != plan.get('property_manifest_sha256')
            or row['property_schema_sha256'] != plan['property_manifest']['schema_sha256']): e.fail('approval_missing')
    spec = wire(row); key = e.transport.fingerprint(**{'method': spec['method'], 'path': spec['path'], 'body': spec['body']})
    def guard():
        publication.check(plan['publication']); publication.check_assets(row['source_assets'])
        if e.content_scope.reason(ident, publication.read(ident)): e.fail('content_review_required')
        fresh, fresh_sha = store.read()
        if fresh_sha != version or e.digest(fresh) != version: e.fail('state_changed')
        if e.plan_recovery.held(fresh, ident, row['page_id']): e.fail('unresolved_operation')
        if not e.p.writer_gate({'old_writer_evidence': writers_guard()}): e.fail('writer_gate_unknown')
        if e.digest(props.schema_projection(api.schema())) != row['property_schema_sha256']: e.fail('property_schema_changed')
    operation = journal['operation']
    if operation is None:
        snapshot = e.read_target(api, ident, source)
        if e.target_sha(snapshot, ident, source) != row['before_sha256']: e.fail('target_changed')
        operation = {'kind': 'properties', 'phase': 'prepared', 'before': snapshot, 'response': None}
        journal['operation'] = operation
        version = store.compare_and_set(version, value)
    if not isinstance(operation, dict) or set(operation) != {'kind', 'phase', 'before', 'response'} or operation['kind'] != 'properties' or operation['phase'] not in ('prepared', 'in_flight', 'acknowledged'): e.fail('state_invalid')
    if e.target_sha(operation['before'], ident, source) != row['before_sha256']: e.fail('state_invalid')
    if operation['phase'] == 'prepared':
        guard()
        if e.target_sha(e.read_target(api, ident, source), ident, source) != row['before_sha256']: e.fail('target_changed')
        if row['property_patch']:
            operation['phase'] = 'in_flight'; version = store.compare_and_set(version, value)
            try:
                guard()
                if e.target_sha(e.read_target(api, ident, source), ident, source) != row['before_sha256']: e.fail('target_changed')
            except Exception:
                operation['phase'] = 'prepared'; version = store.compare_and_set(version, value)
                raise
            try:
                counts['write_calls'] += 1
                response = api.update_properties(row['page_id'], patch_wire_properties(row))
            except e.transport.NotDispatched as exc:
                if exc.request_fingerprint != key: e.fail('uncertain_properties')
                counts['write_calls'] -= 1
                operation['phase'] = 'prepared'; version = store.compare_and_set(version, value)
                e.fail('request_not_dispatched')
            except Exception:
                e.fail('uncertain_properties')
            if response != row['page_id']: e.fail('uncertain_properties')
            operation.update(phase='acknowledged', response=response)
            version = store.compare_and_set(version, value)
    snapshot = e.read_target(api, ident, source)
    try: verify_after(operation['before'], snapshot, row, source)
    except e.Blocked:
        if operation['phase'] == 'in_flight' and e.target_sha(snapshot, ident, source) == row['before_sha256']: e.fail('uncertain_properties')
        raise
    publication.check(plan['publication']); publication.check_assets(row['source_assets'])
    # Independent metadata receipt: content anchors and body ownership unchanged.
    value.setdefault('property_receipts', {})[ident] = {'contract': props.CONTRACT,
        'page_id': row['page_id'], 'data_source_id': source, 'source_commit': plan['publication']['commit'],
        'md_blob': row['source']['blob'], 'manifest_sha256': plan['property_manifest_sha256'],
        'managed': deepcopy(row['property_managed']), 'held': deepcopy(row['held_fields']), 'readback_verified': True}
    counts['updated' if row['property_patch'] else 'noop'] += 1
    if row['held_fields']: counts['retained_conflicts'] += 1
    journal.update(index=journal['index'] + 1, operation=None)
    return store.compare_and_set(version, value)


def creation_fields(publication, ident, data, metadata):
    projection, times, selected, manifest = metadata
    if ident not in selected: return {}, {}, None, {}
    desired, held = props.project(data, ident, projection, times.get(ident))
    assets = {} if manifest['registry'] is None else {props.REGISTRY: manifest['registry']['blob']}
    return desired, held, manifest['schema_sha256'], assets


def guard_creation_schema(row, api):
    e = engine()
    expected = row.get('property_schema_sha256')
    if expected is not None and e.digest(props.schema_projection(api.schema())) != expected:
        e.fail('property_schema_changed')


def record_creation(value, row, plan):
    e = engine()
    if 'property_created' not in row: return
    body_receipt = value['receipts'][row['note_id']]
    native = creation_wire_properties(row)
    if e.run57_recovery.allows(plan, value.get('journal')):
        legacy = e.run57_recovery.creation_properties(row, body_receipt['page_id'])
        if legacy is not None: native = legacy
    value.setdefault('property_receipts', {})[row['note_id']] = {'contract': props.CONTRACT,
        'page_id': body_receipt['page_id'], 'data_source_id': body_receipt['data_source_id'],
        'source_commit': plan['publication']['commit'], 'md_blob': row['source']['blob'],
        'manifest_sha256': plan.get('property_manifest_sha256') or plan['property_create_scope_sha256'],
        'managed': {name: props.value(native[name]) for name in row['property_created']},
        'held': deepcopy(row['held_fields']), 'readback_verified': True}
