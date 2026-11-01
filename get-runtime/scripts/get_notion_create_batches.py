"""Resumable, prefix-verified creation of a NEW page only.

The immutable plan contains root ranges, never compacted paragraphs. Each
request owns complete root subtrees and is limited to 100 recursive blocks and
400 KiB including the wire envelope. A known page and verified prefix are not
permission to resend an ambiguous append. Only a complete expected readback
can establish that append's effect; ambiguous create still needs exact-page
recovery approval. No existing page is adopted, changed or deleted here.
"""
from copy import deepcopy

import get_notion_sink_plan as p
import get_notion_live_transport as transport

CONTRACT = 'get-notion-new-page-batches-1'
MAX_BATCHES = 32
MAX_TOTAL_BLOCKS = 3200
MAX_PLANNED_REQUESTS = 1500
MAX_CHECKPOINT_BYTES = 192 * 1024 * 1024
CHECKPOINT_RESERVE = 6
COMPLETION_CHECKPOINT_RESERVE = 2
# A checkpoint can require remote Git validation. Reserve six checkpoints,
# both complete reads and a margin before beginning another external write.
CHECKPOINT_SECONDS = 30
SAFETY_SECONDS = 30


def engine():
    import get_notion_executor
    return get_notion_executor


def node_count(rows):
    return sum(1 + node_count(row['children']) for row in rows)


def read_requests(rows):
    """Normal API.read_note calls, including schema/identity/page stability."""
    def descendants(items):
        return sum((len(row['children']) + 99) // 100 + descendants(row['children'])
                   for row in items if row['children'])
    return 7 + max(1, (len(rows) + 99) // 100) + descendants(rows)


def request(row, data_source, index, page_id=None, after=None):
    e = engine()
    spec = row['create_batches']['batches'][index]
    roots = row['rendered'][spec['start']:spec['end']]
    blocks = e.request_blocks(roots)
    previous = row['rendered'][spec['start'] - 1]['type'] if spec['start'] else None
    wire = transport.wire_blocks(blocks, previous, uploaded_pngs=row.get('uploaded_pngs'))
    if index == 0:
        body = {'parent': {'type': 'data_source_id', 'data_source_id': data_source},
                'properties': e.property_transaction.creation_wire_properties(row), 'children': wire}
        method, path = 'POST', '/pages'
    else:
        body = {'children': wire, 'after': p.notion_id(after)}
        method, path = 'PATCH', '/blocks/' + p.notion_id(page_id) + '/children'
    if len(e.encoded(body)) > e.MAX_REQUEST_BYTES:
        e.fail('request_budget')
    return {'method': method, 'path': path, 'body': body}, blocks


def prepare(rendered, properties, data_source, uploaded_pngs=None):
    """Return root boundaries; validate every request before any page exists."""
    e = engine()
    if not isinstance(rendered, list) or not rendered or node_count(rendered) > MAX_TOTAL_BLOCKS:
        e.fail('request_budget')
    row = {'rendered': rendered, 'properties': properties,
           'uploaded_pngs': uploaded_pngs, 'create_batches': {'batches': []}}
    ranges = row['create_batches']['batches']
    start = 0
    total = 0
    semantic_bytes = 2
    children_bytes = 2
    create_overhead = len(e.encoded({'parent': {'type': 'data_source_id', 'data_source_id': data_source},
                                    'properties': properties, 'children': []})) - 2
    append_overhead = len(e.encoded({'children': [], 'after': 'b' * 32})) - 2
    for end, root in enumerate(rendered):
        count = node_count([root])
        if count > e.MAX_WRITE_BLOCKS:
            e.fail('request_budget')
        normalized = e.request_blocks([root])
        previous = rendered[end - 1]['type'] if end else None
        native = transport.wire_blocks(normalized, previous, uploaded_pngs=uploaded_pngs)
        semantic_size = len(e.encoded(normalized[0]))
        native_size = len(e.encoded(native[0]))
        overhead = create_overhead if not ranges else append_overhead
        comma = int(end > start)
        if (total + count > e.MAX_WRITE_BLOCKS
                or semantic_bytes + semantic_size + comma > e.MAX_REQUEST_BYTES
                or overhead + children_bytes + native_size + comma > e.MAX_REQUEST_BYTES):
            if end == start:
                e.fail('request_budget')
            ranges.append({'start': start, 'end': end, 'total_blocks': total})
            start = end
            total = 0
            semantic_bytes = children_bytes = 2
            comma = 0
            overhead = append_overhead
        if (semantic_bytes + semantic_size + comma > e.MAX_REQUEST_BYTES
                or overhead + children_bytes + native_size + comma > e.MAX_REQUEST_BYTES):
            e.fail('request_budget')
        total += count
        semantic_bytes += semantic_size + comma
        children_bytes += native_size + comma
    ranges.append({'start': start, 'end': len(rendered), 'total_blocks': total})
    if len(ranges) > MAX_BATCHES:
        e.fail('request_budget')
    # Verify exact full envelopes too; sizing above is only linear-time packing.
    for index in range(len(ranges)):
        request(row, data_source, index, 'a' * 32, 'b' * 32)
    # Two complete prefix reads per batch, plus initial/final checks and writes.
    reads = 4 + read_requests(rendered) + sum(
        read_requests(rendered[:r['start']]) + read_requests(rendered[:r['end']])
        for r in ranges)
    planned = reads + len(ranges) + 2
    if planned > MAX_PLANNED_REQUESTS:
        e.fail('request_budget')
    return {'contract': CONTRACT, 'batches': ranges,
            'total_blocks': node_count(rendered), 'planned_requests': planned}


def validate_plan(row, data_source):
    e = engine()
    if row.get('action') != 'create' or row.get('before_sha256') != e.digest(None):
        e.fail('state_invalid')
    expected = prepare(row['rendered'], e.property_transaction.creation_wire_properties(row), data_source, row.get('uploaded_pngs'))
    if not e.same(expected, row.get('create_batches')) or len(expected['batches']) < 2:
        e.fail('state_invalid')
    if not e.same(row.get('request_blocks'), e.request_blocks(row['rendered'][:expected['batches'][0]['end']])):
        e.fail('state_invalid')


def progress():
    return {'phase': 'prepared', 'response': None, 'roots': [], 'blocks': [], 'prefix_sha256': None}


def validate_operation(operation, row):
    """Strict new schema; legacy single-request journals keep their old codec."""
    e = engine()
    required = {'kind', 'phase', 'batch_index', 'page_id', 'properties_sha256', 'batches'}
    if (not isinstance(operation, dict) or set(operation) != required
            or operation['kind'] != 'create_batches'
            or type(operation['batch_index']) is not int):
        e.fail('state_invalid')
    index = operation['batch_index']
    batches = operation['batches']
    specs = row['create_batches']['batches']
    if (not 0 <= index < len(specs) or not isinstance(batches, list)
            or len(batches) != index + 1):
        e.fail('state_invalid')
    if operation['page_id'] is not None:
        p.notion_id(operation['page_id'])
    if operation['properties_sha256'] is not None:
        p.hex_hash(operation['properties_sha256'], 64)
    seen = set()
    for i, batch in enumerate(batches):
        keys = {'phase', 'response', 'roots', 'blocks', 'prefix_sha256'}
        if (not isinstance(batch, dict) or not keys <= set(batch)
                or set(batch) - keys - {'write_failure', 'recovery_sha256'}
                or batch['phase'] not in ('prepared', 'in_flight', 'acknowledged', 'verified')
                or i < index and batch['phase'] != 'verified'):
            e.fail('state_invalid')
        if 'write_failure' in batch:
            failure = batch['write_failure']
            if (batch['phase'] != 'in_flight' or not isinstance(failure, dict)
                    or set(failure) != {'http_status', 'request_fingerprint'}
                    or type(failure['http_status']) is not int or not 400 <= failure['http_status'] <= 599):
                e.fail('state_invalid')
            p.hex_hash(failure['request_fingerprint'], 64)
        if 'recovery_sha256' in batch:
            if i != 0 or batch['phase'] not in ('acknowledged', 'verified'):
                e.fail('state_invalid')
            p.hex_hash(batch['recovery_sha256'], 64)
        response = batch['response']
        if response is not None:
            if batch['phase'] not in ('acknowledged', 'verified'):
                e.fail('state_invalid')
            if i == 0:
                if p.notion_id(response) != operation['page_id']:
                    e.fail('state_invalid')
            elif (not isinstance(response, list) or len(response) != specs[i]['end'] - specs[i]['start']
                  or len(set(response)) != len(response)):
                e.fail('state_invalid')
            else:
                for bid in response:
                    p.notion_id(bid)
        if batch['phase'] == 'acknowledged' and response is None and 'recovery_sha256' not in batch:
            e.fail('state_invalid')
        if not isinstance(batch['roots'], list) or not isinstance(batch['blocks'], list):
            e.fail('state_invalid')
        if batch['phase'] == 'verified':
            if (operation['page_id'] is None or operation['properties_sha256'] is None
                    or len(batch['roots']) != specs[i]['end'] - specs[i]['start']
                    or len(batch['blocks']) != specs[i]['total_blocks']):
                e.fail('state_invalid')
            p.hex_hash(batch['prefix_sha256'], 64)
            local = set()
            for block in batch['blocks']:
                if not isinstance(block, dict) or set(block) != {'id', 'normalized_hash'}:
                    e.fail('state_invalid')
                bid = p.notion_id(block['id'])
                p.hex_hash(block['normalized_hash'], 64)
                if bid in seen:
                    e.fail('state_invalid')
                seen.add(bid); local.add(bid)
            if len(set(batch['roots'])) != len(batch['roots']) or set(batch['roots']) - local:
                e.fail('state_invalid')
            if i and response is not None and response != batch['roots']:
                e.fail('state_invalid')
        elif batch['roots'] or batch['blocks'] or batch['prefix_sha256'] is not None:
            e.fail('state_invalid')
    if (operation['phase'] != batches[index]['phase']
            or index and (operation['page_id'] is None or operation['properties_sha256'] is None)
            or index == 0 and operation['phase'] in ('prepared', 'in_flight') and operation['page_id'] is not None):
        e.fail('state_invalid')


def checkpoint_size(value, row, store=None):
    """Bound one full durable state, including retained receipts and envelope.

    Allow the complete page's ID/hash history and final ownership receipt to
    grow, but do not charge checkpoints from future invocations to this run.
    EngineStore's already-read outer envelope is the actual persisted state;
    replacing its engine member must not count that member twice.
    """
    e = engine()
    # ID + normalized hash + JSON keys, roots/response copies and phase history.
    growth = row['create_batches']['total_blocks'] * 256 + len(row['create_batches']['batches']) * 1024 + 4096
    envelope = 0
    current = store
    for _ in range(5):
        outer = getattr(current, 'outer', None)
        if isinstance(outer, dict) and isinstance(outer.get('engine_state'), dict):
            envelope = max(envelope, len(e.encoded(outer)) - len(e.encoded(outer['engine_state'])))
        current = getattr(current, 'store', None)
        if current is None:
            break
    operation = (value.get('journal') or {}).get('operation')
    existing = len(e.encoded(operation)) if isinstance(operation, dict) and operation.get('kind') == 'create_batches' else 0
    return len(e.encoded(value)) + envelope + max(0, growth - existing)


def checkpoint_bound(value, row, store=None):
    """One batch's durable reserve, never the sum across resumed invocations.

    Six saves cover prepared/in-flight/acknowledged/verified plus either a
    safe-boundary rollback or the final receipt and journal clear. Hard store
    counters still charge every actual distinct blob and CAS attempt.
    """
    return CHECKPOINT_RESERVE * checkpoint_size(value, row, store)


def resources(api, store, value, row, index, *, completion=False):
    """Reserve a batch, or only final read/receipt/clear; retain all hard caps."""
    e = engine()
    size = checkpoint_size(value, row, store)
    if size > e.MAX_STATE:
        e.fail('request_budget')
    spec = row['create_batches']['batches'][index]
    if completion:
        operation = (value.get('journal') or {}).get('operation')
        validate_operation(operation, row)
        if (index != len(row['create_batches']['batches']) - 1
                or operation['batch_index'] != index or operation['phase'] != 'verified'):
            e.fail('state_invalid')
    checkpoints = COMPLETION_CHECKPOINT_RESERVE if completion else CHECKPOINT_RESERVE
    # A completed prefix needs no pre-write read, create guard or write request.
    reads = read_requests(row['rendered'][:spec['end']])
    if not completion:
        reads += read_requests(row['rendered'][:spec['start']])
    def images(items):
        return sum((r['type'] == 'image') + images(r['children']) for r in items)
    before_images = 0 if completion else images(row['rendered'][:spec['start']])
    after_images = images(row['rendered'][:spec['end']])
    bindings = before_images + after_images
    getter = getattr(getattr(api, 'snapshot_adapter', None), 'asset_get', None)
    if getter is not None and all(hasattr(getter, k) for k in ('downloads', 'response_bytes', 'elapsed')):
        # A rotating signed URL permits at most three downloads per binding.
        # Reserve the worst case rather than exhausting the adapter mid-write.
        downloads = 3 * bindings
        image_bytes = max((v['byte_count'] for v in row.get('uploaded_pngs', {}).values()), default=512000)
        if (getter.downloads + downloads > 12
                or getter.response_bytes + downloads * image_bytes > 8 * 1024 * 1024
                or getter.elapsed + downloads * 15 >= 120):
            return False
    reads += 4 * bindings
    request_api = getattr(api, 'request', None)
    if isinstance(request_api, transport.Transport):
        requests = reads + (0 if completion else 3)
        required = SAFETY_SECONDS + checkpoints * CHECKPOINT_SECONDS + .4 * requests
        # Native response envelopes are larger than semantic blocks. This is
        # admission headroom, not a replacement for the transport's hard cap.
        prefix = row['rendered'][:spec['end']]
        read_bytes = (1 if completion else 3) * (len(e.encoded(prefix)) + node_count(prefix) * 1024) + 512 * 1024
        if (request_api.attempts + requests > 2000
                or request_api.clock() + required >= request_api.deadline
                or request_api.bytes + read_bytes > transport.ro.MAX_HTTP_BYTES):
            return False
    # Wrapper depth is fixed and bounded. Only actual known remote journal
    # counters are consulted; absence of a provider cannot raise any ceiling.
    current = store
    for _ in range(5):
        if all(hasattr(current, key) for key in ('commits_attempted', 'max_commits', 'new_blob_bytes', 'max_batch_bytes')):
            if (current.commits_attempted + checkpoints > current.max_commits
                    or current.new_blob_bytes + checkpoints * size > current.max_batch_bytes):
                return False
            break
        current = getattr(current, 'store', None)
        if current is None:
            break
    return True


def verify_prefix(snapshot, row, data_source, operation, index):
    e = engine()
    spec = row['create_batches']['batches'][index]
    prefix_row = {**row, 'rendered': row['rendered'][:spec['end']]}
    e.created(snapshot, prefix_row, data_source, operation['page_id'])
    view = e.target(snapshot, row['note_id'], data_source)
    if operation['properties_sha256'] is not None and e.digest(view['properties']) != operation['properties_sha256']:
        e.fail('human_edit_conflict')
    previous_roots = []
    for batch in operation['batches'][:index]:
        previous_roots.extend(batch['roots'])
        for block in batch['blocks']:
            if block['id'] not in view['nodes'] or p.block_hash(view['nodes'][block['id']]) != block['normalized_hash']:
                e.fail('human_edit_conflict')
    if view['roots'][:spec['start']] != previous_roots:
        e.fail('human_edit_conflict')
    roots = view['roots'][spec['start']:spec['end']]
    response = operation['batches'][index]['response']
    if index and response is not None and roots != response:
        e.fail('readback_mismatch')
    return view, roots


def execute_row(row, plan, approved_plan_sha, publication, api, store, value, version,
                counts, writers_guard, *, authorize_batch=None, create_recovery=None,
                approved_recovery_sha=None):
    """Return (version, full_snapshot_or_None); None is a durable safe yield."""
    e = engine()
    data_source = plan['policy']['data_source_id']
    ident = row['note_id']
    validate_plan(row, data_source)
    journal = value['journal']
    operation = journal['operation']
    if operation is None:
        if (checkpoint_size(value, row, store) > e.MAX_STATE
                or checkpoint_bound(value, row, store) > MAX_CHECKPOINT_BYTES):
            e.fail('request_budget')
        if e.target_sha(e.read_target(api, ident, data_source), ident, data_source) != row['before_sha256']:
            e.fail('target_changed')
        operation = {'kind': 'create_batches', 'phase': 'prepared', 'batch_index': 0,
                     'page_id': None, 'properties_sha256': None, 'batches': [progress()]}
        journal['operation'] = operation
        version = store.compare_and_set(version, value)
    validate_operation(operation, row)

    def save():
        nonlocal version
        version = store.compare_and_set(version, value)

    def phase(name):
        operation['phase'] = name
        batch['phase'] = name
        save()

    def guard():
        publication.check(plan['publication'])
        publication.check_assets(row.get('source_assets', {}))
        fresh, fresh_sha = store.read()
        e.check_state(fresh)
        if fresh_sha != version or e.digest(fresh) != fresh_sha:
            e.fail('state_changed')
        if e.plan_recovery.held(fresh, ident, operation['page_id']):
            e.fail('unresolved_operation')
        scope = e.content_scope.reason(ident, publication.read(ident))
        if scope:
            e.fail(scope)
        try:
            evidence = writers_guard()
        except Exception:
            e.fail('writer_gate_unknown')
        if not p.writer_gate({'old_writer_evidence': evidence}):
            e.fail('writer_gate_unknown')

    while True:
        index = operation['batch_index']
        batch = operation['batches'][index]
        spec = row['create_batches']['batches'][index]
        if batch['phase'] == 'verified':
            if index == len(row['create_batches']['batches']) - 1:
                # Completion itself needs one final fresh whole-page read. It
                # may resume in a fresh bounded invocation without any write.
                if not resources(api, store, value, row, index, completion=True):
                    return version, None
                snapshot = e.read_target(api, ident, data_source)
                verify_prefix(snapshot, row, data_source, operation, index)
                if e.target_sha(snapshot, ident, data_source) != batch['prefix_sha256']:
                    e.fail('human_edit_conflict')
                return version, snapshot
            if not resources(api, store, value, row, index + 1):
                return version, None
            operation['batch_index'] += 1
            operation['batches'].append(progress())
            operation['phase'] = 'prepared'
            save()
            continue
        if batch['phase'] == 'prepared':
            if not resources(api, store, value, row, index):
                if index:
                    # This next range is durably known to be unsent. Keep
                    # planned yields on the last fully verified boundary.
                    operation['batches'].pop()
                    operation['batch_index'] -= 1
                    operation['phase'] = 'verified'
                    save()
                return version, None
            snapshot = e.read_target(api, ident, data_source)
            if index == 0:
                if e.target_sha(snapshot, ident, data_source) != row['before_sha256']:
                    e.fail('target_changed')
            else:
                verify_prefix(snapshot, row, data_source, operation, index - 1)
                if e.target_sha(snapshot, ident, data_source) != operation['batches'][index - 1]['prefix_sha256']:
                    e.fail('human_edit_conflict')
            guard()
            after = operation['batches'][index - 1]['roots'][-1] if index else None
            wire, blocks = request(row, data_source, index, operation['page_id'], after)
            key = transport.fingerprint(wire['method'], wire['path'], wire['body'])
            if index and hasattr(api, 'preflight_append'):
                try:
                    proposed = api.preflight_append(operation['page_id'], blocks, after)
                except Exception as exc:
                    e.fail(str(exc) if str(exc) in ('unsupported_prepend', 'asset_write_contract_missing', 'request_budget') else 'target_invalid')
                if not e.same(proposed, wire):
                    e.fail('target_changed')
            phase('in_flight')
            try:
                guard()
                if index == 0: e.property_transaction.guard_creation_schema(row, api)
                if authorize_batch is not None:
                    authorize_batch(row, operation, wire)
            except Exception:
                phase('prepared')
                raise
            try:
                counts['write_calls'] += 1
                response = (api.create_page(data_source, ident, e.property_transaction.creation_wire_properties(row), blocks) if index == 0
                            else api.append_blocks(operation['page_id'], blocks, after))
            except transport.NotDispatched as exc:
                if exc.request_fingerprint != key:
                    e.fail('uncertain_create' if index == 0 else 'uncertain_append')
                counts['write_calls'] -= 1
                phase('prepared')
                e.fail('request_not_dispatched')
            except Exception as exc:
                if (isinstance(exc, transport.WriteFailure) and exc.request_fingerprint == key
                        and type(exc.status) is int and 400 <= exc.status <= 599):
                    batch['write_failure'] = {'http_status': exc.status, 'request_fingerprint': key}
                    save()
                e.fail('uncertain_create' if index == 0 else 'uncertain_append')
            if index == 0:
                operation['page_id'] = p.notion_id(response)
            elif (not isinstance(response, list) or len(response) != spec['end'] - spec['start']
                  or len(set(response)) != len(response)):
                e.fail('readback_mismatch')
            else:
                for bid in response:
                    p.notion_id(bid)
            batch['response'] = deepcopy(response)
            phase('acknowledged')
        snapshot = e.read_target(api, ident, data_source)
        if index == 0 and operation['page_id'] is None:
            recovery = create_recovery
            if (not isinstance(recovery, dict) or e.digest(recovery) != approved_recovery_sha
                    or set(recovery) != {'plan_sha256', 'note_id', 'page_id', 'snapshot_sha256'}
                    or recovery['plan_sha256'] != approved_plan_sha or recovery['note_id'] != ident
                    or recovery['snapshot_sha256'] != e.target_sha(snapshot, ident, data_source)):
                e.fail('uncertain_create')
            operation['page_id'] = p.notion_id(recovery['page_id'])
            batch['recovery_sha256'] = approved_recovery_sha
            batch.pop('write_failure', None)
            phase('acknowledged')
        try:
            view, roots = verify_prefix(snapshot, row, data_source, operation, index)
        except e.Blocked:
            if index and batch['phase'] == 'in_flight' and e.target_sha(snapshot, ident, data_source) == operation['batches'][index - 1]['prefix_sha256']:
                e.fail('uncertain_append')
            raise
        publication.check(plan['publication'])
        operation['properties_sha256'] = e.digest(view['properties'])
        owned = {bid for root in roots for bid in e.subtree(view['nodes'], root)}
        batch.update(roots=roots, blocks=[{'id': bid, 'normalized_hash': p.block_hash(view['nodes'][bid])}
                                         for bid in view['order'] if bid in owned],
                     prefix_sha256=e.digest(view))
        batch.pop('write_failure', None)
        phase('verified')
