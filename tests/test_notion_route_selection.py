"""Synthetic route commitments; no real scope inputs or private data."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_route_selection as r
class Selection(unittest.TestCase):
    def setUp(self):
        p=patch.object(r,'APPROVED_COMMITMENTS',frozenset({r.commitment('historical-capture','a'*64)}));p.start();self.addCleanup(p.stop)
    def test_canonical(self):self.assertTrue(r.preflight('schedule',True,'canonical',''))
    def test_dispatch_prepare(self):self.assertTrue(r.preflight('workflow_dispatch',False,'canonical',''))
    def test_exact(self):self.assertTrue(r.preflight('workflow_dispatch',True,'historical-capture','a'*64))
    def test_unknown_digest(self):
        with self.assertRaises(r.SelectionError):r.preflight('workflow_dispatch',True,'historical-capture','b'*64)
    def test_mode_bound(self):
        with self.assertRaises(r.SelectionError):r.preflight('workflow_dispatch',True,'historical-apply','a'*64)
    def test_schedule_manual(self):
        with self.assertRaises(r.SelectionError):r.preflight('schedule',True,'historical-capture','a'*64)
    def test_manual_no_apply(self):
        with self.assertRaises(r.SelectionError):r.preflight('workflow_dispatch',False,'historical-capture','a'*64)
    def test_schedule_no_apply(self):
        with self.assertRaises(r.SelectionError):r.preflight('schedule',False,'canonical','')
    def test_canonical_extra_digest(self):
        with self.assertRaises(r.SelectionError):r.preflight('schedule',True,'canonical','a'*64)
    def test_invalid_event(self):
        with self.assertRaises(r.SelectionError):r.preflight('push',True,'canonical','')
    def test_invalid_mode(self):
        with self.assertRaises(r.SelectionError):r.preflight('workflow_dispatch',True,'other','')
    def test_invalid_bool(self):
        with self.assertRaises(r.SelectionError):r.preflight('schedule',1,'canonical','')
    def test_invalid_digest_type(self):
        with self.assertRaises(r.SelectionError):r.preflight('schedule',True,'canonical',None)
    def test_invalid_hex(self):
        with self.assertRaises(r.SelectionError):r.preflight('workflow_dispatch',True,'historical-capture','A'*64)
    def test_no_credential_import_or_lookup(self):
        source=Path(r.__file__).read_text()
        for forbidden in ('PRIVATE_VAULT_TOKEN','get_notion_executor','get_private_runtime_bindings','urllib','requests'):
            self.assertNotIn(forbidden,source)
    def test_cli_failure_static(self):
        out=io.StringIO()
        with patch.dict(os.environ,{'APPLY':'true','GITHUB_EVENT_NAME':'workflow_dispatch','NOTION_MODE':'historical-capture','NOTION_INPUT_SHA256':'b'*64},clear=True),contextlib.redirect_stdout(out):self.assertEqual(r.main(),1)
        self.assertEqual(out.getvalue(),'notion_route_blocked\n')
if __name__=='__main__':unittest.main()
