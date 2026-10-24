"""Fixed two-page native Notion observation and committed-MD preview only.

Existing runtime secrets are consumed only by fixed-host read transports.
All target blocks/source bodies stay in memory. There is no apply option,
write endpoint, ref update, asset download, artifact or ownership adoption.
"""
import get_private_runtime_bindings as _private_bindings
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

import get_notion_native as native
import get_notion_initial_range as historical
import get_notion_sink_plan as planner
import get_legacy_capture as capture
import get_private_preview_retention as git
from get_legacy_manual import ANCHOR, CAPTURE
from get_mirror_incremental_replay import strict_capture_json, validate_legacy_census

NOTION_VERSION = '2025-09-03'
MAX_RESPONSE = 2 * 1024 * 1024
MAX_HTTP_BYTES = 64 * 1024 * 1024
PAGE_DIFFERENCES={'identity','parent','properties','edit_metadata','creation_metadata','lifecycle','presentation','location','other_page_field','malformed_envelope'}
STAGES = {'input', 'native_read', 'source_read', 'planner', 'historical_range', 'final_readback'}
REASONS = {'permission_denied', 'missing_credentials', 'invalid_read_route',
    'read_transport_failed', 'response_budget', 'read_api_failed', 'request_budget',
    'source_changed', 'source_anchor', 'source_missing', 'planner_failed', 'other',
    'block_budget', 'depth_budget', 'duplicate_or_missing_id', 'duplicate_search_identity',
    'id_property_invalid', 'identity_query_invalid', 'incomplete_children', 'incomplete_pagination',
    'incomplete_properties', 'incomplete_target_read', 'invalid_block_tree', 'malformed_identity',
    'malformed_response', 'page_changed_during_read', 'pagination_budget', 'target_not_visible',
    'unsupported_block', 'unsupported_parent_api_version', 'wrong_block_parent', 'wrong_database',
    'wrong_note_identity', 'wrong_page', 'wrong_parent', 'wrong_schema'}
PLAN_REASONS = {'uncommitted_source', 'dry_run_source', 'incomplete_source', 'id_not_string',
    'unmapped_id', 'missing_target', 'duplicate_id', 'wrong_parent', 'incomplete_target_read',
    'missing_block_ownership', 'human_edit_conflict', 'missing_asset', 'privacy_hold',
    'format_not_accepted', 'writer_gate_unknown', 'authorization_missing', 'unsupported_source_format'}


HISTORICAL_TEMPLATE = _private_bindings.value('get_notion_readonly', 'HISTORICAL_TEMPLATE')
HISTORICAL_TEMPLATE_SHA = _private_bindings.value('get_notion_readonly', 'HISTORICAL_TEMPLATE_SHA')


class ReadFailure(ValueError):
    def __init__(self, reason, status=None):
        super().__init__(reason if reason in REASONS else 'other')
        self.status = status


def fail(reason, status=None):
    raise ReadFailure(reason, status) from None


def read_route(method, path):
    return ((method == 'POST' and path == '/search') or
        (method == 'POST' and re.fullmatch(r'/data_sources/[0-9a-f]{32}/query', path)) or
        (method == 'GET' and re.fullmatch(r'/(pages|data_sources|blocks)/[0-9a-f]{32}', path)) or
        (method == 'GET' and re.fullmatch(r'/blocks/[0-9a-f]{32}/children\?page_size=100(?:&start_cursor=[A-Za-z0-9%+_.~-]+)?', path)))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        fail('invalid_read_route')


class NotionReadTransport:
    """Only idempotent reads/queries, bounded retries; 401/403 never retry."""
    def __init__(self, token, *, opener=None, sleep=time.sleep, clock=time.monotonic):
        if not isinstance(token, str) or not token or any(ord(c) <= 32 for c in token):
            fail('missing_credentials')
        self.token = token
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.sleep = sleep; self.clock = clock; self.deadline = clock() + 20 * 60
        self.last = None; self.attempts = 0; self.retries = 0; self.bytes = 0

    def __call__(self, method, path, body):
        if not isinstance(path, str) or not read_route(method, path): fail('invalid_read_route')
        data = None if body is None else planner.encoded(body)
        if data is not None and len(data) > 16 * 1024: fail('request_budget')
        for attempt in range(3):
            if self.attempts >= 2000 or self.clock() >= self.deadline: fail('request_budget')
            if self.last is not None:
                self.sleep(max(0, .4 - (self.clock() - self.last)))
            self.last = self.clock(); self.attempts += 1
            request = urllib.request.Request('https://api.notion.com/v1' + path, data=data, method=method,
                headers={'Authorization': 'Bearer ' + self.token, 'Notion-Version': NOTION_VERSION,
                         'Content-Type': 'application/json', 'User-Agent': 'get-notion-read-only'})
            try:
                with self.opener.open(request, timeout=30) as response:
                    if response.status != 200: fail('read_api_failed')
                    value = response.read(MAX_RESPONSE + 1)
                self.bytes += len(value)
                if len(value) > MAX_RESPONSE or self.bytes > MAX_HTTP_BYTES: fail('response_budget')
                return strict_capture_json(value, MAX_RESPONSE)
            except urllib.error.HTTPError as error:
                status = error.code; retry_after = error.headers.get('Retry-After', '') if error.headers else ''
                error.close()
                if status in (401, 403): fail('permission_denied', status)
                if status not in (429, 500, 502, 503, 504) or attempt == 2: fail('read_api_failed', status)
                wait = float(retry_after) if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', retry_after) else 2 ** attempt
                self.retries += 1; self.sleep(min(wait, 60))
            except ReadFailure:
                raise
            except (TimeoutError, urllib.error.URLError):
                if attempt == 2: fail('read_transport_failed')
                self.retries += 1; self.sleep(2 ** attempt)
            except Exception:
                fail('read_transport_failed')
        fail('read_transport_failed')


def source_guard(remote, parent):
    metadata = remote.metadata()
    if metadata.get('full_name') != git.REPOSITORY or metadata.get('private') is not True:
        fail('source_anchor')
    if remote.ref(git.MAIN_REF) != parent: fail('source_changed')


def read_committed_sources(remote, parent, snapshots):
    """Read one hash-pinned census and only the two selected committed MD blobs."""
    source_guard(remote, parent)
    commit = remote.commit(parent)
    if len(commit.get('parents', [])) < 1: fail('source_anchor')
    metadata = capture.scope_metadata(remote, CAPTURE)
    row = metadata.get(capture.CENSUS)
    if not row or row[:2] != ('100644', 'blob') or type(row[3]) is not int or not 0 <= row[3] <= 16 * 1024 * 1024:
        fail('source_anchor')
    data = remote.blob(row[2], row[3])
    if git.oid(data) != row[2] or planner.digest(data) != ANCHOR['census_sha256']: fail('source_anchor')
    census = strict_capture_json(data, 16 * 1024 * 1024); validate_legacy_census(census)
    current = remote.check_source_budget(parent)
    mapping = {}; blobs = {}; hashes = {}; complete = {}; paths = {}; sizes = 0
    for ident, snapshot in sorted(snapshots.items()):
        entry = census['eligible'].get(ident)
        if not isinstance(entry, dict): fail('source_missing')
        path = planner.safe_path(git.GET + '/' + entry['filePath']); row = current.get(path)
        if not row or row[:2] != ('100644', 'blob') or type(row[3]) is not int or not 0 <= row[3] <= planner.MAX_NOTE: fail('source_missing')
        value = remote.blob(row[2], row[3]); sizes += len(value)
        if len(value) != row[3] or git.oid(value) != row[2] or sizes > 2 * planner.MAX_NOTE: fail('source_anchor')
        planner.source_body(value, ident)
        paths[path] = row[2]; blobs[row[2]] = value; hashes[path] = planner.digest(value); complete[ident] = True
        mapping[ident] = {'path': path, 'md_blob': row[2], 'page_id': snapshot['page_id'], 'data_source_id': snapshot['parent_data_source']}
    source_guard(remote, parent)
    batch = {'repository': git.REPOSITORY, 'commit': parent, 'parent': commit['parents'][0],
        'publication_status': 'published', 'remote_readback_verified': True,
        'changed_content_ids': [], 'content_tree': paths, 'content_sha256': hashes, 'content_complete': complete}
    return batch, mapping, blobs


def historical_ranges(remote,mapping,blobs,snapshots,template=HISTORICAL_TEMPLATE,expected_template_sha=HISTORICAL_TEMPLATE_SHA):
    """One independently reviewed historical proof; observations grant no ownership."""
    out={'proofs':0,'candidates':0,'blocked':0,'unproven_notes':len(mapping),'managed_blocks':0,'protected_blocks':0,'reason_counts':{},'diagnostics':[]}
    if planner.digest(template)!=expected_template_sha:fail('source_anchor')
    for ident,row in mapping.items():
        if planner.digest(ident)!=template['note_id_sha256']:continue
        snapshot=snapshots[ident]
        proof=historical.reconstruct_proof(template,expected_template_sha,note_id=ident,page_id=snapshot['page_id'],source_path=row['path'])
        # Check both current source (already read/hash-bound) and the exact old
        # Git path/blob membership. No old content or assets are downloaded.
        prior=remote.check_source_budget(proof['source']['commit']).get(row['path'])
        if prior is None or prior[:3]!=('100644','blob',proof['source']['blob']) or row['md_blob']!=proof['source']['blob']:fail('source_anchor')
        result=historical.propose_from_git(proof,template['proof_sha256'],blobs[row['md_blob']],snapshot,row['data_source_id'])
        diagnostic=historical.diagnose_from_git(proof,template['proof_sha256'],blobs[row['md_blob']],snapshot,row['data_source_id'])
        if not historical.valid_diagnostic(diagnostic):fail('planner_failed')
        out['diagnostics'].append(diagnostic)
        safe=historical.public_summary(result);out['proofs']+=1;out['unproven_notes']-=1
        if safe['decision']=='candidate':out['candidates']+=1
        else:
            out['blocked']+=1;reason=safe['reason'];out['reason_counts'][reason]=out['reason_counts'].get(reason,0)+1
        out['managed_blocks']+=safe['managed_blocks'];out['protected_blocks']+=safe['protected_blocks']
    return out


def valid_historical(value):
    keys={'proofs','candidates','blocked','unproven_notes','managed_blocks','protected_blocks','reason_counts','diagnostics'}
    if not isinstance(value,dict) or set(value)!=keys:return False
    for key in keys-{'reason_counts','diagnostics'}:
        cap=2*planner.MAX_BLOCKS if key in ('managed_blocks','protected_blocks') else 2
        if type(value[key]) is not int or not 0<=value[key]<=cap:return False
    diagnostics=value['diagnostics']
    if not isinstance(diagnostics,list) or len(diagnostics)!=value['proofs'] or any(not historical.valid_diagnostic(x) for x in diagnostics):return False
    reasons=value['reason_counts']
    return isinstance(reasons,dict) and not set(reasons)-historical.REASONS and all(type(n) is int and 0<=n<=2 for n in reasons.values()) and sum(reasons.values())==value['blocked'] and value['candidates']+value['blocked']==value['proofs'] and value['proofs']+value['unproven_notes']==2


def run_readonly(remote, request, parent, *, collector=native.collect):
    stage = 'input'
    try:
        git.checked_sha(parent)
        stage = 'native_read'; observation = collector(request)
        snapshots = observation['snapshots']
        if not isinstance(snapshots, dict) or len(snapshots) != 2: fail('planner_failed')
        stage = 'source_read'; batch, mapping, blobs = read_committed_sources(remote, parent, snapshots)
        policy = {'repository': git.REPOSITORY, 'data_source_id': next(iter(snapshots.values()))['parent_data_source'],
            'allowlist': sorted(snapshots), 'read_only': True, 'allow_create': False, 'allow_property_updates': False,
            'allow_deletes': False, 'format_accepted': False, 'expected_source_commit': parent,
            'ownership_sha256': {}, 'initial_two_preview': {'source_commit': parent, 'ids': sorted(snapshots), 'authorized_read_only': True}}
        stage = 'planner'
        # Observation cannot manufacture ownership. Assets remain unresolved;
        # no source URL is fetched, and no current snapshot becomes a receipt.
        plan = planner.plan_sink(batch, mapping, blobs, {}, snapshots, {}, policy)
        if len(plan.get('records', [])) != 2 or plan.get('source_mode') != 'initial_two_preview': fail('planner_failed')
        reasons = Counter(reason for row in plan['records'] for reason in row['reason_codes'])
        if set(reasons) - PLAN_REASONS: fail('planner_failed')
        stage = 'historical_range'; ranges=historical_ranges(remote,mapping,blobs,snapshots)
        stage = 'final_readback'; source_guard(remote, parent)
        return {'status': 'notion_readonly_verified', 'native_pages': len(snapshots),
            'native_blocks': observation['report']['blocks'], 'native_requests': observation['report']['requests'],
            'source_notes_read': len(mapping), 'source_commit': parent, 'planner': planner.public_summary(plan),
            'reason_counts': dict(reasons), 'historical_range':ranges, 'external_writes': 0, 'get_requests': 0, 'asset_downloads': 0,
            'ownership_adopted': False, 'receipt_advance': False, 'main_updated': False}
    except Exception as exc:
        reason = str(exc)
        difference=getattr(exc,'difference_categories',())
        safe_difference=sorted(set(difference)) if isinstance(difference,(list,tuple)) and all(isinstance(x,str) and x in PAGE_DIFFERENCES for x in difference) else []
        return {'status': 'notion_readonly_failed', 'stage': stage,
            'reason': reason if reason in REASONS else 'other', 'page_difference_categories':safe_difference, 'external_writes': 0,
            'get_requests': 0, 'asset_downloads': 0, 'ownership_adopted': False, 'receipt_advance': False, 'main_updated': False}


def public_report(value):
    failed = {'status': 'notion_readonly_failed', 'external_writes': 0, 'apply_ready': False}
    if not isinstance(value, dict): return failed
    if value.get('status') == 'notion_readonly_failed':
        if isinstance(value.get('stage'), str) and value['stage'] in STAGES: failed['stage'] = value['stage']
        if isinstance(value.get('reason'), str) and value['reason'] in REASONS: failed['reason'] = value['reason']
        differences=value.get('page_difference_categories',[])
        if isinstance(differences,list) and all(isinstance(x,str) and x in PAGE_DIFFERENCES for x in differences):failed['page_difference_categories']=sorted(set(differences))
        return failed
    if value.get('status') != 'notion_readonly_verified': return failed
    if any(value.get(k) is not False for k in ('ownership_adopted', 'receipt_advance', 'main_updated')): return failed
    if any(type(value.get(k)) is not int or value[k] != 0 for k in ('external_writes', 'get_requests', 'asset_downloads')): return failed
    if any(type(value.get(k)) is not int or value[k] != 2 for k in ('native_pages', 'source_notes_read')): return failed
    for key, maximum in (('native_blocks', 2 * native.MAX_BLOCKS), ('native_requests', 2000)):
        if type(value.get(key)) is not int or not 0 <= value[key] <= maximum: return failed
    try: git.checked_sha(value.get('source_commit'))
    except Exception: return failed
    summary = value.get('planner'); reasons = value.get('reason_counts')
    if not isinstance(summary, dict) or set(summary) != set(planner.public_summary({})) or summary.get('contract') != planner.CONTRACT or type(summary.get('records')) is not int or summary['records'] != 2 or summary.get('mode') != 'read_only' or summary.get('apply_ready') is not False or type(summary.get('writes')) is not int or summary['writes'] != 0 or summary.get('receipt_advance') is not False: return failed
    if summary.get('decision') not in ('blocked', 'noop_verified', 'preview_diff'): return failed
    if any(type(summary.get(k)) is not int or not 0 <= summary[k] <= 2 for k in ('noop_verified', 'preview_diff', 'blocked')) or sum(summary[k] for k in ('noop_verified', 'preview_diff', 'blocked')) != 2: return failed
    if not isinstance(reasons, dict) or set(reasons) - PLAN_REASONS or any(type(v) is not int or not 0 <= v <= 2 for v in reasons.values()): return failed
    if not valid_historical(value.get('historical_range')):return failed
    return {k: value[k] for k in ('historical_range','status', 'native_pages', 'native_blocks', 'native_requests', 'source_notes_read', 'source_commit', 'planner', 'reason_counts', 'external_writes', 'get_requests', 'asset_downloads', 'ownership_adopted', 'receipt_advance', 'main_updated')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-parent'); parser.add_argument('--publish-report')
    args = parser.parse_args(argv)
    if args.publish_report:
        try:
            with Path(args.publish_report).open('rb') as stream: data = stream.read(1024 * 1024 + 1)
            value = strict_capture_json(data, 1024 * 1024)
        except Exception: value = None
        print(json.dumps(public_report(value))); return 0
    try:
        git.checked_sha(args.expected_parent)
        request = NotionReadTransport(os.environ.get('NOTION_API_KEY'))
        remote = git.GitHubRemote(None, os.environ.get('PRIVATE_VAULT_TOKEN'))
        value = run_readonly(remote, request, args.expected_parent)
    except Exception as exc:
        value = {'status': 'notion_readonly_failed', 'stage': 'input', 'reason': str(exc) if str(exc) in REASONS else 'other'}
    result = public_report(value); print(json.dumps(result))
    return 0 if result['status'] == 'notion_readonly_verified' else 1


if __name__ == '__main__': raise SystemExit(main())
