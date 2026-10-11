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
import sync_flomo_to_raw as f
from source_sync_store import IdentityIndex, source_id


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for module, names in [(g, ['ROOT','RAW_DIR','NOTES_DIR','STATE_DIR']), (f,['ROOT','RAW_DIR','MEMOS_DIR','INDEX_DIR','STATE_PATH','RECENT_INDEX_PATH','LOG_DIR'])]:
            for name in names:
                p = patch.object(module, name, self.root / module.__name__ / name)
                p.start(); self.addCleanup(p.stop)
        # Get identity lookup must encompass both normal and recording notes.
        g.NOTES_DIR = g.RAW_DIR / '01 Notes'
        g.STATE_DIR = g.RAW_DIR / '03 State'
        f.MEMOS_DIR = f.RAW_DIR
        f.INDEX_DIR = f.RAW_DIR / '03 Index'
        f.STATE_PATH = f.INDEX_DIR / 'state.json'

    def note(self, ident='9223372036854775807', **kw):
        return dict(id=ident, title='same', content='summary', created_at='2020-01-01T00:00:00Z', updated_at='2020-01-01T00:00:00Z', **kw)

    def test_idempotency_title_edit_and_same_title_different_id(self):
        for path_for, render, write in [(g.output_path_for,g.render_note,g.write_if_changed),(f.memo_path_for,f.render_memo,f.write_if_changed)]:
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

    def test_flomo_failure_preserves_watermark_then_recovers(self):
        f.STATE_PATH.parent.mkdir(parents=True)
        prior='{"last_sync_at":"2020-01-01T00:00:00Z"}'
        f.STATE_PATH.write_text(prior)
        import urllib.error
        for result in [urllib.error.HTTPError('https://invalid',code,'failure',{},None) for code in (401,429,500)]+[[self.note()]]:
            with patch.dict(os.environ,{'VAULT_ROOT':str(self.root)}), patch.object(sys,'argv',['sync','--start-date','2020-01-01','--end-date','2020-01-01']), patch.object(f,'load_token',return_value='unused'), patch.object(f,'initialize_client'), patch.object(f,'search_memos_for_day',side_effect=[result]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                if isinstance(result,Exception):
                    with self.assertRaises(urllib.error.HTTPError): f.main()
                else:
                    self.assertEqual(f.main(),0)
            self.assertEqual(f.STATE_PATH.read_text(),prior)
        self.assertEqual(len(list(f.RAW_DIR.rglob('*.md'))),1)

    def test_flomo_cap_missing_detail_and_errors(self):
        import datetime
        for payload in [{'memos':[self.note()]}, {}, {'memos':[{'id':'1'}]}, {'memos':[], 'has_more':True}]:
            with patch.object(f,'call_tool',return_value=payload):
                with self.assertRaises(RuntimeError): f.search_memos_for_day('unused',datetime.date.today(),1)
        for payload in [{'error':{'code':1}},{'result':{'isError':True}}]:
            with patch.object(f,'rpc_call',return_value=payload):
                with self.assertRaises(RuntimeError): f.call_tool('unused','memo_search',{},1)

    def test_flomo_preserves_tags_in_original_body(self):
        n=self.note(); n['content']='hello #tag\n\nworld'
        self.assertIn(n['content'],f.render_memo(n))


if __name__ == '__main__': unittest.main()
