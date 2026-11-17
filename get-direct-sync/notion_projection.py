"""Fixed, bounded business-property projection after verified Notion text delivery.

No schema writes, date inference, transcript properties, or ownership adoption.
Values and PATCH intents stay in the private journal; exceptions contain codes.
"""
from copy import deepcopy
import json

from core import Hold
from notion_sink import utf16_chunks
from render import tag_names
from retry import HTTPFailure

FIELDS = {
    '标签': ('tags', 'multi_select'), '主题': ('topics', 'multi_select'),
    '创建时间原值': ('created_at', 'rich_text'), '更新时间原值': ('updated_at', 'rich_text'),
    '内容类型': ('note_type', 'rich_text'), '来源': ('source', 'rich_text'),
    '来源系统': (None, 'rich_text'), '笔记类型': ('note_type', 'select'),
}
NOTE_TYPE_LABELS = {
    'audio': '音频笔记', 'local_audio': '本地音频笔记', 'plain_text': '文字笔记',
    'link': '链接笔记', 'img_text': '图文笔记', 'recorder_audio': '录音卡笔记',
    'recorder_flash_audio': '录音卡闪念笔记',
}
MAX_PAYLOAD = 390000


def _units(value):
    return len(value.encode('utf-16-le')) // 2


def _value(prop, kind):
    if (not isinstance(prop, dict) or prop.get('type', kind) != kind
            or kind not in prop or prop.get('has_more')):
        raise ValueError('target_property_invalid')
    raw = prop[kind]
    if kind == 'rich_text':
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError('target_property_invalid')
        parts = []
        for item in raw:
            if not isinstance(item, dict) or item.get('type', 'text') != 'text':
                raise ValueError('target_property_invalid')
            text = item.get('text', {})
            content = text.get('content') if isinstance(text, dict) else None
            if not isinstance(content, str) or _units(content) > 2000:
                raise ValueError('target_property_invalid')
            annotations = item.get('annotations', {})
            if (not isinstance(annotations, dict) or item.get('href') or text.get('link')
                    or any(annotations.get(k) for k in ('bold', 'italic', 'underline', 'strikethrough', 'code'))
                    or annotations.get('color', 'default') != 'default'):
                raise ValueError('target_property_formatted')
            parts.append(content)
        return ''.join(parts)
    if kind == 'multi_select':
        if (not isinstance(raw, list) or len(raw) > 100
                or any(not isinstance(item, dict) or not isinstance(item.get('name'), str)
                       or not item['name'] for item in raw)):
            raise ValueError('target_property_invalid')
        names = [item['name'] for item in raw]
        if len(set(names)) != len(names):
            raise ValueError('target_property_invalid')
        return sorted(names)
    if kind == 'select':
        if raw is None:
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get('name'), str) or not raw['name']:
            raise ValueError('target_property_invalid')
        return raw['name']
    raise ValueError('target_property_invalid')


def _same(first, second, kind):
    try:
        return _value(first, kind) == _value(second, kind)
    except ValueError:
        return False


def _desired(note, available):
    desired, unprojected = {}, {}
    for name, definition in available.items():
        source, kind = FIELDS[name]
        try:
            if (not isinstance(definition, dict) or definition.get('type') != kind
                    or not isinstance(definition.get('id'), str) or not definition['id']):
                raise ValueError('schema_property_invalid')
            if source is not None and source not in note:
                raise ValueError('source_property_missing')
            raw = 'getnote' if source is None else note[source]
            if kind == 'rich_text':
                if not isinstance(raw, str):
                    raise ValueError('source_property_invalid')
                chunks = utf16_chunks(raw) if raw else []
                if len(chunks) > 100:
                    raise ValueError('property_budget')
                wire = {'rich_text': [{'type': 'text', 'text': {'content': text}} for text in chunks]}
            else:
                config = definition.get(kind)
                options = config.get('options') if isinstance(config, dict) else None
                if (not isinstance(options, list) or any(not isinstance(option, dict)
                        or not isinstance(option.get('name'), str) or not option['name'] for option in options)
                        or len({option['name'] for option in options}) != len(options)):
                    raise ValueError('schema_options_invalid')
                if kind == 'multi_select':
                    try:
                        names = tag_names(raw)
                    except Hold:
                        raise ValueError('source_property_invalid') from None
                    if (len(names) > 100 or len(set(names)) != len(names)
                            or any(name != name.strip() or ',' in name or _units(name) > 100 for name in names)):
                        raise ValueError('source_property_invalid')
                    wire = {'multi_select': [{'name': name} for name in sorted(names)]}
                else:
                    label = NOTE_TYPE_LABELS.get(raw) if isinstance(raw, str) else None
                    if label is None or label not in {option['name'] for option in options}:
                        raise ValueError('note_type_option_unavailable')
                    wire = {'select': {'name': label}}
            if len(json.dumps({'properties': {**desired, name: wire}}, ensure_ascii=False).encode()) > MAX_PAYLOAD:
                raise ValueError('property_budget')
            desired[name] = wire
        except ValueError as error:
            unprojected[name] = str(error)
    return desired, unprojected


class ProjectedNotionSink:
    def __init__(self, text_sink, http, database_id, journal, namespace):
        if (getattr(text_sink, 'database', database_id) != database_id
                or getattr(text_sink, 'namespace', namespace) != namespace):
            raise Hold('metadata_sink_binding_mismatch')
        self.text, self.http, self.database = text_sink, http, database_id
        self.journal, self.namespace = journal, namespace

    def _request(self, method, path, body=None):
        try:
            return self.http.request(method, '/v1' + path, body)
        except HTTPFailure as error:
            raise Hold('metadata_http_' + str(error.status)) from None
        except Exception:
            raise Hold('metadata_transport_uncertain') from None

    def _page(self, pid, sid):
        page = self._request('GET', '/pages/' + pid)
        idprop, ownerprop = getattr(self.text, 'idprop', 'Get ID'), getattr(self.text, 'ownerprop', 'Sync Owner')
        owner = getattr(self.text, 'owner', 'get-direct-v1:' + self.namespace)
        if (not page or page.get('id') != pid or page.get('archived') or page.get('in_trash')
                or page.get('parent', {}).get('database_id', '').replace('-', '') != self.database.replace('-', '')
                or _value(page.get('properties', {}).get(idprop), 'rich_text') != sid
                or _value(page['properties'].get(ownerprop), 'rich_text') != owner):
            raise Hold('metadata_page_identity_mismatch')
        return page['properties']

    def sync(self, snapshot, receipt=None):
        sid, version = snapshot['id'], snapshot['version']
        key = 'projection:notion:' + self.database + ':' + self.namespace + ':' + sid
        state = self.journal.get(key) or {}
        if state.get('pending') and state['pending'].get('version') != version:
            raise Hold('metadata_unresolved_prior_revision')
        text = self.text.sync(snapshot, receipt)
        if text.get('version') != version or not isinstance(text.get('page_id'), str):
            raise Hold('metadata_text_receipt_invalid')
        pid = text['page_id']
        if state and state.get('page_id') != pid:
            raise Hold('metadata_page_identity_mismatch')
        database = self._request('GET', '/databases/' + self.database)
        if (not database or database.get('object') != 'database'
                or database.get('id', '').replace('-', '') != self.database.replace('-', '')
                or database.get('archived') or database.get('in_trash')
                or not isinstance(database.get('properties'), dict)):
            raise Hold('metadata_schema_unavailable')
        available = {name: database['properties'][name] for name in FIELDS if name in database['properties']}
        if not available:
            if state.get('pending'):
                raise Hold('metadata_schema_changed')
            return text
        desired, unprojected = _desired(snapshot['note'], available)
        current = self._page(pid, sid)
        managed = deepcopy(state.get('managed', {}))
        pending = state.get('pending')
        if pending:
            if (pending.get('page_id') != pid or any(name not in available
                    or not isinstance(available[name], dict)
                    or available[name].get('id') != pending['property_ids'].get(name)
                    or not _same(current.get(name), value, FIELDS[name][1])
                    for name, value in pending['patch'].items())):
                raise Hold('metadata_write_requires_reconcile')
            managed.update(pending['managed_after'])
            state = {'page_id': pid, 'managed': managed}
            self.journal.set(key, state)
        patch, managed_after, conflicts, observed = {}, {}, {}, []
        for name, proposed in desired.items():
            kind = FIELDS[name][1]
            try:
                actual, wanted = _value(current.get(name), kind), _value(proposed, kind)
                baseline = managed.get(name)
                if baseline is not None and (baseline.get('property_id') != available[name]['id']
                        or baseline.get('kind') != kind):
                    raise ValueError('managed_property_schema_changed')
                if actual == wanted:
                    observed.append(name)
                    continue
                if baseline is not None:
                    if actual != baseline.get('value'):
                        raise ValueError('property_manual_edit')
                elif actual not in ('', [], None):
                    raise ValueError('property_unowned_value')
                elif wanted in ('', [], None):
                    continue
                patch[name] = proposed
                managed_after[name] = {'kind': kind, 'property_id': available[name]['id'], 'value': wanted}
            except ValueError as error:
                conflicts[name] = str(error)
        if patch:
            pending = {'version': version, 'page_id': pid, 'patch': patch,
                       'property_ids': {name: available[name]['id'] for name in patch},
                       'managed_after': managed_after}
            self.journal.set(key, {'page_id': pid, 'managed': managed, 'pending': pending})
            fresh = self._page(pid, sid)
            if any(not _same(fresh.get(name), current.get(name), FIELDS[name][1]) for name in patch):
                self.journal.set(key, {'page_id': pid, 'managed': managed,
                                       'conflicts': {name: 'property_changed_before_write' for name in patch}})
                raise Hold('metadata_property_conflict')
            failure = None
            try:
                self._request('PATCH', '/pages/' + pid, {'properties': patch})
            except Hold as error:
                failure = str(error)  # Code only; never retain response bodies or URLs.
            checked = self._page(pid, sid)
            if any(not _same(checked.get(name), value, FIELDS[name][1]) for name, value in patch.items()):
                self.journal.set(key, {'page_id': pid, 'managed': managed, 'pending': pending,
                                       'reason': failure or 'metadata_readback_mismatch'})
                raise Hold('metadata_write_requires_reconcile')
            managed.update(managed_after)
        projection = {'status': 'partial' if conflicts or unprojected else 'verified',
                      'managed_fields': sorted(managed), 'observed_fields': sorted(observed),
                      'unprojected': unprojected, 'conflicts': conflicts}
        self.journal.set(key, {'page_id': pid, 'managed': managed, 'projection': projection})
        if conflicts:
            raise Hold('metadata_property_conflict')
        return {**text, 'projection': projection}
