"""Public synthetic tests for the exact acknowledged CREATE capability.

AST adapters execute unchanged production functions/slices with explicit small
codec, provenance and engine adapters. These are NOT full-module authentication,
Git-history, native-API or runtime E2E tests. No private capsule, actual run/page
identity, private note content, credential, repository fixture or network is used.
"""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
def encoded(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(value if isinstance(value,bytes) else encoded(value)).hexdigest()
def oid(raw):return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
class Blocked(ValueError):pass
def fail(reason):raise Blocked(reason)
def checked(value,width):
    if not isinstance(value,str) or re.fullmatch('[0-9a-f]{'+str(width)+'}',value) is None:fail('invalid_value')
    return value

def extract(filename,names,namespace):
    tree=ast.parse((SCRIPTS/filename).read_text())
    selected=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names
        or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets)]
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(SCRIPTS/filename),'exec'),namespace)
    return namespace

def module(name,**values):
    m=ModuleType(name);m.__dict__.update(values);return m

def record_creation(state,row,plan):
    state.setdefault('property_receipts',{})[row['note_id']]={'page_id':state['receipts'][row['note_id']]['page_id'],
        'manifest_sha256':plan['property_manifest_sha256'],'readback_verified':True}

def check_state(value):
    if not isinstance(value,dict):fail('state_invalid')

def fixture():
    note='1000000000000000001';page='1'*32
    manifest={'contract':'synthetic-manifest','data_source_id':'2'*32}
    source={'path':'synthetic/note.md','blob':'3'*40,'sha256':'4'*64}
    rows=[{'note_id':note,'action':'create','source':source,'request_blocks':[],
           'property_created':['Title'],'preserved_asset_bindings':{}},
          {'note_id':'1000000000000000002','action':'create','source':deepcopy(source),
           'request_blocks':[],'property_created':['Title'],'preserved_asset_bindings':{}}]
    manifest['records']={r['note_id']:{'mode':'new_numeric'} for r in rows}
    manifest['source_commit']='5'*40
    plan={'historical_only':True,'historical_manifest':manifest,'historical_manifest_sha256':digest(manifest),
        'publication':{'commit':'5'*40},'policy':{'data_source_id':'2'*32},'records':rows,
        'property_manifest_sha256':'6'*64}
    op={'kind':'create','phase':'in_flight','response':None,'archive_index':0,
        'before':{'complete':True,'native_blocks_available':True,'identity_query_complete':True,'exact_id_matches':[]}}
    state={'policy_sha256':'7'*64,'preserved':['sentinel'],
        'engine_state':{'journal':{'plan_sha256':digest(plan),'plan':plan,'index':0,'operation':op},
        'receipts':{},'property_receipts':{},'failures':{},'unresolved_by_id':{}}}
    ack=deepcopy(state);ack['engine_state']['journal']['operation'].update(phase='acknowledged',response=page)
    policy={'code_sha':'8'*40,'rules_sha256':'8'*64,'get_policy_sha256':'7'*64}
    run={'id':77,'run_attempt':1,'event':'workflow_dispatch','status':'completed','conclusion':'failure','head_sha':'9'*40}
    ctx={'run_id':77,'run_attempt':1,'code_sha':policy['code_sha'],'controller_sha':run['head_sha'],
        'rules_sha256':policy['rules_sha256'],'notion_policy_sha256':'a'*64,'public_policy_sha256':digest(policy)}
    anchor={'commit':'b'*40,'state_blob':oid(encoded(state)),'state_sha256':digest(state)}
    cp={**ctx,**anchor,'contract':'get-notion-journal-checkpoint-1','status':'journal_readback_verified',
        'sequence':1,'previous_record_sha256':None,'parent_commit':'c'*40,'tree':'d'*40,
        'state_bytes':len(encoded(state)),'cas_attempts':1,'readback_verified':True}
    cp['record_sha256']=digest(cp)
    proof={'status':'journal_provenance_verified','run_id':77,'run_attempt':1,'run_binding':run,'latest_binding':deepcopy(run),
        'anchor':anchor,'checkpoint':cp,'checkpoint_count':1,'get_step_record':None,'get_run_success_claim':False,
        'private_readback_required':True,'terminal_conclusions':{'job':'failure','notion':'failure','get':'skipped'},
        'terminal_report':{'contract':'get-private-runtime-report-2','status':'failed','write_state':'unknown',
            'reason':'journal_failed','business_health_asserted':False,'complete_library_claimed':False},
        'fixed_source_record':{'mode':'historical-apply','selection_sha256':digest(manifest)}}
    binding={'proof':proof,'public_policy':policy,'public_policy_sha256':digest(policy),'notion_policy_sha256':'a'*64,
        'plan_sha256':digest(plan),'manifest_sha256':digest(manifest),'source_commit':'5'*40,
        'initial_index':0,'operation_sha256':digest(op),'note_id':note,'page_id':page,'source':source,
        'request_sha256':None,'record_count':2,'selection_sha256':digest(manifest),
        'acknowledged_successor':{'contract':'get-notion-exact-acknowledged-create-receipt-1',
            'anchor':{'commit':'e'*40,'state_blob':oid(encoded(ack)),'state_sha256':digest(ack)},
            'tree':'f'*40,'state_bytes':len(encoded(ack)),
            'operation_sha256':digest(ack['engine_state']['journal']['operation'])}}
    return state,ack,binding

class Adapters(unittest.TestCase):
    def setUp(self):
        self.before,self.ack,self.binding=fixture();self.entries=[self.binding];self.trace=[]
        self.tx=module('get_notion_property_transaction',record_creation=record_creation,
            creation_wire_properties=lambda row:{},creation_readback_properties=lambda row,page:{})
        self.transport=module('get_notion_live_transport',fingerprint=lambda method,path,body:digest({'method':method,'path':path,'body':body}),
            wire_blocks=lambda blocks,previous,uploaded_pngs=None:deepcopy(blocks))
        self.engine=module('get_notion_executor',digest=digest,encoded=encoded,same=lambda a,b:a==b,check_state=check_state,
            fail=fail,Blocked=Blocked,MAX_RECORDS=50,metadata_pending=NS(enabled=lambda plan:False),
            p=NS(hex_hash=checked,note_id=lambda s:checked(s,19),notion_id=lambda s:checked(s,32),git_blob=oid,RENDERER='synthetic'),
            property_transaction=self.tx,transport=self.transport,
            target=lambda snapshot,note,ds:None if snapshot.get('exact_id_matches')==[] else snapshot,
            target_sha=lambda snapshot,note,ds:digest(snapshot))
        self.codec=module('get_notion_outbox',encoded=encoded,digest=digest,check_state=check_state,e=self.engine,fail=fail)
        self.provenance=NS(validate_policy=lambda p,h:p if digest(p)==h else fail('policy'),run_binding=deepcopy,MAX_TOTAL=1000000)
        self.checkpoints=module('get_notion_journal_runtime',provenance=self.provenance,
            journal=NS(checked=checked,MAX_STATE=1024*1024),CONTEXT_KEYS=set(k for k in self.binding['proof']['checkpoint']
                if k in ('run_id','run_attempt','code_sha','controller_sha','rules_sha256','notion_policy_sha256','public_policy_sha256')),
            RECORD_KEYS=set(self.binding['proof']['checkpoint']),MAX_CHECKPOINTS=1024,
            _same_run=lambda a,b,**kw:None,context=lambda c:c,
            fixed_auth=NS(parse=lambda raw,context,run:json.loads(raw)))
        self.bindings=NS(value=lambda m,k:deepcopy(self.entries))
        ns={'deepcopy':deepcopy,'bindings':self.bindings}
        extract('get_notion_create_resume.py',{'TAG','ACK_CONTRACT','KEYS','registry','run_binding','plan_binding','request_sha','allows',
            'initial_state_valid','authenticated_proof_matches','transition_outer','initial_or_transitioned_state_valid',
            'preserve','receipt_only_binding'},ns)
        self.cr=module('get_notion_create_resume',**{k:v for k,v in ns.items() if not k.startswith('__')})
        self.modules={'get_notion_outbox':self.codec,'get_notion_executor':self.engine,
            'get_notion_journal_runtime':self.checkpoints,'get_private_runtime_bindings':self.bindings,
            'get_notion_create_resume':self.cr,'get_notion_property_transaction':self.tx,
            'get_notion_live_transport':self.transport,'get_recurring_policy_recovery':module('get_recurring_policy_recovery',current_get_policy=lambda:'0'*64),
            'get_publication_provenance':module('get_publication_provenance',MAX_TOTAL=1000000)}
        self.patch=patch.dict(sys.modules,self.modules);self.patch.start();self.addCleanup(self.patch.stop)
        plan=self.ack['engine_state']['journal']['plan']
        self.binding['request_sha256']=self.cr.request_sha(plan['records'][0],plan)
        self.auth={**self.binding['proof'],'recovery_anchor':self.cr.TAG}
        self.transitioned=self.cr.transition_outer(self.ack,digest(self.ack),'0'*64,proof=self.auth)
    def complete(self,state=None):
        before=deepcopy(self.transitioned if state is None else state);out=deepcopy(before)
        j=out['engine_state']['journal'];row=j['plan']['records'][0];ident=row['note_id']
        out['engine_state']['receipts'][ident]={'note_id':ident,'page_id':self.binding['page_id'],
            'data_source_id':j['plan']['policy']['data_source_id'],'source_commit':self.binding['source_commit'],
            'md_blob':row['source']['blob'],'creation_plan_sha256':self.binding['plan_sha256'],
            'protected_blocks':[],'asset_bindings':{},'managed_blocks':[],'properties_baseline':{},
            'readback_verified':True,'renderer_version':'synthetic'}
        record_creation(out['engine_state'],row,j['plan']);j.update(index=1,operation=None)
        return before,out

class SchemaState(Adapters):
    def test_strict_ack_schema(self):
        self.assertEqual(self.cr.registry(),[self.binding])
        changes=[lambda b:b['acknowledged_successor'].update(extra=True),
            lambda b:b['acknowledged_successor'].update(contract='other'),
            lambda b:b['acknowledged_successor'].update(state_bytes=True),
            lambda b:b['acknowledged_successor'].update(tree='bad'),
            lambda b:b['acknowledged_successor']['anchor'].update(commit=b['proof']['anchor']['commit']),
            lambda b:b['acknowledged_successor'].update(operation_sha256=b['operation_sha256'])]
        for mutate in changes:
            with self.subTest(mutate=mutate):
                bad=deepcopy(self.binding);mutate(bad);self.entries=[bad]
                with self.assertRaises(Blocked):self.cr.registry()
        self.entries=[self.binding]
    def test_ack_is_not_fabricated_checkpoint(self):
        self.assertEqual(self.binding['proof']['checkpoint_count'],1)
        self.assertEqual(self.binding['proof']['anchor']['state_sha256'],digest(self.before))
        self.assertFalse(self.cr.initial_state_valid(self.before,digest(self.before)))
        self.assertTrue(self.cr.initial_state_valid(self.ack,digest(self.ack)))
        self.assertEqual(self.binding['proof']['terminal_report']['write_state'],'unknown')
    def test_only_exact_ack_and_one_clean_boundary(self):
        j=self.ack['engine_state']['journal'];self.assertTrue(self.cr.allows(j['plan'],j))
        for phase,response,index in [('in_flight',None,0),('prepared',None,0),('acknowledged','2'*32,0),('acknowledged',self.binding['page_id'],1)]:
            bad=deepcopy(j);bad.update(index=index);bad['operation'].update(phase=phase,response=response)
            self.assertFalse(self.cr.allows(bad['plan'],bad))
        clean=deepcopy(j);clean.update(index=1,operation=None);self.assertTrue(self.cr.allows(clean['plan'],clean))
        clean['index']=2;self.assertFalse(self.cr.allows(clean['plan'],clean))
    def test_hash_request_source_and_manifest_are_exact(self):
        for key in ('request_sha256','manifest_sha256','plan_sha256'):
            bad=deepcopy(self.binding);bad[key]='c'*64;self.entries=[bad]
            j=self.ack['engine_state']['journal'];self.assertFalse(self.cr.allows(j['plan'],j))
        self.entries=[self.binding];bad=deepcopy(self.ack);bad['preserved'].append('mutation')
        self.assertFalse(self.cr.initial_state_valid(bad,digest(bad)))
    def test_policy_transition_only(self):
        self.cr.preserve(self.ack,self.transitioned)
        self.assertEqual(self.transitioned['engine_state'],self.ack['engine_state'])
        bad=deepcopy(self.transitioned);bad['preserved']=[]
        with self.assertRaises(Blocked):self.cr.preserve(self.ack,bad)
        with self.assertRaises(Blocked):self.cr.transition_outer(self.ack,digest(self.ack),'9'*64,proof=self.auth)
    def test_legacy_inflight_branch_is_unchanged(self):
        old=deepcopy(self.binding);old.pop('acknowledged_successor');self.entries=[old]
        j=self.before['engine_state']['journal'];self.assertTrue(self.cr.allows(j['plan'],j))
        self.assertTrue(self.cr.initial_state_valid(self.before,digest(self.before)))
        self.assertIsNone(self.cr.receipt_only_binding(j['plan']))
        ack=self.ack['engine_state']['journal'];self.assertFalse(self.cr.allows(ack['plan'],ack))
        for i in (1,2):
            j=deepcopy(j);j.update(index=i,operation=None);self.assertTrue(self.cr.allows(j['plan'],j))

class ReceiptBoundary(Adapters):
    def test_receipt_and_clear_are_two_distinct_exact_cas(self):
        before,done=self.complete();self.cr.preserve(before,done)
        clear=deepcopy(done);clear['engine_state']['journal']=None;self.cr.preserve(done,clear)
        self.assertEqual(clear['preserved'],before['preserved'])
        with self.assertRaises(Blocked):self.cr.preserve(before,clear)
    def test_no_early_clear_or_receipt_mutation(self):
        before,done=self.complete()
        early=deepcopy(before);early['engine_state']['journal']=None
        with self.assertRaises(Blocked):self.cr.preserve(before,early)
        for change in (lambda x:x['engine_state']['receipts'][self.binding['note_id']].update(page_id='2'*32),
                       lambda x:x['engine_state']['property_receipts'].clear(),lambda x:x['preserved'].clear()):
            bad=deepcopy(done);change(bad)
            with self.assertRaises(Blocked):self.cr.preserve(before,bad)
    def test_clear_requires_paired_receipt(self):
        _,done=self.complete();done['engine_state']['property_receipts'].clear()
        clear=deepcopy(done);clear['engine_state']['journal']=None
        with self.assertRaises(Blocked):self.cr.preserve(done,clear)
    def test_native_gate_calls_original_read_target_then_created(self):
        trace=[];snapshot={'exact_id_matches':[self.binding['page_id']],'page_id':self.binding['page_id']}
        def read_target(api,note,ds):trace.append('read_target');return snapshot
        def created(actual,row,ds,page):trace.append('created');self.assertEqual(actual['page_id'],page)
        self.engine.read_target=read_target;self.engine.created=created
        ns={'e':self.engine};extract('get_notion_historical_runtime.py',{'creation_recovery_for'},ns)
        j=self.ack['engine_state']['journal'];p=j['plan']
        result=ns['creation_recovery_for'](j,p['historical_manifest'],p['historical_manifest_sha256'],object())
        self.assertEqual(trace,['read_target','created']);self.assertEqual(result['page_id'],self.binding['page_id'])
        def denied(*args):raise Blocked('native_incomplete')
        self.engine.created=denied
        with self.assertRaises(Blocked):ns['creation_recovery_for'](j,p['historical_manifest'],p['historical_manifest_sha256'],object())
    def test_dispatch_slice_denies_writes_yields_and_clears(self):
        # The exact production dispatch slice, with an executor adapter that
        # performs one receipt CAS then invokes the real boundary callback.
        tree=ast.parse((SCRIPTS/'get_notion_historical_runtime.py').read_text())
        dispatch=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='dispatch')
        apply=next(n for n in dispatch.body if isinstance(n,ast.If) and isinstance(n.test,ast.Name) and n.test.id=='apply')
        start=next(i for i,n in enumerate(apply.body) if isinstance(n,ast.Import) and n.names[0].name=='get_notion_create_resume')
        end=next(i for i,n in enumerate(apply.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='safe' for t in n.targets))
        body=deepcopy(apply.body[start:end]);body.append(ast.Return(value=ast.Name(id='result',ctx=ast.Load())))
        fn=ast.FunctionDef(name='dispatch_slice',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),body=body,decorator_list=[])
        fixture_tree=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]))
        owner=self
        class Store:
            def __init__(self):self.outer=deepcopy(owner.transitioned);self.calls=[]
            def read(self):s=deepcopy(self.outer['engine_state']);return s,digest(s)
            def compare_and_set(self,version,value):
                if version!=digest(self.outer['engine_state']):fail('state_changed')
                wanted=deepcopy(self.outer);wanted['engine_state']=deepcopy(value);owner.cr.preserve(self.outer,wanted)
                self.calls.append('clear' if value['journal'] is None else 'receipt');self.outer=wanted;return digest(value)
        adapter=Store();plan=deepcopy(adapter.outer['engine_state']['journal']['plan']);api=NS(approved={'forbidden'});request=NS(approved={'forbidden'})
        def execute(plan,approved,publication,api,store,**kwargs):
            self.assertEqual(api.approved,frozenset());self.assertEqual(request.approved,frozenset())
            self.assertIsNone(kwargs['authorize_batch'])
            _,done=self.complete(store.outer);_,version=store.read();store.compare_and_set(version,done['engine_state'])
            engine,version=store.read();self.assertIs(kwargs['authorize_row'](engine,plan),False)
            return {'status':'partial','reason':'batch_boundary_yield','state_sha256':version,'external_write_attempts':0}
        self.engine.execute=execute
        def forbidden(*args):raise AssertionError('native write or ordinary authorization attempted')
        ns={'e':self.engine,'deepcopy':deepcopy,'plan':plan,'api':api,'request':request,'adapter':adapter,
            'active':deepcopy(adapter.outer['engine_state']['journal']),'manifest':plan['historical_manifest'],
            'expected':plan['historical_manifest_sha256'],'sha':digest(adapter.outer['engine_state']),
            'publication':object(),'writers_guard':lambda:True,'row_admit':forbidden,'settle_row_yield':None,
            'creation_recovery_for':lambda *args:None,'pipeline':NS(wire_allowlist=forbidden,batch_authorizer=forbidden)}
        # This imported production helper has no private imports or IO.
        settler={};extract('get_notion_single_create_admission.py',{'settle_clean_yield'},settler)
        settler['deepcopy']=deepcopy
        with patch.dict(sys.modules,{'get_notion_single_create_admission':module('get_notion_single_create_admission',**settler)}):
            exec(compile(fixture_tree,str(SCRIPTS/'get_notion_historical_runtime.py'),'exec'),ns);result=ns['dispatch_slice']()
        self.assertEqual(adapter.calls,['receipt','clear']);self.assertIsNone(adapter.outer['engine_state']['journal'])
        self.assertEqual(set(adapter.outer['engine_state']['receipts']),{self.binding['note_id']})
        self.assertEqual(result['external_write_attempts'],0)

class NativeWriteDeny(unittest.TestCase):
    def test_empty_approval_rejects_native_mutations_before_transport(self):
        # Execute the original Transport.__call__ approval gate. No opener or
        # network adapter exists, so an accidental transport call also fails.
        tree=ast.parse((SCRIPTS/'get_notion_live_transport.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Transport')
        call=deepcopy(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='__call__'))
        ns={'re':re,'ro':NS(read_route=lambda method,path:False),'fingerprint':lambda *args:digest(args),'fail':fail}
        exec(compile(ast.Module(body=[call],type_ignores=[]),str(SCRIPTS/'get_notion_live_transport.py'),'exec'),ns)
        subject=NS(approved=frozenset(),attempted=set())
        for method,path,body in [('POST','/pages',{}),('PATCH','/pages/'+'1'*32,{'properties':{}}),
                                 ('PATCH','/blocks/'+'1'*32+'/children',{})]:
            with self.subTest(path=path),self.assertRaisesRegex(Blocked,'write_not_authorized_or_already_attempted'):
                ns['__call__'](subject,method,path,body)
        self.assertEqual(subject.attempted,set())

if __name__=='__main__':unittest.main()
