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
           'schema_exceptions':[],'successor_chain':[],'journal_locator':{'contract':'get-notion-exact-four-slot-locator-1','recent_candidates':3,'total_candidates':4},
           'prefix':[{'note_id':r['note_id'],'page_id':s['engine_state']['receipts'][r['note_id']]['page_id'],
               'body_receipt_sha256':digest(s['engine_state']['receipts'][r['note_id']]),'property_receipt_sha256':digest(s['engine_state']['property_receipts'][r['note_id']])} for r in rows[:6]],
           'remaining':[{'note_id':r['note_id'],'source':r['source'],'request_sha256':self.cr.request_sha(r,plan)} for r in rows[6:]]}
        self.entries=[b];ns={'deepcopy':deepcopy,'bindings':self.bindings}
        tree=ast.parse((SCRIPTS/'get_notion_clean_active_resume.py').read_text())
        names={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}|{'TAG','KEYS','_NATIVE_SEAL','SCHEMA_EXCEPTION_SCOPE_SHA256'}
        extract('get_notion_clean_active_resume.py',names,ns)
        ns['target_code_sha256']=lambda:'e'*64
        ns['SCHEMA_EXCEPTION_SCOPE_SHA256']=digest([])
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

class RealGitFixture(Fixture):
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

class RealGitSuccessor(RealGitFixture):
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

class FiniteGitFixture(RealGitFixture):
    def node(self,commit):
        from get_notion_remote_journal import RemoteJournal
        from get_notion_remote_journal import retention
        tree,entries=RemoteJournal._local_tree(NS(local=retention.LocalGit(self.root)),commit)
        return tree,self.git('rev-list','--parents','-n','1',commit).decode().split()[1:],entries
    def register(self,commits):
        before=self.node(self.base);prior=self.base;chain=[]
        for commit in commits:
            after=self.node(commit);changes=self.bridge.successor_changes(before[2],after[2])
            chain.append({'commit':commit,'parent':prior,'before_tree':before[0],'after_tree':after[0],
                          'changed_paths':len(changes),'changes_sha256':digest(changes)})
            prior=commit;before=after
        self.binding['successor_chain']=chain;self.bridge.registry()
    def chain(self):
        first=self.commit(self.start,self.base,extra=('Wiki/synthetic.md',b'a'))
        second=self.commit(self.start,first,extra=('Raw/05 Chat/synthetic.md',b'b'))
        self.register([first,second]);self.head=second;return first,second

class FiniteRegisteredSuccessor(FiniteGitFixture):
    def test_exact_finite_chain_then_capsule_and_policy(self):
        self.chain();self.assertEqual(self.verify(),self.binding['proof']['anchor'])
        cap=self.commit(self.start,self.head,extra=(self.capsule_path,self.capsule))
        self.head=self.commit(self.new,cap);self.assertEqual(self.verify()['commit'],self.head)
    def test_shorter_prefix_is_not_registered_terminal(self):
        first,_=self.chain();self.head=first
        with self.assertRaises(Blocked):self.verify()
    def test_unknown_later_non_get_commit_rejected(self):
        self.chain();self.head=self.commit(self.start,self.head,extra=('Wiki/later.md',b'x'))
        with self.assertRaises(Blocked):self.verify()
    def test_exact_registration_cannot_admit_protected_state_or_source(self):
        for path in ('Raw/03 Get/note.md','.sync/get-notion/other.json','Schema/rule.md','AGENTS.md','scripts/a.py','Scripts/a.py','.github/workflows/a.yml'):
            with self.subTest(path=path):
                child=self.commit(self.start,self.base,extra=(path,b'x'));self.register([child]);self.head=child
                with self.assertRaises(Blocked):self.verify()
    def test_registered_state_tamper_and_restore_cannot_hide(self):
        z=deepcopy(self.start);z['sentinel'].append('tamper')
        first=self.commit(z,self.base);second=self.commit(self.start,first);self.register([first,second]);self.head=second
        with self.assertRaises(Blocked):self.verify()
    def test_policy_or_capsule_before_registered_chain_rejected(self):
        self.chain();self.head=self.commit(self.new,self.base)
        with self.assertRaises(Blocked):self.verify()
    def test_exact_digest_count_root_parent_and_order_required(self):
        self.chain();original=deepcopy(self.binding['successor_chain'])
        for key,value in [('commit','f'*40),('parent','f'*40),('before_tree','f'*40),('after_tree','f'*40),('changes_sha256','f'*64),('changed_paths',999)]:
            self.binding['successor_chain']=deepcopy(original);self.binding['successor_chain'][0][key]=value
            with self.subTest(key=key),self.assertRaises(Blocked):self.verify()
        self.binding['successor_chain']=list(reversed(original))
        with self.assertRaises(Blocked):self.verify()
    def test_empty_or_duplicate_or_extra_registry_edge_rejected(self):
        self.chain();original=deepcopy(self.binding['successor_chain'])
        for change in (lambda x:x[0].update(extra=True),lambda x:x[0].update(changed_paths=True),lambda x:x.append(deepcopy(x[0]))):
            x=deepcopy(original);change(x);self.binding['successor_chain']=x
            with self.assertRaises(Blocked):self.bridge.registry()
    def test_unchanged_tree_under_new_commit_not_interchangeable(self):
        self.chain();self.head=self.git('commit-tree',self.node(self.head)[0],'-p',self.base,'-m','Different parent and identity').decode().strip()
        with self.assertRaises(Blocked):self.verify()

class FourSlotDiscovery(unittest.TestCase):
    """Actual discovery control flow, explicit synthetic endpoint/proof adapters."""
    def setUp(self):
        from datetime import datetime,timedelta,timezone
        import re
        self.error=type('SyntheticCheckpointFailure',(ValueError,),{})
        def reject(code):raise self.error(code)
        def stamp(x):return datetime.fromisoformat(x.replace('Z','+00:00'))
        self.policy={'repository_id':1,'workflow_id':2,'workflow_path':'.github/workflows/synthetic.yml'}
        self.rows=[{'id':i,'run_attempt':1,'status':'completed','conclusion':'failure','event':'workflow_dispatch',
            'created_at':'2026-01-01T00:00:00Z','updated_at':'2026-01-%02dT00:00:00Z'%i} for i in range(1,7)]
        self.fixed=deepcopy(self.rows[0]);self.trace=[];self.recoveries=[];self.denied=None;self.success=None;self.locator=(1,1)
        owner=self
        def same(a,b,**kw):
            if a!=b:reject('journal_provenance_changed')
        class Client:
            def json(self,path):
                owner.trace.append(path)
                if path==owner.denied:reject('journal_provenance_failed')
                if path=='':return {'id':1,'full_name':'synthetic/repo','private':False}
                if path=='/actions/workflows/2':return {'id':2,'path':owner.policy['workflow_path']}
                if path.startswith('/actions/workflows/2/runs?'):return {'total_count':len(owner.rows),'workflow_runs':deepcopy(owner.rows)}
                if path=='/actions/runs/1':return deepcopy(owner.fixed)
                reject('unexpected_request')
            def logs(self,path):reject('unexpected_logs')
        self.client=Client()
        def proof(row):return {'run_binding':deepcopy(row),'latest_binding':deepcopy(row),'anchor':{'synthetic':True}}
        def recover(client,*,run_id,run_attempt,**kw):
            self.recoveries.append((run_id,run_attempt));client.json('')
            if self.success==run_id:return proof(next(r for r in self.rows if r['id']==run_id))
            reject('journal_provenance_other_controller')
        clean=NS(discovery_locator=lambda policy:self.locator,candidate_policy=lambda policy,row:(policy,row==self.rows[0]))
        auth=NS(clean_active=clean,registered_run=lambda ident,attempt:(ident,attempt)==(1,1),
            candidate_policy=clean.candidate_policy,recover_checkpoint=lambda client,ident,attempt:proof(self.fixed))
        noop=NS(registered_run=lambda *a:False,can_recover=lambda *a:False,OLD_RUN=-1,OLD_ATTEMPT=1,RUN10_RUN=-1,RUN10_ATTEMPT=1,RUN=-1,ATTEMPT=1)
        ns={'deepcopy':deepcopy,'re':re,'timedelta':timedelta,'CheckpointFailure':self.error,'fail':reject,
            'provenance':NS(validate_policy=lambda p,h:p,positive=lambda n:n,timestamp=stamp,REPOSITORY='synthetic/repo',MAX_LOG=100000,MAX_JSON=100000,MAX_TOTAL=1000000,
                a=NS(encoded=encoded),run_binding=lambda r:deepcopy(r),run_semantic_binding=lambda r:deepcopy(r)),
            'journal':NS(checked=lambda *a:None),'_latest_run':lambda r,p:deepcopy(r),'_terminal_run':lambda r,p:deepcopy(r),'_same_run':same,
            'recover_anchor':recover,'authenticated_recovery':auth,'historical_active':noop,'blocked_idle':noop,'idle_recovery':noop,
            'plan_recovery':noop,'png_recovery':noop,'run15_recovery':noop,'run16_recovery':noop,'run18_recovery':noop,'run28_recovery':noop,'run32_recovery':noop,'run53_recovery':noop,'run57_recovery':noop}
        extract('get_notion_journal_runtime.py',{'_ReadBudget','discover_anchor'},ns);self.ns=ns
    def discover(self):return self.ns['discover_anchor'](self.client,public_policy=self.policy,approved_public_policy_sha256='a'*64,notion_policy_sha256='b'*64)
    def test_three_recent_and_exact_fourth_no_fifth(self):
        result=self.discover();self.assertEqual(self.recoveries,[(6,1),(5,1),(4,1),(1,1)])
        self.assertEqual(result['candidate_attempts_checked'],4);self.assertEqual(self.trace.count('/actions/runs/1'),1)
    def test_newest_real_checkpoint_wins_without_locator_fetch(self):
        self.success=5;result=self.discover();self.assertEqual(result['candidate_attempts_checked'],2)
        self.assertNotIn('/actions/runs/1',self.trace);self.assertEqual(self.recoveries,[(6,1),(5,1)])
    def test_missing_fixed_from_listing_still_requires_exact_alias(self):
        self.rows=self.rows[1:];check=lambda p,r:(p,r==self.fixed)
        self.ns['authenticated_recovery'].clean_active.candidate_policy=check;self.ns['authenticated_recovery'].candidate_policy=check
        self.assertEqual(self.discover()['candidate_attempts_checked'],4)
    def test_changed_alias_or_rerun_or_nonterminal_rejected(self):
        for key,value in [('run_attempt',2),('status','in_progress'),('conclusion','success'),('updated_at','2026-02-01T00:00:00Z')]:
            self.fixed=deepcopy(self.rows[0]);self.fixed[key]=value
            with self.subTest(key=key),self.assertRaises(self.error):self.discover()
    def test_denied_locator_no_fallback_or_fifth_candidate(self):
        self.denied='/actions/runs/1'
        with self.assertRaises(self.error):self.discover()
        self.assertEqual(self.recoveries,[(6,1),(5,1),(4,1)])
    def test_fixed_only_deduplicated_not_repeated_four_times(self):
        self.rows=[self.rows[0]];self.assertEqual(self.discover()['candidate_attempts_checked'],1)
        self.assertEqual(self.recoveries,[(1,1)])
    def test_duplicate_listing_or_excluded_fixed_rejected(self):
        self.rows.append(deepcopy(self.rows[0]))
        with self.assertRaises(self.error):self.discover()
        self.rows=self.rows[:-1]
        with self.assertRaises(self.error):self.ns['discover_anchor'](self.client,public_policy=self.policy,approved_public_policy_sha256='a'*64,notion_policy_sha256='b'*64,exclude_run_id=1,exclude_run_attempt=1)
    def test_forty_physical_requests_are_not_reset(self):
        budget=self.ns['_ReadBudget'](self.client)
        for _ in range(40):budget.call('json','')
        with self.assertRaises(self.error):budget.call('json','')
        self.assertEqual(budget.requests,40)

class ExactSchemaExceptions(FiniteGitFixture):
    def schema_chain(self):
        # Three synthetic commits, exactly two preexisting synthetic schema
        # paths. No real private path or object is copied into public tests.
        paths=['Schema/weekly-synthetic.md','Schema/synthetic-index.jsonl']
        self.base=self.commit(self.start,None,extra=(paths[0],b'old'))
        blob=self.git('hash-object','-w','--stdin',data=b'old-index').decode().strip()
        self.git('update-index','--add','--cacheinfo','100644,'+blob+','+paths[1])
        tree=self.git('write-tree').decode().strip();self.base=self.git('commit-tree',tree,'-m','Synthetic base').decode().strip()
        cp=self.binding['proof']['checkpoint'];cp['commit']=self.base;cp['tree']=tree;cp['record_sha256']=digest({k:v for k,v in cp.items() if k!='record_sha256'})
        self.binding['proof']['anchor']['commit']=self.base;self.auth={**self.binding['proof'],'recovery_anchor':self.bridge.TAG}
        commits=[];exceptions=[];parent=self.base
        for i in range(3):
            self.commit(self.start,parent,extra=(paths[0],('version'+str(i)).encode()))
            blob=self.git('hash-object','-w','--stdin',data=('index'+str(i)).encode()).decode().strip()
            self.git('update-index','--add','--cacheinfo','100644,'+blob+','+paths[1])
            tree=self.git('write-tree').decode().strip();commit=self.git('commit-tree',tree,'-p',parent,'-m','Synthetic revision').decode().strip()
            changes=self.bridge.successor_changes(self.node(parent)[2],self.node(commit)[2])
            exceptions.extend({'commit':commit,'path':path,'before':list(old),'after':list(new)}for path,old,new in changes)
            commits.append(commit);parent=commit
        self.register(commits);self.binding['schema_exceptions']=exceptions;self.ns['SCHEMA_EXCEPTION_SCOPE_SHA256']=digest(exceptions)
        self.bridge.registry();self.head=parent;return commits,paths
    def test_six_exact_changes_only(self):
        self.schema_chain();self.assertEqual(self.verify(),self.binding['proof']['anchor'])
    def test_changed_exception_oid_or_path_or_commit_fails_scope_pin(self):
        self.schema_chain();original=deepcopy(self.binding['schema_exceptions'])
        for change in (lambda x:x[0]['after'].__setitem__(2,'f'*40),lambda x:x[0]['before'].__setitem__(2,'f'*40),
                       lambda x:x[0].update(commit='f'*40),lambda x:x[0].update(path='Schema/third.md'),lambda x:x.pop()):
            self.binding['schema_exceptions']=deepcopy(original);change(self.binding['schema_exceptions'])
            with self.assertRaises(Blocked):self.verify()
    def test_same_schema_path_later_commit_is_not_inherited(self):
        _,paths=self.schema_chain();self.head=self.commit(self.start,self.head,extra=(paths[0],b'next'))
        with self.assertRaises(Blocked):self.verify()
    def test_extra_schema_path_even_registered_edge_is_denied(self):
        commits,paths=self.schema_chain();parent=self.head
        child=self.commit(self.start,parent,extra=('Schema/third.md',b'wrong'))
        before=self.node(parent);after=self.node(child);changes=self.bridge.successor_changes(before[2],after[2])
        self.binding['successor_chain'].append({'commit':child,'parent':parent,'before_tree':before[0],'after_tree':after[0],
            'changed_paths':len(changes),'changes_sha256':digest(changes)})
        self.head=child
        with self.assertRaises(Blocked):self.verify()

class RuntimeOldLocatorBoundary(FiniteGitFixture):
    def test_uninspected_fourth_recent_new_checkpoint_stops_before_backend(self):
        # Selection of the older exact proof is NOT current-state authority.
        # Exercise the WHOLE original run() control flow to its actual finite
        # successor gate over real Git, with explicit prior read IO adapters.
        # A fourth recent candidate could hold this unregistered new state.
        import tempfile
        from pathlib import Path
        self.chain();self.head=self.commit(self.new,self.head)
        later=self.advance(self.new);self.head=self.commit(later,self.head)
        self.remote.source_get_tree=lambda sha:'d'*40
        effects=[]
        def forbidden(*a,**kw):effects.append('backend');raise AssertionError('must stop before backend/native/CAS')
        ns={'tempfile':tempfile,'Path':Path,'fail':fail,'PRODUCTION_ACTIVATED':True,'CONTRACT':'synthetic-runtime',
            'fixed_auth':NS(admit=lambda *a,**kw:None),'recurring':NS(validate_policy=lambda p,h,b:p,PRODUCTION_ACTIVATED=True),
            'entry':NS(BOOTSTRAP={}), 'a':NS(checked=lambda *a:None,sha=lambda b:digest(b),encoded=encoded,OfflineGit=lambda p:NS(tree=lambda sha:{self.state_path:True})),
            'provenance':NS(_PublicGithub=type('Unused',(object,),{})),
            'm':NS(Remote=lambda *a:self.remote,guard=lambda r:self.head),
            'r':NS(MAIN_REF='refs/heads/main',readback_checkout=lambda *a:NS(vault=self.root,run=self.git)),
            'journal':NS(ImmutableJournalRemote=lambda r:r,RemoteJournal=forbidden),
            'checkpoints':NS(ExactJournalLoader=lambda *a:lambda sha:None,discover_anchor=lambda *a,**kw:self.auth),
            'outbox':NS(STATE_PATH=self.state_path),'e':NS(digest=digest),'scope_for':lambda p:{},
            'historical_active':NS(PREPARED_TAG='not-matching',INFLIGHT_TAG='not-matching'),
            'authenticated_recovery':NS(TAG='not-matching'),'create_resume':NS(TAG='not-matching'),
            'clean_active':self.bridge,'failure_diagnostic':lambda exc:{'reason':str(exc)},'safe_png_diagnostic':lambda v:{}}
        extract('get_notion_runtime.py',{'run'},ns)
        result=ns['run']('synthetic',forbidden,lambda:True,public_policy={},approved_public_policy_sha256='a'*64,public_client=object())
        self.assertEqual(result['status'],'failed');self.assertEqual(result['stage'],'journal')
        self.assertEqual(result['reason'],'outbox_state_invalid');self.assertEqual(effects,[])

class ExactMutableHead(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        from test_notion_exact_ack_recovery import checked
        self.observed='a'*40;self.calls=[];self.callbacks=[];self.accept=True
        ns={};extract('get_notion_clean_active_resume.py',{'FiniteHeadRemote'},ns);self.cls=ns['FiniteHeadRemote']
        j=module('get_notion_remote_journal',checked=checked,REF='refs/heads/main',codec=NS(fail=fail))
        p=patch.dict(sys.modules,{'get_notion_remote_journal':j});p.start();self.addCleanup(p.stop)
        def push(parent,commit):self.calls.append((parent,commit));self.observed=commit
        def confirm(receipt):self.callbacks.append(deepcopy(receipt));return self.accept
        self.inner=NS(ref=lambda ref:self.observed,cas_main=push,sentinel=object())
        self.guard=self.cls(self.inner,self.observed,confirm)
    def receipt(self,parent='a'*40,commit='b'*40):return {'parent_commit':parent,'commit':commit,'cas_attempts':1}
    def test_foreign_head_before_cas_is_not_inherited(self):
        self.observed='f'*40
        with self.assertRaises(Blocked):self.guard.ref('refs/heads/main')
        with self.assertRaises(Blocked):self.guard.cas_main('a'*40,'b'*40)
        self.assertEqual(self.calls,[])
    def test_own_confirmed_heads_advance_twice_without_pin_reset(self):
        self.guard.cas_main('a'*40,'b'*40);self.assertEqual(self.guard.ref('refs/heads/main'),'b'*40)
        self.assertTrue(self.guard.confirm(self.receipt()));self.assertEqual(self.guard.head,'b'*40)
        self.guard.cas_main('b'*40,'c'*40);self.assertTrue(self.guard.confirm(self.receipt('b'*40,'c'*40)))
        self.assertEqual(self.guard.head,'c'*40);self.assertEqual(len(self.calls),2);self.assertIs(self.guard.sentinel,self.inner.sentinel)
    def test_unknown_cas_is_never_retried(self):
        def push(*a):self.calls.append(a);raise TimeoutError('synthetic')
        self.inner.cas_main=push
        with self.assertRaises(TimeoutError):self.guard.cas_main('a'*40,'b'*40)
        with self.assertRaises(Blocked):self.guard.cas_main('a'*40,'b'*40)
        self.assertEqual(len(self.calls),1);self.assertEqual(self.guard.pending,'b'*40)
    def test_failed_confirmation_does_not_promote(self):
        self.guard.cas_main('a'*40,'b'*40);self.accept=False
        self.assertFalse(self.guard.confirm(self.receipt()));self.assertEqual(self.guard.head,'a'*40)
        with self.assertRaises(Blocked):self.guard.cas_main('b'*40,'c'*40)
        self.assertEqual(len(self.calls),1)
    def test_foreign_descendant_after_own_cas_rejected(self):
        self.guard.cas_main('a'*40,'b'*40);self.observed='f'*40
        with self.assertRaises(Blocked):self.guard.ref('refs/heads/main')
    def test_receipt_identity_and_noop_cannot_substitute(self):
        self.guard.cas_main('a'*40,'b'*40)
        for receipt in [self.receipt('f'*40),self.receipt(commit='f'*40),{'parent_commit':None,'commit':'a'*40,'cas_attempts':0},dict(self.receipt(),cas_attempts=True)]:
            with self.assertRaises(Blocked):self.guard.confirm(receipt)
        self.assertEqual(self.callbacks,[])
    def test_noop_only_exact_current_and_no_pending(self):
        self.assertTrue(self.guard.confirm({'parent_commit':None,'commit':'a'*40,'cas_attempts':0}))
        self.assertEqual(self.guard.head,'a'*40);self.assertEqual(self.calls,[])
    def test_callback_exception_keeps_pending(self):
        self.guard.cas_main('a'*40,'b'*40)
        def bad(receipt):raise OSError('synthetic callback failure')
        self.guard.callback=bad
        with self.assertRaises(OSError):self.guard.confirm(self.receipt())
        self.assertEqual(self.guard.head,'a'*40);self.assertEqual(self.guard.pending,'b'*40)
    def test_original_backend_races_fail_before_native_and_never_retry(self):
        from test_notion_journal_timing import synthetic_journal
        from unittest.mock import patch
        for phase in ('initial_read','before_cas','after_cas','after_confirm','none'):
            with self.subTest(phase=phase),synthetic_journal('success') as (store,journal,sha):
                journal.codec.fail=fail
                with patch.dict(sys.modules,{'get_notion_remote_journal':journal}):
                    inner=NS(ref=lambda ref:store.head,metadata=lambda:{'full_name':journal.REPOSITORY,'private':True},cas_main=store.push)
                    callback=store.callback
                    def confirm(receipt):
                        result=callback(receipt)
                        if phase=='after_confirm':store.head='f'*40
                        return result
                    guard=self.cls(inner,store.initial,confirm);store.remote=guard;store.confirm=guard.confirm;store.guard=lambda head:True
                    store._ref=journal.RemoteJournal._ref.__get__(store);store._head=journal.RemoteJournal._head.__get__(store)
                    store._post_cas_head=journal.RemoteJournal._post_cas_head.__get__(store)
                    current=store._current
                    def guarded_current(**kw):store._head();return current(**kw)
                    store._current=guarded_current
                    build=store._build
                    def built(*a):
                        result=build(*a)
                        if phase=='before_cas':store.head='f'*40
                        return result
                    store._build=built
                    verify=store._verify
                    def verified(commit,parent,*a):
                        store._post_cas_head(commit);receipt,value=verify(commit,parent,*a);receipt['parent_commit']=parent;return receipt,value
                    store._verify=verified
                    if phase=='after_cas':
                        def push(parent,commit):store.push(parent,commit);store.head='f'*40
                        inner.cas_main=push
                    if phase=='initial_read':store.head='f'*40
                    result=store.execute()
                    if phase=='none':self.assertEqual(result['status'],'verified');self.assertEqual(len(store.calls),1)
                    else:
                        self.assertEqual(result['status'],'blocked');self.assertEqual(store.external_writes,[])
                        self.assertEqual(len(store.calls),0 if phase in ('initial_read','before_cas') else 1)
                        if phase in ('after_cas','after_confirm'):
                            self.assertTrue(store.poisoned)
                            with self.assertRaises(journal.JournalFailure):store.compare_and_set_verified(sha(store.old),store.new)
                            self.assertEqual(len(store.calls),1)

    def test_original_protected_guard_through_both_remote_layers(self):
        from unittest.mock import patch
        refs={'refs/heads/synthetic-backup-'+str(i):str(i+1)*40 for i in range(4)}
        calls=[]
        def read(name):calls.append(name);return self.observed if name=='refs/heads/main' else refs[name]
        self.inner.ref=read;self.inner.metadata=lambda:{'full_name':'synthetic/private','private':True}
        ns={'PROTECTED':deepcopy(refs),'r':NS(REPOSITORY='synthetic/private',MAIN_REF='refs/heads/main'),
            'a':NS(checked=lambda *a:None),'fail':fail}
        extract('get_legacy_manual.py',{'guard'},ns)
        # The real immutable wrapper delegates these mutable reads unchanged.
        from collections import OrderedDict
        jns={'OrderedDict':OrderedDict,'MAX_BATCH_BYTES':32*1024*1024,'JournalFailure':Blocked,'retention':NS(GitHubRemote=NS(source_get_tree=lambda *a:None))}
        extract('get_notion_remote_journal.py',{'ImmutableJournalRemote'},jns)
        with patch.dict(sys.modules,{'get_legacy_manual':NS(PROTECTED=deepcopy(refs))}):
            remote=jns['ImmutableJournalRemote'](self.guard)
            self.assertEqual(ns['guard'](remote),'a'*40);self.assertEqual(calls,list(refs)+['refs/heads/main'])
            refs[next(iter(refs))]='f'*40
            with self.assertRaises(Blocked):ns['guard'](remote)
            with self.assertRaises(Blocked):remote.ref('refs/heads/unregistered')
