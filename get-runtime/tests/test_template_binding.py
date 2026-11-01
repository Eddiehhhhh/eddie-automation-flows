"""Only the fixed original source can bridge the two reviewed templates."""
import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

class TemplateBinding(unittest.TestCase):
 def test_exact_transition_and_nearby_rejections(self):
  path=Path(__file__).resolve().parents[1]/'scripts/get_notion_idle_recovery.py';tree=ast.parse(path.read_text());node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='_forward_policy');ns={};exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
  oldhash='1d568531b4672b89ec80eaa08532e152d9c13a400602cfaf5438a3cd24f73dc5';newhash='31623366601a23a56033eae8bb3b09bf314919b6c00b4905797eec26b6675df3'
  ctl=types.SimpleNamespace(fixed_idle_template_transition=lambda a,b:False,FIXED_TEMPLATE_SHA256=oldhash,template_sha256=lambda:newhash,policy_template_bytes=lambda x:b'old',a=types.SimpleNamespace(sha=lambda x:oldhash))
  mods={'get_recurring_controller':ctl,'get_recurring_entry':types.SimpleNamespace(rules_sha256=lambda:'newrules'),'get_recurring_policy_recovery':types.SimpleNamespace(current_get_policy=lambda:'newpolicy')}
  old={'code_sha':'oldcode','rules_sha256':'oldrules','get_policy_sha256':'oldpolicy','controller_template_sha256':oldhash,'repository':'fixed'};new={**old,'code_sha':'newcode','rules_sha256':'newrules','get_policy_sha256':'newpolicy','controller_template_sha256':newhash};row={'public_policy':old,'run_binding':{'id':40657306710}}
  with patch.dict(sys.modules,mods):
   self.assertTrue(ns['_forward_policy'](new,row))
   self.assertFalse(ns['_forward_policy'](new,{**row,'run_binding':{'id':40657306711}}))
   self.assertFalse(ns['_forward_policy']({**new,'repository':'other'},row))
   self.assertFalse(ns['_forward_policy']({**new,'controller_template_sha256':'0'*64},row))
