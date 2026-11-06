"""Resume one owned Get page update without repeating uncertain Notion writes."""
import hashlib

import get_notion_executor as executor
import get_notion_incremental_worker as worker
import get_notion_live_transport as transport
import get_notion_properties as properties
import get_notion_sink_plan as sink

CONTRACT = 'get-notion-incremental-update-1'
ACTIVE = frozenset(('append_in_flight', 'append_verified', 'archive_in_flight',
                    'archive_verified', 'properties_in_flight'))


def stop(reason):
    raise ValueError(reason) from None


def digest(value):
    return hashlib.sha256(sink.encoded(value)).hexdigest()


def managed(snapshot, desired):
    raw = snapshot['properties_raw']
    if not set(desired) <= set(raw):
        stop('manual_edit_or_unknown_write')
    try:
        return {name: properties.value(raw[name]) for name in desired}
    except ValueError:
        stop('manual_edit_or_unknown_write')


def desired_values(desired):
    return {name: properties.value(value) for name, value in desired.items()}


def same_unmanaged(before, after, changed):
    old, new = before['properties_raw'], after['properties_raw']
    return set(old) == set(new) and all(old[name] == new[name] for name in old if name not in changed)


def same_page(before, after, ident, source_id):
    left = executor.target(before, ident, source_id)
    right = executor.target(after, ident, source_id)
    left.pop('last_edited_time')
    right.pop('last_edited_time')
    return left == right


def checked_journal(value, ident, page, source_sha, owned, props, source_id):
    if not isinstance(value, dict) or value.get('contract') != CONTRACT:
        stop('manual_edit_or_unknown_write')
    if (value.get('note_id'), value.get('page_id'), value.get('source_sha256'),
            value.get('prior_target_sha256')) != (ident, page, source_sha, owned['target_sha256']):
        stop('manual_edit_or_unknown_write')
    if value.get('phase') not in ACTIVE:
        stop('manual_edit_or_unknown_write')
    if not isinstance(value.get('before'), dict) or not isinstance(value.get('working'), dict):
        stop('manual_edit_or_unknown_write')
    if (executor.target(value['before'], ident, source_id)['page_id'] != page
            or worker.target_sha(value['before'], props) != owned['target_sha256']):
        stop('manual_edit_or_unknown_write')
    roots = value.get('old_roots')
    if not isinstance(roots, list) or not roots or len(roots) > 20 or len(roots) != len(set(roots)):
        stop('manual_edit_or_unknown_write')
    if roots != sink.snapshot_tree(value['before'])[1]:
        stop('manual_edit_or_unknown_write')
    if not isinstance(value.get('archive_index'), int) or not 0 <= value['archive_index'] <= len(roots):
        stop('manual_edit_or_unknown_write')
    if len(sink.encoded(value)) > 1024 * 1024:
        stop('source_content_held')
    return value


def update(root, ident, page, data, props, blocks, source_id, token, api, owned, journal,
           persist=None):
    source_sha = hashlib.sha256(data).hexdigest()
    current = api.read_note(ident)
    if current.get('page_id') != page or api.matches(ident) != [page]:
        stop('manual_edit_or_unknown_write')
    wanted = desired_values(props)
    wanted_body = sink.comparison_semantics(blocks)
    def checkpoint(value):
        if len(sink.encoded(value)) > 1024 * 1024:
            stop('source_content_held')
        persist(value)
    reserved_now = False
    if journal is None or journal.get('phase') == 'complete':
        if worker.target_sha(current, props) != owned['target_sha256']:
            stop('manual_edit_or_unknown_write')
        old_roots = sink.snapshot_tree(current)[1]
        if not old_roots or len(old_roots) > 20:
            stop('source_content_held')
        journal = {'contract': CONTRACT, 'note_id': ident, 'page_id': page,
                   'source_sha256': source_sha, 'prior_target_sha256': owned['target_sha256'],
                   'phase': 'append_in_flight' if sink.snapshot_tree(current)[3] != wanted_body else 'properties_in_flight',
                   'before': current, 'working': current, 'old_roots': old_roots,
                   'archive_index': 0}
        if persist is None:
            if len(sink.encoded(journal)) > 1024 * 1024:
                stop('source_content_held')
            return {'status': 'update_ready'}
        checkpoint(journal)
        reserved_now = True
    else:
        checked_journal(journal, ident, page, source_sha, owned, props, source_id)
        if persist is None:
            if journal['phase'] == 'append_in_flight':
                row = {'note_id': ident, 'rendered': blocks,
                       'after_anchor': journal['old_roots'][-1]}
                try:
                    executor.appended(journal['before'], current, row, source_id)
                except executor.Blocked:
                    stop('write_uncertain' if same_page(current, journal['before'], ident, source_id)
                         else 'manual_edit_or_unknown_write')
            elif journal['phase'] == 'archive_in_flight':
                old = journal['old_roots'][journal['archive_index']]
                try:
                    executor.archived(journal['working'], current, ident, source_id, old)
                except executor.Blocked:
                    stop('write_uncertain' if same_page(current, journal['working'], ident, source_id)
                         else 'manual_edit_or_unknown_write')
            elif journal['phase'] == 'properties_in_flight':
                if managed(current, props) != wanted:
                    stop('write_uncertain')
            return {'status': 'update_ready'}
    if len(sink.encoded(journal)) > 1024 * 1024:
        stop('source_content_held')

    row = {'note_id': ident, 'rendered': blocks, 'after_anchor': journal['old_roots'][-1]}
    if reserved_now and journal['phase'] == 'append_in_flight':
        request = api.preflight_append(page, blocks, row['after_anchor'])
        writer = write_api(root, ident, source_id, token, page, request['method'],
                           request['path'], request['body'], journal['before'], source_sha)
        writer.append_blocks(page, blocks, row['after_anchor'])
        reserved_now = False
    while True:
        phase = journal['phase']
        current = api.read_note(ident)
        if current.get('page_id') != page or api.matches(ident) != [page]:
            stop('manual_edit_or_unknown_write')
        if phase == 'append_in_flight':
            try:
                roots, _ = executor.appended(journal['before'], current, row, source_id)
            except executor.Blocked:
                if same_page(current, journal['before'], ident, source_id):
                    stop('write_uncertain')
                stop('manual_edit_or_unknown_write')
            journal.update(phase='append_verified', working=current, new_roots=roots)
            checkpoint(journal)
            continue
        if phase in ('append_verified', 'archive_verified'):
            if not same_page(current, journal['working'], ident, source_id):
                stop('manual_edit_or_unknown_write')
            index = journal['archive_index']
            if index == len(journal['old_roots']):
                journal['phase'] = 'properties_in_flight'
                checkpoint(journal)
                reserved_now = True
                continue
            old = journal['old_roots'][index]
            journal['phase'] = 'archive_in_flight'
            checkpoint(journal)
            writer = write_api(root, ident, source_id, token, page, 'PATCH', '/blocks/' + old,
                               {'archived': True}, journal['working'], source_sha)
            writer.archive_block(old)
            continue
        if phase == 'archive_in_flight':
            old = journal['old_roots'][journal['archive_index']]
            try:
                executor.archived(journal['working'], current, ident, source_id, old)
            except executor.Blocked:
                if same_page(current, journal['working'], ident, source_id):
                    stop('write_uncertain')
                stop('manual_edit_or_unknown_write')
            journal.update(phase='archive_verified', working=current,
                           archive_index=journal['archive_index'] + 1)
            checkpoint(journal)
            continue
        if phase == 'properties_in_flight':
            if sink.snapshot_tree(current)[3] != wanted_body:
                stop('manual_edit_or_unknown_write')
            before = managed(journal['before'], props)
            actual = managed(current, props)
            changed = {name for name in props if before[name] != wanted[name]}
            if actual == wanted:
                if not same_unmanaged(journal['before'], current, changed):
                    stop('manual_edit_or_unknown_write')
                target = worker.verify_note(api, ident, page, props, blocks)
                return {'status': 'updated', 'page_id': page,
                        'owned': worker.owned_receipt(ident, page, data, target),
                        'journal_complete': {'contract': CONTRACT, 'note_id': ident,
                            'page_id': page, 'source_sha256': source_sha, 'phase': 'complete'}}
            if actual != before or not same_unmanaged(journal['before'], current, set()):
                stop('write_uncertain')
            if not changed or '笔记 ID' in changed:
                stop('manual_edit_or_unknown_write')
            if not reserved_now:
                stop('write_uncertain')
            patch = {name: props[name] for name in changed}
            writer = write_api(root, ident, source_id, token, page, 'PATCH', '/pages/' + page,
                               {'properties': patch}, current, source_sha)
            writer.update_incremental_properties(page, patch, set(props) - {'笔记 ID'})
            reserved_now = False
            continue
        stop('manual_edit_or_unknown_write')


def write_api(root, ident, source_id, token, page, method, path, body, expected, source_sha):
    # A fresh private-main/source check precedes each physical write.
    current = worker.git(root, 'rev-parse', 'HEAD').decode().strip()
    _, source, _ = worker.source(root, ident, current)
    if hashlib.sha256(source).hexdigest() != source_sha:
        stop('source_changed')
    key = transport.fingerprint(method, path, body)
    writer = transport.API(transport.Transport(token, approved_writes=(key,)), source_id, [ident],
                           approved_writes=(key,))
    observed = writer.read_note(ident)
    if not same_page(observed, expected, ident, source_id) or writer.matches(ident) != [page]:
        stop('manual_edit_or_unknown_write')
    return writer
