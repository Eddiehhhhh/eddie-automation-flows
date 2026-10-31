#!/usr/bin/env python3
"""Read-only notehelper messages, using the existing private-vault writer."""
import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import cloud_private_checkout
import sync_notion_flomo_to_raw as shared
import sync_notion_to_raw as notion
from sync_notion_articles_to_raw import reject

BASE = Path('Raw/16 笔记同步助手/云端消息')
STATE = Path('.github/state/notehelper-messages-state.json')
CONFIG = Path('.github/state/notehelper-messages-config.json')
ENDPOINT = 'https://relay-1.bijitongbu.site/helper/api/graphql'
PREFIX = '同步助手_'
QUERY = '''
    query Search($after: Int, $first: Int, $query: String) {
      search(after: $after, first: $first, query: $query) {
        items {
          id
          title
          author
          content
          originalUrl
          savedAt
          updatedAt
          publishedAt
          description
          siteName
          slug
          image
          pageType
          contentReader
          wordsCount
          readingProgressPercent
          isArchived
          archivedAt
          readAt
          highlights {
            id
            type
            quote
            prefix
            suffix
            patch
            annotation
            createdAt
            updatedAt
            highlightPositionPercent
            shortId
          }
          labels {
            id
            name
            color
            user_id
            created_at
            updated_at
          }
        }
        pageInfo {
          hasNextPage
          hasPreviousPage
          startCursor
          endCursor
          totalCount
        }
      }
    }'''
ATTACHMENT = re.compile(r'(!?)\[([^\]\n]*)\]\(([^\s)]+)\)')
HTML_IMAGE = re.compile(r'<img\b[^>]*\bsrc=[\"\']([^\"\']+)[\"\'][^>]*>', re.I)
MARKER = re.compile(r'<!--nh:([^\s<>]+)-->')
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_BATCH_ASSET_BYTES = 200 * 1024 * 1024


def encoded(value): return shared.encoded(value)
def sha(raw): return shared.digest(raw)


def api_page(token, after):
    if not token: raise ValueError('missing_message_credential')
    request = urllib.request.Request(ENDPOINT, data=encoded({'query': QUERY,
        'variables': {'after': after, 'first': 100, 'query': PREFIX}}),
        headers={'Content-Type': 'application/json', 'x-api-key': token}, method='POST')
    opener = urllib.request.build_opener(shared.NoRedirect())
    with opener.open(request, timeout=45) as response:
        raw = response.read(MAX_SOURCE_BYTES + 1)
        length = response.headers.get('Content-Length')
        if response.status != 200 or len(raw) > MAX_SOURCE_BYTES or (length and int(length) != len(raw)):
            raise ValueError('incomplete_message_response')
    return json.loads(raw)


def fetch_all(token, fetcher=api_page):
    rows = []; ids = set(); cursors = set(); after = None; pages = 0; source_bytes = 0
    for _ in range(100):
        value = fetcher(token, after); pages += 1
        source_bytes += len(encoded(value))
        if source_bytes > MAX_SOURCE_BYTES: raise ValueError('message_source_budget')
        if not isinstance(value, dict) or value.get('errors') or not isinstance(value.get('edges'), list):
            raise ValueError('invalid_message_response')
        info = value.get('pageInfo')
        if not isinstance(info, dict) or type(info.get('hasNextPage')) is not bool:
            raise ValueError('invalid_message_pagination')
        for edge in value['edges']:
            node = edge.get('node') if isinstance(edge, dict) else None
            if not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']:
                raise ValueError('invalid_message_node')
            if node['id'] in ids: raise ValueError('duplicate_source_message_id')
            ids.add(node['id'])
            if not isinstance(node.get('title'), str) or not node['title'].startswith(PREFIX):
                raise ValueError('unexpected_non_message_source')
            if not isinstance(node.get('content'), str): raise ValueError('missing_message_content')
            rows.append(node)
        if not info['hasNextPage']:
            count = info.get('totalCount')
            if count is not None and (type(count) is not int or count != len(ids)):
                raise ValueError('message_source_count_mismatch')
            return rows, {'pages': pages, 'terminal_has_next_page': False, 'unique_ids': len(ids), 'reported_total_count': count}
        cursor = info.get('endCursor')
        if not isinstance(cursor, str) or not re.fullmatch(r'\d+', cursor) or cursor in cursors or not value['edges']:
            raise ValueError('invalid_message_pagination')
        cursors.add(cursor); after = int(cursor)
    raise ValueError('message_page_budget')


def timestamp(value):
    if not isinstance(value, str): raise ValueError('missing_message_source_time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None: raise ValueError('ambiguous_message_source_time')
    return value


def legacy_inventory(root):
    result = {}
    for path in (root / BASE.parent).rglob('*.md'):
        if path.is_relative_to(root / BASE) or path.is_relative_to(root / BASE.parent / '云端原文'): continue
        raw = shared.safe_path(root, path.relative_to(root)).read_bytes()
        ids = sorted(set(MARKER.findall(raw.decode('utf-8'))))
        if ids: result[str(path.relative_to(root))] = {'sha256': sha(raw), 'source_message_ids': ids,
            'merge_status': 'unmerged_legacy_preserved'}
    return result


def suffix(name, raw):
    # Naming only: no type exclusion. Keep truthful bytes, do not infer MIME from a URL.
    ext = Path(urllib.parse.urlsplit(name).path).suffix.lower()
    return ext if re.fullmatch(r'\.[a-z0-9]{1,10}', ext) else '.bin'


def render(node, downloader):
    source = encoded(node); reject(source)
    created = timestamp(node.get('savedAt')); updated = timestamp(node.get('updatedAt'))
    body = node['content']; files = {}; resources = []; asset_paths = {}; total = 0
    links = list(ATTACHMENT.finditer(body)); html_images = list(HTML_IMAGE.finditer(body))
    urls = {m.group(3): (m.group(2), bool(m.group(1)) or m.group(0).startswith('!')) for m in links
        if m.group(1) or '📎' in body[max(0,m.start()-5):m.start()]}
    urls.update({m.group(1): ('image', True) for m in html_images})
    image = node.get('image')
    if image:
        if not isinstance(image,str) or not image.startswith('https://'): raise ValueError('unsupported_message_image_shape')
        urls.setdefault(image,('image',True))
    for url, (name, is_image) in urls.items():
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError('unsafe_asset_url')
        raw = downloader(url); total += len(raw)
        if not raw: raise ValueError('empty_message_asset')
        if len(raw) > shared.MAX_ASSET_BYTES: raise ValueError('asset_file_budget')
        if total > MAX_BATCH_ASSET_BYTES: raise ValueError('asset_batch_budget')
        asset = '_assets/' + sha(raw) + suffix(name if name != 'image' else url, raw)
        files[asset] = raw
        asset_paths[url] = asset
        local = '../../../' + asset
        body = body.replace(url, urllib.parse.quote(local, safe='/._-'))
        resources.append({'path': asset, 'sha256': sha(raw), 'size': len(raw), 'image': is_image})
    if image and image not in node['content']: body += '\n\n![](' + urllib.parse.quote('../../../'+asset_paths[image],safe='/._-') + ')'
    # Never advertise old local-writer IDs/markers in cloud Markdown.
    body = MARKER.sub(lambda m: '<!--source-notehelper:' + m.group(1) + '-->', body)
    version = sha(encoded({'source': sha(source), 'assets': resources, 'renderer': 1}))
    unit = sha(node['id'].encode())[:24]
    directory = Path('versions') / unit / version
    title = node['title'].replace('\n',' ')
    readable = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', title).strip(' .') or '消息'
    while len(readable.encode()) > 150: readable = readable[:-1]
    current = directory / (readable + '.md')
    metadata = {'title': title, 'source_system': 'notehelper-message', 'source_message_ids': [node['id']],
        'created_at': created, 'updated_at': updated, 'source_saved_at': created,
        'source_updated_at': updated, 'legacy_merge_status': 'unmerged_legacy_preserved'}
    markdown = (notion.render_frontmatter(metadata,native_lists=True) + '\n\n# '+title+'\n\n'+body+'\n').encode()
    reject(markdown)
    files[str(directory/'source.json')] = source
    files[str(current)] = markdown
    return {'current_path':str(current),'files':{p:sha(raw) for p,raw in files.items()},
        'resources': resources,'source_saved_at':created,'source_updated_at':updated}, files


def check_files(root, files):
    for relative, expected in files.items():
        path=shared.safe_path(root / BASE,relative)
        if not path.is_file() or sha(path.read_bytes()) != expected: raise ValueError('owned_message_content_edited')


def collect(root, token, *, apply=False, fetcher=api_page, downloader=shared.download_asset):
    root=Path(root)
    if apply: cloud_private_checkout.verify_private_checkout(root,os.environ.get('PRIVATE_VAULT_TOKEN'))
    with shared.writer_lock(root):
        path=shared.safe_path(root,STATE); before=path.read_bytes() if path.exists() else None
        state=json.loads(before) if before else {'version':1,'messages':{}}
        if state.get('version') != 1 or not isinstance(state.get('messages'),dict): raise ValueError('invalid_message_state')
        nodes, proof=fetch_all(token,fetcher)
        candidate=deepcopy(state); held={}; written=0; budget=0
        def persist():
            nonlocal before
            if not apply:return
            if (path.read_bytes() if path.exists() else None) != before: raise ValueError('message_state_changed')
            raw=encoded(candidate);shared.atomic_write(path,raw);before=raw
        for node in nodes:
            ident=node['id']; prior=candidate['messages'].get(ident,{})
            try:
                owned={}
                for files in prior.get('versions',{}).values(): owned.update(files)
                check_files(root,owned)
                record,files=render(node,downloader)
                budget += sum(len(raw) for name,raw in files.items() if name.startswith('_assets/'))
                if budget > MAX_BATCH_ASSET_BYTES: raise ValueError('asset_batch_budget')
                record['versions']={**prior.get('versions',{}),sha(encoded(record['files'])):record['files']}
                if apply:
                    for relative,raw in files.items():shared.immutable_write(shared.safe_path(root/BASE,relative),raw)
                candidate['messages'][ident]=record;persist();written+=1
            except Exception as exc:
                reason=('asset_http_' + str(exc.code)) if isinstance(exc,urllib.error.HTTPError) else (str(exc) if isinstance(exc,ValueError) else 'message_capture_failed')
                allowed={'potential_credential_in_source','asset_file_budget','asset_batch_budget','empty_message_asset',
                    'owned_message_content_edited','stage_edited','missing_message_source_time','ambiguous_message_source_time',
                    'asset_http_403','asset_http_404','asset_http_429','unsupported_message_image_shape','unsafe_asset_url','unsafe_asset_address','incomplete_asset'}
                held[ident]=reason if reason in allowed else 'message_capture_failed'
        candidate['coverage']={**proof,'observed':len(nodes),'captured':written,'held':held,
            'capture_complete':not held,'legacy_merge_status':'unmerged_legacy_preserved',
            'legacy_files':legacy_inventory(root)}
        # Upstream-absent records stay intact. Absence is not deletion.
        candidate['coverage']['upstream_absent_preserved']=len(set(candidate['messages'])-{n['id'] for n in nodes})
        persist()
        return candidate['coverage']


def eligible_path(root,path):
    root=Path(root)
    if not path.is_relative_to(root/BASE): return True
    try:
        state=json.loads(shared.safe_path(root,STATE).read_bytes())
        selected={str(BASE/record['current_path']) for record in state['messages'].values()}
        return str(path.relative_to(root)) in selected
    except Exception:return False


def verify_remote(root):
    root=Path(root);cloud_private_checkout.verify_private_checkout(root,os.environ.get('PRIVATE_VAULT_TOKEN'))
    def git(*args):return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,text=True,timeout=30).stdout.strip()
    git('fetch','origin','main')
    path=shared.safe_path(root,STATE);raw=path.read_bytes();state=json.loads(raw)
    files={str(STATE):sha(raw)}
    for record in state['messages'].values():
        for version in record['versions'].values():files.update({str(BASE/rel):digest for rel,digest in version.items()})
    for relative,expected in files.items():
        content=shared.safe_path(root,relative).read_bytes()
        oid=hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
        if sha(content)!=expected or any(git('rev-parse',ref+':'+relative)!=oid for ref in ('HEAD','origin/main')):
            raise ValueError('message_remote_readback_failed')
    if path.read_bytes()!=raw:raise ValueError('message_state_changed')
    return True


def config(root):
    path=shared.safe_path(root,CONFIG)
    value=json.loads(path.read_bytes()) if path.exists() else {'version':1,'enabled':False}
    if value.get('version')!=1 or type(value.get('enabled')) is not bool:raise ValueError('invalid_message_configuration')
    return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--verify-remote-state',action='store_true');parser.add_argument('--check-config',action='store_true')
    args=parser.parse_args();root=Path(os.environ['VAULT_ROOT'])
    if args.verify_remote_state: verify_remote(root);return
    value=config(root)
    if args.check_config:
        with open(os.environ['GITHUB_OUTPUT'],'a') as stream:stream.write('enabled='+str(value['enabled']).lower()+'\n')
        return
    if not value['enabled']:raise ValueError('message_configuration_disabled')
    coverage=collect(root,os.environ.get('NOTEBOOKSYNCER_API_KEY'),apply=args.apply)
    print(json.dumps({key:coverage[key] for key in ('observed','captured','capture_complete','upstream_absent_preserved')}))
    if not coverage['capture_complete']:raise SystemExit(2)

if __name__=='__main__':main()
