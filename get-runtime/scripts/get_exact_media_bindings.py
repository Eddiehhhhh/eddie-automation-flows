"""Fixed, exact five-digest private binding; never replaces the legacy bundle."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import get_private_runtime_bindings as legacy

BindingError = legacy.BindingError
fail = legacy.fail
encoded = legacy.encoded
MAX_BYTES = 2048
ENV_FILE = 'GET_EXACT_MEDIA_BINDINGS_FILE'
MODULE = 'get_notion_exact_media_projection'
FIELDS = frozenset(('ID_SHA','SOURCE_SHA','IMAGE_LINE_SHA','AUDIO_URL_SHA','METADATA_SHA'))
_CACHE = None
TRUSTED_BUNDLE_SHA256 = '1c07a13a5da14c35ff8c4627d0520976deef36833933fbbd64226be661aa8e8d'

def parse(raw, expected_sha256):
    if expected_sha256 != TRUSTED_BUNDLE_SHA256 or not isinstance(raw,bytes) or len(raw)>MAX_BYTES: fail()
    result = legacy.parse(raw, TRUSTED_BUNDLE_SHA256)
    if set(result) != {MODULE} or set(result[MODULE]) != FIELDS: fail()
    if any(type(v) is not str or re.fullmatch('[0-9a-f]{64}',v) is None for v in result[MODULE].values()): fail()
    return result

def read_local(path, expected_sha256):
    if expected_sha256 != TRUSTED_BUNDLE_SHA256: fail()
    result = legacy.read_local(path, TRUSTED_BUNDLE_SHA256)
    if set(result) != {MODULE} or set(result[MODULE]) != FIELDS: fail()
    if any(type(v) is not str or re.fullmatch('[0-9a-f]{64}',v) is None for v in result[MODULE].values()): fail()
    return result

def value(module,name):
    global _CACHE
    if module != MODULE or name not in FIELDS: fail()
    path=os.environ.get(ENV_FILE)
    if not path: fail()
    if _CACHE is None:
        _CACHE=(path,TRUSTED_BUNDLE_SHA256,read_local(path,TRUSTED_BUNDLE_SHA256))
    if _CACHE[:2] != (path,TRUSTED_BUNDLE_SHA256): fail()
    return deepcopy(_CACHE[2][module][name])

PRIVATE_REPOSITORY = 'Eddiehhhhh/eddie-llm-wiki'
PRIVATE_PATH = '.sync/get-notion/exact-media-bindings-' + TRUSTED_BUNDLE_SHA256 + '.json'

def verified_private_blob(remote, commit):
    """Read only the fixed private destination; return PRIVATE evidence to caller.

    No ref update, blob creation, scope relaxation or caller-provided trust pin.
    The exact commit/tree/blob and byte identity are checked before decoding.
    """
    try:
        if not isinstance(commit,str) or not re.fullmatch('[0-9a-f]{40}',commit): fail()
        metadata=remote.metadata()
        if metadata.get('full_name')!=PRIVATE_REPOSITORY or metadata.get('private') is not True: fail()
        tree=remote.commit(commit)['tree']
        if not isinstance(tree,str) or not re.fullmatch('[0-9a-f]{40}',tree): fail()
        root_tree=tree
        # Resolve only the fixed path; an unrelated large repository tree may be truncated.
        parts=PRIVATE_PATH.split('/')
        for index,part in enumerate(parts):
            entries=remote.tree_entries(tree,False)
            if not isinstance(entries,list) or len(entries)>100000: fail()
            selected=[v for v in entries if isinstance(v,dict) and v.get('path')==part]
            if len(selected)!=1: fail()
            row=selected[0]
            if index<len(parts)-1:
                tree=row.get('sha')
                if row.get('type')!='tree' or row.get('mode')!='040000' or not isinstance(tree,str) or not re.fullmatch('[0-9a-f]{40}',tree): fail()
        if (row.get('type')!='blob' or row.get('mode')!='100644'
                or type(row.get('size')) is not int or not 0<row['size']<=MAX_BYTES
                or not isinstance(row.get('sha'),str) or not re.fullmatch('[0-9a-f]{40}',row['sha'])): fail()
        raw=remote.blob(row['sha'],row['size'])
        if (not isinstance(raw,bytes) or len(raw)!=row['size']
                or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=row['sha']): fail()
        parse(raw,TRUSTED_BUNDLE_SHA256)
        return raw, {'repository':PRIVATE_REPOSITORY,'commit':commit,'tree':root_tree,
                     'path':PRIVATE_PATH,'blob':row['sha'],'bytes':len(raw),'sha256':TRUSTED_BUNDLE_SHA256}
    except BindingError: raise
    except Exception: fail()

def materialize(raw, destination):
    """Create-only local 0600 file, then verify bytes; never replace another file.

    The caller owns the isolated directory and formal preflight/unique-writer
    gates. This primitive grants no remote publication or state write authority.
    """
    fd=None
    try:
        parse(raw,TRUSTED_BUNDLE_SHA256)
        path=Path(destination); parent=path.parent
        info=parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid()
                or stat.S_IMODE(info.st_mode)&0o022): fail()
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as handle:
            fd=None; handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        read_local(path,TRUSTED_BUNDLE_SHA256)
        return {'status':'private_bindings_materialized','bytes':len(raw)}
    except BindingError: raise
    except Exception: fail()
    finally:
        if fd is not None: os.close(fd)

def bootstrap(remote, destination, commit=None):
    """Read fixed private blob and materialize before any production imports."""
    try:
        if commit is None: commit=remote.ref('refs/heads/main')
        raw, proof=verified_private_blob(remote,commit)
        parent=Path(destination).parent
        parent.mkdir(mode=0o700,parents=False,exist_ok=False)
        result=materialize(raw,destination)
        # Detailed provenance is local-private, never stdout or a public artifact.
        proof_path=parent/'readback-proof.json'
        fd=os.open(proof_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        proof_raw=encoded(proof)
        with os.fdopen(fd,'wb') as handle:
            handle.write(proof_raw);handle.flush();os.fsync(handle.fileno())
        if proof_path.read_bytes()!=proof_raw: fail()
        return result
    except BindingError: raise
    except Exception: fail()

def main(argv=None):
    import sys
    argv=sys.argv[1:] if argv is None else argv
    try:
        if len(argv)!=2 or argv[0]!='--destination' or not isinstance(argv[1],str): fail()
        from get_notion_route_selection import selection
        selection()  # Unknown scopes are rejected before any token lookup.
        token=os.environ.get('PRIVATE_VAULT_TOKEN')
        if not token: fail()
        # This transport has a fixed repository and redirect refusal. No runtime
        # modules needing private bindings are imported before materialization.
        from get_private_preview_retention import GitHubRemote
        bootstrap(GitHubRemote(None,token),argv[1])
        print('{"status":"private_bindings_materialized"}',flush=True)
        return 0
    except Exception:
        print('{"status":"private_runtime_bindings_invalid"}',flush=True)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
