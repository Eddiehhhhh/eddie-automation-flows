"""Pure local full-tree denylist check; NOT publication authorization.

Caller retains the private denylist and findings. Reports expose no matches or
filenames. Clean means only that the supplied markers were absent; independent
provenance, complete packaging and runtime-output review remain required.
"""
import hashlib
import os
from pathlib import Path
import stat

MAX_FILES = 10000
MAX_BYTES = 128 * 1024 * 1024
MAX_MARKERS = 10000

class ReleaseGuardError(ValueError): pass

def fail(): raise ReleaseGuardError('release_inventory_invalid') from None

def scan(root, markers):
    try:
        root = Path(root)
        if root.is_symlink() or not root.is_dir(): fail()
        if (not isinstance(markers, (list, tuple)) or not markers or len(markers)>MAX_MARKERS
                or any(not isinstance(m,bytes) or not 8<=len(m)<=8192 for m in markers)
                or len(set(markers))!=len(markers)): fail()
        files=0; total=0; matches=0; affected=0; inventory=hashlib.sha256()
        for directory, dirs, names in os.walk(root,followlinks=False):
            for name in dirs:
                if (Path(directory)/name).is_symlink(): fail()
            for name in sorted(names):
                path=Path(directory)/name
                descriptor=None
                try:
                    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    before=os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: fail()
                    files+=1; total+=before.st_size
                    if files>MAX_FILES or total>MAX_BYTES: fail()
                    with os.fdopen(descriptor,'rb') as handle:
                        descriptor=None; raw=handle.read(before.st_size+1); after=os.fstat(handle.fileno())
                    if len(raw)!=before.st_size or (before.st_ino,before.st_dev,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_dev,after.st_size,after.st_mtime_ns,after.st_ctime_ns): fail()
                    relative=path.relative_to(root).as_posix().encode('utf-8')
                    hits=sum(m in raw or m in relative for m in markers)
                    matches+=hits; affected+=bool(hits)
                    inventory.update(len(relative).to_bytes(8,'big')+relative+hashlib.sha256(raw).digest())
                finally:
                    if descriptor is not None: os.close(descriptor)
            dirs.sort()
        if not files: fail()
        return {'status':'blocked' if matches else 'denylist_clear_not_release_approved',
                'files':files,'bytes':total,'affected_files':affected,'marker_file_hits':matches,
                'inventory_sha256':inventory.hexdigest(),'marker_count':len(markers),
                'publication_authorized':False}
    except ReleaseGuardError: raise
    except Exception: fail()
