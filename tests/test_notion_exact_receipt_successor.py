"""Synthetic bounded adapter tests; no private identities/capsule/network.

Actual data replay is separate private evidence. Native adapters here cannot be
mistaken for live REST readback. Original acknowledgment tests remain unchanged.
"""
import ast
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from test_notion_exact_ack_recovery import (Adapters,Blocked,encoded,digest,oid,
    extract,module,record_creation,fail,SCRIPTS)

class ReceiptAdapters(Adapters):
    def setUp(self):
        super().setUp()
        _,done=self.complete();done['policy_sha256']=self.ack['policy_sha256']
        self.before=deepcopy(self.ack);self.done=done
        b=self.binding;b.pop('acknowledged_successor')
        b['operation_sha256']=digest(self.before['engine_state']['journal']['operation'])
        cp=b['proof']['checkpoint'];cp.update(state_blob=oid(encoded(self.before)),state_sha256=digest(self.before),state_bytes=len(encoded(self.before)))
        cp['record_sha256']=digest({k:v for k,v in cp.items() if k!='record_sha256'})
        b['proof']['anchor']={k:cp[k] for k in ('commit','state_blob','state_sha256')}
        b['receipt_successor']={'contract':'get-notion-exact-persisted-create-receipt-1',
            'anchor':{'commit':'e'*40,'state_blob':oid(encoded(done)),'state_sha256':digest(done)},
            'tree':'f'*40,'state_bytes':len(encoded(done))}
        ns=self.cr.registry.__globals__
        extract('get_notion_create_resume.py',{'RECEIPT_CONTRACT','verify_receipt_successor_state'},ns)
        self.cr.__dict__.update({k:v for k,v in ns.items() if not k.startswith('__')})
        self.auth={**b['proof'],'recovery_anchor':self.cr.TAG}
        self.transitioned=self.cr.transition_outer(done,digest(done),'0'*64,proof=self.auth)
        self.native={'e':self.engine,'deepcopy':deepcopy}
        extract('get_notion_historical_runtime.py',{'verify_persisted_creation_receipt'},self.native)
        self.snapshot={'actual':'fresh-native'};self.native_calls=[]
        def read_target(api,ident,ds):self.native_calls.append('read_target');return self.snapshot
        def created(snapshot,row,ds,page):self.native_calls.append('created');return ['3'*32]
        def receipt(snapshot,ident,ds,managed,publication,source,assets):
            self.native_calls.append('receipt');self.assertIs(snapshot,self.snapshot)
            result=deepcopy(self.done['engine_state']['receipts'][ident]);result.pop('creation_plan_sha256');return result
        self.engine.read_target=read_target;self.engine.created=created;self.engine.receipt=receipt
    def verify_native(self,current=None):
        return self.native['verify_persisted_creation_receipt'](self.transitioned if current is None else current,object())

class SchemaAndState(ReceiptAdapters):
    def test_original_checkpoint_and_failed_outcome_are_unchanged(self):
        self.assertEqual(self.cr.registry(),[self.binding])
        self.assertEqual(self.auth['anchor']['state_sha256'],digest(self.before))
        self.assertEqual(self.auth['checkpoint_count'],1)
        self.assertEqual(self.auth['terminal_report']['write_state'],'unknown')
        self.assertNotEqual(self.auth['anchor'],self.binding['receipt_successor']['anchor'])
    def test_strict_schema_rejects_combined_capability_and_extra_fields(self):
        for mutate in [lambda b:b.update(acknowledged_successor={}),
            lambda b:b['receipt_successor'].update(extra=True),lambda b:b['receipt_successor'].update(contract='other'),
            lambda b:b['receipt_successor'].update(state_bytes=True),lambda b:b['receipt_successor'].update(tree='bad'),
            lambda b:b['receipt_successor']['anchor'].update(commit=b['proof']['anchor']['commit']),
            lambda b:b.update(record_count=1)]:
            with self.subTest(change=mutate):
                bad=deepcopy(self.binding);mutate(bad);self.entries=[bad]
                with self.assertRaises(Blocked):self.cr.registry()
        self.entries=[self.binding]
    def test_only_exact_clean_index_and_plan_allowed(self):
        j=self.done['engine_state']['journal'];self.assertTrue(self.cr.allows(j['plan'],j))
        self.assertFalse(self.cr.allows(self.before['engine_state']['journal']['plan'],self.before['engine_state']['journal']))
        for mutate in [lambda x:x.update(index=0),lambda x:x.update(index=2),
            lambda x:x.update(operation=deepcopy(self.before['engine_state']['journal']['operation'])),
            lambda x:x['plan'].update(extra=True)]:
            bad=deepcopy(j);mutate(bad);self.assertFalse(self.cr.allows(bad['plan'],bad))
    def test_exact_receipt_transition_and_policy_only_cas(self):
        self.cr.verify_receipt_successor_state(self.before,self.done,self.binding)
        self.cr.preserve(self.done,self.transitioned)
        self.assertEqual(self.done['engine_state'],self.transitioned['engine_state'])
        self.assertTrue(self.cr.initial_or_transitioned_state_valid(self.transitioned,digest(self.transitioned)))
    def test_reject_state_policy_plan_target_or_extra_diff(self):
        for mutate in [lambda x:x.update(policy_sha256='e'*64),lambda x:x['preserved'].append('change'),
            lambda x:x['engine_state']['journal']['plan'].update(extra=True),
            lambda x:x['engine_state']['receipts'][self.binding['note_id']].update(page_id='4'*32),
            lambda x:x['engine_state']['property_receipts'].clear(),
            lambda x:x['engine_state']['failures'].update(extra={}),
            lambda x:x['engine_state']['receipts'].pop(self.binding['note_id'])]:
            bad=deepcopy(self.done);mutate(bad)
            self.assertFalse(self.cr.initial_state_valid(bad,digest(bad)))
            with self.assertRaises(Blocked):self.cr.verify_receipt_successor_state(self.before,bad,self.binding)
    def test_reject_wrong_acknowledged_operation(self):
        for mutate in [lambda o:o.update(phase='in_flight'),lambda o:o.update(response='4'*32),lambda o:o.update(archive_index=True)]:
            bad=deepcopy(self.before);mutate(bad['engine_state']['journal']['operation'])
            with self.assertRaises(Blocked):self.cr.verify_receipt_successor_state(bad,self.done,self.binding)
    def test_no_remaining_row_or_receipt_rewrite(self):
        bad=deepcopy(self.transitioned);bad['engine_state']['journal']['index']=2
        with self.assertRaises(Blocked):self.cr.preserve(self.transitioned,bad)
        bad=deepcopy(self.transitioned);bad['engine_state']['receipts'][self.binding['note_id']]['page_id']='4'*32
        with self.assertRaises(Blocked):self.cr.preserve(self.transitioned,bad)
    def test_clean_yield_clears_only_journal_preserving_receipts(self):
        clear=deepcopy(self.transitioned);clear['engine_state']['journal']=None
        self.cr.preserve(self.transitioned,clear)
        for mutate in [lambda s:s['engine_state']['receipts'].clear(),lambda s:s['preserved'].clear()]:
            bad=deepcopy(clear);mutate(bad)
            with self.assertRaises(Blocked):self.cr.preserve(self.transitioned,bad)
    def test_wrong_policy_or_proof_denied(self):
        with self.assertRaises(Blocked):self.cr.transition_outer(self.done,digest(self.done),'e'*64,proof=self.auth)
        wrong=deepcopy(self.auth);wrong['checkpoint_count']+=1
        with self.assertRaises(Blocked):self.cr.transition_outer(self.done,digest(self.done),'0'*64,proof=wrong)

class NativeEvidence(ReceiptAdapters):
    def test_original_native_read_created_and_full_receipt_are_required(self):
        self.assertIs(self.verify_native(),True)
        self.assertEqual(self.native_calls,['read_target','created','receipt'])
    def test_missing_native_readback_and_incomplete_native_denied(self):
        for name in ('read_target','created','receipt'):
            original=getattr(self.engine,name)
            def denied(*a):raise Blocked('native_incomplete')
            setattr(self.engine,name,denied)
            with self.subTest(stage=name),self.assertRaisesRegex(Blocked,'native_incomplete'):self.verify_native()
            setattr(self.engine,name,original)
    def test_changed_native_block_property_or_ownership_denied(self):
        original=self.engine.receipt
        for key,value in [('managed_blocks',[{'id':'5'*32,'normalized_hash':'6'*64}]),('properties_baseline',{'edited':True}),('protected_blocks',[{'id':'7'*32}]),('page_id','8'*32)]:
            def changed(*args,key=key,value=value):
                result=original(*args);result[key]=value;return result
            self.engine.receipt=changed
            with self.subTest(key=key),self.assertRaisesRegex(Blocked,'human_edit_conflict'):self.verify_native()
        self.engine.receipt=original
    def test_unregistered_or_mutated_state_not_native_authority(self):
        bad=deepcopy(self.transitioned);bad['engine_state']['journal']['index']=0
        with self.assertRaises(Blocked):self.verify_native(bad)
        self.entries=[]
        with self.assertRaises(Blocked):self.verify_native()
    def test_native_gate_precedes_policy_transition_in_runtime(self):
        tree=ast.parse((SCRIPTS/'get_notion_runtime.py').read_text())
        candidates=[n for n in ast.walk(tree) if isinstance(n,ast.If) and 'found.get(\'recovery_anchor\') == create_resume.TAG' in ast.unparse(n.test)]
        bridge=next(n for n in candidates if 'verify_active_transition' in ast.unparse(n))
        calls=[ast.unparse(n.func) for n in ast.walk(bridge) if isinstance(n,ast.Call)]
        text=ast.unparse(bridge)
        self.assertLess(text.index('verify_active_transition'),text.index('transition_outer'))
        self.assertLess(text.index('transition_outer'),text.index('confirmed_compare_and_set'))
    def test_existing_old_rule_gate_delegates_without_new_compatibility(self):
        text=(SCRIPTS/'get_notion_authenticated_recovery.py').read_text()
        self.assertIn('return create_resume.allows(plan, journal)',text)
        for path in ('get_notion_fixed_source.py','get_notion_executor.py','get_notion_historical_runtime.py'):
            self.assertIn('authenticated_recovery.allows', (SCRIPTS/path).read_text())

class DispatchBoundary(ReceiptAdapters):
    def test_dispatch_has_empty_write_allowlist_and_only_clean_clear(self):
        tree=ast.parse((SCRIPTS/'get_notion_historical_runtime.py').read_text())
        dispatch=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='dispatch')
        apply=next(n for n in dispatch.body if isinstance(n,ast.If) and isinstance(n.test,ast.Name) and n.test.id=='apply')
        start=next(i for i,n in enumerate(apply.body) if isinstance(n,ast.Import) and n.names[0].name=='get_notion_create_resume')
        end=next(i for i,n in enumerate(apply.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='safe' for t in n.targets))
        body=deepcopy(apply.body[start:end]);body.append(ast.Return(value=ast.Name(id='result',ctx=ast.Load())))
        fn=ast.FunctionDef(name='dispatch_slice',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),body=body,decorator_list=[])
        owner=self
        class Store:
            def __init__(self):self.outer=deepcopy(owner.transitioned);self.calls=[]
            def read(self):s=deepcopy(self.outer['engine_state']);return s,digest(s)
            def compare_and_set(self,version,value):
                if version!=digest(self.outer['engine_state']):fail('state_changed')
                wanted=deepcopy(self.outer);wanted['engine_state']=deepcopy(value);owner.cr.preserve(self.outer,wanted)
                self.calls.append('clear' if value['journal'] is None else 'unexpected');self.outer=wanted;return digest(value)
        adapter=Store();plan=deepcopy(adapter.outer['engine_state']['journal']['plan']);api=NS(approved={'forbidden'});request=NS(approved={'forbidden'})
        def execute(plan,approved,publication,api,store,**kw):
            self.assertEqual(api.approved,frozenset());self.assertEqual(request.approved,frozenset());self.assertIsNone(kw['authorize_batch'])
            state,sha=store.read();self.assertIs(kw['authorize_row'](state,plan),False)
            return {'status':'partial','reason':'batch_boundary_yield','state_sha256':sha,'external_write_attempts':0}
        def forbidden(*args):raise AssertionError('unexpected native write or row authorization')
        self.engine.execute=execute
        ns={'e':self.engine,'deepcopy':deepcopy,'plan':plan,'api':api,'request':request,'adapter':adapter,
            'current':deepcopy(adapter.outer),'active':deepcopy(adapter.outer['engine_state']['journal']),
            'manifest':plan['historical_manifest'],'expected':plan['historical_manifest_sha256'],'sha':digest(adapter.outer['engine_state']),
            'publication':object(),'writers_guard':lambda:True,'row_admit':forbidden,'settle_row_yield':None,
            'verify_persisted_creation_receipt':self.native['verify_persisted_creation_receipt'],
            'creation_recovery_for':lambda *args:None,'pipeline':NS(wire_allowlist=forbidden,batch_authorizer=forbidden)}
        settler={'deepcopy':deepcopy};extract('get_notion_single_create_admission.py',{'settle_clean_yield'},settler)
        with patch.dict(sys.modules,{'get_notion_single_create_admission':module('get_notion_single_create_admission',**settler)}):
            exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'dispatch','exec'),ns)
            result=ns['dispatch_slice']()
        self.assertEqual(self.native_calls,['read_target','created','receipt'])
        self.assertEqual(adapter.calls,['clear']);self.assertIsNone(adapter.outer['engine_state']['journal'])
        self.assertEqual(adapter.outer['engine_state']['receipts'],self.done['engine_state']['receipts'])
        self.assertEqual(result['external_write_attempts'],0)

class RealGitSuccessor(ReceiptAdapters):
    """Original verifier over local synthetic Git objects, no network mocks."""
    def setUp(self):
        super().setUp()
        import tempfile,subprocess,hashlib
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.gitroot=root
        def run(*args,data=None):
            return subprocess.run(['git','-C',str(root),*args],input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
        self.git=run;run('init','-q');run('config','user.email','synthetic@example.invalid');run('config','user.name','Synthetic')
        self.state_path='.sync/get-notion/state.json'
        self.commit_before=self.commit(self.before)
        self.commit_after=self.commit(self.done,parent=self.commit_before)
        b=self.binding;cp=b['proof']['checkpoint'];cp['commit']=self.commit_before;cp['tree']=run('rev-parse',self.commit_before+'^{tree}').decode().strip()
        cp['record_sha256']=digest({k:v for k,v in cp.items() if k!='record_sha256'})
        b['proof']['anchor']['commit']=self.commit_before
        b['receipt_successor']['anchor']['commit']=self.commit_after
        b['receipt_successor']['tree']=run('rev-parse',self.commit_after+'^{tree}').decode().strip()
        self.auth={**b['proof'],'recovery_anchor':self.cr.TAG}
        self.head=self.commit_after
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
        tree=ast.parse((SCRIPTS/'get_notion_remote_journal.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='RemoteJournal')
        method=deepcopy(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_local_tree'))
        from test_notion_exact_ack_recovery import checked
        ns={'hashlib':hashlib,'checked':checked,'MAX_BATCH_BYTES':32*1024*1024,
            'JournalFailure':lambda *a,**kw:Blocked('invalid_git')}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'local_tree','exec'),ns)
        self.journal=module('get_notion_remote_journal',REF='refs/heads/main',STATE_PATH=self.state_path,
            retention=NS(LocalGit=Git,oid=oid,strict_json=__import__('json').loads),RemoteJournal=NS(_local_tree=ns['_local_tree']))
        self.patchgit=patch.dict(sys.modules,{'get_notion_remote_journal':self.journal});self.patchgit.start();self.addCleanup(self.patchgit.stop)
        ns=self.cr.registry.__globals__;extract('get_notion_create_resume.py',{'verify_successor','_verify_acknowledged_successor'},ns)
        self.cr.__dict__.update({k:v for k,v in ns.items() if not k.startswith('__')})
    def commit(self,state,parent=None,extra=None):
        run=self.git
        run('read-tree','--empty') if parent is None else run('read-tree',parent)
        blob=run('hash-object','-w','--stdin',data=encoded(state)).decode().strip()
        run('update-index','--add','--cacheinfo','100644,'+blob+','+self.state_path)
        if extra:
            path,raw=extra;blob=run('hash-object','-w','--stdin',data=raw).decode().strip()
            run('update-index','--add','--cacheinfo','100644,'+blob+','+path)
        tree=run('write-tree').decode().strip()
        return run('commit-tree',tree,*(['-p',parent] if parent else []),'-m','Synthetic test').decode().strip()
    def verify(self):return self.cr.verify_successor(NS(vault=self.gitroot),self.remote,lambda sha:None,self.head,self.auth)
    def test_actual_git_single_child_state_only_is_required(self):
        result=self.verify();self.assertEqual(result,self.binding['receipt_successor']['anchor'])
        self.assertEqual(self.auth['anchor']['commit'],self.commit_before)
    def test_non_state_diff_in_exact_child_is_rejected(self):
        changed=self.commit(self.done,parent=self.commit_before,extra=('unrelated.txt',b'extra'))
        self.head=changed;self.binding['receipt_successor']['anchor']['commit']=changed
        self.binding['receipt_successor']['tree']=self.git('rev-parse',changed+'^{tree}').decode().strip()
        with self.assertRaises(Blocked):self.verify()
    def test_remote_tree_parent_and_ref_mismatch_rejected(self):
        original=self.remote.commit
        for field,value in [('tree','0'*40),('parents',[])]:
            def changed(sha,field=field,value=value):
                result=original(sha)
                if sha==self.commit_after:result[field]=value
                return result
            self.remote.commit=changed
            with self.assertRaises(Blocked):self.verify()
        self.remote.commit=original;self.remote.ref=lambda ref:'0'*40
        with self.assertRaises(Blocked):self.verify()
    def test_only_one_exact_policy_successor_can_be_adopted(self):
        self.head=self.commit(self.transitioned,parent=self.commit_after)
        anchor=self.verify();self.assertEqual(anchor['commit'],self.head)
        self.assertEqual(anchor['state_sha256'],digest(self.transitioned))
        bad=deepcopy(self.transitioned);bad['preserved'].append('change')
        first=self.commit(bad,parent=self.head);self.head=self.commit(self.transitioned,parent=first)
        with self.assertRaises(Blocked):self.verify()
    def test_get_source_descendant_rejected(self):
        self.head=self.commit(self.done,parent=self.commit_after,extra=('Raw/03 Get/changed.md',b'synthetic'))
        with self.assertRaises(Blocked):self.verify()
    def test_receipt_rewrite_and_restore_descendant_rejected(self):
        bad=deepcopy(self.done);bad['engine_state']['receipts'][self.binding['note_id']]['page_id']='6'*32
        changed=self.commit(bad,parent=self.commit_after);self.head=self.commit(self.done,parent=changed)
        with self.assertRaises(Blocked):self.verify()

if __name__=='__main__':unittest.main()
