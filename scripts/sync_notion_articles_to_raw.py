#!/usr/bin/env python3
"""Official read-only Notion article collector; original plugin files stay intact."""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse

import cloud_private_checkout
import sync_notion_to_raw as notion
import sync_notion_flomo_to_raw as shared

BASE = Path('Raw/16 笔记同步助手/云端原文')
STATE = Path('.github/state/notion-articles-sync-state.json')
CONFIG = Path('.github/state/notion-articles-config.json')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def reject(raw):
    if shared.CREDENTIAL_SHAPE.search(raw.decode('utf-8', errors='ignore')):
        raise ValueError('potential_credential_in_source')


def legacy_inventory(root):
    inventory = {}
    for path in (root / BASE.parent).rglob('*.md'):
        if path.is_relative_to(root / BASE): continue
        shared.safe_path(root, path.relative_to(root))
        raw = path.read_bytes()
        reject(raw)
        text = raw.decode('utf-8')
        head = text.split('---', 2)[1] if text.startswith('---\n') else ''
        fields = {}
        for line in head.splitlines():
            key, sep, value = line.partition(':')
            if sep: fields[key] = value.strip().strip('"\'')
        inventory[str(path.relative_to(root))] = {'sha256': shared.digest(raw),
            'id': fields.get('id'), 'url': fields.get('url')}
    return inventory


def relations(page, inventory):
    url = page.get('properties', {}).get('原始链接', {}).get('url')
    paths = sorted(path for path, record in inventory.items() if url and record['url'] == url)
    ids = sorted({inventory[path]['id'] for path in paths if inventory[path]['id']})
    return {'local_existing_paths': paths, 'legacy_upstream_ids': ids,
            'relation_basis': 'exact_original_url' if paths else 'unmapped',
            'relation_unique': len(paths) == 1}


def render(page, blocks, relation, prefix='', historical=False):
    blocks = deepcopy(blocks)
    cache = {}
    def register(rows):
        for row in rows:
            data = row.get(row['type'], {})
            if row['type'] in ('image', 'file', 'pdf', 'audio', 'video'):
                kind = data.get('type')
                if kind in ('file', 'external') and data.get(kind, {}).get('url', '').startswith('assets/'):
                    data[kind]['url'] = prefix + data[kind]['url']
            if '_children' in row:
                cache[row['id']] = row['_children']; row['has_children'] = True
                register(row['_children'])
    register(blocks)
    properties = notion.summarize_properties(page)
    metadata = {'source_system': 'notion-saved-articles', 'notion_page_id': page['id'],
        'title': notion.page_title(page, '标题'), 'author': properties.get('作者'),
        'source': properties.get('平台'), 'url': properties.get('原始链接'),
        'saved': page['created_time'], 'created_at': page['created_time'],
        'updated_at': page['last_edited_time'], 'tags': properties.get('标签', []),
        **relation, 'source_history': historical}
    if relation['relation_unique'] and len(relation['legacy_upstream_ids']) == 1:
        metadata['legacy_upstream_id'] = relation['legacy_upstream_ids'][0]
    body = '\n'.join(notion.render_blocks('', blocks,
        children_loader=lambda key, ident: cache[ident], embed_images=True))
    if relation['local_existing_paths']:
        body += '\n\n## 既有存档\n\n' + '\n'.join('[[%s]]' % path.removesuffix('.md') for path in relation['local_existing_paths'])
    return (notion.render_frontmatter(metadata) + '\n\n' + body + '\n').encode()


def state_bytes(path):
    return path.read_bytes() if path.exists() else None


def owned(root, record):
    files = {**record.get('files', {}), **record.get('current_files', {})}
    for version in record.get('versions', {}).values(): files.update(version)
    for relative, sha in files.items():
        path = shared.safe_path(root / BASE, relative)
        if not path.is_file() or shared.digest(path.read_bytes()) != sha:
            raise ValueError('owned_content_edited')


def sync(root, database_id, api_key, *, apply=False, downloader=shared.download_asset):
    if not UUID.fullmatch(database_id): raise ValueError('invalid_database_id')
    if apply: cloud_private_checkout.verify_private_checkout(root, os.environ.get('PRIVATE_VAULT_TOKEN'))
    with shared.writer_lock(root):
        token = notion.READ_RETRIES.set(2)
        try: return collect(root, database_id, api_key, apply, downloader)
        finally: notion.READ_RETRIES.reset(token)


def collect(root, database_id, api_key, apply, downloader):
    state_path = shared.safe_path(root, STATE)
    original = state_bytes(state_path)
    state = json.loads(original) if original else {'version': 1, 'database_id': database_id, 'pages': {}}
    if state.get('version') != 1 or state.get('database_id') != database_id or not isinstance(state.get('pages'), dict):
        raise ValueError('invalid_state')
    known = set(state.get('generated_files', {}))
    for prior in state['pages'].values():
        known.update(prior.get('current_files', {}))
        for files in prior.get('versions', {}).values(): known.update(files)
    for path in (root / BASE).rglob('*'):
        shared.safe_path(root, path.relative_to(root))
        if path.is_file() and str(path.relative_to(root / BASE)) not in known:
            raise ValueError('unowned_source_namespace')
    rows = list(notion.paginate_database(api_key, database_id))
    ids = [row.get('id') for row in rows]
    if any(not isinstance(ident, str) or not UUID.fullmatch(ident) for ident in ids) or len(set(ids)) != len(ids):
        raise ValueError('incomplete_page_inventory')
    if any(notion.parse_time(row.get('last_edited_time')) is None for row in rows):
        raise ValueError('incomplete_page_inventory')
    # All source metadata and legacy ownership is checked before the first write.
    reject(shared.encoded(rows))
    inventory = legacy_inventory(root)
    if state.get('legacy_files') and state['legacy_files'] != inventory:
        raise ValueError('legacy_inventory_changed')
    state['legacy_files'] = inventory
    report = {'pages': len(rows), 'written': 0, 'unchanged': 0, 'pending': 0, 'held': {},
        'notion_writes': 0, 'legacy_preserved': len(inventory), 'uncovered_daily_input': True}
    def check_state():
        if state_bytes(state_path) != original: raise ValueError('state_changed')
    def persist(candidate):
        nonlocal state, original
        check_state()
        raw = shared.encoded(candidate); reject(raw)
        shared.atomic_write(state_path, raw)
        state, original = candidate, raw
    for page in rows:
        ident = page['id']; prior = state['pages'].get(ident, {})
        try:
            check_state(); owned(root, prior)
            if prior.get('last_edited_time') == page['last_edited_time'] and prior.get('files'):
                report['unchanged'] += 1; continue
            if not apply: report['pending'] += 1; continue
            complete = shared.checked_page(api_key, page)
            blocks = shared.block_tree(api_key, ident)
            reject(shared.encoded({'page': complete, 'blocks': blocks}))
            relation = relations(page, inventory)
            files = {}
            material = shared.materialize_assets({'page': complete, 'blocks': blocks}, files, downloader)
            for raw in files.values(): reject(raw)
            files['source.json'] = shared.encoded({'page': complete, 'blocks': blocks, 'materialized': material})
            files['article.md'] = render(material['page'], material['blocks'], relation, historical=True)
            # Volatile signed URL metadata remains in JSON, but does not cause a new
            # capture when the source's last_edited_time has not changed.
            version = shared.digest(shared.encoded({name: shared.digest(raw) for name, raw in files.items()}))
            prefix = str(Path('versions') / ident / version)
            versioned = {prefix + '/' + name: raw for name, raw in files.items()}
            current = render(material['page'], material['blocks'], relation, prefix=prefix + '/')
            reject(current)
            latest = notion.notion_request(api_key, 'GET', '/pages/' + ident)
            if latest.get('last_edited_time') != page['last_edited_time']:
                raise ValueError('source_changed_during_read')
            check_state(); owned(root, prior)
            current_path = shared.safe_path(root / BASE, ident + '.md')
            if not prior and current_path.exists(): raise ValueError('unowned_current_file')
            for relative, raw in versioned.items():
                shared.immutable_write(shared.safe_path(root / BASE, relative), raw)
            check_state(); owned(root, prior)
            shared.atomic_write(current_path, current)
            hashes = {name: shared.digest(raw) for name, raw in versioned.items()}
            candidate = deepcopy(state)
            candidate['pages'][ident] = {'last_edited_time': page['last_edited_time'],
                'current_version': version, 'title': notion.page_title(page, '标题'), 'files': hashes,
                'current_files': {ident + '.md': shared.digest(current)},
                'versions': {**prior.get('versions', {}), version: hashes}, **relation}
            persist(candidate); report['written'] += 1
        except Exception as exc:
            allowed = {'owned_content_edited','state_changed','source_changed_during_read','unowned_current_file',
                'potential_credential_in_source','asset_file_budget','asset_batch_bound','incomplete_asset',
                'incomplete_notion_pagination','unsupported_block','block_cycle','incomplete_relation'}
            code = str(exc); report['held'][ident] = code if code in allowed else 'source_capture_failed'
            if code == 'state_changed': break
    report['complete'] = not report['held'] and not report['pending']
    if apply:
        check_state()
        # Freeze original plugin ownership even if the source was empty.
        if original is None: persist(deepcopy(state))
        index = ['---', 'source_index: true', '---', '# 云端文章', '',
                 'Notion 原文云端接收；原插件文章、AI整理和日报仍保留。', '']
        for ident, record in sorted(state['pages'].items()):
            title = re.sub(r'[\[\]\r\n]', ' ', record.get('title', ident))
            index.append(f'- [{title}]({ident}.md)')
        generated = {'index.md': '\n'.join(index).encode(), 'coverage.json': shared.encoded(report)}
        prior_generated = state.get('generated_files', {})
        for name, raw in generated.items():
            path = shared.safe_path(root / BASE, name)
            if path.exists() and (name not in prior_generated or shared.digest(path.read_bytes()) != prior_generated[name]):
                raise ValueError('owned_content_edited')
            check_state(); shared.atomic_write(path, raw)
        candidate = deepcopy(state); candidate['generated_files'] = {name: shared.digest(raw) for name,raw in generated.items()}
        persist(candidate)
    return report


def verify_remote_state(root):
    cloud_private_checkout.verify_private_checkout(root, os.environ.get('PRIVATE_VAULT_TOKEN'))
    raw = state_bytes(shared.safe_path(root, STATE))
    if raw is None: raise ValueError('missing_private_state')
    def git(*args):
        result = subprocess.run(['git','-C',str(root),*args], capture_output=True, timeout=60)
        if result.returncode: raise ValueError('private_readback_failed')
        return result.stdout.strip()
    git('fetch','origin','main')
    for path in (STATE, BASE):
        if git('rev-parse','HEAD:' + str(path)) != git('rev-parse','origin/main:' + str(path)):
            raise ValueError('private_readback_failed')
    state = json.loads(raw)
    files = {str(STATE): shared.digest(raw)}
    for record in state['pages'].values():
        owned(root, record)
        rels = {**record['current_files']}
        for version in record['versions'].values(): rels.update(version)
        files.update({str(BASE / rel): sha for rel,sha in rels.items()})
    files.update({str(BASE / name):sha for name,sha in state.get('generated_files',{}).items()})
    files.update({path:record['sha256'] for path,record in state['legacy_files'].items()})
    for path, sha in files.items():
        local = shared.safe_path(root, path).read_bytes()
        if shared.digest(local) != sha:
            raise ValueError('private_readback_failed')
        # git show must preserve trailing bytes (unlike text-oriented rev-parse).
        result = subprocess.run(['git','-C',str(root),'show','origin/main:' + path],capture_output=True,timeout=60)
        if result.returncode or shared.digest(result.stdout) != sha: raise ValueError('private_readback_failed')
    if state_bytes(root / STATE) != raw: raise ValueError('state_changed')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--verify-remote-state', action='store_true')
    args = parser.parse_args()
    try:
        root = Path(os.environ['VAULT_ROOT'])
        if args.verify_remote_state: verify_remote_state(root)
        else:
            config_path = args.config or root / CONFIG
            if config_path.resolve() != (root / CONFIG).resolve(): raise ValueError('invalid_config_path')
            config = json.loads(shared.safe_path(root, CONFIG).read_bytes())
            if config.get('version') != 1 or config.get('enabled') is not True: raise ValueError('disabled_config')
            result = sync(root, config['database_id'], notion.load_notion_api_key(), apply=args.apply)
            print('Article source capture: written=%d unchanged=%d held=%d pending=%d' %
                (result['written'],result['unchanged'],len(result['held']),result['pending']))
            return 0 if result['complete'] else 2
    except Exception:
        print('Article source capture incomplete; private details suppressed.')
        return 2
    print('Private article state and content readback verified.')
    return 0


if __name__ == '__main__': raise SystemExit(main())
