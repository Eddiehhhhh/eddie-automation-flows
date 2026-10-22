"""Immutable private data bindings for the existing Get runtime.

Only data is decoded; no code, URLs, credentials or destinations are executed.
A code-reviewed content digest is the authority root, not a digest supplied by
an input file, current journal, command line or environment variable. This
module performs local reads only and never changes a journal or remote state.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

CONTRACT = 'get-private-runtime-bindings-1'
TRUSTED_BUNDLE_SHA256 = 'fdb48e6578343cb6c4055f58f57134f9a9e157ea25b71933f899b1289ab3950a'
MAX_BYTES = 1024 * 1024
ENV_FILE = 'GET_PRIVATE_BINDINGS_FILE'
_CACHE = None

class BindingError(ValueError):
    """Static failure only: never include private paths, values or exceptions."""

def fail():
    raise BindingError('private_runtime_bindings_invalid') from None

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')

def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result: fail()
        result[key] = value
    return result

def _decode(value, depth=0):
    if depth > 32 or not isinstance(value, dict) or set(value) != {'type', 'value'}: fail()
    kind, item = value['type'], value['value']
    if kind == 'scalar':
        if item is not None and type(item) not in (str, bool, int): fail()
        return item
    if kind == 'bytes':
        if not isinstance(item, str) or len(item) % 2 or not re.fullmatch('[0-9a-f]*', item): fail()
        return bytes.fromhex(item)
    if kind == 'dict':
        if not isinstance(item, dict) or any(not isinstance(k, str) for k in item): fail()
        return {k: _decode(v, depth+1) for k, v in item.items()}
    if kind not in ('list', 'tuple', 'set', 'frozenset') or not isinstance(item, list): fail()
    items = [_decode(v, depth+1) for v in item]
    if kind == 'list': return items
    if kind == 'tuple': return tuple(items)
    try:
        result = set(items)
        if len(result) != len(items): fail()
        return result if kind == 'set' else frozenset(result)
    except Exception: fail()

def parse(raw, expected_sha256):
    """Pure verifier. Production callers must supply the code-pinned digest."""
    try:
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES
                or not isinstance(expected_sha256, str)
                or not re.fullmatch('[0-9a-f]{64}', expected_sha256)
                or hashlib.sha256(raw).hexdigest() != expected_sha256): fail()
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
        if (encoded(document) != raw or not isinstance(document, dict)
                or set(document) != {'contract', 'modules'} or document['contract'] != CONTRACT
                or not isinstance(document['modules'], dict) or not document['modules']): fail()
        result = {}
        for module, fields in document['modules'].items():
            if (not re.fullmatch('get_[a-z0-9_]+', module) or not isinstance(fields, dict)
                    or not fields): fail()
            result[module] = {}
            for name, value in fields.items():
                if not re.fullmatch('[A-Z_][A-Z0-9_]*', name): fail()
                result[module][name] = _decode(value)
        return result
    except BindingError: raise
    except Exception: fail()

def read_local(path, expected_sha256):
    """Require a private regular file; reject links and concurrent mutation."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1 or before.st_uid != os.geteuid()
                or not 0 < before.st_size <= MAX_BYTES): fail()
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            raw = handle.read(MAX_BYTES + 1)
            after = os.fstat(handle.fileno())
        if (len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                   != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)): fail()
        return parse(raw, expected_sha256)
    except BindingError: raise
    except Exception: fail()
    finally:
        if fd is not None: os.close(fd)

def value(module, name):
    """Return a copy from one immutable, fail-closed process-level snapshot."""
    global _CACHE
    path = os.environ.get(ENV_FILE)
    if not path: fail()
    if _CACHE is None:
        _CACHE = (path, TRUSTED_BUNDLE_SHA256, read_local(path, TRUSTED_BUNDLE_SHA256))
    if _CACHE[:2] != (path, TRUSTED_BUNDLE_SHA256): fail()
    try: return deepcopy(_CACHE[2][module][name])
    except Exception: fail()

PRIVATE_REPOSITORY = 'Eddiehhhhh/eddie-llm-wiki'
PRIVATE_PATH = '.sync/get-notion/runtime-bindings-fdb48e6578343cb6c4055f58f57134f9a9e157ea25b71933f899b1289ab3950a.json'

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
        entries=remote.tree_entries(tree,True)
        if not isinstance(entries,list) or len(entries)>100000: fail()
        selected=[v for v in entries if isinstance(v,dict) and v.get('path')==PRIVATE_PATH]
        if len(selected)!=1: fail()
        row=selected[0]
        if (row.get('type')!='blob' or row.get('mode')!='100644'
                or type(row.get('size')) is not int or not 0<row['size']<=MAX_BYTES
                or not isinstance(row.get('sha'),str) or not re.fullmatch('[0-9a-f]{40}',row['sha'])): fail()
        raw=remote.blob(row['sha'],row['size'])
        if (not isinstance(raw,bytes) or len(raw)!=row['size']
                or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=row['sha']): fail()
        parse(raw,TRUSTED_BUNDLE_SHA256)
        return raw, {'repository':PRIVATE_REPOSITORY,'commit':commit,'tree':tree,
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
