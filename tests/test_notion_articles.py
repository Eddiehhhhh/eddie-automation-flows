"""Article collector regressions use synthetic sources and local temporary Git."""
from contextlib import nullcontext
from copy import deepcopy
import datetime as dt
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_notion_articles_to_raw as articles
import iteration_incremental_audit as audit

DB = '11111111-1111-1111-1111-111111111111'
PID = '22222222-2222-2222-2222-222222222222'
OLD = '33333333-3333-3333-3333-333333333333'


def page():
    return {'id': PID, 'created_time':'2026-01-01T00:00:00Z', 'last_edited_time':'2026-11-01T00:00:00Z',
        'properties': {'标题': {'type':'title','title':[{'plain_text':'Source article'}]},
            '原始链接':{'type':'url','url':'https://example.invalid/share?xsec_token=share-context'},
            '标签':{'type':'multi_select','multi_select':[{'name':'agent'}]}}}


def blocks(text='Original source'):
    return [{'id':'body','type':'paragraph','has_children':False,
        'paragraph':{'rich_text':[{'plain_text':text}]}},
        {'id':'image','type':'image','has_children':False,
        'image':{'type':'file','file':{'url':'https://files.invalid/example.png?Expires=synthetic'}}}]


class ArticlesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.legacy = self.root / articles.BASE.parent / '2026-01-01/original.md'
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text('---\nid: '+ OLD + '\nurl: https://example.invalid/share?xsec_token=share-context\n---\nAI summary stays.\n')
        self.original = self.legacy.read_bytes()
        self.rows = [page()]; self.body = blocks()
        self.stack = mock.patch.multiple(articles.notion, paginate_database=mock.DEFAULT,
            retrieve_block_children=mock.DEFAULT, notion_request=mock.DEFAULT)
        self.api = self.stack.start(); self.addCleanup(self.stack.stop)
        self.api['paginate_database'].side_effect = lambda *a,**k: iter(deepcopy(self.rows))
        self.api['retrieve_block_children'].side_effect = lambda *a: deepcopy(self.body)
        self.api['notion_request'].side_effect = lambda *a: deepcopy(self.rows[0])
        for target in ('cloud_private_checkout.verify_private_checkout','shared.writer_lock'):
            patch = mock.patch.object(articles.cloud_private_checkout, 'verify_private_checkout') if target.startswith('cloud') else mock.patch.object(articles.shared,'writer_lock',return_value=nullcontext())
            patch.start(); self.addCleanup(patch.stop)
        self.addCleanup(lambda:self.assertEqual(self.original,self.legacy.read_bytes()))

    def run_sync(self, **kwargs):
        return articles.sync(self.root,DB,'synthetic',apply=True,downloader=kwargs.pop('downloader',lambda u:b'image bytes'),**kwargs)

    def state(self): return json.loads((self.root/articles.STATE).read_bytes())

    def test_capture_images_json_legacy_and_idempotency(self):
        result=self.run_sync(); self.assertTrue(result['complete'])
        record=self.state()['pages'][PID]
        self.assertEqual(record['legacy_upstream_ids'],[OLD])
        self.assertNotEqual(PID,OLD)
        text=(self.root/articles.BASE/(PID+'.md')).read_text()
        self.assertIn('![image](versions/',text); self.assertIn('agent',text)
        self.assertIn('legacy_upstream_id:',text); self.assertNotIn('stage_only:',text)
        source=next(self.root/articles.BASE/p for p in record['files'] if p.endswith('source.json'))
        self.assertIn('Expires=synthetic',source.read_text())
        before={str(p):p.read_bytes() for p in (self.root/articles.BASE/'versions').rglob('*') if p.is_file()}
        again=self.run_sync(); self.assertEqual(again['unchanged'],1)
        self.assertEqual(before,{str(p):p.read_bytes() for p in (self.root/articles.BASE/'versions').rglob('*') if p.is_file()})

    def test_all_63_pages_without_arbitrary_cap(self):
        self.rows=[{**page(),'id':f'{i:08x}-2222-2222-2222-222222222222'} for i in range(63)]
        self.api['notion_request'].side_effect=lambda key,method,path: deepcopy(next(p for p in self.rows if path.endswith(p['id'])))
        self.assertEqual(self.run_sync()['written'],63)
        self.assertEqual(len(self.state()['pages']),63)

    def test_ambiguous_ids_are_relationships_not_single_identity(self):
        other=self.legacy.with_name('second.md'); other.write_bytes(self.original.replace(OLD.encode(),b'44444444-4444-4444-4444-444444444444'))
        self.run_sync(); record=self.state()['pages'][PID]
        self.assertFalse(record['relation_unique']); self.assertEqual(len(record['legacy_upstream_ids']),2)
        self.assertNotIn('legacy_upstream_id:',(self.root/articles.BASE/(PID+'.md')).read_text())

    def test_empty_page_is_retained(self):
        self.body=[]; self.rows[0]['properties'].pop('原始链接')
        self.assertTrue(self.run_sync()['complete']); self.assertIn(PID,self.state()['pages'])

    def test_private_gate_before_source_read_and_write(self):
        with mock.patch.object(articles.cloud_private_checkout,'verify_private_checkout',side_effect=ValueError('blocked')):
            with self.assertRaises(ValueError):self.run_sync()
        self.api['paginate_database'].assert_not_called();self.assertFalse((self.root/articles.BASE).exists())

    def test_credentials_rejected_before_any_source_bytes_written(self):
        for location in ('metadata','body','asset'):
            with self.subTest(location=location):
                secret='ghp_'+'A'*35
                self.rows=[page()];self.body=blocks()
                if location=='metadata':self.rows[0]['properties']['标题']['title'][0]['plain_text']=secret
                if location=='body':self.body=blocks(secret)
                if location=='metadata':
                    with self.assertRaises(ValueError):self.run_sync()
                else:
                    result=self.run_sync(downloader=lambda url:secret.encode() if location=='asset' else b'asset')
                    self.assertEqual(result['held'][PID],'potential_credential_in_source')
                version=self.root/articles.BASE/'versions';self.assertFalse(version.exists())
                if (self.root/articles.STATE).exists():self.assertEqual(self.state()['pages'],{})

    def test_notion_account_key_shape_is_held(self):
        self.body=blocks('ntn_'+'A'*45)
        self.assertEqual(self.run_sync()['held'][PID],'potential_credential_in_source')
        self.assertFalse((self.root/articles.BASE/'versions').exists())

    def test_partial_failure_preserves_successful_object_receipt(self):
        second='55555555-5555-5555-5555-555555555555'
        self.rows.append({**page(),'id':second})
        self.api['notion_request'].side_effect=lambda key,method,path:deepcopy(next(row for row in self.rows if path.endswith(row['id'])))
        calls=[]
        def download(url):
            calls.append(url)
            if len(calls)==1:raise ValueError('incomplete_asset')
            return b'asset'
        result=self.run_sync(downloader=download)
        self.assertFalse(result['complete']);self.assertEqual(result['written'],1)
        self.assertEqual(set(self.state()['pages']),{second})
        self.assertEqual(result['held'],{PID:'incomplete_asset'})

    def test_cli_suppresses_private_error_details(self):
        output=io.StringIO()
        with mock.patch.object(sys,'argv',['collector','--verify-remote-state']), mock.patch.dict(os.environ,{'VAULT_ROOT':str(self.root)}), \
             mock.patch.object(articles,'verify_remote_state',side_effect=ValueError('private content /sensitive-path')), \
             mock.patch('sys.stdout',output):
            self.assertEqual(articles.main(),2)
        self.assertNotIn('sensitive',output.getvalue());self.assertNotIn('private content',output.getvalue())

    def test_current_human_edits_are_held(self):
        self.run_sync();path=self.root/articles.BASE/(PID+'.md');path.write_text('User edited')
        self.rows[0]['last_edited_time']='2026-11-02T00:00:00Z'
        self.assertEqual(self.run_sync()['held'][PID],'owned_content_edited');self.assertEqual(path.read_text(),'User edited')

    def test_historical_edit_is_held(self):
        self.run_sync();p=next(iter(self.state()['pages'][PID]['files']))
        (self.root/articles.BASE/p).write_text('User edited')
        self.assertEqual(self.run_sync()['held'][PID],'owned_content_edited')

    def test_changed_source_keeps_both_versions(self):
        self.run_sync();first=self.state()['pages'][PID]['current_version']
        self.rows[0]['last_edited_time']='2026-11-02T00:00:00Z';self.body=blocks('New original')
        self.run_sync();versions=self.state()['pages'][PID]['versions']
        self.assertIn(first,versions);self.assertEqual(len(versions),2)

    def test_source_changes_during_capture_no_receipt(self):
        self.api['notion_request'].return_value={};self.api['notion_request'].side_effect=lambda *a:{'last_edited_time':'changed'}
        self.assertEqual(self.run_sync()['held'][PID],'source_changed_during_read');self.assertEqual(self.state()['pages'],{})

    def test_concurrent_state_change_not_overwritten(self):
        state_path=self.root/articles.STATE
        def download(url):
            state_path.parent.mkdir(parents=True);state_path.write_bytes(b'{"another_owner": true}')
            return b'asset'
        with self.assertRaisesRegex(ValueError,'state_changed'):self.run_sync(downloader=download)
        self.assertEqual(state_path.read_bytes(),b'{"another_owner": true}')
        self.assertFalse((self.root/articles.BASE/'versions').exists())

    def test_unowned_current_is_never_replaced(self):
        p=self.root/articles.BASE/(PID+'.md');p.parent.mkdir(parents=True);p.write_text('Preexisting')
        with self.assertRaisesRegex(ValueError,'unowned_source_namespace'): self.run_sync()
        self.assertEqual(p.read_text(),'Preexisting')

    def test_attachment_failure_does_not_advance_object(self):
        def fail(url):raise ValueError('incomplete_asset')
        self.assertEqual(self.run_sync(downloader=fail)['held'][PID],'incomplete_asset');self.assertEqual(self.state()['pages'],{})

    def test_no_type_wall_for_pdf_or_binary(self):
        self.body[1]={'id':'file','type':'pdf','pdf':{'type':'file','file':{'url':'https://files.invalid/a.pdf'}}}
        self.assertTrue(self.run_sync(downloader=lambda u:b'%PDF synthetic')['complete'])
        self.assertIn('[pdf](versions/',(self.root/articles.BASE/(PID+'.md')).read_text())

    def test_duplicate_or_invalid_page_id_fails_before_write(self):
        for rows in ([page(),page()],[{**page(),'id':'../../escape'}]):
            self.rows=rows
            with self.assertRaisesRegex(ValueError,'incomplete_page_inventory'):self.run_sync()
            self.assertFalse((self.root/articles.BASE).exists())

    def test_no_writes_to_notion_and_retries_scope_restored(self):
        self.run_sync()
        for call in self.api['notion_request'].call_args_list:self.assertEqual(call.args[1],'GET')
        self.assertEqual(articles.notion.READ_RETRIES.get(),4)

    def test_remote_readback_real_git_and_uncommitted_asset_detection(self):
        self.run_sync()
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','-C',str(self.root),'add','--all'],check=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture'],check=True)
        bare=self.root/'remote.git';subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
        subprocess.run(['git','-C',str(self.root),'remote','add','origin',str(bare)],check=True)
        subprocess.run(['git','-C',str(self.root),'push','-q','origin','HEAD:main'],check=True)
        self.assertTrue(articles.verify_remote_state(self.root))
        (self.root/articles.BASE/(PID+'.md')).write_text('Modified after remote')
        with self.assertRaisesRegex(ValueError,'owned_content_edited'):articles.verify_remote_state(self.root)

    def test_current_only_iteration(self):
        self.run_sync()
        with mock.patch.object(audit,'ROOT',self.root):
            current=self.root/articles.BASE/(PID+'.md')
            self.assertIsNotNone(audit.candidate_from_file(current,str(articles.BASE.parent),dt.date(2026,1,1),dt.date(2026,12,31)))
            for path in (self.root/articles.BASE/'versions').rglob('*.md'):
                self.assertIsNone(audit.candidate_from_file(path,str(articles.BASE.parent),dt.date(2026,1,1),dt.date(2026,12,31)))
            self.assertIsNone(audit.candidate_from_file(self.root/articles.BASE/'index.md',str(articles.BASE.parent),dt.date(2026,1,1),dt.date(2026,12,31)))


class PaginationTests(unittest.TestCase):
    def test_late_pagination_failure_yields_no_partial_rows(self):
        replies=[{'results':[page()],'has_more':True,'next_cursor':'next'}, {'results':[],'has_more':True}]
        with mock.patch.object(articles.notion,'query_database',side_effect=replies):
            stream=articles.notion.paginate_database('fixture',DB)
            with self.assertRaises(RuntimeError):next(stream)

    def test_bounded_retry_after(self):
        error=urllib.error.HTTPError('https://api.notion.invalid',429,'limited',{'Retry-After':'0'},io.BytesIO())
        token=articles.notion.READ_RETRIES.set(2)
        try:
            with mock.patch.object(articles.notion.urllib.request,'urlopen',side_effect=error) as send, mock.patch.object(articles.notion.time,'sleep'):
                with self.assertRaises(RuntimeError):articles.notion.notion_request('fixture','GET','/pages/fixture')
                self.assertEqual(send.call_count,3)
        finally:articles.notion.READ_RETRIES.reset(token)

    def test_oversized_retry_after_does_not_sleep(self):
        error=urllib.error.HTTPError('https://api.notion.invalid',429,'limited',{'Retry-After':'9999'},io.BytesIO())
        with mock.patch.object(articles.notion.urllib.request,'urlopen',side_effect=error), mock.patch.object(articles.notion.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'budget'):articles.notion.notion_request('fixture','GET','/pages/fixture')
            sleep.assert_not_called()


class WorkflowTests(unittest.TestCase):
    def test_source_modes_partial_failure_cancellation_and_persistence(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/sync-notion-personal-data.yml').read_text()
        steps={part.splitlines()[0]:part for part in text.split('      - name: ')[1:]}
        def allowed(name, raw=False, flomo=False, full=False, event='schedule', cancelled=False, overrides=None):
            outcomes={'private_checkout':'success','source_selection':'skipped','raw16_config':'success',
                'raw16_requested':'skipped','raw16_sync':'success','raw16_commit':'success','raw16_readback':'success'}
            outcomes.update(overrides or {})
            model=SimpleNamespace(**{key:SimpleNamespace(outcome=value) for key,value in outcomes.items()})
            model.raw16_config.outputs=SimpleNamespace(enabled='true')
            expression=steps[name].split('        if: ${{ ',1)[1].split(' }}',1)[0]
            expression=expression.replace('!cancelled()','not cancelled()')
            expression=re.sub(r'!inputs\.', 'not inputs.', expression).replace('&&','and').replace('||','or')
            return eval(expression,{'__builtins__':{}},{'steps':model,'inputs':SimpleNamespace(raw16_only=raw,flomo_stage_only=flomo,full_sync=full),
                'github':SimpleNamespace(event_name=event),'cancelled':lambda:cancelled,'success':lambda:True})
        config='Read private article source configuration'
        self.assertTrue(allowed(config));self.assertTrue(allowed(config,raw=True,event='workflow_dispatch'))
        self.assertFalse(allowed(config,event='workflow_dispatch'));self.assertFalse(allowed(config,flomo=True))
        self.assertFalse(allowed(config,full=True));self.assertFalse(allowed(config,cancelled=True))
        self.assertFalse(allowed('Sync podcast notes',raw=True))
        commit='Persist article content and progress'
        self.assertTrue(allowed(commit,overrides={'raw16_sync':'failure'}))
        self.assertFalse(allowed(commit,cancelled=True))
        self.assertFalse(allowed('Verify private article content and state',overrides={'raw16_commit':'failure'}))
        self.assertTrue(allowed('Report incomplete article source capture',overrides={'raw16_sync':'failure'}))
        self.assertIn('notion-articles-sync-state.json',steps[commit]);self.assertIn('--verify-remote-state',steps['Verify private article content and state'])
        self.assertEqual(text.count('cron:'),2)


if __name__=='__main__':unittest.main()
