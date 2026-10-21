"""Bounded property-only runtime adapter. No credentials or independent writes.

Entry is through get_notion_runtime.py, retaining its private clone, authenticated
controller, confirmed remote journal and single-writer guard. Approved manifest
hashes must be added to the reviewed runtime release; CLI hashes alone grant no
standing scope. Historical sources are never mislabeled as new publications.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy
from collections import Counter
from pathlib import Path

import get_notion_executor as e
import get_notion_properties as props
import get_notion_property_transaction as tx
import get_notion_outbox as outbox
import get_notion_live_transport as transport
import get_notion_pipeline as pipeline
import get_confirmed_source as source_loader

# Hashes only. Actual manifests remain private. Empty means no apply activation.
APPROVED_MANIFESTS = frozenset()
DEPENDENCY_LOCK_SHA256 = _private_bindings.value('get_notion_properties_runtime', 'DEPENDENCY_LOCK_SHA256')


def authorize(manifest, expected, *, apply):
    if not isinstance(manifest, dict) or e.digest(manifest) != expected: e.fail('approval_missing')
    e.p.hex_hash(expected, 64)
    if apply and expected not in APPROVED_MANIFESTS: e.fail('approval_missing')
    e.p.hex_hash(manifest.get('source_commit'), 40)
    keys = {'contract', 'source_commit', 'data_source_id', 'schema_sha256', 'fields', 'fill_empty', 'records', 'registry'}
    if (set(manifest) != keys or manifest['contract'] != tx.CONTRACT or manifest['fill_empty'] is not True
            or manifest['fields'] != sorted(props.FIELDS)): e.fail('approval_missing')
    e.p.notion_id(manifest['data_source_id']); e.p.hex_hash(manifest['schema_sha256'], 64)
    records = manifest.get('records')
    if not isinstance(records, dict) or not 1 <= len(records) <= 10: e.fail('request_budget')
    paths = set()
    for ident, row in records.items():
        e.p.note_id(ident)
        if not isinstance(row, dict) or not {'path', 'blob', 'sha256', 'privacy_review_sha256'} <= set(row) <= {'path', 'blob', 'sha256', 'privacy_review_sha256', 'property_adoption'}: e.fail('approval_missing')
        e.p.safe_path(row['path'])
        if row['path'] in paths or not row['path'].lower().endswith(('.md', '.markdown')): e.fail('source_invalid')
        paths.add(row['path'])
        for key, width in (('blob',40),('sha256',64),('privacy_review_sha256',64)): e.p.hex_hash(row[key],width)
    binding = manifest['registry']
    if binding is not None:
        if not isinstance(binding, dict) or set(binding) != {'path','blob','sha256'} or binding['path'] != props.REGISTRY: e.fail('source_invalid')
        e.p.hex_hash(binding['blob'],40);e.p.hex_hash(binding['sha256'],64)
    return expected


class ManifestPublication:
    def __init__(self, root, manifest, expected):
        authorize(manifest, expected, apply=False)
        self.repo = e.git.OfflineGit(root); self.current_ref = 'refs/heads/main'
        commit = manifest['source_commit']; tree = self.repo.tree(commit)
        records = {ident: {k: row[k] for k in ('path', 'blob', 'sha256')} for ident, row in manifest['records'].items()}
        total = 0
        for ident, row in records.items():
            e.p.note_id(ident); e.p.safe_path(row['path'])
            if tree.get(row['path']) != ('100644', 'blob', row['blob']): e.fail('source_invalid')
            data = self.repo.read(commit, row['path'], e.p.MAX_NOTE); total += len(data)
            if total > 10 * e.p.MAX_NOTE or e.digest(data) != row['sha256']: e.fail('source_invalid')
            e.p.source_body(data, ident)
        self.descriptor = {'repository': e.p.REPOSITORY, 'commit': commit, 'parent': commit,
            'changed_content_ids': [], 'records': records, 'initial_cohort': None,
            'historical_property_manifest_sha256': expected,
            'content_tree': {path: value[2] for path, value in tree.items()
                if path.startswith('Raw/03 Get/') and value[:2] == ('100644', 'blob')}}
        if len(e.encoded(self.descriptor)) > 8 * 1024 * 1024: e.fail('request_budget')
        self.check(self.descriptor)
    check = e.GitPublication.check
    check_assets = e.GitPublication.check_assets
    read = e.GitPublication.read


def dispatch(local, remote, token, root, store, request, *, manifest, expected,
             policy_hash, apply, writers_guard):
    authorize(manifest, expected, apply=apply)
    check_dependency()
    current, version = store.read(); outbox.check_state(current)
    active = current['engine_state']['journal']
    if active is not None and (not active['plan'].get('property_only') or active['plan'].get('property_manifest_sha256') != expected): e.fail('journal_busy')
    loader = source_loader.Loader(local, remote, token, root, policy_hash)
    loader.commit(manifest['source_commit'])
    for row in manifest['records'].values(): loader.blob(manifest['source_commit'], row['path'], e.p.MAX_NOTE)
    if manifest.get('registry') is not None: loader.blob(manifest['source_commit'], props.REGISTRY, 16 * 1024 * 1024)
    publication = ManifestPublication(local.vault, manifest, expected)
    source = manifest['data_source_id']; ids = sorted(manifest['records'])
    api = transport.API(request, source, ids)
    schema = api.schema()
    titles = [name for name, value in schema.items() if value.get('type') == 'title']
    if len(titles) != 1: e.fail('property_schema_changed')
    policy = {'data_source_id': source, 'id_property': '笔记 ID', 'title_property': titles[0],
        'allow_create': False, 'allow_archive': False, 'format_accepted': True,
        'old_writer_evidence': writers_guard()}
    if not e.p.writer_gate(policy): e.fail('writer_gate_unknown')
    adapter = outbox.EngineStore(store); state, sha = adapter.read()
    snapshots = {} if active else {ident: api.read_note(ident) for ident in ids}
    if active:
        plan = deepcopy(active['plan'])
        if (plan['rules_sha256'] != e.rules_sha256() or plan['publication'] != publication.descriptor
                or plan['policy'] != policy or plan.get('property_manifest') != manifest): e.fail('journal_busy')
        if e.digest(props.schema_projection(schema)) != manifest['schema_sha256']: e.fail('property_schema_changed')
    else:
        plan = tx.prepare(publication, state, sha, snapshots, schema, manifest, expected, policy)
    actions = Counter(row['action'] for row in plan['records'])
    report = {'contract': pipeline.CONTRACT, 'status': 'prepared', 'source_mode': 'property_backfill',
        'plan_sha256': e.digest(plan), 'journal_sha256': sha, 'selected': len(plan['records']),
        'deferred': 0, 'retained_conflicts': len(plan['skipped_conflicts']), 'actions': dict(actions), 'reason_counts': dict(Counter(row['reason'] for row in plan['records'] if row['action']=='blocked')),
        'native_notes_read': len(snapshots), 'get_requests': 0, 'asset_downloads': 0,
        'external_write_attempts': None if active else 0, 'write_state': 'unknown' if active else 'not_attempted',
        'prior_journal_effects': 'unresolved' if active else 'none', 'complete_library_claimed': False}
    if apply:
        if not isinstance(request, transport.Transport): e.fail('approval_missing')
        allowed = pipeline.wire_allowlist(plan, api); api.approved = allowed; request.approved = allowed
        result = e.execute(plan, e.digest(plan), publication, api, adapter, expected_store_sha=sha, writers_guard=writers_guard)
        safe = e.public_report(result)
        report.update(status='executed', execution=safe, external_write_attempts=safe['external_write_attempts'], write_state=safe['write_state'])
    final, final_sha = store.read()
    return {**pipeline.public_report(report), 'initial_notes': 0, 'initial_range_candidates': 0,
        'queued': len(final['pending_by_id']),
        'outer_journal_sha256': final_sha, 'source_chain_complete': final['last_enqueued_publication'] == final['observed_publication_tip']}


def check_dependency():
    # A reviewed dependency is required before any durable checkpoint/write.
    if e.digest((Path(__file__).resolve().parents[1] / 'requirements-notion-properties.txt').read_bytes()) != DEPENDENCY_LOCK_SHA256:
        e.fail('property_parser_unavailable')
    try:
        import yaml
        if yaml.__version__ != '6.0.3': e.fail('property_parser_unavailable')
    except ImportError: e.fail('property_parser_unavailable')
