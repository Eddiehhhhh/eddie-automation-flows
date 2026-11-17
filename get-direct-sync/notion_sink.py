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
from render import render_text

OWNER = 'get-direct-v1'

class Hold(RuntimeError):
    pass

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
    def __init__(self, transport, database_id, journal, *, namespace='default', id_property='Get ID', owner_property='Sync Owner', title_property='Name'):
        self.http, self.database, self.journal = transport, database_id, journal
        if not namespace or len(namespace) > 100 or not all(c.isalnum() or c in '-_' for c in namespace): raise ValueError('invalid_namespace')
        self.owner = OWNER + ':' + namespace
        self.namespace = namespace
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

    def sync(self, snapshot, receipt=None):
        sid, version = snapshot['id'], snapshot['version']
        text = render_text(snapshot)
        chunks = utf16_chunks(text)
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
                    self.titleprop: {'title': rich(utf16_chunks(str(snapshot['note'].get('title') or sid))[0])},
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
        desired_title = utf16_chunks(str(snapshot['note'].get('title') or sid))[0]
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
                if baseline['page_id'] != pid: raise Hold('page_identity_changed')
                old = [b for b in top_blocks if b['id'] == baseline['block_id']]
                expected_label = self.owner + ':' + sid + ':' + baseline['version']
                if len(old) != 1 or old[0].get('type') != 'toggle' or plain(old[0]['toggle']['rich_text']) != expected_label: raise Hold('managed_content_modified')
                if digest(''.join(self.block_text(baseline['block_id']))) != baseline['content_sha256']: raise Hold('managed_content_modified')
        label = self.owner + ':' + sid + ':' + version
        blocks = [b for b in top_blocks if b.get('type') == 'toggle' and plain(b['toggle']['rich_text']) == label]
        if len(blocks) > 1: raise Hold('duplicate_managed_revision')
        if not blocks:
            if state.get('phase') == 'toggle_create_intent' and state.get('version') == version: raise Hold('toggle_create_requires_reconcile')
            state.update(phase='toggle_create_intent', version=version, page_id=pid)
            self.journal.set(key, dict(state))
            result = self.request('PATCH', f'/blocks/{pid}/children', {'children': [{'object':'block','type':'toggle','toggle':{'rich_text':rich(label)}}]})
            block_id = result['results'][0]['id']
            state.update(phase='writing', block_id=block_id)
            self.journal.set(key, dict(state))
        else:
            block_id = blocks[0]['id']
        existing = self.block_text(block_id)
        if existing != chunks[:len(existing)]: raise Hold('managed_content_modified')
        # An uncertain previous append can only be resumed if its exact whole batch
        # is now visible. A missing/partial batch cannot safely be sent twice.
        pending = state.get('pending')
        if pending:
            end = pending['end']
            if len(existing) < end: raise Hold('append_requires_reconcile')
            state.pop('pending', None)
            self.journal.set(key, dict(state))
        offset = len(existing)
        while offset < len(chunks):
            batch = []
            while offset + len(batch) < len(chunks) and len(batch) < 100:
                block = {'object':'block','type':'paragraph','paragraph':{'rich_text':rich(chunks[offset+len(batch)])}}
                candidate = {'children':batch + [block]}
                if len(json.dumps(candidate).encode()) > 400000 and batch: break
                batch.append(block)
            end = offset + len(batch)
            state.update(phase='writing', version=version, page_id=pid, block_id=block_id, pending={'start':offset,'end':end})
            self.journal.set(key, dict(state))
            self.request('PATCH', f'/blocks/{block_id}/children', {'children':batch})
            observed = self.block_text(block_id)
            if observed != chunks[:end]: raise Hold('append_readback_mismatch')
            state.pop('pending', None)
            self.journal.set(key, dict(state))
            offset = end
        if self.block_text(block_id) != chunks: raise Hold('final_readback_mismatch')
        if observed_title != desired_title:
            state.update(version=version, title_pending={'previous':observed_title,'desired':desired_title})
            self.journal.set(key, dict(state))
            self.request('PATCH', '/pages/' + pid, {'properties':{self.titleprop:{'title':rich(desired_title)}}})
            readback_page = self.request('GET', '/pages/' + pid)
            if not readback_page or plain(readback_page['properties'][self.titleprop]['title']) != desired_title: raise Hold('title_readback_mismatch')
        result = {'page_id':pid, 'block_id':block_id, 'version':version, 'content_sha256':digest(text), 'title':desired_title,'content_scope':'text_and_metadata'}
        self.journal.set(key, {'phase':'verified','page_id':pid,'receipt':result})
        return result
