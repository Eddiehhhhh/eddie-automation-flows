"""Self-contained synthetic admission tests. No private fixture imports or IO."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import get_notion_single_create_admission as admission

def encoded(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()
class Blocked(ValueError):pass
def fail(reason):raise Blocked(reason)
def record_creation(state,row,plan):
    state.setdefault('property_receipts',{})[row['note_id']]={'page_id':state['receipts'][row['note_id']]['page_id'],
        'managed':deepcopy(row['properties']),'manifest_sha256':plan['property_manifest_sha256'],'readback_verified':True}
def check_state(state):
    if not isinstance(state,dict) or not {'journal','receipts','failures'}<=set(state):fail('state_invalid')
ENGINE=SimpleNamespace(encoded=encoded,digest=digest,same=lambda a,b:a==b,check_state=check_state,
    fail=fail,Blocked=Blocked,MAX_WRITE_BLOCKS=100,p=SimpleNamespace(RENDERER='synthetic-renderer'),
    metadata_pending=SimpleNamespace(enabled=lambda p:p.get('metadata_completion',False)),
    property_transaction=SimpleNamespace(record_creation=record_creation))
NATIVE=SimpleNamespace(MAX_BYTES=16*1024*1024)

def fixture(n=2,pad=0):
    plan={'historical_only':True,'property_manifest_sha256':'a'*64,'publication':{'commit':'b'*40},
        'policy':{'data_source_id':'c'*32},'records':[]}
    for i in range(n):
        plan['records'].append({'note_id':'synthetic-'+str(i),'source':{'blob':'d'*40},'action':'create',
            'property_created':['Title'],'properties':{'Title':'synthetic'},
            'rendered':[{'type':'paragraph','payload':{},'children':[]}]})
    outer={'engine_state':{'receipts':{},'property_receipts':{},'failures':{},'journal':None},
        'synthetic_capture':'x'*pad,'pending':['unchanged'],'history':['unchanged']}
    return outer,plan

def prefix(outer,plan,index=1):
    state=outer['engine_state'];state['journal']={'plan':deepcopy(plan),'plan_sha256':digest(plan),'index':index,'operation':None}
    for row in plan['records'][:index]:
        ident=row['note_id'];state['receipts'][ident]={'note_id':ident,'page_id':'e'*32,
            'source_commit':plan['publication']['commit'],'md_blob':row['source']['blob'],
            'data_source_id':plan['policy']['data_source_id'],'creation_plan_sha256':digest(plan),
            'readback_verified':True,'renderer_version':ENGINE.p.RENDERER}
        record_creation(state,row,plan)
    return {'status':'partial','reason':'batch_boundary_yield','state_sha256':digest(state)}

class Store:
    def __init__(self,outer):self.outer=deepcopy(outer);self.calls=0;self.mode=None
    def read(self):
        state=deepcopy(self.outer['engine_state']);return state,digest(state)
    def compare_and_set(self,version,value):
        self.calls+=1
        if version!=digest(self.outer['engine_state']):fail('state_changed')
        if self.mode=='before':fail('state_changed')
        self.outer['engine_state']=deepcopy(value)
        if self.mode=='after':fail('journal_failed')
        return digest(value)

class Tests(unittest.TestCase):
    def admission(self,**kw):
        d=SimpleNamespace(poisoned=False,new_blob_bytes=0,max_batch_bytes=192*1024*1024,commits_attempted=0,max_commits=128)
        t=SimpleNamespace(failed=False,sequence=0)
        for k,v in kw.items():setattr(d,k,v)
        return admission.Admission(ENGINE,NATIVE,d,t,max_checkpoints=1024,max_state=32*1024*1024)
    def test_exact_three_small_two_large_and_initial_plan(self):
        outer,plan=fixture();b=admission.reserve(ENGINE,NATIVE,outer,plan)
        self.assertEqual(b['commits'],6);self.assertEqual(b['checkpoints'],7)
        self.assertEqual(b['bytes'],sum(b['stages'].values()))
        self.assertLess(b['stages']['ack_or_restore_or_failure'],4096)
        self.assertGreater(b['stages']['receipt'],NATIVE.MAX_BYTES)
        prefix(outer,plan);b2=admission.reserve(ENGINE,NATIVE,outer,plan)
        self.assertNotIn('plan',b2['stages']);self.assertEqual(b2['commits'],5)
    def test_current_sized_tenth_row_is_not_blanket_rejected(self):
        outer,plan=fixture(10,pad=3064787);prefix(outer,plan,index=9)
        a=self.admission(new_blob_bytes=120*1024*1024,commits_attempted=37)
        self.assertTrue(a(outer,plan))
    def test_large_growing_state_yields_before_next_create(self):
        outer,plan=fixture(10,pad=11*1024*1024);prefix(outer,plan,index=3)
        a=self.admission(new_blob_bytes=13*11*1024*1024,commits_attempted=13)
        self.assertFalse(a(outer,plan))
    def test_three_slots_cannot_admit_a_create(self):
        outer,plan=fixture();self.assertFalse(self.admission(max_commits=3)(outer,plan))
    def test_one_byte_short_and_per_state_limit(self):
        outer,plan=fixture();a=self.admission();b=admission.reserve(ENGINE,NATIVE,outer,plan)
        a.durable.max_batch_bytes=b['bytes']-1;self.assertFalse(a(outer,plan))
        a.durable.max_batch_bytes=b['bytes'];self.assertTrue(a(outer,plan))
        a.max_state=b['max_state']-1;before=deepcopy(outer)
        with self.assertRaisesRegex(Blocked,'^request_budget$'):a(outer,plan)
        self.assertEqual(outer,before);self.assertEqual(a.durable.commits_attempted,0)
        self.assertFalse(a.clean_boundary_denied)
    def test_checkpoint_limit_and_missing_final_refresh(self):
        outer,plan=fixture();a=self.admission();a.emitter.sequence=1020;self.assertFalse(a(outer,plan))
        a.emitter.sequence=1024
        with self.assertRaises(Blocked):a(outer,plan)
    def test_unknown_operation_and_mismatched_plan_fail_closed(self):
        for phase in ('prepared','in_flight','acknowledged'):
            outer,plan=fixture();prefix(outer,plan);outer['engine_state']['journal']['operation']={'phase':phase}
            with self.assertRaises(Blocked):self.admission()(outer,plan)
        outer,plan=fixture();prefix(outer,plan);plan['publication']['commit']='f'*40
        with self.assertRaises(Blocked):self.admission()(outer,plan)
    def test_prepared_resume_is_reserved_without_mutation(self):
        outer,plan=fixture();prefix(outer,plan,index=0)
        outer['engine_state']['journal']['operation']={'kind':'create','phase':'prepared','response':None}
        before=deepcopy(outer)
        self.assertFalse(self.admission(max_commits=3).prepared_resume(outer,plan))
        self.assertTrue(self.admission().prepared_resume(outer,plan))
        self.assertEqual(outer,before)
    def test_prepared_actual_large_before_is_not_discarded_from_bound(self):
        outer,plan=fixture();prefix(outer,plan,index=0)
        outer['engine_state']['journal']['operation']={'kind':'create','phase':'prepared','response':None,
            'before':{'synthetic_extra':'x'*(20*1024*1024)},'archive_index':0}
        before=deepcopy(outer);a=self.admission(max_batch_bytes=80*1024*1024)
        self.assertFalse(a.prepared_resume(outer,plan));self.assertEqual(outer,before)
        self.assertFalse(a.clean_boundary_denied)
    def test_unknown_resume_is_not_cleared_or_reauthorized(self):
        for phase in ('in_flight','acknowledged'):
            outer,plan=fixture();result=prefix(outer,plan)
            outer['engine_state']['journal']['operation']={'kind':'create','phase':phase,'response':None}
            before=deepcopy(outer);a=self.admission(max_commits=0)
            self.assertTrue(a.prepared_resume(outer,plan))
            self.assertEqual(outer,before)
            result['state_sha256']=digest(outer['engine_state']);store=Store(outer)
            with self.assertRaises(Blocked):a.settle(store,plan,result)
            self.assertEqual(store.calls,0)
    def test_existing_other_row_routes_keep_original_admission(self):
        for key,value in (('action','update'),('create_batches',{'synthetic':True}),('property_only',True)):
            outer,plan=fixture();plan['records'][0][key]=value
            a=self.admission(max_commits=0)
            self.assertTrue(a(outer,plan));self.assertFalse(a.clean_boundary_denied)
    def test_locator_partial_yield_keeps_actual_active_journal(self):
        outer,plan=fixture();result=prefix(outer,plan);store=Store(outer)
        a=self.admission(locator=object());before=deepcopy(outer)
        self.assertEqual(a.settle(store,plan,result),result)
        self.assertEqual(store.calls,0);self.assertEqual(store.outer,before)
    def test_locator_yield_never_accepts_completed_or_unknown(self):
        for change in ({'index':2},{'operation':{'phase':'in_flight'}}):
            outer,plan=fixture();result=prefix(outer,plan)
            outer['engine_state']['journal'].update(change)
            result['state_sha256']=digest(outer['engine_state']);store=Store(outer)
            with self.assertRaises(Blocked):self.admission(locator=object()).settle(store,plan,result)
            self.assertEqual(store.calls,0);self.assertEqual(store.outer,outer)
    def test_locator_stale_yield_cannot_clear_or_rebind(self):
        outer,plan=fixture();result=prefix(outer,plan);store=Store(outer)
        result['state_sha256']='f'*64
        with self.assertRaises(Blocked):self.admission(locator=object()).settle(store,plan,result)
        self.assertEqual(store.calls,0);self.assertEqual(store.outer,outer)
    def test_settle_changes_only_journal(self):
        outer,plan=fixture();result=prefix(outer,plan);before=deepcopy(outer);store=Store(outer)
        out=self.admission().settle(store,plan,result);before['engine_state']['journal']=None
        self.assertEqual(store.outer,before);self.assertEqual(store.calls,1)
        self.assertEqual(out['state_sha256'],digest(before['engine_state']))
    def test_mixed_completed_prefix_retains_original_journal(self):
        for action in ('blocked','noop','update'):
            outer,plan=fixture();plan['records'][0]['action']=action
            result=prefix(outer,plan);store=Store(outer)
            out=self.admission().settle(store,plan,result)
            self.assertEqual(out,result);self.assertEqual(store.calls,0);self.assertEqual(store.outer,outer)
    def test_missing_or_altered_paired_receipt_never_clears(self):
        for kind in ('absent','altered'):
            outer,plan=fixture();result=prefix(outer,plan);ident=plan['records'][0]['note_id']
            if kind=='absent':outer['engine_state']['property_receipts'].pop(ident)
            else:outer['engine_state']['property_receipts'][ident]['managed']={'Title':'changed'}
            result['state_sha256']=digest(outer['engine_state']);store=Store(outer)
            with self.assertRaises(Blocked):self.admission().settle(store,plan,result)
            self.assertEqual(store.calls,0);self.assertEqual(store.outer,outer)
    def test_pending_operation_or_unconfirmed_body_never_clears(self):
        for kind in ('in_flight','acknowledged','body'):
            outer,plan=fixture();result=prefix(outer,plan)
            if kind=='body':outer['engine_state']['receipts'][plan['records'][0]['note_id']]['readback_verified']=False
            else:outer['engine_state']['journal']['operation']={'phase':kind}
            result['state_sha256']=digest(outer['engine_state']);store=Store(outer)
            with self.assertRaises(Blocked):self.admission().settle(store,plan,result)
            self.assertEqual(store.calls,0)
    def test_poison_and_failed_emitter_do_not_touch_store(self):
        for kind in ('poison','emitter'):
            outer,plan=fixture();result=prefix(outer,plan);store=Store(outer);a=self.admission()
            if kind=='poison':a.durable.poisoned=True
            else:a.emitter.failed=True
            with self.assertRaises(Blocked):a.settle(store,plan,result)
            self.assertEqual(store.calls,0)
    def test_failed_or_unknown_clear_is_never_retried(self):
        for mode in ('before','after'):
            outer,plan=fixture();result=prefix(outer,plan);store=Store(outer);store.mode=mode
            with self.assertRaises(Blocked):self.admission().settle(store,plan,result)
            self.assertEqual(store.calls,1)
    def test_clean_initial_yield_does_not_add_cas(self):
        outer,plan=fixture();store=Store(outer);result={'status':'partial','reason':'batch_boundary_yield','state_sha256':digest(outer['engine_state'])}
        self.assertEqual(self.admission().settle(store,plan,result),result);self.assertEqual(store.calls,0)

if __name__=='__main__':unittest.main()
