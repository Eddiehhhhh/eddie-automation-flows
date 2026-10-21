"""Read one registered private Git input into a mode-0600 runner file.

Run only inside the existing single-writer workflow. No Notion access, source
mutation, journal mutation, checkout, or publication. CLI hashes alone confer
no scope. Register exact source commit/blob/path/digest after review.
"""
import get_private_runtime_bindings as _private_bindings
import argparse
import os
from pathlib import Path
import get_notion_executor as e
import get_notion_historical as h
import get_notion_properties_runtime as properties
import get_private_preview_retention as remote_module
from get_mirror_incremental_replay import strict_capture_json

# Reviewed inventory SHA-256:
# 27992f6bcffcc604165e8fb31c292d322747ee09b48415d3dbaae3109a7fcf17
# These are existing immutable inputs, not newly generated source approvals.
INPUT_ANCHORS = _private_bindings.value('get_notion_historical_input', 'INPUT_ANCHORS')
PREFIX='.sync/get-notion/historical-inputs/'
# Exact historical APPLY digest -> existing property-input digest. Empty is
# inert. This release binding is not a caller-supplied mode or property approval.
CREATE_PROPERTY_INPUTS = {}


def anchor_for(expected):
    """Pure registration check; no credentials, remote access or self-approval."""
    e.p.hex_hash(expected,64)
    anchor=INPUT_ANCHORS.get(expected)
    if not isinstance(anchor,dict) or set(anchor)!={'commit','blob','path','size'}:e.fail('approval_missing')
    e.p.hex_hash(anchor['commit'],40);e.p.hex_hash(anchor['blob'],40)
    if anchor['path']!=PREFIX+expected+'.json' or type(anchor['size']) is not int or not 0<anchor['size']<=256*1024:e.fail('approval_missing')
    return dict(anchor)

def creation_property_for(expected, *, required=False):
    """Pure paired-anchor gate; never authorize property-only execution."""
    e.p.hex_hash(expected,64)
    paired=CREATE_PROPERTY_INPUTS.get(expected)
    if paired is None:
        if required:e.fail('approval_missing')
        return None
    e.p.hex_hash(paired,64)
    if expected not in h.APPROVED_MANIFESTS:e.fail('approval_missing')
    anchor_for(expected);anchor_for(paired)
    return paired


def read(remote,expected,*,contract=None):
    anchor=anchor_for(expected)
    if contract not in (None,h.CAPTURE_CONTRACT,h.CONTRACT,properties.tx.CONTRACT):e.fail('approval_missing')
    metadata=remote.metadata()
    if metadata.get('full_name')!=remote_module.REPOSITORY or metadata.get('private') is not True:e.fail('wrong_parent')
    tree=remote.tree_entries(remote.commit(anchor['commit'])['tree'],True)
    matches=[row for row in tree if row.get('path')==anchor['path']]
    if len(matches)!=1 or matches[0].get('mode')!='100644' or matches[0].get('type')!='blob' or matches[0].get('sha')!=anchor['blob']:e.fail('source_invalid')
    data=remote.blob(anchor['blob'],anchor['size'])
    if len(data)!=anchor['size'] or e.p.git_blob(data)!=anchor['blob']:e.fail('source_invalid')
    value=strict_capture_json(data,256*1024)
    if e.encoded(value)!=data or e.digest(value)!=expected:e.fail('source_invalid')
    if contract is not None and value.get('contract')!=contract:e.fail('approval_missing')
    if value.get('contract')==h.CAPTURE_CONTRACT:h.validate_capture(value,expected)
    elif value.get('contract')==h.CONTRACT:h.validate(value,expected)
    elif value.get('contract')==properties.tx.CONTRACT:properties.authorize(value,expected,apply=False)
    else:e.fail('approval_missing')
    return data


def validate_capture_selection(value, expected, *, apply=False):
    """Keep singleton semantics; groups require every original exact approval."""
    if not isinstance(value, dict): e.fail('approval_missing')
    if value.get('contract') != h.CAPTURE_COLLECTION_CONTRACT:
        if len(e.encoded(value)) > 256 * 1024: e.fail('request_budget')
        h.validate_capture(value, expected, apply=apply)
        return ((value, expected),)
    members = h.capture_members(expected)
    if (expected not in h.APPROVED_CAPTURE_GROUPS
            or set(value) != {'contract', 'selection_sha256', 'candidates'}
            or value['selection_sha256'] != expected
            or not isinstance(value['candidates'], list)
            or len(value['candidates']) != len(members)):
        e.fail('approval_missing')
    # All member inputs and source publications use this one fixed repository.
    # Independent original audit/user-scope hashes are retained inside each
    # exact approved member digest; grouping neither merges nor rewrites them.
    if remote_module.REPOSITORY != e.p.REPOSITORY: e.fail('wrong_parent')
    seen_ids = set(); seen_pages = set(); seen_paths = set(); common = None
    result = []
    for candidate, member in zip(value['candidates'], members):
        anchor_for(member)
        if len(e.encoded(candidate)) > 256 * 1024: e.fail('request_budget')
        h.validate_capture(candidate, member, apply=True)
        binding = (e.p.REPOSITORY,) + tuple(candidate[key] for key in
                        ('source_commit', 'data_source_id', 'database_id'))
        if common is not None and binding != common: e.fail('approval_missing')
        common = binding
        for ident, row in candidate['records'].items():
            # These registrations are capture-only existing-page cohorts.
            if row['mode'] != 'existing_placeholder': e.fail('approval_missing')
            page = row['target']['page_id']
            if ident in seen_ids or page in seen_pages or row['path'] in seen_paths:
                e.fail('duplicate_id')
            seen_ids.add(ident); seen_pages.add(page); seen_paths.add(row['path'])
        result.append((candidate, member))
    if len(seen_ids) > h.MAX_CAPTURE_RUN_RECORDS: e.fail('request_budget')
    return tuple(result)


def read_capture_selection(remote, expected):
    members = h.capture_members(expected)
    # Authenticate all registrations before the first private remote read.
    for member in members: anchor_for(member)
    data = [read(remote, member, contract=h.CAPTURE_CONTRACT) for member in members]
    if expected not in h.APPROVED_CAPTURE_GROUPS: return data[0]
    value = {'contract': h.CAPTURE_COLLECTION_CONTRACT, 'selection_sha256': expected,
             'candidates': [strict_capture_json(raw, 256 * 1024) for raw in data]}
    validate_capture_selection(value, expected, apply=True)
    return e.encoded(value)



def validate_apply_selection(value, expected, *, apply=False):
    """Collections preserve original manifests, private anchors and approvals."""
    if not isinstance(value, dict): e.fail('approval_missing')
    if value.get('contract') != h.APPLY_COLLECTION_CONTRACT:
        if len(e.encoded(value)) > 256 * 1024: e.fail('request_budget')
        h.validate(value, expected, apply=apply)
        return ((value, expected),)
    members = h.apply_members(expected)
    if (expected not in h.APPROVED_APPLY_GROUPS
            or set(value) != {'contract', 'selection_sha256', 'manifests'}
            or value['selection_sha256'] != expected
            or not isinstance(value['manifests'], list)
            or len(value['manifests']) != len(members)):
        e.fail('approval_missing')
    if remote_module.REPOSITORY != e.p.REPOSITORY: e.fail('wrong_parent')
    seen_ids = set(); seen_pages = set(); seen_paths = set(); common = None
    result = []
    for manifest, member in zip(value['manifests'], members):
        anchor_for(member)
        if len(e.encoded(manifest)) > 256 * 1024: e.fail('request_budget')
        h.validate(manifest, member, apply=True)
        binding = (e.p.REPOSITORY,) + tuple(manifest[key] for key in
                            ('source_commit', 'data_source_id', 'database_id'))
        if common is not None and binding != common: e.fail('approval_missing')
        common = binding
        for ident, row in manifest['records'].items():
            # This extension groups body adoption only, never create/properties.
            if row['mode'] != 'existing_placeholder': e.fail('approval_missing')
            page = row['target']['page_id']
            if ident in seen_ids or page in seen_pages or row['path'] in seen_paths:
                e.fail('duplicate_id')
            seen_ids.add(ident); seen_pages.add(page); seen_paths.add(row['path'])
        result.append((manifest, member))
    if len(seen_ids) > h.MAX_APPLY_RUN_RECORDS: e.fail('request_budget')
    return tuple(result)


def read_apply_selection(remote, expected):
    members = h.apply_members(expected)
    for member in members:
        anchor_for(member);creation_property_for(member)
    data = [read(remote, member, contract=h.CONTRACT) for member in members]
    if expected not in h.APPROVED_APPLY_GROUPS: return data[0]
    value = {'contract': h.APPLY_COLLECTION_CONTRACT, 'selection_sha256': expected,
             'manifests': [strict_capture_json(raw, 256 * 1024) for raw in data]}
    validate_apply_selection(value, expected, apply=True)
    return e.encoded(value)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha256',required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        data=read(remote_module.GitHubRemote(None,os.environ.get('PRIVATE_VAULT_TOKEN')),args.sha256)
        fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as handle:handle.write(data);handle.flush();os.fsync(handle.fileno())
        print('historical_input_ready');return 0
    except Exception:
        print('historical_input_blocked');return 1

if __name__=='__main__':raise SystemExit(main())
