"""A moving non-Get writer must not cause duplicate Get reads or writes."""
from contextlib import ExitStack
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_recurring_runtime as runtime


class Rebaseline(unittest.TestCase):
    def test_replay_keeps_bytes_and_404_without_source_calls(self):
        src = types.SimpleNamespace(enumerate=Mock(return_value=([{'id': '1'}], True)),
            detail_raw=Mock(side_effect=[b'{ "raw" : 1 }', RuntimeError('GetNote HTTP 404')]))
        replay = runtime.ReplaySource(src)
        self.assertEqual(replay.enumerate(), ([{'id': '1'}], True))
        self.assertEqual(replay.detail_raw('1'), b'{ "raw" : 1 }')
        with self.assertRaises(RuntimeError): replay.detail_raw('2')
        replay.seal()
        self.assertEqual(replay.enumerate(), ([{'id': '1'}], True))
        self.assertEqual(replay.detail_raw('1'), b'{ "raw" : 1 }')
        with self.assertRaises(RuntimeError): replay.detail_raw('2')
        src.enumerate.assert_called_once(); self.assertEqual(src.detail_raw.call_count, 2)
        with self.assertRaises(Exception): replay.detail_raw('3')

    def test_unchanged_linear_tree_only(self):
        old, middle, new = 'a'*40, 'b'*40, 'c'*40
        trees = {old:'1', middle:'1', new:'1'}
        reader = types.SimpleNamespace(source_get_tree=lambda x: trees[x], local=types.SimpleNamespace(run=lambda *args: b'fixed'))
        remote = object()
        with patch.object(runtime, 'fetch_commit'), patch.object(runtime.m, 'guard', return_value=new), \
             patch.object(runtime.entry, 'single_parent', side_effect=lambda _, x: {new:middle,middle:old}[x]):
            self.assertEqual(runtime.rebaseline_parent(None, reader, remote, old, new), new)
            trees[middle] = '2'
            with self.assertRaises(Exception): runtime.rebaseline_parent(None, reader, remote, old, new)

    def test_related_notion_state_change_is_rejected_even_if_get_tree_is_same(self):
        old, new = 'a'*40, 'b'*40
        local = types.SimpleNamespace(run=lambda _cmd, _z, commit, *args: b'old' if commit == old else b'new')
        reader = types.SimpleNamespace(local=local, source_get_tree=lambda _: 'same')
        with patch.object(runtime, 'fetch_commit'), patch.object(runtime.entry, 'single_parent', return_value=old):
            with self.assertRaises(Exception): runtime.rebaseline_parent(local, reader, object(), old, new)

    def test_expected_parent_is_never_rebased(self):
        old, new = 'a'*40, 'b'*40
        reader = types.SimpleNamespace(local=object())
        remote = types.SimpleNamespace(cas_main=Mock())
        replacements = [(runtime, 'validate_policy', {'return_value': {}}),
            (runtime.m, 'guard', {'side_effect': [old, new]}),
            (runtime.staging, 'API', {'return_value': object()}),
            (runtime.pending, 'check', {'return_value': old}),
            (runtime.r, 'readback_checkout', {'return_value': object()}),
            (runtime.m, 'load_evidence', {'return_value': (reader, object(), None)})]
        with ExitStack() as stack:
            for target, name, kw in replacements: stack.enter_context(patch.object(target, name, **kw))
            rebase = stack.enter_context(patch.object(runtime, 'rebaseline_parent'))
            prepare = stack.enter_context(patch.object(runtime.entry, 'prepare'))
            with self.assertRaises(Exception):
                runtime.run('token', object(), policy={}, approved_policy_sha256='c'*64,
                    remote=remote, public_client=object(), expected_parent=old,
                    bootstrap={'capture_anchor': {'capture_commit': 'd'*40}})
        rebase.assert_not_called(); prepare.assert_not_called(); remote.cas_main.assert_not_called()

    def test_move_during_evidence_warmup_reuses_loaded_evidence(self):
        old, new, anchor = 'a'*40, 'b'*40, 'c'*40
        state = {'main': old}
        reader = types.SimpleNamespace(local=types.SimpleNamespace(run=lambda *args: b'fixed'), source_get_tree=lambda _: 'same')
        src = types.SimpleNamespace(enumerate=Mock(return_value=([{'id':'1'}],True)), detail_raw=Mock(return_value=b'raw'))
        def evidence(*args, **kwargs):
            state['main'] = new
            return reader, object(), None
        def prepare(_r, _e, parent, source, **kwargs):
            self.assertEqual(parent, new); source.enumerate(); source.detail_raw('1')
            return {'parent_commit': parent}, {}
        def guard(_remote, expected=None):
            if expected is not None and expected != state['main']: raise ValueError('changed')
            return state['main']
        replacements = [(runtime,'validate_policy',{'return_value':{'rules_sha256':'d'*64}}),
            (runtime.m,'guard',{'side_effect':guard}), (runtime.staging,'API',{'return_value':object()}),
            (runtime.pending,'check',{'return_value':anchor}), (runtime.pending,'exact_ref',{'return_value':anchor}),
            (runtime.r,'readback_checkout',{'return_value':object()}), (runtime.m,'load_evidence',{'side_effect':evidence}),
            (runtime,'load_trusted_inputs',{'return_value':({'commit':anchor},None,None)}),
            (runtime,'fetch_commit',{'return_value':None}), (runtime.entry,'single_parent',{'return_value':old}),
            (runtime.entry,'prepare',{'side_effect':prepare}),
            (runtime.entry,'public_report',{'return_value':{'status':'recurring_local_candidate_verified','plan_sha256':'e'*64}})]
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(target,name,**kw)) for target,name,kw in replacements]
            runtime.run('token',src,policy={},approved_policy_sha256='f'*64,remote=types.SimpleNamespace(),public_client=object(),
                bootstrap={'capture_anchor':{'capture_commit':'1'*40}})
        mocks[6].assert_called_once(); src.enumerate.assert_called_once(); src.detail_raw.assert_called_once()

    def test_move_during_collection_replans_once_without_recollecting(self):
        old,new,anchor='a'*40,'b'*40,'c'*40
        state={'main':old}; trees={old:'same',new:'same'}
        reader=types.SimpleNamespace(local=types.SimpleNamespace(run=lambda *args:b'fixed'),source_get_tree=lambda c:trees[c])
        remote=types.SimpleNamespace(cas_main=Mock())
        src=types.SimpleNamespace(enumerate=Mock(return_value=([{'id':'1'}],True)), detail_raw=Mock(return_value=b'{ "raw": 1 }'))
        calls=[]
        def prepare(_r,_e,parent,source,**kw):
            calls.append(parent); self.assertEqual(source.enumerate(),([{'id':'1'}],True)); self.assertEqual(source.detail_raw('1'),b'{ "raw": 1 }')
            if parent==old: state['main']=new
            return ({'parent_commit':parent}, {})
        def guard(_remote, expected=None):
            if expected is not None and expected != state['main']: raise ValueError('changed')
            return state['main']
        replacements=[(runtime,'validate_policy',{'return_value':{'rules_sha256':'d'*64}}),
            (runtime.m,'guard',{'side_effect':guard}), (runtime.staging,'API',{'return_value':object()}),
            (runtime.pending,'check',{'return_value':anchor}), (runtime.pending,'exact_ref',{'return_value':anchor}),
            (runtime.r,'readback_checkout',{'return_value':object()}), (runtime.m,'load_evidence',{'return_value':(reader,object(),None)}),
            (runtime,'load_trusted_inputs',{'return_value':({'commit':anchor},None,None)}),
            (runtime,'fetch_commit',{'return_value':None}), (runtime.entry,'single_parent',{'return_value':old}),
            (runtime.entry,'prepare',{'side_effect':prepare}),
            (runtime.entry,'public_report',{'return_value':{'status':'recurring_local_candidate_verified','plan_sha256':'e'*64}})]
        with ExitStack() as stack:
            for target,name,kw in replacements: stack.enter_context(patch.object(target,name,**kw))
            result=runtime.run('token',src,policy={},approved_policy_sha256='f'*64,remote=remote,public_client=object(),
                bootstrap={'capture_anchor':{'capture_commit':'1'*40}})
        self.assertEqual(result['status'],'recurring_candidate_verified')
        self.assertEqual(calls,[old,new]); src.enumerate.assert_called_once(); src.detail_raw.assert_called_once()
        remote.cas_main.assert_not_called()
