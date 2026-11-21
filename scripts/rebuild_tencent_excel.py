"""Rebuild a readable XLSX from verified private OOXML entries."""
import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from capture_tencent_complex import prior_complete


def rebuild(capture, output):
    capture, output = Path(capture), Path(output)
    snapshot = json.loads((capture/'snapshot.json').read_text())
    if not prior_complete(capture, snapshot) or output.exists():
        raise ValueError('invalid capture or output exists')
    manifest = json.loads((capture/'excel-manifest.json').read_text())
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=output.parent, prefix='.tencent-excel-')
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for item in manifest['entries']:
                z.write(capture/'excel'/item['path'], item['path'])
        with zipfile.ZipFile(tmp) as z:
            if z.testzip() is not None:
                raise ValueError('rebuilt archive corrupt')
            for item in manifest['entries']:
                h=hashlib.sha256()
                with z.open(item['path']) as src:
                    for chunk in iter(lambda: src.read(1024*1024), b''):
                        h.update(chunk)
                if h.hexdigest()!=item['sha256']:
                    raise ValueError('rebuilt entry mismatch')
        if output.exists():raise ValueError('output exists')
        os.link(tmp, output)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def main():
    p=argparse.ArgumentParser();p.add_argument('--capture-dir',required=True);p.add_argument('--output',required=True);a=p.parse_args();rebuild(a.capture_dir,a.output);print('Excel rebuilt and verified')

if __name__ == '__main__':
    try:main()
    except Exception:raise SystemExit('Excel rebuild failed; private details suppressed')
