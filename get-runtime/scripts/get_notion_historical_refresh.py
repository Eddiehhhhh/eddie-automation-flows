"""Exact media-free historical scope derivation, never digest registration.

Original selectors remain the only CLI inputs. A capability is created solely
inside the authenticated runtime from its original journal and same-policy
sealed source. New mappings live in an original capture CAS, retain every old
capture and consume the original size/count budgets. No IO or authority comes
from a caller's raw derived digest, booleans, or a mutable latest pointer.
"""
from copy import deepcopy
import get_notion_executor as e
import get_notion_historical as h
import get_notion_historical_input as inputs
import get_notion_outbox as outbox

CONTRACT = 'get-notion-equivalent-source-refresh-1'
_SEAL = object()


def fail(reason='approval_missing'):
    e.fail(reason)


def _capture(body):
    value = deepcopy(body); value['contract'] = h.CAPTURE_CONTRACT
    return value


def _body(candidate):
    value = deepcopy(candidate); value['contract'] = h.CONTRACT
    return value


def _at(value, commit):
    result = deepcopy(value); result['source_commit'] = commit
    return result


def _media_free(candidate):
    h.validate_capture(candidate, e.digest(candidate))
    if any(row['mode'] != 'new_numeric' or row['target'] is not None
           or row['review']['media_free'] is not True for row in candidate['records'].values()):
        fail('content_review_required')


def check_saved(saved):
    """Pure schema/old exact approval verification; not a runtime capability."""
    import get_notion_idle_rotation as rotation
    value = saved.get('refresh')
    keys = {'contract', 'old_source_commit', 'old_capture_sha256', 'old_manifest_sha256',
            'old_property_sha256', 'original_selection_sha256', 'source',
            'observed_main_commit', 'get_policy_sha256', 'property_manifest'}
    if (type(value) is not dict or set(value) != keys or value['contract'] != CONTRACT
            or 'native_schema_evidence' not in saved):
        fail('state_invalid')
    for key in ('old_source_commit', 'observed_main_commit'): e.p.hex_hash(value[key], 40)
    for key in ('old_capture_sha256', 'old_manifest_sha256', 'old_property_sha256',
                'original_selection_sha256', 'get_policy_sha256'): e.p.hex_hash(value[key], 64)
    source = rotation.check_source(value['source'])
    if source['kind'] != 'authenticated_idle': fail('state_invalid')
    candidate = saved['candidate']; manifest = saved['manifest']; props = value['property_manifest']
    _media_free(candidate)
    if (manifest != _body(candidate) or candidate['source_commit'] != value['observed_main_commit']
            or candidate['source_commit'] == value['old_source_commit']): fail('state_invalid')
    old = _at(candidate, value['old_source_commit']); old_body = _body(old)
    h.validate_capture(old, value['old_capture_sha256'], apply=True)
    h.validate(old_body, value['old_manifest_sha256'], apply=True)
    for digest in (value['old_capture_sha256'], value['old_manifest_sha256'], value['old_property_sha256']):
        inputs.anchor_for(digest)
    if inputs.creation_property_for(value['old_manifest_sha256'], required=True) != value['old_property_sha256']:
        fail('approval_missing')
    members = h.capture_members(value['original_selection_sha256'])
    if value['old_capture_sha256'] not in members: fail('approval_missing')
    old_props = _at(props, value['old_source_commit'])
    if e.digest(old_props) != value['old_property_sha256']: fail('approval_missing')
    h.bind_creation_properties(old_body, old_props, value['old_property_sha256'])
    h.bind_creation_properties(manifest, props, e.digest(props))
    return value


def _empty_scope(state, candidates, *, allow_captures=()):
    """Never split a partly executed cohort or erase an uncertain operation."""
    engine = state['engine_state']
    if engine['journal'] is not None: fail('journal_busy')
    ids = {ident for candidate in candidates for ident in candidate['records']}
    for mapping in (engine['receipts'], engine.get('property_receipts', {}), engine.get('metadata_results', {}),
                    engine['failures'], engine.get('unresolved_by_id', {}), state['pending_by_id'],
                    state.get('png_assets', {}), state.get('png_assets_seq2', {})):
        if ids.intersection(mapping): fail('content_review_required')
    if ids.intersection(state['rotation_order']): fail('content_review_required')
    for key, saved in state.get('historical_captures', {}).items():
        if key not in allow_captures and ids.intersection(saved['candidate']['records']):
            fail('content_review_required')


class Capability:
    __slots__ = ('session', 'old', 'candidate', 'manifest', 'properties', 'mapping', 'sha', 'seal')
    def __init__(self, session, old, candidate, properties, mapping, seal):
        if seal is not _SEAL: fail()
        self.session = session; self.old = old
        self.candidate = deepcopy(candidate); self.manifest = _body(candidate)
        self.properties = deepcopy(properties); self.mapping = deepcopy(mapping)
        self.sha = e.digest((self.old, self.candidate, self.manifest, self.properties, self.mapping)); self.seal = seal

    def check(self, value, expected, kind):
        if (self.seal is not _SEAL or type(self.session) is not Session
                or self.session.seal is not _SEAL
                or self.sha != e.digest((self.old, self.candidate, self.manifest, self.properties, self.mapping))
                or self.manifest != _body(self.candidate)
                or self.properties != self.mapping.get('property_manifest')
                or self.session.caps.get(e.digest(self.old)) is not self
                or self.session.selection_sha256 != e.digest(self.session.selection)): fail()
        wanted = self.candidate if kind == 'capture' else self.manifest if kind == 'apply' else None
        if wanted is None or value != wanted or expected != e.digest(wanted): fail()
        return True


def authorize(capability, value, expected, kind):
    if type(capability) is not Capability: fail()
    return capability.check(value, expected, kind)


class Session:
    """One authenticated store/source lifetime, strict own-CAS continuation."""
    def __init__(self, store, selection, current, parent, policy, source, seal):
        if seal is not _SEAL: fail()
        self.store = store; self.selection = deepcopy(selection); self.selection_sha256 = e.digest(selection); self.policy = deepcopy(policy)
        self.source = source; self.current = deepcopy(current); self.version = outbox.digest(current)
        self.parent = parent; self.pending = None; self.caps = {}; self.seal = seal

    def read_guard(self, current, version):
        if (self.seal is not _SEAL or current != self.current or version != self.version
                or self.store.poisoned or self.store.remote.ref('refs/heads/main') != self.parent):
            fail('state_changed')

    def before_cas(self, before, after, parent):
        if (self.pending is not None or before != self.current or parent != self.parent
                or outbox.digest(before) != self.version): fail('state_changed')
        # Original executor/state/CAS validators still own every business step.
        # This hook only prevents an unobserved head/current successor between
        # capability issuance and an otherwise ordinary original transaction.
        outbox.check_state(after)
        self.pending = deepcopy(after)

    def confirmed(self, receipt):
        if receipt['cas_attempts'] == 0:
            if self.pending is not None: fail('state_changed')
            return
        if self.pending is None or outbox.digest(self.pending) != receipt['state_sha256']:
            fail('state_changed')
        self.current = self.pending; self.version = receipt['state_sha256']
        self.parent = receipt['commit']; self.pending = None

    def capability(self, original, expected, kind):
        cap = self.caps.get(expected)
        old = cap.old if cap is not None else None
        if old is None or original != old or e.digest(original) != expected: fail()
        cap.check(cap.candidate if kind == 'capture' else cap.manifest,
                  e.digest(cap.candidate if kind == 'capture' else cap.manifest), kind)
        current, version = self.store.read(); self.read_guard(current, version)
        return cap


def _read_triplet(remote, candidate):
    old_capture = e.digest(candidate); old_body = _body(candidate); old_manifest = e.digest(old_body)
    h.validate_capture(candidate, old_capture, apply=True)
    h.validate(old_body, old_manifest, apply=True)
    paired = inputs.creation_property_for(old_manifest, required=True)
    # Full original immutable anchor/tree/blob/bytes/parser gates, not membership
    # in a new runtime table or hashes supplied by the caller.
    if inputs.strict_capture_json(inputs.read(remote, old_capture, contract=h.CAPTURE_CONTRACT), 256*1024) != candidate:
        fail('source_invalid')
    if inputs.strict_capture_json(inputs.read(remote, old_manifest, contract=h.CONTRACT), 256*1024) != old_body:
        fail('source_invalid')
    properties = inputs.strict_capture_json(inputs.read(remote, paired, contract=inputs.properties.tx.CONTRACT), 256*1024)
    h.bind_creation_properties(old_body, properties, paired)
    _media_free(candidate)
    return old_manifest, paired, properties


def _source_bytes(loader, candidate, target):
    old = candidate['source_commit']
    for commit in (old, target):
        loader.commit(commit)
        for ident, row in candidate['records'].items():
            loader.blob(commit, row['path'], e.p.MAX_NOTE)
    reader = loader.reader
    before_tree = reader.tree(old); after_tree = reader.tree(target)
    total = 0
    for ident, row in candidate['records'].items():
        leaf = ('100644', 'blob', row['blob'])
        if before_tree.get(row['path']) != leaf or after_tree.get(row['path']) != leaf:
            fail('content_review_required')
        before = reader.read(old, row['path'], e.p.MAX_NOTE)
        after = reader.read(target, row['path'], e.p.MAX_NOTE)
        total += len(before) + len(after)
        if (before != after or e.p.git_blob(before) != row['blob'] or e.digest(before) != row['sha256']
                or total > 20*e.p.MAX_NOTE): fail('content_review_required')
        h.source_check(before, ident)


def prepare(store, selection, current, parent, policy, local, remote, token, root):
    """Runtime-only. No new scope when bootstrap/active old plans are present."""
    import get_notion_idle_rotation as rotation
    import get_notion_remote_journal as journal
    import get_confirmed_source as source_loader
    import get_publication_provenance as provenance
    if (type(store) is not journal.RemoteJournal or type(store.locator) is not rotation.JournalLocator
            or not store.rotation_required or store.locator.expected != outbox.digest(current)):
        fail('rotation_not_installed')
    if not rotation.enabled(policy): fail('approval_missing')
    mode, selector, members = selection
    if mode not in ('historical-capture', 'historical-apply'): fail()
    source = getattr(store, 'verified_rotation_source', None)
    saved_by_old = {}
    for saved in current.get('historical_captures', {}).values():
        if 'refresh' in saved:
            binding = check_saved(saved)
            key = binding['old_capture_sha256'] if mode == 'historical-capture' else binding['old_manifest_sha256']
            if key in saved_by_old: fail('content_review_required')
            saved_by_old[key] = saved
    chosen = [saved_by_old.get(digest) for _, digest in members]
    has_mapping = any(saved is not None for saved in chosen)
    if not has_mapping:
        if mode != 'historical-capture' or current['engine_state']['journal'] is not None:
            return None
        if (type(source) is not rotation.VerifiedSource or source.seal is not rotation._SOURCE_SEAL
                or source.value['kind'] != 'authenticated_idle'):
            return None
        if any(any(set(candidate['records']).intersection(saved['candidate']['records'])
                       for saved in current.get('historical_captures', {}).values()) for candidate, _ in members):
            return None  # Existing captures are immutable and deliberately not refreshed.
    if has_mapping and mode == 'historical-apply' and not all(chosen): fail('content_review_required')
    if has_mapping:
        existing = [saved['refresh'] for saved in chosen if saved is not None]
        if any(value['get_policy_sha256'] != policy['get_policy_sha256']
               or value['source']['public_policy_sha256'] != provenance.a.sha(provenance.a.encoded(policy))
               or value['source']['code_sha'] != policy['code_sha'] for value in existing): fail()
        target = existing[0]['observed_main_commit']; source_value = existing[0]['source']
        if any(value['observed_main_commit'] != target or value['source'] != source_value for value in existing): fail()
        if mode == 'historical-capture' and any(value['original_selection_sha256'] != selector for value in existing): fail()
    else:
        joint = getattr(source, 'joint', None)
        if (type(joint) is not rotation.JointCanonicalProof or joint.seal is not rotation._JOINT_SEAL
                or joint.private.policy != policy): fail('rotation_source_unverified')
        rotation._history_from_source(source, policy)  # Recheck the entire sealed projection.
        target = joint.journal['get_step_record']['observed_main_commit']
        source_value = source.value
    candidates = [value if mode == 'historical-capture' else _capture(value) for value, _ in members]
    for candidate in candidates: _media_free(candidate)
    if not has_mapping and all(candidate['source_commit'] == target for candidate in candidates):
        return None  # Already at the verified origin: retain the original path.
    active = current['engine_state']['journal']
    if mode == 'historical-capture':
        _empty_scope(current, candidates, allow_captures=tuple(e.digest(saved['candidate']) for saved in chosen if saved))
    elif active is not None:
        if (len(members) != 1 or active['plan'].get('historical_manifest') != chosen[0]['manifest']
                or active['plan'].get('historical_manifest_sha256') != e.digest(chosen[0]['manifest'])
                or active['plan']['rules_sha256'] != e.rules_sha256()): fail('journal_busy')
    else:
        _empty_scope(current, candidates, allow_captures=tuple(e.digest(saved['candidate']) for saved in chosen if saved))
    session = Session(store, selection, current, parent, policy, source, _SEAL)
    loader = source_loader.Loader(local, remote, token, root, policy['get_policy_sha256'])
    loader.ancestry(target, parent)
    for (original, expected), old_candidate, saved in zip(members, candidates, chosen):
        old_manifest, paired, properties = _read_triplet(remote, old_candidate)
        _source_bytes(loader, old_candidate, target)
        candidate = _at(old_candidate, target); new_properties = _at(properties, target)
        mapping = {'contract': CONTRACT, 'old_source_commit': old_candidate['source_commit'],
            'old_capture_sha256': e.digest(old_candidate), 'old_manifest_sha256': old_manifest,
            'old_property_sha256': paired, 'original_selection_sha256': selector if saved is None else saved['refresh']['original_selection_sha256'],
            'source': deepcopy(source_value), 'observed_main_commit': target,
            'get_policy_sha256': policy['get_policy_sha256'], 'property_manifest': new_properties}
        if saved is not None and (saved['candidate'] != candidate or saved['refresh'] != mapping): fail('state_changed')
        cap = Capability(session, original, candidate, new_properties, mapping, _SEAL)
        session.caps[expected] = cap
    observed, version = store.read(); session.read_guard(observed, version)
    store.locator.refresh_session = session
    session.loader = loader
    return session
