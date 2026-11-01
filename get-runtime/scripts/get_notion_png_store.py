"""Three pinned PNGs in the existing outbox CAS. No transport or ref writes.

The evidence SHA and prepared-plan approval are caller-supplied external trust,
not authorship inferred by hashing this envelope. Native attachment candidates
come only from the reviewed fresh byte/block/page readback adapter. This module
joins those candidates to the exact new engine receipt in its SAME outer CAS.
"""
from copy import deepcopy

import get_notion_png_upload as upload
import get_notion_sink_plan as p

CONTRACT = 'get-notion-three-png-store-1'
EVIDENCE_CONTRACT = 'get-notion-three-png-evidence-1'
MAX_BYTES = 32 * 1024
MAX_UPLOADS = 3
PREFIX = '.sync/get-notion/assets/'
ASSET_KEYS = ('png_assets', 'png_assets_seq2')
ATTACHMENT_KEYS = {'note_id', 'page_id', 'block_id', 'upload_id', 'receipt_sha256',
    'asset_path', 'git_blob', 'sha256', 'byte_count', 'mime', 'readback_verified',
    'source_commit', 'md_blob', 'md_sha256', 'reference_sha256', 'evidence_sha256',
    'data_source_id', 'managed_hash'}
encoded = upload.encoded
digest = upload.digest


class Blocked(ValueError): pass

def fail(reason): raise Blocked(reason) from None

def keys(value, wanted):
    if not isinstance(value, dict) or set(value) != set(wanted): fail('png_state_invalid')

def same(a, b): return encoded(a) == encoded(b)

def asset_key(anchor):
    if not isinstance(anchor, dict): fail('png_evidence_invalid')
    path = anchor.get('manifest_path')
    if path == upload.LEGACY_MANIFEST: return 'png_assets'
    if path == upload.SEQ2_MANIFEST: return 'png_assets_seq2'
    fail('png_evidence_invalid')


def checked_key(key):
    if key not in ASSET_KEYS: fail('png_evidence_invalid')
    return key


def check_outer_assets(outer):
    seen = {name: set() for name in ('note_id', 'upload_id', 'page_id', 'block_id')}
    for key in ASSET_KEYS:
        if key not in outer: continue
        assets = outer[key]
        check_container(assets, outer['engine_state'])
        if asset_key(assets['evidence_anchor']) != key: fail('png_evidence_invalid')
        if key != 'png_assets' and 'expired_recovery' in assets: fail('png_state_invalid')
        for ident, row in assets['uploads'].items():
            if ident in seen['note_id']: fail('png_state_invalid')
            seen['note_id'].add(ident)
            uid = row['state']['upload_id']
            if uid is not None:
                if uid in seen['upload_id']: fail('png_state_invalid')
                seen['upload_id'].add(uid)
        for row in assets['attachments'].values():
            for field in ('page_id', 'block_id'):
                if row[field] in seen[field]: fail('png_attachment_invalid')
                seen[field].add(row[field])


def check_anchor(anchor, approved_sha):
    keys(anchor, {'contract', 'repository', 'publication_commit', 'asset_commit',
                  'manifest_path', 'manifest_blob', 'manifest_sha256'})
    upload.valid_hash(approved_sha)
    if (digest(anchor) != approved_sha or anchor['contract'] != EVIDENCE_CONTRACT
            or anchor['repository'] != p.REPOSITORY
            or anchor['manifest_path'] not in (upload.LEGACY_MANIFEST, upload.SEQ2_MANIFEST)):
        fail('png_evidence_invalid')
    for key in ('publication_commit', 'asset_commit', 'manifest_blob'):
        upload.valid_hash(anchor[key], 40)
    upload.valid_hash(anchor['manifest_sha256'])


def check_plan(plan, approved_sha, anchor):
    """Static pinned schema; kernel.prepare separately checks actual PNG bytes."""
    keys(plan, {'contract', 'binding', 'filename', 'version', 'asset_tree_sha256',
                'multipart_body_sha256'})
    upload.valid_hash(approved_sha)
    if digest(plan) != approved_sha: fail('png_plan_changed')
    b = plan['binding']
    keys(b, {'note_id', 'md_sha256', 'asset_path', 'git_blob', 'sha256', 'bytes',
             'mime', 'data_source_id', 'source_commit', 'reference_sha256'})
    p.note_id(b['note_id'])
    pin = upload.pins_for_manifest(anchor['manifest_path']).get(upload.sha(b['note_id'].encode()))
    if pin is None or any(b.get(k) != v for k, v in pin.items()): fail('png_plan_changed')
    if (type(b['bytes']) is not int or not 0 < b['bytes'] <= upload.byte_limit(upload.sha(b['note_id'].encode()))
            or b['mime'] != 'image/png' or b['asset_path'] != PREFIX + b['sha256'] + '.png'
            or b['source_commit'] != anchor['publication_commit']
            or upload.uuid(b['data_source_id']) != b['data_source_id']
            or upload.sha(b['data_source_id'].encode()) != upload.DESTINATION_PIN):
        fail('png_plan_changed')
    for key in ('md_sha256', 'sha256', 'reference_sha256'): upload.valid_hash(b[key])
    upload.valid_hash(b['git_blob'], 40)
    upload.valid_hash(b['source_commit'], 40)
    upload.valid_hash(plan['multipart_body_sha256'])
    tree = {PREFIX + row['sha256'] + '.png': row['git_blob'] for row in upload.pins_for_manifest(anchor['manifest_path']).values()}
    if (plan['contract'] != 'three-png-upload-1' or plan['version'] != upload.VERSION
            or plan['filename'] != 'get-' + b['sha256'] + '.png'
            or plan['asset_tree_sha256'] != digest(tree)):
        fail('png_plan_changed')


def receipt_for(row):
    """Exact kernel receipt, reconstructed only from its durable verified phase."""
    state = upload.check_state(row['state'], row['plan'])
    if state['phase'] != 'uploaded_verified': fail('png_upload_unverified')
    b = row['plan']['binding']
    return {'upload_id': state['upload_id'], 'plan_sha256': row['plan_sha256'],
        'source_binding': deepcopy(b), 'content_length': b['bytes'],
        'content_type': 'image/png', 'native_bytes_verified': False,
        'binding_sha256': digest(b), 'kind': 'notion_uploaded_png',
        'sha256': b['sha256'], 'byte_count': b['bytes'], 'mime': 'image/png'}


def check_attachment(value, ident, assets, engine):
    keys(value, ATTACHMENT_KEYS)
    row = assets['uploads'].get(ident)
    if row is None: fail('png_attachment_invalid')
    receipt = receipt_for(row); b = row['plan']['binding']
    expected = {'note_id': ident, 'upload_id': receipt['upload_id'],
        'receipt_sha256': digest(receipt), 'asset_path': b['asset_path'],
        'git_blob': b['git_blob'], 'sha256': b['sha256'], 'byte_count': b['bytes'],
        'mime': 'image/png', 'readback_verified': True, 'source_commit': b['source_commit'],
        'md_sha256': b['md_sha256'], 'reference_sha256': b['reference_sha256'],
        'evidence_sha256': assets['evidence_sha256'], 'data_source_id': b['data_source_id']}
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items()):
        fail('png_attachment_invalid')
    for key in ('page_id', 'block_id'):
        if upload.uuid(value[key]) != value[key]: fail('png_attachment_invalid')
    upload.valid_hash(value['md_blob'], 40); upload.valid_hash(value['managed_hash'])
    owned = engine['receipts'].get(ident)
    if (not isinstance(owned, dict) or owned.get('readback_verified') is not True
            or any(owned.get(k) != value[k] for k in ('note_id', 'page_id', 'data_source_id', 'source_commit', 'md_blob'))
            or {'id': value['block_id'], 'normalized_hash': value['managed_hash']} not in owned.get('managed_blocks', [])):
        fail('png_attachment_invalid')
    return value


def check_container(value, engine):
    try:
        keys(value, {'contract', 'evidence_anchor', 'evidence_sha256', 'uploads', 'attachments'} | ({'expired_recovery'} if 'expired_recovery' in value else set()))
        if value['contract'] != CONTRACT: fail('png_state_invalid')
        check_anchor(value['evidence_anchor'], value['evidence_sha256'])
        uploads = value['uploads']; attachments = value['attachments']
        if (not isinstance(uploads, dict) or not isinstance(attachments, dict)
                or len(uploads) > MAX_UPLOADS or len(attachments) > MAX_UPLOADS):
            fail('png_state_invalid')
        ids = set()
        for ident, row in uploads.items():
            keys(row, {'plan', 'plan_sha256', 'state'})
            check_plan(row['plan'], row['plan_sha256'], value['evidence_anchor'])
            if row['plan']['binding']['note_id'] != ident: fail('png_state_invalid')
            state = upload.check_state(row['state'], row['plan'])
            if state['phase'] == 'uploaded_verified' and state.get('write_http', {}).get('status') in (401, 403): fail('png_state_invalid')
            uid = state['upload_id']
            if uid is not None:
                if upload.uuid(uid) != uid or uid in ids: fail('png_state_invalid')
                ids.add(uid)
        pages = set(); blocks = set()
        for ident, row in attachments.items():
            check_attachment(row, ident, value, engine)
            if row['page_id'] in pages or row['block_id'] in blocks: fail('png_attachment_invalid')
            pages.add(row['page_id']); blocks.add(row['block_id'])
        if any(ident in engine['receipts'] and ident not in attachments for ident in uploads):
            fail('png_attachment_missing')
        import get_notion_png_expired_recovery as expired
        expired.check_record(value, engine)
        if len(encoded(value)) > MAX_BYTES: fail('png_state_budget')
        return value
    except Blocked: raise
    except Exception: fail('png_state_invalid')


def initialize(outer, evidence_anchor, approved_evidence_sha):
    """Pure explicit opt-in; never rewrites an old envelope on ordinary reads."""
    import get_notion_outbox as outbox
    outbox.check_state(outer); check_anchor(evidence_anchor, approved_evidence_sha)
    key = asset_key(evidence_anchor)
    if key in outer:
        assets = outer[key]
        if not same(assets['evidence_anchor'], evidence_anchor) or assets['evidence_sha256'] != approved_evidence_sha:
            fail('png_evidence_invalid')
        return deepcopy(outer)
    result = deepcopy(outer)
    result[key] = {'contract': CONTRACT, 'evidence_anchor': deepcopy(evidence_anchor),
        'evidence_sha256': approved_evidence_sha, 'uploads': {}, 'attachments': {}}
    outbox.check_state(result)
    return result


def add_upload(outer, plan, approved_plan_sha, *, asset_key=None):
    import get_notion_outbox as outbox
    outbox.check_state(outer)
    if asset_key is None:
        binding = plan.get('binding', {}) if isinstance(plan, dict) else {}
        eligible = [key for key in ASSET_KEYS if key in outer
                    and outer[key]['evidence_anchor']['publication_commit'] == binding.get('source_commit')
                    and upload.sha(str(binding.get('note_id')).encode()) in upload.pins_for_manifest(outer[key]['evidence_anchor']['manifest_path'])]
        if len(eligible) != 1: fail('png_not_initialized')
        asset_key = eligible[0]
    key = checked_key(asset_key)
    if key not in outer: fail('png_not_initialized')
    assets = outer[key]; check_plan(plan, approved_plan_sha, assets['evidence_anchor'])
    ident = plan['binding']['note_id']; result = deepcopy(outer)
    row = {'plan': deepcopy(plan), 'plan_sha256': approved_plan_sha, 'state': upload.initial_state(plan)}
    prior = assets['uploads'].get(ident)
    if prior is not None:
        if prior['plan_sha256'] != approved_plan_sha or not same(prior['plan'], plan): fail('png_plan_changed')
        return result
    result[key]['uploads'][ident] = row
    outbox.check_state(result); preserve(outer, result)
    return result


def preserve(before, after):
    """Durable anti-rearm/rebind guard, shared by both adapters and Git CAS."""
    import get_notion_png_expired_recovery as expired
    expired.preserve(before, after)
    for key in ASSET_KEYS:
        _preserve_container(before, after, key)
    check_outer_assets(after)


def _preserve_container(before, after, key):
    old = before.get(key); new = after.get(key)
    if new is None:
        if old is not None: fail('png_state_regressed')
        return
    check_container(new, after['engine_state'])
    if old is None:
        if new['attachments'] or any(row['state'] != upload.initial_state(row['plan']) for row in new['uploads'].values()):
            fail('png_state_regressed')
        return
    check_container(old, before['engine_state'])
    if any(not same(old[k], new[k]) for k in ('contract', 'evidence_anchor', 'evidence_sha256')):
        fail('png_state_regressed')
    for ident, row in old['uploads'].items():
        nxt = new['uploads'].get(ident)
        if nxt is None or any(not same(row[k], nxt[k]) for k in ('plan', 'plan_sha256')): fail('png_state_regressed')
        a, b = row['state'], nxt['state']
        allowed = {'prepared': {'prepared', 'create_in_flight'},
            'create_in_flight': {'create_in_flight', 'upload_id_saved'},
            'upload_id_saved': {'upload_id_saved', 'send_in_flight'},
            'send_in_flight': {'send_in_flight', 'uploaded_verified'},
            'uploaded_verified': {'uploaded_verified'}}
        if b['phase'] not in allowed[a['phase']]: fail('png_state_regressed')
        if a['upload_id'] is not None and a['upload_id'] != b['upload_id']: fail('png_state_regressed')
        if 'write_http' in a and (b.get('write_http') != a['write_http']
                or a['phase'] == 'create_in_flight' and b['phase'] != a['phase']): fail('png_state_regressed')
        if (a['phase'] == 'uploaded_verified' or a.get('write_http', {}).get('status') in (401, 403)) and not same(a, b): fail('png_state_regressed')
        if a['phase'] == b['phase']:
            if not same({k: v for k, v in a.items() if k != 'write_http'},
                        {k: v for k, v in b.items() if k != 'write_http'}): fail('png_state_regressed')
        elif a['phase'] == 'upload_id_saved' and a['expiry_time'] != b['expiry_time']:
            fail('png_state_regressed')
    for ident in set(new['uploads']) - set(old['uploads']):
        row = new['uploads'][ident]
        if row['state'] != upload.initial_state(row['plan']): fail('png_state_regressed')
    for ident, row in old['attachments'].items():
        if ident not in new['attachments'] or not same(row, new['attachments'][ident]): fail('png_state_regressed')
    for ident in set(new['attachments']) - set(old['attachments']):
        # Attachment cannot be retrofitted in a separate CAS after dequeue.
        if same(before['engine_state']['receipts'].get(ident), after['engine_state']['receipts'].get(ident)):
            fail('png_attachment_not_atomic')


def join_engine(outer_before, outer_after, candidates):
    """Join one selected cohort's byte-readback candidates in the engine CAS."""
    if not isinstance(candidates, dict) or len(candidates) > MAX_UPLOADS: fail('png_attachment_invalid')
    before = outer_before['engine_state']['receipts']; after = outer_after['engine_state']['receipts']
    changed_all = set()
    for key in ASSET_KEYS:
        assets = outer_after.get(key)
        if assets is None: continue
        changed = {ident for ident in assets['uploads'] if ident in after and not same(before.get(ident), after[ident])}
        changed_all.update(changed)
        for ident in changed:
            candidate = candidates.get(ident, assets['attachments'].get(ident))
            if candidate is None: fail('png_attachment_missing')
            check_attachment(candidate, ident, assets, outer_after['engine_state'])
            pending = outer_before['pending_by_id'].get(ident)
            if pending is not None and any(pending[k] != candidate[v] for k, v in
                    (('commit', 'source_commit'), ('blob', 'md_blob'), ('sha256', 'md_sha256'))):
                fail('png_attachment_invalid')
            assets['attachments'][ident] = deepcopy(candidate)
    if set(candidates) - changed_all: fail('png_attachment_invalid')
    preserve(outer_before, outer_after)


def not_dispatched_ticket(before, after):
    """Only called AFTER an exact ordinary forward CAS was fully confirmed."""
    changed = []
    for key in ASSET_KEYS:
        if key not in before or key not in after: continue
        for ident, row in before[key]['uploads'].items():
            nxt = after[key]['uploads'].get(ident)
            if nxt is not None and not same(row['state'], nxt['state']): changed.append((key, ident))
    if len(changed) != 1: return None
    key, ident = changed[0]
    old = before[key]['uploads'][ident]['state']; new = after[key]['uploads'][ident]['state']
    if (old['phase'], new['phase']) not in (('prepared', 'create_in_flight'),
            ('upload_id_saved', 'send_in_flight')) or 'write_http' in old or 'write_http' in new:
        return None
    restored = deepcopy(after); restored[key]['uploads'][ident]['state'] = deepcopy(old)
    if not same(restored, before): return None
    preserve(before, after)
    ticket = {'note_id': ident, 'before_sha256': digest(before), 'after_sha256': digest(after),
        'before_state': deepcopy(old), 'after_state': deepcopy(new), 'request_sha256': new['request_sha256']}
    # Keep historical ticket shape unchanged; an extension is explicitly scoped.
    if key != 'png_assets': ticket['asset_key'] = key
    return ticket


def check_not_dispatched(ticket, current, restored, typed_evidence):
    """One in-memory same-CAS ticket plus typed pre-opener exact request proof."""
    if (not isinstance(ticket, dict) or type(typed_evidence) is not upload.NotDispatched
            or typed_evidence.reason not in ('request_not_dispatched', 'permission_denied')
            or typed_evidence.request_sha256 != ticket.get('request_sha256')
            or digest(current) != ticket.get('after_sha256')
            or digest(restored) != ticket.get('before_sha256')):
        fail('png_not_dispatched_invalid')
    ident = ticket['note_id']; key = checked_key(ticket.get('asset_key', 'png_assets'))
    if (not same(current[key]['uploads'][ident]['state'], ticket['after_state'])
            or not same(restored[key]['uploads'][ident]['state'], ticket['before_state'])
            or not same(not_dispatched_ticket(restored, current), ticket)):
        fail('png_not_dispatched_invalid')



def upload_receipts(outer, *, asset_key='png_assets'):
    """Read-only input for Resolver, from the externally verified outer state."""
    import get_notion_outbox as outbox
    outbox.check_state(outer)
    result = {}
    for ident, row in outer.get(checked_key(asset_key), {}).get('uploads', {}).items():
        if row['state']['phase'] == 'uploaded_verified':
            receipt = receipt_for(row)
            result[ident] = {'receipt': receipt, 'sha256': digest(receipt)}
    return result


class UploadStore:
    """Per-upload kernel adapter, keeping the exact outer CAS version alive."""
    def __init__(self, store, note_id, approved_plan_sha, *, allow_persist=False, asset_key='png_assets'):
        if type(allow_persist) is not bool: fail('png_persist_disabled')
        p.note_id(note_id); upload.valid_hash(approved_plan_sha)
        self.store = store; self.note_id = note_id; self.plan_sha = approved_plan_sha
        self.asset_key = checked_key(asset_key)
        self.allow_persist = allow_persist; self.outer = None; self.version = None
        self._not_dispatched = None

    def read(self):
        import get_notion_outbox as outbox
        value, version = self.store.read(); outbox.check_state(value)
        if digest(value) != version: fail('png_durability_unconfirmed')
        row = value.get(self.asset_key, {}).get('uploads', {}).get(self.note_id)
        if row is None or row['plan_sha256'] != self.plan_sha: fail('png_plan_changed')
        if self._not_dispatched is not None and version != self._not_dispatched['after_sha256']:
            self._not_dispatched = None
        self.outer = deepcopy(value); self.version = version
        return deepcopy(row['state']), digest(row['state'])

    def compare_and_set(self, expected_sha, value):
        import get_notion_outbox as outbox
        self._not_dispatched = None
        if not self.allow_persist: fail('png_persist_disabled')
        if self.outer is None: fail('png_state_changed')
        row = self.outer[self.asset_key]['uploads'][self.note_id]
        if digest(row['state']) != expected_sha: fail('png_state_changed')
        upload.check_state(value, row['plan'])
        result = deepcopy(self.outer)
        result[self.asset_key]['uploads'][self.note_id]['state'] = deepcopy(value)
        outbox.check_state(result); preserve(self.outer, result)
        observed, version = outbox.confirmed_compare_and_set(self.store, self.version, result)
        ticket = not_dispatched_ticket(self.outer, observed)
        self.outer = observed; self.version = version; self._not_dispatched = ticket
        return digest(value)

    def reset_not_dispatched(self, expected_sha, before_full_state, typed_evidence):
        import get_notion_outbox as outbox
        ticket = self._not_dispatched; self._not_dispatched = None
        if not self.allow_persist: fail('png_persist_disabled')
        if (ticket is None or digest(ticket['after_state']) != expected_sha
                or not same(ticket['before_state'], before_full_state)):
            fail('png_not_dispatched_invalid')
        reset = getattr(self.store, 'compare_and_set_png_not_dispatched', None)
        if not callable(reset): fail('png_not_dispatched_unsupported')
        current, version = self.store.read(); outbox.check_state(current)
        if version != digest(current): fail('png_durability_unconfirmed')
        restored = deepcopy(current)
        restored[self.asset_key]['uploads'][self.note_id]['state'] = deepcopy(before_full_state)
        outbox.check_state(restored)
        check_not_dispatched(ticket, current, restored, typed_evidence)
        observed, new_version = reset(version, restored, typed_evidence)
        outbox.check_state(observed)
        if new_version != digest(restored) or not same(observed, restored): fail('png_durability_unconfirmed')
        self.outer = deepcopy(observed); self.version = new_version
        return digest(before_full_state)
