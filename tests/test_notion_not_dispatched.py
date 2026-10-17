"""Known pre-network refusals can stop safely without inventing old evidence."""
from copy import deepcopy
import io
import unittest
import urllib.error
from unittest.mock import patch
import get_notion_executor as e
import get_notion_live_transport as t
import test_notion_executor as f

class ForbiddenOpener:
    def __init__(self,error=None):self.calls=0;self.error=error
    def open(self,*args,**kwargs):
        self.calls+=1
        if self.error:raise self.error
        raise AssertionError('Network is forbidden in this fixture')

class Tests(unittest.TestCase):
    def fixture(self,create=False):
        fixture=f.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        if create:
            fixture.api.pages.clear();state,sha=fixture.store.read();state['receipts']={};fixture.store.compare_and_set(sha,state)
        fixture.publish();return fixture,fixture.plan()
    def rejection(self,kind,row,ds,root=None,wrong=False):
        if kind=='archive':method,path,body='PATCH','/blocks/'+root,{'archived':True}
        elif kind=='append':
            method,path='PATCH','/blocks/'+row['page_id']+'/children';body={'children':t.wire_blocks(row['request_blocks'])}
            if row['after_anchor'] is not None:body['after']=row['after_anchor']
        else:method,path,body='POST','/pages',{'parent':{'type':'data_source_id','data_source_id':ds},'properties':row['properties'],'children':t.wire_blocks(row['request_blocks'])}
        key=t.fingerprint(method,path,body);clock=[0];opener=ForbiddenOpener()
        transport=t.Transport('synthetic',opener=opener,clock=lambda:clock[0],approved_writes=[key]);clock[0]=1201
        try:transport(method,path,body)
        except t.NotDispatched as exc:
            self.assertEqual(transport.attempts,0);self.assertEqual(opener.calls,0)
            if wrong:exc.request_fingerprint='0'*64
            raise
    def test_all_three_kinds_rearm_durably_and_only_fresh_execution_continues(self):
        for kind in ('append','archive','create'):
            with self.subTest(kind=kind):
                fixture,plan=self.fixture(create=kind=='create');row=plan['records'][0]
                name={'append':'append_blocks','archive':'archive_block','create':'create_page'}[kind]
                normal=getattr(fixture.api,name);calls=[]
                def stop(*args):
                    calls.append(1);op=fixture.store.read()[0]['journal']['operation'];self.assertEqual(op['phase'],'in_flight')
                    self.rejection(kind,row,f.DS,args[0] if kind=='archive' else None)
                setattr(fixture.api,name,stop);result=fixture.run_plan(plan)
                self.assertEqual(result['reason'],'request_not_dispatched');self.assertEqual(calls,[1])
                state,_=fixture.store.read();self.assertEqual(state['journal']['operation']['phase'],'prepared')
                self.assertEqual(state['journal']['operation']['kind'],kind)
                self.assertEqual(result['write_calls'],1 if kind=='archive' else 0)
                setattr(fixture.api,name,normal);result=fixture.run_plan(plan)
                self.assertEqual(result['status'],'verified')
                self.assertEqual(fixture.writes(),[('create',)] if kind=='create' else [('append',),('archive',)])
                self.assertIsNone(fixture.store.read()[0]['journal'])
    def test_rearm_cas_failure_keeps_persisted_inflight_unknown(self):
        fixture,plan=self.fixture();row=plan['records'][0];original=fixture.store.compare_and_set;seen=[False]
        def cas(expected,value):
            op=(value['journal'] or {}).get('operation')
            if op and op['phase']=='in_flight':seen[0]=True
            if seen[0] and op and op['phase']=='prepared':raise RuntimeError('private failure')
            return original(expected,value)
        fixture.store.compare_and_set=cas
        fixture.api.append_blocks=lambda *args:self.rejection('append',row,f.DS)
        result=fixture.run_plan(plan)
        self.assertEqual(result['reason'],'journal_failed');self.assertEqual(result['write_state'],'unknown')
        self.assertEqual(fixture.store.read()[0]['journal']['operation']['phase'],'in_flight')
        self.assertEqual(fixture.writes(),[])
    def test_wrong_fingerprint_does_not_rearm(self):
        fixture,plan=self.fixture();row=plan['records'][0]
        fixture.api.append_blocks=lambda *args:self.rejection('append',row,f.DS,wrong=True)
        result=fixture.run_plan(plan);self.assertEqual(result['reason'],'uncertain_append')
        self.assertEqual(fixture.store.read()[0]['journal']['operation']['phase'],'in_flight')
    def test_generic_budget_or_after_effect_never_rearms(self):
        for after in (False,True):
            fixture,plan=self.fixture()
            if after:fixture.api.throw='append_after'
            else:fixture.api.append_blocks=lambda *args:(_ for _ in ()).throw(t.Failure('request_budget'))
            result=fixture.run_plan(plan);self.assertEqual(result['reason'],'uncertain_append')
            self.assertEqual(fixture.store.read()[0]['journal']['operation']['phase'],'in_flight')
    def test_fresh_writer_refusal_after_checkpoint_restores_prepared(self):
        fixture,plan=self.fixture();original=fixture.store.compare_and_set;active=[False]
        def cas(expected,value):
            result=original(expected,value)
            op=(value['journal'] or {}).get('operation')
            if op and op['phase']=='in_flight':active[0]=True
            return result
        fixture.store.compare_and_set=cas
        def guard():
            result=deepcopy(fixture.policy['old_writer_evidence'])
            if active[0]:result['writers']['getnote-to-notion']['active_runs']=1
            return result
        result=e.execute(plan,e.digest(plan),fixture.publication,fixture.api,fixture.store,expected_store_sha=fixture.store.read()[1],writers_guard=guard)
        self.assertEqual(result['reason'],'writer_gate_unknown');self.assertEqual(fixture.writes(),[])
        self.assertEqual(fixture.store.read()[0]['journal']['operation']['phase'],'prepared')
    def test_real_transport_permissions_timeouts_and_parse_errors_are_not_unsent(self):
        method,path,body='PATCH','/blocks/'+'a'*32,{'archived':True};key=t.fingerprint(method,path,body)
        for error in (TimeoutError('private'),urllib.error.HTTPError('hidden',403,'hidden',{},io.BytesIO(b'{}'))):
            opener=ForbiddenOpener(error);transport=t.Transport('synthetic',opener=opener,approved_writes=[key])
            with self.assertRaises(t.Failure) as raised:transport(method,path,body)
            self.assertNotIsInstance(raised.exception,t.NotDispatched);self.assertEqual(opener.calls,1);self.assertEqual(transport.attempts,1)
    def test_opener_entered_same_key_not_dispatched_is_uncertain(self):
        path='/blocks/'+'a'*32;body={'archived':True};key=t.fingerprint('PATCH',path,body)
        opener=ForbiddenOpener(t.NotDispatched(key));transport=t.Transport('synthetic',opener=opener,approved_writes=[key])
        with self.assertRaises(t.Failure) as raised:transport('PATCH',path,body)
        self.assertNotIsInstance(raised.exception,t.NotDispatched)
        self.assertEqual(str(raised.exception),'write_uncertain');self.assertEqual(opener.calls,1)
        self.assertEqual(transport.attempts,1);self.assertIn(key,transport.attempted)
    def test_read_budget_and_typed_api_fingerprint(self):
        clock=[0];transport=t.Transport('synthetic',clock=lambda:clock[0]);clock[0]=1201
        with self.assertRaises(t.Failure) as raised:transport('GET','/pages/'+'a'*32)
        self.assertNotIsInstance(raised.exception,t.NotDispatched)
        import test_live_transport as api_f
        body={'archived':True};key=t.fingerprint('PATCH','/blocks/'+api_f.B,body)
        for wrong in (False,True):
            def request(*args):raise t.NotDispatched('0'*64 if wrong else key)
            api=t.API(request,api_f.D,[api_f.N],approved_writes=[key],data_source_pin=t.native.sha(api_f.D));api.known_blocks.add(api_f.B)
            with self.assertRaises(t.Failure) as raised:api.archive_block(api_f.B)
            self.assertEqual(isinstance(raised.exception,t.NotDispatched),not wrong)
            with self.assertRaisesRegex(t.Failure,'already_attempted'):api.archive_block(api_f.B)

if __name__=='__main__':unittest.main()
