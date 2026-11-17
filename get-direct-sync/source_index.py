"""Index verified receipts; cache identical proof and rotate five remote target audits."""
import hashlib
import json
import posixpath
import re
import urllib.parse

import core
from core import Hold, canonical, requires_binary_assets
from vault_sink import VaultGitHubSink, write_verified

SCHEMA = 'get-direct-current-v1'
CHECK_SCHEMA = 'get-direct-index-checks-v1'
AUDIT_SIZE = 5


def _checked_target(sink, record, expected_relative):
    if (not isinstance(record, dict) or record.get('relative_path') != expected_relative
            or not isinstance(record.get('sha'), str) or not isinstance(record.get('sha256'), str)):
        raise Hold('source_index_receipt_invalid')
    path = '/repos/' + sink.repo + '/contents/' + urllib.parse.quote(expected_relative, safe='/')
    observed = sink.http.request('GET', path + '?' + urllib.parse.urlencode({'ref': sink.branch}))
    if not observed or observed.get('sha') != record['sha']:
        raise Hold('source_index_receipt_target_changed')
    body = sink.content(observed)
    if hashlib.sha256(body).hexdigest() != record['sha256']:
        raise Hold('source_index_receipt_target_changed')
    return body


def _verify_receipt(sink, sid, receipt):
    version = receipt['version']
    _checked_target(sink, receipt['markdown'], receipt['relative_path'])
    source_path = 'get-direct/' + sink.namespace + '/versions/' + sid + '/' + version + '.json'
    raw = _checked_target(sink, receipt.get('source_json'), source_path)
    try:
        source = json.loads(raw)
        if (source.get('owner') != 'get-direct-v1/' + sink.namespace or source.get('id') != sid
                or source.get('version') != version or core.source_version(source['note']) != version):
            raise Hold('source_index_receipt_invalid')
    except Hold:
        raise
    except Exception:
        raise Hold('source_index_receipt_invalid') from None
    return requires_binary_assets(source)


def _label(value):
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value)
    for char in ('\\', '[', ']', '*', '_', '`', '<', '>', '!'):
        value = value.replace(char, '\\' + char)
    return value


def publish(store, http, cfg):
    """Return index receipts; never convert an observed inventory baseline into saved rows."""
    writer = VaultGitHubSink(http, cfg['private_repo'], cfg['data_branch'], cfg['namespace'],
                             store, raw_prefix=cfg['raw_prefix'])
    sink = writer.sink
    repo = http.request('GET', '/repos/' + sink.repo)
    if not repo or repo.get('private') is not True:
        raise Hold('private_repository_required')
    binding = {'repo': sink.repo, 'branch': sink.branch, 'namespace': sink.namespace,
               'raw_prefix': cfg['raw_prefix']}
    cache_key = 'source_index_checks:' + hashlib.sha256(canonical(binding).encode()).hexdigest()
    previous = store.get(cache_key)
    if previous is not None and (not isinstance(previous, dict) or previous.get('schema') != CHECK_SCHEMA
            or previous.get('binding') != binding or not isinstance(previous.get('receipts'), dict)
            or previous.get('audit_after') is not None and not isinstance(previous['audit_after'], str)):
        raise Hold('source_index_check_cache_invalid')
    previous = previous or {'receipts': {}, 'audit_after': None}
    rows, unchanged = [], []
    for sid, encoded in store.db.execute("SELECT id,value FROM receipts WHERE sink='github' ORDER BY id"):
        receipt = json.loads(encoded)
        version = receipt.get('version')
        if (not re.fullmatch(r'[0-9]+', sid) or not isinstance(version, str)
                or not re.fullmatch(r'[0-9a-f]{64}', version)
                or not isinstance(receipt.get('source_title'), str)):
            raise Hold('source_index_receipt_invalid')
        relative = receipt.get('relative_path')
        legacy = cfg['raw_prefix'] + '/' + sid + '.md'
        if (not isinstance(relative, str) or relative != legacy
                and (posixpath.dirname(relative) != cfg['raw_prefix'] + '/' + sid
                     or not relative.endswith('.md'))):
            raise Hold('source_index_receipt_invalid')
        md = receipt.get('markdown')
        if not isinstance(md, dict) or md.get('version') != version:
            raise Hold('source_index_receipt_invalid')
        source_receipt = receipt.get('source_json')
        source_path = 'get-direct/' + sink.namespace + '/versions/' + sid + '/' + version + '.json'
        if (not isinstance(source_receipt, dict) or source_receipt.get('relative_path') != source_path
                or any(not isinstance(record.get(field), str) for record in (md, source_receipt)
                       for field in ('sha', 'sha256'))):
            raise Hold('source_index_receipt_invalid')
        fingerprint = hashlib.sha256(canonical(receipt).encode()).hexdigest()
        cached = previous['receipts'].get(sid)
        reuse = (isinstance(cached, dict) and cached.get('receipt_sha256') == fingerprint
                 and type(cached.get('binary_assets_required')) is bool)
        rows.append((sid, receipt, fingerprint, cached if reuse else None))
        if reuse:
            unchanged.append(sid)
    after = previous.get('audit_after')
    rotation = unchanged if after is None else [sid for sid in unchanged if sid > after] + [sid for sid in unchanged if sid <= after]
    audited = rotation[:AUDIT_SIZE]
    checks, notes, both_verified = {}, {}, 0
    for sid, receipt, fingerprint, cached in rows:
        binary = _verify_receipt(sink, sid, receipt) if cached is None or sid in audited else cached['binary_assets_required']
        checks[sid] = {'receipt_sha256': fingerprint, 'binary_assets_required': binary}
        version, relative = receipt['version'], receipt['relative_path']
        md, source_receipt = receipt['markdown'], receipt['source_json']
        notion = store.receipt('notion', sid) or {}
        notion_version = notion.get('version')
        github_binary = receipt.get('binary_assets_verified') is True
        notion_binary = notion.get('binary_assets_verified') is True
        metadata = notion.get('projection', {}).get('status')
        verified = (notion_version == version and (not binary or github_binary and notion_binary)
                    and metadata != 'partial')
        both_verified += verified
        notes[sid] = {
            'version': version, 'source_title': receipt['source_title'],
            'note_type': receipt.get('note_type', ''), 'updated_at': receipt.get('updated_at'),
            'relative_path': relative,
            'markdown': {key: md[key] for key in ('relative_path', 'sha', 'sha256')},
            'source_json': {key: source_receipt[key] for key in ('relative_path', 'sha', 'sha256')},
            'binary_assets_required': binary, 'github_binary_assets_verified': github_binary,
            'notion_verified_version': notion_version, 'notion_binary_assets_verified': notion_binary,
            'notion_projection_status': metadata, 'status': 'verified' if verified else 'partial',
        }
    next_cache = {'schema': CHECK_SCHEMA, 'binding': binding, 'receipts': checks,
                  'audit_after': audited[-1] if audited else after}
    if next_cache != previous:
        store.set(cache_key, next_cache)
    observed = store.db.execute('SELECT count(*) FROM inventory').fetchone()[0]
    source_holds = sum(json.loads(row[0]).get('status') != 'resolved'
                       for row in store.db.execute("SELECT v FROM journal WHERE k LIKE 'source_hold:%'"))
    pending = store.pending_count()
    status = 'verified' if len(notes) == observed == both_verified and not pending and not source_holds else 'partial'
    summary = {'inventory_observed': observed, 'github_saved': len(notes),
               'both_destinations_verified': both_verified, 'source_holds': source_holds, 'pending': pending}
    owner = 'get-direct-v1/' + sink.namespace
    current = {'schema': SCHEMA, 'owner': owner, 'scope': 'verified-receipts-only',
               'status': status, 'summary': summary, 'notes': notes}
    lines = ['---', 'source_system: getnote', 'sync_owner: ' + json.dumps(owner),
             'scope: verified-receipts-only', 'status: ' + status, '---', '', '# Get 笔记同步索引', '',
             '本页仅列出已读回的 GitHub 回执；基线清点不等于笔记已保存。', '',
             '- 已观察库存：' + str(observed), '- 已保存 GitHub 正文：' + str(len(notes)),
             '- 双端已核验：' + str(both_verified), '- 等待处理：' + str(pending),
             '- 来源待核对：' + str(source_holds), '', '## 已保存笔记', '']
    for sid, row in sorted(notes.items(), key=lambda item: (item[1]['source_title'].casefold(), item[0])):
        target = posixpath.relpath(row['relative_path'], cfg['raw_prefix'])
        lines.append('- [' + _label(row['source_title']) + '](' + urllib.parse.quote(target, safe='/.-')
                     + ') — ' + row['status'])
    if not notes:
        lines.append('当前没有已保存回执；本页不代表已完成全量同步。')
    prefix = 'source_index:' + sink.repo + ':' + sink.branch + ':' + sink.namespace + ':'
    markdown = write_verified(sink, store, prefix + 'index.md', cfg['raw_prefix'] + '/index.md',
                              ('\n'.join(lines) + '\n').encode(), require_managed=True)
    machine = write_verified(sink, store, prefix + 'current.json', cfg['raw_prefix'] + '/current.json',
                             (json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode(),
                             require_managed=True)
    return {'status': status, 'summary': summary, 'index': markdown, 'current': machine,
            'target_audit': {'new_or_changed': len(rows) - len(unchanged),
                             'rotating': len(audited), 'reused': len(unchanged) - len(audited)}}
