"""Public synthetic adapters; actual proof replay is separate private evidence.

No saved receipt is represented as live native evidence by these unit tests.
"""
from copy import deepcopy
from types import SimpleNamespace as NS
import ast
import sys
import unittest
from test_notion_exact_ack_recovery import Adapters,Blocked,encoded,digest,oid,extract,module,record_creation,fail,SCRIPTS

MIRROR_PATH='.github/state/notion-wiki-mirror-state.json'
MIRROR_BEFORE=encoded({'generated_at':'2026-01-01T01:00:00+00:00','preserved':['synthetic']})
MIRROR_AFTER=encoded({'generated_at':'2026-01-01T02:00:00+00:00','preserved':['synthetic']})

def progress_binding(b,state,cap=None,commit='c'*40,tree='d'*40):
    prior=b['prior_capsule'];raw=b'policy progress synthetic capsule'
    cap=cap or {'commit':'1'*40,'parent':prior['commit'],'before_tree':prior['after_tree'],'after_tree':'2'*40,
        'path':'.sync/get-notion/runtime-bindings-'+digest(raw)+'.json','blob':oid(raw),'sha256':digest(raw),'bytes':len(raw)}
    current=deepcopy(state);current['policy_sha256']='6'*64
    policy=dict(b['public_policy'],code_sha='2'*40,rules_sha256='2'*64,get_policy_sha256='6'*64)
    p=deepcopy(b['proof']);p.update(run_id=88,checkpoint_count=2)
    for name in ('run_binding','latest_binding'):p[name].update(id=88,head_sha='3'*40)
    p['terminal_report'].update(execution_reason='approval_missing',external_write_attempts=0,write_state='unknown')
    cp=dict(p['checkpoint'],run_id=88,code_sha=policy['code_sha'],rules_sha256=policy['rules_sha256'],controller_sha='3'*40,
        public_policy_sha256=digest(policy),sequence=1,cas_attempts=1,parent_commit=cap['commit'],previous_record_sha256=None,
        commit=commit,tree=tree,state_blob=oid(encoded(current)),state_sha256=digest(current),state_bytes=len(encoded(current)))
    cp['record_sha256']=digest({k:v for k,v in cp.items() if k!='record_sha256'})
    last=dict(cp,sequence=2,cas_attempts=0,parent_commit=None,previous_record_sha256=cp['record_sha256'])
    last['record_sha256']=digest({k:v for k,v in last.items() if k!='record_sha256'});p['checkpoint']=last;p['anchor']={k:last[k] for k in ('commit','state_blob','state_sha256')}
    return {'mirror':{'commit':'e'*40,'parent':commit,'before_tree':tree,'after_tree':'f'*40,'path':MIRROR_PATH,'mode':'100644',
        'before':{'blob':oid(MIRROR_BEFORE),'sha256':digest(MIRROR_BEFORE),'bytes':len(MIRROR_BEFORE)},
        'after':{'blob':oid(MIRROR_AFTER),'sha256':digest(MIRROR_AFTER),'bytes':len(MIRROR_AFTER)}},'capsule':cap,'state':{'commit':commit,'parent':cap['commit'],'before_tree':cap['after_tree'],'after_tree':tree,
        'state_blob':oid(encoded(current)),'state_sha256':digest(current),'state_bytes':len(encoded(current)),'policy_sha256':current['policy_sha256']},
        'proof':p,'checkpoints':[cp,last],'public_policy':policy,'public_policy_sha256':digest(policy),'notion_policy_sha256':b['notion_policy_sha256']},current

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
        b['prior_capsule']={'commit':'a'*40,'parent':proof['anchor']['commit'],'before_tree':cp['tree'],'after_tree':'f'*40,'path':'.sync/get-notion/runtime-bindings-'+('e'*64)+'.json','blob':'e'*40,'sha256':'e'*64,'bytes':10}
        b['policy_progress'],self.progress=progress_binding(b,s)
        self.checkpoints.journal.MIRROR_PATH=MIRROR_PATH;self.checkpoints.journal.MAX_MIRROR_BYTES=1024*1024
        self.bindings.MAX_BYTES=1024*1024
        self.entries=[b];ns={'deepcopy':deepcopy,'bindings':self.bindings}
        tree=ast.parse((SCRIPTS/'get_notion_clean_active_resume.py').read_text())
        names={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}|{'TAG','KEYS','_NATIVE_SEAL','SCHEMA_EXCEPTION_SCOPE_SHA256','PRIOR_CAPSULE_SCOPE_SHA256','POLICY_PROGRESS_SCOPE_SHA256'}
        extract('get_notion_clean_active_resume.py',names,ns)
        ns['target_code_sha256']=lambda:'e'*64
        ns['SCHEMA_EXCEPTION_SCOPE_SHA256']=digest([])
        ns['PRIOR_CAPSULE_SCOPE_SHA256']=digest(b['prior_capsule'])
        ns['POLICY_PROGRESS_SCOPE_SHA256']=digest(b['policy_progress'])
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
        self.state_path='.sync/get-notion/state.json';self.base=self.commit(self.start,extra=(MIRROR_PATH,MIRROR_BEFORE))
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
        self.register_prior_capsule(self.base)
    def register_prior_capsule(self,parent):
        raw=b'previous synthetic immutable capsule';sha=digest(raw);path='.sync/get-notion/runtime-bindings-'+sha+'.json'
        commit=self.commit(self.start,parent,extra=(path,raw))
        self.binding['prior_capsule']={'commit':commit,'parent':parent,'before_tree':self.git('rev-parse',parent+'^{tree}').decode().strip(),'after_tree':self.git('rev-parse',commit+'^{tree}').decode().strip(),'path':path,'blob':oid(raw),'sha256':sha,'bytes':len(raw)}
        self.ns['PRIOR_CAPSULE_SCOPE_SHA256']=digest(self.binding['prior_capsule'])
        raw2=b'policy progress synthetic capsule';sha2=digest(raw2);path2='.sync/get-notion/runtime-bindings-'+sha2+'.json'
        cap2=self.commit(self.start,commit,extra=(path2,raw2))
        progress=deepcopy(self.start);progress['policy_sha256']='6'*64
        tail=self.commit(progress,cap2)
        cap={'commit':cap2,'parent':commit,'before_tree':self.git('rev-parse',commit+'^{tree}').decode().strip(),
             'after_tree':self.git('rev-parse',cap2+'^{tree}').decode().strip(),'path':path2,'blob':oid(raw2),'sha256':sha2,'bytes':len(raw2)}
        self.binding['policy_progress'],self.progress=progress_binding(self.binding,self.start,cap,tail,self.git('rev-parse',tail+'^{tree}').decode().strip())
        mirror=self.commit(progress,tail,extra=(MIRROR_PATH,MIRROR_AFTER))
        self.binding['policy_progress']['mirror'].update(commit=mirror,after_tree=self.git('rev-parse',mirror+'^{tree}').decode().strip())
        self.ns['POLICY_PROGRESS_SCOPE_SHA256']=digest(self.binding['policy_progress'])
        self.auth={**self.binding['policy_progress']['proof'],'recovery_anchor':self.bridge.TAG};self.head=mirror
        return commit
    def commit(self,state,parent=None,extra=None):
        run=self.git;run('read-tree','--empty') if parent is None else run('read-tree',parent)
        blob=run('hash-object','-w','--stdin',data=encoded(state)).decode().strip();run('update-index','--add','--cacheinfo','100644,'+blob+','+self.state_path)
        if extra:
            path,raw=extra;blob=run('hash-object','-w','--stdin',data=raw).decode().strip();run('update-index','--add','--cacheinfo','100644,'+blob+','+path)
        tree=run('write-tree').decode().strip();return run('commit-tree',tree,*(['-p',parent] if parent else []),'-m','Synthetic').decode().strip()
    def verify(self):return self.bridge.verify_successor(NS(vault=self.root),self.remote,lambda sha:None,self.head,self.auth)

class RealGitSuccessor(RealGitFixture):
    def test_no_state_change_returns_exact_progress_anchor(self):self.assertEqual(self.verify(),self.binding['policy_progress']['proof']['anchor'])
    def test_exact_capsule_then_policy_only_child(self):
        cap=self.commit(self.progress,self.binding['policy_progress']['mirror']['commit'],extra=(self.capsule_path,self.capsule));self.head=self.commit(self.new,cap)
        a=self.verify();self.assertEqual(a['commit'],self.head);self.assertEqual(a['state_sha256'],digest(self.new));self.assertEqual(self.auth['anchor']['commit'],self.binding['policy_progress']['state']['commit'])
    def test_exact_policy_only_one_hop(self):
        self.head=self.commit(self.new,self.base)
        with self.assertRaises(Blocked):self.verify()
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
        self.binding['successor_chain']=chain;self.register_prior_capsule(prior);self.bridge.registry()
    def chain(self):
        first=self.commit(self.start,self.base,extra=('Wiki/synthetic.md',b'a'))
        second=self.commit(self.start,first,extra=('Raw/05 Chat/synthetic.md',b'b'))
        self.register([first,second]);return first,second

class FiniteRegisteredSuccessor(FiniteGitFixture):
    def test_exact_finite_chain_then_capsule_and_policy(self):
        self.chain();self.assertEqual(self.verify(),self.binding['policy_progress']['proof']['anchor'])
        cap=self.commit(self.progress,self.binding['policy_progress']['mirror']['commit'],extra=(self.capsule_path,self.capsule))
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
        mirror_blob=self.git('hash-object','-w','--stdin',data=MIRROR_BEFORE).decode().strip();self.git('update-index','--add','--cacheinfo','100644,'+mirror_blob+','+MIRROR_PATH)
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
        self.bridge.registry();return commits,paths
    def test_six_exact_changes_only(self):
        self.schema_chain();self.assertEqual(self.verify(),self.binding['policy_progress']['proof']['anchor'])
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

class ExactCapsuleHydration(RealGitFixture):
    def hydration(self,raw=b'capsule',remote_raw=None,write_oid=None,readback=None,limit=None):
        raw=bytes(raw);calls=[]
        def blob(sha,size):
            calls.append(('remote',sha,size))
            if limit is not None:fail(limit)
            return raw if remote_raw is None else remote_raw
        def run(*args,data=None):
            calls.append(('write',args,data));return ((oid(raw) if write_oid is None else write_oid)+'\n').encode()
        def local(sha):calls.append(('readback',sha));return raw if readback is None else readback
        self.bridge._hydrate_capsule(NS(run=run,blob=local),NS(blob=blob),oid(raw),len(raw),digest(raw),raw)
        return calls
    def test_remote_validation_precedes_local_write_and_readback(self):
        calls=self.hydration();self.assertEqual([x[0] for x in calls],['remote','write','readback'])
    def test_wrong_short_long_or_nonbytes_remote_stops_before_write(self):
        for raw in (b'capsulf',b'capsul',b'capsules','capsule'):
            with self.subTest(raw=raw),self.assertRaises(Blocked):self.hydration(remote_raw=raw)
    def test_wrong_hash_object_result_or_local_readback_rejected(self):
        for args in ({'write_oid':'f'*40},{'readback':b'wrong'}):
            with self.assertRaises(Blocked):self.hydration(**args)
    def test_remote_budget_failure_not_retried(self):
        with self.assertRaisesRegex(Blocked,'request_budget'):self.hydration(limit='request_budget')
    def test_size_invalid_zero_remote_calls(self):
        calls=[];remote=NS(blob=lambda *a:calls.append(a))
        for size in (True,0,-1,self.bindings.MAX_BYTES+1):
            with self.assertRaises(Blocked):self.bridge._hydrate_capsule(None,remote,'a'*40,size,'b'*64)
        self.assertEqual(calls,[])
    def test_sha256_oid_and_expected_bytes_all_independent(self):
        raw=b'capsule';calls=[];remote=NS(blob=lambda *a:raw)
        git=NS(run=lambda *a,**k:calls.append(a))
        for sha,h,expected in [('f'*40,digest(raw),raw),(oid(raw),'f'*64,raw),(oid(raw),digest(raw),b'other')]:
            with self.assertRaises(Blocked):self.bridge._hydrate_capsule(git,remote,sha,len(raw),h,expected)
        self.assertEqual(calls,[])
    def test_missing_known_blob_is_hydrated_without_native_or_cas(self):
        b=self.binding['prior_capsule'];raw=self.git('cat-file','blob',b['blob']);self.head=self.binding['policy_progress']['mirror']['commit']
        path=self.root/'.git/objects'/b['blob'][:2]/b['blob'][2:];path.unlink()
        original=self.remote.blob;calls=[]
        def blob(sha,size):calls.append((sha,size));return raw if sha==b['blob'] else original(sha,size)
        self.remote.blob=blob;self.native_reads.clear()
        self.assertEqual(self.verify(),self.binding['policy_progress']['proof']['anchor'])
        self.assertEqual(self.git('cat-file','blob',b['blob']),raw)
        self.assertEqual([x for x in calls if x[0]==b['blob']],[(b['blob'],len(raw))]);self.assertEqual(self.native_reads,[])
    def test_existing_known_blob_still_needs_exact_remote_read(self):
        self.head=self.binding['policy_progress']['mirror']['commit'];original=self.remote.blob;calls=[]
        def blob(sha,size):calls.append(sha);return original(sha,size)
        self.remote.blob=blob;self.verify();self.assertEqual(calls.count(self.binding['prior_capsule']['blob']),1)
    def test_known_edge_identity_scope_or_metadata_change_rejected(self):
        self.head=self.binding['prior_capsule']['commit'];original=deepcopy(self.binding['prior_capsule'])
        for key,value in [('commit','f'*40),('parent','f'*40),('before_tree','f'*40),('after_tree','f'*40),('blob','f'*40),('sha256','f'*64),('bytes',1),('path','unrelated')]:
            self.binding['prior_capsule']=deepcopy(original);self.binding['prior_capsule'][key]=value
            with self.subTest(key=key),self.assertRaises(Blocked):self.verify()
        self.binding['prior_capsule']=original
    def test_same_old_payload_other_commit_not_interchangeable(self):
        b=self.binding['prior_capsule'];self.head=self.git('commit-tree',b['after_tree'],'-p',self.base,'-m','Another identity').decode().strip()
        with self.assertRaises(Blocked):self.verify()
    def test_old_capsule_changed_deleted_or_repeated_after_admission_rejects(self):
        b=self.binding['prior_capsule']
        for raw in (b'changed',b'previous synthetic immutable capsule'):
            self.head=self.commit(self.start,b['commit'],extra=(b['path'],raw))
            with self.assertRaises(Blocked):self.verify()
        self.git('read-tree',b['commit']);self.git('update-index','--force-remove',b['path']);tree=self.git('write-tree').decode().strip()
        self.head=self.git('commit-tree',tree,'-p',b['commit'],'-m','delete').decode().strip()
        with self.assertRaises(Blocked):self.verify()
    def test_current_capsule_cannot_precede_known_edge_or_repeat(self):
        self.head=self.commit(self.start,self.base,extra=(self.capsule_path,self.capsule))
        with self.assertRaises(Blocked):self.verify()
        first=self.commit(self.start,self.binding['prior_capsule']['commit'],extra=(self.capsule_path,self.capsule))
        self.head=self.commit(self.start,first,extra=(self.capsule_path,self.capsule))
        with self.assertRaises(Blocked):self.verify()
    def test_known_capsule_with_state_or_extra_leaf_rejected(self):
        b=self.binding['prior_capsule'];self.head=self.commit(self.new,self.base,extra=(b['path'],b'previous synthetic immutable capsule'))
        with self.assertRaises(Blocked):self.verify()
        self.head=self.commit(self.start,b['commit'],extra=('unknown.txt',b'wrong'))
        with self.assertRaises(Blocked):self.verify()
    def test_policy_cannot_precede_new_current_capsule(self):
        self.head=self.commit(self.new,self.binding['prior_capsule']['commit'])
        with self.assertRaises(Blocked):self.verify()
    def original_loader_entry(self,preloaded=False,wrong_remote=False,max_calls=2048):
        # Original loader class and original LocalGit over synthetic real Git.
        # Metadata-only fetch transport is an explicit no-network adapter.
        import hashlib,json
        from pathlib import Path
        import get_private_preview_retention as retention
        from test_notion_exact_ack_recovery import checked
        j=sys.modules['get_notion_remote_journal'];j.retention=retention
        j.MAX_STATE=32*1024*1024;j.MAX_BATCH_BYTES=64*1024*1024
        j.checked=checked;j.REPOSITORY=retention.REPOSITORY
        old=self.binding['prior_capsule'];self.head=self.commit(self.progress,self.binding['policy_progress']['mirror']['commit'],extra=(self.capsule_path,self.capsule))
        blobs={sha:self.git('cat-file','blob',sha) for sha in (old['blob'],self.binding['policy_progress']['capsule']['blob'],oid(self.capsule),oid(encoded(self.start)),oid(encoded(self.progress)),oid(MIRROR_BEFORE),oid(MIRROR_AFTER))}
        metadata={}
        for line in self.git('cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype)').decode().splitlines():
            sha,kind=line.split()
            if kind!='tree':continue
            entries=[]
            for row in self.git('ls-tree','-z',sha).split(b'\0'):
                if not row:continue
                left,path=row.split(b'\t');mode,typ,ident=left.decode().split();r={'mode':mode,'type':typ,'sha':ident,'path':path.decode()}
                if typ=='blob':r['size']=len(blobs[ident])
                entries.append(r)
            metadata[sha]=entries
        for sha in blobs:
            if not preloaded:(self.root/'.git/objects'/sha[:2]/sha[2:]).unlink()
        self.git('remote','add','origin',retention.REMOTE_URL)
        local=retention.LocalGit(self.root);fetches=[];reads=[]
        def run(*args,**kw):
            if 'fetch' in args:
                self.assertIn('--filter=blob:none',args);fetches.append(args);return b''
            return local.run(*args,**kw)
        self.remote.local=NS(vault=self.root,run=run)
        self.remote.metadata=lambda:{'full_name':retention.REPOSITORY,'private':True}
        self.remote.tree_entries=lambda sha:deepcopy(metadata[sha])
        def blob(sha,size):
            reads.append((sha,size))
            return b'wrong' if wrong_remote and sha==old['blob'] else blobs[sha]
        self.remote.blob=blob
        ns={'Path':Path,'journal':j,'codec':self.codec,'fail':fail,'CheckpointFailure':Blocked,'hashlib':hashlib}
        extract('get_notion_journal_runtime.py',{'ExactJournalLoader'},ns)
        loader=ns['ExactJournalLoader'](self.root,self.remote,max_calls=max_calls)
        loader(self.head)
        if not preloaded:
            self.assertEqual(local.run('cat-file','--batch-check=%(objectname) %(objecttype)',data=(oid(self.capsule)+'\n').encode()),(oid(self.capsule)+' missing\n').encode())
        self.native_reads.clear()
        anchor=self.bridge.verify_successor(NS(vault=self.root),self.remote,loader,self.head,self.auth)
        self.assertEqual(anchor,self.binding['policy_progress']['proof']['anchor']);self.assertEqual(self.native_reads,[])
        self.assertEqual(local.blob(oid(self.capsule)),self.capsule)
        self.assertEqual([sha for sha,size in reads].count(old['blob']),1)
        self.assertEqual([sha for sha,size in reads].count(oid(self.capsule)),1)
        self.assertTrue(fetches)
    def test_original_loader_blobless_entry_hydrates_both_exact_capsules(self):self.original_loader_entry()
    def test_original_loader_preloaded_entry_still_checks_remote_both(self):self.original_loader_entry(preloaded=True)
    def test_original_loader_wrong_capsule_remote_fails_before_native(self):
        with self.assertRaises(Blocked):self.original_loader_entry(wrong_remote=True)
        self.assertEqual(self.native_reads,[])
    def test_original_loader_original_call_budget_not_bypassed(self):
        with self.assertRaisesRegex(Blocked,'journal_loader_budget'):self.original_loader_entry(max_calls=1)
        self.assertEqual(self.native_reads,[])

class ExactPolicyProgress(Fixture):
    def test_actual_kind_progress_keeps_distinct_run_checkpoint_state(self):
        progress=self.binding['policy_progress'];p={**progress['proof'],'recovery_anchor':self.bridge.TAG}
        self.assertTrue(self.bridge.progress_state_valid(self.progress,digest(self.progress)))
        self.assertTrue(self.bridge.authenticated_proof_matches(p,self.binding))
        witness=self.bridge._NativeWitness(digest(self.progress),self.binding['plan_sha256'],self.ns['_NATIVE_SEAL'])
        self.assertEqual(self.bridge.transition_outer(self.progress,digest(self.progress),'0'*64,proof=p,native=witness),self.new)
        self.assertEqual(progress['proof']['checkpoint_count'],2)
        self.assertEqual([x['cas_attempts'] for x in progress['checkpoints']],[1,0])
        self.assertEqual(progress['proof']['terminal_report']['write_state'],'unknown')
    def test_old_and_progress_proofs_never_mix_states(self):
        p={**self.binding['policy_progress']['proof'],'recovery_anchor':self.bridge.TAG}
        for state,proof in ((self.start,p),(self.progress,self.auth)):
            with self.assertRaises(Blocked):self.bridge._transition_bytes(state,digest(state),'0'*64,proof=proof)
        for key in ('run_id','checkpoint','checkpoint_count','anchor','run_binding'):
            mixed=deepcopy(p);mixed[key]=deepcopy(self.auth[key])
            self.assertFalse(self.bridge.authenticated_proof_matches(mixed,self.binding))
    def test_progress_policy_alone_never_grants_extra_state(self):
        for change in (lambda v:v['sentinel'].append('x'),lambda v:v['engine_state']['journal'].update(index=7),
                       lambda v:v['engine_state']['receipts'].clear(),lambda v:v['engine_state']['failures'].update(x={})):
            s=deepcopy(self.progress);change(s);self.assertFalse(self.bridge.progress_state_valid(s,digest(s)))
            with self.assertRaises(Blocked):self.bridge.preserve(s,self.new)
    def test_progress_scope_pin_rejects_every_unreviewed_field(self):
        good=deepcopy(self.binding['policy_progress'])
        for field in good:
            self.binding['policy_progress']=deepcopy(good);self.binding['policy_progress'][field]=None
            with self.subTest(field=field),self.assertRaises(Blocked):self.bridge.registry()
        self.binding['policy_progress']=good
    def test_progress_checkpoint_failure_and_cas_semantics_even_if_scope_resealed(self):
        good=deepcopy(self.binding['policy_progress'])
        changes=(lambda v:v['proof']['terminal_report'].update(write_state='confirmed'),
                 lambda v:v['proof']['terminal_report'].update(external_write_attempts=1),
                 lambda v:v['proof']['terminal_report'].update(execution_reason='transport_read_failed'),
                 lambda v:v['proof'].update(checkpoint_count=26),
                 lambda v:v['proof'].update(run_id=self.auth['run_id']),
                 lambda v:v['proof']['latest_binding'].update(run_attempt=2),
                 lambda v:v['checkpoints'][0].update(cas_attempts=0),
                 lambda v:v['checkpoints'][1].update(cas_attempts=1),
                 lambda v:v['checkpoints'][1].update(parent_commit='f'*40),
                 lambda v:v['state'].update(parent='f'*40),
                 lambda v:v['capsule'].update(parent='f'*40))
        for change in changes:
            self.binding['policy_progress']=deepcopy(good);change(self.binding['policy_progress']);self.ns['POLICY_PROGRESS_SCOPE_SHA256']=digest(self.binding['policy_progress'])
            with self.assertRaises(Blocked):self.bridge.registry()
        self.binding['policy_progress']=good;self.ns['POLICY_PROGRESS_SCOPE_SHA256']=digest(good)

class ExactPolicyProgressGit(RealGitFixture):
    def test_old_proof_cannot_admit_actual_progress_head(self):
        self.auth={**self.binding['proof'],'recovery_anchor':self.bridge.TAG}
        with self.assertRaises(Blocked):self.verify()
    def test_exact_progress_then_current_capsule_and_only_policy(self):
        cap=self.commit(self.progress,self.head,extra=(self.capsule_path,self.capsule));self.head=self.commit(self.new,cap)
        self.assertEqual(self.verify()['state_sha256'],digest(self.new))
    def test_progress_state_cannot_be_skipped_or_changed(self):
        v=self.binding['policy_progress'];self.head=v['capsule']['commit']
        with self.assertRaises(Blocked):self.verify()
        s=deepcopy(self.progress);s['sentinel'].append('changed');self.head=self.commit(s,v['capsule']['commit'])
        with self.assertRaises(Blocked):self.verify()
    def test_same_policy_bytes_other_commit_not_progress(self):
        v=self.binding['policy_progress']['state'];self.head=self.git('commit-tree',v['after_tree'],'-p',v['parent'],'-m','different identity').decode().strip()
        with self.assertRaises(Blocked):self.verify()
    def test_progress_capsule_extra_leaf_or_replacement_fails(self):
        v=self.binding['policy_progress'];self.head=self.commit(self.start,v['capsule']['commit'],extra=('unregistered',b'x'))
        with self.assertRaises(Blocked):self.verify()
        self.head=self.commit(self.progress,v['state']['commit'],extra=(v['capsule']['path'],b'changed'))
        with self.assertRaises(Blocked):self.verify()
    def test_policy_before_new_capsule_or_two_policy_changes_fails(self):
        self.head=self.commit(self.new,self.head)
        with self.assertRaises(Blocked):self.verify()
        v=self.binding['policy_progress'];cap=self.commit(self.progress,v['mirror']['commit'],extra=(self.capsule_path,self.capsule));a=self.commit(self.new,cap)
        b=self.commit(self.progress,a);self.head=self.commit(self.new,b)
        with self.assertRaises(Blocked):self.verify()

class RuntimeEmitterContract(unittest.TestCase):
    def test_original_emitter_never_rebound_to_callback(self):
        tree=ast.parse((SCRIPTS/'get_notion_runtime.py').read_text());run=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='run')
        assignments=[n for n in ast.walk(run)if isinstance(n,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='confirm'for t in n.targets)]
        self.assertEqual(len(assignments),2)
        self.assertTrue(any(isinstance(n.value,ast.Lambda)for n in assignments))
        self.assertTrue(any(isinstance(n.value,ast.Call)and isinstance(n.value.func,ast.Attribute)and n.value.func.attr=='CheckpointEmitter'for n in assignments))
        journal=[n for n in ast.walk(run)if isinstance(n,ast.Call)and isinstance(n.func,ast.Attribute)and n.func.attr=='RemoteJournal']
        self.assertEqual(len(journal),1);self.assertEqual(ast.unparse(next(k.value for k in journal[0].keywords if k.arg=='confirm')),'journal_confirm')
        for name in ('capture_group','apply_group'):
            calls=[n for n in ast.walk(run)if isinstance(n,ast.Call)and isinstance(n.func,ast.Attribute)and n.func.attr==name]
            self.assertEqual(len(calls),1);self.assertEqual(ast.unparse(next(k.value for k in calls[0].keywords if k.arg=='emitter')),'confirm')
        calls=[n for n in ast.walk(run)if isinstance(n,ast.Call)and isinstance(n.func,ast.Attribute)and n.func.attr=='Admission']
        self.assertEqual(len(calls),1);self.assertEqual(ast.unparse(calls[0].args[3]),'confirm')
    def test_callback_is_distinct_only_for_exact_bridge(self):
        tree=ast.parse((SCRIPTS/'get_notion_runtime.py').read_text());run=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='run')
        assigned=[n for n in ast.walk(run)if isinstance(n,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='journal_confirm'for t in n.targets)]
        self.assertEqual(sorted(ast.unparse(n.value)for n in assigned),['confirm','remote.remote.confirm'])
        branches=[n for n in ast.walk(run)if isinstance(n,ast.If)and 'clean_active.TAG' in ast.unparse(n.test)]
        self.assertEqual(sum('journal_confirm = remote.remote.confirm' in ast.unparse(n)for n in branches),1)

class OriginalEmitterAdmission(Fixture):
    def setup_emitter(self):
        import io
        from unittest.mock import patch
        from test_notion_exact_ack_recovery import checked
        journal=module('get_notion_remote_journal',checked=checked,codec=self.codec,MAX_STATE=32*1024*1024,
            REF='refs/heads/main',REPOSITORY='synthetic/repo',STATE_PATH='.sync/get-notion/state.json',RECEIPT_CONTRACT='synthetic-receipt')
        patcher=patch.dict(sys.modules,{'get_notion_remote_journal':journal});patcher.start();self.addCleanup(patcher.stop)
        ns={'journal':journal,'codec':self.codec,'context':lambda c:c,'fail':fail}
        extract('get_notion_journal_runtime.py',{'CheckpointEmitter','RECEIPT_KEYS','MAX_CHECKPOINTS','CONTRACT','MARKER'},ns)
        emitter=ns['CheckpointEmitter'](io.StringIO(),context=deepcopy(self.auth['checkpoint']))
        class Remote:
            head='a'*40
            def ref(self,name):return self.head
            def cas_main(self,parent,commit):self.head=commit
        remote=Remote();finite=self.bridge.FiniteHeadRemote(remote,remote.head,emitter)
        def receipt(commit,parent,count):
            return {'contract':journal.RECEIPT_CONTRACT,'repository':journal.REPOSITORY,'ref':journal.REF,'path':journal.STATE_PATH,
                'commit':commit,'parent_commit':parent,'tree':'c'*40,'state_blob':oid(encoded(self.new)),'state_sha256':digest(self.new),
                'state_bytes':len(encoded(self.new)),'cas_attempts':count,'readback_verified':True}
        self.assertTrue(finite.confirm(receipt('a'*40,None,0)))
        finite.cas_main('a'*40,'b'*40);self.assertTrue(finite.confirm(receipt('b'*40,'a'*40,1)))
        self.assertEqual(emitter.sequence,2);self.assertIs(finite.callback,emitter)
        self.engine.MAX_WRITE_BLOCKS=100;self.engine.p.RENDERER='synthetic'
        plan=deepcopy(self.new['engine_state']['journal']['plan'])
        for row in plan['records']:row['rendered']=[]
        state=deepcopy(self.new);state['engine_state']['journal']['plan']=plan;state['engine_state']['journal']['plan_sha256']=digest(plan)
        durable=NS(poisoned=False,new_blob_bytes=0,max_batch_bytes=64*1024*1024,commits_attempted=1,max_commits=128)
        return emitter,finite,durable,state,plan
    def test_original_emitter_sequence_two_admits_and_callback_method_reproduces_bug(self):
        import get_notion_single_create_admission as admission
        emitter,finite,durable,state,plan=self.setup_emitter()
        real=admission.Admission(self.engine,NS(MAX_BYTES=10000),durable,emitter,max_checkpoints=1024,max_state=32*1024*1024)
        self.assertTrue(real(state,plan))
        wrong=admission.Admission(self.engine,NS(MAX_BYTES=10000),durable,finite.confirm,max_checkpoints=1024,max_state=32*1024*1024)
        with self.assertRaisesRegex(Blocked,'approval_missing'):wrong(state,plan)
    def test_real_count_budget_and_failed_flag_not_hidden(self):
        import get_notion_single_create_admission as admission
        emitter,finite,durable,state,plan=self.setup_emitter()
        real=admission.Admission(self.engine,NS(MAX_BYTES=10000),durable,emitter,max_checkpoints=3,max_state=32*1024*1024)
        self.assertFalse(real(state,plan));self.assertTrue(real.clean_boundary_denied)
        emitter.failed=True
        with self.assertRaisesRegex(Blocked,'approval_missing'):real(state,plan)

class ExactTimestampMirror(RealGitFixture):
    def test_exact_timestamp_mirror_returns_original208_anchor(self):
        self.assertEqual(self.verify(),self.binding['policy_progress']['proof']['anchor'])
    def test_same_payload_another_commit_and_second_mirror_rejected(self):
        m=self.binding['policy_progress']['mirror']
        self.head=self.git('commit-tree',m['after_tree'],'-p',m['parent'],'-m','another commit').decode().strip()
        with self.assertRaises(Blocked):self.verify()
        self.head=self.commit(self.progress,m['commit'],extra=(MIRROR_PATH,encoded({'generated_at':'2026-01-01T03:00:00+00:00','preserved':['synthetic']})))
        with self.assertRaises(Blocked):self.verify()
    def test_exact_mirror_pin_parent_tree_mode_path_and_leaf(self):
        good=deepcopy(self.binding['policy_progress'])
        for key,value in [('commit','f'*40),('parent','f'*40),('before_tree','f'*40),('after_tree','f'*40),('path','unrelated'),('mode','100755')]:
            self.binding['policy_progress']=deepcopy(good);self.binding['policy_progress']['mirror'][key]=value
            self.ns['POLICY_PROGRESS_SCOPE_SHA256']=digest(self.binding['policy_progress'])
            with self.subTest(key=key),self.assertRaises(Blocked):self.verify()
        self.binding['policy_progress']=good;self.ns['POLICY_PROGRESS_SCOPE_SHA256']=digest(good)
    def test_extra_leaf_or_state_change_cannot_hide_in_mirror(self):
        m=self.binding['policy_progress']['mirror']
        self.head=self.commit(self.progress,m['commit'],extra=('unknown',b'x'))
        with self.assertRaises(Blocked):self.verify()
        self.head=self.commit(self.new,m['commit'])
        with self.assertRaises(Blocked):self.verify()
    def test_exact_transport_bytes_required_even_when_local_blob_exists(self):
        old=self.remote.blob;m=self.binding['policy_progress']['mirror'];self.remote.blob=lambda sha,size:b'wrong' if sha==m['after']['blob'] else old(sha,size)
        with self.assertRaises(Blocked):self.verify()
    def test_timestamp_semantics_without_scope_shortcut(self):
        import json
        m=self.binding['policy_progress']['mirror']
        cases=[{'generated_at':'2026-01-01T02:00:00+00:00','preserved':['changed']},
               {'generated_at':'2026-01-01T02:00:00+00:00','preserved':['synthetic'],'extra':True},
               {'generated_at':'2026-01-01T00:00:00+00:00','preserved':['synthetic']},
               {'generated_at':'2026-01-01T02:00:00','preserved':['synthetic']},
               {'generated_at':'invalid','preserved':['synthetic']}]
        for value in cases:
            raw=encoded(value);pin=deepcopy(m);pin['after']={'blob':oid(raw),'sha256':digest(raw),'bytes':len(raw)}
            remote=NS(blob=lambda sha,size:raw if sha==oid(raw) else MIRROR_BEFORE)
            with self.assertRaises(Blocked):self.bridge._verify_exact_mirror_delta(remote,pin)
