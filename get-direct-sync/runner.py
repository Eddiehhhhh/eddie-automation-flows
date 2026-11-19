"""Cloud-only Get fan-out runner. Logs contain counts, never source snapshots."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import quote

from assets import AssetArchive, AssetTransport, asset_references
from core import CheckpointFailure, GetSource, Hold, Store, identity, requires_binary_assets, snapshot
from incremental_source import IncrementalSelector
from live_http import HTTP, NotionDownload, PacedHTTP
from media_delivery import GitHubMedia, NotionMedia, manifest_assets, read_asset_bytes
from notion_sink import NotionSink
from notion_projection import ProjectedNotionSink
from remote_state import RemoteState
from retry import HTTPFailure, RetryStopped, TransportFailure
from vault_sink import VaultGitHubSink
from source_index import publish as publish_source_index
from reviewed_history import clock_parent, plan_at_head, verify_clock, candidates, RUNTIME_SECONDS, RESERVE_SECONDS

ENV_NAMES = ('GET_SYNC_CONFIG_JSON', 'GETNOTE_API_KEY', 'GETNOTE_CLIENT_ID',
             'PRIVATE_VAULT_TOKEN', 'NOTION_API_KEY')
HOURLY_ENV_NAMES = ('GITHUB_EVENT_NAME', 'GITHUB_REF', 'GITHUB_ACTOR', 'GITHUB_TRIGGERING_ACTOR',
                    'GITHUB_REPOSITORY', 'GITHUB_RUN_ATTEMPT', 'GET_HOURLY_DISPATCH_KEY')
OWNER_ENV_NAMES = ('CODEX_THREAD_ID', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')
MODES = ('inspect', 'baseline', 'sync', 'backfill', 'drain')


def configuration(environ):
    if any(not isinstance(environ.get(k), str) or not environ[k] for k in ENV_NAMES):
        raise Hold('runtime_configuration_missing')
    encoded = environ['GET_SYNC_CONFIG_JSON']
    if len(encoded) > 65536:
        raise Hold('runtime_configuration_invalid')
    try:
        cfg = json.loads(encoded)
    except Exception:
        raise Hold('runtime_configuration_invalid') from None
    required = {'private_repo', 'data_branch', 'state_branch', 'namespace', 'raw_prefix',
                'notion_database_id', 'notion_properties', 'allowed_asset_hosts', 'scope'}
    if not isinstance(cfg, dict) or set(cfg) != required:
        raise Hold('runtime_configuration_invalid')
    if (cfg['scope'] not in ('isolated-validation', 'production') or
            not isinstance(cfg['private_repo'], str) or
            not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', cfg['private_repo']) or
            not isinstance(cfg['namespace'], str) or
            not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', cfg['namespace'])):
        raise Hold('runtime_configuration_invalid')
    for key in ('data_branch', 'state_branch'):
        branch = cfg[key]
        if (not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_-]{0,120}', branch)
                or '//' in branch or branch.endswith('/')):
            raise Hold('runtime_configuration_invalid')
    if (cfg['data_branch'] == cfg['state_branch'] or cfg['state_branch'] in ('main', 'master') or
            cfg['scope'] == 'isolated-validation' and cfg['data_branch'] in ('main', 'master')):
        raise Hold('runtime_configuration_invalid')
    if (not isinstance(cfg['notion_database_id'], str) or
            not re.fullmatch(r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})', cfg['notion_database_id'])):
        raise Hold('runtime_configuration_invalid')
    prefix = cfg['raw_prefix']
    if (not isinstance(prefix, str) or not prefix.startswith('Raw/03 Get/') or
            any(p in ('', '.', '..') or p.startswith('.') for p in prefix.split('/')) or
            any(ord(c) < 32 for c in prefix)):
        raise Hold('runtime_configuration_invalid')
    properties = cfg['notion_properties']
    if (not isinstance(properties, dict) or
            any(k not in ('id_property', 'owner_property', 'title_property') for k in properties) or
            any(not isinstance(v, str) or not v or len(v) > 100 for v in properties.values())):
        raise Hold('runtime_configuration_invalid')
    hosts = cfg['allowed_asset_hosts']
    if (not isinstance(hosts, list) or len(hosts) > 32 or
            any(not isinstance(h, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', h)
                or '..' in h or '.' not in h for h in hosts)):
        raise Hold('runtime_configuration_invalid')
    return cfg


def run_owner(environ):
    thread = environ.get('CODEX_THREAD_ID')
    if isinstance(thread, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', thread):
        return thread
    run, attempt = environ.get('GITHUB_RUN_ID'), environ.get('GITHUB_RUN_ATTEMPT')
    if (isinstance(run, str) and isinstance(attempt, str) and
            re.fullmatch(r'[1-9][0-9]{0,30}', run) and re.fullmatch(r'[1-9][0-9]{0,5}', attempt)):
        return 'github-' + run + '-' + attempt
    return 'get-run-' + secrets.token_hex(12)


def transports(environ):
    get = HTTP('https://openapi.biji.com', {
        'Authorization': environ['GETNOTE_API_KEY'], 'X-Client-ID': environ['GETNOTE_CLIENT_ID']})
    github = HTTP('https://api.github.com', {
        'Authorization': 'Bearer ' + environ['PRIVATE_VAULT_TOKEN'],
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
        'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
    notion = HTTP('https://api.notion.com', {
        'Authorization': 'Bearer ' + environ['NOTION_API_KEY'], 'Notion-Version': '2022-06-28'})
    return (PacedHTTP(get, min_interval=0.25), PacedHTTP(github, min_interval=0.2),
            PacedHTTP(notion, allow_notion_readonly_posts=True, min_interval=0.4))


class ReadOnlyJournal:
    def get(self, key):
        return None

    def set(self, key, value):
        raise Hold('inspect_write_forbidden')


def inspect(cfg, get_http, github_http, notion_http):
    result = get_http.request('GET', '/open/api/v1/resource/note/list')
    if (not result or result.get('success') is not True or
            not isinstance(result.get('data', {}).get('notes'), list) or
            type(result['data'].get('has_more')) is not bool):
        raise Hold('source_probe_failed')
    notes = result['data']['notes']
    if notes:
        import urllib.parse
        nid = identity(notes[0])
        detail = get_http.request('GET', '/open/api/v1/resource/note/detail?' +
                                  urllib.parse.urlencode({'id': nid, 'image_quality': 'original'}))
        if not detail or detail.get('success') is not True:
            raise Hold('source_probe_failed')
        s = snapshot(detail.get('data', {}).get('note', {}))
        if s['id'] != nid:
            raise Hold('source_probe_failed')
    remote = RemoteState(github_http, cfg['private_repo'], cfg['state_branch'],
                         cfg['namespace'], 'inspect')
    _, _, state = remote._read()
    ref = github_http.request('GET', '/repos/' + cfg['private_repo'] + '/git/ref/heads/' + cfg['data_branch'])
    if not ref or not ref.get('object', {}).get('sha'):
        raise Hold('data_branch_unavailable')
    NotionSink(notion_http, cfg['notion_database_id'], ReadOnlyJournal(),
               namespace=cfg['namespace'], **cfg['notion_properties']).preflight()
    return {'phase': 'inspected', 'source_probe_count': min(len(notes), 1),
            'private_target_verified': True, 'notion_schema_verified': True,
            'state_checkpoint_present': state['checkpoint'] is not None,
            'state_lease_present': state['lease'] is not None}


class ManifestCache:
    def __init__(self, archive, store, github_media=None, get_http=None):
        self.archive, self.store, self.cache = archive, store, {}
        self.github_media, self.get_http = github_media, get_http

    def _archive(self, s):
        try:
            return self.archive.archive_snapshot(s)
        except HTTPFailure as error:
            if self.get_http is None or error.status not in (401, 403, 404, 410):
                raise
        from urllib.parse import urlencode
        result = self.get_http.request('GET', '/open/api/v1/resource/note/detail?' +
                                       urlencode({'id': s['id'], 'image_quality': 'original'}))
        if not result or result.get('success') is not True:
            raise Hold('asset_refresh_source_invalid')
        fresh = snapshot(result.get('data', {}).get('note', {}))
        if fresh['id'] != s['id'] or fresh['version'] != s['version']:
            raise Hold('asset_refresh_revision_changed')
        manifest = self.archive.archive_snapshot(fresh)
        original = {ref['field']: hashlib.sha256(ref['url'].encode()).hexdigest()
                    for ref in asset_references(s['note'])}
        for asset in manifest_assets(fresh, manifest):
            asset['retrieval_url_sha256'] = asset['source_url_sha256']
            asset['source_url_sha256'] = original[asset['source_field']]
        return manifest

    def _restore(self, s, manifest):
        refs = {ref['field']: ref['url'] for ref in asset_references(s['note'])}
        refreshed = None
        for asset in manifest_assets(s, manifest):
            if self.github_media.restore(asset, self.archive.root):
                continue
            options = {'expected_sha256': asset['sha256'], 'expected_size': asset['bytes']}
            try:
                self.archive.fetch_with_retry(refs[asset['source_field']], **options)
            except HTTPFailure as error:
                if self.get_http is None or error.status not in (401, 403, 404, 410):
                    raise
                if refreshed is None:
                    from urllib.parse import urlencode
                    result = self.get_http.request('GET', '/open/api/v1/resource/note/detail?' +
                                                   urlencode({'id': s['id'], 'image_quality': 'original'}))
                    note = (result or {}).get('data', {}).get('note', {})
                    if not result or result.get('success') is not True or identity(note) != s['id']:
                        raise Hold('asset_refresh_source_invalid')
                    refreshed = {ref['field']: ref['url'] for ref in asset_references(note)}
                url = refreshed.get(asset['source_field'])
                if not url or url == refs[asset['source_field']]:
                    raise Hold('asset_refresh_reference_unavailable')
                # A fresh URL may only supply the exact bytes committed for this old revision.
                self.archive.fetch_with_retry(url, **options)
            read_asset_bytes(asset, self.archive.root)

    def get(self, s):
        key = s['id'] + ':' + s['version']
        if key not in self.cache:
            durable = 'asset_manifest:' + key
            previous = self.store.get(durable)
            if previous is not None and self.github_media is not None:
                self._restore(s, previous)
                manifest = previous
            else:
                manifest = self._archive(s)
                manifest_assets(s, manifest)
                if previous is not None and previous != manifest:
                    raise Hold('asset_manifest_changed')
                if previous is None:
                    self.store.set(durable, manifest)
            self.cache[key] = manifest
        return self.cache[key]


class CompleteSink:
    def __init__(self, name, text, media, store, manifests, archive_root):
        self.name, self.text, self.media = name, text, media
        self.store, self.manifests, self.root = store, manifests, archive_root

    def sync(self, s, receipt=None):
        binary = requires_binary_assets(s)
        manifest = self.manifests.get(s) if binary else None
        key = 'text_receipt:' + self.name + ':' + s['id'] + ':' + s['version']
        baseline = self.store.get(key) or receipt
        text = self.text.sync(s, baseline)
        if text.get('version') != s['version']:
            raise Hold('unverified_receipt')
        self.store.set(key, text)
        if not binary:
            if text.get('projection', {}).get('status') == 'partial':
                raise Hold('metadata_projection_incomplete')
            return text
        self.store.set('media_intent:' + self.name + ':' + s['id'] + ':' + s['version'],
                       {'phase': 'delivery_intent', 'version': s['version']})
        if self.name == 'github':
            media = self.media.sync_manifest(s, manifest, self.root)
        else:
            media = self.media.sync(s, text, manifest, self.root)
        if media.get('version') != s['version'] or media.get('binary_assets_verified') is not True:
            raise Hold('binary_assets_not_verified')
        if text.get('projection', {}).get('status') == 'partial':
            raise Hold('metadata_projection_incomplete')
        return {**text, 'media': media, 'binary_assets_verified': True,
                'content_scope': 'text_and_binary_assets'}


def complete(store, s):
    return all((store.receipt(k, s['id']) or {}).get('version') == s['version'] and
               (not requires_binary_assets(s) or (store.receipt(k, s['id']) or {}).get('binary_assets_verified') is True)
               for k in ('github', 'notion'))


def verify_legacy_writers(cfg, github_http, environ=None):
    if cfg['scope'] != 'production':
        return
    env = os.environ if environ is None else environ
    repo = env.get('GITHUB_REPOSITORY')
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise Hold('production_control_plane_required')
    for workflow in ('get-canonical.yml', 'get-direct-sync.yml'):
        path = '/repos/' + repo + '/actions/workflows/' + workflow
        definition = github_http.request('GET', path)
        if not definition or definition.get('state') != 'disabled_manually':
            raise Hold('legacy_writer_not_disabled')
        for status in ('in_progress', 'queued'):
            result = github_http.request('GET', path + '/runs?per_page=100&status=' + status)
            if (not isinstance(result, dict) or type(result.get('total_count')) is not int or
                    result['total_count'] > 100 or not isinstance(result.get('workflow_runs'), list) or
                    result['total_count'] != len(result['workflow_runs'])):
                raise Hold('legacy_writer_control_unverified')
            for run in result['workflow_runs']:
                if status == 'in_progress':
                    raise Hold('legacy_writer_running')
                if type(run.get('id')) is not int or run['id'] <= 0:
                    raise Hold('legacy_writer_control_unverified')
                jobs = github_http.request('GET', '/repos/' + repo + '/actions/runs/' + str(run['id']) + '/jobs?per_page=100')
                if not isinstance(jobs, dict) or jobs.get('total_count') != 0 or jobs.get('jobs') != []:
                    raise Hold('legacy_writer_queued_with_jobs')


def revision_digest(sid, version):
    return hashlib.sha256((sid + ':' + version).encode('ascii')).hexdigest()


def reviewed_revisions(store, requested, batch_size, paused_ids):
    tokens = requested.split(',')
    if (len(tokens) != batch_size or len(tokens) > 5 or len(set(tokens)) != len(tokens)
            or any(not re.fullmatch('[0-9a-f]{64}', token) for token in tokens)):
        raise Hold('reviewed_revisions_invalid')
    if store.pending_count():
        raise Hold('reviewed_backfill_pending_work')
    wanted = set(tokens)
    targets = {sid: version for sid, version in store.db.execute('SELECT id,version FROM inventory')
               if revision_digest(sid, version) in wanted}
    if len(targets) != len(wanted) or targets.keys() & set(paused_ids):
        raise Hold('reviewed_revisions_unavailable')
    for sid in targets:
        if (store.get('source_hold:' + sid) or {}).get('status') == 'held':
            raise Hold('reviewed_revisions_unavailable')
    return targets


def execute(cfg, mode, batch_size, get_http, github_http, notion_http, *, owner=None,
            review='', publish_large=None, hourly_env=None, monotonic=time.monotonic):
    deadline = monotonic() + RUNTIME_SECONDS
    if mode not in MODES or type(batch_size) is not int or not 1 <= batch_size <= 10:
        raise Hold('runner_arguments_invalid')
    if (review and mode != 'backfill' or mode == 'backfill' and
            cfg['scope'] == 'production' and not review):
        raise Hold('reviewed_backfill_required')
    if mode == 'inspect':
        return inspect(cfg, get_http, github_http, notion_http), 0
    verify_legacy_writers(cfg, github_http)
    remote = RemoteState(github_http, cfg['private_repo'], cfg['state_branch'],
                         cfg['namespace'], owner or run_owner({}))
    store, archive = None, None
    report = None
    with tempfile.TemporaryDirectory(prefix='get-direct-') as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        assets = root / 'assets'
        assets.mkdir(mode=0o700)
        try:
            remote.acquire(root / 'state.sqlite')
            store = Store(root / 'state.sqlite', checkpoint=remote.checkpoint)
            if getattr(remote, 'recovery_evidence', None):
                store.set('last_verified_lease_recovery', remote.recovery_evidence)
            initialized = store.db.execute("SELECT 1 FROM meta WHERE k='initialized'").fetchone() is not None
            if mode == 'baseline' and (initialized or store.pending_count() or
                                      store.db.execute('SELECT count(*) FROM inventory').fetchone()[0]):
                raise Hold('baseline_already_initialized')
            if mode != 'baseline' and not initialized:
                raise Hold('baseline_required')
            store.bind(cfg)
            observed, selected, source_holds, new_source_holds = 0, 0, [], []
            policy = store.get('sync_policy') or {'paused_ids': []}
            paused_ids = policy.get('paused_ids')
            if (not isinstance(paused_ids, list) or len(paused_ids) > 10000 or
                    any(not isinstance(sid, str) or not re.fullmatch('[0-9]+', sid) for sid in paused_ids)):
                raise Hold('source_policy_invalid')
            if mode in ('baseline', 'sync', 'backfill'):
                def source_hold(value):
                    source_holds.append(value['id'])
                selector = IncrementalSelector(store, paused_ids) if mode == 'sync' else None
                staged_holds = []
                def stage_source_hold(value):
                    source_hold(value)
                    staged_holds.append(value)
                reviewed = None
                if mode == 'backfill' and review:
                    reviewed = reviewed_revisions(store, review, batch_size, paused_ids)
                    def selector(summary):
                        return identity(summary) in reviewed
                elif mode=='backfill':
                    waiting_ids={row[0] for row in store.db.execute('SELECT id FROM pending')}
                    delivered=set(store.db.execute('SELECT id,version FROM delivered'))
                    inventory=dict(store.db.execute('SELECT id,version FROM inventory'))
                    chosen=set()
                    def selector(summary):
                        sid=identity(summary)
                        if len(chosen)>=batch_size or sid in paused_ids or sid in waiting_ids or (sid,inventory.get(sid)) in delivered:return False
                        prior=store.get('source_hold:'+sid)
                        if (prior and prior.get('status')=='held'
                            and prior.get('note',{}).get('updated_at')==summary.get('updated_at')):return False
                        chosen.add(sid);return True
                source=GetSource(get_http)
                snapshots = source.scan(on_note_hold=stage_source_hold,select_note=selector)
                observed = getattr(source,'observed_count',len(snapshots)+len(source_holds))
                if reviewed is not None:
                    if staged_holds or {s['id']: s['version'] for s in snapshots} != reviewed:
                        raise Hold('reviewed_source_changed')
                    adapter = NotionSink(notion_http, cfg['notion_database_id'], ReadOnlyJournal(),
                                         namespace=cfg['namespace'], **cfg['notion_properties'])
                    for s in snapshots:
                        if not complete(store, s) and adapter.pages(s['id']):
                            raise Hold('reviewed_destination_changed')
                for value in staged_holds:
                    previous = store.get('source_hold:' + value['id'])
                    if (previous and previous.get('status') == 'held' and
                            all(previous.get(k) == value[k] for k in ('version', 'reason'))):
                        continue
                    new_source_holds.append(value['id'])
                    history_key = 'source_hold_history:' + value['id'] + ':' + value['version']
                    if store.get(history_key) is None:
                        store.set(history_key, value)
                    store.set('source_hold:' + value['id'], dict(value, status='held'))
                for s in snapshots:
                    current_key = 'source_hold:' + s['id']
                    previous = store.get(current_key)
                    if previous and previous.get('status') != 'resolved':
                        store.set('source_hold_history:' + s['id'] + ':' + previous['version'], previous)
                        store.set(current_key, {'status': 'resolved', 'version': s['version'],
                                                'previous_version': previous['version']})
                if mode == 'backfill':
                    pending = set(store.db.execute('SELECT id,version FROM pending'))
                    batch = [s for s in snapshots if not complete(store, s)
                             and (s['id'],s['version']) not in pending][:batch_size]
                    selected = len(batch)
                    # Only this reviewed batch advances inventory and enters the outbox.
                    store.accept(batch)
                    store.enqueue(batch)
                elif mode == 'sync':
                    selector.commit(source, snapshots)
                    selected = len(snapshots)
                else:
                    store.accept(snapshots)
            counts = {'github': {'verified': 0, 'held': 0}, 'notion': {'verified': 0, 'held': 0}}
            history = None
            if mode != 'baseline':
                archive = AssetArchive(assets, AssetTransport(cfg['allowed_asset_hosts']))
                github_text = VaultGitHubSink(github_http, cfg['private_repo'], cfg['data_branch'],
                                             cfg['namespace'], store, raw_prefix=cfg['raw_prefix'])
                source_base = 'https://github.com/' + cfg['private_repo'] + '/blob/' + quote(cfg['data_branch'], safe='')
                notion_text = NotionSink(notion_http, cfg['notion_database_id'], store,
                                        namespace=cfg['namespace'], source_base=source_base, **cfg['notion_properties'])
                notion_text = ProjectedNotionSink(notion_text,notion_http,cfg['notion_database_id'],store,cfg['namespace'])
                github_media = GitHubMedia(github_http, cfg['private_repo'], cfg['data_branch'], cfg['namespace'],
                                           publish_large=publish_large)
                manifests = ManifestCache(archive, store, github_media, get_http)
                notion_media = NotionMedia(notion_http, cfg['notion_database_id'], store,
                                          namespace=cfg['namespace'], download=NotionDownload(assets), source_base=source_base,
                                          **cfg['notion_properties'])
                sinks = {
                    'github': CompleteSink('github', github_text, github_media, store, manifests, assets),
                    'notion': CompleteSink('notion', notion_text, notion_media, store, manifests, assets)}
                user_work_waiting = store.pending_count() > 0
                counts = store.drain(sinks, limit=batch_size, paused_ids=paused_ids)
                parent_run = clock_parent(hourly_env or {})
                available = lambda: monotonic() + RESERVE_SECONDS < deadline
                if (mode == 'sync' and cfg['scope'] == 'production' and cfg['data_branch'] == 'main'
                        and parent_run and not user_work_waiting and not new_source_holds
                        and not store.pending_count() and available()):
                    try:
                        plan = plan_at_head(github_http, cfg)
                        trusted_clock = plan and verify_clock(github_http, hourly_env.get('GITHUB_REPOSITORY'), parent_run)
                    except (Hold, HTTPFailure, RetryStopped, TransportFailure, KeyError, ValueError, TypeError):
                        plan, trusted_clock = None, False
                        history = {'deferred': 'plan_unavailable'}
                    if plan and not trusted_clock:
                        history = {'deferred': 'clock_unverified'}
                    elif plan:
                        history = {}
                        lookup = NotionSink(notion_http, cfg['notion_database_id'], ReadOnlyJournal(),
                                            namespace=cfg['namespace'], **cfg['notion_properties'])
                        for reviewed in candidates(plan, cfg, store, get_http, github_http, lookup,
                                                   paused_ids, history, available):
                            history['enqueued'] += store.enqueue([reviewed])['enqueued']
                            result = store.drain(sinks, limit=1, paused_ids=paused_ids)
                            for dest in ('github', 'notion'):
                                for metric in ('verified', 'held'):
                                    counts[dest][metric] += result[dest][metric]
            index = publish_source_index(store, github_http, cfg)
            reasons = dict(Counter(row[0] for row in store.db.execute('SELECT reason FROM held')))
            active_source_holds = sum(json.loads(row[0]).get('status') != 'resolved'
                                      for row in store.db.execute("SELECT v FROM journal WHERE k LIKE 'source_hold:%'"))
            report = {'phase': mode, 'observed': observed, 'selected': selected,
                      'source_holds': len(source_holds), 'source_holds_active': active_source_holds,
                      'new_source_holds': len(new_source_holds), 'paused_sources': len(paused_ids),
                      'paused_pending': sum(row[0] in set(paused_ids)
                                            for row in store.db.execute('SELECT id FROM pending')),
                      'pending': store.pending_count(), 'github': counts['github'],
                      'notion': counts['notion'], 'held_reasons': reasons,
                      'inventory_status': index['status'], 'index_summary': index['summary']}
            if history is not None:
                report['reviewed_history'] = history
        finally:
            if archive is not None:
                archive.close()
            if store is not None:
                store.db.close()
            if remote.lease is not None:
                remote.release()
    failed = report['new_source_holds'] or any(report[name]['held'] for name in ('github', 'notion'))
    return report, 2 if failed else 0


def main(argv=None, environ=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=MODES, required=True)
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--reviewed-revisions', default='')
    args = parser.parse_args(argv)
    if environ is None:
        environ = {k: os.environ.get(k) for k in ENV_NAMES + OWNER_ENV_NAMES + HOURLY_ENV_NAMES}
    try:
        cfg = configuration(environ)
        from git_media import publish_asset
        publisher = lambda http, *args: publish_asset(http, environ['PRIVATE_VAULT_TOKEN'], *args)
        report, status = execute(cfg, args.mode, args.batch_size, *transports(environ), owner=run_owner(environ),
                                 review=args.reviewed_revisions, publish_large=publisher,
                                 hourly_env={k: environ.get(k) for k in HOURLY_ENV_NAMES})
    except CheckpointFailure as error:
        reason = getattr(error, 'reason', 'checkpoint_failed')
        if not re.fullmatch(r'(?:checkpoint_[a-z_]{1,60}|checkpoint_http_[1-5][0-9]{2})', reason):
            reason = 'checkpoint_failed'
        report, status = {'phase': 'held', 'held_reasons': {reason: 1}}, 1
        stage = getattr(error, 'stage', None)
        if stage in ('context', 'read_repository', 'read_ref', 'read_commit', 'read_tree', 'read_blob',
                     'write_blob', 'write_tree', 'write_commit', 'write_ref', 'confirm_ref',
                     'verify_readback', 'sqlite_backup', 'compress_checkpoint', 'read_expected_tree',
                     'check_publish_ref', 'write_contents', 'verify_contents_blob', 'verify_contents_parent'):
            report['checkpoint_stage'] = stage
    except Exception:
        report, status = {'phase': 'held', 'held_reasons': {'runtime_held': 1}}, 1
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
