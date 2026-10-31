import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('notion_safety_candidate', Path(__file__).resolve().parents[1] / 'scripts' / 'sync_wiki_to_notion.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
with patch.dict(os.environ, {'VAULT_ROOT': '/tmp/notion-safety-review/empty-fixture'}):
    spec.loader.exec_module(m)

class SafetyCandidateTests(unittest.TestCase):
    def test_replace_held_before_read_archive_or_append(self):
        with patch.object(m,'retrieve_block_children') as read, patch.object(m,'archive_block') as archive, patch.object(m,'append_blocks') as append:
            with self.assertRaises(m.MirrorHeld):
                m.replace_page_content('fixture','page',[m.paragraph_block('source')])
            read.assert_not_called(); archive.assert_not_called(); append.assert_not_called()

    def test_search_transport_failure_retries_as_read(self):
        with patch.object(m.urllib.request, 'urlopen', side_effect=[TimeoutError('lost'), io.BytesIO(b'{"results":[]}')]) as request, patch.object(m.time,'sleep'):
            self.assertEqual(m.notion_request('fixture','POST','/search',{}), {'results':[]})
            self.assertEqual(request.call_count, 2)

    def test_recover_mode_unknown_error_does_not_persist(self):
        with patch.object(m.sys,'argv',['sync_wiki_to_notion.py','--recover-intents']), patch.object(m,'_main',side_effect=m.UnknownWrite('fixture')), patch.object(m,'persist_private_state') as persist, patch.object(m,'write_json') as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(), 2)
            persist.assert_not_called()
            write.assert_not_called()

    def test_mutation_gate_blocks_create_append_title_and_archive(self):
        for method,path in [('POST','/pages'),('PATCH','/blocks/page/children'),('PATCH','/pages/page'),('PATCH','/blocks/block')]:
            with self.subTest(path=path), patch.object(m.urllib.request,'urlopen') as request:
                with self.assertRaises(m.MirrorHeld):
                    m.notion_request('fixture',method,path,{})
                request.assert_not_called()

    def test_get_keeps_existing_transport_and_server_retry(self):
        for error in [TimeoutError('lost'),urllib.error.HTTPError('fixture',503,'server',{},io.BytesIO(b'fixture'))]:
            with self.subTest(error=type(error).__name__),patch.object(m.urllib.request,'urlopen',side_effect=[error,io.BytesIO(b'{"results":[]}' )]) as request,patch.object(m.time,'sleep'):
                self.assertEqual(m.notion_request('fixture','GET','/blocks/page/children'),{'results':[]})
                self.assertEqual(request.call_count,2)

    def test_unknown_marker_retains_entries_and_exits_two(self):
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'state.json';state.write_text(json.dumps({'entries':{'source':{'signature':'old'}}}))
            with patch.object(m,'STATE_PATH',state),patch.object(m,'_main',side_effect=m.UnknownWrite('fixture')),patch.object(m,'persist_private_state',side_effect=lambda payload: m.write_json(state, payload)),contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(m.main(),2)
            saved=json.loads(state.read_text());self.assertEqual(saved['entries'],{'source':{'signature':'old'}})
            self.assertEqual(saved['write_outcome'],{'status':'unknown_write','retry_allowed':False})
            self.assertEqual(json.loads(output.getvalue())['status'],'unknown_write')

    def run_mirror(self, previous, children, nodes=None):
        args=SimpleNamespace(execute_request=None,execute_revision=None,allow_new_page=False,persist_recovery=False,recover_intents=False,path_prefix=None,limit=0,plan_only=False,root_title='fixture',parent_page_title='fixture',parent_page_id='parent',dry_run=False,full_sync=False)
        nodes=nodes or [m.Node('.',None,'fixture','root',m.ROOT_SIGNATURE)]
        patches={'parse_args':args,'load_patterns':([],[]),'resolve_source_paths':(Path('/tmp'),[],None,'fixture'),'build_nodes':nodes,'load_notion_api_key':'fixture','load_state':previous,'state_secret':None,'resolve_parent_page_id':'parent','blocks_for_node':[]}
        with contextlib.ExitStack() as stack:
            for name,value in patches.items():stack.enter_context(patch.object(m,name,return_value=value))
            stack.enter_context(patch.object(m,'list_child_pages',side_effect=lambda key,page:children.get(page,{})))
            create=stack.enter_context(patch.object(m,'create_page'));title=stack.enter_context(patch.object(m,'update_page_title'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            rc=m.main();create.assert_not_called();title.assert_not_called();return rc

    def test_legacy_noop_preserves_existing_management_metadata(self):
        metadata={'signature':m.ROOT_SIGNATURE,'page_id':'root','managed_block_baseline':[]}
        with patch.object(m,'save_state') as save:
            self.assertEqual(self.run_mirror({'entries':{'.':metadata}},{'parent':{'fixture':'root'}}),0)
        self.assertEqual(save.call_args.kwargs['entries']['.']['page_id'],'root')
        self.assertEqual(save.call_args.kwargs['entries']['.']['managed_block_baseline'],[])

    def test_new_page_held_before_create_without_durable_receipt(self):
        self.assertEqual(self.run_mirror({},{}),2)

    def test_changed_existing_page_held_before_title_or_content_write(self):
        previous={'entries':{'.':{'signature':m.ROOT_SIGNATURE},'file.md':{'signature':'old'}}}
        children={'parent':{'fixture':'root'},'root':{'file':'page'}}
        nodes=[m.Node('.',None,'fixture','root',m.ROOT_SIGNATURE),m.Node('file.md','.','file','file','new')]
        self.assertEqual(self.run_mirror(previous,children,nodes),2)

    def test_unresolved_unknown_remains_held_next_run(self):
        self.assertEqual(self.run_mirror({'write_outcome':{'status':'unknown_write','retry_allowed':False}},{}),2)


class DurableRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state.json'
        self.state.write_text(json.dumps({'entries': {'legacy': {'signature': 'retained'}}}))
        self.patch = patch.object(m, 'STATE_PATH', self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.remote = Path(self.tmp.name) / 'private-remote.json'

    def persist(self, state):
        m.write_json(self.state, state)
        self.remote.write_bytes(self.state.read_bytes())

    def create_intent(self):
        blocks = [m.paragraph_block('eddie-mirror-operation:fixture'), m.paragraph_block('source')]
        with patch.object(m, 'persist_private_state', side_effect=self.persist):
            intent = m.prepare_intent('fixture', 'create', 'parent', blocks)
        return intent, blocks

    def test_private_checkpoint_requires_commit_and_remote_readback(self):
        from types import SimpleNamespace
        root = Path(self.tmp.name)
        path = root / '.github/state/notion-wiki-mirror-state.json'
        path.parent.mkdir(parents=True)
        calls = [SimpleNamespace(returncode=0),
                 SimpleNamespace(returncode=0, stdout='abc\n'),
                 SimpleNamespace(returncode=0, stdout='abc\trefs/heads/main\n')]
        with patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'synthetic'}), patch.object(m, 'verify_private_checkout'), patch.object(m, 'ROOT', root), patch.object(m, 'STATE_PATH', path), patch.object(m.subprocess, 'run', side_effect=calls) as run, patch.object(m, '_run_checkpoint', return_value=0) as publish:
            m.persist_private_state({'entries': {'legacy': {'signature': 'retained'}}})
            command = publish.call_args.args[0]
            self.assertIn('commit_private_vault.py', ' '.join(command))
            self.assertEqual(command[-2:], ['--path', '.github/state/notion-wiki-mirror-state.json'])
            self.assertEqual(len(run.call_args_list), 3)
        calls[-1] = SimpleNamespace(returncode=0, stdout='different\trefs/heads/main\n')
        with patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN': 'synthetic'}), patch.object(m, 'verify_private_checkout'), patch.object(m, 'ROOT', root), patch.object(m, 'STATE_PATH', path), patch.object(m.subprocess, 'run', side_effect=calls), patch.object(m, '_run_checkpoint', return_value=0), self.assertRaises(m.MirrorHeld):
            m.persist_private_state({'entries': {}})

    def test_create_server_commit_response_loss_and_clean_runner_readback(self):
        intent, blocks = self.create_intent()
        # Server committed but runner lost the response and all local files.
        self.state.unlink()
        self.state.write_bytes(self.remote.read_bytes())
        restored = m.load_state()['write_intents']['fixture']
        with patch.object(m, 'retrieve_block_children', side_effect=[[{'id': 'created', 'type': 'child_page'}], blocks]), patch.object(m, 'notion_request') as write:
            result = m.recover_intent('fixture', restored)
            self.assertEqual(result, {'status': 'verified', 'target_id': 'created', 'retry_allowed': False})
            write.assert_not_called()
        with self.assertRaises(m.MirrorHeld):
            m.prepare_intent('fixture', 'create', 'parent', blocks)
        self.assertIn('legacy', m.load_state()['entries'])

    def test_checkpoint_rejects_public_wrong_repo_or_unreadable_visibility_before_write(self):
        from types import SimpleNamespace
        for origin, metadata, failure in [
            ('https://github.com/Eddiehhhhh/eddie-automation-flows.git', {}, None),
            ('https://github.com/Eddiehhhhh/eddie-llm-wiki.git', {'private': False, 'full_name': 'Eddiehhhhh/eddie-llm-wiki'}, None),
            ('https://github.com/Eddiehhhhh/eddie-llm-wiki.git', {'private': True, 'full_name': 'other/repo'}, None),
            ('https://github.com/Eddiehhhhh/eddie-llm-wiki.git', {}, TimeoutError('private sentinel')),
        ]:
            before = self.state.read_bytes()
            with self.subTest(origin=origin, metadata=metadata), patch.dict(os.environ, {'PRIVATE_VAULT_TOKEN':'synthetic'}), patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=origin)) as run, patch.object(m,'verify_private_checkout', side_effect=ValueError('Private checkout verification failed')), patch.object(m,'write_json') as write:
                with self.assertRaises(m.MirrorHeld):
                    m.persist_private_state({'marker':'PRIVATE_SENTINEL'})
                write.assert_not_called()
                self.assertLessEqual(run.call_count, 2)
                self.assertEqual(self.state.read_bytes(), before)

    def test_checkpoint_failure_never_allows_request(self):
        with patch.object(m, 'persist_private_state', side_effect=m.MirrorHeld('failed')), patch.object(m, 'notion_request') as write:
            with self.assertRaises(m.MirrorHeld):
                m.prepare_intent('fixture', 'create', 'parent', [m.paragraph_block('eddie-mirror-operation:fixture')])
            write.assert_not_called()

    def test_create_partial_or_duplicate_target_stays_held(self):
        intent, blocks = self.create_intent()
        for observations in [
            [[{'id': 'one', 'type': 'child_page'}], blocks[:1]],
            [[{'id': 'one', 'type': 'child_page'}, {'id': 'two', 'type': 'child_page'}], blocks, blocks],
            [[]],
        ]:
            with patch.object(m, 'retrieve_block_children', side_effect=observations):
                self.assertEqual(m.recover_intent('fixture', intent)['status'], 'held')

    def test_append_response_lost_recovers_without_reissue_and_protects_human_edits(self):
        baseline = [{'id': 'old', **m.paragraph_block('old content')}]
        state = m.load_state()
        state['entries']['managed'] = {'page_id': 'target', 'managed_block_baseline': [{'id': 'old', 'hash': m.block_identity(baseline[0])}]}
        self.persist(state)
        blocks = [m.paragraph_block('new content')]
        with patch.object(m, 'persist_private_state', side_effect=self.persist):
            intent = m.prepare_intent('append-fixture', 'append', 'parent', blocks, 'target', baseline)
        self.state.unlink()
        self.state.write_bytes(self.remote.read_bytes())
        restored = m.load_state()['write_intents']['append-fixture']
        with patch.object(m, 'retrieve_block_children', return_value=baseline + blocks), patch.object(m, 'notion_request') as write:
            self.assertEqual(m.recover_intent('fixture', restored)['status'], 'verified')
            write.assert_not_called()
        human = [{'id': 'old', **m.paragraph_block('human edited after baseline')}]
        for observed in [human + blocks, baseline, baseline + blocks + [m.paragraph_block('human new text')]]:
            with patch.object(m, 'retrieve_block_children', return_value=observed):
                self.assertEqual(m.recover_intent('fixture', restored)['status'], 'held')

    def test_old_target_without_management_identity_is_held(self):
        with patch.object(m, 'persist_private_state') as persist:
            with self.assertRaises(m.MirrorHeld):
                m.prepare_intent('append', 'append', 'parent', [m.paragraph_block('new')], 'legacy', [{'id': 'old', **m.paragraph_block('old')}])
            persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
