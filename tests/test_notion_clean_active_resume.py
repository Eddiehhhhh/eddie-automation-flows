"""Public synthetic adapters; actual proof replay is separate private evidence.

No saved receipt is represented as live native evidence by these unit tests.
"""
from copy import deepcopy
from types import SimpleNamespace as NS
import ast
import sys
import unittest
from test_notion_exact_ack_recovery import Adapters,Blocked,encoded,digest,oid,extract,module,record_creation,fail,SCRIPTS

class Fixture(Adapters):
    def setUp(self):
        super().setUp();self.engine.MAX_STATE=32*1024*1024
        snapshot={'complete':True,'native_blocks_available':True,'identity_query_complete':True,'exact_id_matches':[]}
        row=deepcopy(self.before['engine_state']['journal']['plan']['records'][0]);row['before_sha256']=digest(snapshot)
        rows=[dict(deepcopy(row),note_id=str(1000000000000000001+i)) for i in range(10)]
        plan=deepcopy(self.before['engine_state']['journal']['plan']);plan.update(records=rows,rules_sha256='d'*64)
        plan['historical_manifest']['records']={r['note_id']:{'mode':'new_numeric'} for r in rows};plan['historical_manifest_sha256']=digest(plan['historical_manifest'])
        s={'policy_sha256':'7'*64,'sentinel':['preserve-all-other-state'],
           'engine_state':{'journal':{'plan_sha256':digest(plan),'plan':plan,'index':6,'operation':None},
               'receipts':{},'property_receipts':{},'failures':{},'unresolved_by_id':{}}}
        for i,r in enumerate(rows[:6]):
            s['engine_state']['receipts'][r['note_id']]=self.body(plan,r,format(i+1,'032x'));record_creation(s['engine_state'],r,plan)
        proof=deepcopy(self.binding['proof']);proof.update(checkpoint_count=26)
        proof['terminal_report']={'contract':'get-private-runtime-report-2','status':'executed','business_health_asserted':False,
            'complete_library_claimed':False,'execution_status':'blocked','execution_reason':'transport_read_failed','write_state':'uncertain','external_write_attempts':6,'asset_downloads':0}
        proof['fixed_source_record']['selection_sha256']=plan['historical_manifest_sha256']
        cp=proof['checkpoint'];cp.update(sequence=26,parent_commit=None,cas_attempts=0,state_bytes=len(encoded(s)),state_blob=oid(encoded(s)),state_sha256=digest(s),previous_record_sha256='f'*64)
        cp['record_sha256']=digest({k:v for k,v in cp.items() if k!='record_sha256'});proof['anchor']={k:cp[k] for k in ('commit','state_blob','state_sha256')}
        b={'contract':'get-notion-exact-clean-active-1','proof':proof,'public_policy':self.binding['public_policy'],
           'public_policy_sha256':self.binding['public_policy_sha256'],'notion_policy_sha256':self.binding['notion_policy_sha256'],
           'plan_sha256':digest(plan),'plan_bytes':len(encoded(plan)),'engine_rules_sha256':plan['rules_sha256'],
           'manifest_sha256':plan['historical_manifest_sha256'],'property_manifest_sha256':plan['property_manifest_sha256'],
           'source_commit':plan['publication']['commit'],'initial_index':6,'record_count':10,'target_code_sha256':'e'*64,
           'prefix':[{'note_id':r['note_id'],'page_id':s['engine_state']['receipts'][r['note_id']]['page_id'],
               'body_receipt_sha256':digest(s['engine_state']['receipts'][r['note_id']]),'property_receipt_sha256':digest(s['engine_state']['property_receipts'][r['note_id']])} for r in rows[:6]],
           'remaining':[{'note_id':r['note_id'],'source':r['source'],'request_sha256':self.cr.request_sha(r,plan)} for r in rows[6:]]}
        self.entries=[b];ns={'deepcopy':deepcopy,'bindings':self.bindings}
        tree=ast.parse((SCRIPTS/'get_notion_clean_active_resume.py').read_text())
        names={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}|{'TAG','KEYS','_NATIVE_SEAL'}
        extract('get_notion_clean_active_resume.py',names,ns)
        ns['target_code_sha256']=lambda:'e'*64
        b['invariant_sha256']=digest(ns['invariant'](s,b))
        self.bridge=module('get_notion_clean_active_resume',**{k:v for k,v in ns.items() if not k.startswith('__')});self.ns=ns
        self.start=s;self.binding=b;self.auth={**proof,'recovery_anchor':self.bridge.TAG};self.native_reads=[];self.native_state=s
        def read(api,ident,ds):
            self.native_reads.append(ident)
            prior=self.native_state['engine_state']['receipts'].get(ident)
            return {'exact_id_matches':[prior['page_id']],'receipt':deepcopy(prior)} if prior else deepcopy(snapshot)
        def created(snap,r,ds,page):
            if snap.get('exact_id_matches')!=[page]:fail('native_wrong_page')
            return []
        def receipt(snap,ident,ds,managed,pub,source,assets):
            result=deepcopy(snap['receipt']);result.pop('creation_plan_sha256');return result
        self.engine.read_target=read;self.engine.created=created;self.engine.receipt=receipt
        self.witness=self.bridge.verify_native(s,object())
        self.new=self.bridge.transition_outer(s,digest(s),'0'*64,proof=self.auth,native=self.witness)
    def body(self,plan,row,page):
        return {'note_id':row['note_id'],'page_id':page,'data_source_id':plan['policy']['data_source_id'],
            'source_commit':plan['publication']['commit'],'md_blob':row['source']['blob'],'creation_plan_sha256':digest(plan),
            'protected_blocks':[],'asset_bindings':{},'managed_blocks':[],'properties_baseline':{},'readback_verified':True,'renderer_version':'synthetic'}
    def prepared(self,state):
        z=deepcopy(state);j=z['engine_state']['journal'];j['operation']={'kind':'create','phase':'prepared','response':None,'archive_index':0,
            'before':{'complete':True,'native_blocks_available':True,'identity_query_complete':True,'exact_id_matches':[]}}
        return z
    def advance(self,state):
        prepared=self.prepared(state);self.bridge.preserve(state,prepared)
        flight=deepcopy(prepared);flight['engine_state']['journal']['operation']['phase']='in_flight';self.bridge.preserve(prepared,flight)
        ack=deepcopy(flight);j=ack['engine_state']['journal'];page=format(j['index']+1,'032x');j['operation'].update(phase='acknowledged',response=page);self.bridge.preserve(flight,ack)
        done=deepcopy(ack);j=done['engine_state']['journal'];row=j['plan']['records'][j['index']]
        done['engine_state']['receipts'][row['note_id']]=self.body(j['plan'],row,page);record_creation(done['engine_state'],row,j['plan']);j.update(index=j['index']+1,operation=None)
        self.bridge.preserve(ack,done);return done

class Identity(Fixture):
    def test_exact_registry_and_real_failure_contract(self):
        self.assertEqual(self.bridge.registry(),[self.binding]);self.assertTrue(self.bridge.initial_state_valid(self.start,digest(self.start)))
        self.assertEqual(self.binding['proof']['terminal_report']['write_state'],'uncertain')
    def test_bad_proof_fields_rejected(self):
        changes=[lambda b:b.update(initial_index=5),lambda b:b.update(record_count=11),lambda b:b.update(extra=True),
            lambda b:b['proof'].update(checkpoint_count=25),lambda b:b['proof']['terminal_report'].update(write_state='confirmed'),
            lambda b:b['proof']['terminal_report'].update(execution_reason=None),lambda b:b['proof']['terminal_report'].update(external_write_attempts=7),
            lambda b:b['proof']['checkpoint'].update(cas_attempts=1),lambda b:b['proof']['checkpoint'].update(record_sha256='0'*64),
            lambda b:b['proof']['run_binding'].update(conclusion='success'),lambda b:b['proof'].update(run_id=999),
            lambda b:b['proof']['latest_binding'].update(run_attempt=2),lambda b:b['proof']['fixed_source_record'].update(selection_sha256='0'*64)]
        for change in changes:
            with self.subTest(change=change):
                b=deepcopy(self.binding);change(b);self.entries=[b]
                with self.assertRaises(Blocked):self.bridge.registry()
        self.entries=[self.binding]
    def test_target_code_changes_do_not_grant_old_rules(self):
        self.ns['target_code_sha256']=lambda:'f'*64
        self.assertFalse(self.bridge.state_valid(self.start))
    def test_changed_plan_source_rules_policy_or_target_rejected(self):
        for key in ('rules_sha256','publication','historical_manifest_sha256','property_manifest_sha256','policy','records'):
            z=deepcopy(self.start);z['engine_state']['journal']['plan'][key]='changed'
            self.assertFalse(self.bridge.initial_state_valid(z,digest(z)))
    def test_nonzero_operation_wrong_index_and_extra_state_rejected(self):
        for change in (lambda s:s['engine_state']['journal'].update(index=5),lambda s:s['engine_state']['journal'].update(index=7),
            lambda s:s['engine_state']['journal'].update(operation={}),lambda s:s.update(extra=True),lambda s:s['sentinel'].append('change')):
            z=deepcopy(self.start);change(z);self.assertFalse(self.bridge.initial_state_valid(z,digest(z)))
    def test_all_prefix_receipt_pair_pages_pinned(self):
        for field in ('receipts','property_receipts'):
            for row in self.binding['prefix']:
                z=deepcopy(self.start);z['engine_state'][field][row['note_id']]['page_id']='f'*32
                self.assertFalse(self.bridge.state_valid(z))
    def test_other_receipts_holds_failures_or_capture_cannot_change(self):
        for field in ('receipts','property_receipts','failures','unresolved_by_id'):
            z=deepcopy(self.new);z['engine_state'][field]['1000000000000000099']={}
            with self.assertRaises(Blocked):self.bridge.preserve(self.new,z)
        z=deepcopy(self.new);z['historical_captures']={}
        with self.assertRaises(Blocked):self.bridge.preserve(self.new,z)
    def test_remaining_order_fingerprint_or_registry_scope_reject(self):
        for change in (lambda b:b['remaining'].reverse(),lambda b:b['remaining'][0].update(request_sha256='f'*64),lambda b:b['remaining'][0]['source'].update(blob='f'*40)):
            b=deepcopy(self.binding);change(b);self.entries=[b];self.assertFalse(self.bridge.state_valid(self.start))
        self.entries=[self.binding]

class NativeAndTransition(Fixture):
    def test_fresh_all_ten_reads_before_policy_only_transition(self):
        self.assertEqual(self.native_reads,[r['note_id'] for r in self.start['engine_state']['journal']['plan']['records']])
        self.bridge.preserve(self.start,self.new);z=deepcopy(self.new);z['policy_sha256']=self.start['policy_sha256'];self.assertEqual(z,self.start)
    def test_missing_wrong_or_stale_native_witness_rejects(self):
        for value in (None,{},NS(state_sha=digest(self.start)),self.bridge._NativeWitness('f'*64,self.binding['plan_sha256'],self.ns['_NATIVE_SEAL'])):
            with self.assertRaises(Blocked):self.bridge.transition_outer(self.start,digest(self.start),'0'*64,proof=self.auth,native=value)
    def test_missing_native_read_never_falls_back(self):
        self.engine.read_target=lambda *args:fail('transport_read_failed')
        with self.assertRaises(Blocked):self.bridge.verify_native(self.start,object())
    def test_native_changed_prefix_or_existing_remaining_rejected(self):
        for index in (0,5,6,9):
            self.native_state=deepcopy(self.start);plan=self.native_state['engine_state']['journal']['plan'];row=plan['records'][index]
            if index<6:self.native_state['engine_state']['receipts'][row['note_id']]['page_id']='f'*32
            else:self.native_state['engine_state']['receipts'][row['note_id']]=self.body(plan,row,'f'*32)
            with self.assertRaises(Blocked):self.bridge.verify_native(self.start,object())
    def test_changed_proof_or_policy_rejected(self):
        for key in ('job_id','run_id','run_attempt','checkpoint_count','terminal_report'):
            p=deepcopy(self.auth);p[key]='wrong'
            with self.assertRaises(Blocked):self.bridge.transition_outer(self.start,digest(self.start),'0'*64,proof=p,native=self.witness)
        with self.assertRaises(Blocked):self.bridge.transition_outer(self.start,digest(self.start),'f'*64,proof=self.auth,native=self.witness)

class CasEdges(Fixture):
    def test_exact_four_rows_and_final_clear(self):
        s=self.new
        for _ in range(4):s=self.advance(s)
        self.assertEqual(s['engine_state']['journal']['index'],10)
        clear=deepcopy(s);clear['engine_state']['journal']=None;self.bridge.preserve(s,clear)
        self.assertEqual(len(clear['engine_state']['receipts']),10)
    def test_early_clear_skip_rewind_and_plan_rewrite_rejected(self):
        for change in (lambda s:s['engine_state'].update(journal=None),lambda s:s['engine_state']['journal'].update(index=8),
            lambda s:s['engine_state']['journal'].update(index=5),lambda s:s['engine_state']['journal']['plan'].update(rules_sha256='0'*64)):
            z=deepcopy(self.new);change(z)
            with self.assertRaises(Blocked):self.bridge.preserve(self.new,z)
    def test_cannot_skip_prepared_or_ack(self):
        z=self.prepared(self.new);z['engine_state']['journal']['operation']['phase']='in_flight'
        with self.assertRaises(Blocked):self.bridge.preserve(self.new,z)
        prior=self.prepared(self.new);ack=deepcopy(prior);ack['engine_state']['journal']['operation'].update(phase='acknowledged',response='a'*32)
        with self.assertRaises(Blocked):self.bridge.preserve(prior,ack)
    def test_unknown_create_no_reset_no_native_retry(self):
        x=self.prepared(self.new);x['engine_state']['journal']['operation']['phase']='in_flight'
        with self.assertRaises(Blocked):self.bridge.preserve(x,self.prepared(self.new))
        with self.assertRaisesRegex(Blocked,'uncertain_create'):self.bridge.verify_native(x,object())
    def test_current_receipt_requires_exact_page_and_pair(self):
        x=self.prepared(self.new);x['engine_state']['journal']['operation'].update(phase='acknowledged',response='a'*32)
        z=deepcopy(x);j=z['engine_state']['journal'];row=j['plan']['records'][6];z['engine_state']['receipts'][row['note_id']]=self.body(j['plan'],row,'b'*32)
        record_creation(z['engine_state'],row,j['plan']);j.update(index=7,operation=None)
        with self.assertRaises(Blocked):self.bridge.preserve(x,z)
    def test_noncurrent_prefix_tamper_rejected_at_every_cas(self):
        z=self.prepared(self.new);ident=self.binding['prefix'][0]['note_id'];z['engine_state']['receipts'][ident]['readback_verified']=False
        with self.assertRaises(Blocked):self.bridge.preserve(self.new,z)

class TargetPin(unittest.TestCase):
    def setUp(self):
        import tempfile,re
        from pathlib import Path
        from unittest.mock import patch
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.loader=self.root/'get_private_runtime_bindings.py';self.loader.write_text("TRUSTED_BUNDLE_SHA256 = '"+'a'*64+"'\n")
        (self.root/'exact.py').write_text('SAFE = True\n')
        self.doc={'contract':'synthetic','modules':{'other':{'FIELD':{'type':'scalar','value':'kept'}},
            'get_notion_clean_active_resume':{'REGISTRY':{'type':'list','value':[{'type':'dict','value':{'target_code_sha256':{'type':'scalar','value':'b'*64},'scope':{'type':'scalar','value':'one'}}}]}}}}
        def bounded(path,maximum):
            if path.is_symlink():fail('alias')
            data=path.read_bytes()
            if len(data)>maximum:fail('budget')
            return data
        engine=module('get_notion_executor',digest=digest,git=NS(bounded_file=bounded),fail=fail)
        self.patch=patch.dict(sys.modules,{'get_notion_executor':engine});self.patch.start();self.addCleanup(self.patch.stop)
        ns={'deepcopy':deepcopy,'Path':Path,'re':re,'__file__':str(self.root/'bridge.py'),'bindings':NS(encoded=encoded)}
        extract('get_notion_clean_active_resume.py',{'target_material_sha256'},ns);self.target=ns['target_material_sha256'];self.initial=self.target(self.doc)
    def test_only_unique_self_reference_normalized(self):
        z=deepcopy(self.doc);z['modules']['get_notion_clean_active_resume']['REGISTRY']['value'][0]['value']['target_code_sha256']['value']='c'*64
        self.assertEqual(self.target(z),self.initial)
        self.loader.write_text("TRUSTED_BUNDLE_SHA256 = '"+'d'*64+"'\n");self.assertEqual(self.target(self.doc),self.initial)
    def test_other_capsule_data_cannot_inherit_target(self):
        z=deepcopy(self.doc);z['modules']['other']['FIELD']['value']='changed';self.assertNotEqual(self.target(z),self.initial)
    def test_exact_registration_scope_cannot_inherit_target(self):
        z=deepcopy(self.doc);z['modules']['get_notion_clean_active_resume']['REGISTRY']['value'][0]['value']['scope']['value']='broader'
        self.assertNotEqual(self.target(z),self.initial)
    def test_additional_script_and_script_byte_change_pinned(self):
        path=self.root/'extra.py';path.write_text('EXTRA = True\n');self.assertNotEqual(self.target(self.doc),self.initial);path.unlink()
        (self.root/'exact.py').write_text('SAFE = False\n');self.assertNotEqual(self.target(self.doc),self.initial)
    def test_additional_capsule_module_pinned(self):
        z=deepcopy(self.doc);z['modules']['extra']={'DATA':{'type':'scalar','value':True}};self.assertNotEqual(self.target(z),self.initial)
    def test_duplicate_loader_pin_or_missing_pin_reject(self):
        raw=self.loader.read_text()
        for changed in (raw+raw,'NO_PIN = True\n'):
            self.loader.write_text(changed)
            with self.assertRaises(Blocked):self.target(self.doc)
    def test_second_registration_rejected(self):
        z=deepcopy(self.doc);rows=z['modules']['get_notion_clean_active_resume']['REGISTRY']['value'];rows.append(deepcopy(rows[0]))
        with self.assertRaises(Blocked):self.target(z)
    def test_symlink_in_script_closure_rejected(self):
        (self.root/'alias.py').symlink_to(self.root/'exact.py')
        with self.assertRaises(Blocked):self.target(self.doc)

class RealGitSuccessor(Fixture):
    """Original full local-tree verifier over real synthetic Git objects."""
    def setUp(self):
        super().setUp()
        import tempfile,subprocess,hashlib,json
        from pathlib import Path
        from unittest.mock import patch
        from test_notion_exact_ack_recovery import checked
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        def run(*args,data=None):return subprocess.run(['git','-C',str(self.root),*args],input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
        self.git=run;run('init','-q');run('config','user.email','synthetic@example.invalid');run('config','user.name','Synthetic')
        self.state_path='.sync/get-notion/state.json';self.base=self.commit(self.start)
        b=self.binding;cp=b['proof']['checkpoint'];cp.update(commit=self.base,tree=run('rev-parse',self.base+'^{tree}').decode().strip());cp['record_sha256']=digest({k:v for k,v in cp.items() if k!='record_sha256'})
        b['proof']['anchor']['commit']=self.base;self.auth={**b['proof'],'recovery_anchor':self.bridge.TAG};self.head=self.base
        self.capsule=b'synthetic immutable capsule';self.bindings.TRUSTED_BUNDLE_SHA256=digest(self.capsule)
        self.ns['_capsule_bytes']=lambda:self.capsule
        self.capsule_path='.sync/get-notion/runtime-bindings-'+digest(self.capsule)+'.json'
        owner=self
        class Git:
            def __init__(self,path):pass
            def run(self,*args,data=None):return run(*args,data=data)
            def blob(self,sha):return run('cat-file','blob',sha)
        class Remote:
            def ref(self,ref):return owner.head
            def commit(self,sha):return {'tree':run('rev-parse',sha+'^{tree}').decode().strip(),'parents':run('rev-list','--parents','-n','1',sha).decode().split()[1:]}
            def blob(self,sha,size):return run('cat-file','blob',sha)
        self.remote=Remote()
        tree=ast.parse((SCRIPTS/'get_notion_remote_journal.py').read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='RemoteJournal')
        method=deepcopy(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_local_tree'))
        ns={'hashlib':hashlib,'checked':checked,'MAX_BATCH_BYTES':32*1024*1024,'JournalFailure':lambda *a,**kw:Blocked('invalid_git')}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'original_local_tree','exec'),ns)
        journal=module('get_notion_remote_journal',REF='refs/heads/main',STATE_PATH=self.state_path,retention=NS(LocalGit=Git,oid=oid,strict_json=json.loads),RemoteJournal=NS(_local_tree=ns['_local_tree']))
        patcher=patch.dict(sys.modules,{'get_notion_remote_journal':journal});patcher.start();self.addCleanup(patcher.stop)
    def commit(self,state,parent=None,extra=None):
        run=self.git;run('read-tree','--empty') if parent is None else run('read-tree',parent)
        blob=run('hash-object','-w','--stdin',data=encoded(state)).decode().strip();run('update-index','--add','--cacheinfo','100644,'+blob+','+self.state_path)
        if extra:
            path,raw=extra;blob=run('hash-object','-w','--stdin',data=raw).decode().strip();run('update-index','--add','--cacheinfo','100644,'+blob+','+path)
        tree=run('write-tree').decode().strip();return run('commit-tree',tree,*(['-p',parent] if parent else []),'-m','Synthetic').decode().strip()
    def verify(self):return self.bridge.verify_successor(NS(vault=self.root),self.remote,lambda sha:None,self.head,self.auth)
    def test_no_state_change_returns_original_anchor(self):self.assertEqual(self.verify(),self.binding['proof']['anchor'])
    def test_exact_capsule_then_policy_only_child(self):
        cap=self.commit(self.start,self.base,extra=(self.capsule_path,self.capsule));self.head=self.commit(self.new,cap)
        a=self.verify();self.assertEqual(a['commit'],self.head);self.assertEqual(a['state_sha256'],digest(self.new));self.assertEqual(self.auth['anchor']['commit'],self.base)
    def test_exact_policy_only_one_hop(self):
        self.head=self.commit(self.new,self.base);self.assertEqual(self.verify()['commit'],self.head)
    def test_two_state_changes_even_restored_rejected(self):
        first=self.commit(self.new,self.base);z=deepcopy(self.new);z['sentinel'].append('tamper');bad=self.commit(z,first);self.head=self.commit(self.new,bad)
        with self.assertRaises(Blocked):self.verify()
    def test_source_or_unrelated_path_change_rejected(self):
        for path in ('Raw/03 Get/synthetic.md','unrelated.txt',self.capsule_path):
            self.head=self.commit(self.start,self.base,extra=(path,b'wrong'))
            with self.assertRaises(Blocked):self.verify()
    def test_extra_change_bundled_with_policy_rejected(self):
        self.head=self.commit(self.new,self.base,extra=('unrelated.txt',b'wrong'))
        with self.assertRaises(Blocked):self.verify()
    def test_merge_rejected(self):
        child=self.commit(self.start,self.base,extra=('branch.txt',b'x'));tree=self.git('rev-parse',child+'^{tree}').decode().strip()
        self.head=self.git('commit-tree',tree,'-p',self.base,'-p',child,'-m','Synthetic merge').decode().strip()
        with self.assertRaises(Blocked):self.verify()
    def test_remote_wrong_tree_or_blob_rejected(self):
        self.head=self.commit(self.new,self.base);original=self.remote.commit
        self.remote.commit=lambda sha:dict(original(sha),tree='f'*40)
        with self.assertRaises(Blocked):self.verify()
        self.remote.commit=original;self.remote.blob=lambda sha,size:b'wrong'
        with self.assertRaises(Blocked):self.verify()
    def test_ref_drift_at_end_rejected(self):
        self.head=self.commit(self.new,self.base);calls=[]
        def ref(name):calls.append(True);return self.head if len(calls)==1 else 'f'*40
        self.remote.ref=ref
        with self.assertRaises(Blocked):self.verify()
