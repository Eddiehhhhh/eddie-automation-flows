"""Notion coalesces code text runs; recover the published request without resending."""
import copy
import subprocess
import unittest
from unittest import mock
import test_wiki_notion_executor as fixtures
import test_wiki_notion_bulk as bulk_fixtures
m=fixtures.m


class IntentRichTextTests(unittest.TestCase):
    def fixture(self, create=False):
        fx=fixtures.GuardedExecutorTests(methodName='test_append_checkpoints_before_single_write_and_noops_repeat')
        fx.setUp();self.addCleanup(fx.doCleanups)
        if not create:fx.managed()
        fx.source.write_text('```python\n'+'synthetic_line = 1\n'*400+'```\n')
        for args in [('add','Wiki/note.md'),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic long code')]:
            subprocess.run(['git','-C',str(fx.root),*args],check=True,capture_output=True)
        old_send=fx.send
        def merged_server(request,timeout):
            result=old_send(request,timeout)
            for block in fx.blocks[fixtures.TARGET]:
                if block['type']=='code':
                    runs=block['code']['rich_text']
                    block['code']['rich_text']=m.text_rich_text('placeholder')
                    block['code']['rich_text'][0]['text']['content']=''.join(run['text']['content']for run in runs)
                    block['code']['rich_text'][0]['annotations']={'bold':False,'italic':False,'strikethrough':False,'underline':False,'code':False,'color':'default'}
            return result
        patch=mock.patch.object(m.urllib.request,'urlopen',side_effect=merged_server);patch.start();self.addCleanup(patch.stop)
        # Reproduce the exact pre-fix comparison, including the dispatched state.
        with mock.patch.object(m,'intent_expected_matches',side_effect=lambda intent,actual:[m.block_identity(b)for b in actual]==intent.get('expected')):
            with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',fixtures.PARENT,create)
        intent=next(iter(m.load_state()['write_intents'].values()))
        self.assertEqual(intent['status'],'dispatched')
        actual=m.retrieve_block_tree('synthetic',fixtures.TARGET)
        tail=actual[len(intent['baseline']):]
        self.assertNotEqual([m.block_identity(b)for b in tail],intent['expected'])
        self.assertTrue(m.intent_expected_matches(intent,tail))
        return fx,intent

    def recovered(self, create):
        fx,intent=self.fixture(create)
        sends=len(fx.requests)
        fx.state.write_bytes(fx.remote.read_bytes())
        with mock.patch.object(m,'_send_once',side_effect=AssertionError('recovery must never resend')):
            self.assertEqual(m.recover_intents('synthetic'),{'verified':1,'held':0})
            self.assertEqual(m.recover_intents('synthetic',persist=True),{'verified':1,'held':0})
        self.assertEqual(len(fx.requests),sends)
        entry=m.load_state()['entries']['Wiki/note.md']
        self.assertEqual(entry['managed_block_baseline'],m._baseline(m.retrieve_block_tree('synthetic',fixtures.TARGET)))
        self.assertEqual(m.execute_revision('synthetic','Wiki/note.md',fixtures.PARENT,create)['skipped'],1)
        self.assertEqual(len(fx.requests),sends)
        self.assertEqual(len(fx.blocks[fixtures.PARENT]),1)

    def test_dispatched_create_get_only_recovery_and_noop(self):self.recovered(True)
    def test_dispatched_append_get_only_recovery_and_noop(self):self.recovered(False)

    def test_prefix_human_change_still_holds_even_when_new_tail_is_equivalent(self):
        fx,intent=self.fixture()
        fx.blocks[fixtures.TARGET][0]['paragraph']['rich_text']=m.text_rich_text('a real human content change')
        with mock.patch.object(m,'_send_once',side_effect=AssertionError('no resend')):
            self.assertEqual(m.recover_intents('synthetic'),{'verified':0,'held':1})
        self.assertEqual(m.load_state()['entries']['Wiki/note.md']['signature'],'previous')
        self.assertEqual(len(fx.requests),1)

    def test_same_attributes_coalesce_but_links_styles_and_other_properties_do_not(self):
        expected=m.paragraph_block('');expected['paragraph']['rich_text']=m.text_rich_text('alpha')+m.text_rich_text('beta')
        actual=m.paragraph_block('alphabeta')
        intent={'expected':[m.block_identity(expected)],'context':{'request':{'children':[expected]}}}
        before=copy.deepcopy(intent)
        self.assertTrue(m.intent_expected_matches(intent,[actual]))
        self.assertNotEqual(m.block_identity(expected),m.block_identity(actual))  # baseline format remains strict
        self.assertEqual(intent,before)
        for change in ['content','link','annotation','other']:
            observed=copy.deepcopy(actual)
            run=observed['paragraph']['rich_text'][0]
            if change=='content':run['text']['content']='not the original content'
            if change=='link':run['text']['link']={'url':'https://example.invalid/changed'}
            if change=='annotation':run['annotations']={'bold':True}
            if change=='other':observed['paragraph']['color']='red'
            self.assertFalse(m.intent_expected_matches(intent,[observed]),change)
        linked=copy.deepcopy(expected)
        for run in linked['paragraph']['rich_text']:run['text']['link']={'url':'https://example.invalid/original'}
        linked_actual=copy.deepcopy(actual);linked_actual['paragraph']['rich_text'][0]['text']['link']={'url':'https://example.invalid/original'}
        linked_intent={'expected':[m.block_identity(linked)],'context':{'request':{'children':[linked]}}}
        self.assertTrue(m.intent_expected_matches(linked_intent,[linked_actual]))
        linked_actual['paragraph']['rich_text'][0]['text']['link']['url']='https://example.invalid/other'
        self.assertFalse(m.intent_expected_matches(linked_intent,[linked_actual]))

    def test_unknown_send_consumes_one_attempt_and_never_overruns_budget(self):
        fx=bulk_fixtures.BulkTests(methodName='test_full_hierarchy_parent_first_original_root_retained_and_noop')
        fx.setUp();self.addCleanup(fx.doCleanups)
        module=bulk_fixtures.m
        fx.legacy_root();state=module.load_state();state['bulk_request']['max_batches']=1;fx.persist(state)
        fx.lose=1
        counts=module.execute_bulk('synthetic',bulk_fixtures.REQUEST)
        self.assertEqual(counts['attempted_batches'],1)
        self.assertEqual(counts['verified_batches'],0)
        self.assertEqual(len(fx.sends),1)
        self.assertEqual(sum(i['status']=='unknown_write'for i in module.load_state()['write_intents'].values()),1)
        self.assertGreater(counts['deferred'],0)
        self.assertNotIn('Wiki',module.load_state()['entries'])
        # A later read-only recovery and a new pass may reuse verified history, but no extra request in this pass.
        self.assertEqual(module.recover_intents('synthetic',persist=True)['verified'],1)
        self.assertEqual(len(fx.sends),1)
        self.assertEqual(module.execute_bulk('synthetic',bulk_fixtures.REQUEST)['attempted_batches'],1)
        self.assertEqual(len(fx.sends),2)

    def test_missing_or_tampered_original_request_never_weakens_old_expected_hash(self):
        payload=m.code_block('');payload['code']['rich_text']=m.text_rich_text('first')+m.text_rich_text('second')
        actual=m.code_block('firstsecond')
        intent={'expected':[m.block_identity(payload)],'context':{'request':{'children':[payload]}}}
        self.assertTrue(m.intent_expected_matches(intent,[actual]))
        missing=copy.deepcopy(intent);missing.pop('context')
        self.assertFalse(m.intent_expected_matches(missing,[actual]))
        changed=copy.deepcopy(intent);changed['context']['request']['children'][0]['code']['rich_text'][0]['text']['content']='tampered'
        self.assertFalse(m.intent_expected_matches(changed,[actual]))


if __name__=='__main__':unittest.main()
