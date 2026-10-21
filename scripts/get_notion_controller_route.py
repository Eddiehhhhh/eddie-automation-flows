"""Bounded manual modes inside the existing single-writer controller.

Preflight is credential-free and runs before Get. Workflow inputs select only a
mode and registered digest, never a path, commit, payload or new authorization.
The actual Notion runtime retains all policy, publication and journal checks.
"""
import argparse
import os
from pathlib import Path
import tempfile

import get_notion_executor as e
import get_notion_historical as historical
import get_notion_historical_input as private_input
import get_notion_properties_runtime as properties

MODES = ('canonical', 'historical-capture', 'historical-apply', 'property-apply')


def preflight(event, apply, mode, expected):
    """Validate only public selection and reviewed registrations, without I/O."""
    if event not in ('schedule', 'workflow_dispatch') or type(apply) is not bool:
        e.fail('approval_missing')
    if mode not in MODES or not isinstance(expected, str):
        e.fail('approval_missing')
    if mode == 'canonical':
        if expected or event == 'schedule' and not apply:
            e.fail('approval_missing')
        return None
    if event != 'workflow_dispatch' or not apply:
        e.fail('approval_missing')
    if mode == 'historical-capture':
        for member in historical.capture_members(expected):
            private_input.anchor_for(member)
        return historical.CAPTURE_CONTRACT
    if mode == 'historical-apply':
        for member in historical.apply_members(expected):
            private_input.anchor_for(member)
            private_input.creation_property_for(member)
        return historical.CONTRACT
    registrations = {
        'historical-apply': historical.APPROVED_MANIFESTS,
        'property-apply': properties.APPROVED_MANIFESTS,
    }
    e.p.hex_hash(expected, 64)
    if expected not in registrations[mode]:
        e.fail('approval_missing')
    private_input.anchor_for(expected)
    return {
        'historical-capture': historical.CAPTURE_CONTRACT,
        'historical-apply': historical.CONTRACT,
        'property-apply': properties.tx.CONTRACT,
    }[mode]


def selection():
    raw = os.environ.get('APPLY')
    if raw not in ('true', 'false'):
        e.fail('approval_missing')
    mode = os.environ.get('NOTION_MODE', 'canonical')
    expected = os.environ.get('NOTION_INPUT_SHA256', '')
    contract = preflight(os.environ.get('GITHUB_EVENT_NAME'), raw == 'true', mode, expected)
    return mode, expected, contract


def dispatch(mode, expected, contract, args):
    # Repeat the pure gate before any credential lookup, including direct calls.
    selected = selection()
    if selected != (mode, expected, contract) or mode == 'canonical':
        e.fail('approval_missing')
    if not args.apply or not args.scope_from_policy:
        e.fail('approval_missing')
    remote = private_input.remote_module.GitHubRemote(None, os.environ.get('PRIVATE_VAULT_TOKEN'))
    data = (private_input.read_capture_selection(remote, expected)
            if mode == 'historical-capture'
            else private_input.read_apply_selection(remote, expected)
            if mode == 'historical-apply'
            else private_input.read(remote, expected, contract=contract))
    flag = {'historical-capture': 'historical-capture',
            'historical-apply': 'historical-manifest',
            'property-apply': 'property-manifest'}[mode]
    # mkstemp is exclusive, unpredictable and 0600, even under a permissive umask.
    fd, name = tempfile.mkstemp(prefix='get-notion-input-', suffix='.json',
                                dir=os.environ.get('RUNNER_TEMP'))
    path = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        import get_notion_runtime as runtime
        return runtime.main([
            '--policy', str(args.policy),
            '--approved-policy-sha256', args.approved_policy_sha256,
            '--publication-file', str(args.publication_file),
            '--apply', '--scope-from-policy',
            '--' + flag, str(path), '--approved-' + flag + '-sha256', expected,
        ])
    finally:
        path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--approved-policy-sha256')
    parser.add_argument('--publication-file', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--scope-from-policy', action='store_true')
    args = parser.parse_args(argv)
    try:
        mode, expected, contract = selection()
        if args.preflight:
            if (args.policy is not None or args.approved_policy_sha256 is not None
                    or args.publication_file is not None or args.apply or args.scope_from_policy):
                e.fail('approval_missing')
            print('notion_route_ready')
            return 0
        if args.policy is None or args.publication_file is None:
            e.fail('approval_missing')
        e.p.hex_hash(args.approved_policy_sha256, 64)
        return dispatch(mode, expected, contract, args)
    except Exception:
        print('notion_route_blocked')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
