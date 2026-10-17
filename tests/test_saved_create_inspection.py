"""Synthetic runner boundary tests; no private journal or network fixtures."""
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

PATH=Path(__file__).resolve().parents[1]/'diagnostics'/'run_saved_create_diagnostic.py'
spec=importlib.util.spec_from_file_location('saved_inspection',PATH)
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
import get_legacy_manual as m
import get_notion_executor as e
import get_notion_outbox as outbox
import get_notion_png_store as store
import get_notion_uploaded_png as png
import get_notion_live_transport as live

class InspectionTests(unittest.TestCase):
    def fixture(self):
        note='1234567890123456789';source='1'*32;page='2'*32
        plan={'policy':{'data_source_id':source},'records':[{'note_id':note}]}
        op={'kind':'create','phase':'acknowledged','response':page}
        state={'engine_state':{'journal':{'index':0,'plan':plan,'operation':op}}}
        raw=json.dumps(state,sort_keys=True,separators=(',',':')).encode()
        class Remote:
            def commit(self,sha):return {'tree':'root'}
            def tree_entries(self,sha):
                return [{'path':name,'type':kind,'mode':mode,'sha':nxt} for name,kind,mode,nxt in {
                    'root':[('.sync','tree','040000','sync')],
                    'sync':[('get-notion','tree','040000','notion')],
                    'notion':[('state.json','blob','100644',r.STATE_BLOB)],
                }[sha]]
            def blob(self,sha,size):return raw
        return state,raw,Remote(),source,page,note
    @contextlib.contextmanager
    def setup_case(self):
        state,raw,remote,source,page,note=self.fixture()
        with contextlib.ExitStack() as stack:
            for name,value in {'STATE_BYTES':len(raw),'STATE_SHA':hashlib.sha256(raw).hexdigest(),
                               'PLAN_SHA':e.digest(state['engine_state']['journal']['plan']),
                               'OP_SHA':e.digest(state['engine_state']['journal']['operation'])}.items():
                stack.enter_context(patch.object(r,name,value))
            guard=stack.enter_context(patch.object(m,'guard',return_value=r.STATE_COMMIT))
            # Isolate orchestration, not the separately tested strict codecs.
            stack.enter_context(patch.object(outbox,'check_state'))
            stack.enter_context(patch.object(outbox,'encoded',return_value=raw))
            stack.enter_context(patch.object(store,'upload_receipts',return_value={note:{'receipt':{},'sha256':'a'*64}}))
            stack.enter_context(patch.object(e.p,'writer_gate',return_value=True))
            stack.enter_context(patch.object(png,'bind_snapshot',return_value={'snapshot':{'page_id':page},'asset_downloads':1}))
            yield stack,remote,guard,source,page,note
    def run_case(self,remote):
        calls=[]
        def request(*args):calls.append(args);return {}
        result=r.inspect(remote,request,lambda:{},lambda url:b'',lambda *args:{'matches':True})
        return result,calls
    def test_head_drift_stops_before_notion(self):
        with self.setup_case() as (_,remote,guard,*_):
            guard.return_value='0'*40
            result,calls=self.run_case(remote)
            self.assertEqual(result['reason'],'state_changed');self.assertEqual(calls,[])
    def test_blob_drift_stops_before_notion(self):
        with self.setup_case() as (_,remote,*_):
            remote.blob=lambda *args:b'PRIVATE'
            result,calls=self.run_case(remote)
            self.assertEqual(result['reason'],'state_changed');self.assertEqual(calls,[])
    def test_operation_drift_stops_before_notion(self):
        with self.setup_case() as (stack,remote,*_):
            stack.enter_context(patch.object(r,'OP_SHA','0'*64))
            result,calls=self.run_case(remote)
            self.assertEqual(result['reason'],'operation_changed');self.assertEqual(calls,[])
    def test_write_routes_rejected_before_request(self):
        routes=[('POST','/pages'),('PATCH','/blocks/'+'2'*32+'/children'),
                ('PATCH','/blocks/'+'2'*32),('POST','/file_uploads'),
                ('POST','/file_uploads/'+'2'*32+'/send'),('DELETE','/pages/'+'2'*32)]
        for method,path in routes:
            with self.subTest(method=method,path=path),self.setup_case() as (stack,remote,*_):
                class API:
                    def __init__(self,request,*args):self.request=request
                    def read_note(self,ident):return self.request(method,path,{})
                stack.enter_context(patch.object(live,'API',API))
                result,calls=self.run_case(remote)
                self.assertEqual(result['reason'],'write_forbidden');self.assertEqual(calls,[])
    def test_unrelated_reads_and_query_scope_rejected(self):
        for method,path,body in [
            ('POST','/search',{}),
            ('POST','/data_sources/'+'1'*32+'/query',{'page_size':100,'filter':{'property':'笔记 ID','rich_text':{'equals':'9999999999999999999'}}}),
            ('GET','/pages/'+'3'*32,None),
            ('GET','/blocks/'+'3'*32,None),
        ]:
            with self.subTest(path=path),self.setup_case() as (stack,remote,*_):
                class API:
                    def __init__(self,request,*args):self.request=request
                    def read_note(self,ident):return self.request(method,path,body)
                stack.enter_context(patch.object(live,'API',API))
                result,calls=self.run_case(remote)
                self.assertEqual(result['reason'],'read_scope_changed');self.assertEqual(calls,[])
    def test_exact_query_and_nested_pagination_are_allowed(self):
        with self.setup_case() as (stack,remote,guard,source,page,note):
            child='3'*32;calls=[]
            class API:
                def __init__(self,request,*args):self.request=request
                def read_note(self,ident):
                    query={'page_size':100,'filter':{'property':'笔记 ID','rich_text':{'equals':ident}}}
                    self.request('POST','/data_sources/'+source+'/query',query)
                    self.request('POST','/data_sources/'+source+'/query',{**query,'start_cursor':'cursor-2'})
                    self.request('GET','/blocks/'+page+'/children?page_size=100',None)
                    self.request('GET','/blocks/'+page+'/children?page_size=100&start_cursor=cursor-2',None)
                    self.request('GET','/blocks/'+child+'/children?page_size=100',None)
                    self.request('GET','/blocks/'+child,None)
                    return {'page_id':page}
            stack.enter_context(patch.object(live,'API',API))
            def request(method,path,body):
                calls.append((method,path));return {'results':[{'id':child}]}
            result=r.inspect(remote,request,lambda:{},lambda url:b'',lambda *args:{'matches':True})
            self.assertEqual(result['status'],'inspected');self.assertEqual(len(calls),6)
    def test_private_failure_text_is_not_reported(self):
        with self.setup_case() as (stack,remote,*_):
            stack.enter_context(patch.object(live,'API',side_effect=ValueError('PRIVATE NAME URL TOKEN')))
            result,calls=self.run_case(remote)
            self.assertEqual(result['reason'],'inspection_failed')
            self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(calls,[])
    def test_permission_failure_is_fixed(self):
        with self.setup_case() as (stack,remote,*_):
            stack.enter_context(patch.object(live,'API',side_effect=ValueError('permission_denied')))
            result,_=self.run_case(remote);self.assertEqual(result['reason'],'permission_denied')
    def test_final_guard_failure_is_not_success(self):
        with self.setup_case() as (stack,remote,guard,source,page,note):
            guard.side_effect=[r.STATE_COMMIT,'0'*40]
            api=stack.enter_context(patch.object(live,'API'));api.return_value.read_note.return_value={'page_id':page}
            result,calls=self.run_case(remote)
            self.assertEqual(result['reason'],'state_changed');self.assertEqual(result['stage'],'final_guard')
            self.assertEqual(calls,[])
    def test_success_has_no_mutation_capability(self):
        with self.setup_case() as (stack,remote,guard,source,page,note):
            api=stack.enter_context(patch.object(live,'API'));api.return_value.read_note.return_value={'page_id':page}
            result,calls=self.run_case(remote)
            self.assertEqual(result['status'],'inspected');self.assertEqual(result['journal_writes'],0)
            self.assertEqual(result['remote_writes'],0);self.assertEqual(calls,[])
            self.assertEqual(guard.call_count,2)

if __name__=='__main__':unittest.main()
