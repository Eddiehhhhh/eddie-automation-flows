"""Synthetic-only tests: never embed production paths, records or identifiers."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_runtime_bindings as b


def scalar(v): return {'type': 'scalar', 'value': v}
def document(value=None):
    return {'contract': b.CONTRACT, 'modules': {'get_example': {'ITEM': value or scalar('synthetic')}}}
def digest(raw): return hashlib.sha256(raw).hexdigest()


class Bindings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data.json'
        self.raw = b.encoded(document()); self.path.write_bytes(self.raw); self.path.chmod(0o600)
        self.addCleanup(setattr, b, '_CACHE', b._CACHE); b._CACHE = None
    def rejected(self, raw):
        with self.assertRaisesRegex(b.BindingError, '^private_runtime_bindings_invalid$'):
            b.parse(raw, digest(raw))
    def test_canonical_data(self):
        self.assertEqual(b.parse(self.raw, digest(self.raw)), {'get_example': {'ITEM': 'synthetic'}})
    def test_wrong_hash(self):
        with self.assertRaises(b.BindingError): b.parse(self.raw, '0'*64)
    def test_duplicate_json_keys(self):
        self.rejected(b'{"contract":"x","contract":"x"}')
    def test_noncanonical(self): self.rejected(json.dumps(document()).encode())
    def test_unknown_top_level(self):
        d = document(); d['code'] = 'pass'; self.rejected(b.encoded(d))
    def test_bad_contract(self):
        d = document(); d['contract'] = 'other'; self.rejected(b.encoded(d))
    def test_invalid_module(self):
        d = document(); d['modules'] = {'../else': {'ITEM': scalar('x')}}; self.rejected(b.encoded(d))
    def test_invalid_name(self):
        d = document(); d['modules']['get_example'] = {'__import__': scalar('x')}; self.rejected(b.encoded(d))
    def test_extra_wrapper_key(self):
        self.rejected(b.encoded(document({'type': 'scalar', 'value': 'x', 'code': 'pass'})))
    def test_no_python_execution(self):
        self.rejected(b.encoded(document({'type': 'python', 'value': '__import__("os").system("false")'})))
    def test_float_disallowed(self): self.rejected(b.encoded(document(scalar(1.5))))
    def test_list_disallowed_as_scalar(self): self.rejected(b.encoded(document(scalar([]))))
    def test_bytes(self):
        self.assertEqual(b.parse(b.encoded(document({'type':'bytes','value':'00ff'})), digest(b.encoded(document({'type':'bytes','value':'00ff'}))))['get_example']['ITEM'], b'\x00\xff')
    def test_invalid_bytes(self):
        for v in ['x', 'F0', 'abc']:
            with self.subTest(v=v): self.rejected(b.encoded(document({'type':'bytes','value':v})))
    def test_container_types(self):
        for kind, expected in [('list',[1]),('tuple',(1,)),('set',{1}),('frozenset',frozenset({1}))]:
            raw=b.encoded(document({'type':kind,'value':[scalar(1)]}))
            self.assertEqual(b.parse(raw,digest(raw))['get_example']['ITEM'],expected)
    def test_duplicate_set(self): self.rejected(b.encoded(document({'type':'set','value':[scalar(1),scalar(1)]})))
    def test_unhashable_set(self): self.rejected(b.encoded(document({'type':'set','value':[{'type':'list','value':[]}]})))
    def test_nested_dict(self):
        raw=b.encoded(document({'type':'dict','value':{'x':scalar(True)}}))
        self.assertEqual(b.parse(raw,digest(raw))['get_example']['ITEM'],{'x':True})
    def test_depth_bound(self):
        v=scalar('x')
        for _ in range(34): v={'type':'list','value':[v]}
        self.rejected(b.encoded(document(v)))
    def test_size_bound(self):
        with patch.object(b,'MAX_BYTES',3): self.rejected(self.raw)
    def test_read_private_file(self): self.assertEqual(b.read_local(self.path,digest(self.raw))['get_example']['ITEM'],'synthetic')
    def test_public_permissions(self):
        self.path.chmod(0o644)
        with self.assertRaises(b.BindingError): b.read_local(self.path,digest(self.raw))
    def test_symlink(self):
        p=self.path.with_name('link'); p.symlink_to(self.path)
        with self.assertRaises(b.BindingError): b.read_local(p,digest(self.raw))
    def test_hardlink(self):
        os.link(self.path,self.path.with_name('hard'))
        with self.assertRaises(b.BindingError): b.read_local(self.path,digest(self.raw))
    def test_directory(self):
        with self.assertRaises(b.BindingError): b.read_local(self.path.parent,digest(self.raw))
    def test_missing_env(self):
        with patch.dict(os.environ,{},clear=True), self.assertRaises(b.BindingError): b.value('get_example','ITEM')
    def test_missing_file_static_error(self):
        with self.assertRaisesRegex(b.BindingError,'^private_runtime_bindings_invalid$'): b.read_local('/missing/private-name',digest(self.raw))
    def test_production_pin_required(self):
        with patch.dict(os.environ,{b.ENV_FILE:str(self.path)}), self.assertRaises(b.BindingError): b.value('get_example','ITEM')
    def test_cached_copy_and_path_pinning(self):
        raw=b.encoded(document({'type':'list','value':[scalar('x')]})); self.path.write_bytes(raw)
        with patch.object(b,'TRUSTED_BUNDLE_SHA256',digest(raw)), patch.dict(os.environ,{b.ENV_FILE:str(self.path)}):
            result=b.value('get_example','ITEM'); result.append('changed')
            self.assertEqual(b.value('get_example','ITEM'),['x'])
            with patch.dict(os.environ,{b.ENV_FILE:str(self.path)+'other'}), self.assertRaises(b.BindingError): b.value('get_example','ITEM')
    def test_unknown_binding(self):
        with patch.object(b,'TRUSTED_BUNDLE_SHA256',digest(self.raw)), patch.dict(os.environ,{b.ENV_FILE:str(self.path)}), self.assertRaises(b.BindingError): b.value('get_example','MISSING')
    def test_pin_cannot_change_after_load(self):
        with patch.object(b,'TRUSTED_BUNDLE_SHA256',digest(self.raw)), patch.dict(os.environ,{b.ENV_FILE:str(self.path)}):
            b.value('get_example','ITEM')
            with patch.object(b,'TRUSTED_BUNDLE_SHA256','0'*64), self.assertRaises(b.BindingError): b.value('get_example','ITEM')

if __name__ == '__main__': unittest.main()
