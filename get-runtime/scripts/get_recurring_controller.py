"""Render a reviewed controller C after immutable runtime A is published.

The literal template is part of A. C contains A's SHA and the externally approved
policy, never C's own SHA. Provenance compares workflow bytes at run.head_sha
against this deterministic rendering, rather than trusting a self-hash from C.
"""
import get_private_runtime_bindings as _private_bindings
import argparse
import base64
import json
from pathlib import Path
from weakref import WeakKeyDictionary

import get_recurring_entry as entry
import get_recurring_controller_legacy as legacy
from get_recurring_entry import a, m, r

TEMPLATE = '.github/workflow-templates/get-canonical-controller.yml.disabled'
MAX_CONTROLLER = 32768
JOB_NAME = 'canonical-get'
STEP_NAME = 'Emit verified canonical publication anchor'
SCOPE_CONTRACT = 'get-recurring-production-scope-1'
SCHEDULE_BLOCK = "  schedule:\n    - cron: '17 * * * *'\n"
# Only fully verified immutable file bytes are retained, and only for the
# lifetime of the built-in bounded public transport that obtained them.
# Never cache run/attempt/job metadata, logs, or a policy/event trust decision.
_VERIFIED_CONTROLLER_BYTES = WeakKeyDictionary()


def template_bytes():
    return a.bounded_file(Path(__file__).resolve().parents[1] / TEMPLATE, MAX_CONTROLLER)


def template_sha256():
    return a.sha(template_bytes())


PRE_PRIVATE_TEMPLATE_SHA256 = '9555eb4a70a35381505a46d767aa17663489e4e4548c3803d8c16a2a90cfb4f9'

PRE_FIXED_TEMPLATE_SHA256 = '17520052435895138a6529f3a84958dddb8cc50b9c23b07e63c12e52c8f8f178'

# Exact forward-only template migration. The existing private idle capsule must
# separately authenticate the actual terminal original attempt and idle state.
FIXED_TEMPLATE_SHA256 = '1d568531b4672b89ec80eaa08532e152d9c13a400602cfaf5438a3cd24f73dc5'
PRE_FIXED_RUNTIME_SHA = '0e60865fb8e73793310c16e343d7df1717ee49c5'

def fixed_idle_template_transition(old, new):
    if not isinstance(old, dict) or not isinstance(new, dict): return False
    if (old.get('code_sha') != PRE_FIXED_RUNTIME_SHA
            or old.get('controller_template_sha256') != PRE_FIXED_TEMPLATE_SHA256
            or new.get('controller_template_sha256') != FIXED_TEMPLATE_SHA256
            or template_sha256() != FIXED_TEMPLATE_SHA256): return False
    # Validate both literal templates, not a caller-supplied digest pair.
    return (a.sha(policy_template_bytes(old)) == PRE_FIXED_TEMPLATE_SHA256
        and a.sha(policy_template_bytes(new)) == FIXED_TEMPLATE_SHA256)


def historical_template_digests():
    return (legacy.TEMPLATE_SHA256, PRE_PRIVATE_TEMPLATE_SHA256, PRE_FIXED_TEMPLATE_SHA256, FIXED_TEMPLATE_SHA256)


def policy_template_bytes(policy):
    """Select only the exact current or pinned historical controller template."""
    import get_publication_provenance as p
    digest = policy.get('controller_template_sha256') if isinstance(policy, dict) else None
    current = template_bytes()
    if digest == a.sha(current):
        return current
    if digest == FIXED_TEMPLATE_SHA256:
        raw = (Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller-fixed-v1.yml.disabled').read_bytes()
        if len(raw) > MAX_CONTROLLER or a.sha(raw) != FIXED_TEMPLATE_SHA256:
            p.fail('provenance_controller_template')
        return raw
    if digest == PRE_FIXED_TEMPLATE_SHA256:
        raw = (Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller-pre-fixed.yml.disabled').read_bytes()
        if len(raw) > MAX_CONTROLLER or a.sha(raw) != PRE_FIXED_TEMPLATE_SHA256:
            p.fail('provenance_controller_template')
        return raw
    if digest == PRE_PRIVATE_TEMPLATE_SHA256:
        raw = (Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller-pre-private.yml.disabled').read_bytes()
        if not isinstance(raw, bytes) or len(raw) > MAX_CONTROLLER or a.sha(raw) != PRE_PRIVATE_TEMPLATE_SHA256:
            p.fail('provenance_controller_template')
        return raw
    if digest == legacy.TEMPLATE_SHA256:
        raw = legacy.TEMPLATE_BYTES
        if not isinstance(raw, bytes) or len(raw) > MAX_CONTROLLER or a.sha(raw) != legacy.TEMPLATE_SHA256:
            p.fail('provenance_controller_template')
        return raw
    p.fail('provenance_controller_policy')


def scope_for(policy, policy_hash):
    return {'contract': SCOPE_CONTRACT, 'production_approved': True,
            'public_policy_sha256': policy_hash, 'rules_sha256': policy['rules_sha256'],
            'get_policy_sha256': policy['get_policy_sha256'], 'private_repository': r.REPOSITORY,
            'main_ref': r.MAIN_REF, 'max_new': 50, 'schedule_minute': 17,
            'receipt_path': entry.FULL_RECEIPT, 'sink': 'same-controller-confirmed-content-only',
            'permissions': 'existing-only',
            'notion_scope_contract': 'get-notion-standing-runtime-1', 'notion_batch_max': 50,
            'notion_journal_path': '.sync/get-notion/state.json',
            'notion_initial_proof_sha256': _private_bindings.value('get_recurring_controller', 'INITIAL_PROOF_SHA256')}


def render(policy, approved_policy_sha256, approved_scope_sha256, *, scheduled=True):
    # Full policy schema/externally approved hash validation belongs to the
    # shared provenance validator. This renderer allows only safe substitutions.
    import get_publication_provenance as p
    policy = p.validate_policy(policy, approved_policy_sha256)
    if policy['job_name'] != JOB_NAME or policy['publication_step_name'] != STEP_NAME:
        p.fail('provenance_controller_policy')
    template = policy_template_bytes(policy)
    if a.sha(a.encoded(scope_for(policy, approved_policy_sha256))) != a.checked(approved_scope_sha256, 64):
        p.fail('provenance_controller_policy')
    replacements = {'@@RUNTIME_SHA@@': a.checked(policy['code_sha'], 40),
                    '@@POLICY_B64@@': base64.b64encode(a.encoded(policy)).decode('ascii'),
                    '@@POLICY_SHA256@@': a.checked(approved_policy_sha256, 64),
                    '@@SCOPE_SHA256@@': approved_scope_sha256}
    text = template.decode('utf-8')
    if type(scheduled) is not bool or text.count(SCHEDULE_BLOCK) != 1:
        p.fail('provenance_controller_template')
    if not scheduled:
        text = text.replace(SCHEDULE_BLOCK, '', 1)
    for key, value in replacements.items():
        if key not in text:
            p.fail('provenance_controller_template')
        text = text.replace(key, value)
    if '@@' in text or len(text.encode()) > MAX_CONTROLLER:
        p.fail('provenance_controller_template')
    return text.encode()


def expected_bytes(policy, *, scheduled=True):
    # This is not a trust decision: policy was independently authenticated by
    # its external approved hash at entry. No unknown workflow is hashed here.
    digest = a.sha(a.encoded(policy))
    return render(policy, digest, a.sha(a.encoded(scope_for(policy, digest))), scheduled=scheduled)


def verify_remote(client, controller_sha, policy, *, event='schedule'):
    import get_publication_provenance as p
    if event not in ('schedule', 'workflow_dispatch'):
        p.fail('provenance_controller')
    a.checked(controller_sha, 40)
    # Validate the caller's policy even on a cache hit. A cached blob is not
    # cached approval and cannot bless a different policy or event.
    scheduled = expected_bytes(policy)
    # Only the original bounded journal wrapper may share this invocation's
    # already-verified immutable bytes. No duck-typed or recursive unwrapping,
    # dynamic response caching, caller-injected envelope or cross-client reuse.
    owner = client
    import get_notion_journal_runtime as journal_runtime
    if type(client) is journal_runtime._ReadBudget:
        owner = client.client
    cacheable = type(owner) in (p.PublicGithub, p.AnonymousGithub)
    cache = _VERIFIED_CONTROLLER_BYTES.get(owner) if cacheable else None
    def cache_key(raw):
        return (p.REPOSITORY, controller_sha, policy['workflow_path'], r.oid(raw), a.sha(raw))
    raw = None
    if cache is not None:
        matches = [(key, saved) for key, saved in cache.items()
                   if key[:3] == (p.REPOSITORY, controller_sha, policy['workflow_path'])]
        if len(matches) > 1:
            p.fail('provenance_controller')
        if matches:
            key, saved = matches[0]
            if type(saved) is not bytes or key != cache_key(saved):
                p.fail('provenance_controller')
            raw = saved
    if raw is None:
        value = client.json('/contents/' + policy['workflow_path'] + '?ref=' + controller_sha)
        if not isinstance(value, dict) or value.get('type') != 'file' or value.get('path') != policy['workflow_path'] or value.get('encoding') != 'base64' or type(value.get('size')) is not int or not 0 <= value['size'] <= MAX_CONTROLLER:
            p.fail('provenance_controller')
        try:
            raw = base64.b64decode(value['content'].replace('\n', ''), validate=True)
        except Exception:
            p.fail('provenance_controller')
        if len(raw) != value['size'] or r.oid(raw) != value.get('sha'):
            p.fail('provenance_controller')
    # Only these two reviewed renderings exist. Both use one A/policy/receipt
    # chain; the manual variant differs solely by removing the fixed cron block.
    # A schedule event can never borrow approval of the manual-only variant.
    if raw != scheduled and (event != 'workflow_dispatch' or raw != expected_bytes(policy, scheduled=False)):
        p.fail('provenance_controller_mismatch')
    if cacheable:
        if cache is None:
            cache = {}
            _VERIFIED_CONTROLLER_BYTES[owner] = cache
        cache[cache_key(raw)] = raw
    return controller_sha


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--approved-policy-sha256', required=True)
    parser.add_argument('--approved-scope-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--enable-schedule', action='store_true', help='Render the fixed minute-17 schedule only after manual acceptance')
    args = parser.parse_args(argv)
    try:
        policy = m.strict_capture_json(a.bounded_file(args.policy, 16384), 16384)
        raw = render(policy, args.approved_policy_sha256, args.approved_scope_sha256, scheduled=args.enable_schedule)
        # Local artifact only; exclusive create never overwrites an existing file.
        import os
        target = args.output.absolute()
        if '..' in target.parts or any(path.is_symlink() for path in (target, *target.parents)):
            raise ValueError('controller_output')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        print(json.dumps({'status': 'controller_rendered_local', 'controller_sha256': a.sha(raw),
                          'bytes': len(raw), 'schedule_enabled': args.enable_schedule, 'published': False})); return 0
    except Exception:
        print(json.dumps({'status': 'controller_render_failed', 'published': False})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
