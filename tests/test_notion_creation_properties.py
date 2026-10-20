"""Real scheduled runtime publication→outbox→native create, no manual manifest."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import get_notion_executor as e
import get_notion_creation_properties as creation
import get_notion_runtime as runtime
import get_notion_properties as m
import test_notion_runtime as rt
import test_notion_properties as prop
import test_confirmed_source as source

class Tests(unittest.TestCase):
    def setUp(self):
        self.case=rt.RuntimeTests(methodName='test_zero_changes_queue_stays_empty')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.ident=source.N(8)
    def publish(self,**fields):
        data=prop.source(self.ident,**fields)
        with patch.object(source,'md',return_value=data):self.case.f.publication({self.ident:'Synthetic body'})
        return self.case.f.clone(shallow=True)
    def test_scheduled_new_page_maps_four_fields_without_per_note_manifest(self):
        local=self.publish();result=self.case.invoke(local,True)
        self.assertEqual(result['execution']['status'],'verified',result)
        actual=self.case.api.pages[self.ident]['properties_raw']
        desired,_=m.project(prop.source(self.ident),self.ident,m.schema_projection(prop.schema()))
        for name,value in desired.items():self.assertEqual(m.value(actual[name]),m.value(value))
        receipt=self.case.store.value['engine_state']['property_receipts'][self.ident]
        self.assertEqual(receipt['manifest_sha256'],e.digest(creation.SCOPE));self.assertFalse(receipt['held'])
        self.assertEqual(self.case.invoke(local,True)['status'],'queue_empty')
        self.assertEqual(len([x for x in self.case.api.calls if x[0]=='create']),1)
    def test_missing_timezone_and_options_are_partial_not_silent_defaults(self):
        local=self.publish(created_at='2026-10-01 11:22:33',tags=['unknown'],note_type='text')
        result=self.case.invoke(local,True)
        self.assertEqual(result['execution']['status'],'partial',result)
        self.assertEqual(result['execution']['retained_conflicts'],1)
        receipt=self.case.store.value['engine_state']['property_receipts'][self.ident]
        self.assertEqual(receipt['held'],{'创建时间':'property_timezone_required','标签':'property_option_missing','笔记类型':'property_option_missing'})
        self.assertNotIn('笔记类型',self.case.api.pages[self.ident]['properties_raw'])
    def test_bound_registry_supplies_timezone_without_exporting_audio(self):
        fields={'created_at':'2026-10-01 11:22:33','updated_at':'2026-10-02 12:23:34'}
        data=prop.source(self.ident,**fields)
        row={'baseline_blob':e.p.git_blob(data),'mirror_sha256':e.digest(data),'filePath':'notes/'+self.ident+'.md',
            'source_metadata':{'timestamps':{'created_at':'2026-10-01T11:22:33+08:00','edited_at':'2026-10-02T12:23:34+08:00'},
                'attachments':[{'url':'PRIVATE SIGNED URL','duration':14000}]}}
        self.case.f.write(m.REGISTRY,e.encoded({'newById':{self.ident:row}}))
        result=self.case.invoke(self.publish(**fields),True)
        self.assertEqual(result['execution']['status'],'verified',result)
        props=self.case.api.pages[self.ident]['properties_raw'];self.assertTrue(props['创建时间']['date']['start'].endswith('+08:00'))
        self.assertNotIn('PRIVATE SIGNED URL',str(props));self.assertNotIn('PRIVATE SIGNED URL',str(result))
    def test_unreviewed_recording_still_blocked_before_create(self):
        result=self.case.invoke(self.publish(note_type='recorder_audio'),True)
        self.assertEqual(result['execution']['blocked'],1,result);self.assertFalse(self.case.request.attempted)
        self.assertNotIn(self.ident,self.case.store.value['engine_state'].get('property_receipts',{}))
    def test_scheduled_long_create_maps_metadata_and_retains_bounded_batches(self):
        import test_notion_create_batches as batches
        local=self.publish();self.case.api=batches.API(self.case.adapter)
        self.case.request=rt.FixtureTransport(self.case.api)
        self.case.request.fixture=batches.PagedOfficialFixture(self.case.api)
        with patch.object(e.p,'render_markdown',return_value=(batches.rendered(150,False),{},[])):
            result=self.case.invoke(local,True)
        self.assertEqual(result['execution']['status'],'verified',result)
        self.assertEqual([x[0] for x in self.case.api.calls],['create','append'])
        self.assertEqual(set(self.case.store.value['engine_state']['property_receipts'][self.ident]['managed']),set(m.FIELDS))
    def test_later_body_update_preserves_manually_changed_metadata(self):
        local=self.publish();self.assertEqual(self.case.invoke(local,True)['execution']['created'],1)
        manual={'type':'multi_select','multi_select':[{'name':'manual'}]}
        self.case.api.pages[self.ident]['properties_raw']['标签']=deepcopy(manual)
        self.case.api.edited(self.case.api.pages[self.ident])
        data=prop.source(self.ident,updated_at='2026-10-03T12:23:34+08:00').replace(b'Old source',b'Changed body')
        with patch.object(source,'md',return_value=data):self.case.f.publication({self.ident:'Changed body'})
        self.case.request=rt.FixtureTransport(self.case.api)
        result=self.case.invoke(self.case.f.clone(shallow=True),True)
        self.assertEqual(result['execution']['updated'],1,result)
        self.assertEqual(self.case.api.pages[self.ident]['properties_raw']['标签'],manual)
    def test_dependency_failure_happens_before_enqueue_or_external_write(self):
        local=self.publish();before=self.case.store.read()
        with patch.object(runtime.property_runtime,'check_dependency',side_effect=e.Blocked('property_parser_unavailable')):
            with self.assertRaisesRegex(e.Blocked,'property_parser_unavailable'):self.case.invoke(local,True)
        self.assertEqual(self.case.store.read(),before);self.assertFalse(self.case.request.attempted)
    def test_scope_cannot_select_history_or_change_field_list(self):
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):creation.validate({**creation.SCOPE,'target':'all_pages'})
        for desc in ({'initial_cohort':{},'outbox_receipt_sha256':'a'*64,'records':{self.ident:{}},'changed_content_ids':[self.ident]},
            {'initial_cohort':None,'historical_manifest_sha256':'b'*64,'outbox_receipt_sha256':'a'*64,'records':{self.ident:{}},'changed_content_ids':[self.ident]},
            {'initial_cohort':None,'records':{self.ident:{}},'changed_content_ids':[self.ident]}):
            pub=type('Publication',(),{'descriptor':desc})();self.assertFalse(creation.eligible(pub))
    def test_standing_scope_is_bound_in_runtime_policy(self):
        scope=runtime.scope_for({'rules_sha256':'a'*64})
        self.assertEqual(scope['new_page_property_scope_sha256'],e.digest(creation.SCOPE))

if __name__=='__main__':unittest.main()
