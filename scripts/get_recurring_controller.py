"""Render a reviewed controller C after immutable runtime A is published.

The literal template is part of A. C contains A's SHA and the externally approved
policy, never C's own SHA. Provenance compares workflow bytes at run.head_sha
against this deterministic rendering, rather than trusting a self-hash from C.
"""
import argparse
import base64
import json
from pathlib import Path

import get_recurring_entry as entry
from get_recurring_entry import a, m, r

TEMPLATE = '.github/workflow-templates/get-canonical-controller.yml.disabled'
MAX_CONTROLLER = 32768
JOB_NAME = 'canonical-get'
STEP_NAME = 'Emit verified canonical publication anchor'
SCOPE_CONTRACT = 'get-recurring-production-scope-1'


def template_bytes():
    return a.bounded_file(Path(__file__).resolve().parents[1] / TEMPLATE, MAX_CONTROLLER)


def template_sha256():
    return a.sha(template_bytes())


def scope_for(policy, policy_hash):
    return {'contract': SCOPE_CONTRACT, 'production_approved': True,
            'public_policy_sha256': policy_hash, 'rules_sha256': policy['rules_sha256'],
            'get_policy_sha256': policy['get_policy_sha256'], 'private_repository': r.REPOSITORY,
            'main_ref': r.MAIN_REF, 'max_new': 50, 'schedule_minute': 17,
            'receipt_path': entry.FULL_RECEIPT, 'sink': 'same-controller-confirmed-content-only',
            'permissions': 'existing-only',
            'notion_scope_contract': 'get-notion-standing-runtime-1', 'notion_batch_max': 50,
            'notion_journal_path': '.sync/get-notion/state.json',
            'notion_initial_proof_sha256': 'f3b616ec734324e785f4258482fb961ee937161316375ad8b28ea29c7d60f11e'}


def render(policy, approved_policy_sha256, approved_scope_sha256):
    # Full policy schema/externally approved hash validation belongs to the
    # shared provenance validator. This renderer allows only safe substitutions.
    import get_publication_provenance as p
    policy = p.validate_policy(policy, approved_policy_sha256)
    if policy['controller_template_sha256'] != template_sha256() or policy['job_name'] != JOB_NAME or policy['publication_step_name'] != STEP_NAME:
        p.fail('provenance_controller_policy')
    if a.sha(a.encoded(scope_for(policy, approved_policy_sha256))) != a.checked(approved_scope_sha256, 64):
        p.fail('provenance_controller_policy')
    replacements = {'@@RUNTIME_SHA@@': a.checked(policy['code_sha'], 40),
                    '@@POLICY_B64@@': base64.b64encode(a.encoded(policy)).decode('ascii'),
                    '@@POLICY_SHA256@@': a.checked(approved_policy_sha256, 64),
                    '@@SCOPE_SHA256@@': approved_scope_sha256}
    text = template_bytes().decode('utf-8')
    for key, value in replacements.items():
        if key not in text:
            p.fail('provenance_controller_template')
        text = text.replace(key, value)
    if '@@' in text or len(text.encode()) > MAX_CONTROLLER:
        p.fail('provenance_controller_template')
    return text.encode()


def expected_bytes(policy):
    # This is not a trust decision: policy was independently authenticated by
    # its external approved hash at entry. No unknown workflow is hashed here.
    digest = a.sha(a.encoded(policy))
    return render(policy, digest, a.sha(a.encoded(scope_for(policy, digest))))


def verify_remote(client, controller_sha, policy):
    import get_publication_provenance as p
    a.checked(controller_sha, 40)
    value = client.json('/contents/' + policy['workflow_path'] + '?ref=' + controller_sha)
    if not isinstance(value, dict) or value.get('type') != 'file' or value.get('path') != policy['workflow_path'] or value.get('encoding') != 'base64' or type(value.get('size')) is not int or not 0 <= value['size'] <= MAX_CONTROLLER:
        p.fail('provenance_controller')
    try:
        raw = base64.b64decode(value['content'].replace('\n', ''), validate=True)
    except Exception:
        p.fail('provenance_controller')
    if len(raw) != value['size'] or r.oid(raw) != value.get('sha'):
        p.fail('provenance_controller')
    if raw != expected_bytes(policy):
        p.fail('provenance_controller_mismatch')
    return controller_sha


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--approved-policy-sha256', required=True)
    parser.add_argument('--approved-scope-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        policy = m.strict_capture_json(a.bounded_file(args.policy, 16384), 16384)
        raw = render(policy, args.approved_policy_sha256, args.approved_scope_sha256)
        # Local artifact only; exclusive create never overwrites an existing file.
        import os
        target = args.output.absolute()
        if '..' in target.parts or any(path.is_symlink() for path in (target, *target.parents)):
            raise ValueError('controller_output')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        print(json.dumps({'status': 'controller_rendered_local', 'controller_sha256': a.sha(raw),
                          'bytes': len(raw), 'published': False})); return 0
    except Exception:
        print(json.dumps({'status': 'controller_render_failed', 'published': False})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
