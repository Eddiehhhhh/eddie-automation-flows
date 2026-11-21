"""Offline full pipeline contract, not live service acceptance."""
import unittest
from core import Store,GitHubSink,snapshot
from notion_sink import NotionSink
from test_core import GitFake,note
from test_notion_sink import Fake
class Integration(unittest.TestCase):
 def test_unowned_notion_page_is_preserved_only_when_requested(self):
  state = Store(':memory:')
  self.addCleanup(state.db.close)
  github = GitFake()
  notion = Fake()
  notion.pages['old'] = {'id': 'old', 'properties': {
      'Get ID': {'rich_text': [{'plain_text': '1'}]}, 'Sync Owner': {'rich_text': []}}}
  sinks = {'github': GitHubSink(github, 'owner/private', 'main', 'test'),
           'notion': NotionSink(notion, 'db', state, namespace='test')}
  revision = snapshot(note())
  state.accept([])
  state.accept([revision])

  default = state.drain(sinks)
  self.assertEqual(default['notion']['held'], 1)
  self.assertEqual(state.pending_count(), 1)
  self.assertIsNone(state.get('existing_page_conflict:1'))
  github_receipt = state.receipt('github', '1')
  self.assertEqual(github_receipt['version'], revision['version'])

  result = state.drain(sinks, preserve_existing=True)
  self.assertEqual(result['preserved_existing'], 1)
  self.assertEqual(state.pending_count(), 0)
  self.assertEqual(state.get('sync_policy')['paused_ids'], ['1'])
  self.assertEqual(state.get('existing_page_conflict:1'),
                   {'reason': 'unknown_existing_page_ownership', 'revisions': [revision]})
  self.assertEqual(state.receipt('github', '1'), github_receipt)
  self.assertIsNone(state.receipt('notion', '1'))
  self.assertIsNone(state.db.execute('SELECT 1 FROM delivered WHERE id=?', ('1',)).fetchone())
  self.assertTrue(notion.calls)
  self.assertTrue(all(method == 'GET' or (method == 'POST' and path == '/v1/databases/db/query')
                      for method, path, _ in notion.calls))

 def test_duplicate_notion_remains_pending_with_durable_conflict(self):
  state=Store(':memory:');gh=GitFake();notion=Fake();notion.pages={'p1':{'id':'p1'},'p2':{'id':'p2'}}
  sinks={'github':GitHubSink(gh,'owner/private','main','test'),'notion':NotionSink(notion,'db',state,namespace='test')}
  state.accept([]);state.accept([snapshot(note())]);result=state.drain(sinks)
  self.assertEqual(result['notion']['held'],1);self.assertIsNone(state.receipt('notion','1'))
  self.assertEqual(state.db.execute('select count(*) from pending').fetchone()[0],1)
  self.assertEqual(state.get('conflict:notion:db:test:1')['reason'],'duplicate_source_pages')
  self.assertFalse(any(m=='PATCH' or p=='/v1/pages' for m,p,b in notion.calls))
 def test_full_dual_delivery_update_repeat(self):
  state=Store(':memory:');gh=GitFake();notion=Fake()
  sinks={'github':GitHubSink(gh,'owner/private','main','test'), 'notion':NotionSink(notion,'db',state,namespace='test')}
  state.accept([]);state.accept([snapshot(note())]);self.assertEqual(state.drain(sinks)['notion']['verified'],1)
  state.accept([snapshot(note(text='edited'))]);self.assertEqual(state.drain(sinks)['notion']['verified'],1)
  self.assertEqual(len(notion.pages),1);self.assertEqual(gh.writes,2)
  self.assertEqual(state.receipt('notion','1')['version'],state.receipt('github','1')['version'])
  state.accept([snapshot(note(text='edited'))]);self.assertEqual(state.drain(sinks)['github']['verified'],0)
if __name__=='__main__':unittest.main()
