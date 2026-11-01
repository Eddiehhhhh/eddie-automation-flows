"""Same-policy locator hints and authenticated idle rotation.

A private commit message is only a bounded search hint. It is never a
checkpoint, a publication, a native receipt or permission to write. Every hint
consumer must authenticate the original Actions attempt and current state.
No extra commit, state field, workflow, credential or transport is created here.
"""
from copy import deepcopy
from datetime import datetime, timezone
from weakref import WeakKeyDictionary
import hashlib
import json
import math
import re

PREFIX = 'GET_NOTION_LOCATOR_V1 '
SUBJECT = 'Persist private Notion transaction journal'
IDENTITY = ('Get journal', 'get-journal@users.noreply.github.com')
MAX_HINT = 16384
CONTRACT = 'get-notion-state-commit-locator-1'
SOURCE_KEYS = {'run_id', 'run_attempt', 'controller_sha', 'code_sha',
               'public_policy_sha256', 'receipt_commit', 'receipt_sha256',
               'receipt_sequence', 'record_sha256', 'idle_run_id',
               'idle_run_attempt', 'idle_record_sha256', 'kind'}
HINT_KEYS = {'contract', 'execution', 'source', 'state_sha256', 'state_blob',
             'idle_acceptance'}
EXECUTION_KEYS = {'run_id', 'run_attempt', 'controller_sha', 'code_sha',
                  'public_policy_sha256', 'notion_policy_sha256', 'rules_sha256'}
IDLE_KEYS = {'contract', 'plan_sha256', 'before_state_sha256',
             'after_engine_sha256', 'receipt_set_sha256', 'records', 'before_commit'}


class RotationFailure(ValueError):
    pass


class ClosureReservation:
    """A smaller reservation INSIDE the unchanged original native budget.

    These constants reserve ordinary-phase headroom, not a second sub-budget.
    Closure may consume the actual remainder of the ORIGINAL total budget. It
    is not a promise that arbitrary edited pages fit. Exhaustion preserves the
    index=end journal; no counter, deadline or Transport instance is reset here.
    """
    ATTEMPTS = 256
    BYTES = 16*1024*1024
    SECONDS = 120

    def __init__(self, request):
        from get_notion_live_transport import Transport
        if type(request) is not Transport:
            fail('rotation_budget')
        self.request = request
        self.active = False
        self.start = None
        self.check(False)

    def check(self, closure=None):
        request = self.request
        now = request.clock()
        if (type(request.attempts) is not int or not 0 <= request.attempts <= 2000
                or type(request.bytes) is not int or not 0 <= request.bytes <= 64*1024*1024
                or type(now) not in (int, float) or type(request.deadline) not in (int, float)
                or not math.isfinite(now) or not math.isfinite(request.deadline)):
            fail('rotation_budget')
        if closure is None:
            closure = self.active
        if closure:
            if self.start is None:
                fail('rotation_budget')
            attempts, size, clock = self.start
            if (request.attempts < attempts or request.bytes < size
                    or request.attempts >= 2000 or request.bytes >= 64*1024*1024
                    or not clock <= now < request.deadline):
                fail('rotation_budget')
        elif (request.attempts >= 2000-self.ATTEMPTS
                or request.bytes >= 64*1024*1024-self.BYTES
                or now >= request.deadline-self.SECONDS):
            fail('rotation_budget')

    def begin(self):
        if self.active:
            fail('rotation_budget')
        self.start = (self.request.attempts, self.request.bytes, self.request.clock())
        self.active = True
        try:
            self.check(True)
        except Exception:
            self.end()
            raise

    def end(self):
        self.active = False
        self.start = None


def fail(code='rotation_unverified'):
    raise RotationFailure(code) from None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def oid(raw, kind='blob'):
    return hashlib.sha1(kind.encode()+b' '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def checked(value, width):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{%d}' % width, value):
        fail()
    return value


def positive(value):
    if type(value) is not int or not 1 <= value <= 2**53-1:
        fail()
    return value


def check_execution(value):
    if not isinstance(value, dict) or set(value) != EXECUTION_KEYS:
        fail()
    for key in ('run_id', 'run_attempt'):
        positive(value[key])
    for key in EXECUTION_KEYS-{'run_id', 'run_attempt'}:
        checked(value[key], 40 if key in ('controller_sha', 'code_sha') else 64)
    return deepcopy(value)


def check_source(value):
    if not isinstance(value, dict) or set(value) != SOURCE_KEYS:
        fail()
    if value['kind'] not in ('exact_bootstrap', 'authenticated_idle'):
        fail()
    for key in ('run_id', 'run_attempt', 'receipt_sequence'):
        positive(value[key])
    for key in ('controller_sha', 'code_sha', 'receipt_commit'):
        checked(value[key], 40)
    for key in ('public_policy_sha256', 'receipt_sha256', 'record_sha256'):
        checked(value[key], 64)
    if value['kind'] == 'authenticated_idle':
        if (value['idle_run_id'], value['idle_run_attempt']) != (value['run_id'], value['run_attempt']):
            fail()  # Only one genuine canonical attempt may supply both proofs.
        checked(value['idle_record_sha256'], 64)
    elif any(value[k] is not None for k in ('idle_run_id', 'idle_run_attempt', 'idle_record_sha256')):
        fail()
    return deepcopy(value)


def check_idle(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != IDLE_KEYS
            or value['contract'] != 'get-notion-native-idle-acceptance-1'
            or type(value['records']) is not int or not 1 <= value['records'] <= 10):
        fail()
    for key in IDLE_KEYS-{'contract', 'records'}:
        checked(value[key], 40 if key == 'before_commit' else 64)
    return deepcopy(value)


def check_hint(value):
    if not isinstance(value, dict) or set(value) != HINT_KEYS or value['contract'] != CONTRACT:
        fail()
    check_execution(value['execution'])
    check_source(value['source'])
    checked(value['state_sha256'], 64)
    checked(value['state_blob'], 40)
    check_idle(value['idle_acceptance'])
    if len(encoded(value)) > MAX_HINT:
        fail('rotation_budget')
    return deepcopy(value)


def message(value):
    return SUBJECT+'\n\n'+PREFIX+encoded(check_hint(value)).decode()+'\n'


def parse_message(text):
    if not isinstance(text, str): fail()
    if PREFIX not in text:
        return None
    if len(text.encode()) > MAX_HINT+256:
        fail('rotation_budget')
    if not text.startswith(SUBJECT+'\n\n'+PREFIX) or text.count(PREFIX) != 1:
        fail()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail()
            result[key] = value
        return result
    try:
        value = json.loads(text[len(SUBJECT+'\n\n'+PREFIX):], object_pairs_hook=unique)
    except RotationFailure:
        raise
    except Exception:
        fail()
    value = check_hint(value)
    if message(value).rstrip('\n') != text.rstrip('\n') or text.endswith('\n\n'):
        fail()
    return value


def commit_envelope(value, commit):
    """Reconstruct only our unsigned UTC commit, then verify its complete OID.

    Unknown commits retain the original metadata-only behavior. A marked but
    malformed commit is not silently treated as unmarked. Response text never
    leaves this module in an exception or diagnostic.
    """
    checked(commit, 40)
    if not isinstance(value, dict) or value.get('sha') != commit:
        fail()
    text = value.get('message')
    if not isinstance(text, str):
        return None
    hint = parse_message(text)
    if hint is None:
        return None
    try:
        tree = checked(value['tree']['sha'], 40)
        parents = value['parents']
        if not isinstance(parents, list) or len(parents) != 1:
            fail()
        parent = checked(parents[0]['sha'], 40)
        lines = ['tree '+tree, 'parent '+parent]
        for role in ('author', 'committer'):
            person = value[role]
            if (person.get('name'), person.get('email')) != IDENTITY:
                fail()
            stamp = person.get('date')
            if not isinstance(stamp, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', stamp):
                fail()
            epoch = int(datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp())
            lines.append(role+' '+IDENTITY[0]+' <'+IDENTITY[1]+'> '+str(epoch)+' +0000')
        raw = ('\n'.join(lines)+'\n\n'+message(hint)).encode()
        if oid(raw, 'commit') != commit:
            fail()
    except RotationFailure:
        raise
    except Exception:
        fail()
    return {'commit': commit, 'parent': parent, 'tree': tree, 'hint': hint,
            'committed_at': value['committer']['date']}


def latest_hint(resolver, *, at_commit=None):
    """Bounded current state-change locator, NOT authenticated state authority.

    Uses the same PrivateRecords node budget, fixed-path Merkle checks, current
    repository/head guards and immutable metadata. No second history budget.
    """
    import get_private_proof as proof
    if type(resolver) is not proof.PrivateRecords:
        fail()
    resolver.guard()
    start = 0
    if at_commit is not None:
        checked(at_commit, 40)
        for start in range(resolver.limit):
            node = resolver.node(start)
            if node is None: fail()
            if node['commit'] == at_commit: break
        else: fail('rotation_budget')
    head = resolver.node(start)
    if head is None or proof.PATHS['journal'] not in head['objects']:
        return None
    state = head['objects'][proof.PATHS['journal']]
    for index in range(start, resolver.limit):
        node = resolver.node(index)
        if node is None:
            fail()
        prior = resolver.node(index+1) if node['parent'] is not None else None
        previous = prior['objects'].get(proof.PATHS['journal']) if prior else None
        if previous != state:
            envelope = node.get('locator')
            resolver.guard()
            if envelope is None:
                return None  # Original historical commit; exact old fallback only.
            if (envelope['commit'] != node['commit'] or envelope['parent'] != node['parent']
                    or envelope['tree'] != node['tree']
                    or envelope['hint']['state_blob'] != state[0]):
                fail()
            raw = resolver.object(node, 'journal')
            if raw is None or sha(raw) != envelope['hint']['state_sha256']:
                fail()
            return deepcopy(envelope)
    fail('rotation_budget')


def check_checkpoint_hint(envelope, found, state):
    """Call only beside original RemoteJournal current readback, never instead."""
    import get_notion_outbox as outbox
    outbox.check_state(state)
    hint = check_hint(envelope['hint'])
    execution = hint['execution']
    checkpoint = found['checkpoint']
    if (found.get('status') != 'journal_provenance_verified'
            or found.get('private_readback_required') is not True
            or (found['run_id'] == execution['run_id'] and
                any(checkpoint.get(k) != v for k, v in execution.items()))
            or found['anchor']['state_blob'] != hint['state_blob']
            or found['anchor']['state_sha256'] != hint['state_sha256']
            or outbox.digest(state) != hint['state_sha256']
            or oid(outbox.encoded(state)) != hint['state_blob']):
        fail('rotation_checkpoint_changed')
    # The original _current additionally rejects any later state edit/revert.
    return deepcopy(hint)


def enabled(policy):
    import get_notion_checkpoint_resume as bootstrap
    rows=bootstrap.registry()
    if not rows:return False
    binding=rows[0]
    return (bootstrap.target_code_sha256() == binding['target_code_sha256']
            and bootstrap.candidate_policy(policy, binding['proof']['latest_binding'])[1])


def current_hint(client, policy):
    """Only this exact reviewed target enables the new locator mechanism."""
    import get_publication_provenance as provenance
    import get_notion_runtime as runtime
    if not enabled(policy):
        return None
    resolver = getattr(client, 'private_proof_source', None)
    if resolver is None:
        fail()
    envelope = latest_hint(resolver)
    if envelope is None:
        return None
    execution = envelope['hint']['execution']
    if (execution['code_sha'] != policy['code_sha']
            or execution['public_policy_sha256'] != provenance.a.sha(provenance.a.encoded(policy))
            or execution['notion_policy_sha256'] != runtime.e.digest(runtime.scope_for(policy))
            or execution['rules_sha256'] != policy['rules_sha256']):
        fail('rotation_policy_changed')
    return envelope


def validate_found(envelope, found):
    if envelope is None:
        return found
    hint = check_hint(envelope['hint'])
    if (any(found['checkpoint'].get(k) != hint['execution'][k]
            for k in ('code_sha','rules_sha256','public_policy_sha256','notion_policy_sha256'))
            or (found['run_id'] == hint['execution']['run_id'] and
         any(found['checkpoint'].get(k) != v for k, v in hint['execution'].items()))
            or found['checkpoint']['state_blob'] != hint['state_blob']
            or found['checkpoint']['state_sha256'] != hint['state_sha256']):
        fail('rotation_checkpoint_changed')
    # This remains untrusted until the original current read verifies the full
    # anchor->head ancestry, every state-touch and exact state bytes.
    return {**found, 'locator_envelope': deepcopy(envelope)}


def clear_after(before):
    """The original EngineStore's final clear, without executing a CAS.

    Rows have already advanced to index=end. Only the original skipped-conflict
    queue rotation can accompany the journal clear; no receipt, hold or other
    outer state may be edited to manufacture an idle boundary.
    """
    import get_notion_outbox as outbox
    outbox.check_state(before)
    journal = before['engine_state']['journal']
    if (journal is None or journal['operation'] is not None
            or journal['index'] != len(journal['plan']['records'])):
        fail('rotation_busy')
    after = deepcopy(before)
    after['engine_state']['journal'] = None
    plan = journal['plan']
    if not (plan.get('property_only') or plan.get('historical_only')):
        for ident in plan['skipped_conflicts']:
            if ident in after['rotation_order']:
                after['rotation_order'].remove(ident)
                after['rotation_order'].append(ident)
    outbox.check_state(after)
    return after


def verify_idle_origin(resolver, envelope, engine_state):
    """Authenticate the marker's actual clear parent and every retained state.

    The marker is not evidence on its own. This bounded walk shares the original
    PrivateRecords history/object budgets. Its first appearance must be on the
    exact clear CAS; later original state commits may preserve it only while
    the completed engine state is byte-identical. An arbitrary ancestor with
    the same engine hash cannot substitute for that causal edge.
    """
    import get_private_proof as private
    import get_notion_outbox as outbox
    if type(resolver) is not private.PrivateRecords:
        fail()
    marker = check_idle(envelope['hint']['idle_acceptance'])
    if marker is None or sha(engine_state) != marker['after_engine_sha256']:
        fail('rotation_native_unverified')
    resolver.guard()
    started = False
    for index in range(resolver.limit):
        node = resolver.node(index)
        if node is None: fail('rotation_native_unverified')
        if not started:
            if node['commit'] != envelope['commit']: continue
            started = True
        parent = resolver.node(index+1) if node['parent'] is not None else None
        if parent is None: fail('rotation_native_unverified')
        raw = resolver.object(node, 'journal')
        value = private.strict(raw, private.MAX_OBJECT)
        outbox.check_state(value)
        if value['engine_state'] != engine_state:
            fail('rotation_native_changed')
        changed = node['objects'].get(private.PATHS['journal']) != parent['objects'].get(private.PATHS['journal'])
        if changed:
            located = node.get('locator')
            if (located is None or located['commit'] != node['commit']
                    or located['parent'] != node['parent'] or located['tree'] != node['tree']
                    or located['hint']['state_blob'] != oid(raw)
                    or located['hint']['state_sha256'] != sha(raw)
                    or located['hint']['idle_acceptance'] != marker):
                fail('rotation_native_unverified')
        if node['parent'] == marker['before_commit']:
            before = private.strict(resolver.object(parent, 'journal'), private.MAX_OBJECT)
            outbox.check_state(before)
            if (not changed or outbox.digest(before) != marker['before_state_sha256']
                    or clear_after(before) != value):
                fail('rotation_native_unverified')
            plan = before['engine_state']['journal']['plan']
            if (sha(plan) != marker['plan_sha256']
                    or len(plan['records']) != marker['records']
                    or sha(receipt_set(before['engine_state'], plan)) != marker['receipt_set_sha256']):
                fail('rotation_native_unverified')
            previous = parent.get('locator')
            if previous is not None and previous['hint']['idle_acceptance'] is not None:
                fail('rotation_native_unverified')
            resolver.guard()
            return before
    fail('rotation_budget')


def source_search(client, policy, listed, *, exclude=None):
    """At most one idle proposal plus the previous exact source within 4 slots.

    Listing timestamps are eligibility hints only. Neither a new timestamp nor
    a positive panel result is publication/idle authority. Original verifiers
    decide; malformed, unavailable or contradictory proof never falls back.
    """
    import get_publication_provenance as p
    import get_private_proof as private
    envelope = current_hint(client, policy)
    if envelope is None:
        return None
    hint = envelope['hint']
    prior = hint['source']
    chosen = (prior['run_id'], prior['run_attempt'])
    proposal = None
    resolver = client.private_proof_source
    state = private.strict(resolver.object(resolver.node(0), 'journal'), private.MAX_OBJECT)
    marker = hint['idle_acceptance']
    if (state['engine_state']['journal'] is None and marker is not None
            and sha(state['engine_state']) == marker['after_engine_sha256']):
        for run in listed:
            key = (run['id'], run['run_attempt'])
            if (key == exclude or key == chosen or run['status'] != 'completed'
                    or run['conclusion'] != 'success'):
                continue
            if (run['id'] == hint['execution']['run_id']
                    or p.timestamp(run['created_at']) >= p.timestamp(envelope['committed_at'])):
                proposal = key
                break
    return {'envelope': envelope, 'previous': chosen, 'proposal': proposal,
            'locator': proposal or chosen,
            'joint': proposal is not None or prior['kind'] == 'authenticated_idle'}


def _idle_report(report):
    if (not isinstance(report, dict) or report.get('contract') != 'get-private-runtime-report-2'
            or report.get('write_state') not in ('confirmed', 'not_attempted')
            or type(report.get('external_write_attempts')) is not int
            or report['external_write_attempts'] < 0):
        fail('rotation_not_idle')
    if report.get('status') == 'queue_empty':
        if report['external_write_attempts'] != 0 or report['write_state'] != 'not_attempted':
            fail('rotation_not_idle')
    elif (report.get('status') != 'executed'
          or report.get('execution_status') != 'verified'
          or report.get('execution_reason') is not None):
        fail('rotation_not_idle')


def observe_source(client, policy, run, value, *, search=None):
    """Receive only the just-completed original verifier result in this call."""
    import get_notion_checkpoint_resume as bootstrap
    import get_publication_provenance as p
    import get_private_proof as private
    import get_notion_outbox as outbox
    if not enabled(policy):
        return value
    binding = bootstrap.registry()[0]
    if type(value) is JointCanonicalProof:
        if value.seal is not _JOINT_SEAL or search is None:
            fail()
        source = value.source
        proof = value.journal
        _idle_report(proof['terminal_report'])
        record = proof['get_step_record']
        verified = value.private
        if type(verified) is not _PreparedCanonical or verified.seal is not _JOINT_SEAL:
            fail()
        state = verified.state
        idle_envelope = verified.envelope
        hint = search['envelope']['hint']
        if (verified.current_sha256 != hint['state_sha256']
                or (search['proposal'] is not None
                    and verified.current_engine != state['engine_state'])):
            fail('rotation_state_changed')
        previous = hint['source']
        if (source['receipt_sequence'] < previous['receipt_sequence']
                or (source['receipt_sequence'] == previous['receipt_sequence']
                    and source['anchor'] != {'commit': previous['receipt_commit'],
                                             'receipt_sha256': previous['receipt_sha256']})):
            fail('rotation_downgrade')
        selected = {'run_id': source['run_id'], 'run_attempt': source['run_attempt'],
            'controller_sha': run['head_sha'], 'code_sha': policy['code_sha'],
            'public_policy_sha256': p.a.sha(p.a.encoded(policy)),
            'receipt_commit': source['anchor']['commit'],
            'receipt_sha256': source['anchor']['receipt_sha256'],
            'receipt_sequence': source['receipt_sequence'],
            'record_sha256': source['record_sha256'],
            'idle_run_id': proof['run_id'], 'idle_run_attempt': proof['run_attempt'],
            'idle_record_sha256': proof['checkpoint']['record_sha256'],
            'kind': 'authenticated_idle'}
        if (record['record_sha256'] != source['record_sha256']
                or (source['run_id'], source['run_attempt']) != (proof['run_id'], proof['run_attempt'])):
            fail()
        witness = VerifiedSource(selected, _SOURCE_SEAL)
        witness.joint = value
        witness.envelope = deepcopy(idle_envelope)
        witness.needs_native = previous != selected
        _OBSERVATIONS[client] = (p.a.sha(p.a.encoded(policy)), witness)
        return source
    # The one exact bootstrap source is allowed only with the original complete
    # immutable registration and the original source verifier's exact result.
    original = binding['source_binding']
    record = original['get_record']
    if (value['run_id'] != original['run_binding']['id']
            or value['run_attempt'] != original['run_binding']['run_attempt']
            or value['job_id'] != original['job_id']
            or value['record_sha256'] != record['record_sha256']
            or value['anchor'] != {'commit': record['receipt_commit'], 'receipt_sha256': record['receipt_sha256']}
            or value['receipt_sequence'] != record['receipt_sequence']):
        if search is not None: fail()
        return value  # Normal source discovery is not a locator installation.
    selected = {'run_id': value['run_id'], 'run_attempt': value['run_attempt'],
        'controller_sha': original['run_binding']['head_sha'],
        'code_sha': original['public_policy']['code_sha'],
        'public_policy_sha256': original['public_policy_sha256'],
        'receipt_commit': value['anchor']['commit'], 'receipt_sha256': value['anchor']['receipt_sha256'],
        'receipt_sequence': value['receipt_sequence'], 'record_sha256': value['record_sha256'],
        'idle_run_id': None, 'idle_run_attempt': None, 'idle_record_sha256': None,
        'kind': 'exact_bootstrap'}
    witness = VerifiedSource(selected, _SOURCE_SEAL)
    witness.needs_native = False
    _OBSERVATIONS[client] = (p.a.sha(p.a.encoded(policy)), witness)
    return value


def observation_for(client, policy, source):
    """Only the just-verified exact candidate's sealed in-memory fact."""
    import get_publication_provenance as provenance
    item = _OBSERVATIONS.get(client)
    if item is None: return None
    policy_hash, witness = item
    if (policy_hash != provenance.a.sha(provenance.a.encoded(policy))
            or type(witness) is not VerifiedSource or witness.seal is not _SOURCE_SEAL):
        fail('rotation_source_unverified')
    value = witness.value
    expected = {'run_id': value['run_id'], 'run_attempt': value['run_attempt'],
        'receipt_sequence': value['receipt_sequence'], 'record_sha256': value['record_sha256'],
        'anchor': {'commit': value['receipt_commit'], 'receipt_sha256': value['receipt_sha256']}}
    if any(source.get(key) != wanted for key, wanted in expected.items()):
        return None
    return item


def select_observation(client, policy, chosen, item, *, required=False):
    """Restore only the original selection result, never pick an older seal."""
    _OBSERVATIONS.pop(client, None)
    if item is None:
        if required: fail('rotation_source_unverified')
        return None
    _OBSERVATIONS[client] = item
    try:
        if observation_for(client, policy, chosen) is not item:
            fail('rotation_source_unverified')
        return observed_history_witness(client, policy, chosen)
    except Exception:
        _OBSERVATIONS.pop(client, None)
        raise


def receipt_set(engine, plan):
    """Only batch receipts, not a claim that the complete library is accepted."""
    rows = plan.get('records')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
        fail()
    answer = {}
    for row in rows:
        ident = row['note_id']
        body = engine['receipts'].get(ident)
        if ident in answer or not body or body.get('readback_verified') is not True:
            fail('rotation_incomplete')
        if (plan.get('property_only') or 'property_incremental_scope' in plan
                or plan.get('property_manifest_sha256') or 'property_created' in row):
            import get_notion_executor as executor
        pair = engine.get('property_receipts', {}).get(ident)
        if plan.get('property_only'):
            # The original property transaction never rewrites body receipts.
            expected_pair = {'contract': executor.properties.CONTRACT,
                'page_id': row['page_id'], 'data_source_id': plan['policy']['data_source_id'],
                'source_commit': plan['publication']['commit'], 'md_blob': row['source']['blob'],
                'manifest_sha256': plan['property_manifest_sha256'],
                'managed': deepcopy(row['property_managed']), 'held': deepcopy(row['held_fields']),
                'readback_verified': True}
            if row.get('property_only') is not True or pair != expected_pair:
                fail('rotation_unpaired_receipt')
        elif 'property_incremental_scope' in plan:
            executor.incremental_properties.check_plan(plan)
            metadata = engine.get('metadata_results', {}).get(ident)
            bound = row.get('metadata_completion')
            if not isinstance(bound, dict) or not isinstance(metadata, dict):
                fail('rotation_unpaired_receipt')
            required = {'contract': executor.metadata_pending.CONTRACT,
                'source_commit': plan['publication']['commit'], 'md_blob': row['source']['blob'],
                'page_id': body['page_id'], 'data_source_id': body['data_source_id'],
                'plan_sha256': executor.digest(plan), 'facet_sha256': executor.digest(bound),
                'properties_sha256': executor.digest(body['properties_baseline']), 'readback_verified': True}
            if any(metadata.get(key) != value for key, value in required.items()):
                fail('rotation_unpaired_receipt')
            if metadata.get('property_write_verified'):
                # A new ownership receipt must be for this precise incremental
                # scope. An unchanged historical ownership receipt is not
                # promoted into a new one merely by an equal observation.
                if (pair is None or pair.get('manifest_sha256') != plan['property_incremental_scope_sha256']
                        or pair.get('source_commit') != plan['publication']['commit']
                        or pair.get('md_blob') != row['source']['blob']):
                    fail('rotation_unpaired_receipt')
            if pair is not None and (pair.get('page_id') != body['page_id']
                    or pair.get('data_source_id') != body['data_source_id']
                    or pair.get('readback_verified') is not True):
                fail('rotation_unpaired_receipt')
        elif plan.get('property_manifest_sha256') or 'property_created' in row:
            expected = deepcopy(engine)
            executor.property_transaction.record_creation(expected, row, plan)
            expected_pair = expected.get('property_receipts', {}).get(ident)
            if expected_pair is None or pair != expected_pair:
                fail('rotation_unpaired_receipt')
            for key, value in {'page_id': body['page_id'], 'data_source_id': body['data_source_id'],
                    'source_commit': plan['publication']['commit'], 'md_blob': row['source']['blob'],
                    'manifest_sha256': plan.get('property_manifest_sha256') or plan.get('property_create_scope_sha256'),
                    'readback_verified': True}.items():
                if pair.get(key) != value: fail('rotation_unpaired_receipt')
            if 'held_fields' in row and pair.get('held') != row['held_fields']:
                fail('rotation_unpaired_receipt')
        answer[ident] = {key: deepcopy(engine.get(key, {}).get(ident))
                         for key in ('receipts', 'property_receipts', 'metadata_results')}
    return answer


def verify_idle_clear(before, after, api, *, writers_guard, parent):
    """Fresh full native batch acceptance BEFORE the original journal-clear CAS.

    No NativeWitness constructor is public. A failed/uncertain read leaves the
    original completed-but-not-cleared journal untouched; it never repeats POST.
    """
    import get_notion_outbox as outbox
    import get_notion_executor as engine
    checked(parent, 40)
    outbox.check_state(before)
    outbox.check_state(after)
    old = before['engine_state']
    new = after['engine_state']
    journal = old['journal']
    if (journal is None or journal['operation'] is not None
            or journal['index'] != len(journal['plan']['records'])
            or new['journal'] is not None):
        fail('rotation_busy')
    expected = clear_after(before)
    if expected != after:
        fail('rotation_state_changed')
    plan = journal['plan']
    receipts = receipt_set(old, plan)
    if plan.get('property_manifest_sha256'):
        manifest = plan.get('property_manifest')
        if (not isinstance(manifest, dict) or engine.digest(manifest) != plan['property_manifest_sha256']
                or engine.digest(engine.properties.schema_projection(api.schema())) != manifest['schema_sha256']):
            fail('rotation_schema_changed')
    if not engine.p.writer_gate({'old_writer_evidence': writers_guard()}):
        fail('rotation_writer')
    data_source = plan['policy']['data_source_id']
    for row in plan['records']:
        ident = row['note_id']
        if ident in old.get('unresolved_by_id', {}):
            fail('rotation_unknown_write')
        snapshot = engine.read_target(api, ident, data_source)
        body = old['receipts'][ident]
        pair = old.get('property_receipts', {}).get(ident)
        if pair is not None:
            view = engine.target(snapshot, ident, data_source)
            if view is None or view['page_id'] != pair['page_id']:
                fail('rotation_native_changed')
            for name, expected in pair['managed'].items():
                if engine.properties.value(view['properties'].get(name)) != expected:
                    fail('rotation_native_changed')
        if plan.get('property_only'):
            engine.property_transaction.verify_after(row['before'], snapshot, row, data_source)
            managed = [block['id'] for block in body['managed_blocks']]
        elif row.get('action') == 'create':
            managed = engine.created(snapshot, row, data_source, body['page_id'])
        elif row.get('action') in ('noop', 'update'):
            view = engine.target(snapshot, ident, data_source)
            if view is None or view['page_id'] != body['page_id']:
                fail('rotation_native_changed')
            managed = [block['id'] for block in body['managed_blocks']]
        else:
            fail('rotation_incomplete')
        fresh = engine.receipt(snapshot, ident, data_source, managed,
            plan['publication'], row['source'], row.get('preserved_asset_bindings'))
        if plan.get('property_only'):
            # Body provenance is intentionally unchanged by a property-only
            # transaction. verify_after above binds every native property to
            # the immutable before snapshot and exact approved patch.
            fresh['source_commit'] = body['source_commit']
            fresh['properties_baseline'] = deepcopy(body['properties_baseline'])
        if 'creation_plan_sha256' in body:
            fresh['creation_plan_sha256'] = body['creation_plan_sha256']
        if fresh != body:
            fail('rotation_native_changed')
        if engine.metadata_pending.enabled(plan):
            stored = old.get('metadata_results', {}).get(ident)
            if not isinstance(stored, dict): fail('rotation_unpaired_receipt')
            observed = deepcopy(old)
            counts = {'body_verified': 0, 'metadata_pending': 0, 'retained_conflicts': 0}
            engine.metadata_pending.record(observed, row, plan, snapshot, api, counts)
            checked_metadata = observed['metadata_results'][ident]
            if any(stored.get(key) != value for key, value in checked_metadata.items() if key != 'held'):
                fail('rotation_native_changed')
            # Fresh reads may not erase a durable hold or create new ownership.
            # Newly detected failures stop; older held fields stay exactly as
            # recorded in state and in the authenticated idle marker.
            if set(checked_metadata['held']) - set(stored['held']):
                fail('rotation_native_changed')
    if not engine.p.writer_gate({'old_writer_evidence': writers_guard()}):
        fail('rotation_writer')
    return {'contract': 'get-notion-native-idle-acceptance-1',
            'plan_sha256': engine.digest(plan),
            'before_state_sha256': outbox.digest(before),
            'after_engine_sha256': engine.digest(new),
            'receipt_set_sha256': sha(receipts), 'records': len(plan['records']),
            'before_commit': parent}


_SOURCE_SEAL = object()


class VerifiedSource:
    def __init__(self, value, seal):
        if seal is not _SOURCE_SEAL:
            fail()
        self.value = check_source(value)
        self.seal = seal


class JournalLocator:
    """Annotate original state commits; never add a commit or alter state bytes.

    Runtime installs this only after the original live execution, source,
    checkpoint and mutable-current guards. New source admission is separately
    sealed and must already have passed the authenticated idle route.
    """
    def __init__(self, execution, source, *, current, previous=None,
                 api_factory, writers_guard, reservation):
        import get_notion_outbox as outbox
        outbox.check_state(current)
        if type(source) is not VerifiedSource or source.seal is not _SOURCE_SEAL:
            fail()
        if not callable(api_factory) or not callable(writers_guard):
            fail()
        self.execution = check_execution(execution)
        self.source = check_source(source.value)
        self.api_factory = api_factory
        self.writers_guard = writers_guard
        if type(reservation) is not ClosureReservation:
            fail('rotation_budget')
        self.reservation = reservation
        self.idle = None
        self.expected = outbox.digest(current)
        if previous is not None:
            previous = check_hint(previous)
            if previous['state_sha256'] != self.expected:
                fail('rotation_state_changed')
            if (current['engine_state']['journal'] is not None
                    and previous['source'] != self.source):
                fail('rotation_busy')
            self.idle = previous['idle_acceptance']
        self.pending = None

    def prepare(self, before, after, data, parent):
        import get_notion_outbox as outbox
        outbox.check_state(before)
        outbox.check_state(after)
        if (self.pending is not None or outbox.digest(before) != self.expected
                or outbox.encoded(after) != data or data == outbox.encoded(before)):
            fail('rotation_state_changed')
        old = before['engine_state']['journal']
        new = after['engine_state']['journal']
        idle = deepcopy(self.idle)
        if old is not None and new is None:
            # Full readback is deliberately before _build / push. If it fails,
            # the saved index=end, operation=null journal remains recoverable.
            self.reservation.begin()
            try:
                api = self.api_factory(old['plan'])
                if getattr(api, 'request', None) is not self.reservation.request:
                    fail('rotation_budget')
                idle = verify_idle_clear(before, after, api,
                                         writers_guard=self.writers_guard, parent=parent)
            finally:
                self.reservation.end()
        elif new is not None:
            idle = None
        elif before['engine_state'] != after['engine_state']:
            idle = None
        value = {'contract': CONTRACT, 'execution': self.execution,
                 'source': self.source, 'state_sha256': outbox.digest(data),
                 'state_blob': oid(data), 'idle_acceptance': idle}
        session = getattr(self, 'refresh_session', None)
        if session is not None:
            from get_notion_historical_refresh import Session
            if type(session) is not Session: fail('rotation_state_changed')
            session.before_cas(before, after, parent)
        self.pending = check_hint(value)
        return message(value)

    def confirmed(self, receipt):
        session = getattr(self, 'refresh_session', None)
        if session is not None: session.confirmed(receipt)
        if receipt['cas_attempts'] == 0:
            if self.pending is not None:
                fail('rotation_state_changed')
            return
        value = self.pending
        if (value is None or value['state_sha256'] != receipt['state_sha256']
                or value['state_blob'] != receipt['state_blob']):
            fail('rotation_state_changed')
        self.expected = value['state_sha256']
        self.idle = value['idle_acceptance']
        self.pending = None



_JOINT_SEAL = object()
_OBSERVATIONS = WeakKeyDictionary()


class VerifiedHistoryBundle:
    """At most two exact intervals attested by ONE terminal canonical producer.

    This is not a stored pointer, a raw receipt capability or an ancestry cache.
    Creation occurs only after joint source+journal tail authentication. Every
    consumer still checks original receipt bytes, endpoints, deltas and states.
    """
    __slots__ = ('intervals', 'seal')

    def __setattr__(self, name, value):
        raise AttributeError('Immutable authenticated intervals')

    def __init__(self, intervals, seal):
        import get_recurring_entry as entry
        if (seal is not _JOINT_SEAL or type(intervals) is not tuple
                or not 1 <= len(intervals) <= 2
                or any(type(w) is not entry.VerifiedHistoryWitness for w in intervals)
                or len({w.receipt_commit for w in intervals}) != len(intervals)
                or len({w.observed_commit for w in intervals}) != len(intervals)
                or len({w.record_sha256 for w in intervals}) != 1
                or len(intervals) == 2 and intervals[0].receipt_sequence != intervals[1].receipt_sequence+1):
            fail('rotation_history_unverified')
        for witness in intervals:
            for field, width in (('receipt_commit',40), ('receipt_sha256',64),
                    ('observed_commit',40), ('record_sha256',64)):
                checked(getattr(witness,field), width)
            if type(witness.receipt_sequence) is not int or not 1 <= witness.receipt_sequence <= 1000000:
                fail('rotation_history_unverified')
        object.__setattr__(self, 'intervals', intervals)
        object.__setattr__(self, 'seal', seal)


def history_interval(bundle, *, ancestor=None, observed=None):
    import get_recurring_entry as entry
    if type(bundle) is not VerifiedHistoryBundle or bundle.seal is not _JOINT_SEAL:
        entry.fail('entry_history_witness')
    # Revalidate the immutable tuple at every original consumer boundary.
    VerifiedHistoryBundle(bundle.intervals, _JOINT_SEAL)
    if (ancestor is None) == (observed is None): entry.fail('entry_history_witness')
    rows = [w for w in bundle.intervals if (w.receipt_commit == ancestor if ancestor is not None
                                          else w.observed_commit == observed)]
    if len(rows) > 1: entry.fail('entry_history_witness')
    return rows[0] if rows else None


def prepare_history_links(resolver, record, policy):
    """Read exact receipt linkage before the joint verifier's final HTTP reads.

    These values are INERT preparation, not authority. The successful original
    producer (publication_record -> confirmed_private_handoff -> verify_plan)
    validates both the current receipt's entire transaction and its immediate
    previous-receipt interval. Only a later sealed joint result may project that
    already-authenticated producer fact; it cannot validate itself recursively.
    """
    import get_recurring_entry as entry
    import get_private_proof as private
    if type(resolver) is not private.PrivateRecords:
        fail('rotation_history_unverified')
    class Reader:
        def read(self, commit, path, maximum):
            if path != entry.FULL_RECEIPT: fail('rotation_history_unverified')
            raw = resolver.object(resolver.read_node(commit), 'publication')
            if not isinstance(raw, bytes) or len(raw) > maximum: fail('rotation_history_unverified')
            return raw
    reader = Reader()
    commit = record['receipt_commit']
    current = entry.read_receipt(reader, commit, record['receipt_sha256'], policy['get_policy_sha256'])
    if (current['sequence'] != record['receipt_sequence']
            or resolver.read_node(commit)['parent'] != current['parent_commit']):
        fail('rotation_history_unverified')
    previous = None
    if current['sequence'] >= 2:
        prior = entry.read_receipt(reader, current['previous_receipt_commit'],
            current['previous_receipt_sha256'], policy['get_policy_sha256'])
        if (prior['sequence']+1 != current['sequence']
                or prior['state_sha256'] != current['previous_legacy_sha256']
                or prior['registry_sha256'] != current['previous_registry_sha256']
                or current['previous_receipt_commit'] == commit):
            fail('rotation_history_unverified')
        previous = (current['previous_receipt_commit'], current['previous_receipt_sha256'],
                    prior['sequence'], current['parent_commit'], record['record_sha256'])
    resolver.guard()
    return {'current_receipt_sha256': record['receipt_sha256'],
            'current_receipt_commit': commit, 'previous_interval': previous}


def _history_from_source(witness, policy):
    """Project an already jointly authenticated fact into the ORIGINAL type.

    The observed source commit is taken from the authenticated Get record, not
    the journal/current head. No new identity lookup, endpoint-equality grant,
    mutable cache or wider history-jump implementation is introduced here.
    """
    import get_publication_provenance as p
    if type(witness) is not VerifiedSource or witness.seal is not _SOURCE_SEAL:
        fail()
    if witness.value['kind'] != 'authenticated_idle':
        return None
    joint = getattr(witness, 'joint', None)
    if (type(joint) is not JointCanonicalProof or joint.seal is not _JOINT_SEAL
            or type(joint.private) is not _PreparedCanonical
            or joint.private.seal is not _JOINT_SEAL
            or joint.private.policy != policy):
        fail('rotation_source_unverified')
    record = joint.journal['get_step_record']
    source = joint.source
    if (source['anchor'] != {'commit': record['receipt_commit'], 'receipt_sha256': record['receipt_sha256']}
            or source['receipt_sequence'] != record['receipt_sequence']
            or source['record_sha256'] != record['record_sha256']
            or source['run_id'] != joint.journal['run_id']
            or source['run_attempt'] != joint.journal['run_attempt']
            or witness.value['public_policy_sha256'] != p.a.sha(p.a.encoded(policy))):
        fail('rotation_source_unverified')
    links = joint.private.history_links
    if sha(links) != joint.private.history_links_sha256:
        fail('rotation_history_unverified')
    if (links['current_receipt_sha256'] != record['receipt_sha256']
            or links['current_receipt_commit'] != record['receipt_commit']):
        fail('rotation_history_unverified')
    current = p.entry.VerifiedHistoryWitness(record['receipt_commit'], record['receipt_sha256'],
        record['receipt_sequence'], record['observed_main_commit'], record['record_sha256'])
    intervals = (current,)
    if links['previous_interval'] is not None:
        intervals += (p.entry.VerifiedHistoryWitness(*links['previous_interval']),)
    return VerifiedHistoryBundle(intervals, _JOINT_SEAL)


def observed_history_witness(client, policy, source):
    item = _OBSERVATIONS.get(client)
    if item is None: return None
    import get_publication_provenance as p
    if item[0] != p.a.sha(p.a.encoded(policy)):
        fail('rotation_source_unverified')
    witness = item[1]
    if (source['run_id'], source['run_attempt']) != (witness.value['run_id'], witness.value['run_attempt']):
        return None
    return _history_from_source(witness, policy)


class JointCanonicalProof:
    def __init__(self, source, journal, seal, *, private=None):
        if seal is not _JOINT_SEAL:
            fail()
        self.source = deepcopy(source)
        self.journal = deepcopy(journal)
        self.private = private
        self.seal = seal


class _PreparedCanonical:
    def __init__(self, seal):
        if seal is not _JOINT_SEAL: fail()
        self.seal = seal


def prepare_canonical_journal(client, policy, run, job, raw_log):
    """Intersection of original journal checks with source authentication.

    All journal expansion and idle-origin private reads occur BEFORE the
    source verifier's existing final attempt/alias requests. The finish stage
    then applies both original tail contracts to those same actual responses.
    There is no dynamic HTTP cache or extra identity request.
    """
    import get_notion_journal_runtime as journal
    import get_publication_provenance as provenance
    import get_notion_runtime as runtime
    import get_private_proof as private
    policy_hash = provenance.a.sha(provenance.a.encoded(policy))
    notion_hash = runtime.e.digest(runtime.scope_for(policy))
    journal._terminal_run(run, policy)
    if run['conclusion'] != 'success':
        fail('rotation_not_idle')
    reviewed = journal.controller.expected_bytes(policy)
    if (reviewed.count(('      - name: '+journal.NOTION_STEP).encode()) != 1
            or reviewed.count(b'        id: notion_sync') != 1):
        journal.fail('journal_provenance_controller_step')
    if (job.get('run_id') != run['id'] or job.get('run_attempt', run['run_attempt']) != run['run_attempt']
            or job.get('head_sha') != run['head_sha'] or job.get('status') != 'completed'
            or job.get('conclusion') != 'success'
            or not provenance.timestamp(run['created_at']) <= provenance.timestamp(job.get('started_at'))
               <= provenance.timestamp(job.get('completed_at')) <= provenance.timestamp(run['updated_at'])):
        journal.fail('journal_provenance_job')
    provenance.positive(job.get('id'))
    steps = job.get('steps')
    if not isinstance(steps, list) or len(steps) > 100:
        journal.fail('journal_provenance_steps')
    def selected(name):
        rows = [step for step in steps if isinstance(step, dict) and step.get('name') == name]
        if len(rows) != 1 or rows[0].get('status') != 'completed':
            journal.fail('journal_provenance_steps')
        provenance.positive(rows[0].get('number'))
        return rows[0]
    get_step = selected(policy['publication_step_name'])
    notion_step = selected(journal.NOTION_STEP)
    if (get_step.get('conclusion') != 'success' or notion_step.get('conclusion') != 'success'
            or get_step['number'] >= notion_step['number']):
        fail('rotation_not_idle')
    get_times = journal._interval(get_step, job)
    notion_times = journal._interval(notion_step, job)
    if get_times[1] > notion_times[0]:
        journal.fail('journal_provenance_time')
    expected = journal.context({'run_id': run['id'], 'run_attempt': run['run_attempt'],
        'code_sha': policy['code_sha'], 'controller_sha': run['head_sha'],
        'rules_sha256': policy['rules_sha256'], 'notion_policy_sha256': notion_hash,
        'public_policy_sha256': policy_hash})
    raw = private.expand_for(client, raw_log, policy=policy, run=run,
                             notion_policy_sha256=notion_hash)
    records, get_record = journal._records(raw, expected, notion_times, get_times, policy, run)
    report = journal._terminal_report(raw, notion_times, completed_at=notion_step.get('completed_at'))
    final = records[-1]
    result = {'status': 'journal_provenance_verified',
        'anchor': {key: final[key] for key in ('commit', 'state_blob', 'state_sha256')},
        'checkpoint': final, 'checkpoint_count': len(records),
        'run_id': run['id'], 'run_attempt': run['run_attempt'], 'job_id': job['id'],
        'get_step_record': get_record, 'get_run_success_claim': False,
        'terminal_report': report,
        'terminal_conclusions': {'job': job['conclusion'], 'notion': notion_step['conclusion'],
                                 'get': get_step['conclusion']},
        'run_binding': provenance.run_binding(run), 'completed_at': job['completed_at'],
        'job_sha256': journal.codec.digest(job), 'private_readback_required': True,
        'credentials_created': 0, 'transport': 'caller_injected', 'new_permissions': 0}

    _idle_report(report)
    resolver = client.private_proof_source
    resolver.guard()
    node = resolver.read_node(result['anchor']['commit'])
    raw = resolver.object(node, 'journal')
    state = private.strict(raw, private.MAX_OBJECT)
    import get_notion_outbox as outbox
    outbox.check_state(state)
    if (state['engine_state']['journal'] is not None
            or sha(raw) != result['anchor']['state_sha256']
            or oid(raw) != result['anchor']['state_blob']):
        fail('rotation_not_idle')
    envelope = latest_hint(resolver, at_commit=result['anchor']['commit'])
    if envelope is None: fail('rotation_native_unverified')
    verify_idle_origin(resolver, envelope, state['engine_state'])
    current_raw = resolver.object(resolver.node(0), 'journal')
    current = private.strict(current_raw, private.MAX_OBJECT)
    outbox.check_state(current)
    resolver.guard()
    history_links = prepare_history_links(resolver, result['get_step_record'], policy)
    prepared = _PreparedCanonical(_JOINT_SEAL)
    prepared.history_links = history_links
    prepared.history_links_sha256 = sha(history_links)
    prepared.result = result
    prepared.run = deepcopy(run)
    prepared.policy = deepcopy(policy)
    prepared.state = state
    prepared.envelope = envelope
    prepared.current_sha256 = sha(current_raw)
    prepared.current_engine = deepcopy(current['engine_state'])
    return prepared


def finish_canonical_journal(prepared, original, latest):
    """Pure tail checks; no IO or delayed compact expansion after tail reads."""
    import get_notion_journal_runtime as journal
    import get_publication_provenance as provenance
    if type(prepared) is not _PreparedCanonical or prepared.seal is not _JOINT_SEAL:
        fail()
    run, policy = prepared.run, prepared.policy
    journal._terminal_run(original, policy)
    journal._latest_run(latest, policy)
    if provenance.run_binding(original) != provenance.run_binding(run):
        journal.fail('journal_provenance_changed')
    clock = journal._same_run(run, latest, cross_endpoint=True)
    if (latest['run_attempt'] == run['run_attempt']
            and provenance.run_semantic_binding(latest) != provenance.run_semantic_binding(run)):
        journal.fail('journal_provenance_changed')
    if latest['run_attempt'] != run['run_attempt']:
        fail('rotation_attempt_changed')
    return {**deepcopy(prepared.result),
        'latest_binding': provenance.run_binding(latest), 'latest_attempt': latest['run_attempt'],
        **({'clock_witness': clock} if clock is not None else {})}


def install_locator(client, policy, execution, current, found, durable, request, *, writers_guard):
    """Finish current/source/native guards before installing any message writer."""
    import get_notion_checkpoint_resume as bootstrap
    import get_notion_historical_runtime as historical
    import get_notion_live_transport as transport
    import get_notion_journal_runtime as journal
    import get_notion_outbox as outbox
    import get_notion_runtime as runtime
    import get_private_proof as private
    import get_publication_provenance as p
    if not enabled(policy):
        return False
    binding = bootstrap.registry()[0]
    item = _OBSERVATIONS.pop(client, None)
    policy_hash = p.a.sha(p.a.encoded(policy))
    if item is None or item[0] != policy_hash or type(item[1]) is not VerifiedSource:
        fail('rotation_source_unverified')
    witness = item[1]
    previous = None
    envelope = found.get('locator_envelope')
    if envelope is not None:
        previous = check_checkpoint_hint(envelope, found, current)
    elif found.get('recovery_anchor') != bootstrap.TAG:
        # First deployment must use the exact old proof, not state self-hashes.
        fail('rotation_checkpoint_changed')
    def api_factory(plan):
        klass = historical.API if plan.get('historical_only') else transport.API
        return klass(request, plan['policy']['data_source_id'], sorted(row['note_id'] for row in plan['records']))
    reservation = ClosureReservation(request)
    if getattr(request, 'closure_reservation', None) is not None:
        fail('rotation_budget')
    request.closure_reservation = reservation
    if witness.needs_native:
        if current['engine_state']['journal'] is not None:
            fail('rotation_busy')
        resolver = client.private_proof_source
        marker = witness.envelope['hint']['idle_acceptance']
        if marker is None:
            fail('rotation_native_unverified')
        before = verify_idle_origin(resolver, witness.envelope, current['engine_state'])
        after = clear_after(before)
        if after['engine_state'] != current['engine_state']:
            fail('rotation_state_changed')
        reservation.begin()
        try:
            native = verify_idle_clear(before, after, api_factory(before['engine_state']['journal']['plan']),
                writers_guard=writers_guard, parent=marker['before_commit'])
            if native != marker:
                fail('rotation_native_changed')
        finally:
            reservation.end()
        resolver.guard()
    observed, observed_sha = durable.read()
    if observed != current or observed_sha != outbox.digest(current):
        fail('rotation_state_changed')
    if not runtime.e.p.writer_gate({'old_writer_evidence': writers_guard()}):
        fail('rotation_writer')
    context = journal.context({**execution, 'rules_sha256': policy['rules_sha256'],
        'notion_policy_sha256': runtime.e.digest(runtime.scope_for(policy)),
        'public_policy_sha256': policy_hash})
    durable.locator = JournalLocator(context, witness, current=current, previous=previous,
        api_factory=api_factory, writers_guard=writers_guard, reservation=reservation)
    durable.history_witness = _history_from_source(witness, policy)
    durable.verified_rotation_source = witness
    return True
