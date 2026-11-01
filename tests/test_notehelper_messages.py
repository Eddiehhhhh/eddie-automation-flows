import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import sync_notehelper_messages_to_raw as m


def node(ident='synthetic-message',content='普通消息'):
    return {'id':ident,'title':m.PREFIX+'例子','content':content,
        'savedAt':'2026-10-09T03:04:05.000Z','updatedAt':'2026-10-10T03:04:05.000Z','image':None}
def page(nodes,next=False,cursor=None):
    return {'edges':[{'node':n} for n in nodes],'pageInfo':{'hasNextPage':next,'endCursor':cursor}}


class Messages(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        self.guard=patch.object(m.cloud_private_checkout,'verify_private_checkout');self.guard.start()
    def tearDown(self):self.guard.stop();self.tmp.cleanup()
    def collect(self,nodes,**kwargs):return m.collect(self.root,'synthetic',fetcher=lambda *_:page(nodes),**kwargs)
    def state(self):return json.loads((self.root/m.STATE).read_bytes())
    def test_readonly_dryrun(self):
        self.assertTrue(self.collect([node()])['capture_complete']);self.assertFalse((self.root/m.BASE).exists());self.assertFalse((self.root/m.STATE).exists())
    def test_private_guard_zero_writes(self):
        for reason in ('public_repository','wrong_repository','missing_proof'):
            with patch.object(m.cloud_private_checkout,'verify_private_checkout',side_effect=ValueError(reason)):
                with self.assertRaises(ValueError):self.collect([node()],apply=True)
            self.assertFalse((self.root/m.STATE).exists());self.assertFalse((self.root/m.BASE).exists())
    def test_capture_source_current_seconds_neutral_identity(self):
        self.collect([node(content='内容\n<!--nh:synthetic-old-->')],apply=True)
        record=self.state()['messages']['synthetic-message'];path=self.root/m.BASE/record['current_path'];text=path.read_text()
        self.assertIn('2026-10-09T03:04:05',text);self.assertIn('source_message_ids:',text)
        self.assertNotIn('\nid:',text);self.assertNotIn('<!--nh:',text);self.assertIn('<!--source-notehelper:',text)
        self.assertTrue(m.eligible_path(self.root,path));self.assertFalse(m.eligible_path(self.root,path.parent.parent/'index.md'))
        raw=json.loads((path.parent/'source.json').read_bytes());self.assertEqual(raw['content'],'内容\n<!--nh:synthetic-old-->')
    def test_remote_every_blob_and_edited_asset_rejected(self):
        self.collect([node(content='📎 [a.pdf](https://files.example.invalid/a)')],apply=True,downloader=lambda _:b'%PDF-fixture')
        git=lambda *args:subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True,text=True).stdout.strip()
        git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture')
        git('add','--',str(m.BASE),str(m.STATE));git('commit','-qm','fixture');git('branch','-M','main')
        remote=self.root.parent/(self.root.name+'-remote.git')
        subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        try:
            git('remote','add','origin',str(remote));git('push','-q','origin','main')
            self.assertTrue(m.verify_remote(self.root))
            index=self.root/m.BASE/'index.md';index_raw=index.read_bytes();index.write_bytes(b'changed index')
            with self.assertRaisesRegex(ValueError,'message_remote_readback_failed'):m.verify_remote(self.root)
            index.write_bytes(index_raw)
            asset=self.root/m.BASE/self.state()['messages']['synthetic-message']['resources'][0]['path'];asset.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'message_remote_readback_failed'):m.verify_remote(self.root)
        finally:
            import shutil
            shutil.rmtree(remote)
    def test_http_auth_confined_to_relay(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response
        raw=json.dumps(page([node()])).encode();response.read.return_value=raw;response.status=200;response.headers={'Content-Length':str(len(raw))}
        opener=MagicMock();opener.open.return_value=response
        with patch.object(m.urllib.request,'build_opener',return_value=opener):m.api_page('synthetic-not-valid',None)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,m.ENDPOINT);self.assertEqual(request.get_header('X-api-key'),'synthetic-not-valid')
        self.assertEqual(json.loads(request.data)['variables']['query'],m.PREFIX)
        self.assertEqual(request.method,'POST')

    def test_current_index_only_state_current_and_manual_hold(self):
        self.collect([node()],apply=True);first=self.state()['messages']['synthetic-message']['current_path']
        self.collect([node(content='新版')],apply=True);state=self.state();current=state['messages']['synthetic-message']['current_path']
        index=self.root/m.BASE/'index.md';text=index.read_text()
        import urllib.parse
        self.assertIn('stage_only: true',text);self.assertIn(urllib.parse.quote(current,safe='/._-'),text)
        self.assertNotIn(urllib.parse.quote(first,safe='/._-'),text);self.assertEqual(m.sha(index.read_bytes()),state['index_sha256'])
        self.assertFalse(m.eligible_path(self.root,index))
        index.write_text('人工入口');before=index.read_bytes()
        report=self.collect([node(content='第三版')],apply=True)
        self.assertFalse(report['capture_complete']);self.assertEqual(report['held']['index'],'message_index_edited')
        self.assertEqual(index.read_bytes(),before)

    def test_source_edit_keeps_versions_and_upstream_absence(self):
        self.collect([node()],apply=True);old=self.state()['messages']['synthetic-message']['current_path']
        self.collect([node(content='新版')],apply=True);record=self.state()['messages']['synthetic-message']
        self.assertEqual(len(record['versions']),2);self.assertTrue((self.root/m.BASE/old).exists())
        self.assertFalse(m.eligible_path(self.root,self.root/m.BASE/old))
        coverage=self.collect([],apply=True);self.assertEqual(coverage['upstream_absent_preserved'],1)
    def test_manual_edit_hold_no_overwrite(self):
        self.collect([node()],apply=True);record=self.state()['messages']['synthetic-message'];path=self.root/m.BASE/record['current_path'];path.write_text('人工修改')
        report=self.collect([node(content='新版')],apply=True)
        self.assertFalse(report['capture_complete']);self.assertEqual(path.read_text(),'人工修改');self.assertEqual(len(self.state()['messages']['synthetic-message']['versions']),1)
        self.assertTrue(m.eligible_path(self.root,path))
    def test_credential_held_other_survives(self):
        fake='ghp_'+'A'*36
        coverage=self.collect([node('secret','凭据 '+fake),node('clean')],apply=True)
        self.assertEqual(coverage['held'],{'secret':'potential_credential_in_source'});self.assertEqual(set(self.state()['messages']),{'clean'})
        for path in (self.root/m.BASE).rglob('*'):
            if path.is_file():self.assertNotIn(fake.encode(),path.read_bytes())
    def test_pdf_download_relative_link_and_sha(self):
        url='https://files.example.invalid/synthetic.pdf';raw=b'%PDF-1.7\nfixture'
        self.collect([node(content='📎 [文档.pdf]('+url+')')],apply=True,downloader=lambda u:raw)
        record=self.state()['messages']['synthetic-message'];path=self.root/m.BASE/record['current_path']
        text=path.read_text();self.assertNotIn(url,text);self.assertEqual(len(record['resources']),1)
        resource=record['resources'][0];self.assertEqual((self.root/m.BASE/resource['path']).read_bytes(),raw)
        link='../../../'+resource['path'];self.assertIn(link,text);self.assertEqual((path.parent/link).resolve(),(self.root/m.BASE/resource['path']).resolve())
    def test_asset_failure_object_only(self):
        report=self.collect([node('bad','📎 [a.pdf](https://files.example.invalid/a)'),node('clean')],apply=True,downloader=lambda _:(_ for _ in ()).throw(ValueError('incomplete_asset')))
        self.assertEqual(report['held'],{'bad':'incomplete_asset'});self.assertEqual(set(self.state()['messages']),{'clean'})
    def test_empty_alt_image_html_image_and_metadata_image(self):
        row=node(content='![](https://files.example.invalid/one.png)\n<img src="https://files.example.invalid/two.png">')
        row['image']='https://files.example.invalid/one.png'
        seen=[]
        def download(url):seen.append(url);return b'PNG-'+url.encode()
        self.collect([row],apply=True,downloader=download)
        record=self.state()['messages']['synthetic-message']
        self.assertEqual(len(seen),2);self.assertEqual(len(record['resources']),2)
        text=(self.root/m.BASE/record['current_path']).read_text()
        self.assertNotIn('https://files.example.invalid',text)
    def test_http_403_explicit_held(self):
        import urllib.error
        def fail(_):raise urllib.error.HTTPError('https://files.example.invalid',403,'forbidden',{},None)
        report=self.collect([node(content='📎 [a.pdf](https://files.example.invalid/a)')],apply=True,downloader=fail)
        self.assertEqual(report['held'],{'synthetic-message':'asset_http_403'})

    def test_source_times_required_no_import_fallback(self):
        row=node();row['savedAt']=None
        self.assertFalse(self.collect([row],apply=True)['capture_complete']);self.assertFalse(self.state()['messages'])
    def test_legacy_multiple_ids_and_manual_text_never_suppressed(self):
        path=self.root/m.BASE.parent/'旧日报.md';path.parent.mkdir(parents=True);raw=b'handwritten\n<!--nh:one-->\n<!--nh:two-->';path.write_bytes(raw)
        report=self.collect([node('one'),node('two')],apply=True)
        legacy=report['legacy_files'][str(path.relative_to(self.root))];self.assertEqual(legacy['source_message_ids'],['one','two']);self.assertIn('unmerged',legacy['merge_status']);self.assertEqual(path.read_bytes(),raw);self.assertTrue(m.eligible_path(self.root,path))
    def test_pagination_full_terminal(self):
        calls=[]
        def fetch(_,after):
            calls.append(after);return page([node('one')],True,'100') if after is None else page([node('two')])
        rows,proof=m.fetch_all('synthetic',fetch);self.assertEqual(calls,[None,100]);self.assertEqual(len(rows),2);self.assertFalse(proof['terminal_has_next_page'])
    def test_invalid_pagination_and_duplicates_no_state(self):
        cases=[{'edges':[],'pageInfo':{}},page([node()],True,None),page([node(),node()]),{'data':{'search':page([node()])}}]
        for value in cases:
            with self.assertRaises(ValueError):m.collect(self.root,'synthetic',apply=True,fetcher=lambda *_:value)
            self.assertFalse((self.root/m.STATE).exists())
    def test_count_mismatch_and_insecure_embeds_failclosed(self):
        value=page([node()]);value['pageInfo']['totalCount']=2
        with self.assertRaisesRegex(ValueError,'message_source_count_mismatch'):m.fetch_all('synthetic',lambda *_:value)
        for content in ('![](http://files.example.invalid/a.png)','<img src="http://files.example.invalid/a.png">','📎 [a.pdf](http://files.example.invalid/a.pdf)'):
            with patch.object(m.shared,'download_asset') as download:
                report=self.collect([node(content=content)],apply=True)
                self.assertEqual(report['held'],{'synthetic-message':'unsafe_asset_url'});download.assert_not_called()
    def test_nonmessage_failclosed(self):
        row=node();row['title']='ordinary article'
        with self.assertRaisesRegex(ValueError,'unexpected_non_message'):self.collect([row],apply=True)
    def test_no_source_mutation_query(self):
        self.assertIn('items {',m.QUERY);self.assertIn('content',m.QUERY);self.assertNotIn('mutation',m.QUERY.lower());self.assertNotIn('delete',m.QUERY.lower())
    def test_missing_config_default_off(self):self.assertFalse(m.config(self.root)['enabled'])
    def test_asset_budgets_hold_reason(self):
        with patch.object(m.shared,'MAX_ASSET_BYTES',2):
            report=self.collect([node(content='📎 [a.pdf](https://files.example.invalid/a)')],apply=True,downloader=lambda _:b'123')
        self.assertEqual(report['held'],{'synthetic-message':'asset_file_budget'})

class Wiring(unittest.TestCase):
    def test_current_selection_shared_consumers(self):
        scripts=Path(__file__).resolve().parents[1]/'scripts'
        for name in ('iteration_incremental_audit.py','iteration_closeout.py'):
            self.assertIn('notehelper_messages.eligible_path(ROOT,path)',(scripts/name).read_text())
        self.assertIn('notehelper_messages.BASE',(scripts/'audit_raw_filenames.py').read_text())
    def test_workflow_modes_failure_and_same_commit(self):
        from types import SimpleNamespace
        import re
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/sync-notion-personal-data.yml').read_text()
        steps={part.splitlines()[0]:part for part in text.split('      - name: ')[1:]}
        def allowed(name,cancelled=False,only=False,overrides=None):
            outcomes={'private_checkout':'success','source_selection':'skipped','messages_config':'success','messages_sync':'failure','messages_commit':'success','messages_readback':'success','messages_requested':'skipped'}
            outcomes.update(overrides or {})
            model=SimpleNamespace(**{key:SimpleNamespace(outcome=value) for key,value in outcomes.items()})
            model.messages_config.outputs=SimpleNamespace(enabled='true')
            expression=steps[name].split('        if: ${{ ',1)[1].split(' }}',1)[0]
            expression=expression.replace('!cancelled()','not cancelled()')
            expression=re.sub(r'!inputs\.', 'not inputs.', expression).replace('&&','and').replace('||','or')
            return eval(expression,{'__builtins__':{}},{'steps':model,'inputs':SimpleNamespace(raw16_only=False,flomo_stage_only=False,full_sync=False,messages_only=only, notion_sources_only=False),'github':SimpleNamespace(event_name='workflow_dispatch' if only else 'schedule'),'cancelled':lambda:cancelled})
        config='Read private message source configuration'
        self.assertTrue(allowed(config));self.assertTrue(allowed(config,only=True));self.assertFalse(allowed(config,cancelled=True))
        self.assertIn('continue-on-error: true',steps[config])
        self.assertTrue(allowed('Persist message content and progress'))
        self.assertTrue(allowed('Report incomplete message capture'))
        self.assertFalse(allowed('Collect notehelper messages privately',cancelled=True))
        self.assertLess(text.index('name: Commit Raw updates'),text.index('name: Collect notehelper messages privately'))
        commit=steps['Persist message content and progress'];self.assertIn(str(m.BASE),commit);self.assertIn(str(m.STATE),commit)
        for name in ('Read private article source configuration','Read private flomo stage configuration','Sync podcast notes'):
            self.assertIn('!inputs.messages_only',steps[name])

if __name__=='__main__':unittest.main()
