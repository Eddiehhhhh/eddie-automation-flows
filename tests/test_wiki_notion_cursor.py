"""A bounded pass must continue past an unchanged prefix on a clean runner."""
import copy
import subprocess
import unittest
from unittest import mock
import test_wiki_notion_bulk as fixtures
m, REQUEST, OUTER = fixtures.m, fixtures.REQUEST, fixtures.OUTER


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.BulkTests(methodName='test_full_hierarchy_parent_first_original_root_retained_and_noop')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def update_request(self, **values):
        state=m.load_state();state['bulk_request'].update(values);self.fx.persist(state)

    def committed(self,path,text):
        (self.fx.root/path).write_text(text)
        for argv in [('add',path),('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic source change')]:
            subprocess.run(['git','-C',str(self.fx.root),*argv],check=True,capture_output=True)

    def fully_mirrored(self):
        self.fx.legacy_root();self.assertEqual(m.execute_bulk('synthetic',REQUEST)['deferred'],0)
        self.update_request(max_seconds=1,follow_source_changes=True)
        self.selected=[];self.clock=[0.0]
        original_children=m.retrieve_block_children;original_page=m.get_page
        def children(*args):
            self.clock[0]+=.3;return original_children(*args)
        def page(*args):
            self.clock[0]+=.3;return original_page(*args)
        adopt=m._adopt_bulk_target
        def adopted(key,node,*args):
            self.selected.append(node.path);return adopt(key,node,*args)
        for patch in [mock.patch.object(m.time,'monotonic',side_effect=lambda:self.clock[0]),
                      mock.patch.object(m,'retrieve_block_children',side_effect=children),
                      mock.patch.object(m,'get_page',side_effect=page),
                      mock.patch.object(m,'_adopt_bulk_target',side_effect=adopted)]:
            patch.start();self.addCleanup(patch.stop)

    def fresh_pass(self):
        self.fx.state_path.write_bytes(self.fx.remote.read_bytes())
        return m.execute_bulk('synthetic',REQUEST)

    def test_wall_time_prefix_does_not_starve_tail_and_cycle_then_restarts(self):
        self.fully_mirrored();sends=len(self.fx.sends)
        for _ in range(8):
            result=self.fresh_pass()
            if m.load_state()['bulk_progress']['cycle_complete']:break
        self.assertEqual(self.selected,['.','Schema','Wiki','Schema/b.md','Wiki/a.md'])
        self.assertEqual((result['verified'],result['held'],result['deferred']),(5,0,0))
        self.assertEqual(len(self.fx.sends),sends)
        self.fresh_pass()
        self.assertEqual(self.selected[-1],'.')
        self.assertGreater(m.load_state()['bulk_progress']['counts']['deferred'],0)

    def test_changed_and_added_prefix_do_not_reset_cursor_or_claim_false_complete(self):
        self.fully_mirrored()
        for _ in range(4):self.fresh_pass()
        self.assertEqual(self.selected[-1],'Schema/b.md')
        self.committed('Schema/b.md','a newly committed source revision\n')
        self.committed('README.md','a source added before the saved cursor\n')
        result=self.fresh_pass()
        self.assertEqual(self.selected[-1],'Wiki/a.md')
        self.assertEqual(result['deferred'],2)
        self.assertTrue(m.load_state()['bulk_progress']['cycle_complete'])
        self.update_request(max_seconds=900)
        result=self.fresh_pass()
        self.assertEqual((result['verified'],result['held'],result['deferred']),(6,0,0))
        self.assertEqual(m.load_state()['entries']['Schema/b.md']['signature'],m.sha256_text('a newly committed source revision\n'))
        self.assertIn('README.md',m.load_state()['entries'])

    def test_unknown_before_cursor_remains_held_while_tail_advances(self):
        self.fully_mirrored()
        for _ in range(4):self.fresh_pass()
        state=m.load_state();target=state['entries']['Schema/b.md']['page_id']
        state['write_intents']['synthetic-unknown']={'kind':'append','status':'unknown_write','target_id':target,
                                                  'context':{'entry_key':'Schema/b.md'},'retry_allowed':False}
        self.fx.persist(state);sends=len(self.fx.sends)
        result=self.fresh_pass()
        self.assertEqual(self.selected[-1],'Wiki/a.md')
        self.assertEqual((result['held'],result['verified']),(1,4))
        self.assertEqual(len(self.fx.sends),sends)
        self.assertEqual(m.load_state()['write_intents']['synthetic-unknown']['status'],'unknown_write')

    def test_resumed_child_rechecks_moved_ancestor_and_never_replaces_it(self):
        self.fx.legacy_root();self.update_request(max_batches=1)
        self.fresh_pass();schema=m.load_state()['entries']['Schema']['page_id']
        self.fx.pages[schema]['parent']['page_id']=OUTER
        self.fresh_pass();before=len(self.fx.sends)
        result=self.fresh_pass()
        self.assertEqual(result['held'],2)
        self.assertEqual(m.load_state()['entries']['Schema']['page_id'],schema)
        self.assertNotIn('Schema/b.md',m.load_state()['entries'])
        self.assertIn('Wiki/a.md',m.load_state()['entries'])
        self.assertEqual(len(self.fx.sends),before+1)

    def test_partial_source_stays_at_cursor_and_fresh_runner_skips_verified_batches(self):
        self.committed('Schema/b.md','\n\n'.join('paragraph '+str(index) for index in range(250)))
        self.fx.legacy_root();self.update_request(max_batches=1,follow_source_changes=True)
        starts=[]
        for _ in range(10):
            result=self.fresh_pass();progress=m.load_state()['bulk_progress'];starts.append(copy.deepcopy(progress['next_order']))
            if progress['cycle_complete']:break
        self.assertEqual((result['held'],result['deferred'],result['verified']),(0,0,5))
        self.assertGreater(starts.count([2,1,'Schema/b.md']),1)
        source=m.load_state()['entries']['Schema/b.md']
        blocks=self.fx.children[source['page_id']]
        markers=[m.block_identity(block)for block in blocks if block['type']=='paragraph'and block['paragraph']['rich_text'][0]['text']['content'].startswith('eddie-mirror-operation:')]
        self.assertEqual(len(markers),3)
        self.assertEqual(len(markers),len(set(markers)))
        self.assertEqual(len(self.fx.sends),6)


if __name__=='__main__':unittest.main()
