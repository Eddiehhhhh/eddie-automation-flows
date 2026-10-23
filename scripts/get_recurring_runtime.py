"""Prepared live Get orchestration; default read-only, production activation pending.

One canonical engine and one shared CAS publisher. Existing private credentials
are consumed only by CLI entry, never by public Actions provenance. No schedule
is installed here. A separately reviewed production scope is required for apply.
"""
import get_private_proof as private_proof
import argparse
import json
import os
from pathlib import Path
import tempfile

import get_publication_provenance as provenance
import get_recurring_entry as entry
import get_recurring_controller as controller
from get_recurring_entry import a, engine, m, n, r

PRODUCTION_ACTIVATED = True  # Changing this requires the final code/rules review.
SCOPE_CONTRACT = 'get-recurring-production-scope-1'
SCOPE_KEYS = {'contract', 'production_approved', 'public_policy_sha256', 'rules_sha256',
              'get_policy_sha256', 'private_repository', 'main_ref', 'max_new',
              'schedule_minute', 'receipt_path', 'sink', 'permissions',
              'notion_scope_contract', 'notion_batch_max', 'notion_journal_path',
              'notion_initial_proof_sha256'}
ERRORS = {'runtime_activation_pending', 'runtime_scope', 'runtime_context',
          'runtime_receipt', 'runtime_failed'}

# Fixed diagnostics only; private exception text/arguments are never emitted.
STAGES = frozenset(('policy', 'execution_provenance', 'remote_guard', 'checkout',
    'capture_evidence', 'parent_history', 'bootstrap_history', 'bootstrap_inventory',
    'current_inventory', 'receipt_provenance', 'trusted_inputs', 'trusted_states', 'legacy_baselines',
    'new_baselines', 'preview_inputs', 'canonical_prepare', 'candidate_report',
    'publication', 'old_writer_guard', 'credentials', 'source_transport'))
ENTRY_ERRORS = frozenset(('entry_batch_bound', 'entry_bootstrap_changed',
    'entry_content_ids', 'entry_counts', 'entry_external_anchor_required',
    'entry_foreign_delta', 'entry_foreign_get_change', 'entry_history_bound', 'entry_history_witness',
    'entry_non_linear_history', 'entry_ownership_changed', 'entry_plan',
    'entry_policy_changed', 'entry_publication_unconfirmed', 'entry_readback',
    'entry_receipt_anchor', 'entry_receipt_budget', 'entry_receipt_only_commit',
    'entry_receipt_parent', 'entry_receipt_schema', 'entry_rules_changed',
    'entry_source_detail_missing', 'entry_source_schema', 'entry_state_encoding',
    'entry_trigger'))


CORE_ERRORS = frozenset(('adapter_adoption_changed_note', 'adapter_alias', 'adapter_anchor', 'adapter_arguments', 'adapter_atomic_unavailable', 'adapter_baseline_hash', 'adapter_baseline_readback', 'adapter_blob_budget', 'adapter_blob_readback', 'adapter_candidate_publish', 'adapter_candidate_readback', 'adapter_detail_schema', 'adapter_detail_set', 'adapter_duplicate_source', 'adapter_file_budget', 'adapter_file_changed', 'adapter_incomplete_capture', 'adapter_incomplete_list', 'adapter_missing_blob', 'adapter_old_hash', 'adapter_operation', 'adapter_output', 'adapter_output_conflict', 'adapter_ownership_changed', 'adapter_path', 'adapter_receipt', 'adapter_source_changed', 'adapter_source_identity', 'adapter_source_tree', 'adapter_state_budget', 'adapter_state_commit', 'adapter_state_hash', 'adapter_state_schema', 'adapter_state_source_hash', 'adapter_state_worktree_changed', 'adapter_tree_budget', 'adapter_write_scope', 'asset_materialization_required', 'baseline_missing', 'canonical_alias', 'canonical_baseline_identity', 'canonical_baseline_type', 'canonical_baseline_version', 'canonical_budget', 'canonical_current_set', 'canonical_details', 'canonical_duplicate_identity', 'canonical_duplicate_path', 'canonical_generated_header', 'canonical_identity', 'canonical_legacy_binding', 'canonical_legacy_state_missing', 'canonical_note_budget', 'canonical_owner_overlap', 'canonical_parent_changed', 'canonical_path', 'canonical_preview_anchor', 'canonical_preview_blob', 'canonical_preview_schema', 'canonical_readback', 'canonical_reserved_changed', 'canonical_selection', 'canonical_source_schema', 'canonical_state_hash', 'canonical_state_schema', 'canonical_write_scope', 'destination_collision', 'detail_identity', 'detail_required', 'identity_reserved', 'legacy_missing', 'legacy_references_unresolved', 'legacy_structure_ambiguous', 'local_note_edited', 'preview_source_changed', 'provenance_budget', 'provenance_controller_policy', 'provenance_credentials', 'provenance_exclusion', 'provenance_execution', 'provenance_job', 'provenance_job_bound', 'provenance_log_redirect', 'provenance_logs_shape', 'provenance_logs_unavailable', 'provenance_no_receipt', 'provenance_no_verified_publication', 'provenance_policy', 'provenance_receipt_fork', 'provenance_receipt_sequence', 'provenance_record', 'provenance_record_count', 'provenance_repository', 'provenance_run', 'provenance_run_bound', 'provenance_run_changed', 'provenance_schema', 'provenance_target', 'provenance_timestamp', 'provenance_transport', 'provenance_unavailable', 'provenance_unconfirmed', 'provenance_workflow', 'source_created_changed', 'source_field_missing', 'source_metadata_changed', 'source_metadata_shape', 'source_missing', 'source_note_type_changed', 'source_payload_incomplete', 'source_shadowed_field', 'source_shape_unsupported', 'source_structured_text_unmapped', 'source_text_missing', 'source_transcript_pending', 'source_unknown_changed', 'source_unknown_fields', 'source_version_mismatch', 'source_version_race'))
CORE_ERRORS = CORE_ERRORS | {'provenance_prepare_only', 'provenance_history_witness', 'provenance_candidate_bound'}
SOURCE_ERRORS = {'GetNote HTTP '+str(v): 'source_http_'+str(v) for v in (400,401,403,404,408,429,500,502,503,504)}
SOURCE_ERRORS.update({'GetNote transport failed': 'source_transport_failed', 'GetNote redirect blocked': 'source_redirect_blocked', 'GetNote incomplete body': 'source_incomplete_body', 'Get response byte limit reached': 'source_response_budget'})


# All values are program-defined fixed diagnostics, never exception arguments.
PUBLIC_ERROR_CODES = frozenset(ERRORS | m.ERRORS | ENTRY_ERRORS | m.c.ERRORS | CORE_ERRORS | set(SOURCE_ERRORS.values()))

def public_result(result):
    import get_private_runtime_report as report
    return report.public(result, PUBLIC_ERROR_CODES, allowed_stages=STAGES)

def at_stage(stage, operation, *args, **kwargs):
    if stage not in STAGES:
        fail('runtime_failed')
    try:
        return operation(*args, **kwargs)
    except (Exception, SystemExit) as exc:
        if not isinstance(getattr(exc, 'runtime_stage', None), str) or exc.runtime_stage not in STAGES:
            exc.runtime_stage = stage
        raise



def fail(code):
    raise entry.EntryFailure(code)


def validate_policy(policy, expected, bootstrap):
    value = provenance.validate_policy(policy, expected)
    if value['rules_sha256'] != entry.rules_sha256() or value['get_policy_sha256'] != entry.policy_sha256(value['rules_sha256'], bootstrap):
        fail('runtime_scope')
    return value


def validate_scope(scope, expected, policy, policy_hash):
    if not PRODUCTION_ACTIVATED:
        fail('runtime_activation_pending')
    if not isinstance(scope, dict) or set(scope) != SCOPE_KEYS or a.sha(a.encoded(scope)) != a.checked(expected, 64):
        fail('runtime_scope')
    wanted = controller.scope_for(policy, policy_hash)
    if scope != wanted or scope['production_approved'] is not True or type(scope['max_new']) is not int or type(scope['schedule_minute']) is not int:
        fail('runtime_scope')


def context(policy, value):
    if not isinstance(value, dict) or set(value) != {'run_id', 'run_attempt', 'code_sha', 'controller_sha'} or value['code_sha'] != policy['code_sha']:
        fail('runtime_context')
    provenance.positive(value['run_id']); provenance.positive(value['run_attempt']); a.checked(value['controller_sha'], 40)
    return value


def fetch_commit(local, commit, depth=1):
    a.checked(commit, 40)
    local.run('fetch', '--depth=' + str(depth), '--filter=blob:none', '--no-tags',
              '--no-recurse-submodules', '--no-write-fetch-head', 'origin', commit)


def load_inventory(reader, remote, token, directory, commit):
    # The exact-object downloader creates only its final child directory.
    # Each caller-owned container must exist first; never reuse/replace a
    # foreign directory or symlink, and never broaden the selected blob set.
    directory.mkdir(mode=0o700)
    return engine.load_current(reader, remote, token, directory, commit)


def load_receipt(reader, remote, token, root, commit):
    """Only the exact receipt blob proven at the exact known commit is fetched."""
    row = remote.check_source_budget(commit).get(entry.FULL_RECEIPT)
    if row is None or row[:2] != ('100644', 'blob') or type(row[3]) is not int or not 0 <= row[3] <= entry.MAX_RECEIPT or reader.tree(commit).get(entry.FULL_RECEIPT) != row[:3]:
        fail('runtime_receipt')
    if reader.local.run('cat-file', '--batch-check', data=(row[2] + '\n').encode()).endswith(b' missing\n'):
        values = m.fetch_selected(remote, {entry.FULL_RECEIPT: row}, token, root / ('receipt-' + commit))
        if set(values) != {entry.FULL_RECEIPT} or len(values[entry.FULL_RECEIPT]) != row[3]:
            fail('runtime_receipt')
        m.import_blob(reader.local, values[entry.FULL_RECEIPT], row[2])


def load_trusted_inputs(local, reader, evidence, remote, token, root, parent, client, policy, *, bootstrap, exclude_run_id, exclude_run_attempt):
    # Explicit bounded object-only reads; OfflineGit itself cannot lazy fetch.
    at_stage('parent_history', fetch_commit, local, parent, entry.MAX_INTERVENING + 2)
    at_stage('bootstrap_history', fetch_commit, local, bootstrap['commit'])
    at_stage('bootstrap_inventory', load_inventory, reader, remote, token, root / 'bootstrap', bootstrap['commit'])
    current = at_stage('current_inventory', load_inventory, reader, remote, token, root / 'parent', parent)
    anchor = None
    history_witness = None
    if entry.FULL_RECEIPT in reader.tree(parent):
        verified = at_stage('receipt_provenance', provenance.discover, client, policy, exclude_run_id=exclude_run_id, exclude_run_attempt=exclude_run_attempt, capture_history_witness=True)
        anchor = verified['anchor']; commit = anchor['commit']
        fetch_commit(local, commit, entry.MAX_INTERVENING + 2)
        load_receipt(reader, remote, token, root, commit)
        receipt = entry.read_receipt(reader, commit, anchor['receipt_sha256'], policy['get_policy_sha256'])
        if receipt['sequence'] != verified['receipt_sequence']:
            fail('runtime_receipt')
        before = receipt['parent_commit']; previous = receipt['previous_receipt_commit']
        if provenance.history_witness_required(anchor, receipt):
            history_witness = verified.get('history_witness') or at_stage('receipt_provenance', provenance.verify_history_witness, client, policy, candidate_runs_checked=verified['candidate_runs_checked'])
            fetch_commit(local, history_witness.observed_commit)

        # The independently authenticated receipt binds these exact hashes.
        fetch_commit(local, before, entry.MAX_INTERVENING + 2)
        fetch_commit(local, previous)
        if receipt['sequence'] > 1:
            load_receipt(reader, remote, token, root, previous)
        for target in sorted({before, commit}):
            at_stage('current_inventory', load_inventory, reader, remote, token, root / target, target)
    _, _, states = at_stage('trusted_states', entry.trust_current, reader, evidence, parent, policy['rules_sha256'], anchor, bootstrap=bootstrap, history_witness=history_witness)
    _, legacy, registry = states
    at_stage('legacy_baselines', m.selected_baselines, reader, legacy, parent, remote)
    at_stage('new_baselines', engine.new_baselines, reader, registry, parent, remote)
    preview = None
    if n.STATE not in current:
        _, preview = at_stage('preview_inputs', m.load_canonical_inputs, reader, remote, token, root, parent)
    return anchor, preview, history_witness


def run(token, source, *, policy, approved_policy_sha256, remote=None, public_client=None,
        expected_parent=None, apply=False, scope=None, approved_scope_sha256=None,
        run_context=None, trigger='manual', checkpoint=None, on_publication=None,
        sink=None, writers_guard=None, work=None, bootstrap=entry.BOOTSTRAP):
    """Injected I/O; source/selection/rendering/state/CAS remain shared engine code.

    on_publication receives a validated counts/hash-only marker after readback.
    It runs before an optional private sink. Sink exceptions are a separate
    partial outcome and never erase verified Get success or its receipt.
    """
    policy = at_stage('policy', validate_policy, policy, approved_policy_sha256, bootstrap)
    if trigger not in ('manual', 'schedule'):
        fail('runtime_scope')
    if expected_parent is not None:
        a.checked(expected_parent, 40)
    if apply:
        validate_scope(scope, approved_scope_sha256, policy, approved_policy_sha256)
        run_context = context(policy, run_context)
        if checkpoint is None:
            m.fail('manual_checkpoint')
    elif sink is not None or on_publication is not None:
        fail('runtime_scope')
    remote = remote if remote is not None else m.Remote(None, token)
    client = public_client if public_client is not None else provenance.AnonymousGithub()
    if isinstance(client, provenance._PublicGithub):
        client.private_proof_source = private_proof.PrivateRecords(remote)
    if apply:
        at_stage('execution_provenance', provenance.verify_execution, client, policy, run_context, trigger)
    parent = at_stage('remote_guard', m.guard, remote)
    if expected_parent is not None and parent != expected_parent:
        m.fail('manual_parent_changed')
    with tempfile.TemporaryDirectory(prefix='get-recurring-runtime-', dir=work) as folder:
        root = Path(folder)
        local = at_stage('checkout', r.readback_checkout, root / 'objects', bootstrap['capture_anchor']['capture_commit'], token)
        remote.local = local
        reader, evidence, _ = at_stage('capture_evidence', m.load_evidence, local, remote, token, root, parent, anchor=bootstrap['capture_anchor'])
        anchor, preview, history_witness = at_stage('trusted_inputs', load_trusted_inputs, local, reader, evidence, remote, token, root, parent, client, policy,
            bootstrap=bootstrap, exclude_run_id=run_context['run_id'] if run_context else None,
            exclude_run_attempt=run_context['run_attempt'] if run_context else None)
        at_stage('remote_guard', m.guard, remote, parent)
        plan, files = at_stage('canonical_prepare', entry.prepare, reader, evidence, parent, source, approved_rules_sha256=policy['rules_sha256'],
            anchor=anchor, preview=preview, bootstrap=bootstrap, trigger=trigger, history_witness=history_witness)
        at_stage('remote_guard', m.guard, remote, parent)
        result = at_stage('candidate_report', entry.public_report, plan, files)
        if result['status'] != 'recurring_local_candidate_verified':
            at_stage('candidate_report', fail, 'runtime_failed')
        result.update(status='recurring_candidate_verified', readback_verified=True,
                      production_activated=PRODUCTION_ACTIVATED, sink_status='not_requested')
        if not apply:
            return result
        def readback(commit, values):
            return m.fetch_selected(remote, {p: ('100644', 'blob', r.oid(data), len(data)) for p, data in values.items()}, token, root / 'readback')
        publication = at_stage('publication', m.publish, reader, remote, plan, files, result['plan_sha256'], readback,
            checkpoint=checkpoint, verifier=lambda rd, pl, fs: entry.verify_plan(rd, evidence, pl, fs, policy['rules_sha256'], bootstrap=bootstrap, history_witness=history_witness),
            writers_guard=writers_guard if writers_guard is not None else m.disabled_writers)
        # From this point any local callback failure must preserve actual Get
        # success. The public record contains no note IDs, paths, or source text.
        result.update(publication, status='recurring_transaction_verified')
        try:
            record = provenance.publication_record(reader, evidence, plan, files, publication, policy['rules_sha256'],
                **run_context, bootstrap=bootstrap, history_witness=history_witness)
            handoff = entry.confirmed_private_handoff(reader, evidence, plan, files, publication, policy['rules_sha256'], bootstrap=bootstrap, history_witness=history_witness)
            result['publication_record'] = record
            result['confirmed_content_ids'] = len(handoff['changed_content_ids'])
            # Validate the exact output before crossing even an injected boundary.
            line = provenance.publication_line(record)
            if on_publication is not None:
                on_publication(line)
        except Exception:
            result.update(publication_output='failed', sink_status='not_started')
            return result
        result['publication_output'] = 'confirmed'
        if sink is not None and handoff['changed_content_ids']:
            result['notion_writes'] = None  # Callback owns its independently verified outcome.
            try:
                sink(handoff)
                result['sink_status'] = 'callback_returned'
            except Exception:
                result['sink_status'] = 'partial'
        elif sink is not None:
            result['sink_status'] = 'no_content_change'
        return result


def failure(exc=None, *, attempt_started=False):
    # Missing/malformed output cannot prove that no write happened.
    result = {'status': 'recurring_failed', 'main_updated': None, 'write_state': 'unknown'}
    if exc is None:
        return result
    code = str(exc)
    result['error'] = code if code in ERRORS | m.ERRORS | ENTRY_ERRORS | m.c.ERRORS | CORE_ERRORS else SOURCE_ERRORS.get(code, 'runtime_failed')
    stage = getattr(exc, 'runtime_stage', None)
    if isinstance(stage, str) and stage in STAGES:
        result['stage'] = stage
    result['failure_kind'] = ('missing_key' if isinstance(exc, KeyError) else
        'invalid_type' if isinstance(exc, TypeError) else
        'invalid_value' if isinstance(exc, ValueError) else
        'io_error' if isinstance(exc, OSError) else 'guard_or_runtime')
    state = getattr(exc, 'write_state', 'uncertain' if attempt_started else 'not_attempted')
    if state in ('not_attempted', 'uncertain', 'confirmed'):
        result.update(write_state=state, main_updated=True if state == 'confirmed' else None if state == 'uncertain' else False,
                      cas_attempts=0 if state == 'not_attempted' else 1)
    return result


def write_publication_file(path, line):
    """Controlled current-job handoff only after shared publisher readback."""
    if not isinstance(line, str) or not line.startswith(provenance.MARKER) or len(line.encode()) > 8192:
        fail('runtime_context')
    record = m.strict_capture_json(line[len(provenance.MARKER):].encode(), 8192)
    if provenance.publication_line(record) != line:
        fail('runtime_context')
    target = Path(path).absolute()
    if '..' in target.parts or any(item.is_symlink() for item in (target, *target.parents)):
        fail('runtime_context')
    raw = (line + '\n').encode()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    if a.bounded_file(target, 8193) != raw:
        fail('runtime_context')


def executed_code_sha():
    # Checkout A is independently pinned by exact controller bytes at C.
    root = Path(__file__).resolve().parents[1]
    return a.checked(a.OfflineGit(root).local.run('rev-parse', 'HEAD').decode().strip(), 40)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--approved-policy-sha256', required=True)
    parser.add_argument('--scope', type=Path); parser.add_argument('--scope-from-policy', action='store_true'); parser.add_argument('--approved-scope-sha256')
    parser.add_argument('--apply', action='store_true'); parser.add_argument('--checkpoint', type=Path)
    parser.add_argument('--publication-file', type=Path)
    parser.add_argument('--expected-parent'); parser.add_argument('--trigger', choices=('manual', 'schedule'), default='manual')
    args = parser.parse_args(argv)
    attempt_started = False
    try:
        # Fail closed before any credential lookup or network on pending apply.
        if args.apply and not PRODUCTION_ACTIVATED:
            fail('runtime_activation_pending')
        if args.publication_file and not args.apply:
            fail('runtime_scope')
        policy = validate_policy(m.strict_capture_json(a.bounded_file(args.policy, 16384), 16384), args.approved_policy_sha256, entry.BOOTSTRAP)
        scope = None; run_context = None
        if args.apply:
            if bool(args.scope) == args.scope_from_policy:
                fail('runtime_scope')
            scope = controller.scope_for(policy, args.approved_policy_sha256) if args.scope_from_policy else m.strict_capture_json(a.bounded_file(args.scope, 16384), 16384)
            validate_scope(scope, args.approved_scope_sha256, policy, args.approved_policy_sha256)
            if not args.checkpoint:
                m.fail('manual_checkpoint')
            if os.environ.get('GITHUB_REPOSITORY') != provenance.REPOSITORY or os.environ.get('GITHUB_WORKFLOW_REF') != provenance.REPOSITORY + '/' + policy['workflow_path'] + '@refs/heads/' + policy['head_branch'] or os.environ.get('GITHUB_REF') != 'refs/heads/' + policy['head_branch'] or os.environ.get('GITHUB_EVENT_NAME') != ('schedule' if args.trigger == 'schedule' else 'workflow_dispatch'):
                fail('runtime_context')
            run_context = context(policy, {'run_id': int(os.environ.get('GITHUB_RUN_ID', '0')),
                'run_attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '0')), 'code_sha': executed_code_sha(), 'controller_sha': os.environ.get('GITHUB_SHA')})
        github_token = os.environ.get('GITHUB_TOKEN')
        public_client = provenance.PublicGithub(github_token)
        writers_guard = lambda: m.disabled_writers(token=github_token)
        at_stage('old_writer_guard', writers_guard)  # Same identity; no fallback.
        import sync_getnote_to_raw as sync
        from source_sync_rate import RequestPolicy
        key, client_id = at_stage('credentials', sync.load_credentials)
        source = at_stage('source_transport', m.c.Source, key, client_id, RequestPolicy(seconds=35 * 60))
        def checkpoint(plan):
            nonlocal attempt_started
            m.write_checkpoint(args.checkpoint, plan)
            attempt_started = True
        def emit(line):
            if args.publication_file:
                write_publication_file(args.publication_file, line)
            record = m.strict_capture_json(line[len(provenance.MARKER):].encode(), 8192)
            print(private_proof.line('publication', record), flush=True)
        result = run(os.environ.get('PRIVATE_VAULT_TOKEN'), source, policy=policy,
            approved_policy_sha256=args.approved_policy_sha256, expected_parent=args.expected_parent,
            apply=args.apply, scope=scope, approved_scope_sha256=args.approved_scope_sha256,
            run_context=run_context, trigger=args.trigger, checkpoint=checkpoint if args.apply else None,
            on_publication=emit if args.apply else None, public_client=public_client,
            writers_guard=writers_guard)
    except (Exception, SystemExit) as exc:
        result = failure(exc, attempt_started=attempt_started)
    print(json.dumps(public_result(result)), flush=True)
    # No built-in Notion call/job; an independent sink must not turn a successful
    # Get job into failed provenance. Callback partials are explicitly nonfatal.
    return 0 if result.get('status') in ('recurring_candidate_verified', 'recurring_transaction_verified') and result.get('publication_output') != 'failed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
