"""Carry verified private Get receipt IDs to Notion without losing failed IDs.

The private repository is the sole queue and write-intent store. Run this only
under the shared private-vault writer concurrency group.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

import get_notion_incremental_worker as worker
import get_notion_native as native

RECEIPT = 'Raw/03 Get/03 State/canonical-sync-receipt.json'
ROOT = '.sync/get-notion-incremental'
QUEUE = ROOT + '/queue.json'
CONFIG = ROOT + '/config.json'
OWNED = ROOT + '/owned'
UPDATES = ROOT + '/updates'
SEED = '3760f11239cf72f4f6aac1913d70ddc8ae2253f1'
CONTRACT = 'get-notion-incremental-queue-1'
MAX_CHAIN = 24
MAX_IDS = 5000
HOLD_CODES = frozenset({'source_content_held', 'source_metadata_held',
    'source_properties_held', 'source_too_large', 'existing_content_held',
    'unowned_target', 'manual_edit_or_unknown_write', 'historical_write_held',
    'recording_metadata_held', 'recording_layout_held', 'missing_asset',
    'existing_note_requires_review', 'existing_content_differs', 'existing_properties_differ'})
SAFE_ERRORS = HOLD_CODES | frozenset({'invalid_source', 'source_changed',
    'wrong_repository', 'source_missing_or_duplicate', 'source_unregistered',
    'source_conflict', 'source_path_invalid', 'source_timestamp_invalid',
    'private_state_missing', 'private_state_budget', 'private_state_invalid',
    'receipt_invalid', 'receipt_missing', 'receipt_changed',
    'receipt_chain_invalid', 'receipt_gap_held', 'receipt_seed_invalid',
    'receipt_history_unavailable', 'queue_invalid', 'queue_budget',
    'config_invalid', 'wrong_data_source', 'private_checkout_dirty',
    'private_path_invalid', 'private_index_failed', 'private_commit_unconfirmed',
    'private_head_changed', 'target_unverified', 'permission_denied',
    'read_api_failed', 'write_uncertain', 'request_budget', 'response_budget'})


def stop(reason):
    raise ValueError(reason) from None


def public_reason(exc):
    reason = str(exc)
    return reason if reason in SAFE_ERRORS else 'controller_failed'


def git(root, *args, input=None):
    return subprocess.check_output(['git', '-C', str(root), *args], input=input,
                                   stderr=subprocess.DEVNULL).decode().strip()


def head(root):
    return git(root, 'rev-parse', 'HEAD')


def committed_json(root, path, commit='HEAD', missing=False):
    try:
        raw = subprocess.check_output(['git', '-C', str(root), 'show', commit + ':' + path],
                                      stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        if missing:
            return None
        stop('private_state_missing')
    if len(raw) > 1024 * 1024:
        stop('private_state_budget')
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError):
        stop('private_state_invalid')


def receipt(root, commit):
    if not re.fullmatch('[a-f0-9]{40}', commit):
        stop('receipt_invalid')
    value = committed_json(root, RECEIPT, commit)
    if (not isinstance(value, dict) or value.get('contract') != 'get-canonical-sync-receipt-1'
            or type(value.get('sequence')) is not int or value['sequence'] < 4
            or not isinstance(value.get('previous_receipt_commit'), str)
            or not re.fullmatch('[a-f0-9]{40}', value['previous_receipt_commit'])
            or not isinstance(value.get('changed_content_ids'), list)):
        stop('receipt_invalid')
    ids = value['changed_content_ids']
    if (len(ids) > 50 or ids != sorted(set(ids))
            or any(not isinstance(x, str) or not re.fullmatch('[0-9]{19}', x) for x in ids)):
        stop('receipt_invalid')
    return value


def queue(root):
    value = committed_json(root, QUEUE, missing=True)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'contract', 'receipt_commit', 'sequence', 'pending'}
            or value['contract'] != CONTRACT
            or not isinstance(value['receipt_commit'], str)
            or not re.fullmatch('[a-f0-9]{40}', value['receipt_commit'])
            or type(value['sequence']) is not int or value['sequence'] < 4
            or not isinstance(value['pending'], list) or len(value['pending']) > MAX_IDS
            or len(value['pending']) != len(set(value['pending']))
            or any(not isinstance(x, str) or not re.fullmatch('[0-9]{19}', x) for x in value['pending'])):
        stop('queue_invalid')
    return value


def admission(root, old):
    if git(root, 'rev-parse', '--is-shallow-repository') != 'false':
        stop('receipt_history_unavailable')
    latest = git(root, 'log', '-1', '--format=%H', 'HEAD', '--', RECEIPT)
    if not re.fullmatch('[a-f0-9]{40}', latest):
        stop('receipt_missing')
    if subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:' + RECEIPT],
                               stderr=subprocess.DEVNULL) != subprocess.check_output(
                                   ['git', '-C', str(root), 'show', latest + ':' + RECEIPT],
                                   stderr=subprocess.DEVNULL):
        stop('receipt_changed')
    anchor = old['receipt_commit'] if old else SEED
    if latest == anchor:
        chain = [receipt(root, SEED)] if old is None else []
    else:
        chain = []
        cursor = latest
        for _ in range(MAX_CHAIN):
            row = receipt(root, cursor)
            chain.append((cursor, row))
            previous = row['previous_receipt_commit']
            if previous == anchor:
                break
            try:
                git(root, 'merge-base', '--is-ancestor', previous, cursor)
            except subprocess.CalledProcessError:
                stop('receipt_chain_invalid')
            cursor = previous
        else:
            # shortcut: a gap over 24 receipts needs a separately reviewed replay.
            stop('receipt_gap_held')
        chain.reverse()
    if old is None:
        seed = receipt(root, SEED)
        if seed['sequence'] != 4:
            stop('receipt_seed_invalid')
        previous_sequence = 3
        rows = [(SEED, seed)] if latest == SEED else [(SEED, seed), *chain]
    else:
        previous_sequence = old['sequence']
        rows = chain
    pending = [] if old is None else list(old['pending'])
    for commit, row in rows:
        if row['sequence'] != previous_sequence + 1:
            stop('receipt_chain_invalid')
        previous_sequence = row['sequence']
        for ident in row['changed_content_ids']:
            if ident in pending:
                pending.remove(ident)
            pending.append(ident)
    if len(pending) > MAX_IDS:
        stop('queue_budget')
    return {'contract': CONTRACT, 'receipt_commit': latest,
            'sequence': previous_sequence, 'pending': pending}


def config(root):
    value = committed_json(root, CONFIG)
    if (not isinstance(value, dict) or set(value) != {'contract', 'data_source_id'}
            or value['contract'] != 'get-notion-incremental-config-1'):
        stop('config_invalid')
    ident = native.uuid(value['data_source_id'])
    if native.sha(ident) != native.DATA_SOURCE_PIN:
        stop('wrong_data_source')
    return ident


def save(root, changes, message):
    if not changes:
        return head(root)
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        stop('private_checkout_dirty')
    for path, value in changes.items():
        if not path.startswith(ROOT + '/') or '..' in Path(path).parts:
            stop('private_path_invalid')
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n')
    subprocess.check_call(['git', '-C', str(root), 'add', '--', *changes],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    staged = subprocess.run(['git', '-C', str(root), 'diff', '--cached', '--quiet']).returncode
    if staged == 0:
        return head(root)
    if staged != 1:
        stop('private_index_failed')
    subprocess.check_call(['git', '-C', str(root), 'config', 'user.name', 'github-actions[bot]'])
    subprocess.check_call(['git', '-C', str(root), 'config', 'user.email',
                           '41898282+github-actions[bot]@users.noreply.github.com'])
    subprocess.check_call(['git', '-C', str(root), 'commit', '-m', message],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(['git', '-C', str(root), 'fetch', 'origin', 'main'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(['git', '-C', str(root), 'merge-base', '--is-ancestor', 'origin/main', 'HEAD'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(['git', '-C', str(root), 'push', 'origin', 'HEAD:main'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if git(root, 'ls-remote', 'origin', 'refs/heads/main').split()[0] != head(root):
        stop('private_commit_unconfirmed')
    return head(root)


def run(root, token, limit, apply):
    current = head(root)
    if git(root, 'ls-remote', 'origin', 'refs/heads/main').split()[0] != current:
        stop('private_head_changed')
    source_id = config(root)
    planned = admission(root, queue(root))
    if not apply:
        ready = verified = held = 0
        held_reasons = {}
        for ident in planned['pending'][:limit]:
            try:
                status = worker.run(root, ident, current, source_id, token,
                    owned=committed_json(root, OWNED + '/' + ident + '.json', missing=True),
                    journal=committed_json(root, UPDATES + '/' + ident + '.json', missing=True))['status']
            except ValueError as exc:
                if str(exc) not in HOLD_CODES:
                    raise
                held += 1
                held_reasons[str(exc)] = held_reasons.get(str(exc), 0) + 1
                continue
            if status == 'ready':
                ready += 1
            elif status == 'already_present':
                verified += 1
            elif status == 'update_ready':
                ready += 1
            else:
                stop('target_unverified')
        return {'status': 'dry_run_partial' if held else 'dry_run_ready',
                'ready': ready, 'verified': verified, 'held': held,
                'pending': len(planned['pending']), 'held_reasons': held_reasons}
    save(root, {QUEUE: planned}, 'Queue verified Get changes for Notion')
    completed = 0
    held = 0
    def defer(ident):
        planned['pending'].remove(ident)
        planned['pending'].append(ident)
    for ident in list(planned['pending'])[:limit]:
        current = head(root)
        try:
            owned = committed_json(root, OWNED + '/' + ident + '.json', missing=True)
            journal_path = UPDATES + '/' + ident + '.json'
            journal = committed_json(root, journal_path, missing=True)
            result = worker.run(root, ident, current, source_id, token,
                owned=owned, journal=journal)
            status = result['status']
            if status == 'update_ready':
                def persist_update(value):
                    save(root, {journal_path: value}, 'Reserve or verify one Get to Notion update step')
                result = worker.run(root, ident, head(root), source_id, token,
                    apply=True, owned=owned, journal=journal, persist_update=persist_update)
            if status == 'ready':
                marker_path = worker.ATTEMPTS + '/' + ident + '.json'
                if committed_json(root, marker_path, missing=True) is not None:
                    held += 1
                    defer(ident)
                    continue
                marker = {'note_id': ident, 'source_sha256': result['source_blob'],
                          'run_id': os.environ['GITHUB_RUN_ID'],
                          'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
                          'status': 'attempt_reserved'}
                current = save(root, {marker_path: marker}, 'Reserve one Get to Notion attempt')
                result = worker.run(root, ident, current, source_id, token, apply=True)
            if result['status'] not in ('created', 'already_present', 'updated') or not isinstance(result.get('owned'), dict):
                stop('target_unverified')
            planned['pending'].remove(ident)
            changes = {OWNED + '/' + ident + '.json': result['owned'], QUEUE: planned}
            if result['status'] == 'updated':
                changes[journal_path] = result['journal_complete']
            save(root, changes,
                 'Confirm one Get note in Notion')
            completed += 1
        except ValueError as exc:
            if str(exc) not in HOLD_CODES:
                raise
            held += 1
            defer(ident)
    if held:
        save(root, {QUEUE: planned}, 'Rotate held Get notes for Notion')
    return {'status': 'partial' if planned['pending'] else 'complete', 'completed': completed,
            'held': held, 'pending': len(planned['pending'])}


def adopt_one(root, token, ident, apply):
    """Adopt one exact legacy match without writing to Notion or touching the queue."""
    if not isinstance(ident, str) or not re.fullmatch('[0-9]{19}', ident):
        stop('invalid_source')
    current = head(root)
    if git(root, 'ls-remote', 'origin', 'refs/heads/main').split()[0] != current:
        stop('private_head_changed')
    source_id = config(root)
    path = OWNED + '/' + ident + '.json'
    legacy = committed_json(root, path, missing=True)
    if committed_json(root, UPDATES + '/' + ident + '.json', missing=True) is not None:
        stop('unowned_target')
    candidate = worker.adopt(root, ident, current, source_id, token, legacy)
    if not apply:
        return {'status': 'adoption_ready'}
    save(root, {path: candidate}, 'Adopt one exact Get page baseline for Notion updates')
    observed = worker.run(root, ident, head(root), source_id, token, owned=candidate)
    if observed['status'] != 'already_present' or observed['owned'] != candidate:
        stop('target_unverified')
    return {'status': 'adopted'}


def note_from_attempt_commit(root, commit):
    """Select one private note without putting its ID in a public workflow input."""
    if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit):
        stop('unowned_target')
    try:
        git(root, 'merge-base', '--is-ancestor', commit, 'HEAD')
        paths = git(root, 'diff-tree', '--no-commit-id', '--name-only', '-r', commit).splitlines()
    except subprocess.CalledProcessError:
        stop('unowned_target')
    if len(paths) != 1 or not paths[0].startswith(worker.ATTEMPTS + '/'):
        stop('unowned_target')
    path = paths[0]
    ident = Path(path).stem
    if path != worker.ATTEMPTS + '/' + ident + '.json' or not re.fullmatch('[0-9]{19}', ident):
        stop('unowned_target')
    try:
        git(root, 'cat-file', '-e', commit + '^:' + path)
    except subprocess.CalledProcessError:
        pass
    else:
        stop('unowned_target')
    marker = committed_json(root, path, commit)
    if not isinstance(marker, dict) or marker.get('note_id') != ident:
        stop('unowned_target')
    return ident


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--max-notes', type=int, default=50)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--adopt-attempt-commit')
    args = parser.parse_args()
    if not 1 <= args.max_notes <= 50:
        stop('invalid_batch')
    try:
        result = (adopt_one(args.vault, os.environ['NOTION_API_KEY'],
                            note_from_attempt_commit(args.vault, args.adopt_attempt_commit), args.apply)
                  if args.adopt_attempt_commit else
                  run(args.vault, os.environ['NOTION_API_KEY'], args.max_notes, args.apply))
    except ValueError as exc:
        raise SystemExit(public_reason(exc)) from None
    except Exception:
        raise SystemExit('controller_failed') from None
    print(json.dumps(result))
    if result['status'] in ('partial', 'dry_run_partial'):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
