"""One Get note from an authenticated private-main checkout to the Get data source.

The controller must commit an attempt marker before invoking --apply. A failed
or missing write response never licenses a second create attempt.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import subprocess

import get_notion_live_transport as transport
import get_notion_properties as properties
import get_notion_sink_plan as sink
import get_notion_incremental_media as media

REPO = 'Eddiehhhhh/eddie-llm-wiki'
REGISTRY = 'Raw/03 Get/03 State/canonical-registry.json'
LEGACY = 'Raw/03 Get/03 State/legacy-reconciliation-state.json'
STATE = '.sync/get-notion/state.json'
ATTEMPTS = '.sync/get-notion-incremental/attempts'
RECORDING_FIELDS = frozenset(('录音时间', '时长', '参与人数', '内容类型', '录音总结'))


def stop(reason):
    raise ValueError(reason) from None


def git(root, *args, input=None):
    return subprocess.check_output(['git', '-C', str(root), *args], input=input,
                                   stderr=subprocess.DEVNULL)


def source(root, ident, expected_commit):
    if not re.fullmatch(r'[0-9]{19}', ident) or not re.fullmatch(r'[a-f0-9]{40}', expected_commit):
        stop('invalid_source')
    if git(root, 'rev-parse', 'HEAD').decode().strip() != expected_commit:
        stop('source_changed')
    origin = git(root, 'remote', 'get-url', 'origin').decode().strip()
    if origin not in (f'https://github.com/{REPO}.git', f'https://github.com/{REPO}',
                      f'git@github.com:{REPO}.git'):
        stop('wrong_repository')
    remote = git(root, 'ls-remote', 'origin', 'refs/heads/main').decode().split()
    if len(remote) != 2 or remote != [expected_commit, 'refs/heads/main']:
        stop('source_changed')
    registry = json.loads(git(root, 'show', 'HEAD:' + REGISTRY))
    if ident in registry.get('conflictsById', {}):
        stop('source_conflict')
    row = registry.get('newById', {}).get(ident)
    if row is None:
        legacy = json.loads(git(root, 'show', 'HEAD:' + LEGACY))
        if ident in legacy.get('conflictsById', {}):
            stop('source_conflict')
        original = legacy.get('legacyById', {}).get(ident)
        if not isinstance(original, dict) or not isinstance(original.get('ownership'), dict):
            stop('source_unregistered')
        row = {'filePath': original['ownership'].get('filePath'),
               'baseline_blob': original.get('baseline_blob'), 'source_metadata': None}
    if not isinstance(row, dict):
        stop('source_unregistered')
    relative = row.get('filePath')
    if (not isinstance(relative, str) or not relative.endswith('.md')
            or PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts
            or relative.startswith('./')):
        stop('source_path_invalid')
    path = 'Raw/03 Get/' + relative
    tree = git(root, 'ls-tree', '-z', 'HEAD', '--', path).split(b'\0')
    if len(tree) != 2 or tree[1] != b'' or not tree[0].startswith(b'100644 blob '):
        stop('source_missing_or_duplicate')
    blob, _, tree_path = tree[0].partition(b'\t')
    if tree_path.decode() != path:
        stop('source_changed')
    data = git(root, 'show', 'HEAD:' + path)
    if len(data) > sink.MAX_NOTE:
        stop('source_too_large')
    sink.source_body(data, ident)
    content_blob = git(root, 'hash-object', '--stdin', input=data).decode().strip()
    if (blob.split()[2].decode() != content_blob or row.get('baseline_blob') != content_blob):
        stop('source_changed')
    if row.get('source_metadata') is None:
        fields = properties.source_fields(data, ident)
        if fields.get('attachments') not in (None, []):
            stop('source_metadata_held')
        row['source_metadata'] = {'attachments': [], 'timestamps': {}}
    state = json.loads(git(root, 'show', 'HEAD:' + STATE))
    unresolved = state.get('engine_state', {}).get('unresolved_by_id')
    if not isinstance(unresolved, dict) or ident in unresolved:
        stop('historical_write_held')
    return path, data, row


def desired(data, ident, path, row, schema, *, media_assets=None, media_receipts=None,
            media_preview=False):
    recording = properties.source_fields(data, ident).get('note_type') in ('recorder_audio', 'recorder_flash_audio')
    title_lines = [line[7:] for line in data.decode().split('\n---\n', 1)[0].splitlines()
                   if line.startswith('title: ')]
    if len(title_lines) != 1:
        stop('source_title_invalid')
    try:
        title = json.loads(title_lines[0])
    except ValueError:
        stop('source_title_invalid')
    if not isinstance(title, str) or not title or len(title) > 2000:
        stop('source_title_invalid')
    names = [name for name, value in schema.items() if value.get('type') == 'title']
    if len(names) != 1:
        stop('target_schema_changed')
    timestamps = row['source_metadata'].get('timestamps')
    if not isinstance(timestamps, dict):
        stop('source_timestamps_missing')
    exact_dates = {}
    compatible = dict(timestamps)
    fields = properties.source_fields(data, ident)
    for source_key, target_key in (('created_at', '创建时间'), ('updated_at', '更新时间')):
        raw, precise = fields.get(source_key), timestamps.get('edited_at' if source_key == 'updated_at' else source_key)
        if precise is None:
            continue
        try:
            original = datetime.fromisoformat(raw)
            observed = datetime.fromisoformat(precise)
        except (TypeError, ValueError):
            stop('source_timestamp_invalid')
        if (original.tzinfo is None and observed.tzinfo is not None
                and original == observed.replace(tzinfo=None, microsecond=0)):
            compatible['edited_at' if source_key == 'updated_at' else source_key] = observed.replace(microsecond=0).isoformat()
            exact_dates[target_key] = observed.isoformat()
    projected, held = properties.project(data, ident, properties.schema_projection(schema),
                                         compatible, row['source_metadata'])
    if held:
        stop('source_properties_held')
    for name, precise in exact_dates.items():
        projected[name] = {'type': 'date', 'date': {'start': precise}}
    media_assets = [] if media_assets is None else media_assets
    if media_assets:
        asset_map, content_tree = media.render_assets(media_assets, media_receipts,
                                                       preview=media_preview)
    else:
        asset_map, content_tree = {}, {}
    blocks, used_assets, warnings = sink.render_markdown(data, ident, asset_map, path, content_tree)
    if warnings or not blocks or len(blocks) > 100:
        stop('source_content_held')
    if media_assets:
        if (len(used_assets) != len(asset_map) or set(used_assets) != set(asset_map)
                or media.image_count(blocks) != len(media_assets)):
            stop('source_content_held')
        if media_receipts is not None:
            media.bind_uploaded_blocks(blocks, media_assets, media_receipts)
    elif used_assets:
        stop('source_content_held')
    props = {'笔记 ID': {'type': 'rich_text', 'rich_text': [sink.text_run(ident)]},
             names[0]: {'type': 'title', 'title': [sink.text_run(title)]}, **projected}
    if recording:
        props.update(recording_properties(data, ident, schema))
        blocks = recording_blocks(blocks)
    return properties.date_wire_properties(props, properties.date_wire_binding(props)), blocks


def recording_properties(data, ident, schema):
    from get_notion_credential_projection import render_body
    body = render_body(data, ident, sink.source_body(data, ident))
    info = re.search(r'^#### 录音信息\s*\n(.*?)(?=^#{2,4} |\Z)', body, re.M | re.S)
    summary = re.search(r'^#### 录音总结\s*\n\s*([^\n]+)', body, re.M)
    if info is None or summary is None:
        stop('recording_metadata_held')
    labels = {}
    for name, value in re.findall(r'^- \*\*(录音时间|时长|参与人数|内容类型)\*\*：(.+)$', info[1], re.M):
        if name in labels or not value.strip():
            stop('recording_metadata_held')
        labels[name] = value.strip()
    if set(labels) != {'录音时间', '时长', '参与人数', '内容类型'}:
        stop('recording_metadata_held')
    labels['录音总结'] = summary[1].strip()
    result = {}
    for name, value in labels.items():
        if schema.get(name, {}).get('type') != 'rich_text':
            stop('recording_schema_changed')
        result[name] = {'type': 'rich_text', 'rich_text': properties._rich_text_payload(value)}
    return result


def recording_blocks(blocks):
    def label(block):
        return ''.join(run['text']['content'] for run in block['payload']['rich_text'])
    if (len(blocks) < 4 or blocks[0]['type'] != 'heading_2' or label(blocks[0]) != 'AI 总结'
            or blocks[1]['type'] != 'heading_3' or '智能总结' not in label(blocks[1])):
        stop('recording_layout_held')
    original = [index for index, block in enumerate(blocks)
                if block['type'] == 'heading_2' and label(block) == '原文']
    if len(original) != 1 or original[0] <= 2:
        stop('recording_layout_held')
    summary = []
    for block in blocks[2:original[0]]:
        if block['type'] in ('heading_3', 'heading_4'):
            summary.append(sink.block('heading_2', block['payload']))
        else:
            summary.append(block)
    first = sink.block('heading_1', {'rich_text': [sink.text_run('录音智能总结')], 'is_toggleable': True}, summary)
    second = sink.block('heading_1', {'rich_text': [sink.text_run('原文')], 'is_toggleable': True}, blocks[original[0] + 1:])
    return [first, second]


def target_sha(snapshot, names):
    semantic = sink.snapshot_tree(snapshot)[3]
    raw = snapshot['properties_raw']
    values = {}
    for name in names:
        if name not in raw:
            stop('existing_properties_differ')
        if name in properties.FIELDS or name in RECORDING_FIELDS or name == '笔记 ID':
            values[name] = properties.value(raw[name])
        else:
            values[name] = [x.get('plain_text') for x in raw[name].get('title', [])]
    return hashlib.sha256(sink.encoded({'blocks': semantic, 'properties': values})).hexdigest()


def verify_note(api, ident, page, props, blocks, *, snapshot=None):
    snapshot = api.read_note(ident) if snapshot is None else snapshot
    if snapshot.get('page_id') != page or sink.snapshot_tree(snapshot)[3] != sink.comparison_semantics(blocks):
        stop('existing_content_differs')
    actual = snapshot['properties_raw']
    for name, value in props.items():
        if name not in actual:
            stop('existing_properties_differ')
        if name in properties.FIELDS or name in RECORDING_FIELDS or name == '笔记 ID':
            if properties.value(actual[name]) != properties.value(value):
                stop('existing_properties_differ')
        elif [x.get('plain_text') for x in actual[name].get('title', [])] != [x['text']['content'] for x in value['title']]:
            stop('existing_properties_differ')
    return target_sha(snapshot, props)


def owned_receipt(ident, page, data, target, *, created=False, adoption=None):
    receipt = {'note_id': ident, 'page_id': page,
               'source_sha256': hashlib.sha256(data).hexdigest(), 'target_sha256': target}
    if created:
        receipt['origin'] = 'created'
    if adoption is not None:
        receipt['origin'] = 'adopted'
        receipt['adoption'] = adoption
    return receipt


def checked_adoption(owned):
    value = owned.get('adoption')
    if (owned.get('origin') != 'adopted' or not isinstance(value, dict)
            or set(value) != {'contract', 'source_commit', 'source_blob',
                              'initial_target_sha256', 'target_snapshot_sha256'}
            or value.get('contract') != 'get-notion-exact-adoption-1'
            or any(not isinstance(value.get(key), str) or re.fullmatch('[a-f0-9]{40}', value[key]) is None
                   for key in ('source_commit', 'source_blob'))
            or any(not isinstance(value.get(key), str) or re.fullmatch('[a-f0-9]{64}', value[key]) is None
                   for key in ('initial_target_sha256', 'target_snapshot_sha256'))):
        stop('manual_edit_or_unknown_write')
    return value


def validate_owned_page(owned, ident, page):
    required = {'note_id', 'page_id', 'source_sha256', 'target_sha256'}
    if not isinstance(owned, dict):
        stop('manual_edit_or_unknown_write')
    shape = (required if 'origin' not in owned else
             required | {'origin', 'adoption'} if owned.get('origin') == 'adopted' else
             required | {'origin'} if owned.get('origin') == 'created' else set())
    if (set(owned) != shape or owned['note_id'] != ident or owned['page_id'] != page
            or any(not isinstance(owned[key], str)
                   or re.fullmatch(r'[a-f0-9]{64}', owned[key]) is None
                   for key in ('source_sha256', 'target_sha256'))):
        stop('manual_edit_or_unknown_write')
    if owned.get('origin') == 'adopted':
        checked_adoption(owned)
    return owned


def adopt(root, ident, commit, source_id, token, legacy_owned):
    """Read-only candidate; caller must save the receipt with private-main CAS."""
    path, data, row = source(root, ident, commit)
    required = {'note_id', 'page_id', 'source_sha256', 'target_sha256'}
    if (not isinstance(legacy_owned, dict) or set(legacy_owned) != required
            or legacy_owned.get('note_id') != ident
            or legacy_owned.get('source_sha256') != hashlib.sha256(data).hexdigest()):
        stop('unowned_target')
    api = transport.API(transport.Transport(token), source_id, [ident])
    props, blocks = desired(data, ident, path, row, api.schema())
    if api.matches(ident) != [legacy_owned.get('page_id')]:
        stop('unowned_target')
    page = legacy_owned['page_id']
    target = verify_note(api, ident, page, props, blocks)
    if target != legacy_owned.get('target_sha256'):
        stop('manual_edit_or_unknown_write')
    import get_notion_executor as executor
    snapshot = api.read_note(ident)
    if (api.matches(ident) != [page] or snapshot.get('page_id') != page
            or target_sha(snapshot, props) != target):
        stop('manual_edit_or_unknown_write')
    full = executor.target_sha(snapshot, ident, source_id)
    blob = git(root, 'rev-parse', 'HEAD:' + path).decode().strip()
    if not re.fullmatch('[a-f0-9]{40}', blob):
        stop('unowned_target')
    adoption = {'contract': 'get-notion-exact-adoption-1', 'source_commit': commit,
                'source_blob': blob, 'initial_target_sha256': target,
                'target_snapshot_sha256': full}
    return owned_receipt(ident, page, data, target, adoption=adoption)


def attempt_from_main(root, ident):
    try:
        value = json.loads(git(root, 'show', 'HEAD:' + ATTEMPTS + '/' + ident + '.json'))
    except (subprocess.CalledProcessError, ValueError):
        stop('attempt_not_reserved')
    if not isinstance(value, dict) or set(value) != {'note_id', 'source_sha256', 'run_id', 'run_attempt', 'status'}:
        stop('attempt_not_reserved')
    return value


def run(root, ident, commit, source_id, token, apply=False, owned=None, journal=None,
        persist_update=None):
    path, data, row = source(root, ident, commit)
    media_assets = media.source_assets(root, commit, ident, data, path, row)
    request = transport.Transport(token)
    api = transport.API(request, source_id, [ident])
    api.uploaded_pngs = {}
    schema = api.schema()
    matches = api.matches(ident)
    if len(matches) > 1:
        stop('duplicate_id')
    if matches:
        page = matches[0]
        if media_assets:
            if owned is None:
                stop('unowned_target')
            validate_owned_page(owned, ident, page)
            if hashlib.sha256(data).hexdigest() != owned['source_sha256']:
                stop('existing_content_differs')
            snapshot = api.read_note(ident)
            existing_media = media.match_existing_images(snapshot, media_assets)
            if existing_media is None:
                stop('existing_content_differs')
            props, blocks = desired(data, ident, path, row, schema,
                                    media_assets=media_assets, media_preview=True)
            snapshot = media.bind_existing_snapshot(snapshot, existing_media)
            blocks = media.bind_existing_blocks(blocks, existing_media)
            target = verify_note(api, ident, page, props, blocks, snapshot=snapshot)
            if owned.get('origin') == 'adopted':
                import get_notion_executor as executor
                if executor.target_sha(snapshot, ident, source_id) != owned['adoption']['target_snapshot_sha256']:
                    stop('manual_edit_or_unknown_write')
                result_owned = owned
            elif owned.get('origin') == 'created':
                result_owned = owned_receipt(ident, page, data, target, created=True)
            else:
                if owned.get('target_sha256') != target:
                    stop('manual_edit_or_unknown_write')
                result_owned = owned
            return {'status': 'already_present', 'page_id': page, 'owned': result_owned}
        props, blocks = desired(data, ident, path, row, schema)
        if owned is not None:
            validate_owned_page(owned, ident, page)
            source_sha = hashlib.sha256(data).hexdigest()
            if source_sha != owned['source_sha256'] or (isinstance(journal, dict)
                    and journal.get('phase') != 'complete'):
                if owned.get('origin') not in ('created', 'adopted'):
                    stop('unowned_target')
                import get_notion_incremental_update as update
                return update.update(root, ident, page, data, props, blocks,
                                     source_id, token, api, owned, journal,
                                     persist_update if apply else None)
        try:
            target = verify_note(api, ident, page, props, blocks)
        except ValueError as exc:
            if str(exc) in ('existing_content_differs', 'existing_properties_differ'):
                raise
            raise
        if owned is not None and owned.get('origin') == 'adopted':
            import get_notion_executor as executor
            if executor.target_sha(api.read_note(ident), ident, source_id) != owned['adoption']['target_snapshot_sha256']:
                stop('manual_edit_or_unknown_write')
        return {'status': 'already_present', 'page_id': page,
                'owned': owned if owned is not None and owned.get('origin') == 'adopted' else
                         owned_receipt(ident, page, data, target,
                                       created=owned is not None and owned.get('origin') == 'created')}
    if media_assets:
        if apply:
            # The controller must first publish upload checkpoints. Read and
            # validate those durable receipts before rendering any write payload.
            receipts = media.receipts_from_main(root, ident, data)
            api.uploaded_pngs = media.upload_handles(media_assets, receipts)
        else:
            receipts = None
        props, blocks = desired(data, ident, path, row, schema,
                                media_assets=media_assets, media_receipts=receipts,
                                media_preview=not apply)
        if not apply:
            return {'status': 'ready', 'source_blob': hashlib.sha256(data).hexdigest()}
        media.bind_uploaded_blocks(blocks, media_assets, receipts)
    else:
        props, blocks = desired(data, ident, path, row, schema)
    if not apply:
        return {'status': 'ready', 'source_blob': hashlib.sha256(data).hexdigest()}
    check_attempt(attempt_from_main(root, ident), ident, data)
    payload = {'parent': {'type': 'data_source_id', 'data_source_id': api.source},
               'properties': props, 'children': transport.wire_blocks(
                   blocks, uploaded_pngs=api.uploaded_pngs)}
    key = transport.fingerprint('POST', '/pages', payload)
    writer = transport.Transport(token, approved_writes=(key,))
    page = transport.API(writer, source_id, [ident], approved_writes=(key,),
                         uploaded_pngs=api.uploaded_pngs).create_page(source_id, ident, props, blocks)
    check = transport.API(transport.Transport(token), source_id, [ident])
    if check.matches(ident) != [page]:
        stop('write_uncertain')
    target = verify_note(check, ident, page, props, blocks,
                         snapshot=media.bind_uploaded_snapshot(check.read_note(ident),
                             media_assets, receipts) if media_assets else None)
    return {'status': 'created', 'page_id': page,
            'owned': owned_receipt(ident, page, data, target, created=True)}


def prepare_media(root, ident, commit, token, persist):
    """Checkpoint verified private Git media before reserving a Notion page create."""
    path, data, row = source(root, ident, commit)
    assets = media.source_assets(root, commit, ident, data, path, row)
    if not assets:
        return {}
    previous = media.receipts_from_main(root, ident, data)
    def checkpoint(receipts):
        persist({'contract': media.CONTRACT, 'note_id': ident,
                 'source_sha256': hashlib.sha256(data).hexdigest(), 'assets': receipts})
    return media.publish_missing(assets, token, previous, checkpoint)


def check_attempt(attempt, ident, data):
    if (attempt.get('note_id') != ident
            or attempt.get('source_sha256') != hashlib.sha256(data).hexdigest()
            or attempt.get('run_id') != os.environ.get('GITHUB_RUN_ID')
            or attempt.get('run_attempt') != os.environ.get('GITHUB_RUN_ATTEMPT')
            or attempt.get('status') != 'attempt_reserved'):
        stop('attempt_not_reserved')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--note-id', required=True)
    parser.add_argument('--data-source-id', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = run(Path(args.source_root), args.note_id, args.source_commit,
                 args.data_source_id, os.environ['NOTION_API_KEY'], args.apply)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
