"""Real bare remote + blob:none client; all I/O stays in local fixtures."""
from copy import deepcopy
from pathlib import Path
import base64
import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

import get_notion_png_loader as x
from test_notion_png_evidence import Fixture


class Remote:
    def __init__(self, bare):
        self.bare = bare; self.calls = []; self.blobs = []
        def run(*args):
            return subprocess.run(['git','-C',str(bare),*args],capture_output=True,check=True).stdout
        def tree(commit):
            result={}
            for row in run('ls-tree','-rz',commit).split(b'\0'):
                if row:
                    meta,path=row.split(b'\t',1);result[path.decode()]=tuple(meta.decode().split())
            return result
        self.reader=SimpleNamespace(local=SimpleNamespace(run=run,blob=lambda oid:run('cat-file','blob',oid)),tree=tree)
        self.bad_ref = None; self.mutate_blob = None; self.on_ref = None
    def metadata(self):
        self.calls.append('metadata')
        return {'full_name': x.m.r.REPOSITORY, 'private': True}
    def ref(self, ref):
        self.calls.append(ref)
        if self.on_ref: self.on_ref(ref)
        if ref == self.bad_ref: return '0'*40
        if ref in x.m.PROTECTED: return x.m.PROTECTED[ref]
        return self.reader.local.run('rev-parse', ref).decode().strip()
    def request(self, path, **kw):
        assert path.startswith('/git/blobs/')
        self.blobs.append(path); oid = path.split('/')[-1]
        data = self.reader.local.blob(oid)
        value = {'sha': oid, 'size': len(data), 'encoding': 'base64', 'content': base64.b64encode(data).decode()}
        return self.mutate_blob(value) if self.mutate_blob else value


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.f = Fixture(self.root/'source')
        self.bare = self.root/'remote.git'
        self.run_git('clone', '--bare', '-q', str(self.f.root), str(self.bare))
        self.run_git('-C', str(self.bare), 'config', 'uploadpack.allowFilter', 'true')
        self.remote = Remote(self.bare); self.local = self.clone()
    def run_git(self, *args):
        return subprocess.run(['git', *args], capture_output=True, check=True).stdout
    def clone(self):
        target = self.root/('client'+str(len(list(self.root.glob('client*')))))
        self.run_git('clone', '-q', '--filter=blob:none', '--depth=1', '--no-checkout', 'file://'+str(self.bare), str(target))
        return x.m.r.LocalGit(target)
    def load(self, **kw):
        return x.load(kw.get('local', self.local), self.remote, kw.get('publication',self.f.descriptor),
                      kw.get('anchor',self.f.anchor), kw.get('approved',x.evidence.digest(self.f.anchor)), 'a'*64)
    def push(self, path, data):
        self.f.write(path,data); head=self.f.commit()
        self.f.run('push','-q','file://'+str(self.bare),'HEAD:refs/heads/main')
        return head
    def missing(self, oid):
        reader=x.m.a.OfflineGit(self.local.vault)
        return reader.local.run('cat-file','--batch-check',data=(oid+'\n').encode()).endswith(b' missing\n')
    def test_real_partial_clone_only_seven_blobs_and_no_ref_or_index_changes(self):
        before=x.layout(self.local);desc=x.evidence.encoded(self.f.descriptor)
        value=self.load()
        wanted={self.f.anchor['manifest_blob']}|{r['blob'] for r in self.f.records.values()}|{r['asset']['blob'] for r in self.f.manifest['records'].values()}
        self.assertEqual(set(self.remote.blobs),{'/git/blobs/'+v for v in wanted});self.assertEqual(len(self.remote.blobs),7)
        inherited=self.remote.reader.tree(self.f.asset_commit)['inherited.bin'][2]
        self.assertTrue(self.missing(inherited));self.assertEqual(x.layout(self.local),before)
        self.assertEqual(x.evidence.encoded(self.f.descriptor),desc)
        self.assertEqual(value.snapshot_head,self.f.asset_commit)
        for ident in self.f.records:
            bound=value.evidence.resolve(ident,self.f.data[ident],self.f.urls[ident],current_commit=value.snapshot_head)
            self.assertEqual(value.evidence.read_asset(bound,current_commit=value.snapshot_head),self.f.images[ident])
        self.assertEqual(len(self.remote.blobs),7)
    def test_repeated_load_or_guard_never_refetches_selected_bodies(self):
        value=self.load();calls=list(self.remote.blobs);before=len(self.remote.calls)
        value.fresh_guard();value.fresh_guard();self.load()
        self.assertEqual(self.remote.blobs,calls)
        self.assertGreaterEqual(self.remote.calls[before:].count('metadata'),3)
        self.assertGreaterEqual(self.remote.calls[before:].count(x.m.r.MAIN_REF),6)
    def test_upload_callback_returns_literal_true_only_after_fresh_checks(self):
        value=self.load();before=len(self.remote.calls)
        self.assertIs(value.before_write(),True);self.assertGreater(len(self.remote.calls),before)
        self.remote.bad_ref=next(iter(x.m.PROTECTED))
        with self.assertRaises(Exception):value.before_write()
    def test_unrelated_remote_advance_verified_without_local_main_change(self):
        value=self.load();before=x.layout(self.local);calls=list(self.remote.blobs)
        head=self.push('.sync/get-notion/state.json',b'{"synthetic":"state"}')
        self.assertEqual(value.fresh_guard(),head)
        self.assertEqual(x.layout(self.local),before);self.assertEqual(self.remote.blobs,calls)
        with self.assertRaisesRegex(x.Blocked,'source_changed'):value.fresh_guard(value.snapshot_head)
    def test_each_exact_source_asset_manifest_and_other_get_change_rejected(self):
        value=self.load();base=self.f.asset_commit;paths=[self.f.records['100']['path'],x.evidence.MANIFEST_PATH,
            self.f.manifest['records']['102']['asset']['path'],'Raw/03 Get/03 State/receipt.json']
        for path in paths:
            self.f.run('reset','--hard',base);head=self.push(path,b'changed')
            with self.subTest(path=path),self.assertRaisesRegex(x.Blocked,'source_changed'):value.fresh_guard()
            self.run_git('-C',str(self.bare),'update-ref','refs/heads/main',base,head)
    def test_protected_ref_or_wrong_private_metadata_stops_before_blob_fetch(self):
        self.remote.bad_ref=next(iter(x.m.PROTECTED))
        with self.assertRaises(Exception):self.load()
        self.assertEqual(self.remote.blobs,[])
        self.remote.bad_ref=None;self.remote.metadata=lambda:{'full_name':x.m.r.REPOSITORY,'private':False}
        with self.assertRaises(Exception):self.load()
        self.assertEqual(self.remote.blobs,[])
    def test_head_race_after_metadata_cannot_pass(self):
        value=self.load();count=[0]
        def race(ref):
            if ref==x.m.r.MAIN_REF:
                count[0]+=1
                if count[0]==2:self.push('race.txt',b'race')
        self.remote.on_ref=race
        with self.assertRaisesRegex(x.Blocked,'source_changed'):value.fresh_guard()
    def test_wrong_anchor_or_non_partial_clone_has_zero_remote_reads(self):
        with self.assertRaises(Exception):self.load(approved='0'*64)
        self.assertFalse(self.remote.calls);self.assertFalse(self.remote.blobs)
        self.local.run('config','remote.origin.partialclonefilter','tree:0')
        with self.assertRaisesRegex(x.Blocked,'partial_clone'):self.load()
        self.assertFalse(self.remote.calls)
    def test_bad_asset_delta_fetches_manifest_only(self):
        self.f.publish(extra={'outside.txt':b'forbidden'})
        self.f.run('push','-q','--force','file://'+str(self.bare),'HEAD:refs/heads/main')
        self.local=self.clone()
        with self.assertRaisesRegex(x.Blocked,'delta'):self.load()
        self.assertEqual(self.remote.blobs,['/git/blobs/'+self.f.anchor['manifest_blob']])
    def test_corrupt_remote_body_not_imported(self):
        self.remote.mutate_blob=lambda v:dict(v,content=base64.b64encode(b'corrupt').decode())
        with self.assertRaises(Exception):self.load()
        self.assertTrue(self.missing(self.f.anchor['manifest_blob']))
    def test_rewind_before_asset_and_local_ref_change_rejected(self):
        value=self.load();self.run_git('-C',str(self.bare),'update-ref','refs/heads/main',self.f.publication)
        with self.assertRaises(Exception):value.fresh_guard()
        self.local.run('update-ref','refs/heads/extra',self.f.asset_commit)
        with self.assertRaisesRegex(x.Blocked,'local_changed'):value.fresh_guard()
    def test_cached_local_tree_corruption_not_hidden(self):
        value=self.load();oid=self.local.run('rev-parse',value.snapshot_head+':Raw').decode().strip()
        # Normalize only this disposable fixture; do not assume loose layout.
        from git_object_fixture import remove_objects
        import zlib
        path,_=remove_objects(self.local.vault,oid)[oid]
        bad=b'corrupt tree';path.write_bytes(zlib.compress(b'tree '+str(len(bad)).encode()+b'\0'+bad))
        with self.assertRaises(Exception):value.fresh_guard()
    def test_selected_queue_subset_does_not_change_manifest_or_original_publication(self):
        desc=deepcopy(self.f.descriptor);desc['records']={'100':desc['records']['100']}
        value=self.load(publication=desc)
        self.assertEqual(len(value.evidence.asset_tree),3)
        with self.assertRaises(Exception):value.evidence.resolve('101',self.f.data['101'],self.f.urls['101'],current_commit=value.snapshot_head)
    def test_changed_remote_main_at_start_does_not_silently_repin(self):
        self.push('outside.txt',b'new')
        with self.assertRaises(Exception):self.load()
        self.assertEqual(self.remote.blobs,[])
    def test_oversized_manifest_asset_stops_before_six_body_reads(self):
        self.f.manifest['records']['100']['asset']['bytes']=x.evidence.MAX_PNG+1
        self.f.publish();self.f.run('push','-q','--force','file://'+str(self.bare),'HEAD:refs/heads/main');self.local=self.clone()
        with self.assertRaisesRegex(x.Blocked,'manifest'):self.load()
        self.assertEqual(self.remote.blobs,['/git/blobs/'+self.f.anchor['manifest_blob']])
    def test_snapshot_after_assets_cannot_rewind_to_assets(self):
        self.push('.sync/get-notion/state.json',b'initial journal');self.local=self.clone()
        value=self.load();self.assertNotEqual(value.snapshot_head,self.f.asset_commit)
        self.run_git('-C',str(self.bare),'update-ref','refs/heads/main',self.f.asset_commit)
        with self.assertRaises(Exception):value.fresh_guard()
    def test_observed_advance_cannot_rewind_even_with_same_source(self):
        value=self.load();head=self.push('later.txt',b'later')
        self.assertEqual(value.fresh_guard(),head)
        self.run_git('-C',str(self.bare),'update-ref','refs/heads/main',self.f.asset_commit)
        with self.assertRaises(Exception):value.fresh_guard()
    def test_server_ignoring_filter_is_detected_before_further_work(self):
        value=self.load();self.run_git('-C',str(self.bare),'config','uploadpack.allowFilter','false')
        self.push('unrequested.txt',b'server sends extra object despite request filter')
        with self.assertRaisesRegex(x.Blocked,'unrequested_blob'):value.fresh_guard()


if __name__=='__main__':unittest.main()
