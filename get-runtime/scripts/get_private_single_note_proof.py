"""Resolve a public proof key to one private Get note ID.

The selector is safe to expose in workflow metadata. The ID, canonical source
path, source blob, and nonce come only from the code-hash-bound private bundle.
"""
import os
import re
import stat
from pathlib import Path

import get_private_runtime_bindings as bindings

CONTRACT = 'get-private-single-note-proof-1'
KEYS = frozenset(('recording-card-20261108',))


def fail():
    raise ValueError('private_single_note_proof_invalid') from None


def resolve(proof_key):
    if not isinstance(proof_key, str) or proof_key not in KEYS:
        fail()
    values = bindings.value('get_private_single_note_proof', 'PROOF_KEYS')
    if not isinstance(values, dict) or set(values) != KEYS:
        fail()
    seen_ids = set()
    for key, row in values.items():
        if (key not in KEYS or not isinstance(row, dict)
                or set(row) != {'note_id', 'source_path', 'source_blob_sha', 'nonce'}
                or not isinstance(row['note_id'], str)
                or not re.fullmatch(r'[0-9]{1,64}', row['note_id'])
                or row['note_id'] in seen_ids
                or not isinstance(row['source_path'], str)
                or not row['source_path'].startswith('Raw/03 Get/99 录音卡笔记/')
                or not row['source_path'].endswith('-' + row['note_id'] + '.md')
                or not isinstance(row['source_blob_sha'], str)
                or not re.fullmatch(r'[0-9a-f]{40}', row['source_blob_sha'])
                or not isinstance(row['nonce'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['nonce'])):
            fail()
        seen_ids.add(row['note_id'])
    return values[proof_key]['note_id']


def runtime_note_id():
    value = os.environ.get('GET_SINGLE_NOTE_ID')
    if value:
        return value if re.fullmatch(r'[0-9]{1,64}', value) else None
    if os.environ.get('GET_SINGLE_NOTE_MODE', '').lower() != 'true':
        return None
    path = runtime_path()
    if path is None:
        return None
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 66):
            return None
        raw = os.read(fd, 67)
        if len(raw) > 66 or not raw.endswith(b'\n'):
            return None
        value = raw[:-1].decode('ascii')
        return value if re.fullmatch(r'[0-9]{1,64}', value) else None
    except Exception:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def runtime_path():
    runner_temp = os.environ.get('RUNNER_TEMP')
    run_id, attempt = os.environ.get('GITHUB_RUN_ID'), os.environ.get('GITHUB_RUN_ATTEMPT')
    if (not runner_temp or not Path(runner_temp).is_absolute()
            or not re.fullmatch(r'[1-9][0-9]{0,19}', run_id or '')
            or not re.fullmatch(r'[1-9][0-9]{0,4}', attempt or '')):
        return None
    root = Path(runner_temp)
    try:
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            return None
        return root.resolve(strict=True) / f'get-single-note-proof-{run_id}-{attempt}.txt'
    except Exception:
        return None


def export_to_github_env(proof_key, github_env):
    note_id = resolve(proof_key)
    path = Path(github_env)
    if not path.is_file() or path.is_symlink():
        fail()
    proof_path = runtime_path()
    if proof_path is None:
        fail()
    print(f'::add-mask::{note_id}', flush=True)
    fd = os.open(proof_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            fail()
        raw = (note_id + '\n').encode('ascii')
        if os.write(fd, raw) != len(raw):
            fail()
        os.fsync(fd)
    finally:
        os.close(fd)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(f'GET_SINGLE_NOTE_ID={note_id}\n')
        stream.flush()
        os.fsync(stream.fileno())
    return {'status': 'private_single_note_proof_resolved'}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--key', required=True)
    parser.add_argument('--github-env', required=True)
    args = parser.parse_args(argv)
    try:
        result = export_to_github_env(args.key, args.github_env)
        print('{"status":"private_single_note_proof_resolved"}', flush=True)
        return 0
    except Exception:
        print('{"status":"private_single_note_proof_invalid"}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
