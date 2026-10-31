"""A precise Notion app-link response transformation is not arbitrary link loss."""
import copy
import unittest
from unittest import mock
import test_wiki_notion_executor as fixtures
m=fixtures.m
BASE='https://app.notion.com/p/'+'a'*32


def block(url=BASE+'?pvs=204',text='unchanged label'):
    value=m.paragraph_block(text)
    value['paragraph']['rich_text'][0]['text']['link']={'url':url}
    return value


def intent(expected):
    return {'expected':[m.block_identity(expected)],'context':{'request':{'children':[expected]}}}


class NotionPvsRecoveryTests(unittest.TestCase):
    def test_exact_directional_query_loss_keeps_old_hashes_and_saved_request(self):
        expected,actual=block(),block(BASE);record=intent(expected);before=copy.deepcopy(record)
        self.assertNotEqual(m.block_identity(expected),m.block_identity(actual))
        self.assertNotEqual(m._semantic_block_identity(expected),m._semantic_block_identity(actual))
        self.assertTrue(m.intent_expected_matches(record,[actual]))
        self.assertEqual(record,before)
        self.assertFalse(m.intent_expected_matches(intent(actual),[expected]))

    def test_other_link_text_style_or_fragment_changes_are_not_hidden(self):
        cases=[BASE+'?pvs=205',BASE+'?pvs=204&other=1',BASE+'?other=1&pvs=204',
               BASE+'?pvs=204&pvs=204',BASE+'?pvs=204#fragment',BASE+'#fragment',
               BASE.replace('app.notion.com','app.notion.com.invalid')+'?pvs=204',
               BASE.replace('app.notion.com','example.invalid')+'?pvs=204',
               BASE.replace('/p/','/different/')+'?pvs=204',
               BASE.replace('https:','http:')+'?pvs=204',
               BASE.replace('/p/','/p/not-an-id-')+'?pvs=204']
        for url in cases:
            with self.subTest(url=url):
                actual_url=url.split('?')[0] if '?' in url else BASE
                self.assertFalse(m.intent_expected_matches(intent(block(url)),[block(actual_url)]))
        for change in ['id','host','text','bold','color','fragment','extra_link_property']:
            observed=block(BASE);run=observed['paragraph']['rich_text'][0]
            if change=='id':run['text']['link']['url']=BASE[:-1]+'b'
            if change=='host':run['text']['link']['url']=BASE.replace('app.notion.com','notion.so')
            if change=='text':run['text']['content']='different label'
            if change=='bold':run['annotations']={'bold':True}
            if change=='color':observed['paragraph']['color']='red'
            if change=='fragment':run['text']['link']['url']+='#fragment'
            if change=='extra_link_property':run['text']['link']['unexpected']=True
            self.assertFalse(m.intent_expected_matches(intent(block()),[observed]),change)

    def test_context_missing_or_tampered_cannot_authorize_normalization(self):
        record=intent(block());record['context']['request']['children'][0]=block(BASE)
        self.assertFalse(m.intent_expected_matches(record,[block(BASE)]))
        for context in [{},{'request':{}},{'request':{'children':[]}}]:
            record=intent(block());record['context']=context
            self.assertFalse(m.intent_expected_matches(record,[block(BASE)]))

    def test_unicode_loss_is_still_a_real_content_difference(self):
        expected=block(text='before\u200b\u200bafter');actual=block(BASE,text='beforeafter')
        self.assertFalse(m.intent_expected_matches(intent(expected),[actual]))

    def recover(self,create):
        fx=fixtures.GuardedExecutorTests('test_append_checkpoints_before_single_write_and_noops_repeat')
        fx.setUp();self.addCleanup(fx.doCleanups)
        if not create:fx.managed()
        old_render=m.file_page_blocks
        def rendered(*args,**kwargs):return old_render(*args,**kwargs)+[block()]
        old_send=fx.send
        def normalized_server(request,timeout):
            response=old_send(request,timeout)
            for b in fx.blocks[fixtures.TARGET]:
                for run in b.get(b['type'],{}).get('rich_text',[]):
                    link=run.get('text',{}).get('link')
                    if link and link.get('url')==BASE+'?pvs=204':link['url']=BASE
            return response
        def old_compare(record,actual):
            return [m._semantic_block_identity(b)for b in record['context']['request']['children']]==[m._semantic_block_identity(b)for b in actual]
        with mock.patch.object(m,'file_page_blocks',side_effect=rendered),mock.patch.object(m.urllib.request,'urlopen',side_effect=normalized_server):
            with mock.patch.object(m,'intent_expected_matches',side_effect=old_compare):
                with self.assertRaises(m.MirrorHeld):m.execute_revision('synthetic','Wiki/note.md',fixtures.PARENT,create)
            record=next(iter(m.load_state()['write_intents'].values()));self.assertEqual(record['status'],'dispatched');saved=copy.deepcopy(record)
            fx.state.write_bytes(fx.remote.read_bytes())
            with mock.patch.object(m,'_send_once',side_effect=AssertionError('GET recovery may not resend')):
                self.assertEqual(m.recover_intents('synthetic',persist=True),{'verified':1,'held':0})
            self.assertEqual(len(fx.requests),1)
            restored=next(iter(m.load_state()['write_intents'].values()))
            self.assertEqual(restored['expected'],saved['expected']);self.assertEqual(restored['context']['request'],saved['context']['request'])
            self.assertEqual(m.load_state()['entries']['Wiki/note.md']['managed_block_baseline'],m._baseline(m.retrieve_block_tree('synthetic',fixtures.TARGET)))
            self.assertEqual(m.execute_revision('synthetic','Wiki/note.md',fixtures.PARENT,create)['skipped'],1)
            self.assertEqual(len(fx.requests),1)

    def test_create_recovers_by_reads_only_then_noops(self):self.recover(True)
    def test_append_recovers_by_reads_only_then_noops(self):self.recover(False)
