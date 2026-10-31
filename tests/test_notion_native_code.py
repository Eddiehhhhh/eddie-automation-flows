"""Native code chunks preserve literal whitespace; historical adoption stays stable."""
import copy
import unittest
from unittest import mock
import subprocess
import test_wiki_notion_bulk as fixtures
m=fixtures.m


def code_text(blocks):
    return ''.join(run['text']['content']for block in blocks if block['type']=='code'for run in block['code']['rich_text'])


class NativeCodeTests(unittest.TestCase):
    def test_long_python_indentation_blank_lines_and_boundary_whitespace_exact(self):
        payload='\n\n    def synthetic():\n        if ready:\n            act()\n\n'+('    # boundary comment with spaces   \n'*120)+'\tfinish()  \n\n'
        source='```python\n'+payload+'\n```'
        legacy=m.markdown_to_blocks(source)
        native=m.native_markdown_to_blocks(source)
        self.assertNotEqual(code_text(legacy),payload)  # reproduces trimming in the historical renderer
        self.assertEqual(code_text(native),payload)
        self.assertEqual(code_text(native).encode('utf-8'),payload.encode('utf-8'))
        self.assertEqual(next(b['code']['language']for b in native if b['type']=='code'),'python')
        self.assertEqual(m.markdown_to_blocks(source),legacy)

    def test_unicode_chunk_boundaries_preserve_all_codepoints_and_utf16_budget(self):
        payload='  '+('中文🙂🧑‍💻\t\n'*800)+'  \n'
        runs=m.exact_text_rich_text(payload)
        self.assertEqual(''.join(r['text']['content']for r in runs),payload)
        self.assertTrue(all(len(r['text']['content'].encode('utf-16-le'))//2<=1800 for r in runs))
        self.assertGreater(len(runs),1)

    def test_short_code_remains_identical_and_frontmatter_long_values_are_exact(self):
        short='```python\n    print("synthetic")  \n\n```'
        self.assertEqual(m.native_markdown_to_blocks(short),m.markdown_to_blocks(short))
        yaml='key: synthetic\n'+('  nested: value with spaces   \n'*100)
        source='---\n'+yaml+'---\nbody\n'
        native=m.native_markdown_to_blocks(source)
        self.assertEqual(code_text(native),yaml.strip('\n'))

    def test_renderer_upgrade_invalidates_old_cycle_once_without_rewriting_plain_sources(self):
        fx=fixtures.BulkTests(methodName='test_full_hierarchy_parent_first_original_root_retained_and_noop')
        fx.setUp();self.addCleanup(fx.doCleanups)
        fx.legacy_root();m.execute_bulk('synthetic',fixtures.REQUEST)
        state=m.load_state();state['bulk_progress'].update(cycle_complete=False,next_order=[2,1,'Wiki/a.md'],renderer_revision='old-native-renderer')
        fx.persist(state);sends=len(fx.sends);selected=[];adopt=m._adopt_bulk_target
        def record(key,node,*args):
            selected.append(node.path);return adopt(key,node,*args)
        with mock.patch.object(m,'_adopt_bulk_target',side_effect=record):
            self.assertEqual(m.execute_bulk('synthetic',fixtures.REQUEST)['deferred'],0)
        self.assertEqual(selected[0],'.')
        self.assertEqual(len(selected),5)
        self.assertEqual(m.load_state()['bulk_progress']['renderer_revision'],m.NATIVE_RENDERER_REVISION)
        self.assertEqual(len(fx.sends),sends)

    def test_existing_long_code_gets_one_profile_revision_then_true_noop(self):
        fx=fixtures.BulkTests(methodName='test_full_hierarchy_parent_first_original_root_retained_and_noop')
        fx.setUp();self.addCleanup(fx.doCleanups)
        payload='    synthetic_line()  \n'*200
        subprocess.run(['git','-C',str(fx.root),'config','user.name','Synthetic'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(fx.root),'config','user.email','synthetic@example.invalid'],check=True,capture_output=True)
        (fx.root/'Wiki/a.md').write_text('```python\n'+payload+'\n```\n')
        for args in [('add','Wiki/a.md'),('commit','-m','synthetic code')]:
            subprocess.run(['git','-C',str(fx.root),*args],check=True,capture_output=True)
        root=fx.legacy_root();directory=fx.add_page(root,'Wiki',m.directory_page_blocks('Wiki'))
        target=fx.add_page(directory,'a.md',m.file_page_blocks(fx.root,'Wiki/a.md'))
        before=copy.deepcopy(fx.children[target]);signature=m.file_signature(fx.root/'Wiki/a.md')
        state=m.load_state();state['bulk_request']['follow_source_changes']=True
        state['entries']['Wiki']={'type':'directory','signature':m.DIRECTORY_SIGNATURE}
        state['entries']['Wiki/a.md']={'type':'file','signature':signature};fx.persist(state)
        self.assertEqual(m.execute_bulk('synthetic',fixtures.REQUEST)['held'],0)
        entry=m.load_state()['entries']['Wiki/a.md']
        self.assertEqual(entry['signature'],signature)
        self.assertTrue(entry['render_version'].startswith('native-v1:'))
        self.assertEqual(fx.children[target][:len(before)],before)
        self.assertEqual(code_text(fx.children[target][len(before):]),payload)
        sends=len(fx.sends)
        self.assertEqual(m.execute_bulk('synthetic',fixtures.REQUEST)['held'],0)
        self.assertEqual(len(fx.sends),sends)


if __name__=='__main__':unittest.main()
