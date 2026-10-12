import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_getnote_to_raw as g
from source_sync_store import IdentityIndex, source_id


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from source_sync_assets import AssetStore
        store_patch = patch.object(g, 'AssetStore', side_effect=lambda root, **kw: AssetStore(root, fetch=lambda url, **kwargs: (b'\x89PNG\r\n\x1a\nfixture', 'image/png'), **kw))
        store_patch.start(); self.addCleanup(store_patch.stop)
        for module, names in [(g, ['ROOT','RAW_DIR','NOTES_DIR','STATE_DIR'])]:
            for name in names:
                p = patch.object(module, name, self.root / module.__name__ / name)
                p.start(); self.addCleanup(p.stop)
        # Get identity lookup must encompass both normal and recording notes.
        g.ROOT = g.RAW_DIR.parent
        g.NOTES_DIR = g.RAW_DIR / '01 Notes'
        g.STATE_DIR = g.RAW_DIR / '03 State'

    def note(self, ident='9223372036854775807', **kw):
        return dict(id=ident, title='same', content='summary', created_at='2020-01-01T00:00:00Z', updated_at='2020-01-01T00:00:00Z', **kw)

    def test_idempotency_title_edit_and_same_title_different_id(self):
        for path_for, render, write in [(g.output_path_for,g.render_note,g.write_if_changed)]:
            n = self.note()
            first = path_for(n)
            self.assertTrue(write(first, render(n)))
            stamp = first.stat().st_mtime_ns
            self.assertEqual(first, path_for(n))
            self.assertFalse(write(path_for(n), render(n)))
            self.assertEqual(stamp, first.stat().st_mtime_ns)
            self.assertNotEqual(first, path_for(self.note('2')))
            n['title'] = 'new title'; n['content'] = 'edited old note'
            self.assertEqual(first, path_for(n))
            self.assertTrue(write(first, render(n)))

    def test_plugin_canonical_frontmatter_and_namespace(self):
        path = g.RAW_DIR / 'legacy.md'
        g.write_if_changed(path, '---\ncanonicalId: "api:9223372036854775807"\nsource: "api"\nsource_ids:\n  api: "9223372036854775807"\n  legacy: "different"\n---\nbody')
        self.assertEqual(g.output_path_for(self.note()), path)
        self.assertNotEqual(g.output_path_for(self.note('different')), path)

    def test_frontmatter_standalone_delimiter_and_symlink_directory(self):
        from source_sync_store import frontmatter
        path=g.RAW_DIR/'note.md'
        g.write_if_changed(path,'---\ntitle: "A---B"\n---not-a-delimiter: value\nsource_system: getnote\nnote_id: "9223372036854775807"\n---\nprivate original')
        self.assertEqual(frontmatter(path)['title'],'A---B')
        self.assertEqual(g.output_path_for(self.note()),path)
        (g.RAW_DIR/'alias').symlink_to(g.RAW_DIR,target_is_directory=True)
        with self.assertRaises(ValueError): g.output_path_for(self.note('other'))
        self.assertIn('private original',path.read_text())

    def test_symlink_source_ancestor_is_rejected(self):
        outside=self.root/'outside';outside.mkdir()
        g.ROOT=self.root/'vault';g.ROOT.mkdir()
        (g.ROOT/'Raw').symlink_to(outside,target_is_directory=True)
        g.RAW_DIR=g.ROOT/'Raw'/'03 Get'
        g.NOTES_DIR=g.RAW_DIR/'01 Notes'
        with self.assertRaises(ValueError): g.output_path_for(self.note())
        self.assertEqual(list(outside.iterdir()),[])

    def test_conflicting_duplicate_content_is_preserved(self):
        first=g.RAW_DIR/'a.md';second=g.RAW_DIR/'b.md'
        g.write_if_changed(first,g.render_note(self.note())+'unique A')
        g.write_if_changed(second,g.render_note(self.note())+'unique B')
        with self.assertRaises(ValueError): self.run_get([self.note()])
        self.assertTrue(first.read_text().endswith('unique A'))
        self.assertTrue(second.read_text().endswith('unique B'))

    def test_all_same_source_identity_fields_must_agree(self):
        variants = [
            'note_id: "A"\nnote_id: "B"',
            'note_id: "A"\ncanonicalId: "api:B"',
            'note_id: "A"\nsource_ids:\n  api: "B"',
            'note_id: "A"\ncanonicalId: "api:B"\nsource_ids:\n  api: "B"',
            'canonicalId: "api:A"\nsource_ids:\n  api: "B"',
        ]
        path=g.RAW_DIR/'conflict.md'
        for fields in variants:
            original='---\nsource_system: getnote\n'+fields+'\n---\nUnique B original'
            g.write_if_changed(path,original)
            with self.assertRaises(ValueError): self.run_get([self.note()])
            self.assertEqual(path.read_text(),original)
            self.assertFalse(g.STATE_DIR.exists())

    def test_legacy_original_without_raw_cache_survives_pending(self):
        path=g.RAW_DIR/'old-plugin-recording.md'
        original='---\ncanonicalId: "api:9223372036854775807"\nsource_ids:\n  api: "9223372036854775807"\n---\n## 原始转写\nBEGIN unique complete original END'
        g.write_if_changed(path,original)
        timestamp=path.stat().st_mtime_ns
        result=self.run_get([self.note(note_type='recorder_audio',audio={})])
        self.assertEqual(result,2)
        self.assertEqual(path.read_text(),original)
        self.assertEqual(path.stat().st_mtime_ns,timestamp)
        self.assertFalse((g.STATE_DIR/'raw-cache').exists())
        receipt=json.loads((g.STATE_DIR/'receipt.json').read_text())
        self.assertFalse(receipt['complete'])
        self.assertEqual(receipt['pending'],1)
        self.assertEqual(len(json.loads((g.STATE_DIR/'pending.json').read_text())['pending_ids']),1)

    def test_large_id_float_rejected(self):
        self.assertEqual(source_id(9223372036854775807), '9223372036854775807')
        with self.assertRaises(ValueError): source_id(9.223372036854776e18)

    def test_duplicate_frontmatter_fails_closed(self):
        g.write_if_changed(g.RAW_DIR/'a.md',g.render_note(self.note()))
        g.write_if_changed(g.RAW_DIR/'b.md',g.render_note(self.note()))
        with self.assertRaises(ValueError): IdentityIndex(g.RAW_DIR,'getnote','note_id')

    def test_recording_long_original_and_pending(self):
        original = 'BEGIN' + '原文'*30000 + 'END'
        n=self.note(note_type='recorder_audio',audio={'original':original},timeline=[{'time':2}],chapter_timeline=['chapter'],quick_note='quick')
        rendered=g.render_note(n)
        self.assertIn(original, rendered)
        self.assertIn('## AI 总结',rendered)
        self.assertIn('chapter_timeline',rendered)
        self.assertIn('99 录音卡',str(g.output_path_for(n)))
        self.assertFalse(g.transcription_pending(n))
        n['audio']={}
        self.assertTrue(g.transcription_pending(n))
        n['audio']={'original':'   \n'}
        self.assertTrue(g.transcription_pending(n))

    def test_cursor_contract_and_since_id(self):
        for mode,pages in [('cursor',[{'notes':[self.note()], 'has_more':True,'cursor':'big'}, {'notes':[self.note('2')],'has_more':False}]),('since_id',[{'list':[self.note()]}, {'list':[]}])]:
            with patch.object(g,'api_get',side_effect=[{'data':p} for p in pages]) as api:
                got=g.fetch_note_list('unused','unused',pagination=mode)
                self.assertEqual(len(got),2 if mode=='cursor' else 1)
                self.assertEqual(api.call_args_list[1].args[1],{mode:'big' if mode=='cursor' else '9223372036854775807'})

    def test_pagination_cap_and_cycle_fail(self):
        p={'data':{'notes':[self.note()],'has_more':True,'cursor':'x'}}
        with patch.object(g,'api_get',return_value=p):
            with self.assertRaises(RuntimeError): g.fetch_note_list('x','y',max_pages=1)
            with self.assertRaises(RuntimeError): g.fetch_note_list('x','y')

    def run_get(self, details):
        with patch.dict(os.environ,{'VAULT_ROOT':str(self.root)}), patch.object(sys,'argv',['sync']), patch.object(g,'load_credentials',return_value=('unused','unused')), patch.object(g,'fetch_note_list',return_value=[self.note()]), patch.object(g,'fetch_note_detail',side_effect=details), contextlib.redirect_stdout(io.StringIO()):
            return g.main()

    def test_failure_no_receipt_and_recovery_delayed_transcript(self):
        import urllib.error
        for status in [401,429,500]:
            with self.assertRaises(urllib.error.HTTPError):
                self.run_get([urllib.error.HTTPError('https://invalid',status,'failure',{},None)])
            self.assertFalse(g.STATE_DIR.exists())
        self.run_get([self.note(note_type='recorder_audio',audio={})])
        self.assertEqual(json.loads((g.STATE_DIR/'pending.json').read_text())['pending_ids'],['9223372036854775807'])
        n=self.note(note_type='recorder_audio',audio={'original':'late complete transcript'},attachments=[{'url':'https://example.invalid/image.png','type':'image'}])
        self.run_get([n])
        self.assertEqual(json.loads((g.STATE_DIR/'pending.json').read_text())['pending_ids'],[])
        self.assertEqual(len(list(g.RAW_DIR.rglob('*.md'))),1)
        manifests=list((g.STATE_DIR/'attachments').glob('*.json'))
        self.assertEqual(json.loads(manifests[0].read_text())['attachments'],n['attachments'])
        before={str(p):p.stat().st_mtime_ns for p in g.RAW_DIR.rglob('*') if p.is_file()}
        self.run_get([n])
        self.assertEqual(before,{str(p):p.stat().st_mtime_ns for p in g.RAW_DIR.rglob('*') if p.is_file()})

    def test_transient_pending_does_not_erase_original(self):
        self.run_get([self.note(note_type='recorder_audio',audio={'original':'complete original'})])
        self.run_get([self.note(note_type='recorder_audio',audio={})])
        self.assertIn('complete original',next(g.RAW_DIR.rglob('*.md')).read_text())
        self.assertEqual(len(json.loads((g.STATE_DIR/'pending.json').read_text())['pending_ids']),1)

    def test_asset_failure_does_not_change_receipt_or_note_then_recovers(self):
        self.run_get([self.note()])
        before={p:p.read_bytes() for p in g.RAW_DIR.rglob('*') if p.is_file()}
        n=self.note(attachments=[{'type':'image','url':'https://example.invalid/image'}])
        from source_sync_assets import AssetError
        with patch.object(g,'AssetStore') as store:
            store.return_value.materialize.side_effect=AssetError('timeout')
            with self.assertRaises(AssetError): self.run_get([n])
        self.assertEqual(before,{p:p.read_bytes() for p in g.RAW_DIR.rglob('*') if p.is_file()})
        self.run_get([n])
        self.assertIn('## 本地附件',next(g.RAW_DIR.rglob('*.md')).read_text())
        self.assertEqual(len(list((g.RAW_DIR/'_assets').rglob('*.png'))),1)


if __name__ == '__main__': unittest.main()

