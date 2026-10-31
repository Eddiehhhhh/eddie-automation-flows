"""Bounded hierarchy execution against a synthetic Git and persistent Notion server."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import notion_private_assets as assets
import base64
spec = importlib.util.spec_from_file_location('notion_bulk', Path(__file__).resolve().parents[1] / 'scripts/sync_wiki_to_notion.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
OUTER = str(uuid.uuid5(uuid.NAMESPACE_URL, 'synthetic:outer'))
REQUEST = str(uuid.uuid4())


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'private'
        self.root.mkdir()
        for relative in ['Wiki/a.md', 'Schema/b.md']:
            path = self.root / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text('# Synthetic\n\n' + relative + '\n')
        for args in [('init',), ('add', 'Wiki/a.md', 'Schema/b.md'), ('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic')]:
            subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
        self.snapshot = subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD'],text=True).strip()
        self.state_path = self.root / '.github/state/notion-wiki-mirror-state.json'
        self.remote = Path(self.tmp.name) / 'remote.json'
        self.pages = {}
        self.children = {OUTER: []}
        self.sends = []
        self.checkpoints = 0
        self.lose = None
        request = {'request_id': REQUEST, 'enabled': True, 'parent_page_id': OUTER,
                   'source_snapshot': self.snapshot, 'include_patterns': list(m.DEFAULT_INCLUDE_PATTERNS),
                   'exclude_patterns': list(m.DEFAULT_EXCLUDE_PATTERNS), 'sources': [
                       {'path': p, 'signature': m.file_signature(self.root / p)} for p in ['Schema/b.md','Wiki/a.md']]}
        m.write_json(self.state_path, {'root_title': m.DEFAULT_ROOT_TITLE, 'entries': {}, 'bulk_request': request})
        self.remote.write_bytes(self.state_path.read_bytes())
        for patch in [mock.patch.object(m,'ROOT',self.root), mock.patch.object(m,'STATE_PATH',self.state_path),
                      mock.patch.object(m,'require_published_state'), mock.patch.object(m,'persist_private_state',side_effect=self.persist),
                      mock.patch.object(m,'state_secret',return_value=None),
                      mock.patch.object(m,'retrieve_block_children',side_effect=lambda key,target:copy.deepcopy(self.children[target])),
                      mock.patch.object(m,'get_page',side_effect=lambda key,target:copy.deepcopy(self.pages[target])),
                      mock.patch.object(m,'_send_once',side_effect=self.send),
                      mock.patch.dict(os.environ, {'CODEX_THREAD_ID':'synthetic-owner'})]:
            patch.start();self.addCleanup(patch.stop)

    def persist(self, state):
        self.checkpoints += 1
        m.write_json(self.state_path,state)
        self.remote.write_bytes(self.state_path.read_bytes())

    def add_page(self, parent, title, blocks):
        page_id = str(uuid.uuid5(uuid.NAMESPACE_URL, parent + '/' + title))
        self.pages[page_id] = {'id': page_id, 'parent': {'type':'page_id','page_id':parent},
                              'properties': {'title': {'type':'title', 'title':[{'plain_text':title}]}}, 'archived': False}
        self.children[parent].append({'id':page_id,'type':'child_page','child_page':{'title':title}})
        self.children[page_id] = []
        self.append(page_id, blocks)
        return page_id

    def append(self, target, blocks):
        for block in blocks:
            observed=copy.deepcopy(block)
            kind=observed['type']
            block_id = str(uuid.uuid4())
            nested = observed[kind].pop('children', None)
            if observed[kind].get('type') == 'file_upload':
                observed[kind].pop('file_upload')
                observed[kind].update(type='file',file={'url':'https://s3.us-west-2.amazonaws.com/secure.notion-static.com/synthetic/file?signature=synthetic'})
            observed.update(id=block_id, has_children=bool(nested))
            if nested is not None:
                self.children[block_id] = []
                self.append(block_id, nested)
            if kind == 'table_row':
                for cell in observed[kind]['cells']:
                    for part in cell:
                        part['plain_text'] = part['text']['content']
                        part['annotations'] = {'bold':False,'italic':False,'strikethrough':False,'underline':False,'code':False,'color':'default'}
                        part['text']['link'] = None
            if kind.startswith('heading_'):observed[kind]['is_toggleable']=False
            if kind!='divider':observed[kind]['color']='default'
            self.children[target].append(observed)

    def send(self, key, method, path, payload):
        durable=json.loads(self.remote.read_text())
        self.assertTrue(any(x['status']=='dispatched' for x in durable['write_intents'].values()))
        self.sends.append((method,path))
        if method=='POST':
            title=payload['properties']['title']['title'][0]['text']['content']
            self.add_page(payload['parent']['page_id'],title,payload['children'])
        else:
            self.append(path.split('/')[2],payload['children'])
        if self.lose == len(self.sends):
            raise m.UnknownWrite('synthetic response lost')

    def legacy_root(self):
        target=self.add_page(OUTER,m.DEFAULT_ROOT_TITLE,m.root_page_blocks(m.DEFAULT_ROOT_TITLE))
        state=m.load_state();state['entries']['.']={'type':'root','signature':m.ROOT_SIGNATURE};self.persist(state)
        return target

    def test_full_hierarchy_parent_first_original_root_retained_and_noop(self):
        original=self.legacy_root()
        result=m.execute_bulk('synthetic',REQUEST)
        self.assertEqual(result['held'],0)
        self.assertEqual(result['total'],5)
        self.assertEqual(result['verified'],5)
        self.assertEqual(result['adopted'],1)
        self.assertEqual(len(self.sends),4)
        self.assertEqual(m.load_state()['entries']['.']['page_id'],original)
        self.assertEqual(len(self.children[OUTER]),1)
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['skipped'],5)
        checkpoint=self.checkpoints
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['skipped'],5)
        self.assertEqual(self.checkpoints,checkpoint)
        self.assertEqual(len(self.sends),4)

    def test_legacy_file_source_and_extra_human_blocks_machine_bound_without_write(self):
        root=self.legacy_root()
        directory=self.add_page(root,'Wiki',m.directory_page_blocks('Wiki'))
        body=m.file_page_blocks(self.root,'Wiki/a.md')
        target=self.add_page(directory,'a.md',body+[m.paragraph_block('human addition')])
        before=copy.deepcopy(self.children[target])
        state=m.load_state();state['entries']['Wiki']={'signature':m.DIRECTORY_SIGNATURE,'type':'directory'}
        state['entries']['Wiki/a.md']={'signature':m.file_signature(self.root/'Wiki/a.md'),'type':'file'}
        self.persist(state)
        result=m.execute_bulk('synthetic',REQUEST)
        self.assertEqual(result['held'],0)
        self.assertEqual(result['adopted'],3)
        self.assertEqual(self.children[target][:len(before)],before)
        self.assertEqual(m.load_state()['entries']['Wiki/a.md']['page_id'],target)
        self.assertEqual(len(self.sends),2)

    def test_unknown_directory_fences_children_not_independent_branch(self):
        self.legacy_root();self.lose=1
        result=m.execute_bulk('synthetic',REQUEST)
        self.assertEqual(result['held'],2)
        self.assertIn('Schema',m.load_state()['bulk_progress']['held'])
        self.assertIn('Schema/b.md',m.load_state()['bulk_progress']['held'])
        self.assertIn('Wiki/a.md',m.load_state()['entries'])
        original_sends=len(self.sends)
        self.state_path.write_bytes(self.remote.read_bytes())
        self.assertEqual(m.recover_intents('synthetic',persist=True)['verified'],1)
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
        self.assertEqual(len(self.sends),original_sends+1)

    def test_legacy_contextless_unknown_stops_all_before_target_mutation(self):
        state=m.load_state();state['write_intents']={'old':{'status':'unknown_write','kind':'create'}};self.persist(state)
        with self.assertRaises(m.MirrorHeld):m.execute_bulk('synthetic',REQUEST)
        self.assertEqual(self.sends,[])

    def test_bounded_initial_passes_finish_without_waiting_for_daily_schedule(self):
        self.legacy_root();state=m.load_state();state['bulk_request']['max_batches']=1;self.persist(state)
        passes=[]
        for _ in range(5):
            result=m.execute_bulk('synthetic',REQUEST);passes.append(result)
            if not result['deferred']:break
        self.assertEqual(len(self.sends),4)
        self.assertEqual(passes[-1]['held'],0)
        self.assertEqual(passes[-1]['verified'],5)
        self.assertEqual(len(passes),4)

    def test_wrong_opaque_or_manifest_prevents_every_write(self):
        before=self.checkpoints
        with self.assertRaises(m.MirrorHeld):m.execute_bulk('synthetic',str(uuid.uuid4()))
        state=m.load_state();state['bulk_request']['sources'][0]['signature']='0'*64;self.persist(state)
        with self.assertRaises(m.MirrorHeld):m.execute_bulk('synthetic',REQUEST)
        self.assertEqual(self.sends,[])
        self.assertEqual(self.checkpoints,before+1)

    def test_target_manual_rename_or_parent_change_never_creates_replacement(self):
        self.legacy_root();m.execute_bulk('synthetic',REQUEST)
        target=m.load_state()['entries']['Wiki/a.md']['page_id']
        self.pages[target]['parent']['page_id']=OUTER
        before=len(self.sends)
        result=m.execute_bulk('synthetic',REQUEST)
        self.assertEqual(result['held'],1)
        self.assertEqual(len(self.sends),before)

    def test_parent_pagination_failure_never_means_missing_page(self):
        self.legacy_root()
        with mock.patch.object(m,'list_child_pages',side_effect=m.MirrorHeld('incomplete pagination')):
            self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],5)
        self.assertEqual(self.sends,[])

    def test_native_table_real_response_defaults_and_nested_human_edit(self):
        path = self.root / 'Wiki/a.md'
        path.write_text('# Synthetic table\n\n| name | value |\n| --- | --- |\n| one | two |\n')
        subprocess.run(['git','-C',str(self.root),'add','Wiki/a.md'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic table'],check=True,capture_output=True)
        state=m.load_state();state['bulk_request']['follow_source_changes']=True;self.persist(state)
        self.legacy_root()
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
        target=m.load_state()['entries']['Wiki/a.md']['page_id']
        table=next(block for block in self.children[target] if block['type']=='table')
        rows=self.children[table['id']]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['table_row']['cells'][1][0]['text']['content'],'two')
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
        sends=len(self.sends)
        rows[1]['table_row']['cells'][1][0]['text']['content']='human edited nested cell'
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],1)
        self.assertEqual(len(self.sends),sends)
        self.assertEqual(rows[1]['table_row']['cells'][1][0]['text']['content'],'human edited nested cell')

    def test_nested_payload_limits_and_fenced_table_literal(self):
        rows='\n'.join('| row | value |' for _ in range(300))
        blocks=m.native_markdown_to_blocks('| name | value |\n| --- | --- |\n'+rows)
        self.assertEqual(len(blocks),4)
        self.assertEqual(sum(len(b['table']['children'])-1 for b in blocks),300)
        chunks=m.payload_chunks(blocks*10)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(chunk)<=97 for chunk in chunks))
        literal=m.native_markdown_to_blocks('```md\n| a | b |\n| --- | --- |\n```')
        self.assertEqual([b['type'] for b in literal],['code'])

    def test_fresh_sparse_runner_reads_sources_from_fixed_git_objects(self):
        (self.root/'Wiki/a.md').unlink()
        (self.root/'Schema/b.md').unlink()
        self.legacy_root()
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
        self.assertEqual(m.load_state()['entries']['Wiki/a.md']['signature'],m.sha256_text('# Synthetic\n\nWiki/a.md\n'))

    def test_compaction_only_verified_and_human_append_preserved(self):
        self.legacy_root();m.execute_bulk('synthetic',REQUEST)
        state=m.load_state()
        self.assertTrue(all(i['receipt_compacted'] and 'request' not in i['context'] for i in state['write_intents'].values()))
        sample=copy.deepcopy(next(iter(state['write_intents'].values())))
        sample.update(status='unknown_write',expected=['sentinel-private-request'],baseline=['sentinel-private-baseline'])
        sample.pop('receipt_compacted');state['write_intents']['synthetic-pending']=sample
        m.compact_verified_intents(state,self.snapshot)
        self.assertEqual(state['write_intents']['synthetic-pending']['expected'],['sentinel-private-request'])
        target=m.load_state()['entries']['Wiki/a.md']['page_id']
        self.append(target,[m.paragraph_block('new human append')])
        sends=len(self.sends)
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
        self.assertEqual(len(self.sends),sends)
        self.assertEqual(self.children[target][-1]['paragraph']['rich_text'][0]['text']['content'],'new human append')

    def asset_source(self):
        data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2r8sAAAAASUVORK5CYII=')
        path=self.root/'assets/photo.png';path.parent.mkdir();path.write_bytes(data)
        (self.root/'Wiki/a.md').write_text('# Synthetic image\n\n![Original](../assets/photo.png)\n')
        subprocess.run(['git','-C',str(self.root),'add','assets/photo.png','Wiki/a.md'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic image'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.root),'remote','add','origin','https://github.com/example/private-fixture.git'],check=True,capture_output=True)
        state=m.load_state();state['bulk_request']['follow_source_changes']=True;self.persist(state)
        self.legacy_root()
        return data

    def test_native_asset_real_caller_remote_checkpoints_binary_readback_and_noop(self):
        data=self.asset_source();calls=[]
        upload='11111111-1111-4111-8111-111111111111'
        def api(key,method,path,body=None,content_type=None):
            calls.append((method,path))
            if method=='POST':
                remote=json.loads(self.remote.read_text())
                phase=next(iter(remote['assets'].values()))['phase']
                self.assertEqual(phase,'dispatched' if path.endswith('/send') else 'prepared')
            if path=='/users/me':return {'bot':{'workspace_limits':{'max_file_upload_size_in_bytes':5*1024*1024}}}
            if path=='/file_uploads':return {'object':'file_upload','id':upload,'status':'pending'}
            receipt=next(iter(m.load_state()['assets'].values()))
            return {'object':'file_upload','id':upload,'status':'uploaded','filename':receipt['filename'],
                    'content_type':receipt['content_type'],'content_length':len(data),'expiry_time':None}
        with mock.patch.object(assets.private_checkout,'REPOSITORY','example/private-fixture'),mock.patch.object(assets,'_request',side_effect=api),mock.patch.object(assets,'_download_returned_asset',return_value=data):
            result=m.execute_bulk('synthetic',REQUEST)
            self.assertEqual((result['held'],result['assets_ready'],result['assets_held']),(0,1,0))
            target=m.load_state()['entries']['Wiki/a.md']['page_id']
            self.assertEqual(sum(b['type']=='image' for b in self.children[target]),1)
            writes=sum(method=='POST' for method,_ in calls)
            self.assertEqual(writes,2)
            sends=len(self.sends)
            self.assertEqual(m.execute_bulk('synthetic',REQUEST)['assets_held'],0)
            self.assertEqual(sum(method=='POST' for method,_ in calls),writes)
            self.assertEqual(len(self.sends),sends)
            # Target bytes, rather than signed URL text, determine whether a human replacement occurred.
            m._ASSET_READBACK_CACHE.clear()
            with mock.patch.object(assets,'_download_returned_asset',return_value=b'changed binary'):
                self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],1)
            self.assertEqual(len(self.sends),sends)

    def test_asset_failure_retains_clickable_original_and_separate_incomplete_count(self):
        self.asset_source()
        with mock.patch.object(assets.private_checkout,'REPOSITORY','example/private-fixture'),mock.patch.object(assets,'_request',side_effect=assets.AssetHeld('asset_access_or_limit_denied')):
            result=m.execute_bulk('synthetic',REQUEST)
            self.assertEqual(result['held'],0)
            self.assertEqual((result['assets_held'],result['assets_fallback']),(1,1))
            self.assertEqual(result['verified'],5)
            target=m.load_state()['entries']['Wiki/a.md']['page_id']
            parts=[part for b in self.children[target] for part in b.get(b['type'],{}).get('rich_text',[])]
            links=[part['text']['link']['url'] for part in parts if part.get('text',{}).get('link')]
            self.assertTrue(any(url.startswith('https://github.com/example/private-fixture/blob/') for url in links))
            self.assertFalse(any(b['type']=='image' for b in self.children[target]))
            sends=len(self.sends)
            self.assertEqual(m.execute_bulk('synthetic',REQUEST)['assets_held'],1)
            self.assertEqual(len(self.sends),sends)

    def test_true_plain_source_noop_and_reverted_source_gets_new_revision(self):
        self.legacy_root();m.execute_bulk('synthetic',REQUEST)
        entry=m.load_state()['entries']['Wiki/a.md']
        self.assertEqual(entry['render_version'],'legacy-v1')
        original=(self.root/'Wiki/a.md').read_text()
        state=m.load_state();state['bulk_request']['follow_source_changes']=True;self.persist(state)
        for text in ['changed source body\n',original]:
            (self.root/'Wiki/a.md').write_text(text)
            subprocess.run(['git','-C',str(self.root),'add','Wiki/a.md'],check=True,capture_output=True)
            subprocess.run(['git','-C',str(self.root),'-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic revision'],check=True,capture_output=True)
            self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
        final=m.load_state()['entries']['Wiki/a.md']
        self.assertEqual(final['signature'],m.sha256_text(original))
        self.assertEqual(final['revision_generation'],3)

    def test_recursive_html_css_json_dependency_inventory_is_bounded_and_visible(self):
        self.asset_source()
        (self.root/'assets/view.html').write_text('<link href="style.css"><img src="photo.png">')
        (self.root/'assets/style.css').write_text('body{background:url(photo.png)} @import "style.css";')
        (self.root/'assets/config.json').write_text('{"image":"photo.png","ordinaryText":"not-a-path"}')
        (self.root/'Wiki/a.md').write_text('[View](../assets/view.html)\n[Config](../assets/config.json)\n')
        subprocess.run(['git','-C',str(self.root),'add','assets/view.html','assets/style.css','assets/config.json','Wiki/a.md'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic bundle'],check=True,capture_output=True)
        with mock.patch.object(assets.private_checkout,'REPOSITORY','example/private-fixture'),mock.patch.object(assets,'_request',side_effect=assets.AssetHeld('asset_access_or_limit_denied')):
            result=m.execute_bulk('synthetic',REQUEST)
            self.assertEqual(result['held'],0)
            self.assertEqual(result['assets_held'],4)
            self.assertEqual(result['assets_fallback'],4)
            target=m.load_state()['entries']['Wiki/a.md']['page_id']
            linked=[part['text']['link']['url'] for b in self.children[target] for part in b.get(b['type'],{}).get('rich_text',[]) if part.get('text',{}).get('link')]
            self.assertTrue(any(url.endswith('/assets/photo.png') for url in linked))
            self.assertTrue(any(url.endswith('/assets/style.css') for url in linked))
            sends=len(self.sends)
            self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
            self.assertEqual(len(self.sends),sends)
        self.assertEqual(m.bundle_references('body{background:url("a.png")} @import "b.css";','.css'),['a.png','b.css'])
        with self.assertRaises(ValueError):m.bundle_references('<base href="/different/">','.html')

    def test_unknown_asset_send_clean_runner_only_reads_then_appends_one_ready_version(self):
        data=self.asset_source();calls=[];lose=[True]
        upload='11111111-1111-4111-8111-111111111111'
        def api(key,method,path,body=None,content_type=None):
            calls.append((method,path))
            if path=='/users/me':return {'bot':{'workspace_limits':{'max_file_upload_size_in_bytes':5*1024*1024}}}
            if path=='/file_uploads':return {'object':'file_upload','id':upload,'status':'pending'}
            if path.endswith('/send') and lose[0]:
                lose[0]=False
                raise assets.UnknownAssetWrite('asset_write_unknown')
            receipt=next(iter(m.load_state()['assets'].values()))
            return {'object':'file_upload','id':upload,'status':'uploaded','filename':receipt['filename'],
                    'content_type':receipt['content_type'],'content_length':len(data),'expiry_time':None}
        with mock.patch.object(assets.private_checkout,'REPOSITORY','example/private-fixture'),mock.patch.object(assets,'_request',side_effect=api),mock.patch.object(assets,'_download_returned_asset',return_value=data):
            first=m.execute_bulk('synthetic',REQUEST)
            self.assertEqual((first['held'],first['assets_held']),(0,1))
            target=m.load_state()['entries']['Wiki/a.md']['page_id']
            original=copy.deepcopy(self.children[target])
            self.state_path.write_bytes(self.remote.read_bytes())
            second=m.execute_bulk('synthetic',REQUEST)
            self.assertEqual((second['held'],second['assets_ready']),(0,1))
            self.assertEqual(sum(method=='POST' for method,_ in calls),2)
            self.assertEqual(self.children[target][:len(original)],original)
            self.assertEqual(sum(b['type']=='image' for b in self.children[target]),1)
            sends=len(self.sends)
            self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
            self.assertEqual(len(self.sends),sends)

    def test_legacy_pipe_table_gets_native_revision_without_source_change(self):
        path=self.root/'Wiki/a.md'
        path.write_text('| name | value |\n| --- | --- |\n| one | two |\n')
        subprocess.run(['git','-C',str(self.root),'add','Wiki/a.md'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic table'],check=True,capture_output=True)
        root=self.legacy_root()
        directory=self.add_page(root,'Wiki',m.directory_page_blocks('Wiki'))
        target=self.add_page(directory,'a.md',m.file_page_blocks(self.root,'Wiki/a.md'))
        original=copy.deepcopy(self.children[target])
        signature=m.file_signature(path)
        state=m.load_state();state['bulk_request']['follow_source_changes']=True
        state['entries']['Wiki']={'type':'directory','signature':m.DIRECTORY_SIGNATURE}
        state['entries']['Wiki/a.md']={'type':'file','signature':signature}
        self.persist(state)
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
        self.assertEqual(m.load_state()['entries']['Wiki/a.md']['signature'],signature)
        self.assertTrue(m.load_state()['entries']['Wiki/a.md']['render_version'].startswith('native-v1:'))
        self.assertEqual(self.children[target][:len(original)],original)
        self.assertEqual(sum(b['type']=='table' for b in self.children[target]),1)
        sends=len(self.sends)
        self.assertEqual(m.execute_bulk('synthetic',REQUEST)['held'],0)
        self.assertEqual(len(self.sends),sends)

    def test_failed_checkpoint_aborts_before_any_other_source(self):
        self.legacy_root()
        with mock.patch.object(m,'persist_private_state',side_effect=m.MirrorHeld('checkpoint failed','checkpoint_publication_failed')):
            with self.assertRaises(m.MirrorHeld):m.execute_bulk('synthetic',REQUEST)
        self.assertEqual(self.sends,[])


if __name__ == '__main__':unittest.main()
