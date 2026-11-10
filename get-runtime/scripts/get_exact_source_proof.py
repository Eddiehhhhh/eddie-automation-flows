"""Bounded commit-only verification for one previously authenticated publication.

This resolver is invoked only for the exact registered original run and pointer.
The ordinary private-proof history budget remains unchanged.
"""
import hashlib
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time

MAX_CHAIN = 4096
MAX_PACK_BYTES = 128 * 1024 * 1024
TIMEOUT_SECONDS = 120
ORIGINAL_RUN_ID = 40657306710
ORIGINAL_POINTER_SHA256 = 'd5e1232fafe1cf03fd576c4ec938a1d802b24b9db4426710b12d803f54d7823e'


def _cap_file():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PACK_BYTES, MAX_PACK_BYTES))


def _chain(snapshot, remote, head, observed, origin):
    """Prove a first-parent chain with recomputed commit hashes; no tree fetch."""
    import get_private_preview_retention as retention
    env = retention.safe_environment(remote.token)
    env['GIT_NO_LAZY_FETCH'] = '1'
    env['GIT_TERMINAL_PROMPT'] = '0'
    deadline=time.monotonic()+TIMEOUT_SECONDS
    def command(*args, data=None):
        remaining=deadline-time.monotonic()
        if remaining<=0: raise ValueError('exact_proof_timeout')
        with tempfile.TemporaryFile(dir=snapshot) as output:
            result = subprocess.run(['git', '-C', str(snapshot), *args], env=env,
                stdout=output, stderr=subprocess.DEVNULL, input=data, timeout=remaining,
                preexec_fn=_cap_file, check=False)
            if result.returncode: raise ValueError('exact_proof_git')
            if output.tell()>16*1024*1024: raise ValueError('exact_proof_output')
            output.seek(0); return output.read()
    command('init', '--bare', '--quiet')
    command('-c', 'protocol.version=2', 'fetch', '--depth='+str(MAX_CHAIN),
        '--filter=tree:0', '--no-tags', '--no-recurse-submodules', retention.REMOTE_URL,
        '+refs/heads/main:refs/heads/exact-proof')
    if command('rev-parse', 'refs/heads/exact-proof').decode().strip() != head:
        raise ValueError('exact_proof_head')
    commits = command('rev-list', '--first-parent', '--max-count='+str(MAX_CHAIN), head).decode().splitlines()
    if not commits or commits[0] != head or observed not in commits or origin not in commits:
        raise ValueError('exact_proof_ancestry')
    oi, ri = commits.index(observed), commits.index(origin)
    if oi > ri: raise ValueError('exact_proof_ancestry')
    chosen = commits[:ri+1]
    packed = command('cat-file', '--batch', data=('\n'.join(chosen)+'\n').encode())
    import io
    stream=io.BytesIO(packed)
    trees={}
    for index, expected in enumerate(chosen):
        header=stream.readline().decode().split()
        if len(header)!=3 or header[:2]!=[expected,'commit'] or not header[2].isdigit():
            raise ValueError('exact_proof_commit')
        size=int(header[2])
        if not 0<size<=1024*1024: raise ValueError('exact_proof_commit')
        raw=stream.read(size)
        if len(raw)!=size or stream.read(1)!=b'\n' or hashlib.sha1(b'commit '+str(size).encode()+b'\0'+raw).hexdigest()!=expected:
            raise ValueError('exact_proof_commit')
        headers=raw.split(b'\n\n',1)[0].splitlines()
        roots=[x[5:].decode() for x in headers if x.startswith(b'tree ')]
        if len(roots)!=1: raise ValueError('exact_proof_commit')
        if expected in (observed,origin): trees[expected]=roots[0]
        parents=[x[7:].decode() for x in headers if x.startswith(b'parent ')]
        if len(parents)>1 or index+1<len(chosen) and parents!=[chosen[index+1]]:
            raise ValueError('exact_proof_ancestry')
    if stream.read(1): raise ValueError('exact_proof_commit')
    return trees



def resolve(source, pointer, policy, run):
    """Return None unless exact independently registered producer matches."""
    import get_publication_registered_predecessor as registered
    import get_publication_provenance as provenance
    import get_private_proof as proof
    if run.get('id')!=ORIGINAL_RUN_ID or run.get('run_attempt')!=1 or pointer.get('digest')!=ORIGINAL_POINTER_SHA256:
        return None
    rows = [row for row in registered.registry()
        if provenance.run_binding(run)==row['run_binding'] and policy==row['public_policy']]
    if len(rows)!=1 or proof.pointer('publication',rows[0]['get_record'])!=pointer:
        return None
    record=rows[0]['get_record']; observed=record['observed_main_commit']; origin=record['receipt_commit']
    source.guard(); head=source.head
    with tempfile.TemporaryDirectory(prefix='get-exact-proof-') as directory:
        trees=_chain(Path(directory), source.remote, head, observed, origin)
    # The ordinary source reader authenticates the exact commit, every path
    # component tree, and the receipt bytes with the original bounded checks.
    onode=source.read_node(origin); vnode=source.read_node(observed)
    if onode['tree']!=trees[origin] or vnode['tree']!=trees[observed]: raise ValueError('exact_proof_tree')
    first=source.object(onode,'publication'); second=source.object(vnode,'publication')
    if first is None or first!=second or proof.sha(first)!=record['receipt_sha256']:
        raise ValueError('exact_proof_receipt')
    receipt=proof.strict(first,proof.MAX_OBJECT,serializer=proof.public_encoded)
    if (onode['parent']!=receipt.get('parent_commit') or receipt.get('sequence')!=record['receipt_sequence']):
        raise ValueError('exact_proof_receipt')
    source.guard()
    if source.head!=head: raise ValueError('exact_proof_head')
    return record
