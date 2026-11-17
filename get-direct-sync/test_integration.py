"""Offline full pipeline contract, not live service acceptance."""
import unittest
from core import Store,GitHubSink,snapshot
from notion_sink import NotionSink
from test_core import GitFake,note
from test_notion_sink import Fake
class Integration(unittest.TestCase):
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
