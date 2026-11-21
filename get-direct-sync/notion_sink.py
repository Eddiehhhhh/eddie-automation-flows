"""Conservative Notion adapter. Requires a durable journal and exclusive writer lease.
Transport request(method, '/v1/...', body=None) must pin Notion-Version 2022-06-28,
reject redirects, redact failures, and never automatically retry writes.
No deletion: prior verified revisions remain collapsed as history. Attachments are
preserved by renderer as metadata/URLs, not falsely claimed as binary backups.
"""
import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from core import Hold
from render import render_text, source_title
import notion_native as native

OWNER = 'get-direct-v1'

def rich(s):
    return [{'type': 'text', 'text': {'content': s}}]

def plain(items):
    return ''.join(x.get('plain_text', x.get('text', {}).get('content', '')) for x in items)

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()

def utf16_chunks(text, limit=2000):
    chunks, current, units = [], [], 0
    for char in text:
        n = len(char.encode('utf-16-le')) // 2
        if units + n > limit:
            chunks.append(''.join(current)); current, units = [], 0
        current.append(char); units += n
    if current or not chunks: chunks.append(''.join(current))
    return chunks

class NotionSink:
    def __init__(self, transport, database_id, journal, *, namespace='default', id_property='Get ID', owner_property='Sync Owner', title_property='Name', source_base=None):
        self.http, self.database, self.journal = transport, database_id, journal
        if not namespace or len(namespace) > 100 or not all(c.isalnum() or c in '-_' for c in namespace): raise ValueError('invalid_namespace')
        self.owner = OWNER + ':' + namespace
        self.namespace = namespace
        self.source_base = source_base
        self.idprop, self.ownerprop, self.titleprop = id_property, owner_property, title_property

    def request(self, method, path, body=None):
        return self.http.request(method, '/v1' + path, body)

    def children(self, block):
        out, cursor, seen = [], None, set()
        while True:
            q = {'page_size': 100}
            if cursor: q['start_cursor'] = cursor
            result = self.request('GET', f'/blocks/{block}/children?' + urlencode(q))
            out.extend(result['results'])
            if not result.get('has_more'): return out
            cursor = result.get('next_cursor')
            if not cursor or cursor in seen: raise Hold('invalid_block_pagination')
            seen.add(cursor)

    def preflight(self):
        database = self.request('GET', '/databases/' + self.database)
        if not database or database.get('object') != 'database': raise Hold('notion_database_unavailable')
        if database.get('id', '').replace('-', '') != self.database.replace('-', ''): raise Hold('notion_database_identity_mismatch')
        if database.get('archived') or database.get('in_trash'): raise Hold('notion_database_not_live')
        sources = database.get('data_sources')
        if sources is not None and (not isinstance(sources, list) or len(sources) > 1): raise Hold('notion_multiple_data_sources_unsupported')
        properties = database.get('properties')
        if not isinstance(properties, dict): raise Hold('notion_database_schema_unavailable')
        if len({self.idprop, self.ownerprop, self.titleprop}) != 3: raise Hold('notion_properties_must_be_distinct')
        for name, expected_type in ((self.idprop, 'rich_text'), (self.ownerprop, 'rich_text'), (self.titleprop, 'title')):
            prop = properties.get(name)
            if not isinstance(prop, dict) or prop.get('type') != expected_type:
                raise Hold('notion_database_property_type_mismatch')

    def pages(self, source_id):
        body = {'filter': {'property': self.idprop, 'rich_text': {'equals': source_id}}, 'page_size': 100}
        result = self.request('POST', f'/databases/{self.database}/query', body)
        if result.get('has_more') or len(result['results']) > 1:
            # Separate durable evidence: never overwrite a pending write intent/receipt.
            self.journal.set('conflict:notion:'+self.database+':'+self.namespace+':'+source_id, {
                'reason':'duplicate_source_pages','observed_count':len(result['results']),
                'additional_results':bool(result.get('has_more')),
                'observed_at':datetime.now(timezone.utc).isoformat(),
                'disposition':'held_no_target_write','automatic_resolution':False})
            raise Hold('duplicate_source_pages')
        return result['results']

    def block_text(self, block_id):
        chunks = []
        for b in self.children(block_id):
            if b.get('type') != 'paragraph' or b.get('has_children') or b['paragraph'].get('color','default') != 'default': raise Hold('managed_content_modified')
            # Reject formatting edits too; do not erase/rewrite human annotations.
            for item in b['paragraph']['rich_text']:
                if item.get('href') or item.get('text', {}).get('link'): raise Hold('managed_content_modified')
                a = item.get('annotations', {})
                if any(a.get(k) for k in ('bold','italic','strikethrough','underline','code')) or a.get('color','default') != 'default': raise Hold('managed_content_modified')
            chunks.append(plain(b['paragraph']['rich_text']))
        return chunks

    def origin(self, sid, version):
        return native.source_url(self.source_base, self.namespace, sid, version)

    @staticmethod
    def display_title(label, url):
        return rich(label + ' · ') + native.text_items('源文件', utf16_chunks, url=url)

    def legacy_header(self, sid, version):
        return self.display_title('历史正文', self.origin(sid, version))

    def section_header(self, sid, version, section):
        url = self.origin(sid, version)
        if section['kind'] == 'heading_1':
            return native.text_items(section['label'], utf16_chunks, url=url)
        return self.display_title(section['label'], url)

    def verify_text_receipt(self, top_blocks, sid, receipt):
        if receipt.get('format') == native.FORMAT:
            sections = receipt['sections']
            expected_ids = [section['block_id'] for section in sections]
            actual_ids = [block['id'] for block in top_blocks if block['id'] in expected_ids]
            if not sections or expected_ids[0] != receipt['block_id'] or actual_ids != expected_ids:
                raise Hold('managed_content_modified')
            primary = None
            for section in sections:
                matches = [block for block in top_blocks if block['id'] == section['block_id']]
                if (len(matches) != 1 or not self.matches_container(matches[0], section['kind'],
                        self.section_header(sid, receipt['version'], section))
                        or native.body_digest(native.read_blocks(self.children(section['block_id'])))
                        != section['presentation_sha256']):
                    raise Hold('managed_content_modified')
                if primary is None:
                    primary = matches[0]
            return primary

        matches = [block for block in top_blocks if block['id'] == receipt['block_id']]
        label = self.owner + ':' + sid + ':' + receipt['version']
        headers = [rich(label)]
        if self.source_base:
            headers.append(self.legacy_header(sid, receipt['version']))
        if (len(matches) != 1 or not any(self.matches_container(matches[0], 'toggle', header) for header in headers)
                or digest(''.join(self.block_text(receipt['block_id']))) != receipt['content_sha256']):
            raise Hold('managed_content_modified')
        return matches[0]

    @staticmethod
    def matches_container(block, kind, header):
        payload = block.get(kind, {})
        return (block.get('type') == kind and not block.get('archived') and not block.get('in_trash')
                and payload.get('color', 'default') == 'default'
                and (kind != 'heading_1' or payload.get('is_toggleable') is True)
                and native.matches_title(payload.get('rich_text', []), header))

    def write_body(self, pid, version, role, kind, headers, body, wanted, read, state, key,
                   known_id=None, native_format=False):
        active = state.get('version') == version and state.get('active_section', 'body') == role
        blocks = [block for block in self.children(pid)
                  if any(self.matches_container(block, kind, header) for header in headers)]
        if len(blocks) > 1:
            raise Hold('duplicate_managed_revision')
        expected = known_id or (state.get('block_id') if active and state.get('phase') == 'writing' else None)
        if expected and (not blocks or blocks[0]['id'] != expected):
            raise Hold('managed_content_modified')
        if not blocks:
            if active and state.get('phase') == 'toggle_create_intent':
                raise Hold('toggle_create_requires_reconcile')
            state.update(phase='toggle_create_intent', version=version, page_id=pid, active_section=role)
            if native_format:
                state['format'] = native.FORMAT
            self.journal.set(key, dict(state))
            payload = {'rich_text': headers[0]}
            if kind == 'heading_1':
                payload['is_toggleable'] = True
            result = self.request('PATCH', f'/blocks/{pid}/children',
                                  {'children': [{'object': 'block', 'type': kind, kind: payload}]})
            block_id = result['results'][0]['id']
            state.update(phase='writing', block_id=block_id)
            self.journal.set(key, dict(state))
        else:
            block_id = blocks[0]['id']
        existing = read(block_id)
        if existing != wanted[:len(existing)]:
            raise Hold('managed_content_modified')
        pending = state.get('pending') if active else None
        if pending:
            if len(existing) < pending['end']:
                raise Hold('append_requires_reconcile')
            state.pop('pending', None)
            self.journal.set(key, dict(state))
        offset = len(existing)
        while offset < len(body):
            batch = []
            while offset + len(batch) < len(body) and len(batch) < 100:
                block = body[offset + len(batch)]
                if len(json.dumps({'children': batch + [block]}).encode()) > native.MAX_APPEND_BYTES:
                    if not batch:
                        raise Hold('notion_block_exceeds_safe_request_size')
                    break
                batch.append(block)
            end = offset + len(batch)
            state.update(phase='writing', version=version, page_id=pid, block_id=block_id,
                         active_section=role, pending={'start': offset, 'end': end})
            if native_format:
                state['format'] = native.FORMAT
            self.journal.set(key, dict(state))
            self.request('PATCH', f'/blocks/{block_id}/children', {'children': batch})
            if read(block_id) != wanted[:end]:
                raise Hold('append_readback_mismatch')
            state.pop('pending', None)
            self.journal.set(key, dict(state))
            offset = end
        if read(block_id) != wanted:
            raise Hold('final_readback_mismatch')
        return block_id

    def hide_legacy_label(self, pid, sid, previous, state, key):
        if not previous or previous.get('format') == native.FORMAT:
            return
        block = self.verify_text_receipt(self.children(pid), sid, previous)
        header = self.legacy_header(sid, previous['version'])
        if native.read_rich(block['toggle']['rich_text']) == native.read_rich(header):
            return
        if state.get('legacy_label_intent'):
            raise Hold('legacy_label_requires_reconcile')
        state['legacy_label_intent'] = block['id']
        self.journal.set(key, dict(state))
        self.request('PATCH', '/blocks/' + block['id'], {'toggle': {'rich_text': header}})
        block = self.verify_text_receipt(self.children(pid), sid, previous)
        if native.read_rich(block['toggle']['rich_text']) != native.read_rich(header):
            raise Hold('legacy_label_requires_reconcile')

    def sync(self, snapshot, receipt=None):
        sid, version = snapshot['id'], snapshot['version']
        text = render_text(snapshot)
        key = 'notion:' + self.database + ':' + self.namespace + ':' + sid
        state = self.journal.get(key) or {}
        if (state.get('pending') or state.get('title_pending') or state.get('phase') in ('page_create_intent', 'toggle_create_intent', 'writing', 'page_known')) and state.get('version') != version:
            raise Hold('unresolved_prior_revision')
        self.preflight()
        pages = self.pages(sid)
        if not pages:
            if receipt or state: raise Hold('page_create_or_missing_requires_reconcile')
            # Must commit before the potentially ambiguous external create.
            state = {'phase': 'page_create_intent', 'version': version}
            self.journal.set(key, state)
            page = self.request('POST', '/pages', {
                'parent': {'database_id': self.database},
                'properties': {
                    self.idprop: {'rich_text': rich(sid)},
                    self.ownerprop: {'rich_text': rich(self.owner)},
                    self.titleprop: {'title': rich(utf16_chunks(source_title(snapshot))[0])},
                }})
            state = {'phase': 'page_known', 'page_id': page['id'], 'version': version}
            self.journal.set(key, state)
        else:
            page = pages[0]
            if not state and not receipt: raise Hold('unknown_existing_page_ownership')
        pid = page['id']
        page = self.request('GET', '/pages/' + pid)
        if not page or page.get('archived') or page.get('in_trash'): raise Hold('page_not_live')
        props = page['properties']
        if plain(props.get(self.idprop, {}).get('rich_text', [])) != sid or plain(props.get(self.ownerprop, {}).get('rich_text', [])) != self.owner:
            raise Hold('page_ownership_mismatch')
        parent = page.get('parent', {})
        if parent.get('database_id', '').replace('-', '') != self.database.replace('-', ''): raise Hold('page_parent_mismatch')
        desired_title = utf16_chunks(source_title(snapshot))[0]
        observed_title = plain(props.get(self.titleprop, {}).get('title', []))
        journal_receipt = state.get('receipt') if state.get('phase') == 'verified' else None
        if journal_receipt and receipt and journal_receipt['page_id'] != receipt['page_id']: raise Hold('page_identity_changed')
        baseline_receipt = journal_receipt or receipt or state.get('receipt')
        title_pending = state.get('title_pending')
        if title_pending:
            if observed_title != title_pending['desired']: raise Hold('title_write_requires_reconcile')
        elif baseline_receipt:
            if 'title' not in baseline_receipt: raise Hold('title_baseline_missing')
            if observed_title != baseline_receipt['title']: raise Hold('managed_title_modified')
        elif observed_title != desired_title:
            raise Hold('managed_title_modified')
        top_blocks = self.children(pid)
        for baseline in (receipt, state.get('receipt')):
            if baseline:
                if baseline['page_id'] != pid:
                    raise Hold('page_identity_changed')
                self.verify_text_receipt(top_blocks, sid, baseline)
        same_version = baseline_receipt if baseline_receipt and baseline_receipt['version'] == version else None
        partial = state.get('version') == version and state.get('phase') in ('writing', 'toggle_create_intent')
        native_format = self.source_base and (
            same_version.get('format') == native.FORMAT if same_version else
            state.get('format') == native.FORMAT if partial else True)
        if native_format:
            prior_sections = same_version['sections'] if same_version else (
                state.get('native_sections', []) if partial else [])
            prior = {section['role']: section for section in prior_sections}
            sections = []
            rendered = native.sections(snapshot, utf16_chunks)
            if same_version and set(prior) != {role for role, _, _, _ in rendered}:
                raise Hold('managed_content_modified')
            for role, title, kind, body in rendered:
                label = title or 'Get 正文'
                wanted = native.read_blocks(body)
                section = {'role': role, 'label': label, 'kind': kind,
                           'presentation_sha256': native.body_digest(wanted)}
                previous = prior.get(role) or {}
                if previous and any(previous.get(key) != value for key, value in section.items()):
                    raise Hold('managed_content_modified')
                section['block_id'] = self.write_body(pid, version, role, kind,
                    [self.section_header(sid, version, section)], body, wanted,
                    lambda block_id: native.read_blocks(self.children(block_id)), state, key,
                    known_id=previous.get('block_id'), native_format=True)
                sections.append(section)
                if not same_version and not previous:
                    state['native_sections'] = sections.copy()
                    self.journal.set(key, dict(state))
            if baseline_receipt and baseline_receipt['version'] != version:
                self.hide_legacy_label(pid, sid, baseline_receipt, state, key)
            block_id = sections[0]['block_id']
        else:
            label = self.owner + ':' + sid + ':' + version
            headers = [rich(label)]
            if same_version and self.source_base:
                headers.append(self.legacy_header(sid, version))
            wanted = utf16_chunks(text)
            body = [{'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': rich(chunk)}}
                    for chunk in wanted]
            block_id = self.write_body(pid, version, 'body', 'toggle', headers, body, wanted,
                                       self.block_text, state, key,
                                       known_id=same_version['block_id'] if same_version else None)
        if observed_title != desired_title:
            state.update(version=version, title_pending={'previous':observed_title,'desired':desired_title})
            self.journal.set(key, dict(state))
            self.request('PATCH', '/pages/' + pid, {'properties':{self.titleprop:{'title':rich(desired_title)}}})
            readback_page = self.request('GET', '/pages/' + pid)
            if not readback_page or plain(readback_page['properties'][self.titleprop]['title']) != desired_title: raise Hold('title_readback_mismatch')
        result = {'page_id':pid, 'block_id':block_id, 'version':version, 'content_sha256':digest(text), 'title':desired_title,'content_scope':'text_and_metadata'}
        if native_format:
            result.update(format=native.FORMAT, sections=sections)
        self.journal.set(key, {'phase':'verified','page_id':pid,'receipt':result})
        return result
