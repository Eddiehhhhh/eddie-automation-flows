"""Get-only history checks must not recursively expand unrelated project trees."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_legacy_adapter as adapter
import get_recurring_entry as entry


class HistorySubtree(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.env=adapter.retention.safe_environment(None)
  self.git('init','-q');self.git('config','user.name','Test');self.git('config','user.email','test@example.invalid')
  self.note=self.root/'Raw/03 Get/note.md';self.note.parent.mkdir(parents=True);self.note.write_text('original')
  project=self.root/'project/assets';project.mkdir(parents=True)
  for i in range(100):(project/str(i)).write_text('unrelated')
  self.commits=[self.commit('initial')]
  for i in range(3):
   (project/'0').write_text(str(i));self.commits.append(self.commit('project update'))
  self.reader=adapter.OfflineGit(self.root)
 def git(self,*args):
  return subprocess.check_output(['git','-C',str(self.root),*args],env=self.env,stderr=subprocess.DEVNULL,text=True).strip()
 def commit(self,message):
  self.git('add','--all');self.git('commit','-qm',message);return self.git('rev-parse','HEAD')
 def test_exact_first_parent_history_needs_no_full_recursive_tree(self):
  with patch.object(self.reader,'tree',side_effect=AssertionError('full recursive tree forbidden')):
   entry.unchanged_get_history(self.reader,self.commits[0],self.commits[-1])
  self.assertEqual(self.reader.cache,{})
  self.assertEqual(len(self.reader._source_get_trees),len(self.commits))
  self.assertEqual(self.reader.source_get_tree(self.commits[-1]),self.git('rev-parse',self.commits[-1]+':Raw/03 Get'))
  with patch.object(self.reader.local,'run',side_effect=AssertionError('cached immutable tree queried again')):
   self.reader.source_get_tree(self.commits[-1])
 def test_intermediate_get_edit_then_revert_still_rejected(self):
  self.note.write_text('changed');self.commit('change Get')
  self.note.write_text('original');reverted=self.commit('revert Get')
  self.assertEqual(self.reader.source_get_tree(self.commits[0]),self.reader.source_get_tree(reverted))
  with self.assertRaisesRegex(RuntimeError,'entry_foreign_get_change'):
   entry.unchanged_get_history(self.reader,self.commits[0],reverted)
 def test_non_commit_object_rejected(self):
  with self.assertRaisesRegex(RuntimeError,'adapter_anchor'):
   self.reader.source_get_tree(self.git('rev-parse','HEAD^{tree}'))
 def test_get_path_blob_cannot_be_used_as_tree(self):
  self.git('rm','-qr','Raw/03 Get');self.note.parent.parent.mkdir(exist_ok=True)
  self.note.parent.write_text('not a tree')
  commit=self.commit('replace Get directory')
  with self.assertRaisesRegex(RuntimeError,'adapter_anchor'):self.reader.source_get_tree(commit)

if __name__=='__main__':unittest.main()
