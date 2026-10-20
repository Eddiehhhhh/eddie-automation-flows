from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_properties as m
import get_notion_property_transaction as tx
import get_notion_executor as e
import get_notion_pipeline as pipeline
import test_notion_executor as f


def schema():
    return {'创建时间': {'id':'created','type':'date'}, '更新时间':{'id':'updated','type':'date'},
        '标签':{'id':'tags','type':'multi_select','multi_select':{'options':[{'id':'tag-one','name':'existing'}]}},
        '笔记类型':{'id':'kind','type':'select','select':{'options':[{'id':'kind-one','name':'plain_text'}]}}}


def source(ident=f.N, **overrides):
    fields = {'note_id':ident,'title':'Synthetic title','created_at':'2026-10-01T11:22:33+08:00',
        'updated_at':'2026-10-02T12:23:34+08:00','tags':['existing'],'note_type':'plain_text'}
    fields.update(overrides)
    return ('---\n'+'\n'.join(k+': '+json.dumps(v,ensure_ascii=False) for k,v in fields.items())+'\n---\nOld source\n').encode()


def blank_properties():
    return {name:{'type':kind,kind:[] if kind=='multi_select' else None} for name,(_,kind) in m.FIELDS.items()}


class MappingTests(unittest.TestCase):
    def setUp(self): self.schema=m.schema_projection(schema())
    def test_four_exact_types_and_no_unrequested_fields(self):
        values,held=m.project(source(audio_url='DO NOT EXPORT',audio_duration=0),f.N,self.schema)
        self.assertFalse(held);self.assertEqual(set(values),set(m.FIELDS));self.assertNotIn('DO NOT EXPORT',str(values))
        self.assertEqual(values['创建时间']['date']['start'],'2026-10-01T11:22:33+08:00')
    def test_naive_datetime_requires_bound_matching_wall_clock(self):
        data=source(created_at='2026-10-01 11:22:33')
        _,held=m.project(data,f.N,self.schema);self.assertEqual(held['创建时间'],'property_timezone_required')
        values,held=m.project(data,f.N,self.schema,{'created_at':'2026-10-01T11:22:33+08:00'})
        self.assertNotIn('创建时间',held);self.assertTrue(values['创建时间']['date']['start'].endswith('+08:00'))
        _,held=m.project(data,f.N,self.schema,{'created_at':'2026-10-01T11:22:34+08:00'})
        self.assertEqual(held['创建时间'],'property_timestamp_mismatch')
    def test_missing_options_held_without_coercion_or_schema_change(self):
        values,held=m.project(source(note_type='recorder_audio',tags=['unknown']),f.N,self.schema)
        self.assertNotIn('笔记类型',values);self.assertNotIn('标签',values)
        self.assertEqual(set(held.values()),{'property_option_missing'})
    def test_empty_or_missing_source_never_clears_existing_values(self):
        values,held=m.project(source(tags=[],created_at=''),f.N,self.schema)
        self.assertNotIn('标签',values);self.assertNotIn('创建时间',values)
        self.assertEqual(set(held.values()),{'property_source_empty'})
    def test_semantics_normalize_timezone_and_server_option_fields(self):
        a={'type':'date','date':{'start':'2026-10-01T11:22:33+08:00','end':None,'time_zone':None}}
        b={'type':'date','date':{'start':'2026-10-01T03:22:33Z'}}
        self.assertEqual(m.value(a),m.value(b))
        self.assertEqual(m.value({'type':'multi_select','multi_select':[{'id':'1','color':'blue','name':'existing'}]}),{'type':'multi_select','multi_select':[{'name':'existing'}]})
    def test_three_way_fill_empty_then_preserve_manual_change(self):
        desired,_=m.project(source(),f.N,self.schema);current=blank_properties()
        updates,managed,held=m.three_way(desired,current,{},fill_empty=True)
        self.assertEqual(set(updates),set(m.FIELDS));self.assertFalse(held)
        current=deepcopy(desired);current['标签']={'type':'multi_select','multi_select':[{'name':'manual'}]}
        changed=deepcopy(desired);changed['更新时间']={'type':'date','date':{'start':'2026-10-03T12:23:34+08:00'}}
        updates,_,held=m.three_way(changed,current,managed,fill_empty=True)
        self.assertEqual(set(updates),{'更新时间'});self.assertEqual(held['标签'],'property_manual_edit')
    def test_unowned_nonempty_values_remain_even_when_old_receipt_exists(self):
        desired,_=m.project(source(),f.N,self.schema);current=blank_properties();current['创建时间']={'type':'date','date':{'start':'2020-01-01'}}
        updates,managed,held=m.three_way(desired,current,{},fill_empty=True)
        self.assertNotIn('创建时间',updates);self.assertNotIn('创建时间',managed);self.assertEqual(held['创建时间'],'property_unowned_value')
    def test_matching_value_does_not_create_ownership(self):
        desired,_=m.project(source(),f.N,self.schema)
        updates,managed,held=m.three_way(desired,desired,{},fill_empty=True)
        self.assertEqual((updates,managed,held),({},{},{}))
    def test_date_ranges_are_preserved_as_manual_schema_hold(self):
        desired,_=m.project(source(),f.N,self.schema);current=blank_properties()
        current['创建时间']={'type':'date','date':{'start':'2026-01-01','end':'2026-01-03'}}
        patch,_,held=m.three_way(desired,current,{},fill_empty=True)
        self.assertNotIn('创建时间',patch);self.assertEqual(held['创建时间'],'property_schema_changed')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.f=f.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.source_path.write_bytes(source());commit=self.f.commit('metadata source')
        self.publication=e.GitPublication(self.f.repo,commit,self.f.parent,commit)
        self.f.publication=self.publication
        self.f.api.pages[f.N]['properties_raw'].update(blank_properties())
        state,sha=self.f.store.read()
        state['receipts'][f.N]=e.receipt(self.f.api.pages[f.N],f.N,f.DS,self.f.owned,self.publication.descriptor,self.publication.descriptor['records'][f.N])
        self.f.store.compare_and_set(sha,state)
        self.schema=schema();self.calls=[];self.mode=None
        self.f.api.schema=lambda:deepcopy(self.schema)
        def update(page,properties):
            value,_=self.f.store.read();self.assertEqual(value['journal']['operation']['kind'],'properties');self.assertEqual(value['journal']['operation']['phase'],'in_flight')
            self.calls.append(deepcopy(properties))
            if self.mode=='before':raise TimeoutError('PRIVATE')
            self.f.api.pages[f.N]['properties_raw'].update(deepcopy(properties));self.f.api.edited(self.f.api.pages[f.N])
            if self.mode=='human':self.f.api.pages[f.N]['properties_raw']['User']['rich_text']=[e.p.text_run('Concurrent human')]
            if self.mode=='after':raise TimeoutError('PRIVATE')
            return page
        self.f.api.update_properties=update
        self.manifest={'contract':tx.CONTRACT,'source_commit':self.publication.descriptor['commit'],'data_source_id':f.DS,'schema_sha256':e.digest(m.schema_projection(self.schema)),
            'fields':sorted(m.FIELDS),'fill_empty':True,'records':{f.N:{**self.publication.descriptor['records'][f.N],'privacy_review_sha256':'c'*64}},'registry':None}
    def prepare(self):
        state,sha=self.f.store.read()
        return tx.prepare(self.publication,state,sha,{f.N:self.f.api.read_note(f.N)},self.schema,self.manifest,e.digest(self.manifest),self.f.policy)
    def run_plan(self,plan):
        return e.execute(plan,e.digest(plan),self.publication,self.f.api,self.f.store,expected_store_sha=self.f.store.read()[1],writers_guard=lambda:deepcopy(self.f.policy['old_writer_evidence']))
    def test_journaled_backfill_preserves_body_receipt_and_title_and_repeat_is_empty(self):
        prior=deepcopy(self.f.store.read()[0]['receipts']);before=deepcopy(self.f.api.pages[f.N]);plan=self.prepare()
        allowed=pipeline.wire_allowlist(plan,None);self.assertEqual(len(allowed),1)
        result=self.run_plan(plan);self.assertEqual(result['status'],'verified',result);self.assertEqual(result['updated'],1)
        self.assertEqual(len(self.calls),1);after=self.f.api.pages[f.N]
        self.assertEqual(after['recursive_blocks'],before['recursive_blocks']);self.assertEqual(after['properties_raw']['Title'],before['properties_raw']['Title'])
        state,_=self.f.store.read();self.assertEqual(state['receipts'],prior);self.assertEqual(set(state['property_receipts'][f.N]['managed']),set(m.FIELDS));self.assertIsNone(state['journal'])
        self.assertEqual(self.prepare()['records'],[])
    def test_uncertain_after_effect_recovers_by_readback_without_duplicate(self):
        plan=self.prepare();self.mode='after';result=self.run_plan(plan);self.assertEqual(result['reason'],'uncertain_properties',result)
        self.mode=None;result=self.run_plan(plan);self.assertEqual(result['status'],'verified',result);self.assertEqual(len(self.calls),1)
    def test_uncertain_without_effect_never_retries(self):
        plan=self.prepare();self.mode='before';self.assertEqual(self.run_plan(plan)['reason'],'uncertain_properties')
        self.mode=None
        for _ in range(2):self.assertEqual(self.run_plan(plan)['reason'],'uncertain_properties')
        self.assertEqual(len(self.calls),1);self.assertNotIn('property_receipts',self.f.store.read()[0])
    def test_unrelated_concurrent_edit_prevents_receipt(self):
        plan=self.prepare();self.mode='human';out=self.run_plan(plan)
        self.assertEqual(out['reason'],'readback_mismatch',out);self.assertNotIn('property_receipts',self.f.store.read()[0])
    def test_schema_change_prevents_write(self):
        plan=self.prepare();self.schema['标签']['multi_select']['options'].append({'id':'new','name':'new'})
        result=self.run_plan(plan);self.assertEqual(result['reason'],'property_schema_changed',result);self.assertFalse(self.calls)
    def test_changed_target_prevents_write(self):
        plan=self.prepare();self.f.api.pages[f.N]['properties_raw']['标签']={'type':'multi_select','multi_select':[{'name':'manual'}]}
        self.assertEqual(self.run_plan(plan)['reason'],'target_changed');self.assertFalse(self.calls)
    def test_unowned_page_and_wrong_blob_fail_without_adoption(self):
        state,sha=self.f.store.read();state['receipts']={};self.f.store.compare_and_set(sha,state)
        with self.assertRaisesRegex(e.Blocked,'missing_block_ownership'):self.prepare()
        self.assertFalse(self.calls)
    def test_manifest_hash_and_review_evidence_required(self):
        state,sha=self.f.store.read()
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):
            tx.prepare(self.publication,state,sha,{f.N:self.f.api.read_note(f.N)},self.schema,self.manifest,'a'*64,self.f.policy)
        del self.manifest['records'][f.N]['privacy_review_sha256']
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):self.prepare()
    def test_unowned_nonempty_property_is_held_while_other_fields_fill(self):
        self.f.api.pages[f.N]['properties_raw']['创建时间']={'type':'date','date':{'start':'2020-01-01'}}
        result=self.run_plan(self.prepare());self.assertEqual(result['status'],'partial',result)
        self.assertEqual(self.f.api.pages[f.N]['properties_raw']['创建时间']['date']['start'],'2020-01-01')
        state,_=self.f.store.read();self.assertEqual(state['property_receipts'][f.N]['held']['创建时间'],'property_unowned_value')
        repeated=self.run_plan(self.prepare());self.assertEqual(repeated['status'],'partial',repeated);self.assertEqual(repeated['retained_conflicts'],1)
    def test_typed_creation_readback_validates_all_fields(self):
        projected,_=m.project(source(),f.N,m.schema_projection(self.schema))
        self.f.api.pages[f.N]['properties_raw'].update(deepcopy(projected))
        view=e.target(self.f.api.pages[f.N],f.N,f.DS)
        row={'note_id':f.N,'rendered':view['semantic'],'properties':{**projected,'Title':self.f.api.pages[f.N]['properties_raw']['Title']}}
        self.assertEqual(e.created(self.f.api.pages[f.N],row,f.DS,view['page_id']),view['order'])
        self.f.api.pages[f.N]['properties_raw']['笔记类型']['select']['name']='wrong'
        with self.assertRaisesRegex(e.Blocked,'readback_mismatch'):e.created(self.f.api.pages[f.N],row,f.DS,view['page_id'])
    def test_persist_failure_before_inflight_prevents_write(self):
        plan=self.prepare();original=self.f.store.compare_and_set
        def fail(sha,value):
            op=value.get('journal',{}).get('operation') if value.get('journal') else None
            if op and op.get('phase')=='in_flight':raise RuntimeError('Synthetic checkpoint failure')
            return original(sha,value)
        with patch.object(self.f.store,'compare_and_set',side_effect=fail):
            result=self.run_plan(plan)
        self.assertEqual(result['reason'],'journal_failed');self.assertFalse(self.calls)

if __name__=='__main__':unittest.main()

class IntegrationTests(TransactionTests):
    def test_new_page_properties_are_part_of_same_approved_create(self):
        state,sha=self.f.store.read();state['receipts']={};self.f.store.compare_and_set(sha,state)
        del self.f.api.pages[f.N]
        state,sha=self.f.store.read()
        plan=e.prepare(self.publication,state,sha,{f.N:f.missing()},self.f.policy,
            property_manifest=self.manifest,expected_property_manifest_sha256=e.digest(self.manifest),property_schema=self.schema)
        self.assertEqual(set(plan['records'][0]['properties']),set(m.FIELDS)|{'笔记 ID','Title'})
        result=self.run_plan(plan);self.assertEqual(result['status'],'verified',result)
        saved=self.f.store.read()[0];self.assertEqual(set(saved['property_receipts'][f.N]['managed']),set(m.FIELDS))
        self.assertEqual(saved['property_receipts'][f.N]['md_blob'],saved['receipts'][f.N]['md_blob'])
    def create_with_missing_options(self, long_page=False):
        state,sha=self.f.store.read();state['receipts']={};self.f.store.compare_and_set(sha,state)
        del self.f.api.pages[f.N]
        self.schema['标签']['multi_select']['options']=[]
        self.schema['笔记类型']['select']['options']=[]
        self.manifest['schema_sha256']=e.digest(m.schema_projection(self.schema))
        state,sha=self.f.store.read()
        if long_page:
            import test_notion_create_batches as batches
            self.f.api=batches.API(self.f.store);self.f.api.schema=lambda:deepcopy(self.schema)
            with patch.object(e.p,'render_markdown',return_value=(batches.rendered(150,False),{},[])):
                plan=e.prepare(self.publication,state,sha,{f.N:f.missing()},self.f.policy,
                    property_manifest=self.manifest,expected_property_manifest_sha256=e.digest(self.manifest),property_schema=self.schema)
            self.assertIn('create_batches',plan['records'][0])
        else:
            plan=e.prepare(self.publication,state,sha,{f.N:f.missing()},self.f.policy,
                property_manifest=self.manifest,expected_property_manifest_sha256=e.digest(self.manifest),property_schema=self.schema)
            self.assertNotIn('create_batches',plan['records'][0])
        result=self.run_plan(plan)
        self.assertEqual(result['created'],1,result);self.assertEqual(result['status'],'partial',result)
        self.assertEqual(result['retained_conflicts'],1,result)
        state,_=self.f.store.read();self.assertIsNone(state['journal'])
        self.assertEqual(set(state['property_receipts'][f.N]['held']),{'标签','笔记类型'})
        self.assertTrue(state['receipts'][f.N]['managed_blocks'])
    def test_short_create_reports_missing_metadata_as_partial(self):
        self.create_with_missing_options()
    def test_batched_create_reports_missing_metadata_as_partial(self):
        self.create_with_missing_options(long_page=True)
    def test_new_page_schema_change_is_not_an_uncertain_create(self):
        state,sha=self.f.store.read();state['receipts']={};self.f.store.compare_and_set(sha,state)
        del self.f.api.pages[f.N];state,sha=self.f.store.read()
        plan=e.prepare(self.publication,state,sha,{f.N:f.missing()},self.f.policy,
            property_manifest=self.manifest,expected_property_manifest_sha256=e.digest(self.manifest),property_schema=self.schema)
        self.schema['笔记类型']['select']['options'].append({'id':'new','name':'new'})
        result=self.run_plan(plan);self.assertEqual(result['reason'],'property_schema_changed',result)
        self.assertEqual(result['write_calls'],0);self.assertNotIn(f.N,self.f.api.pages)
        self.assertEqual(self.f.store.read()[0]['journal']['operation']['phase'],'prepared')
    def bind_naive_registry(self, wrong_blob=False):
        old=self.publication.descriptor['commit'];data=source(created_at='2026-10-01 11:22:33',updated_at='2026-10-02 12:23:34')
        self.f.source_path.write_bytes(data)
        registry={'newById':{f.N:{'baseline_blob':'f'*40 if wrong_blob else e.p.git_blob(data),'mirror_sha256':e.digest(data),'filePath':'notes/a.md',
            'source_metadata':{'timestamps':{'created_at':'2026-10-01T11:22:33+08:00','edited_at':'2026-10-02T12:23:34+08:00','record_updated_at':'2026-10-19T00:00:00+08:00'},
            'attachments':[{'type':'audio','url':'PRIVATE SIGNED URL','duration':14000}], 'tags':[{'name':'existing','id':'opaque-tag-id','type':'ai'}]}}}}
        path=self.f.repo/m.REGISTRY;path.parent.mkdir(parents=True,exist_ok=True);raw=e.encoded(registry);path.write_bytes(raw)
        commit=self.f.commit('source with bound registry');self.publication=e.GitPublication(self.f.repo,commit,old,commit);self.f.publication=self.publication
        state,sha=self.f.store.read();state['receipts'][f.N]=e.receipt(self.f.api.pages[f.N],f.N,f.DS,self.f.owned,self.publication.descriptor,self.publication.descriptor['records'][f.N]);self.f.store.compare_and_set(sha,state)
        self.manifest['source_commit']=self.publication.descriptor['commit']
        self.manifest['records']={f.N:{**self.publication.descriptor['records'][f.N],'privacy_review_sha256':'c'*64}}
        self.manifest['registry']={'path':m.REGISTRY,'blob':e.p.git_blob(raw),'sha256':e.digest(raw)}
    def test_registry_timestamp_blob_binding_recovers_timezone_without_audio_export(self):
        self.bind_naive_registry();plan=self.prepare();row=plan['records'][0]
        self.assertTrue(row['property_patch']['更新时间']['date']['start'].endswith('+08:00'))
        self.assertNotIn('PRIVATE SIGNED URL',str(plan));self.assertNotIn('opaque-tag-id',str(row['property_patch']))
        result=self.run_plan(plan);self.assertEqual(result['status'],'verified',result)
        self.assertEqual(self.f.api.pages[f.N]['properties_raw']['更新时间']['date']['start'],'2026-10-02T12:23:34+08:00')
    def test_registry_wrong_md_binding_blocks_without_write(self):
        self.bind_naive_registry(wrong_blob=True)
        with self.assertRaisesRegex(e.Blocked,'property_source_invalid'):self.prepare()
        self.assertFalse(self.calls)
    def test_registry_change_after_plan_blocks_without_write(self):
        self.bind_naive_registry();plan=self.prepare();path=self.f.repo/m.REGISTRY;path.write_bytes(path.read_bytes()+b' ');self.f.commit('concurrent registry')
        result=self.run_plan(plan);self.assertEqual(result['reason'],'source_changed',result);self.assertFalse(self.calls)
    def test_property_result_does_not_dequeue_or_advance_pending_body_work(self):
        import get_notion_outbox as outbox
        from test_notion_outbox import Store
        outer=Store();state,_=self.f.store.read();outer.value['engine_state']=state
        origin={'commit':self.publication.descriptor['parent'],'receipt_sha256':None,'sequence':0}
        tip={'commit':self.publication.descriptor['commit'],'receipt_sha256':'b'*64,'sequence':1}
        outer.value.update(origin=origin,last_enqueued_publication=tip,observed_publication_tip=tip,policy_sha256='c'*64,
            pending_by_id={f.N:{**self.publication.descriptor['records'][f.N],**tip,'parent':origin['commit']}},rotation_order=[f.N])
        before=deepcopy(outer.value)
        self.f.store=outbox.EngineStore(outer);self.f.api.store=self.f.store
        plan=self.prepare();result=self.run_plan(plan);self.assertEqual(result['status'],'verified',result)
        self.assertEqual(outer.value['pending_by_id'],before['pending_by_id']);self.assertEqual(outer.value['rotation_order'],before['rotation_order'])
        self.assertEqual(outer.value['engine_state']['receipts'],before['engine_state']['receipts'])
        self.assertEqual(outer.value['last_enqueued_publication'],before['last_enqueued_publication'])
    def test_native_api_only_updates_read_page_and_approved_four_properties(self):
        import get_notion_live_transport as t
        from test_executor_live_adapter import DB
        page=self.f.api.pages[f.N]['page_id'];calls=[]
        def request(method,path,body):
            calls.append((method,path,deepcopy(body)))
            if path.startswith('/data_sources/'):
                return {'object':'data_source','id':f.DS,'parent':{'database_id':DB},'properties':{**self.schema,'笔记 ID':{'type':'rich_text'}}}
            return {'object':'page','id':page,'parent':{'data_source_id':f.DS}}
        fields,_=m.project(source(),f.N,m.schema_projection(self.schema))
        api=t.API(request,f.DS,[f.N],data_source_pin=t.native.sha(f.DS),database_pin=t.native.sha(DB))
        with self.assertRaisesRegex(t.Failure,'unread_target'):api.update_properties(page,fields)
        api.known_pages.add(page)
        with self.assertRaisesRegex(t.Failure,'write_not_authorized'):api.update_properties(page,fields)
        api.approved=frozenset({t.fingerprint('PATCH','/pages/'+page,{'properties':fields})})
        self.assertEqual(api.update_properties(page,fields),page)
        self.assertEqual(len([x for x in calls if x[0]=='PATCH']),1)
        with self.assertRaisesRegex(t.Failure,'write_not_authorized'):api.update_properties(page,fields)
        with self.assertRaisesRegex(t.Failure,'invalid_approval'):api.update_properties(page,{'Title':{'type':'title','title':[]}})

class AdoptionTests(TransactionTests):
    def test_exact_external_prior_writer_scope_can_adopt_one_generated_property(self):
        current={'type':'date','date':{'start':'2026-10-01'}}
        self.f.api.pages[f.N]['properties_raw']['创建时间']=current
        self.manifest['records'][f.N]['property_adoption']={'page_id':self.f.api.pages[f.N]['page_id'],
            'snapshot_sha256':e.target_sha(self.f.api.pages[f.N],f.N,f.DS),
            'prior_writer_proof_sha256':'d'*64,'fields':{'创建时间':deepcopy(current)}}
        plan=self.prepare();self.assertIn('创建时间',plan['records'][0]['property_patch'])
        out=self.run_plan(plan);self.assertEqual(out['status'],'verified',out)
    def test_fresh_observation_without_prior_writer_proof_cannot_adopt(self):
        self.manifest['records'][f.N]['property_adoption']={'page_id':self.f.api.pages[f.N]['page_id'],
            'snapshot_sha256':e.target_sha(self.f.api.pages[f.N],f.N,f.DS),'fields':{'创建时间':blank_properties()['创建时间']}}
        with self.assertRaisesRegex(e.Blocked,'missing_block_ownership'):self.prepare()
    def test_stale_adoption_snapshot_cannot_overwrite_manual_change(self):
        current={'type':'date','date':{'start':'2026-10-01'}}
        self.f.api.pages[f.N]['properties_raw']['创建时间']=deepcopy(current)
        self.manifest['records'][f.N]['property_adoption']={'page_id':self.f.api.pages[f.N]['page_id'],
            'snapshot_sha256':e.target_sha(self.f.api.pages[f.N],f.N,f.DS),
            'prior_writer_proof_sha256':'d'*64,'fields':{'创建时间':deepcopy(current)}}
        self.f.api.pages[f.N]['properties_raw']['创建时间']['date']['start']='2020-01-01'
        with self.assertRaisesRegex(e.Blocked,'missing_block_ownership'):self.prepare()

class LegacyTagTests(unittest.TestCase):
    def test_quoted_yaml_flow_sequence_preserves_names_with_commas(self):
        self.assertEqual(m.tag_names('[录音笔记, "has, comma", 工作记录]'),['录音笔记','has, comma','工作记录'])
    def test_no_generic_comma_split_or_coercion(self):
        for raw in ('one, two','[one, 123]','[one, null]','[one, true]','[one, 2026-01-01]','[[nested]]','[one, one]','{one: two}'):
            with self.subTest(raw=raw),self.assertRaisesRegex(ValueError,'property_source_invalid'):m.tag_names(raw)
    def test_aliases_anchors_tags_and_excessive_tokens_rejected(self):
        for raw in ('[&x one, *x]','[!!str one]','['+','.join('a'+str(i) for i in range(1000))+']'):
            with self.subTest(raw=raw),self.assertRaisesRegex(ValueError,'property_source_invalid'):m.tag_names(raw)
    def test_legacy_yaml_string_flows_into_native_multiselect(self):
        projection=m.schema_projection(schema());values,held=m.project(source(tags='[existing]'),f.N,projection)
        self.assertFalse(held);self.assertEqual(values['标签'],{'type':'multi_select','multi_select':[{'name':'existing'}]})
    def test_missing_yaml_dependency_is_explicit_safe_hold(self):
        import builtins
        original=builtins.__import__
        def unavailable(name,*args,**kwargs):
            if name=='yaml':raise ImportError('No parser')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=unavailable):
            values,held=m.project(source(tags='[existing]'),f.N,m.schema_projection(schema()))
        self.assertNotIn('标签',values);self.assertEqual(held['标签'],'property_parser_unavailable')
