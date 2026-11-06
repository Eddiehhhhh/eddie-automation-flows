"""One registered GitHub publication; no Get client and no new writer."""
import os
from pathlib import Path

import get_private_runtime_bindings as bindings
import get_notion_executor as e
import get_notion_publication_replay as replay

CONTRACT = 'get-notion-exact-archive-replay-1'
SELECTION = '97b7d03eff1b05750bd8398af5ea6e3338147b7b1fc430102d33965a8391a88c'


def binding():
    original = bindings.value('get_notion_exact_replay', 'ORIGINAL')
    if e.digest(original) != SELECTION:
        e.fail('approval_missing')
    return original


def admission():
    from get_notion_controller_route import selection
    if (selection() != ('historical-apply', SELECTION, CONTRACT)
            or os.environ.get('GET_SINGLE_NOTE_MODE', '').lower() not in ('', 'false')
            or os.environ.get('GET_SINGLE_NOTE_ID')):
        e.fail('approval_missing')
    return binding()


def origin(client, execution, b):
    if execution['run_id'] == b['selection']['run_id']:
        e.fail('approval_missing')
    observed = replay.verify(client, b['source_policy'], b['source_policy_sha256'], b['selection'])
    record = observed['record']
    if (observed['source_job_id'] != b['job_id'] or observed['notion_outcome'] != 'failure'
            or not record['main_updated'] or record['cas_attempts'] != 1
            or record['observed_main_commit'] != b['selection']['receipt_commit']
            or record['receipt_sequence'] != b['sequence']):
        e.fail('approval_missing')
    return {'commit': record['receipt_commit'], 'receipt_sha256': record['receipt_sha256'],
            'sequence': record['receipt_sequence']}


def receipt_matches(commit, digest, value, b):
    return (commit == b['selection']['receipt_commit'] and digest == b['selection']['receipt_sha256']
        and value.get('parent_commit') == b['parent']
        and value.get('previous_receipt_commit') == b['previous_commit']
        and value.get('previous_receipt_sha256') == b['previous_sha256']
        and value.get('changed_content_ids') == [b['note_id']]
        and value.get('sequence') == b['sequence'] and value.get('source_scope') == 'single_note'
        and value.get('policy_sha256') == b['source_policy']['get_policy_sha256'])


def read_source(local, remote, token, root, tip, b):
    import get_confirmed_source as source
    import get_recurring_entry as entry
    loader = source.Loader(local, remote, token, root, b['source_policy']['get_policy_sha256'])
    receipt, row, paths = loader.publication(tip)
    if not receipt_matches(tip['commit'], tip['receipt_sha256'], receipt, b) or len(paths) != 1:
        e.fail('approval_missing')
    before = loader.reader.tree(row['parent'])
    if paths[0] in before:
        e.fail('approval_missing')
    md = loader.blob(tip['commit'], paths[0], e.p.MAX_NOTE)
    e.p.source_body(md, b['note_id'])
    if entry.n.json_header(md).get('note_id') != b['note_id']:
        e.fail('source_invalid')
    return loader.reader.tree(tip['commit'])[paths[0]][2]


def unchanged_journal(local, anchor, source_commit, path, *, hydrate_fixed_history=None):
    """Check every mainline tree, and prove any older merged fork never contained the journal."""
    start = anchor['commit']
    span = start + '..' + source_commit
    shallow = Path(local.vault) / '.git/shallow'
    def bounded_span():
        raw = local.run('rev-list', '--parents', '--max-count=513', span).decode().splitlines()
        parents = {parts[0]: parts[1:] for parts in (row.split() for row in raw)}
        if not parents or len(parents) != len(raw):
            e.fail('approval_missing')
        boundaries = e.git.bounded_file(shallow, 2 * 1024 * 1024).decode().splitlines() if shallow.exists() else ()
        return parents, len(parents) > 512 or not parents.keys().isdisjoint(boundaries)
    parents, incomplete = bounded_span()
    if incomplete and shallow.exists() and hydrate_fixed_history is not None:
        # Separate shallow slices can hide a merge base and inflate old..new.
        # Fetch the registered descendant's trees/parents once, then enforce
        # the same 512 limit and the same per-commit journal checks.
        hydrate_fixed_history(source_commit)
        parents, incomplete = bounded_span()
    if incomplete:
        e.fail('approval_missing')
    local.run('merge-base', '--is-ancestor', start, source_commit)
    expected = ('100644 blob ' + anchor['state_blob'] + '\t' + path + '\0').encode()
    main = set()
    forks = []
    cursor = source_commit
    while cursor != start:
        inbound = parents.get(cursor)
        if (not inbound or len(inbound) > 2 or cursor in main
                or local.run('ls-tree', '-z', cursor, '--', path) != expected):
            e.fail('approval_missing')
        main.add(cursor)
        if len(inbound) == 2:
            forks.append(inbound[1])
        cursor = inbound[0]
    side = set()
    for cursor in forks:
        while cursor in parents:
            inbound = parents[cursor]
            if (cursor in main or cursor in side or len(inbound) != 1
                    or local.run('ls-tree', '-z', cursor, '--', path)):
                e.fail('approval_missing')
            side.add(cursor)
            cursor = inbound[0]
        local.run('merge-base', '--is-ancestor', cursor, start)
        if local.run('ls-tree', '-z', cursor, '--', path):
            e.fail('approval_missing')
    if main | side != set(parents):
        e.fail('approval_missing')


def bridge_state(local, loader, old, source_commit):
    """Prove the fixed pre-publication interval, then resume the regular 256 limit."""
    import get_notion_outbox as outbox
    import get_notion_idle_recovery as idle
    b = binding()
    if (old != idle.reviewed_baseline()['anchor']
            or source_commit != b['selection']['receipt_commit']):
        e.fail('approval_missing')
    for commit in (old['commit'], source_commit):
        loader(commit)
    from get_confirmed_source import Loader
    source = Loader(local, None, None, None, b['source_policy']['get_policy_sha256'])
    unchanged_journal(local, old, source_commit, outbox.STATE_PATH, hydrate_fixed_history=source.fetch)
    reader = outbox.git.OfflineGit(local.vault)
    for commit in (old['commit'], source_commit):
        row = reader.tree(commit).get(outbox.STATE_PATH)
        if row != ('100644', 'blob', old['state_blob']):
            e.fail('approval_missing')
        raw = reader.read(commit, outbox.STATE_PATH, outbox.MAX_STATE)
        if outbox.digest(raw) != old['state_sha256']:
            e.fail('approval_missing')
    return {**old, 'commit': source_commit}


def target_guard(state, request, b, source_blob, *, final=False):
    import get_notion_live_transport as live
    import get_notion_outbox as outbox
    retained = bindings.value('get_notion_plan_recovery', 'RETAINED_HOLDS')
    nid = b['note_id']
    engine = state['engine_state']
    if engine.get('unresolved_by_id') != retained or nid in retained:
        e.fail('journal_busy')
    receipts = engine['receipts']
    if not receipts:
        e.fail('approval_missing')
    if engine['journal'] is not None:
        outbox.single_note_selection(state, nid, resume=True)
        desc = engine['journal']['plan']['publication']
        if (desc['commit'] != b['selection']['receipt_commit']
                or desc['outbox_receipt_sha256'] != b['selection']['receipt_sha256']):
            e.fail('journal_busy')
    data_source = next(iter(receipts.values()))['data_source_id']
    snap = live.API(request, data_source, [nid]).read_note(nid)
    if snap['complete'] is not True or snap['identity_query_complete'] is not True:
        e.fail('target_invalid')
    matches = snap['exact_id_matches']
    if matches:
        existing = receipts.get(nid, {})
        if len(matches) != 1: e.fail('duplicate_id')
        if (matches[0] != existing.get('page_id') or existing.get('md_blob') != source_blob
                or existing.get('source_commit') != b['selection']['receipt_commit']):
            e.fail('target_invalid')
        view = e.target(snap, nid, data_source)
        managed = existing['managed_blocks']
        owned = {row['id'] for row in managed}
        observed = [{'id': ident, 'normalized_hash': e.p.block_hash(view['nodes'][ident])}
                    for ident in view['order'] if ident in owned]
        properties = engine.get('property_receipts', {}).get(nid)
        baseline = existing['properties_baseline']
        titles = [name for name, prop in baseline.items() if prop.get('type') == 'title']
        if (not managed or managed != observed or not properties or properties['readback_verified'] is not True
                or any(properties[k] != existing[k] for k in ('data_source_id','page_id','source_commit','md_blob'))
                or properties.get('held') != {} or set(properties['managed']) != set(e.properties.FIELDS)
                or len(titles) != 1 or view['properties'].get(titles[0]) != baseline[titles[0]]
                or any(name not in view['properties']
                    or e.properties.value(view['properties'][name]) != value for name,value in properties['managed'].items())):
            e.fail('readback_mismatch')
    elif final or nid in receipts:
        e.fail('target_invalid')
