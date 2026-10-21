"""Synthetic-only release guard checks."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_public_release_guard as g

class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); (self.root/'a.py').write_bytes(b'generic code')
        self.markers=[b'private-example-value']
    def test_clean_not_permission(self):
        result=g.scan(self.root,self.markers)
        self.assertEqual(result['status'],'denylist_clear_not_release_approved'); self.assertFalse(result['publication_authorized'])
    def test_content(self):
        (self.root/'fixture').write_bytes(self.markers[0]); r=g.scan(self.root,self.markers)
        self.assertEqual((r['affected_files'],r['marker_file_hits']),(1,1))
        self.assertNotIn('private-example-value',str(r)); self.assertNotIn('fixture',str(r))
    def test_filename(self):
        (self.root/'private-example-value').write_bytes(b'')
        self.assertEqual(g.scan(self.root,self.markers)['affected_files'],1)
    def test_hidden_files_scanned(self):
        (self.root/'.hidden').write_bytes(self.markers[0]); self.assertEqual(g.scan(self.root,self.markers)['files'],2)
        self.assertEqual(g.scan(self.root,self.markers)['affected_files'],1)
    def test_nested(self):
        (self.root/'tests').mkdir(); (self.root/'tests'/'fixture').write_bytes(self.markers[0])
        self.assertEqual(g.scan(self.root,self.markers)['affected_files'],1)
    def test_symlink(self):
        (self.root/'link').symlink_to(self.root/'a.py')
        with self.assertRaises(g.ReleaseGuardError):g.scan(self.root,self.markers)
    def test_directory_link(self):
        (self.root/'link').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(g.ReleaseGuardError):g.scan(self.root,self.markers)
    def test_hardlink(self):
        os.link(self.root/'a.py',self.root/'hard')
        with self.assertRaises(g.ReleaseGuardError):g.scan(self.root,self.markers)
    def test_empty_markers_rejected(self):
        with self.assertRaises(g.ReleaseGuardError):g.scan(self.root,[])
    def test_duplicate_markers_rejected(self):
        with self.assertRaises(g.ReleaseGuardError):g.scan(self.root,self.markers*2)
    def test_short_markers_rejected(self):
        with self.assertRaises(g.ReleaseGuardError):g.scan(self.root,[b'x'])
    def test_budget(self):
        with patch.object(g,'MAX_BYTES',1),self.assertRaises(g.ReleaseGuardError):g.scan(self.root,self.markers)
    def test_files_budget(self):
        with patch.object(g,'MAX_FILES',0),self.assertRaises(g.ReleaseGuardError):g.scan(self.root,self.markers)
    def test_hash_changes(self):
        before=g.scan(self.root,self.markers)['inventory_sha256']; (self.root/'a.py').write_bytes(b'new')
        self.assertNotEqual(g.scan(self.root,self.markers)['inventory_sha256'],before)
    def test_deterministic(self):self.assertEqual(g.scan(self.root,self.markers),g.scan(self.root,self.markers))
    def test_empty_tree(self):
        (self.root/'a.py').unlink()
        with self.assertRaises(g.ReleaseGuardError):g.scan(self.root,self.markers)
if __name__=='__main__':unittest.main()
