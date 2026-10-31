"""End-to-end guarded executor tests with synthetic private Git and Notion seams."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('notion_executor', Path(__file__).resolve().parents[1] / 'scripts/sync_wiki_to_notion.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
PARENT = '11111111-1111-1111-1111-111111111111'
TARGET = '22222222-2222-2222-2222-222222222222'


class GuardedExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'vault'
        self.root.mkdir()
        self.source = self.root / 'Wiki/note.md'
        self.source.parent.mkdir()
        self.source.write_text('# Synthetic source\n\nFirst body\n')
        for args in [('init',), ('add', 'Wiki/note.md'), ('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic')]:
            subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
        self.state = self.root / '.github/state/notion-wiki-mirror-state.json'
        m.write_json(self.state, {'entries': {}})
        self.remote = Path(self.tmp.name) / 'remote-state.json'
        self.blocks = {PARENT: [], TARGET: []}
        self.events = []
        self.requests = []
        self.lose_response = False
        self.human_during_dispatch = False
        patches = [mock.patch.object(m,'require_published_state'), mock.patch.object(m,'ROOT',self.root), mock.patch.object(m,'STATE_PATH',self.state),
                   mock.patch.object(m,'state_secret',return_value=None),
                   mock.patch.dict(os.environ, {'CODEX_THREAD_ID':'synthetic-owner'}),
                   mock.patch.object(m,'persist_private_state',side_effect=self.persist),
                   mock.patch.object(m,'retrieve_block_children',side_effect=lambda key,target:copy.deepcopy(self.blocks[target])),
                   mock.patch.object(m.urllib.request,'urlopen',side_effect=self.send)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def persist(self, state):
        m.write_json(self.state,state)
        self.remote.write_bytes(self.state.read_bytes())
        statuses=[x['status']for x in state.get('write_intents',{}).values()]
        pending=[x for x in statuses if x!='verified']
        status=pending[0] if pending else ('verified' if statuses else 'none')
        self.events.append(status)
        if status=='dispatched' and self.human_during_dispatch:
            self.blocks[TARGET][0]['paragraph']['rich_text']=m.text_rich_text('new human text after checkpoint')

    def send(self, request, timeout):
        self.assertEqual(timeout,60)
        durable=json.loads(self.remote.read_text())
        self.assertTrue(any(x['status']=='dispatched'for x in durable['write_intents'].values()))
        self.requests.append(request.method)
        payload=json.loads(request.data)
        if request.method=='POST':
            self.assertEqual(payload['properties']['title']['title'],m.text_rich_text('note'))
            self.blocks[PARENT].append({'id':TARGET,'type':'child_page','child_page':{'title':'note'}})
            destination=TARGET
        else:
            destination=request.full_url.split('/blocks/')[1].split('/children')[0]
        for block in payload['children']:
            self.blocks[destination].append({'id':f'block-{len(self.blocks[destination])}',**copy.deepcopy(block)})
        if self.lose_response:
            self.lose_response=False
            raise TimeoutError('SYNTHETIC_RESPONSE_LOSS')
        return io.BytesIO(b'{"object":"list","results":[]}')

    def managed(self):
        self.blocks[TARGET]=[{'id':'original',**m.paragraph_block('human content retained')}]
        self.blocks[PARENT]=[{'id':TARGET,'type':'child_page','child_page':{'title':'note'}}]
        state={'entries':{'Wiki/note.md':{'signature':'previous','page_id':TARGET,'parent_page_id':PARENT,
                                       'managed_block_baseline':m._baseline(self.blocks[TARGET])}}}
        m.write_json(self.state,state)

    def test_append_checkpoints_before_single_write_and_noops_repeat(self):
        self.managed()
        before=copy.deepcopy(self.blocks[TARGET][0])
        result=m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.assertEqual(result,{'verified_batches':1,'skipped':0})
        self.assertEqual(self.events,['prepared','dispatched','verified'])
        self.assertEqual(self.requests,['PATCH'])
        self.assertEqual(self.blocks[TARGET][0],before)
        self.assertEqual(m.execute_revision('synthetic','Wiki/note.md',PARENT)['skipped'],1)
        self.assertEqual(self.requests,['PATCH'])
        self.assertEqual(m.recover_intents('synthetic',persist=True),{'verified':0,'held':0,'already_verified':1})
        self.assertEqual(self.events,['prepared','dispatched','verified'])

    def test_create_lost_response_clean_runner_recovers_no_duplicate(self):
        self.lose_response=True
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT,True)
        self.assertEqual(self.requests,['POST'])
        self.state.unlink()
        self.state.parent.mkdir(parents=True,exist_ok=True)
        self.state.write_bytes(self.remote.read_bytes())
        self.assertEqual(m.recover_intents('synthetic'),{'verified':1,'held':0})
        self.assertEqual(self.events,['prepared','dispatched','unknown_write'])
        self.assertEqual(m.recover_intents('synthetic',persist=True),{'verified':1,'held':0})
        self.assertEqual(m.execute_revision('synthetic','Wiki/note.md',PARENT,True)['skipped'],1)
        self.assertEqual(len(self.blocks[PARENT]),1)
        self.assertEqual(self.requests,['POST'])

    def test_append_lost_response_recovery_and_restart_never_resend(self):
        self.managed();self.lose_response=True
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.assertEqual(m.recover_intents('synthetic',persist=True)['verified'],1)
        self.assertEqual(self.requests,['PATCH'])

    def test_human_edit_between_checkpoint_and_write_is_preserved_held(self):
        self.managed();self.human_during_dispatch=True
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.assertEqual(self.blocks[TARGET][0]['paragraph']['rich_text'],m.text_rich_text('new human text after checkpoint'))
        self.assertEqual(m.recover_intents('synthetic',persist=True)['held'],1)
        self.assertEqual(self.requests,['PATCH'])
        self.assertEqual(m.load_state()['entries']['Wiki/note.md']['signature'],'previous')

    def test_legacy_entry_cannot_be_duplicated_even_when_create_allowed(self):
        m.write_json(self.state,{'entries':{'Wiki/note.md':{'signature':'old'}}})
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT,True)
        self.assertEqual(self.requests,[])
        self.assertEqual(self.events,[])

    def test_reviewed_signature_mismatch_blocks_before_target_write(self):
        self.managed()
        with self.assertRaises(m.MirrorHeld):
            m.execute_revision('synthetic','Wiki/note.md',PARENT,expected_signature='0'*64)
        self.assertEqual(self.requests,[])
        self.assertEqual(self.events,[])

    def test_checkpoint_failure_has_zero_target_requests(self):
        self.managed()
        with mock.patch.object(m,'persist_private_state',side_effect=m.MirrorHeld('unpublished')):
            with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.assertEqual(self.requests,[])

    def test_render_uses_frozen_committed_source_during_local_race(self):
        self.managed()
        original=self.source.read_text()
        renderer=m.file_page_blocks
        def changed_source(root,relative,markdown=None):
            self.source.write_text('SYNTHETIC_UNCOMMITTED_RACE')
            return renderer(root,relative,markdown=markdown)
        with mock.patch.object(m,'file_page_blocks',side_effect=changed_source):
            m.execute_revision('synthetic','Wiki/note.md',PARENT)
        rendered=json.dumps(self.blocks[TARGET])
        self.assertIn('First body',rendered)
        self.assertNotIn('SYNTHETIC_UNCOMMITTED_RACE',rendered)
        self.assertEqual(m.load_state()['entries']['Wiki/note.md']['signature'],m.sha256_text(original))

    def test_unchanged_source_does_not_hide_new_human_edits(self):
        self.managed()
        m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.blocks[TARGET][0]['paragraph']['rich_text']=m.text_rich_text('later human edit')
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.assertEqual(self.requests,['PATCH'])

    def test_unpublished_verified_state_cannot_be_reused_as_success(self):
        self.managed()
        with mock.patch.object(m,'require_published_state',side_effect=m.MirrorHeld('private publication missing')):
            with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.assertEqual(self.requests,[])

    def test_large_revision_verifies_each_batch_and_never_duplicates(self):
        self.managed()
        blocks=[m.paragraph_block('synthetic '+str(i))for i in range(220)]
        with mock.patch.object(m,'file_page_blocks',return_value=blocks):
            self.assertEqual(m.execute_revision('synthetic','Wiki/note.md',PARENT)['verified_batches'],3)
        self.assertEqual(self.requests,['PATCH']*3)
        self.assertEqual(self.events,['prepared','dispatched','verified']*3)
        self.assertEqual(len(self.blocks[TARGET]),230)

    def test_partial_server_append_is_not_verified_or_retried(self):
        self.managed();self.lose_response=True
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.blocks[TARGET].pop()
        self.assertEqual(m.recover_intents('synthetic',persist=True)['held'],1)
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.assertEqual(self.requests,['PATCH'])

    def test_new_nested_human_content_is_held_without_overwrite(self):
        self.managed()
        self.blocks[TARGET][0]['has_children']=True
        with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',PARENT)
        self.assertEqual(self.requests,[])
        self.assertTrue(self.blocks[TARGET][0]['has_children'])

    def test_incomplete_or_repeated_pagination_fails_closed(self):
        for responses in [
            [{'results':[],'has_more':True,'next_cursor':None}],
            [{'results':[],'has_more':True,'next_cursor':'same'},{'results':[],'has_more':True,'next_cursor':'same'}],
            [{'results':[]}], [{'results':'bad','has_more':False}],
        ]:
            with self.subTest(responses=responses),mock.patch.object(m,'notion_request',side_effect=responses):
                with self.assertRaises(m.MirrorHeld):
                    self._real_children('synthetic',PARENT)

    def _real_children(self,*args):
        # setUp patches m.retrieve_block_children; its wrapped source is saved below.
        return REAL_CHILDREN(*args)


class ReadOnlyCliTests(unittest.TestCase):
    def test_readonly_flags_cannot_be_combined_with_mutating_recovery_or_execution(self):
        import contextlib
        for flags in [
            ['--recover-intents','--persist-recovery','--dry-run'],
            ['--recover-intents','--persist-recovery','--plan-only'],
            ['--execute-revision','Wiki/note.md','--dry-run'],
            ['--execute-revision','Wiki/note.md','--recover-intents'],
            ['--execute-request','33333333-3333-4333-8333-333333333333','--dry-run'],
        ]:
            with self.subTest(flags=flags),mock.patch.object(m.sys,'argv',['script',*flags]),mock.patch.object(m,'load_notion_api_key') as key,mock.patch.object(m,'persist_private_state') as persist,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(),2)
                key.assert_not_called();persist.assert_not_called()

    def test_opaque_request_binds_private_source_without_echoing_it(self):
        import contextlib
        request_id='33333333-3333-4333-8333-333333333333'
        request={'request_id':request_id,'source_path':'Wiki/PRIVATE_SENTINEL.md','source_signature':'a'*64,'parent_page_id':PARENT,'allow_new_page':False}
        output=io.StringIO()
        with mock.patch.object(m.sys,'argv',['script','--execute-request',request_id]),mock.patch.object(m,'load_state',return_value={'execution_request':request}),mock.patch.object(m,'load_notion_api_key',return_value='synthetic'),mock.patch.object(m,'execute_revision',return_value={'verified_batches':1,'skipped':0}) as execute,contextlib.redirect_stdout(output):
            self.assertEqual(m.main(),0)
        execute.assert_called_once_with('synthetic','Wiki/PRIVATE_SENTINEL.md',PARENT,False,expected_signature='a'*64)
        self.assertNotIn('PRIVATE_SENTINEL',output.getvalue())

    def test_workflow_exposes_private_token_and_keeps_readonly_recovery_out_of_commit_step(self):
        workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/sync-wiki-to-notion.yml').read_text()
        self.assertIn('PRIVATE_VAULT_TOKEN: ${{ secrets.PRIVATE_VAULT_TOKEN }}',workflow)
        self.assertIn('inputs.recover_intents == false',workflow)
        self.assertIn('REVISION_REQUEST: ${{ inputs.revision_request }}',workflow)
        self.assertNotIn('revision_source:',workflow)
        self.assertIn('args+=(--execute-request "$REVISION_REQUEST")',workflow)
        self.assertNotIn('--execute-revision "${{',workflow)


class PublishedStateTests(unittest.TestCase):
    def test_publication_requires_committed_identical_state_and_matching_remote(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'state.json';path.write_text('{"synthetic":true}')
            for committed,remote,success in [(path.read_bytes(),'abc',True),(b'old-state','abc',False),(path.read_bytes(),'other',False)]:
                responses=[SimpleNamespace(returncode=0,stdout=committed),SimpleNamespace(returncode=0,stdout='abc\n'),SimpleNamespace(returncode=0,stdout=remote+'\trefs/heads/main\n')]
                with self.subTest(remote=remote,success=success),mock.patch.object(m,'ROOT',root),mock.patch.object(m,'STATE_PATH',path),mock.patch.object(m,'verify_private_checkout'),mock.patch.object(m.subprocess,'run',side_effect=responses):
                    if success:m.require_published_state()
                    else:
                        with self.assertRaises(m.MirrorHeld):m.require_published_state()


REAL_CHILDREN=m.retrieve_block_children
if __name__=='__main__':unittest.main()
