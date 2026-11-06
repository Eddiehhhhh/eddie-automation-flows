import ast
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
import re
import sys
import struct
import types
import unittest
import zlib
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import get_inline_image_assets as inline_images
import test_hourly_duplicate_isolation as fixture

CANON = fixture.c
ENTRY = fixture.runtime.entry
SOURCE_ASSETS = ENTRY.engine.source_assets


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def tiny_png(pixel=255,padding=0):
    def chunk(kind,payload):
        return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
    extra=chunk(b'tEXt',b'k\0'+b'x'*padding) if padding else b''
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,6,0,0,0))+extra
            +chunk(b'IDAT',zlib.compress(bytes([0,0,0,0,pixel])))+chunk(b'IEND',b''))


def writer_helpers():
    path = SCRIPTS / 'get_canonical_writer.py'
    tree = ast.parse(path.read_text())
    names = {'validate_inline_image_manifest', 'validate_media_payload'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    def fail(code):
        raise ValueError(code)
    def checked(value, width):
        if not isinstance(value,str) or re.fullmatch(r'[0-9a-f]{%d}' % width,value) is None:
            raise ValueError('hash_invalid')
        return value
    def path_ok(value):
        if not isinstance(value,str) or value.startswith('/') or '..' in Path(value).parts:
            raise ValueError('path_invalid')
        return value
    namespace = {'deepcopy':deepcopy, 'hashlib':hashlib, 'os':os, 'Path':Path,
        're':re, 'inline_images':inline_images, 'fail':fail,
        'legacy':types.SimpleNamespace(checked=checked,path_ok=path_ok,
            retention=types.SimpleNamespace(oid=git_blob))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def entry_asset_delta_helper():
    path = SCRIPTS / 'get_recurring_entry.py'
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'verify_asset_delta')
    def fail(code): raise ValueError(code)
    namespace={'fail':fail,'a':types.SimpleNamespace(sha=lambda b:hashlib.sha256(b).hexdigest()),
        'r':types.SimpleNamespace(oid=git_blob)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace['verify_asset_delta']


def registry_asset_baseline_helper():
    path = SCRIPTS / 'get_canonical_writer.py'
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'verify_registered_asset_refs')
    def fail(code): raise ValueError(code)
    namespace={'fail':fail,'legacy':types.SimpleNamespace(path_ok=lambda p:p)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace['verify_registered_asset_refs']


class CanonicalInlineBindingTests(unittest.TestCase):
    def setUp(self):
        self.helpers = writer_helpers()
        self.body = 'before\n![x](https://cdn.example/a.png)\nafter'
        self.data = tiny_png()
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.path = 'Raw/03 Get/_assets/images/' + self.sha + '.png'
        ref = inline_images.extract(self.body)[0]
        self.row = {'reference_sha256':ref['reference_sha256'],
            'source_sha256':ref['source_sha256'], 'asset_path':self.path,
            'asset_blob':git_blob(self.data), 'asset_sha256':self.sha,
            'asset_bytes':len(self.data), 'asset_mime':'image/png'}
        self.media = {'manifest':{'contract':'get-inline-image-manifest-1','images':[self.row]},
                      'files':{self.path:self.data}}

    def test_authenticated_source_is_rewritten_and_exact_asset_is_bound(self):
        display, manifest, files = self.helpers['validate_media_payload'](
            {'content':self.body}, '05 图片笔记/note.md', self.media)
        self.assertEqual(display['content'], 'before\n![x](../_assets/images/' + self.sha + '.png)\nafter')
        self.assertEqual(manifest, self.media['manifest'])
        self.assertEqual(files, {'_assets/images/' + self.sha + '.png':self.data})

    def test_wrong_bytes_or_extra_sibling_file_fail_closed(self):
        bad = deepcopy(self.media);bad['files'][self.path]=b'changed'
        with self.assertRaises(ValueError):
            self.helpers['validate_media_payload']({'content':self.body}, '05 图片笔记/note.md', bad)
        bad = deepcopy(self.media);bad['files']['_assets/images/sibling.png']=b'x'
        with self.assertRaises(ValueError):
            self.helpers['validate_media_payload']({'content':self.body}, '05 图片笔记/note.md', bad)

    def test_manifest_path_and_mime_must_match_content_address(self):
        for changes in ({'asset_path':'Raw/03 Get/_assets/images/../private.png'},
                        {'asset_mime':'image/svg+xml'}):
            bad = deepcopy(self.media)
            bad['manifest']['images'][0].update(changes)
            with self.assertRaises(ValueError):
                self.helpers['validate_media_payload']({'content':self.body}, '05 图片笔记/note.md', bad)

    def test_html_disguised_as_png_is_held(self):
        bad=self.media.copy();bad['files']={self.path:b'<html>expired URL</html>'}
        with self.assertRaises(ValueError):
            self.helpers['validate_media_payload']({'content':self.body}, '05 图片笔记/note.md', bad)

    def test_untitled_image_path_never_uses_signed_source_url(self):
        marker='signature-only-marker'
        note={'note_id':'12345','title':'','note_type':'plain_text',
              'content':'![alt](https://cdn.example/image.png?token='+marker+')'}
        path=CANON.new_path(note,'12345')
        self.assertIn('图片笔记-12345.md',path)
        self.assertNotIn(marker,path)

    def test_receipt_delta_requires_registered_asset_tree_and_exact_bytes(self):
        verify=entry_asset_delta_helper()
        class Reader:
            def __init__(self,tree,data):self._tree=tree;self.data=data
            def tree(self,commit):return self._tree[commit]
            def read(self,commit,path,cap):return self.data
        before='parent';after='candidate'
        registry={'newById':{'123':{'source_metadata':self.media['manifest'] | {'inline_images':self.media['manifest']}}}}
        # Registry rows use source_metadata.inline_images, not the media input envelope.
        registry['newById']['123']['source_metadata']={'inline_images':self.media['manifest']}
        good=Reader({before:{},after:{self.path:('100644','blob',git_blob(self.data))}},self.data)
        self.assertEqual(verify(good,before,after,{self.path},registry),{self.path:{'123'}})
        missing=Reader({before:{},after:{}},self.data)
        with self.assertRaises(ValueError):verify(missing,before,after,{self.path},registry)
        changed=Reader({before:{},after:{self.path:('100644','blob',git_blob(b'other'))}},self.data)
        with self.assertRaises(ValueError):verify(changed,before,after,{self.path},registry)

    def test_old_manifest_must_still_exist_at_pinned_parent_oid(self):
        verify=registry_asset_baseline_helper()
        image=self.row
        registry={'newById':{'123':{'source_metadata':{'inline_images':{'images':[image]}}}}}
        class Reader:
            def __init__(self,tree):self._tree=tree
            def tree(self,commit):return self._tree
        self.assertTrue(verify(Reader({self.path:('100644','blob',image['asset_blob'])}),'parent',registry))
        with self.assertRaises(ValueError):verify(Reader({}),'parent',registry)
        with self.assertRaises(ValueError):verify(Reader({self.path:('100755','blob',image['asset_blob'])}),'parent',registry)

    def test_real_canonical_receipt_holds_bad_and_overbudget_images_per_note(self):
        repo=fixture.HourlyDuplicateIsolation();repo.setUp()
        self.addCleanup(repo.doCleanups)
        parent,legacy_sha,registry_sha=repo.save_parent()
        reader=CANON.legacy.OfflineGit(repo.root)
        before=ENTRY.load_states(reader,repo.evidence,parent,legacy_sha,registry_sha)
        marker='signature-only-marker'
        good=fixture.note('55555','good ![x](https://cdn.example/good.png?token='+marker+')')
        good['title']=''
        large=fixture.note('66666','large ![x](https://cdn.example/large.png)')
        bad=fixture.note('77777','bad ![x](https://cdn.example/bad.png)')
        notes={row['note_id']:row for row in (good,large,bad)}
        class Source:
            def enumerate(self,source_id=None):return list(notes.values()),True
            def detail_raw(self,ident):return CANON.legacy.encoded({'data':notes[ident]})
        class AssetStore:
            def __init__(self,root,**kwargs):self.root=Path(root)
            def materialize(self,note):
                url=note['attachments'][0]['url']
                if 'bad.png' in url:raise SOURCE_ASSETS.AssetError('synthetic failure')
                data=tiny_png(0 if 'new.png' in url else 255,9000 if 'large.png' in url else 0);sha=hashlib.sha256(data).hexdigest();relative='_assets/images/'+sha+'.png'
                path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
                return [{'status':'downloaded','path':relative,'sha256':sha,'bytes':len(data),'mime':'image/png'}]
        rules=ENTRY.rules_sha256();bootstrap={'commit':parent};policy=ENTRY.policy_sha256(rules,bootstrap)
        previous={'sequence':4,'state_sha256':legacy_sha,'registry_sha256':registry_sha}
        anchor={'commit':parent,'receipt_sha256':'a'*64};original=ENTRY.read_receipt
        def receipt(rd,commit,digest,approved):
            if commit==parent and digest==anchor['receipt_sha256']:return previous
            return original(rd,commit,digest,approved)
        original_candidate=CANON.git_candidate
        def bounded_candidate(*args,**kwargs):
            changes=args[4]
            if sum(map(len,changes.values()))>8000:
                raise CANON.raw.ParityFailure('canonical_budget')
            return original_candidate(*args,**kwargs)
        with patch.object(ENTRY,'trust_current',return_value=(policy,previous,before)), \
             patch.object(ENTRY,'bootstrap_states',return_value=before), \
             patch.object(ENTRY,'read_receipt',side_effect=receipt), \
             patch.object(CANON,'git_candidate',side_effect=bounded_candidate), \
             patch.object(ENTRY.engine.source_assets,'AssetStore',AssetStore):
            plan,files=ENTRY.prepare(reader,repo.evidence,parent,Source(),approved_rules_sha256=rules,
                anchor=anchor,bootstrap=bootstrap,source_id=None)
        asset_path=ENTRY.r.GET+'/_assets/images/'+hashlib.sha256(tiny_png()).hexdigest()+'.png'
        markdown_path=ENTRY.r.GET+'/'+CANON.new_path(good,'55555')
        markdown=files[markdown_path].decode()
        self.assertIn('../_assets/images/',markdown)
        self.assertEqual(files[asset_path],tiny_png())
        state=json.loads(files[ENTRY.r.GET+'/'+CANON.STATE])
        self.assertEqual(state['conflictsById']['66666'],'asset_materialization_required')
        self.assertEqual(state['conflictsById']['77777'],'asset_materialization_required')
        self.assertIn('inline_images',state['newById']['55555']['source_metadata'])
        self.assertNotIn('66666',state['newById'])
        self.assertNotIn('77777',state['newById'])
        receipt_value=json.loads(files[ENTRY.FULL_RECEIPT])
        self.assertEqual(receipt_value['changed_content_ids'],['55555'])
        self.assertEqual(plan['proposed_content_ids'],['55555'])
        self.assertNotIn(marker,json.dumps(plan,ensure_ascii=False))
        self.assertNotIn(marker,b''.join(files.values()).decode('utf-8',errors='ignore'))

        def update(note):
            current,old_legacy,old_registry=ENTRY.load_states(reader,repo.evidence,parent_ref[0],
                states_sha[0],registry_sha_ref[0],source_id='55555')
            class SingleSource:
                def enumerate(self,ident):
                    return [note],{'contract':'get-single-note-source-scope-1','note_id':ident,'complete_catalog':False}
                def detail_raw(self,ident):return CANON.legacy.encoded({'data':note})
            with patch.object(ENTRY.engine.source_assets,'AssetStore',AssetStore):
                candidate,_=ENTRY.engine.prepare(reader,repo.evidence,parent_ref[0],current,SingleSource(),
                    legacy_sha=states_sha[0],registry_sha=registry_sha_ref[0],source_id='55555')
            next_states=ENTRY.load_states(reader,repo.evidence,candidate['candidate_commit'],
                candidate['state_sha256'],candidate['registry_sha256'],source_id='55555')
            ids=ENTRY.transition(reader,repo.evidence,parent_ref[0],candidate['candidate_commit'],
                (current,old_legacy,old_registry),next_states,source_id='55555')
            self.assertEqual(ids,['55555'])
            parent_ref[0]=candidate['candidate_commit'];states_sha[0]=candidate['state_sha256'];registry_sha_ref[0]=candidate['registry_sha256']
            return candidate,next_states

        first_asset=asset_path
        parent_ref=[plan['candidate_commit']];states_sha=[plan['state_sha256']];registry_sha_ref=[plan['registry_sha256']]
        text_edit=fixture.note('55555','edited text ![x](https://cdn.example/good.png)',1760000001000)
        _,text_states=update(text_edit)
        row=text_states[2]['newById']['55555']
        self.assertEqual(row['source_metadata']['inline_images']['images'][0]['asset_path'],first_asset)

        replacement=fixture.note('55555','replace ![x](https://cdn.example/new.png)',1760000002000)
        replaced_plan,replaced_states=update(replacement)
        new_asset=replaced_states[2]['newById']['55555']['source_metadata']['inline_images']['images'][0]['asset_path']
        self.assertNotEqual(new_asset,first_asset)
        self.assertEqual(reader.tree(parent_ref[0])[new_asset][2],replaced_states[2]['newById']['55555']['source_metadata']['inline_images']['images'][0]['asset_blob'])

        deleted=fixture.note('55555','image removed',1760000003000)
        _,deleted_states=update(deleted)
        self.assertNotIn('inline_images',deleted_states[2]['newById']['55555']['source_metadata'])


if __name__ == '__main__':
    unittest.main()
