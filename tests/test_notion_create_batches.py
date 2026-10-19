"""Synthetic-only interrupted long NEW-page creation. No network or secrets."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_executor as e
import get_notion_create_batches as b
import get_notion_live_transport as t
import get_notion_pipeline as pipeline
import test_notion_executor as f
from test_executor_live_adapter import OfficialFixture, DB
from test_notion_pipeline import FixtureTransport

U='6'*32
IMAGE={'kind':'notion_uploaded_png','upload_id':U,'sha256':'7'*64,
       'byte_count':30,'mime':'image/png','caption':[]}
HANDLES={U:{k:IMAGE[k] for k in ('sha256','byte_count','mime')}}


def rendered(count=268, image=True):
    rows=[f.paragraph('Synthetic paragraph %04d' % i) for i in range(count)]
    if count > 100:
        rows[20]=e.p.block('bulleted_list_item',{'rich_text':[e.p.text_run('Parent bullet')]},
                           [f.paragraph('First child'),f.paragraph('Second child')])
        rows[99]=e.p.block('quote',{'rich_text':[e.p.text_run('Quote')]},[f.paragraph('Nested quote')])
        for i in range(95,99):
            rows[i]=e.p.block('numbered_list_item',{'rich_text':[e.p.text_run('List item '+str(i))]})
        rows[95]['payload']['list_start_index']=1
        rows[95]['payload']['list_format']='numbers'
        if image:rows[3]=e.p.block('image',deepcopy(IMAGE))
        rows[130]['payload']['rich_text'][0]['annotations']['bold']=True
        rows[131]=e.p.block('code',{'rich_text':[e.p.text_run('print("synthetic")')],
                                   'caption':[],'language':'python'})
    return rows


class API(f.API):
    def before(self,kind):
        value,_=self.store.read();op=value['journal']['operation']
        assert op['kind']=='create_batches' and op['phase']=='in_flight'
        assert kind==('create' if op['batch_index']==0 else 'append')
        self.calls.append((kind,))
        if self.hook:self.hook(kind)
        if self.throw==kind+'_before':raise TimeoutError('synthetic transport loss')


class PagedOfficialFixture(OfficialFixture):
    def __call__(self,method,path,body=None):
        result=super().__call__(method,path,body)
        if method=='GET' and path.startswith('/blocks/') and result.get('object')=='list':
            start=int(parse_qs(urlsplit(path).query).get('start_cursor',['0'])[0])
            rows=result['results'];end=start+100
            result={'object':'list','results':rows[start:end],'has_more':end<len(rows),
                    'next_cursor':str(end) if end<len(rows) else None}
        return result


class PagedTransport(FixtureTransport):
    def __init__(self,api):
        super().__init__(api)
        self.fixture=PagedOfficialFixture(api)


class BudgetStore:
    """Synthetic in-memory durable codec with real per-invocation byte caps."""
    max_commits=400
    max_batch_bytes=192*1024*1024
    def __init__(self,value):
        self.value=deepcopy(value)
        self.outer={'engine_state':deepcopy(value),'synthetic_envelope':'x'*13000}
        self.commits_attempted=0;self.new_blob_bytes=0
    def read(self):return deepcopy(self.value),e.digest(self.value)
    def compare_and_set(self,expected,value):
        assert expected==e.digest(self.value)
        e.check_state(value)
        outer={**self.outer,'engine_state':deepcopy(value)}
        size=len(e.encoded(outer))
        assert size<=e.MAX_STATE
        assert self.commits_attempted+1<=self.max_commits
        assert self.new_blob_bytes+size<=self.max_batch_bytes
        self.commits_attempted+=1;self.new_blob_bytes+=size
        self.value=deepcopy(value);self.outer=outer
        return e.digest(value)


class Tests(unittest.TestCase):
    def fixture(self, count=268, image=True):
        fixture=f.Tests(methodName='test_real_git_update_journal_before_writes_preserves_unowned_and_properties')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        state,sha=fixture.store.read();state['receipts']={};fixture.store.compare_and_set(sha,state)
        fixture.api=API(fixture.store)
        fixture.publish('Synthetic long-page fixture')
        rows=rendered(count,image)
        class Assets:
            def handles(self):return deepcopy(HANDLES)
        bindings={'uploaded_png':{'path':fixture.source_path.relative_to(fixture.repo).as_posix(),
                                  'git_blob':fixture.publication.descriptor['records'][f.N]['blob']}} if image else {}
        with patch.object(e.p,'render_markdown',return_value=(deepcopy(rows),bindings,[])):
            plan=fixture.plan(reviewed_png=Assets() if image else None)
        self.assertEqual(plan['records'][0]['action'],'create',plan['records'][0].get('reason'))
        self.assertIn('create_batches',plan['records'][0])
        return fixture,plan,rows

    def pipeline_fixture(self):
        fixture,_,rows=self.fixture(image=False)
        policy={'contract':pipeline.CONTRACT,'rules_sha256':e.rules_sha256(),
                'data_source_id':f.DS,'data_source_pin':t.native.sha(f.DS),
                'database_pin':t.native.sha(DB),'title_property':'Title',
                'allow_apply':True,'allow_create':True,'allow_archive':True,
                'format_accepted':True,'initial_scope_sha256':None,
                'plan_authorization':'standing_bounded'}
        def run(request):
            with patch.object(e.p,'render_markdown',return_value=(deepcopy(rows),{},[])):
                return pipeline.run(fixture.publication,fixture.store,request,policy,e.digest(policy),
                                    apply=True,writers_guard=lambda:deepcopy(fixture.policy['old_writer_evidence']))
        return fixture,run

    def test_pipeline_official_wire_pagination_and_dynamic_exact_allowlist(self):
        fixture,run=self.pipeline_fixture();request=PagedTransport(fixture.api)
        result=run(request)
        self.assertEqual(result['status'],'executed',result)
        self.assertEqual(result['execution']['status'],'verified',result)
        self.assertEqual(result['execution']['created'],1)
        self.assertEqual(len(request.attempted),3)
        self.assertEqual(request.approved,request.attempted)
        self.assertTrue(any('start_cursor=' in path for method,path in request.fixture.calls))
        self.assertEqual(fixture.writes(),[('create',),('append',),('append',)])
        repeat=run(PagedTransport(fixture.api))
        self.assertEqual(repeat['selected'],0,repeat)

    def test_pipeline_unknown_append_resume_has_no_duplicate_create_or_append(self):
        fixture,run=self.pipeline_fixture();fixture.api.throw='append_after'
        first=run(PagedTransport(fixture.api))
        self.assertEqual(first['execution']['reason'],'uncertain_append',first)
        fixture.api.throw=None
        resumed=PagedTransport(fixture.api);result=run(resumed)
        self.assertEqual(result['execution']['status'],'verified',result)
        self.assertEqual(len(resumed.attempted),1)
        self.assertEqual(fixture.writes(),[('create',),('append',),('append',)])

    def test_full_268_roots_271_nodes_image_lists_order_and_ownership(self):
        fixture,plan,rows=self.fixture()
        row=plan['records'][0];spec=row['create_batches']
        self.assertEqual((len(rows),spec['total_blocks'],len(spec['batches'])),(268,271,3))
        for i,rng in enumerate(spec['batches']):
            wire,blocks=b.request(row,f.DS,i,'a'*32,'b'*32)
            self.assertLessEqual(b.node_count(blocks),100)
            self.assertLessEqual(len(e.encoded(wire['body'])),400*1024)
        self.assertEqual(fixture.run_plan(plan)['status'],'verified')
        self.assertEqual(fixture.writes(),[('create',),('append',),('append',)])
        view=e.target(fixture.api.pages[f.N],f.N,f.DS)
        self.assertEqual(e.p.comparison_semantics(view['semantic'],None),e.p.comparison_semantics(rows,None))
        state,_=fixture.store.read();receipt=state['receipts'][f.N]
        self.assertEqual(len(receipt['managed_blocks']),271)
        self.assertEqual(receipt['protected_blocks'],[])
        self.assertEqual(receipt['creation_plan_sha256'],e.digest(plan))
        self.assertIsNone(state['journal'])
        self.assertFalse(any(x[0]=='archive' for x in fixture.writes()))

    def test_no_compaction_large_2000_root_case(self):
        fixture,plan,rows=self.fixture(2000,False)
        self.assertEqual(len(plan['records'][0]['create_batches']['batches']),21)
        self.assertEqual(fixture.run_plan(plan)['status'],'verified')
        self.assertEqual(len(fixture.api.pages[f.N]['recursive_blocks']),2003)
        self.assertEqual(len(fixture.writes()),21)

    def test_planned_resource_and_unsplittable_subtree_limits(self):
        fixture,plan,rows=self.fixture(image=False)
        props=plan['records'][0]['properties']
        with self.assertRaisesRegex(e.Blocked,'request_budget'):
            b.prepare([e.p.block('quote',{'rich_text':[]},[f.paragraph('child') for _ in range(100)])],props,f.DS)
        with self.assertRaisesRegex(e.Blocked,'request_budget'):
            b.prepare([f.paragraph('x')]*3201,props,f.DS)
        deep=e.p.block('quote',{'rich_text':[]},[e.p.block('quote',{'rich_text':[]},[e.p.block('quote',{'rich_text':[]},[f.paragraph('too deep')])])])
        with self.assertRaisesRegex(e.Blocked,'request_budget'):
            b.prepare([deep],props,f.DS)
        dense=[e.p.block('bulleted_list_item',{'rich_text':[]},[f.paragraph('child')]) for _ in range(1000)]
        with self.assertRaisesRegex(e.Blocked,'request_budget'):
            b.prepare(dense,props,f.DS)

    def test_split_by_wire_envelope_bytes_without_splitting_roots(self):
        props={'笔记 ID':{'type':'rich_text','rich_text':[e.p.text_run(f.N)]}}
        rows=[f.paragraph('x'*16000) for _ in range(60)]
        spec=b.prepare(rows,props,f.DS)
        self.assertGreater(len(spec['batches']),1)
        row={'rendered':rows,'properties':props,'create_batches':spec}
        for i,_ in enumerate(spec['batches']):
            wire,blocks=b.request(row,f.DS,i,'a'*32,'b'*32)
            self.assertLessEqual(len(e.encoded(wire['body'])),400*1024)
            self.assertTrue(all(sum(len(run['text']['content']) for run in r['payload']['rich_text'])==16000 for r in blocks))

    def test_unknown_create_never_retried_or_adopted_without_recovery(self):
        for after in (False,True):
            with self.subTest(after=after):
                fixture,plan,_=self.fixture(image=False)
                fixture.api.throw='create_after' if after else 'create_before'
                self.assertEqual(fixture.run_plan(plan)['reason'],'uncertain_create')
                fixture.api.throw=None
                for _ in range(2):
                    self.assertEqual(fixture.run_plan(plan)['reason'],'uncertain_create')
                self.assertEqual(fixture.writes(),[('create',)])
                self.assertEqual(fixture.store.read()[0]['receipts'],{})
                if after:
                    recovery={'plan_sha256':e.digest(plan),'note_id':f.N,'page_id':fixture.api.pages[f.N]['page_id'],
                              'snapshot_sha256':e.target_sha(fixture.api.pages[f.N],f.N,f.DS)}
                    out=fixture.run_plan(plan,create_recovery=recovery,approved_recovery_sha=e.digest(recovery))
                    self.assertEqual(out['status'],'verified')
                    self.assertEqual(fixture.writes(),[('create',),('append',),('append',)])

    def test_unknown_append_exact_effect_recovers_without_duplicate(self):
        fixture,plan,_=self.fixture()
        fixture.api.throw='append_after'
        self.assertEqual(fixture.run_plan(plan)['reason'],'uncertain_append')
        state,_=fixture.store.read();op=state['journal']['operation']
        self.assertEqual(op['batch_index'],1)
        self.assertEqual(op['batches'][0]['phase'],'verified')
        self.assertEqual(op['batches'][1]['phase'],'in_flight')
        self.assertEqual(state['receipts'],{})
        fixture.api.throw=None
        self.assertEqual(fixture.run_plan(plan)['status'],'verified')
        self.assertEqual(fixture.writes(),[('create',),('append',),('append',)])

    def test_unknown_append_without_effect_never_retries(self):
        fixture,plan,_=self.fixture()
        fixture.api.throw='append_before'
        self.assertEqual(fixture.run_plan(plan)['reason'],'uncertain_append')
        fixture.api.throw=None
        for _ in range(2):self.assertEqual(fixture.run_plan(plan)['reason'],'uncertain_append')
        self.assertEqual(fixture.writes(),[('create',),('append',)])
        self.assertNotIn(f.N,fixture.store.read()[0]['receipts'])

    def test_manual_insert_edit_replace_or_property_change_stops_without_deletion(self):
        for change in ('insert','edit','replace','property','reorder'):
            with self.subTest(change=change):
                fixture,plan,_=self.fixture(image=False)
                with patch.object(b,'resources',side_effect=lambda api,store,value,row,index:index==0):
                    result=fixture.run_plan(plan)
                self.assertEqual(result['reason'],'batch_boundary_yield')
                page=fixture.api.pages[f.N];before=deepcopy(page)
                if change=='insert':
                    extra,_=fixture.api.rows([f.paragraph('Manual insertion')],page['page_id'])
                    page['recursive_blocks']+=extra
                elif change=='edit':page['recursive_blocks'][0]['payload']['rich_text']=[e.p.text_run('Manual edit')]
                elif change=='replace':page['recursive_blocks'][0]['id']=fixture.api.new_id()
                elif change=='property':page['properties_raw']['Manual']={'type':'number','number':3}
                else:page['recursive_blocks'][0],page['recursive_blocks'][1]=page['recursive_blocks'][1],page['recursive_blocks'][0]
                changed=deepcopy(page)
                result=fixture.run_plan(plan)
                self.assertEqual(result['status'],'blocked')
                self.assertIn(result['reason'],('human_edit_conflict','readback_mismatch'))
                self.assertEqual(page,changed)
                self.assertEqual(fixture.writes(),[('create',)])
                self.assertEqual(fixture.store.read()[0]['receipts'],{})

    def test_verified_boundary_yield_and_resume_preserve_exact_page_and_ids(self):
        fixture,plan,_=self.fixture()
        with patch.object(b,'resources',side_effect=lambda api,store,value,row,index:index==0):
            out=fixture.run_plan(plan)
        self.assertEqual(out['status'],'partial')
        self.assertEqual(out['reason'],'batch_boundary_yield')
        state,_=fixture.store.read();op=state['journal']['operation'];saved=deepcopy(op)
        self.assertEqual(op['phase'],'verified')
        self.assertEqual(state['failures'],{})
        self.assertEqual(state['receipts'],{})
        self.assertEqual(op['page_id'],fixture.api.pages[f.N]['page_id'])
        self.assertEqual(fixture.run_plan(plan)['status'],'verified')
        receipt=fixture.store.read()[0]['receipts'][f.N]
        self.assertEqual(receipt['page_id'],saved['page_id'])
        self.assertEqual(receipt['managed_blocks'][:len(saved['batches'][0]['blocks'])],saved['batches'][0]['blocks'])
        self.assertEqual(fixture.writes(),[('create',),('append',),('append',)])

    def test_crashes_before_or_after_every_checkpoint_never_repeat_writes(self):
        # 1 plan, 12 batch phases, 1 receipt and 1 journal clear = 15 CAS.
        for point in range(1,16):
            for persisted in (False,True):
                with self.subTest(point=point,persisted=persisted):
                    fixture,plan,_=self.fixture(image=False)
                    original=fixture.store.compare_and_set;number=[0]
                    def crash(expected,value):
                        number[0]+=1
                        if number[0]==point and not persisted:raise RuntimeError('synthetic crash')
                        result=original(expected,value)
                        if number[0]==point:raise RuntimeError('synthetic crash')
                        return result
                    fixture.store.compare_and_set=crash
                    fixture.run_plan(plan)
                    fixture.store.compare_and_set=original
                    state,_=fixture.store.read()
                    if state['journal'] is None and f.N in state['receipts']:
                        continue
                    op=state['journal']['operation'] if state['journal'] else None
                    recovery={}
                    if op and op['batch_index']==0 and op['phase']=='in_flight' and fixture.api.pages:
                        recovery={'plan_sha256':e.digest(plan),'note_id':f.N,'page_id':fixture.api.pages[f.N]['page_id'],
                                  'snapshot_sha256':e.target_sha(fixture.api.pages[f.N],f.N,f.DS)}
                    result=fixture.run_plan(plan,create_recovery=recovery or None,approved_recovery_sha=e.digest(recovery) if recovery else None)
                    if result['status']=='blocked':
                        self.assertIn(result['reason'],('uncertain_create','uncertain_append'))
                    else:self.assertEqual(result['status'],'verified')
                    self.assertLessEqual(fixture.writes().count(('create',)),1)
                    self.assertLessEqual(fixture.writes().count(('append',)),2)
                    self.assertFalse(any(x[0]=='archive' for x in fixture.writes()))

    def test_not_dispatched_rearms_only_matching_exact_current_batch(self):
        for wrong in (False,True):
            fixture,plan,_=self.fixture(image=False)
            original=fixture.api.append_blocks
            def stop(page,blocks,after):
                state,_=fixture.store.read();op=state['journal']['operation']
                wire,_=b.request(plan['records'][0],f.DS,op['batch_index'],page,after)
                key=t.fingerprint(wire['method'],wire['path'],wire['body'])
                raise t.NotDispatched('0'*64 if wrong else key)
            fixture.api.append_blocks=stop
            result=fixture.run_plan(plan)
            self.assertEqual(result['reason'],'uncertain_append' if wrong else 'request_not_dispatched')
            fixture.api.append_blocks=original
            result=fixture.run_plan(plan)
            self.assertEqual(result['reason'],'uncertain_append' if wrong else None)
            self.assertEqual(fixture.writes(),[('create',)] if wrong else [('create',),('append',),('append',)])

    def test_schema_rejects_missing_phase_duplicate_ids_or_rebound_page(self):
        fixture,plan,_=self.fixture(image=False)
        with patch.object(b,'resources',side_effect=lambda api,store,value,row,index:index==0):fixture.run_plan(plan)
        state,_=fixture.store.read()
        for mutate in (lambda op:op.update(page_id='9'*32),
                       lambda op:op['batches'][0].pop('prefix_sha256'),
                       lambda op:op['batches'][0]['blocks'].append(op['batches'][0]['blocks'][0]),
                       lambda op:op.update(phase='prepared')):
            changed=deepcopy(state);mutate(changed['journal']['operation'])
            with self.assertRaises(e.Blocked):e.check_state(changed)

    def test_budget_yield_does_not_start_new_write(self):
        fixture,plan,_=self.fixture(image=False)
        fake=t.Transport('synthetic',clock=lambda:1100,sleep=lambda _:None)
        fake.deadline=1200
        fixture.api.request=fake
        out=fixture.run_plan(plan)
        self.assertEqual(out['reason'],'batch_boundary_yield')
        self.assertEqual(fixture.writes(),[])
        self.assertEqual(out['write_state'],'not_attempted')
        fake.deadline=3000
        self.assertEqual(fixture.run_plan(plan)['status'],'verified')

    def test_sequential_receipt_growth_all_four_pages_with_real_byte_boundary_yields(self):
        # Synthetic publications, source text and IDs. Real engine/state codecs,
        # prefix verification and byte admission run without a network or Git.
        fixture,template,_=self.fixture(image=False)
        store=BudgetStore(e.initial_state());api=API(store)
        totals=[];old_second_bound=None
        for number,count in enumerate((2012,2268,182,268)):
            ident=str(8000000000000000000+number)
            raw=f.md(ident,'Synthetic complete recording '+str(number))
            desc=deepcopy(template['publication'])
            source={'path':'Raw/03 Get/notes/synthetic-'+str(number)+'.md',
                    'blob':e.p.git_blob(raw),'sha256':e.digest(raw)}
            desc.update(records={ident:source},changed_content_ids=[ident],
                        commit=('%040x'%(number+1)),initial_cohort=None)
            class Publication:
                descriptor=desc
                def check(self,other):assert other==self.descriptor
                def check_assets(self,assets):assert assets=={}
                def read(self,note):assert note==ident;return raw
            publication=Publication()
            rows=rendered(count,False)
            for root in rows:
                if root['type']=='paragraph':
                    root['payload']['rich_text']=[e.p.text_run('Synthetic complete text '*20)]
            state,sha=store.read();previous=deepcopy(state['receipts'])
            with patch.object(e.p,'render_markdown',return_value=(rows,{},[])):
                plan=e.prepare(publication,state,sha,{ident:f.missing()},fixture.policy)
            row=plan['records'][0]
            self.assertEqual(row['action'],'create',row)
            planned=deepcopy(state)
            planned['journal']={'plan_sha256':e.digest(plan),'plan':plan,'index':0,'operation':None}
            if number==1:
                old_second_bound=(len(e.encoded(planned))+row['create_batches']['total_blocks']*256
                    +len(row['create_batches']['batches'])*1024)*(4*len(row['create_batches']['batches'])+3)
                self.assertGreater(old_second_bound,b.MAX_CHECKPOINT_BYTES)
            self.assertLess(b.checkpoint_bound(planned,row,store),b.MAX_CHECKPOINT_BYTES)
            before=len(api.calls);yields=0
            for invocation in range(len(row['create_batches']['batches'])+3):
                current,_=store.read()
                # Model other work already charged this invocation. Leave only
                # eight checkpoint slots; the hard 192 MiB cap is unchanged.
                size=b.checkpoint_size(planned if current['journal'] is None else current,row,store)
                store.new_blob_bytes=store.max_batch_bytes-8*size
                store.commits_attempted=0
                result=e.execute(plan,e.digest(plan),publication,api,store,
                    expected_store_sha=store.read()[1],writers_guard=lambda:deepcopy(fixture.policy['old_writer_evidence']))
                self.assertLessEqual(store.new_blob_bytes,192*1024*1024)
                state,_=store.read()
                for key,receipt in previous.items():self.assertEqual(state['receipts'][key],receipt)
                if result['status']=='verified':break
                self.assertEqual(result['reason'],'batch_boundary_yield',result)
                self.assertEqual(state['journal']['operation']['phase'],'verified')
                self.assertNotIn(ident,state['receipts']);self.assertEqual(state['failures'],{})
                yields+=1
            self.assertEqual(result['status'],'verified',result)
            self.assertGreater(yields,0)
            writes=[call for call in api.calls[before:] if call[0]!='read']
            self.assertEqual(writes.count(('create',)),1)
            self.assertEqual(writes.count(('append',)),len(row['create_batches']['batches'])-1)
            self.assertEqual(len(state['receipts'][ident]['managed_blocks']),b.node_count(rows))
            totals.append(len(state['receipts']))
        self.assertEqual(totals,[1,2,3,4])

    def test_impossible_single_checkpoint_rejected_before_create(self):
        fixture,plan,_=self.fixture(image=False)
        fixture.store=BudgetStore(fixture.store.read()[0]);fixture.api.store=fixture.store
        fixture.store.outer['synthetic_envelope']='x'*e.MAX_STATE
        result=fixture.run_plan(plan)
        # The hard checkpoint codec refuses the initial plan itself.
        self.assertEqual(result['status'],'blocked');self.assertEqual(fixture.writes(),[])
        value=fixture.store.read()[0];row=plan['records'][0]
        self.assertGreater(b.checkpoint_size(value,row,fixture.store),e.MAX_STATE)
        with self.assertRaisesRegex(e.Blocked,'request_budget'):
            b.resources(fixture.api,fixture.store,value,row,0)

    def test_outer_envelope_and_operation_growth_counted_once(self):
        fixture,plan,_=self.fixture(image=False)
        state=fixture.store.read()[0]
        state['journal']={'plan_sha256':e.digest(plan),'plan':plan,'index':0,'operation':None}
        store=BudgetStore(state);row=plan['records'][0]
        size=b.checkpoint_size(state,row,store)
        self.assertEqual(size-b.checkpoint_size(state,row),len(e.encoded(store.outer))-len(e.encoded(state)))
        state['journal']['operation']={'kind':'create_batches','synthetic_history':'x'*30000}
        # Replacing existing history with the full-history reserve must not
        # add it a second time as more verified batches are retained.
        self.assertEqual(b.checkpoint_size(state,row,store),size-4)
        self.assertEqual(b.checkpoint_bound(state,row,store),6*b.checkpoint_size(state,row,store))

    def test_png_budget_yields_verified_then_finishes_with_fresh_budget(self):
        fixture,plan,_=self.fixture()
        class Getter:
            downloads=0;response_bytes=0;elapsed=0
        class Adapter:
            asset_get=Getter()
        fixture.api.snapshot_adapter=Adapter()
        original=fixture.api.read_note
        def read(note):
            value=original(note)
            if value.get('exact_id_matches'):
                Adapter.asset_get.downloads+=3
                Adapter.asset_get.response_bytes+=3*IMAGE['byte_count']
            return value
        fixture.api.read_note=read
        for _ in range(4):
            result=fixture.run_plan(plan)
            if result['status']=='verified':break
            self.assertEqual(result['reason'],'batch_boundary_yield',result)
            state,_=fixture.store.read()
            self.assertEqual(state['journal']['operation']['phase'],'verified')
            self.assertEqual(state['receipts'],{})
            self.assertLessEqual(Adapter.asset_get.downloads,12)
            Adapter.asset_get.downloads=0;Adapter.asset_get.response_bytes=0
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(fixture.writes(),[('create',),('append',),('append',)])

    def test_dynamic_authorizer_rejects_payload_or_page_forgery(self):
        fixture,plan,_=self.fixture(image=False)
        with patch.object(b,'resources',side_effect=lambda api,store,value,row,index:index==0):fixture.run_plan(plan)
        state,sha=fixture.store.read();op=state['journal']['operation']
        op.update(batch_index=1,phase='in_flight');next_batch=b.progress();next_batch['phase']='in_flight';op['batches'].append(next_batch)
        fixture.store.compare_and_set(sha,state)
        request=t.Transport('synthetic',clock=lambda:0,sleep=lambda _:None)
        api=t.API(request,f.DS,[f.N],data_source_pin=t.native.sha(f.DS),database_pin=t.native.sha(DB))
        row=plan['records'][0];wire,_=b.request(row,f.DS,1,op['page_id'],op['batches'][0]['roots'][-1])
        authorize=pipeline.batch_authorizer(plan,api,request,fixture.store)
        forged=deepcopy(wire);forged['body']['children'][0]['numbered_list_item']['rich_text'][0]['text']['content']='Forged content'
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):authorize(row,op,forged)
        self.assertEqual(request.approved,frozenset())
        forged=deepcopy(wire);forged['path']='/blocks/'+'9'*32+'/children'
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):authorize(row,op,forged)
        authorize(row,op,wire)
        key=t.fingerprint(wire['method'],wire['path'],wire['body'])
        self.assertEqual(request.approved,frozenset([key]))
        self.assertEqual(api.approved,request.approved)
        request.attempted.add(key)
        with self.assertRaisesRegex(e.Blocked,'approval_missing'):authorize(row,op,wire)

    def test_only_one_large_page_selected_rest_remain_deferred(self):
        fixture,_,rows=self.fixture(image=False)
        fixture.parent=fixture.publication.descriptor['commit']
        fixture.source_path.write_bytes(f.md(f.N,'changed'))
        second=fixture.source_path.parent/'b.md';second.write_bytes(f.md(f.N2,'second'))
        commit=fixture.commit('two new pages')
        fixture.publication=e.GitPublication(fixture.repo,commit,fixture.parent,commit)
        with patch.object(e.p,'render_markdown',return_value=(deepcopy(rows),{},[])):
            plan=fixture.plan()
        self.assertEqual([r['note_id'] for r in plan['records']],[f.N])
        self.assertEqual(plan['deferred_ids'],[f.N2])
        self.assertEqual(fixture.run_plan(plan)['created'],1)
        self.assertNotIn(f.N2,fixture.store.read()[0]['failures'])


if __name__=='__main__':unittest.main()
