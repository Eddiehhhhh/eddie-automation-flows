"""Read a version-pinned, private opt-in for bounded hourly history recovery."""
import base64
from collections import Counter
import hashlib
import json
import re
from urllib.parse import quote, urlencode

from assets import asset_references
from core import Hold, requires_binary_assets, snapshot
from retry import HTTPFailure, RetryStopped, TransportFailure

SCHEMA = 'get-reviewed-hourly-history-v1'
LIMIT = 5
RUNTIME_SECONDS = 30 * 60
# Leave enough time before cut-off for a bounded network request, checkpoint and release.
RESERVE_SECONDS = 180
RATE_LIMIT_RESERVE = 1500
SOURCE_ID = re.compile(r'[0-9]{1,40}')
SHA1 = re.compile(r'[a-f0-9]{40}')
SHA256 = re.compile(r'[a-f0-9]{64}')


def clock_parent(env):
    if (env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or env.get('GITHUB_REF') != 'refs/heads/main'
            or env.get('GITHUB_ACTOR') != 'github-actions[bot]'
            or env.get('GITHUB_TRIGGERING_ACTOR') != 'github-actions[bot]'
            or env.get('GITHUB_RUN_ATTEMPT') != '1'):
        return None
    key = env.get('GET_HOURLY_DISPATCH_KEY', '')
    match = re.fullmatch(r'clock-([1-9][0-9]{0,24})', key)
    return match[1] if match else None


def plan_at_head(http, cfg):
    prefix = '/repos/' + cfg['private_repo']
    metadata = http.request('GET', prefix)
    if not metadata or metadata.get('private') is not True or metadata.get('default_branch') != 'main':
        raise Hold('history_plan_private_main_required')
    ref = http.request('GET', prefix + '/git/ref/heads/main')
    head = (ref or {}).get('object', {}).get('sha', '')
    if not SHA1.fullmatch(head):
        raise Hold('history_plan_invalid_head')
    path = 'get-direct/' + cfg['namespace'] + '/reviewed-backfill.json'
    item = http.request('GET', prefix + '/contents/' + path + '?ref=' + head)
    if item is None:
        return None
    if (item.get('type') != 'file' or item.get('path') != path or item.get('encoding') != 'base64'
            or type(item.get('size')) is not int or not 0 <= item['size'] <= 800000):
        raise Hold('history_plan_invalid')
    raw = base64.b64decode(item['content'].replace('\n', ''), validate=True)
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if len(raw) != item['size'] or item.get('sha') != blob:
        raise Hold('history_plan_invalid')
    plan = json.loads(raw)
    if (not isinstance(plan, dict) or set(plan) != {'schema', 'namespace', 'excluded_source_ids', 'reviewed'}
            or plan['schema'] != SCHEMA or plan['namespace'] != cfg['namespace']
            or not isinstance(plan['excluded_source_ids'], list) or len(plan['excluded_source_ids']) > 10000
            or any(not isinstance(sid, str) or not SOURCE_ID.fullmatch(sid) for sid in plan['excluded_source_ids'])
            or not isinstance(plan['reviewed'], list) or len(plan['reviewed']) > 1000):
        raise Hold('history_plan_invalid')
    seen = set()
    for row in plan['reviewed']:
        if (not isinstance(row, dict) or set(row) != {'source_id', 'source_version', 'path', 'local_blob', 'reviewed_revision'}
                or any(not isinstance(value, str) for value in row.values())):
            raise Hold('history_plan_invalid')
        sid, version, old_path = row['source_id'], row['source_version'], row['path']
        if (not SOURCE_ID.fullmatch(sid) or sid in seen or not SHA256.fullmatch(version)
                or not SHA1.fullmatch(row['local_blob'])
                or not old_path.startswith('Raw/03 Get/') or not old_path.endswith('.md')
                or '\\' in old_path or any(ord(c) < 32 for c in old_path)
                or any(part in ('', '.', '..') for part in old_path.split('/'))
                or row['reviewed_revision'] != hashlib.sha256((sid + ':' + version).encode()).hexdigest()):
            raise Hold('history_plan_invalid')
        seen.add(sid)
    return blob, plan


def check_candidate(row, plan_blob, cfg, get, github, notion):
    sid, version = row['source_id'], row['source_version']
    original = get.request('GET', '/open/api/v1/resource/note/detail?' +
                           urlencode({'id': sid, 'image_quality': 'original'}))
    if original is None:
        return None, 'source_missing'
    if original.get('success') is not True:
        raise Hold('history_source_unavailable')
    try:
        snap = snapshot(original.get('data', {}).get('note', {}))
        if (snap['id'] != sid or snap['version'] != version or snap['note'].get('note_type') != 'plain_text'
                or not snap['note']['content'].strip() or requires_binary_assets(snap) or asset_references(snap['note'])):
            return None, 'source_changed'
    except Hold:
        return None, 'source_changed'
    prefix = '/repos/' + cfg['private_repo']
    ref = github.request('GET', prefix + '/git/ref/heads/main')
    head = (ref or {}).get('object', {}).get('sha', '')
    if not SHA1.fullmatch(head):
        raise Hold('history_plan_invalid_head')
    current_plan = github.request('GET', prefix + '/contents/get-direct/' + cfg['namespace'] + '/reviewed-backfill.json?ref=' + head)
    if not current_plan or current_plan.get('sha') != plan_blob:
        raise Hold('history_plan_changed')
    path = row['path']
    legacy = github.request('GET', prefix + '/contents/' + quote(path, safe='/') + '?ref=' + head)
    if (not legacy or legacy.get('type') != 'file' or legacy.get('path') != path
            or legacy.get('sha') != row['local_blob'] or legacy.get('encoding') != 'base64'
            or type(legacy.get('size')) is not int or not 0 < legacy['size'] <= 500000):
        return None, 'legacy_changed'
    data = base64.b64decode(legacy['content'].replace('\n', ''), validate=True)
    if (hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != row['local_blob']
            or snap['note']['content'].strip().encode() not in data.partition(b'\n---\n')[2]):
        return None, 'legacy_body_changed'
    # Refuse any existing page. Runtime Notion ownership checks also protect the write-after-query race.
    if notion.pages(sid):
        return None, 'notion_exists'
    return snap, None


def rest_budget_available(http):
    try:
        limit = http.request('GET', '/rate_limit')
        remaining = limit['resources']['core']['remaining']
    except (Hold, HTTPFailure, RetryStopped, TransportFailure, KeyError, TypeError):
        return False
    return type(remaining) is int and remaining >= RATE_LIMIT_RESERVE


def should_resume_migration(report):
    if report.get('phase') == 'sync':
        return report.get('pending') == 0 and report.get('resume_history_from_clock') is True
    history = report.get('reviewed_history') or {}
    return (report.get('phase') == 'history' and report.get('pending') == 0
            and not history.get('deferred') and history.get('remaining_approved', 0) > 0
            and (report.get('notion', {}).get('verified', 0) > 0 or bool(history.get('newly_suspended'))))


def candidates(plan, cfg, store, get, github, notion, paused_ids, report, available, *, check_limit=LIMIT):
    blob, value = plan
    key = 'reviewed_hourly_history'
    previous = store.get(key) or {}
    skipped = dict(previous.get('skipped', {})) if previous.get('plan_blob') == blob else {}
    excluded = set(value['excluded_source_ids']) | set(paused_ids)
    inventory = dict(store.db.execute('SELECT id,version FROM inventory'))
    delivered = set(store.db.execute('SELECT id,version FROM delivered'))
    report.update(approved=len(value['reviewed']), checked=0, enqueued=0, suspended=len(skipped))
    recorded = Counter()
    for row in value['reviewed']:
        sid, version, rev = row['source_id'], row['source_version'], row['reviewed_revision']
        if ((sid, version) in delivered or rev in skipped or
                all((store.receipt(sink, sid) or {}).get('version') == version for sink in ('github', 'notion'))):
            continue
        if not available() or report['checked'] >= check_limit:
            break
        if not rest_budget_available(github):
            report['deferred'] = 'rest_budget'
            break
        report['checked'] += 1
        if sid in excluded or (store.get('source_hold:' + sid) or {}).get('status') == 'held':
            snap, reason = None, 'paused'
        elif inventory.get(sid) != version:
            snap, reason = None, 'inventory_changed'
        else:
            try:
                snap, reason = check_candidate(row, blob, cfg, get, github, notion)
            except (Hold, HTTPFailure, RetryStopped, TransportFailure, KeyError, ValueError, TypeError):
                report['deferred'] = 'precheck_unavailable'
                break
        if reason is not None:
            skipped[rev] = reason
            recorded[reason] += 1
        elif not available():
            break
        else:
            yield snap
            if store.pending_count():
                break
    if recorded:
        store.set(key, {'plan_blob': blob, 'skipped': skipped})
        report['suspended'] = len(skipped)
        report['newly_suspended'] = dict(recorded)
    report['remaining_approved'] = sum(
        row['reviewed_revision'] not in skipped
        and not all((store.receipt(sink, row['source_id']) or {}).get('version') == row['source_version']
                    for sink in ('github', 'notion'))
        for row in value['reviewed']
    )


def verify_clock(http, runtime_repo, run_id):
    if not isinstance(runtime_repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', runtime_repo):
        return False
    row = http.request('GET', '/repos/' + runtime_repo + '/actions/runs/' + run_id)
    if not isinstance(row, dict):
        return False
    return (str(row.get('id')) == run_id and row.get('path') == '.github/workflows/get-cloud-clock.yml'
            and row.get('head_branch') == 'main' and row.get('event') == 'workflow_dispatch'
            and row.get('status') in ('in_progress', 'completed')
            and (row.get('repository') or {}).get('full_name') == runtime_repo
            and (row.get('head_repository') or {}).get('full_name') == runtime_repo
            and (row.get('head_repository') or {}).get('fork') is False)
