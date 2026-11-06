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


def desired(data, ident, path, row, schema):
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
    blocks, assets, warnings = sink.render_markdown(data, ident, {}, path, {})
    if assets or warnings or not blocks or len(blocks) > 100:
        # shortcut: media and longer notes wait for the existing upload/paging path.
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


def verify_note(api, ident, page, props, blocks):
    snapshot = api.read_note(ident)
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


def owned_receipt(ident, page, data, target):
    return {'note_id': ident, 'page_id': page,
            'source_sha256': hashlib.sha256(data).hexdigest(), 'target_sha256': target}


def attempt_from_main(root, ident):
    try:
        value = json.loads(git(root, 'show', 'HEAD:' + ATTEMPTS + '/' + ident + '.json'))
    except (subprocess.CalledProcessError, ValueError):
        stop('attempt_not_reserved')
    if not isinstance(value, dict) or set(value) != {'note_id', 'source_sha256', 'run_id', 'run_attempt', 'status'}:
        stop('attempt_not_reserved')
    return value


def run(root, ident, commit, source_id, token, apply=False):
    path, data, row = source(root, ident, commit)
    request = transport.Transport(token)
    api = transport.API(request, source_id, [ident])
    schema = api.schema()
    props, blocks = desired(data, ident, path, row, schema)
    matches = api.matches(ident)
    if len(matches) > 1:
        stop('duplicate_id')
    if matches:
        page = matches[0]
        try:
            target = verify_note(api, ident, page, props, blocks)
        except ValueError as exc:
            if str(exc) in ('existing_content_differs', 'existing_properties_differ'):
                raise
            raise
        return {'status': 'already_present', 'page_id': page,
                'owned': owned_receipt(ident, page, data, target)}
    if not apply:
        return {'status': 'ready', 'source_blob': hashlib.sha256(data).hexdigest()}
    check_attempt(attempt_from_main(root, ident), ident, data)
    payload = {'parent': {'type': 'data_source_id', 'data_source_id': api.source},
               'properties': props, 'children': transport.wire_blocks(blocks)}
    key = transport.fingerprint('POST', '/pages', payload)
    writer = transport.Transport(token, approved_writes=(key,))
    page = transport.API(writer, source_id, [ident], approved_writes=(key,)).create_page(source_id, ident, props, blocks)
    check = transport.API(transport.Transport(token), source_id, [ident])
    if check.matches(ident) != [page]:
        stop('write_uncertain')
    target = verify_note(check, ident, page, props, blocks)
    return {'status': 'created', 'page_id': page,
            'owned': owned_receipt(ident, page, data, target)}


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
