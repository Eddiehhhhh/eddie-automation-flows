"""Read-only attestation for one historical incremental Get page.

No page content, token, ID, URL, response body, or private digest is printed.
This probe cannot promote ownership; it only supplies an exact migration gate.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

import get_notion_incremental_controller as controller
import get_notion_incremental_worker as worker
import get_notion_live_transport as transport
import get_notion_native as native
from get_mirror_incremental_replay import strict_capture_json

RUN_ID = 42267203139
RUN_ATTEMPT = 1
WORKFLOW_ID = 409214823
RUN_HEAD_SHA = 'cf164aaf7fb1baf56af8d3f74948bc41b66df489'
ATTEMPT_COMMIT = 'ce806d720381a91bcc94741abfe0e7a6a7a2c21f'
OWNED_COMMIT = '41b3bb86f355f7de0c1e59a65f7321005dec0ce1'
REPOSITORY = 'Eddiehhhhh/eddie-automation-flows'
PHASE = 'start'


def mark(phase):
    global PHASE
    PHASE = phase


def stop(code):
    raise ValueError(code) from None


def time_of(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).timestamp()
    except (AttributeError, ValueError):
        stop('time_unverified')


def fixed_read(url, token=None, *, github=False):
    expected = ('https://api.github.com/repos/' + REPOSITORY + '/actions/runs/' + str(RUN_ID)
                if github else 'https://api.notion.com/v1/users/me')
    if url != expected:
        stop('read_unverified')
    if not github and (not isinstance(token, str) or not token or any(ord(c) <= 32 for c in token)):
        stop('credential_unavailable')
    headers = {'Accept': 'application/vnd.github+json' if github else 'application/json',
               'User-Agent': 'get-origin-readonly-probe'}
    if not github:
        headers['Authorization'] = 'Bearer ' + token
        headers['Notion-Version'] = transport.VERSION
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         transport.ro.NoRedirect()).open(request, timeout=30) as response:
            if response.status != 200:
                stop('read_unverified')
            raw = response.read(65537)
        if len(raw) > 65536:
            stop('read_unverified')
        value = strict_capture_json(raw, 65536)
        if not isinstance(value, dict):
            stop('read_unverified')
        return value
    except ValueError:
        raise
    except Exception:
        stop('read_unverified')


def introduced_unchanged(root, path, commit):
    newest = worker.git(root, 'log', '-1', '--format=%H', 'HEAD', '--', path).decode().strip()
    if newest != commit:
        stop('history_unverified')
    original = worker.git(root, 'show', commit + ':' + path)
    if worker.git(root, 'show', 'HEAD:' + path) != original:
        stop('history_unverified')
    try:
        worker.git(root, 'cat-file', '-e', commit + '^:' + path)
    except subprocess.CalledProcessError:
        return
    stop('history_unverified')


def assess(root, notion_token):
    mark('private_head')
    head = controller.head(root)
    if controller.git(root, 'ls-remote', 'origin', 'refs/heads/main').split()[0] != head:
        stop('source_changed')
    mark('receipt_history')
    paths = worker.git(root, 'diff-tree', '--no-commit-id', '--name-only', '-r',
                       ATTEMPT_COMMIT).decode().splitlines()
    if len(paths) != 1 or not paths[0].startswith(worker.ATTEMPTS + '/'):
        stop('history_unverified')
    attempt_path = paths[0]
    note_id = Path(attempt_path).stem
    if attempt_path != worker.ATTEMPTS + '/' + note_id + '.json' or not re.fullmatch(r'[0-9]{10,}', note_id):
        stop('history_unverified')
    owned_path = controller.OWNED + '/' + note_id + '.json'
    introduced_unchanged(root, attempt_path, ATTEMPT_COMMIT)
    introduced_unchanged(root, owned_path, OWNED_COMMIT)
    worker.git(root, 'merge-base', '--is-ancestor', ATTEMPT_COMMIT, OWNED_COMMIT)
    worker.git(root, 'merge-base', '--is-ancestor', OWNED_COMMIT, head)
    attempt = controller.committed_json(root, attempt_path)
    owned = controller.committed_json(root, owned_path)
    if (not isinstance(attempt, dict) or set(attempt) != {'note_id', 'source_sha256',
            'run_id', 'run_attempt', 'status'} or attempt.get('note_id') != note_id
            or attempt.get('run_id') != str(RUN_ID) or attempt.get('run_attempt') != str(RUN_ATTEMPT)
            or attempt.get('status') != 'attempt_reserved'):
        stop('attempt_unverified')
    if (not isinstance(owned, dict) or set(owned) != {'note_id', 'page_id',
            'source_sha256', 'target_sha256'} or owned.get('note_id') != note_id
            or owned.get('source_sha256') != attempt['source_sha256']
            or any(not isinstance(owned.get(key), str) or re.fullmatch('[a-f0-9]{64}', owned[key]) is None
                   for key in ('source_sha256', 'target_sha256'))):
        stop('owned_unverified')
    page_id = native.uuid(owned['page_id'])
    mark('github_run')
    run = fixed_read('https://api.github.com/repos/' + REPOSITORY + '/actions/runs/' + str(RUN_ID),
                     github=True)
    if (run.get('id') != RUN_ID or run.get('run_attempt') != RUN_ATTEMPT
            or run.get('workflow_id') != WORKFLOW_ID or run.get('event') != 'workflow_dispatch'
            or run.get('status') != 'completed' or run.get('conclusion') != 'failure'
            or run.get('head_sha') != RUN_HEAD_SHA
            or any(not isinstance(run.get(key), dict) or run[key].get('full_name') != REPOSITORY
                   or run[key].get('private') is not False
                   for key in ('repository', 'head_repository'))):
        stop('run_unverified')
    reserved = int(worker.git(root, 'show', '-s', '--format=%ct', ATTEMPT_COMMIT))
    confirmed = int(worker.git(root, 'show', '-s', '--format=%ct', OWNED_COMMIT))
    if not reserved <= confirmed or confirmed - reserved > 60:
        stop('time_unverified')
    if not time_of(run.get('created_at')) <= reserved <= confirmed <= time_of(run.get('updated_at')):
        stop('time_unverified')
    mark('source_note')
    path, data, row = worker.source(root, note_id, head)
    if hashlib.sha256(data).hexdigest() != owned['source_sha256']:
        stop('source_changed')
    mark('notion_target')
    source_id = controller.config(root)
    api = transport.API(transport.Transport(notion_token), source_id, [note_id])
    props, blocks = worker.desired(data, note_id, path, row, api.schema())
    if api.matches(note_id) != [page_id]:
        stop('target_unverified')
    if worker.verify_note(api, note_id, page_id, props, blocks) != owned['target_sha256']:
        stop('target_unverified')
    mark('notion_creator')
    raw_page = api.request('GET', '/pages/' + page_id, None)
    bot = fixed_read('https://api.notion.com/v1/users/me', notion_token)
    creator = raw_page.get('created_by')
    if (raw_page.get('object') != 'page' or native.uuid(raw_page.get('id')) != page_id
            or raw_page.get('archived') is True or raw_page.get('in_trash') is True
            or native.uuid(raw_page.get('parent', {}).get('data_source_id')) != source_id
            or not isinstance(creator, dict) or creator.get('object') != 'user'
            or bot.get('object') != 'user' or bot.get('type') != 'bot'
            or creator.get('id') != bot.get('id')):
        stop('creator_unverified')
    created = time_of(raw_page.get('created_time'))
    edited = time_of(raw_page.get('last_edited_time'))
    if not reserved <= created <= edited <= confirmed:
        stop('time_unverified')
    mark('final_readback')
    if api.matches(note_id) != [page_id] or controller.head(root) != head:
        stop('concurrent_change')
    if controller.git(root, 'ls-remote', 'origin', 'refs/heads/main').split()[0] != head:
        stop('concurrent_change')
    return {'status': 'origin_proof_ready'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vault', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = assess(args.vault, os.environ.get('NOTION_API_KEY'))
    except Exception as exc:
        allowed = {'time_unverified', 'read_unverified', 'credential_unavailable',
                   'history_unverified', 'source_changed', 'attempt_unverified',
                   'owned_unverified', 'run_unverified', 'target_unverified',
                   'creator_unverified', 'concurrent_change'}
        code = str(exc) if isinstance(exc, ValueError) and str(exc) in allowed else 'probe_failed'
        result = {'status': 'origin_proof_blocked', 'reason': code, 'phase': PHASE}
    print(json.dumps(result, separators=(',', ':')))
    return 0 if result['status'] == 'origin_proof_ready' else 2


if __name__ == '__main__':
    raise SystemExit(main())
