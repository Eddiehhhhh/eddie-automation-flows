"""Fail closed before Get reads when an official staged candidate is unresolved."""
import re

import get_publication_provenance as provenance
import get_legacy_manual as m

REF = 'refs/heads/codex/get-pending'
PREFIX = 'refs/heads/codex/get-stage-'
MAX_REFS = 256


def fail():
    raise ValueError('pending_unresolved') from None


def exact_ref(api):
    value = api.call('GET', '/git/ref/' + REF.removeprefix('refs/'), missing=True)
    if (not isinstance(value, dict) or value.get('ref') != REF
            or value.get('object', {}).get('type') != 'commit'):
        fail()
    return m.a.checked(value['object'].get('sha'), 40)


def ancestor(api, older, newer):
    m.a.checked(older, 40); m.a.checked(newer, 40)
    if older == newer:
        return True
    value = api.call('GET', '/compare/' + older + '...' + newer)
    return (value.get('status') == 'ahead'
            and value.get('merge_base_commit', {}).get('sha') == older)


def check(api, client, policy, parent):
    anchor = exact_ref(api)
    if not ancestor(api, anchor, parent):
        fail()
    refs = api.list_stage_refs()
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        fail()
    seen = set()
    for row in refs:
        if not isinstance(row, dict) or not isinstance(row.get('ref'), str):
            fail()
        match = re.fullmatch(re.escape(PREFIX) + r'([1-9][0-9]{0,19})', row['ref'])
        if not match or row['ref'] in seen or row.get('object', {}).get('type') != 'commit':
            fail()
        seen.add(row['ref'])
        sha = m.a.checked(row['object'].get('sha'), 40)
        run_id = int(match.group(1))
        run = client.json('/actions/runs/' + str(run_id))
        if (not isinstance(run, dict) or run.get('id') != run_id
                or not isinstance(run.get('repository'), dict)
                or not isinstance(run.get('head_repository'), dict)
                or any(repo.get('id') != policy['repository_id'] or repo.get('full_name') != provenance.REPOSITORY
                       or repo.get('private') is not False for repo in (run['repository'], run['head_repository']))
                or type(run.get('workflow_id')) is not int or run['workflow_id'] <= 0):
            fail()
        if run.get('workflow_id') != policy['workflow_id']:
            fail()
        provenance.check_run_identity(run, policy)
        if not ancestor(api, sha, anchor):
            fail()
    return anchor
