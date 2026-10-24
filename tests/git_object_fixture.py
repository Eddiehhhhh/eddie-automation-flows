"""Deterministic, offline object damage in disposable test repositories only."""
import os
from pathlib import Path
import re
import subprocess


AUTO_CONFIG = {'gc.auto': '0', 'gc.autoDetach': 'false', 'maintenance.auto': 'false'}


def git(repo, *args, data=None, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    command = ['git', '-c', 'protocol.allow=never']
    for key, value in AUTO_CONFIG.items():
        command += ['-c', key + '=' + value]
    return subprocess.run(command + ['-C', str(repo), *args], input=data,
                          env=env, capture_output=True, check=check)


def disable_auto_maintenance(repo):
    # Persist locally so production helpers invoked by a test inherit the same
    # fixture constraint; explicit pack/unpack operations remain available.
    for key, value in AUTO_CONFIG.items():
        git(repo, 'config', '--local', key, value)


def assert_missing(repo, *oids):
    result = git(repo, 'cat-file', '--batch-check', data=('\n'.join(oids) + '\n').encode())
    expected = ''.join(oid + ' missing\n' for oid in oids).encode()
    if result.stdout != expected:
        raise AssertionError('fixture object is still readable')


def remove_objects(repo, *oids):
    """Pack all PRESENT objects once, unpack once, then remove the whole cohort.

    This includes unreferenced candidate commits and their blobs. Enumerating
    existing objects rather than walking refs keeps a real partial clone's
    already-missing assets missing, with all transports disabled. Returns each
    removed object's loose path and original compressed bytes for corruption
    tests. Missing targets are errors, never skipped or repaired by a fetch.
    """
    repo = Path(repo)
    if not oids or len(set(oids)) != len(oids) or any(not re.fullmatch(r'[0-9a-f]{40}', oid) for oid in oids):
        raise AssertionError('invalid fixture object cohort')
    objects = repo / '.git/objects'
    if not objects.is_dir() or (objects / 'info/alternates').exists():
        raise AssertionError('fixture requires its own non-bare object store')
    disable_auto_maintenance(repo)
    # Validate every target before any layout change or deletion.
    original = {oid: git(repo, 'cat-file', '-p', oid).stdout for oid in oids}
    inventory = git(repo, 'cat-file', '--batch-all-objects', '--batch-check=%(objectname)').stdout
    pack_dir = objects / 'pack'; pack_dir.mkdir(exist_ok=True)
    # No --revs: do not walk a missing object's references or omit dangling
    # candidate objects. No deltas: the resulting pack is self-contained.
    packed = git(repo, 'pack-objects', '--window=0', '--no-reuse-delta',
                 str(pack_dir / 'pack'), data=inventory).stdout.decode().strip()
    payload = (pack_dir / ('pack-' + packed + '.pack')).read_bytes()
    git(repo, 'prune-packed')
    paths = {oid: objects / oid[:2] / oid[2:] for oid in oids}
    for oid, path in paths.items():
        if path.exists() or git(repo, 'cat-file', '-p', oid).stdout != original[oid]:
            raise AssertionError('fixture did not exercise a packed target')
    for path in pack_dir.iterdir():
        path.unlink()
    git(repo, 'unpack-objects', '-r', data=payload)
    after = git(repo, 'cat-file', '--batch-all-objects', '--batch-check=%(objectname)').stdout
    if set(after.splitlines()) != set(inventory.splitlines()):
        raise AssertionError('fixture normalization changed the present object set')
    removed = {}
    for oid, path in paths.items():
        if git(repo, 'cat-file', '-p', oid).stdout != original[oid]:
            raise AssertionError('fixture object readback changed')
        removed[oid] = (path, path.read_bytes())
    for path, _ in removed.values():
        path.unlink()
    assert_missing(repo, *oids)
    return removed
