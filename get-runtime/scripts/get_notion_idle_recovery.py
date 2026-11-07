"""One code-registered idle predecessor, disabled until real evidence is reviewed.

This is policy continuity, not transaction recovery or publication authority.
The sole capsule must be populated from the actual terminal attempt and private
readback after the engine is idle. Nothing is learned from the current state.
No clock exception, transport, writer, permission or budget is introduced here.
"""
import get_private_runtime_bindings as _private_bindings
from copy import deepcopy

TAG = 'exact_idle_predecessor_checkpoint_v1'
# Deliberately empty. Never predict a future run, checkpoint or state pin.
PREDECESSOR = _private_bindings.value('get_notion_idle_recovery', 'PREDECESSOR')
MAX_EXTRA_RECEIPTS = 16
CAPSULE_KEYS = {'public_policy', 'public_policy_sha256', 'notion_policy_sha256',
    'run_binding', 'job_id', 'job_sha256', 'get_record', 'checkpoint_count',
    'checkpoint', 'anchor', 'receipts'}
CAPTURE_KEYS = CAPSULE_KEYS | {'latest_binding', 'fixed_source_record', 'terminal_report', 'capture_sha256'}
GROUP_CAPTURE_KEYS = CAPSULE_KEYS | {'latest_binding', 'fixed_source_record', 'terminal_report', 'group_capture_evidence'}
APPLY_KEYS = CAPSULE_KEYS | {'latest_binding', 'fixed_source_record', 'terminal_report', 'apply_evidence'}


def _capture(value):
    return isinstance(value, dict) and set(value) == CAPTURE_KEYS


def _group_capture(value):
    return isinstance(value,dict) and set(value)==GROUP_CAPTURE_KEYS


def _apply(value):
    return isinstance(value,dict) and set(value)==APPLY_KEYS

def _fixed(value):
    return _capture(value) or _apply(value) or _group_capture(value)


RECEIPT_KEYS = {'commit', 'receipt_sha256', 'sequence', 'policy_sha256'}


def _capsule():
    """Validate and copy immutable code registration, never runtime input."""
    import get_notion_journal_runtime as c
    import get_notion_outbox as o
    import get_notion_run53_recovery as run53
    import get_notion_run57_recovery as run57
    p = c.provenance
    if PREDECESSOR is None:
        return None
    try:
        value = deepcopy(PREDECESSOR)
        if not isinstance(value, dict) or set(value) not in (CAPSULE_KEYS, CAPTURE_KEYS, APPLY_KEYS, GROUP_CAPTURE_KEYS):
            raise ValueError
        policy = p.validate_policy(value['public_policy'], value['public_policy_sha256'])
        c.journal.checked(value['notion_policy_sha256'], 64)
        c.journal.checked(value['job_sha256'], 64)
        p.positive(value['job_id'])
        run = value['run_binding']
        if (not isinstance(run, dict) or p.run_binding(run) != run
                or run['status'] != 'completed' or run['conclusion'] != 'success'
                or run['id'] in (run53.RUN, run57.RUN)
                or run['event'] not in ('schedule', 'workflow_dispatch')
                or run['workflow_id'] != policy['workflow_id']
                or run['path'] != policy['workflow_path']
                or run['head_branch'] != policy['head_branch']):
            raise ValueError
        p.positive(run['id']); p.positive(run['run_attempt'])
        c.journal.checked(run['head_sha'], 40)
        if p.timestamp(run['created_at']) > p.timestamp(run['updated_at']):
            raise ValueError
        record = value['get_record']
        if not _fixed(value):
            p.parse_record(p.publication_line(record).encode(), policy, run)
        elif record is not None or value['receipts'] != ():
            raise ValueError
        count = value['checkpoint_count']
        final = value['checkpoint']
        if (type(count) is not int or not 1 <= count <= c.MAX_CHECKPOINTS
                or not isinstance(final, dict) or set(final) != c.RECORD_KEYS
                or final['contract'] != c.CONTRACT
                or final['status'] != 'journal_readback_verified'
                or type(final['sequence']) is not int or final['sequence'] != count
                or final['readback_verified'] is not True
                or type(final['cas_attempts']) is not int or final['cas_attempts'] not in (0, 1)):
            raise ValueError
        expected = {'run_id': run['id'], 'run_attempt': run['run_attempt'],
            'code_sha': policy['code_sha'], 'controller_sha': run['head_sha'],
            'rules_sha256': policy['rules_sha256'],
            'notion_policy_sha256': value['notion_policy_sha256'],
            'public_policy_sha256': value['public_policy_sha256']}
        if any(final[key] != expected[key] for key in c.CONTEXT_KEYS):
            raise ValueError
        if count == 1:
            if final['previous_record_sha256'] is not None: raise ValueError
        else:
            c.journal.checked(final['previous_record_sha256'], 64)
        if final['cas_attempts']:
            c.journal.checked(final['parent_commit'], 40)
        elif final['parent_commit'] is not None:
            raise ValueError
        for key in ('commit', 'tree', 'state_blob'):
            c.journal.checked(final[key], 40)
        for key in ('state_sha256', 'record_sha256'):
            c.journal.checked(final[key], 64)
        if (type(final['state_bytes']) is not int
                or not 0 < final['state_bytes'] <= c.journal.MAX_STATE
                or o.digest({k: v for k, v in final.items() if k != 'record_sha256'}) != final['record_sha256']
                or value['anchor'] != {k: final[k] for k in ('commit', 'state_blob', 'state_sha256')}):
            raise ValueError
        if _fixed(value):
            latest = value['latest_binding']
            c._same_run(run, latest, cross_endpoint=True)
            if (p.run_binding(latest) != latest or p.run_semantic_binding(run) != p.run_semantic_binding(latest)
                    or latest['status'] != 'completed' or latest['conclusion'] != 'success'
                    or latest['run_attempt'] != run['run_attempt'] or run['event'] != 'workflow_dispatch'):
                raise ValueError
            fixed = c.fixed_auth.parse(o.encoded(value['fixed_source_record']), expected, run)
            if _apply(value):
                if fixed['mode']!='historical-apply':raise ValueError
                _check_apply_evidence(value)
                return value
            if fixed['mode'] != 'historical-capture': raise ValueError
            if _group_capture(value):
                _check_group_capture_evidence(value)
                return value
            c.journal.checked(value['capture_sha256'], 64)
            report = value['terminal_report']
            if (not isinstance(report,dict) or set(report) != {'contract','status','business_health_asserted','complete_library_claimed'}
                    or report['contract'] != 'get-private-runtime-report-2' or report['status'] != 'captured'
                    or report['business_health_asserted'] is not False
                    or report['complete_library_claimed'] is not False): raise ValueError
            return value
        receipts = value['receipts']
        if not isinstance(receipts, tuple) or len(receipts) > MAX_EXTRA_RECEIPTS:
            raise ValueError
        seen = set(); previous = 2
        for receipt in receipts:
            if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
                raise ValueError
            c.journal.checked(receipt['commit'], 40)
            c.journal.checked(receipt['receipt_sha256'], 64)
            c.journal.checked(receipt['policy_sha256'], 64)
            sequence = receipt['sequence']
            if (type(sequence) is not int or not previous < sequence <= record['receipt_sequence']
                    or receipt['commit'] in seen):
                raise ValueError
            seen.add(receipt['commit']); previous = sequence
        if record['receipt_sequence'] > 2 and not any(
                receipt['commit'] == record['receipt_commit']
                and receipt['receipt_sha256'] == record['receipt_sha256']
                and receipt['sequence'] == record['receipt_sequence'] for receipt in receipts):
            raise ValueError
        return value
    except Exception:
        c.fail('journal_provenance_record')


def _forward_policy(policy, capsule):
    """Executing rules/Get policy; unchanged or one exact fixed-source template."""
    import get_recurring_entry as entry
    import get_recurring_controller as controller
    import get_recurring_policy_recovery as recovery
    old = capsule['public_policy']
    template_transition = controller.fixed_idle_template_transition(old, policy)
    stable = set(old) - {'code_sha', 'rules_sha256', 'get_policy_sha256'}
    if template_transition: stable.remove('controller_template_sha256')
    return (isinstance(policy, dict)
        and all(policy.get(key) == old[key] for key in stable)
        and (template_transition or old['controller_template_sha256'] == controller.template_sha256())
        and policy.get('code_sha') != old['code_sha']
        and policy.get('rules_sha256') == entry.rules_sha256()
        and policy.get('get_policy_sha256') == recovery.current_get_policy()
        and policy.get('get_policy_sha256') != old['get_policy_sha256'])


def candidate_policy(policy, run):
    """Called only inside the ordinary fresh-original-attempt verifier."""
    import get_publication_provenance as p
    capsule = _capsule()
    if (capsule is not None and _forward_policy(policy, capsule)
            and p.run_binding(run) == capsule['run_binding']):
        return deepcopy(capsule['public_policy']), True
    return policy, False


def registered_run(run_id):
    capsule = _capsule()
    return capsule is not None and type(run_id) is int and run_id == capsule['run_binding']['id']


def verify_original_record(record, job_id, *, job=None):
    import get_publication_provenance as p
    import get_notion_outbox as o
    capsule = _capsule()
    if (capsule is None or _fixed(capsule) or record != capsule['get_record']
            or type(job_id) is not int or job_id != capsule['job_id']
            or not isinstance(job, dict) or job.get('id') != job_id
            or o.digest(job) != capsule['job_sha256']):
        p.fail('provenance_record')


def accepts_receipt(commit, receipt_sha256, value, requested_policy):
    """Exact additional old receipts only; normal receipt/chain checks remain."""
    import get_recurring_policy_recovery as recovery
    capsule = _capsule()
    return (capsule is not None and isinstance(value, dict)
        and requested_policy == recovery.current_get_policy()
        and any(commit == row['commit'] and receipt_sha256 == row['receipt_sha256']
            and type(value.get('sequence')) is int and value['sequence'] == row['sequence']
            and value.get('policy_sha256') == row['policy_sha256']
            and requested_policy != row['policy_sha256'] for row in capsule['receipts']))


def can_recover(policy, run_id, run_attempt):
    capsule = _capsule()
    return (capsule is not None and type(run_id) is int and type(run_attempt) is int
        and (run_id, run_attempt) == (capsule['run_binding']['id'], capsule['run_binding']['run_attempt'])
        and _forward_policy(policy, capsule))


def _matches_proof(result, capsule):
    capture = _fixed(capsule)
    fixed_matches = (not capture or (
        all(result.get(k) == capsule[k] for k in ('latest_binding','fixed_source_record','terminal_report'))
        and result.get('terminal_conclusions') == {'job':'success','notion':'success','get':'skipped'}
        and result.get('get_run_success_claim') is False)) if isinstance(result,dict) else False
    return (isinstance(result, dict) and fixed_matches
        and result.get('status') == 'journal_provenance_verified'
        and result.get('anchor') == capsule['anchor']
        and result.get('run_id') == capsule['run_binding']['id']
        and result.get('run_attempt') == capsule['run_binding']['run_attempt']
        and result.get('run_binding') == capsule['run_binding']
        and result.get('job_id') == capsule['job_id']
        and result.get('job_sha256') == capsule['job_sha256']
        and result.get('checkpoint_count') == capsule['checkpoint_count']
        and result.get('checkpoint') == capsule['checkpoint']
        and result.get('get_step_record') == capsule['get_record']
        and result.get('private_readback_required') is True
        and result.get('clock_witness') is None)


def recover_checkpoint(client, run_id, run_attempt, *, current_public_policy):
    """Same bounded client; full original chain, exact controller and fresh reread."""
    import get_notion_journal_runtime as c
    capsule = _capsule()
    if not can_recover(current_public_policy, run_id, run_attempt):
        c.fail('journal_provenance_other_controller')
    result = c.recover_anchor(client, run_id=run_id, run_attempt=run_attempt,
        public_policy=capsule['public_policy'],
        approved_public_policy_sha256=capsule['public_policy_sha256'],
        notion_policy_sha256=capsule['notion_policy_sha256'], require_success=True,
        **({'include_job_digest':True, 'include_terminal_report':True} if _fixed(capsule) else {}))
    if not _matches_proof(result, capsule):
        c.fail('journal_provenance_record')
    return {**result, 'recovery_anchor': TAG}


def transition_outer(state, sha, new_get_policy_sha256, *, proof):
    """Pure single-field change; caller MUST use ordinary confirmed CAS/readback."""
    import get_notion_outbox as o
    import get_private_preview_retention as retention
    import get_recurring_policy_recovery as recovery
    capsule = _capsule()
    o.check_state(state)
    raw = o.encoded(state)
    if (capsule is None or not _matches_proof(proof, capsule)
            or proof.get('recovery_anchor') != TAG
            or sha != capsule['anchor']['state_sha256'] or o.digest(raw) != sha
            or len(raw) != capsule['checkpoint']['state_bytes']
            or retention.oid(raw) != capsule['anchor']['state_blob']
            or state['engine_state']['journal'] is not None
            or state['policy_sha256'] != capsule['public_policy']['get_policy_sha256']
            or new_get_policy_sha256 != recovery.current_get_policy()
            or new_get_policy_sha256 == state['policy_sha256']):
        o.fail('outbox_state_invalid')
    if _capture(capsule):
        import get_notion_historical as historical
        selected = capsule['fixed_source_record']['selection_sha256']
        saved = state.get('historical_captures', {}).get(selected)
        if not isinstance(saved,dict) or o.digest(saved) != capsule['capture_sha256']:
            o.fail('outbox_state_invalid')
        historical.check_captures({selected:saved})
        historical.check_native_schema_capture(saved)
    if _group_capture(capsule):
        _check_group_capture_evidence(capsule,state)
    if _apply(capsule):
        _check_apply_evidence(capsule,state)
    result = deepcopy(state)
    result['policy_sha256'] = new_get_policy_sha256
    o.check_state(result)
    return result


def _check_apply_evidence(capsule,state=None):
    """Exact completed selection, including metadata holds; no new write grant."""
    import get_notion_outbox as o
    import get_notion_historical as historical
    c=capsule['apply_evidence'];report=capsule['terminal_report']
    keys={'manifest','property_manifest_sha256','plan_sha256','body_receipt_sha256','property_receipt_sha256','held'}
    if not isinstance(c,dict) or set(c)!=keys:o.fail('outbox_state_invalid')
    selected=capsule['fixed_source_record']['selection_sha256'];manifest=c['manifest']
    historical.validate(manifest,selected,apply=True)
    ids=set(manifest['records'])
    if (not ids or not historical.creation_ids(manifest)==ids
            or not all(isinstance(c[k],dict) and set(c[k])==ids for k in ('body_receipt_sha256','property_receipt_sha256','held'))):o.fail('outbox_state_invalid')
    for k in ('property_manifest_sha256','plan_sha256'):o.e.p.hex_hash(c[k],64)
    for ident in ids:
        o.e.p.hex_hash(c['body_receipt_sha256'][ident],64);o.e.p.hex_hash(c['property_receipt_sha256'][ident],64)
        if not isinstance(c['held'][ident],dict):o.fail('outbox_state_invalid')
    reportkeys={'contract','status','business_health_asserted','complete_library_claimed','execution_status','execution_reason','write_state','external_write_attempts','asset_downloads'}
    held=any(c['held'].values())
    if (not isinstance(report,dict) or set(report)!=reportkeys
            or report['contract']!='get-private-runtime-report-2' or report['status']!='executed'
            or report['business_health_asserted'] is not False or report['complete_library_claimed'] is not False
            or report['execution_status']!=('partial' if held else 'verified')
            or report['execution_reason'] is not None or report['write_state']!='confirmed'
            or type(report['external_write_attempts']) is not int or not 0<=report['external_write_attempts']<=len(ids)
            or type(report['asset_downloads']) is not int or report['asset_downloads']!=0):o.fail('outbox_state_invalid')
    if state is None:return
    engine=state['engine_state']
    if engine['journal'] is not None:o.fail('outbox_state_invalid')
    historical.require_creation_capture(state.get('historical_captures',{}),manifest,selected)
    for ident in ids:
        source=manifest['records'][ident];body=engine['receipts'].get(ident);prop=engine['property_receipts'].get(ident)
        if (not isinstance(body,dict) or not isinstance(prop,dict)
                or o.digest(body)!=c['body_receipt_sha256'][ident] or o.digest(prop)!=c['property_receipt_sha256'][ident]
                or body.get('readback_verified') is not True or prop.get('readback_verified') is not True
                or body.get('creation_plan_sha256')!=c['plan_sha256'] or prop.get('manifest_sha256')!=c['property_manifest_sha256']
                or prop.get('held')!=c['held'][ident] or body.get('page_id')!=prop.get('page_id')
                or body.get('note_id')!=ident or ident in engine['failures'] or ident in engine['unresolved_by_id']
                or any(r.get('source_commit')!=manifest['source_commit'] or r.get('md_blob')!=source['blob'] or r.get('data_source_id')!=manifest['data_source_id'] for r in (body,prop))):o.fail('outbox_state_invalid')


def _check_group_capture_evidence(capsule, state=None):
    """One exact completed capture group; no apply grant or hold release."""
    import get_notion_outbox as o
    import get_notion_historical as historical
    import get_notion_historical_input as private_input
    evidence = capsule['group_capture_evidence']
    selected = capsule['fixed_source_record']['selection_sha256']
    if (not isinstance(evidence,dict)
            or set(evidence) != {'descriptor','candidates','capture_sha256'}
            or selected not in historical.APPROVED_CAPTURE_GROUPS
            or evidence['descriptor'] != historical.APPROVED_CAPTURE_GROUPS[selected]):
        o.fail('outbox_state_invalid')
    members = historical.capture_members(selected)
    hashes = evidence['capture_sha256']
    if not isinstance(hashes,dict) or set(hashes) != set(members):
        o.fail('outbox_state_invalid')
    for digest in hashes.values():o.e.p.hex_hash(digest,64)
    collection = {'contract':historical.CAPTURE_COLLECTION_CONTRACT,
        'selection_sha256':selected,'candidates':evidence['candidates']}
    validated = private_input.validate_capture_selection(collection,selected,apply=True)
    report = capsule['terminal_report']
    if (not isinstance(report,dict)
            or set(report) != {'contract','status','business_health_asserted','complete_library_claimed','journal_write_state'}
            or report['contract'] != 'get-private-runtime-report-2'
            or report['status'] != 'captured'
            or report['business_health_asserted'] is not False
            or report['complete_library_claimed'] is not False
            or report['journal_write_state'] != 'confirmed'):
        o.fail('outbox_state_invalid')
    if state is None:return
    for candidate, member in validated:
        saved = state.get('historical_captures',{}).get(member)
        if (not isinstance(saved,dict) or o.digest(saved) != hashes[member]
                or saved.get('candidate') != candidate):
            o.fail('outbox_state_invalid')
        historical.check_captures({member:saved})
        historical.check_native_schema_capture(saved)

# New approval is scoped to one already idle baseline, not an old run grant.
REVIEWED_BASELINE_TAG = 'reviewed_successor_idle_baseline_v1'

def reviewed_material_sha256(document):
    """Bind all code/data except the two unavoidable self references."""
    import re
    from pathlib import Path
    import get_notion_executor as e
    import get_private_runtime_bindings as bindings
    hashes={};total=0
    paths=sorted(Path(__file__).resolve().parent.glob('*.py'))
    if not paths or len(paths)>500:e.fail('approval_missing')
    for path in paths:
        raw=e.git.bounded_file(path,2*1024*1024);total+=len(raw)
        if total>16*1024*1024:e.fail('request_budget')
        if path.name=='get_private_runtime_bindings.py':
            raw,count=re.subn(rb"TRUSTED_BUNDLE_SHA256 = '[0-9a-f]{64}'",b"TRUSTED_BUNDLE_SHA256 = '<capsule-self-reference>'",raw)
            if count!=1:e.fail('approval_missing')
        hashes[path.name]=e.digest(raw)
    document=deepcopy(document)
    try:
        target=document['modules']['get_notion_idle_recovery']['REVIEWED_BASELINE']['value']['target_material_sha256']
        if set(target)!={'type','value'} or target['type']!='scalar':e.fail('approval_missing')
        e.p.hex_hash(target['value'],64);target['value']='<target-self-reference>'
    except Exception:e.fail('approval_missing')
    return e.digest({'scripts':hashes,'capsule':e.digest(bindings.encoded(document))})


def reviewed_baseline():
    import json
    import get_notion_outbox as o
    import get_notion_checkpoint_resume as checkpoint
    b=_private_bindings.value('get_notion_idle_recovery','REVIEWED_BASELINE')
    if b is None:return None
    keys={'contract','anchor','state_bytes','old_policy_sha256','body_receipts','property_receipts',
          'unresolved','body_receipts_sha256','property_receipts_sha256','unresolved_sha256','target_material_sha256'}
    if not isinstance(b,dict) or set(b)!=keys or b['contract']!=REVIEWED_BASELINE_TAG:o.fail('approval_missing')
    if not isinstance(b['anchor'],dict) or set(b['anchor'])!={'commit','state_blob','state_sha256'}:o.fail('approval_missing')
    for key,width in [('commit',40),('state_blob',40),('state_sha256',64)]:o.e.p.hex_hash(b['anchor'][key],width)
    for key in ('old_policy_sha256','body_receipts_sha256','property_receipts_sha256','unresolved_sha256','target_material_sha256'):o.e.p.hex_hash(b[key],64)
    for key,limit in [('state_bytes',o.MAX_STATE),('body_receipts',2500),('property_receipts',2500),('unresolved',16)]:
        if type(b[key])is not int or not 0<b[key]<=limit:o.fail('approval_missing')
    if reviewed_material_sha256(json.loads(checkpoint._capsule_bytes()))!=b['target_material_sha256']:o.fail('approval_missing')
    return deepcopy(b)


def _reviewed_idle_state(state,sha,b):
    import get_notion_outbox as o
    o.check_state(state)
    engine=state['engine_state']
    if (o.digest(state)!=sha or sha!=b['anchor']['state_sha256'] or len(o.encoded(state))!=b['state_bytes']
            or state['policy_sha256']!=b['old_policy_sha256'] or engine['journal']is not None
            or len(engine['receipts'])!=b['body_receipts'] or o.digest(engine['receipts'])!=b['body_receipts_sha256']
            or len(engine.get('property_receipts',{}))!=b['property_receipts'] or o.digest(engine.get('property_receipts',{}))!=b['property_receipts_sha256']
            or len(engine.get('unresolved_by_id',{}))!=b['unresolved'] or o.digest(engine.get('unresolved_by_id',{}))!=b['unresolved_sha256']
            or engine['unresolved_by_id']!=_private_bindings.value('get_notion_plan_recovery','RETAINED_HOLDS')):
        o.fail('outbox_state_invalid')


def reviewed_baseline_anchor(raw,public_policy,modes):
    """Selection is read-only; the original RemoteJournal revalidates this pin."""
    import get_notion_outbox as o
    import get_private_preview_retention as retention
    import get_recurring_entry as entry
    b=reviewed_baseline()
    if b is None or any(mode is not None for mode in modes):return None
    if (public_policy.get('get_policy_sha256')!=entry.policy_sha256(entry.rules_sha256(),entry.BOOTSTRAP)
            or public_policy.get('rules_sha256')!=entry.rules_sha256()):o.fail('approval_missing')
    # Changed state uses normal authenticated discovery; it never borrows this grant.
    if not isinstance(raw,bytes) or o.digest(raw)!=b['anchor']['state_sha256']:return None
    if len(raw)!=b['state_bytes'] or retention.oid(raw)!=b['anchor']['state_blob']:o.fail('outbox_state_invalid')
    state=retention.strict_json(raw);_reviewed_idle_state(state,b['anchor']['state_sha256'],b)
    return {'anchor':deepcopy(b['anchor']),'recovery_anchor':REVIEWED_BASELINE_TAG,
            'approval_source':'reviewed_successor_capsule','get_run_success_claim':False,'private_readback_required':True}


def transition_reviewed_baseline(state,sha,new_get_policy_sha256):
    """Only outer policy changes; all receipts, holds and source cursors survive."""
    import get_notion_outbox as o
    import get_recurring_policy_recovery as recovery
    b=reviewed_baseline()
    if b is None or new_get_policy_sha256!=recovery.current_get_policy() or new_get_policy_sha256==b['old_policy_sha256']:o.fail('approval_missing')
    _reviewed_idle_state(state,sha,b)
    result=deepcopy(state);result['policy_sha256']=new_get_policy_sha256
    o.check_state(result)
    if {k:v for k,v in result.items()if k!='policy_sha256'}!={k:v for k,v in state.items()if k!='policy_sha256'}:o.fail('outbox_state_invalid')
    return result
