"""Offline manual/hourly entry around the canonical Get engine.

No network, credentials, publication, ref writes, or enabled schedule. Bootstrap
is externally fixed below. Every later call REQUIRES a last-publication commit
and receipt SHA-256 obtained from an independently verified publication result;
computing that SHA from the supplied repository does not establish trust.
Receipts are integrity/continuity records, not cryptographic authorship claims.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
from dataclasses import dataclass
import argparse
import json
import os
from pathlib import Path
import tempfile

import get_canonical_manual as engine
import get_recurring_policy_recovery as recovery
from get_canonical_manual import a, m, n, r

CONTRACT = 'get-recurring-local-entry-1'
RECEIPT_CONTRACT = 'get-canonical-sync-receipt-1'
RECEIPT = engine.RECURRING_RECEIPT
FULL_RECEIPT = r.GET + '/' + RECEIPT
MAX_RECEIPT = 512 * 1024
# Non-Get commits accumulate between publications. Every first-parent step
# inside this finite window still goes through the original subtree checks.
# shortcut: over 8192 commits without a Get publication require a renewed verified anchor.
MAX_INTERVENING = 8192
PILOT_MIGRATION = _private_bindings.value('get_recurring_entry', 'PILOT_MIGRATION')
BOOTSTRAP = _private_bindings.value('get_recurring_entry', 'BOOTSTRAP')
STATE_PATHS = {r.GET + '/' + a.STATE, r.GET + '/' + n.STATE}
RECEIPT_KEYS = {
    'contract', 'policy_sha256', 'sequence', 'parent_commit',
    'previous_receipt_commit', 'previous_receipt_sha256', 'before_get_tree',
    'previous_legacy_sha256', 'previous_registry_sha256',
    'state_sha256', 'registry_sha256', 'delta_sha256',
    'engine_payload_sha256', 'source_summary_sha256', 'detail_receipts_sha256',
    'changed_content_ids', 'counts', 'conflict_reason_counts',
}
LEGACY_RECEIPT_KEYS = RECEIPT_KEYS
RECEIPT_KEYS = RECEIPT_KEYS | {'source_scope'}


class EntryFailure(a.ParityFailure):
    pass


HISTORY_CHECKS = frozenset((
    'merge_first_parent_tree_mismatch', 'pilot_start_disallowed',
    'pilot_migration_unconfigured', 'pilot_migration_malformed',
    'pilot_tree_pair_unmatched', 'pilot_tree_diverged',
    'pilot_parent_mismatch', 'pilot_migration_unclosed',
))


def fail(code):
    raise EntryFailure(code)


def fail_history(check):
    if check not in HISTORY_CHECKS:
        fail('entry_history_witness')
    error = EntryFailure('entry_foreign_get_change')
    error.history_check = check
    raise error


def rules_sha256():
    """Bind the shared engine, this entry, and the disabled template bytes."""
    template = Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-hourly.yml.disabled'
    return a.sha(a.encoded({'engine_rules_sha256': m.rules_sha256(),
                           'disabled_template_sha256': a.sha(a.bounded_file(template, m.c.MIB)),
                           'controller_template_sha256': a.sha(a.bounded_file(template.with_name('get-canonical-controller.yml.disabled'), m.c.MIB))}))


def policy_sha256(approved_rules_sha256, bootstrap):
    a.checked(approved_rules_sha256, 64)
    return a.sha(a.encoded({'contract': CONTRACT, 'bootstrap': bootstrap,
                           'rules_sha256': approved_rules_sha256, 'max_new': 50,
                           'receipt_path': RECEIPT}))


def single_parent(reader, commit):
    a.checked(commit, 40)
    rows = reader.local.run('rev-list', '--parents', '-n', '1', commit).decode().split()
    if len(rows) != 2 or rows[0] != commit:
        fail('entry_non_linear_history')
    return rows[1]


def mainline_parent(reader, commit, expected_get_tree):
    a.checked(commit, 40); a.checked(expected_get_tree, 40)
    rows = reader.local.run('rev-list', '--parents', '-n', '1', commit).decode().split()
    if not rows or rows[0] != commit or len(rows) not in (2, 3):
        fail('entry_non_linear_history')
    parent = rows[1]
    a.checked(parent, 40)
    if len(rows) == 3 and reader.source_get_tree(parent) != expected_get_tree:
        fail_history('merge_first_parent_tree_mismatch')
    return parent


@dataclass(frozen=True)
class VerifiedHistoryWitness:
    """In-memory authority returned only by original publication verification.

    Never read this from a plan, receipt, CLI, repository file or business state.
    """
    receipt_commit: str
    receipt_sha256: str
    receipt_sequence: int
    observed_commit: str
    record_sha256: str


def select_history_witness(value, *, ancestor=None, observed=None):
    """Exact interval selection, never widen a witness over an unknown target."""
    if value is None: return None
    if type(value) is VerifiedHistoryWitness: return value
    import get_notion_idle_rotation as rotation
    return rotation.history_interval(value, ancestor=ancestor, observed=observed)


def check_history_witness(value):
    if value is None or type(value) is VerifiedHistoryWitness: return
    import get_notion_idle_rotation as rotation
    if type(value) is not rotation.VerifiedHistoryBundle or value.seal is not rotation._JOINT_SEAL:
        fail('entry_history_witness')
    rotation.VerifiedHistoryBundle(value.intervals, rotation._JOINT_SEAL)


def unchanged_get_history(reader, ancestor, descendant, *, history_witness=None, source_id=None):
    """Reject an edit-and-revert too, rather than merely comparing tip trees."""
    a.checked(ancestor, 40); a.checked(descendant, 40)
    expected = reader.source_get_tree(ancestor)
    history_witness = select_history_witness(history_witness, ancestor=ancestor)
    if history_witness is not None:
        if type(history_witness) is not VerifiedHistoryWitness:
            fail('entry_history_witness')
        if ancestor == history_witness.receipt_commit:
            # The fresh original canonical proof authenticates the prefix;
            # endpoint equality alone never supplies that authority.
            raw = reader.read(ancestor, FULL_RECEIPT, MAX_RECEIPT)
            value = m.strict_capture_json(raw, MAX_RECEIPT)
            if (a.sha(raw) != history_witness.receipt_sha256
                    or type(value.get('sequence')) is not int
                    or value['sequence'] != history_witness.receipt_sequence
                    or reader.source_get_tree(history_witness.observed_commit) != expected):
                fail('entry_history_witness')
            ancestor = history_witness.observed_commit
    cursor = descendant
    pilot_migration = None
    pilot_start_allowed = True
    for _ in range(MAX_INTERVENING + 1):
        current = reader.source_get_tree(cursor)
        if current != expected:
            if pilot_migration is None:
                # This is a fixed historical Get-tree transition in the trusted
                # lineage, not a transition caused by the current source item.
                if not pilot_start_allowed:
                    fail_history('pilot_start_disallowed')
                if PILOT_MIGRATION is None:
                    fail_history('pilot_migration_unconfigured')
                if type(PILOT_MIGRATION) is not tuple or len(PILOT_MIGRATION) != 4:
                    fail_history('pilot_migration_malformed')
                before, migration, before_tree, after_tree = PILOT_MIGRATION
                if expected != before_tree or current != after_tree:
                    fail_history('pilot_tree_pair_unmatched')
                pilot_migration = (before, migration, before_tree, after_tree)
                pilot_start_allowed = False
            before, migration, before_tree, after_tree = pilot_migration
            if current != after_tree:
                fail_history('pilot_tree_diverged')
            if cursor == migration:
                if single_parent(reader, cursor) != before:
                    fail_history('pilot_parent_mismatch')
                expected = before_tree
                cursor = before
                pilot_migration = None
            else:
                cursor = mainline_parent(reader, cursor, after_tree)
            continue
        if pilot_migration is not None:
            fail_history('pilot_migration_unclosed')
        if cursor == ancestor:
            return
        cursor = mainline_parent(reader, cursor, expected)
        pilot_start_allowed = False
    fail('entry_history_bound')


def delta(reader, before, after, *, exclude_receipt=True):
    old = reader.tree(before); new = reader.tree(after)
    return [{'path': p, 'before': list(old[p]) if p in old else None, 'after': list(new[p]) if p in new else None}
            for p in sorted(set(old) | set(new))
            if old.get(p) != new.get(p) and (not exclude_receipt or p != FULL_RECEIPT)]


def load_states(reader, evidence, commit, legacy_sha, registry_sha, *, source_id=None):
    current = n.git_inventory(reader, commit, only_id=source_id)
    legacy, registry = engine.load_states(reader, evidence, commit, current, legacy_sha, registry_sha, source_id=source_id)
    n.verify_registered_asset_refs(reader,commit,registry,source_id=source_id)
    if a.encoded(legacy) != current[a.STATE]:
        fail('entry_state_encoding')
    return current, legacy, registry


def verify_asset_delta(reader,before,after,changed_paths,registry):
    """Bind changed image blobs to registry owners and verify exact bytes."""
    owners={}
    for ident,row in registry['newById'].items():
        manifest=(row.get('source_metadata',{}).get('inline_images') or {}).get('images',[])
        for image in manifest:
            owners.setdefault(image['asset_path'],[]).append((ident,image))
    found={}
    for path in changed_paths & set(owners):
        matches=owners[path];expected=matches[0][1]
        if any(any(item[key]!=expected[key] for key in ('asset_path','asset_blob','asset_sha256','asset_bytes','asset_mime')) for _,item in matches):fail('entry_ownership_changed')
        old_tree=reader.tree(before).get(path);new_tree=reader.tree(after).get(path)
        if (new_tree!=('100644','blob',expected['asset_blob'])
                or old_tree is not None and old_tree!=new_tree):fail('entry_ownership_changed')
        data=reader.read(after,path,expected['asset_bytes'])
        if (len(data)!=expected['asset_bytes'] or a.sha(data)!=expected['asset_sha256']
                or r.oid(data)!=expected['asset_blob']):fail('entry_ownership_changed')
        found[path]={ident for ident,_ in matches}
    return found


def owners(legacy, registry):
    out = {i: (row['ownership']['filePath'], row['ownership']['mirror_sha256'], row['baseline_blob'])
           for i, row in legacy['legacyById'].items()}
    for ident, row in registry['newById'].items():
        if ident in out:
            fail('entry_ownership_changed')
        out[ident] = (row['filePath'], row['mirror_sha256'], row['baseline_blob'])
    if len({row[0] for row in out.values()}) != len(out):
        fail('entry_ownership_changed')
    return out


def bootstrap_states(reader, evidence, bootstrap):
    commit = bootstrap['commit']
    if evidence.anchor != bootstrap['capture_anchor'] or reader.source_get_tree(commit) != bootstrap['get_tree']:
        fail('entry_bootstrap_changed')
    tree = reader.tree(commit)
    if FULL_RECEIPT in tree or r.GET + '/' + n.STATE in tree:
        fail('entry_bootstrap_changed')
    result = load_states(reader, evidence, commit, bootstrap['legacy_sha256'], None)
    _, legacy, registry = result
    counts = {'legacy_managed': len(legacy['legacyById']), 'legacy_conflicts': len(legacy['conflictsById']),
              'legacy_pending': len(legacy['pending_ids']),
              'existing_unmanaged': sum(v == 'existing_unmanaged' for v in registry['reserved'].values())}
    if any(counts[k] != bootstrap[k] for k in counts):
        fail('entry_bootstrap_changed')
    return result


def read_receipt(reader, commit, expected_sha, policy):
    a.checked(commit, 40); a.checked(expected_sha, 64)
    raw = reader.read(commit, FULL_RECEIPT, MAX_RECEIPT)
    if a.sha(raw) != expected_sha:
        fail('entry_receipt_anchor')
    value = m.strict_capture_json(raw, MAX_RECEIPT)
    if not isinstance(value, dict) or set(value) not in (LEGACY_RECEIPT_KEYS, RECEIPT_KEYS) or a.encoded(value) != raw:
        fail('entry_receipt_schema')
    if set(value)==LEGACY_RECEIPT_KEYS:
        value={**value,'source_scope':'complete_catalog'}
    if value['source_scope'] not in ('complete_catalog','single_note'):
        fail('entry_receipt_schema')
    if value['contract'] != RECEIPT_CONTRACT or (value['policy_sha256'] != policy
            and not recovery.accepts_receipt(commit, expected_sha, value, policy)):
        fail('entry_policy_changed')
    if type(value['sequence']) is not int or not 1 <= value['sequence'] <= 1000000:
        fail('entry_receipt_schema')
    for key in ('parent_commit', 'previous_receipt_commit', 'before_get_tree'):
        a.checked(value[key], 40)
    for key in ('previous_legacy_sha256', 'state_sha256', 'registry_sha256', 'delta_sha256',
                'engine_payload_sha256', 'source_summary_sha256', 'detail_receipts_sha256'):
        a.checked(value[key], 64)
    for key in ('previous_receipt_sha256', 'previous_registry_sha256'):
        if value[key] is not None:
            a.checked(value[key], 64)
    ids = value['changed_content_ids']
    if not isinstance(ids, list) or len(ids) > n.MAX_RECORDS or any(not isinstance(i, str) for i in ids) or ids != sorted(set(ids)):
        fail('entry_receipt_schema')
    if value['source_scope']=='single_note' and len(ids)>1:fail('entry_receipt_schema')
    for ident in ids:
        n.identity(ident)
    counts = value['counts']
    if not isinstance(counts, dict) or set(counts) != set(engine.COUNTS) - {'changed_paths', 'notes_changed', 'changed_bytes', 'transaction_bytes'}:
        fail('entry_receipt_schema')
    if any(type(v) is not int or not 0 <= v <= m.MAX_CHANGED_BYTES for v in counts.values()) or counts['new_selected'] > 50:
        fail('entry_receipt_schema')
    if not n.valid_reason_counts(value['conflict_reason_counts'], counts['new_pending']):
        fail('entry_receipt_schema')
    return value


def transition(reader, evidence, before, after, before_states, after_states, expected_ids=None, *, source_id=None):
    """Complete commit delta plus immutable ownership; never overwrite foreign files."""
    _, old_legacy, old_registry = before_states
    current, legacy, registry = after_states
    old = owners(old_legacy, old_registry); new = owners(legacy, registry)
    if set(old_legacy['legacyById']) != set(legacy['legacyById']) or old_legacy['adoption_ids'] != legacy['adoption_ids']:
        fail('entry_ownership_changed')
    if not set(old_registry['newById']) <= set(registry['newById']):
        fail('entry_ownership_changed')
    if any(i not in new or old[i][0] != new[i][0] for i in old):
        fail('entry_ownership_changed')
    if len(set(new) - set(old)) > 50:
        fail('entry_batch_bound')
    changed = delta(reader, before, after)
    changed_paths = {change['path'] for change in changed}
    if source_id is not None and set(new) - set(old) - {source_id}:
        fail('entry_single_scope')
    # A state-only receipt does not record the selected ID. Keep its original
    # full checks so a later reader can reproduce the proof without guessing.
    single_content = source_id in new and r.GET + '/' + new[source_id][0] in changed_paths
    preserved = set()
    for ident, previous in old_registry['newById'].items():
        row = registry['newById'][ident]
        rel = previous['filePath']
        path = r.GET + '/' + rel
        data = current.get(rel)
        held_unchanged = (ident in registry['conflictsById']
                          and row == previous and path not in changed_paths and data is not None
                          and before_states[0].get(rel) == data
                          and reader.tree(before).get(path) == ('100644', 'blob', r.oid(data)))
        if (single_content and ident != source_id) or held_unchanged:
            if (row != previous or path in changed_paths or data is None
                    or before_states[0].get(rel) != data
                    or reader.tree(before).get(path) != ('100644', 'blob', r.oid(data))):
                fail('entry_ownership_changed')
            # The complete delta proves the same mode and blob at the new tip.
            # Preserve these bytes; an externally edited owner cannot be overwritten.
            preserved.add(ident)
            continue
        old_metadata=deepcopy(previous['source_metadata']);old_metadata.pop('inline_images',None)
        if any(row[k] != previous[k] for k in ('filePath', 'created_at', 'preview_anchor', 'unknown_sha256')) or not set(previous['text_fields']) <= set(row['text_fields']) or n.metadata_missing(old_metadata, row['source_metadata']):
            fail('entry_ownership_changed')
        if n.json_header(before_states[0][previous['filePath']]).get('note_type') != n.json_header(current[row['filePath']]).get('note_type'):
            fail('entry_ownership_changed')
    for ident, (rel, digest, blob) in new.items():
        if ident in preserved:
            continue
        data = current.get(rel)
        if data is None or a.sha(data) != digest or r.oid(data) != blob:
            fail('entry_ownership_changed')
    path_ids = {r.GET + '/' + row[0]: ident for ident, row in new.items()}
    asset_ids=verify_asset_delta(reader,before,after,changed_paths,registry)
    content_ids = set()
    for row in changed:
        path = row['path']; r.safe_path(path)
        if row['after'] is None or row['after'][:2] != ['100644', 'blob']:
            fail('entry_foreign_delta')
        if path in STATE_PATHS:
            continue
        if path in asset_ids:
            content_ids.update(asset_ids[path])
            continue
        if path not in path_ids:
            fail('entry_foreign_delta')
        ident = path_ids[path]; rel, digest, blob = new[ident]
        data = current.get(rel)
        if data is None or a.sha(data) != digest or r.oid(data) != blob or row['after'][2] != blob:
            fail('entry_ownership_changed')
        content_ids.add(ident)
    ids = sorted(content_ids)
    if source_id is not None and set(ids) - {source_id}:
        fail('entry_single_scope')
    if len(ids) != len(set(ids)) or expected_ids is not None and ids != expected_ids:
        fail('entry_content_ids')
    return ids


def verify_trusted_publication(reader, evidence, anchor, policy, bootstrap, *, history_witness=None, source_id=None):
    """The commit/hash pair is EXTERNAL trust, supplied only after verified readback.

    We verify its complete transaction and its immediate predecessor link. The
    trusted last-publication anchor stands for the independently verified earlier
    chain; this is not an attempt to infer authenticated authorship from Git.
    """
    if not isinstance(anchor, dict) or set(anchor) != {'commit', 'receipt_sha256'}:
        fail('entry_external_anchor_required')
    commit = anchor['commit']
    receipt = read_receipt(reader, commit, anchor['receipt_sha256'], policy)
    parent = single_parent(reader, commit)
    if parent != receipt['parent_commit'] or reader.source_get_tree(parent) != receipt['before_get_tree']:
        fail('entry_receipt_parent')
    previous = receipt['previous_receipt_commit']
    if receipt['sequence'] == 1:
        if previous != bootstrap['commit'] or receipt['previous_receipt_sha256'] is not None or receipt['previous_registry_sha256'] is not None or receipt['previous_legacy_sha256'] != bootstrap['legacy_sha256']:
            fail('entry_receipt_anchor')
    else:
        if receipt['previous_receipt_sha256'] is None or receipt['previous_registry_sha256'] is None:
            fail('entry_receipt_anchor')
        prior = read_receipt(reader, previous, receipt['previous_receipt_sha256'], policy)
        if prior['sequence'] + 1 != receipt['sequence'] or prior['state_sha256'] != receipt['previous_legacy_sha256'] or prior['registry_sha256'] != receipt['previous_registry_sha256']:
            fail('entry_receipt_anchor')
    unchanged_get_history(reader, previous, parent, history_witness=history_witness, source_id=source_id)
    if a.sha(a.encoded(delta(reader, parent, commit))) != receipt['delta_sha256']:
        fail('entry_foreign_delta')
    all_delta = delta(reader, parent, commit, exclude_receipt=False)
    if FULL_RECEIPT not in {row['path'] for row in all_delta} or len(all_delta) < 2:
        fail('entry_receipt_only_commit')
    prior_states = load_states(reader, evidence, parent, receipt['previous_legacy_sha256'], receipt['previous_registry_sha256'], source_id=source_id)
    next_states = load_states(reader, evidence, commit, receipt['state_sha256'], receipt['registry_sha256'], source_id=source_id)
    ids = receipt['changed_content_ids']
    receipt_source = ids[0] if receipt['source_scope'] == 'single_note' and len(ids) == 1 else None
    transition(reader, evidence, parent, commit, prior_states, next_states, ids, source_id=receipt_source)
    if sum(receipt['counts'][k] for k in ('new_created', 'new_preview_imported', 'new_updated', 'legacy_source_updated')) != len(receipt['changed_content_ids']):
        fail('entry_counts')
    check_counts(receipt['counts'], next_states)
    return receipt, next_states


def check_counts(counts, states):
    _, legacy, registry = states
    expected = {'legacy_managed': len(legacy['legacyById']), 'legacy_conflicts': len(legacy['conflictsById']),
                'legacy_pending': len(legacy['pending_ids']), 'new_managed': len(registry['newById']),
                'new_pending': len(registry['conflictsById']),
                'existing_unmanaged': sum(v == 'existing_unmanaged' for v in registry['reserved'].values())}
    if any(counts.get(k) != v for k, v in expected.items()):
        fail('entry_counts')


def trust_current(reader, evidence, parent, approved_rules_sha256, anchor=None, *, bootstrap=BOOTSTRAP, history_witness=None, source_id=None):
    if approved_rules_sha256 != rules_sha256():
        fail('entry_rules_changed')
    policy = policy_sha256(approved_rules_sha256, bootstrap)
    initial = bootstrap_states(reader, evidence, bootstrap)
    if anchor is None:
        trusted = bootstrap['commit']; receipt = None; states = initial
    else:
        receipt, states = verify_trusted_publication(reader, evidence, anchor, policy, bootstrap, history_witness=history_witness, source_id=source_id)
        trusted = anchor['commit']
    unchanged_get_history(reader, trusted, parent, history_witness=history_witness, source_id=source_id)
    # Equal full Get trees guarantee these exact state/inventory bytes at parent.
    states = load_states(reader, evidence, parent,
        receipt['state_sha256'] if receipt else bootstrap['legacy_sha256'],
        receipt['registry_sha256'] if receipt else None, source_id=source_id)
    return policy, receipt, states


def add_receipt(reader, parent, engine_plan, files, receipt_bytes):
    """Rebuild directly on the same parent, never a reachable second snapshot."""
    local = reader.local; wanted = dict(reader.tree(engine_plan['candidate_commit']))
    writes = {**files, FULL_RECEIPT: receipt_bytes}
    wanted[FULL_RECEIPT] = ('100644', 'blob', r.oid(receipt_bytes))
    with tempfile.TemporaryDirectory(prefix='get-receipt-index-') as folder:
        env = dict(local.env, GIT_INDEX_FILE=str(Path(folder) / 'index'))
        local.run('read-tree', parent, env=env)
        for path, data in sorted(writes.items()):
            blob = local.run('hash-object', '-w', '--stdin', data=data, env=env).decode().strip()
            if blob != r.oid(data):
                fail('entry_readback')
            local.run('update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + path, env=env)
        tree = a.checked(local.run('write-tree', '--missing-ok', env=env).decode().strip(), 40)
        if local.tree(tree) != wanted or any(local.blob(wanted[p][2]) != v for p, v in writes.items()):
            fail('entry_readback')
        timestamp = local.run('show', '-s', '--format=%ct', parent).decode().strip()
        if not timestamp.isdigit():
            fail('entry_readback')
        env.update(GIT_AUTHOR_NAME='Get local proposal', GIT_AUTHOR_EMAIL='get-proposal@example.invalid',
                   GIT_COMMITTER_NAME='Get local proposal', GIT_COMMITTER_EMAIL='get-proposal@example.invalid',
                   GIT_AUTHOR_DATE='@' + timestamp + ' +0000', GIT_COMMITTER_DATE='@' + timestamp + ' +0000')
        commit = a.checked(local.run('commit-tree', tree, '-p', parent, '-m', 'Prepare canonical Get transaction with receipt', env=env).decode().strip(), 40)
    if reader.tree(commit) != wanted or single_parent(reader, commit) != parent:
        fail('entry_readback')
    return commit, tree, writes


def prepare(reader, evidence, parent, source, *, approved_rules_sha256, anchor=None, preview=None, bootstrap=BOOTSTRAP, trigger='manual', history_witness=None, source_id=None):
    if trigger not in ('manual', 'schedule'):
        fail('entry_trigger')
    policy, previous, states = trust_current(reader, evidence, parent, approved_rules_sha256, anchor, bootstrap=bootstrap, history_witness=history_witness, source_id=source_id)
    current, legacy, registry = states
    legacy_sha = previous['state_sha256'] if previous else bootstrap['legacy_sha256']
    registry_sha = previous['registry_sha256'] if previous else None
    plan, files = engine.prepare(reader, evidence, parent, current, source, legacy_sha=legacy_sha,
                                 registry_sha=registry_sha, preview=preview, source_id=source_id)
    # The canonical engine is the sole selector/renderer/state transition owner.
    check_counts(plan['counts'], load_states(reader, evidence, plan['candidate_commit'], plan['state_sha256'], plan['registry_sha256'], source_id=source_id))
    plan['entry_contract'] = CONTRACT
    plan['policy_sha256'] = policy
    plan['engine_approval_payload_sha256'] = plan['approval_payload_sha256']
    plan['single_source_id'] = source_id
    plan['source_scope']='single_note' if source_id is not None else 'complete_catalog'
    plan['catalog_complete']=source_id is None
    if not files:
        plan['receipt_sha256'] = anchor['receipt_sha256'] if anchor else None
        plan['receipt_commit'] = anchor['commit'] if anchor else None
        plan['proposed_content_ids'] = []
        return plan, files
    next_states = load_states(reader, evidence, plan['candidate_commit'], plan['state_sha256'], plan['registry_sha256'], source_id=source_id)
    ids = transition(reader, evidence, parent, plan['candidate_commit'], states, next_states, source_id=source_id)
    receipt = {'contract': RECEIPT_CONTRACT, 'policy_sha256': policy,
               'sequence': previous['sequence'] + 1 if previous else 1, 'parent_commit': parent,
               'previous_receipt_commit': anchor['commit'] if anchor else bootstrap['commit'],
               'previous_receipt_sha256': anchor['receipt_sha256'] if anchor else None,
               'before_get_tree': reader.source_get_tree(parent),
               'previous_legacy_sha256': legacy_sha, 'previous_registry_sha256': registry_sha,
               'state_sha256': plan['state_sha256'], 'registry_sha256': plan['registry_sha256'],
               'delta_sha256': a.sha(a.encoded(delta(reader, parent, plan['candidate_commit']))),
               'engine_payload_sha256': plan['engine_approval_payload_sha256'],
               'source_summary_sha256': plan['source_summary_sha256'],
               'source_scope': plan['source_scope'],
               'detail_receipts_sha256': plan['detail_receipts_sha256'],
               'changed_content_ids': ids, 'counts': plan['counts'],
               'conflict_reason_counts': plan['conflict_reason_counts']}
    raw = a.encoded(receipt)
    if len(raw) > MAX_RECEIPT:
        fail('entry_receipt_budget')
    commit, tree, files = add_receipt(reader, parent, plan, files, raw)
    before = reader.tree(parent); after = reader.tree(commit)
    plan.update(candidate_commit=commit, candidate_tree=tree, receipt_commit=commit, receipt_sha256=a.sha(raw), proposed_content_ids=ids,
                changes=[{'path': p, 'before_blob': before.get(p, [None, None, None])[2],
                          'after_blob': after[p][2], 'sha256': a.sha(data), 'bytes': len(data)}
                         for p, data in sorted(files.items())])
    existing = {row[2] for row in before.values()}
    plan['transaction_bytes'] = sum(len(data) for oid, data in {r.oid(v): v for v in files.values()}.items() if oid not in existing)
    # Receipt parent pins the complete transaction. A changed archive parent
    # needs a fresh local preparation, preserving the archive commit verbatim.
    plan['approval_payload_sha256'] = a.sha(a.encoded({'policy_sha256': policy,
        'engine_payload_sha256': plan['engine_approval_payload_sha256'], 'receipt_sha256': plan['receipt_sha256']}))
    verify_plan(reader, evidence, plan, files, approved_rules_sha256, bootstrap=bootstrap, history_witness=history_witness, source_id=source_id)
    return plan, files


def verify_plan(reader, evidence, plan, files, approved_rules_sha256, *, bootstrap=BOOTSTRAP, history_witness=None, source_id=None):
    engine.verify_plan(reader, plan, files, with_recurring_receipt=True)
    bootstrap_states(reader, evidence, bootstrap)
    if plan.get('entry_contract') != CONTRACT or plan.get('policy_sha256') != policy_sha256(approved_rules_sha256, bootstrap) or approved_rules_sha256 != rules_sha256():
        fail('entry_policy_changed')
    single_id=plan.get('single_source_id');single_path=plan.get('single_source_path')
    if (single_id is None)!=(single_path is None):fail('entry_single_scope')
    if single_id != source_id:
        fail('entry_single_scope')
    if (plan.get('source_scope') != ('single_note' if source_id is not None else 'complete_catalog')
            or plan.get('catalog_complete') is not (source_id is None)):
        fail('entry_single_scope')
    if single_id is not None:
        n.identity(single_id);r.safe_path(single_path)
        if any(value!=single_id for value in plan.get('proposed_content_ids',[])):fail('entry_single_scope')
    if not files:
        if plan.get('receipt_commit') is not None:
            verify_trusted_publication(reader, evidence, {'commit': plan['receipt_commit'], 'receipt_sha256': plan['receipt_sha256']}, plan['policy_sha256'], bootstrap, history_witness=history_witness, source_id=source_id)
            unchanged_get_history(reader, plan['receipt_commit'], plan['parent_commit'], history_witness=history_witness, source_id=source_id)
        if plan.get('proposed_content_ids') != []:
            fail('entry_content_ids')
        return
    if plan.get('receipt_commit') != plan['candidate_commit']:
        fail('entry_receipt_anchor')
    receipt, _ = verify_trusted_publication(reader, evidence,
        {'commit': plan['candidate_commit'], 'receipt_sha256': plan['receipt_sha256']}, plan['policy_sha256'], bootstrap, history_witness=history_witness, source_id=source_id)
    if (receipt['changed_content_ids'] != plan['proposed_content_ids'] or receipt['counts'] != plan['counts']
            or receipt['engine_payload_sha256'] != plan['engine_approval_payload_sha256']
            or receipt['source_scope'] != plan['source_scope']
            or (single_id is not None and any(value!=single_id for value in receipt['changed_content_ids']))):
        fail('entry_plan')
    expected = a.sha(a.encoded({'policy_sha256': plan['policy_sha256'],
        'engine_payload_sha256': plan['engine_approval_payload_sha256'], 'receipt_sha256': plan['receipt_sha256']}))
    if expected != plan['approval_payload_sha256']:
        fail('entry_plan')


def confirmed_private_handoff(reader, evidence, plan, files, publication, approved_rules_sha256, *, bootstrap=BOOTSTRAP, history_witness=None, source_id=None):
    """For a future sink only AFTER the shared publisher's verified readback.

    This helper is not a publisher or a trust-provenance fetcher. The caller must
    pass the actual m.publish result, never a file supplied by the repository.
    State-only or no-op transactions yield no IDs and never enqueue Notion.
    """
    if publication != {'main_updated': bool(files), 'cas_attempts': 1 if files else 0, 'readback_verified': True}:
        fail('entry_publication_unconfirmed')
    verify_plan(reader, evidence, plan, files, approved_rules_sha256, bootstrap=bootstrap, history_witness=history_witness, source_id=source_id)
    return {'contract': 'get-confirmed-content-handoff-1', 'commit': plan['candidate_commit'],
            'receipt_commit': plan['receipt_commit'], 'receipt_sha256': plan['receipt_sha256'],
            'changed_content_ids': list(plan['proposed_content_ids']),
            'source_scope': plan['source_scope'], 'catalog_complete': plan['catalog_complete']}


def public_report(plan, files):
    """Counts and hashes only; reject malformed reports rather than echo input."""
    bad = {'status': 'recurring_local_candidate_failed', 'main_updated': False,
           'cas_attempts': 0, 'apply_ready': False, 'schedule_enabled': False}
    try:
        report = {k: a.checked(plan[k], width) for k, width in engine.HASHES if k != 'plan_sha256'}
        report['plan_sha256'] = a.sha(a.encoded(plan))
        counts = plan['counts']
        keys = set(engine.COUNTS) - {'changed_paths', 'notes_changed', 'changed_bytes', 'transaction_bytes'}
        if not isinstance(counts, dict) or set(counts) != keys or any(type(v) is not int or not 0 <= v <= m.MAX_CHANGED_BYTES for v in counts.values()):
            return bad
        report.update(counts)
        a.checked(plan['policy_sha256'], 64)
        if plan['receipt_sha256'] is not None:
            a.checked(plan['receipt_sha256'], 64)
            a.checked(plan['receipt_commit'], 40)
        if type(plan['transaction_bytes']) is not int or not 0 <= plan['transaction_bytes'] <= m.MAX_CHANGED_BYTES:
            return bad
        ids = plan['proposed_content_ids']
        if not isinstance(ids, list) or len(ids) > n.MAX_RECORDS:
            return bad
        report.update(status='recurring_local_candidate_verified', main_updated=False, cas_attempts=0,
                      local_proposal_only=True, apply_ready=False, schedule_enabled=False,
                      changed_paths=len(files), notes_changed=len(ids),
                      changed_bytes=sum(map(len, files.values())), transaction_bytes=plan['transaction_bytes'],
                      receipt_commit=plan['receipt_commit'], receipt_sha256=plan['receipt_sha256'], policy_sha256=plan['policy_sha256'],
                      source_scope=plan['source_scope'], catalog_complete=plan['catalog_complete'],
                      asset_downloads=0, notion_writes=0, confirmed_content_ids=0,
                      opaque_source_fields_rendered=False, complete_source_rendering_claimed=False)
        return report
    except Exception:
        return bad


class SnapshotSource:
    """Offline snapshots use the same enumerate/detail protocol as live Get."""
    def __init__(self, value):
        if not isinstance(value, dict) or set(value) != {'summaries', 'terminal', 'details'} or not isinstance(value['details'], dict):
            fail('entry_source_schema')
        self.value = value

    def enumerate(self, source_id=None):
        return deepcopy(self.value['summaries']), self.value['terminal']

    def detail_raw(self, ident):
        if ident not in self.value['details']:
            fail('entry_source_detail_missing')
        value = self.value['details'][ident]
        if value is None:
            raise RuntimeError('GetNote HTTP 404')
        return a.encoded({'data': {'note': value}})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'source-snapshot', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('parent', 'approved-rules-sha256'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--trusted-receipt-commit'); parser.add_argument('--trusted-receipt-sha256')
    parser.add_argument('--trigger', choices=('manual', 'schedule'), default='manual')
    args = parser.parse_args(argv)
    try:
        if bool(args.trusted_receipt_commit) != bool(args.trusted_receipt_sha256):
            fail('entry_external_anchor_required')
        anchor = {'commit': args.trusted_receipt_commit, 'receipt_sha256': args.trusted_receipt_sha256} if args.trusted_receipt_commit else None
        reader = a.OfflineGit(args.repo)
        evidence = a.CaptureEvidence(reader, BOOTSTRAP['capture_anchor'])
        value = m.strict_capture_json(a.bounded_file(args.source_snapshot, 128 * m.c.MIB), 128 * m.c.MIB)
        preview = n.preview_records(reader) if anchor is None else None
        plan, files = prepare(reader, evidence, args.parent, SnapshotSource(value),
            approved_rules_sha256=args.approved_rules_sha256, anchor=anchor, preview=preview, trigger=args.trigger)
        # Private artifacts are local only; exclusive/atomic helper preserves any
        # pre-existing foreign output and is never attached to public Actions.
        old_umask = os.umask(0o077)
        try:
            a.save_git_plan(args.output, plan, {'proposed_content_ids': plan['proposed_content_ids'],
                                              'confirmed_content_ids': [], 'receipt_sha256': plan['receipt_sha256']})
        finally:
            os.umask(old_umask)
        print(json.dumps(public_report(plan, files))); return 0
    except Exception:
        print(json.dumps({'status': 'recurring_local_candidate_failed', 'main_updated': False,
                          'cas_attempts': 0, 'apply_ready': False, 'schedule_enabled': False})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
