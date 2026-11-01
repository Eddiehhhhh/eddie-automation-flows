"""Explicitly disabled registries retain denial fences; no private inputs or API."""
import ast
from copy import deepcopy
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'

def fail(reason):raise ValueError(reason)

def functions(filename,names,values):
    tree=ast.parse((SCRIPTS/filename).read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SCRIPTS/filename),'exec'),values)
    return values

class DisabledHistory(unittest.TestCase):
    def setUp(self):
        self.entries=[]
        self.codec=types.SimpleNamespace(fail=fail)
        self.journal=types.SimpleNamespace(fail=fail)
        self.modules={'get_notion_outbox':self.codec,'get_notion_journal_runtime':self.journal,
            'get_notion_executor':types.SimpleNamespace(digest=lambda value:'synthetic'),
            'get_notion_idle_recovery':types.SimpleNamespace(),
            'get_publication_provenance':types.SimpleNamespace()}
        self.patch=patch.dict(sys.modules,self.modules);self.patch.start();self.addCleanup(self.patch.stop)
        self.bindings=types.SimpleNamespace(value=lambda *args:deepcopy(self.entries))

    def registry(self,filename,names):
        ns={'bindings':self.bindings,'deepcopy':deepcopy}
        return functions(filename,{'registry',*names},ns)

    def test_clean_disabled_is_no_policy_or_discovery_admission(self):
        ns=self.registry('get_notion_clean_active_resume.py',{'discovery_locator','candidate_policy','run_binding'})
        self.assertEqual(ns['registry'](),[])
        ns['run_binding']=lambda *args:None
        policy={'synthetic':'unchanged'}
        self.assertEqual(ns['candidate_policy'](policy,{}),(policy,False))
        self.assertIsNone(ns['discovery_locator'](policy))

    def test_checkpoint_all_optional_callers_decline_when_disabled(self):
        names={'run_binding','plan_binding','discovery_locator','source_candidate_policy',
            'source_discovery_locator','verify_source_record','negative_binding'}
        ns=self.registry('get_notion_checkpoint_resume.py',names)
        self.assertIsNone(ns['run_binding'](123,1));self.assertIsNone(ns['plan_binding']({}))
        self.assertIsNone(ns['discovery_locator']({}));self.assertIsNone(ns['source_discovery_locator']({}))
        policy={'synthetic':'unchanged'}
        self.assertEqual(ns['source_candidate_policy'](policy,{}),(policy,False))
        self.assertFalse(ns['verify_source_record']({},1,job={},run={}))
        self.assertIsNone(ns['negative_binding']({},{}))

    def test_disabled_negative_proofs_cannot_be_used_as_absence(self):
        ns=self.registry('get_notion_checkpoint_resume.py',{'check_negative_observation','require_missing_negative'})
        with self.assertRaisesRegex(ValueError,'journal_provenance_changed'):ns['check_negative_observation']({}, {}, {}, {}, b'')
        with self.assertRaisesRegex(ValueError,'journal_provenance_changed'):ns['require_missing_negative'](None,{}, {})

    def test_malformed_registries_do_not_become_disabled(self):
        for filename in ['get_notion_clean_active_resume.py','get_notion_checkpoint_resume.py']:
            for value in [None,{},False,[{},{}]]:
                self.entries=value
                with self.assertRaisesRegex(ValueError,'outbox_state_invalid'):self.registry(filename,set())['registry']()

    def hold_namespace(self):
        self.holds={str(1000000000000000001+i):{'contract':'get-notion-exact-uncertain-hold-1',
            'note_id':str(1000000000000000001+i),'page_id':str(i+1)*32,'effects':'unresolved','retry_allowed':False,
            'reason':'uncertain_append','original_proof':'synthetic-'+str(i)}for i in range(3)}
        self.modules['get_notion_historical_active_recovery']=types.SimpleNamespace(inflight_hold=lambda:None)
        self.modules['get_notion_authenticated_recovery']=types.SimpleNamespace(inflight_holds=lambda:[])
        sys.modules.update({k:v for k,v in self.modules.items()if k.startswith('get_notion_')})
        import json
        codec=lambda value:json.dumps(value,sort_keys=True).encode()
        ns={'_private_bindings':types.SimpleNamespace(value=lambda *args:deepcopy(self.holds)),
            'p':types.SimpleNamespace(encoded=codec),'deepcopy':deepcopy}
        return functions('get_notion_plan_recovery.py',{'validate_holds','held','preserve_holds'},ns)

    def test_three_deny_only_holds_preserve_full_original_fields(self):
        ns=self.hold_namespace();engine={'unresolved_by_id':deepcopy(self.holds)}
        self.assertEqual(ns['validate_holds'](engine['unresolved_by_id']),self.holds)
        for ident,row in self.holds.items():
            self.assertTrue(ns['held'](engine,ident))
            self.assertTrue(ns['held']({'unresolved_by_id':{}},ident))
            self.assertTrue(ns['held']({'unresolved_by_id':{}},'other',page=row['page_id']))
            self.assertTrue(ns['held'](engine,'other',page=row['page_id']))
        self.assertFalse(ns['held'](engine,'ordinary-new-note'))
        self.assertEqual(engine['unresolved_by_id'],self.holds)

    def test_hold_removed_changed_or_retry_enabled_rejected(self):
        ns=self.hold_namespace();before={'unresolved_by_id':deepcopy(self.holds)};ident=next(iter(self.holds))
        for mutate in [lambda v:v.pop(ident),lambda v:v[ident].update(retry_allowed=True),lambda v:v[ident].update(original_proof='changed')]:
            changed=deepcopy(before);mutate(changed['unresolved_by_id'])
            with self.assertRaisesRegex(ValueError,'state_invalid'):ns['preserve_holds'](before,changed)

    def test_malformed_deny_pin_is_not_admitted(self):
        ns=self.hold_namespace();self.holds[next(iter(self.holds))]['retry_allowed']=True
        with self.assertRaisesRegex(ValueError,'state_invalid'):ns['validate_holds']({})

    def test_old_png_replacement_disabled_before_any_state_or_transport(self):
        ns=functions('get_notion_png_expired_recovery.py',{'transition'},
            {'_private_bindings':types.SimpleNamespace(value=lambda *args:False),'fail':fail})
        with self.assertRaisesRegex(ValueError,'write_not_authorized'):ns['transition']({},new_policy_sha256='unused',evidence={})


class ReviewedPolicyHandoff(unittest.TestCase):
    def setUp(self):
        import hashlib,json
        self.encode=lambda x:json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
        self.digest=lambda x:hashlib.sha256(x if isinstance(x,bytes)else self.encode(x)).hexdigest()
        self.oid=lambda x:hashlib.sha1(b'blob '+str(len(x)).encode()+b'\0'+x).hexdigest()
        self.state={'policy_sha256':'a'*64,'source_cursor':{'preserve':'all'},'engine_state':{'journal':None,
            'receipts':{'one':{'body':'preserved'},'two':{'body':'also preserved'}},
            'property_receipts':{'one':{'properties':['original']}},'unresolved_by_id':{str(i):{'page_id':str(i),'retry_allowed':False}for i in range(3)}}}
        e=self.state['engine_state'];self.raw=self.encode(self.state)
        self.binding={'anchor':{'commit':'c'*40,'state_blob':self.oid(self.raw),'state_sha256':self.digest(self.raw)},
            'state_bytes':len(self.raw),'old_policy_sha256':'a'*64,'body_receipts':2,'property_receipts':1,'unresolved':3,
            'body_receipts_sha256':self.digest(e['receipts']),'property_receipts_sha256':self.digest(e['property_receipts']),
            'unresolved_sha256':self.digest(e['unresolved_by_id'])}
        self.policy={'get_policy_sha256':'b'*64,'rules_sha256':'c'*64}
        codec=types.SimpleNamespace(check_state=lambda x:x,digest=self.digest,encoded=self.encode,fail=fail)
        mods={'get_notion_outbox':codec,'get_recurring_policy_recovery':types.SimpleNamespace(current_get_policy=lambda:'b'*64),
            'get_private_preview_retention':types.SimpleNamespace(oid=self.oid,strict_json=json.loads),
            'get_recurring_entry':types.SimpleNamespace(rules_sha256=lambda:'c'*64,BOOTSTRAP={},policy_sha256=lambda *args:'b'*64)}
        patcher=patch.dict(sys.modules,mods);patcher.start();self.addCleanup(patcher.stop)
        self.ns=functions('get_notion_idle_recovery.py',{'_reviewed_idle_state','reviewed_baseline_anchor','transition_reviewed_baseline'},
            {'reviewed_baseline':lambda:deepcopy(self.binding),'deepcopy':deepcopy,'REVIEWED_BASELINE_TAG':'reviewed_successor_idle_baseline_v1',
             '_private_bindings':types.SimpleNamespace(value=lambda *args:deepcopy(self.state['engine_state']['unresolved_by_id']))})

    def test_reviewed_handoff_changes_only_outer_policy_not_any_receipt_hold_or_cursor(self):
        old=deepcopy(self.state);after=self.ns['transition_reviewed_baseline'](self.state,self.digest(self.state),'b'*64)
        self.assertEqual(after,{**self.state,'policy_sha256':'b'*64});self.assertEqual(self.state,old)

    def test_baseline_is_new_approval_not_fabricated_github_attempt(self):
        found=self.ns['reviewed_baseline_anchor'](self.raw,self.policy,(None,None))
        self.assertEqual(found['approval_source'],'reviewed_successor_capsule')
        self.assertFalse(found['get_run_success_claim']);self.assertNotIn('checkpoint',found);self.assertNotIn('run_id',found)

    def test_changed_or_already_adopted_state_uses_normal_discovery(self):
        after={**self.state,'policy_sha256':'b'*64}
        self.assertIsNone(self.ns['reviewed_baseline_anchor'](self.encode(after),self.policy,(None,None)))
        changed={**self.state,'source_cursor':{'changed':True}}
        self.assertIsNone(self.ns['reviewed_baseline_anchor'](self.encode(changed),self.policy,(None,None)))

    def test_historical_modes_never_get_baseline_handoff(self):
        self.assertIsNone(self.ns['reviewed_baseline_anchor'](self.raw,self.policy,({},None)))

    def test_wrong_new_policy_or_unapproved_rules_rejected(self):
        with self.assertRaisesRegex(ValueError,'approval_missing'):self.ns['transition_reviewed_baseline'](self.state,self.digest(self.state),'d'*64)
        for key in self.policy:
            with self.assertRaisesRegex(ValueError,'approval_missing'):self.ns['reviewed_baseline_anchor'](self.raw,{**self.policy,key:'d'*64},(None,None))

    def test_active_changed_receipt_deleted_hold_or_changed_old_policy_rejected(self):
        for field in ('journal','receipts','property_receipts','unresolved_by_id','policy_sha256','source_cursor'):
            value=deepcopy(self.state)
            if field in value['engine_state']:value['engine_state'][field]={}
            else:value[field]='changed'
            with self.subTest(field=field),self.assertRaises(ValueError):self.ns['transition_reviewed_baseline'](value,self.digest(value),'b'*64)

class DisabledRotation(unittest.TestCase):
    def test_disabled_prepared_registration_does_not_index_or_enable_locator(self):
        modules={'get_notion_checkpoint_resume':types.SimpleNamespace(registry=lambda:[])}
        with patch.dict(sys.modules,modules):
            ns=functions('get_notion_idle_rotation.py',{'enabled'},{});self.assertFalse(ns['enabled']({}))

class ReviewedMaterialPin(unittest.TestCase):
    def test_only_explicit_self_references_are_normalized(self):
        import hashlib,json,re,tempfile
        encode=lambda v:json.dumps(v,sort_keys=True,separators=(',',':')).encode()
        digest=lambda v:hashlib.sha256(v if isinstance(v,bytes)else encode(v)).hexdigest()
        def checked(v,n):
            if not isinstance(v,str)or re.fullmatch('[0-9a-f]{'+str(n)+'}',v)is None:fail('approval_missing')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);loader=root/'get_private_runtime_bindings.py';other=root/'ordinary.py'
            loader.write_text("TRUSTED_BUNDLE_SHA256 = '"+'a'*64+"'\n");other.write_text('VALUE = 1\n')
            doc={'modules':{'get_notion_idle_recovery':{'REVIEWED_BASELINE':{'type':'dict','value':{
                'target_material_sha256':{'type':'scalar','value':'b'*64},'authority':{'type':'scalar','value':'original'}}}}}}
            engine=types.SimpleNamespace(digest=digest,fail=fail,p=types.SimpleNamespace(hex_hash=checked),git=types.SimpleNamespace(bounded_file=lambda p,maximum:p.read_bytes()))
            mods={'get_notion_executor':engine,'get_private_runtime_bindings':types.SimpleNamespace(encoded=encode)}
            with patch.dict(sys.modules,mods):
                ns=functions('get_notion_idle_recovery.py',{'reviewed_material_sha256'},{'deepcopy':deepcopy,'__file__':str(other)})
                initial=ns['reviewed_material_sha256'](doc)
                loader.write_text("TRUSTED_BUNDLE_SHA256 = '"+'c'*64+"'\n")
                doc['modules']['get_notion_idle_recovery']['REVIEWED_BASELINE']['value']['target_material_sha256']['value']='d'*64
                self.assertEqual(ns['reviewed_material_sha256'](doc),initial)
                other.write_text('VALUE = 2\n');self.assertNotEqual(ns['reviewed_material_sha256'](doc),initial)
                other.write_text('VALUE = 1\n');doc['modules']['get_notion_idle_recovery']['REVIEWED_BASELINE']['value']['authority']['value']='changed'
                self.assertNotEqual(ns['reviewed_material_sha256'](doc),initial)

if __name__=='__main__':unittest.main()
