"""Read one registered private Git input into a mode-0600 runner file.

Run only inside the existing single-writer workflow. No Notion access, source
mutation, journal mutation, checkout, or publication. CLI hashes alone confer
no scope. Register exact source commit/blob/path/digest after review.
"""
import argparse
import os
from pathlib import Path
import get_notion_executor as e
import get_notion_historical as h
import get_private_preview_retention as remote_module
from get_mirror_incremental_replay import strict_capture_json

INPUT_ANCHORS={}
PREFIX='.sync/get-notion/historical-inputs/'


def read(remote,expected):
    e.p.hex_hash(expected,64)
    anchor=INPUT_ANCHORS.get(expected)
    if not isinstance(anchor,dict) or set(anchor)!={'commit','blob','path','size'}:e.fail('approval_missing')
    e.p.hex_hash(anchor['commit'],40);e.p.hex_hash(anchor['blob'],40)
    if anchor['path']!=PREFIX+expected+'.json' or type(anchor['size']) is not int or not 0<anchor['size']<=256*1024:e.fail('approval_missing')
    metadata=remote.metadata()
    if metadata.get('full_name')!=remote_module.REPOSITORY or metadata.get('private') is not True:e.fail('wrong_parent')
    tree=remote.tree_entries(remote.commit(anchor['commit'])['tree'],True)
    matches=[row for row in tree if row.get('path')==anchor['path']]
    if len(matches)!=1 or matches[0].get('mode')!='100644' or matches[0].get('type')!='blob' or matches[0].get('sha')!=anchor['blob']:e.fail('source_invalid')
    data=remote.blob(anchor['blob'],anchor['size'])
    value=strict_capture_json(data,256*1024)
    if e.encoded(value)!=data or e.digest(value)!=expected:e.fail('source_invalid')
    if value.get('contract')==h.CAPTURE_CONTRACT:h.validate_capture(value,expected)
    else:h.validate(value,expected)
    return data


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
