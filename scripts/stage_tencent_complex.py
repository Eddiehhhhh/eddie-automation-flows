"""Stage a validated Tencent capture into a private-vault checkout, never push."""
import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from capture_tencent_complex import prior_complete, render_snapshot


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def index_digest(path):
    return digest(path) if path.exists() else 'absent'


def stage(capture, vault, relative, expected_index):
    capture, vault = Path(capture).resolve(), Path(vault).resolve()
    source = Path(relative)
    if source.is_absolute() or '..' in source.parts or source.parts[:2] != ('Raw', '06 TencentDocs') or len(source.parts) < 3:
        raise ValueError('invalid private destination')
    target = vault
    for part in source.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError('symlink destination')
    snapshot = json.loads((capture / 'snapshot.json').read_text())
    if not prior_complete(capture, snapshot):
        raise ValueError('capture integrity failure')
    index = target / '索引.md'
    if index.is_symlink() or index_digest(index) != expected_index:
        raise ValueError('private index changed')
    marker = '<!-- tencent-complex-managed-v1 -->'
    if index.exists() and marker not in index.read_text(encoding='utf-8'):
        raise ValueError('existing index is not managed')
    reading = render_snapshot(snapshot)
    version = hashlib.sha256((digest(capture/'snapshot.json') + digest(capture/'excel-manifest.json') + hashlib.sha256(reading.encode()).hexdigest()).encode()).hexdigest()
    version_name = '快照-' + version[:20]
    final = target / version_name
    target.mkdir(parents=True, exist_ok=True)
    if final.exists():
        if final.is_symlink() or not prior_complete(final, snapshot) or (final/'阅读入口.md').read_text(encoding='utf-8') != reading:
            raise ValueError('existing version mismatch')
    else:
        with tempfile.TemporaryDirectory(dir=target, prefix='.tencent-stage-') as temp:
            copied = Path(temp) / 'payload'
            copied.mkdir()
            manifest = json.loads((capture/'excel-manifest.json').read_text())
            paths = ['snapshot.json', 'excel-manifest.json'] + ['excel/' + entry['path'] for entry in manifest['entries']]
            for relative_path in paths:
                src = capture / relative_path
                if src.is_symlink() or not src.is_file() or not src.resolve().is_relative_to(capture):
                    raise ValueError('invalid capture entry')
                destination = copied / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, destination)
                if digest(src) != digest(destination):
                    raise ValueError('copy readback mismatch')
            (copied/'阅读入口.md').write_text(reading, encoding='utf-8')
            if not prior_complete(copied, snapshot):
                raise ValueError('staged readback mismatch')
            copied.rename(final)
    text = '\n'.join(['---', 'source_system: tencent-docs', '---', '', marker, '# 腾讯文档资料入口', '', f'- [阅读表格]({version_name}/阅读入口.md)', f'- [结构化数据]({version_name}/snapshot.json)', f'- [Excel 文件清单]({version_name}/excel-manifest.json)', '', '图片已作为 Excel 组成文件保存；与来源记录的逐张对应尚未验证。', ''])
    if index_digest(index) != expected_index:
        raise ValueError('private index changed during stage')
    fd, name = tempfile.mkstemp(dir=target, prefix='.tencent-index-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(name, index)
    finally:
        if os.path.exists(name): os.unlink(name)
    if index.read_text(encoding='utf-8') != text or (final/'阅读入口.md').read_text(encoding='utf-8') != reading or not prior_complete(final, snapshot):
        raise ValueError('final readback mismatch')
    return {'version': version_name, 'files': len(json.loads((final/'excel-manifest.json').read_text())['entries'])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture-dir', required=True)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--relative-destination', required=True)
    parser.add_argument('--expected-index', required=True)
    args = parser.parse_args()
    if args.expected_index != 'absent' and not re.fullmatch('[0-9a-f]{64}', args.expected_index):
        raise ValueError('invalid expected index')
    result = stage(args.capture_dir, args.vault, args.relative_destination, args.expected_index)
    print(json.dumps({'files': result['files'], 'staged': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Tencent private staging failed; private details suppressed')
