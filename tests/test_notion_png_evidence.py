"""Synthetic offline Git evidence; no production content or network transport."""
from copy import deepcopy
from pathlib import Path
import json
import os
import struct
import subprocess
import tempfile
import unittest
import zlib

import get_legacy_adapter as git
import get_recurring_entry as entry
import get_notion_png_evidence as x


def png(width=2, height=2):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'sRGB', b'\0') + chunk(b'IDAT', zlib.compress((b'\0' + b'\0\x11\x22' * width) * height)) + chunk(b'IEND', b''))


class Fixture:
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir()
        self.run('init', '-q', '-b', 'main'); self.run('config', 'user.name', 'Fixture')
        self.run('config', 'user.email', 'fixture@example.invalid'); self.run('config', 'gc.auto', '0')
        self.write('Raw/03 Get/03 State/receipt.json', b'{}')
        self.write('inherited.bin', b'Never downloaded inherited bytes')
        self.commit()
        self.data = {}; self.urls = {}; self.images = {}; self.records = {}
        for i in range(3):
            ident = str(100 + i); url = f'https://get-notes.umiwi.com/synthetic-{i}.png?signature=example'
            md = ('---\nnote_id: ' + json.dumps(ident) + '\ntitle: "Synthetic"\n---\n# Exact text\n\n![Caption ' + str(i) + '](' + url + ')\n\nTail\n').encode()
            path = 'Raw/03 Get/notes/' + ident + '.md'
            self.data[ident] = md; self.urls[ident] = url; self.images[ident] = png(i + 2, 2)
            self.records[ident] = {'path': path, 'blob': x.p.git_blob(md), 'sha256': x.digest(md)}
            self.write(path, md)
        self.publication = self.commit(); self.parent = self.publication
        self.descriptor = {'repository': x.p.REPOSITORY, 'commit': self.publication,
                           'parent': self.run('rev-parse', 'HEAD^').decode().strip(),
                           'changed_content_ids': sorted(self.records), 'records': deepcopy(self.records),
                           'content_tree': {r['path']: r['blob'] for r in self.records.values()}}
        self.manifest = {'contract': x.CONTRACT, 'repository': x.p.REPOSITORY,
                         'publication_commit': self.publication, 'records': {}}
        for ident in self.records:
            image = self.images[ident]; sha = x.digest(image); width, height = x.png_shape(image)
            asset = {'path': x.PREFIX + sha + '.png', 'blob': x.p.git_blob(image), 'sha256': sha,
                     'bytes': len(image), 'mime': 'image/png', 'width': width, 'height': height}
            self.manifest['records'][ident] = {'source': self.records[ident],
                                             'reference_sha256': x.digest(self.urls[ident].encode()), 'asset': asset}
        self.publish()

    def run(self, *args, data=None, root=None, check=True, env=None):
        return subprocess.run(['git', '-C', str(root or self.root), *args], input=data,
                              check=check, capture_output=True, env=env).stdout

    def write(self, path, data):
        target = self.root / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)

    def commit(self):
        self.run('add', '.'); self.run('commit', '-qm', 'Synthetic fixture')
        return self.run('rev-parse', 'HEAD').decode().strip()

    def publish(self, raw=None, extra=None):
        self.run('reset', '--hard', self.parent)
        for ident, row in self.manifest['records'].items(): self.write(row['asset']['path'], self.images[ident])
        data = x.encoded(self.manifest) if raw is None else raw
        self.write(x.MANIFEST_PATH, data)
        if extra:
            for path, value in extra.items(): self.write(path, value)
        self.asset_commit = self.commit()
        self.anchor = {'contract': x.CONTRACT, 'repository': x.p.REPOSITORY,
                       'publication_commit': self.publication, 'asset_commit': self.asset_commit,
                       'manifest_path': x.MANIFEST_PATH, 'manifest_blob': x.p.git_blob(data),
                       'manifest_sha256': x.digest(data)}

    def evidence(self, **kwargs):
        reader = kwargs.pop('reader', git.OfflineGit(self.root))
        return x.AssetEvidence(reader, kwargs.pop('descriptor', self.descriptor),
                               kwargs.pop('anchor', self.anchor), kwargs.pop('expected', x.digest(self.anchor)),
                               kwargs.pop('current', self.run('rev-parse', 'HEAD').decode().strip()), **kwargs)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name) / 'source')

    def test_exact_three_resolve_preserves_original_publication_and_get_history(self):
        f = self.f; before = x.encoded(f.descriptor); e = f.evidence()
        self.assertEqual(len(e.asset_tree), 3)
        self.assertEqual(x.encoded(f.descriptor), before)
        self.assertEqual(git.OfflineGit(f.root).source_get_tree(f.parent), git.OfflineGit(f.root).source_get_tree(f.asset_commit))
        entry.unchanged_get_history(git.OfflineGit(f.root), f.publication, f.asset_commit)
        for ident in f.records:
            b = e.resolve(ident, f.data[ident], f.urls[ident], current_commit=f.asset_commit)
            self.assertEqual(e.read_asset(b, current_commit=f.asset_commit), f.images[ident])
            self.assertEqual(b['source_commit'], f.publication)
            self.assertNotIn('https://', x.encoded(b).decode())
        self.assertEqual(x.encoded(f.descriptor), before)

    def test_anchor_cannot_be_self_approved_after_change(self):
        bad = deepcopy(self.f.anchor); bad['manifest_sha256'] = 'a' * 64
        with self.assertRaisesRegex(x.Blocked, 'asset_evidence_anchor'): self.f.evidence(anchor=bad)
        for key in ('repository', 'contract', 'publication_commit', 'manifest_path'):
            bad = deepcopy(self.f.anchor); bad[key] = 'incorrect'
            with self.subTest(key=key), self.assertRaises(x.Blocked): self.f.evidence(anchor=bad, expected=x.digest(bad))

    def test_exact_manifest_schema_three_entries_and_strict_json(self):
        f = self.f; original = deepcopy(f.manifest)
        for mutate in (lambda m: m.update(extra='untrusted'), lambda m: m['records'].pop('102'),
                       lambda m: m['records'].update({'999': deepcopy(m['records']['100'])}),
                       lambda m: m['records']['100'].update(url='https://secret.invalid'),
                       lambda m: m['records']['100']['asset'].update(bytes=True)):
            f.manifest = deepcopy(original); mutate(f.manifest)
            # Four-entry case writes the same bytes only; schema must reject first.
            if '999' in f.manifest['records']: f.images['999'] = f.images['100']
            f.publish()
            with self.assertRaises(x.Blocked): f.evidence()
        f.manifest = original
        for data in (b'{"records":{},"records":{}}', x.encoded(original) + b'\n', b'{"bad":NaN}'):
            f.publish(raw=data)
            with self.assertRaises(x.Blocked): f.evidence()

    def test_source_identity_and_reference_are_exact(self):
        f = self.f; e = f.evidence()
        for ident, data, reference in (('999', f.data['100'], f.urls['100']),
                ('100', f.data['101'], f.urls['100']), ('100', f.data['100'] + b' ', f.urls['100']),
                ('100', f.data['100'], f.urls['100'].replace('example', 'changed'))):
            with self.assertRaises(x.Blocked): e.resolve(ident, data, reference, current_commit=f.asset_commit)
        bad = deepcopy(f.descriptor); bad['records']['100']['blob'] = 'f' * 40
        with self.assertRaisesRegex(x.Blocked, 'asset_source_changed'): f.evidence(descriptor=bad)

    def test_manifest_reference_hash_and_source_hash_are_checked_against_git(self):
        f = self.f; original = deepcopy(f.manifest)
        for apply in (lambda m: m['records']['100'].update(reference_sha256='a' * 64),
                      lambda m: m['records']['100']['source'].update(sha256='b' * 64)):
            f.manifest = deepcopy(original); apply(f.manifest); f.publish()
            with self.assertRaises(x.Blocked): f.evidence()

    def test_extra_edit_delete_or_non_asset_addition_in_same_commit_rejects(self):
        f = self.f
        for path, data in (('unrelated.txt', b'not authorized'),
                           ('Raw/03 Get/03 State/receipt.json', b'changed'),
                           (f.records['100']['path'], b'changed')):
            f.publish(extra={path: data})
            with self.assertRaises(x.Blocked): f.evidence()
        f.publish(); f.run('rm', 'inherited.bin'); f.asset_commit = f.commit(); f.anchor['asset_commit'] = f.asset_commit
        with self.assertRaises(x.Blocked): f.evidence()

    def test_preexisting_asset_or_manifest_path_cannot_be_reused_as_append(self):
        f = self.f; f.run('reset', '--hard', f.publication)
        first = f.manifest['records']['100']['asset']['path']; f.write(first, b'preexisting')
        f.parent = f.commit(); f.publish()
        with self.assertRaisesRegex(x.Blocked, 'asset_evidence_delta'): f.evidence()

    def test_asset_path_escape_mime_shape_hash_and_bytes_reject(self):
        f = self.f; original = deepcopy(f.manifest)
        mutations = ({'path': x.PREFIX + '../outside.png'}, {'mime': 'image/jpeg'}, {'width': 999},
                     {'sha256': 'a' * 64}, {'blob': 'a' * 40}, {'bytes': 1}, {'bytes': x.MAX_PNG + 1})
        for change in mutations:
            f.manifest = deepcopy(original); f.manifest['records']['100']['asset'].update(change); f.publish()
            with self.subTest(change=change), self.assertRaises(x.Blocked): f.evidence()

    def test_current_ancestor_and_selected_md_asset_manifest_guards(self):
        f = self.f; e = f.evidence()
        with self.assertRaises(x.Blocked): e.check_current(f.publication)
        f.write('unrelated.txt', b'allowed later peer'); head = f.commit()
        self.assertTrue(e.check_current(head))
        with self.assertRaises(x.Blocked): e.check_current(f.asset_commit)
        for path in (f.records['100']['path'], x.MANIFEST_PATH, f.manifest['records']['100']['asset']['path']):
            f.run('reset', '--hard', head); f.write(path, b'changed'); changed = f.commit()
            with self.assertRaisesRegex(x.Blocked, 'asset_current_changed'): e.check_current(changed)

    def test_constructor_rejects_asset_commit_not_descended_from_publication_or_merge(self):
        f = self.f; f.run('checkout', '--orphan', 'other'); f.run('rm', '-rf', '.')
        f.write('other', b'foreign'); other = f.commit()
        with self.assertRaises(x.Blocked): f.evidence(current=other)
        f.run('checkout', 'main'); tree = f.run('rev-parse', 'HEAD^{tree}').decode().strip()
        merged = f.run('commit-tree', tree, '-p', f.parent, '-p', other, '-m', 'merge').decode().strip()
        f.run('update-ref', 'refs/heads/main', merged); anchor = dict(f.anchor, asset_commit=merged)
        with self.assertRaisesRegex(x.Blocked, 'asset_evidence_delta'): f.evidence(anchor=anchor, expected=x.digest(anchor), current=merged)

    def test_bindings_and_asset_tree_are_defensive_and_tampering_rejects(self):
        f = self.f; e = f.evidence(); tree = e.asset_tree; tree.clear()
        descriptor = e.descriptor; descriptor['asset_commit'] = 'f' * 40
        binding = e.resolve('100', f.data['100'], f.urls['100'], current_commit=f.asset_commit)
        binding['asset']['path'] = x.MANIFEST_PATH
        with self.assertRaises(x.Blocked): e.read_asset(binding, current_commit=f.asset_commit)
        self.assertEqual(len(e.asset_tree), 3); self.assertEqual(e.descriptor['asset_commit'], f.asset_commit)

    def test_later_queue_subset_keeps_full_evidence_but_only_resolves_selected_ids(self):
        f = self.f; original = f.evidence()
        old = original.resolve('100', f.data['100'], f.urls['100'], current_commit=f.asset_commit)
        desc = deepcopy(f.descriptor); desc['records'].pop('100')
        e = f.evidence(descriptor=desc)
        self.assertEqual(len(e.asset_tree), 3)
        for ident in ('101', '102'):
            b = e.resolve(ident, f.data[ident], f.urls[ident], current_commit=f.asset_commit)
            self.assertEqual(e.read_asset(b, current_commit=f.asset_commit), f.images[ident])
        with self.assertRaises(x.Blocked): e.resolve('100', f.data['100'], f.urls['100'], current_commit=f.asset_commit)
        with self.assertRaises(x.Blocked): e.read_asset(old, current_commit=f.asset_commit)

    def test_no_initial_or_fabricated_changed_content_selection(self):
        f = self.f
        for ids in ([], ['100', '101'], ['100', '100', '101', '102'], [100, '101', '102']):
            desc = deepcopy(f.descriptor); desc['changed_content_ids'] = ids
            with self.assertRaises(x.Blocked): f.evidence(descriptor=desc)

    def test_invalid_resolver_ident_and_typed_binding_fail_closed(self):
        f = self.f; e = f.evidence()
        for ident in (None, [], {}, True):
            with self.assertRaises(x.Blocked): e.resolve(ident, f.data['100'], f.urls['100'], current_commit=f.asset_commit)
        b = e.resolve('100', f.data['100'], f.urls['100'], current_commit=f.asset_commit)
        b['asset']['bytes'] = float(b['asset']['bytes'])
        with self.assertRaises(x.Blocked): e.read_asset(b, current_commit=f.asset_commit)

    def test_missing_or_corrupt_selected_blob_fails_without_fallback(self):
        from git_object_fixture import remove_objects
        f = self.f; oid = f.manifest['records']['100']['asset']['blob']
        removed = remove_objects(f.root, oid)
        with self.assertRaises(x.Blocked): f.evidence()
        path, _ = removed[oid]; path.write_bytes(b'corrupt')
        with self.assertRaises(x.Blocked): f.evidence()

    def test_real_blob_none_clone_reads_only_seven_selected_blobs_and_preserves_refs_index(self):
        f = self.f; f.run('config', 'uploadpack.allowFilter', 'true')
        clone = Path(self.tmp.name) / 'partial'
        subprocess.run(['git', 'clone', '-q', '--filter=blob:none', '--no-checkout', f.root.as_uri(), str(clone)], check=True)
        reader = git.OfflineGit(clone); tree = reader.tree(f.asset_commit)
        for path in [x.MANIFEST_PATH] + [r['path'] for r in f.records.values()] + [r['asset']['path'] for r in f.manifest['records'].values()]:
            data = f.run('cat-file', 'blob', tree[path][2]); f.run('hash-object', '-w', '--stdin', root=clone, data=data)
        inherited = tree['inherited.bin'][2]
        def missing():
            result = subprocess.run(['git', '-C', str(clone), 'cat-file', '-e', inherited], env=reader.local.env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        missing(); refs = f.run('show-ref', root=clone); head = (clone / '.git/HEAD').read_bytes()
        index = clone / '.git/index'; before_index = index.read_bytes() if index.exists() else None
        e = f.evidence(reader=reader)
        for ident in f.records:
            b = e.resolve(ident, f.data[ident], f.urls[ident], current_commit=f.asset_commit)
            self.assertEqual(e.read_asset(b, current_commit=f.asset_commit), f.images[ident])
        missing(); self.assertEqual(f.run('show-ref', root=clone), refs); self.assertEqual((clone / '.git/HEAD').read_bytes(), head)
        self.assertEqual(index.read_bytes() if index.exists() else None, before_index)


class PureChecks(unittest.TestCase):
    def test_png_crc_trailing_invalid_format_reject(self):
        good = png(); self.assertEqual(x.png_shape(good), (2, 2))
        for data in (good[:-1], good + b'extra', good[:40] + bytes([good[40] ^ 1]) + good[41:], b'not png', png(10001, 1)):
            with self.assertRaises(x.Blocked): x.png_shape(data)

    def test_reference_unknown_double_image_html_and_scheme_reject(self):
        base = b'---\nnote_id: "100"\ntitle: "Synthetic"\n---\n'
        for body in (b'![x](https://evil.invalid/x.png)', b'![x](http://get-notes.umiwi.com/x.png)',
                     b'![x](https://get-notes.umiwi.com/x.png)\n![y](https://get-notes.umiwi.com/y.png)',
                     b'![x](https://get-notes.umiwi.com/x.png)\n<IMG src="other">',
                     b'![[file.png]]', b'![x](https://user@get-notes.umiwi.com/x.png)'):
            with self.assertRaises(x.Blocked): x.exact_reference(base + body, '100')


if __name__ == '__main__': unittest.main()
